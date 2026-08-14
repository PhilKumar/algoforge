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

test('architecture routes stay private and both visual blueprints are available', async ({ page }) => {
  const unauthenticated = await page.goto('/architecture');
  expect(unauthenticated?.status()).toBe(401);

  await login(page);
  const response = await page.goto('/architecture');
  expect(response?.status()).toBe(200);
  await expect(page.locator('#page-title')).toContainText('Two platforms');

  const chapterLabels = {
    cryptoforge: ['Chapter 1 ·', 'Chapter 2 ·', 'Chapter 3 ·', 'Chapter 5 ·', 'Chapter 6 ·', 'Chapter 7 ·'],
    philforge: ['Chapter 1 ·', 'Chapter 2 ·', 'Chapter 4 ·', 'Chapter 5 ·', 'Chapter 6 ·', 'Chapter 7 ·'],
  };
  for (const platform of ['cryptoforge', 'philforge'] as const) {
    const legacyResponse = await page.request.get(`/architecture/docs/${platform}`, { maxRedirects: 0 });
    expect(legacyResponse.status()).toBe(307);
    expect(legacyResponse.headers().location).toBe(`/architecture/${platform}`);

    const documentResponse = await page.goto(`/architecture/${platform}`);
    expect(documentResponse?.status()).toBe(200);
    await expect(page.locator('body')).toHaveAttribute('data-platform', platform);
    await expect(page.locator('#document-body')).toHaveAttribute('aria-busy', 'false');
    expect(await page.locator('.doc-section').count()).toBeGreaterThan(35);
    expect(await page.locator('.doc-table').count()).toBeGreaterThan(12);
    await expect(page.locator('.doc-section.is-chapter')).toHaveCount(6);
    await expect(page.locator('#document-toc .toc-chapter')).toHaveCount(6);
    const renderedChapterLabels = await page.locator('#document-toc .toc-chapter').allTextContents();
    expect(renderedChapterLabels.map((label) => label.match(/^Chapter \d+ ·/)?.[0])).toEqual(chapterLabels[platform]);
    await expect(page.locator('.rail-card')).toContainText('Full blueprint complete');
    await expect(page.locator('.chapter-gate')).toContainText('The full blueprint is ready');
    const ids = await page.locator('.doc-section').evaluateAll((sections) => sections.map((section) => section.id));
    expect(new Set(ids).size).toBe(ids.length);
    await expect(page.locator('.empty-search')).toHaveCount(0);
    if (platform === 'cryptoforge') {
      const publicOriginRow = page.locator('.doc-table tr').filter({ hasText: 'Public origins' });
      await expect(publicOriginRow).toContainText('philforge.in/crypto');
      await expect(publicOriginRow).toContainText('crypto.philforge.in');
    }
  }
});

test('each blueprint chapter reveals only its own collapsed topic list', async ({ page }) => {
  await login(page);

  for (const platform of ['cryptoforge', 'philforge']) {
    await page.goto(`/architecture/${platform}`);
    await expect(page.locator('#document-body')).toHaveAttribute('aria-busy', 'false');

    const groups = page.locator('#document-toc .toc-group');
    await expect(groups).toHaveCount(6);
    await expect(page.locator('#document-toc > a')).toHaveText('Document scope');
    await expect(page.locator('#document-toc .toc-group[open]')).toHaveCount(0);

    await groups.nth(0).locator('summary').click();
    await expect(groups.nth(0)).toHaveAttribute('open', '');
    expect(await groups.nth(0).locator('.toc-topics a').count()).toBeGreaterThan(1);

    await groups.nth(1).locator('summary').click();
    await expect(groups.nth(0)).not.toHaveAttribute('open', '');
    await expect(groups.nth(1)).toHaveAttribute('open', '');
    await expect(page.locator('#document-toc .toc-group[open]')).toHaveCount(1);

    await groups.nth(2).locator('summary').focus();
    await groups.nth(2).locator('summary').press('Enter');
    await expect(groups.nth(1)).not.toHaveAttribute('open', '');
    await expect(groups.nth(2)).toHaveAttribute('open', '');

    await groups.nth(2).locator('.toc-topics a').nth(1).click();
    await expect(groups.nth(2)).toHaveAttribute('open', '');
    await expect.poll(() => page.evaluate(() => window.location.hash)).not.toBe('');
  }
});

