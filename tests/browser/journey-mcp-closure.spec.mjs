import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';

const fixturePath = fileURLToPath(new URL('./fixtures/mcp_readonly_server.py', import.meta.url));

for (const width of [1440, 390]) {
  test(`MCP 本地连接、只读授权、保存重试和刷新恢复 ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const name = `只读授权回归 ${width} ${Date.now()}`;
    const created = await page.request.post('/api/mcp/servers', {
      data: { name, transport: 'stdio', command: 'python3', args: [fixturePath] },
    });
    expect(created.status()).toBe(201);
    const server = (await created.json()).item;
    try {
      await page.goto('/#/admin/mcp');
      const card = page.getByRole('article').filter({ has: page.getByRole('heading', { name, exact: true }) });
      await card.getByRole('button', { name: '测试连接', exact: true }).click();
      await expect(card).toContainText('2 个工具');
      await card.getByRole('button', { name: '查看工具', exact: true }).click();
      const dialog = page.getByRole('dialog');
      const lookup = dialog.getByRole('checkbox', { name: '允许正式分析使用 lookup', exact: true });
      await expect(lookup).toBeEnabled();
      await expect(dialog.getByRole('checkbox', { name: '允许正式分析使用 delete_record', exact: true })).toBeDisabled();
      await lookup.check();
      let attempts = 0;
      await page.route(`**/api/mcp/servers/${server.id}`, route => {
        if (route.request().method() === 'PATCH' && ++attempts === 1) {
          return route.fulfill({ status: 503, json: { ok: false, error: '授权保存暂不可用，请重试' } });
        }
        return route.continue();
      });
      await dialog.getByRole('button', { name: '保存授权', exact: true }).click();
      await expect(dialog.getByRole('alert')).toContainText('授权保存暂不可用');
      await expect(lookup).toBeChecked();
      await dialog.getByRole('button', { name: '保存授权', exact: true }).click();
      await expect(dialog.getByRole('button', { name: '保存授权', exact: true })).toBeDisabled();
      const saved = await page.request.get(`/api/mcp/servers/${server.id}/tools`);
      expect((await saved.json()).formal_read_tools).toEqual(['lookup']);
      await page.reload();
      await card.getByRole('button', { name: '查看工具', exact: true }).click();
      await expect(lookup).toBeChecked();
      expect(await dialog.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
      await page.screenshot({ path: testInfo.outputPath(`mcp-authorization-${width}.png`) });
      await lookup.uncheck();
      await dialog.getByRole('button', { name: '保存授权', exact: true }).click();
      await expect(dialog.getByRole('button', { name: '保存授权', exact: true })).toBeDisabled();
      const revoked = await page.request.get(`/api/mcp/servers/${server.id}/tools`);
      expect((await revoked.json()).formal_read_tools).toEqual([]);
    } finally {
      await page.request.delete(`/api/mcp/servers/${server.id}`);
    }
  });
}
