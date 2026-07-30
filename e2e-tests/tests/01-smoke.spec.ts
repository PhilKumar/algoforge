/**
 * 01-smoke.spec.ts
 * Smoke tests for PhilForge:
 *   1. Login via password-first auth shell
 *   2. Health endpoint returns OK
 *   3. Auth status reflects authenticated session
 */

import { test, expect, Page } from '@playwright/test';

const USERNAME = process.env.E2E_USERNAME || 'admin';
const PIN = process.env.E2E_PIN || '123456';
const OFFLINE_E2E = process.env.E2E_OFFLINE !== '0';
const BASE_ORIGIN = new URL(process.env.E2E_BASE_URL || process.env.BASE_URL || 'http://localhost:8000').origin;

const tickerMock = {
  status: 'ok',
  nifty: { price: 22000 },
  banknifty: { price: 47000 },
  midcpnifty: { price: 12000 },
  sensex: { price: 73000 },
};

const paperStatusMock = {
  running: false,
  in_trade: false,
  total_pnl: 0,
  trades_today: 0,
  positions: [],
  closed_trades: [],
  event_log: [],
};

const liveStatusMock = {
  running: false,
  in_trade: false,
  total_pnl: 0,
  trades_today: 0,
  positions: [],
  closed_trades: [],
  event_log: [],
};

const scalpStatusMock = {
  running: false,
  open_trades: [],
  closed_trades: [],
  events: [],
  session_pnl: 0,
};

const dashboardSummaryMock = {
  paper_flow: { pnl: 0, trades: 0 },
  real_flow: { pnl: 0, trades: 0, source_label: 'E2E mock' },
  paper_strategy_flow: {},
  live_strategy_flow: {},
  scalp_flow: {},
  active_count: 0,
  active_detail: 'No strategies running',
  strategy_count: 0,
  backtest_count: 0,
  best_run: null,
  worst_run: null,
  recent_transactions: [],
  running_engines: [],
  fii_dii: { status: 'unavailable' },
};

async function installOfflineE2E(page: Page) {
  if (!OFFLINE_E2E) return;

  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (!['http:', 'https:'].includes(url.protocol) || url.origin === BASE_ORIGIN) {
      await route.fallback();
      return;
    }
    if (url.hostname === 'fonts.googleapis.com') {
      await route.fulfill({ contentType: 'text/css', body: '' });
      return;
    }
    if (url.hostname === 'fonts.gstatic.com') {
      await route.fulfill({ status: 204, body: '' });
      return;
    }
    throw new Error(`Offline E2E blocked external request: ${url.href}`);
  });

  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/health' || path.startsWith('/api/auth/')) {
      await route.continue();
      return;
    }
    if (path.includes('/broker') || path.includes('/dhan')) {
      await route.fulfill({ json: { status: 'error', message: 'E2E offline broker mock', available_balance: 0, funds: {} } });
      return;
    }
    if (path === '/api/ticker') await route.fulfill({ json: tickerMock });
    else if (path === '/api/dashboard/summary') await route.fulfill({ json: dashboardSummaryMock });
    else if (path === '/api/backfill/status') await route.fulfill({ json: { status: 'idle', running: false } });
    else if (path === '/api/strategies') await route.fulfill({ json: [] });
    else if (path === '/api/strategies/folders') await route.fulfill({ json: [] });
    else if (path === '/api/runs') await route.fulfill({ json: [] });
    else if (path.startsWith('/api/runs/')) await route.fulfill({ json: { status: 'error', message: 'E2E offline run mock' } });
    else if (path === '/api/engines/all') await route.fulfill({ json: { engines: [] } });
    else if (path === '/api/expiry-dates') await route.fulfill({ json: { status: 'ok', nifty: '2026-05-07', banknifty: '2026-05-28', sensex: '2026-05-01' } });
    else if (path.startsWith('/api/expiry-list/')) await route.fulfill({ json: { status: 'ok', expiries: ['2026-05-07', '2026-05-14'] } });
    else if (path === '/api/option-ltp') await route.fulfill({ json: { status: 'ok', ltp: 110.5 } });
    else if (path === '/api/paper/status') await route.fulfill({ json: paperStatusMock });
    else if (path === '/api/live/status') await route.fulfill({ json: liveStatusMock });
    else if (path === '/api/scalp/status') await route.fulfill({ json: scalpStatusMock });
    else if (path === '/api/terminal/cascade/scan') {
      await route.fulfill({ json: { status: 'empty', cached: false, scan_date: '2026-07-29' } });
    }
    else if (path === '/api/cascade/paper/status') await route.fulfill({ json: { status: 'not_started', mode: 'paper', live_gate: { enabled: false } } });
    else if (path === '/api/fib-boundary/paper/status') await route.fulfill({ json: { status: 'not_started', mode: 'paper' } });
    else if (path === '/api/candle-entry/paper/status') await route.fulfill({ json: { status: 'not_started', mode: 'paper' } });
    else if (path === '/api/orders' || path === '/api/positions') await route.fulfill({ json: { status: 'success', data: [] } });
    else if (path === '/api/portfolio/history') await route.fulfill({ json: { status: 'success', monthly: {}, yearly: {} } });
    else throw new Error(`Offline E2E has no mock for ${request.method()} ${path}`);
  });
}

