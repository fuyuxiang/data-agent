import { expect, test } from '@playwright/test';

// These tests exercise the application and API contract with deterministic
// result fixtures; they do not claim to validate an external language model.
async function source(page, filename, value) {
  const response = await page.request.post('/api/sources/upload', { multipart: {
    file: { name: filename, mimeType: 'text/csv', buffer: Buffer.from(`region,sales\nEast,${value}\n`) },
  } });
  expect(response.status()).toBe(201);
  return (await response.json()).items[0];
}

async function analysis(page, data) {
  const response = await page.request.post('/api/analyses', { data });
  expect(response.status()).toBe(201);
  return (await response.json()).item;
}

async function publishedResults(page, runs) {
  await page.route('**/api/analyses?*', route => route.fulfill({ json: { ok: true,
    items: runs.map(run => ({ ...run, execution_status: 'finished', quality_status: 'passed',
      publication: { id: `published-${run.id}` } })).reverse(),
  } }));
  for (const run of runs) {
    await page.route(`**/api/analyses/${run.id}/results`, route => route.fulfill({ json: {
      ok: true, status: 'published', manifest: { payload: { summary: '已核验的测试结果', tables: [], charts: [] } }, artifacts: [],
    } }));
    await page.route(`**/api/analyses/${run.id}/feedback`, route => route.fulfill({ json: { ok: true, item: null } }));
  }
}

async function twoScopes(page) {
  const originalSource = await source(page, `original-${Date.now()}.csv`, 100);
  const latestSource = await source(page, `latest-${Date.now()}.csv`, 200);
  const original = await analysis(page, { objective: '查看原始范围销售额', source_ids: [originalSource.id],
    contract: { objective: '查看原始范围销售额', time_range: { start: '2026-01-01', end: '2026-02-01' } } });
  const latest = await analysis(page, { session_id: original.session_id,
    objective: '查看另一范围销售额', source_ids: [latestSource.id] });
  await publishedResults(page, [original, latest]);
  return { originalSource, latestSource, original, latest };
}

test('普通多轮追问沿用上一问题的数据范围与来源任务', async ({ page }) => {
  const { latestSource, original, latest } = await twoScopes(page);
  await page.goto(`/#/conversation?id=${original.session_id}`);
  const composer = page.locator('.composer');
  await expect(composer).toContainText(latestSource.name);
  const createdResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/analyses'
    && response.request().method() === 'POST');
  await page.locator('.composer__input').fill('按月份进一步比较');
  await page.locator('.composer__input').press('Enter');
  const response = await createdResponse;
  expect(response.status()).toBe(201);
  const request = response.request().postDataJSON();
  expect(request.source_ids).toEqual([latestSource.id]);
  expect(request.parent_run_id).toBe(latest.id);
  expect((await response.json()).item.parent_run_id).toBe(latest.id);
});

test('追问保存后网络断开，原问题保留且重试复用同一任务', async ({ page }) => {
  const run = await analysis(page, { objective: '已有问题', source_ids: [] });
  await publishedResults(page, [run]);
  let firstRunId = '';
  const keys = [];
  await page.route('**/api/analyses', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    keys.push(route.request().postDataJSON().idempotency_key);
    const response = await route.fetch();
    if (keys.length === 1) {
      firstRunId = (await response.json()).item.id;
      return route.abort('failed');
    }
    return route.fulfill({ response });
  });
  await page.goto(`/#/conversation?id=${run.session_id}`);
  const input = page.locator('.composer__input');
  await input.fill('继续核对同一份分析的口径');
  await input.press('Enter');
  await expect(page.locator('.toast')).toContainText('无法开始分析');
  await expect(input).toHaveValue('继续核对同一份分析的口径');
  const retried = page.waitForResponse(response => new URL(response.url()).pathname === '/api/analyses'
    && response.request().method() === 'POST');
  await input.press('Enter');
  const response = await retried;
  expect(response.status()).toBe(200);
  expect((await response.json()).item.id).toBe(firstRunId);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBeTruthy();
  expect(keys[1]).toBe(keys[0]);
});

