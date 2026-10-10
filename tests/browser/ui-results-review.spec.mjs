import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';

const pause = () => {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
};

async function workbench(page) {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
  await page.goto('/#/workbench');
  await expect(page.locator('.composer__input')).toBeVisible();
}

async function resultFixture(page, { failFirstPage = false, resultFailure = false, extraTable = false, partialTable = false } = {}) {
  const response = await page.request.post('/api/analyses', { data: {
    objective: '查看大量明细和中文长类目', source_ids: [],
  } });
  expect(response.status()).toBe(201);
  const run = (await response.json()).item;
  const categories = ['华东区域超长业务分类名称_AnnualRevenueComparison202610', '华北', '华南', '西南', '华中'];
  const chart = (id, type = 'bar') => ({ id, title: type === 'pie' ? '销售额构成' : '销售额按区域对比（长类目）', type,
    option: type === 'pie' ? { series: [{ type: 'pie', data: categories.map((name, index) => ({ name, value: (index + 1) * 50000 })) }] }
      : { xAxis: { type: 'category', data: categories }, yAxis: { type: 'value' },
        series: [{ name: '销售额（元）', type: 'bar', data: [-5000, 0, 150000, 300000, 420000] }] },
  });
  const rows = Array.from({ length: 125 }, (_, index) => ({
    '区域': index === 0 ? '华东超长名称连续标识符_AnnualRevenueComparison202610' : `区域 ${index + 1}`,
    '销售额（元）': index === 0 ? 0.0001 : index === 1 ? null : (index - 2) * 52340.75,
    '备注': index === 0 ? '这是较长中文说明，供检查结果展示换行和局部滚动是否正常。'.repeat(4) : '正常',
  }));
  const payload = {
    summary: '## 核心结论\n华东区域需要持续关注，其余区域保持增长。\n\n|口径|说明|\n|---|---|\n|分析范围|当前有权访问的数据|\n\n```sql\nSELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region ORDER BY revenue DESC;\n```',
    kpis: [{ id: 'small', label: '最小金额（元）', value: 0.0001, delta: -0.05 }, { id: 'zero', label: '零值', value: 0 },
      { id: 'neg', label: '退货金额（元）', value: -52340.75 }],
    charts: [chart('chart-0'), chart('chart-1', 'pie'), chart('chart-2')],
    tables: [{ id: 'detail', title: '区域销售明细', result_id: 'ui-review-detail', columns: ['区域', '销售额（元）', '备注'], total_rows: rows.length }],
    report: { recommendations: { short_term: ['关注华东经营效率'], medium_term: [], long_term: [] } },
    limitations: ['仅展示已核验数据，业务建议需人工复核'], evidence_refs: ['ui-review-source'], validation: { status: 'PASS' },
  };
  await page.route('**/api/analyses?*', route => route.fulfill({ json: {
    ok: true, items: [{ ...run, execution_status: 'partial', quality_status: 'passed', publication: { id: 'pub' } }],
  } }));
  if (extraTable) {
    payload.tables.push({ id: 'extra', title: '补充明细', result_id: 'ui-extra-table', columns: ['区域'], total_rows: 125 });
    await page.route('**/api/query-results/ui-extra-table', route => route.fulfill({ json: { ok: true, result: { data: [{ '区域': '补充预览' }], rows: 125, total_rows: 125 } } }));
  }
  let resultFailed = false;
  await page.route(`**/api/analyses/${run.id}/results`, route => {
    if (resultFailure && !resultFailed) {
      resultFailed = true;
      return route.fulfill({ status: 503, json: { ok: false, error: '结果服务暂时不可用' } });
    }
    return route.fulfill({ json: { ok: true, status: 'published', manifest: { payload }, artifacts: [] } });
  });
  let failed = false;
  await page.route(`**/api/analyses/${run.id}/details?*`, route => {
    const cursor = Number(new URL(route.request().url()).searchParams.get('cursor') || 0);
    if (failFirstPage && cursor === 50 && !failed) {
      failed = true;
      return route.fulfill({ status: 503, json: { ok: false, error: '明细服务暂时不可用' } });
    }
    return route.fulfill({ json: {
      ok: true, items: rows.slice(cursor, cursor + 50), columns: payload.tables[0].columns,
      total: partialTable ? 1000 : rows.length, completeness: partialTable ? 'partial' : 'complete', returned_total: rows.length, next_cursor: cursor + 50 < rows.length ? cursor + 50 : null,
    } });
  });
  await page.goto(`/#/conversation?id=${run.session_id}`);
  if (resultFailure) await expect(page.locator('[role=alert]')).toContainText('结果服务暂时不可用');
  else await expect(page.locator('.result')).toBeVisible();
  return { run, payload };
}

