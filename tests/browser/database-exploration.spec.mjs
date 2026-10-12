import { expect, test } from '@playwright/test';

async function databaseFixture(page) {
  const tables = Array.from({ length: 238 }, (_, index) => ({ name: `table_${String(index).padStart(3, '0')}`,
    source_name: `table_${String(index).padStart(3, '0')}`, rows: 25_974_414, rows_estimated: true,
    columns: 273, comment: index === 237 ? '订单金额 单位：分' : '设备记录', object_type: 'table' }));
  const source = { id: 'readonly-doris-review', name: '大型只读数仓', kind: 'database', engine_family: 'doris',
    read_only: true, status: 'ready', catalog_total: tables.length, tables: tables.slice(0, 100) };
  await page.route('**/api/sources', route => route.fulfill({ json: { ok: true, items: [source] } }));
  await page.route('**/api/sources/readonly-doris-review/catalog*', route => {
    const parameters = new URL(route.request().url()).searchParams;
    const matched = tables.filter(table => `${table.name} ${table.comment}`.includes(parameters.get('q') || ''));
    const later = matched.filter(table => table.name > (parameters.get('cursor') || ''));
    const items = later.slice(0, Number(parameters.get('limit') || 25));
    return route.fulfill({ json: { ok: true, items, total: matched.length,
      next_cursor: later.length > items.length ? items.at(-1).name : null } });
  });
  await page.route('**/api/sources/readonly-doris-review/schema*', route => {
    const parameters = new URL(route.request().url()).searchParams;
    const offset = Number(parameters.get('column_offset') || 0);
    const size = Number(parameters.get('column_limit') || 50);
    const columns = Array.from({ length: 273 }, (_, index) => ({
      name: index === 250 ? 'pay_amount' : `field_${String(index).padStart(3, '0')}`,
      type: 'varchar(512)', comment: index === 250 ? '应支付金额 单位：分' : '业务字段', nullable: true,
    }));
    const matched = columns.filter(column => `${column.name} ${column.comment}`.includes(parameters.get('column_query') || ''));
    return route.fulfill({ json: { ok: true, schema: { tables: [{ name: parameters.get('table'),
      columns: matched.slice(offset, offset + size), column_count: matched.length,
      next_column_offset: offset + size < matched.length ? offset + size : null }] } } });
  });
  return source;
}

test('数据库目录可搜索后面的表，宽表字段可完整翻页，金额单位可见', async ({ page }) => {
  const source = await databaseFixture(page);
  await page.goto('/#/admin/data');
  await page.getByRole('button', { name: '查看', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: source.name });
  await expect(drawer).toContainText('238 个表或视图');
  await expect(drawer).not.toContainText('undefined 行');
  await drawer.getByRole('tab', { name: '结构', exact: true }).click();
  await drawer.getByLabel('搜索数据库数据表').fill('订单金额');
  await drawer.getByRole('button', { name: '搜索', exact: true }).click();
  await drawer.getByRole('button', { name: /table_237.*订单金额/ }).click();
  await expect(drawer).toContainText('field_000');
  for (let pageNumber = 1; pageNumber <= 5; pageNumber++) {
    await drawer.getByRole('button', { name: '下一页字段', exact: true }).click();
    await expect(drawer).toContainText(pageNumber === 5 ? 'pay_amount' : `field_${String(pageNumber * 50).padStart(3, '0')}`);
  }
  await expect(drawer).toContainText('应支付金额 单位：分');
  await expect(drawer.getByRole('button', { name: '下一页字段', exact: true })).toBeDisabled();
  await page.screenshot({ path: 'test-results/database-wide-schema.png', fullPage: true });
});

