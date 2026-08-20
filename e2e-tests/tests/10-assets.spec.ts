import { test, expect, Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const USERNAME = process.env.E2E_USERNAME || 'admin';
const PIN = process.env.E2E_PIN || '123456';

async function login(page: Page) {
  const response = await page.request.post('/api/auth/login', {
    data: { username: USERNAME, password: PIN },
  });
  expect(response.status()).toBe(200);
}

test('Assets stays private and opens inside the normal application shell', async ({ page }) => {
  const unauthenticated = await page.request.get('/architecture', { maxRedirects: 0 });
  expect(unauthenticated.status()).toBe(401);

  await login(page);
  const legacyAtlas = await page.request.get('/architecture', { maxRedirects: 0 });
  expect(legacyAtlas.status()).toBe(307);
  expect(legacyAtlas.headers().location).toBe('/app#assets/overview');

  await page.goto('/app#assets/overview');
  await expect(page.locator('.header-shell')).toBeVisible();
  await expect(page.locator('#nav-assets')).toHaveClass(/active/);
  await expect(page.locator('#assets-page')).toHaveClass(/active-page/);
  await expect(page.locator('#assets-page .pf-workspace-hero h1')).toHaveText('Assets');
  const atlas = page.locator('philforge-architecture-atlas');
  await expect(atlas.locator('#flow-nodes .flow-node')).toHaveCount(6);

  const chapterLabels = {
    cryptoforge: ['Chapter 1 ·', 'Chapter 2 ·', 'Chapter 3 ·', 'Chapter 5 ·', 'Chapter 6 ·', 'Chapter 7 ·'],
    philforge: ['Chapter 1 ·', 'Chapter 2 ·', 'Chapter 4 ·', 'Chapter 5 ·', 'Chapter 6 ·', 'Chapter 7 ·'],
  };
  for (const platform of ['cryptoforge', 'philforge'] as const) {
    const legacyResponse = await page.request.get(`/architecture/docs/${platform}`, { maxRedirects: 0 });
    expect(legacyResponse.status()).toBe(307);
    expect(legacyResponse.headers().location).toBe(`/app#assets/${platform}`);
    const oldReader = await page.request.get(`/architecture/${platform}`, { maxRedirects: 0 });
    expect(oldReader.status()).toBe(307);
    expect(oldReader.headers().location).toBe(`/app#assets/${platform}`);

    await page.goto(`/app#assets/${platform}`);
    await expect(page.locator('.header-shell')).toBeVisible();
    await expect(page.locator('#architecture-reader-panel')).toBeVisible();
    // The blueprint reader must stay frame-free — that is the fix this guards.
    // The tearsheet panel is a separate, deliberate frame.
    await expect(page.locator('#architecture-reader-panel iframe')).toHaveCount(0);
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
    await page.goto(`/app#assets/${platform}`);
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
    await expect.poll(() => page.evaluate(() => window.location.hash)).toBe(`#assets/${platform}`);
  }
});

test('platform tabs redraw the system map and support keyboard navigation', async ({ page }) => {
  await login(page);
  await page.goto('/app#assets/overview');

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

test('assets page is accessible and does not overflow desktop or mobile', async ({ page }) => {
  await login(page);
  await page.goto('/app#assets/overview');
  const atlas = page.locator('philforge-architecture-atlas');
  await expect(atlas.locator('#flow-nodes .flow-node')).toHaveCount(6);

  const desktopOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  expect(desktopOverflow).toBe(false);

  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.locator('#assets-page .pf-workspace-hero h1')).toBeVisible();
  const mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  expect(mobileOverflow).toBe(false);
});

test('visual blueprint search filters sections without exposing Markdown downloads', async ({ page }) => {
  await login(page);
  await page.goto('/app#assets/cryptoforge');
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
    await page.goto(`/app#assets/${platform}`);
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
    await page.goto(`/app#assets/${platform.id}`);
    const reader = page.locator('#architecture-reader-view');
    await expect(reader.locator('#document-body')).toHaveAttribute('aria-busy', 'false', { timeout: 30_000 });
    await expect(page.locator('.header-shell')).toBeVisible();
    await expect(reader.locator('.reader-header')).toHaveCount(0);
    const accent = await reader.evaluate((node) => getComputedStyle(node).getPropertyValue('--accent').trim());
    expect(accent).toBe(platform.accent);
    const readingSizes = await reader.locator('.doc-section').first().evaluate((section) => {
      const root = section.getRootNode() as ShadowRoot;
      return {
        body: Number.parseFloat(getComputedStyle(section.querySelector('p')!).fontSize),
        topic: Number.parseFloat(getComputedStyle(root.querySelector('#document-toc a')!).fontSize),
        table: Number.parseFloat(getComputedStyle(root.querySelector('.doc-table td')!).fontSize),
      };
    });
    expect(readingSizes.body).toBeGreaterThanOrEqual(16);
    expect(readingSizes.topic).toBeGreaterThanOrEqual(12);
    expect(readingSizes.table).toBeGreaterThanOrEqual(13);
    let overflow = await reader.evaluate((node) => node.scrollWidth > node.clientWidth + 1);
    expect(overflow).toBe(false);
    // No frame exclusion: axe walks the same-origin tearsheet, and the
    // document is expected to pass on its own terms too.
    const results = await new AxeBuilder({ page }).include('#assets-page').analyze();
    expect(results.violations).toEqual([]);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    const mobileReader = page.locator('#architecture-reader-view');
    await expect(mobileReader.locator('#document-body')).toHaveAttribute('aria-busy', 'false', { timeout: 30_000 });
    overflow = await mobileReader.evaluate((node) => node.scrollWidth > node.clientWidth + 1);
    expect(overflow).toBe(false);
  }
});

/** The combined net figure of the published tearsheet, formatted the way the
 *  page prints it (Indian grouping, no rupee sign), read from the report data
 *  that build_report.py renders from. */
function tearsheetHeadline(): string {
  const dataPath = resolve(process.cwd(), '..', 'tools', 'tearsheet', 'report_data.json');
  const data = JSON.parse(readFileSync(dataPath, 'utf8'));
  const net = Math.round(Number(data.headline.combined.net));
  const digits = String(Math.abs(net));
  if (digits.length <= 3) return digits;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ',');
  return `${rest},${last3}`;
}

