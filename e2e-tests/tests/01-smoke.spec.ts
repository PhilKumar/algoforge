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

// ── Auth helper ─────────────────────────────────────────────
// Current login defaults to username + password, but we keep a fallback
// for explicit PIN mode in case a branch toggles that UI back on.
async function login(page: Page) {
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

test('Appearance, mobile nav, and scalp launchpad screenshot surfaces render', async ({ page }, testInfo) => {
  await page.route('**/api/expiry-list/**', async route => {
    await route.fulfill({ json: { status: 'ok', expiries: ['2026-03-26', '2026-04-02'] } });
  });
  await page.route('**/api/ticker**', async route => {
    await route.fulfill({
      json: {
        status: 'ok',
        nifty: { price: 22000 },
        banknifty: { price: 47000 },
        midcpnifty: { price: 12000 },
        sensex: { price: 73000 },
      },
    });
  });
  await page.route('**/api/option-ltp**', async route => {
    await route.fulfill({ json: { status: 'ok', ltp: 110.5 } });
  });
  await page.route('**/api/scalp/status', async route => {
    await route.fulfill({ json: { running: false, open_trades: [], closed_trades: [], events: [], session_pnl: 0 } });
  });

  await login(page);

  await page.click('#appearance-btn');
  await expect(page.locator('#appearance-modal')).toHaveClass(/open/);
  await page.locator('#appearance-modal .appearance-modal').screenshot({
    path: testInfo.outputPath('appearance-modal.png'),
  });
  await page.click('[data-pf-action="closeAppearanceModal"]');

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.nav-bar')).toBeVisible();
  await page.locator('.nav-bar').screenshot({ path: testInfo.outputPath('mobile-nav.png') });

  await page.click('#nav-scalp');
  await expect(page.locator('#scalp-page')).toHaveClass(/active-page/);
  await expect(page.locator('#scalp-form-title')).toBeVisible();
  await page.locator('#scalp-page').screenshot({ path: testInfo.outputPath('scalp-launchpad.png') });
});
