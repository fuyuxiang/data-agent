import { expect, test } from '@playwright/test';

async function bootstrapWith(page, patch) {
  await page.route('**/api/bootstrap?*', async route => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ response, json: { ...data, ...patch } });
  });
}

test('空数据引导明确接入职责，载入演示数据不承诺自动开始分析', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await bootstrapWith(page, { sources: [], agents: [], recommended_questions: [], providers: [] });
  await page.goto('/#/workbench');
  await expect(page.locator('.workbench__title')).toContainText('业务数据分析');
  await expect(page.locator('.workbench__hello')).not.toContainText('给出可核验的结论');
  await expect(page.locator('.workbench .card')).toContainText('请联系管理员');
  await expect.poll(() => page.locator('.view').evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(2);
  await page.getByRole('button', { name: '使用演示数据', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('合成样例');
  await expect(page.getByRole('dialog')).toContainText('不代表真实企业数据');
  await expect(page.getByRole('dialog').getByRole('button', { name: '载入演示数据', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).not.toContainText('载入并开始');
});

test('只读权限下保留查看入口并明确不能发起分析', async ({ page }) => {
  await bootstrapWith(page, { active_membership: { role: 'viewer' } });
  await page.goto('/#/workbench');
  await expect(page.locator('.composer__input')).toBeDisabled();
  await expect(page.locator('.view')).toContainText('发起分析需要分析权限');
  await expect(page.locator('.sidebar .main-nav .nav-item')).toHaveCount(4);
  await expect(page.locator('.sidebar').getByRole('button', { name: '管理后台', exact: true })).toHaveCount(0);
});

test('模型空态说明自主分析前提，正式指标仍能执行确定性试算', async ({ page }) => {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
  await bootstrapWith(page, { providers: [] });
  await page.route('**/api/providers', route => route.fulfill({ json: { ok: true, items: [] } }));
  await page.goto('/#/admin/models');
  await expect(page.locator('.empty')).toContainText('测试连接');
  await expect(page.locator('.empty')).toContainText('自主分析');
  await page.goto('/#/metrics');
  await page.locator('.metric-item').filter({ has: page.locator('b', { hasText: /^销售额$/ }) }).click();
  const response = page.waitForResponse(item => new URL(item.url()).pathname === '/api/admin/metric-trial'
    && item.request().method() === 'POST');
  await page.getByRole('button', { name: '运行试算', exact: true }).click();
  expect((await response).ok()).toBe(true);
  await expect(page.locator('.card').filter({ hasText: '试算' }).locator('tbody tr').first()).toBeVisible();
});

test('MCP 未连接状态使用显示名称，保留原始状态值和连接操作', async ({ page }) => {
  await page.route('**/api/mcp/servers', route => route.fulfill({ json: { ok: true, items: [
    { id: 'copy-disconnected', name: '业务只读工具', status: 'disconnected', transport: 'http',
      url: 'https://example.test/mcp', enabled: true },
  ] } }));
  await page.goto('/#/admin/mcp');
  const card = page.locator('.card').filter({ has: page.getByText('业务只读工具', { exact: true }) });
  await expect(card.locator('.status')).toHaveText('未连接');
  await expect(card.locator('.status')).toHaveAttribute('data-status', 'disconnected');
  await expect(card.getByRole('button', { name: '测试连接', exact: true })).toBeEnabled();
  await expect(page.locator('.view')).toContainText('操作前须明确确认');
});

const STATES = [
  ['queued', { execution_status: 'queued' }, '正在分析'],
  ['running', { execution_status: 'running', hasChart: true }, '正在分析'],
  ['waiting_input', { execution_status: 'waiting_input', stop_reason: 'contract_confirmation_required' }, '等待补充或确认信息'],
  ['waiting_approval', { execution_status: 'waiting_approval', stop_reason: 'tool_approval_required' }, '等待操作授权'],
  ['waiting_job', { execution_status: 'waiting_job', stop_reason: 'external_job_running' }, '正在分析'],
  ['failed-model', { execution_status: 'failed', stop_reason: 'model_not_configured' }, '分析未完成'],
  ['failed-budget', { execution_status: 'failed', stop_reason: 'model_budget_exceeded' }, '分析未完成'],
  ['finished-blocked', { execution_status: 'finished', quality_status: 'blocked', stop_reason: 'publication_gate_blocked' }, '本次分析已结束'],
  ['finished-passed', { execution_status: 'finished', quality_status: 'passed' }, '结果通过发布校验'],
  ['partial', { execution_status: 'partial', hasChart: true }, '分析部分完成'],
  ['cancelling', { execution_status: 'cancelling', stop_reason: 'cancel_confirmation_pending' }, '正在终止分析'],
  ['cancelled', { execution_status: 'cancelled', stop_reason: 'user_cancelled' }, '分析已取消'],
];

test('原始状态枚举对应真实文案，未校验、部分完成与取消均不被包装为成功', async ({ page }) => {
  await page.goto('/#/workbench');
  await expect(page.locator('.workbench__title')).toBeVisible();
  await page.evaluate(async states => {
    const { ExecutionStatus } = await import('/src/components/execution.js');
    const host = document.createElement('div');
    host.id = 'copy-state-test';
    document.body.append(host);
    Vue.createApp({ render: () => Vue.h('div', states.map(([id, run]) =>
      Vue.h('section', { 'data-case': id }, [Vue.h(ExecutionStatus, { run })]))) }).mount(host);
  }, STATES);
  for (const [id, run, headline] of STATES) {
    const item = page.locator(`#copy-state-test [data-case="${id}"]`);
    await expect(item.locator('.execution')).toHaveAttribute('data-state', run.execution_status);
    await expect(item.locator('.execution__head')).toContainText(headline);
    await expect(item).not.toContainText('已理解问题');
    await expect(item).not.toContainText('已生成结论');
  }
  await expect(page.locator('[data-case="queued"] .step[data-state="done"]')).toHaveCount(1);
  await expect(page.locator('[data-case="queued"] .step').first()).toContainText('接收分析问题');
  await expect(page.locator('[data-case="running"] .step[data-state="done"]').last()).toContainText('整理分析结果');
  await expect(page.locator('[data-case="failed-model"]')).toContainText('工作空间所有者');
  await expect(page.locator('[data-case="failed-budget"]')).toContainText('缩小问题范围');
  await expect(page.locator('[data-case="finished-blocked"]')).toContainText('暂不能生成正式成果');
  await expect(page.locator('[data-case="finished-blocked"]')).not.toContainText('结果通过发布校验');
  await expect(page.locator('[data-case="cancelling"]')).toContainText('正在确认外部任务是否停止');
  await expect(page.locator('[data-case="cancelling"] .execution__head')).not.toContainText('分析已取消');
});

test('部分数据导出提示保留范围，未标注比较周期的指标不默认显示环比', async ({ page }) => {
  await page.goto('/#/workbench');
  await expect(page.locator('.workbench__title')).toBeVisible();
  await page.evaluate(async () => {
    const { ResultView } = await import('/src/components/result-blocks.js');
    const host = document.createElement('div');
    host.id = 'copy-result-test';
    document.body.append(host);
    Vue.createApp({ render: () => Vue.h(ResultView, {
      payload: {
        kpis: [{ id: 'sales', label: '销售额', value: 100, delta: 0.12 }],
        charts: [{ id: 'sales-chart', title: '销售预览', type: 'bar', total_rows: 100, preview_rows: 1,
          encoding: { x: ['区域 A'], series: [{ name: '销售额', values: [100] }] } }],
      },
      tables: [{ id: 'sales-table', columns: ['sales'], rows: [{ sales: 100 }], total: 100,
        sourceTotal: 1000, paginated: false, completeness: 'partial' }],
    }) }).mount(host);
  });
  const result = page.locator('#copy-result-test');
  await expect(result).toContainText('本次返回 100 条，原始结果共 1000 条');
  await expect(result).toContainText('导出 Excel 查看本次返回的明细');
  await expect(result).toContainText('变化率 +12%');
  await expect(result).not.toContainText('完整结果可查看');
  await expect(result).not.toContainText('环比');
});

const PAGES = [
  ['workbench', '工作台'], ['agents', '智能体'], ['library', '资料库'], ['metrics', '指标中心'],
  ['trash', '回收站'], ['admin/agents', '智能体'], ['admin/skills', '技能'], ['admin/data', '数据'],
  ['admin/metrics', '指标中心'], ['admin/knowledge', '知识'], ['admin/models', '模型'],
  ['admin/mcp', 'MCP'], ['admin/integrations', '集成'], ['admin/runs', '运行记录'],
  ['admin/evaluations', '评测'], ['admin/users', '用户管理'], ['admin/settings', '系统设置'],
  ['admin/trash', '回收站'],
];

for (const width of [1440, 390]) {
  test(`全部页面在 ${width}px 保持菜单名称、标题与文案布局`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const response = await page.request.post('/api/sessions', { data: { name: '文案审查空会话', source_ids: [] } });
    expect(response.ok()).toBe(true);
    const sessionId = (await response.json()).item.id;
    for (const [route, title] of [...PAGES, [`conversation?id=${sessionId}`, '对话']]) {
      await page.goto(`/#/${route}`);
      await expect(page.locator('.topbar__title')).toHaveText(title);
      await expect(page.locator('.view .skeleton')).toHaveCount(0);
      await expect(page.locator('.sidebar .main-nav .nav-item span')).toHaveText(['工作台', '智能体', '资料库', '指标中心']);
      if (route.startsWith('admin/')) {
        await expect(page.locator('.admin__nav-label')).toHaveText(['构建', '数据', '能力', '运营', '系统']);
        await expect(page.locator('.admin__nav-item')).toHaveText([
          '智能体', '技能', '数据', '指标中心', '知识', '模型', 'MCP', '集成', '运行记录', '评测', '用户管理', '系统设置', '回收站',
        ]);
      }
      await expect.poll(() => page.locator('.view').evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(2);
      await page.screenshot({ path: testInfo.outputPath(`copy-${route.split('?')[0].replaceAll('/', '-')}.png`), fullPage: true });
    }
    expect(errors).toEqual([]);
  });
}
