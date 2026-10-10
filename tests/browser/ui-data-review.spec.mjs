import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';

test.beforeEach(async ({ page }) => {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
});

async function isolatedMetricCatalog(page) {
  const model = { id: 'ui-review-model', name: '回归测试语义模型', enabled: true, version: 1,
    source_id: 'ui-review-source', table: 'review_table', dimensions: [
      { name: 'review_region', label: '区域' }, { name: 'review_channel', label: '渠道' },
    ], measures: [{ name: 'review_amount', label: '金额' }] };
  const metrics = ['甲', '乙'].map((label, index) => ({ id: `ui-review-metric-${index}`, model_id: model.id,
    name: `ui_review_amount_${index}`, label: `回归销售额${label}`, status: 'approved', metric_type: 'atomic',
    unit: '元', measure: 'review_amount', aliases: [], description: '浏览器隔离测试数据' }));
  await page.route('**/api/semantic/metrics', route => route.fulfill({ json: { ok: true, items: metrics } }));
  await page.route('**/api/semantic/models', route => route.fulfill({ json: { ok: true, items: [model] } }));
}

test('智能体搜索保留分组，企业分组不混入官方智能体', async ({ page }) => {
  await page.goto('/#/agents');
  await expect(page.locator('.agent-card')).not.toHaveCount(0);
  await page.getByPlaceholder('搜索智能体').fill('不可能匹配的智能体名称');
  await expect(page.locator('.segmented button')).toHaveCount(3);
  await expect(page.locator('.view__inner')).toContainText('没有匹配的智能体');
  await page.getByPlaceholder('搜索智能体').fill('');
  await page.locator('.segmented button').filter({ hasText: '企业' }).click();
  await expect(page.locator('.agent-card .badge--brand')).toHaveCount(0);
});

test('数据预览切表忽略旧响应，重新加载保留当前表，失败可重试', async ({ page }) => {
  const source = { id: 'src_preview_review', name: '预览切表测试', kind: 'file', status: 'ready',
    tables: [{ name: 'a', rows: 12, columns: 1 }, { name: 'b', rows: 19, columns: 1 }] };
  await page.route('**/api/sources', route => route.fulfill({ json: { ok: true, items: [source] } }));
  let releaseFirst;
  const first = new Promise(resolve => { releaseFirst = resolve; });
  const requests = [];
  let bRequests = 0;
  await page.route('**/api/sources/src_preview_review/preview*', async route => {
    const table = new URL(route.request().url()).searchParams.get('table');
    requests.push(table);
    if (table === 'a') await first;
    if (table === 'b' && ++bRequests === 1) {
      await route.fulfill({ status: 500, json: { ok: false, error: '预览暂时不可用' } });
      return;
    }
    await route.fulfill({ json: { ok: true, preview: { columns: ['当前表'], data: [{ 当前表: table }] } } });
  });
  await page.goto('/#/admin/data');
  await page.getByRole('button', { name: '查看', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: source.name });
  await drawer.getByRole('tab', { name: '预览', exact: true }).click();
  await drawer.getByRole('button', { name: '加载预览', exact: true }).click();
  await expect.poll(() => requests).toContain('a');
  await drawer.getByLabel('预览数据表').selectOption('b');
  await expect(drawer).toContainText('预览暂时不可用');
  await drawer.getByRole('button', { name: '加载预览', exact: true }).click();
  await expect(drawer.locator('tbody td')).toHaveText('b');
  releaseFirst();
  await page.waitForTimeout(150);
  await expect(drawer.locator('tbody td')).toHaveText('b');
  await drawer.getByRole('button', { name: '重新加载预览', exact: true }).click();
  await expect.poll(() => requests.filter(item => item === 'b').length).toBe(3);
  await expect(drawer).toContainText('b · 展示前 1 行');
});

