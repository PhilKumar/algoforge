/**
 * 01-smoke.spec.ts
 * Smoke tests for AlgoForge:
 *   1. Login via PIN pad
 *   2. Health endpoint returns OK
 *   3. Auth status reflects authenticated session
 */

import { test, expect, Page } from '@playwright/test';

const PIN = process.env.E2E_PIN || '123456';

// ── Auth helper ─────────────────────────────────────────────
// Login page is a PIN-pad served at GET /. Each digit is a
// <button class="key" data-val="N">. After the 6th digit the
// page POSTs /api/auth/login and redirects to strategy.html.
async function login(page: Page) {
  await page.goto('/');
  for (const digit of PIN.split('')) {
    await page.click(`[data-val="${digit}"]`);
  }
  // Wait for the authenticated shell (nav bar rendered by strategy.html)
  await page.waitForSelector('.nav-tab', { timeout: 15_000 });
}

// ── Health check ─────────────────────────────────────────────
test('Health endpoint returns OK', async ({ request }) => {
  const resp = await request.get('/api/health');
  expect(resp.status()).toBe(200);
  const body = await resp.json();
  expect(body).toMatchObject({ status: 'ok' });
});

// ── Login ────────────────────────────────────────────────────
test('PIN-pad login succeeds and loads main app', async ({ page }) => {
  await login(page);
  // Nav tabs should be visible after successful authentication
  await expect(page.locator('.nav-tab').first()).toBeVisible();
});

// ── Auth status ──────────────────────────────────────────────
test('Auth status returns authenticated after login', async ({ page }) => {
  await login(page);
  const resp = await page.request.get('/api/auth/status');
  expect(resp.status()).toBe(200);
  const body = await resp.json();
  expect(body.authenticated).toBe(true);
});