test('中文输入法确认不提交，技能候选替换当前 @查询', async ({ page }) => {
  await workbench(page);
  const input = page.locator('.composer__input');
  let submissions = 0;
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/analyses' && request.method() === 'POST') submissions += 1;
  });
  await input.fill('输入法确认候选词');
  await input.evaluate(element => {
    element.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 229, isComposing: true, bubbles: true }));
    element.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true }));
  });
  await expect(page).toHaveURL(/#\/workbench/);
  expect(submissions).toBe(0);
  await input.fill('@归');
  await page.getByRole('button', { name: /归因分析/ }).click();
  await expect(input).toHaveValue('@归因分析 ');
  await input.fill('本月销售额是多少？');
  await input.press('Shift+Enter');
  await expect(input).toHaveValue('本月销售额是多少？\n');
  await input.press('Enter');
  await expect(page).toHaveURL(/#\/conversation/);
  expect(submissions).toBe(1);
});

test('技能预判只匹配最新问题，清空后旧响应不恢复提示', async ({ page }) => {
  await workbench(page);
  const first = pause();
  let firstRequested = false;
  await page.route('**/api/skills/resolve', async route => {
    const question = route.request().postDataJSON().question;
    if (question.includes('旧问题')) {
      firstRequested = true;
      await first.promise;
    }
    await route.fulfill({ json: { selected: [{ id: question, name: question.includes('旧问题') ? '旧技能' : '新技能' }] } }).catch(() => {});
  });
  await page.locator('.composer__input').fill('这是旧问题');
  await expect.poll(() => firstRequested).toBe(true);
  await page.locator('.composer__input').fill('这是新问题');
  await expect(page.locator('.composer__bar')).toContainText('新技能');
  first.release();
  await expect(page.locator('.composer__bar')).not.toContainText('旧技能');
  await page.locator('.composer__input').fill('');
  await expect(page.locator('.composer__bar')).not.toContainText('将使用');
});

test('提交即时禁用并防重复，失败保留输入和附件', async ({ page }) => {
  await workbench(page);
  const response = pause();
  let submissions = 0;
  await page.route('**/api/analyses', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    submissions += 1;
    await response.promise;
    await route.fulfill({ status: 503, json: { ok: false, error: '分析服务暂时不可用' } });
  });
  await page.locator('.composer input[type="file"]').setInputFiles({ name: '备注.txt', mimeType: 'text/plain', buffer: Buffer.from('订单 12 笔') });
  await page.locator('.composer__input').fill('分析上传的订单');
  await page.locator('.composer__send').click();
  await expect(page.locator('.composer__send')).toBeDisabled();
  await expect(page.getByRole('button', { name: '移除 备注.txt' })).toBeDisabled();
  await expect.poll(() => submissions).toBe(1);
  response.release();
  await expect(page.locator('.toast')).toContainText('分析服务暂时不可用');
  await expect(page.locator('.composer__input')).toHaveValue('分析上传的订单');
  await expect(page.locator('.attachment-chip')).toContainText('备注.txt');
  await expect(page.locator('.composer__send')).toBeEnabled();
  expect(submissions).toBe(1);
});