test('从旧成果追问锁定原分析，不串入会话后来的数据范围', async ({ page }) => {
  const { originalSource, original } = await twoScopes(page);
  const file = { id: 'journey-library-result', title: '一月经营成果', run_id: original.id,
    extension: '.html', category: '网页', filename: 'report.html', size_bytes: 100, previewable: true };
  await page.route('**/api/library', route => route.fulfill({ json: { ok: true, items: [file], categories: [] } }));
  await page.route(`**/api/library/${file.id}`, route => route.fulfill({ json: { ok: true,
    item: { ...file, analysis: { id: original.id, session_id: original.session_id, archived: false } },
  } }));
  await page.goto('/#/library');
  await page.getByRole('button', { name: '追问', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`run=${original.id}`));
  await expect(page.locator('.composer')).toContainText(originalSource.name);
  await expect(page.locator('.composer__input')).toHaveValue('围绕「一月经营成果」继续追问：');
  const createdResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/analyses'
    && response.request().method() === 'POST');
  await page.locator('.composer__input').fill('请比较同一时间范围的地区差异');
  await page.locator('.composer__input').press('Enter');
  const response = await createdResponse;
  expect(response.status()).toBe(201);
  const child = (await response.json()).item;
  expect(child.parent_run_id).toBe(original.id);
  expect(child.source_scope).toEqual([originalSource.id]);
  expect(child.contract.payload.time_range).toEqual({ start: '2026-01-01', end: '2026-02-01' });
});

