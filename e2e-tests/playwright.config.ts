import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['html', { open: 'never' }], ['list']],

  projects: [
    {
      name: 'philforge',
      use: {
        ...devices['Desktop Chrome'],
        baseURL: process.env.E2E_BASE_URL || process.env.BASE_URL || 'http://localhost:8000',
      },
    },
  ],
});
