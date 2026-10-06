import { expect, test } from '@playwright/test';

/** 管理后台：能力、主题与响应式。 */

test.beforeEach(async ({ page }) => {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
  await page.goto('/#/admin/skills');
  await expect(page.locator('.page-head__title')).toHaveText('技能');
});

test('后台导航按构建/数据/能力/运营/系统分组', async ({ page }) => {
  const groups = await page.locator('.admin__nav-label').allInnerTexts();
  expect(groups).toEqual(['构建', '数据', '能力', '运营', '系统']);
  const items = await page.locator('.admin__nav-item').allInnerTexts();
  expect(items).toEqual([
    '智能体', '技能',
    '数据', '指标中心', '知识',
    '模型', 'MCP', '集成',
    '运行记录', '评测',
    '用户管理', '系统设置',
  ]);
});

test('技能页展示内置技能并支持打开编辑器', async ({ page }) => {
  const cards = page.locator('.grid--3 .card');
  await expect(cards).toHaveCount(10);
  await expect(page.locator('.view__inner')).toContainText('归因分析');
  await expect(page.locator('.view__inner')).toContainText('预测分析');

  await page.locator('button', { hasText: '查看' }).first().click();
  await expect(page.locator('.modal')).toBeVisible();
  // 编辑器是三栏：左配置 / 中编辑 / 右测试，没有节点连线
  await expect(page.locator('.builder__rail')).toBeVisible();
  await expect(page.locator('.builder__test')).toContainText('测试');
  await expect(page.locator('.modal')).toContainText('触发与示例');
  await expect(page.locator('.modal')).toContainText('高级');
});

test('描述生成技能配置草稿，可继续编辑', async ({ page }) => {
  await page.locator('button', { hasText: '描述生成草稿' }).click();
  await page.locator('.modal textarea').fill(
    '创建一个门店经营分析技能，需要分析销售额、订单量、客单价、库存以及同比环比，并输出主要问题和经营建议。',
  );
  await page.locator('.modal button', { hasText: '生成草稿' }).click();
  await expect(page.locator('.modal')).toContainText('触发');
  await expect(page.locator('.modal')).toContainText('将使用');

  await page.locator('.modal button', { hasText: '载入到编辑器' }).click();
  // 草稿落到编辑器里，字段齐全即可，不要求模型参与
  await expect(page.locator('.modal .builder__rail')).toBeVisible();
  await expect(page.locator('.modal input').first()).not.toHaveValue('');
  await page.locator('.modal footer button', { hasText: '保存' }).click();
  await expect(page.locator('.view')).toContainText('门店经营分析');
});

test('从数据表建立语义模型并新建指标', async ({ page }) => {
  await page.goto('/#/admin/metrics');
  await expect(page.locator('.page-head__title')).toHaveText('指标中心');
  await page.locator('.tabs button', { hasText: '语义模型' }).click();
  await page.getByRole('button', { name: '新建语义模型' }).click();
  const model = page.locator('.modal');
  await model.locator('input').first().fill('门店复盘事实模型');
  await model.getByRole('button', { name: '添加度量' }).click();
  const measure = model.locator('.row:has(button[aria-label="删除度量"])');
  await measure.locator('input').first().fill('review_sales');
  await measure.locator('input').nth(1).fill('复盘销售额');
  await measure.locator('select').first().selectOption({ label: '销售额' });
  await model.getByRole('button', { name: '保存模型' }).click();
  await expect(page.locator('.view')).toContainText('门店复盘事实模型');

  await page.locator('.tabs button', { hasText: '指标' }).first().click();
  await page.getByRole('button', { name: '新建指标' }).click();
  const metric = page.locator('.modal');
  await metric.locator('input').nth(0).fill('复盘销售额');
  await metric.locator('input').nth(1).fill('review_sales_metric');
  await metric.locator('select').first().selectOption({ label: '门店复盘事实模型' });
  await metric.locator('select').nth(2).selectOption({ label: '复盘销售额' });
  await metric.getByRole('button', { name: '保存为草稿' }).click();
  await expect(page.locator('.view')).toContainText('复盘销售额');
});