test('部分结果、精确小数和建议分类清楚，更多图表和结果限制可访问', async ({ page }) => {
  await workbench(page);
  await resultFixture(page);
  await expect(page.locator('.execution__head')).toContainText('分析部分完成');
  await expect(page.locator('.execution__head')).not.toContainText('分析已完成并通过核验');
  await expect(page.locator('.kpi').first()).toContainText('0.0001');
  await expect(page.locator('.kpi').first()).toContainText('-5%');
  await expect(page.locator('.insight__label')).toHaveText('建议');
  await expect(page.locator('.chart-card')).toHaveCount(2);
  await page.getByRole('button', { name: '查看其余 1 张图表' }).click();
  await expect(page.locator('.chart-card')).toHaveCount(3);
  await page.locator('.result__limits summary').click();
  await expect(page.locator('.result__limits li')).toBeVisible();
  const data = await page.locator('.chart-card__canvas').first().evaluate(element => {
    const option = window.echarts.getInstanceByDom(element).getOption();
    return { data: option.series[0].data, categories: option.xAxis[0].data, interval: option.xAxis[0].axisLabel.interval, overflow: option.xAxis[0].axisLabel.overflow };
  });
  expect(data.data).toEqual([-5000, 0, 150000, 300000, 420000]);
  expect(data.categories).toHaveLength(5);
  expect(data.interval).toBe(0);
  expect(data.overflow).toBe('truncate');
});

test('明细分页失败可恢复，记录总数与当前范围对应正确', async ({ page }) => {
  await workbench(page);
  await resultFixture(page, { failFirstPage: true });
  const table = page.locator('.result-table');
  await expect(table).toContainText('共 125 条');
  await expect(table).toContainText('当前 1–50 条');
  await page.getByRole('button', { name: '区域销售明细下一页' }).click();
  await expect(table).toContainText('明细服务暂时不可用');
  await expect(table).toContainText('当前 1–50 条');
  await page.getByRole('button', { name: '重试加载', exact: true }).click();
  await expect(table).toContainText('当前 51–100 条');
  await expect(table.locator('tbody tr').first()).toContainText('区域 51');
  await page.getByRole('button', { name: '区域销售明细下一页' }).click();
  await expect(table).toContainText('当前 101–125 条');
  await expect(table.locator('tbody tr')).toHaveCount(25);
  await expect(page.getByRole('button', { name: '区域销售明细下一页' })).toBeDisabled();
  await page.getByRole('button', { name: '区域销售明细上一页' }).click();
  await expect(table).toContainText('当前 51–100 条');
});

test('成果生成防重复并反馈处理中，失败后恢复操作', async ({ page }) => {
  await workbench(page);
  const { run } = await resultFixture(page);
  const response = pause();
  let requests = 0;
  await page.route(`**/api/analyses/${run.id}/artifacts`, async route => {
    requests += 1;
    await response.promise;
    await route.fulfill({ status: 503, json: { ok: false, error: '报告生成暂时不可用' } });
  });
  await page.getByRole('button', { name: '生成报告', exact: true }).click();
  await expect(page.getByRole('button', { name: '生成中…', exact: true })).toBeDisabled();
  await expect.poll(() => requests).toBe(1);
  response.release();
  await expect(page.getByRole('button', { name: '生成报告', exact: true })).toBeEnabled();
  await expect(page.locator('.toast')).toContainText('报告生成暂时不可用');
  expect(requests).toBe(1);
});

