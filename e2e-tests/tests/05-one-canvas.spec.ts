/**
 * 05-one-canvas.spec.ts
 *
 * PhilForge has exactly ONE chart renderer and it addresses its drawing
 * surfaces by FIXED ids (`pf-bench-canvas-host` and friends). That means only
 * one chart may be mounted at a time — and the failure when two are is silent:
 * `getElementById` returns whichever host comes first in document order, so the
 * newer chart paints into the older one's canvas and the dialog you are looking
 * at stays blank. No console error, no failed request, nothing to grep for.
 *
 * It bit for real once the Equity page grew charts that stay in the DOM after
 * you navigate away (an expanded scanner row, the ladder overlay): opening a
 * scalp option chart afterwards drew nothing at all.
 *
 * This pins the invariant rather than the one path that broke.
 */
import { test, expect, Page } from '@playwright/test';

async function openEquity(page: Page) {
  await page.click('#nav-trading');
  await page.locator('.page-section.active-page [data-pf-trading-page="stock-terminal-page"]').click();
  await expect(page.locator('#stock-terminal-page')).toHaveClass(/active-page/);
}

const USERNAME = process.env.E2E_USERNAME || 'admin';
const PIN = process.env.E2E_PIN || '123456';

async function login(page: Page) {
  await page.goto('/app');
  await page.fill('#username-input', USERNAME);
  const pw = page.locator('#password-input');
  if (await pw.isVisible()) { await pw.fill(PIN); await page.click('#unlock-btn'); }
  else { for (const d of PIN.split('')) await page.click(`[data-val="${d}"]`); }
  await page.waitForSelector('.nav-tab', { timeout: 15_000 });
}

function dailyCandles(n = 60, start = 2149) {
  const rows = [];
  let base = start;
  for (let i = 0; i < n; i += 1) {
    base -= 3;
    rows.push({
      t: new Date(Date.UTC(2026, 4, 1 + i, 3, 45)).toISOString().replace('Z', ''),
      o: base, h: base + 8, l: base - 8, c: base + 2,
    });
  }
  return rows;
}

async function stubEquityScan(page: Page) {
  await page.route('**/api/terminal/cascade/scan?**', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      status: 'ok', scanned_at: new Date().toISOString(), universe: 223, no_history: 0,
      candidates: [{
        symbol: 'PHOENIXLTD', name: 'Phoenix Mills', last_price: 1908.7, strength_pct: 10.7,
        pullback_pct: 12.6, recent_high: 2149, affordable_shares: 104, rungs_fundable: 3,
        score: 1, etf: false,
      }],
      rejected_sample: [], rejected_total: 0, cached: false,
    }),
  }));
  await page.route('**/api/terminal/cascade/scan/chart?**', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      status: 'ok', symbol: 'PHOENIXLTD', name: 'Phoenix Mills', chart_mode: 'native_ohlc',
      timeframe: '1d', candles: dailyCandles(), recent_high: 2149,
      recent_high_lookback: 20, last_price: 1908.7, pullback_pct: 12.6,
    }),
  }));
}

async function stubScalpChart(page: Page, candles = true) {
  await page.route('**/api/scalp/trades/*/chart**', route => {
    const base = Math.floor(Date.UTC(2026, 7, 18, 3, 45) / 1000);
    const rows = [];
    let p = 200;
    for (let i = 0; i < 70; i += 1) {
      p += (i % 3 === 0 ? 2.5 : -1.2);
      rows.push({ t: base + i * 300, o: p, h: p + 3, l: p - 3, c: p + 1 });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        status: 'ok',
        instrument: { underlying: 'NIFTY', strike: 24300, option_type: 'CE', expiry: '2026-08-18' },
        live_price: 244.85, timeframe: '5m',
        candles: candles ? rows : [],
        lines: [], entries: [], exits: [],
      }),
    });
  });
}

/** Distinct colours painted into a canvas — 1 means it never drew. */
async function paintedColours(page: Page, selector: string) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel) as HTMLCanvasElement | null;
    if (!el || !el.width || !el.height) return 0;
    const ctx = el.getContext('2d');
    if (!ctx) return 0;
    const d = ctx.getImageData(0, 0, el.width, el.height).data;
    const seen = new Set<string>();
    for (let i = 0; i < d.length; i += 4) seen.add(`${d[i]},${d[i + 1]},${d[i + 2]}`);
    return seen.size;
  }, selector);
}

test('a scalp chart still paints with an Equity scan chart left open', async ({ page }) => {
  await stubEquityScan(page);
  await stubScalpChart(page);
  await login(page);

  // Open a scanner chart and deliberately LEAVE IT OPEN.
  await openEquity(page);
  await page.click('[data-equity-strategy="tworeds"]');
  await page.click('#tworeds-scan-run');
  await expect(page.locator('#tworeds-scan-body table')).toBeVisible({ timeout: 10_000 });
  await page.click('#tworeds-scan-body .cascade-scan-chart-btn');
  await expect(page.locator('#pf-bench-canvas-main')).toBeVisible({ timeout: 10_000 });

  await page.evaluate(async () => {
    // @ts-ignore — a global on this page
    await window.openScalpOptionChart(1);
    await new Promise(r => setTimeout(r, 800));
  });

  // The id must be unique again: the stale host is retired on mount.
  await expect(page.locator('#pf-bench-canvas-host')).toHaveCount(1);
  // And the scalp dialog's own canvas must have real content in it.
  const colours = await paintedColours(page, '#scalp-option-chart-canvas canvas');
  expect(colours).toBeGreaterThan(3);
});

test('a contract with no candles says so, and does not blame the renderer', async ({ page }) => {
  await stubScalpChart(page, false);
  await login(page);
  await page.evaluate(async () => {
    // @ts-ignore
    await window.openScalpOptionChart(1);
    await new Promise(r => setTimeout(r, 600));
  });
  const text = await page.locator('#scalp-option-chart-body').textContent();
  expect(text).toContain('No 5-minute candles');
  expect(text).not.toContain('renderer is unavailable');
});
