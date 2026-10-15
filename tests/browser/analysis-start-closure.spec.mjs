import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
  await page.goto('/#/workbench');
  await expect(page.locator('.workbench__title')).toBeVisible();
  await page.locator('.sidebar__new').click();
});

test('明确问数提交后自动确认并持久保存，复杂问题保留业务确认', async ({ page }) => {
  // Real application and job submission, without a configured paid model.
  // This verifies starting/recovery; it does not assert an AI answer exists.
  await page.locator('.composer__input').fill('本月销售额是多少？');
  const started = page.waitForResponse(response => response.url().endsWith('/start') && response.request().method() === 'POST');
  await page.locator('.composer__send').click();
  const response = await (await started).json();
  expect(response.auto_confirmed).toBe(true);
  expect(response.item.contract.confirmed_at).toBeTruthy();
  await expect(page).toHaveURL(/#\/conversation/);
  await page.reload();
  await expect(page.locator('.turn__bubble').last()).toContainText('本月销售额');
  await expect(page.locator('.clarify')).toHaveCount(0);

  await page.locator('.sidebar__new').click();
  await expect(page.locator('.workbench__title')).toBeVisible();
  await page.locator('.composer__input').fill('为什么华东销售下降？');
  await page.locator('.composer__send').click();
  await expect(page.locator('.clarify')).toBeVisible();
  await expect(page.locator('.execution__head')).toContainText('等待补充或确认信息');
});

test('两个附件全部读取后才启动，提交期间重复操作只创建一个分析', async ({ page }) => {
  let releaseUpload;
  const ready = new Promise(resolve => { releaseUpload = resolve; });
  let uploadRunId;
  await page.route('**/api/analyses/*/attachments', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    uploadRunId = route.request().url().split('/').at(-2);
    await ready;
    return route.continue();
  });
  await page.locator('.composer input[type="file"]').setInputFiles([
    { name: 'east.txt', mimeType: 'text/plain', buffer: Buffer.from('华东销售额 120 元') },
    { name: 'west.txt', mimeType: 'text/plain', buffer: Buffer.from('华西销售额 80 元') },
  ]);
  await page.locator('.composer__input').fill('汇总附件中的销售额');
  const started = page.waitForResponse(response => response.url().endsWith('/start') && response.request().method() === 'POST');
  await page.locator('.composer__send').click();
  await expect(page.getByRole('status')).toContainText('正在上传并读取附件');
  await expect(page.locator('.composer__send')).toBeDisabled();
  await expect.poll(() => uploadRunId).toBeTruthy();
  const before = await (await page.request.get(`/api/analyses/${uploadRunId}`)).json();
  expect(before.item.contract.confirmed_at).toBeNull();
  releaseUpload();
  const response = await (await started).json();
  expect(response.auto_confirmed).toBe(true);
  await expect(page).toHaveURL(/#\/conversation/);
  const attachments = await (await page.request.get(`/api/analyses/${uploadRunId}/attachments`)).json();
  expect(attachments.items.map(item => item.filename).sort()).toEqual(['east.txt', 'west.txt']);
  const runs = await (await page.request.get(`/api/analyses?session_id=${response.item.session_id}`)).json();
  expect(runs.items).toHaveLength(1);
});

test('附件读取失败保留问题和附件，可修正后重试', async ({ page }) => {
  await page.route('**/api/analyses/*/attachments', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    return route.fulfill({ status: 400, json: { ok: false, error: '文件内容无法读取，请换一份文件' } });
  });
  await page.locator('.composer input[type="file"]').setInputFiles({
    name: 'orders.txt', mimeType: 'text/plain', buffer: Buffer.from('订单销售额 120 元'),
  });
  await page.locator('.composer__input').fill('汇总附件中的销售额');
  await page.locator('.composer__send').click();
  await expect(page.locator('.toast')).toContainText('文件内容无法读取');
  await expect(page.locator('.composer__input')).toHaveValue('汇总附件中的销售额');
  await expect(page.locator('.attachment-chip')).toContainText('orders.txt');
  await expect(page.locator('.composer__send')).toBeEnabled();
  await page.unroute('**/api/analyses/*/attachments');
  await page.locator('.composer__send').click();
  await expect(page).toHaveURL(/#\/conversation/);
});

test('自动技能预判只作提示，不会把第一个结果强制指定成唯一能力', async ({ page }) => {
  await page.route('**/api/skills/resolve', route => route.fulfill({ json: {
    ok: true, selected: [{ id: 'data-analysis', name: '数据分析' }, { id: 'visualization', name: '可视化' }],
  } }));
  await page.locator('.composer__input').fill('查看月度销售趋势图');
  await expect(page.locator('.composer__bar')).toContainText('数据分析、可视化');
  const requestPromise = page.waitForRequest(request => new URL(request.url()).pathname === '/api/analyses' && request.method() === 'POST');
  await page.locator('.composer__send').click();
  const payload = (await requestPromise).postDataJSON();
  expect(payload.skill_id).toBeUndefined();
  expect(payload.skill_ids).toBeUndefined();
});

for (const role of ['analyst', 'viewer']) {
  test(`${role} 无可用数据时只显示可执行的引导`, async ({ page }) => {
    await page.route('**/api/bootstrap*', async route => {
      const response = await route.fetch();
      const json = await response.json();
      json.active_membership = { ...(json.active_membership || {}), role };
      json.sources = [];
      await route.fulfill({ response, json });
    });
    await page.reload();
    await expect(page.getByText('还没有可分析的数据', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '接入数据', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '使用演示数据', exact: true })).toHaveCount(0);
    if (role === 'viewer') await expect(page.locator('.composer__input')).toBeDisabled();
    else await expect(page.locator('.composer__input')).toBeEnabled();
  });
}
