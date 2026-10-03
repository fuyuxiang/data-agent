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