test('指标试算切换和改条件清除旧结果，失败可恢复', async ({ page }) => {
  await isolatedMetricCatalog(page);
  let releaseFirst;
  const first = new Promise(resolve => { releaseFirst = resolve; });
  const requests = [];
  await page.route('**/api/admin/metric-trial', async route => {
    const body = route.request().postDataJSON();
    requests.push(body);
    const requestNumber = requests.length;
    if (requestNumber === 1) await first;
    if (requestNumber === 2) {
      await route.fulfill({ status: 500, json: { ok: false, error: '试算暂时失败' } });
      return;
    }
    await route.fulfill({ json: { ok: true, result: { columns: [body.metric], data: [{ [body.metric]: requestNumber === 1 ? 111 : 222 }] }, plan: { sql: 'SELECT 222', model: { name: '测试模型', version: 1 }, metric: { label: body.metric, version: 1 } } } });
  });
  await page.goto('/#/metrics');
  const cards = page.locator('.metric-item');
  await cards.first().click();
  await page.getByRole('button', { name: '运行试算', exact: true }).click();
  await expect.poll(() => requests.length).toBe(1);
  await cards.nth(1).click();
  await page.getByRole('button', { name: '运行试算', exact: true }).click();
  await expect(page.locator('.metric-layout > aside')).toContainText('试算暂时失败');
  await page.getByRole('button', { name: '运行试算', exact: true }).click();
  await expect(page.locator('.metric-layout > aside tbody td')).toHaveText('222');
  releaseFirst();
  await page.waitForTimeout(150);
  await expect(page.locator('.metric-layout > aside tbody td')).toHaveText('222');
  await page.locator('.metric-layout > aside input[type=number]').fill('100');
  await expect(page.locator('.metric-layout > aside tbody td')).toHaveCount(0);
  await page.locator('.metric-layout > aside input[type=number]').fill('0');
  await page.getByRole('button', { name: '运行试算', exact: true }).click();
  await expect(page.locator('.metric-layout > aside')).toContainText('行数上限应为 1 到 5000 的整数');
  expect(requests.length).toBe(3);
});

test('知识保存阻止重复提交，检索输入变化不显示旧问题结果，中文输入法不误提交', async ({ page }) => {
  let writes = 0;
  let releaseWrite;
  const pendingWrite = new Promise(resolve => { releaseWrite = resolve; });
  await page.route('**/api/knowledge/entries', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    writes += 1;
    await pendingWrite;
    await route.fulfill({ json: { ok: true, item: { ...route.request().postDataJSON(), id: 'review-entry' } } });
  });
  await page.goto('/#/admin/knowledge');
  await page.getByRole('button', { name: '新增', exact: true }).first().click();
  const editor = page.getByRole('dialog', { name: '新增知识条目' });
  await expect(editor.getByRole('button', { name: '保存', exact: true })).toBeDisabled();
  await editor.getByPlaceholder('销售额口径').fill('防重复知识条目');
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  await expect(editor.getByRole('button', { name: '保存中…', exact: true })).toBeDisabled();
  expect(writes).toBe(1);
  releaseWrite();
  await expect(editor).toHaveCount(0);
  await page.getByRole('tab', { name: '检索测试', exact: true }).click();
  let releaseSearch;
  const pendingSearch = new Promise(resolve => { releaseSearch = resolve; });
  let searches = 0;
  await page.route('**/api/knowledge/search', async route => {
    searches += 1;
    await pendingSearch;
    await route.fulfill({ json: { ok: true, items: [{ document_name: '旧问题片段', text: '旧响应不应显示', score: 0.85 }] } });
  });
  const input = page.getByPlaceholder('例如：GMV 的计算口径是什么？');
  await input.fill('销售额');
  await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true });
  expect(searches).toBe(0);
  await page.getByRole('button', { name: '检索', exact: true }).click();
  await expect.poll(() => searches).toBe(1);
  await input.fill('退款金额');
  releaseSearch();
  await page.waitForTimeout(150);
  await expect(page.locator('.view__inner')).not.toContainText('旧响应不应显示');
});