test('成果的原分析已删除时明确引导恢复，成果下载仍保留', async ({ page }) => {
  const file = { id: 'journey-archived-result', title: '已保存成果', run_id: 'archived-analysis',
    extension: '.html', category: '网页', filename: 'report.html', size_bytes: 100 };
  await page.route('**/api/library', route => route.fulfill({ json: { ok: true, items: [file], categories: [] } }));
  await page.route(`**/api/library/${file.id}`, route => route.fulfill({ json: { ok: true, item: { ...file,
    analysis: { id: file.run_id, session_id: 'archived-session', archived: true },
  } } }));
  await page.goto('/#/library');
  await page.getByRole('button', { name: '追问', exact: true }).click();
  await expect(page).toHaveURL(/#\/trash\?collection=agent_runs/);
  await expect(page.locator('.toast')).toContainText('恢复后可继续追问');
});

test('长会话加载早期消息与分析后，状态轮询保留历史并更新最新内容', async ({ page }) => {
  const sessionId = 'journey-long-conversation';
  let earlierLoaded = false;
  const message = (id, content, runId) => ({ id, role: 'user', content,
    metadata: { run_id: runId }, created_at: `2026-01-${id === 'older-message' ? '01' : '02'}T00:00:00Z` });
  const oldRun = { id: 'older-run', session_id: sessionId, execution_status: 'failed',
    created_at: '2026-01-01T00:00:00Z', source_scope: [], contract: { payload: { objective: '早期问题' } } };
  const recentRun = { id: 'recent-run', session_id: sessionId, execution_status: 'running',
    created_at: '2026-01-02T00:00:00Z', source_scope: [], contract: { payload: { objective: '最近问题' } } };
  await page.route(`**/api/sessions/${sessionId}*`, route => {
    const before = new URL(route.request().url()).searchParams.get('before');
    if (before) earlierLoaded = true;
    return route.fulfill({ json: { ok: true, item: { id: sessionId },
      messages: [before ? message('older-message', '早期问题', oldRun.id) : message('recent-message', '最近问题', recentRun.id)],
      message_page: { has_more: !before, next_before: before ? null : 201 },
    } });
  });
  await page.route('**/api/analyses?*', route => {
    const older = new URL(route.request().url()).searchParams.has('offset');
    return route.fulfill({ json: { ok: true,
      items: older ? [oldRun] : [{ ...recentRun, execution_status: earlierLoaded ? 'failed' : 'running' }],
      has_more: !older, next_offset: older ? null : 50,
    } });
  });
  await page.goto(`/#/conversation?id=${sessionId}`);
  await expect(page.locator('.turn__bubble')).toHaveText('最近问题');
  await page.getByRole('button', { name: '加载更早的对话与分析', exact: true }).click();
  await expect(page.locator('.turn__bubble')).toHaveText(['早期问题', '最近问题']);
  await expect(page.locator('[data-run-id="older-run"]')).toBeVisible();
  await expect(page.locator('[data-run-id="recent-run"] .execution')).toHaveAttribute('data-state', 'failed');
  await expect(page.locator('.turn__bubble')).toHaveText(['早期问题', '最近问题']);
});

test('刷新近期页会移除另一页面删除的分析与消息，并保留已加载的早期记录', async ({ page }) => {
  const sessionId = 'journey-cross-tab-deletion';
  let deleted = false;
  const runs = ['old-a', 'old-b', 'deleted', 'recent'].map((id, index) => ({ id,
    session_id: sessionId, source_scope: [], history_cursor: index + 1,
    created_at: `2026-01-0${index + 1}T00:00:00Z`,
    execution_status: id === 'recent' ? 'running' : 'failed', contract: { payload: { objective: id } },
  }));
  const messages = runs.map(run => ({ id: `message-${run.id}`, role: 'user', content: `问题 ${run.id}`,
    cursor: run.history_cursor, created_at: run.created_at, metadata: { run_id: run.id } }));
  await page.route(`**/api/sessions/${sessionId}*`, route => {
    const before = new URL(route.request().url()).searchParams.has('before');
    return route.fulfill({ json: { ok: true, item: { id: sessionId },
      messages: before ? messages.slice(0, 2) : deleted ? [messages[1], messages[3]] : messages.slice(2),
      message_page: { has_more: !before, next_before: before ? null : 3 },
    } });
  });
  await page.route('**/api/analyses?*', route => {
    const older = new URL(route.request().url()).searchParams.has('offset');
    return route.fulfill({ json: { ok: true,
      items: older ? runs.slice(0, 2).reverse() : deleted
        ? [{ ...runs[3], execution_status: 'failed' }, runs[1]] : runs.slice(2).reverse(),
      has_more: !older, next_offset: older ? null : 2,
    } });
  });
  await page.goto(`/#/conversation?id=${sessionId}`);
  await page.getByRole('button', { name: '加载更早的对话与分析', exact: true }).click();
  await expect(page.locator('.turn__bubble')).toHaveText(['问题 old-a', '问题 old-b', '问题 deleted', '问题 recent']);
  deleted = true;
  await expect(page.locator('[data-run-id="recent"] .execution')).toHaveAttribute('data-state', 'failed');
  await expect(page.locator('[data-run-id="deleted"]')).toHaveCount(0);
  await expect(page.locator('.turn__bubble')).toHaveText(['问题 old-a', '问题 old-b', '问题 recent']);
  await expect(page.locator('[data-run-id="old-a"]')).toBeVisible();
  await expect(page.getByRole('button', { name: '加载更早的对话与分析', exact: true })).toHaveCount(0);
});

test('删除当前页分析后仍能加载完整历史，新增分析不改变历史游标', async ({ page }) => {
  const sessionId = 'history-mutation-session';
  let runs = Array.from({ length: 51 }, (_, index) => ({
    id: `history-mutation-${index + 1}`, session_id: sessionId, history_cursor: index + 1,
    created_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
    execution_status: 'failed', source_scope: [], contract: { payload: { objective: `历史问题 ${index + 1}` } },
  }));
  await page.route(`**/api/sessions/${sessionId}*`, route => route.fulfill({ json: {
    ok: true, item: { id: sessionId }, messages: [], message_page: { has_more: false },
  } }));
  const paging = [];
  await page.route('**/api/analyses?*', route => {
    const query = new URL(route.request().url()).searchParams;
    paging.push(Object.fromEntries(query));
    const before = query.get('before');
    const eligible = runs.filter(run => !before || run.history_cursor < Number(before)).slice().reverse();
    const items = eligible.slice(Number(query.get('offset') || 0), Number(query.get('offset') || 0) + 50);
    return route.fulfill({ json: { ok: true, items, has_more: eligible.length > items.length,
      next_before: eligible.length > items.length ? items.at(-1).history_cursor : null,
      next_offset: eligible.length > items.length ? items.length : null,
    } });
  });
  await page.route('**/api/analyses/history-mutation-*', route => {
    if (route.request().method() !== 'DELETE') return route.continue();
    runs = runs.filter(run => !route.request().url().endsWith('/' + run.id));
    return route.fulfill({ json: { ok: true, archived: true } });
  });
  await page.goto(`/#/conversation?id=${sessionId}`);
  await expect(page.locator('[data-run-id]')).toHaveCount(50);
  await page.locator('[data-run-id="history-mutation-2"]').getByRole('button', { name: '删除分析', exact: true }).click();
  await page.getByRole('dialog', { name: '删除分析', exact: true }).getByRole('button', { name: '移入回收站', exact: true }).click();
  await expect(page.locator('[data-run-id]')).toHaveCount(49);
  runs.push({ ...runs.at(-1), id: 'history-mutation-new', history_cursor: 52 });
  await page.getByRole('button', { name: '加载更早的对话与分析', exact: true }).click();
  await expect(page.locator('[data-run-id="history-mutation-1"]')).toBeVisible();
  await expect(page.locator('[data-run-id]')).toHaveCount(50);
  expect(paging.at(-1).before).toBe('2');
  await expect(page.getByRole('button', { name: '加载更早的对话与分析', exact: true })).toHaveCount(0);
});

test('后台积累的新内容超过一页时，可补齐中间的消息与分析', async ({ page }) => {
  const sessionId = 'history-page-gap';
  let advanced = false;
  const runs = Array.from({ length: 52 }, (_, index) => ({
    id: `gap-run-${index + 1}`, session_id: sessionId, history_cursor: index + 1,
    created_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(), source_scope: [],
    execution_status: 'failed', contract: { payload: { objective: `分析 ${index + 1}` } },
  }));
  const messages = Array.from({ length: 205 }, (_, index) => ({
    id: `gap-message-${index + 1}`, role: 'user', content: `消息 ${index + 1}`, cursor: index + 1,
    created_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(), metadata: {},
  }));
  await page.route(`**/api/sessions/${sessionId}*`, route => {
    const before = new URL(route.request().url()).searchParams.get('before');
    const eligible = (advanced ? messages : messages.slice(0, 1)).filter(item => !before || item.cursor < Number(before));
    const items = eligible.slice(-200);
    return route.fulfill({ json: { ok: true, item: { id: sessionId }, messages: items,
      message_page: { has_more: eligible.length > items.length, next_before: items[0]?.cursor },
    } });
  });
  await page.route('**/api/analyses?*', route => {
    const before = new URL(route.request().url()).searchParams.get('before');
    const eligible = (advanced ? runs : [{ ...runs[0], execution_status: 'running' }])
      .filter(item => !before || item.history_cursor < Number(before)).slice().reverse();
    const items = eligible.slice(0, 50);
    return route.fulfill({ json: { ok: true, items, has_more: eligible.length > items.length,
      next_before: eligible.length > items.length ? items.at(-1).history_cursor : null,
    } });
  });
  await page.goto(`/#/conversation?id=${sessionId}`);
  await expect(page.locator('.turn__bubble')).toHaveText('消息 1');
  advanced = true;
  await expect(page.locator('[data-run-id="gap-run-52"]')).toBeAttached();
  await expect(page.getByRole('button', { name: '加载更早的对话与分析', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '加载更早的对话与分析', exact: true }).click();
  await expect(page.locator('.turn__bubble')).toHaveText(messages.map(message => message.content));
  await expect(page.locator('[data-run-id]')).toHaveCount(52);
  await expect(page.getByRole('button', { name: '加载更早的对话与分析', exact: true })).toHaveCount(0);
});

for (const width of [1440, 390]) {
  test(`追问改看二月可核对编辑口径，确认失败保留输入（${width}px）`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 950 });
    const parent = await analysis(page, { objective: '查看一月销售额', source_ids: [],
      time_range: { start: '2026-01-01', end: '2026-01-31' }, coverage: '一月全部销售记录' });
    await page.request.post(`/api/analyses/${parent.id}/control`, { data: { action: 'cancel' } });
    await page.goto(`/#/conversation?id=${parent.session_id}`);
    await page.locator('.composer__input').fill('改看2026年2月的销售额');
    await page.locator('.composer__send').click();
    const card = page.locator('.clarify');
    await expect(card).toContainText('2026-02-01 至 2026-02-28');
    await expect(card).toContainText('本次问题调整了统计时间');
    await card.getByRole('button', { name: '查看口径', exact: true }).click();
    await card.getByLabel('统计范围', { exact: true }).fill('华东全部授权销售记录');
    await card.getByLabel('分析维度', { exact: true }).fill('区域、商品');
    await card.getByLabel('分析指标', { exact: true }).fill('销售额');
    let attempts = 0;
    await page.route('**/api/analyses/*/contract/confirm', route => ++attempts === 1
      ? route.fulfill({ status: 503, json: { ok: false, error: '暂未开始，请重试' } }) : route.continue());
    await card.getByRole('button', { name: '开始分析', exact: true }).click();
    await expect(page.locator('.toast')).toContainText('暂未开始，请重试');
    await expect(card.getByLabel('统计范围', { exact: true })).toHaveValue('华东全部授权销售记录');
    expect(await card.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath(`followup-period-retry-${width}.png`), fullPage: true });
    const confirmed = page.waitForResponse(response => response.url().endsWith('/contract/confirm') && response.request().method() === 'POST');
    await card.getByRole('button', { name: '开始分析', exact: true }).click();
    const response = await confirmed;
    expect(response.status()).toBe(200);
    const contract = (await response.json()).item.contract.payload;
    expect(contract.time_range).toEqual({ start: '2026-02-01', end: '2026-02-28' });
    expect(contract.coverage).toBe('华东全部授权销售记录');
    expect(contract.dimensions).toEqual(['区域', '商品']);
    expect(contract.metrics).toEqual(['销售额']);
    await expect(card).toHaveCount(0);
  });
}