// ── Auth helper ─────────────────────────────────────────────
// Current login defaults to username + password, but we keep a fallback
// for explicit PIN mode in case a branch toggles that UI back on.
async function login(page: Page) {
  await installOfflineE2E(page);
  await page.goto('/');

  await page.fill('#username-input', USERNAME);

  const passwordInput = page.locator('#password-input');
  if (await passwordInput.isVisible()) {
    await passwordInput.fill(PIN);
    await page.click('#unlock-btn');
  } else {
    for (const digit of PIN.split('')) {
      await page.click(`[data-val="${digit}"]`);
    }
  }

  // Wait for the authenticated shell (nav bar rendered by strategy.html)
  await page.waitForSelector('.nav-tab', { timeout: 15_000 });
}

// ── Health check ─────────────────────────────────────────────
test('Health endpoint returns OK', async ({ request }) => {
  const resp = await request.get('/api/health');
  expect(resp.status()).toBe(200);
  const body = await resp.json();
  expect(body).toMatchObject({ status: 'ok' });
});

// ── Login ────────────────────────────────────────────────────
test('PIN-pad login succeeds and loads main app', async ({ page }) => {
  await login(page);
  // Nav tabs should be visible after successful authentication
  await expect(page.locator('.nav-tab').first()).toBeVisible();
});

// ── Auth status ──────────────────────────────────────────────
test('Auth status returns authenticated after login', async ({ page }) => {
  await login(page);
  const resp = await page.request.get('/api/auth/status');
  expect(resp.status()).toBe(200);
  const body = await resp.json();
  expect(body.authenticated).toBe(true);
});

test('Appearance presets switch and persist after reload', async ({ page }) => {
  await login(page);

  await page.click('#appearance-btn');
  await expect(page.locator('#appearance-modal')).toHaveClass(/open/);

  await page.click('[data-appearance-tint="native"]');
  await expect(page.locator('html')).not.toHaveAttribute('data-pf-tint');

  const tintPalettes: Record<string, string> = {};
  for (const tint of ['jade', 'cobalt', 'copper', 'fuchsia', 'lime']) {
    await page.click(`[data-appearance-tint="${tint}"]`);
    await expect(page.locator('html')).toHaveAttribute('data-pf-tint', tint);
    tintPalettes[tint] = await page.evaluate(() => {
      const root = getComputedStyle(document.documentElement);
      return [
        root.getPropertyValue('--bg').trim(),
        root.getPropertyValue('--card').trim(),
        root.getPropertyValue('--accent').trim(),
        getComputedStyle(document.body).backgroundImage,
      ].join('|');
    });
  }
  expect(new Set(Object.values(tintPalettes)).size).toBe(5);

  const fontStacks: Record<string, string> = {};
  for (const font of ['forge', 'atelier', 'exchange', 'blueprint', 'scribe']) {
    await page.click(`[data-appearance-font="${font}"]`);
    await expect(page.locator('html')).toHaveAttribute('data-pf-font', font);
    fontStacks[font] = await page.evaluate(() => {
      const root = getComputedStyle(document.documentElement);
      return [
        root.getPropertyValue('--font-body').trim(),
        root.getPropertyValue('--font-display').trim(),
        root.getPropertyValue('--font-mono').trim(),
      ].join('|');
    });
  }
  expect(new Set(Object.values(fontStacks)).size).toBe(5);

  await page.reload();
  await expect(page.locator('.nav-tab').first()).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('data-pf-tint', 'lime');
  await expect(page.locator('html')).toHaveAttribute('data-pf-font', 'scribe');
});