test('资料库重命名只提交一次，菜单点外关闭，分类计数与删除同步', async ({ page }) => {
  await page.goto('/#/library');
  await page.locator('input[type=file]').setInputFiles({ name: 'ui-review-file.txt', mimeType: 'text/plain', buffer: Buffer.from('UI review') });
  const tile = page.locator('.file-tile').filter({ hasText: 'ui-review-file' }).first();
  await expect(tile).toBeVisible();
  await expect(page.locator('.chip-group').getByRole('button', { name: /^全部\s*\d+$/ })).toHaveCount(1);
  await expect(tile.locator('.row--between').getByRole('button', { name: '更多', exact: true })).toBeVisible();
  await tile.getByRole('button', { name: '更多', exact: true }).click();
  await page.getByRole('heading', { name: '资料库', exact: true }).click();
  await expect(tile.locator('.dropdown__menu')).toHaveCount(0);
  await tile.getByRole('button', { name: '更多', exact: true }).click();
  await tile.getByRole('button', { name: '重命名', exact: true }).click();
  const modal = page.getByRole('dialog', { name: '重命名', exact: true });
  let writes = 0;
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/library/*', async route => {
    if (route.request().method() !== 'PATCH') return route.continue();
    writes += 1;
    await pending;
    await route.continue();
  });
  await modal.locator('input').fill('已重命名的资料');
  await modal.getByRole('button', { name: '保存', exact: true }).click();
  await expect(modal.getByRole('button', { name: '保存中…', exact: true })).toBeDisabled();
  expect(writes).toBe(1);
  release();
  await expect(modal).toHaveCount(0);
  const renamed = page.locator('.file-tile').filter({ hasText: '已重命名的资料' });
  await renamed.getByRole('button', { name: '更多', exact: true }).click();
  await renamed.getByRole('button', { name: '删除', exact: true }).click();
  await expect(renamed).toHaveCount(0);
  const count = await page.locator('.file-tile').count();
  await expect(page.locator('.chip-group .chip').first()).toHaveText(`全部 ${count}`);
});

test('资料库 Markdown 预览显示标题和表格，保留零和负数并清理脚本', async ({ page }) => {
  await page.goto('/#/library');
  await page.locator('input[type=file]').setInputFiles({ name: 'ui-review-preview.md', mimeType: 'text/markdown', buffer: Buffer.from('# 经营复盘\n\n|区域|销售额|\n|---|---:|\n|华东|0|\n|华北|-12.30|\n\n<script>window.previewUnsafe=true</script>') });
  await page.locator('.file-tile').filter({ hasText: 'ui-review-preview' }).getByRole('button', { name: '预览', exact: true }).click();
  const modal = page.getByRole('dialog', { name: 'ui-review-preview', exact: true });
  await expect(modal.getByRole('heading', { name: '经营复盘', exact: true })).toBeVisible();
  await expect(modal.locator('tbody td').nth(1)).toHaveText('0');
  await expect(modal.locator('tbody td').nth(3)).toHaveText('-12.30');
  await expect(modal.locator('script')).toHaveCount(0);
  expect(await page.evaluate(() => window.previewUnsafe)).toBeUndefined();
});

test('CSV 资料在线表格预览保留重复表头、引号、多行文本和精度，不触发下载', async ({ page }) => {
  const downloads = [];
  page.on('download', item => downloads.push(item.suggestedFilename()));
  await page.goto('/#/library');
  await page.locator('input[type=file]').setInputFiles({ name: 'ui-review-table.csv', mimeType: 'text/csv', buffer: Buffer.from('\uFEFF区域,金额,备注,金额\r\n华东,0,"门店,直营",1.00000001\r\n华北,-12.30,"多行\n说明含""引号""",9007199254740993\r\n') });
  await page.locator('.file-tile').filter({ hasText: 'ui-review-table' }).getByRole('button', { name: '预览', exact: true }).click();
  const modal = page.getByRole('dialog', { name: 'ui-review-table', exact: true });
  await expect(modal).toContainText('共 2 行数据');
  await expect(modal.locator('thead th')).toHaveText(['区域', '金额', '备注', '金额']);
  await expect(modal.locator('tbody tr').first().locator('td')).toHaveText(['华东', '0', '门店,直营', '1.00000001']);
  await expect(modal.locator('tbody tr').last().locator('td').last()).toHaveText('9007199254740993');
  await expect(modal.locator('tbody tr').last().locator('td').nth(2)).toHaveText('多行\n说明含"引号"');
  expect(downloads).toEqual([]);
});

for (const [name, content, expected] of [
  ['quoted-empty-middle', '金额\n1\n""\n3\n', ['1', '', '3']],
  ['quoted-empty-eof', '金额\r\n1\r\n""', ['1', '']],
  ['trailing-physical-lines', '金额\n1\n\n\n', ['1']],
  ['quoted-multiline', '备注\n"第一行\n第二行"\n""\n', ['第一行\n第二行', '']],
]) {
  test(`单列CSV保留显式空记录，末尾换行不制造记录：${name}`, async ({ page }) => {
    await page.goto('/#/library');
    await page.locator('input[type=file]').setInputFiles({ name: `${name}.csv`, mimeType: 'text/csv', buffer: Buffer.from(content) });
    await page.locator('.file-tile').filter({ hasText: name }).getByRole('button', { name: '预览', exact: true }).click();
    const modal = page.getByRole('dialog', { name, exact: true });
    await expect(modal).toContainText(`共 ${expected.length} 行数据`);
    await expect(modal.locator('tbody tr')).toHaveCount(expected.length);
    await expect(modal.locator('tbody td')).toHaveText(expected);
  });
}

test('JSON 资料预览保留大整数和小数原文，不把业务ok字段作为API错误', async ({ page }) => {
  const content = '{"ok":false,"large":9007199254740993,"decimal":0.000000000000001,"zero":0,"missing":null}';
  await page.goto('/#/library');
  await page.locator('input[type=file]').setInputFiles({ name: 'ui-review-exact.json', mimeType: 'application/json', buffer: Buffer.from(content) });
  await page.locator('.file-tile').filter({ hasText: 'ui-review-exact' }).getByRole('button', { name: '预览', exact: true }).click();
  const modal = page.getByRole('dialog', { name: 'ui-review-exact', exact: true });
  await expect(modal.locator('pre')).toHaveText(content);
  await expect(modal.getByRole('alert')).toHaveCount(0);
});

test('资料库大图片等比适配弹窗，预览请求失败后可重新加载', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/#/library');
  await page.locator('input[type=file]').setInputFiles({ name: 'ui-review-large-logo.png', mimeType: 'image/png',
    buffer: await readFile(new URL('../../frontend/src/assets/logo-shuqing.png', import.meta.url)) });
  let failures = 1;
  await page.route('**/api/library/*/preview*', async route => {
    if (failures-- > 0) await route.fulfill({ status: 503, json: { ok: false, error: '图片服务暂时不可用' } });
    else await route.continue();
  });
  await page.locator('.file-tile').filter({ hasText: 'ui-review-large-logo' }).getByRole('button', { name: '预览', exact: true }).click();
  const modal = page.getByRole('dialog', { name: 'ui-review-large-logo', exact: true });
  await expect(modal.getByRole('alert')).toHaveText('图片服务暂时不可用');
  await expect(modal.locator('.library-image-preview img')).toHaveCount(0);
  await modal.getByRole('button', { name: '重新加载预览', exact: true }).click();
  const image = modal.getByRole('img', { name: 'ui-review-large-logo', exact: true });
  await expect(image).toBeVisible();
  await expect(modal.getByRole('alert')).toHaveCount(0);
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 700 }]) {
    await page.setViewportSize(viewport);
    const dimensions = await image.evaluate(node => {
      const imageBox = node.getBoundingClientRect();
      const container = node.parentElement;
      const containerBox = container.getBoundingClientRect();
      return { width: imageBox.width, height: imageBox.height, naturalWidth: node.naturalWidth,
        naturalHeight: node.naturalHeight, complete: node.complete,
        overflowX: container.scrollWidth - container.clientWidth,
        overflowY: container.scrollHeight - container.clientHeight,
        contained: imageBox.left >= containerBox.left && imageBox.right <= containerBox.right + 1
          && imageBox.top >= containerBox.top && imageBox.bottom <= containerBox.bottom + 1 };
    });
    expect(dimensions.complete).toBe(true);
    expect(dimensions.naturalWidth).toBe(1024);
    expect(dimensions.naturalHeight).toBe(1024);
    expect(dimensions.width).toBeLessThan(dimensions.naturalWidth);
    expect(dimensions.height).toBeLessThan(dimensions.naturalHeight);
    expect(dimensions.width / dimensions.height).toBeCloseTo(1, 2);
    expect(dimensions.overflowX).toBeLessThanOrEqual(1);
    expect(dimensions.overflowY).toBeLessThanOrEqual(1);
    expect(dimensions.contained).toBe(true);
    await expect.poll(() => modal.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  }
});

test('指标多维试算图区分组合类别，空值保留且说明图表的20组范围', async ({ page }) => {
  await isolatedMetricCatalog(page);
  await page.route('**/api/admin/metric-trial', async route => {
    const body = route.request().postDataJSON();
    const data = Array.from({ length: 28 }, (_, index) => ({
      [body.group_by[0]]: `区域${index % 3}`, [body.group_by[1]]: `渠道${index}`, [body.metric]: index === 0 ? null : index === 1 ? -1.25 : index === 2 ? 0 : index,
    }));
    await route.fulfill({ json: { ok: true, result: { columns: [...body.group_by, body.metric], data }, plan: { sql: 'SELECT ...', model: { name: '测试模型', version: 1 }, metric: { label: body.metric, version: 1 } } } });
  });
  await page.goto('/#/metrics');
  await page.locator('.metric-item').filter({ has: page.getByText('回归销售额甲', { exact: true }) }).click();
  const aside = page.locator('.metric-layout > aside');
  await aside.locator('input[type=checkbox]').first().check();
  await aside.locator('input[type=checkbox]').last().check();
  await page.getByRole('button', { name: '运行试算', exact: true }).click();
  await expect(aside).toContainText('返回 28 行');
  await expect(aside).toContainText('图表展示前 20 组');
  await expect(aside.locator('.chart-card__canvas canvas')).toBeVisible();
  const option = await aside.locator('.chart-card__canvas').evaluate(node => window.echarts.getInstanceByDom(node).getOption());
  expect(option.xAxis[0].data[0]).toBe('区域0 / 渠道0');
  expect(option.series[0].data.slice(0, 3)).toEqual([null, -1.25, 0]);
  const bounds = await aside.locator('.chart-card__canvas').evaluate(node => {
    const chart = window.echarts.getInstanceByDom(node);
    const rect = type => {
      const group = chart.getViewOfComponentModel(chart.getModel().getComponent(type)).group;
      const value = group.getBoundingRect().clone(); value.applyTransform(group.getComputedTransform());
      return { top: value.y, bottom: value.y + value.height };
    };
    return { legend: rect('legend'), zoom: rect('dataZoom'), height: chart.getHeight() };
  });
  expect(bounds.legend.bottom).toBeLessThan(bounds.zoom.top);
  expect(bounds.zoom.bottom).toBeLessThanOrEqual(bounds.height);
});

