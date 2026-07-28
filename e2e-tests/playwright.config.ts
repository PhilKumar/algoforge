import { defineConfig, devices } from '@playwright/test';
import os from 'os';
import path from 'path';

const artifactRoot = process.env.PLAYWRIGHT_ARTIFACT_ROOT || path.join(os.tmpdir(), 'philforge-playwright');
const outputDir = process.env.PLAYWRIGHT_OUTPUT_DIR || path.join(artifactRoot, 'test-results');
const htmlReportDir = process.env.PLAYWRIGHT_HTML_REPORT || path.join(artifactRoot, 'playwright-report');

export default defineConfig({
  testDir: './tests',
  outputDir,
  snapshotPathTemplate: '{snapshotDir}{/testFileDir}/{testFileName}-snapshots/{arg}{ext}',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['html', { open: 'never', outputFolder: htmlReportDir }], ['list']],

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
