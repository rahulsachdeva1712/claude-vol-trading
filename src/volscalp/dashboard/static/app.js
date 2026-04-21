// volscalp dashboard — vanilla JS, single file.
(function () {
  'use strict';

  const state = {
    snapshot: null,
    chart: null,
    chartData: { labels: [], datasets: [{ label: 'Cumulative P&L', data: [], tension: 0.25, fill: false, borderColor: '#5aa0ff' }] },
  };

  function $(id) { return document.getElementById(id); }

  function fmtPnl(v) {
    if (v === null || v === undefined) return '—';
    const n = Number(v);
    const sign = n > 0 ? '+' : '';
    return sign + n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
  }

  function cssPnl(v) {
    const n = Number(v);
    if (n > 0) return 'pos';
    if (n < 0) return 'neg';
    return '';
  }

  function renderKpis(snap) {
    const agg = snap.aggregate || {};
    const kpis = [
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
    const html = kpis.map(k =>
      `<div class="kpi"><div class="label">${k.label}</div><div class="value ${k.cls}">${k.value}</div></div>`
    ).join('');
    $('kpis').innerHTML = html;
  }

  function renderCycle(underlying, engine) {
    const el = $('cycle-' + underlying);
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
          <div>Lock: ${engine.locked ? 'YES @ ' + Number(engine.floor).toFixed(2) : 'no'}</div>
        </div>
        <div class="mtm ${mtmCls}">${fmtPnl(mtm)}</div>
      </div>
      <table>
        <thead><tr><th>Leg</th><th>Status</th><th>Entry</th><th>SL</th><th>LTP</th><th>P&L</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="6"><em>no legs yet</em></td></tr>'}</tbody>
      </table>
    `;
  }

  function renderTrades(trades) {
    const tbody = document.querySelector('#trades tbody');
    if (!tbody) return;
    if (!trades || !trades.length) { tbody.innerHTML = '<tr><td colspan="8"><em>no closed trades yet</em></td></tr>'; return; }
    tbody.innerHTML = trades.map(t =>
      `<tr>
        <td>${t.cycle_no}</td>
        <td>${t.underlying}</td>
        <td>${t.started_at ?? ''}</td>
        <td>${t.ended_at ?? ''}</td>
        <td>${t.exit_reason ?? ''}</td>
        <td>${fmtPnl(t.peak_mtm)}</td>
        <td>${fmtPnl(t.trough_mtm)}</td>
        <td class="${cssPnl(t.cycle_pnl)}">${fmtPnl(t.cycle_pnl)}</td>
      </tr>`
    ).join('');
  }

  function renderPnlChart(trades) {
    const ctx = $('pnl-chart').getContext('2d');
    const sorted = (trades || []).slice().reverse();
    const labels = sorted.map(t => '#' + t.cycle_no);
    let cum = 0;
    const data = sorted.map(t => { cum += Number(t.cycle_pnl) || 0; return cum; });
    state.chartData.labels = labels;
    state.chartData.datasets[0].data = data;

    if (!state.chart) {
      state.chart = new Chart(ctx, {
        type: 'line',
        data: state.chartData,
        options: {
          responsive: true,
          animation: false,
          plugins: { legend: { labels: { color: '#e6eaf2' } } },
          scales: {
            x: { ticks: { color: '#8a93a6' }, grid: { color: '#242a36' } },
            y: { ticks: { color: '#8a93a6' }, grid: { color: '#242a36' } },
          },
        },
      });
    } else {
      state.chart.update();
    }
  }

  function renderAll(snap) {
    state.snapshot = snap;
    $('mode-pill').textContent = 'mode: ' + (snap.mode || '?');
    $('mode-select').value = snap.mode || 'paper';
    $('lots-input').value = snap.lots_per_trade || 1;
    renderKpis(snap);
    const engines = snap.engines || {};
    renderCycle('NIFTY', engines.NIFTY);
    renderCycle('BANKNIFTY', engines.BANKNIFTY);
    renderTrades(snap.closed_trades || []);
    renderPnlChart(snap.closed_trades || []);
  }

  async function fetchStatus() {
    try {
      const res = await fetch('/api/status');
      if (res.ok) renderAll(await res.json());
    } catch (e) { /* ignore */ }
  }

  function connectWs() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(proto + '://' + location.host + '/ws');
    const conn = $('conn');
    ws.onopen = () => { conn.textContent = 'WS: connected'; conn.style.background = '#1f3323'; };
    ws.onclose = () => {
      conn.textContent = 'WS: reconnecting…'; conn.style.background = '#331f22';
      setTimeout(connectWs, 1500);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.kind === 'snapshot' && msg.payload) {
          renderAll(msg.payload);
        } else {
          // partial update — re-fetch status for now (cheap, localhost).
          fetchStatus();
        }
      } catch (e) { /* ignore */ }
    };
  }

  document.addEventListener('DOMContentLoaded', () => {
    fetchStatus();
    connectWs();
    setInterval(fetchStatus, 2000); // safety net

    $('apply-config').addEventListener('click', async () => {
      const mode = $('mode-select').value;
      const lots = parseInt($('lots-input').value || '1', 10);
      const body = { lots_per_trade: lots };
      await fetch('/api/config', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
      if (mode !== (state.snapshot && state.snapshot.mode)) {
        if (mode === 'live' && !confirm('Switch to LIVE trading? Real orders will be placed on Dhan.')) return;
        await fetch('/api/mode', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ mode, confirm: true }) });
      }
      fetchStatus();
    });

    $('kill-switch').addEventListener('click', async () => {
      if (!confirm('Kill switch: halts entries and force-closes all positions. Continue?')) return;
      await fetch('/api/kill', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ confirm: true }) });
      fetchStatus();
    });
  });
})();
