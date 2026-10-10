import { expect, test } from '@playwright/test';

const adminPages = {
  agents: '智能体', skills: '技能', models: '模型', mcp: 'MCP',
  integrations: '集成', runs: '运行记录', evaluations: '评测',
  users: '用户管理', settings: '系统设置',
};
const runDetail = (question, actions = []) => ({
  run: { execution_status: 'finished', quality_status: 'passed', duration_seconds: 0,
    usage: { model_tokens: 0 }, source_scope: [], started_at: null, finished_at: null },
  contract: { payload: { objective: question } }, metrics: [],
  skills: { requested: [], used: [], allowed_tools: [], warnings: [] },
  actions, decisions: [], artifacts: [],
});

const listEndpoints = {
  agents: '**/api/agents', skills: '**/api/skills', models: '**/api/providers',
  mcp: '**/api/mcp/servers', integrations: '**/api/connectors', runs: '**/api/admin/runs?*',
  evaluations: '**/api/admin/evaluations', users: '**/api/workspaces/default/members',
  settings: '**/api/lifecycle/settings',
};

for (const [route, endpoint] of Object.entries(listEndpoints)) {
  test(`${adminPages[route]}列表加载失败保持明确错误并可重试`, async ({ page }) => {
    let calls = 0;
    await page.route(endpoint, request => ++calls === 1
      ? request.fulfill({ status: 500, json: { ok: false, error: '数据暂不可用，请稍后重试' } })
      : request.continue());
    await page.goto(`/#/admin/${route}`);
    await expect(page.locator('.page-head__title')).toHaveText(adminPages[route]);
    const error = page.locator('.view').getByRole('alert');
    await expect(error).toContainText('数据暂不可用');
    await expect(page.locator('.view .empty')).toHaveCount(0);
    if (route === 'evaluations') await expect(page.locator('.metric-strip')).toHaveCount(0);
    if (route === 'runs') await expect(page.locator('.toolbar')).not.toContainText('0 条记录');
    const search = route === 'skills' ? page.getByPlaceholder('搜索技能')
      : route === 'runs' ? page.getByPlaceholder('搜索问题、智能体或技能') : null;
    if (search) await search.fill('保留筛选词');
    await error.getByRole('button', { name: '重试', exact: true }).click();
    await expect(error).toHaveCount(0);
    await expect(page.locator('.skeleton')).toHaveCount(0);
    await expect.poll(() => calls).toBeGreaterThan(1);
    if (search) await expect(search).toHaveValue('保留筛选词');
  });
}

test.beforeEach(async ({ page }) => {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
});

for (const [width, height] of [[1366, 768], [1440, 900], [1920, 1080]]) {
  test(`全部管理入口在 ${width}×${height} 可读，编辑器操作区可达`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    for (const [route, title] of Object.entries(adminPages)) {
      await page.goto(`/#/admin/${route}`);
      await expect(page.locator('.page-head__title')).toHaveText(title);
      await expect(page.locator('.admin__nav .admin__nav-item.active')).toHaveText(title);
      await expect(page.locator('.skeleton')).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), route).toBeLessThanOrEqual(1);
      if (width === 1366 && ['agents', 'skills', 'runs', 'users'].includes(route)) {
        await page.screenshot({ path: testInfo.outputPath(`admin-${route}-after.png`) });
      }
      if (route === 'agents' || route === 'skills') {
        if (route === 'agents') await page.getByRole('button', { name: '新建智能体' }).click();
        else await page.getByRole('button', { name: '查看', exact: true }).first().click();
        const modal = page.getByRole('dialog');
        await expect(modal).toBeVisible();
        await expect(modal.locator('.builder')).toBeVisible();
        await expect(modal.getByRole('button', { name: '关闭', exact: true }).last()).toBeVisible();
        const bounds = await modal.boundingBox();
        expect(bounds.x).toBeGreaterThanOrEqual(0);
        expect(bounds.y).toBeGreaterThanOrEqual(0);
        expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
        expect(bounds.y + bounds.height).toBeLessThanOrEqual(height);
        expect(await modal.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
        if (width === 1366) await page.screenshot({ path: testInfo.outputPath(`admin-${route === 'agents' ? 'builder' : 'skill-editor'}-after.png`) });
        await modal.getByRole('button', { name: '关闭', exact: true }).last().click();
      }
    }
    expect(errors).toEqual([]);
  });
}

