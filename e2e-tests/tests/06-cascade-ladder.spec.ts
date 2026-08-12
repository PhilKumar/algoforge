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

/** How many columns the doc is actually rendered in, measured, not declared. */
async function renderedColumns(doc: any) {
  const xs = await doc.locator('section').evaluateAll(nodes =>
    nodes.map(n => Math.round(n.getBoundingClientRect().x)));
  return new Set(xs).size;
}

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

  expect(await renderedColumns(doc)).toBeGreaterThan(1);

  const block = (await doc.boundingBox())!;
  const card = (await page.locator('#terminal-cascade-panel').boundingBox())!;
  expect(block.width).toBeGreaterThan(card.width * 0.9);

  // Filling the width must not have made the prose unreadable: a column has to
  // stay near a normal measure, which is what the column width is for.
  const column = (await doc.locator('section').first().boundingBox())!;
  expect(column.width).toBeLessThan(620);
});

test('the steps pack down each column with no holes left under the short ones', async ({ page }) => {
  // Phil: "Move the 10 above correctly". The first fix used a GRID, which lays
  // out in rows, so every row was as tall as its tallest step and step 10 sat
  // alone under a third of a screen of white. Columns flow instead: within a
  // column, each step starts where the one above it ended.
  await page.setViewportSize({ width: 1800, height: 1100 });
  const doc = await openStrategyDoc(page);

  const boxes = await doc.locator('section').evaluateAll(nodes => nodes.map((n) => {
    const r = n.getBoundingClientRect();
    return { x: Math.round(r.x), top: r.top, bottom: r.bottom };
  }));
  const byColumn = new Map<number, { top: number; bottom: number }[]>();
  for (const box of boxes) {
    if (!byColumn.has(box.x)) byColumn.set(box.x, []);
    byColumn.get(box.x)!.push(box);
  }
  expect(byColumn.size).toBeGreaterThan(1);
  for (const column of byColumn.values()) {
    column.sort((a, b) => a.top - b.top);
    for (let i = 1; i < column.length; i += 1) {
      // The 15px section margin, and nothing like a row's worth of dead space.
      expect(column[i].top - column[i - 1].bottom).toBeLessThan(40);
    }
  }
});

test('on a phone it is one column and the page never scrolls sideways', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 850 });
  const doc = await openStrategyDoc(page);
  expect(await renderedColumns(doc)).toBe(1);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});

/* ── Preview Chart draws what the FORM says ───────────────────────────
   Phil: "why chart button is here... After loading the stock also it is not
   showing the correct chart." One fault behind both. The button passed no
   arguments and the resolver preferred a running campaign, then fell back to
   campaigns[0] — so with campaigns live it drew whichever stock was first in
   the list, at ITS mother, whatever you had picked. */

/** Pick a scrip the way the scanner does — `_stockTerminalSelected` is
 *  module-scope, so the only honest way in is the page's own selector. The
 *  page auto-selects the first name in the list, which is exactly the state
 *  the bug hid in: a scrip IS selected and the chart drew a campaign instead. */
async function pickScrip(page: Page) {
  // WAIT for the page's own auto-selection, do not race it. The scrip list is
  // fetched asynchronously and `selectStockTerminal` then writes BOTH the
  // module-scope `_stockTerminalSelected` and this input. A test that reads or
  // writes the input before that lands is overwritten a moment later — and the
  // resolver reads the module binding first, so no input write can win. Locally
  // the list is instant and the race never showed; CI is slower and it failed
  // three times.
  await page.waitForFunction(() => {
    const el = document.getElementById('stock-terminal-symbol') as HTMLInputElement | null;
    return !!(el && el.value);
  }, null, { timeout: 8_000 }).catch(() => { /* no list in this environment */ });

  const selected = await page.locator('#stock-terminal-symbol').inputValue();
  // Whatever the page selected IS the picked scrip — that is precisely the
  // state the bug hid in: a scrip is picked and the chart drew a campaign.
  if (selected) return selected;
  // No list at all: the hidden input is the same fallback Start Paper reads,
  // and with no list nothing will come along and overwrite it.
  await page.evaluate(() => {
    (document.getElementById('stock-terminal-symbol') as HTMLInputElement).value = 'INFY';
  });
  return 'INFY';
}

function twoRunningCampaigns() {
  const one = climbingCampaign().campaigns[0];
  const two = JSON.parse(JSON.stringify(one));
  two.instrument = { symbol: 'TATAPOWER', name: 'Tata Power', signal_symbol: 'TATAPOWER', reference_mode: 'own_scrip' };
  two.mother.signal.timestamp = '2026-03-09T09:15:00+05:30';
  return { status: 'ok', campaigns: [one, two] };
}

