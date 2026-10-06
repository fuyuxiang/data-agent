import { expect, test } from '@playwright/test';

/** 指标中心、资料库、智能体：V2 的一等模块与用户端导航。 */

test.beforeEach(async ({ page }) => {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
});

test('用户端导航只有四个入口', async ({ page }) => {
  await page.goto('/#/workbench');
  await expect(page.locator('.sidebar .nav-item')).toHaveCount(4);
  const labels = await page.locator('.sidebar .main-nav .nav-item span').allInnerTexts();
  expect(labels).toEqual(['工作台', '智能体', '资料库', '指标中心']);
});

test('用户端不出现内部技术概念', async ({ page }) => {
  for (const route of ['workbench', 'agents', 'library', 'metrics']) {
    await page.goto(`/#/${route}`);
    await page.waitForTimeout(500);
    const text = await page.locator('.view').innerText();
    for (const forbidden of ['MCP', '模型服务', 'Contract', 'Plan', 'Action', 'Trace', '审计日志', 'Workflow']) {
      expect(text, `${route} 不应出现 ${forbidden}`).not.toContain(forbidden);
    }
  }
});

test('指标中心是一等模块：可搜索、可看口径、可试算', async ({ page }) => {
  await page.goto('/#/metrics');
  await expect(page.locator('.page-head__title')).toHaveText('指标中心');
  const rows = page.locator('.metric-layout article.card');
  await expect(rows.first()).toBeVisible();
  await expect(page.locator('.metric-layout')).toContainText('销售额');

  await page.locator('.metric-layout article.card:has(b:text-is("销售额"))').click();
  const main = page.locator('.definition').first();
  await expect(main).toContainText('业务定义');
  await expect(main).toContainText('计算方式');
  await expect(main).toContainText('同义词');
  // 版本等治理信息默认折叠在高级设置里
  await expect(page.locator('.definition')).toHaveCount(1);
  await page.locator('.advanced-toggle').click();
  await expect(page.locator('.definition')).toHaveCount(2);
  await expect(page.locator('.definition').last()).toContainText('技术负责人');

  await expect(page.locator('.card').filter({ hasText: '试算' })).toBeVisible();
  await page.locator('.card').filter({ hasText: '试算' }).locator('button', { hasText: '运行试算' }).click();
  await expect(page.locator('.card').filter({ hasText: '试算' }).locator('pre')).toContainText('SELECT');
});

test('指标中心按内部四个分组组织，不拆一级 Tab', async ({ page }) => {
  await page.goto('/#/metrics');
  await expect(page.locator('.page-head__title')).toHaveText('指标中心');
  await expect(page.locator('.tabs button').first()).toBeVisible();
  const tabs = await page.locator('.tabs button').allInnerTexts();
  expect(tabs[0]).toMatch(/^指标\d+$/);
  expect(Number(tabs[0].replace('指标', ''))).toBeGreaterThanOrEqual(8);
  expect(tabs[1]).toBe('维度7');
  expect(tabs[2]).toMatch(/^语义模型\d+$/);
  expect(Number(tabs[2].replace('语义模型', ''))).toBeGreaterThanOrEqual(1);
  expect(tabs[3]).toBe('业务术语');
});

test('资料库按用户能理解的分类组织', async ({ page }) => {
  await page.goto('/#/library');
  await expect(page.locator('.page-head__title')).toHaveText('资料库');
  await expect(page.locator('.toolbar .chip').first()).toBeVisible();
  const chips = await page.locator('.toolbar .chip').allInnerTexts();
  for (const label of ['全部', '报告', '演示文稿', '表格', '网页']) {
    expect(chips.join(' ')).toContain(label);
  }
  // 还没有产出时给出的是可操作的空状态，不是空白
  await expect(page.locator('.empty h3')).toContainText('资料库还是空的');
  await expect(page.locator('.empty p')).toContainText('生成报告');
});

test('资料库文件可作为后续分析的证据', async ({ page }) => {
  await page.goto('/#/library');
  await page.locator('input[type="file"]').setInputFiles({
    name: '门店备忘.txt', mimeType: 'text/plain', buffer: Buffer.from('门店 A 本月销售额 120 元'),
  });
  await expect(page.locator('.file-tile')).toContainText('门店备忘');
  await page.locator('.chip', { hasText: '上传文件' }).click();
  await expect(page.locator('.file-tile')).toHaveCount(1);
  await page.locator('.file-tile button', { hasText: '追问' }).click();
  await expect(page).toHaveURL(/#\/workbench\?.*file=/);
  await expect(page.locator('.composer__input')).toHaveValue(/门店备忘/);
  await page.locator('.composer__send').click();
  await expect(page).toHaveURL(/#\/conversation/);
  const sessionId = new URLSearchParams(page.url().split('?')[1]).get('id');
  const runs = await page.request.get(`/api/analyses?session_id=${sessionId}`);
  const runId = (await runs.json()).items[0].id;
  const attachments = await page.request.get(`/api/analyses/${runId}/attachments`);
  expect((await attachments.json()).items.map(item => item.filename)).toContain('门店备忘.txt');
});

test('智能体页展示可使用的智能体', async ({ page }) => {
  await page.goto('/#/agents');
  await expect(page.locator('.page-head__title')).toHaveText('智能体');
  await expect(page.locator('.agent-card').first()).toBeVisible();
  await expect(page.locator('.agent-card').first()).toBeVisible();
  await expect(page.locator('.agent-card').first()).toContainText('数擎超级智能体');
  await expect(page.locator('.agent-card').first()).toContainText('开始使用');
});