test('窄窗口和 125%/150% 等效布局下复杂表单仍可编辑', async ({ page }) => {
  for (const [width, height] of [[1024, 768], [1093, 614], [911, 512]]) {
    await page.setViewportSize({ width, height });
    await page.goto('/#/admin/agents');
    await page.getByRole('button', { name: '新建智能体' }).click();
    const modal = page.getByRole('dialog');
    await modal.locator('.builder__rail').getByRole('button', { name: '体验设置' }).click();
    await modal.getByText('推荐问题（每行一个，最多 8 条）').locator('..').locator('textarea').fill('超长中文'.repeat(50));
    await expect(modal.getByRole('button', { name: '保存', exact: true })).toBeVisible();
    expect(await modal.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
    await modal.getByRole('button', { name: '关闭', exact: true }).last().click();
  }
});

test('技能与智能体每行一项字段保留输入中的换行，保存时再规范化', async ({ page }) => {
  await page.goto('/#/admin/skills');
  await page.getByRole('button', { name: '新建技能', exact: true }).click();
  const modal = page.getByRole('dialog');
  await modal.getByRole('button', { name: '触发与示例' }).click();
  const triggers = modal.getByText('触发场景（每行一个）', { exact: true }).locator('..').locator('textarea');
  await triggers.fill('第一条');
  await triggers.press('End');
  await triggers.press('Enter');
  await triggers.pressSequentially('second_identifier');
  await expect(triggers).toHaveValue('第一条\nsecond_identifier');
  await modal.getByRole('button', { name: '关闭', exact: true }).last().click();
  await page.goto('/#/admin/agents');
  await page.getByRole('button', { name: '新建智能体' }).click();
  await modal.getByRole('button', { name: '体验设置' }).click();
  const questions = modal.getByText('推荐问题（每行一个，最多 8 条）').locator('..').locator('textarea');
  await questions.fill('第一问');
  await questions.press('End');
  await questions.press('Enter');
  await questions.pressSequentially('second_question');
  await expect(questions).toHaveValue('第一问\nsecond_question');
});

test('描述生成草稿只提交一次，失败后可保留输入重试', async ({ page }) => {
  let count = 0;
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/skills/generate', async route => {
    count += 1;
    await pending;
    await route.fulfill({ status: 500, json: { ok: false, error: '草稿服务暂不可用' } });
  });
  await page.goto('/#/admin/skills');
  await page.getByRole('button', { name: '描述生成草稿' }).click();
  const modal = page.getByRole('dialog');
  await modal.locator('textarea').fill('创建一个可用于经营复盘的分析技能');
  await modal.getByRole('button', { name: '生成草稿', exact: true }).click();
  await expect(modal.getByRole('button', { name: '生成中…' })).toBeDisabled();
  await expect(modal.getByRole('button', { name: '取消' })).toBeDisabled();
  await expect.poll(() => count).toBe(1);
  release();
  await expect(modal.getByRole('button', { name: '生成草稿', exact: true })).toBeEnabled();
  await expect(modal.locator('textarea')).toHaveValue('创建一个可用于经营复盘的分析技能');
  await expect(page.locator('.toast')).toContainText('草稿服务暂不可用');
});

test('技能详情立即反馈，失败可重试，关闭后迟到响应不重开', async ({ page }) => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  let requests = 0;
  await page.route('**/api/skills/visualization', async route => {
    requests += 1;
    if (requests === 1) {
      await route.fulfill({ status: 500, json: { ok: false, error: '技能配置暂不可用' } });
      return;
    }
    await pending;
    await route.continue();
  });
  await page.goto('/#/admin/skills');
  await page.locator('.file-tile').filter({ hasText: '数据可视化' }).getByRole('button', { name: '查看', exact: true }).click();
  const modal = page.getByRole('dialog', { name: '数据可视化', exact: true });
  await expect(modal).toContainText('技能配置暂不可用');
  await modal.getByRole('button', { name: '重试', exact: true }).click();
  await expect(modal).toContainText('正在读取技能配置');
  await modal.getByRole('button', { name: '关闭', exact: true }).last().click();
  release();
  await expect(modal).toHaveCount(0);
});