test('桌面尺寸、窄窗口及125%与150%布局下结果局部滚动且图表重排', async ({ page }) => {
  await workbench(page);
  await resultFixture(page);
  for (const viewport of [{ width: 1366, height: 768 }, { width: 1440, height: 900 }, { width: 1920, height: 1080 }, { width: 1100, height: 760 }]) {
    await page.setViewportSize(viewport);
    for (const zoom of viewport.width === 1440 ? [1, 1.25, 1.5] : [1]) {
      await page.evaluate(value => { document.documentElement.style.zoom = value; }, zoom);
      await expect.poll(() => page.locator('.view').evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(2);
      const canvas = page.locator('.chart-card__canvas').first();
      await expect.poll(() => canvas.evaluate(element => Math.abs(window.echarts.getInstanceByDom(element).getWidth() - element.clientWidth))).toBeLessThanOrEqual(2);
      await expect(page.locator('.composer__send')).toBeVisible();
    }
  }
});

test('初次结果加载失败可重试，额外表格预览和部分返回明确说明', async ({ page }) => {
  await workbench(page);
  await resultFixture(page, { resultFailure: true, extraTable: true, partialTable: true });
  await page.getByRole('button', { name: '重试加载结果' }).click();
  await expect(page.locator('.result')).toBeVisible();
  await expect(page.locator('.result-table').first()).toContainText('本次返回 125 条，原始结果共 1000 条');
  await expect(page.locator('.result-table').last()).toContainText('当前为前 1 条预览');
  await expect(page.locator('.result-table').last()).toContainText('补充预览');
  await expect(page.locator('.result-table').last().getByRole('button', { name: /下一页/ })).toHaveCount(0);
});

test('澄清日期有可见校验，提交失败后可恢复并防止重复确认', async ({ page }) => {
  await workbench(page);
  const created = await page.request.post('/api/analyses', { data: { objective: '为什么华东销售下降？', source_ids: [] } });
  const run = (await created.json()).item;
  await page.goto(`/#/conversation?id=${run.session_id}`);
  await expect(page.locator('.clarify')).toBeVisible();
  await page.locator('.clarify').getByRole('button', { name: '自定义', exact: true }).click();
  const start = page.locator('.clarify').getByLabel('开始日期');
  const end = page.locator('.clarify').getByLabel('结束日期');
  await expect(page.locator('.clarify').getByRole('button', { name: '开始分析', exact: true })).toBeDisabled();
  await start.fill('2026-10-10');
  await end.fill('2026-10-01');
  await expect(page.locator('.clarify')).toContainText('结束日期不能早于开始日期');
  await end.fill('2026-10-11');
  const response = pause();
  let requests = 0;
  await page.route(`**/api/analyses/${run.id}/contract/confirm`, async route => {
    requests += 1;
    await response.promise;
    await route.fulfill({ status: 503, json: { ok: false, error: '确认服务暂时不可用' } });
  });
  await page.locator('.clarify').getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.locator('.clarify').getByRole('button', { name: '正在提交…' })).toBeDisabled();
  await expect(start).toBeDisabled();
  await expect.poll(() => requests).toBe(1);
  response.release();
  await expect(page.locator('.toast')).toContainText('确认服务暂时不可用');
  await expect(start).toHaveValue('2026-10-10');
  await expect(page.locator('.clarify').getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
  expect(requests).toBe(1);
});

test('来源 SQL 局部滚动且可复制原文', async ({ page, context }) => {
  await workbench(page);
  const { run } = await resultFixture(page);
  const sql = 'SELECT ' + Array.from({ length: 40 }, (_, index) => `SUM(revenue_${index}) AS long_column_identifier_${index}`).join(',\n') + '\nFROM sales GROUP BY region;';
  await page.route(`**/api/analyses/${run.id}/execution`, route => route.fulfill({ json: { ok: true, item: {
    actions: [{ tool_id: 'query_data', arguments: { sql } }],
  } } }));
  await page.route(`**/api/analyses/${run.id}/validations`, route => route.fulfill({ json: { ok: true, items: [] } }));
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.getByRole('button', { name: '查看来源', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: '查看来源', exact: true });
  await expect(drawer).toBeVisible();
  await expect(drawer.locator('pre')).toHaveText(sql);
  expect(await drawer.locator('pre').evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
  await drawer.getByRole('button', { name: '复制执行的 SQL', exact: true }).click();
  await expect(page.locator('.toast')).toContainText('执行的 SQL已复制');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(sql);
});

test('空结果和全零构成说明明确，多类目缩放和热力图保留零值负数及空值含义', async ({ page }) => {
  await workbench(page);
  const serverHeatmap = JSON.parse(execFileSync('python3', ['-c', `
import json
import pandas as pd
from backend.services.charts import make_spec
frame = pd.DataFrame({'行': ['A', 'A', 'B'], '列': ['X', 'Y', 'X'], '值': [-5, 0, None]})
print(json.dumps(make_spec(frame, chart_type='heatmap', x='行', y='值'), ensure_ascii=False))
`], { encoding: 'utf8' }));
  const options = await page.evaluate(async heatmap => {
    const { ChartCard, buildOption, formatValue } = await import('/src/components/chart.js');
    const { ResultView } = await import('/src/components/result-blocks.js');
    const many = { type: 'bar', title: '多类目结果', option: {
      xAxis: { type: 'category', data: Array.from({ length: 30 }, (_, index) => `类目 ${index + 1}`) },
      yAxis: { type: 'value' }, series: [{ name: '数值', type: 'bar', data: Array.from({ length: 30 }, (_, index) => index - 15) }],
    } };
    const host = document.createElement('div');
    host.style.width = '700px';
    host.id = 'result-empty-test';
    document.body.append(host);
    Vue.createApp({ render: () => Vue.h('div', [
      Vue.h(ResultView, { payload: {} }),
      Vue.h(ChartCard, { spec: { type: 'pie', title: '全零构成', encoding: { x: ['A', 'B'], series: [{ values: [0, 0] }] } } }),
      Vue.h(ChartCard, { spec: many }), Vue.h(ChartCard, { spec: heatmap }),
    ]) }).mount(host);
    const manyOption = buildOption(many);
    const heatOption = buildOption(heatmap);
    return {
      categories: manyOption.xAxis.data.length, values: manyOption.series[0].data,
      zoom: manyOption.dataZoom[0].endValue, minimum: heatOption.visualMap.min,
      heatValues: heatOption.series[0].data.map(point => point[2]), missing: formatValue(null), small: formatValue(0.0001),
    };
  }, serverHeatmap);
  await expect(page.locator('#result-empty-test')).toContainText('本次分析没有可展示的结论或数据');
  await expect(page.locator('#result-empty-test')).toContainText('各类目数值均为 0，暂无构成比例');
  await expect(page.locator('#result-empty-test .chart-card__canvas')).toHaveCount(2);
  expect(options.categories).toBe(30);
  expect(options.values).toHaveLength(30);
  expect(options.zoom).toBe(11);
  expect(options.minimum).toBe(-5);
  expect(options.heatValues).toEqual([-5, 0]);
  expect(options.missing).toBe('—');
  expect(options.small).toBe('0.0001');
});

test('服务端多序列图标题、图例与缩放条各占独立空间，缺失值图表不声称全零', async ({ page }) => {
  await workbench(page);
  const serverSpec = JSON.parse(execFileSync('python3', ['-c', `
import json
import pandas as pd
from backend.services.charts import make_spec
frame = pd.DataFrame({'region': [f'区域 {i}' for i in range(20)], 'amount': list(range(20)), 'target': list(range(20, 40))})
print(json.dumps(make_spec(frame, chart_type='grouped_bar', title='区域销售比较', x='region', y=['amount', 'target']), ensure_ascii=False))
`], { encoding: 'utf8' }));
  const bounds = await page.evaluate(async spec => {
    const { ChartCard } = await import('/src/components/chart.js');
    const host = document.createElement('div');
    host.id = 'chart-layout-regression'; host.style.width = '700px'; document.body.append(host);
    Vue.createApp({ render: () => Vue.h('div', [
      Vue.h(ChartCard, { spec }),
      ...['pie', 'boxplot', 'gauge'].map(type => Vue.h(ChartCard, { spec: {
        type, title: '缺失值 ' + type, encoding: { x: ['A'], series: [{ values: [null] }] },
      } })),
    ]) }).mount(host);
    await Vue.nextTick(); await Vue.nextTick();
    const chart = window.echarts.getInstanceByDom(host.querySelector('.chart-card__canvas'));
    const rectangle = type => {
      const model = chart.getModel().getComponent(type);
      const group = chart.getViewOfComponentModel(model).group;
      const rect = group.getBoundingRect().clone(); rect.applyTransform(group.getComputedTransform());
      return { top: rect.y, bottom: rect.y + rect.height };
    };
    return { title: rectangle('title'), legend: rectangle('legend'), zoom: rectangle('dataZoom'), height: chart.getHeight() };
  }, serverSpec);
  expect(bounds.title.bottom).toBeLessThan(bounds.legend.top);
  expect(bounds.legend.bottom).toBeLessThan(bounds.zoom.top);
  expect(bounds.zoom.bottom).toBeLessThanOrEqual(bounds.height);
  await expect(page.locator('#chart-layout-regression .chart-card__canvas')).toHaveCount(1);
  await expect(page.locator('#chart-layout-regression')).not.toContainText('各类目数值均为 0');
  await expect(page.locator('#chart-layout-regression .empty')).toHaveCount(3);
});
