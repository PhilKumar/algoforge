import { test, expect, Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const USERNAME = process.env.E2E_USERNAME || 'admin';
const PIN = process.env.E2E_PIN || '123456';

async function login(page: Page) {
  const response = await page.request.post('/api/auth/login', {
    data: { username: USERNAME, password: PIN },
  });
  expect(response.status()).toBe(200);
}

test('Architecture stays private and opens inside the normal application shell', async ({ page }) => {
  const unauthenticated = await page.request.get('/architecture', { maxRedirects: 0 });
  expect(unauthenticated.status()).toBe(401);

  await login(page);
  const legacyAtlas = await page.request.get('/architecture', { maxRedirects: 0 });
  expect(legacyAtlas.status()).toBe(307);
  expect(legacyAtlas.headers().location).toBe('/app#architecture/overview');

  await page.goto('/app#architecture/overview');
  await expect(page.locator('.header-shell')).toBeVisible();
  await expect(page.locator('#nav-architecture')).toHaveClass(/active/);
  await expect(page.locator('#architecture-page')).toHaveClass(/active-page/);
  await expect(page.locator('#architecture-page .pf-workspace-hero h1')).toHaveText('Architecture');
  const atlas = page.locator('philforge-architecture-atlas');
  await expect(atlas.locator('#flow-nodes .flow-node')).toHaveCount(6);

  const chapterLabels = {
    cryptoforge: ['Chapter 1 ·', 'Chapter 2 ·', 'Chapter 3 ·', 'Chapter 5 ·', 'Chapter 6 ·', 'Chapter 7 ·'],
    philforge: ['Chapter 1 ·', 'Chapter 2 ·', 'Chapter 4 ·', 'Chapter 5 ·', 'Chapter 6 ·', 'Chapter 7 ·'],
  };
  for (const platform of ['cryptoforge', 'philforge'] as const) {
    const legacyResponse = await page.request.get(`/architecture/docs/${platform}`, { maxRedirects: 0 });
    expect(legacyResponse.status()).toBe(307);
    expect(legacyResponse.headers().location).toBe(`/app#architecture/${platform}`);
    const oldReader = await page.request.get(`/architecture/${platform}`, { maxRedirects: 0 });
    expect(oldReader.status()).toBe(307);
    expect(oldReader.headers().location).toBe(`/app#architecture/${platform}`);

    await page.goto(`/app#architecture/${platform}`);
    await expect(page.locator('.header-shell')).toBeVisible();
    await expect(page.locator('#architecture-reader-panel')).toBeVisible();
    await expect(page.locator('#architecture-page iframe')).toHaveCount(0);
    const reader = page.locator('#architecture-reader-view');
    await expect(reader).toHaveAttribute('data-platform', platform);
    await expect(reader.locator('#document-body')).toHaveAttribute('aria-busy', 'false');
    expect(await reader.locator('.doc-section').count()).toBeGreaterThan(35);
    expect(await reader.locator('.doc-table').count()).toBeGreaterThan(12);
    await expect(reader.locator('.doc-section.is-chapter')).toHaveCount(6);
    await expect(reader.locator('#document-toc .toc-chapter')).toHaveCount(6);
    const renderedChapterLabels = await reader.locator('#document-toc .toc-chapter').allTextContents();
    expect(renderedChapterLabels.map((label) => label.match(/^Chapter \d+ ·/)?.[0])).toEqual(chapterLabels[platform]);
    await expect(reader.locator('.rail-card')).toContainText('Full blueprint complete');
    await expect(reader.locator('.reader-header')).toHaveCount(0);
    const ids = await reader.locator('.doc-section').evaluateAll((sections) => sections.map((section) => section.id));
    expect(new Set(ids).size).toBe(ids.length);
    await expect(reader.locator('.empty-search')).toHaveCount(0);
    if (platform === 'cryptoforge') {
      const publicOriginRow = reader.locator('.doc-table tr').filter({ hasText: 'Public origins' });
      await expect(publicOriginRow).toContainText('philforge.in/crypto');
      await expect(publicOriginRow).toContainText('crypto.philforge.in');
    }
  }
});

test('each blueprint chapter reveals only its own collapsed topic list', async ({ page }) => {
  await login(page);

  for (const platform of ['cryptoforge', 'philforge']) {
    await page.goto(`/app#architecture/${platform}`);
    const reader = page.locator('#architecture-reader-view');
    await expect(reader).toHaveAttribute('data-platform', platform);
    await expect(reader.locator('#document-body')).toHaveAttribute('aria-busy', 'false');

    const groups = reader.locator('#document-toc .toc-group');
    await expect(groups).toHaveCount(6);
    await expect(reader.locator('#document-toc > a')).toHaveText('Document scope');
    await expect(reader.locator('#document-toc .toc-group[open]')).toHaveCount(0);

    await groups.nth(0).locator('summary').click();
    await expect(groups.nth(0)).toHaveAttribute('open', '');
    expect(await groups.nth(0).locator('.toc-topics a').count()).toBeGreaterThan(1);

    await groups.nth(1).locator('summary').click();
    await expect(groups.nth(0)).not.toHaveAttribute('open', '');
    await expect(groups.nth(1)).toHaveAttribute('open', '');
    await expect(reader.locator('#document-toc .toc-group[open]')).toHaveCount(1);

    await groups.nth(2).locator('summary').focus();
    await groups.nth(2).locator('summary').press('Enter');
    await expect(groups.nth(1)).not.toHaveAttribute('open', '');
    await expect(groups.nth(2)).toHaveAttribute('open', '');

    await groups.nth(2).locator('.toc-topics a').nth(1).click();
    await expect(groups.nth(2)).toHaveAttribute('open', '');
    await expect.poll(() => page.evaluate(() => window.location.hash)).toBe(`#architecture/${platform}`);
  }
});

test('platform tabs redraw the system map and support keyboard navigation', async ({ page }) => {
  await login(page);
  await page.goto('/app#architecture/overview');

  await expect(page.locator('.topbar-brand-text')).toHaveText('PhilForge');
  const atlas = page.locator('philforge-architecture-atlas');
  const pulse = await atlas.locator('.flow-rail span').evaluate((node) => {
    const style = getComputedStyle(node);
    return { name: style.animationName, iterations: style.animationIterationCount, state: style.animationPlayState };
  });
  expect(pulse).toEqual({ name: 'railPulse', iterations: 'infinite', state: 'running' });

  const tabs = atlas.locator('[role="tab"]');
  await tabs.nth(1).click();
  await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true');
  await expect(atlas.locator('#map-title')).toContainText('24/7 digital-asset');
  await expect(atlas.locator('#flow-nodes')).toContainText('Binance');

  await tabs.nth(1).press('ArrowRight');
  await expect(tabs.nth(2)).toHaveAttribute('aria-selected', 'true');
  await expect(atlas.locator('#map-title')).toContainText('Indian-market');
  await expect(atlas.locator('#flow-nodes')).toContainText('Dhan + Upstox');
});

test('architecture atlas is accessible and does not overflow desktop or mobile', async ({ page }) => {
  await login(page);
  await page.goto('/app#architecture/overview');
  const atlas = page.locator('philforge-architecture-atlas');
  await expect(atlas.locator('#flow-nodes .flow-node')).toHaveCount(6);

  const desktopOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  expect(desktopOverflow).toBe(false);

  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.locator('#architecture-page .pf-workspace-hero h1')).toBeVisible();
  const mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  expect(mobileOverflow).toBe(false);
});

