/**
 * 04-two-red-equity.spec.ts
 *
 * The Equity page's second strategy. These are regression tests for the three
 * things that fail SILENTLY in this stack and so cannot be caught by reading
 * the code:
 *   1. A delegated action missing from PF_DELEGATED_ACTIONS — the click is
 *      simply ignored, with no console error.
 *   2. A renderer typo — the table comes out blank, and a green pytest run
 *      says nothing about it.
 *   3. The screen sending the Cascade's 1% pullback band instead of the
 *      ladder's 8% — the rows would look plausible and be the wrong scrips.
 */
import { test, expect, Page } from '@playwright/test';

const USERNAME = process.env.E2E_USERNAME || 'admin';
const PIN = process.env.E2E_PIN || '123456';

async function login(page: Page) {
  await page.goto('/app');
  await page.fill('#username-input', USERNAME);
  const passwordInput = page.locator('#password-input');
  if (await passwordInput.isVisible()) {
    await passwordInput.fill(PIN);
    await page.click('#unlock-btn');
  } else {
    for (const digit of PIN.split('')) await page.click(`[data-val="${digit}"]`);
  }
  await page.waitForSelector('.nav-tab', { timeout: 15_000 });
}

test('the Equity tab carries both strategies and switches between them', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (msg) => { if (msg.type() === 'error') errors.push(msg.text()); });
  page.on('pageerror', (err) => errors.push(String(err)));

  await login(page);

  // The tab is called Equity now, not Terminal.
  const tab = page.locator('#nav-terminal');
  await expect(tab).toContainText('Equity');
  await tab.click();

  const cascade = page.locator('#equity-strategy-cascade');
  const tworeds = page.locator('#equity-strategy-tworeds');
  const desk = page.locator('#equity-strategy-desk');

  // Cash Cascade is the default section.
  await expect(cascade).toBeVisible();
  await expect(tworeds).toBeHidden();
  await expect(desk).toBeHidden();

  // Switch to the ladder.
  await page.click('[data-equity-strategy="tworeds"]');
  await expect(tworeds).toBeVisible();
  await expect(cascade).toBeHidden();
  await expect(desk).toBeHidden();

  // The manual desk is its own section, not a footer under both strategies.
  // Its panels must not appear while a strategy is showing.
  await expect(page.locator('#stock-terminal-search')).toBeHidden();
  await page.click('[data-equity-strategy="desk"]');
  await expect(desk).toBeVisible();
  await expect(cascade).toBeHidden();
  await expect(tworeds).toBeHidden();
  await expect(page.locator('#stock-terminal-search')).toBeVisible();
  await expect(page.locator('#stock-terminal-orders-body')).toBeVisible();

  await page.click('[data-equity-strategy="tworeds"]');
  await expect(tworeds).toBeVisible();
  await expect(desk).toBeHidden();

  // Its own panels are really there.
  await expect(page.locator('#tworeds-scan-panel')).toBeVisible();
  await expect(page.locator('#tworeds-setup-panel')).toBeVisible();
  await expect(page.locator('#tworeds-campaigns-panel')).toBeVisible();
  await expect(page.locator('#tworeds-scan-run')).toBeVisible();

  // The defaults the backtest chose are the ones selected.
  await expect(page.locator('#tworeds-min-fall')).toHaveValue('8');
  await expect(page.locator('#tworeds-target')).toHaveValue('0.75');
  await expect(page.locator('#tworeds-mother-timeframe')).toHaveValue('1d');

  // Switch back.
  await page.click('[data-equity-strategy="cascade"]');
  await expect(cascade).toBeVisible();
  await expect(tworeds).toBeHidden();

  expect(errors.filter((e) => !/favicon|manifest|Failed to load resource/i.test(e))).toEqual([]);
});

test('the start button is wired through the delegated-action allowlist', async ({ page }) => {
  await login(page);
  await page.click('#nav-terminal');
  await page.click('[data-equity-strategy="tworeds"]');

  // No scrip typed: the handler must run and complain, which is the proof the
  // action reached it at all. A missing allowlist entry fails SILENTLY.
  await page.click('#tworeds-start');
  await expect(page.locator('#tworeds-form-status')).toContainText('Pick a scrip first', { timeout: 5000 });

  // Now a scrip but no mother.
  await page.fill('#tworeds-symbol', 'RELIANCE');
  await page.click('#tworeds-start');
  await expect(page.locator('#tworeds-form-status')).toContainText('mother candle', { timeout: 5000 });
});

test('the ladder screen renders its own columns and arithmetic', async ({ page }) => {
  const seen: string[] = [];
  await page.route('**/api/terminal/cascade/scan?**', async (route) => {
    const url = route.request().url();
    seen.push(url);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'ok',
        scanned_at: new Date().toISOString(),
        universe: 223,
        no_history: 0,
        candidates: [
          {
            symbol: 'PAYTM', name: 'One 97', last_price: 800, strength_pct: 12.5,
            pullback_pct: 20, recent_high: 1000, affordable_shares: 250,
            rungs_fundable: 3, score: 1, etf: false,
          },
        ],
        rejected_sample: [],
        rejected_total: 0,
        cached: false,
      }),
    });
  });

  await login(page);
  await page.click('#nav-terminal');
  await page.click('[data-equity-strategy="tworeds"]');
  await page.fill('#tworeds-scan-capital', '200000');
  await page.click('#tworeds-scan-run');

  const table = page.locator('#tworeds-scan-body table');
  await expect(table).toBeVisible({ timeout: 10_000 });

  // The screen must ask the server for the 8% band, not the Cascade's 1%.
  expect(seen.some((url) => url.includes('min_pullback=8'))).toBeTruthy();

  // Ladder columns, not Cascade columns.
  await expect(table).toContainText('First buy');
  await expect(table).toContainText('Target');
  await expect(table).not.toContainText('Rungs');

  // 20% down on a 200,000 purse commits 40,000, which is 50 shares at 800.
  const row = page.locator('#tworeds-scan-body tr[data-symbol="PAYTM"]');
  await expect(row).toContainText('₹40,000');
  await expect(row).toContainText('50');
  // Target = 800 + 0.75 x (1000 - 800) = 950.
  await expect(row).toContainText('₹950');

  // No "Use" button in ladder mode; clicking the row fills the campaign form.
  await expect(row.locator('.cascade-scan-pick')).toHaveCount(0);
  await row.locator('td').first().click();
  await expect(page.locator('#tworeds-symbol')).toHaveValue('PAYTM');
});

test('the two-red status endpoint answers and declares itself paper-only', async ({ page }) => {
  await login(page);
  const body = await page.evaluate(async () => {
    const res = await fetch('/api/two-red/status', { credentials: 'same-origin' });
    return { ok: res.ok, data: await res.json() };
  });
  expect(body.ok).toBeTruthy();
  expect(body.data.live.enabled).toBe(false);
  expect(body.data.defaults.min_fall_pct).toBe(8);
  expect(body.data.defaults.target_fraction).toBe(0.75);
  expect(Array.isArray(body.data.campaigns)).toBeTruthy();
});