test('Cascade generated statuses remain legible in light mode', async ({ page }) => {
  await login(page);

  const statuses = await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'light');

    const app = window as typeof window & {
      _cascadeSetStatus?: (message: string, tone: string) => void;
      _setCandleEntryFormStatus?: (message: string, tone: string) => void;
      _renderCandleEntryStatus?: (payload: unknown) => void;
      _fibSetFormStatus?: (message: string, tone: string) => void;
    };
    if (!app._cascadeSetStatus || !app._setCandleEntryFormStatus || !app._renderCandleEntryStatus || !app._fibSetFormStatus) {
      throw new Error('Cascade status renderers are unavailable');
    }

    app._cascadeSetStatus('Signal replay is running', 'busy');
    app._setCandleEntryFormStatus('Historical 1H replay completed. Fixed-strike P&L is withheld.', 'success');
    app._renderCandleEntryStatus({
      campaign: {
        running: false,
        replay_complete: true,
        status: 'completed',
        contract: { underlying: 'NIFTY', strike: 24000, option_type: 'CE', expiry: '2026-08-06', lot_size: 65 },
        entry_stop: 23900,
        target_index: 24100,
        qualifying_reds: [],
        pricing_warning: 'Historical replay verifies index geometry only. Fixed-strike option P&L is withheld.',
      },
    });
    app._fibSetFormStatus('Replaying the index geometry...', 'busy');

    const color = (selector: string) => {
      const element = document.querySelector(selector);
      if (!element) throw new Error(`Missing ${selector}`);
      return getComputedStyle(element).color;
    };
    return {
      signalReplay: color('#cascade-form-status'),
      candleReplay: color('#candle-entry-form-status'),
      candleBadge: color('#candle-entry-badge'),
      candleWarning: color('#candle-entry-summary .is-warning'),
      fibBoundary: color('#fibx-form-status'),
    };
  });

  expect(statuses).toEqual({
    signalReplay: 'rgb(146, 64, 14)',
    candleReplay: 'rgb(4, 120, 87)',
    candleBadge: 'rgb(146, 64, 14)',
    candleWarning: 'rgb(146, 64, 14)',
    fibBoundary: 'rgb(146, 64, 14)',
  });
});

test('Appearance, mobile nav, and scalp launchpad match screenshots', async ({ page }) => {
  await login(page);

  await page.click('#appearance-btn');
  await expect(page.locator('#appearance-modal')).toHaveClass(/open/);
  await expect(page.locator('#appearance-modal .appearance-modal')).toHaveScreenshot('appearance-modal.png', {
    animations: 'disabled',
    maxDiffPixelRatio: 0.04,
  });
  await page.click('[data-pf-action="closeAppearanceModal"]');

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.nav-bar')).toBeVisible();
  await expect(page.locator('.nav-bar')).toHaveScreenshot('mobile-nav.png', {
    animations: 'disabled',
    maxDiffPixelRatio: 0.04,
  });

  await page.click('#nav-scalp');
  await expect(page.locator('#scalp-page')).toHaveClass(/active-page/);
  await expect(page.locator('#scalp-form-title')).toBeVisible();
  await expect(page.locator('#scalp-page')).toHaveScreenshot('scalp-launchpad.png', {
    animations: 'disabled',
    maxDiffPixelRatio: 0.04,
  });
});

