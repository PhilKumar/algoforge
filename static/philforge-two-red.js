/* The two-red ladder console, on the Equity page.
 *
 * Paper only. Nothing in this file can place an order -- there is no live
 * executor written for this strategy at all, so the buttons here start, stop,
 * close and delete PAPER campaigns and nothing else.
 *
 * Kept out of philforge-app.js on purpose: that file is already very large,
 * and this console talks to its own six endpoints and shares no state with the
 * Cash Cascade beyond the scrip list.
 */
(function () {
  'use strict';

  var POLL_MS = 15000;
  var timer = null;
  var lastCampaigns = [];
  var lastClosed = [];

  function $(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }

  function rupees(value, digits) {
    if (value === null || value === undefined || value === '') return '—';
    return '₹' + Number(value).toLocaleString('en-IN', {
      minimumFractionDigits: digits === undefined ? 0 : digits,
      maximumFractionDigits: digits === undefined ? 0 : digits
    });
  }

  function setStatus(text, tone) {
    var el = $('tworeds-form-status');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = tone === 'error' ? 'var(--danger)'
      : tone === 'success' ? 'var(--success)' : 'var(--muted)';
  }

  /* ── the section switcher ───────────────────────────────────────
   * Three groups of panels on one page: the two strategies, and the manual
   * desk (scrip list, order ticket, broker book) which is not a strategy at
   * all. Switching hides and shows rather than re-rendering, so an open chart
   * or a half-typed mother survives a trip to another section and back.
   *
   * The desk is exported on window because philforge-app.js has to ask whether
   * it is on screen before spending a quote poll on it.
   */
  function showStrategy(which) {
    var groups = {
      cascade: $('equity-strategy-cascade'),
      tworeds: $('equity-strategy-tworeds'),
      desk: $('equity-strategy-desk')
    };
    if (!groups.cascade || !groups.tworeds || !groups.desk) return;
    Object.keys(groups).forEach(function (key) {
      groups[key].style.display = key === which ? '' : 'none';
    });
    var buttons = document.querySelectorAll('#equity-strategy-switch [data-equity-strategy]');
    Array.prototype.forEach.call(buttons, function (button) {
      var active = button.getAttribute('data-equity-strategy') === which;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-checked', active ? 'true' : 'false');
    });
    // Polling only while its own half is on screen: this console is useless in
    // the background and the Dhan rate budget is account-wide.
    if (which === 'tworeds') {
      refresh();
      startPolling();
    } else {
      stopPolling();
    }
  }

  function startPolling() {
    if (timer) return;
    timer = setInterval(function () {
      var panel = $('equity-strategy-tworeds');
      if (!panel || panel.style.display === 'none') return;
      if (document.hidden) return;
      refresh();
    }, POLL_MS);
  }

  function stopPolling() {
    if (!timer) return;
    clearInterval(timer);
    timer = null;
  }

  /* ── campaigns ──────────────────────────────────────────────── */

  function stateChip(campaign) {
    var map = {
      WATCHING: ['watching', 'var(--muted)'],
      ARMED: ['armed', '#fbbf24'],
      HOLDING: ['holding', '#38bdf8'],
      CLOSED: ['closed', '#6ee7b7'],
      VOID: ['void', 'var(--muted)'],
      KILLED: ['killed', 'var(--danger)']
    };
    var row = map[campaign.status] || [String(campaign.status || '').toLowerCase(), 'var(--muted)'];
    return '<span class="terminal-cascade-pill" style="color:' + row[1] + ';">' + esc(row[0]) + '</span>';
  }

  function moneyCell(campaign) {
    // A campaign that has not sold anything has NO result. A confident ₹0.00
    // there reads as "traded and came out level", which is a different and
    // wrong statement. Same rule the fib-space book follows.
    if (campaign.realised) {
      var net = Number(campaign.realised.net_pnl);
      return '<strong style="color:' + (net >= 0 ? '#6ee7b7' : 'var(--danger)') + ';">' +
        rupees(net, 2) + '</strong><span class="cascade-scan-name">realised, after costs</span>';
    }
    if (campaign.open_money) {
      var open = Number(campaign.open_money.net_pnl);
      return '<span style="color:' + (open >= 0 ? '#6ee7b7' : 'var(--danger)') + ';">' +
        rupees(open, 2) + '</span><span class="cascade-scan-name">if sold now</span>';
    }
    return '—';
  }

  function campaignRow(campaign) {
    var mother = campaign.mother || {};
    var when = mother.timestamp ? String(mother.timestamp).slice(0, 16).replace('T', ' ') : '—';
    var actions = '';
    if (campaign.running) {
      actions += '<button type="button" class="btn btn-sm btn-outline" data-two-red-stop="' +
        esc(campaign.symbol) + '">Stop</button>';
      if (campaign.quantity > 0) {
        actions += '<button type="button" class="btn btn-sm btn-danger" data-two-red-kill="' +
          esc(campaign.symbol) + '">Close now</button>';
      }
    } else {
      actions += '<button type="button" class="btn btn-sm btn-danger" data-two-red-delete="' +
        esc(campaign.symbol) + '">Delete</button>';
    }

    var target = campaign.target
      ? rupees(campaign.target, 2) +
        (campaign.target_gap_pct !== null && campaign.target_gap_pct !== undefined
          ? '<span class="cascade-scan-name">' + Number(campaign.target_gap_pct).toFixed(1) + '% away</span>'
          : '')
      : '—';

    return '<tr>' +
      '<td><strong>' + esc(campaign.symbol) + '</strong><span class="cascade-scan-name">' +
      esc(mother.timeframe || '') + ' mother · ' + esc(when) + '</span></td>' +
      '<td>' + stateChip(campaign) + '</td>' +
      '<td class="num">' + rupees(mother.high, 2) + '</td>' +
      '<td class="num">' + campaign.rung + ' of ' + campaign.rungs + '</td>' +
      '<td class="num">' + (campaign.quantity || 0) + '</td>' +
      '<td class="num">' + (campaign.average_entry ? rupees(campaign.average_entry, 2) : '—') + '</td>' +
      '<td class="num">' + target + '</td>' +
      '<td class="num">' + moneyCell(campaign) + '</td>' +
      '<td class="cascade-scan-row-actions">' + actions + '</td>' +
      '</tr>';
  }

  function renderCampaigns(campaigns) {
    var body = $('tworeds-campaigns-body');
    if (!body) return;
    if (!campaigns.length) {
      body.innerHTML = '<div class="terminal-cascade-empty">Nothing running. Name a mother candle above to start one.</div>';
      return;
    }
    body.innerHTML =
      '<div class="cascade-scan-table-wrap"><table class="cascade-scan-table">' +
      '<thead><tr>' +
      '<th>Scrip</th><th>State</th><th class="num">Mother high</th>' +
      '<th class="num" title="How many rungs of the ladder have filled">Rungs</th>' +
      '<th class="num">Shares</th><th class="num">Avg entry</th>' +
      '<th class="num">Target</th><th class="num">P&amp;L</th><th></th>' +
      '</tr></thead><tbody>' + campaigns.map(campaignRow).join('') + '</tbody></table></div>';
  }

  function renderClosed(rows) {
    var body = $('tworeds-closed-body');
    if (!body) return;
    if (!rows.length) {
      body.innerHTML = '<div class="terminal-cascade-empty">No closed campaigns yet.</div>';
      return;
    }
    body.innerHTML =
      '<div class="cascade-scan-table-wrap"><table class="cascade-scan-table">' +
      '<thead><tr><th>Scrip</th><th>Ended</th><th class="num">Shares</th>' +
      '<th class="num">Invested</th><th class="num">Net</th></tr></thead><tbody>' +
      rows.map(function (row) {
        var net = row.realised ? Number(row.realised.net_pnl) : null;
        return '<tr>' +
          '<td><strong>' + esc(row.symbol) + '</strong></td>' +
          '<td>' + esc((row.exit && row.exit.reason) || row.ended_reason || '—') + '</td>' +
          '<td class="num">' + (row.quantity || 0) + '</td>' +
          '<td class="num">' + rupees(row.invested, 0) + '</td>' +
          '<td class="num">' + (net === null ? '—' :
            '<strong style="color:' + (net >= 0 ? '#6ee7b7' : 'var(--danger)') + ';">' + rupees(net, 2) + '</strong>') +
          '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }

  async function refresh() {
    try {
      var res = await fetch('/api/two-red/status', { credentials: 'same-origin', cache: 'no-store' });
      if (!res.ok) return;
      var data = await res.json();
      lastCampaigns = data.campaigns || [];
      lastClosed = data.closed || [];
      renderCampaigns(lastCampaigns);
      renderClosed(lastClosed);
      var meta = $('tworeds-campaigns-meta');
      if (meta) {
        var holding = lastCampaigns.filter(function (row) { return row.quantity > 0; }).length;
        meta.textContent = lastCampaigns.length + ' campaign' + (lastCampaigns.length === 1 ? '' : 's')
          + ' · ' + holding + ' holding shares';
      }
    } catch (err) {
      // A failed poll is not worth a banner: the next one is 15 seconds away
      // and the table still shows the last good state.
    }
  }

  /* ── actions ────────────────────────────────────────────────── */

  window.startTwoRedCampaign = async function startTwoRedCampaign() {
    var symbol = ($('tworeds-symbol') || {}).value || '';
    var mother = ($('tworeds-mother-timestamp') || {}).value || '';
    if (!symbol.trim()) { setStatus('Pick a scrip first.', 'error'); return; }
    if (!mother) { setStatus('Name the mother candle.', 'error'); return; }
    setStatus('Starting…');
    var button = $('tworeds-start');
    if (button) button.disabled = true;
    try {
      var res = await fetch('/api/two-red/start', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: symbol.trim().toUpperCase(),
          mother_timestamp: mother,
          mother_timeframe: ($('tworeds-mother-timeframe') || {}).value || '1d',
          capital_inr: parseFloat(($('tworeds-capital') || {}).value || '200000'),
          min_fall_pct: parseFloat(($('tworeds-min-fall') || {}).value || '8'),
          target_fraction: parseFloat(($('tworeds-target') || {}).value || '0.75')
        })
      });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(data.detail || 'Could not start the campaign.');
      var campaign = data.campaign || {};
      // A replayed campaign can arrive already finished, and saying "started"
      // for something that is over reads as a fault when the table shows it
      // closed a second later.
      if (campaign.status === 'VOID') {
        setStatus('That mother is spent — price closed back above its high before any buy. Nothing is running.', 'error');
      } else if (campaign.status === 'CLOSED') {
        setStatus('Replayed to now and it has already finished. See the row below.', 'success');
      } else {
        setStatus('Running. ' + (campaign.quantity ? campaign.quantity + ' shares already bought on replay.' : 'Watching for two reds.'), 'success');
      }
      await refresh();
    } catch (err) {
      setStatus(String((err && err.message) || err), 'error');
    } finally {
      if (button) button.disabled = false;
    }
  };

  window.refreshTwoRedCampaigns = function refreshTwoRedCampaigns() { refresh(); };

  async function post(url, symbol) {
    var res = await fetch(url + '?symbol=' + encodeURIComponent(symbol), {
      method: 'POST', credentials: 'same-origin'
    });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.detail || 'Request failed.');
    return data;
  }

  async function handle(symbol, kind) {
    try {
      if (kind === 'stop') {
        var ok = await window.customConfirm(
          'Stop watching ' + esc(symbol) + '? Anything already bought stays held — this does not sell.',
          { title: 'Stop campaign', okText: 'Stop' }
        );
        if (!ok) return;
        await post('/api/two-red/stop', symbol);
        setStatus(symbol + ' stopped. Any shares are still held.', 'success');
      } else if (kind === 'kill') {
        var okKill = await window.customConfirm(
          'Close ' + esc(symbol) + ' now at the last traded price? No Dhan order is sent — this is paper.',
          { title: 'Close basket', okText: 'Close now', danger: true }
        );
        if (!okKill) return;
        await post('/api/two-red/kill', symbol);
        setStatus(symbol + ' closed at the last price.', 'success');
      } else if (kind === 'delete') {
        var okDelete = await window.customConfirm(
          'Delete ' + esc(symbol) + ' from this page? If it traded, its record moves to Closed Campaigns.',
          { title: 'Delete campaign', okText: 'Delete', danger: true }
        );
        if (!okDelete) return;
        var res = await fetch('/api/two-red?symbol=' + encodeURIComponent(symbol), {
          method: 'DELETE', credentials: 'same-origin'
        });
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok) throw new Error(data.detail || 'Delete failed.');
        setStatus(symbol + ' deleted.', 'success');
      }
      await refresh();
    } catch (err) {
      setStatus(String((err && err.message) || err), 'error');
    }
  }

  /* ── wiring ─────────────────────────────────────────────────── */

  // philforge-app.js polls the LTP and the order book on timers. Those cost
  // Dhan calls against an ACCOUNT-WIDE rate budget, and both are useless while
  // the desk is hidden behind another section, so it asks this first.
  window.pfEquityDeskVisible = function pfEquityDeskVisible() {
    var desk = document.getElementById('equity-strategy-desk');
    return !!desk && desk.style.display !== 'none';
  };

  function init() {
    var page = $('stock-terminal-page');
    if (!page) return;

    var switcher = $('equity-strategy-switch');
    if (switcher) {
      switcher.addEventListener('click', function (event) {
        var button = event.target.closest('[data-equity-strategy]');
        if (!button) return;
        showStrategy(button.getAttribute('data-equity-strategy'));
      });
    }

    var campaigns = $('tworeds-campaigns-body');
    if (campaigns) {
      campaigns.addEventListener('click', function (event) {
        var stop = event.target.closest('[data-two-red-stop]');
        if (stop) return void handle(stop.getAttribute('data-two-red-stop'), 'stop');
        var kill = event.target.closest('[data-two-red-kill]');
        if (kill) return void handle(kill.getAttribute('data-two-red-kill'), 'kill');
        var drop = event.target.closest('[data-two-red-delete]');
        if (drop) return void handle(drop.getAttribute('data-two-red-delete'), 'delete');
      });
    }

    // The screen's rows have no "Use" button, so picking a scrip for a campaign
    // is a click on the row itself. Filling the form is the whole handoff
    // between the two halves of this strategy.
    var screen = $('tworeds-scan-body');
    if (screen) {
      screen.addEventListener('click', function (event) {
        if (event.target.closest('button')) return;
        var row = event.target.closest('tr[data-symbol]');
        if (!row) return;
        var symbol = row.getAttribute('data-symbol');
        var field = $('tworeds-symbol');
        if (field) field.value = symbol;
        var banner = $('tworeds-selected');
        if (banner) {
          banner.textContent = 'Campaign setup is for ' + symbol;
          banner.classList.add('is-set');
        }
        var capital = $('tworeds-scan-capital');
        var purse = $('tworeds-capital');
        if (capital && purse && capital.value) purse.value = capital.value;
        setStatus(symbol + ' picked. Name the mother candle to start.', '');
      });
    }

    // The readable IST echo, same as the Cascade's mother field: the native
    // picker renders in browser locale, which reads as mm/dd to half the world.
    var motherField = $('tworeds-mother-timestamp');
    var readable = $('tworeds-mother-readable');
    if (motherField && readable) {
      var sync = function () {
        var match = String(motherField.value || '').match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
        readable.textContent = match
          ? match[3] + '-' + match[2] + '-' + match[1] + '  ' + match[4] + ':' + match[5] + ' IST'
          : '--';
      };
      sync();
      motherField.addEventListener('input', sync);
      motherField.addEventListener('change', sync);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
