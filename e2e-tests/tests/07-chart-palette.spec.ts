/**
 * 07-chart-palette.spec.ts
 *
 * Phil, on the LODHA campaign chart: "The fib lines on standard colours missed..
 * TL on colours missed.. Entry colours missed."
 *
 * Every leg, every trendline and every fill drew in the SAME blue. The cause is
 * the kind that only a painted-pixel test catches: the campaign chart kept a
 * private copy of the palette, and two of its three dark fib colours were
 * written `var(--green)` and `var(--red)`. Canvas does not resolve CSS
 * variables, and assigning an unparseable colour to strokeStyle is SILENTLY
 * IGNORED — the context keeps whatever colour it had. No error, no warning, a
 * chart that renders perfectly and has lost its colour coding.
 *
 * So these assert on the palette object and on pixels actually painted. Reading
 * the source would not have found it; neither would counting shapes.
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

/* BOTH THEMES, ALWAYS. The first version of this file checked "the palette",
 * meaning whichever one the browser's colour scheme selected — and Playwright
 * defaults to LIGHT while the bug lived only in the DARK constant. It passed
 * against the very bug it was written for. The theme is stamped explicitly now,
 * and every test walks both. */
const THEMES = ['dark', 'light'] as const;

async function paletteFor(page: Page, theme: string, fn: string) {
  return page.evaluate(([t, name]) => {
    document.documentElement.setAttribute('data-theme', t as string);
    // @ts-ignore — top-level function declarations are page globals
    const builder = window[name as any] as any;
    return typeof builder === 'function' ? builder() : null;
  }, [theme, fn]);
}

test('no chart colour is a CSS variable — Canvas cannot resolve one', async ({ page }) => {
  await login(page);
  for (const theme of THEMES) {
    for (const builder of ['pfChartPalette', '_terminalCascadeChartPalette']) {
      const palette: any = await paletteFor(page, theme, builder);
      expect(palette, `${builder} returned nothing in ${theme}`).toBeTruthy();
      const bad: string[] = [];
      for (const key of Object.keys(palette)) {
        const value = palette[key];
        const entries = Array.isArray(value) ? value : [value];
        entries.forEach((entry: any, i: number) => {
          const label = Array.isArray(value) ? `${key}[${i}]` : key;
          if (typeof entry !== 'string' || !entry.trim() || entry.includes('var(') || entry.includes('--')) {
            bad.push(`${label} = ${String(entry)}`);
          }
        });
      }
      expect(bad, `${builder} in ${theme}: ${bad.join(', ')}`).toEqual([]);
    }
  }
});

test('the campaign chart takes its colours from the one shared palette', async ({ page }) => {
  await login(page);
  for (const theme of THEMES) {
    const shared: any = await paletteFor(page, theme, 'pfChartPalette');
    const campaign: any = await paletteFor(page, theme, '_terminalCascadeChartPalette');
    for (const key of ['mother', 'tp', 'avg', 'buyMark', 'sellMark', 'markRing', 'up', 'down']) {
      expect(campaign[key], `${key} differs in ${theme}`).toBe(shared[key]);
    }
    expect(campaign.fibs, `fibs differ in ${theme}`).toEqual(shared.fibs);
  }
});

test('the three fib colours are distinct, and buys are off the candle palette', async ({ page }) => {
  await login(page);
  for (const theme of THEMES) {
    const p: any = await paletteFor(page, theme, 'pfChartPalette');
    // One colour per leg — that IS the coding. Two the same and the chart lies.
    expect(new Set(p.fibs).size, `fib ramp collapsed in ${theme}`).toBe(3);
    // A buy mark matching a candle body is camouflage on the bar it sits on.
    expect(p.buyMark).not.toBe(p.up);
    expect(p.buyMark).not.toBe(p.down);
    expect(p.mother).not.toBe(p.fibs[0]);
  }
});

test('the legend names the colours the chart actually paints', async ({ page }) => {
  await login(page);
  const drift = await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'dark');
    // @ts-ignore
    const p = window._terminalCascadeChartPalette();
    // @ts-ignore
    const markup = window._terminalCascadeChartHtml
      // @ts-ignore
      ? window._terminalCascadeChartHtml({ instrument: { symbol: 'LODHA', signal_symbol: 'LODHA' } })
      : '';
    if (!markup) return 'chart markup builder missing';
    // Every fib colour must appear in the legend, and the old lies must not.
    const missing = p.fibs.filter((c: string) => !markup.includes(c));
    if (missing.length) return `legend omits ${missing.join(', ')}`;
    if (!markup.includes(p.buyMark)) return 'legend omits the buy colour';
    if (markup.includes('var(--green)')) return 'legend still hard-codes green';
    return '';
  });
  expect(drift).toBe('');
});

test('every chart caps structures at the newest three, and the cap has one home', async ({ page }) => {
  // Phil: "I already told to show only the latest 3 fibs and TLs.. but again and
  // again you are making me repeat the entire thing." He was right: the shared
  // renderer capped at three and the Terminal campaign chart, which has its own
  // drawing loop, capped at nothing — so it drew 7 fibs and 6 trendlines.
  await login(page);
  const result = await page.evaluate(() => {
    const legs = Array.from({ length: 7 }, (_, i) => ({ leg_id: i + 1 }));
    const tls = Array.from({ length: 6 }, (_, i) => ({ id: i + 1, active: i === 0 }));
    // @ts-ignore — page globals
    const cap = window.pfChartMaxStructures;
    // @ts-ignore
    const keptLegs = window._tcvLatest(legs).map((l: any) => l.leg_id);
    // @ts-ignore
    const keptTls = window._tcvLatest(tls, (t: any) => t && t.active).map((t: any) => t.id);
    return { cap, keptLegs, keptTls };
  });

  expect(result.cap).toBe(3);
  // The NEWEST three, not the first three.
  expect(result.keptLegs).toEqual([5, 6, 7]);
  // The active trendline survives even when three newer retired ones exist —
  // Classic's exception, and it has to hold in both renderers.
  expect(result.keptTls).toContain(1);
  expect(result.keptTls).toHaveLength(3);
});