/** The symbol+mother the chart endpoint was actually asked for. */
async function chartRequestFor(page: Page, act: () => Promise<void>) {
  let asked: URLSearchParams | null = null;
  await page.route('**/api/terminal/cascade/chart**', route => {
    asked = new URL(route.request().url()).searchParams;
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ status: 'error' }) });
  });
  await act();
  await expect.poll(() => asked).not.toBeNull();
  return asked!;
}

test('Preview Chart draws the scrip you picked, not the first running campaign', async ({ page }) => {
  await openEquityCascade(page, twoRunningCampaigns());
  const picked = await pickScrip(page);
  expect(['ADANIENT', 'TATAPOWER']).not.toContain(picked);
  // Both fields in ONE evaluate immediately before the click: the scrip list
  // loads asynchronously and re-renders this input, so setting it earlier can
  // be undone under CI timing.
  await page.evaluate((symbol) => {
    (document.getElementById('stock-terminal-symbol') as HTMLInputElement).value = symbol;
    (document.getElementById('terminal-cascade-mother-timestamp') as HTMLInputElement).value = '2026-08-03T09:15';
  }, picked);

  const asked = await chartRequestFor(page, () => page.click('#terminal-cascade-chart-btn'));
  expect(asked.get('symbol')).toBe(picked);
  // ADANIENT's mother is 2026-02-02 — charting the typed one is the whole point.
  expect(asked.get('mother_timestamp')).toBe('2026-08-03T09:15');
  expect(asked.get('timeframe')).toBe('15m');
});

test('a picked scrip with no mother is refused, never quietly swapped', async ({ page }) => {
  await openEquityCascade(page, twoRunningCampaigns());
  const picked = await pickScrip(page);
  await page.evaluate((symbol) => {
    (document.getElementById('stock-terminal-symbol') as HTMLInputElement).value = symbol;
    (document.getElementById('terminal-cascade-mother-timestamp') as HTMLInputElement).value = '';
  }, picked);

  let called = false;
  await page.route('**/api/terminal/cascade/chart**', route => { called = true; return route.abort(); });
  await page.click('#terminal-cascade-chart-btn');

  const status = page.locator('#terminal-cascade-form-status');
  await expect(status).toContainText('mother candle');
  await expect(status).toContainText(picked);
  expect(called).toBe(false);
});

test('a campaign card still charts its own campaign', async ({ page }) => {
  // The cards pass symbol and mother explicitly; that path must be untouched.
  await openEquityCascade(page, twoRunningCampaigns());
  const card = page.locator('[data-terminal-cascade-symbol="TATAPOWER"]');
  await expect(card).toBeVisible({ timeout: 10_000 });
  await card.locator('summary').click();

  const asked = await chartRequestFor(page, () => card.getByRole('button', { name: 'Chart', exact: true }).first().click());
  expect(asked.get('symbol')).toBe('TATAPOWER');
  expect(asked.get('mother_timestamp')).toBe('2026-03-09T09:15:00+05:30');
});

test('with nothing picked it asks for a scrip, it does not chart a campaign', async ({ page }) => {
  // The same substitution in miniature: an empty form used to fall through to
  // campaigns[0]. Preview names the gap instead.
  //
  // The list is emptied BEFORE login, because the page auto-selects the first
  // name when it has one — and clearing the input afterwards would not undo
  // that: `_stockTerminalSelected` is a module-scope binding that survives it,
  // and it is what the resolver reads first.
  await page.route('**/api/terminal/nifty200**', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', data: [] }),
  }));
  await openEquityCascade(page, twoRunningCampaigns());
  let called = false;
  await page.route('**/api/terminal/cascade/chart**', route => { called = true; return route.abort(); });
  // A mother IS given, so a refusal can only be about the missing scrip.
  await page.evaluate(() => {
    (document.getElementById('terminal-cascade-mother-timestamp') as HTMLInputElement).value = '2026-08-03T09:15';
  });
  await page.click('#terminal-cascade-chart-btn');
  await expect(page.locator('#terminal-cascade-form-status')).toContainText('Pick a scrip');
  expect(called).toBe(false);
});

/* ── The mother you LOOKED at is the mother you START ─────────────────
   Phil, on a KALYANKJIL scanner chart showing an unbroken 1D high:
   "But KALYANKJIL in this chart is high not broken correct? Then why I
   cannot see that on the selected chart?"

   Because a campaign's mother is the candle at that timestamp ON ITS OWN
   TIMEFRAME. He read 648.95 off the 1D chart while the form sat on its 15m
   default, so the campaign took the 15m bar at that stamp — a different,
   much lower high, broken within the hour. The scanner hint was TEXT; it
   carries the timeframe now. */

