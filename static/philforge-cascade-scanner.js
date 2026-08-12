/* Equity page instrument scanner.
 *
 * Read-only. It ranks scrips and fills in the campaign form when one is
 * picked; it never starts a campaign or places an order. Kept in its own file
 * so it does not tangle with the rest of the Terminal code.
 *
 * TWO STRATEGIES, ONE SCANNER. The Equity page screens the same universe for
 * the Cash Cascade and for the two-red ladder, and they disagree only about
 * what counts as a useful fall and what the capital then does with it. That is
 * a difference in PARAMETERS, not in rendering -- so this is a factory called
 * twice rather than a second table that merely resembles the first. Everything
 * below is per-instance state; nothing is shared but the CSS.
 */
function createEquityScanner(cfg) {
  'use strict';

  var els = {};
  var lastRows = [];
  var lastPayload = null;
  var page = 0;
  var PAGE_SIZE = 5;
  var pendingScrollState = null;
  // Mirrors engine/cascade_scanner.py LEVEL_ALLOCATION. Change both together.
  var LEVEL_ALLOCATION = [0.20, 0.30, 0.50];
  // 'cascade' ranks for the three-rung fib pool; 'ladder' ranks for the
  // two-red ladder, whose rung size is a percentage of the FALL.
  var mode = cfg.mode || 'cascade';

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

  function capitalNow() {
    return parseFloat((els.capital && els.capital.value) || '0') || 0;
  }

  /* THE LADDER'S ARITHMETIC ON TODAY'S PRICE.
   *
   * Phil's funding rule, the one the 36-month runs were done under: the percent
   * price is down from the mother high is the percent of the purse the buy
   * commits. A 9% fall buys 9% of capital. The target is 0.75 of the way back
   * to that high -- 0.25 was the shipped number and it books about half as
   * much, because a quarter of a small gap does not clear delivery costs.
   *
   * The high here is the 20-session high the ranking is measured from, which is
   * a PROXY for the daily run-mother of the backtest, not the same object. Near
   * enough to size a first buy from; not a claim the two agree bar for bar.
   */
  function ladderMath(row) {
    var capital = capitalNow();
    var price = Number(row.last_price) || 0;
    var fall = Number(row.pullback_pct) || 0;
    var commit = capital * (fall / 100);
    var shares = price > 0 ? Math.floor(commit / price) : 0;
    var target = price + 0.75 * ((Number(row.recent_high) || price) - price);
    return {
      commit: commit,
      shares: shares,
      target: target,
      gainPct: price > 0 ? ((target - price) / price) * 100 : 0
    };
  }

  function captureScrollState() {
    var wrap = els.body && els.body.querySelector('.cascade-scan-table-wrap');
    return {
      pageX: window.scrollX,
      pageY: window.scrollY,
      tableLeft: wrap ? wrap.scrollLeft : 0
    };
  }

  function restoreScrollState(state) {
    if (!state) return;
    requestAnimationFrame(function () {
      window.scrollTo({ left: state.pageX, top: state.pageY, behavior: 'auto' });
      var wrap = els.body && els.body.querySelector('.cascade-scan-table-wrap');
      if (wrap) wrap.scrollLeft = state.tableLeft;
    });
  }

  function render(payload, keepPage) {
    var scrollState = pendingScrollState || captureScrollState();
    pendingScrollState = null;
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
        .map(function (row) { return '<li><strong>' + esc(row.symbol) + '</strong> — ' + esc(row.reason) + '</li>'; })
        .join('');
      els.body.innerHTML =
        '<div class="cascade-scan-empty">' +
        '<p>Nothing qualifies right now.</p>' +
        (why ? '<p class="cascade-scan-empty-note">Why the others were dropped:</p><ul>' + why + '</ul>' : '') +
        '</div>';
      restoreScrollState(scrollState);
      return;
    }

    var pages = Math.max(1, Math.ceil(lastRows.length / PAGE_SIZE));
    page = Math.min(page, pages - 1);
    var start = page * PAGE_SIZE;
    var pageRows = lastRows.slice(start, start + PAGE_SIZE);
    var rows = pageRows.map(function (row, offset) {
      var index = start + offset;
      var tail;
      if (mode === 'ladder') {
        var math = ladderMath(row);
        tail =
          '<td class="num">' + (capitalNow() > 0 ? money(math.commit) : '—') + '</td>' +
          '<td class="num' + (math.shares >= 1 ? '' : ' cascade-scan-short') + '">' +
          (math.shares >= 1 ? math.shares : 'under 1') + '</td>' +
          '<td class="num">' + money(math.target) + ' <span class="cascade-scan-name">+' +
          math.gainPct.toFixed(1) + '%</span></td>';
      } else {
        tail =
          '<td class="num">' + row.affordable_shares + '</td>' +
          '<td class="num">' + rungLabel(row.rungs_fundable) + '</td>';
      }
      return '' +
        '<tr data-symbol="' + esc(row.symbol) + '">' +
        '<td class="cascade-scan-rank">' + (index + 1) + '</td>' +
        '<td><strong>' + esc(row.symbol) + '</strong>' + (row.etf ? ' <span class="cascade-scan-etf">ETF</span>' : '') + '<span class="cascade-scan-name">' + esc(row.name || '') + '</span></td>' +
        '<td class="num">' + money(row.last_price) + '</td>' +
        '<td class="num cascade-scan-pullback">-' + row.pullback_pct.toFixed(1) + '%</td>' +
        '<td class="num">+' + row.strength_pct.toFixed(1) + '%</td>' +
        tail +
        '<td class="cascade-scan-row-actions">' +
        // Same classes as "Open Chart" in the instrument panel, so the two are
        // the same control rather than two that merely resemble each other.
        '<button type="button" class="btn btn-sm terminal-cascade-chart-launch cascade-scan-chart-btn" data-symbol="' + esc(row.symbol) + '">Chart</button>' +
        // No "Use" in ladder mode: there is no two-red campaign form to fill
        // yet, and a button that silently does nothing is worse than no button.
        (mode === 'ladder' ? '' :
          '<button type="button" class="btn btn-sm terminal-cascade-chart-launch cascade-scan-pick" data-symbol="' + esc(row.symbol) + '">Use</button>') +
        '</td>' +
        '</tr>';
    }).join('');

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

    var tailHead = mode === 'ladder'
      ? '<th class="num" title="Capital this buy commits: the percent price is down from the high">First buy</th>' +
        '<th class="num" title="Shares that money buys at today&#39;s price">Shares</th>' +
        '<th class="num" title="0.75 of the way back to the high, from today&#39;s price">Target</th>'
      : '<th class="num" title="Shares your capital buys">Shares</th>' +
        '<th class="num" title="How many of the three buy levels your capital can reach">Rungs</th>';

    els.body.innerHTML =
      '<div class="cascade-scan-table-wrap"><table class="cascade-scan-table">' +
      '<thead><tr>' +
      '<th>#</th><th>Scrip</th><th class="num">Price</th>' +
      '<th class="num" title="How far below its recent high it is trading">Off high</th>' +
      '<th class="num" title="Trend over the last 60 sessions">Trend</th>' +
      tailHead +
      '<th></th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>' + pager;
    restoreScrollState(scrollState);
  }

  /* The chart itself is NOT drawn here. It used to be: a hand-rolled SVG with a
   * fixed viewBox, a wall-clock x-axis, no session-gap blocks and no pan or
   * zoom. That is a second renderer, and a second renderer drifts -- the fib
   * overlay proved it before being ported. PhilForge draws every chart through
   * pfBenchDrawChart (static/philforge-bench-chart.js); this file owns only the
   * payload translator, `scanCanvasPayload` below.
   *
   * `esc` stays because the table markup uses it. */

  function esc(text) {
    return String(text).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
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
    if (mode === 'ladder') return ladderFooter(payload, capital, price, rupees);
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

  /* The same footer for the ladder, whose money works differently: one buy
   * sized by the fall, and only later buys if price makes a new low and prints
   * another pair of reds on a slower chart. The later rungs cannot be priced
   * now -- they depend on lows that have not happened -- so the strip shows the
   * FIRST buy honestly and says the rest is conditional rather than inventing
   * three numbers the way the Cascade's fixed pool split legitimately can.
   */
  function ladderFooter(payload, capital, price, rupees) {
    var fall = Number(payload.pullback_pct) || 0;
    var commit = capital * (fall / 100);
    var shares = Math.floor(commit / price);
    var high = Number(payload.recent_high) || price;
    var target = price + 0.75 * (high - price);
    var profit = shares * (target - price);
    var cells = '' +
      '<div class="cascade-scan-rung' + (shares >= 1 ? '' : ' is-short') + '">' +
      '<span>First buy · ' + fall.toFixed(1) + '% of purse</span>' +
      '<strong>' + rupees(commit) + '</strong>' +
      '<em>' + (shares >= 1 ? shares + ' share' + (shares === 1 ? '' : 's') : 'cannot afford one share') + '</em>' +
      '</div>' +
      '<div class="cascade-scan-rung">' +
      '<span>Target · 0.75 back to the high</span>' +
      '<strong>' + rupees(target) + '</strong>' +
      '<em>' + rupees(high) + ' high</em>' +
      '</div>' +
      '<div class="cascade-scan-rung' + (profit > 100 ? '' : ' is-short') + '">' +
      '<span>If it gets there</span>' +
      '<strong>' + rupees(profit) + '</strong>' +
      // ~Rs 86 is the measured delivery cost of a round trip at these sizes.
      // A gain that does not clear it is the exact way this rule used to lose.
      '<em>' + (profit > 100 ? 'clears ~₹86 of costs' : 'too small for ₹86 of costs') + '</em>' +
      '</div>';
    return '<div class="cascade-scan-rungs-strip">' + cells +
      '<div class="cascade-scan-rung-note">Buy at ' + rupees(capital) +
      ' purse. Rungs 2-4 only exist if price makes a new low and prints two more reds ' +
      'on a slower chart, so they cannot be priced today.</div></div>';
  }

  // The charts a scanned scrip can be looked at on. 4H is folded from hourly
  // bars and 1W from daily ones, so those two cost no extra broker call.
  var CHART_TFS = ['15m', '1h', '4h', '1d', '1w'];
  // Per-scanner, so the Cascade screen and the ladder screen can sit on
  // different charts without fighting over one variable.
  var chartTf = '1d';
  // A chart that has not answered in this long is not going to. Long enough for
  // Dhan's own 30s historical timeout plus one retry, short enough that nobody
  // sits watching a placeholder.
  // Overridable so a test can assert the timeout BEHAVIOUR without waiting the
  // real 45 seconds for it. Production never sets the global.
  var CHART_TIMEOUT_MS = Number(window.pfScanChartTimeoutMs) || 45000;
  // Monotonic id for the newest chart request; a reply that is not the newest is
  // dropped rather than painted into a row that has since been replaced.
  var chartRequest = 0;

  /* The server's own reason for a refusal.
   *
   * error_handlers.py answers 4xx as { success:false, error:{...detail} }; this
   * used to read `data.detail`, which is never there, so every refusal read
   * "Chart failed" and said nothing about why. */
  function chartError(body) {
    if (typeof window.pfErrorText === 'function') return window.pfErrorText(body, 'Chart failed');
    var error = body && body.error;
    return (error && (error.detail || error.message)) || (body && body.detail) || 'Chart failed';
  }

  function tfToggleHtml() {
    return '<div class="terminal-cascade-tf-toggle cascade-scan-chart-tf" role="radiogroup" aria-label="Chart timeframe">' +
      CHART_TFS.map(function (tf) {
        return '<button type="button" class="terminal-cascade-tf-option' +
          (tf === chartTf ? ' is-active' : '') + '" data-scan-tf="' + tf +
          '" role="radio" aria-checked="' + (tf === chartTf ? 'true' : 'false') + '">' +
          tf.toUpperCase() + '</button>';
      }).join('') + '</div>';
  }

  async function toggleChart(symbol, row, keepOpen) {
    var existing = row.nextElementSibling;
    var isChartRow = existing && existing.classList.contains('cascade-scan-chart-row');
    if (isChartRow && !keepOpen) { existing.remove(); return; }
    // Scoped to THIS scanner's body. A document-wide sweep would close the
    // other strategy's open chart, which is the classic way two instances of
    // one component start fighting.
    els.body.querySelectorAll('.cascade-scan-chart-row').forEach(function (node) { node.remove(); });

    var holder = document.createElement('tr');
    holder.className = 'cascade-scan-chart-row';
    holder.dataset.symbol = symbol;
    // The ladder table carries one more column than the Cascade's, so the
    // chart row has to span the right number or the layout shears.
    holder.innerHTML = '<td colspan="' + (mode === 'ladder' ? 9 : 8) + '"><div class="cascade-scan-chart-flow">' +
      tfToggleHtml() +
      '<div class="cascade-scan-chart">Loading ' + esc(symbol) + ' ' + chartTf + ' candles…</div></div></td>';
    row.parentNode.insertBefore(holder, row.nextSibling);
    var flow = holder.querySelector('.cascade-scan-chart-flow');
    requestAnimationFrame(function () { flow.classList.add('open'); });
    var box = holder.querySelector('.cascade-scan-chart');
    // EVERY CHART REQUEST GETS A DEADLINE AND AN OWNER.
    //
    // "Loading PHOENIXLTD 1d candles…" sat on screen indefinitely: the fetch had
    // no timeout, so anything slow upstream -- Dhan retrying a 30s call, the
    // account-wide rate budget, a browser connection queued behind the page's
    // pollers -- left the box on its placeholder with nothing to click. A chart
    // that cannot load must SAY so.
    //
    // The token guards the other half: clicking through the timeframe strip
    // replaces this row, and a reply from an abandoned request would otherwise
    // paint into a detached box while the visible one still said Loading.
    chartRequest += 1;
    var token = chartRequest;
    var timer = null;
    try {
      var controller = typeof AbortController === 'function' ? new AbortController() : null;
      var deadline = Number(window.pfScanChartTimeoutMs) || CHART_TIMEOUT_MS;
      if (controller) timer = setTimeout(function () { controller.abort(); }, deadline);
      var res = await fetch('/api/terminal/cascade/scan/chart?symbol=' + encodeURIComponent(symbol) +
        '&timeframe=' + encodeURIComponent(chartTf),
        { credentials: 'same-origin', cache: 'no-store', signal: controller ? controller.signal : undefined });
      if (timer) { clearTimeout(timer); timer = null; }
      if (token !== chartRequest) return;
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(chartError(data));
      // THE ONE RENDERER. This used to be a hand-rolled SVG in this file, with
      // a fixed viewBox, a wall-clock x-axis, no session gaps and no pan or
      // zoom -- a second renderer that drifted from the real one exactly as
      // the fib overlay did before it was ported. pfBenchDrawChart is the
      // Canvas from CryptoForge and the only chart PhilForge draws.
      if (typeof window.pfBenchDrawChart === 'function') {
        // Only ONE canvas may be mounted: the renderer finds its surfaces by
        // fixed ids, so any other open chart has to go first.
        if (typeof window._pfChartCanvasTeardown === 'function') window._pfChartCanvasTeardown();
        box.innerHTML = '';
        window.pfBenchDrawChart(box, scanCanvasPayload(data));
        box.insertAdjacentHTML('beforeend', motherHint(data));
        box.insertAdjacentHTML('beforeend', capitalFooter(data));
      } else {
        box.textContent = 'Chart renderer not loaded.';
      }
    } catch (err) {
      if (timer) clearTimeout(timer);
      if (token !== chartRequest) return;
      if (typeof window._pfChartCanvasTeardown === 'function') window._pfChartCanvasTeardown();
      var aborted = err && err.name === 'AbortError';
      var reason = aborted
        ? ('The chart did not answer within ' + Math.max(1, Math.round(deadline / 1000)) + 's. '
           + 'Dhan is slow or rate limited right now.')
        : ((err && err.message) || String(err));
      box.innerHTML = '<div class="cascade-scan-chart-error"><p>' + esc(reason) + '</p>'
        + '<button type="button" class="btn btn-sm btn-outline" data-scan-chart-retry="'
        + esc(symbol) + '">Try again</button></div>';
    }
  }

  /* The scan chart, in the renderer's own contract.
   *
   * `t` crosses as epoch SECONDS -- the renderer does arithmetic on it and the
   * route speaks ISO. The levels are what make the picture worth looking at:
   * the high the ranking is measured from, and for the ladder the price its
   * first buy becomes legal at. Without those it is just candles.
   */
  function scanCanvasPayload(payload) {
    var epoch = function (value) {
      var parsed = Date.parse(value);
      return Number.isFinite(parsed) ? Math.round(parsed / 1000) : null;
    };
    var candles = (payload.candles || []).map(function (row) {
      return { t: epoch(row.t), o: Number(row.o), h: Number(row.h), l: Number(row.l), c: Number(row.c) };
    }).filter(function (row) {
      return row.t !== null && [row.o, row.h, row.l, row.c].every(Number.isFinite);
    });

    var high = Number(payload.recent_high);
    var lines = [];
    if (Number.isFinite(high) && high > 0) {
      lines.push({
        price: Math.round(high * 100) / 100,
        label: (payload.recent_high_lookback || 20) + 'D HIGH',
        filled: true
      });
      if (mode === 'ladder') {
        var gate = high * (1 - (cfg.minPullback || 8) / 100);
        lines.push({
          price: Math.round(gate * 100) / 100,
          label: 'FIRST BUY BELOW (' + (cfg.minPullback || 8) + '%)',
          filled: false
        });
        var price = Number(payload.last_price);
        if (Number.isFinite(price) && price > 0) {
          lines.push({
            price: Math.round((price + 0.75 * (high - price)) * 100) / 100,
            label: 'TARGET IF BOUGHT NOW',
            filled: false
          });
        }
      }
    }
    // MARK THE CANDLE THAT MADE THE HIGH. This is the mother candidate, and
    // finding it by eye on several hundred bars is the thing Phil could not do.
    // The high is a DAILY number, so on a finer chart it is the bar that printed
    // it -- matched on price rather than assumed from the timestamp, because one
    // daily high maps to exactly one intraday bar and that bar's high IS it.
    var motherIndex = -1;
    for (var i = 0; i < candles.length; i += 1) {
      if (motherIndex < 0 || candles[i].h > candles[motherIndex].h) motherIndex = i;
    }
    var mother = null;
    if (motherIndex >= 0) {
      candles[motherIndex].is_mother = true;
      mother = { high: candles[motherIndex].h, low: candles[motherIndex].l };
    }
    return {
      timeframe: payload.timeframe || '1d',
      candles: candles,
      lines: lines,
      mother: mother
    };
  }

  /* The exact bar to name as the mother, in the form the campaign box wants.
   * The chart marks it; this says what to type. */
  function motherHint(payload) {
    var rows = payload.candles || [];
    if (!rows.length) return '';
    var best = rows[0];
    rows.forEach(function (row) { if (Number(row.h) > Number(best.h)) best = row; });
    var stamp = String(best.t || '').slice(0, 16);
    var warn = payload.high_in_view === false
      ? '<em class="cascade-scan-mother-warn">The ' + (payload.recent_high_lookback || 20) +
        'D high was set on ' + esc(String(payload.recent_high_date || '')) +
        ', before this window starts — switch to a higher timeframe to see it.</em>'
      : '';
    return '<div class="cascade-scan-mother-hint">' +
      '<span>Highest ' + esc(payload.timeframe || '') + ' candle in view</span>' +
      '<strong>' + esc(stamp.replace('T', ' ')) + ' IST</strong>' +
      '<em>high ' + Number(best.h).toLocaleString('en-IN', { maximumFractionDigits: 2 }) + '</em>' +
      warn + '</div>';
  }

  function pick(symbol) {
    // Ladder mode has no campaign form to fill; its rows never render a Use
    // button, and this guard means a stray click cannot half-fill the
    // Cascade's form with a scrip picked for a different strategy.
    if (mode === 'ladder') return;
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
    els.body.querySelectorAll('.cascade-scan-table tr.is-picked').forEach(function (node) {
      node.classList.remove('is-picked');
    });
    var picked = els.body.querySelector('.cascade-scan-table tr[data-symbol="' + symbol + '"]');
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
  }

  /* The pullback band, which is the whole difference between the two screens.
   * Sent explicitly even when it matches the endpoint's default, because the
   * server's cache key is built from these values -- an omitted parameter and
   * an equal one would otherwise land on the same saved scan by accident. */
  function scanParams() {
    var out = '';
    if (cfg.minPullback != null) out += '&min_pullback=' + encodeURIComponent(cfg.minPullback);
    if (cfg.maxPullback != null) out += '&max_pullback=' + encodeURIComponent(cfg.maxPullback);
    return out;
  }

  async function run(refresh) {
    var capital = parseFloat((els.capital && els.capital.value) || '100000');
    if (!(capital > 0)) { setStatus('Enter the capital you would put on one campaign.', 'warn'); return; }
    pendingScrollState = captureScrollState();
    setStatus('Scanning 200+ scrips — this takes a few seconds…', '');
    els.run.disabled = true;
    els.run.classList.add('is-working');
    els.body.innerHTML = '<div class="cascade-scan-loading">' +
      '<div class="cascade-scan-spinner" aria-hidden="true"></div>' +
      '<div>Pulling daily candles for 223 scrips…</div></div>';
    try {
      var url = '/api/terminal/cascade/scan?capital_inr=' + encodeURIComponent(capital) +
        scanParams() + (refresh ? '&refresh=true' : '');
      var res = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
      var data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Scan failed');
      lastPayload = data;
      render(data, true);
      setStatus('Scanned ' + new Date(data.scanned_at).toLocaleTimeString('en-IN'), 'ok');
    } catch (err) {
      setStatus(String((err && err.message) || err), 'error');
      els.body.innerHTML = '<div class="cascade-scan-empty"><p>Could not run the scan.</p></div>';
    } finally {
      els.run.disabled = false;
      els.run.classList.remove('is-working');
    }
  }

  async function restoreToday() {
    try {
      var capital = parseFloat((els.capital && els.capital.value) || '100000');
      if (!(capital > 0)) return;
      var res = await fetch(
        '/api/terminal/cascade/scan?capital_inr=' + encodeURIComponent(capital) + scanParams() + '&load_only=true',
        { credentials: 'same-origin', cache: 'no-store' }
      );
      var data = await res.json();
      if (!res.ok || data.status !== 'ok') return;
      lastPayload = data;
      render(data);
      setStatus('Today\'s scan restored · ' + new Date(data.scanned_at).toLocaleTimeString('en-IN'), 'ok');
    } catch (err) {
      // No saved scan is a normal first-visit state. Keep the initial prompt.
    }
  }

  function init() {
    var prefix = cfg.prefix;
    els.run = $(prefix + '-run');
    if (!els.run) return;
    els.capital = $(prefix + '-capital');
    els.status = $(prefix + '-status');
    els.meta = $(prefix + '-meta');
    els.body = $(prefix + '-body');
    if (!els.body) return;

    els.run.addEventListener('click', function () { run(true); });
    els.body.addEventListener('click', function (event) {
      var use = event.target.closest('.cascade-scan-pick');
      if (use) { pick(use.dataset.symbol); return; }
      var chart = event.target.closest('.cascade-scan-chart-btn');
      if (chart) { toggleChart(chart.dataset.symbol, chart.closest('tr')); return; }
      var retry = event.target.closest('[data-scan-chart-retry]');
      if (retry) {
        var retryRow = retry.closest('.cascade-scan-chart-row');
        var retryOwner = retryRow && retryRow.previousElementSibling;
        if (retryOwner) toggleChart(retryRow.dataset.symbol, retryOwner, true);
        return;
      }
      var tf = event.target.closest('[data-scan-tf]');
      if (tf) {
        chartTf = tf.getAttribute('data-scan-tf');
        var chartRow = tf.closest('.cascade-scan-chart-row');
        var owner = chartRow && chartRow.previousElementSibling;
        // Redraw the SAME row on the new chart. `keepOpen` stops the toggle
        // reading as a close, which is what a plain re-click would do.
        if (owner) toggleChart(chartRow.dataset.symbol, owner, true);
        return;
      }
      var pager = event.target.closest('[data-page]');
      if (pager && !pager.disabled) {
        page += pager.dataset.page === 'next' ? 1 : -1;
        render(lastPayload, true);
      }
    });
    restoreToday();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}

/* The Cash Cascade screen: any discount of 1% or more in a name still trending
 * up. These are the endpoint's own defaults, kept explicit so the two screens
 * read side by side. */
createEquityScanner({ prefix: 'cascade-scan', mode: 'cascade', minPullback: 1, maxPullback: 25 });

/* The two-red ladder screen. The 8% floor is not a taste: at anything shallower
 * the quarter-of-the-gap target used to be worth ₹8-18 against ₹86 of delivery
 * costs, and the 36-month run over 23 NSE names only turned every closed trade
 * green once the first buy waited for an 8% fall. Above 25% it is not a
 * pullback any more -- the two losers in that run, IRCTC and DMART, were falls
 * that never came back. */
createEquityScanner({ prefix: 'tworeds-scan', mode: 'ladder', minPullback: 8, maxPullback: 25 });

/* Mother-candle field: readable IST echo.
 *
 * The native picker stays -- it is the only reliable way to enter a datetime --
 * but it renders in browser locale, which reads as mm/dd to half the world. The
 * echo underneath states the value unambiguously in IST, which is the only
 * timezone this system trades in.
 */
(function () {
  'use strict';

  function readable(value) {
    var match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    if (!match) return '--';
    return match[3] + '-' + match[2] + '-' + match[1] + '  ' + match[4] + ':' + match[5] + ' IST';
  }

  function init() {
    var input = document.getElementById('terminal-cascade-mother-timestamp');
    var hint = document.getElementById('terminal-cascade-mother-readable');
    if (!input || !hint) return;

    var sync = function () { hint.textContent = readable(input.value); };
    sync();
    input.addEventListener('input', sync);
    input.addEventListener('change', sync);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
