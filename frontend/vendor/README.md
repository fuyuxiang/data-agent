# Vendored browser dependencies

These files are pinned so production builds do not depend on a public CDN at
runtime. Update them deliberately and run the backend and frontend test suites
after every change.

| File | Package | Version | Source |
| --- | --- | --- | --- |
| `vue.global.prod.js` | Vue | 3.5.41 | `https://cdn.jsdelivr.net/npm/vue@3.5.41/dist/vue.global.prod.js` |
| `echarts.min.js` | Apache ECharts | 6.1.0 | `https://cdn.jsdelivr.net/npm/echarts@6.1.0/dist/echarts.min.js` |
| `echarts-china.min.js` | Apache ECharts China map | 4.9.0 asset | `https://raw.githubusercontent.com/apache/echarts/4.9.0/map/js/china.js` |
| `marked.min.js` | Marked | 18.0.7 | `https://unpkg.com/marked@18.0.7/lib/marked.umd.js` |
| `purify.min.js` | DOMPurify | 3.4.14 | `https://cdn.jsdelivr.net/npm/dompurify@3.4.14/dist/purify.min.js` |

SHA-256 digests are recorded in `SHA256SUMS` and are verified by CI.

The bundled China map is the historical Apache ECharts 4.9.0 dataset and is
provided under Apache-2.0. Operators publishing maps in China remain
responsible for using an approved, current map source and any required map
review number; replace this asset during deployment where applicable.
