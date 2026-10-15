import { expect, test } from '@playwright/test';

test('旧对话可搜索、分页、刷新恢复，并继续查看原提问', async ({ page }) => {
  const prefix = `历史回归 ${Date.now()}`;
  const session = (await (await page.request.post('/api/sessions', { data: { name: `${prefix} 目标` } })).json()).item;
  await page.request.post('/api/analyses', { data: { session_id: session.id, objective: '历史提问：核对本月经营情况' } });
  for (let index = 0; index < 32; index++) {
    expect((await page.request.post('/api/sessions', { data: { name: `${prefix} ${index}` } })).ok()).toBeTruthy();
  }
  await page.goto('/#/workbench');
  await page.getByRole('button', { name: '全部对话', exact: true }).click();
  await page.getByPlaceholder('搜索对话名称').fill(prefix);
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.locator('.history-item')).toHaveCount(30);
  await expect(page.getByRole('navigation', { name: '对话分页' })).toContainText('共 33 个对话');
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.locator('.history-item')).toHaveCount(3);
  await page.reload();
  await expect(page.getByPlaceholder('搜索对话名称')).toHaveValue(prefix);
  await expect(page.getByRole('navigation', { name: '对话分页' })).toContainText('第 2 页');
  await page.getByPlaceholder('搜索对话名称').fill(`${prefix} 目标`);
  await page.getByPlaceholder('搜索对话名称').press('Enter');
  await expect(page.locator('.history-item')).toHaveCount(1);
  await page.locator('.history-item').click();
  await expect(page).toHaveURL(new RegExp(`conversation\\?id=${session.id}`));
  await expect(page.locator('.turn__bubble')).toContainText('历史提问：核对本月经营情况');
  await page.reload();
  await expect(page.locator('.turn__bubble')).toContainText('历史提问：核对本月经营情况');
  await expect(page.locator('.topbar__sub')).toHaveText(`${prefix} 目标`);
  expect(await page.evaluate(async () => (await import('/src/store.js')).state.activeSessionId)).toBe(session.id);
});

test('历史加载失败可以重试，空搜索结果有解释', async ({ page }) => {
  let failed = true;
  await page.route('**/api/sessions?*', route => failed
    ? route.fulfill({ status: 503, json: { ok: false, error: '对话暂时不可用' } }) : route.continue());
  await page.goto('/#/history');
  await expect(page.getByRole('alert')).toContainText('对话暂时不可用');
  failed = false;
  await page.getByRole('button', { name: '重新加载' }).click();
  await expect(page.locator('.history-item').first()).toBeVisible();
  await page.getByPlaceholder('搜索对话名称').fill('绝不存在的对话关键词');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByRole('heading', { name: '没有匹配的对话' })).toBeVisible();
});

test('删除正在查看的会话后回到可提问页面', async ({ page }) => {
  const session = (await (await page.request.post('/api/sessions', { data: { name: '待删除的当前会话' } })).json()).item;
  await page.goto(`/#/conversation?id=${session.id}`);
  await expect(page.locator('.composer__input')).toBeVisible();
  page.once('dialog', dialog => dialog.accept());
  await page.locator('.sidebar__session').filter({ hasText: '待删除的当前会话' })
    .getByRole('button', { name: '删除会话', exact: true }).click();
  await expect(page).toHaveURL(/#\/workbench$/);
  await expect(page.locator('.composer__input')).toBeVisible();
  expect((await page.request.get(`/api/sessions/${session.id}`)).status()).toBe(404);
});

for (const width of [1440, 390]) {
  test(`全部对话在 ${width}px 可查找，长标题不溢出`, async ({ page }) => {
    const title = '历史长标题'.repeat(30);
    await page.request.post('/api/sessions', { data: { name: title } });
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/#/history?q=历史长标题');
    await expect(page.locator('.history-item').first()).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    await page.screenshot({ path: `test-results/journey-history-${width}.png`, fullPage: true });
  });
}

test('390px首次管理员空态可完整阅读并操作', async ({ page }, testInfo) => {
  await page.route('**/api/bootstrap*', async route => {
    const response = await route.fetch();
    const bootstrap = await response.json();
    await route.fulfill({ json: { ...bootstrap, sources: [], agents: [], recommended_questions: [] } });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/#/workbench');
  await expect(page.getByRole('button', { name: '接入数据', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '使用演示数据', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  expect(await page.locator('.workbench__empty > .grow').evaluate(element => element.clientWidth)).toBeGreaterThan(150);
  expect(await page.locator('.workbench__empty').evaluate(element => element.clientHeight)).toBeLessThan(350);
  await page.screenshot({ path: testInfo.outputPath('first-use-390.png'), fullPage: true });
});
