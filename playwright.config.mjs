import { defineConfig } from '@playwright/test';

// The browser fixture is intentionally loopback-only.  Inherited corporate
// proxies must not make Playwright mistake a proxy response for this server.
for (const name of ['ALL_PROXY', 'HTTPS_PROXY', 'HTTP_PROXY', 'all_proxy', 'https_proxy', 'http_proxy']) {
  delete process.env[name];
}
process.env.NO_PROXY = '127.0.0.1,localhost';
process.env.no_proxy = process.env.NO_PROXY;

export default defineConfig({
  testDir: './tests/browser',
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:5013',
    browserName: 'chromium',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'python3 -m scripts.run_browser_fixture',
    url: 'http://127.0.0.1:5013/api/ready',
    timeout: 30_000,
    reuseExistingServer: false,
  },
});