test('visual blueprint search filters sections without exposing Markdown downloads', async ({ page }) => {
  await login(page);
  await page.goto('/app#architecture/cryptoforge');
  const reader = page.locator('#architecture-reader-view');
  await expect(reader.locator('#document-body')).toHaveAttribute('aria-busy', 'false');

  await reader.locator('#blueprint-search').fill('Ed25519');
  await expect(reader.locator('#search-status')).toContainText('section');
  expect(await reader.locator('.doc-section:visible').count()).toBeGreaterThan(0);
  expect(await reader.locator('.doc-section[hidden]').count()).toBeGreaterThan(0);

  await reader.locator('#blueprint-search').fill('no-such-architecture-token');
  await expect(reader.locator('.empty-search')).toContainText('No blueprint section');
});

test('blueprint navigation stays fixed while each document scrolls', async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 900 });

  for (const platform of ['cryptoforge', 'philforge']) {
    await page.goto(`/app#architecture/${platform}`);
    const reader = page.locator('#architecture-reader-view');
    await expect(reader).toHaveAttribute('data-platform', platform);
    await expect(reader.locator('#document-body')).toHaveAttribute('aria-busy', 'false');
    const rail = reader.locator('.document-rail');
    await reader.locator('.reader-layout').evaluate((node) => {
      document.documentElement.style.scrollBehavior = 'auto';
      window.scrollTo(0, node.getBoundingClientRect().top + window.scrollY + 200);
    });
    await expect.poll(() => rail.evaluate((node) => Math.round(node.getBoundingClientRect().top))).toBe(112);

    const contentBefore = await reader.locator('.doc-section').nth(2).evaluate((node) => node.getBoundingClientRect().top);
    await page.evaluate(() => { window.scrollBy(0, 520); });
    await expect.poll(() => rail.evaluate((node) => Math.round(node.getBoundingClientRect().top))).toBe(112);

    await expect.poll(() => reader.locator('.doc-section').nth(2).evaluate((node) => node.getBoundingClientRect().top))
      .toBeLessThan(contentBefore - 400);
    await expect(rail.locator('.rail-sticky')).toHaveCSS('overflow-y', 'auto');
  }
});

test('both visual readers pass accessibility and responsive overflow checks', async ({ page }) => {
  await login(page);
  const platforms = [
    { id: 'cryptoforge', accent: '#f5b84b' },
    { id: 'philforge', accent: '#27d3b4' },
  ];
  for (const platform of platforms) {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`/app#architecture/${platform.id}`);
    const reader = page.locator('#architecture-reader-view');
    await expect(reader.locator('#document-body')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('.header-shell')).toBeVisible();
    await expect(reader.locator('.reader-header')).toHaveCount(0);
    const accent = await reader.evaluate((node) => getComputedStyle(node).getPropertyValue('--accent').trim());
    expect(accent).toBe(platform.accent);
    let overflow = await reader.evaluate((node) => node.scrollWidth > node.clientWidth + 1);
    expect(overflow).toBe(false);
    const results = await new AxeBuilder({ page }).include('#architecture-page').analyze();
    expect(results.violations).toEqual([]);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    const mobileReader = page.locator('#architecture-reader-view');
    await expect(mobileReader.locator('#document-body')).toHaveAttribute('aria-busy', 'false');
    overflow = await mobileReader.evaluate((node) => node.scrollWidth > node.clientWidth + 1);
    expect(overflow).toBe(false);
  }
});
