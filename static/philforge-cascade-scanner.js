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
        '<td><button type="button" class="cascade-scan-pick" data-symbol="' + row.symbol + '">Use</button></td>' +
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
      var button = event.target.closest('.cascade-scan-pick');
      if (button) pick(button.dataset.symbol);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