test('旺季等不明确时间必须选择日期，刷新后仍可补充并开始', async ({ page }) => {
  const parent = await analysis(page, { objective: '原时间范围', source_ids: [],
    time_range: { start: '2026-01-01', end: '2026-01-31' } });
  await page.request.post(`/api/analyses/${parent.id}/control`, { data: { action: 'cancel' } });
  const child = await analysis(page, { objective: '改看旺季销售额', source_ids: [],
    session_id: parent.session_id, parent_run_id: parent.id });
  await page.goto(`/#/conversation?id=${parent.session_id}&run=${child.id}`);
  await page.reload();
  const card = page.locator('.clarify');
  await expect(card).toContainText('请选择本次分析的时间范围');
  await expect(card.getByRole('button', { name: '开始分析', exact: true })).toBeDisabled();
  await card.getByRole('button', { name: '自定义', exact: true }).click();
  await card.getByLabel('开始日期', { exact: true }).fill('2026-06-01');
  await card.getByLabel('结束日期', { exact: true }).fill('2026-08-31');
  const confirmed = page.waitForResponse(response => response.url().endsWith('/contract/confirm') && response.request().method() === 'POST');
  await card.getByRole('button', { name: '开始分析', exact: true }).click();
  const response = await confirmed;
  expect(response.status()).toBe(200);
  expect((await response.json()).item.contract.payload.time_range).toEqual({ start: '2026-06-01', end: '2026-08-31' });
});

