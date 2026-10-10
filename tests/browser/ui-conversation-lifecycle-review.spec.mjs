import { expect, test } from '@playwright/test';

const sessionId = 'conversation-source-review';
const runId = 'conversation-source-run';

const pause = () => {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
};

async function settleResponse(page, response) {
  await (await response).finished();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
}

async function conversation(page) {
  await page.route(`**/api/sessions/${sessionId}`, route => route.fulfill({ json: { ok: true, messages: [] } }));
  await page.route('**/api/analyses?*', route => route.fulfill({ json: {
    ok: true, items: [{ id: runId, session_id: sessionId, execution_status: 'partial', publication: { id: 'review-publication' } }],
  } }));
  await page.route(`**/api/analyses/${runId}/results`, route => route.fulfill({ json: {
    ok: true, status: 'published', manifest: { payload: { summary: '可核验的来源测试结论', tables: [], charts: [] } }, artifacts: [],
  } }));
  await page.goto(`/#/conversation?id=${sessionId}`);
  await expect(page.locator('.result')).toBeVisible();
}

for (const label of ['查看来源', '执行过程']) {
  test(`${label}的过期失败不在离开的页面弹出通知`, async ({ page }) => {
    await conversation(page);
    const response = pause();
    let requested = false;
    await page.route(`**/api/analyses/${runId}/execution`, async route => {
      requested = true;
      await response.promise;
      await route.fulfill({ status: 503, json: { ok: false, error: '已经离开的来源请求失败' } });
    });
    await page.getByRole('button', { name: label, exact: true }).click();
    await expect.poll(() => requested).toBe(true);
    await page.locator('.main-nav').getByRole('button', { name: '工作台', exact: true }).click();
    await expect(page.locator('.composer__input')).toBeVisible();
    const delivered = page.waitForResponse(response => new URL(response.url()).pathname === `/api/analyses/${runId}/execution`);
    response.release();
    await settleResponse(page, delivered);
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.toast')).toHaveCount(0);
  });
}

test('离开会话后来源的旧成功响应不继续读取核验或重新打开抽屉', async ({ page }) => {
  await conversation(page);
  const response = pause();
  let requested = false;
  let validations = 0;
  await page.route(`**/api/analyses/${runId}/execution`, async route => {
    requested = true;
    await response.promise;
    await route.fulfill({ json: { ok: true, item: { actions: [{ tool_id: 'query_data', arguments: { sql: 'SELECT 1' } }] } } });
  });
  await page.route(`**/api/analyses/${runId}/validations`, route => {
    validations += 1;
    return route.fulfill({ json: { ok: true, items: [] } });
  });
  await page.getByRole('button', { name: '查看来源', exact: true }).click();
  await expect.poll(() => requested).toBe(true);
  await page.locator('.main-nav').getByRole('button', { name: '工作台', exact: true }).click();
  await expect(page.locator('.composer__input')).toBeVisible();
  const delivered = page.waitForResponse(response => new URL(response.url()).pathname === `/api/analyses/${runId}/execution`);
  response.release();
  await settleResponse(page, delivered);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(validations).toBe(0);
});

test('当前来源读取失败仍给出反馈，重新读取与关闭可正常工作', async ({ page }) => {
  await conversation(page);
  let attempts = 0;
  await page.route(`**/api/analyses/${runId}/execution`, route => {
    attempts += 1;
    return attempts === 1
      ? route.fulfill({ status: 503, json: { ok: false, error: '当前来源暂时不可用' } })
      : route.fulfill({ json: { ok: true, item: { actions: [{ tool_id: 'query_data', arguments: { sql: 'SELECT 1' } }] } } });
  });
  await page.route(`**/api/analyses/${runId}/validations`, route => route.fulfill({ json: { ok: true, items: [] } }));
  const trigger = page.getByRole('button', { name: '查看来源', exact: true });
  await trigger.click();
  await expect(page.locator('.toast')).toContainText('当前来源暂时不可用');
  await trigger.click();
  const drawer = page.getByRole('dialog', { name: '查看来源', exact: true });
  await expect(drawer.locator('pre')).toHaveText('SELECT 1');
  await drawer.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(drawer).toHaveCount(0);
  await expect(trigger).toBeFocused();
});
