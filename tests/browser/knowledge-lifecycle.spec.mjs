import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  expect((await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } })).ok()).toBeTruthy();
  await page.goto('/#/admin/knowledge');
  await expect(page.locator('.page-head__title')).toHaveText('知识');
});

function card(page, name) {
  return page.locator('.view article.card').filter({ has: page.getByText(name, { exact: true }) });
}

async function uploadDocument(page, name, content) {
  const uploaded = page.waitForResponse(response => response.url().endsWith('/api/knowledge/documents')
    && response.request().method() === 'POST');
  await page.locator('input[type="file"]').setInputFiles({
    name: `${name}.md`, mimeType: 'text/markdown', buffer: Buffer.from(content),
  });
  const response = await uploaded;
  expect(response.status()).toBe(201);
  const document = (await response.json()).item;
  await page.getByRole('tab', { name: /^知识文档/ }).click();
  await expect(card(page, name)).toBeVisible();
  return document;
}

async function deleteDocumentDialog(page, name) {
  await card(page, name).getByRole('button', { name: `删除文档 ${name}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除知识文档', exact: true });
  await expect(dialog).not.toContainText('正在检查智能体引用');
  return dialog;
}

async function restoreFromTrash(page, href, name) {
  await page.goto(`/${href}`);
  await expect(page.locator('.page-head__title')).toHaveText('回收站');
  const archived = card(page, name);
  await expect(archived).toBeVisible();
  await archived.getByRole('button', { name: '恢复', exact: true }).click();
  await expect(archived).toHaveCount(0);
  await page.goto('/#/admin/knowledge');
  await expect(page.locator('.page-head__title')).toHaveText('知识');
}

test('文档上传后删除必须确认，回收站能够恢复文档及检索', async ({ page }) => {
  const name = '文档回收站验证';
  const document = await uploadDocument(page, name, '文档回收站验证的采购复核必须使用原始订单凭据。');
  let deleteRequests = 0;
  page.on('request', request => {
    if (request.method() === 'DELETE' && request.url().endsWith(`/api/knowledge/documents/${document.id}`)) deleteRequests++;
  });
  let dialog = await deleteDocumentDialog(page, name);
  await expect(dialog.getByRole('button', { name: '确认删除', exact: true })).toBeEnabled();
  expect(deleteRequests).toBe(0);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(card(page, name)).toBeVisible();
  dialog = await deleteDocumentDialog(page, name);
  const href = await dialog.getByRole('link', { name: '回收站', exact: true }).getAttribute('href');
  expect(href).toBe('#/admin/trash?collection=knowledge_documents');
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(card(page, name)).toHaveCount(0);
  expect(deleteRequests).toBe(1);
  await restoreFromTrash(page, href, name);
  await page.getByRole('tab', { name: /^知识文档/ }).click();
  await expect(card(page, name)).toBeVisible();
  await page.getByRole('tab', { name: '检索测试', exact: true }).click();
  await page.getByPlaceholder('例如：GMV 的计算口径是什么？').fill('文档回收站验证');
  await page.getByRole('button', { name: '检索', exact: true }).click();
  await expect(card(page, name)).toContainText('采购复核必须使用原始订单凭据');
});

test('结构化知识条目删除必须确认，并可从回收站恢复', async ({ page }) => {
  const name = '条目回收站验证';
  const group = page.locator('section.card').filter({ has: page.getByRole('heading', { name: '业务规则', exact: true }) });
  await group.getByRole('button', { name: '新增', exact: true }).click();
  const editor = page.getByRole('dialog', { name: '新增知识条目', exact: true });
  await editor.locator('input').first().fill(name);
  await editor.locator('textarea').fill('条目回收站验证必须保留采购原始凭据。');
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  await expect(editor).not.toBeVisible();
  await expect(card(page, name)).toBeVisible();
  await card(page, name).getByRole('button', { name: `删除条目 ${name}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除知识条目', exact: true });
  await expect(dialog).toContainText(name);
  const href = await dialog.getByRole('link', { name: '回收站', exact: true }).getAttribute('href');
  expect(href).toBe('#/admin/trash?collection=knowledge_entries');
  const before = (await (await page.request.get('/api/knowledge/entries')).json()).items;
  const entry = before.find(item => item.name === name);
  expect(entry).toBeTruthy();
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(card(page, name)).toHaveCount(0);
  await restoreFromTrash(page, href, name);
  await expect(card(page, name)).toBeVisible();
  await page.getByRole('tab', { name: '检索测试', exact: true }).click();
  await page.getByPlaceholder('例如：GMV 的计算口径是什么？').fill(name);
  await page.getByRole('button', { name: '检索', exact: true }).click();
  await expect(card(page, name)).toContainText('必须保留采购原始凭据');
});

test('发布智能体的文档引用阻止删除，解除并重新发布后才可删除', async ({ page }) => {
  const name = '已发布引用验证文档';
  const document = await uploadDocument(page, name, '已发布引用验证文档包含订单原始凭据。');
  const sources = (await (await page.request.get('/api/sources')).json()).items;
  const created = await page.request.post('/api/agents', { data: {
    name: '文档引用验证助手', source_ids: [sources[0].id], knowledge_document_ids: [document.id],
  } });
  expect(created.status()).toBe(201);
  const agent = (await created.json()).item;
  expect((await page.request.post(`/api/agents/${agent.id}/publish`)).ok()).toBeTruthy();
  const dialog = await deleteDocumentDialog(page, name);
  await expect(dialog).toContainText(agent.name);
  await expect(dialog).toContainText('已发布版本');
  await expect(dialog).toContainText('修改配置并重新发布');
  await expect(dialog.getByRole('link', { name: '管理智能体引用' })).toHaveAttribute('href', '#/admin/agents');
  await expect(dialog.getByRole('button', { name: '确认删除', exact: true })).toBeDisabled();
  expect((await page.request.delete(`/api/knowledge/documents/${document.id}`)).status()).toBe(409);
  expect((await page.request.patch(`/api/knowledge/documents/${document.id}`, { data: { enabled: false } })).status()).toBe(409);
  expect((await page.request.patch(`/api/agents/${agent.id}`, { data: { knowledge_document_ids: [] } })).ok()).toBeTruthy();
  await dialog.getByRole('button', { name: '刷新引用', exact: true }).click();
  const reference = dialog.locator('p').filter({ hasText: agent.name });
  await expect(reference).toContainText('已发布版本');
  await expect(reference).not.toContainText('草稿配置');
  await expect(dialog.getByRole('button', { name: '确认删除', exact: true })).toBeDisabled();
  expect((await page.request.post(`/api/agents/${agent.id}/publish`)).ok()).toBeTruthy();
  await dialog.getByRole('button', { name: '刷新引用', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '确认删除', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(card(page, name)).toHaveCount(0);
});

test('删除后延迟到达的旧检索响应不会重新显示文档', async ({ page }) => {
  const name = '异步旧检索验证文档';
  const document = await uploadDocument(page, name, '异步旧检索验证文档应在删除后退出召回。');
  let releaseSearch, markCaptured;
  const released = new Promise(resolve => { releaseSearch = resolve; });
  const captured = new Promise(resolve => { markCaptured = resolve; });
  let held = false;
  await page.route('**/api/knowledge/search', async route => {
    if (held || route.request().method() !== 'POST') return route.continue();
    held = true;
    const response = await route.fetch();
    const payload = await response.json();
    expect(payload.items.some(item => item.document_id === document.id)).toBeTruthy();
    markCaptured();
    await released;
    await route.fulfill({ response, json: payload });
  });
  await page.getByRole('tab', { name: '检索测试', exact: true }).click();
  await page.getByPlaceholder('例如：GMV 的计算口径是什么？').fill(name);
  await page.getByRole('button', { name: '检索', exact: true }).click();
  await captured;
  await page.getByRole('tab', { name: /^知识文档/ }).click();
  const dialog = await deleteDocumentDialog(page, name);
  await expect(dialog.getByRole('button', { name: '确认删除', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(card(page, name)).toHaveCount(0);
  await page.getByRole('tab', { name: '检索测试', exact: true }).click();
  const staleResponse = page.waitForResponse(response => response.url().endsWith('/api/knowledge/search')
    && response.request().method() === 'POST');
  releaseSearch();
  await (await staleResponse).finished();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(card(page, name)).toHaveCount(0);
  await expect(page.locator('.empty')).toContainText('还没有检索结果');
  await expect(page.getByRole('button', { name: '检索', exact: true })).toBeEnabled();
});
