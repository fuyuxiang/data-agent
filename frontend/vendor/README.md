# Vendored browser dependencies

These files are pinned so production builds do not depend on a public CDN at
runtime. Update them deliberately and run the backend and frontend test suites
after every change.

| File | Package | Version | Source |
| --- | --- | --- | --- |
| `vue.global.prod.js` | Vue | 3.5.41 | `https://cdn.jsdelivr.net/npm/vue@3.5.41/dist/vue.global.prod.js` |
| `echarts.min.js` | Apache ECharts | 6.1.0 | `https://cdn.jsdelivr.net/npm/echarts@6.1.0/dist/echarts.min.js` |
| `marked.min.js` | Marked | 18.0.7 | `https://unpkg.com/marked@18.0.7/lib/marked.umd.js` |
| `purify.min.js` | DOMPurify | 3.4.14 | `https://cdn.jsdelivr.net/npm/dompurify@3.4.14/dist/purify.min.js` |

SHA-256 digests are recorded in `SHA256SUMS` and are verified by CI.