test('platform tabs redraw the system map and support keyboard navigation', async ({ page }) => {
  await login(page);
  await page.goto('/architecture');

  await expect(page.locator('.brand strong')).toHaveText('PHILFORGE');
  await expect(page.locator('.brand-logo')).toBeVisible();
  const pulse = await page.locator('.flow-rail span').evaluate((node) => {
    const style = getComputedStyle(node);
    return { name: style.animationName, iterations: style.animationIterationCount, state: style.animationPlayState };
  });
  expect(pulse).toEqual({ name: 'railPulse', iterations: 'infinite', state: 'running' });

  const tabs = page.locator('[role="tab"]');
  await tabs.nth(1).click();
  await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#map-title')).toContainText('24/7 digital-asset');
  await expect(page.locator('#flow-nodes')).toContainText('Binance');

  await tabs.nth(1).press('ArrowRight');
  await expect(tabs.nth(2)).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#map-title')).toContainText('Indian-market');
  await expect(page.locator('#flow-nodes')).toContainText('Dhan + Upstox');
});

test('architecture atlas is accessible and does not overflow desktop or mobile', async ({ page }) => {
  await login(page);
  await page.goto('/architecture');
  await expect(page.locator('#flow-nodes .flow-node')).toHaveCount(6);

  const desktopOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  expect(desktopOverflow).toBe(false);

  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.locator('#page-title')).toBeVisible();
  const mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  expect(mobileOverflow).toBe(false);
});

test('visual blueprint search filters sections without exposing Markdown downloads', async ({ page }) => {
  await login(page);
  await page.goto('/architecture/cryptoforge');
  await expect(page.locator('#document-body')).toHaveAttribute('aria-busy', 'false');

  await page.fill('#blueprint-search', 'Ed25519');
  await expect(page.locator('#search-status')).toContainText('section');
  expect(await page.locator('.doc-section:visible').count()).toBeGreaterThan(0);
  expect(await page.locator('.doc-section[hidden]').count()).toBeGreaterThan(0);

  await page.fill('#blueprint-search', 'no-such-architecture-token');
  await expect(page.locator('.empty-search')).toContainText('No blueprint section');
});

test('blueprint navigation stays fixed while each document scrolls', async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 900 });

  for (const platform of ['cryptoforge', 'philforge']) {
    await page.goto(`/architecture/${platform}`);
    await expect(page.locator('#document-body')).toHaveAttribute('aria-busy', 'false');

    const rail = page.locator('.document-rail');
    await page.evaluate(() => {
      document.documentElement.style.scrollBehavior = 'auto';
      const layout = document.querySelector('.reader-layout');
      if (layout) window.scrollTo(0, layout.getBoundingClientRect().top + window.scrollY + 200);
    });
    await expect.poll(async () => Math.round((await rail.boundingBox())?.y || 0)).toBe(95);

    const contentBefore = await page.locator('.doc-section').nth(2).boundingBox();
    await page.evaluate(() => window.scrollBy(0, 520));
    await expect.poll(async () => Math.round((await rail.boundingBox())?.y || 0)).toBe(95);

    expect(contentBefore).not.toBeNull();
    await expect.poll(async () => (await page.locator('.doc-section').nth(2).boundingBox())?.y || 0)
      .toBeLessThan(contentBefore!.y - 400);
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
    await page.goto(`/architecture/${platform.id}`);
    await expect(page.locator('#document-body')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('.reader-brand strong')).toHaveText('PHILFORGE');
    await expect(page.locator('.reader-brand .brand-logo')).toBeVisible();
    const accent = await page.locator('body').evaluate((node) => getComputedStyle(node).getPropertyValue('--accent').trim());
    expect(accent).toBe(platform.accent);
    let overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    expect(overflow).toBe(false);
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations).toEqual([]);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await expect(page.locator('#document-body')).toHaveAttribute('aria-busy', 'false');
    overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    expect(overflow).toBe(false);
  }
});
