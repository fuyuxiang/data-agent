# 产品截图维护

[返回 README](../README.md) · [开发指南](development.md)

README 截图来自本仓库的真实前后端，使用 `POST /api/demo/seed` 生成的合成零售数据。生成脚本创建隔离的临时数据库和文件目录，不读取现有 `storage/`，也不配置外部模型或拦截 API 返回值。

| 文件 | 界面与内容 |
| --- | --- |
| [workbench.png](assets/screenshots/workbench.png) | 工作台、提问入口和演示数据推荐问题。 |
| [metric-trial.png](assets/screenshots/metric-trial.png) | 销售额定义、区域维度选择，以及真实指标查询生成的图表与数据表。 |
| [agents.png](assets/screenshots/agents.png) | 管理后台的智能体编辑与发布界面。 |
| [capture.json](assets/screenshots/capture.json) | 数据来源、页面路径、截图视口和外部模型使用状态。 |

截图用于展示界面与确定性指标查询。它们不构成模型分析、真实经营数据或生产规模的验证证据。

## 重新生成

先按照[开发指南](development.md)安装 Python 依赖和 Node.js 20+，再在仓库根目录执行：

```bash
npm ci
npx playwright install chromium
npm run build
PYTHON=.venv/bin/python node scripts/capture_readme.mjs
```

Windows PowerShell 的最后一步使用：

```powershell
$env:PYTHON = ".venv\Scripts\python.exe"
node scripts/capture_readme.mjs
```

脚本启动仅监听回环地址的临时 Flask 实例，在页面加载、指标试算和图表就绪后截图，检查页面异常和失败的 HTTP 请求，退出时停止实例并清理临时数据。演示时间窗口随首次载入月份移动，截图中的金额与时间也会相应变化。

同一脚本还将 [architecture.svg](assets/architecture.svg) 渲染为 [architecture.png](assets/architecture.png)。架构图以 SVG 为维护源，PNG 用于不支持 SVG 的查看环境。

脚本在 `.tmp/readme-review/` 生成桌面和窄屏的本地 Markdown 预览，检查本地图片加载与页面溢出。预览使用项目内置的 Markdown 解析器和简化样式，供排版复核；最终显示以代码托管平台的渲染为准。该目录不提交到 Git。

更新 README 前应检查图片中的文字、选中状态、数据范围、单位和图表是否完整，并确认图片说明与截图一致。