test('智能体测试输入法确认不提交，关闭编辑器后旧任务不污染新配置', async ({ page }) => {
  let submitted = 0;
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/analyses', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    submitted += 1;
    await pending;
    await route.fulfill({ json: { ok: true, item: { id: 'late-run', session_id: 'late-session' } } });
  });
  await page.goto('/#/admin/agents');
  await page.getByRole('button', { name: '新建智能体' }).click();
  const modal = page.getByRole('dialog');
  const input = modal.getByRole('textbox', { name: '测试问题', exact: true });
  await input.fill('库存情况');
  await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 229, isComposing: true });
  expect(submitted).toBe(0);
  await input.press('Enter');
  await expect.poll(() => submitted).toBe(1);
  await modal.getByRole('button', { name: '关闭', exact: true }).last().click();
  await page.getByRole('button', { name: '新建智能体' }).click();
  release();
  await expect(modal.locator('.builder__test')).toContainText('还没有测试记录');
  await expect(modal.locator('.builder__test')).not.toContainText('正在使用当前配置分析');
});

test('智能体测试进度请求失败后不会残留运行中消息，并保留重试问题', async ({ page }) => {
  await page.route('**/api/analyses', route => route.request().method() === 'POST'
    ? route.fulfill({ json: { ok: true, item: { id: 'admin-progress-fail', session_id: 'admin-session' } } })
    : route.continue());
  await page.route('**/api/analyses/admin-progress-fail', route => route.fulfill({ status: 500, json: { ok: false, error: '进度服务暂不可用' } }));
  await page.goto('/#/admin/agents');
  await page.getByRole('button', { name: '新建智能体' }).click();
  const modal = page.getByRole('dialog');
  await modal.getByRole('textbox', { name: '测试问题', exact: true }).fill('查询库存');
  await modal.getByRole('button', { name: '发送测试问题', exact: true }).click();
  await expect(modal.locator('.builder__test')).toContainText('测试进度读取失败');
  await expect(modal.locator('.builder__test')).not.toContainText('正在使用当前配置分析');
  await expect(modal.getByRole('textbox', { name: '测试问题', exact: true })).toHaveValue('查询库存');
  await expect(modal.getByRole('button', { name: '发送测试问题', exact: true })).toBeEnabled();
});

test('运行记录搜索、零耗时、失败详情重试和完整 SQL 可用', async ({ page }, testInfo) => {
  const sql = 'SELECT ' + 'very_long_column_identifier,'.repeat(40) + ' amount FROM orders';
  await page.route('**/api/admin/runs?*', route => route.fulfill({ json: { ok: true, items: [
    { id: 'run-zero', question: '零耗时查询', execution_status: 'finished', duration_seconds: 0, skill_ids: [], created_at: '2026-10-10T12:34:56Z' },
    { id: 'run-other', question: '其他查询', execution_status: 'failed', skill_ids: [] },
  ] } }));
  let calls = 0;
  await page.route('**/api/admin/runs/run-zero', route => {
    calls += 1;
    return calls === 1 ? route.fulfill({ status: 500, json: { ok: false, error: '详情暂不可用' } })
      : route.fulfill({ json: { ok: true, item: runDetail('零耗时查询', [{ tool_id: 'query_data', status: 'failed', arguments: { sql }, created_at: '1' }]) } });
  });
  await page.goto('/#/admin/runs');
  await page.getByPlaceholder('搜索问题、智能体或技能').fill('零耗时');
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page.locator('tbody')).toContainText('0s');
  await page.getByRole('button', { name: '详情', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: '运行详情' });
  await expect(drawer).toContainText('详情暂不可用');
  await drawer.getByRole('button', { name: '重试' }).click();
  await expect(drawer).toContainText('0s');
  await drawer.getByRole('tab', { name: '工具调用' }).click();
  await drawer.getByText('查看 SQL', { exact: true }).click();
  await expect(drawer.locator('pre')).toHaveText(sql);
  expect(await drawer.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath('admin-run-sql-after.png') });
});

test('运行详情关闭后不被迟到响应重新打开', async ({ page }) => {
  await page.route('**/api/admin/runs?*', route => route.fulfill({ json: { ok: true, items: [
    { id: 'late', question: '迟到的查询', skill_ids: [] },
  ] } }));
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/admin/runs/late', async route => {
    await pending;
    await route.fulfill({ json: { ok: true, item: runDetail('迟到的查询') } });
  });
  await page.goto('/#/admin/runs');
  await page.getByRole('button', { name: '详情', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: '运行详情' });
  await expect(drawer).toContainText('正在读取运行详情');
  await drawer.getByRole('button', { name: '关闭', exact: true }).click();
  release();
  await expect(drawer).toHaveCount(0);
});

