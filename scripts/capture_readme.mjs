/** Capture README assets from the real app and its synthetic demo dataset.
 * Run after npm run build: PYTHON=.venv/bin/python node scripts/capture_readme.mjs
 * No external model, private workspace, or mocked HTTP response is used.
 */
import { chromium, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = resolve(root, 'docs/assets/screenshots');
const storage = await mkdtemp(resolve(tmpdir(), 'data-agent-readme-'));
const portServer = createServer();
await new Promise(resolve => portServer.listen(0, '127.0.0.1', resolve));
const port = portServer.address().port;
await new Promise(resolve => portServer.close(resolve));
const baseURL = `http://127.0.0.1:${port}`;
const env = { ...process.env, MERIDIAN_ENV: 'test', MERIDIAN_STORAGE_DIR: storage,
  MERIDIAN_FRONTEND_DIR: resolve(root, 'frontend/dist'), NO_PROXY: '127.0.0.1,localhost',
  no_proxy: '127.0.0.1,localhost' };
for (const key of ['ALL_PROXY', 'HTTPS_PROXY', 'HTTP_PROXY', 'all_proxy', 'https_proxy', 'http_proxy',
  'MERIDIAN_TRUSTED_HOSTS', 'MERIDIAN_ALLOWED_ORIGINS', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL']) {
  delete env[key];
}
const code = `
import os
from pathlib import Path
from backend import create_app
storage = Path(os.environ['MERIDIAN_STORAGE_DIR'])
app = create_app({'TESTING': True, 'DATABASE_PATH': storage / 'meridian.sqlite3',
                  'STORAGE_DIR': storage, 'SECRET_KEY': 'readme-synthetic-demo-only'})
app.run(host='127.0.0.1', port=${port}, debug=False, use_reloader=False)
`;
const server = spawn(process.env.PYTHON || 'python3', ['-c', code], { cwd: root, env, stdio: ['ignore', 'ignore', 'pipe'] });
let serverError = '';
server.stderr.on('data', data => { serverError = (serverError + data).slice(-4000); });
server.on('error', error => { serverError += error.message; });
let browser;
try {
  await expect(async () => {
    const response = await fetch(`${baseURL}/api/health`);
    expect(response.ok).toBe(true);
  }).toPass({ timeout: 30_000 });
  browser = await chromium.launch();
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1, locale: 'zh-CN', colorScheme: 'light', reducedMotion: 'reduce' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => {
    if (response.url().startsWith(baseURL) && response.status() >= 400) {
      errors.push(`${response.status()} ${new URL(response.url()).pathname}`);
    }
  });
  const seeded = await context.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
  expect(seeded.ok()).toBe(true);
  await mkdir(output, { recursive: true });
  const shots = [];
  async function capture(name, route, description, selector) {
    await expect(page.locator(selector)).toBeVisible();
    await expect(page.locator('.skeleton')).toHaveCount(0);
    await page.evaluate(() => document.fonts.ready);
    await page.mouse.move(0, 0);
    await page.screenshot({ path: resolve(output, `${name}.png`), animations: 'disabled' });
    shots.push({ file: `${name}.png`, route, description, viewport: page.viewportSize() });
    console.log(`Captured ${name}.png`);
  }
  await page.goto('/#/workbench', { waitUntil: 'networkidle' });
  await capture('workbench', '/#/workbench', '工作台与真实演示数据推荐问题；未配置模型。', '.workbench__title');

  await page.setViewportSize({ width: 1440, height: 1800 });
  await page.goto('/#/metrics', { waitUntil: 'networkidle' });
  await page.locator('.metric-layout article.card').filter({ has: page.locator('b', { hasText: /^销售额$/ }) }).click();
  await page.locator('.checkbox').filter({ hasText: '区域' }).getByRole('checkbox').check();
  const trialResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/admin/metric-trial'
    && response.request().method() === 'POST');
  await page.getByRole('button', { name: '运行试算' }).click();
  expect((await trialResponse).ok()).toBe(true);
  await expect(page.locator('.chart-card canvas').first()).toBeVisible();
  await page.waitForFunction(() => {
    const canvas = document.querySelector('.chart-card__canvas');
    const chart = canvas && window.echarts?.getInstanceByDom(canvas);
    return chart && chart.getOption().series?.[0]?.data?.length > 0 && chart.getZr().animation.isFinished();
  });
  await page.locator('.page-head__title').scrollIntoViewIfNeeded();
  await expect(page.getByText('查看 SQL 与口径', { exact: true })).toBeInViewport({ ratio: 1 });
  await capture('metric-trial', '/#/metrics', '销售额按区域试算；图表来自本地合成数据上的真实指标查询。', '.chart-card');

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/#/admin/agents', { waitUntil: 'networkidle' });
  await page.locator('.file-tile').first().getByRole('button', { name: '编辑', exact: true }).click();
  await expect(page.locator('.overlay')).toHaveCSS('opacity', '1');
  await expect(page.locator('.modal')).toBeVisible();
  await page.locator('.builder__rail button').filter({ hasText: '技能' }).click();
  await expect(page.locator('.modal')).toContainText('数据分析');
  await capture('agents', '/#/admin/agents', '管理后台的智能体编辑器与发布入口。', '.builder');
  expect(errors).toEqual([]);
  await writeFile(resolve(output, 'capture.json'), JSON.stringify({
    dataset: 'POST /api/demo/seed; synthetic retail sales; rolling 24-month window',
    external_model: false, screenshots: shots,
  }, null, 2) + '\n');
  const diagram = await context.newPage();
  await diagram.setViewportSize({ width: 1500, height: 1170 });
  await diagram.goto(pathToFileURL(resolve(root, 'docs/assets/architecture.svg')).href);
  await diagram.evaluate(() => document.fonts.ready);
  await diagram.screenshot({ path: resolve(root, 'docs/assets/architecture.png') });
  console.log('Rendered architecture.png from architecture.svg');

  // Local Markdown preview for visual review; generated files stay out of Git.
  const marked = Function((await readFile(resolve(root, 'frontend/vendor/marked.min.js'), 'utf8')) + ';return marked;')();
  const reviewDir = resolve(root, '.tmp/readme-review');
  await mkdir(reviewDir, { recursive: true });
  const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <base href="${pathToFileURL(root).href}"><meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      *{box-sizing:border-box}body{margin:0;color:#1f2328;background:#fff;font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}
      main{max-width:980px;margin:auto;padding:32px}h1,h2{border-bottom:1px solid #d8dee4;padding-bottom:.3em}h2{margin-top:32px}
      img{max-width:100%;height:auto}table{display:block;max-width:100%;overflow:auto;border-collapse:collapse;margin:16px 0}
      th,td{border:1px solid #d0d7de;padding:8px 13px;min-width:110px}tr:nth-child(2n){background:#f6f8fa}
      pre{overflow:auto;background:#f6f8fa;padding:16px;border-radius:6px}code{font:13px/1.6 ui-monospace,monospace}
      p code,li code,td code{background:#eff1f3;padding:2px 4px;border-radius:4px}a{color:#0969da;text-decoration:none}
      details{margin:16px 0}summary{cursor:pointer}blockquote{border-left:4px solid #d0d7de;margin:16px 0;padding-left:16px;color:#656d76}
      @media(max-width:600px){main{padding:16px;font-size:14px}h1{font-size:28px}h2{font-size:22px}}
    </style><main>${marked.parse(await readFile(resolve(root, 'README.md'), 'utf8'))}</main></html>`;
  const previewFile = resolve(reviewDir, 'index.html');
  await writeFile(previewFile, html);
  const preview = await context.newPage();
  await preview.route(/^https?:\/\//, route => route.abort());
  for (const width of [1024, 390]) {
    await preview.setViewportSize({ width, height: 900 });
    await preview.goto(pathToFileURL(previewFile).href);
    await expect.poll(() => preview.locator('img').evaluateAll(images => images
      .filter(image => image.src.startsWith('file:')).every(image => image.complete && image.naturalWidth > 0))).toBe(true);
    expect(await preview.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await preview.screenshot({ path: resolve(reviewDir, `readme-${width}.png`), fullPage: true });
  }
  console.log('Reviewed Markdown layout and local assets at 1024px and 390px');
} catch (error) {
  console.error(serverError);
  throw error;
} finally {
  await browser?.close();
  const stopped = new Promise(resolve => server.once('exit', resolve));
  if (server.exitCode === null && server.pid) { server.kill('SIGTERM'); await stopped; }
  await rm(storage, { recursive: true, force: true });
}
