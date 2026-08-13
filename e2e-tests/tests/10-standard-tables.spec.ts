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

test('every workspace and overlay uses the Cascade ledger table contract', async ({ page }) => {
  await login(page);

  const pageIds = [
    'dashboard-page',
    'builder-page',
    'results-page',
    'live-page',
    'portfolio-page',
    'stock-terminal-page',
    'options-cascade-page',
    'insights-page',
    'scalp-page',
    'charts-page',
  ];

  await page.evaluate((ids) => {
    for (const id of ids) {
      const host = document.getElementById(id)!;
      const probe = document.createElement('div');
      probe.innerHTML = `<table data-table-probe="${id}"><thead><tr><th>Price</th></tr></thead><tbody><tr><td>1,234.50</td></tr><tr><td>1,235.50</td></tr></tbody></table>`;
      host.appendChild(probe);
    }

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = '<table data-table-probe="overlay"><thead><tr><th>Price</th></tr></thead><tbody><tr><td>1,234.50</td></tr></tbody></table>';
    document.body.appendChild(overlay);
  }, pageIds);

  const styles = await page.evaluate((ids) => {
    const read = (name: string) => {
      const table = document.querySelector(`[data-table-probe="${name}"]`)!;
      const th = getComputedStyle(table.querySelector('th')!);
      const td = getComputedStyle(table.querySelector('td')!);
      return {
        tableWidth: getComputedStyle(table).width,
        headerPadding: `${th.paddingTop} ${th.paddingRight} ${th.paddingBottom} ${th.paddingLeft}`,
        cellPadding: `${td.paddingTop} ${td.paddingRight} ${td.paddingBottom} ${td.paddingLeft}`,
        headerPosition: th.position,
        headerTop: th.top,
        headerFontSize: th.fontSize,
        headerFontWeight: th.fontWeight,
        headerTransform: th.textTransform,
        headerLetterSpacing: th.letterSpacing,
        cellBorder: td.borderBottomWidth,
        numericVariant: td.fontVariantNumeric,
      };
    };
    return Object.fromEntries([...ids, 'overlay'].map(id => [id, read(id)]));
  }, pageIds);

  for (const name of [...pageIds, 'overlay']) {
    expect(styles[name].tableWidth).not.toBe('0px');
    expect(styles[name]).toMatchObject({
      headerPadding: '10px 13px 10px 13px',
      cellPadding: '10px 13px 10px 13px',
      headerPosition: 'sticky',
      headerTop: '0px',
      headerFontSize: '10px',
      headerFontWeight: '800',
      headerTransform: 'uppercase',
      headerLetterSpacing: '0.8px',
      cellBorder: '1px',
      numericVariant: 'tabular-nums',
    });
  }

  const light = await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'light');
    const th = getComputedStyle(document.querySelector('[data-table-probe="scalp-page"] th')!);
    const td = getComputedStyle(document.querySelector('[data-table-probe="scalp-page"] td')!);
    return { headerColor: th.color, headerBackground: th.backgroundImage, cellBorder: td.borderBottomColor };
  });

  expect(light.headerColor).toBe('rgb(71, 85, 105)');
  expect(light.headerBackground).toContain('rgb(248, 250, 252)');
  expect(light.headerBackground).toContain('rgb(233, 239, 247)');
  expect(light.cellBorder).toBe('rgba(15, 23, 42, 0.08)');
});