test('语义模型切换数据源忽略旧结构响应，读取前不允许添加空字段', async ({ page }) => {
  const sources = [{ id: 'model-source-a', name: '数据A', status: 'ready', tables: [] }, { id: 'model-source-b', name: '数据B', status: 'ready', tables: [] }];
  await page.route('**/api/sources', route => route.fulfill({ json: { ok: true, items: sources } }));
  let releaseFirst;
  const first = new Promise(resolve => { releaseFirst = resolve; });
  await page.route('**/api/sources/model-source-*/schema', async route => {
    const suffix = route.request().url().includes('model-source-a') ? 'a' : 'b';
    if (suffix === 'a') await first;
    await route.fulfill({ json: { ok: true, schema: { tables: [{ name: `table_${suffix}`, columns: [{ name: `value_${suffix}` }] }] } } });
  });
  await page.goto('/#/admin/metrics');
  await page.getByRole('tab', { name: /语义模型/ }).click();
  await page.getByRole('button', { name: '新建语义模型', exact: true }).click();
  const modal = page.getByRole('dialog', { name: '新建语义模型' });
  await expect(modal.getByRole('button', { name: '添加度量', exact: true })).toBeDisabled();
  await modal.locator('.model-basics select').first().selectOption('model-source-b');
  await expect(modal.locator('.model-basics select').nth(1)).toHaveValue('table_b');
  releaseFirst();
  await page.waitForTimeout(150);
  await modal.getByRole('button', { name: '添加度量', exact: true }).click();
  await expect(modal.getByLabel('度量技术名称')).toHaveValue('value_b');
  await expect(modal.getByLabel('度量数据字段')).toHaveValue('value_b');
});

