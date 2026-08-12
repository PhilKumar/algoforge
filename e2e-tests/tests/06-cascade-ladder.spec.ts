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


/** A campaign pooling money it cannot yet spend: Rs 379.78 against a Rs 1,789 share. */
function underfundedCampaign() {
  const c: any = climbingCampaign();
  const row = c.campaigns[0];
  row.instrument = { symbol: 'ADANIENSOL', name: 'Adani Energy Solutions', signal_symbol: 'ADANIENSOL', reference_mode: 'own_scrip' };
  row.status = 'WAITING';
  row.config = { capital_inr: 10000, timeframe: '1h', target_fraction: 0.25, product_type: 'CNC', escalates: true };
  row.structure = { timeframe: '1h', started_on: '1h', bars: 106, bars_to_next: 94,
                    next_timeframe: '1d', escalated: false, climbs: true, ladder: ['1h', '1d', '1w'] };
  row.average_entry_price = 0; row.target_price = 0; row.open_quantity = 0;
  row.open_invested_inr = 0; row.pending_inr = 379.78; row.cash_carry_inr = 0;
  row.last_trade_close = 1789;
  row.open_fills = []; row.rounds = [];
  row.rungs = [
    { leg_id: 1, level: 2, signal_price: 1700, budget_inr: 379.78, status: 'COLLECTED' },
    { leg_id: 1, level: 4, signal_price: 1600, budget_inr: 569.67, status: 'PENDING' },
    { leg_id: 1, level: 8, signal_price: 1400, budget_inr: 949.45, status: 'PENDING' },
  ];
  return c;
}

test('a pool short of one share says so, instead of blaming red candles', async ({ page }) => {
  // Phil: "How can this go for a buy when it has only reached Rs 378 while the
  // stock price is Rs 1789?" He was right, and the ENGINE agreed with him — it
  // will not arm a buy-stop until the pool clears one share. The card was the
  // liar: it said "waiting for two red closes", naming a condition that was not
  // the blocker at all.
  //
  // These are the REAL ADANIENSOL numbers: Rs 379.78 pooled at L2, Rs 1,898.90
  // across the whole leg, one share Rs 1,789. It CAN buy — exactly one share,
  // and only once price has fallen through L8.
  await openEquityCascade(page, underfundedCampaign());
  const card = page.locator('[data-terminal-cascade-symbol="ADANIENSOL"]');
  await expect(card).toBeVisible({ timeout: 10_000 });
  await card.locator('summary').click();

  const waiting = card.locator('.pf-campaign-waiting');
  await expect(waiting).not.toContainText('two red closes');
  await expect(waiting).toContainText('short of one share');
  await expect(waiting).toContainText('1,409.22');   // 1,789.00 - 379.78
  await expect(waiting).not.toContainText('cannot buy');
});

test('a ladder that can never reach one share says THAT, and names the capital', async ({ page }) => {
  const payload: any = underfundedCampaign();
  // Halve the two rungs still to come: Rs 379.78 + Rs 759.56 never reaches
  // Rs 1,789, so no amount of waiting fixes it. That is a capital problem.
  payload.campaigns[0].rungs[1].budget_inr = 284.83;
  payload.campaigns[0].rungs[2].budget_inr = 474.73;
  await openEquityCascade(page, payload);
  const card = page.locator('[data-terminal-cascade-symbol="ADANIENSOL"]');
  await expect(card).toBeVisible({ timeout: 10_000 });
  await card.locator('summary').click();

  const waiting = card.locator('.pf-campaign-waiting');
  await expect(waiting).toContainText('cannot buy');
  await expect(waiting).toContainText('10,000');
  await expect(waiting).not.toContainText('two red closes');
});

/* ── The strategy, written on the page ────────────────────────────────
   Phil: "as we have worked on n number of strategies fine tuning many times,
   I want to know this in each page.. inside something like (i).. what is the
   final of this". So the doc is a deliverable, not decoration, and these
   tests hold it to two things a stale doc always fails: it must state the
   numbers the ENGINE uses, and it must not sell the ladder as free money. */

