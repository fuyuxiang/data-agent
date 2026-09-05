import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const load = async (filename) => {
  const sandbox = {};
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  vm.runInNewContext(
    await readFile(new URL(`../frontend/vendor/${filename}`, import.meta.url), 'utf8'),
    sandbox,
    { filename },
  );
  return sandbox;
};

const vue = await load('vue.global.prod.js');
assert.equal(vue.Vue?.version, '3.5.41');
assert.equal(typeof vue.Vue?.compile, 'function');

const echarts = await load('echarts.min.js');
assert.equal(echarts.echarts?.version, '6.1.0');
assert.equal(typeof echarts.echarts?.init, 'function');

vm.runInNewContext(
  await readFile(new URL('../frontend/vendor/echarts-china.min.js', import.meta.url), 'utf8'),
  echarts,
  { filename: 'echarts-china.min.js' },
);
assert.ok(echarts.echarts?.getMap('china')?.geoJSON);

const marked = await load('marked.min.js');
assert.equal(typeof marked.marked?.parse, 'function');
assert.match(marked.marked.parse('# Meridian'), /<h1>Meridian<\/h1>/);

const purify = await load('purify.min.js');
assert.equal(purify.DOMPurify?.version, '3.4.14');
// Without a DOM the factory deliberately exposes only metadata. Browser-side
// sanitization is exercised through the application bundle in real browsers.
assert.equal(purify.DOMPurify?.isSupported, false);

console.log('Verified pinned browser dependency versions and public APIs.');