test('只读成员的资料和指标仍可查看，写入和试算入口按权限隐藏', async ({ page }) => {
  await page.route('**/api/bootstrap*', async route => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ json: { ...body, active_membership: { ...body.active_membership, role: 'viewer' } } });
  });
  await page.goto('/#/library');
  await expect(page.locator('input[type=file]')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '下载', exact: true })).toHaveCount(0);
  await page.goto('/#/metrics');
  await page.locator('.metric-item').first().click();
  await expect(page.locator('.metric-layout > aside')).toContainText('当前为只读权限');
  await expect(page.getByRole('button', { name: '运行试算', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '编辑', exact: true })).toHaveCount(0);
});

test('指标停用请求期间禁用重复操作并恢复状态', async ({ page }) => {
  await isolatedMetricCatalog(page);
  let writes = 0;
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/semantic/metrics/*', async route => {
    if (route.request().method() !== 'PATCH') return route.continue();
    writes += 1;
    await pending;
    await route.fulfill({ json: { ok: true, item: { status: 'deprecated' } } });
  });
  await page.goto('/#/metrics');
  await page.locator('.metric-item').filter({ has: page.getByText('回归销售额甲', { exact: true }) }).click();
  await page.getByRole('button', { name: '停用', exact: true }).click();
  await expect(page.getByRole('button', { name: '停用中…', exact: true })).toBeDisabled();
  expect(writes).toBe(1);
  release();
  await expect(page.locator('.metric-layout > aside .card').first()).toContainText('已停用');
  await expect(page.getByRole('button', { name: '停用中…', exact: true })).toHaveCount(0);
});

test('关闭再打开相同数据源忽略上次的迟到结构响应', async ({ page }) => {
  const source = { id: 'schema-reopen-review', name: '结构重开测试', kind: 'file', status: 'ready', tables: [] };
  await page.route('**/api/sources', route => route.fulfill({ json: { ok: true, items: [source] } }));
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  let requests = 0;
  await page.route('**/api/sources/schema-reopen-review/schema', async route => {
    const number = ++requests;
    if (number === 1) await pending;
    await route.fulfill({ json: { ok: true, schema: { tables: [{ name: number === 1 ? '过期数据表' : '最新数据表', columns: [{ name: 'id', type: 'integer' }] }] } } });
  });
  await page.goto('/#/admin/data');
  await page.getByRole('button', { name: '查看', exact: true }).click();
  await page.getByRole('tab', { name: '结构', exact: true }).click();
  await expect.poll(() => requests).toBe(1);
  await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '查看', exact: true }).click();
  await page.getByRole('tab', { name: '结构', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('最新数据表');
  release();
  await page.waitForTimeout(150);
  await expect(page.getByRole('dialog')).not.toContainText('过期数据表');
});

