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

test('Scalp follows the selected skin and reserves green for trading meaning', async ({ page }) => {
  await login(page);
  await page.waitForFunction(() => typeof (window as any)._renderScalpStatus === 'function');
  await page.evaluate(() => {
    // Magenta makes any stale fixed blue/purple workspace override obvious.
    (window as any).pfApplyAppearance({ tint: 'magenta' }, { persist: false });
    (window as any)._renderScalpStatus({
      running: false,
      session_pnl: 0,
      open_trades: [],
      closed_trades: [],
      file_trades: [],
      event_log: [
        { time: '09:20', type: 'entry', message: 'Order accepted' },
        { time: '09:35', type: 'exit', message: 'Target reached' },
        { time: '09:40', type: 'info', message: 'Feed refreshed' },
      ],
    });
  });

  const palette = await page.evaluate(() => {
    const color = (selector: string) => getComputedStyle(document.querySelector(selector)!).color;
    const eventColors = Array.from(document.querySelectorAll('#scalp-event-log > div span:last-child'))
      .map(el => getComputedStyle(el).color);
    return {
      title: color('#scalp-form-title'),
      quote: color('#scalp-live-ltp'),
      call: color('#scalp-option-toggle [data-tone="bull"]'),
      start: color('#scalp-start-btn'),
      buy: color('#scalp-page [onclick="submitScalpEntry(\'BUY\')"]'),
      eventColors,
    };
  });

  expect(palette.title).toBe('rgb(192, 132, 252)');
  expect(palette.quote).toBe('rgb(232, 121, 249)');
  expect(palette.call).toBe('rgb(232, 121, 249)');
  expect(palette.start).toBe('rgb(232, 121, 249)');
  expect(palette.eventColors).toEqual([
    'rgb(232, 121, 249)',
    'rgb(251, 191, 36)',
    'rgb(192, 132, 252)',
  ]);
  // BUY remains green because direction, targets and positive P&L are exactly
  // where the Cascade palette uses semantic green.
  expect(palette.buy).toBe('rgb(52, 211, 153)');

  const lightPalette = await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'light');
    const color = (selector: string) => getComputedStyle(document.querySelector(selector)!).color;
    return {
      title: color('#scalp-form-title'),
      quote: color('#scalp-live-ltp'),
      call: color('#scalp-option-toggle [data-tone="bull"]'),
      eventColors: Array.from(document.querySelectorAll('#scalp-event-log > div span:last-child'))
        .map(el => getComputedStyle(el).color),
    };
  });
  expect(lightPalette).toEqual({
    title: 'rgb(126, 34, 206)',
    quote: 'rgb(162, 28, 175)',
    call: 'rgb(162, 28, 175)',
    eventColors: [
      'rgb(162, 28, 175)',
      'rgb(146, 64, 14)',
      'rgb(126, 34, 206)',
    ],
  });
});
