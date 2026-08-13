import { test, expect } from '@playwright/test';

test('Scalp uses the Cascade hierarchy and reserves green for trading meaning', async ({ page }) => {
  await page.goto('/strategy.html');
  await page.waitForFunction(() => typeof (window as any)._renderScalpStatus === 'function');
  await page.evaluate(() => {
    // Forest is the strongest regression case: the user's global tint is green,
    // but Scalp's informational hierarchy must remain blue/purple/amber.
    document.documentElement.setAttribute('data-pf-tint', 'forest');
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

  expect(palette.title).toBe('rgb(196, 181, 253)');
  expect(palette.quote).toBe('rgb(147, 197, 253)');
  expect(palette.call).toBe('rgb(147, 197, 253)');
  expect(palette.start).toBe('rgb(219, 234, 254)');
  expect(palette.eventColors).toEqual([
    'rgb(147, 197, 253)',
    'rgb(251, 191, 36)',
    'rgb(196, 181, 253)',
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
    title: 'rgb(109, 40, 217)',
    quote: 'rgb(29, 78, 216)',
    call: 'rgb(29, 78, 216)',
    eventColors: [
      'rgb(29, 78, 216)',
      'rgb(146, 64, 14)',
      'rgb(109, 40, 217)',
    ],
  });
});
