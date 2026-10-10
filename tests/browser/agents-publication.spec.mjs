import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
});

async function publishedAgent(page, name) {
  const sources = (await (await page.request.get('/api/sources')).json()).items;
  const created = await page.request.post('/api/agents', { data: {
    name, source_ids: [sources[0].id], skill_ids: [],
  } });
  expect(created.ok()).toBeTruthy();
  const item = (await created.json()).item;
  expect((await page.request.post(`/api/agents/${item.id}/publish`)).ok()).toBeTruthy();
  return { ...item, source: sources[0] };
}

async function chooseDrawer(page) {
  await page.locator('.composer__scope[title="选择本次使用的智能体"]').click();
  return page.getByRole('dialog', { name: '选择智能体' });
}

test('已发布智能体首载与刷新后均可选择', async ({ page }) => {
  await publishedAgent(page, '刷新可选助手');
  await page.goto('/#/workbench');
  await expect(await chooseDrawer(page)).toContainText('刷新可选助手');
  await page.reload();
  await expect(await chooseDrawer(page)).toContainText('刷新可选助手');
});

test('关闭未保存的编辑不会改变线上数据范围', async ({ page }) => {
  const agent = await publishedAgent(page, '独立草稿助手');
  const added = await page.request.post('/api/sources/upload', { multipart: {
    file: { name: 'unsaved-publication.csv', mimeType: 'text/csv', buffer: Buffer.from('name,value\nB,2\n') },
  } });
  expect(added.ok()).toBeTruthy();
  const extra = (await added.json()).items[0];
  await page.goto('/#/admin/agents');
  const tile = page.locator('.file-tile').filter({ hasText: '独立草稿助手' });
  await tile.getByRole('button', { name: '编辑', exact: true }).click();
  const modal = page.locator('.modal');
  await modal.locator('.builder__rail button').filter({ hasText: '数据' }).click();
  await modal.locator('label.card').filter({ hasText: extra.name }).getByRole('checkbox').check();
  await modal.locator('.modal__head button[aria-label="关闭"]').click();
  await expect(tile).toContainText('1 个数据源');
  await page.locator('.sidebar .main-nav .nav-item').filter({ hasText: '工作台' }).click();
  const drawer = await chooseDrawer(page);
  await drawer.getByRole('button').filter({ hasText: '独立草稿助手' }).click();
  await page.locator('.composer__scope[title="选择本次分析的数据范围"]').click();
  const scope = page.getByRole('dialog', { name: '选择数据范围' });
  await expect(scope.locator('label').filter({ hasText: agent.source.name }).getByRole('checkbox')).toBeChecked();
  await expect(scope).not.toContainText(extra.name);
});

test('保存草稿保留线上版本，发布新版本后切换用户端配置', async ({ page }) => {
  const agent = await publishedAgent(page, '线上名称助手');
  await page.goto('/#/admin/agents');
  await page.locator('.file-tile').filter({ hasText: '线上名称助手' }).getByRole('button', { name: '编辑', exact: true }).click();
  const modal = page.locator('.modal');
  await modal.locator('input').first().fill('新名称草稿助手');
  await modal.locator('.modal__foot').getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.locator('.file-tile').filter({ hasText: '新名称草稿助手' })).toContainText('有未发布修改');
  const live = (await (await page.request.get('/api/agents?view=published')).json()).items.find(item => item.id === agent.id);
  expect(live.name).toBe('线上名称助手');
  await modal.locator('.modal__head button[aria-label="关闭"]').click();
  await page.goto('/#/workbench');
  const drawer = await chooseDrawer(page);
  await expect(drawer).toContainText('线上名称助手');
  await expect(drawer).not.toContainText('新名称草稿助手');
  await page.goto('/#/admin/agents');
  await page.locator('.file-tile').filter({ hasText: '新名称草稿助手' }).getByRole('button', { name: '编辑', exact: true }).click();
  await page.locator('.modal__foot').getByRole('button', { name: '发布新版本', exact: true }).click();
  await expect.poll(async () => {
    const result = await (await page.request.get('/api/agents?view=published')).json();
    return result.items.find(item => item.id === agent.id)?.name;
  }).toBe('新名称草稿助手');
  await page.locator('.modal__head button[aria-label="关闭"]').click();
  await page.goto('/#/workbench');
  await expect(await chooseDrawer(page)).toContainText('新名称草稿助手');
});