test('评测有反馈和长模型名时正常渲染', async ({ page }) => {
  await page.route('**/api/admin/evaluations', route => route.fulfill({ json: { ok: true,
    totals: { runs: 1, finished: 1, published: 1, failed: 0, satisfaction: 0 },
    reasons: [], failures: [], usage: [{ model: 'long_model_identifier_'.repeat(15), requests: 1, total_tokens: 123456789 }],
    feedback: [{ id: 'feedback', rating: 'incorrect', comment: '反馈内容'.repeat(40), created_at: '2026-10-10T12:34:56Z' }],
  } }));
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/#/admin/evaluations');
  await expect(page.locator('.view')).toContainText('需改进');
  await expect(page.locator('.view')).toContainText('0%');
  await expect(page.locator('.view')).toContainText('123,456,789');
  expect(errors).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
});

test('MCP 保存防重复，测试读取真实 result.tools，工具失败可重试', async ({ page }) => {
  let saving = 0;
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/mcp/servers', async route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { ok: true, items: [] } });
    saving += 1;
    await pending;
    await route.fulfill({ json: { ok: true, item: { id: 'mcp-review', name: '工具连接', url: 'https://example.com/mcp', transport: 'http' } } });
  });
  await page.route('**/api/mcp/servers/mcp-review/test', route => route.fulfill({ json: { ok: true, result: { tools: [{ name: 'read_data' }, { name: 'delete_data' }] } } }));
  let reads = 0;
  await page.route('**/api/mcp/servers/mcp-review/tools', route => ++reads === 1
    ? route.fulfill({ status: 500, json: { ok: false, error: '工具清单暂不可用' } })
    : route.fulfill({ json: { ok: true, items: [{ name: 'read_data', description: '读取数据' }] } }));
  await page.goto('/#/admin/mcp');
  await page.getByRole('button', { name: '添加 MCP 服务' }).click();
  let modal = page.getByRole('dialog');
  await modal.getByText('名称', { exact: false }).first().locator('..').locator('input').fill('工具连接');
  await modal.getByText('服务地址', { exact: false }).locator('..').locator('input').fill('https://example.com/mcp');
  await modal.getByRole('button', { name: '保存', exact: true }).click();
  await expect(modal.getByRole('button', { name: '保存中…' })).toBeDisabled();
  expect(saving).toBe(1);
  release();
  await expect(modal).toHaveCount(0);
  await page.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(page.locator('.view')).toContainText('2 个工具');
  await page.getByRole('button', { name: '查看工具' }).click();
  modal = page.getByRole('dialog');
  await expect(modal).toContainText('工具清单暂不可用');
  await modal.getByRole('button', { name: '重试' }).click();
  await expect(modal).toContainText('read_data');
});

test('集成提交真实类型和 SMTP 字段，现有连接不调用不存在的更新接口', async ({ page }) => {
  const payloads = [];
  await page.route('**/api/connectors', route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { ok: true, items: [] } });
    const payload = route.request().postDataJSON();
    payloads.push(payload);
    return route.fulfill({ json: { ok: true, item: { id: `conn-${payload.type}`, type: payload.type, name: payload.name, configured: true, status: 'configured' } } });
  });
  await page.goto('/#/admin/integrations');
  const feishu = page.locator('.file-tile').filter({ hasText: '飞书' });
  await feishu.getByRole('button', { name: '接入', exact: true }).click();
  let modal = page.getByRole('dialog');
  await modal.getByText('Webhook 地址').locator('..').locator('input').fill('https://example.com/webhook');
  await modal.getByRole('button', { name: '保存', exact: true }).click();
  await expect(modal).toHaveCount(0);
  expect(payloads[0].type).toBe('lark');
  await feishu.getByRole('button', { name: '查看配置' }).click();
  modal = page.getByRole('dialog');
  await expect(modal).toContainText('不支持更新已保存的连接凭据');
  await expect(modal.getByRole('button', { name: '保存', exact: true })).toHaveCount(0);
  await modal.getByRole('button', { name: '关闭', exact: true }).last().click();
  await page.locator('.file-tile').filter({ hasText: '邮件' }).getByRole('button', { name: '接入', exact: true }).click();
  modal = page.getByRole('dialog');
  await modal.getByText('SMTP 主机').locator('..').locator('input').fill('smtp.example.com');
  await modal.getByText('收件人', { exact: false }).locator('..').locator('input').fill('review@example.com');
  await modal.getByRole('button', { name: '保存', exact: true }).click();
  await expect(modal).toHaveCount(0);
  expect(payloads[1]).toMatchObject({ type: 'email', host: 'smtp.example.com', recipient: 'review@example.com', port: 587, use_tls: true });
});