for (const entry of [
  { route: 'metrics', endpoint: '**/api/semantic/metrics', retry: '重新加载指标中心', content: '销售额' },
  { route: 'admin/data', endpoint: '**/api/sources', retry: '重新加载数据源', content: '即时零售月度销售样例' },
  { route: 'admin/knowledge', endpoint: '**/api/knowledge/entries', retry: '重新加载知识', content: '销售额口径' },
]) {
  test(`${entry.route}加载失败明确显示错误并可原位重试`, async ({ page }) => {
    let failures = 1;
    await page.route(entry.endpoint, async route => {
      if (failures-- > 0) await route.fulfill({ status: 503, json: { ok: false, error: '服务暂时不可用' } });
      else await route.continue();
    });
    await page.goto(`/#/${entry.route}`);
    await expect(page.getByRole('alert')).toContainText('服务暂时不可用');
    await expect(page.locator('.view__inner .empty')).toHaveCount(0);
    await page.getByRole('button', { name: entry.retry, exact: true }).click();
    await expect(page.locator('.view__inner')).toContainText(entry.content);
    await expect(page.getByRole('alert')).toHaveCount(0);
  });
}

for (const viewport of [{ width: 1366, height: 768 }, { width: 1440, height: 900 }, { width: 1920, height: 1080 }, { width: 1024, height: 700 }]) {
  test(`资料/指标/数据/知识/回收站布局 ${viewport.width}×${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    for (const route of ['agents', 'library', 'metrics', 'admin/metrics', 'admin/data', 'admin/knowledge', 'admin/trash']) {
      await page.goto(`/#/${route}`);
      await expect(page.locator('.page-head__title')).toBeVisible();
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
      if (route.endsWith('metrics')) {
        await page.locator('.metric-item').first().click();
        await page.getByRole('button', { name: '添加', exact: true }).click();
        await expect(page.locator('.metric-layout > aside .card').last()).toBeVisible();
        await expect.poll(() => page.locator('.metric-layout > aside').evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
      }
      if (route === 'admin/metrics') {
        await page.getByRole('tab', { name: /语义模型/ }).click();
        await page.getByRole('button', { name: '新建语义模型', exact: true }).click();
        await page.getByRole('button', { name: '添加度量', exact: true }).click();
        const modal = page.getByRole('dialog', { name: '新建语义模型' });
        const box = await modal.boundingBox();
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.y).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
        expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
        await expect.poll(() => modal.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
        await modal.getByRole('button', { name: '关闭', exact: true }).click();
      }
    }
  });
}