async function openStrategyDoc(page: Page) {
  await openEquityCascade(page, { status: 'ok', campaigns: [] });
  const doc = page.locator('#cash-cascade-rules');
  await expect(doc).toBeHidden();
  await page.click('[data-pf-info="cash-cascade-rules"]');
  await expect(doc).toBeVisible();
  return doc;
}

test('the (i) opens the complete Cash Cascade strategy', async ({ page }) => {
  const doc = await openStrategyDoc(page);
  // Every step of the loop, in the order the engine runs it.
  for (const heading of ['What gets on the list', 'The mother candle', 'Trendline',
                         'the fib rungs', 'How much money each leg gets', 'a rung does not buy',
                         'one target for the basket', 'how the campaign ages',
                         'How a campaign ends', 'What it costs']) {
    await expect(doc).toContainText(heading);
  }
  // And it closes again — a manual you cannot put away is in the way.
  await page.click('[data-pf-info="cash-cascade-rules"]');
  await expect(doc).toBeHidden();
});

test('the doc quotes the numbers the engine actually runs on', async ({ page }) => {
  const doc = await openStrategyDoc(page);
  const text = (await doc.innerText()).replace(/\s+/g, ' ');

  // Rungs and their split: LEVEL_ALLOCATION = {2: 0.20, 4: 0.30, 8: 0.50}.
  expect(text).toContain('L2, L4 and L8');
  expect(text).toMatch(/20% to L2, 30% to L4, 50% to L8/);
  // target_fraction 0.25, measured to the MOTHER HIGH, not a fixed percent.
  expect(text).toMatch(/0\.25 × \(mother high − average entry\)/);
  // The scanner's gates: min_price 200, 60-session trend, 20-session high, 1-25%.
  expect(text).toContain('60 sessions');
  expect(text).toContain('20 sessions');
  expect(text).toContain('1–25% below');
  // ESCALATION_BARS and the ladder, with 4h deliberately absent.
  expect(text).toContain('200 bars');
  expect(text).toMatch(/5m → 15m → 1H → 1D → 1W/);
  // CNC only, and the reason MTF is refused.
  expect(text).toContain('CNC');
  expect(text).toContain('never MTF');
});

test('the doc admits the climbing ladder measured worse than fixed 1H', async ({ page }) => {
  // The one paragraph a strategy page is most tempted to leave out. Both
  // figures come from the 15-stock, 2-year run and are in the config's own
  // comment; if that verdict is ever re-measured, this test is the reminder
  // that the page says it too.
  const doc = await openStrategyDoc(page);
  const warn = doc.locator('.pf-info-warn');
  await expect(warn).toBeVisible();
  await expect(warn).toContainText('19,804');
  await expect(warn).toContainText('32,764');
  await expect(warn).toContainText('fixed 1H');
});

test('on a wide screen the doc fills the card instead of hugging the left', async ({ page }) => {
  // Phil, on the first version: "Why one side.. Make it spread evenly on the
  // page.. Not only on the left". It was capped at 78ch, which kept the lines
  // readable and left half a widescreen empty. The measure moved to the
  // COLUMN, so the block fills the card and no line got longer.
  await page.setViewportSize({ width: 1800, height: 1100 });
  const doc = await openStrategyDoc(page);

  const columns = await doc.evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  expect(columns).toBeGreaterThan(1);

  const block = (await doc.boundingBox())!;
  const card = (await page.locator('#terminal-cascade-panel').boundingBox())!;
  expect(block.width).toBeGreaterThan(card.width * 0.9);

  // Filling the width must not have made the prose unreadable: a column has to
  // stay near a normal measure, which is what the min track size is for.
  const column = (await doc.locator('section').first().boundingBox())!;
  expect(column.width).toBeLessThan(620);
});

test('on a phone it is one column and the page never scrolls sideways', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 850 });
  const doc = await openStrategyDoc(page);
  const columns = await doc.evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  expect(columns).toBe(1);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
