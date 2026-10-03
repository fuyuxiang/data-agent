import { cp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const frontend = resolve(root, 'frontend');
const output = resolve(frontend, 'dist');

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await cp(resolve(frontend, 'src'), resolve(output, 'src'), { recursive: true });
await cp(resolve(frontend, 'vendor'), resolve(output, 'vendor'), { recursive: true });

const vueSource = await readFile(resolve(frontend, 'vendor/vue.global.prod.js'), 'utf8');
const decodeHtml = (value) => value
  .replaceAll('&quot;', '"').replaceAll('&#39;', "'")
  .replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');
globalThis.document = {
  createElement() {
    let html = '';
    return {
      children: [{ getAttribute: () => decodeHtml((html.match(/foo="([\s\S]*)">/) || [])[1] || '') }],
      set innerHTML(value) { html = String(value); },
      get textContent() { return decodeHtml(html); },
    };
  },
};
const VueCompiler = Function(`${vueSource}; return Vue;`)();
globalThis.Vue = VueCompiler;

/**
 * Discover every module that may declare a component template.
 *
 * The list used to be hardcoded, so a new component file silently shipped
 * without a precompiled render function and the production bundle came up
 * blank.  Scanning the directory removes that whole failure mode.
 */
async function collectModules(directory) {
  const found = [];
  for (const entry of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.isDirectory()) {
      if (entry.name === 'assets' || entry.name === 'node_modules') continue;
      found.push(...await collectModules(join(directory, entry.name)));
    } else if (extname(entry.name) === '.js') {
      found.push(join(directory, entry.name));
    }
  }
  return found;
}

const renderSources = [];
for (const file of await collectModules(resolve(output, 'src'))) {
  const source = await readFile(file, 'utf8');
  if (!/template\s*:/.test(source)) continue;
  const compiled = source.replace(/template\s*:\s*`([\s\S]*?)`/g, (_match, template) => {
    const render = VueCompiler.compile(template, { hoistStatic: false, cacheHandlers: false });
    const index = renderSources.length;
    renderSources.push(String(render));
    return `render: window.__MERIDIAN_RENDERS[${index}]`;
  });
  await writeFile(file, compiled);
}

const renderBundle = [
  'window.__MERIDIAN_RENDERS = [];',
  // Keep Vue's compiler-generated `_Vue` closure name. Its leading underscore
  // deliberately avoids component-proxy shadowing inside `with (_ctx)`.
  ...renderSources.map((source) => `{ const _Vue = window.Vue; const render = ${source}; render._rc = true; window.__MERIDIAN_RENDERS.push(render); }`),
].join('\n');
await writeFile(resolve(output, 'src/renders.js'), renderBundle);

let html = await readFile(resolve(frontend, 'index.html'), 'utf8');
html = html.replaceAll('href="/src/', 'href="./src/').replaceAll('src="/src/', 'src="./src/').replaceAll('src="/vendor/', 'src="./vendor/');
html = html.replace(
  '<script type="module" src="./src/app.js"></script>',
  '<script src="./src/renders.js"></script>\n    <script type="module" src="./src/app.js"></script>',
);
await writeFile(resolve(output, 'index.html'), html);
console.log(`Built static frontend with ${renderSources.length} precompiled templates at ${output}`);
console.log(`Modules: ${renderSources.length} of ${relative(root, resolve(output, 'src'))} tree`);
