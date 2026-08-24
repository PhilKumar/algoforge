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

test('CryptoForge skins keep PhilForge surfaces readable and logos native', async ({ page }) => {
  await login(page);
  const darkAccents: Record<string, string> = {
    gold: '#f59e0b', arctic: '#60a5fa', magenta: '#e879f9',
    citrus: '#a3e635', graphite: '#cbd5e1', bronze: '#d6a06a',
  };
  const lightAccents: Record<string, string> = {
    gold: '#b45309', arctic: '#1d4ed8', magenta: '#a21caf',
    citrus: '#4d7c0f', graphite: '#334155', bronze: '#92400e',
  };
  const originalLogo = await page.locator('.header-brand-logo').evaluate((el) => getComputedStyle(el).backgroundImage);

  for (const [tint, expectedAccent] of Object.entries(darkAccents)) {
    const state = await page.evaluate((nextTint) => {
      (window as any).pfApplyTheme('dark', { persist: false });
      (window as any).pfApplyAppearance({ tint: nextTint }, { persist: false });
      const root = getComputedStyle(document.documentElement);
      const logo = document.querySelector('.header-brand-logo')!;
      return {
        accent: root.getPropertyValue('--accent').trim(),
        text: root.getPropertyValue('--text').trim(),
        card: root.getPropertyValue('--card').trim(),
        green: root.getPropertyValue('--green').trim(),
        red: root.getPropertyValue('--red').trim(),
        logo: getComputedStyle(logo).backgroundImage,
        inlineLogo: (logo as HTMLElement).style.backgroundImage,
      };
    }, tint);
    expect(state.accent).toBe(expectedAccent);
    expect(state.text).toBe('#dde3ee');
    expect(state.card).toBe('rgba(18, 26, 42, 0.85)');
    expect(state.green).not.toBe(state.red);
    expect(state.logo).toBe(originalLogo);
    expect(state.inlineLogo).toBe('');
  }

  for (const [tint, expectedAccent] of Object.entries(lightAccents)) {
    const state = await page.evaluate((nextTint) => {
      (window as any).pfApplyTheme('light', { persist: false });
      (window as any).pfApplyAppearance({ tint: nextTint }, { persist: false });
      const root = getComputedStyle(document.documentElement);
      return {
        accent: root.getPropertyValue('--accent').trim(),
        text: root.getPropertyValue('--text').trim(),
        card: root.getPropertyValue('--card').trim(),
        green: root.getPropertyValue('--green').trim(),
        red: root.getPropertyValue('--red').trim(),
      };
    }, tint);
    expect(state.accent).toBe(expectedAccent);
    expect(state.text).toBe('#0f172a');
    expect(state.card).toBe('rgba(255,255,255,0.96)');
    expect(state.green).not.toBe(state.red);
  }
});
