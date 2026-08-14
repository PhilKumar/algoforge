(() => {
  'use strict';

  const views = {
    estate: {
      name: 'FORGE ESTATE',
      title: 'Shared request-to-audit path',
      subtitle: 'The same guarded pattern supports two different markets without mixing their accounts, clocks, brokers, or execution rules.',
      rule: 'No browser action can bypass the API, risk checks, or audit write.',
      nodes: [
        ['01', 'Browser', 'Authenticated terminal', 'User intent + live state', 'HTTPS · SESSION'],
        ['02', 'Nginx', 'TLS edge gateway', 'Static assets and API routing', 'BLUE / GREEN'],
        ['03', 'FastAPI', 'Application gateway', 'Identity, validation and orchestration', 'ASYNC API'],
        ['04', 'Risk gate', 'Policy boundary', 'Mode, limits, ownership and readiness', 'FAIL CLOSED'],
        ['05', 'Market edge', 'Broker + data APIs', 'Orders, quotes and historical candles', 'EXTERNAL'],
        ['06', 'Evidence', 'State + audit trail', 'Positions, events, P&L and recovery', 'DURABLE']
      ],
      metrics: [['MARKETS', 'Crypto + NSE'], ['CONTROL PLANES', '2 isolated'], ['DELIVERY', 'Blue-green'], ['PRIMARY STORE', 'SQLite family']],
      io: [
        ['INPUT', ['Authenticated click', 'Market tick or candle', 'Strategy settings']],
        ['PROCESS', ['Validate identity', 'Evaluate strategy + risk', 'Route and reconcile']],
        ['OUTPUT', ['Broker order state', 'Live user interface', 'Auditable event record']]
      ],
      trust: [['01', 'Browser', 'Requests actions; never owns broker truth.'], ['02', 'Application', 'Owns identity, validation and user state.'], ['03', 'Engine', 'Owns strategy state and risk decisions.'], ['04', 'External', 'Broker remains the final order authority.']],
      cadence: [['Market event', 'Tick or candle', 94], ['Engine cycle', 'Seconds', 77], ['Screen refresh', 'Seconds', 60], ['Recovery check', 'Minutes', 38], ['Backup', 'Scheduled', 18]],
      ownership: [['Identity', 'FastAPI session layer', 'Resolves the user before private work.'], ['Strategy', 'Dedicated engine instance', 'One lifecycle owner for each run.'], ['Orders', 'Broker plus reconciler', 'Local state follows external truth.'], ['Display', 'Browser renderer', 'Shows state; never creates execution truth.']]
    },
    crypto: {
      name: 'CRYPTOFORGE',
      title: '24/7 digital-asset execution path',
      subtitle: 'A continuous market stack for operator-run strategies and a separately signed buyer execution channel.',
      rule: 'Exchange reconciliation wins over stale local state after every interruption.',
      nodes: [
        ['01', 'Operator UI', 'Crypto terminal', 'Strategy, backtest and portfolio actions', 'HTTPS'],
        ['02', 'FastAPI', 'Control gateway', 'Session, command validation and status', 'PORT 9000/9001'],
        ['03', 'Worker', 'Single active strategy', 'Scalp, Cascade and condition engines', 'ONE OWNER'],
        ['04', 'Risk', 'Execution guard', 'Mode, quantity, stops and order checks', 'BEFORE ORDER'],
        ['05', 'Exchange', 'Trading adapters', 'Binance; CoinDCX and Delta adapters', '24 × 7'],
        ['06', 'Journal', 'Portfolio evidence', 'Orders, fills, P&L and recovery state', 'SQLITE + JSON']
      ],
      metrics: [['CLOCK', '24 × 7'], ['DEFAULT VENUE', 'Binance Spot'], ['BUYER CHANNEL', 'Signed feed'], ['ENGINE OWNER', '1 active worker']],
      io: [
        ['INPUT', ['Exchange ticks + candles', 'Operator strategy command', 'Signed buyer entitlement']],
        ['PROCESS', ['Build indicators', 'Evaluate entry / exit / risk', 'Submit then reconcile']],
        ['OUTPUT', ['Order and fill state', 'Portfolio + campaign view', 'Durable journal event']]
      ],
      trust: [['01', 'Operator session', 'Controls only the authenticated workspace.'], ['02', 'Server worker', 'Owns the active strategy lifecycle.'], ['03', 'Signed buyer feed', 'Carries entitlement; never carries API secrets.'], ['04', 'Desktop executor', 'Keeps buyer credentials at the buyer edge.']],
      cadence: [['Exchange stream', 'Sub-second', 98], ['Strategy cycle', '1–5 sec', 83], ['Status polling', 'Few seconds', 68], ['Reconciliation', 'Event + periodic', 49], ['Portfolio repair', 'On demand', 23]],
      ownership: [['Market data', 'Exchange adapter', 'Normalises quotes and candles.'], ['Live run', 'Background worker', 'Starts, stops and restores one strategy.'], ['Buyer keys', 'Desktop executor', 'Secrets do not cross the signed feed.'], ['History', 'Journal + portfolio store', 'Preserves decisions and external results.']]
    },
    phil: {
      name: 'PHILFORGE',
      title: 'Session-aware Indian-market execution path',
      subtitle: 'A user-scoped system for NSE equities and options, built around IST sessions, expiry rules and evidence-backed prices.',
      rule: 'Missing contract prices withhold unsafe P&L; they never become synthetic zeroes.',
      nodes: [
        ['01', 'Terminal', 'User workspace', 'Equity, Scalp, Cascade and backtest', 'MFA SESSION'],
        ['02', 'FastAPI', 'User-scoped gateway', 'Identity, action token and validation', 'PORT 8000/8001'],
        ['03', 'Engine registry', 'Per-user runtimes', 'Independent lifecycle and recovery state', 'ISOLATED'],
        ['04', 'Risk + contract', 'Market rule gate', 'Lots, expiry, costs and mode checks', 'IST AWARE'],
        ['05', 'Broker edge', 'Dhan + Upstox', 'Dhan execution; exact historical backfill', 'EXTERNAL'],
        ['06', 'Ledger', 'SQLite WAL', 'User state, trades, metrics and audit', 'DURABLE']
      ],
      metrics: [['MARKET', 'NSE / India'], ['PRIMARY BROKER', 'Dhan'], ['HISTORY SOURCE', 'Upstox gaps'], ['STATE MODEL', 'User-scoped']],
      io: [
        ['INPUT', ['Dhan ticks + broker state', 'User command + action token', 'Historical contract candles']],
        ['PROCESS', ['Resolve user + engine', 'Apply session / expiry / risk', 'Execute or simulate with costs']],
        ['OUTPUT', ['Order / position ledger', 'Charts + campaign status', 'Backtest evidence + metrics']]
      ],
      trust: [['01', 'Authenticated user', 'Every private action resolves a user ID.'], ['02', 'Action token', 'Sensitive commands need fresh proof of intent.'], ['03', 'Engine registry', 'One user cannot control another runtime.'], ['04', 'Broker boundary', 'Dhan remains authoritative for live orders.']],
      cadence: [['Live tick stream', 'Sub-second', 98], ['Scalp evaluation', 'Seconds', 84], ['Cascade polling', 'Seconds', 70], ['Candle close work', '5m / 15m / 1h', 43], ['Historical repair', 'Exact gaps', 24]],
      ownership: [['Identity', 'Session + user database', 'Scopes every record and runtime.'], ['Execution', 'Dhan adapter', 'Routes orders and reconciles status.'], ['Historical data', 'Cache then Upstox', 'Fetches only exact missing intervals.'], ['Analytics', 'Backtest + ledger services', 'Produces metrics from priced evidence.']]
    }
  };

  const q = (selector) => document.querySelector(selector);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function renderNodes(items) {
    const root = q('#flow-nodes');
    const fragment = document.createDocumentFragment();
    items.forEach(([number, title, subtitle, detail, meta]) => {
      const li = make('li', 'flow-node');
      li.append(make('div', 'node-icon', number));
      const card = make('div', 'node-card');
      card.append(make('span', '', subtitle), make('h3', '', title), make('p', '', detail), make('small', '', meta));
      li.append(card);
      fragment.append(li);
    });
    root.replaceChildren(fragment);
  }

  function renderMetrics(items) {
    const fragment = document.createDocumentFragment();
    items.forEach(([label, value]) => {
      const cell = make('div', 'metric');
      cell.append(make('span', '', label), make('strong', '', value));
      fragment.append(cell);
    });
    q('#metric-strip').replaceChildren(fragment);
  }

  function renderIo(columns) {
    const root = q('#io-map');
    const fragment = document.createDocumentFragment();
    columns.forEach(([label, entries], index) => {
      if (index) fragment.append(make('div', 'io-arrow', '→'));
      const column = make('div', 'io-column');
      column.append(make('strong', '', label));
      entries.forEach((entry) => column.append(make('div', 'io-chip', entry)));
      fragment.append(column);
    });
    root.replaceChildren(fragment);
  }

  function renderTrust(items) {
    const fragment = document.createDocumentFragment();
    items.forEach(([number, title, detail]) => {
      const li = make('li');
      li.append(make('b', '', number));
      const copy = make('span');
      const strong = make('strong', '', `${title} · `);
      copy.append(strong, document.createTextNode(detail));
      li.append(copy);
      fragment.append(li);
    });
    q('#trust-list').replaceChildren(fragment);
  }

  function renderCadence(items) {
    const fragment = document.createDocumentFragment();
    items.forEach(([label, value, width]) => {
      const row = make('div', 'cadence-row');
      const track = make('div', 'cadence-track');
      const bar = make('i');
      bar.style.setProperty('--bar', `${width}%`);
      track.append(bar);
      row.append(make('span', '', label), track, make('b', '', value));
      fragment.append(row);
    });
    q('#cadence-chart').replaceChildren(fragment);
  }

  function renderOwnership(items) {
    const fragment = document.createDocumentFragment();
    items.forEach(([label, owner, detail]) => {
      const row = make('div', 'owner-row');
      const copy = make('div');
      copy.append(make('strong', '', owner), make('small', '', detail));
      row.append(make('span', '', label), copy);
      fragment.append(row);
    });
    q('#ownership').replaceChildren(fragment);
  }

  function render(platform) {
    const view = views[platform] || views.estate;
    document.body.dataset.platform = platform;
    q('#active-view-name').textContent = view.name;
    q('#map-title').textContent = view.title;
    q('#map-subtitle').textContent = view.subtitle;
    q('#flow-rule').textContent = view.rule;
    renderNodes(view.nodes);
    renderMetrics(view.metrics);
    renderIo(view.io);
    renderTrust(view.trust);
    renderCadence(view.cadence);
    renderOwnership(view.ownership);
  }

  const tabs = [...document.querySelectorAll('[role="tab"][data-platform]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => {
      tabs.forEach((item) => {
        const selected = item === tab;
        item.setAttribute('aria-selected', String(selected));
        item.tabIndex = selected ? 0 : -1;
      });
      render(tab.dataset.platform);
    });
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let next = index;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      tabs[next].focus();
      tabs[next].click();
    });
  });

  const themeButton = q('#theme-toggle');
  const savedTheme = localStorage.getItem('forge-architecture-theme');
  if (savedTheme === 'light' || savedTheme === 'dark') document.documentElement.dataset.theme = savedTheme;
  themeButton.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('forge-architecture-theme', next);
    themeButton.setAttribute('aria-label', `Switch to ${next === 'light' ? 'dark' : 'light'} theme`);
  });

  render('estate');
})();
