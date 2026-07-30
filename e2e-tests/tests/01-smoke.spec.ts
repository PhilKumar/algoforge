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