test('数据库探查展示选定范围和样本限制，运行任务可取消并重试', async ({ page }) => {
  const source = await databaseFixture(page);
  let cancelRequested = false;
  let submitted;
  let runNumber = 0;
  await page.route('**/api/sources/readonly-doris-review/profile-jobs', route => {
    submitted = route.request().postDataJSON();
    runNumber++;
    return route.fulfill({ status: 202, json: { ok: true, job: { id: 'database-profile-review' } } });
  });
  await page.route('**/api/jobs/database-profile-review', route => route.fulfill({ json: { ok: true, item:
    runNumber > 1 ? { status: 'completed', result: { profile: { sampled: true, rows: 500, column_count: 10,
      sample_rows: 500, completeness: 'partial', accuracy: 'sample_based', columns: [],
      sampling_note: '有界预览样本，统计仅描述当前返回记录，不能推断全表分布。',
      scope: { table: 'table_000', columns: Array(10).fill('field'), time_start: '2026-10-01', time_end: '2026-10-12' } } } }
      : cancelRequested ? { status: 'cancelled' } : { status: 'running', message: '正在检查查询范围' } } }));
  await page.route('**/api/jobs/database-profile-review/cancel', route => {
    cancelRequested = true;
    return route.fulfill({ json: { ok: true, accepted: true } });
  });
  await page.goto('/#/admin/data');
  await page.getByRole('button', { name: '查看', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: source.name });
  await drawer.getByRole('tab', { name: '数据探查', exact: true }).click();
  await drawer.getByLabel('探查时间字段').fill('field_001');
  await drawer.getByLabel('探查开始日期').fill('2026-10-01');
  await drawer.getByLabel('探查结束日期').fill('2026-10-12');
  await drawer.getByRole('button', { name: '开始探查', exact: true }).click();
  await expect(drawer.getByRole('button', { name: '取消探查', exact: true })).toBeVisible();
  await expect.poll(() => submitted?.time_start).toBe('2026-10-01');
  await drawer.getByRole('button', { name: '取消探查', exact: true }).click();
  await expect(drawer).toContainText('本次探查已取消');
  await drawer.getByRole('button', { name: '开始探查', exact: true }).click();
  await expect(drawer).toContainText('不能推断全表分布');
  await expect(drawer).toContainText('本次检查 500 行');
  await expect(drawer).toContainText('2026-10-01 至 2026-10-12');
  await page.screenshot({ path: 'test-results/database-exploration-scope.png', fullPage: true });
});

test('窄屏探查可搜索宽表末尾字段并准确提交字段和日期格式', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const source = await databaseFixture(page);
  let submitted;
  await page.route('**/api/sources/readonly-doris-review/profile-jobs', route => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ status: 202, json: { ok: true, job: { id: 'narrow-profile' } } });
  });
  await page.route('**/api/jobs/narrow-profile', route => route.fulfill({ json: { ok: true, item:
    { status: 'completed', result: { profile: { mode: 'aggregate', rows: 100,
      columns: [], note: '去重数为近似统计。', scope: { table: 'table_000', columns: ['pay_amount'] } } } } } }));
  await page.goto('/#/admin/data');
  await page.getByRole('button', { name: '查看', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: source.name });
  await drawer.getByRole('tab', { name: '数据探查', exact: true }).click();
  await drawer.getByLabel('探查方式', { exact: true }).selectOption('aggregate');
  await drawer.getByRole('button', { name: '清空选择', exact: true }).click();
  await drawer.getByLabel('搜索探查字段').fill('单位：分');
  await drawer.getByRole('button', { name: '查找字段', exact: true }).click();
  await drawer.getByRole('checkbox', { name: '选择字段 pay_amount', exact: true }).check();
  await drawer.getByLabel('时间字段格式', { exact: true }).selectOption('yyyyMMdd');
  await drawer.getByRole('button', { name: '开始探查', exact: true }).click();
  await expect.poll(() => submitted?.columns).toEqual(['pay_amount']);
  expect(submitted.time_format).toBe('yyyyMMdd');
  await expect(drawer).toContainText('去重数为近似统计');
  await page.screenshot({ path: 'test-results/database-exploration-mobile.png', fullPage: true });
});
