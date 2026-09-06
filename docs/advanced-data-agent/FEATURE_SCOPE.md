# 功能范围与处置

| 范围 | 决议 | 实现位置/说明 |
|---|---|---|
| 自主分析、四段确认、过程、三成果 Tab | REFACTOR | `backend/agent/`、`backend/api/analyses.py`、`frontend/src/analysis-panel.js` |
| 文件/数据库/HTTP/表格连接与本地小数据 | KEEP+HARDEN | `backend/services/datasets.py`；增加真实分页与完整性传播 |
| Trino/Iceberg/Spark-Livy | ADD | `backend/services/data_plane/`、`backend/api/warehouse.py`、`deploy/warehouse/` |
| 统计/ML/时间序列方法 | KEEP | 由统一工具入口调用；远端大数据使用受信 JobSpec |
| 图表、DOCX/PPTX/XLSX/HTML | KEEP+MERGE | 兼容导出保留；正式三类成果统一从 ResultManifest 渲染 |
| 工作流版本/审批/暂停/fork/manifests | KEEP+REFACTOR | 确定性 DAG 保留，动作进入统一 ToolExecutor/预算/发布边界 |
| 团队与子 Agent | REFACTOR | 成员使用同一 AgentLoop；成员及团队汇总均须发布 |
| 知识、记忆、Skills | KEEP+GOVERN | 精确文档位置、actor scope、Skill candidate/test/publish/rollback |
| 对话旧 API | COMPAT | 只建立契约草稿，不保留第二 Agent 循环 |
| 通用 draw.io | REMOVE | UI、路由、工具、资源与构建复制均删除 |
| GPU 主机/SSH 模型部署 | REMOVE | 与 Trino/Livy 目标作业链无关；paramiko 依赖删除 |
| 宿主写改删、Shell/Git、自改 Hook | REMOVE | 不再注册，不再存在于生产工具实现 |
| 外部业务记录 CRUD/通用任务平台 | REMOVE/MERGE | 与分析无关的写操作删除；分析团队状态合并到 Run/Plan/Action |
| 用户上传、历史记录、备份恢复 | KEEP | 清理脚本只读，不删除数据目录或历史集合 |
