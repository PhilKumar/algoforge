import { test, expect } from '@playwright/test';

test('remaining workspaces follow the Cascade hierarchy under a green appearance tint', async ({ page }) => {
  await page.goto('/strategy.html');
  await page.evaluate(() => {
    document.documentElement.setAttribute('data-pf-tint', 'forest');
  });

  const dark = await page.evaluate(() => {
    const color = (selector: string) => getComputedStyle(document.querySelector(selector)!).color;
    const custom = (selector: string, property: string) =>
      getComputedStyle(document.querySelector(selector)!).getPropertyValue(property).trim();
    return {
      dashboard: color('#dash-active-count'),
      builder: color('#builder-page label[style*="color: var(--accent)"]'),
      results: color('#results-page .analytics-inner h4'),
      resultsRisk: color('#res-risk'),
      live: color('#live-panels-container .ico'),
      portfolio: color('#portfolio-balance'),
      equityQuote: color('#stock-terminal-ltp'),
      equityOrder: color('#stock-unit-price'),
      insightsAccent: custom('#insights-page', '--accent'),
      charts: color('#charts-page #ch-content p[style*="color:var(--accent)"]'),
      refreshButton: color('#stock-terminal-page button[onclick="initStockTerminalPage(true)"]'),
      semanticWin: color('#dash-best-pnl'),
      semanticBuy: color('#stock-terminal-page button[onclick="submitStockTerminalOrder(\'BUY\')"]'),
    };
  });

  expect(dark).toEqual({
    dashboard: 'rgb(147, 197, 253)',
    builder: 'rgb(147, 197, 253)',
    results: 'rgb(147, 197, 253)',
    resultsRisk: 'rgb(251, 191, 36)',
    live: 'rgb(196, 181, 253)',
    portfolio: 'rgb(147, 197, 253)',
    equityQuote: 'rgb(147, 197, 253)',
    equityOrder: 'rgb(147, 197, 253)',
    insightsAccent: '#93c5fd',
    charts: 'rgb(147, 197, 253)',
    refreshButton: 'rgb(219, 234, 254)',
    semanticWin: 'rgb(52, 211, 153)',
    semanticBuy: 'rgb(52, 211, 153)',
  });

  const light = await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'light');
    const color = (selector: string) => getComputedStyle(document.querySelector(selector)!).color;
    const custom = (selector: string, property: string) =>
      getComputedStyle(document.querySelector(selector)!).getPropertyValue(property).trim();
    return {
      dashboard: color('#dash-active-count'),
      builder: color('#builder-page label[style*="color: var(--accent)"]'),
      results: color('#results-page .analytics-inner h4'),
      resultsRisk: color('#res-risk'),
      live: color('#live-panels-container .ico'),
      portfolio: color('#portfolio-balance'),
      equityQuote: color('#stock-terminal-ltp'),
      insightsAccent: custom('#insights-page', '--accent'),
      charts: color('#charts-page #ch-content p[style*="color:var(--accent)"]'),
      refreshButton: color('#stock-terminal-page button[onclick="initStockTerminalPage(true)"]'),
      semanticWin: color('#dash-best-pnl'),
      semanticBuy: color('#stock-terminal-page button[onclick="submitStockTerminalOrder(\'BUY\')"]'),
    };
  });

  expect(light).toEqual({
    dashboard: 'rgb(29, 78, 216)',
    builder: 'rgb(29, 78, 216)',
    results: 'rgb(29, 78, 216)',
    resultsRisk: 'rgb(146, 64, 14)',
    live: 'rgb(109, 40, 217)',
    portfolio: 'rgb(29, 78, 216)',
    equityQuote: 'rgb(29, 78, 216)',
    insightsAccent: '#1d4ed8',
    charts: 'rgb(29, 78, 216)',
    refreshButton: 'rgb(23, 37, 84)',
    semanticWin: 'rgb(4, 120, 87)',
    semanticBuy: 'rgb(6, 78, 59)',
  });
});
