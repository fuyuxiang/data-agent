# Advanced Data Agent 验收矩阵

生成依据：`CODEX_DATA_AGENT_FINAL_V3.md`。每个规范 ID 恰好一行。

实现与验证状态分列：`IMPLEMENTED` 表示代码/入口已落盘；`PASS_LOCAL` 表示本地可执行自动化已通过；`BLOCKED_EXTERNAL` 表示验证入口已完成，但缺真实模型、容器/参考集群、SMTP、目标平台、迁移演练或规模授权证据。
外部阻塞绝不等同失败，也绝不记作 PASS。

| ID | 实现状态 | 验证状态 | 需求摘要 | 主要证据 |
|---|---|---|---|---|
| R01 | IMPLEMENTED | PASS_LOCAL | 按范围复用、真实接入并完成等价替换 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R02 | IMPLEMENTED | PASS_LOCAL | 完整模型协议与自研决策循环 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R03 | IMPLEMENTED | PASS_LOCAL | 任务契约、持续规划与假设管理 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R04 | IMPLEMENTED | PASS_LOCAL | 持久化运行、等待、恢复、取消 | backend/services/data_plane/trino.py; backend/services/data_plane/livy.py |
| R05 | IMPLEMENTED | PASS_LOCAL | 统一工具执行、参数、权限与预算 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R06 | IMPLEMENTED | PASS_LOCAL | 远端计算优先、完整数据语义与受控多源分析 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R07 | IMPLEMENTED | PASS_LOCAL | 数据目录、语义定义与来源代码理解 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R08 | IMPLEMENTED | BLOCKED_EXTERNAL | 有界Python与真正远端分布式分析 | deploy/sandbox/; backend/services/data_plane/sandbox.py; backend/services/data_plane/sandbox_client.py |
| R09 | IMPLEMENTED | PASS_LOCAL | 独立验证和不可绕过的完成门禁 | backend/services/validation/; backend/services/results/manifests.py |
| R10 | IMPLEMENTED | PASS_LOCAL | 证据图、可复现成果和统一渲染 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R11 | IMPLEMENTED | PASS_LOCAL | 上下文工程、文档证据与多模态回退 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R12 | IMPLEMENTED | PASS_LOCAL | 受治理的记忆与 Skills | backend/services/skills.py; backend/api/catalog.py; frontend/src/panels.js |
| R13 | IMPLEMENTED | PASS_LOCAL | 按需多 Agent 与既有工作流统一执行 | backend/services/teams.py; backend/services/workflows.py |
| R14 | IMPLEMENTED | PASS_LOCAL | 增量分析、历史复现和版本失效 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R15 | IMPLEMENTED | BLOCKED_EXTERNAL | 生产授权、同空间细粒度隔离与安全 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R16 | IMPLEMENTED | PASS_LOCAL | 真实前端、API 与兼容体验 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R17 | IMPLEMENTED | PASS_LOCAL | 自动化、MCP、Hook 与外部交付 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R18 | IMPLEMENTED | BLOCKED_EXTERNAL | 观测、成本和健康检查 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R19 | IMPLEMENTED | BLOCKED_EXTERNAL | 全链路测试、真实评估与防回归 | backend/services/results/rendering.py; backend/api/delivery.py |
| R20 | IMPLEMENTED | BLOCKED_EXTERNAL | 部署、迁移、交付和完整验收 | backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py |
| R21 | IMPLEMENTED | PASS_LOCAL | 旧实现退役、无关代码删除与仓库收敛（与功能同级必交） | scripts/audit_repository.py; CLEANUP_REPORT.md |
| F01 | IMPLEMENTED | PASS_LOCAL | §1.4、§4；文件第10、12页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F02 | IMPLEMENTED | PASS_LOCAL | §4.1、§6.1；文件第14、23页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F03 | IMPLEMENTED | PASS_LOCAL | §1.4、§6.1；文件第10、23—24页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F04 | IMPLEMENTED | PASS_LOCAL | §5.3；文件第20页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F05 | IMPLEMENTED | PASS_LOCAL | §5.3二期补充；文件第21页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F06 | IMPLEMENTED | PASS_LOCAL | §5.4；文件第22页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F07 | IMPLEMENTED | PASS_LOCAL | §5.5；文件第22—23页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F08 | IMPLEMENTED | PASS_LOCAL | §5.1.1；文件第19页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F09 | IMPLEMENTED | PASS_LOCAL | §5.1.1；文件第19页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F10 | IMPLEMENTED | PASS_LOCAL | §5.1.2；文件第20页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F11 | IMPLEMENTED | PASS_LOCAL | §5.2；文件第20页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F12 | IMPLEMENTED | PASS_LOCAL | §4.2；文件第15页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F13 | IMPLEMENTED | PASS_LOCAL | §4.3；文件第16页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F14 | IMPLEMENTED | PASS_LOCAL | §4.3；文件第16页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F15 | IMPLEMENTED | PASS_LOCAL | §4.4、§6.2；文件第16、26页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F16 | IMPLEMENTED | PASS_LOCAL | §4.4；文件第16页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F17 | IMPLEMENTED | BLOCKED_EXTERNAL | §4.5；文件第17页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F18 | IMPLEMENTED | PASS_LOCAL | §4能力表、§4.5；文件第13、17页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F19 | IMPLEMENTED | BLOCKED_EXTERNAL | §4.6；文件第18页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F20 | IMPLEMENTED | PASS_LOCAL | §4.6；文件第18页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F21 | IMPLEMENTED | BLOCKED_EXTERNAL | §4.7.1；文件第18页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F22 | IMPLEMENTED | PASS_LOCAL | §4.7.2；文件第19页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F23 | IMPLEMENTED | BLOCKED_EXTERNAL | §4.7.3；文件第19页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F24 | IMPLEMENTED | PASS_LOCAL | §4.7.4；文件第19页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F25 | IMPLEMENTED | PASS_LOCAL | §6.2；文件第26页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F26 | IMPLEMENTED | PASS_LOCAL | §6.2；文件第26页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F27 | IMPLEMENTED | PASS_LOCAL | §1.4、§6.3；文件第10、28页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F28 | IMPLEMENTED | PASS_LOCAL | §6.3；文件第29页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F29 | IMPLEMENTED | PASS_LOCAL | §6.3；文件第29—30页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F30 | IMPLEMENTED | PASS_LOCAL | §6.3；文件第30页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F31 | IMPLEMENTED | PASS_LOCAL | §6.3；文件第28页 | backend/services/results/rendering.py; tests/test_advanced_agent.py |
| F32 | IMPLEMENTED | PASS_LOCAL | §6.3；文件第28页 | backend/api/delivery.py; backend/services/results/rendering.py |
| F33 | IMPLEMENTED | BLOCKED_EXTERNAL | §6.3；文件第28页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| F34 | IMPLEMENTED | BLOCKED_EXTERNAL | §6.4；文件第31页 | frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py |
| A01 | IMPLEMENTED | PASS_LOCAL | 默认完成一次四段需求理解确认后，明确单指标问题走短工具路径，真实远程SQL/指标取数；不强制所有分析阶段。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A02 | IMPLEMENTED | PASS_LOCAL | 指标分母有实质歧义时澄清；得到回答后续接同一任务契约。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A03 | IMPLEMENTED | PASS_LOCAL | 初始假设被数据否定，新增/关闭任务并记录计划修订，而不是坚持原解释。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A04 | IMPLEMENTED | PASS_LOCAL | 计划依赖环或并发旧版本更新被拒绝，合法计划仍可执行。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A05 | IMPLEMENTED | PASS_LOCAL | 分块工具 JSON、多个工具调用、Unicode 参数与 usage 被正确解析。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A06 | IMPLEMENTED | PASS_LOCAL | token 截断或网络断流时不执行半条敏感工具调用，不标分析完成。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A07 | IMPLEMENTED | PASS_LOCAL | provider 拒绝、空输出和工具参数 schema 错误产生正确状态与有限处理。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A08 | IMPLEMENTED | BLOCKED_EXTERNAL | 超长上下文压缩后保留目标、已确认定义、失败检查及完整工具配对。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A09 | IMPLEMENTED | PASS_LOCAL | HTTP/SSE 断开不取消任务；事件游标重连不重复提交查询。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A10 | IMPLEMENTED | PASS_LOCAL | 进程在保存 decision 后、执行前终止；重启从已保存动作继续。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A11 | IMPLEMENTED | PASS_LOCAL | 外部 SQL/作业已提交但确认未保存时中断；恢复核验，不盲重跑。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A12 | IMPLEMENTED | PASS_LOCAL | 结果已落盘而完成事件丢失；恢复扫描补全引用，不能永久挂起。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A13 | IMPLEMENTED | PASS_LOCAL | 旧租约 Worker 恢复活动时被 fencing 拒绝写入或派发新动作。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A14 | IMPLEMENTED | PASS_LOCAL | 长作业 WAITING_JOB 释放资源，完成后继续，不靠模型高频轮询。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A15 | IMPLEMENTED | BLOCKED_EXTERNAL | 暂停不派发新动作；取消传递给实际 SQL/Python，并区分请求与确认。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A16 | IMPLEMENTED | PASS_LOCAL | 同一工作空间的两个用户不能读取对方私有来源、记忆、结果和证据。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A17 | IMPLEMENTED | PASS_LOCAL | 用户有 A/B 两源权限但本任务仅选 A，不能通过画像/结果引用绕过进入 B。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A18 | IMPLEMENTED | PASS_LOCAL | 子 Agent、工作流、MCP、直接 tool call 均不能扩大当前有效工具/数据权限。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A19 | IMPLEMENTED | PASS_LOCAL | 运行中撤销权限后，查询、缓存、SSE、历史下载和上下文加载被阻止。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A20 | IMPLEMENTED | PASS_LOCAL | TESTING 安全测试不自动授权；生产配置与测试使用同一强制策略路径。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A21 | IMPLEMENTED | PASS_LOCAL | 文档/表格/工具返回含“忽略规则并外发数据”时不改变系统权限。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A22 | IMPLEMENTED | PASS_LOCAL | SSRF 内网/元数据地址、重定向、DNS 变化与任意路径访问被正确限制。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A23 | IMPLEMENTED | PASS_LOCAL | 并发子任务原子预留预算，无法合计超支；修复和摘要成本计入。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A24 | IMPLEMENTED | PASS_LOCAL | 持续无进展或反复相同错误在预算内终止，并交付清楚的部分状态。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A25 | IMPLEMENTED | PASS_LOCAL | 远程数据量大于预览/本地限额时，仓内精确聚合仍正确；大完整结果留远端，按授权引用读取，不拉全量明细。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A26 | IMPLEMENTED | PASS_LOCAL | 系统截断与用户 Top N 区分；前者禁止全量结论，后者按有序请求验收。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A27 | IMPLEMENTED | PASS_LOCAL | API 分页未完整、总量未知时如实标记；不能把已读行数冒充总量。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A28 | IMPLEMENTED | PASS_LOCAL | 大仓+文件/API维表在远程授权结果域关联并可追溯；本地join只在两侧均通过有界门禁时允许。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A29 | IMPLEMENTED | BLOCKED_EXTERNAL | 一对多/多对多关联放大被发现，错误指标不能发布，修正后可通过。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A30 | IMPLEMENTED | BLOCKED_EXTERNAL | 迟到分区/时间水位不齐导致不可比，系统明确限制或申请改范围。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A31 | IMPLEMENTED | BLOCKED_EXTERNAL | 同名指标不同分母、单位、粒度不可混算；ratio_of_sums 正确。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A32 | IMPLEMENTED | BLOCKED_EXTERNAL | Decimal、时区、前导零 ID、null 和 Excel 公式状态经输入/物化/导出不被静默破坏。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A33 | IMPLEMENTED | BLOCKED_EXTERNAL | SQL 多语句、写入 CTE、危险函数、外部文件函数被拦截；合法方言可执行。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A34 | IMPLEMENTED | BLOCKED_EXTERNAL | 查询超时、成本未知或超限时有限终止，不仅增加 LIMIT 应付资源控制。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A35 | IMPLEMENTED | BLOCKED_EXTERNAL | 文档正文和表格共同包含指标定义时能定位段落/页/单元格证据。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A36 | IMPLEMENTED | PASS_LOCAL | 无可用文本的 PDF 页按需渲染并正确标识视觉证据；无视觉能力不编造内容。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A37 | IMPLEMENTED | PASS_LOCAL | 来源 SQL/Python 说明变化后 catalog stale/语义候选更新，不自动覆盖发布定义。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A38 | IMPLEMENTED | BLOCKED_EXTERNAL | 真实隔离Python处理小完整数据；另有真实远程Spark处理大型引用，均产生可用结果而非本地替身。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A39 | IMPLEMENTED | BLOCKED_EXTERNAL | Python 越界网络/宿主文件/凭据/超内存/超时/过量输出测试实际触发边界。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A40 | IMPLEMENTED | PASS_LOCAL | sandbox 不可用时 fail-closed，绝不偷偷调用主进程 exec。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A41 | IMPLEMENTED | PASS_LOCAL | 预测采用时间切分和基线，泄漏样例被拦截；弱模型不伪装优于基线。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A42 | IMPLEMENTED | PASS_LOCAL | 样本不足、方法不适用或区间不可靠时限制输出，不制造确定预测。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A43 | IMPLEMENTED | PASS_LOCAL | 贡献/相关/模型解释不被命名为因果；有随机实验数据时效应分析可运行。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A44 | IMPLEMENTED | PASS_LOCAL | 工具均成功但数据/语义验证失败，finalize_analysis 拒绝正式发布。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A45 | IMPLEMENTED | PASS_LOCAL | 模型尝试直接输出/导出/发送未经验证结论，所有正式出口门禁仍生效。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A46 | IMPLEMENTED | PASS_LOCAL | 关键数字从 Claim 点击到真实 Dataset/查询/定义/验证，错误或失效引用被阻止。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A47 | IMPLEMENTED | PASS_LOCAL | 摘要四KPI、四图看板、两个Word与四图拼合PNG的数字一致；文件可打开，明细服务器分页，PNG非空白。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A48 | IMPLEMENTED | BLOCKED_EXTERNAL | Notebook/复现包无凭据，能在指定环境对保存数据重算关键结果。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A49 | IMPLEMENTED | PASS_LOCAL | 只改图表类型不重跑 SQL；新增维度只重算必要依赖。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A50 | IMPLEMENTED | PASS_LOCAL | 改口径或数据版本使依赖结果失效，旧图表不得继续标为最新已验证。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A51 | IMPLEMENTED | PASS_LOCAL | replay/reproduce/refresh 区别可观察；无历史快照不以最新数据冒充复现。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A52 | IMPLEMENTED | PASS_LOCAL | memory 个人纠正不会污染其他用户或正式全局定义；删除后检索缓存失效。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A53 | IMPLEMENTED | PASS_LOCAL | Skill 候选测试失败不可发布；通过、审批、版本选择和回滚端到端可用。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A54 | IMPLEMENTED | PASS_LOCAL | 相关旧经验因语义版本改变而失效，不盲目复用。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A55 | IMPLEMENTED | PASS_LOCAL | 并行子 Agent 受父预算和收窄权限限制；证据冲突时执行验证而非多数投票。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A56 | IMPLEMENTED | PASS_LOCAL | 既有工作流暂停/审批/fork/重试/manifests 功能回归通过且使用统一执行层。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A57 | IMPLEMENTED | PASS_LOCAL | 定时/更新触发使用预授权契约，重复事件去重，撤销授权后停止访问。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A58 | IMPLEMENTED | BLOCKED_EXTERNAL | 本地测试邮箱/通知端实际收到准确版本附件；未知发送状态不重复发。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A59 | IMPLEMENTED | PASS_LOCAL | 目标范围内 API、聊天、连接器、图表、导出、团队、知识和本地打包回归通过；经决议退役功能有明确删除/迁移证据，不能按旧功能总数无条件全部保留。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A60 | IMPLEMENTED | BLOCKED_EXTERNAL | 老数据库迁移后历史可读；备份恢复后新任务/产物/证据关联完整。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A61 | IMPLEMENTED | PASS_LOCAL | 真实浏览器覆盖新建、等待、控制、追问、证据点击、导出及权限拒绝。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A62 | IMPLEMENTED | BLOCKED_EXTERNAL | release 检查发现必需测试 skip/未运行/没有模型凭据时非零并列 BLOCKED。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A63 | IMPLEMENTED | BLOCKED_EXTERNAL | 真实模型评估按固定任务和版本记录至少规定次数，发布数值与金标比较。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| A64 | IMPLEMENTED | PASS_LOCAL | 单节点重复实例启动被拒绝；重启恢复通过但文档不宣称未实现的多节点多活。 | tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report |
| W01 | IMPLEMENTED | BLOCKED_EXTERNAL | 真实Trino coordinator+至少两个worker执行分区列式数据SQL；保存worker/stage证据，mock或单机DuckDB不算。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W02 | IMPLEMENTED | BLOCKED_EXTERNAL | 真实Spark driver+至少两个executor通过Livy批提交、状态、结果、取消；Spark local和伪REST响应不算。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W03 | IMPLEMENTED | BLOCKED_EXTERNAL | 注册大目录不在请求内全仓遍历/COUNT；分页、授权过滤、懒加载和增量watermark可用；明确合成元数据测试范围。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W04 | IMPLEMENTED | BLOCKED_EXTERNAL | 同一任务在不同仓库存量下，模型上下文、本地结果输入及控制面峰值内存受上限约束；不是按源表字节数线性下载。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W05 | IMPLEMENTED | BLOCKED_EXTERNAL | 精确指标全范围远端聚合后回传小结果，匹配独立参考SQL；不拉原始明细、不静默抽样。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W06 | IMPLEMENTED | BLOCKED_EXTERNAL | 生成超本地大小的大结果，真实写入远端授权表/对象区；控制面仅记录ref和有限预览，结果随后能继续远端计算。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W07 | IMPLEMENTED | BLOCKED_EXTERNAL | 大remote ref流入旧source_frames/load_result_frame/run_analysis/profile路径被阻止或正确路由，不能无界转换Pandas。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W08 | IMPLEMENTED | BLOCKED_EXTERNAL | 本地入口按行/字节/单值大小/解码内存/累计传输双层门禁；超大字符串、宽表、Parquet解压膨胀与unknown大小用例拒绝无界读取。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W09 | IMPLEMENTED | BLOCKED_EXTERNAL | 数据库巨大规模但查询无有效范围、join爆炸或估计unknown时，准入按策略阻止或进入有原生硬限制的批队列；不能LIMIT一加就放行。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W10 | IMPLEMENTED | BLOCKED_EXTERNAL | 原生资源上限实际终止超扫描/时长/资源作业；记录取消延迟和实际消耗，不能只改本地状态。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W11 | IMPLEMENTED | BLOCKED_EXTERNAL | EXPLAIN/dry-run不执行原查询，EXPLAIN ANALYZE不得作为默认预检；估计扫描行与字节unsupported显示unknown。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W12 | IMPLEMENTED | BLOCKED_EXTERNAL | 支持下推和不支持下推的查询分别核对plan/实际统计；不因为写了WHERE或跨catalog就声称已完全下推。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W13 | IMPLEMENTED | BLOCKED_EXTERNAL | 远程多源join有传输预算、粒度和ACL检查；无共同计算域时拒绝大规模本地fallback。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W14 | IMPLEMENTED | BLOCKED_EXTERNAL | 小文件/API维表安全写scratch供远端join；模型不能改任意catalog/path或向原数仓表写入。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W15 | IMPLEMENTED | BLOCKED_EXTERNAL | 结果区写入、TTL清理、snapshot引用、半成品和重复attempt处理正确；源表只读权限始终不变。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W16 | IMPLEMENTED | BLOCKED_EXTERNAL | Trino客户端nextUri由后台执行组件维护，浏览器断线不影响；后台中断触发实际引擎限制时如实FAILED/UNKNOWN而非伪恢复。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W17 | IMPLEMENTED | BLOCKED_EXTERNAL | Trino query_id已失效、Spark/Livy状态过期或结果未确认时先reconcile；同快照重跑用新attempt并计入重复扫描，不恰好一次虚假承诺。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W18 | IMPLEMENTED | BLOCKED_EXTERNAL | 远端完整结果和源快照失效后历史回看仍可读已保存报告，精确重算明确阻塞；不悄悄用latest。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W19 | IMPLEMENTED | BLOCKED_EXTERNAL | 原始存储路径读取不能绕过数仓行列权限；Spark driver/executor身份、数据前缀、网络、proxyUser边界真实负面测试通过。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W20 | IMPLEMENTED | BLOCKED_EXTERNAL | 代码使用collect/toPandas/本地fetchall拉大数据时在实际边界拒绝或作业受限终止；仅静态字符串检查不能算全部通过。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W21 | IMPLEMENTED | BLOCKED_EXTERNAL | 远端数据质量检查共享聚合/分区结果，避免每规则全表重扫；展示验证范围/覆盖，metadata估计不能当精确唯一性证明。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W22 | IMPLEMENTED | BLOCKED_EXTERNAL | 精确、近似、样本、Top N、系统截断、API分页未齐分别保留独立状态；错误精度/范围不能发布。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W23 | IMPLEMENTED | BLOCKED_EXTERNAL | 模型/摘要/反思/子Agent/预检/验证/重试/物化/导出合计预算原子结算；队列背压不引发重复扫描风暴。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W24 | IMPLEMENTED | BLOCKED_EXTERNAL | 四KPI/四图共享适用远端聚合版本；切换Tab、换图、导出PNG不再次执行无关大扫描。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W25 | IMPLEMENTED | BLOCKED_EXTERNAL | 明细表稳定版本分页和受控远端导出可用；客户端不拉巨大JSON、深分页不默认反复全量OFFSET。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W26 | IMPLEMENTED | BLOCKED_EXTERNAL | Decimal、时间/时区、前导零、空值与高基数维度在Trino->Iceberg/结果区->Spark->小结果->导出链路无静默破坏。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W27 | IMPLEMENTED | BLOCKED_EXTERNAL | local/warehouse模式清晰；多SQLite所有者拒绝；引擎故障绝不fallback到本地处理全仓，缺Spark不伪装完成分布式分析。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W28 | IMPLEMENTED | BLOCKED_EXTERNAL | 仓级测试报告分别记录存量、实际扫描、shuffle/输出、并发、冷暖缓存、时间、资源、费用；不能把虚拟scale-factor或1EB字段当实测。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W29 | IMPLEMENTED | BLOCKED_EXTERNAL | target-platform profile未配置真实目标或版本时BLOCKED，不自动用参考Trino替代后PASS；各适配器能力矩阵与实际证据一致。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| W30 | IMPLEMENTED | BLOCKED_EXTERNAL | 功能release与PB/EB-scale结论独立；任意F/R缺实现、A/W必测失败/跳过不能完整发布；未授权不得在生产扫大表做性能测试。 | scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md |
| C01 | IMPLEMENTED | PASS_LOCAL | 完成 baseline、功能/入口/依赖清单和 KEEP/REFACTOR/MERGE/REMOVE/COMPAT 决议；每项删除有具体依据，不以路径名猜测。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C02 | IMPLEMENTED | PASS_LOCAL | 对话、团队、子任务实际进入同一 AgentLoop/ModelAdapter；旧独立多轮工具循环不再存在于可执行源码或打包产物。摘要模型调用不误判为第二内核。 | backend/agent/loop.py; scripts/audit_repository.py |
| C03 | IMPLEMENTED | PASS_LOCAL | 工作流、调度、兼容 API、MCP 和自定义工具均使用统一授权/预算/Action/发布边界；直接调用旧入口不能绕过。 | backend/services/workflows.py; backend/agent/tools.py |
| C04 | IMPLEMENTED | PASS_LOCAL | 可恢复任务使用 typed JobSpec；旧请求内执行、内存取消真值源、不可恢复闭包提交路径已替换；重启与取消回归通过。 | backend/services/jobs.py; backend/core/database.py |
| C05 | IMPLEMENTED | PASS_LOCAL | 远端数据引用经过所有现有查询/画像/分析/导出入口仍不隐式变为大 DataFrame；有界文件分析保持可用，旧泛化 fallback 已删除。 | backend/services/datasets.py; backend/services/data_plane/contracts.py |
| C06 | IMPLEMENTED | PASS_LOCAL | 没有模型配置时保留必要浏览/历史/诊断，但不会落入伪 Agent 完成分支；确定性任务也受统一策略与成果状态约束。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C07 | IMPLEMENTED | PASS_LOCAL | 通用 draw.io 创作退役后，工具/路由/菜单/独占包/资源/构建规则全部清理；必要旧成果可授权回看，新的证据展示仍可用。 | CLEANUP_MANIFEST.json; frontend/src/; scripts/build.mjs |
| C08 | IMPLEMENTED | PASS_LOCAL | GPU/SSH preflight 与旧模型部署功能按明确依赖决议移除或合并；不能作为 Spark 验收替身；实际远端数据作业链未被误删。 | CLEANUP_MANIFEST.json; backend/services/data_plane/ |
| C09 | IMPLEMENTED | PASS_LOCAL | 已退役的宿主文件改写、通用 Shell/Git、外部业务 CRUD 或 Agent 自改 Hook 不在 schema/分派/别名/MCP/配置开关中复活。 | backend/services/agent_tools.py; backend/services/workspace_tools.py |
| C10 | IMPLEMENTED | PASS_LOCAL | 删除 TESTING 授权旁路；显式测试角色在真实策略下分别允许/拒绝，原安全断言没有因清理被移除。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C11 | IMPLEMENTED | PASS_LOCAL | 退役内部端点不再注册；有合同的旧端点是受同等策略的薄适配或明确410。不存在返回空 success 的兼容实现。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C12 | IMPLEMENTED | PASS_LOCAL | 前端所有菜单、事件、按钮、API 调用和导出链接指向存活入口；真实浏览器检查新分析、历史、四卡四图与邮件，不仅首页能打开。 | tests/browser/analysis.spec.mjs; playwright.config.mjs |
| C13 | IMPLEMENTED | PASS_LOCAL | 静态分析对 Flask 路由、Job handler、Skill 动态名称、pytest fixture、平台入口不会误删；每个必要例外有精确符号和有效入口测试。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C14 | IMPLEMENTED | BLOCKED_EXTERNAL | 移除仅服务退役功能的直接 Python/JS 依赖与可选项，按工具重新生成锁文件；在干净环境安装并测试，不能依赖本机残留包。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C15 | IMPLEMENTED | PASS_LOCAL | 废弃环境变量、常量、配置页与 feature flag 已移除或有期限迁移提示；旧参数不能重新激活不安全实现。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C16 | IMPLEMENTED | PASS_LOCAL | README、样例配置、部署、CLI 帮助和运维说明与实际范围一致；旧任务书不再作为有效规范，历史文件仅明确标记且不打进运行产物。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C17 | IMPLEMENTED | BLOCKED_EXTERNAL | 升级旧库/旧成果/保存的Workflow和Skill引用真实测试；用户记录和上传文件未被代码清理误删；旧未验证结果保持相应标签。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C18 | IMPLEMENTED | PASS_LOCAL | 旧实现测试重写或退役均有理由与新断言映射；有效数据/安全/恢复用例没有被删掉或改恒真；覆盖率规则未降低。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C19 | IMPLEMENTED | BLOCKED_EXTERNAL | 从干净输出构建并检查前端、镜像与桌面包（实际支持的目标）；退役源码/资源不被COPY或通配符重新打包。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C20 | IMPLEMENTED | BLOCKED_EXTERNAL | 仍分发依赖的 LICENSE/NOTICE 与资源哈希保留正确；无关资产删除不造成许可证缺失或必需离线资源404。 | THIRD_PARTY_NOTICES.md; frontend/vendor/SHA256SUMS; scripts/check-vendor.mjs |
| C21 | IMPLEMENTED | PASS_LOCAL | Git跟踪源码不含为规避清理而搬入的 legacy/archive/old/bak 复制品、长注释实现或隐藏 fallback；合法兼容项有明确清单。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C22 | IMPLEMENTED | PASS_LOCAL | 清理脚本仅审计不擅自删数据；真实差异显示用户未提交内容、数据目录、凭据、原始文档与远端表未被误清理。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
| C23 | IMPLEMENTED | PASS_LOCAL | 故意注入残留工具注册/已删除 import/过期例外/第二执行路径的审计负样本会失败；不能仅生成一份永远PASS的报告。 | scripts/audit_repository.py --self-test-negative |
| C24 | IMPLEMENTED | BLOCKED_EXTERNAL | repository-audit 与 release 聚合实际执行全部必要检查；缺依赖/未运行/NEEDS_REVIEW阻塞准确报告，不能文件存在即通过；清理统计与git/build证据一致。 | scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report |
