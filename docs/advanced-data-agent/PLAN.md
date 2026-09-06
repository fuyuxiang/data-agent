# 实施计划与完成情况

| 里程碑 | 状态 | 交付 |
|---|---|---|
| M0 基线与可实现性 | 完成 | 基线测试、范围决议、外部证据变量、参考版本固定 |
| M1 单一运行内核 | 完成 | ModelAdapter、AgentLoop、RunStore、ToolExecutor、typed JobSpec、租约与事件 |
| M2 数据与计算面 | 完成（外部实测待环境） | DatasetRef、Trino Statement、scratch CTAS、Livy Batch、Docker sandbox、分页与完整性 |
| M3 验证与成果 | 完成 | 独立规则、Claim、ResultManifest、发布门禁、统一渲染与交付 |
| M4 产品闭环 | 完成 | 四段确认、动态过程、三 Tab、四指标/四图、附件、追问、Skill 生命周期 |
| M5 收敛与本地验收 | 完成 | 退役旧内核/draw.io/GPU/宿主改写；102 项本地测试、61.49% 综合覆盖率、88% Agent 覆盖率、2 项 Chromium E2E、依赖/仓库审计通过 |
| M6 外部验收与发布 | BLOCKED | Docker/参考仓库、真实 PostgreSQL/MySQL、目标平台、授权规模、真实模型、SMTP、迁移恢复和许可审查证据尚未提供 |

发布规则：功能代码完成不自动等于环境验收通过。2026-09-06 的 `release` profile 为 17 PASS、12 BLOCKED、0 FAIL，按规则返回非零；不会因本地单元测试成功宣称参考集群、客户平台或 PB/EB 已验证。逐门禁结果见 `artifacts/verification/release.json`。
