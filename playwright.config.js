import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/browser',
  testMatch: '**/*.browser.js',
  workers: 1,
  use: { browserName: 'chromium', headless: true },
});