test('成员角色更新失败恢复原值，系统设置保存防重复', async ({ page }) => {
  await page.route('**/api/workspaces/default/members', route => route.fulfill({ json: { ok: true, items: [{ user_id: 'review-member', name: '体验复核成员', email: 'review@example.com', role: 'analyst' }] } }));
  await page.route('**/api/workspaces/default/members/review-member', route => route.fulfill({ status: 500, json: { ok: false, error: '角色更新暂不可用' } }));
  await page.goto('/#/admin/users');
  const role = page.getByRole('combobox', { name: '修改体验复核成员的角色' });
  await role.selectOption('viewer');
  await expect(role).toHaveValue('analyst');
  await expect(role).toBeEnabled();
  let saves = 0;
  const methods = [];
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/lifecycle/settings', async route => {
    if (route.request().method() === 'GET') return route.continue();
    saves += 1;
    methods.push(route.request().method());
    await pending;
    await route.continue();
  });
  await page.goto('/#/admin/settings');
  await page.getByRole('button', { name: '保存设置', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存中…' })).toBeDisabled();
  expect(saves).toBe(1);
  release();
  await expect(page.getByRole('button', { name: '保存设置', exact: true })).toBeEnabled();
  expect(methods).toEqual(['PUT']);
  await expect(page.locator('.toast').filter({ hasText: '设置已保存' })).toBeVisible();
});

