import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/#/workbench');
  await expect(page.locator('.composer__input')).toBeVisible();
});

test('命令面板支持焦点、键盘选择、空态及焦点返回', async ({ page }) => {
  const trigger = page.locator('.topbar__search');
  await trigger.click();
  const dialog = page.getByRole('dialog', { name: '搜索页面与动作' });
  const search = dialog.getByRole('textbox');
  await expect(search).toBeFocused();
  await search.fill('资料库');
  await search.press('Enter');
  await expect(page).toHaveURL(/#\/library/);
  await expect(trigger).toBeFocused();
  await trigger.click();
  await search.fill('不存在的入口');
  await expect(dialog).toContainText('没有匹配的页面或动作');
  await search.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test('嵌套浮层只关闭顶部并恢复下层焦点与背景滚动', async ({ page }) => {
  await page.locator('.sidebar__session-actions [aria-label="重命名会话"]').first().click();
  const rename = page.getByRole('dialog', { name: '重命名会话' });
  const input = rename.getByRole('textbox');
  await expect(input).toBeFocused();
  await page.keyboard.press('Control+k');
  await expect(page.getByRole('dialog', { name: '搜索页面与动作' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(rename).toBeVisible();
  await expect(input).toBeFocused();
  expect(await page.evaluate(() => document.body.style.overflow)).toBe('hidden');
  await page.keyboard.press('Escape');
  await expect(rename).toHaveCount(0);
  expect(await page.evaluate(() => document.body.style.overflow)).toBe('');
});

test('初始打开的弹窗与隐藏字段不破坏焦点循环', async ({ page }) => {
  await page.evaluate(async () => {
    const { Modal } = await import('/src/components/ui.js');
    const host = document.createElement('div');
    document.body.append(host);
    window.reviewModal = Vue.createApp({
      render: () => Vue.h(Modal, { open: true, title: '焦点验证' }, {
        default: () => [Vue.h('input', { type: 'hidden' }), Vue.h('input', { autofocus: true, 'aria-label': '可见输入' })],
        footer: () => Vue.h('button', { id: 'focus-last' }, '保存'),
      }),
    });
    window.reviewModal.mount(host);
  });
  const dialog = page.getByRole('dialog', { name: '焦点验证' });
  await expect(dialog.getByLabel('可见输入')).toBeFocused();
  await dialog.getByRole('button', { name: '保存' }).focus();
  await page.keyboard.press('Tab');
  await expect(dialog.getByRole('button', { name: '关闭' })).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(dialog.getByRole('button', { name: '保存' })).toBeFocused();
  await page.evaluate(() => window.reviewModal.unmount());
  expect(await page.evaluate(() => document.body.style.overflow)).toBe('');
});

test('登录提交防重复，失败后保留输入并可重试', async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ json: {
    ok: true, authenticated: false, local_mode: false, registration_open: true,
  } }));
  let attempts = 0;
  let release;
  const first = new Promise(resolve => { release = resolve; });
  await page.route('**/api/auth/login', async route => {
    attempts += 1;
    if (attempts === 1) await first;
    await route.fulfill({ status: 401, json: { ok: false, error: '用户名或密码错误' } });
  });
  await page.reload();
  await page.getByRole('textbox', { name: '用户名', exact: true }).fill('review-user');
  await page.getByLabel('密码', { exact: true }).fill('review-password');
  await page.getByRole('button', { name: '进入数擎' }).click();
  await expect(page.getByRole('button', { name: '正在验证…' })).toBeDisabled();
  await page.locator('form').evaluate(form => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(attempts).toBe(1);
  release();
  await expect(page.getByRole('alert')).toContainText('用户名或密码错误');
  await expect(page.getByRole('textbox', { name: '用户名', exact: true })).toHaveValue('review-user');
  await page.getByRole('button', { name: '进入数擎' }).click();
  await expect.poll(() => attempts).toBe(2);
});

test('启动请求失败呈现可用重试入口', async ({ page }) => {
  let fail = true;
  await page.route('**/api/bootstrap*', route => fail
    ? route.fulfill({ status: 503, json: { ok: false, error: '服务暂时不可用' } })
    : route.continue());
  await page.reload();
  await expect(page.getByRole('alert')).toContainText('工作空间暂时无法加载');
  fail = false;
  await page.getByRole('button', { name: '重新加载' }).click();
  await expect(page.locator('.composer__input')).toBeVisible();
});

test('登录后的工作空间加载失败可重试，认证完成不提前显示只读工作台', async ({ page }) => {
  const bootstrap = await (await page.request.get('/api/bootstrap')).json();
  let authenticated = false;
  let unavailable = true;
  await page.route('**/api/auth/me', route => route.fulfill({ json: {
    ok: true, authenticated, local_mode: false,
    user: authenticated ? { id: 'shell-review', username: 'shell-review' } : null,
  } }));
  await page.route('**/api/auth/login', route => {
    authenticated = true;
    return route.fulfill({ json: { ok: true, active_workspace_id: 'default' } });
  });
  await page.route('**/api/bootstrap*', route => unavailable
    ? route.fulfill({ status: 503, json: { ok: false, error: '登录后数据暂时不可用' } })
    : route.fulfill({ json: bootstrap }));
  await page.reload();
  await expect(page.getByRole('heading', { name: '登录数擎' })).toBeVisible();
  expect(await page.evaluate(async () => (await import('/src/store.js')).state.ready)).toBe(false);
  await page.getByLabel('用户名', { exact: true }).fill('shell-review');
  await page.getByLabel('密码', { exact: true }).fill('review-password');
  await page.getByRole('button', { name: '进入数擎', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('登录后数据暂时不可用');
  await expect(page.locator('.app')).toHaveCount(0);
  unavailable = false;
  await page.getByRole('button', { name: '重新加载', exact: true }).click();
  await expect(page.locator('.composer__input')).toBeEnabled();
  expect(await page.evaluate(async () => (await import('/src/store.js')).state.ready)).toBe(true);
});

test('已有工作空间的静默刷新失败保留数据、输入和权限，后续刷新可恢复', async ({ page }) => {
  const input = page.locator('.composer__input');
  await input.fill('静默刷新保留未发送的问题');
  const before = await page.evaluate(async () => {
    const { state } = await import('/src/store.js');
    return { sessions: state.sessions, sources: state.sources, role: state.workspaceRole, active: state.activeSessionId };
  });
  await page.route('**/api/bootstrap*', route => route.fulfill({ status: 503, json: { ok: false, error: '刷新暂时不可用' } }));
  await page.evaluate(async () => (await import('/src/store.js')).bootstrap({ quiet: true }));
  await expect(input).toHaveValue('静默刷新保留未发送的问题');
  await expect(input).toBeEnabled();
  await expect(page.locator('.boot-screen')).toHaveCount(0);
  const failed = await page.evaluate(async () => {
    const { state } = await import('/src/store.js');
    return { ready: state.ready, checking: state.authChecking, error: state.bootstrapError,
      sessions: state.sessions, sources: state.sources, role: state.workspaceRole, active: state.activeSessionId };
  });
  expect(failed).toEqual({ ready: true, checking: false, error: '刷新暂时不可用', ...before });
  await page.unroute('**/api/bootstrap*');
  await page.evaluate(async () => (await import('/src/store.js')).bootstrap({ quiet: true }));
  expect(await page.evaluate(async () => (await import('/src/store.js')).state.bootstrapError)).toBe('');
  await expect(input).toHaveValue('静默刷新保留未发送的问题');
});

test('退出和认证过期都清除工作空间就绪状态与权限', async ({ page }) => {
  await page.evaluate(async () => {
    (await import('/src/store.js')).state.user = { id: 'shell-review', username: 'shell-review' };
  });
  await page.route('**/api/auth/logout', route => route.fulfill({ json: { ok: true } }));
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录数擎' })).toBeVisible();
  expect(await page.evaluate(async () => {
    const { state } = await import('/src/store.js');
    return { ready: state.ready, role: state.workspaceRole };
  })).toEqual({ ready: false, role: '' });
  await page.reload();
  await expect(page.locator('.composer__input')).toBeVisible();
  await page.route('**/api/bootstrap*', route => route.fulfill({ status: 401, json: { ok: false, error: '请重新登录' } }));
  await page.evaluate(async () => (await import('/src/store.js')).bootstrap({ quiet: true }));
  await expect(page.getByRole('heading', { name: '登录数擎' })).toBeVisible();
  await expect(page.locator('.app')).toHaveCount(0);
  expect(await page.evaluate(async () => {
    const { state } = await import('/src/store.js');
    return { ready: state.ready, role: state.workspaceRole };
  })).toEqual({ ready: false, role: '' });
});

test('数据表保留特殊值，长文本在表格内换行，多列局部滚动', async ({ page }) => {
  await page.evaluate(async () => {
    const { DataTable } = await import('/src/components/ui.js');
    const host = document.createElement('div');
    host.style.width = '440px';
    host.style.maxWidth = '100%';
    document.querySelector('.view').append(host);
    Vue.createApp({ render: () => Vue.h(DataTable, { rows: [{
      说明: '长标识'.repeat(160), 零值: 0, 空值: null, 负数: -123.456789, 小数: 0.0000001,
      日期: '2026-10-11T00:00:00Z', 英文: 'NoSpaceIdentifier'.repeat(40),
    }] }) }).mount(host);
  });
  const region = page.getByRole('region', { name: '数据明细，可滚动查看' });
  const row = region.locator('tbody tr');
  await expect(row.locator('td').nth(4)).toHaveText('1e-7');
  await expect(row).toContainText('-123.456789');
  await expect(row.locator('td').nth(2)).toHaveText('—');
  const size = await region.evaluate(el => ({ scroll: el.scrollWidth, width: el.clientWidth, doc: document.documentElement.scrollWidth, viewport: innerWidth }));
  expect(size.scroll).toBeGreaterThan(size.width);
  expect(size.doc).toBe(size.viewport);
});

test('公共Tabs支持左右键和首尾键选择', async ({ page }) => {
  await page.goto('/#/metrics');
  const first = page.getByRole('tab', { name: /^指标/ });
  await first.focus();
  await first.press('ArrowRight');
  await expect(page.getByRole('tab', { name: /^维度/ })).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('End');
  await expect(page.getByRole('tab', { name: '业务术语' })).toBeFocused();
  await page.keyboard.press('Home');
  await expect(first).toBeFocused();
});

test('窄窗口与手动折叠导航保留图标尺寸、名称及展开能力', async ({ page }) => {
  await page.setViewportSize({ width: 960, height: 640 });
  const footer = page.locator('.sidebar__foot');
  for (const name of ['回收站', '管理后台', '全局搜索']) {
    const button = footer.getByRole('button', { name, exact: true });
    await expect(button).toBeVisible();
    expect(await button.locator('svg').evaluate(el => el.getBoundingClientRect().width)).toBe(15);
    await expect(button.locator('span')).toBeHidden();
  }
  await page.getByRole('button', { name: '展开导航', exact: true }).click();
  await expect(footer.getByRole('button', { name: '管理后台', exact: true }).locator('span')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(960);
  await page.getByRole('button', { name: '折叠导航', exact: true }).click();
  await footer.getByRole('button', { name: '全局搜索', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '搜索页面与动作' })).toBeVisible();
});

test('手机导航覆盖正文，焦点受控，关闭及选页后无横向溢出', async ({ page }) => {
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto('/#/workbench');
    const trigger = page.getByRole('button', { name: '展开导航', exact: true });
    const before = await page.locator('.main').evaluate(el => el.getBoundingClientRect().width);
    await trigger.click();
    const navigation = page.getByRole('dialog', { name: '导航', exact: true });
    await expect(navigation).toBeVisible();
    await expect(navigation.getByRole('button', { name: '关闭导航', exact: true })).toBeFocused();
    expect(await page.locator('.main').evaluate(el => el.inert)).toBe(true);
    expect(await page.evaluate(() => document.body.style.overflow)).toBe('hidden');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    expect(await page.locator('.main').evaluate(el => el.getBoundingClientRect().width)).toBe(before);
    await page.keyboard.press('Escape');
    await expect(navigation).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(await page.evaluate(() => document.body.style.overflow)).toBe('');
    await trigger.click();
    await navigation.getByRole('button', { name: '关闭导航', exact: true }).click();
    await expect(trigger).toBeFocused();
    await trigger.click();
    await page.getByRole('button', { name: '关闭导航遮罩', exact: true }).click({ position: { x: width - 8, y: 820 } });
    await expect(navigation).toHaveCount(0);
    await trigger.click();
    await navigation.getByRole('button', { name: '资料库', exact: true }).click();
    await expect(page).toHaveURL(/#\/library/);
    await expect(navigation).toHaveCount(0);
    expect(await page.locator('.main').evaluate(el => el.inert)).toBe(false);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
  }
});

test('首次所有者表单执行密码长度校验并保留失败输入', async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ json: {
    ok: true, authenticated: false, local_mode: false, bootstrap_required: true,
  } }));
  let attempts = 0;
  await page.route('**/api/auth/register', route => {
    attempts += 1;
    return route.fulfill({ status: 400, json: { ok: false, error: '初始化令牌不正确' } });
  });
  await page.reload();
  await expect(page.getByRole('heading', { name: '创建首位系统所有者' })).toBeVisible();
  await page.getByLabel('企业邮箱').fill('review@example.com');
  await page.getByLabel('密码', { exact: true }).fill('short');
  await page.getByLabel('初始化令牌').fill('review-token');
  await page.getByRole('button', { name: '创建账号并进入' }).click();
  expect(attempts).toBe(0);
  await page.getByLabel('密码', { exact: true }).fill('review-password-long');
  await page.getByRole('button', { name: '创建账号并进入' }).click();
  await expect(page.getByRole('alert')).toContainText('初始化令牌不正确');
  await expect(page.getByLabel('企业邮箱')).toHaveValue('review@example.com');
  await expect(page.getByLabel('初始化令牌')).toHaveValue('review-token');
});

test('注册验证码有缺失提示、发送状态及重复请求保护', async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ json: {
    ok: true, authenticated: false, local_mode: false, registration_open: true, email_code_required: true,
  } }));
  let attempts = 0;
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/auth/send-code', async route => {
    attempts += 1;
    await pending;
    await route.fulfill({ json: { ok: true, message: '验证码已发送' } });
  });
  await page.reload();
  await page.getByRole('button', { name: '创建账号', exact: true }).click();
  await page.getByRole('button', { name: '发送邮箱验证码' }).click();
  await expect(page.getByRole('alert')).toContainText('请先填写企业邮箱');
  await page.getByLabel('企业邮箱').fill('review@example.com');
  await page.getByRole('button', { name: '发送邮箱验证码' }).click();
  await expect(page.getByRole('button', { name: '正在发送…' })).toBeDisabled();
  await page.evaluate(async () => (await import('/src/store.js')).sendAuthCode());
  expect(attempts).toBe(1);
  release();
  await expect(page.getByRole('status')).toContainText('验证码已发送');
});