test('停用模型的删除确认仍展示关联指标', async ({ page }) => {
  const models = await (await page.request.get('/api/semantic/models')).json();
  const sample = models.items[0];
  expect(sample).toBeTruthy();
  const created = await page.request.post('/api/semantic/models', { data: {
    name: '待清理语义模型', source_id: sample.source_id, table: sample.table,
    dimensions: sample.dimensions, measures: sample.measures,
  } });
  expect(created.ok()).toBeTruthy();
  const model = (await created.json()).item;
  const metric = await page.request.post('/api/semantic/metrics', { data: {
    name: 'pending_model_metric', model_id: model.id, measure: sample.measures[0].name, status: 'draft',
  } });
  expect(metric.ok()).toBeTruthy();
  expect((await page.request.patch(`/api/semantic/models/${model.id}`, { data: { enabled: false } })).ok()).toBeTruthy();

  await page.goto('/#/admin/metrics');
  await page.locator('.tabs button', { hasText: '语义模型' }).click();
  const card = page.locator('article.card').filter({ hasText: '待清理语义模型' });
  await card.getByRole('button', { name: '删除模型' }).click();
  const dialog = page.getByRole('dialog', { name: '删除语义模型' });
  await expect(dialog).toContainText('pending_model_metric');
  await expect(dialog).toContainText('请先启用模型');
  await expect(dialog.getByRole('button', { name: '确认删除' })).toHaveCount(0);
});

test('指标删除先提示依赖，移除依赖后可完成删除', async ({ page }) => {
  const models = await (await page.request.get('/api/semantic/models')).json();
  const sample = models.items[0];
  const created = await page.request.post('/api/semantic/models', { data: {
    name: '删除验证模型', source_id: sample.source_id, table: sample.table,
    dimensions: sample.dimensions, measures: sample.measures,
  } });
  expect(created.ok()).toBeTruthy();
  const model = (await created.json()).item;
  const baseResponse = await page.request.post('/api/semantic/metrics', { data: {
    name: 'delete_review_base', model_id: model.id,
    measure: sample.measures[0].name, status: 'approved',
  } });
  expect(baseResponse.ok()).toBeTruthy();
  const base = (await baseResponse.json()).item;
  const dependentResponse = await page.request.post('/api/semantic/metrics', { data: {
    name: 'delete_review_derived', model_id: model.id, metric_type: 'derived',
    expression: 'delete_review_base * 2', status: 'approved',
  } });
  expect(dependentResponse.ok()).toBeTruthy();
  const dependent = (await dependentResponse.json()).item;

  await page.goto('/#/admin/metrics');
  await page.locator('article.card:has(b:text-is("delete_review_base"))')
    .getByRole('button', { name: '删除指标' }).click();
  const dialog = page.getByRole('dialog', { name: '删除指标' });
  await expect(dialog).toContainText('delete_review_derived');
  await expect(dialog.getByRole('button', { name: '确认删除' })).toHaveCount(0);
  await dialog.getByRole('button', { name: '取消' }).click();

  expect((await page.request.delete(`/api/semantic/metrics/${dependent.id}`)).ok()).toBeTruthy();
  await page.reload();
  await page.locator('article.card:has(b:text-is("delete_review_base"))')
    .getByRole('button', { name: '删除指标' }).click();
  await expect(dialog.getByRole('button', { name: '确认删除' })).toBeEnabled();
  await dialog.getByRole('button', { name: '确认删除' }).click();
  await expect(page.locator('article.card:has(b:text-is("delete_review_base"))')).toHaveCount(0);
  expect((await page.request.get(`/api/semantic/metrics/${base.id}/references`)).status()).toBe(404);
});

test('多表质量检查重试和切表时只显示当前表结果', async ({ page }) => {
  const source = {
    id: 'src_two_tables', name: '双表数据', kind: 'file', status: 'ready',
    tables: [{ name: 'a', rows: 111, columns: 1 }, { name: 'b', rows: 222, columns: 1 }],
  };
  await page.route('**/api/sources', route => route.fulfill({ json: { ok: true, items: [source] } }));
  let releaseFirst;
  const firstResponse = new Promise(resolve => { releaseFirst = resolve; });
  const requested = [];
  let bRequests = 0;
  await page.route('**/api/sources/src_two_tables/profile*', async (route) => {
    const table = new URL(route.request().url()).searchParams.get('table');
    requested.push(table);
    if (table === 'a') await firstResponse;
    if (table === 'b' && ++bRequests === 1) {
      await route.fulfill({ status: 500, json: { ok: false, error: '暂时失败' } });
      return;
    }
    await route.fulfill({ json: { ok: true, profile: {
      rows: table === 'a' ? 111 : 222, column_count: 1, missing_cells: 0,
      duplicate_rows: 0, columns: [],
    } } });
  });

  await page.goto('/#/admin/data');
  await page.locator('.file-tile').filter({ hasText: '双表数据' }).getByRole('button', { name: '查看' }).click();
  const drawer = page.getByRole('dialog', { name: '双表数据' });
  await drawer.getByRole('tab', { name: '数据质量' }).click();
  await drawer.getByRole('button', { name: '运行数据质量检查' }).click();
  await expect.poll(() => requested).toContain('a');
  await drawer.locator('select').selectOption('b');
  await expect(drawer).toContainText('暂时失败');
  await drawer.getByRole('button', { name: '运行数据质量检查' }).click();
  await expect(drawer.locator('.metric-strip__item').first()).toContainText('222');
  releaseFirst();
  await expect.poll(() => requested.filter(table => table === 'b').length).toBe(2);
  await expect(drawer.locator('.metric-strip__item').first()).toContainText('222');
});