test('保留策略通过真实接口保存、重载回显，自定义校验可达', async ({ page }) => {
  await page.goto('/#/admin/settings');
  const cases = [
    ['7 天', '7', null], ['14 天', '14', null],
    ['30 天', 'custom', 30], ['90 天', 'custom', 90], ['180 天', 'custom', 180],
  ];
  for (const [label, preset, days] of cases) {
    await page.getByRole('button', { name: label, exact: true }).click();
    const saved = page.waitForResponse(response => response.url().endsWith('/api/lifecycle/settings')
      && response.request().method() === 'PUT');
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    expect((await saved).status()).toBe(200);
    await expect(page.getByRole('button', { name: '保存设置', exact: true })).toBeEnabled();
    const settings = (await (await page.request.get('/api/lifecycle/settings')).json()).settings;
    expect(settings.retention_preset).toBe(preset);
    if (days !== null) expect(settings.retention_custom_days).toBe(days);
    await page.reload();
    await expect(page.getByRole('button', { name: label, exact: true })).toHaveClass(/active/);
  }
  await page.getByRole('button', { name: '自定义', exact: true }).click();
  await page.getByLabel('保留天数').fill('0');
  await page.getByRole('button', { name: '保存设置', exact: true }).click();
  await expect(page.locator('.toast').filter({ hasText: '请输入 1 至 3650 之间的整数' })).toBeVisible();
  await page.getByLabel('保留天数').fill('45');
  await page.getByRole('button', { name: '保存设置', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存设置', exact: true })).toBeEnabled();
  expect((await (await page.request.get('/api/lifecycle/settings')).json()).settings)
    .toEqual({ retention_preset: 'custom', retention_custom_days: 45 });
  await page.reload();
  await expect(page.getByRole('button', { name: '自定义', exact: true })).toHaveClass(/active/);
  await expect(page.getByLabel('保留天数')).toHaveValue('45');
  await page.getByRole('button', { name: '永久保留', exact: true }).click();
  await page.getByRole('button', { name: '保存设置', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存设置', exact: true })).toBeEnabled();
  expect((await (await page.request.get('/api/lifecycle/settings')).json()).settings.retention_preset).toBe('forever');
});

test('审计日志保留本地日期、秒和时区，原始时间可查看', async ({ page }) => {
  const timestamps = ['2026-10-11T01:02:03Z', '2026-10-11T12:34:56Z'];
  await page.route('**/api/audit?*', route => route.fulfill({ json: { ok: true, items: timestamps.map((created_at, index) => ({
    id: `audit-${index}`, event_type: '配置变更', object_type: '智能体', actor: 'review', created_at,
  })) } }));
  await page.goto('/#/admin/settings');
  await page.getByRole('tab', { name: /审计日志/ }).click();
  const times = page.locator('.timeline time');
  await expect(times).toHaveCount(2);
  const texts = await times.allTextContents();
  expect(texts[0]).not.toBe(texts[1]);
  const localTimes = await page.evaluate(values => values.map(value => {
    const date = new Date(value);
    const pad = number => String(number).padStart(2, '0');
    const offset = -date.getTimezoneOffset();
    const minutes = Math.abs(offset) % 60;
    return {
      date: `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())}`,
      clock: `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`,
      zone: offset === 0 ? 'GMT' : `GMT${offset > 0 ? '+' : '-'}${Math.floor(Math.abs(offset) / 60)}${minutes ? ':' + pad(minutes) : ''}`,
    };
  }), timestamps);
  for (let index = 0; index < timestamps.length; index += 1) {
    await expect(times.nth(index)).toHaveAttribute('datetime', timestamps[index]);
    await expect(times.nth(index)).toHaveAttribute('title', timestamps[index]);
    expect(texts[index]).toContain(localTimes[index].date);
    expect(texts[index]).toContain(localTimes[index].clock);
    expect(texts[index]).toContain(localTimes[index].zone);
  }
});

test('Teams 用途不受自定义名称影响，历史通用连接不误归品牌', async ({ page }) => {
  const connectors = [{ id: 'legacy-webhook', name: '历史 Teams 通知', type: 'webhook', configured: true }];
  const payloads = [];
  await page.route('**/api/connectors', route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { ok: true, items: connectors } });
    const payload = route.request().postDataJSON();
    payloads.push(payload);
    const item = { ...payload, id: 'custom-teams', configured: true, status: 'configured' };
    connectors.push(item);
    return route.fulfill({ json: { ok: true, item } });
  });
  await page.goto('/#/admin/integrations');
  await expect(page.locator('.file-tile').filter({ hasText: '历史 Teams 通知' })).toContainText('通用 Webhook 连接器');
  const teams = page.locator('.file-tile').filter({ has: page.getByText('Teams', { exact: true }) });
  const wecom = page.locator('.file-tile').filter({ has: page.getByText('企业微信', { exact: true }) });
  await teams.getByRole('button', { name: '接入', exact: true }).click();
  let modal = page.getByRole('dialog');
  await modal.getByText('名称', { exact: false }).first().locator('..').locator('input').fill('销售分析群');
  await modal.getByText('Webhook 地址').locator('..').locator('input').fill('https://example.com/hook');
  await modal.getByRole('button', { name: '保存', exact: true }).click();
  await expect(modal).toHaveCount(0);
  expect(payloads[0]).toMatchObject({ channel: 'teams', type: 'webhook', name: '销售分析群' });
  await page.reload();
  await expect(teams.getByRole('button', { name: '查看配置', exact: true })).toBeVisible();
  await expect(wecom.getByRole('button', { name: '接入', exact: true })).toBeVisible();
  await teams.getByRole('button', { name: '查看配置', exact: true }).click();
  modal = page.getByRole('dialog', { name: '销售分析群 集成' });
  await expect(modal).toBeVisible();
});

test('stdio 参数空行忽略，清空后不传参数，其他参数原文保存', async ({ page }) => {
  await page.goto('/#/admin/mcp');
  for (const [name, text, expected] of [
    ['参数保真回归', 'server.py\n\n  keep spaces  \n--label=销售 分析\n \n', ['server.py', '  keep spaces  ', '--label=销售 分析']],
    ['无参数回归', '', []],
  ]) {
    await page.getByRole('button', { name: '添加 MCP 服务', exact: true }).click();
    const modal = page.getByRole('dialog');
    await modal.getByText('名称', { exact: false }).first().locator('..').locator('input').fill(name);
    await modal.locator('select').selectOption('stdio');
    await modal.getByText('执行命令', { exact: false }).locator('..').locator('input').fill('python3');
    const args = modal.getByText('命令参数（每行一个）', { exact: true }).locator('..').locator('textarea');
    await args.fill('先输入再清空');
    await args.fill(text);
    const saved = page.waitForResponse(response => response.url().endsWith('/api/mcp/servers')
      && response.request().method() === 'POST');
    await modal.getByRole('button', { name: '保存', exact: true }).click();
    const response = await saved;
    expect(response.status()).toBe(201);
    const item = (await response.json()).item;
    expect(item.args).toEqual(expected);
    await expect(modal).toHaveCount(0);
    const listed = (await (await page.request.get('/api/mcp/servers')).json()).items;
    expect(listed.find(row => row.id === item.id).args).toEqual(expected);
  }
});

test('MCP 与集成删除后可通过各自回收站入口恢复', async ({ page }) => {
  const suffix = Date.now();
  const mcpName = `可恢复页面 MCP ${suffix}`;
  const connectorName = `可恢复页面邮件 ${suffix}`;
  const created = await page.request.post('/api/mcp/servers', { data: { name: mcpName, transport: 'stdio', command: 'python3', args: [] } });
  expect(created.status()).toBe(201);
  const mcp = (await created.json()).item;
  await page.goto('/#/admin/mcp');
  await page.getByRole('article').filter({ has: page.getByRole('heading', { name: mcp.name, exact: true }) }).getByRole('button', { name: '删除', exact: true }).click();
  const confirmation = page.getByRole('dialog', { name: '删除 MCP 服务', exact: true });
  await confirmation.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(confirmation).toHaveCount(0);
  await page.locator('.view__inner .page-head').getByRole('button', { name: '回收站', exact: true }).click();
  await expect(page.getByRole('combobox', { name: '回收站内容类型' })).toHaveValue('mcp_servers');
  const row = page.locator('.view__inner article').filter({ has: page.getByText(mcp.name, { exact: true }) });
  await expect(row).toContainText('MCP 服务');
  await row.getByRole('button', { name: '恢复', exact: true }).click();
  await expect(row).toHaveCount(0);
  expect((await (await page.request.get('/api/mcp/servers')).json()).items.some(item => item.id === mcp.id)).toBe(true);

  const connector = await page.request.post('/api/connectors', { data: { name: connectorName, type: 'email', channel: 'email', host: 'smtp.example.com', recipient: 'review@example.com' } });
  expect(connector.status()).toBe(201);
  const connectorId = (await connector.json()).item.id;
  await page.goto('/#/admin/integrations');
  await page.locator('.file-tile').filter({ has: page.getByText('邮件', { exact: true }) }).getByRole('button', { name: '移除', exact: true }).click();
  const remove = page.getByRole('dialog', { name: '移除集成', exact: true });
  await remove.getByRole('button', { name: '确认移除', exact: true }).click();
  await expect(remove).toHaveCount(0);
  await page.locator('.view__inner .page-head').getByRole('button', { name: '回收站', exact: true }).click();
  await expect(page.getByRole('combobox', { name: '回收站内容类型' })).toHaveValue('connectors');
  const connectorRow = page.locator('.view__inner article').filter({ has: page.getByText(connectorName, { exact: true }) });
  await expect(connectorRow).toContainText('集成连接');
  await connectorRow.getByRole('button', { name: '恢复', exact: true }).click();
  await expect(connectorRow).toHaveCount(0);
  expect((await (await page.request.get('/api/connectors')).json()).items.some(item => item.id === connectorId)).toBe(true);
});

test('技能改问题或配置会失效旧报告，保存期间迟到测试不污染新版本', async ({ page }) => {
  const skillId = 'ui-admin-current-skill';
  const created = await page.request.post('/api/skills', { data: {
    id: skillId, name: '当前配置测试技能', description: '校验当前配置', category: '数据分析',
    instruction: '先核对业务口径，再查询数据。', triggers: ['库存'], allowed_tools: ['get_schema'],
  } });
  expect(created.status()).toBe(201);
  let releaseOldQuestion;
  let releaseOldConfig;
  const oldQuestion = new Promise(resolve => { releaseOldQuestion = resolve; });
  const oldConfig = new Promise(resolve => { releaseOldConfig = resolve; });
  let requests = 0;
  await page.route(`**/api/skills/${skillId}/test`, async route => {
    const number = ++requests;
    if (number === 1) await oldQuestion;
    if (number === 3) await oldConfig;
    await route.fulfill({ json: { ok: true,
      evaluation: { passed: true, checks: [] },
      resolution: { selected: [{ id: `selected-${number}`, name: `第${number}次命中结果` }] },
    } });
  });
  await page.goto('/#/admin/skills');
  await page.locator('.file-tile').filter({ has: page.getByText('当前配置测试技能', { exact: true }) })
    .getByRole('button', { name: '查看', exact: true }).click();
  const modal = page.getByRole('dialog', { name: '当前配置测试技能', exact: true });
  const question = modal.getByPlaceholder('输入一个测试问题');
  const testButton = modal.getByRole('button', { name: '校验配置', exact: true });
  await question.fill('旧问题');
  await testButton.click();
  await expect.poll(() => requests).toBe(1);
  await question.fill('新问题');
  await expect(testButton).toBeEnabled();
  await testButton.click();
  await expect(modal).toContainText('第2次命中结果');
  const firstResponse = page.waitForResponse(response => response.url().endsWith(`/api/skills/${skillId}/test`)
    && response.request().postDataJSON().question === '旧问题');
  releaseOldQuestion();
  await firstResponse;
  await expect(modal).toContainText('第2次命中结果');
  await expect(modal).not.toContainText('第1次命中结果');
  await modal.getByPlaceholder('一句话说明这个技能解决什么问题，Agent 靠它决定何时使用。').fill('更新后的技能说明');
  await expect(modal).not.toContainText('第2次命中结果');
  await expect(testButton).toBeDisabled();
  await modal.getByRole('button', { name: '保存', exact: true }).click();
  await expect(testButton).toBeEnabled();
  await testButton.click();
  await expect.poll(() => requests).toBe(3);
  await modal.getByRole('button', { name: '保存', exact: true }).click();
  await expect(testButton).toBeEnabled();
  const thirdResponse = page.waitForResponse(response => response.url().endsWith(`/api/skills/${skillId}/test`));
  releaseOldConfig();
  await thirdResponse;
  await expect(modal).not.toContainText('第3次命中结果');
  await testButton.click();
  await expect(modal).toContainText('第4次命中结果');
});

test('主要短表单与删除确认在桌面和窄窗口内比例可用', async ({ page }) => {
  for (const [width, height] of [[1366, 768], [1440, 900], [911, 512]]) {
    await page.setViewportSize({ width, height });
    for (const [route, entry, title] of [
      ['models', '添加模型服务', '添加模型服务'],
      ['mcp', '添加 MCP 服务', '添加 MCP 服务'],
      ['users', '添加用户', '添加用户'],
    ]) {
      await page.goto(`/#/admin/${route}`);
      await page.getByRole('button', { name: entry, exact: true }).click();
      const modal = page.getByRole('dialog', { name: title, exact: true });
      await expect(modal).toBeVisible();
      const bounds = await modal.boundingBox();
      expect(bounds.x).toBeGreaterThanOrEqual(0);
      expect(bounds.y).toBeGreaterThanOrEqual(0);
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
      expect(bounds.y + bounds.height).toBeLessThanOrEqual(height);
      await expect(modal.locator('.modal__foot button').last()).toBeVisible();
      await modal.getByRole('button', { name: '取消', exact: true }).click();
    }
  }
});

test('智能体九个配置区与技能五个配置区均能切换并访问字段', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.goto('/#/admin/agents');
  await page.getByRole('button', { name: '新建智能体' }).click();
  const modal = page.getByRole('dialog');
  for (const label of ['基本信息', '模型', '技能', '数据', '指标', '知识', 'MCP', '体验设置', '权限']) {
    await modal.locator('.builder__rail').getByRole('button', { name: label, exact: true }).click();
    await expect(modal.locator('.builder__rail .active')).toHaveText(label);
    await expect(modal.getByRole('button', { name: '保存', exact: true })).toBeVisible();
    expect(await modal.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  }
  await modal.getByRole('button', { name: '关闭', exact: true }).last().click();
  await page.goto('/#/admin/skills');
  await page.getByRole('button', { name: '查看', exact: true }).first().click();
  for (const label of ['基本信息', '触发与示例', '数据与知识', '输入输出', '高级']) {
    await modal.locator('.builder__rail').getByRole('button', { name: label, exact: true }).click();
    await expect(modal.locator('.builder__rail .active')).toHaveText(label);
    await expect(modal.getByRole('button', { name: '关闭', exact: true }).last()).toBeVisible();
    expect(await modal.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  }
  await modal.getByRole('button', { name: '关闭', exact: true }).last().click();
  await page.goto('/#/admin/settings');
  await page.getByRole('tab', { name: /审计日志/ }).click();
  await expect(page.locator('.view')).toContainText('审计日志');
});