function scanChart(timeframe: string) {
  const day = timeframe === '1d';
  const candles = [];
  for (let i = 0; i < 40; i += 1) {
    // IST-offset stamps, exactly as the endpoint sends them (bar.timestamp
    // .isoformat() on IST-aware candles). A Z-suffixed UTC stamp here would
    // make the fixture disagree with production by five and a half hours.
    const stamp = day
      ? `2026-07-${String(1 + i).padStart(2, '0')}T09:15:00+05:30`
      : `2026-08-11T${String(9 + Math.floor(i / 4)).padStart(2, '0')}:${['15', '30', '45', '00'][i % 4]}:00+05:30`;
    const high = i === 30 ? (day ? 648.95 : 570.7) : 560 + i;
    candles.push({ t: stamp, o: high - 8, h: high, l: high - 12, c: high - 5 });
  }
  return {
    status: 'ok', symbol: 'KALYANKJIL', name: 'Kalyan Jewellers', chart_mode: 'native_ohlc',
    timeframe, candles, recent_high: 648.95, recent_high_lookback: 20,
    recent_high_date: '2026-08-11', high_in_view: true, last_price: 602.2, pullback_pct: 7.8,
  };
}

async function openScanChart(page: Page, timeframe: string) {
  await page.route('**/api/terminal/cascade/scan/chart**', route => {
    const tf = new URL(route.request().url()).searchParams.get('timeframe') || '1d';
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(scanChart(tf)) });
  });
  await page.route('**/api/terminal/cascade/scan**', route => {
    if (route.request().url().includes('/scan/chart')) return route.fallback();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      status: 'ok', capital_inr: 100000, generated_at: '2026-08-12T20:00:00+05:30',
      candidates: [{ symbol: 'KALYANKJIL', name: 'Kalyan Jewellers', last_price: 602.2, strength_pct: 74,
        pullback_pct: 7.8, recent_high: 648.95, affordable_shares: 166, rungs_fundable: 3, score: 13.5, etf: false }],
      rejected: [],
    }) });
  });
  await openEquityCascade(page, { status: 'ok', campaigns: [] });
  await page.click('#cascade-scan-run');
  // Scoped to the CASCADE scanner: the ladder screen renders its own table for
  // the same scrip, and the chart row this opens is itself a tr[data-symbol].
  const row = page.locator('#cascade-scan-body .cascade-scan-table tr[data-symbol="KALYANKJIL"]')
    .filter({ hasNot: page.locator('.cascade-scan-chart') }).first();
  await expect(row).toBeVisible({ timeout: 15_000 });
  await row.locator('.cascade-scan-chart-btn').click();
  if (timeframe !== '1d') await page.click(`#cascade-scan-body [data-scan-tf="${timeframe}"]`);
  await expect(page.locator('#cascade-scan-body .cascade-scan-mother-hint')).toBeVisible({ timeout: 15_000 });
}

test('Use this mother carries the timeframe it was read on, not just the stamp', async ({ page }) => {
  await openScanChart(page, '1d');
  // The form starts on 15m — the default that caused this.
  await expect(page.locator('#terminal-cascade-timeframe')).toHaveValue('15m');

  await page.click('#cascade-scan-body .cascade-scan-use-mother');

  // Both halves must land, and the timeframe is the half that was missing.
  await expect(page.locator('#terminal-cascade-timeframe')).toHaveValue('1d');
  await expect(page.locator('#terminal-cascade-mother-timestamp')).toHaveValue('2026-07-31T09:15');
  // And it says which candle the campaign will actually read.
  await expect(page.locator('#cascade-scan-status')).toContainText('1D');
});

test('a chart-only timeframe offers no button, and says why', async ({ page }) => {
  // 4H and 1W draw fine but no campaign can start on them: the ladder skips 4H
  // (a 375-minute session) and starts no higher than 1D. Filling the form with
  // one would produce a start the server must reject.
  await openScanChart(page, '1w');
  await expect(page.locator('#cascade-scan-body .cascade-scan-use-mother')).toHaveCount(0);
  await expect(page.locator('#cascade-scan-body .cascade-scan-mother-hint')).toContainText('cannot start on 1W');
});

test('a mother that is already spent is refused with the reason', async ({ page }) => {
  await openEquityCascade(page, { status: 'ok', campaigns: [] });
  await page.route('**/api/terminal/cascade/start', route => route.fulfill({
    status: 400, contentType: 'application/json',
    body: JSON.stringify({ success: false, error: { code: 'bad_request', title: 'Cannot start',
      message: 'Cannot start',
      detail: 'That 15m mother is spent — price has already traded above its high of 570.70, so no fib leg can form and the campaign would be over before it started.' } }),
  }));
  await page.evaluate(() => {
    (document.getElementById('terminal-cascade-mother-timestamp') as HTMLInputElement).value = '2026-07-21T13:15';
  });
  await page.click('#terminal-cascade-start');
  await page.click('#confirm-ok-btn');
  // The server's reason, not "Could not start the campaign."
  await expect(page.locator('#terminal-cascade-form-status')).toContainText('already traded above its high', { timeout: 10_000 });
});
