# 经纬分析工作台

经纬分析工作台是一套 Vue 3 + Python 的本地优先数据分析系统。它把数据接入、只读查询、统计建模、自然语言分析、知识检索、流程审批、多顾问协作、看板和 Office 成果交付放在同一工作空间中。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

浏览器访问 `http://127.0.0.1:5001`。

如果当前 Python 环境已经包含依赖，也可以直接运行 `python app.py`。生产环境使用已锁定的 `requirements.lock` 和 Waitress，完整配置、备份和容量边界见 [生产运行手册](docs/PRODUCTION.md)。

## 前端

前端使用 Vue 3 Global Build 与原生 ES Modules，不依赖在线 CDN。ECharts、Marked 和 DOMPurify 均随项目本地提供，因此离线也能使用。

```bash
npm run check
npm run build
```

`npm run build` 会输出可独立托管的静态前端到 `frontend/dist`。完整产品仍需要 Python API 服务。

## 首次使用

1. 在“数据目录”上传 CSV/Excel/JSON/Parquet，或连接数据库、HTTP JSON。
2. 勾选“用于当前会话”，回到“分析会话”直接提问。
3. 如需模型规划，在“系统设置 → 模型服务”添加任意 OpenAI-Compatible 配置；未配置时会使用确定性本地规划器。
4. 在“业务知识”添加指标口径与规则，在“自动化中心”固化工作流和人工审批。
5. 把查询图表加入看板，或导出 CSV、Excel、Word、PowerPoint 和独立 HTML。

## 安全默认值

- 分析查询仅允许只读 SQL。
- 清洗不覆盖原始数据。
- 外部凭据加密落库，API 不回传明文。
- 工作流审批不会自动放行。
- 归档记录可恢复；永久删除要求显式确认。

参考工程审阅见 [docs/REFERENCE_REVIEW.md](docs/REFERENCE_REVIEW.md)，架构说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，功能覆盖见 [docs/FEATURE_PARITY.md](docs/FEATURE_PARITY.md)。当前支持单节点生产运行，不代表已具备多副本高可用能力。
