/**
 * 06-cascade-ladder.spec.ts
 *
 * Phil's rule for the cash Cascade: he names the mother, the campaign starts on
 * 15m, and the structure climbs 15m -> 1H -> 1D -> 1W as it ages, ending only
 * when the target is hit. The page has to SHOW that — which rung it is drawing
 * on now, and which it can still reach — because the alternative is a campaign
 * that quietly became a daily position while its chip still said 15M.
 *
 * Staying fixed is a real choice, not a fallback: measured over two years on 15
 * stocks, fixed 1H beat the climbing ladder. So both must be reachable and both
 * must render honestly.
 */
import { test, expect, Page } from '@playwright/test';

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

/** One campaign, mid-ladder: started on 15m, currently drawing 1D. */
function climbingCampaign() {
  return {
    status: 'ok',
    campaigns: [{
      running: true,
      status: 'TRENDLINE_ACTIVE',
      mode: 'paper',
      instrument: { symbol: 'ADANIENT', name: 'Adani Enterprises', signal_symbol: 'ADANIENT', reference_mode: 'own_scrip' },
      config: { capital_inr: 300000, timeframe: '15m', target_fraction: 0.25, product_type: 'CNC', escalates: true },
      structure: {
        timeframe: '1d', started_on: '15m', bars: 189, bars_to_next: 12,
        next_timeframe: '1w', escalated: true, climbs: true,
        ladder: ['15m', '1h', '1d', '1w'],
      },
      mother: { signal: { timestamp: '2026-02-02T09:15:00+05:30', high: 2600, low: 2540, open: 2560, close: 2550 },
                trade: { timestamp: '2026-02-02T09:15:00+05:30', high: 2600, low: 2540, open: 2560, close: 2550 } },
      average_entry_price: 2380, target_price: 2435, open_quantity: 14,
      open_invested_inr: 33320, pending_inr: 0, cash_carry_inr: 0,
      last_trade_close: 2401, last_trade_timestamp: '2026-08-12T15:15:00+05:30',
      rungs: [], open_fills: [], rounds: [], events: [],
      geometry: { trendlines: [], legs: [] },
    }],
  };
}

async function openEquityCascade(page: Page, status: object) {
  await page.route('**/api/terminal/cascade/status**', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(status),
  }));
  await login(page);
  await page.click('#nav-terminal');
  await page.click('[data-equity-strategy="cascade"]');
}

test('the setup form draws the ladder the campaign will climb', async ({ page }) => {
  await openEquityCascade(page, { status: 'ok', campaigns: [] });

  // 15m is the default start — Phil's rule, not 5m.
  await expect(page.locator('#terminal-cascade-timeframe')).toHaveValue('15m');
  await expect(page.locator('#terminal-cascade-escalates')).toHaveValue('1');

  const note = page.locator('#terminal-cascade-ladder-note');
  const rungs = note.locator('.terminal-cascade-rung');
  await expect(rungs).toHaveCount(4);
  await expect(rungs).toHaveText(['15M', '1H', '1D', '1W']);
  // The rung it starts on is the lit one.
  await expect(note.locator('.terminal-cascade-rung.is-now')).toHaveText('15M');
  // 4H is NOT a rung: an NSE session is 375 minutes.
  await expect(note).not.toContainText('4H');
});

test('a shorter start gives a shorter ladder', async ({ page }) => {
  await openEquityCascade(page, { status: 'ok', campaigns: [] });
  await page.selectOption('#terminal-cascade-timeframe', '1h');
  const rungs = page.locator('#terminal-cascade-ladder-note .terminal-cascade-rung');
  await expect(rungs).toHaveText(['1H', '1D', '1W']);
});

test('choosing to stay fixed says so instead of drawing a ladder', async ({ page }) => {
  await openEquityCascade(page, { status: 'ok', campaigns: [] });
  await page.selectOption('#terminal-cascade-escalates', '0');
  const note = page.locator('#terminal-cascade-ladder-note');
  await expect(note.locator('.terminal-cascade-ladder-fixed')).toHaveText('Fixed on 15M');
  await expect(note.locator('.terminal-cascade-rung')).toHaveCount(0);
});

test('the ladder choice reaches the API', async ({ page }) => {
  await openEquityCascade(page, { status: 'ok', campaigns: [] });
  await page.selectOption('#terminal-cascade-escalates', '0');
  // The mother field is PhilForge's own calendar widget — readonly by design, so
  // it cannot be typed into. And `_stockTerminalSelected` is a module-scope
  // binding, not a window property, so it cannot be set from here either; the
  // start function falls back to the hidden symbol input, which can.
  await page.evaluate(() => {
    const stamp = document.getElementById('terminal-cascade-mother-timestamp') as HTMLInputElement;
    stamp.value = '2026-08-03T09:15';
    (document.getElementById('stock-terminal-symbol') as HTMLInputElement).value = 'ADANIENT';
  });

  let sent: any = null;
  await page.route('**/api/terminal/cascade/start', route => {
    sent = JSON.parse(route.request().postData() || '{}');
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'started' }) });
  });

  await page.click('#terminal-cascade-start');
  await page.click('#confirm-ok-btn');
  await expect.poll(() => sent?.escalates).toBe(false);
  expect(sent.timeframe).toBe('15m');
});

