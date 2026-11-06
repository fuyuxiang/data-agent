import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  expect((await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } })).ok()).toBe(true);
});

test('接入和删除数据后无须刷新即可在工作台使用正确范围', async ({ page }) => {
  const filename = `review-state-${Date.now()}`;
  await page.goto('/#/admin/data');
  await expect(page.locator('.skeleton')).toHaveCount(0);
  await page.locator('.page-head input[type=file]').setInputFiles({
    name: `${filename}.csv`, mimeType: 'text/csv', buffer: Buffer.from('region,amount\nEast,10\nWest,20\n'),
  });
  const tile = page.locator('article').filter({ hasText: filename });
  await expect(tile).toBeVisible();
  await page.locator('.sidebar .main-nav').getByRole('button', { name: '工作台', exact: true }).click();
  await page.locator('.composer__scope[title="选择本次分析的数据范围"]').click();
  const scope = page.getByRole('dialog', { name: '选择数据范围' });
  await expect(scope).toContainText(filename);
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '管理后台', exact: true }).click();
  await page.locator('.admin__nav').getByRole('button', { name: '数据', exact: true }).click();
  await tile.getByRole('button', { name: '删除', exact: true }).click();
  await expect(tile).toHaveCount(0);
  await page.locator('.sidebar .main-nav').getByRole('button', { name: '工作台', exact: true }).click();
  await page.locator('.composer__scope[title="选择本次分析的数据范围"]').click();
  await expect(scope).not.toContainText(filename);
  const sources = (await (await page.request.get('/api/sources')).json()).items;
  expect(sources.some(item => item.name === filename)).toBe(false);
});

