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
  var lastPayload = null;
  var page = 0;
  var PAGE_SIZE = 10;
  // Mirrors engine/cascade_scanner.py LEVEL_ALLOCATION. Change both together.
  var LEVEL_ALLOCATION = [0.20, 0.30, 0.50];

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

  function render(payload, keepPage) {
    lastRows = payload.candidates || [];
    if (!keepPage) page = 0;
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

    var start = page * PAGE_SIZE;
    var pageRows = lastRows.slice(start, start + PAGE_SIZE);
    var rows = pageRows.map(function (row, offset) {
      var index = start + offset;
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
        // Same classes as "Open Chart" in the instrument panel, so the two are
        // the same control rather than two that merely resemble each other.
        '<button type="button" class="btn btn-sm terminal-cascade-chart-launch cascade-scan-chart-btn" data-symbol="' + row.symbol + '">Chart</button>' +
        '<button type="button" class="btn btn-sm terminal-cascade-chart-launch cascade-scan-pick" data-symbol="' + row.symbol + '">Use</button>' +
        '</td>' +
        '</tr>';
    }).join('');

    var pages = Math.max(1, Math.ceil(lastRows.length / PAGE_SIZE));
    var pager = pages > 1 ? (
      '<div class="cascade-scan-pager">' +
      '<button type="button" class="btn btn-sm btn-outline" data-page="prev"' +
      (page === 0 ? ' disabled' : '') + '>Previous</button>' +
      '<span class="cascade-scan-pager-label">' + (start + 1) + '&ndash;' +
      Math.min(start + PAGE_SIZE, lastRows.length) + ' of ' + lastRows.length + '</span>' +
      '<button type="button" class="btn btn-sm btn-outline" data-page="next"' +
      (page >= pages - 1 ? ' disabled' : '') + '>Next</button>' +
      '</div>'
    ) : '';

    els.body.innerHTML =
      '<div class="cascade-scan-table-wrap"><table class="cascade-scan-table">' +
      '<thead><tr>' +
      '<th>#</th><th>Scrip</th><th class="num">Price</th>' +
      '<th class="num" title="How far below its recent high it is trading">Off high</th>' +
      '<th class="num" title="Trend over the last 60 sessions">Trend</th>' +
      '<th class="num" title="Shares your capital buys">Shares</th>' +
      '<th class="num" title="How many of the three buy levels your capital can reach">Rungs</th>' +
      '<th></th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>' + pager;
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
               up: '#0f766e', down: '#be123c', high: '#7c3aed', now: '#334155',
               zone: 'rgba(190,18,60,.09)', low: '#0369a1', rung: 'rgba(3,105,161,.55)' };
    }
    return { bg: '#07101d', grid: 'rgba(148,163,184,.12)', axis: 'rgba(148,163,184,.55)',
             up: '#3fae56', down: '#d9534f', high: '#a855f7', now: '#e2e8f0',
             zone: 'rgba(217,83,79,.12)', low: '#38bdf8', rung: 'rgba(56,189,248,.5)' };
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

    // Same proportions and left price gutter as the campaign chart, so moving
    // between the two does not mean re-learning where to look.
    var W = 1320, H = 380, padL = 150, padR = 22, padT = 16, padB = 28;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var n = rows.length, cw = plotW / Math.max(n, 1);

    var lo = rows[0].l, hi = rows[0].h;
    rows.forEach(function (row) { lo = Math.min(lo, row.l); hi = Math.max(hi, row.h); });
    hi = Math.max(hi, payload.recent_high);
    lo = Math.min(lo, payload.last_price);
    var span = (hi - lo) || 1;
    var maxP = hi + span * 0.06, minP = lo - span * 0.06;

    // The leg the ranking is reading: the window's highest high, and the lowest
    // low printed since it. Every number under the chart is measured off this
    // pair, so it is drawn rather than left implied.
    var highAt = 0;
    rows.forEach(function (row, i) { if (row.h >= rows[highAt].h) highAt = i; });
    var lowAt = highAt;
    for (var k = highAt; k < n; k += 1) { if (rows[k].l <= rows[lowAt].l) lowAt = k; }
    var swingLow = rows[lowAt].l;
    var legRange = payload.recent_high - swingLow;
    var retraced = legRange > 0 ? ((payload.recent_high - payload.last_price) / legRange) * 100 : 0;

    var X = function (i) { return padL + i * cw + cw / 2; };
    var Y = function (p) { return padT + ((maxP - p) / ((maxP - minP) || 1)) * plotH; };
    var num = function (v) { return Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 }); };
    var day = function (t) {
      return new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short' })
        .format(new Date(t));
    };

    var out = ['<rect x="0" y="0" width="' + W + '" height="' + H + '" fill="' + PAL.bg + '"/>'];

    var sharpY = function (v) { return Math.round(v) + 0.5; };
    // Price labels live in the left gutter, as they do on the campaign chart.
    var gutter = function (y, text, colour) {
      out.push('<text x="' + (padL - 8) + '" y="' + (y + 3).toFixed(1) + '" fill="' + colour +
        '" font-size="10" font-family="monospace" text-anchor="end">' + esc(text) + '</text>');
    };

    for (var g = 0; g <= 4; g += 1) {
      var price = minP + (maxP - minP) * (g / 4), y = Y(price);
      out.push('<line x1="' + padL + '" y1="' + sharpY(y) + '" x2="' + (padL + plotW) + '" y2="' + sharpY(y) +
        '" stroke="' + PAL.grid + '" stroke-width="1" shape-rendering="crispEdges"/>');
      gutter(y, num(price), PAL.axis);
    }

    // The discount being ranked, shaded between the recent high and now.
    var yHigh = Y(payload.recent_high), yNow = Y(payload.last_price);
    out.push('<rect x="' + padL + '" y="' + yHigh.toFixed(1) + '" width="' + plotW + '" height="' +
      Math.max(0, yNow - yHigh).toFixed(1) + '" fill="' + PAL.zone + '"/>');

    // Strokes centred on a half pixel, fills aligned to whole ones -- otherwise
    // the rasteriser spreads every 1px line across two columns and the whole
    // chart reads as soft.
    var sharp = function (v) { return Math.round(v) + 0.5; };
    var solid = function (v) { return Math.round(v); };

    rows.forEach(function (row, i) {
      var up = row.c >= row.o;
      var colour = up ? PAL.up : PAL.down;
      var x = X(i);
      var bodyTop = solid(Y(Math.max(row.o, row.c)));
      var bodyBottom = solid(Y(Math.min(row.o, row.c)));
      var bw = Math.max(1, cw * 0.62);
      var left = solid(x - bw / 2);
      var right = Math.max(solid(x + bw / 2), left + 1);
      out.push('<line x1="' + sharp(x) + '" y1="' + solid(Y(row.h)) + '" x2="' + sharp(x) +
        '" y2="' + solid(Y(row.l)) + '" stroke="' + colour + '" stroke-width="1" shape-rendering="crispEdges"/>');
      out.push('<rect x="' + left + '" y="' + bodyTop + '" width="' + (right - left) +
        '" height="' + Math.max(bodyBottom - bodyTop, 1) + '" fill="' + colour +
        '" shape-rendering="crispEdges"/>');
    });

    // The high bar is banded and tagged the way the campaign chart marks its
    // mother candle -- this is the same role on a different timeframe.
    var xh = X(highAt), bwh = Math.max(cw * 0.62, 6);
    out.push('<rect x="' + (xh - bwh / 2 - 3).toFixed(1) + '" y="' + (padT + 1) + '" width="' + (bwh + 6).toFixed(1) +
      '" height="' + (plotH - 2).toFixed(1) + '" fill="' + PAL.high + '" opacity=".09"/>');
    out.push('<text x="' + xh.toFixed(1) + '" y="' + Math.max(Y(rows[highAt].h) - 8, padT + 10).toFixed(1) +
      '" fill="' + PAL.high + '" font-size="9.5" font-family="monospace" font-weight="700" text-anchor="middle">MC</text>');

    out.push('<line x1="' + padL + '" y1="' + sharpY(yHigh) + '" x2="' + (padL + plotW) + '" y2="' + sharpY(yHigh) +
      '" stroke="' + PAL.high + '" stroke-width="1.25" stroke-dasharray="5 3" shape-rendering="crispEdges"/>');
    gutter(yHigh, payload.recent_high_lookback + 'd high ' + num(payload.recent_high), PAL.high);
    var yLow = Y(swingLow);
    out.push('<line x1="' + padL + '" y1="' + sharpY(yLow) + '" x2="' + (padL + plotW) + '" y2="' + sharpY(yLow) +
      '" stroke="' + PAL.low + '" stroke-width="1.25" stroke-dasharray="5 3" shape-rendering="crispEdges"/>');
    gutter(yLow, 'leg low ' + num(swingLow), PAL.low);

    // Mark which two bars set the leg, so the lines are traceable to candles.
    [[highAt, PAL.high], [lowAt, PAL.low]].forEach(function (pair) {
      out.push('<line x1="' + X(pair[0]).toFixed(1) + '" y1="' + padT + '" x2="' + X(pair[0]).toFixed(1) +
        '" y2="' + (padT + plotH) + '" stroke="' + pair[1] + '" stroke-width="1" stroke-dasharray="1 4" opacity=".55"/>');
    });

    out.push('<line x1="' + padL + '" y1="' + sharpY(yNow) + '" x2="' + (padL + plotW) + '" y2="' + sharpY(yNow) +
      '" stroke="' + PAL.now + '" stroke-width="1.25" stroke-dasharray="2 3" shape-rendering="crispEdges"/>');
    gutter(yNow, 'now ' + num(payload.last_price), PAL.now);
    out.push('<text x="' + (padL + 6) + '" y="' + (yNow + 13).toFixed(1) + '" fill="' + PAL.now +
      '" font-size="10" font-family="monospace" opacity=".8">-' + payload.pullback_pct.toFixed(1) +
      '% off the high · ' + retraced.toFixed(0) + '% down the leg</text>');

    var ticks = Math.min(6, n);
    for (var t = 0; t < ticks; t += 1) {
      var at = Math.round((n - 1) * (t / Math.max(ticks - 1, 1)));
      out.push('<text x="' + X(at).toFixed(1) + '" y="' + (H - 7) + '" fill="' + PAL.axis +
        '" font-size="10" font-family="monospace" text-anchor="middle">' + esc(day(rows[at].t)) + '</text>');
    }

    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet" ' +
      'shape-rendering="geometricPrecision" class="cascade-scan-chart-svg" role="img" ' +
      'aria-label="Daily candles for ' + esc(payload.symbol) + '">' + out.join('') + '</svg>';
    return svg + capitalFooter(payload);
  }

  /* What the campaign would actually do with the capital in the box. The ladder
   * itself cannot be drawn here -- its rungs come from 5m trendline anchors that
   * only exist once a campaign is running -- but the pool split is fixed, so the
   * three slices and what each can buy at today's price are real numbers now. */
  function capitalFooter(payload) {
    var capital = parseFloat((els.capital && els.capital.value) || '0');
    var price = Number(payload.last_price) || 0;
    if (!(capital > 0) || !(price > 0)) return '';
    var rupees = function (v) {
      return '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 });
    };
    var cells = LEVEL_ALLOCATION.map(function (share, i) {
      var slice = capital * share;
      var shares = Math.floor(slice / price);
      return '<div class="cascade-scan-rung' + (shares >= 1 ? '' : ' is-short') + '">' +
        '<span>Buy ' + (i + 1) + ' · ' + Math.round(share * 100) + '%</span>' +
        '<strong>' + rupees(slice) + '</strong>' +
        '<em>' + (shares >= 1 ? shares + ' share' + (shares === 1 ? '' : 's') : 'cannot afford one share') + '</em>' +
        '</div>';
    }).join('');
    return '<div class="cascade-scan-rungs-strip">' + cells +
      '<div class="cascade-scan-rung-note">Pool split per leg at ' + rupees(capital) +
      ' capital. Rung prices are set by the 5m ladder once the campaign starts.</div></div>';
  }

  async function toggleChart(symbol, row) {
    var existing = row.nextElementSibling;
    if (existing && existing.classList.contains('cascade-scan-chart-row')) { existing.remove(); return; }
    document.querySelectorAll('.cascade-scan-chart-row').forEach(function (node) { node.remove(); });

    var holder = document.createElement('tr');
    holder.className = 'cascade-scan-chart-row';
    holder.innerHTML = '<td colspan="8"><div class="cascade-scan-chart-flow">' +
      '<div class="cascade-scan-chart">Loading ' + esc(symbol) + ' daily candles…</div></div></td>';
    row.parentNode.insertBefore(holder, row.nextSibling);
    var flow = holder.querySelector('.cascade-scan-chart-flow');
    requestAnimationFrame(function () { flow.classList.add('open'); });
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
    document.querySelectorAll('.cascade-scan-table tr.is-picked').forEach(function (node) {
      node.classList.remove('is-picked');
    });
    var picked = document.querySelector('.cascade-scan-table tr[data-symbol="' + symbol + '"]');
    if (picked) picked.classList.add('is-picked');
    // Say plainly which scrip the setup below is now for, since the form
    // itself only shows a symbol field that is easy to miss.
    var banner = $('terminal-cascade-selected');
    if (banner) {
      banner.textContent = 'Campaign setup is for ' + symbol + (row ? ' · ' + (row.name || '') : '');
      banner.classList.add('is-set');
    }
    // The page header carries the selected chip and the live LTP, but only
    // reacts to the stock list. Picking from the scanner has to reach it too,
    // otherwise the header keeps reading "—" while a campaign is being set up.
    if (typeof window.selectStockTerminal === 'function') {
      try { window.selectStockTerminal(symbol); } catch (err) { /* header is optional */ }
    }
    // Jump, do not glide. Smooth scrolling across a long page reads as a lag
    // between the click and anything happening.
    var panel = $('terminal-cascade-panel');
    if (panel && panel.scrollIntoView) panel.scrollIntoView({ block: 'nearest' });
  }

  async function run(refresh) {
    var capital = parseFloat((els.capital && els.capital.value) || '100000');
    if (!(capital > 0)) { setStatus('Enter the capital you would put on one campaign.', 'warn'); return; }
    setStatus('Scanning 200+ scrips — this takes a few seconds on a cold run…', '');
    els.run.disabled = true;
    els.run.classList.add('is-working');
    els.body.innerHTML = '<div class="cascade-scan-loading">' +
      '<div class="cascade-scan-spinner" aria-hidden="true"></div>' +
      '<div>Pulling daily candles for 223 scrips…</div></div>';
    try {
      var url = '/api/terminal/cascade/scan?capital_inr=' + encodeURIComponent(capital) +
        (refresh ? '&refresh=true' : '');
      var res = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
      var data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Scan failed');
      lastPayload = data;
      render(data);
      setStatus('Scanned ' + new Date(data.scanned_at).toLocaleTimeString('en-IN'), 'ok');
    } catch (err) {
      setStatus(String((err && err.message) || err), 'error');
      els.body.innerHTML = '<div class="cascade-scan-empty"><p>Could not run the scan.</p></div>';
    } finally {
      els.run.disabled = false;
      els.run.classList.remove('is-working');
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
      if (chart) { toggleChart(chart.dataset.symbol, chart.closest('tr')); return; }
      var pager = event.target.closest('[data-page]');
      if (pager && !pager.disabled) {
        page += pager.dataset.page === 'next' ? 1 : -1;
        render(lastPayload, true);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* Mother-candle field: readable IST echo and a sensible default.
 *
 * The native picker stays -- it is the only reliable way to enter a datetime --
 * but it renders in browser locale, which reads as mm/dd to half the world. The
 * echo underneath states the value unambiguously in IST, which is the only
 * timezone this system trades in.
 */
(function () {
  'use strict';

  var MINUTES = { '5m': 5, '15m': 15, '1h': 60 };

  function pad(value) { return String(value).padStart(2, '0'); }

  function readable(value) {
    if (!value) return '--';
    var d = new Date(value);
    if (isNaN(d.getTime())) return '--';
    return pad(d.getDate()) + '-' + pad(d.getMonth() + 1) + '-' + d.getFullYear() +
      '  ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ' IST';
  }

  // Last completed candle of the selected timeframe, so the field is never
  // blank and never points at a bar that has not closed.
  function lastClosed(tf) {
    var step = MINUTES[tf] || 5;
    var now = new Date();
    var d = new Date(now.getTime() - step * 60000);
    if (step === 60) {
      d.setMinutes(15, 0, 0);
      if (d > now) d.setHours(d.getHours() - 1);
    } else {
      d.setMinutes(Math.floor(d.getMinutes() / step) * step, 0, 0);
    }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function init() {
    var input = document.getElementById('terminal-cascade-mother-timestamp');
    var hint = document.getElementById('terminal-cascade-mother-readable');
    var tf = document.getElementById('terminal-cascade-timeframe');
    if (!input || !hint) return;

    var sync = function () { hint.textContent = readable(input.value); };
    if (!input.value) input.value = lastClosed(tf ? tf.value : '5m');
    sync();
    input.addEventListener('input', sync);
    input.addEventListener('change', sync);
    if (tf) {
      tf.addEventListener('change', function () {
        input.value = lastClosed(tf.value);
        sync();
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
