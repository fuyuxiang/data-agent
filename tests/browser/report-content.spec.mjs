import { expect, test } from '@playwright/test';

for (const width of [1280, 390]) {
  test(`报告内容、指标解释与多表分页在 ${width}px 可用`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const created = await page.request.post('/api/analyses', { data: {
      objective: '比较已选数据并生成报告', source_ids: [],
    } });
    const run = (await created.json()).item;
    const payload = {
      summary: '当前数值合计为 0，分组差异需要结合业务口径核对。',
      kpis: [{ id: 'total', label: '合计', value: 0, unit: '单位', definition: '全部已选记录的数值合计',
        formula: 'SUM(amount)', definition_status: 'exploratory' }],
      report: {
        findings: [{ type: 'fact', text: 'B 组数值为 15。', confidence: 'high', confidence_reason: '直接核对完整查询结果' },
          { type: 'inference', text: '差异可能与记录构成有关。', confidence: 'low', confidence_reason: '尚无独立影响因素证据' }],
        actions: [{ text: '复核负值记录', owner: '数据负责人', scope: '当前数据范围' }],
      },
      glossary: [{ name: '分组数值', result_id: 'groups', column: 'amount', unit: '单位',
        definition: '同组记录的数值合计', formula: 'SUM(amount)', definition_status: 'exploratory' }],
      data_quality: { summary: '查询完整性已核验；业务准确性尚未独立评估' },
      tables: [{ id: 'total-table', result_id: 'total-result', title: '整体统计', columns: ['amount'], total_rows: 1,
        server_paginated: true },
      { id: 'group-table', result_id: 'groups', title: '分组明细', columns: ['group', 'amount'], total_rows: 125,
        server_paginated: true }],
      evidence_refs: ['total-result', 'groups'], limitations: ['因果解释尚未验证'],
    };
    const artifacts = [];
    await page.route('**/api/analyses?*', route => route.fulfill({ json: { ok: true,
      items: [{ ...run, execution_status: 'finished', quality_status: 'passed', publication: { id: 'published' } }],
    } }));
    await page.route(`**/api/analyses/${run.id}/results`, route => route.fulfill({ json: {
      ok: true, status: 'published', manifest: { payload }, artifacts,
    } }));
    let pagedSecond = false;
    await page.route(`**/api/analyses/${run.id}/details?*`, route => {
      const params = new URL(route.request().url()).searchParams;
      const second = params.get('result_id') === 'groups';
      const cursor = Number(params.get('cursor') || 0);
      pagedSecond ||= second && cursor === 50;
      return route.fulfill({ json: { ok: true,
        items: second ? Array.from({ length: Math.min(50, 125 - cursor) }, (_, index) => ({
          group: `分组 ${cursor + index + 1}`, amount: cursor + index,
        })) : [{ amount: 0 }],
        columns: second ? ['group', 'amount'] : ['amount'],
        total: second ? 125 : 1, returned_total: second ? 125 : 1, completeness: 'complete',
      } });
    });
    let generated = false;
    await page.route(`**/api/analyses/${run.id}/artifacts`, route => {
      expect(route.request().postDataJSON().kinds).toEqual(['report_html']);
      generated = true;
      artifacts.push({ id: 'html-report', kind: 'report_html', title: '分组分析网页报告',
        filename: 'report.html', previewable: true, size: 100, download_url: '/api/artifacts/html-report/download' });
      return route.fulfill({ json: { ok: true, items: artifacts } });
    });
    await page.goto(`/#/conversation?id=${run.session_id}`);
    await expect(page.locator('.result')).toBeVisible();
    await expect(page.locator('.kpi__value')).toHaveText('0');
    await page.getByText('口径说明', { exact: true }).click();
    await expect(page.locator('.kpi')).toContainText('全部已选记录的数值合计');
    await expect(page.locator('.insight')).toHaveCount(3);
    await expect(page.locator('.insight').nth(1)).toContainText('推测');
    await expect(page.locator('.insight').nth(1)).toContainText('置信度低');
    await page.locator('.insight').nth(1).getByText('查看解释与依据').click();
    await expect(page.locator('.insight').nth(1)).toContainText('尚无独立影响因素证据');
    await page.getByText('指标口径与数据质量', { exact: true }).click();
    await expect(page.locator('.result__limits').last()).toContainText('业务准确性尚未独立评估');
    await expect(page.locator('.report-glossary')).toContainText('同组记录的数值合计');
    await page.getByRole('button', { name: '分组明细下一页' }).click();
    await expect.poll(() => pagedSecond).toBe(true);
    await expect(page.locator('.result-table').nth(1)).toContainText('分组 51');
    await page.getByRole('button', { name: '网页报告', exact: true }).click();
    await expect.poll(() => generated).toBe(true);
    await expect(page.locator('.file-card')).toContainText('分组分析网页报告');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: `/private/tmp/dataagent-report-ui-${width}.png`, fullPage: true });
  });
}

test('技能方法参考可在管理后台编辑并随版本保存', async ({ page }) => {
  const created = await page.request.post('/api/skills', { data: {
    id: 'reference-ui-fixture', name: '通用方法参考测试', description: '查看与编辑通用方法参考',
    instruction: '按需读取 methods.md 中的章节，再查询真实数据。',
    triggers: ['方法参考'], example_questions: ['按照方法参考分析数据'], allowed_tools: ['query_data'],
    reference_documents: { 'methods.md': '## 对比方法\n先确认基期与单位。' },
  } });
  expect(created.status()).toBe(201);
  await page.goto('/#/admin/skills');
  await page.locator('article').filter({ hasText: '通用方法参考测试' }).getByRole('button', { name: '查看', exact: true }).click();
  await page.getByRole('button', { name: '方法参考', exact: true }).click();
  const content = page.getByRole('textbox', { name: 'methods.md参考内容' });
  await expect(content).toHaveValue('## 对比方法\n先确认基期与单位。');
  await content.fill('## 对比方法\n先确认基期与单位。\n分母为零时说明不可比较。');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get('/api/skills/reference-ui-fixture')).json())
    .item.reference_documents['methods.md']).toContain('分母为零');
});

test('口径确认使用通用关注项，不预设业务维度或时间', async ({ page }) => {
  const created = await page.request.post('/api/analyses', { data: { objective: '比较已选数据中的分组差异', source_ids: [] } });
  const run = (await created.json()).item;
  let confirmed;
  await page.route(`**/api/analyses/${run.id}/contract/confirm`, route => {
    confirmed = route.request().postDataJSON().contract;
    return route.fulfill({ json: { ok: true, item: { ...run, execution_status: 'queued' } } });
  });
  await page.goto(`/#/conversation?id=${run.session_id}`);
  await expect(page.locator('.clarify')).toContainText('未限定时间');
  await expect(page.getByRole('button', { name: '城市差异', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '品类表现', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '分组差异', exact: true }).click();
  await page.getByRole('button', { name: '查看口径', exact: true }).click();
  await expect(page.locator('.clarify .definition')).toContainText('图表与数据');
  await expect(page.locator('.clarify .definition')).not.toContainText('source_scope');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect.poll(() => confirmed?.dimensions).toEqual(['整体']);
  expect(confirmed.confirmed_assumptions).toContain('重点关注：分组差异');
  expect(confirmed.time_range).toEqual({});
});
