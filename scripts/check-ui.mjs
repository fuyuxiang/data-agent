import fs from 'node:fs';

const directory = new URL('../frontend/src/', import.meta.url);
const source = name => fs.readFileSync(new URL(name, directory), 'utf8');
const product = source('styles.css');
const legacy = source('legacy.css');
const responsive = source('responsive.css');
const scripts = ['app.js', 'analysis-panel.js', 'components.js', 'panels.js', 'settings-panel.js']
  .map(source).join('\n');
const css = product + legacy + responsive;
const values = property => [...css.matchAll(new RegExp(`(?<![\\w-])${property}\\s*:\\s*([^;}]+)`, 'g'))]
  .map(match => match[1].trim());
const unique = property => new Set(values(property));
const tokenBlocks = product.match(/:root(?:\[data-theme=['"]dark['"]\])?\s*\{[^{}]*\}/g) || [];
// The specified brand ramp plus dark, status and chart palettes exceed 20
// swatches. Keep their exact approved values and forbid literals in rules.
const stylesWithoutTokens = tokenBlocks.reduce((text, block) => text.replace(block, ''), product) + legacy + responsive;
const cssClasses = new Set([...css.matchAll(/\.([A-Za-z_][\w-]*)/g)].map(match => match[1]));
const classReferenced = name => new RegExp(`(?<![\\w-])${name}(?![\\w-])`).test(scripts);
const unusedClasses = [...cssClasses].filter(name => !classReferenced(name)
  && !/^(fade|slide|toast)-(enter|leave)-(active|from|to)$/.test(name));
const shadowValues = unique('box-shadow');
const radiusValues = unique('border-radius');
const stylesheetLines = [product, legacy, responsive]
  .reduce((total, sheet) => total + sheet.split(/\r?\n/).length, 0);

const checks = [
  [product.includes('@layer legacy, legacy-responsive, product, product-responsive;')
    && product.includes("@import url('./legacy.css') layer(legacy);")
    && product.includes("@import url('./responsive.css');"), '样式层必须让产品规则优先于兼容规则'],
  [(product.match(/:root\s*\{/g) || []).length === 1 && !/:root\s*\{/.test(legacy + responsive), '品牌令牌只能有一个默认定义'],
  [product.includes('--brand-600: #0a47a8') && product.includes('--brand-700: #003078'), '品牌蓝令牌必须与标识一致'],
  [!/(?:#(?:6366f1|a78bfa|8b5cf6|ec4899|f472b6))\b/i.test(css), '样式中仍有旧版紫粉主色'],
  [!css.includes('!important'), '样式中不得用 !important 覆盖冲突'],
  [!stylesWithoutTokens.match(/#[0-9a-fA-F]{3,8}\b/g), '硬编码颜色只能定义在令牌块'],
  [unique('font-size').size <= 9, '字号超过九级'],
  [[...unique('font-weight')].every(value => ['400', '500', '600', '700'].includes(value)), '字重只能使用 400/500/600/700'],
  [[...radiusValues].every(value => value === '0' || /^var\(--radius(?:-sm|-lg|-xl|-full)?\)$/.test(value)), '圆角必须使用五个令牌或直角'],
  [[...shadowValues].every(value => value === 'none' || /^var\(--sh-(?:1|2|3|focus)\)$/.test(value)), '阴影必须使用四个令牌'],
  [(css.match(/(?:linear|radial)-gradient\s*\(/g) || []).length <= 3, '渐变数量超过设计约束'],
  [(css.match(/@media\b/g) || []).length <= 4, '媒体查询必须收敛在四个块内'],
  [stylesheetLines <= 1800, `三个样式文件合计 ${stylesheetLines} 行，超过 1800 行上限`],
  [unusedClasses.length <= 12, `仍有 ${unusedClasses.length} 个未引用类名：${unusedClasses.join(', ')}`],
  [!/(?:window\.)?(?:prompt|confirm)\s*\(/.test(scripts), '业务流程中不得使用浏览器原生输入与确认弹窗'],
  [product.includes(':focus-visible'), '键盘焦点必须可见'],
  [!/@font-face\b/.test(css) && !/https?:\/\/[^)]*\.(?:woff2?|ttf|otf)/i.test(css), '字体不得依赖远程文件'],
  [['fade', 'slide', 'toast'].every(name =>
    ['enter-active', 'enter-from', 'leave-active', 'leave-to'].every(suffix =>
      legacy.includes(`.${name}-${suffix}`))), 'Vue 运行时过渡类必须保留'],
];

const failed = checks.filter(([passes]) => !passes).map(([, message]) => message);
if (failed.length) {
  for (const message of failed) console.error(`UI check failed: ${message}`);
  process.exitCode = 1;
} else {
  console.log('Verified brand, design tokens, live classes, dialog, focus and transition invariants.');
}
