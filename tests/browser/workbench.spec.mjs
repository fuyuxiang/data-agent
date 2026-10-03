import { expect, test } from '@playwright/test';

/**
 * 工作台与对话：产品的主路径。
 *
 * 断言的是"用户看得见的行为"，不是内部实现——AI First 的意思是打开系统
 * 就能提问，而不是先去配置一堆东西。
 */

test.beforeEach(async ({ page }) => {
  await page.request.post('/api/demo/seed', { data: { workspace_id: 'default' } });
  await page.goto('/#/workbench');
  await expect(page.locator('.workbench__title')).toBeVisible();
});

test('工作台以提问为第一入口', async ({ page }) => {
  await expect(page.locator('.workbench__title')).toContainText('今天想分析什么');
  await expect(page.locator('.composer__input')).toBeVisible();
  await expect(page.locator('.workbench__capabilities .capability')).toHaveCount(6);
  // 推荐问题来自后端，不允许在前端写死
  await expect(page.locator('.suggest-list .suggest')).toHaveCount(4);
  await expect(page.locator('.suggest').first()).toContainText('销售额');
});

test('首页不出现治理仪表盘类信息', async ({ page }) => {
  const text = await page.locator('.view').innerText();
  for (const forbidden of ['治理完成率', '指标审批', '系统健康', '数据质量仪表盘', '运行数量']) {
    expect(text).not.toContain(forbidden);
  }
});

test('输入问题会预判将使用的技能', async ({ page }) => {
  await page.locator('.composer__input').fill('为什么华东销售下降？');
  await expect(page.locator('.composer__bar')).toContainText('将使用');
  await expect(page.locator('.composer__bar')).toContainText('归因分析');
});

test('显式指定技能时预判跟随指定', async ({ page }) => {
  await page.locator('.composer__input').fill('@预测分析 预测下个月销售额');
  await expect(page.locator('.composer__bar')).toContainText('预测分析');
});

test('提问后进入对话并给出卡片式意图澄清', async ({ page }) => {
  await page.locator('.sidebar__new').click();
  await page.locator('.composer__input').fill('为什么华东销售下降？');
  await page.locator('.composer__send').click();

  await expect(page).toHaveURL(/#\/conversation/);
  await expect(page.locator('.turn__bubble').first()).toContainText('华东销售');
  await expect(page.locator('.execution')).toBeVisible();
  await expect(page.locator('.execution__head')).toContainText('等待你的确认');
  await expect(page.locator('.clarify')).toBeVisible();
  await expect(page.locator('.clarify__title')).toContainText('我理解你想分析');
  // 澄清卡只出现业务语言，不出现内部对象名
  await expect(page.locator('.clarify')).toContainText('分析时间');
  await expect(page.locator('.clarify')).toContainText('重点关注');
  await expect(page.locator('.clarify')).not.toContainText('Analysis Contract');
  await expect(page.locator('.clarify')).not.toContainText('coverage');
});

test('执行状态显示任务步骤而不暴露思维链', async ({ page }) => {
  await page.locator('.composer__input').fill('本月销售额是多少？');
  await page.locator('.composer__send').click();
  const latest = page.locator('.execution').last();
  await expect(latest.locator('.execution__steps')).toBeVisible();
  const steps = await latest.locator('.step').allInnerTexts();
  expect(steps.join(' ')).toContain('已理解问题');
  expect(steps.join(' ')).toContain('已生成结论');
  const text = await page.locator('.view').innerText();
  for (const forbidden of ['思维链', 'reasoning', '思考过程', '让我想想']) {
    expect(text).not.toContain(forbidden);
  }
});

test('新对话会创建并切换会话', async ({ page }) => {
  await page.locator('.sidebar__new').click();
  await expect(page.locator('.sidebar__recent .recent-item').first()).toBeVisible();
  await expect(page).toHaveURL(/#\/workbench/);
});

test('附件选择可撤销且发送时真正上传', async ({ page }) => {
  await page.locator('.sidebar__new').click();
  const input = page.locator('.composer input[type="file"]');
  await input.setInputFiles({
    name: '备注.txt', mimeType: 'text/plain', buffer: Buffer.from('本月门店订单 12 笔'),
  });
  await expect(page.locator('.attachment-chip')).toContainText('备注.txt');
  await expect(page.locator('.composer__scope').first()).toContainText('选择数据');
  await page.getByRole('button', { name: '移除 备注.txt' }).click();
  await expect(page.locator('.attachment-chip')).toHaveCount(0);
  await expect(page.locator('.composer__scope').first()).not.toContainText('选择数据');

  await input.setInputFiles({
    name: '备注.txt', mimeType: 'text/plain', buffer: Buffer.from('本月门店订单 12 笔'),
  });
  await page.locator('.composer__input').fill('分析附件中的门店订单');
  await page.locator('.composer__send').click();
  await expect(page).toHaveURL(/#\/conversation/);
  const sessionId = new URLSearchParams(page.url().split('?')[1]).get('id');
  const runs = await (await page.request.get(`/api/analyses?session_id=${sessionId}`)).json();
  const attachments = await (await page.request.get(`/api/analyses/${runs.items[0].id}/attachments`)).json();
  expect(attachments.items.map(item => item.filename)).toContain('备注.txt');
});