test('the tearsheet is served whole and embedded in the workspace it belongs to', async ({ page }) => {
  const unauthenticated = await page.request.get('/assets/tearsheet', { maxRedirects: 0 });
  expect(unauthenticated.status()).toBe(401);

  await login(page);
  const assetsPath = await page.request.get('/assets', { maxRedirects: 0 });
  expect(assetsPath.status()).toBe(307);
  expect(assetsPath.headers().location).toBe('/app#assets/overview');

  const document_ = await page.request.get('/assets/tearsheet');
  expect(document_.status()).toBe(200);
  const body = await document_.text();
  // Served with a real document shell, not as the bare artifact fragment.
  expect(body.startsWith('<!DOCTYPE html>')).toBe(true);
  expect(body).toContain('PhilForge Options Tearsheet');
  // The figure the document exists to publish must survive the round trip.
  // It is read from the data the page is built from, not pinned by hand:
  // the tearsheet is regenerated whenever the book changes.
  expect(body).toContain(tearsheetHeadline());

  await page.goto('/app#assets/tearsheet');
  await expect(page.locator('#assets-tearsheet-panel')).toBeVisible();
  await expect(page.locator('#architecture-view-title')).toHaveText('Five-Year Tearsheet');

  const framed = page.frameLocator('#assets-tearsheet-frame');
  await expect(framed.locator('h1')).toBeVisible();
  await expect(framed.locator('body')).toContainText(tearsheetHeadline());

  // The document wears the blueprint reader's chrome rather than its own, so a
  // reader moving between the Tearsheet and CryptoForge tabs sees one design:
  // meta chips, a search box and a contents rail built from the headings.
  await expect(framed.locator('.document-hero .meta-chip').first()).toBeVisible();
  await expect(framed.locator('#tearsheet-search')).toBeVisible();
  const contents = framed.locator('#document-toc a');
  expect(await contents.count()).toBe(await framed.locator('#document-body > section').count());
  await expect(contents.first()).toBeVisible();

  // The search filters sections; it is the one piece of reader behaviour this
  // document had to reimplement, so it is worth proving rather than assuming.
  await framed.locator('#tearsheet-search').fill('drawdown');
  await expect.poll(async () => framed.locator('#document-body > section:visible').count())
    .toBeLessThan(await contents.count());
  await framed.locator('#tearsheet-search').fill('');
  await expect.poll(async () => framed.locator('#document-body > section:visible').count())
    .toBe(await contents.count());

  // The terminal stamps data-theme only when it is light, so an unstamped
  // workspace is dark. The document must not fall back to its own default.
  const framedTheme = () => page.locator('#assets-tearsheet-frame')
    .evaluate((node: HTMLIFrameElement) => node.contentDocument?.documentElement.dataset.theme || '');
  await expect.poll(framedTheme).toBe('dark');
  await expect(page.locator('#assets-tearsheet-open')).toHaveAttribute('href', '/assets/tearsheet?doc=options&theme=dark');

  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  await expect.poll(framedTheme).toBe('light');
  await expect(page.locator('#assets-tearsheet-open')).toHaveAttribute('href', '/assets/tearsheet?doc=options&theme=light');
  await page.evaluate(() => document.documentElement.removeAttribute('data-theme'));
  await expect.poll(framedTheme).toBe('dark');

  // A framed document is the one place a horizontal scrollbar hides.
  const framedOverflow = await page.locator('#assets-tearsheet-frame').evaluate((node: HTMLIFrameElement) => {
    const win = node.contentWindow!;
    win.scrollTo(9999, 0);
    const x = win.scrollX;
    win.scrollTo(0, 0);
    return x;
  });
  expect(framedOverflow).toBe(0);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('#assets-tearsheet-open')).toBeVisible();
  const mobileOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  );
  expect(mobileOverflow).toBe(false);
});

test('production CSP permits the same-origin tearsheet frame and nothing else', async ({ page }) => {
  // The embed is invisible in production if this policy forbids frames — which
  // it did, as frame-src 'none'. Locally there is no nginx in front of the app,
  // so the deployed policy can only be checked by reading it.
  const conf = readFileSync(resolve(process.cwd(), '..', 'deploy', 'nginx.conf'), 'utf8');
  expect(conf).toContain("frame-src 'self'");
  expect(conf).not.toContain("frame-src 'none'");
  expect(conf).toContain("frame-ancestors 'self'");
  expect(page).toBeTruthy();
});

test('the tearsheet document passes accessibility on its own, in both themes', async ({ page }) => {
  // This test does a login, then TWO full navigations and TWO whole-document
  // axe scans of a long tearsheet — inside the 30s default meant for one.
  // On a loaded CI runner it lands at 30.6s and 31.5s, i.e. it fails while
  // still making progress, and it blocked two deploys on 2026-08-15 with
  // nothing wrong in the diff. The assertions are unchanged; only the budget
  // is honest about the work.
  test.setTimeout(90_000);
  await login(page);
  for (const theme of ['dark', 'light'] as const) {
    await page.goto(`/assets/tearsheet?theme=${theme}`);
    await expect(page.locator('h1')).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations).toEqual([]);
  }
});
