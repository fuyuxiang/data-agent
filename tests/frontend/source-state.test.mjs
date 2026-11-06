import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

globalThis.Vue = { reactive: value => value, computed: getter => ({ get value() { return getter(); } }) };
globalThis.document = { documentElement: { dataset: {} } };
globalThis.localStorage = globalThis.sessionStorage = { getItem: () => null };
const { actions, state } = await import('../../frontend/src/store.js');
const response = data => Response.json({ ok: true, ...data });

beforeEach(() => {
  state.workspaceId = 'default';
  state.user = null;
  state.authRequired = false;
  state.sources = [{ id: 'original-source', name: '原有范围' }];
});

test('all authorized pages commit atomically, including an empty page and 101 sources', async () => {
  const sources = Array.from({ length: 101 }, (_, index) => ({ id: `source-${index}` }));
  const paths = [];
  globalThis.fetch = async path => {
    paths.push(path);
    assert.deepEqual(state.sources, [{ id: 'original-source', name: '原有范围' }]);
    if (paths.length === 1) return response({ items: [], next_cursor: 'hidden' });
    if (paths.length === 2) return response({ items: sources.slice(0, 100), next_cursor: 'last-page' });
    return response({ items: sources.slice(99), next_cursor: null });
  };
  assert.equal((await actions.refreshSources()).length, 101);
  assert.deepEqual(state.sources, sources);
  assert.deepEqual(paths, ['/api/sources', '/api/sources?cursor=hidden', '/api/sources?cursor=last-page']);
});

test('a failed continuation or repeated cursor preserves the last complete scope', async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    if (++calls === 1) return response({ items: [{ id: 'partial' }], next_cursor: 'next' });
    throw new Error('续页失败');
  };
  await assert.rejects(actions.refreshSources(), /续页失败/);
  assert.equal(state.sources[0].id, 'original-source');
  calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return response({ items: [], next_cursor: 'repeated' });
  };
  await assert.rejects(actions.refreshSources(), /分页未能继续/);
  assert.equal(calls, 2);
  assert.equal(state.sources[0].id, 'original-source');
});

test('delayed list and catalog responses cannot resurrect a deleted source', async () => {
  let release;
  globalThis.fetch = () => new Promise(resolve => { release = resolve; });
  const loading = actions.refreshSources();
  actions.updateSource({ id: 'original-source', name: '最新目录', workspace_id: 'default' });
  assert.equal(state.sources[0].name, '最新目录');
  actions.forgetSource('original-source');
  actions.updateSource({ id: 'original-source', name: '过期目录', workspace_id: 'default' });
  release(response({ items: [{ id: 'original-source', name: '过期列表' }] }));
  assert.equal(await loading, null);
  assert.deepEqual(state.sources, []);
});

test('workspace changes discard an older list and catalog response', async () => {
  let release;
  globalThis.fetch = () => new Promise(resolve => { release = resolve; });
  const loading = actions.refreshSources();
  state.workspaceId = 'new-workspace';
  state.sources = [{ id: 'new-source' }];
  actions.updateSource({ id: 'new-source', workspace_id: 'default' });
  actions.forgetSource('new-source', 'default');
  release(response({ items: [{ id: 'old-source' }] }));
  assert.equal(await loading, null);
  assert.deepEqual(state.sources, [{ id: 'new-source' }]);
});