test('内置超级智能体使用授权数据并能够创建实际分析', async ({ page }) => {
  await page.goto('/#/workbench');
  const drawer = await chooseDrawer(page);
  await drawer.getByRole('button').filter({ hasText: '数擎超级智能体' }).click();
  await page.locator('.composer__input').fill('查询销售额');
  const responsePromise = page.waitForResponse(response => response.url().includes('/api/analyses')
    && response.request().method() === 'POST');
  await page.locator('.composer__send').click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const analysis = (await response.json()).item;
  expect(analysis.agent_id).toBe('agent-superskill');
  expect(analysis.source_scope.length).toBeGreaterThan(0);
  await expect(page).toHaveURL(/#\/conversation/);
});

test('编辑器显示遗留失效绑定并允许解除', async ({ page }) => {
  const agent = await publishedAgent(page, '解除失效绑定助手');
  await page.route('**/api/agents', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch();
    const data = await response.json();
    const item = data.items.find(item => item.id === agent.id);
    item.source_ids = [...item.source_ids, 'legacy-missing-source'];
    item.knowledge_document_ids = ['legacy-missing-document'];
    await route.fulfill({ response, json: data });
  });
  await page.goto('/#/admin/agents');
  await page.locator('.file-tile').filter({ hasText: '解除失效绑定助手' }).getByRole('button', { name: '编辑', exact: true }).click();
  const modal = page.locator('.modal');
  await modal.locator('.builder__rail button').filter({ hasText: '数据' }).click();
  const missingSource = modal.locator('label').filter({ hasText: 'legacy-missing-source' });
  await missingSource.getByRole('checkbox').click();
  await expect(missingSource).toHaveCount(0);
  await modal.locator('.builder__rail button').filter({ hasText: '知识' }).click();
  const missingDocument = modal.locator('label').filter({ hasText: 'legacy-missing-document' });
  await missingDocument.getByRole('checkbox').click();
  await expect(missingDocument).toHaveCount(0);
  const saved = page.waitForResponse(response => response.url().endsWith(`/api/agents/${agent.id}`)
    && response.request().method() === 'PATCH');
  await modal.locator('.modal__foot').getByRole('button', { name: '保存', exact: true }).click();
  expect((await saved).status()).toBe(200);
});

test('模型、技能、指标和连接的失效绑定都可解除保存', async ({ page }) => {
  const agent = await publishedAgent(page, '完整解除失效配置助手');
  await page.route('**/api/agents', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch();
    const data = await response.json();
    Object.assign(data.items.find(item => item.id === agent.id), {
      provider_id: 'legacy-missing-model', skill_ids: ['legacy-missing-skill'],
      metric_ids: ['legacy-missing-metric'], mcp_server_ids: ['legacy-missing-mcp'],
    });
    await route.fulfill({ response, json: data });
  });
  await page.goto('/#/admin/agents');
  await page.locator('.file-tile').filter({ hasText: agent.name }).getByRole('button', { name: '编辑', exact: true }).click();
  const modal = page.locator('.modal');
  await modal.locator('.builder__rail').getByRole('button', { name: '模型', exact: true }).click();
  await expect(modal).toContainText('legacy-missing-model');
  await modal.locator('label').filter({ hasText: '使用工作空间默认模型' }).getByRole('radio').check();
  for (const [section, id] of [['技能', 'legacy-missing-skill'], ['MCP', 'legacy-missing-mcp']]) {
    await modal.locator('.builder__rail').getByRole('button', { name: section, exact: true }).click();
    const binding = modal.locator('label').filter({ hasText: id });
    await binding.getByRole('checkbox').click();
    await expect(binding).toHaveCount(0);
  }
  await modal.locator('.builder__rail').getByRole('button', { name: '指标', exact: true }).click();
  await modal.getByRole('button').filter({ hasText: 'legacy-missing-metric' }).click();
  const saved = page.waitForResponse(response => response.url().endsWith(`/api/agents/${agent.id}`)
    && response.request().method() === 'PATCH');
  await modal.locator('.modal__foot').getByRole('button', { name: '保存', exact: true }).click();
  expect((await saved).status()).toBe(200);
  const actual = (await (await page.request.get('/api/agents')).json()).items.find(item => item.id === agent.id);
  expect(actual.provider_id).toBeNull();
  expect(actual.skill_ids).toEqual([]);
  expect(actual.metric_ids).toEqual([]);
  expect(actual.mcp_server_ids).toEqual([]);
});
