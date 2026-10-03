/**
 * 前端静态检查。
 *
 * 这些断言都对应真实踩过的坑：
 *   - 构建脚本按目录扫描模块，新增文件不会再漏预编译；
 *   - 组件模板里不能出现反引号（会提前终止模板字符串，构建期静默出错）；
 *   - 旧的品牌色 rgba 残留必须清零（V1 的 indigo 主色就是靠 rgba 逃过了检查）；
 *   - 不允许出现 V1 的死选择器与旧产品术语。
 */

import { readFile, readdir } from 'node:fs/promises';
import { extname, join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const src = resolve(root, 'frontend/src');
const styles = resolve(src, 'styles');

const problems = [];
const notes = [];
const fail = (message) => problems.push(message);

async function collect(directory) {
  const found = [];
  for (const entry of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.isDirectory()) {
      if (['assets', 'node_modules', 'dist'].includes(entry.name)) continue;
      found.push(...await collect(join(directory, entry.name)));
    } else {
      found.push(join(directory, entry.name));
    }
  }
  return found;
}

const files = await collect(src);
const jsFiles = files.filter((file) => extname(file) === '.js');
const cssFiles = files.filter((file) => extname(file) === '.css');

/* ---- 1. Vue 组件模板里禁止反引号 ---- */
for (const file of jsFiles) {
  const source = await readFile(file, 'utf8');
  const pattern = /template\s*:\s*`([\s\S]*?)`/g;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    if (match[1].includes('`')) {
      const line = source.slice(0, match.index).split('\n').length;
      fail(`${file}:${line} 组件模板内出现反引号，会提前终止模板字符串`);
    }
  }
}

/* ---- 1b. 组件选项不得重复声明 ----
 * 同一个对象里写两遍 `computed` / `methods` / `data`，后者会静默覆盖前者，
 * 症状是页面白屏 + "Cannot read properties of undefined"。 */
const OPTIONS = ['setup', 'data', 'computed', 'methods', 'watch', 'mounted', 'template'];
for (const file of jsFiles) {
  const lines = (await readFile(file, 'utf8')).split('\n');
  // 一个文件可以有多个组件，必须按组件对象分别统计，否则会误报。
  const owner = [];
  let current = file;
  let components = 0;
  lines.forEach((line) => {
    if (/^(export )?const [A-Z][A-Za-z0-9_]* = \{/.test(line)) {
      current = `${file}#${components}`;
      components += 1;
    }
    owner.push(current);
  });
  const seen = new Map();
  lines.forEach((line, index) => {
    const match = /^ {2}([a-zA-Z]+)\s*[(:]/.exec(line);
    if (!match || !OPTIONS.includes(match[1])) return;
    const key = `${owner[index]}::${match[1]}`;
    if (seen.has(key)) {
      fail(`${file}:${index + 1} 重复声明组件选项 ${match[1]}（首次在第 ${seen.get(key)} 行）`);
    } else {
      seen.set(key, index + 1);
    }
  });
}

/* ---- 1c. 模板属性里不得出现 \\n ----
 * 模板字面量会把 \\n 变成真实换行，Vue 再把它编进 JS 字符串字面量，直接语法错误。 */
for (const file of jsFiles) {
  const source = await readFile(file, 'utf8');
  const pattern = /template\s*:\s*`([\s\S]*?)`/g;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    if (/\\n/.test(match[1])) {
      const line = source.slice(0, match.index).split('\n').length;
      fail(`${file}:${line} 模板中出现 \\n，会被外层模板字面量转成真实换行并破坏生成代码`);
    }
  }
}

/* ---- 1d. Vue API 必须显式解构 ----
 * 组件里用了 computed / nextTick / ref 却没从 Vue 解构，运行时会抛
 * ReferenceError，症状是点一下才白屏。 */
const VUE_APIS = ['computed', 'nextTick', 'ref', 'reactive', 'watch', 'onMounted', 'onBeforeUnmount'];
for (const file of jsFiles) {
  const source = await readFile(file, 'utf8');
  const destructured = new Set();
  for (const match of source.matchAll(/const \{([^}]*)\} = Vue/g)) {
    match[1].split(',').forEach((name) => destructured.add(name.trim()));
  }
  const body = source.replace(/const \{[^}]*\} = Vue/g, '');
  for (const name of VUE_APIS) {
    if (!new RegExp(`(?<![.\\w])${name}\\s*\\(`).test(body)) continue;
    if (destructured.has(name) || /this\.\$/.test(source)) continue;
    fail(`${file} 使用了 ${name} 但没有从 Vue 解构`);
  }
  if (/\bVue\.h\(/.test(body) && !/const \{\s*h\s*\} = Vue/.test(source)) {
    fail(`${file} 使用了 Vue.h 但没有解构 h`);
  }
}

/* ---- 2. 迁移到新结构 ---- */
const required = [
  'app.js', 'router.js', 'store.js', 'api.js', 'theme.js',
  'components/icons.js', 'components/ui.js', 'components/chart.js',
  'components/composer.js', 'components/execution.js', 'components/result-blocks.js',
  'views/workbench.js', 'views/conversation.js', 'views/agents.js', 'views/library.js',
  'views/metrics.js', 'views/admin-skills.js', 'views/admin-agents.js', 'views/admin-data.js',
  'views/admin-knowledge.js', 'views/admin-capabilities.js', 'views/admin-operations.js',
  'views/admin-system.js',
  'styles/tokens.css', 'styles/base.css', 'styles/components.css',
  'styles/layout.css', 'styles/views.css', 'styles/responsive.css',
];
const relative = files.map((file) => file.slice(src.length + 1));
for (const name of required) {
  if (!relative.includes(name)) fail(`缺少 V2 文件：frontend/src/${name}`);
}