test('修改指标定义后清除旧试算，使用新名称重新查询', async ({ page }) => {
  const model = (await (await page.request.get('/api/semantic/models')).json()).items[0];
  const name = `review_metric_${Date.now()}`;
  const created = await page.request.post('/api/semantic/metrics', { data: {
    name, label: '审查指标', model_id: model.id, measure: model.measures[0].name, status: 'approved',
  } });
  expect(created.ok()).toBe(true);
  const metric = (await created.json()).item;
  await page.goto('/#/admin/metrics');
  await page.getByPlaceholder('搜索指标名称、口径或同义词').fill(name);
  await page.locator('.metric-item').click();
  const aside = page.locator('.metric-layout > aside');
  await aside.getByRole('button', { name: '运行试算', exact: true }).click();
  await expect(aside.locator('tbody tr')).not.toHaveCount(0);
  await aside.getByRole('button', { name: '编辑', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '编辑指标' });
  await dialog.getByLabel(/技术名称/).fill(`${name}_changed`);
  await dialog.getByRole('button', { name: '保存为草稿', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(aside.locator('tbody tr')).toHaveCount(0);
  const request = page.waitForRequest(r => new URL(r.url()).pathname === '/api/admin/metric-trial');
  await aside.getByRole('button', { name: '运行试算', exact: true }).click();
  expect((await request).postDataJSON().metric).toBe(`${name}_changed`);
  await expect(aside.locator('tbody tr')).not.toHaveCount(0);
  expect((await (await page.request.get('/api/semantic/metrics')).json()).items.find(item => item.id === metric.id).name)
    .toBe(`${name}_changed`);
  expect((await page.request.delete(`/api/semantic/metrics/${metric.id}`)).ok()).toBe(true);
});

test('未配置分析服务时提供可执行的试算入口，按角色展示配置入口', async ({ page }) => {
  for (const role of ['owner', 'editor', 'analyst', 'viewer']) {
    await page.route('**/api/bootstrap*', async route => {
      const response = await route.fetch();
      const json = await response.json();
      await route.fulfill({ response, json: { ...json, providers: [], active_membership: { role } } });
    });
    await page.goto('/#/workbench');
    await page.reload();
    if (role === 'viewer') {
      await expect(page.locator('.composer__input')).toBeDisabled();
      await expect(page.locator('.workbench__notice')).toHaveCount(0);
    } else {
      await expect(page.locator('.workbench__notice')).toContainText('自主分析尚未启用');
      await expect(page.getByRole('button', { name: '先试算指标', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '启用自主分析', exact: true })).toHaveCount(role === 'owner' ? 1 : 0);
      await page.getByRole('button', { name: '先试算指标', exact: true }).click();
      await expect(page).toHaveURL(/#\/metrics/);
    }
    await page.unroute('**/api/bootstrap*');
  }
});

test('指标试算使用业务表头和定义格式，原值仍可核对', async ({ page }) => {
  await page.goto('/#/metrics');
  await page.locator('.metric-item').filter({ has: page.locator('b', { hasText: /^销售额$/ }) }).click();
  await page.locator('.checkbox').filter({ hasText: '区域' }).getByRole('checkbox').check();
  await page.getByRole('button', { name: '运行试算', exact: true }).click();
  const table = page.locator('.metric-layout > aside table');
  await expect(table.locator('th').last()).toHaveText('销售额（元）');
  const value = table.locator('tbody tr').first().locator('td').last();
  const raw = Number(await value.getAttribute('title'));
  expect(await value.innerText()).toBe(new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(raw));
  await page.getByText('查看 SQL 与口径', { exact: true }).click();
  await expect(page.locator('.metric-layout > aside pre')).toContainText('SELECT');
});

test('指标目录、图表与长标签在两种桌面和缩放后完整显示', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const size of [{ width: 1440, height: 900 }, { width: 1280, height: 720 }]) {
    await page.setViewportSize(size);
    await page.goto('/#/metrics');
    await page.locator('.metric-item').filter({ has: page.locator('b', { hasText: /^销售额$/ }) }).click();
    await page.locator('.checkbox').filter({ hasText: '区域' }).getByRole('checkbox').check();
    await page.getByRole('button', { name: '运行试算', exact: true }).click();
    await expect(page.locator('.chart-card canvas')).toBeVisible();
    for (const collapsed of [false, true]) {
      if (collapsed) await page.getByRole('button', { name: '折叠导航', exact: true }).click();
      for (const zoom of [1, 1.25]) {
        await page.evaluate(value => { document.documentElement.style.zoom = value; }, zoom);
        await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
        await expect.poll(() => page.locator('.chart-card__canvas').evaluate(el =>
          Math.abs(window.echarts.getInstanceByDom(el).getWidth() - el.clientWidth))).toBeLessThanOrEqual(2);
      }
      await page.evaluate(() => { document.documentElement.style.zoom = 1; });
    }
    await page.getByRole('button', { name: '展开导航', exact: true }).click();
  }
  expect(errors).toEqual([]);
});

test('技能发布和停用同步到提问候选，刷新后保持一致', async ({ page }) => {
  const id = `review-skill-${Date.now()}`;
  const name = `审查技能${Date.now()}`;
  expect((await page.request.post('/api/skills', { data: {
    id, name, description: '核查技能发布和停用状态', instruction: '读取有权访问的数据并核对结果。',
    triggers: ['核查技能'], example_questions: ['核查技能'], allowed_tools: ['query_data'],
  } })).ok()).toBe(true);
  await page.goto('/#/workbench');
  const draftPreview = page.waitForResponse(response => response.url().endsWith('/api/skills/resolve'));
  await page.locator('.composer__input').fill(`@${name}`);
  expect((await (await draftPreview).json()).selected.some(item => item.id === id)).toBe(false);
  await expect(page.locator('.composer__resolution').filter({ hasText: name })).toHaveCount(0);
  await expect(page.locator('.command__item').filter({ hasText: name })).toHaveCount(0);
  await page.getByRole('button', { name: '管理后台', exact: true }).click();
  await page.locator('.admin__nav').getByRole('button', { name: '技能', exact: true }).click();
  const tile = page.locator('article').filter({ hasText: name });
  await tile.getByRole('button', { name: '查看', exact: true }).click();
  let editor = page.getByRole('dialog', { name });
  await editor.locator('.modal__foot').getByRole('button', { name: '发布', exact: true }).click();
  await expect(tile).toContainText('已发布');
  await editor.locator('.modal__head').getByRole('button', { name: '关闭', exact: true }).click();
  await page.locator('.sidebar .main-nav').getByRole('button', { name: '工作台', exact: true }).click();
  await page.locator('.composer__input').fill(`@${name}`);
  await expect(page.locator('.command__item').filter({ hasText: name })).toBeVisible();
  await page.getByRole('button', { name: '管理后台', exact: true }).click();
  await page.locator('.admin__nav').getByRole('button', { name: '技能', exact: true }).click();
  await tile.getByRole('button', { name: '查看', exact: true }).click();
  editor = page.getByRole('dialog', { name });
  await editor.locator('.modal__foot').getByRole('button', { name: '停用', exact: true }).click();
  await expect(tile).toContainText('已停用');
  await editor.locator('.modal__head').getByRole('button', { name: '关闭', exact: true }).click();
  expect((await (await page.request.get(`/api/skills/${id}`)).json()).item.status).toBe('disabled');
  await page.locator('.sidebar .main-nav').getByRole('button', { name: '工作台', exact: true }).click();
  await page.locator('.composer__input').fill(`@${name}`);
  await expect(page.locator('.command__item').filter({ hasText: name })).toHaveCount(0);
  await page.reload();
  await page.locator('.composer__input').fill(`@${name}`);
  await expect(page.locator('.command__item').filter({ hasText: name })).toHaveCount(0);
});

test('用户回收站与其他业务页面使用一致的留白', async ({ page }) => {
  await page.goto('/#/trash');
  const head = page.locator('.page-head');
  const main = page.locator('.main');
  await expect(head).toBeVisible();
  expect((await head.boundingBox()).x - (await main.boundingBox()).x).toBeGreaterThanOrEqual(16);
});

test('发布期间弹窗关闭状态明确，完成后可正常关闭', async ({ page }) => {
  const name = `发布状态审查${Date.now()}`;
  const source = (await (await page.request.get('/api/sources')).json()).items[0];
  const created = await page.request.post('/api/agents', { data: { name, source_ids: [source.id], skill_ids: [] } });
  expect(created.ok()).toBe(true);
  const agent = (await created.json()).item;
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route(`**/api/agents/${agent.id}/publish`, async route => {
    await pending;
    await route.continue();
  });
  await page.goto('/#/admin/agents');
  await page.locator('.file-tile').filter({ hasText: name }).getByRole('button', { name: '编辑', exact: true }).click();
  const modal = page.getByRole('dialog', { name });
  await modal.locator('.modal__foot').getByRole('button', { name: '发布', exact: true }).click();
  const close = modal.locator('.modal__head').getByRole('button', { name: '关闭', exact: true });
  await expect(close).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(modal).toBeVisible();
  release();
  await expect(close).toBeEnabled();
  await close.click();
  await expect(modal).toHaveCount(0);
  expect((await (await page.request.get('/api/agents')).json()).items.find(item => item.id === agent.id).status).toBe('published');
});

test('保存和删除模型配置即时更新工作台提示，不将配置视为连接测试', async ({ page }) => {
  const name = `配置状态审查${Date.now()}`;
  await page.goto('/#/admin/models');
  await page.getByRole('button', { name: '添加模型服务', exact: true }).click();
  const editor = page.getByRole('dialog', { name: '添加模型服务' });
  await editor.getByPlaceholder('生产模型').fill(name);
  // This checks configuration persistence, without depending on external DNS or calling a model.
  await editor.getByLabel('接口地址', { exact: true }).fill('https://1.1.1.1/v1');
  await editor.getByPlaceholder('gpt-4.1-mini').fill('isolated-test-model');
  await editor.getByLabel('API Key', { exact: true }).fill('isolated-test-key-not-for-external-use');
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  await expect(editor).toHaveCount(0);
  const card = page.locator('article').filter({ hasText: name });
  await expect(card).toContainText('已配置');
  await expect(card).not.toContainText('连接成功');
  await page.locator('.sidebar .main-nav').getByRole('button', { name: '工作台', exact: true }).click();
  await expect(page.locator('.workbench__notice')).toHaveCount(0);
  await page.getByRole('button', { name: '管理后台', exact: true }).click();
  await page.locator('.admin__nav').getByRole('button', { name: '模型', exact: true }).click();
  await card.getByRole('button', { name: '删除', exact: true }).click();
  await page.getByRole('dialog', { name: '删除模型服务' }).getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(card).toHaveCount(0);
  await page.locator('.sidebar .main-nav').getByRole('button', { name: '工作台', exact: true }).click();
  await expect(page.locator('.workbench__notice')).toContainText('自主分析尚未启用');
});

test('后台完整读取超过一页的数据源，工作台不丢失第 101 个来源', async ({ page }) => {
  const sources = Array.from({ length: 101 }, (_, index) => ({
    id: `review-paged-${String(index).padStart(3, '0')}`, name: `分页来源 ${index}`,
    kind: 'file', status: 'ready', tables: [],
  }));
  const cursors = [];
  await page.route(/\/api\/sources(?:\?.*)?$/, route => {
    const cursor = new URL(route.request().url()).searchParams.get('cursor') || '';
    cursors.push(cursor);
    return route.fulfill({ json: { ok: true,
      items: cursor ? sources.slice(100) : sources.slice(0, 100),
      next_cursor: cursor ? null : sources[99].id,
    } });
  });
  await page.goto('/#/admin/data');
  await expect(page.locator('article.file-tile')).toHaveCount(101);
  expect(cursors).toEqual(['', sources[99].id]);
  await page.locator('.sidebar .main-nav').getByRole('button', { name: '工作台', exact: true }).click();
  await page.locator('.composer__scope[title="选择本次分析的数据范围"]').click();
  const scope = page.getByRole('dialog', { name: '选择数据范围' });
  await expect(scope.getByRole('checkbox')).toHaveCount(101);
  await expect(scope).toContainText(sources[100].name);
  expect(await page.evaluate(async () => (await import('/src/store.js')).state.sources.length)).toBe(101);
});

test('权限过滤后的空页继续翻页，续页失败保留范围并可重试', async ({ page }) => {
  await page.goto('/#/workbench');
  await expect(page.locator('.composer__input')).toBeVisible();
  const before = await page.evaluate(async () => (await import('/src/store.js')).state.sources.map(item => item.id));
  const sources = [{ id: 'review-visible-next', name: '第二页可见来源', kind: 'file', status: 'ready', tables: [] }];
  let failing = true;
  await page.route(/\/api\/sources(?:\?.*)?$/, route => {
    const cursor = new URL(route.request().url()).searchParams.get('cursor');
    if (!cursor) return route.fulfill({ json: { ok: true, items: [], next_cursor: 'hidden-page' } });
    if (failing) return route.fulfill({ status: 500, json: { ok: false, error: '续页暂不可用' } });
    return route.fulfill({ json: { ok: true, items: sources, next_cursor: null } });
  });
  await page.getByRole('button', { name: '管理后台', exact: true }).click();
  await page.locator('.admin__nav').getByRole('button', { name: '数据', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('续页暂不可用');
  expect(await page.evaluate(async () => (await import('/src/store.js')).state.sources.map(item => item.id))).toEqual(before);
  failing = false;
  await page.getByRole('button', { name: '重新加载数据源', exact: true }).click();
  await expect(page.locator('article.file-tile')).toHaveCount(1);
  await expect(page.locator('article.file-tile')).toContainText('第二页可见来源');
});

test('延迟返回的数据源加载不会覆盖更新后的范围或其他空间', async ({ page }) => {
  await page.goto('/#/workbench');
  await expect(page.locator('.composer__input')).toBeVisible();
  let release;
  let gate;
  let started = 0;
  await page.route(/\/api\/sources(?:\?.*)?$/, async route => {
    started += 1;
    await gate;
    await route.fulfill({ json: { ok: true, items: [{ id: 'stale-source', name: '过期来源' }] } });
  });
  for (const change of ['delete', 'workspace']) {
    gate = new Promise(resolve => { release = resolve; });
    await page.evaluate(async () => {
      const { actions, state } = await import('/src/store.js');
      state.sources = [{ id: 'stale-source', name: '旧来源' }];
      window.reviewSourceRefresh = actions.refreshSources();
    });
    await expect.poll(() => started).toBe(change === 'delete' ? 1 : 2);
    await page.evaluate(async mode => {
      const { actions, state } = await import('/src/store.js');
      if (mode === 'delete') actions.forgetSource('stale-source');
      else { state.workspaceId = 'another-workspace'; state.sources = [{ id: 'current-source' }]; }
    }, change);
    release();
    expect(await page.evaluate(() => window.reviewSourceRefresh)).toBeNull();
    expect(await page.evaluate(async () => (await import('/src/store.js')).state.sources.map(item => item.id)))
      .toEqual(change === 'delete' ? [] : ['current-source']);
  }
});

test('停用技能保留未保存字段，失败可重试，保存后可重新发布', async ({ page }) => {
  const id = `review-preserve-${Date.now()}`;
  const name = '停用保留修改审查';
  expect((await page.request.post('/api/skills', { data: {
    id, name, description: '原始描述', instruction: '读取授权数据。',
    triggers: ['停用保留修改审查'], allowed_tools: ['query_data'],
  } })).ok()).toBe(true);
  expect((await page.request.post(`/api/skills/${id}/publish`)).ok()).toBe(true);
  let failing = true;
  await page.route(`**/api/skills/${id}`, route => {
    if (route.request().method() === 'PATCH' && route.request().postDataJSON().status === 'disabled' && failing) {
      return route.fulfill({ status: 500, json: { ok: false, error: '停用暂时失败' } });
    }
    return route.continue();
  });
  await page.goto('/#/admin/skills');
  await page.locator('article').filter({ hasText: name }).getByRole('button', { name: '查看', exact: true }).click();
  const editor = page.locator('section[role=dialog]');
  await editor.getByLabel(/^名称/).fill('已保留的新名称');
  await editor.getByLabel(/^描述/).fill('已保留的新描述');
  await editor.locator('.modal__foot').getByRole('button', { name: '停用', exact: true }).click();
  await expect(page.locator('.toast').filter({ hasText: '停用暂时失败' })).toBeVisible();
  await expect(editor.getByLabel(/^名称/)).toHaveValue('已保留的新名称');
  failing = false;
  await editor.locator('.modal__foot').getByRole('button', { name: '停用', exact: true }).click();
  await expect(editor.locator('.modal__foot').getByRole('button', { name: '发布', exact: true })).toBeEnabled();
  await expect(editor.getByLabel(/^名称/)).toHaveValue('已保留的新名称');
  await expect(editor.getByLabel(/^描述/)).toHaveValue('已保留的新描述');
  await expect(editor.getByRole('status')).toContainText('有未保存的修改');
  const disabled = (await (await page.request.get(`/api/skills/${id}`)).json()).item;
  expect(disabled.status).toBe('disabled');
  expect(disabled.name).toBe(name);
  await expect(editor.getByRole('button', { name: '校验配置', exact: true })).toBeDisabled();
  await editor.locator('.modal__foot').getByRole('button', { name: '保存', exact: true }).click();
  await expect(editor.getByRole('status')).toHaveCount(0);
  await expect(editor.getByRole('button', { name: '校验配置', exact: true })).toBeEnabled();
  await editor.locator('.modal__foot').getByRole('button', { name: '发布', exact: true }).click();
  await expect(editor.locator('.modal__foot').getByRole('button', { name: '停用', exact: true })).toBeEnabled();
  const published = (await (await page.request.get(`/api/skills/${id}`)).json()).item;
  expect(published.status).toBe('published');
  expect(published.name).toBe('已保留的新名称');
  expect(published.description).toBe('已保留的新描述');
});

test('百分比指标在表格、坐标与实际图表提示中使用同一格式', async ({ page }) => {
  await page.goto('/#/metrics');
  await page.locator('.metric-item').filter({ has: page.locator('b', { hasText: /^折扣率$/ }) }).click();
  await page.locator('.checkbox').filter({ hasText: '区域' }).getByRole('checkbox').check();
  await page.getByRole('button', { name: '运行试算', exact: true }).click();
  const cell = page.locator('.metric-layout > aside tbody tr').first().locator('td').last();
  await expect(cell).toBeVisible();
  const raw = await cell.getAttribute('title');
  const formatted = await cell.innerText();
  expect(formatted).toMatch(/^\d+\.\d{2}%$/);
  const chart = page.locator('.chart-card__canvas');
  await expect.poll(() => chart.evaluate((el, value) => {
    const option = window.echarts.getInstanceByDom(el).getOption();
    return [option.yAxis[0].axisLabel.formatter(Number(value)), option.tooltip[0].valueFormatter(value)];
  }, raw)).toEqual([formatted, formatted]);
  await chart.evaluate(el => window.echarts.getInstanceByDom(el).dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex: 0 }));
  await expect(chart).toContainText(formatted);
});

test('高精度金额在表格和实际图表提示中保留分位，零值与缺失值不混淆', async ({ page }) => {
  const model = (await (await page.request.get('/api/semantic/models')).json()).items
    .find(item => item.name === '销售月度事实模型');
  expect(model).toBeTruthy();
  const name = `review_decimal_${Date.now()}`;
  const response = await page.request.post('/api/semantic/metrics', { data: {
    name, label: '精确金额', model_id: model.id, measure: model.measures[0].name,
    format: ',.2f', unit: '元', status: 'approved',
  } });
  expect(response.ok()).toBe(true);
  const metric = (await response.json()).item;
  await page.route('**/api/admin/metric-trial', route => route.fulfill({ json: {
    ok: true, result: { columns: ['区域', name], data: [
      { 区域: '大额', [name]: '9999999999999999.99' },
      { 区域: '空值', [name]: null }, { 区域: '零值', [name]: '0' },
      { 区域: '负数', [name]: '-1.005' },
    ] }, plan: { sql: 'SELECT decimal_amount' },
  } }));
  await page.goto('/#/metrics');
  await page.getByPlaceholder('搜索指标名称、口径或同义词').fill(name);
  await page.locator('.metric-item').click();
  await page.locator('.checkbox').filter({ hasText: '区域' }).getByRole('checkbox').check();
  await page.getByRole('button', { name: '运行试算', exact: true }).click();
  const table = page.locator('.metric-layout > aside table');
  await expect(table.locator('th').last()).toHaveText('精确金额（元）');
  await expect(table.locator('tbody tr td:last-child')).toHaveText(['9,999,999,999,999,999.99', '—', '0.00', '-1.01']);
  await expect(table.locator('tbody tr td:last-child').first()).toHaveAttribute('title', '9999999999999999.99');
  const chart = page.locator('.chart-card__canvas');
  await expect.poll(() => chart.evaluate(el => window.echarts.getInstanceByDom(el).getOption().series[0].data[0]))
    .toBe('9999999999999999.99');
  await chart.evaluate(el => window.echarts.getInstanceByDom(el).dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex: 0 }));
  await expect(chart).toContainText('9,999,999,999,999,999.99');
  expect((await page.request.delete(`/api/semantic/metrics/${metric.id}`)).ok()).toBe(true);
});
