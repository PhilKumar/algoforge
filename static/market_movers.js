const MARKET_MOVERS_REFRESH_MS = 30000;
let moversNextRefreshAt = 0;
let moversRefreshTimer = null;
let moversCountdownTimer = null;
let latestPayload = null;
let marketMoversLoading = false;

function moversSunIcon() {
  return `
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4"></circle>
      <path d="M12 2v2"></path>
      <path d="M12 20v2"></path>
      <path d="m4.93 4.93 1.41 1.41"></path>
      <path d="m17.66 17.66 1.41 1.41"></path>
      <path d="M2 12h2"></path>
      <path d="M20 12h2"></path>
      <path d="m6.34 17.66-1.41 1.41"></path>
      <path d="m19.07 4.93-1.41 1.41"></path>
    </svg>
  `;
}

function moversMoonIcon() {
  return `
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"></path>
    </svg>
  `;
}

function applyMarketTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const toggle = document.getElementById('theme-toggle');
  if (toggle) toggle.innerHTML = theme === 'light' ? moversMoonIcon() : moversSunIcon();
}

function toggleMarketTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  applyMarketTheme(next);
  try {
    localStorage.setItem('algoforge_theme', next);
  } catch (e) {}
}

function formatCurrency(value) {
  const amount = Number(value || 0);
  return `₹${amount.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatSignedCurrency(value) {
  const amount = Number(value || 0);
  const sign = amount > 0 ? '+' : amount < 0 ? '-' : '';
  return `${sign}${formatCurrency(Math.abs(amount))}`;
}

function formatSignedPercent(value) {
  const amount = Number(value || 0);
  const sign = amount > 0 ? '+' : amount < 0 ? '' : '';
  return `${sign}${amount.toFixed(2)}%`;
}

function formatVolume(value) {
  const amount = Math.max(0, Number(value || 0));
  if (amount >= 1e7) return `${(amount / 1e7).toFixed(amount >= 5e7 ? 0 : 1)}Cr`;
  if (amount >= 1e5) return `${(amount / 1e5).toFixed(amount >= 5e5 ? 0 : 1)}L`;
  if (amount >= 1e3) return `${(amount / 1e3).toFixed(amount >= 5e3 ? 0 : 1)}K`;
  return amount.toLocaleString('en-IN');
}

function formatAsOf(value) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'medium',
    timeZone: 'Asia/Kolkata',
  }).format(date);
}

function sourceLabel(payload) {
  if (!payload) return 'Waiting for feed';
  if (payload.stale) return 'Cached Snapshot';
  if (payload.source === 'dhan_quote') return 'Dhan Multi-Quote';
  if (payload.source === 'yfinance_fallback') return 'Fallback Feed';
  return 'Market Snapshot';
}

function marketStatusLabel(payload) {
  if (!payload) return 'Loading live breadth...';
  if (payload.status !== 'ok') return 'Feed unavailable';
  if (payload.stale) return 'Serving cached snapshot';
  return 'Live Nifty 50 breadth';
}

function computeMedian(items) {
  const values = items
    .filter((item) => !item.unavailable)
    .map((item) => Number(item.change_pct || 0))
    .sort((a, b) => a - b);
  if (!values.length) return 0;
  const middle = Math.floor(values.length / 2);
  return values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

function computeIndustryMoves(items) {
  const grouped = new Map();
  items.filter((item) => !item.unavailable).forEach((item) => {
    const industry = item.industry || 'Other';
    const bucket = grouped.get(industry) || { industry, total: 0, count: 0, volume: 0 };
    bucket.total += Number(item.change_pct || 0);
    bucket.count += 1;
    bucket.volume += Number(item.volume || 0);
    grouped.set(industry, bucket);
  });
  return Array.from(grouped.values())
    .map((bucket) => ({
      ...bucket,
      change_pct: bucket.count ? bucket.total / bucket.count : 0,
    }))
    .sort((a, b) => b.change_pct - a.change_pct);
}

function tileSize(weight) {
  const value = Number(weight || 1);
  if (value >= 2.2) return 'tile-xl';
  if (value >= 1.7) return 'tile-lg';
  if (value >= 1.2) return 'tile-wide';
  if (value >= 1.05) return 'tile-md';
  return 'tile-sm';
}

function tileTone(item) {
  if (item.unavailable) return { rgb: '107, 114, 128', alpha: '0.10', state: 'is-unavailable' };
  const pct = Number(item.change_pct || 0);
  const intensity = Math.min(Math.abs(pct) / 4.5, 1);
  if (pct > 0) return { rgb: '49, 212, 191', alpha: (0.14 + intensity * 0.20).toFixed(3), state: 'is-positive' };
  if (pct < 0) return { rgb: '255, 123, 130', alpha: (0.14 + intensity * 0.20).toFixed(3), state: 'is-negative' };
  return { rgb: '147, 164, 188', alpha: '0.12', state: 'is-flat' };
}

function renderHero(cardId, item, priceId, absId, volumeId) {
  const card = document.getElementById(cardId);
  if (!card) return;
  const symbolEl = card.querySelector('.hero-symbol');
  const companyEl = card.querySelector('.hero-company');
  const changeEl = card.querySelector('.hero-change');
  const priceEl = document.getElementById(priceId);
  const absEl = document.getElementById(absId);
  const volumeEl = document.getElementById(volumeId);

  if (!item) {
    symbolEl.textContent = '--';
    companyEl.textContent = 'Awaiting quote';
    changeEl.textContent = '0.00%';
    priceEl.textContent = '--';
    absEl.textContent = '--';
    volumeEl.textContent = '--';
    return;
  }

  symbolEl.textContent = item.symbol || '--';
  companyEl.textContent = item.name || item.industry || 'Awaiting quote';
  changeEl.textContent = formatSignedPercent(item.change_pct);
  changeEl.classList.toggle('positive', Number(item.change_pct || 0) >= 0);
  changeEl.classList.toggle('negative', Number(item.change_pct || 0) < 0);
  priceEl.textContent = formatCurrency(item.price || 0);
  absEl.textContent = formatSignedCurrency(item.change || 0);
  volumeEl.textContent = formatVolume(item.volume || 0);
}

function renderRailList(containerId, items, emptyMessage) {
  const host = document.getElementById(containerId);
  if (!host) return;
  if (!items.length) {
    host.innerHTML = `<div class="heatmap-empty">${emptyMessage}</div>`;
    return;
  }
  host.innerHTML = items
    .map(
      (item, index) => `
        <div class="rail-row">
          <span class="rail-rank">${index + 1}</span>
          <div class="rail-main">
            <span class="rail-symbol">${item.symbol}</span>
            <span class="rail-company">${item.name}</span>
          </div>
          <span class="rail-move ${Number(item.change_pct || 0) >= 0 ? 'positive' : 'negative'}">${formatSignedPercent(item.change_pct)}</span>
        </div>
      `
    )
    .join('');
}

function renderIndustryList(items) {
  const host = document.getElementById('industry-list');
  if (!host) return;
  if (!items.length) {
    host.innerHTML = `<div class="heatmap-empty">Industry drift will appear once quotes arrive.</div>`;
    return;
  }
  host.innerHTML = items
    .slice(0, 6)
    .map(
      (item) => `
        <div class="industry-row">
          <div class="industry-main">
            <span class="industry-name">${item.industry}</span>
            <span class="industry-meta">${item.count} stocks • ${formatVolume(item.volume)} volume</span>
          </div>
          <span class="industry-move ${item.change_pct >= 0 ? 'positive' : 'negative'}">${formatSignedPercent(item.change_pct)}</span>
        </div>
      `
    )
    .join('');
}

function renderHeatmap(items) {
  const host = document.getElementById('heatmap-grid');
  if (!host) return;
  if (!items.length) {
    host.innerHTML = `<div class="heatmap-empty">The Nifty 50 mosaic is waiting for its first snapshot.</div>`;
    return;
  }

  const ranked = [...items].sort((a, b) => {
    const weightDelta = Number(b.weight || 0) - Number(a.weight || 0);
    if (Math.abs(weightDelta) > 0.0001) return weightDelta;
    return String(a.symbol || '').localeCompare(String(b.symbol || ''));
  });

  host.innerHTML = ranked
    .map((item) => {
      const tone = tileTone(item);
      return `
        <article class="heatmap-tile ${tileSize(item.weight)} ${tone.state}" style="--tile-rgb:${tone.rgb}; --tile-alpha:${tone.alpha};">
          <div class="tile-head">
            <div>
              <div class="tile-symbol">${item.symbol}</div>
              <div class="tile-industry">${item.industry || 'Industry'}</div>
            </div>
            <span class="tile-change ${Number(item.change_pct || 0) >= 0 ? 'positive' : 'negative'}">${item.unavailable ? 'No feed' : formatSignedPercent(item.change_pct)}</span>
          </div>
          <div class="tile-body">
            <div class="tile-price">${item.unavailable ? 'Awaiting quote' : formatCurrency(item.price || 0)}</div>
            <div class="tile-foot">
              <span>${item.name || ''}</span>
              <span>${formatVolume(item.volume || 0)}</span>
            </div>
          </div>
        </article>
      `;
    })
    .join('');
}

function updateBreadth(payload, industryMoves) {
  const items = Array.isArray(payload.items) ? payload.items : [];
  const breadth = payload.breadth || {};
  const advancers = Number(breadth.advancers || 0);
  const decliners = Number(breadth.decliners || 0);
  const flat = Number(breadth.flat || 0);
  const total = Math.max(1, advancers + decliners + flat);
  const median = computeMedian(items);

  document.getElementById('breadth-advancers').textContent = advancers;
  document.getElementById('breadth-decliners').textContent = decliners;
  document.getElementById('breadth-median').textContent = formatSignedPercent(median);
  document.getElementById('breadth-bar-advancers').style.width = `${(advancers / total) * 100}%`;
  document.getElementById('breadth-bar-flat').style.width = `${(flat / total) * 100}%`;
  document.getElementById('breadth-bar-decliners').style.width = `${(decliners / total) * 100}%`;

  const strongest = industryMoves[0];
  const weakest = industryMoves[industryMoves.length - 1];
  document.getElementById('market-strongest-industry').textContent = strongest ? `${strongest.industry} ${formatSignedPercent(strongest.change_pct)}` : '--';
  document.getElementById('market-weakest-industry').textContent = weakest ? `${weakest.industry} ${formatSignedPercent(weakest.change_pct)}` : '--';
}

function renderSnapshot(payload) {
  latestPayload = payload;
  const items = Array.isArray(payload.items) ? payload.items : [];
  const leaders = Array.isArray(payload.leaders) ? payload.leaders : [];
  const laggards = Array.isArray(payload.laggards) ? payload.laggards : [];
  const industryMoves = computeIndustryMoves(items);

  document.getElementById('market-source').textContent = sourceLabel(payload);
  document.getElementById('market-status').textContent = marketStatusLabel(payload);
  document.getElementById('market-as-of').textContent = formatAsOf(payload.as_of);
  document.getElementById('page-message').textContent = payload.message || 'Nifty 50 live breadth is synced to a standalone feed.';

  updateBreadth(payload, industryMoves);
  renderHero('top-gainer-card', leaders[0], 'top-gainer-price', 'top-gainer-abs', 'top-gainer-volume');
  renderHero('top-laggard-card', laggards[0], 'top-laggard-price', 'top-laggard-abs', 'top-laggard-volume');
  renderHeatmap(items);
  renderRailList('leaders-list', leaders, 'Positive movers will appear once data is available.');
  renderRailList('laggards-list', laggards, 'Negative movers will appear once data is available.');
  renderIndustryList(industryMoves);
}

function updateCountdown() {
  const age = document.getElementById('market-age');
  if (!age || !moversNextRefreshAt) return;
  const remaining = Math.max(0, Math.ceil((moversNextRefreshAt - Date.now()) / 1000));
  age.textContent = `Refresh in ${remaining}s`;
}

async function loadMarketMovers() {
  if (marketMoversLoading) return;
  marketMoversLoading = true;
  try {
    const response = await fetch('/api/market-movers/nifty50', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    if (response.status === 401) {
      window.location.href = '/';
      return;
    }
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const payload = await response.json();
    renderSnapshot(payload);
  } catch (error) {
    document.getElementById('market-status').textContent = 'Feed unavailable';
    document.getElementById('page-message').textContent = latestPayload
      ? 'Unable to refresh right now. Showing the latest available snapshot.'
      : 'Unable to fetch market movers right now.';
  } finally {
    marketMoversLoading = false;
    moversNextRefreshAt = Date.now() + MARKET_MOVERS_REFRESH_MS;
    updateCountdown();
  }
}

function startMarketMovers() {
  applyMarketTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark');
  document.getElementById('theme-toggle')?.addEventListener('click', toggleMarketTheme);
  loadMarketMovers();
  moversRefreshTimer = window.setInterval(loadMarketMovers, MARKET_MOVERS_REFRESH_MS);
  moversCountdownTimer = window.setInterval(updateCountdown, 1000);
}

document.addEventListener('DOMContentLoaded', startMarketMovers);