for (const width of [1440, 390]) {
  test(`结果反馈可说明部分错误、失败保留输入、刷新后恢复（${width}px）`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const run = await analysis(page, { objective: '核对反馈闭环', source_ids: [] });
    await publishedResults(page, [run]);
    let saved = null;
    let attempts = 0;
    await page.route(`**/api/analyses/${run.id}/feedback`, route => route.fulfill({ json: { ok: true, item: saved } }));
    await page.route('**/api/feedback', route => {
      attempts += 1;
      if (attempts === 1) return route.fulfill({ status: 503, json: { ok: false, error: '反馈暂未保存，请重试' } });
      saved = { id: 'persisted-feedback', ...route.request().postDataJSON() };
      return route.fulfill({ status: 201, json: { ok: true, item: saved } });
    });
    await page.goto(`/#/conversation?id=${run.session_id}`);
    await page.getByRole('button', { name: '结果需要改进', exact: true }).click();
    const modal = page.getByRole('dialog', { name: '反馈分析结果', exact: true });
    await expect(modal.getByRole('button', { name: '提交反馈', exact: true })).toBeDisabled();
    await modal.getByLabel('结果评价', { exact: true }).selectOption('partially_correct');
    const comment = modal.getByLabel('需要改进的问题', { exact: true });
    await comment.fill('销售额正确，但同比引用了错误的基期，请核对一月数据。');
    await modal.getByRole('button', { name: '提交反馈', exact: true }).click();
    await expect(modal.getByRole('alert')).toContainText('反馈暂未保存');
    await expect(comment).toHaveValue('销售额正确，但同比引用了错误的基期，请核对一月数据。');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`feedback-retry-${width}.png`), fullPage: true });
    await modal.getByRole('button', { name: '提交反馈', exact: true }).click();
    await expect(modal).toHaveCount(0);
    expect(saved.rating).toBe('partially_correct');
    expect(saved.comment).toContain('同比引用了错误的基期');
    await page.reload();
    await expect(page.getByText('已反馈：部分正确', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '结果需要改进', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await page.getByRole('button', { name: '结果需要改进', exact: true }).click();
    await expect(modal.getByLabel('需要改进的问题', { exact: true })).toHaveValue(saved.comment);
    await expect(modal.getByLabel('结果评价', { exact: true })).toHaveValue('partially_correct');
  });
}