test('a running campaign shows the rung it is on, not the one it started on', async ({ page }) => {
  await openEquityCascade(page, climbingCampaign());

  const card = page.locator('[data-terminal-cascade-symbol="ADANIENT"]');
  await expect(card).toBeVisible({ timeout: 10_000 });

  // The chip must not still claim 15M nine months in.
  await expect(card.locator('.pf-campaign-pill', { hasText: '15m → 1d' })).toBeVisible();

  await card.locator('summary').click();
  const strip = card.locator('.terminal-cascade-ladder-strip');
  await expect(strip.locator('.terminal-cascade-rung')).toHaveText(['15M', '1H', '1D', '1W']);
  await expect(strip.locator('[data-state="now"]')).toHaveText('1D');
  await expect(strip.locator('[data-state="done"]')).toHaveCount(2);   // 15M and 1H
  await expect(strip.locator('[data-state="ahead"]')).toHaveText('1W');

  await expect(card).toContainText('12 bars to 1W');
});

test('the stat strip is one row, whatever the campaign has to say', async ({ page }) => {
  // It was a `repeat(7, 1fr)` grid, so the eighth stat dropped onto a second
  // row and left one card sitting alone under the others. A figure strip that
  // grows must not be pinned to a column count.
  await openEquityCascade(page, climbingCampaign());
  const card = page.locator('[data-terminal-cascade-symbol="ADANIENT"]');
  await expect(card).toBeVisible({ timeout: 10_000 });
  await card.locator('summary').click();

  const cells = card.locator('.pf-campaign-stat');
  await expect(cells).toHaveCount(8);

  // Same top edge for every cell IS the definition of one row.
  const tops = await cells.evaluateAll((nodes) =>
    nodes.map((n) => Math.round(n.getBoundingClientRect().top)));
  expect(new Set(tops).size, `stats wrapped onto ${new Set(tops).size} rows`).toBe(1);

  // And every line inside a cell stays a single line.
  const wrapped = await cells.evaluateAll((nodes) => {
    const bad: string[] = [];
    nodes.forEach((cell) => {
      cell.querySelectorAll('.pf-campaign-stat-label, .pf-campaign-stat-value, .pf-campaign-stat-note')
        .forEach((el) => {
          const style = getComputedStyle(el);
          const lineHeight = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.2;
          if (el.getBoundingClientRect().height > lineHeight * 1.6) bad.push(el.textContent || '');
        });
    });
    return bad;
  });
  expect(wrapped, `wrapped onto two lines: ${wrapped.join(' | ')}`).toEqual([]);

  // The shortened note keeps its full explanation reachable.
  const drawing = cells.filter({ hasText: 'Drawing on' });
  await expect(drawing).toHaveAttribute('title', /climbs to 1W/);
});

test('a refusal shows the reason the server gave, not a generic failure', async ({ page }) => {
  // error_handlers.py answers 4xx as { success:false, error:{ code,title,message,detail } }.
  // The page read `data.detail`, found nothing, and printed its fallback — so
  // every fixable refusal on this page looked like an unexplained failure.
  await openEquityCascade(page, { status: 'ok', campaigns: [] });
  await page.evaluate(() => {
    (document.getElementById('terminal-cascade-mother-timestamp') as HTMLInputElement).value = '2026-08-03T09:15';
    (document.getElementById('stock-terminal-symbol') as HTMLInputElement).value = 'ADANIENT';
  });
  await page.route('**/api/terminal/cascade/start', route => route.fulfill({
    status: 400, contentType: 'application/json',
    body: JSON.stringify({
      success: false,
      error: { code: 400, title: 'Bad Request', message: 'The request could not be understood.',
               detail: '15m mother timestamp is not aligned.' },
    }),
  }));

  await page.click('#terminal-cascade-start');
  await page.click('#confirm-ok-btn');
  await expect(page.locator('#terminal-cascade-form-status')).toContainText('15m mother timestamp is not aligned');
  await expect(page.locator('#terminal-cascade-form-status')).not.toContainText('did not start');
});

test('changing the start timeframe snaps the mother onto that grid', async ({ page }) => {
  await openEquityCascade(page, { status: 'ok', campaigns: [] });
  const stamp = page.locator('#terminal-cascade-mother-timestamp');

  // 10:05 is a real 5m bar and NOT a real 15m one. It must move to 10:00, the
  // open of the 15m bar that contains it — grids run from 09:15, so 10:00 is on
  // the 15m grid (09:15, 09:30, 09:45, 10:00).
  await page.evaluate(() => {
    (document.getElementById('terminal-cascade-mother-timestamp') as HTMLInputElement).value = '2026-08-03T10:05';
  });
  await page.selectOption('#terminal-cascade-timeframe', '15m');
  await expect(stamp).toHaveValue('2026-08-03T10:00');

  // 1H is NSE aligned at :15, never :00.
  await page.selectOption('#terminal-cascade-timeframe', '1h');
  await expect(stamp).toHaveValue('2026-08-03T09:15');

  // A daily mother is always the 09:15 session open.
  await page.selectOption('#terminal-cascade-timeframe', '1d');
  await expect(stamp).toHaveValue('2026-08-03T09:15');
});