const retired = [
  'components.js', 'panels.js', 'analysis-panel.js', 'settings-panel.js',
  'styles.css', 'legacy.css', 'responsive.css',
];
for (const name of retired) {
  if (relative.includes(name)) fail(`V1 文件仍然存在：frontend/src/${name}`);
}

/* ---- 3. 设计令牌契约 ---- */
const tokens = await readFile(join(styles, 'tokens.css'), 'utf8');
for (const token of [
  '--brand-primary: #4f6bff', '--sidebar: #0c1222', '--app-bg: #f6f8fc',
  '--text-primary: #161b26', '--border: #e5e9f0', '--chart-1: #4f6bff',
  '--radius-composer: 16px', '--sidebar-width: 240px',
]) {
  if (!tokens.includes(token)) fail(`设计令牌缺失或被改动：${token}`);
}
if (!tokens.includes('[data-theme="dark"]')) fail('tokens.css 缺少深色主题覆盖');
if ((tokens.match(/--chart-\d/g) || []).length < 6) fail('图表色板少于六色');

/* ---- 4. 硬编码色：rgba / rgb / hsl 一律不允许 ---- */
const HEX = /#[0-9a-fA-F]{3,8}\b/;
for (const file of [...cssFiles, ...jsFiles]) {
  const source = await readFile(file, 'utf8');
  if (file.endsWith('.css')) {
    for (const match of source.matchAll(/(rgba?|hsla?)\(/g)) {
      const line = source.slice(0, match.index).split('\n').length;
      // tokens.css 的遮罩与侧栏分隔线是唯二的合法例外。
      if (file.endsWith('tokens.css')) continue;
      fail(`${file}:${line} 出现硬编码 ${match[1]}()，请改用设计令牌`);
    }
  }
  // 旧 indigo 主色残留（V1 用 rgba 躲过了 #hex 检查）
  for (const match of source.matchAll(/37\s*,\s*99\s*,\s*235/gi)) {
    const line = source.slice(0, match.index).split('\n').length;
    fail(`${file}:${line} 残留 V1 旧主色 rgba(37,99,235,…)`);
  }
  if (file.endsWith('icons.js') && HEX.test(source) && !/^const PATHS/m.test(source)) {
    fail(`${file} 硬编码了颜色，应改用 tokens.css`);
  }
}

/* ---- 5. 死选择器：定义了但没有任何 JS 引用 ---- */
const allJs = (await Promise.all(jsFiles.map((file) => readFile(file, 'utf8')))).join('\n');
// Vue 在运行期给 <Transition> 生成的类不会出现在源码里，属于误报。
const TRANSITION = /-(enter|leave|move)(-(active|from|to))?$/;
// 运行时拼接出来的类名，源码里只会出现前缀。登记在这里，避免靠阈值兜底。
const DYNAMIC = new Set(['insight--fact', 'insight--inference', 'insight--risk']);
// 扩展名片段不是类名，是选择器正则的已知误报。
const NOT_A_CLASS = new Set(['css', 'js', 'mjs', 'png', 'html']);
const deadClasses = [];
for (const file of cssFiles) {
  const source = await readFile(file, 'utf8');
  for (const match of source.matchAll(/\.([a-z][a-z0-9_-]{2,})(?=[\s,{:>])/gi)) {
    const name = match[1];
    if (TRANSITION.test(name) || DYNAMIC.has(name) || NOT_A_CLASS.has(name)) continue;
    if (!allJs.includes(name)) deadClasses.push(`${file}: .${name}`);
  }
}
if (deadClasses.length > 8) {
  fail(`存在 ${deadClasses.length} 个未使用的 CSS 类（上限 8）：${deadClasses.slice(0, 10).join('、')}`);
}
notes.push(`${deadClasses.length} 个未使用 CSS 类`);

/* ---- 6. 旧产品术语不得出现在用户端 ---- */
const userSurface = ['app.js', 'router.js', 'views/workbench.js', 'views/conversation.js',
  'views/agents.js', 'views/library.js', 'views/metrics.js', 'components/composer.js',
  'components/execution.js', 'components/result-blocks.js'].map((name) => join(src, name));
for (const file of userSurface) {
  const source = await readFile(file, 'utf8');
  for (const term of ['指标治理', '数据资产', '数据治理', '智能分析', '专家团', '成果中心', '资产中心']) {
    if (source.includes(term)) fail(`${file} 用户端仍出现旧产品术语：${term}`);
  }
}

/* ---- 7. 用户端不得出现内部技术概念 ---- */
for (const file of [join(src, 'views/workbench.js'), join(src, 'views/conversation.js')]) {
  const source = await readFile(file, 'utf8');
  for (const term of ['Analysis Contract', 'task_contract', 'run_events', 'ExecutionStatus 面板']) {
    if (source.includes(term)) fail(`${file} 用户端暴露了内部对象：${term}`);
  }
}

/* ---- 8. 图标只用一套 ---- */
const icons = await readFile(join(src, 'components/icons.js'), 'utf8');
if (/[\u{1F300}-\u{1FAFF}]/u.test(icons)) fail('icons.js 使用了 Emoji 图标');
notes.push(`${(icons.match(/^\s{2}\w+:/gm) || []).length} 个图标`);

/* ---- 结果 ---- */
notes.forEach((note) => console.log(`  · ${note}`));
if (problems.length) {
  console.error(`\n前端静态检查未通过（${problems.length} 项）：`);
  problems.forEach((problem) => console.error(`  ✗ ${problem}`));
  process.exit(1);
}
console.log(`前端静态检查通过：${jsFiles.length} 个模块、${cssFiles.length} 个样式文件。`);