// ── Test Bench ───────────────────────────────────────────────
// A blank chart is the failure this catches. The renderer is hand-written
// Canvas: a typo in a draw layer throws inside a paint loop, the surface stays
// empty, and every Python test still passes. So this asserts the semantic paint
// record — real candles, real geometry, real labels — not just that a canvas
// element exists.
const testBenchRunMock = {
  status: 'ok',
  strategy: 'fib',
  summary: {
    instrument: 'NIFTY',
    timeframe: '15m',
    outcome: 'Target hit',
    entry_timestamp: '2026-07-21T12:15:00',
    exit_timestamp: '2026-07-21T15:15:00',
    entry_count: 2,
    unpriced_entries: 0,
    spend_inr: 31200,
    net_pnl: 18400,
    costs_total: 620,
    strike: 24450,
    option_type: 'CE',
    expiry: '2026-08-04',
    lot_size: 65,
    underlying: 'NIFTY',
  },
  entries: [
    { timestamp: '2026-07-21T12:15:00', spot: 24450, option_price: 180, lots: 1, quantity: 65, level: 4, leg_id: 1, spend_inr: 11700, strike: 24450, option_type: 'CE' },
    { timestamp: '2026-07-21T13:15:00', spot: 24400, option_price: 150, lots: 2, quantity: 130, level: 8, leg_id: 1, spend_inr: 19500, strike: 24400, option_type: 'CE' },
  ],
  chart: {
    timeframe: '15m',
    candles: [
      { t: 1784017500, o: 24600, h: 24650, l: 24560, c: 24580, is_mother: true },
      { t: 1784018400, o: 24580, h: 24590, l: 24440, c: 24450, is_mother: false },
      { t: 1784019300, o: 24450, h: 24460, l: 24390, c: 24400, is_mother: false },
      { t: 1784020200, o: 24400, h: 24640, l: 24395, c: 24630, is_mother: false },
    ],
    mother: { high: 24650, low: 24560 },
    trendlines: [{ id: 1, a1: { t: 1784017500, p: 24650 }, a2: { t: 1784018400, p: 24590 }, active: true }],
    legs: [{
      leg_id: 1,
      touch_timestamp: 1784018400,
      touch_high: 24590,
      low: 24440,
      levels: { '0': 24590, '1': 24440, '2': 24500, '4': 24450, '8': 24400 },
      orders: [{ level: 4, inr_notional: 11700 }, { level: 8, inr_notional: 19500 }],
    }],
    entries: [{ t: 1784018400, price: 24450 }, { t: 1784019300, price: 24400 }],
    exits: [{ t: 1784020200, price: 24630, pnl: 18400 }],
    avg_entry_price: 24425,
    tp_price: 24625,
    tp_label: 'TARGET HIT',
  },
};

test('Test Bench draws one mother candle and every level it bought', async ({ page }) => {
  await login(page);
  await page.route('**/api/test-bench/run', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(testBenchRunMock) }));

  await page.click('#nav-test-bench');
  // The app upgrades every datetime-local input into its own read-only calendar
  // widget, so the value is set the way that widget sets it.
  await page.evaluate(() => {
    const input = document.getElementById('tb-mother') as HTMLInputElement;
    input.value = '2026-07-21T09:15';
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await page.click('#tb-run');
  await page.waitForSelector('#pf-bench-canvas-main', { timeout: 10_000 });

  const paint = await page.evaluate(() => {
    const app = window as typeof window & { _pfChartCanvas?: { paint?: Record<string, unknown> } };
    if (!app._pfChartCanvas || !app._pfChartCanvas.paint) throw new Error('The Test Bench canvas never painted');
    return app._pfChartCanvas.paint;
  });

  expect(paint).toMatchObject({ candles: 4, trendlines: 1, markers: 3 });
  const labels = paint.labelTexts as string[];
  // The two lines that decide whether the trade worked, and what it cost.
  expect(labels.some((text) => text.startsWith('TARGET HIT'))).toBe(true);
  expect(labels.some((text) => text.includes('₹11,700'))).toBe(true);
  expect(labels.some((text) => text.includes('₹19,500'))).toBe(true);

  // The verdict panel reads the same run in words.
  await expect(page.locator('#tb-verdict')).toContainText('Target hit');
  await expect(page.locator('#tb-verdict')).toContainText('₹31,200');
  await expect(page.locator('#tb-entries tbody tr')).toHaveCount(2);
});

test('Test Bench calendar offers only the minutes its timeframe can open on', async ({ page }) => {
  await login(page);
  await page.click('#nav-test-bench');

  // A 5-minute picker cannot express a 1m mother at all, and an every-minute
  // list on 1H is 59 choices that all fail with "no candle at that time".
  const minutesFor = async (timeframe: string) => {
    await page.selectOption('#tb-timeframe', timeframe);
    await page.click('#tb-mother');
    await page.waitForSelector('.pf-cascade-calendar:not([hidden])');
    const values = await page.$$eval('[data-pf-calendar-minute] option', (opts) =>
      opts.map((o) => (o as HTMLOptionElement).value));
    await page.click('[data-pf-calendar-cancel]');
    return values;
  };

  expect(await minutesFor('1m')).toHaveLength(60);
  expect(await minutesFor('15m')).toEqual(['0', '15', '30', '45']);
  // NSE opens at 09:15, so every 1H bar opens at :15 and no other minute.
  expect(await minutesFor('1h')).toEqual(['15']);

  // And a 1m timestamp survives the round trip through the picker.
  await page.selectOption('#tb-timeframe', '1m');
  await page.click('#tb-mother');
  await page.waitForSelector('.pf-cascade-calendar:not([hidden])');
  await page.selectOption('[data-pf-calendar-hour]', '10');
  await page.selectOption('[data-pf-calendar-minute]', '37');
  await page.click('[data-pf-calendar-apply]');
  expect(await page.inputValue('#tb-mother')).toMatch(/T10:37$/);
});

