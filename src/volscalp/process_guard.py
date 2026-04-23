"""Process hygiene: PID file, stale-process cleanup, signal handlers.

Contract:
    on startup:
        1. Read PID file if present. If the recorded PID is alive and
           looks like a previous volscalp instance, terminate it
           (SIGTERM, wait, SIGKILL fallback).
        2. Scan current user's processes for any stray volscalp workers
           that lost their PID file, and terminate those too.
        3. Write our own PID file atomically.
    on shutdown (SIGINT/SIGTERM/atexit):
        4. Remove the PID file.
        5. Call registered cleanup callbacks (in LIFO order).
"""
from __future__ import annotations

import asyncio
import atexit
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import psutil

from .logging_setup import get_logger

log = get_logger(__name__)

_PROCESS_MARK = "volscalp"  # substring we look for when scanning cmdlines
_shutdown_callbacks: list[Callable[[], Awaitable[None] | None]] = []
_shutdown_requested = asyncio.Event()


def _self_tree_pids() -> set[int]:
    """PIDs we must NEVER terminate: self, ancestors (shell/launcher wrappers),
    descendants (child processes we spawned). Any of these may show up with
    a cmdline containing ``volscalp`` (e.g. a Windows ``py`` launcher that
    forwards the full command-line to the real interpreter)."""
    pids: set[int] = {os.getpid()}
    try:
        me = psutil.Process(os.getpid())
    except psutil.Error:
        return pids
    try:
        pids.update(p.pid for p in me.parents())
    except psutil.Error:
        pass
    try:
        pids.update(c.pid for c in me.children(recursive=True))
    except psutil.Error:
        pass
    return pids


def _is_volscalp_proc(proc: psutil.Process) -> bool:
    """Heuristic: does this process look like a previous volscalp run?"""
    try:
        cmdline = " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if proc.pid == os.getpid():
        return False
    if _PROCESS_MARK not in cmdline.lower():
        return False
    try:
        return proc.username() == psutil.Process(os.getpid()).username()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _terminate(proc: psutil.Process, timeout_s: float = 5.0) -> None:
    """SIGTERM, wait, SIGKILL fallback."""
    if proc.pid == os.getpid():
        log.error("refused_to_terminate_self", pid=proc.pid)
        return
    try:
        log.warning("terminating_stale_process", pid=proc.pid, cmdline=" ".join(proc.cmdline()[:4]))
        proc.terminate()
        try:
            proc.wait(timeout=timeout_s)
            return
        except psutil.TimeoutExpired:
            log.warning("stale_process_did_not_exit_sigkill", pid=proc.pid)
            proc.kill()
            proc.wait(timeout=timeout_s)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        log.warning("stale_process_cleanup_error", pid=proc.pid, error=str(e))


def cleanup_stale_processes(pid_file: Path) -> int:
    """Kill any previous volscalp instances. Returns count of processes killed."""
    killed = 0
    safe_pids = _self_tree_pids()
    log.debug("stale_scan_safe_pids", pids=sorted(safe_pids), self_pid=os.getpid())

    # 1. PID file check.
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            log.warning("pid_file_corrupt_removing", path=str(pid_file))
            pid_file.unlink(missing_ok=True)
            old_pid = None
        if old_pid and old_pid not in safe_pids and psutil.pid_exists(old_pid):
            try:
                proc = psutil.Process(old_pid)
                if _is_volscalp_proc(proc):
                    _terminate(proc)
                    killed += 1
            except psutil.NoSuchProcess:
                pass
        pid_file.unlink(missing_ok=True)

    # 2. Broader scan for orphaned workers — skip self, ancestors, descendants.
    for proc in psutil.process_iter(["pid"]):
        if proc.pid in safe_pids:
            continue
        if _is_volscalp_proc(proc):
            _terminate(proc)
            killed += 1

    if killed:
        log.info("stale_processes_cleaned", count=killed)
    return killed


def write_pid_file(pid_file: Path) -> None:
    """Write our PID atomically."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = pid_file.with_suffix(pid_file.suffix + ".tmp")
    tmp.write_text(str(os.getpid()))
    tmp.replace(pid_file)
    log.info("pid_file_written", path=str(pid_file), pid=os.getpid())


def remove_pid_file(pid_file: Path) -> None:
    try:
        if pid_file.exists():
            # Only remove if it's ours.
            try:
                recorded = int(pid_file.read_text().strip())
            except (ValueError, OSError):
                recorded = -1
            if recorded == os.getpid():
                pid_file.unlink(missing_ok=True)
                log.info("pid_file_removed", path=str(pid_file))
    except Exception as e:  # noqa: BLE001
        log.warning("pid_file_remove_error", error=str(e))


def register_shutdown(callback: Callable[[], Awaitable[None] | None]) -> None:
    """Register a callback to run on graceful shutdown (LIFO order)."""
    _shutdown_callbacks.append(callback)


def request_shutdown() -> None:
    """Flip the shutdown event. Idempotent."""
    if not _shutdown_requested.is_set():
        log.info("shutdown_requested")
        _shutdown_requested.set()


def shutdown_event() -> asyncio.Event:
    return _shutdown_requested


def install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Install SIGINT/SIGTERM handlers that flip the shutdown event."""
    def _handler(sig: int) -> None:
        log.warning("signal_received", signal=signal.Signals(sig).name)
        request_shutdown()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handler, sig)
            except NotImplementedError:
                signal.signal(sig, lambda s, _f: _handler(s))
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda s, _f: _handler(s))


async def run_shutdown_callbacks() -> None:
    """Run all registered shutdown callbacks in reverse order."""
    start = time.monotonic()
    for cb in reversed(_shutdown_callbacks):
        try:
            result = cb()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=10.0)
        except Exception as e:  # noqa: BLE001
            log.warning("shutdown_callback_error", error=str(e))
    log.info("shutdown_callbacks_complete", elapsed_s=round(time.monotonic() - start, 3))


def setup_process_hygiene(pid_file: Path) -> None:
    """One-shot: clean stale, write PID, install atexit."""
    cleanup_stale_processes(pid_file)
    write_pid_file(pid_file)
    atexit.register(remove_pid_file, pid_file)
