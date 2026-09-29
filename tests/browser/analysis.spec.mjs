import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';

test('shows the login portal when authentication is required', async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ json: {
    authenticated: false, local_mode: false, registration_open: false,
    bootstrap_required: false, email_code_required: false,
  } }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '登录数擎平台' })).toBeVisible();
  await expect(page.getByText('证据单元格校验')).toBeVisible();
  await expect(page.getByRole('button', { name: '进入数擎平台' })).toBeVisible();
  await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
  const contrast = await page.evaluate(() => {
    const heading = document.querySelector('.login-heading h2');
    const panel = document.querySelector('.portal-login');
    const luminance = color => {
      const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(value => value / 255);
      const linear = channels.map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
      return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
    };
    const foreground = luminance(getComputedStyle(heading).color);
    const background = luminance(getComputedStyle(panel).backgroundColor);
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  });
  expect(contrast).toBeGreaterThanOrEqual(4.5);
  const heroContrast = await page.evaluate(() => {
    const foreground = getComputedStyle(document.querySelector('.portal-summary')).color;
    const background = getComputedStyle(document.querySelector('.portal-hero')).backgroundColor;
    const luminance = color => {
      const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number)
        .map(value => value / 255)
        .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
      return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
    };
    const values = [luminance(foreground), luminance(background)];
    return (Math.max(...values) + 0.05) / (Math.min(...values) + 0.05);
  });
  expect(heroContrast).toBeGreaterThanOrEqual(4.5);
});

test('keeps the grouped navigation usable at a compact desktop width', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 720 });
  await page.goto('/');
  const sidebar = page.locator('.app-sidebar');
  await expect(sidebar).toHaveCSS('width', '56px');
  await sidebar.hover();
  await expect(sidebar).toHaveCSS('width', '238px');
  await page.getByRole('button', { name: '数据资产', exact: true }).click();
  await expect(page.getByRole('heading', { name: '数据资产' })).toBeVisible();
});

test('keeps the mobile navigation and analysis readable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '今天想了解什么？' })).toBeVisible();
  await page.getByRole('button', { name: '打开导航' }).click();
  await page.getByRole('button', { name: '数据资产', exact: true }).click();
  await expect(page.getByRole('heading', { name: '数据资产' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test('keeps all administration pages readable in dark mode', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('meridian-theme', 'dark'));
  await page.goto('/');
  for (const label of ['数据资产', '指标中心', '知识库', '系统管理']) {
    await page.getByRole('button', { name: label, exact: true }).click();
    await expect(page.getByRole('heading', { name: label }).first()).toBeVisible();
    const channels = await page.evaluate(() => getComputedStyle(document.querySelector('.workspace-page'))
      .backgroundColor.match(/[\d.]+/g).slice(0, 3).map(Number));
    expect(Math.max(...channels)).toBeLessThan(70);
  }
});


test('creates an analysis contract and manages a real indexed attachment', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '今天想了解什么？' })).toBeVisible();
  await page.getByRole('button', { name: '切换深色模式' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.getByRole('button', { name: '切换浅色模式' }).click();

  // 测试 fixture 是临时空数据库，先在数据资产页上传一个 CSV，使当前分析自动绑定到数据源
  await page.getByRole('button', { name: '数据资产', exact: true }).click();
  await page.locator('input[type=file][accept*=".csv"]').setInputFiles({
    name: 'sales.csv', mimeType: 'text/csv',
    buffer: Buffer.from('region,month,sales\nNorth,2026-01-01,120\nSouth,2026-01-01,90\n'),
  });
  await expect(page.getByText('sales', { exact: true }).first()).toBeVisible();

  await page.getByRole('button', { name: '智能分析' }).click();
  await expect(page.getByPlaceholder('描述分析问题；Enter 发送，Shift+Enter 换行')).toBeVisible();

  const composer = page.getByPlaceholder('描述分析问题；Enter 发送，Shift+Enter 换行');
  await page.locator('.composer-file-button input[type=file]').setInputFiles({
    name: 'definition.md', mimeType: 'text/markdown', buffer: Buffer.from('# 指标口径\n销售额按区域汇总。'),
  });
  await expect(page.getByText('definition.md')).toBeVisible();
  await composer.fill('核对区域销售额及口径');
  await composer.press('Enter');
  await expect(page.getByRole('heading', { name: '请核对本次分析的目标与范围' })).toBeVisible();

  const coverage = page.locator('.contract-grid label').filter({ hasText: '统计覆盖范围' }).locator('textarea');
  await coverage.fill('已选授权来源的全部完整记录');
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
    summary: '北区销售额为 120 元，结果已通过独立验证。',
    kpis: [1, 2, 3, 4].map(id => ({ id: `k${id}`, label: `指标 ${id}`, value: id * 10 })),
    charts: [],
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
    if (path === '/api/analyses/run-ui/details') {
      return route.fulfill({ json: { items: [], columns: [], next_cursor: null } });
    }
    if (path === '/api/analyses/run-ui/results') {
      return route.fulfill({ json: {
        status: 'published', manifest: { payload: manifest },
        artifacts: [{ id: 'artifact-ui', filename: 'summary.docx', download_url: '/api/artifacts/artifact-ui/download' }],
      } });
    }
    if (path === '/api/analyses/run-ui/evidence') {
      return route.fulfill({ json: { claims: [{ id: 'claim-1', payload: { text: '北区销售额为 120 元，结果已通过独立验证。', evidence_refs: ['dataset-ref-1'], numbers: [{ text: '120', start: 7, end: 10 }], evidence_cells: [{ number: '120', ref: 'dataset-ref-1', result_id: 'qry-1', row: 0, column: 'sales', value: 120 }], definition_refs: ['metric:sales@1'], status: 'validated', numeric_replay: 'PASS' } }] } });
    }
    if (path === '/api/analyses/run-ui/feedback') {
      return route.fulfill({ json: { item: null } });
    }
    if (path === '/api/analyses/run-ui/email/eml' && request.method() === 'POST') {
      return route.fulfill({ json: { eml: { id: 'email-ui', filename: 'analysis.eml', download_url: '/api/artifacts/email-ui/download' } } });
    }
    if (path === '/api/analyses/run-ui/evidence/claims/claim-1/cells/0') {
      return route.fulfill({ json: { item: { claim: '北区销售额为 120 元，结果已通过独立验证。', number: '120', result_id: 'qry-1', row_index: 0, column: 'sales', value: '120', row: { region: '北区', sales: '120' }, metric: { metric_id: 'sales', metric_version: 1 } } } });
    }
    if (path === '/api/analyses/run-ui') return route.fulfill({ json: { item: run } });
    return route.continue();
  });
  await page.route('**/api/artifacts/artifact-ui/download', route => route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'Content-Disposition': 'attachment; filename="summary.docx"' },
    body: Buffer.from('browser-download-fixture'),
  }));
  await page.route(/\/api\/artifacts\/email-ui\/download(?:\?.*)?$/, route => route.fulfill({
    status: 200,
    headers: { 'Content-Type': 'message/rfc822', 'Content-Disposition': 'attachment; filename="analysis.eml"' },
    body: Buffer.from('browser-email-fixture'),
  }));

  await page.goto('/');
  await page.getByRole('button', { name: '智能分析' }).click();
  await expect(page.locator('.analysis-summary-markdown').getByRole('button', { name: '回放数字 120 的数据证据' })).toBeVisible();
  await expect(page.getByText('metric:sales@1')).toBeVisible();
  await page.locator('.analysis-summary-markdown').getByRole('button', { name: '回放数字 120 的数据证据' }).click();
  await expect(page.getByRole('heading', { name: '数据证据回放' })).toBeVisible();
  await expect(page.getByText('北区', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '可视化看板' }).click();
  await expect(page.getByText('当前已验证数据没有适合绘图的维度和数值列。')).toBeVisible();
  await page.getByRole('button', { name: '完整报告' }).click();
  await expect(page.getByRole('heading', { name: '行动建议' })).toBeVisible();
  await page.getByRole('button', { name: '邮件分享' }).click();
  const emailDialog = page.getByRole('dialog', { name: '邮件分享分析成果' });
  await expect(emailDialog).toBeVisible();
  await emailDialog.getByLabel('收件人邮箱，多个用逗号分隔').fill('reviewer@example.com');
  const emailDownloadPromise = page.waitForEvent('download');
  await emailDialog.getByRole('button', { name: '下载邮件文件' }).click();
  const emailDownload = await emailDownloadPromise;
  expect(emailDownload.suggestedFilename()).toBe('analysis.eml');
  expect((await readFile(await emailDownload.path())).toString()).toBe('browser-email-fixture');
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: 'summary.docx' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('summary.docx');
});