test('Test Bench switches cleanly between the two strategies', async ({ page }) => {
  await login(page);
  await page.click('#nav-test-bench');

  // Fib names the levels it buys; Two Red names the charts it climbs through.
  await page.selectOption('#tb-strategy', 'fib');
  await expect(page.locator('#tb-timeframe option[value="1m"]')).toHaveText(/L4/);
  await expect(page.locator('#tb-rung-field')).toBeVisible();

  await page.selectOption('#tb-strategy', 'two_red');
  await expect(page.locator('#tb-timeframe option[value="1m"]')).toHaveText(/1m → 5m → 15m → 1H/);
  // A 1H start has nothing above it, so it is a single trade and says so.
  await expect(page.locator('#tb-timeframe option[value="1h"]')).toHaveText(/^1H · 1H$/);
  // The rupee-per-level box is a fib control; the ladder sizes itself in lots.
  await expect(page.locator('#tb-rung-field')).toBeHidden();
  await expect(page.locator('#tb-explainer')).toContainText('two red candles');
});

test('Desktop nav keeps every tab visible and Test Bench in its slot', async ({ page }) => {
  // The nav positions tabs with per-id CSS order rules; a tab without one
  // lands at order 0 and renders on top of the brand panel. This is exactly
  // how the Test Bench tab shipped broken, so the desktop row is asserted
  // here the way a user sees it: positions, not markup.
  await page.setViewportSize({ width: 1600, height: 900 });
  await login(page);

  const box = async (selector: string) => {
    const b = await page.locator(selector).boundingBox();
    if (!b) throw new Error(`${selector} is not visible`);
    return b;
  };
  const brand = await box('.header-brand-panel');
  const cascade = await box('#nav-cascade');
  const bench = await box('#nav-test-bench');
  const insights = await box('#nav-insights-wrap');
  const results = await box('#nav-results');

  // Between Cascade and Insights, on the same row, clear of the brand panel.
  expect(bench.x).toBeGreaterThan(cascade.x + cascade.width - 1);
  expect(bench.x + bench.width).toBeLessThanOrEqual(insights.x + 1);
  expect(Math.abs(bench.y - cascade.y)).toBeLessThan(2);
  expect(bench.x).toBeGreaterThan(brand.x + brand.width - 1);
  // And the far end of the row must not run off the screen.
  expect(results.x + results.width).toBeLessThanOrEqual(1600);
});

test('Candle Entry tab offers the full ladder of starting charts', async ({ page }) => {
  await login(page);
  await page.click('#nav-cascade');
  await page.click('#oc-tabbtn-candle');

  // The four ladders, each named by the charts it climbs through.
  await expect(page.locator('#candle-entry-timeframe option')).toHaveCount(4);
  await expect(page.locator('#candle-entry-timeframe option[value="1m"]')).toHaveText(/1m → 5m → 15m → 1H/);
  await expect(page.locator('#candle-entry-timeframe option[value="1h"]')).toHaveText(/^1H · 1H$/);

  // Switching the chart retunes the mother calendar's minutes.
  await page.selectOption('#candle-entry-timeframe', '1h');
  await expect(page.locator('#candle-entry-mother-timestamp')).toHaveAttribute('data-pf-calendar-minutes', '15');
  await page.selectOption('#candle-entry-timeframe', '15m');
  await expect(page.locator('#candle-entry-mother-timestamp')).toHaveAttribute('data-pf-calendar-minutes', '0,15,30,45');

  // The copy sells the ladder, not the old single 1H buy.
  await expect(page.locator('#candle-entry-page-kicker, #options-cascade-page')).toContainText('TWO-RED LADDER');
});
