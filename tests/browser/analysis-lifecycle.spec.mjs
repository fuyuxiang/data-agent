import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
});

async function createRun(page, objective, sessionId) {
  const response = await page.request.post('/api/analyses', { data: {
    objective, source_ids: [], ...(sessionId ? { session_id: sessionId } : {}),
  } });
  expect(response.status()).toBe(201);
  return (await response.json()).item;
}

const turn = (page, run) => page.locator(`[data-run-id="${run.id}"]`);

test('分析删除有确认，恢复后原会话与问题完整显示', async ({ page }) => {
  const run = await createRun(page, '保留原问题的恢复分析');
  await page.goto(`/#/conversation?id=${run.session_id}`);
  await turn(page, run).getByRole('button', { name: '终止分析', exact: true }).click();
  await expect(turn(page, run)).toContainText('分析已取消');
  await turn(page, run).getByRole('button', { name: '删除分析', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除分析' });
  await expect(dialog).toContainText('资料库');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(turn(page, run)).toBeVisible();
  await turn(page, run).getByRole('button', { name: '删除分析', exact: true }).click();
  await dialog.getByRole('button', { name: '移入回收站', exact: true }).click();
  await expect(turn(page, run)).toHaveCount(0);
  await expect(page.locator('.conversation')).not.toContainText('保留原问题的恢复分析');
  await page.getByRole('button', { name: '恢复已删除分析' }).click();
  await expect(page).toHaveURL(/#\/trash/);
  const row = page.locator('article.card').filter({ hasText: '保留原问题的恢复分析' });
  await row.getByRole('button', { name: '恢复', exact: true }).click();
  await expect(row).toHaveCount(0);
  await page.goto(`/#/conversation?id=${run.session_id}`);
  await expect(turn(page, run)).toContainText('分析已取消');
  await expect(page.locator('.turn--user')).toContainText('保留原问题的恢复分析');
});

test('异步终止持续轮询直到确认停止', async ({ page }) => {
  const run = await createRun(page, '异步取消状态');
  let cancelling = false;
  let cancelPolls = 0;
  await page.route('**/api/analyses?*', route => {
    const status = cancelling ? (++cancelPolls >= 2 ? 'cancelled' : 'cancelling') : 'running';
    return route.fulfill({ json: { ok: true, items: [{ ...run, execution_status: status, stop_reason: '' }] } });
  });
  await page.route(`**/api/analyses/${run.id}/control`, route => {
    cancelling = true;
    return route.fulfill({ json: { ok: true, item: { ...run, execution_status: 'cancelling', stop_reason: 'cancel_confirmation_pending' } } });
  });
  await page.goto(`/#/conversation?id=${run.session_id}`);
  await turn(page, run).getByRole('button', { name: '终止分析', exact: true }).click();
  await expect(turn(page, run)).toContainText('正在终止分析');
  await expect(turn(page, run).getByRole('button', { name: '删除分析', exact: true })).toBeVisible({ timeout: 10_000 });
  expect(cancelPolls).toBeGreaterThanOrEqual(2);
});

test('终止旧分析不会停止同会话其他任务的状态更新', async ({ page }) => {
  const old = await createRun(page, '旧分析等待口径');
  const current = await createRun(page, '仍需轮询的新分析', old.session_id);
  let cancelled = false;
  let afterCancelPolls = 0;
  await page.route('**/api/analyses?*', route => {
    const done = cancelled && ++afterCancelPolls >= 2;
    return route.fulfill({ json: { ok: true, items: [
      { ...current, execution_status: done ? 'cancelled' : 'running', stop_reason: '' },
      { ...old, execution_status: cancelled ? 'cancelled' : 'waiting_input' },
    ] } });
  });
  await page.route(`**/api/analyses/${old.id}/control`, route => {
    cancelled = true;
    return route.fulfill({ json: { ok: true, item: { ...old, execution_status: 'cancelled' } } });
  });
  await page.goto(`/#/conversation?id=${old.session_id}`);
  await turn(page, old).getByRole('button', { name: '终止分析', exact: true }).click();
  await expect(turn(page, current)).toContainText('分析已取消', { timeout: 10_000 });
  expect(afterCancelPolls).toBeGreaterThanOrEqual(2);
});

test('外部终止失败明确提示并可重试', async ({ page }) => {
  const run = await createRun(page, '取消失败后重试');
  const pending = { ...run, execution_status: 'cancelling', stop_reason: 'cancel_retry_pending',
    cancel_errors: [{ job_id: 'remote', message: '外部引擎暂时不可连接' }] };
  let retried = false;
  await page.route('**/api/analyses?*', route => route.fulfill({ json: {
    ok: true, items: [retried ? { ...run, execution_status: 'cancelled' } : pending],
  } }));
  await page.route(`**/api/analyses/${run.id}/control`, route => {
    retried = true;
    return route.fulfill({ json: { ok: true, item: { ...run, execution_status: 'cancelled' } } });
  });
  await page.goto(`/#/conversation?id=${run.session_id}`);
  await expect(turn(page, run)).toContainText('外部引擎暂时不可连接');
  await turn(page, run).getByRole('button', { name: '重试终止', exact: true }).click();
  await expect(turn(page, run)).toContainText('分析已取消');
});

test('恢复数据源不会把用户选中的会话切回服务端默认会话', async ({ page }) => {
  const chosen = await createRun(page, '恢复后继续当前会话');
  await createRun(page, '服务端较新的默认会话');
  const uploaded = await page.request.post('/api/sources/upload', { multipart: {
    file: { name: 'restore-session-scope.csv', mimeType: 'text/csv', buffer: Buffer.from('name,value\nA,1\n') },
  } });
  expect(uploaded.status()).toBe(201);
  const source = (await uploaded.json()).items[0];
  const dependent = await page.request.post('/api/analyses', { data: {
    objective: '依赖数据源恢复的历史分析', source_ids: [source.id],
  } });
  expect(dependent.status()).toBe(201);
  const dependentRun = (await dependent.json()).item;
  expect((await page.request.post(`/api/analyses/${dependentRun.id}/control`, { data: { action: 'cancel' } })).status()).toBe(200);
  expect((await page.request.delete(`/api/analyses/${dependentRun.id}`)).status()).toBe(200);
  expect((await page.request.delete(`/api/sources/${source.id}`)).status()).toBe(200);
  await page.goto('/#/workbench');
  await page.locator('.sidebar').getByRole('button', { name: '恢复后继续当前会话', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(chosen.session_id));
  await page.getByRole('button', { name: '恢复已删除分析' }).click();
  await page.getByRole('combobox', { name: '回收站内容类型' }).selectOption('');
  const history = page.locator('article.card').filter({ hasText: '依赖数据源恢复的历史分析' });
  await expect(history).toContainText('请先恢复分析使用的数据源');
  await expect(history.getByRole('button', { name: '恢复', exact: true })).toHaveCount(0);
  const row = page.locator('article.card').filter({ hasText: source.name });
  await row.getByRole('button', { name: '恢复', exact: true }).click();
  await expect(page.getByRole('button', { name: '刷新', exact: true })).toBeEnabled();
  await expect(history.getByRole('button', { name: '恢复', exact: true })).toBeEnabled();
  await page.locator('.sidebar .main-nav').getByRole('button', { name: '工作台', exact: true }).click();
  await page.locator('.composer__input').fill('恢复后继续追问');
  const sent = page.waitForResponse(response => response.url().includes('/api/analyses')
    && response.request().method() === 'POST');
  await page.locator('.composer__send').click();
  const response = await sent;
  expect(response.status()).toBe(201);
  expect((await response.json()).item.session_id).toBe(chosen.session_id);
});