test('creates a knowledge explanation with a validated in-app form', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '知识库', exact: true }).click();
  await page.getByRole('button', { name: '解释·规则·背景' }).click();
  await page.getByRole('button', { name: '新增' }).first().click();
  const dialog = page.getByRole('dialog', { name: '新增指标解释' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('正式指标口径请在指标中心创建并审批')).toBeVisible();
  await dialog.getByRole('button', { name: '保存条目' }).click();
  await expect(dialog.getByRole('alert')).toContainText('请填写指标名称');
  await dialog.getByLabel('指标名称').fill('活跃用户数');
  await dialog.getByLabel('指标定义').fill('在指定日期至少发生一次有效行为的去重用户数。');
  await dialog.getByRole('button', { name: '保存条目' }).click();
  await expect(page.getByText('活跃用户数', { exact: true })).toBeVisible();
});


test('builds and validates an approved semantic metric from the UI', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '数据资产', exact: true }).click();
  await page.locator('input[type=file][accept*=".csv"]').setInputFiles({
    name: 'sales.csv', mimeType: 'text/csv',
    buffer: Buffer.from('region,month,sales\nNorth,2026-01-01,120\nSouth,2026-01-01,90\n'),
  });
  await expect(page.getByText('sales', { exact: true }).first()).toBeVisible();

  await page.getByRole('button', { name: '指标中心' }).click();
  await page.locator('.surface-header').getByRole('button', { name: '新建语义模型' }).click();
  await page.getByLabel('模型名称').fill('销售事实模型');
  await page.getByRole('button', { name: '保存并校验' }).click();
  await expect(page.getByText('销售事实模型', { exact: false }).first()).toBeVisible();

  await page.locator('.surface-header').getByRole('button', { name: '新建指标' }).click();
  await page.getByLabel('技术名称').fill('total_sales');
  await page.getByLabel('业务名称').fill('销售额');
  await page.getByLabel('聚合度量').selectOption('sales');
  await page.getByLabel('初始状态').selectOption('approved');
  await page.getByRole('button', { name: '保存并校验' }).click();
  await expect(page.getByText('销售额', { exact: true }).first()).toBeVisible();

  await page.getByRole('button', { name: '执行验收' }).click();
  await expect(page.getByText('结果可追溯')).toBeVisible();
  await expect(page.getByRole('cell', { name: '210' })).toBeVisible();
});
