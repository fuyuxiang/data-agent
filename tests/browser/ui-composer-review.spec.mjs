import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/#/workbench');
  await expect(page.locator('.composer__input')).toBeVisible();
  await page.evaluate(async () => {
    const { state } = await import('/src/store.js');
    state.skills = [{ id: 'composer-review-skill', name: '归因分析', description: '解释销售变化' }];
  });
});

test('当前光标的技能候选替换完整查询并保留后续问题与插入位置', async ({ page }) => {
  const input = page.locator('.composer__input');
  await input.fill('为什么 @归因 的销售额下降？');
  await input.evaluate(el => {
    el.setSelectionRange(6, 6);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.getByRole('button', { name: /归因分析.*技能/ }).click();
  await expect(input).toHaveValue('为什么 @归因分析 的销售额下降？');
  await expect(input).toBeFocused();
  expect(await input.evaluate(el => el.selectionStart)).toBe('为什么 @归因分析 '.length);
  await input.pressSequentially('查看');
  await expect(input).toHaveValue('为什么 @归因分析 查看的销售额下降？');
});

test('光标移动可切换技能查询，Escape关闭候选而不重新打开', async ({ page }) => {
  const input = page.locator('.composer__input');
  await input.fill('@归 查询销售额');
  await input.press('Home');
  await input.press('ArrowRight');
  await input.press('ArrowRight');
  await expect(page.getByRole('button', { name: /归因分析.*技能/ })).toBeVisible();
  await input.press('Escape');
  await expect(page.getByRole('button', { name: /归因分析.*技能/ })).toHaveCount(0);
});

test('推荐问题和链接预填自动聚焦、伸高并预判技能，清空后高度恢复', async ({ page }) => {
  const question = '请解释华东销售额下降的原因，比较渠道并列出证据。'.repeat(14);
  const resolved = [];
  await page.route('**/api/skills/resolve', route => {
    resolved.push(route.request().postDataJSON().question);
    return route.fulfill({ json: { selected: [{ id: 'composer-review-skill', name: '预填技能' }] } });
  });
  await page.evaluate(async value => {
    const { state } = await import('/src/store.js');
    state.sources = [{ id: 'composer-review-source', status: 'ready', name: '示例数据' }];
    state.agents = [];
    state.recommendedQuestions = [value];
  }, question);
  const input = page.locator('.composer__input');
  const originalHeight = await input.evaluate(el => el.getBoundingClientRect().height);
  await page.locator('.suggest').click();
  await expect(input).toHaveValue(question);
  await expect(input).toBeFocused();
  await expect.poll(() => input.evaluate(el => el.getBoundingClientRect().height)).toBeGreaterThan(originalHeight);
  await expect(page.locator('.composer__bar')).toContainText('预填技能');
  expect(resolved).toContain(question);
  await input.fill('');
  await expect.poll(() => input.evaluate(el => el.getBoundingClientRect().height)).toBe(originalHeight);
  await page.goto('/#/workbench?ask=' + encodeURIComponent(question));
  await expect(input).toHaveValue(question);
  await expect(input).toBeFocused();
  await expect.poll(() => input.evaluate(el => el.getBoundingClientRect().height)).toBeGreaterThan(originalHeight);
  await expect(page.locator('.composer__bar')).toContainText('预填技能');
});
