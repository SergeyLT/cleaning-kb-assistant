import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

process.env.PLAYWRIGHT_BROWSERS_PATH ??= path.resolve('.tmp/ms-playwright');
const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
const port = process.env.UI_TEST_PORT || '8877';

export default defineConfig({
  testDir: './tests/ui',
  outputDir: '.tmp/playwright/results',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  reporter: [['line'], ['json', { outputFile: '.tmp/playwright/report.json' }]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    locale: 'ru-RU',
    timezoneId: 'Europe/Moscow',
  },
  projects: [
    { name: 'desktop', testIgnore: /mobile\.spec\.ts/, use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } } },
    { name: 'mobile', testMatch: /mobile\.spec\.ts/, use: { ...devices['iPhone 13'], viewport: { width: 375, height: 812 } } },
  ],
  webServer: {
    command: `"${python}" tests/ui/server.py`,
    url: `http://127.0.0.1:${port}/live`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
});
