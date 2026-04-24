// volscalp dashboard — vanilla JS. Paper + Live tabs, shared rendering.
(function () {
  'use strict';

  const MODES = ['paper', 'live'];
  const UNDERLYINGS = ['NIFTY', 'BANKNIFTY'];

  const state = {
    snapshot: null,
    activeMode: 'paper',
    charts: {},                 // 'pnl-<mode>' | 'equity-<mode>' -> Chart
    chartData: {
      // Today's P&L — cumulative across cycles inside the current session
      'pnl-paper':    { labels: [], datasets: [{ label: "Today's P&L (paper)", data: [], tension: 0.25, fill: false, borderColor: '#5aa0ff' }] },
      'pnl-live':     { labels: [], datasets: [{ label: "Today's P&L (live)",  data: [], tension: 0.25, fill: false, borderColor: '#f2a83b' }] },
      // Lifetime equity curve — one point per trading day
      'equity-paper': { labels: [], datasets: [{ label: 'Cumulative P&L across days (paper)', data: [], tension: 0.25, fill: false, borderColor: '#5aa0ff' }] },
      'equity-live':  { labels: [], datasets: [{ label: 'Cumulative P&L across days (live)',  data: [], tension: 0.25, fill: false, borderColor: '#f2a83b' }] },
    },
  };

  function $(id) { return document.getElementById(id); }
  function fmtPnl(v) {
    if (v === null || v === undefined) return '—';
    const n = Number(v);
    const sign = n > 0 ? '+' : '';
    return sign + n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
  }
  function cssPnl(v) { const n = Number(v); if (n > 0) return 'pos'; if (n < 0) return 'neg'; return ''; }

  function renderKpis(mode, agg) {
    const host = $('kpis-' + mode);
    if (!host) return;
    const items = [
      { label: 'Today MTM', value: fmtPnl(agg.total_mtm), cls: cssPnl(agg.total_mtm) },
      { label: 'Today Realized', value: fmtPnl(agg.realized_pnl), cls: cssPnl(agg.realized_pnl) },
      { label: 'Today Unrealized', value: fmtPnl(agg.unrealized_pnl), cls: cssPnl(agg.unrealized_pnl) },
      { label: 'Cumulative P&L', value: fmtPnl(agg.cumulative_pnl), cls: cssPnl(agg.cumulative_pnl) },
      { label: 'Open Positions', value: agg.open_positions ?? 0, cls: '' },
      { label: 'Closed Trades', value: agg.closed_cycles ?? 0, cls: '' },
      { label: 'Win Rate', value: (agg.win_rate_pct ?? 0).toFixed(1) + '%', cls: '' },
      { label: 'Avg P&L / Trade', value: fmtPnl(agg.avg_pnl_per_trade), cls: cssPnl(agg.avg_pnl_per_trade) },
      { label: 'Profit Factor', value: (agg.profit_factor ?? 0).toFixed(2), cls: '' },
      { label: 'Max Drawdown', value: fmtPnl(agg.max_drawdown), cls: 'neg' },
    ];
    host.innerHTML = items.map(k =>
      `<div class="kpi"><div class="label">${k.label}</div><div class="value ${k.cls}">${k.value}</div></div>`
    ).join('');
  }

  function renderCycle(mode, underlying, engine) {
    const el = $('cycle-' + mode + '-' + underlying);
    if (!el) return;
    if (!engine) { el.innerHTML = '<em>no data</em>'; return; }
    const mtm = Number(engine.mtm ?? 0);
    const mtmCls = mtm > 0 ? 'pos' : (mtm < 0 ? 'neg' : '');
    const legs = engine.legs || [];
    const rows = legs.map(l =>
      `<tr>
        <td>Slot ${l.slot} — ${l.kind} ${l.option_type} ${l.strike}</td>
        <td class="status-${l.status}">${l.status}</td>
        <td>${l.entry_price ? Number(l.entry_price).toFixed(2) : '—'}</td>
        <td>${l.sl ? Number(l.sl).toFixed(2) : '—'}</td>
        <td>${l.ltp ? Number(l.ltp).toFixed(2) : '—'}</td>
        <td class="${cssPnl(l.pnl)}">${fmtPnl(l.pnl)}</td>
      </tr>`
    ).join('');
    el.innerHTML = `
      <div class="cycle-head">
        <div>
          <div><b>Cycle ${engine.cycle_no ?? '—'}</b> · state: ${engine.state ?? '—'}</div>
          <div>ATM: ${engine.atm ?? '—'} · Spot: ${engine.spot ? Number(engine.spot).toFixed(2) : '—'}</div>
        </div>
        <div class="mtm ${mtmCls}">${fmtPnl(mtm)}</div>
      </div>
      <table>
        <thead><tr><th>Leg</th><th>Status</th><th>Entry</th><th>SL</th><th>LTP</th><th>P&L</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="6"><em>no legs yet</em></td></tr>'}</tbody>
      </table>
    `;
  }

  function renderTrades(mode, trades) {
    const tbody = document.querySelector('#trades-' + mode + ' tbody');
    if (!tbody) return;
    if (!trades || !trades.length) {
      tbody.innerHTML = '<tr><td colspan="8"><em>no closed trades yet</em></td></tr>';
      return;
    }
    tbody.innerHTML = trades.map(t =>
      `<tr>
        <td>${t.cycle_no}</td><td>${t.underlying}</td>
        <td>${t.started_at ?? ''}</td><td>${t.ended_at ?? ''}</td>
        <td>${t.exit_reason ?? ''}</td>
        <td>${fmtPnl(t.peak_mtm)}</td><td>${fmtPnl(t.trough_mtm)}</td>
        <td class="${cssPnl(t.cycle_pnl)}">${fmtPnl(t.cycle_pnl)}</td>
      </tr>`
    ).join('');
  }

  // Shared Chart.js options — same look for both charts.
  function chartOptions() {
    return {
      responsive: true,
      animation: false,
      plugins: { legend: { labels: { color: '#e6eaf2' } } },
      scales: {
        x: { ticks: { color: '#8a93a6' }, grid: { color: '#242a36' } },
        y: { ticks: { color: '#8a93a6' }, grid: { color: '#242a36' } },
      },
    };
  }

  // Render or update a line chart by key ('pnl-<mode>' | 'equity-<mode>').
  // Hides the canvas and shows a sibling .chart-empty div when there's no data,
  // so Chart.js never draws an orphan axis.
  function renderLineChart(key, canvasId, emptyId, labels, data) {
    const ctx = $(canvasId);
    const empty = $(emptyId);
    if (!ctx) return;
    const hasData = labels.length > 0 && data.length > 0;
    if (empty) empty.classList.toggle('hidden', hasData);
    ctx.classList.toggle('hidden', !hasData);
    if (!hasData) return;

    state.chartData[key].labels = labels;
    state.chartData[key].datasets[0].data = data;

    if (!state.charts[key]) {
      state.charts[key] = new Chart(ctx.getContext('2d'), {
        type: 'line',
        data: state.chartData[key],
        options: chartOptions(),
      });
    } else {
      state.charts[key].update();
    }
  }

  // Today's P&L — cumulative across cycles inside the current session.
  // `trades` is newest-first from /api/closed_trades (today scope).
  function renderPnlChart(mode, trades) {
    const sorted = (trades || []).slice().reverse();
    const labels = sorted.map(t => '#' + t.cycle_no);
    let cum = 0;
    const data = sorted.map(t => { cum += Number(t.cycle_pnl) || 0; return cum; });
    renderLineChart('pnl-' + mode, 'pnl-chart-' + mode, 'pnl-empty-' + mode, labels, data);
  }

  // Lifetime equity curve — one point per trading day from /api/equity_curve.
  // The rightmost y-value equals the KPI strip's "Cumulative P&L".
  function renderEquityChart(mode, days) {
    const rows = days || [];
    const labels = rows.map(d => d.session_date);
    const data = rows.map(d => Number(d.cum_pnl) || 0);
    renderLineChart('equity-' + mode, 'equity-chart-' + mode, 'equity-empty-' + mode, labels, data);
  }

  function renderLiveControls(snap) {
    const liveTree = (snap.modes || {}).live;
    const hasLive = !!snap.live_available;
    const tab = document.querySelector('.tab[data-mode="live"] .live-pill');
    if (!hasLive) {
      if (tab) { tab.textContent = 'unavailable'; tab.className = 'pill live-pill warn'; }
      $('live-unavailable').classList.remove('hidden');
      return;
    }
    $('live-unavailable').classList.add('hidden');
    const armed = !!(liveTree && liveTree.armed);
    const killed = !!(liveTree && liveTree.kill_switch);
    const statusEl = $('live-status');
    if (killed) { statusEl.textContent = 'KILLED'; statusEl.className = 'pill danger'; }
    else if (armed) { statusEl.textContent = 'ARMED'; statusEl.className = 'pill ok'; }
    else { statusEl.textContent = 'disarmed'; statusEl.className = 'pill'; }
    if (tab) {
      tab.textContent = killed ? 'killed' : (armed ? 'armed' : 'disarmed');
      tab.className = 'pill live-pill ' + (killed ? 'danger' : (armed ? 'ok' : ''));
    }
    $('live-arm').disabled = armed || killed;
    $('live-disarm').disabled = !armed;
    $('live-kill').disabled = killed;
    $('live-kill-clear').disabled = !killed;
  }

  function renderTree(mode, tree) {
    if (!tree) return;
    $('lots-input-' + mode).value = tree.lots_per_trade || 1;
    renderKpis(mode, tree.aggregate || {});
    const engines = tree.engines || {};
    UNDERLYINGS.forEach(u => renderCycle(mode, u, engines[u]));
  }

  function renderAll(snap) {
    state.snapshot = snap;
    const modes = snap.modes || {};
    MODES.forEach(m => renderTree(m, modes[m]));
    renderLiveControls(snap);
  }

  async function fetchStatus() {
    try {
      const res = await fetch('/api/status');
      if (res.ok) renderAll(await res.json());
    } catch (e) { /* ignore */ }
  }

  async function fetchTrades(mode) {
    try {
      const [tradesRes, equityRes] = await Promise.all([
        fetch('/api/closed_trades?mode=' + mode),
        fetch('/api/equity_curve?mode=' + mode),
      ]);
      if (tradesRes.ok) {
        const body = await tradesRes.json();
        renderTrades(mode, body.trades || []);
        renderPnlChart(mode, body.trades || []);
      }
      if (equityRes.ok) {
        const body = await equityRes.json();
        renderEquityChart(mode, body.days || []);
      }
    } catch (e) { /* ignore */ }
  }

  function connectWs() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(proto + '://' + location.host + '/ws');
    const conn = $('conn');
    ws.onopen = () => { conn.textContent = 'WS: connected'; conn.className = 'pill ok'; };
    ws.onclose = () => {
      conn.textContent = 'WS: reconnecting…'; conn.className = 'pill warn';
      setTimeout(connectWs, 1500);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.kind === 'snapshot' && msg.payload) {
          renderAll(msg.payload);
        } else {
          fetchStatus();
        }
      } catch (e) { /* ignore */ }
    };
  }

  function selectTab(mode) {
    state.activeMode = mode;
    document.querySelectorAll('.tab').forEach(btn => {
      const on = btn.dataset.mode === mode;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.pane').forEach(p => p.classList.toggle('hidden', p.id !== 'pane-' + mode));
    fetchTrades(mode);
  }

  async function postJson(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    fetchStatus();
    fetchTrades('paper');
    fetchTrades('live');
    connectWs();
    setInterval(() => { fetchStatus(); fetchTrades(state.activeMode); }, 2500);

    document.querySelectorAll('.tab').forEach(btn =>
      btn.addEventListener('click', () => selectTab(btn.dataset.mode))
    );

    document.querySelectorAll('.apply').forEach(btn => {
      btn.addEventListener('click', async () => {
        const mode = btn.dataset.mode;
        const lots = parseInt($('lots-input-' + mode).value || '1', 10);
        await postJson('/api/config', { mode, lots_per_trade: lots });
        fetchStatus();
      });
    });

    $('live-arm').addEventListener('click', async () => {
      if (!confirm('Arm LIVE trading? Real orders will be placed on Dhan when signals fire.')) return;
      const res = await postJson('/api/live/arm', { confirm: true });
      if (!res.ok) alert('Arm failed: ' + (await res.text()));
      fetchStatus();
    });

    $('live-disarm').addEventListener('click', async () => {
      await postJson('/api/live/disarm');
      fetchStatus();
    });

    $('live-kill').addEventListener('click', async () => {
      if (!confirm('Kill switch: disarms live and force-closes every live position. Continue?')) return;
      await postJson('/api/live/kill', { confirm: true });
      fetchStatus();
    });

    $('live-kill-clear').addEventListener('click', async () => {
      if (!confirm('Clear the kill flag? (This does not re-arm — you still need to click Arm afterwards.)')) return;
      await postJson('/api/live/kill/clear');
      fetchStatus();
    });
  });
})();
