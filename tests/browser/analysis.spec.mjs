import { expect, test } from '@playwright/test';


test('creates an analysis contract and manages a real indexed attachment', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('今天要从数据中确认什么？')).toBeVisible();

  const composer = page.getByPlaceholder('描述分析问题；Enter 发送，Shift+Enter 换行');
  await composer.fill('核对区域销售额及口径');
  await composer.press('Enter');
  await expect(page.getByRole('heading', { name: '确认一次，随后自主执行' })).toBeVisible();

  const coverage = page.locator('.contract-grid label').filter({ hasText: '统计覆盖范围' }).locator('textarea');
  await coverage.fill('已选授权来源的全部完整记录');
  await page.locator('input[type=file]').setInputFiles({
    name: 'definition.md', mimeType: 'text/markdown', buffer: Buffer.from('# 指标口径\n销售额按区域汇总。'),
  });
  await expect(page.getByText('definition.md')).toBeVisible();
  await page.getByTitle('移除').click();
  await expect(page.getByText('definition.md')).toHaveCount(0);
});


test('opens grounded evidence and downloads a published artifact', async ({ page }) => {
  const run = {
    id: 'run-ui', session_id: 'local-default', workspace_id: 'default', version: 4,
    created_at: '2026-09-06T08:00:00+00:00', execution_status: 'finished',
    outcome: 'complete', quality_status: 'passed', source_scope: [],
    contract: {
      version: 1, confirmed_at: '2026-09-06T08:00:01+00:00',
      payload: { objective: '核对区域销售额', coverage: '全部数据', dimensions: ['区域'], deliverables: ['summary'] },
    },
  };
  const manifest = {
    summary: '北区销售额高于南区，结果已通过独立验证。',
    kpis: [1, 2, 3, 4].map(id => ({ id: `k${id}`, label: `指标 ${id}`, value: id * 10 })),
    charts: [1, 2, 3, 4].map(id => ({ id: `c${id}`, title: `图表 ${id}`, available: false, unavailable_reason: '此浏览器样例不渲染数据' })),
    limitations: ['仅适用于已确认范围'],
    report: { problem_and_definitions: {}, data_results: '已核对', attribution: [], limitations: [] },
  };

  await page.route(/\/api\/analyses(?:\/[^?]*)?(?:\?.*)?$/, async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/analyses' && request.method() === 'GET') {
      return route.fulfill({ json: { items: [run] } });
    }
    if (path === '/api/analyses/run-ui/events') {
      return route.fulfill({ json: { items: [], next_cursor: 0 } });
    }
    if (path === '/api/analyses/run-ui/attachments') {
      return route.fulfill({ json: { items: [] } });
    }
    if (path === '/api/analyses/run-ui/results') {
      return route.fulfill({ json: {
        status: 'published', manifest: { payload: manifest },
        artifacts: [{ id: 'artifact-ui', filename: 'summary.docx', download_url: '/api/artifacts/artifact-ui/download' }],
      } });
    }
    if (path === '/api/analyses/run-ui/evidence') {
      return route.fulfill({ json: { claims: [{ id: 'claim-1', text: '北区销售额', evidence_refs: ['dataset-ref-1'] }] } });
    }
    if (path === '/api/analyses/run-ui') return route.fulfill({ json: { item: run } });
    return route.continue();
  });
  await page.route('**/api/artifacts/artifact-ui/download', route => route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'Content-Disposition': 'attachment; filename="summary.docx"' },
    body: Buffer.from('browser-download-fixture'),
  }));

  await page.goto('/');
  await expect(page.getByText('北区销售额高于南区')).toBeVisible();
  await page.getByText('证据与 Claim').click();
  await expect(page.getByText('dataset-ref-1')).toBeVisible();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: 'summary.docx' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('summary.docx');
});


test('builds and validates an approved semantic metric from the UI', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '数据目录' }).click();
  await page.locator('input[type=file][accept*=".csv"]').setInputFiles({
    name: 'sales.csv', mimeType: 'text/csv',
    buffer: Buffer.from('region,month,sales\nNorth,2026-01-01,120\nSouth,2026-01-01,90\n'),
  });
  await expect(page.getByText('sales', { exact: true }).first()).toBeVisible();

  await page.getByRole('button', { name: '指标中心' }).click();
  await page.getByRole('button', { name: '新建语义模型' }).click();
  await page.getByLabel('模型名称').fill('销售事实模型');
  await page.getByRole('button', { name: '保存并校验' }).click();
  await expect(page.getByText('销售事实模型', { exact: false }).first()).toBeVisible();

  await page.getByRole('button', { name: '新建指标' }).click();
  await page.getByLabel('技术名称').fill('total_sales');
  await page.getByLabel('显示名称').fill('销售额');
  await page.getByLabel('度量').selectOption('sales');
  await page.getByLabel('初始状态').selectOption('approved');
  await page.getByRole('button', { name: '保存并校验' }).click();
  await expect(page.getByText('销售额', { exact: true }).first()).toBeVisible();

  await page.getByRole('button', { name: '执行验收' }).click();
  await expect(page.getByText('结果可追溯')).toBeVisible();
  await expect(page.getByRole('cell', { name: '210' })).toBeVisible();
});
