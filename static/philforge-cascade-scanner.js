/* Terminal Cascade instrument scanner.
 *
 * Read-only. It ranks scrips and fills in the campaign form when one is
 * picked; it never starts a campaign or places an order. Kept in its own file
 * so it does not tangle with the rest of the Terminal code.
 */
(function () {
  'use strict';

  var els = {};
  var lastRows = [];

  function $(id) { return document.getElementById(id); }

  function money(value) {
    return '₹' + Number(value || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  }

  function setStatus(text, tone) {
    if (!els.status) return;
    els.status.textContent = text;
    els.status.className = 'cascade-scan-status' + (tone ? ' cascade-scan-status-' + tone : '');
  }

  function rungLabel(count) {
    // The number that decides whether this is really a cascade or just one buy.
    if (count >= 3) return '<span class="cascade-scan-rungs ok">3 of 3</span>';
    if (count === 2) return '<span class="cascade-scan-rungs warn">2 of 3</span>';
    return '<span class="cascade-scan-rungs bad">' + count + ' of 3</span>';
  }

  function render(payload) {
    lastRows = payload.candidates || [];
    if (els.meta) {
      els.meta.textContent =
        lastRows.length + ' of ' + payload.universe + ' scanned' +
        (payload.no_history ? ' · ' + payload.no_history + ' had no history' : '') +
        (payload.cached ? ' · cached' : '');
    }

    if (!lastRows.length) {
      var why = (payload.rejected_sample || [])
        .map(function (row) { return '<li><strong>' + row.symbol + '</strong> — ' + row.reason + '</li>'; })
        .join('');
      els.body.innerHTML =
        '<div class="cascade-scan-empty">' +
        '<p>Nothing qualifies right now.</p>' +
        (why ? '<p class="cascade-scan-empty-note">Why the others were dropped:</p><ul>' + why + '</ul>' : '') +
        '</div>';
      return;
    }

    var rows = lastRows.map(function (row, index) {
      return '' +
        '<tr data-symbol="' + row.symbol + '">' +
        '<td class="cascade-scan-rank">' + (index + 1) + '</td>' +
        '<td><strong>' + row.symbol + '</strong><span class="cascade-scan-name">' + (row.name || '') + '</span></td>' +
        '<td class="num">' + money(row.last_price) + '</td>' +
        '<td class="num cascade-scan-pullback">-' + row.pullback_pct.toFixed(1) + '%</td>' +
        '<td class="num">+' + row.strength_pct.toFixed(1) + '%</td>' +
        '<td class="num">' + row.affordable_shares + '</td>' +
        '<td class="num">' + rungLabel(row.rungs_fundable) + '</td>' +
        '<td class="cascade-scan-row-actions">' +
        '<button type="button" class="cascade-scan-chart-btn" data-symbol="' + row.symbol + '">Chart</button>' +
        '<button type="button" class="cascade-scan-pick" data-symbol="' + row.symbol + '">Use</button>' +
        '</td>' +
        '</tr>';
    }).join('');

    els.body.innerHTML =
      '<div class="cascade-scan-table-wrap"><table class="cascade-scan-table">' +
      '<thead><tr>' +
      '<th>#</th><th>Scrip</th><th class="num">Price</th>' +
      '<th class="num" title="How far below its recent high it is trading">Off high</th>' +
      '<th class="num" title="Trend over the last 60 sessions">Trend</th>' +
      '<th class="num" title="Shares your capital buys">Shares</th>' +
      '<th class="num" title="How many of the three buy levels your capital can reach">Rungs</th>' +
      '<th></th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  /* ── chart ──────────────────────────────────────────────────────
   * Native exchange OHLC, drawn body-and-wick. Same palette as the campaign
   * chart so the two read as one system. Nothing here is smoothed or
   * synthesised: the whole job of this chart is to justify the ranking, and a
   * lookalike series would be justifying something else.
   */

  function palette() {
    var theme = document.documentElement.getAttribute('data-theme');
    if (!theme || theme === 'auto') {
      theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }
    if (theme === 'light') {
      return { bg: '#ffffff', grid: 'rgba(15,23,42,.10)', axis: 'rgba(51,65,85,.75)',
               up: '#0f766e', down: '#be123c', high: '#7c3aed', now: '#334155', zone: 'rgba(190,18,60,.09)' };
    }
    return { bg: '#07101d', grid: 'rgba(148,163,184,.12)', axis: 'rgba(148,163,184,.55)',
             up: '#3fae56', down: '#d9534f', high: '#a855f7', now: '#e2e8f0', zone: 'rgba(217,83,79,.12)' };
  }

  function esc(text) {
    return String(text).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }

  function chartSvg(payload) {
    var PAL = palette();
    var rows = payload.candles || [];
    if (!rows.length) return '<div class="cascade-scan-empty">No candles returned.</div>';

    var W = 1100, H = 300, padL = 12, padR = 74, padT = 12, padB = 24;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var n = rows.length, cw = plotW / Math.max(n, 1);

    var lo = rows[0].l, hi = rows[0].h;
    rows.forEach(function (row) { lo = Math.min(lo, row.l); hi = Math.max(hi, row.h); });
    hi = Math.max(hi, payload.recent_high);
    lo = Math.min(lo, payload.last_price);
    var span = (hi - lo) || 1;
    var maxP = hi + span * 0.06, minP = lo - span * 0.06;

    var X = function (i) { return padL + i * cw + cw / 2; };
    var Y = function (p) { return padT + ((maxP - p) / ((maxP - minP) || 1)) * plotH; };
    var num = function (v) { return Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 }); };
    var day = function (t) {
      return new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short' })
        .format(new Date(t));
    };

    var out = ['<rect x="0" y="0" width="' + W + '" height="' + H + '" fill="' + PAL.bg + '"/>'];

    for (var g = 0; g <= 3; g += 1) {
      var price = minP + (maxP - minP) * (g / 3), y = Y(price);
      out.push('<line x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' + (padL + plotW) + '" y2="' + y.toFixed(1) +
        '" stroke="' + PAL.grid + '" stroke-width="1"/>');
      out.push('<text x="' + (padL + plotW + 6) + '" y="' + (y + 3).toFixed(1) + '" fill="' + PAL.axis +
        '" font-size="10" font-family="monospace">' + num(price) + '</text>');
    }

    // The discount being ranked, shaded between the recent high and now.
    var yHigh = Y(payload.recent_high), yNow = Y(payload.last_price);
    out.push('<rect x="' + padL + '" y="' + yHigh.toFixed(1) + '" width="' + plotW + '" height="' +
      Math.max(0, yNow - yHigh).toFixed(1) + '" fill="' + PAL.zone + '"/>');

    rows.forEach(function (row, i) {
      var up = row.c >= row.o;
      var colour = up ? PAL.up : PAL.down;
      var x = X(i);
      var bodyTop = Y(Math.max(row.o, row.c));
      var bodyH = Math.max(1, Math.abs(Y(row.o) - Y(row.c)));
      var bw = Math.max(1, cw * 0.62);
      out.push('<line x1="' + x.toFixed(1) + '" y1="' + Y(row.h).toFixed(1) + '" x2="' + x.toFixed(1) +
        '" y2="' + Y(row.l).toFixed(1) + '" stroke="' + colour + '" stroke-width="1"/>');
      out.push('<rect x="' + (x - bw / 2).toFixed(1) + '" y="' + bodyTop.toFixed(1) + '" width="' + bw.toFixed(1) +
        '" height="' + bodyH.toFixed(1) + '" fill="' + colour + '"/>');
    });

    out.push('<line x1="' + padL + '" y1="' + yHigh.toFixed(1) + '" x2="' + (padL + plotW) + '" y2="' + yHigh.toFixed(1) +
      '" stroke="' + PAL.high + '" stroke-width="1.25" stroke-dasharray="5 3"/>');
    out.push('<text x="' + (padL + 5) + '" y="' + (yHigh - 4).toFixed(1) + '" fill="' + PAL.high +
      '" font-size="10" font-family="monospace">' + payload.recent_high_lookback + '-session high ' +
      num(payload.recent_high) + '</text>');
    out.push('<line x1="' + padL + '" y1="' + yNow.toFixed(1) + '" x2="' + (padL + plotW) + '" y2="' + yNow.toFixed(1) +
      '" stroke="' + PAL.now + '" stroke-width="1" stroke-dasharray="2 3"/>');
    out.push('<text x="' + (padL + 5) + '" y="' + (yNow + 12).toFixed(1) + '" fill="' + PAL.now +
      '" font-size="10" font-family="monospace">now ' + num(payload.last_price) + '  (-' +
      payload.pullback_pct.toFixed(1) + '%)</text>');

    var ticks = Math.min(6, n);
    for (var t = 0; t < ticks; t += 1) {
      var at = Math.round((n - 1) * (t / Math.max(ticks - 1, 1)));
      out.push('<text x="' + X(at).toFixed(1) + '" y="' + (H - 7) + '" fill="' + PAL.axis +
        '" font-size="10" font-family="monospace" text-anchor="middle">' + esc(day(rows[at].t)) + '</text>');
    }

    return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet" ' +
      'class="cascade-scan-chart-svg" role="img" aria-label="Daily candles for ' + esc(payload.symbol) + '">' +
      out.join('') + '</svg>';
  }

  async function toggleChart(symbol, row) {
    var existing = row.nextElementSibling;
    if (existing && existing.classList.contains('cascade-scan-chart-row')) { existing.remove(); return; }
    document.querySelectorAll('.cascade-scan-chart-row').forEach(function (node) { node.remove(); });

    var holder = document.createElement('tr');
    holder.className = 'cascade-scan-chart-row';
    holder.innerHTML = '<td colspan="8"><div class="cascade-scan-chart">Loading ' + esc(symbol) + ' daily candles…</div></td>';
    row.parentNode.insertBefore(holder, row.nextSibling);
    var box = holder.querySelector('.cascade-scan-chart');
    try {
      var res = await fetch('/api/terminal/cascade/scan/chart?symbol=' + encodeURIComponent(symbol),
        { credentials: 'same-origin', cache: 'no-store' });
      var data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Chart failed');
      box.innerHTML = chartSvg(data);
    } catch (err) {
      box.textContent = 'Could not load candles: ' + ((err && err.message) || err);
    }
  }

  function pick(symbol) {
    var row = lastRows.find(function (item) { return item.symbol === symbol; });
    var input = $('terminal-cascade-symbol') || $('terminal-symbol-input') || $('terminal-search-input');
    if (input) {
      input.value = symbol;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    if (row && row.rungs_fundable < 3) {
      setStatus(
        symbol + ' selected. Only ' + row.rungs_fundable + ' of 3 buy levels can afford a share at this capital — ' +
        'the averaging will barely happen.',
        'warn'
      );
    } else {
      setStatus(symbol + ' selected. Set the mother candle to start.', 'ok');
    }
    var panel = $('terminal-cascade-panel');
    if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function run(refresh) {
    var capital = parseFloat((els.capital && els.capital.value) || '100000');
    if (!(capital > 0)) { setStatus('Enter the capital you would put on one campaign.', 'warn'); return; }
    setStatus('Scanning 200+ scrips — this takes a few seconds on a cold run…', '');
    els.run.disabled = true;
    try {
      var url = '/api/terminal/cascade/scan?capital_inr=' + encodeURIComponent(capital) +
        (refresh ? '&refresh=true' : '');
      var res = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
      var data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Scan failed');
      render(data);
      setStatus('Scanned ' + new Date(data.scanned_at).toLocaleTimeString('en-IN'), 'ok');
    } catch (err) {
      setStatus(String((err && err.message) || err), 'error');
      els.body.innerHTML = '<div class="cascade-scan-empty"><p>Could not run the scan.</p></div>';
    } finally {
      els.run.disabled = false;
    }
  }

  function init() {
    els.run = $('cascade-scan-run');
    if (!els.run) return;
    els.capital = $('cascade-scan-capital');
    els.status = $('cascade-scan-status');
    els.meta = $('cascade-scan-meta');
    els.body = $('cascade-scan-body');

    els.run.addEventListener('click', function () { run(true); });
    els.body.addEventListener('click', function (event) {
      var use = event.target.closest('.cascade-scan-pick');
      if (use) { pick(use.dataset.symbol); return; }
      var chart = event.target.closest('.cascade-scan-chart-btn');
      if (chart) toggleChart(chart.dataset.symbol, chart.closest('tr'));
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