test('智能体发布会保存当前编辑，删除需要确认', async ({ page }) => {
  const sources = await (await page.request.get('/api/sources')).json();
  const created = await page.request.post('/api/agents', { data: {
    name: '待发布助手', source_ids: [sources.items[0].id], skill_ids: [],
  } });
  expect(created.ok()).toBeTruthy();
  const agentId = (await created.json()).item.id;
  await page.goto('/#/admin/agents');
  const tile = page.locator('.file-tile').filter({ hasText: '待发布助手' });
  await tile.getByRole('button', { name: '编辑' }).click();
  await page.locator('.modal input').first().fill('已发布助手');
  await page.locator('.modal__foot').getByRole('button', { name: '发布' }).click();
  await expect.poll(async () => {
    const agents = await (await page.request.get('/api/agents')).json();
    const agent = agents.items.find(item => item.id === agentId);
    return { name: agent?.name, status: agent?.status };
  }).toEqual({ name: '已发布助手', status: 'published' });
  await page.locator('.modal__head button').click();
  await page.locator('.file-tile').filter({ hasText: '已发布助手' })
    .getByRole('button', { name: '删除' }).click();
  await expect(page.getByRole('dialog', { name: '删除智能体' })).toContainText('已发布助手');
  await page.getByRole('dialog', { name: '删除智能体' })
    .getByRole('button', { name: '删除' }).click();
  await expect(page.locator('.file-tile').filter({ hasText: '已发布助手' })).toHaveCount(0);
});

test('运行记录与评测是后台的运营视图', async ({ page }) => {
  await page.goto('/#/admin/runs');
  await expect(page.locator('.page-head__title')).toHaveText('运行记录');
  await page.goto('/#/admin/evaluations');
  await expect(page.locator('.page-head__title')).toHaveText('评测');
  await expect(page.locator('.metric-strip__item').first()).toBeVisible();
});

test('深浅色模式切换不丢失当前页面', async ({ page }) => {
  const before = await page.locator('.page-head__title').innerText();
  await page.locator('.topbar .icon-btn').last().click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.locator('.page-head__title')).toHaveText(before);
  await page.locator('.topbar .icon-btn').last().click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
});

test('深色模式下侧栏与内容区都成立', async ({ page }) => {
  await page.locator('.topbar .icon-btn').last().click();
  const sidebar = await page.locator('.sidebar').evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  );
  const body = await page.locator('.app').evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  );
  expect(sidebar).not.toBe(body);
});

for (const width of [1280, 1440, 1920]) {
  test(`关键页面在 ${width} 宽度下无横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    for (const route of ['workbench', 'metrics', 'admin/skills', 'admin/data']) {
      await page.goto(`/#/${route}`);
      await page.waitForTimeout(600);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, `${route} 在 ${width} 下出现横向溢出`).toBeLessThanOrEqual(1);
    }
  });
}

test('侧栏可折叠', async ({ page }) => {
  const wide = await page.locator('.app').evaluate(
    (node) => getComputedStyle(node).gridTemplateColumns.split(' ')[0],
  );
  await page.locator('.topbar .icon-btn').first().click();
  await page.waitForTimeout(300);
  const narrow = await page.locator('.app').evaluate(
    (node) => getComputedStyle(node).gridTemplateColumns.split(' ')[0],
  );
  expect(parseInt(narrow, 10)).toBeLessThan(parseInt(wide, 10));
});
