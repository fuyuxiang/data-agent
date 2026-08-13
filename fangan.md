# Data Agent 架构审计与升级方案

> 审计基线：仓库 `main@ffd5ef3d`  
> 审计与资料截止时间：2026-08-13（Asia/Shanghai）  
> 审计范围：后端、前端、语义层、查询编译、安全、执行、Trace、Golden Set、依赖与交付工程  
> 说明：本文是改造方案，不直接修改现有业务代码。外部结论优先引用官方文档；产品预览能力均按“可参考、不可直接当稳定依赖”处理。

## 1. 结论先行

当前项目已经形成了一个方向正确、边界清楚的“可信问数 MVP”，但还不是可面向企业生产环境交付的完整 Data Agent。

最值得保留的架构决策是：**LLM 只负责把自然语言映射为结构化意图，SQL 由语义层和确定性编译器生成；Verified Query 也必须经过权限改写和执行护栏。** 这条路线与 2026 年 Snowflake Cortex Analyst、Databricks Genie、Microsoft Fabric Data Agent、Google Looker Conversational Analytics 强调的语义层、可信资产、已验证答案和独立评测方向一致，不应改回“LLM 直接生成任意 SQL”。

但是，当前实现仍存在数个阻断投产的问题：

1. **身份与会话越权风险**：请求头 `X-Username` 可伪造；复用 `conversation_id` 时没有验证会话所有者和数据集。
2. **Verified Query 存在列权限旁路风险**：固定 SQL 只校验表白名单，列脱敏只覆盖“直接列别名”，聚合或表达式引用受限列时可能绕过列策略。
3. **元数据写入没有提交边界**：请求级 SQLAlchemy Session 只 `close()`，不 `commit()`；会话、Turn、Trace、反馈、发布状态在真实请求结束后会被回滚。
4. **时间语义不可靠**：Prompt 要求模型输出绝对日期，却没有注入当前日期/时区；`clock.now()` 在非冻结状态下也永远返回当天 09:00。
5. **Golden Set 的“真实模型”和“澄清回填”并未真正闭环**：真实模式的 `intent` 被固定成 `None`，差异默认 XFAIL；follow-up 没有提交所选选项，只是再次发送原问题。
6. **查询编译覆盖面与 Intent Schema 不一致**：`trend`、`detail` 名义上存在，实际仍走聚合 SQL；多指标只使用第一个指标的时间字段；过滤值基于字符串外观猜数据类型。
7. **运行与交付工程不完整**：无 Alembic、无 CI/CD、无容器/部署文件、后端依赖无锁定、无生产级就绪探针/指标/日志规范、无依赖漏洞门禁。

综合判断：

| 维度 | 当前成熟度 | 判断 |
|---|---:|---|
| 核心架构方向 | 8/10 | 确定性编译、语义层、分阶段 Trace 的方向正确 |
| 单数据集功能闭环 | 7/10 | 简单聚合、TopN、同比环比、RLS、引证已能运行 |
| 查询正确性 | 5/10 | 边界校验、时间语义、趋势/明细、多指标口径仍有缺口 |
| 安全与权限 | 3/10 | 有 AST/RLS/列策略基础，但认证、所有权、VQ、DB 最小权限未闭环 |
| 可靠性与可恢复性 | 3/10 | 同步单请求可用，事务、幂等、取消、重试恢复、异步任务不足 |
| 评测可信度 | 4/10 | 有 Golden Set 形式，但真实模型与多轮评测存在空洞 |
| 可观测性 | 5/10 | 产品化 Trace 是亮点，但缺少标准遥测、SLO、脱敏和跨服务关联 |
| 前端产品化 | 5/10 | 三栏工作台清楚，但身份、会话恢复、结构化澄清、流式与响应式不足 |
| 部署与运维 | 2/10 | 仍是开发仓库，不具备可重复发布和生产运维能力 |

可以把它理解为：**MVP 完成度约 70%，企业生产就绪度约 35%。** 第一阶段不应扩功能，而应先完成 P0 安全、事务和评测修复。

---

## 2. 当前真实架构

### 2.1 已实现链路

```text
Vue 3 工作台
   │  /api/chat、/api/semantic、/api/trace
   ▼
FastAPI（当前是一个进程内模块化单体）
   │
   ├─ 1. Verified Query 精确文本召回
   ├─ 2. OpenAI-compatible Chat Completions → JSON 意图
   ├─ 3. 枚举解析、置信度判断、澄清
   ├─ 4. 意图 + 单数据集语义模型 → SQLGlot AST
   ├─ 5. RLS 注入 → 列脱敏 → LIMIT → AST 表白名单 → EXPLAIN
   ├─ 6. PostgreSQL 执行 → 空值/截断/量级校验
   └─ 7. 模板化答案 + 口径/时间/过滤/权限引证
          │
          └─ Conversation / Turn / TraceStage / Feedback

PostgreSQL
   ├─ agent_meta：语义、用户角色、会话、Trace、Verified Query
   └─ sample：业务样本数据
```

设计文档声称的 `api-gateway / semantic-service / query-service` 目前并不是三个可独立部署服务，而是同一个 FastAPI 应用中的模块。对当前代码规模而言，**模块化单体反而是合理形态**；在没有独立扩缩容、独立故障域或团队边界之前，不建议为了与设计图一致而拆微服务。

### 2.2 已经做对的事情

- `QueryIntent` 被当作 LLM、编译器、澄清、Trace、回放的统一契约，而不是每层发明一套结构。
- SQLGlot AST 构造和终检优于字符串拼 SQL；行级策略在编译后 AST 上注入。
- 指标聚合受 `allowed_aggregations` 硬约束，能够阻断余额、等级等字段被错误求和。
- 指标时间字段显式化，指标版本和用户/权限过滤能进入引证。
- 权限拒答使用通用文案，降低元数据泄漏。
- Trace 是可查询的数据模型而不是普通日志，并支持基于意图快照重编译。
- 测试覆盖了编译器、权限、执行、API、前端组件和 Golden Set；工程并非只有演示页面。
- “单数据集、先可信再扩展”的范围控制是正确的，避免过早引入 Join Planner、Python 沙箱和自由规划 Agent。

这些基础应保留并加强，而不是重写。

---

## 3. 已验证的代码与测试事实

### 3.1 本次实际执行结果

| 检查 | 结果 | 结论 |
|---|---|---|
| `python -m pytest -q` | 360 passed，1 failed | `QueryIntent` 缺少“聚合类意图至少一个指标”的 Schema 校验；失败用例位于 `backend/tests/intent/test_schema.py:54` |
| Golden Set（stub） | 22 pass | 只能证明当前 stub 与确定性链路的 22 个文件用例通过，不能代表真实模型完整通过 |
| `npm test -- --reporter=dot` | 112 passed | 有大量 Element Plus / router 组件未注册警告，测试挂载环境与真实应用存在差异 |
| `npm run build` | 通过 | 主 JS chunk 约 1,041 kB（gzip 约 343 kB），Vite 给出大 chunk 警告 |
| `python -m pip check` | 通过 | 当前本机安装包无依赖冲突；不代表依赖可复现或无漏洞 |
| `npm ls --depth=0` | 通过 | 本地直接依赖树有效 |
| `npm audit` | 未得到有效报告 | 一次因 audit endpoint/包树 400，一次 TLS 中断；不能据此判定安全 |
| `pip-audit -r requirements.txt` | 未完成 | requirements 只有范围、无锁文件，工具需要临时解析/安装依赖，审计未在合理时间完成 |

仓库有 181 个 tracked files，但没有 README、Alembic、Dockerfile、Compose、CI workflow、生产部署配置或后端锁文件。

### 3.2 关键证据

- `backend/app/core/db.py:22-27` 的请求 Session 只关闭不提交；`semantic/service.py:29-30`、反馈、Trace、会话等都只 flush。
- `backend/app/core/security.py:11-16` 直接信任用户可控请求头；前端 `src/main.ts:10-11` 默认把身份设为 `admin`。
- `pipeline/orchestrator.py:350-356` 找到任意 `conversation_id` 就返回，没有校验 `user_id` 或 `dataset_name`。
- `pipeline/orchestrator.py:138-150` 在意图识别前直接执行精确命中的固定 SQL；`security/pipeline.py:85-99` 只解析 AST 后走通用表白名单。
- `security/columns.py:108-114` 仅当 Alias 内部正好是直接 `Column` 才脱敏；`SUM(secret_col)`、函数或复杂表达式不会命中。
- `intent/recognizer.py:231-241` 使用 Chat Completions 的 `json_object`，不是严格 JSON Schema 结构化输出。
- `intent/prompt.py:68-89` 没有传入“当前日期、时区、财务日历”；模型却被要求直接给绝对日期。
- `compiler/query.py:120` 多指标统一使用 `metrics[0].time_field`；`TimeGrain` 没有被编译成趋势分组。
- `compiler/predicates.py:25-35` 通过 `float(value)` 猜字面量类型；字符串 ID `001` 会被当数字。
- `security/guardrails.py:53-69` 只读取 EXPLAIN 根节点 `Plan Rows`，却把它称作“预估扫描行数”，不能代表子计划扫描量。
- `pipeline/orchestrator.py:388-396` 通过未经策略改写的原始 SQL 读取全表 `MAX(time_field)`，且固定使用第一个指标。
- `pipeline/orchestrator.py:335-338` 只持久化 headline/conclusion；前端 `stores/session.ts:141-151` 重开会话时只能伪造一个没有引证、结果和警告的残缺答案。
- `tests/golden/test_golden_set.py:135-143` 把真实 `intent` 固定输出为 `None`；`runner.py:106-121` 又允许默认 XFAIL。
- `tests/golden/runner.py:155-168` follow-up 继续发送原问题，没有提交 `select_option_index` 对应的选择。
- `tests/golden/conftest.py:35-41` 的临时权限 fixture 实际为空操作。

---

## 4. 2026 年同类技术对照

### 4.1 直接同类产品

| 2026 标杆能力 | 官方现状 | 当前项目 | 应吸收的做法 |
|---|---|---|---|
| Snowflake Cortex Analyst VQR | Verified Query 保存问题与逻辑语义层 SQL，并记录 verified_by / verified_at；2026-04 已进入 Semantic View | 有问题、固定物理 SQL、意图快照、hit_count | 改为“版本化逻辑计划/逻辑 SQL”，增加审核者、审核时间、语义版本、有效期、最近验证结果，不把物理 SQL 当永恒真相 |
| Snowflake Analyst Evaluations | 执行生成 SQL 与 VQ SQL 比结果，追踪准确率、回归和延迟；评测时临时移除被测 VQ，防止答案泄漏 | Golden case 同时充当 stub 输出和期望，真实模式默认 XFAIL | 运行时可信资产与 holdout benchmark 分离；结果等价作为主指标，意图/计划作为诊断指标 |
| Databricks Genie Agent | Knowledge Store、Example SQL、Trusted Assets、Benchmarks 分层；Benchmark 不进入运行时上下文；Agent mode 才做多步深度分析 | 语义元数据、VQ、Golden Set 的边界混合；只有单步问数 | 明确分成语义知识、运行时可信资产、离线评测集；基础问数与深度分析两种执行模式隔离 |
| Microsoft Fabric Data Agent | AI Schema、Instructions、Verified Answers；SDK 评测保存期望/实际/判定，支持诊断快照 | 有禁用场景、引证、Trace，但缺版本化配置快照与 Eval Run | 每次运行固化语义版本、Prompt 版本、模型快照、权限策略版本，评测结果可回看 |
| Google Looker Conversational Analytics | 以 LookML 为真相源，LLM 只决定字段、过滤、排序和限制，由 Looker 组合查询；Golden Query、业务词表、专业化 Agent；高级分析另启 Python | 核心路线接近；目前单数据集且前端硬编码 `orders` | 保持语义层编译；增加领域化 Agent/数据集路由；Python 深度分析作为独立受控模式，不混入基础查询 |

参考资料：

- [Snowflake Cortex Analyst Verified Query Repository](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/verified-query-repository)
- [Snowflake Cortex Analyst evaluations](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst-evaluations)
- [Databricks Genie Agents concepts](https://docs.databricks.com/aws/en/genie-agents/concepts)
- [Microsoft Fabric semantic model best practices](https://learn.microsoft.com/en-us/fabric/data-science/semantic-model-best-practices)
- [Microsoft Fabric data-agent evaluation](https://learn.microsoft.com/en-us/fabric/data-science/evaluate-data-agent)
- [Google Looker Conversational Analytics overview](https://docs.cloud.google.com/looker/docs/conversational-analytics-overview)

### 4.2 Agent/API/协议层

- OpenAI 官方文档在 2026-08 推荐新项目使用 Responses API；当前模型指南的生产主线是 GPT-5.6 系列，并要求以代表性 eval 比较能力、延迟和成本。项目当前默认 `gpt-4o-mini` + Chat Completions + `json_object`，可以升级，但**必须通过本项目 Golden Set 选型**，不能因为“最新”就全量使用最高档模型。参考 [OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model)、[Responses API migration](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。
- MCP `2026-07-28` 已改为无状态核心、可缓存 list、header 路由并强化授权。这适合未来把“只读、受治理的语义查询能力”暴露给外部 Agent，但不应把原始数据库或任意 SQL 工具直接暴露出去。参考 [MCP 2026-07-28 specification announcement](https://blog.modelcontextprotocol.io/posts/2026-07-28/)。
- OpenTelemetry 语义约定可用于统一 HTTP、DB、模型、工具、会话的链路属性；GenAI 内容属性可能包含 PII，且 GenAI 约定仍在独立演进，实施时必须固定版本并默认不采集完整 Prompt/输出。参考 [OpenTelemetry semantic conventions 1.43](https://opentelemetry.io/docs/specs/semconv/) 和 [GenAI attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)。
- OWASP 2026 Agentic Top 10 将 Goal Hijack、Tool Misuse、Identity & Privilege Abuse、Memory Poisoning、Unexpected Code Execution 等列为核心风险。当前项目虽然不是自由行动 Agent，仍直接命中身份、工具权限和上下文污染三个风险域。参考 [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)。
- NIST AI 600-1 强调治理、上线前测试、内容/数据来源和事件披露。对本项目的具体落地就是：语义版本审批、评测门禁、可追溯 Trace、风险分级和事件审计。参考 [NIST AI RMF Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)。

### 4.3 不应盲目跟进的热点

1. **不应把基础问数改成多 Agent。** 当前链路只有一个非确定性节点，多 Agent 会增加延迟、成本和不可复现性，不能改善指标口径。
2. **不应立即引入 A2A。** 当前没有跨组织 Agent 协作需求；内部模块函数调用更清晰。
3. **不应立即引入向量数据库作为“长期记忆”。** 当前结构化 slot state 比聊天摘要/向量记忆更可靠；向量检索只在数据集、术语、VQ 数量扩大后用于候选召回。
4. **不应为了流行框架重写成 LangGraph。** 基础查询 30 秒内同步完成，现有显式 pipeline 更容易验证。只有深度分析、等待人工确认、长任务恢复出现后，才评估 LangGraph/Temporal 一类 durable runtime。
5. **不应开放原始 SQL MCP 工具。** 如需 MCP，只暴露 `list_datasets`、`describe_semantic_model`、`run_governed_query(plan)`、`get_query_status` 等受治理工具。

---

## 5. 问题清单与优先级

### 5.1 P0：阻断投产，必须先修

| ID | 问题 | 影响 | 修复要点 |
|---|---|---|---|
| P0-01 | 可伪造 `X-Username` | 任意用户可成为 admin | 接 OIDC/OAuth2 Authorization Code + PKCE；后端验证 JWT 的 issuer/audience/signature/expiry；开发头仅在显式 `AUTH_MODE=dev` 时启用且生产启动失败 |
| P0-02 | 会话复用未验所有权/数据集 | 可向他人会话写 Turn、继承其 slot state | `_conversation()` 改为按 `(conversation_id, principal.user_id)` 查询并校验 `dataset_name`；失败统一 404；增加跨用户 POST 测试 |
| P0-03 | Semantic 管理 API 无鉴权 | 普通/匿名调用者可读物理表和敏感字段配置、触发发布 | 列表和详情按“可见数据集”过滤；lint/publish 仅 `semantic_admin`；响应按角色隐藏 physical_table、physical_column、敏感级别 |
| P0-04 | Verified Query 列权限旁路 | 固定 SQL 可聚合/表达式读取被 DENY/MASK 的列 | 停止直接执行物理 SQL；从 `intent_snapshot`/CanonicalPlan 重新编译。过渡期对 VQ AST 建立列级 lineage，任何引用列先做 `assert_column_access`，函数和表达式也必须覆盖 |
| P0-05 | 元数据事务不提交 | 会话、Trace、反馈、发布在请求后丢失 | `get_meta_session()` 使用 try/yield/commit，异常 rollback；读取接口可复用；写入与业务查询失败语义明确。增加“新 Session 可读”的 API 集成测试 |
| P0-06 | 数据库并非真正只读 | AST 漏洞或副作用函数可能写库/影响系统 | 使用独立最小权限 DB role；`default_transaction_read_only=on`；禁止 CREATE/TEMP；设置 statement/lock/idle timeout；SQL 函数 allowlist，阻断 `pg_sleep`、管理函数、dblink/外部函数 |
| P0-07 | 时间基准缺失且 wall clock 错误 | “本月/上月/今天”随模型猜测；生产时间戳固定 09:00 | `clock.now()` 正常返回 timezone-aware UTC；冻结只在测试注入 Clock；Intent 只返回相对时间表达式，确定性 TimeResolver 按用户时区/财务日历生成绝对区间 |
| P0-08 | 核心 Schema 校验缺失 | 非法 Intent 延迟到编译阶段，当前测试已红 | 为 QueryIntent 增加跨字段 validator；confidence 限制 `[0,1]`；sort limit `1..max_top_n`；各 operator 值数量严格校验 |
| P0-09 | Trace/错误/SQL 可见范围过宽 | 普通用户可能看到物理表、策略和内部异常 | 用户 Trace 只展示安全摘要和逻辑计划；原始 Prompt、物理 SQL、异常栈仅审计角色可见；所有 Trace 增加分类、脱敏和保留期 |
| P0-10 | 默认密钥和配置可用于生产 | `postgres:postgres`、开发 JWT secret、空 API key 缺少启动校验 | `environment=production` 时对弱 secret、默认 DSN、宽 CORS、dev auth、无 TLS 配置直接 fail-fast；使用 secret manager |

### 5.2 P1：正确性与可信度

| ID | 问题 | 影响 | 修复要点 |
|---|---|---|---|
| P1-01 | `trend`/`detail` 仅存在于枚举 | 用户意图与实际 SQL 形状不一致 | 为每种 kind 定义可执行契约；Trend 用 time bucket + group/order；Detail 明确允许列、默认排序和分页；未实现的 kind 先拒绝而不是伪装支持 |
| P1-02 | 多指标共用第一个时间字段 | 不同时间口径指标被静默错算 | 编译前要求同一 time basis，或分别聚合后按维度合并；引证必须逐指标展示时间字段和版本 |
| P1-03 | 字面量按字符串外观猜类型 | ID、前导零、Decimal、日期比较错误 | FieldDef 增加 physical/logical data type；根据字段类型构造 AST literal/parameter，不做 `float()` 猜测 |
| P1-04 | 比较查询 FULL OUTER 维度未 COALESCE | 只存在于基期的维度显示 NULL | 维度投影使用 `COALESCE(current.dim, baseline.dim)`；当前/基期缺失状态显式化；只校验指标列的 all-null |
| P1-05 | 指标表达式和权限依赖解析不一致 | 复合指标嵌套、循环、函数、列权限可能判断错 | 统一 SQLGlot/自定义表达式 AST；构建指标 DAG；发布时检测循环、类型、单位和可加性；权限 lineage 复用同一 DAG |
| P1-06 | fixed_filter 是自由 SQL 文本 | 方言绑定、函数滥用、列权限和可审计性差 | 改为结构化 Predicate DSL；旧文本只读迁移，发布前解析并规范化；禁止子查询和非 allowlist 函数 |
| P1-07 | 禁用场景只是 Prompt 文本 | 模型可能忽略业务禁区 | 增加可执行 scope policy：允许/禁止主题、指标/数据集映射、必需过滤；LLM 只做候选分类，PolicyResolver 最终决定 |
| P1-08 | 枚举别名冲突取第一项 | 可能静默映射错误实体 | 发布 lint 检测归一化后的别名冲突；resolver 返回 0/1/N 个候选，N>1 必须澄清；大字典用受权限约束的检索索引 |
| P1-09 | Freshness 查询绕过当前权限 | 引证可能泄漏其他区域最新日期，且表/列字符串未受 AST 保护 | freshness 作为数据契约/摄取水位存入元数据；或生成同权限、同数据集的受控 MAX plan；按当前指标而非第一个指标 |
| P1-10 | Decimal 不被数字逻辑识别 | NUMERIC 的格式化、同比、量级校验失效 | 使用 `numbers.Number` + Decimal 安全运算；结果 Schema 保留精度与单位；禁止浮点直接比较财务值 |
| P1-11 | 成本护栏把根输出行数当扫描行数 | 大扫描小聚合会错误 PASS | 递归读取 EXPLAIN：Total Cost、各节点 Plan Rows、relation、Seq Scan、join、sort/hash 内存；规则按 warehouse profile 配置；记录估计误差 |
| P1-12 | 截断和重试边界不准确 | 恰好 limit 行被误报；超时后的同连接重试可能处于 aborted transaction | SQL 请求 `limit+1`、客户端裁一行；重试使用新连接/显式 rollback；按 SQLSTATE 分类，不匹配错误字符串；指数退避+抖动 |

### 5.3 P2：平台能力与规模化

| ID | 问题 | 升级方向 |
|---|---|---|
| P2-01 | 单数据集、无关系/Join | 加入实体、主键、外键、关系基数、join type、fanout 策略、桥表、默认时间；先支持审核后的静态 Join Graph，不让 LLM 自由拼 Join |
| P2-02 | 语义模型只有 boolean publish | 建立 immutable semantic revision：draft → linted → approved → published → retired；支持 diff、owner、审批、回滚和兼容性检查 |
| P2-03 | 缺少物理契约校验 | 发布时连接 information_schema，检查表/列/类型/权限；定期 drift scan；数据新鲜度、唯一性、非空、基数进入 health |
| P2-04 | VQ 只能精确文本召回 | 先意图再做 canonical signature；可选 embedding 只召回候选，最终按结构化兼容性、语义版本和权限过滤；记录 match reason/confidence |
| P2-05 | 没有缓存与预聚合 | 缓存键包含 canonical plan、semantic revision、principal policy hash、source watermark；绝不只按自然语言或 SQL 缓存；热点指标引入预聚合 |
| P2-06 | 同步请求无法承载深度分析 | 基础问数保留同步；多步分析进入 Job/Run 状态机，支持 checkpoint、取消、超时、预算、部分进度；到这一步再选 LangGraph/Temporal |
| P2-07 | Trace 仅在业务库中 | 业务 Trace 保留用于产品回看，同时输出 OpenTelemetry；建立 latency、token、cost、refusal、clarify、eval regression、warehouse scan 指标和 SLO |
| P2-08 | 反馈只是记录 | 负反馈进入 triage：意图/时间/语义/编译/数据/结论；确认后生成候选 Golden Case 或 VQ，必须人工审核后晋级 |

### 5.4 P3：体验与工程

- 工作台不再硬编码 `orders`，按用户权限加载专业化 Agent/数据域；建议一开始做“销售问数 Agent”“运营问数 Agent”，而不是一个全局 Agent。
- 澄清改为结构化 API：前端发送 `{turn_id, target, selected_value}`，后端直接 patch slot，不再把选项 label 重新交给 LLM 猜。
- 会话历史持久化完整 Answer（引证、假设、警告、列、分页结果引用、Trace 链接），重开后原样恢复。
- SSE/WebSocket 推送阶段进度和最终结果，提供取消；基础问数不需要逐 token 输出，因为答案由模板生成。
- 结果表分页/虚拟化；列展示使用业务名/格式/单位；下载由后端按权限重新生成并审计。
- CSV 对 `= + - @ \t \r` 开头单元格做公式注入转义，增加 UTF-8 BOM 和本地化分隔符选项。
- 增加移动/窄屏布局、键盘操作、ARIA、色彩对比和错误恢复。
- Element Plus 按需引入和 manualChunks；让组件测试注册真实插件或稳定 stub，并把 Vue warning 视作测试失败。
- 引入 Alembic、`pyproject.toml`、`uv.lock`（或等价 Python lock）、Node engine/packageManager 固定、Docker、Compose、CI、SBOM、secret scan、SAST、依赖审计、覆盖率与负载测试。

---

## 6. 目标架构

### 6.1 目标形态：模块化单体 + 可选异步 Worker

```text
┌──────────────────────────── Vue Workbench ────────────────────────────┐
│ OIDC 登录 │ Data Domain │ 对话/澄清 │ 证据/结果 │ Trace │ Admin/Eval │
└─────────────────────────────────┬─────────────────────────────────────┘
                                  │ HTTPS / SSE
┌─────────────────────────────────▼─────────────────────────────────────┐
│ FastAPI API/BFF（模块化单体）                                         │
│ AuthN/OIDC → AuthZ → Rate Limit → Idempotency → Request Context       │
│                                                                       │
│ Query Workflow                                                        │
│  1 Domain Router       2 Intent Extractor       3 Deterministic Resolve│
│  4 Canonical Plan      5 Policy Compiler        6 Dialect Compiler     │
│  7 Plan Guard          8 Execution Gateway      9 Result Validator     │
│ 10 Answer Renderer / Citation                                         │
│                                                                       │
│ Semantic Registry │ Trusted Assets │ Conversation │ Eval │ Audit       │
└───────────────┬───────────────────────┬───────────────────────────────┘
                │                       │
      ┌─────────▼──────────┐  ┌────────▼──────────────────────────────┐
      │ Metadata PostgreSQL │  │ Data Warehouse / Governed DB Role     │
      │ revisions/runs/evals│  │ native RLS + read only + timeouts     │
      └─────────┬──────────┘  └───────────────────────────────────────┘
                │
      ┌─────────▼──────────┐
      │ Redis（按需）       │  幂等、限流、短期 plan/result cache
      └────────────────────┘

可选 Worker（仅深度分析/批量评测/定时任务）：
Job Queue → durable workflow → 多个受控 Query Plan → Python sandbox（可选）→ 报告
```

当前不需要把 Semantic、Query、Platform 拆成网络服务。先通过包边界、接口和独立数据模型做到可拆；当出现以下任一条件再拆：

- 查询执行需要独立水平扩缩容或不同网络隔离；
- 语义服务被多个产品消费并需要独立 SLA；
- Trace/Eval 写入量影响在线查询；
- 三个模块由不同团队独立发布；
- 合规要求元数据面与数据执行面物理隔离。

### 6.2 核心契约升级

#### IntentV2：LLM 只表达用户语言，不负责计算绝对时间

```json
{
  "kind": "aggregate | trend | ranking | detail | unsupported",
  "domain_candidates": [{"name": "sales", "confidence": 0.96}],
  "metrics": [{"name": "sales_revenue", "confidence": 0.98}],
  "dimensions": [{"name": "province", "confidence": 0.91}],
  "filters": [{
    "field": "region_code",
    "operator": "in",
    "spoken_values": ["华东"],
    "confidence": 0.97
  }],
  "time_expression": {
    "text": "本月",
    "kind": "relative",
    "unit": "month",
    "offset": 0,
    "to_date": true,
    "confidence": 0.99
  },
  "comparison": "mom",
  "sort": {"by": "sales_revenue", "direction": "desc", "limit": 10},
  "ambiguities": []
}
```

LLM 输出用 Pydantic 生成的 JSON Schema，以 Structured Outputs `strict` 方式解析；必须处理 refusal、incomplete/max tokens、provider error 和 schema mismatch。绝对日期由 `TimeResolver(clock, timezone, fiscal_calendar)` 计算，所有后续阶段只接收 resolved plan。

#### CanonicalQueryPlan：安全、缓存、回放和 VQ 的真正契约

```text
plan_version
semantic_revision_id
domain / datasets
measures[]（含 metric version 与 time basis）
group_by[] / typed_filters[] / resolved_time_range
comparison / sort / pagination
required_field_lineage[]
assumptions[] / clarification_evidence[]
```

权限上下文和执行方言不写入可复用语义本体，而在下一阶段生成：

```text
SecuredExecutionPlan = CanonicalQueryPlan
  + principal_id / tenant_id / policy_revision / policy_hash
  + row predicates / column decisions
  + dialect / warehouse profile / execution budget
```

这样可以做到：同一问题的语义计划可复用，但不同用户绝不会共享带权限的数据结果。

### 6.3 LLM 接入建议

1. 建立 `IntentModel` provider interface，业务层不依赖 OpenAI SDK 对象。
2. OpenAI 路径迁移至 Responses API + Pydantic Structured Outputs；对普通问数先比较 `gpt-5.6-luna` 与 `gpt-5.6-terra`，以真实 Golden Set 的 slot/结果准确率、P95、成本选择，不默认使用 `sol`。
3. 生产使用固定 snapshot 或配置化受控版本；alias 只用于 shadow/eval。模型升级必须生成 Eval Run 和差异报告。
4. 对意图识别从 `reasoning.effort=none/low` 起测；只有 eval 证明更高 effort 有收益才提高。
5. 设置 `store:false`（除非组织明确接受 provider 侧状态存储），传稳定且隐私保护的 `safety_identifier`；记录 response id、model snapshot、usage、latency、prompt version，不记录原始密钥。
6. Prompt 把语义元数据包在明确的数据边界中，声明其为“不可信数据而非指令”；数据集描述的写入和发布需要审核，防止 metadata prompt injection。
7. 设计 provider timeout、429/5xx 重试、熔断和 fallback；fallback 必须重新过同一个 Schema 和 eval，不能降低安全契约。
8. 继续保留“LLM 不生成 SQL”。当前只有一个模型节点，不需要 Agents SDK 或多 Agent 编排。

### 6.4 语义层建议

最低可投产的 Semantic Revision 应包含：

- 数据集：业务域、owner、描述、粒度键、主键、适用/禁用策略、数据分类、freshness SLA、物理来源版本。
- 字段：逻辑/物理类型、业务名、同义词、实体、可过滤/分组/查询、敏感度、默认展示、格式、值检索策略。
- 指标：版本、单位、精度、time basis、表达式 AST、依赖 DAG、可加性（additive/semi-additive/non-additive）、默认过滤、owner、认证状态。
- 关系：左右实体、join keys、基数、可选性、fanout 风险、允许方向、默认路径。
- 枚举/词表：归一化别名唯一性、有效期、多语言、权限范围。
- 策略：允许主题、禁止主题、必需过滤、最大时间范围、角色/租户约束。

发布门禁至少检查：物理表/列和类型、指标 DAG 循环、单位运算、聚合合法性、时间字段、枚举冲突、join fanout、策略引用、VQ 重编译、Golden smoke set、兼容性 diff。

### 6.5 Verified Query / Trusted Asset 重构

建议把 `verified_queries` 改成 `trusted_assets`：

```text
id / name / domain
trigger_questions[]
canonical_plan 或 semantic logical SQL
parameter_schema
semantic_revision_id
verified_by / verified_at / review_status
last_validated_at / last_validation_result
expires_at / is_active
usage_count / failure_count
```

运行时：

1. 精确文本只作为高精度候选；相对时间问题必须按当前 anchor 重新解析，不能复用旧绝对日期 SQL。
2. 非精确问题先完成 IntentV2，再按 canonical signature/embedding 找候选。
3. 校验参数、语义版本、权限、数据域和时间兼容性。
4. 从 canonical plan 重新执行权限编译和方言编译。
5. 响应明确标记使用了哪个 trusted asset 和验证时间。

评测时，被测 trusted asset 必须从运行时召回集合中移除，避免“拿答案做提示再证明自己答对”。

---

## 7. 评测体系重建

### 7.1 修复当前 Golden Set 空洞

1. `_to_outcome()` 返回真实 `turn.intent_snapshot` 或 resolved plan，不能写死 `None`。
2. 真实模型差异不再全局默认 XFAIL。可接受的字段显式 tolerance；关键指标/时间/权限/状态差异必须 FAIL。
3. `run_clarify_followup()` 应读取第一次响应的 options，选择 `select_option_index`，调用结构化 clarify API；不能再次发送原问题。
4. `case.as_of` 真正传给每个 case 的 Clock fixture，而不是全局固定 2026-08-12。
5. `ephemeral_policy` 必须实际创建 case 级策略并在事务中回滚。
6. 失败报告保存 model、prompt version、semantic revision、intent diff、plan diff、SQL diff、result diff 和 Trace id。

### 7.2 数据集分层

```text
tests/golden/runtime_examples/   # 可进入召回/Prompt 的 trusted examples
tests/golden/holdout/            # CI 盲测，绝不进入运行时上下文
tests/golden/security/           # 越权、注入、枚举探测、SQL 函数、跨租户
tests/golden/multiturn/          # 澄清、槽位替换、并发和会话所有权
tests/golden/robustness/         # 错别字、同义改写、长输入、冲突指令
tests/golden/temporal/           # 月末、闰年、跨年、时区、财务日历、DST
```

### 7.3 评分维度

| 层级 | 主断言 | 说明 |
|---|---|---|
| Intent | 槽位准确率、歧义召回率、unsupported | 用于定位 LLM 问题，不单独代表答案正确 |
| Resolve | 枚举/实体、时间区间、假设、澄清 | 必须完全确定性 |
| Plan | 指标版本、时间口径、维度、typed filter、policy lineage | 最重要的结构化正确性契约 |
| SQL | AST 性质、允许表/列/函数、RLS、LIMIT、方言 | 不要求字符串完全一致 |
| Result | 列、行、Decimal tolerance、排序、当前/基期 | 最终业务正确性的核心 |
| Answer | headline、单位、引证完整、警告、无泄漏 | 模板/生成答案都必须测 |
| 非功能 | P50/P95、token、成本、scan rows、clarify/refusal rate | 防止“更准但不可用” |

### 7.4 CI 门禁建议

- 所有确定性单测和 holdout stub：100% 通过。
- 安全集：100% 通过，任何跨租户/列权限/元数据泄漏直接阻断。
- 真实模型关键集：指标、时间、权限、状态不得回归；非关键措辞只记 diff。
- 结果正确率门槛按域设置，首个销售域建议 ≥98%；拒答/澄清的 precision 与 recall 分开统计。
- P95 基础问数（不含仓库排队）目标 <5s；模型和仓库分别设预算。
- 每次 Prompt、模型、语义版本变更产生可比较 Eval Run；禁止只看本次总分，不看已通过用例的 regression。

---

## 8. 安全与治理详细方案

### 8.1 身份、权限和租户

- OIDC claims 映射内部 immutable user id；用户名仅展示，不作为权限主键。
- API 每次请求生成 `PrincipalContext(user_id, tenant_id, roles, groups, attributes, auth_time)`。
- Conversation、Turn、Trace、Feedback、TrustedAsset 管理操作都以 principal 做对象级鉴权。
- `ConversationRow.user_id` 增加到 users 的外键；dataset_name 改成 semantic revision 外键或稳定 domain id。
- 管理面角色至少拆为 semantic_viewer、semantic_editor、semantic_approver、security_admin、trace_auditor、eval_operator。
- 多角色列策略按明确 lattice 合并，不能依赖 ORM 返回顺序；DENY > MASK > ALLOW 或组织定义的显式规则需有单测。

### 8.2 数据执行防线

采用四层 defense in depth：

1. **语义可见性**：不可见字段/指标不进入模型上下文和候选检索。
2. **计划权限编译**：根据字段 lineage 对 measure、dimension、filter、sort、join key 全量检查。
3. **SQL AST 守卫**：SELECT-only、表/列/函数 allowlist、无锁、无副作用、LIMIT、无未知 dialect node。
4. **数据库强制**：独立只读 role、native RLS/security barrier view、statement timeout、资源组/warehouse quota。

不能把 AST 守卫当成数据库权限的替代品；两者必须同时存在。

### 8.3 Agent 特有风险

- 用户问题、枚举值、语义描述、VQ 问题、外部 MCP 内容都标记为不可信数据，不能改变系统规则。
- Prompt/模型输出不参与权限决策；所有授权在代码和数据库完成。
- MCP 如启用，遵循 2026-07-28 规范和 OAuth audience/issuer 校验；不做 token passthrough；scope 最小化。
- 深度分析 Python sandbox 默认无网络、只读输入、CPU/内存/时间/文件大小限额、临时文件隔离、包 allowlist。
- 任何未来写回、发消息、建工单等 side effect 都必须单独权限、幂等 key、预览和人工确认；基础问数 Agent 保持 read-only。

### 8.4 隐私与审计

- Trace 内容分级：public diagnostic / user-private / sensitive / admin-only。
- 默认记录 hash、长度、结构和引用 id，不记录完整 Prompt、数据行、token、PII；需要采样时先脱敏并有保留期限。
- 原始问题、反馈评论、导出行为进入数据保留和删除策略。
- 审计日志 append-only，记录谁在何时发布语义版本、批准 trusted asset、查看物理 SQL、导出数据、重放查询。
- 定义 AI 事件流程：错误数字、越权、大规模异常查询、Prompt injection、模型供应商故障都能关联 Trace/Eval/版本并回滚。

---

## 9. 可观测性、SLO 与成本

### 9.1 Trace 模型升级

每个 Query Run 至少记录：

```text
request_id / trace_id / run_id / conversation_id / turn_id
principal_hash / tenant_id / domain
semantic_revision / policy_revision / prompt_version / model_snapshot
stage input/output schema version（内容按级别脱敏）
LLM tokens/cache/reasoning tokens/latency/retry
compiled plan hash / secured SQL hash / trusted_asset_id
EXPLAIN cost / recursive rows / scan relations / warehouse queue+execution
result rows / truncated / validator issues
status / error_code / cancellation / idempotency outcome
```

Trace Stage 的数据库记录继续保留用于产品回看；同时以 OpenTelemetry span 输出到统一观测平台。不要把 OpenTelemetry 当成业务 Trace 的替代，因为业务回放需要稳定、版本化的结构。

### 9.2 建议 SLO

- API 可用性：99.9%（生产初期）。
- 基础问数成功链路 P95：<5 秒；P99：<10 秒，不含明确异步任务。
- 权限策略漏施加：0。
- 元数据写入持久化成功率：99.99%。
- Trace 完整率：>99.9%，任何 answered turn 必须有 resolve/plan/security/execute/answer span。
- 安全/关键 Golden 回归：0。
- 语义发布回滚时间：<15 分钟。

### 9.3 缓存与成本

- Prompt 固定前缀按 provider 官方能力做缓存，记录 cache hit/write token；不要把频繁变化的用户/权限上下文放在可共享前缀。
- 语义定义做 revision cache；权限可见视图按 `(principal policy hash, revision)` 短缓存。
- 结果缓存必须包含权限 hash 与 source watermark，并设置较短 TTL；敏感结果可完全禁用共享缓存。
- 对高频指标建立预聚合/物化视图；成本护栏同时使用时间范围、估计扫描、Total Cost、并发和租户预算。
- Eval 和批处理使用独立 warehouse/resource group，避免影响在线问数。

---

## 10. 前端与 API 升级

### 10.1 API v2 建议

保留 `/api/chat/*` 兼容层，新增：

```text
POST /api/v2/query-runs
  body: {agent_id, question, conversation_id?, idempotency_key}
  return: {run_id, status, events_url}

POST /api/v2/query-runs/{run_id}/clarifications
  body: {target, selected_value, expected_state_version}

GET  /api/v2/query-runs/{run_id}
GET  /api/v2/query-runs/{run_id}/events       # SSE
POST /api/v2/query-runs/{run_id}/cancel
GET  /api/v2/conversations/{id}?include=turns,answers
```

- 所有 POST 支持 Idempotency-Key。
- Conversation 有 `state_version` 做乐观锁，防止并发问题覆盖 slot。
- 错误响应使用稳定 `error_code` + 用户安全文案 + trace_id；内部 detail 不直接返回。
- OpenAPI 生成 TypeScript client，避免手工同步 Pydantic/TS 类型。

### 10.2 工作台

- 登录后先选择/自动路由到授权的数据 Agent，不再写死 `orders`。
- 澄清卡直接提交结构化选择；多个澄清项可一次确认。
- Condition Panel 直接提交 Canonical Plan patch，不再拼自然语言后重新识别。
- 会话恢复完整答案、结果引用和 slot；分页结果可重新拉取，不能在 Turn JSONB 无限保存大结果。
- 进度展示“理解问题 / 解析口径 / 权限与成本检查 / 查询数据 / 生成答案”，支持取消和重试。
- 对普通用户显示逻辑计划和口径；物理 SQL 只对有权限角色显示。
- Bundle 拆分 Element Plus、管理面和 Trace；启用路由级预取策略和性能预算。

---

## 11. 工程与部署基线

### 11.1 后端

- 建立 `pyproject.toml`，固定 Python 支持范围（建议 3.12/3.13）和工具：ruff、mypy/pyright、pytest、coverage。
- 使用 `uv.lock` 或等价锁文件；运行依赖与开发依赖分组；不再只写 `>=`。
- Alembic 管理 schema；`init_db.py` 仅用于本地样本，生产禁止执行 `DROP TABLE`。
- FastAPI lifespan 做启动校验、连接池预热和 shutdown；健康探针拆成 `/livez`、`/readyz`。
- API server 与离线 worker 分进程；不要在 Web worker 内跑大批 Eval。
- 使用结构化日志、统一 error taxonomy、request/trace id 中间件。
- 单元、集成、契约、安全、Golden、负载测试分 marker；真实模型测试作为受控 CI job。

### 11.2 前端

- 固定 Node LTS 和 npm/pnpm 版本，在 `package.json` 加 `engines`、`packageManager`。
- ESLint + Prettier + typecheck + Vitest coverage；测试 warning 视作失败。
- 按需加载 Element Plus；为主 chunk 设置预算，例如 gzip <200 kB（具体值以真实网络性能预算为准）。
- E2E 使用 Playwright，覆盖 OIDC、问数、澄清、越权 404、会话恢复、取消、导出。

### 11.3 CI/CD 与供应链

建议流水线：

```text
lint/typecheck
  → unit/integration
  → security golden
  → frontend build/e2e
  → real-model canary eval（受控密钥与预算）
  → SAST/secret/dependency/license/SBOM/container scan
  → build immutable image
  → staging migration + smoke + eval diff
  → approval/canary production
  → SLO observation + automated rollback trigger
```

依赖更新使用 Renovate/Dependabot；每个更新 PR 自动跑 Golden 和构建。当前 npm/pip 审计未获得完整报告，因此第一轮 CI 建立时应生成一次基线 SBOM 与漏洞报告，不能把本次“未完成”理解为“无漏洞”。

---

## 12. 建议目录演进

不需要一次性搬完；按阶段逐步引入。

```text
backend/
  app/
    api/v1/                    # 现有兼容 API
    api/v2/
    auth/                      # OIDC/JWT、PrincipalContext、RBAC
    workflow/                  # QueryRun 状态机与 stage interface
    llm/                       # provider、Responses、structured parsing、routing
    intent/v2.py
    temporal/                  # clock/timezone/fiscal calendar/resolver
    semantic/
      registry.py
      revisions.py
      relations.py
      lineage.py
      lint/
    planning/                  # CanonicalQueryPlan
    compiler/
      dialects/postgres.py
    security/
      policy_compiler.py
      ast_guard.py
    execution/
      gateway.py
      explain.py
      validators/
    trusted_assets/
    evals/
    observability/
  alembic/
  pyproject.toml
  uv.lock

frontend/src/
  auth/
  generated-api/
  features/query-run/
  features/conversations/
  features/semantic-admin/
  features/evals/
  features/trace/
```

各 stage 接口返回版本化 dataclass/Pydantic model，不传任意 dict；数据库 ORM 与领域模型继续分离。

---

## 13. 分阶段实施计划

### Phase 0：安全与持久化止血（第 1～2 周）

目标：消除阻断投产的越权和数据丢失。

改动：

- `core/db.py`：请求事务 commit/rollback；sample connection 强制 read-only。
- `core/security.py`、前端 auth：OIDC/JWT；生产禁用 header 身份。
- `pipeline/orchestrator.py`：会话 owner/dataset 校验；所有失败有稳定 error code。
- `api/semantic.py`、`api/trace.py`：对象级与角色级鉴权。
- Verified SQL 暂时关闭或增加完整列 lineage guard；优先切换为 intent_snapshot 重编译。
- 修复 `clock.now()`、当前日期/时区；修复 QueryIntent/Filter/Sort/Confidence validator。
- 添加跨用户 POST、VQ 受限列聚合、副作用函数、事务跨 Session 持久化测试。

验收：

- P0 安全集 100% 通过。
- 新 Session 能读取刚创建的 Conversation/Turn/Trace/Feedback。
- 任意非管理员无法发布语义或看到物理 SQL。
- 只读 DB role 即便绕过应用 AST 也无法写数据。

### Phase 1：查询正确性与可信资产（第 3～5 周）

目标：让“系统支持的 Intent”与“实际正确执行的能力”一致。

改动：

- IntentV2、确定性 TimeResolver、Typed Filter。
- CanonicalQueryPlan；Trend/Ranking/Detail 分别编译，未完成的形状明确拒绝。
- 多指标 time basis 校验、Decimal、comparison COALESCE、limit+1、重试连接修复。
- 统一 metric expression/lineage DAG；fixed_filter 迁移到 DSL。
- Trusted Asset v2，保存 plan + semantic revision + audit 字段。
- EXPLAIN 递归成本分析和 warehouse profile。

验收：

- 时间边界集、指标 DAG、类型过滤、多指标口径集全部通过。
- 相同 Canonical Plan 在同一 revision 上生成稳定 AST。
- Trusted Asset 不保存可直接绕过当前语义/权限的物理 SQL。

### Phase 2：真实评测与可观测性（第 6～8 周）

目标：任何模型、Prompt、语义变更都可量化验证和回滚。

改动：

- 修复 Golden runner 的真实 intent、case clock、policy fixture、clarify selection。
- runtime examples 与 holdout benchmarks 分离。
- EvalRun/EvalResult 数据表和报告页；结果等价、回归、延迟、成本指标。
- OpenTelemetry + dashboards + alerts；Trace 分级、脱敏、保留策略。
- OpenAI Responses + Structured Outputs provider，shadow 比较 luna/terra/现有模型。

验收：

- 真实模型关键集不再默认 XFAIL。
- 每次模型/Prompt/语义变更自动生成与基线的 regression report。
- answered turn 的完整 Trace 率 >99.9%，且普通用户看不到敏感 Trace 字段。

### Phase 3：语义规模化与产品体验（第 9～12 周）

目标：从单个 `orders` 样本扩展到少量真实业务域。

改动：

- Semantic Revision、审批/回滚、物理 drift lint、关系/基数/Join Graph。
- 先接入 2～3 个专业化 Agent/域，不做全局大一统 Agent。
- 数据集/术语/Trusted Asset 权限感知召回。
- SSE、取消、结构化澄清、完整会话恢复、Plan patch、前端性能与无障碍。
- Alembic、锁文件、容器、CI/CD、SBOM、漏洞门禁、staging/canary。

验收：

- 每个域有 owner、SLA、holdout set 和发布审批。
- Join fanout 用例和跨域拒答用例通过。
- 生产部署可从空环境按文档重复构建、迁移、回滚。

### Phase 4：可选深度分析与开放协议（第 13 周以后）

前置条件：Phase 0～3 达标且真实需求存在。

- 增加异步 Agent mode：受预算的多步查询、图表和结构化报告。
- Python sandbox 只处理已授权查询结果，不直接拿 warehouse credential。
- 引入 durable workflow 和人工确认；选型 LangGraph/Temporal 前做故障恢复 PoC。
- 对外提供只读 governed semantic MCP server，按 MCP 2026-07-28 实现授权、无状态部署、缓存和审计。
- 写回/告警/工单能力单独立项，不与只读问数权限混用。

---

## 14. 资源与工作量粗估

以当前代码规模、首批 2～3 个真实业务域为假设：

| 阶段 | 后端 | 前端 | 数据/语义 | QA/安全 | 日历时间 |
|---|---:|---:|---:|---:|---:|
| Phase 0 | 1.5 人 | 0.5 人 | 0.2 人 | 0.5 人 | 2 周 |
| Phase 1 | 1.5 人 | 0.3 人 | 0.5 人 | 0.5 人 | 3 周 |
| Phase 2 | 1.0 人 | 0.5 人 | 0.3 人 | 0.7 人 | 3 周 |
| Phase 3 | 1.5 人 | 1.0 人 | 1.0 人 | 0.7 人 | 4 周 |

总计约 10～12 周可达到“首批真实域受控投产”，不含企业统一身份、网络、仓库权限审批的外部排期。若只有 1 名全栈开发，应保持相同优先级，预计 4～6 个月，不应压缩掉 Phase 0 和真实评测。

---

## 15. 最终验收清单

### 安全

- [ ] 生产环境无法使用 `X-Username` 冒充身份。
- [ ] 会话、Turn、Trace、反馈、导出均有对象级鉴权。
- [ ] VQ/Trusted Asset 无法读取当前用户不可见的表、列、行或函数。
- [ ] 数据库账户只读，native RLS/安全视图与应用策略双保险。
- [ ] 管理 API、物理 SQL、敏感 Trace 有独立角色。
- [ ] Prompt/元数据/MCP 注入安全集 100% 通过。

### 正确性

- [ ] Intent Schema 对 kind、metric、time、filter、sort、confidence 做完整交叉校验。
- [ ] 时间由确定性 resolver 处理，覆盖时区、闰年、月末、跨年和财务日历。
- [ ] Trend、Ranking、Detail 的实际 SQL 形状符合契约；未支持能力明确拒绝。
- [ ] 多指标时间口径、复合指标 DAG、半可加性、Decimal 精度有测试。
- [ ] 引证逐指标包含版本、时间口径、过滤、权限和 freshness 来源。

### 评测

- [ ] runtime trusted examples 与 holdout eval 分离。
- [ ] 真实模型不再以全局 XFAIL 掩盖差异。
- [ ] 澄清、多轮、权限、拒答、结果等价真正端到端执行。
- [ ] 模型、Prompt、语义发布均有回归报告和门槛。

### 可靠性与运维

- [ ] 请求事务可提交/回滚，跨 Session 集成测试通过。
- [ ] 幂等、并发 slot 更新、取消、超时、重试和连接恢复有测试。
- [ ] `/livez`、`/readyz`、SLO dashboard、告警、审计和回滚可用。
- [ ] 依赖锁定、迁移、容器、CI、SBOM、漏洞扫描可重复执行。

### 产品

- [ ] 用户按权限选择专业化 Agent/数据域，不再硬编码 `orders/admin`。
- [ ] 澄清和条件编辑走结构化 Plan patch。
- [ ] 历史会话完整恢复答案、引证、警告和结果引用。
- [ ] 进度、取消、错误恢复、响应式、无障碍和 CSV 安全达标。

---

## 16. 最终建议

这个项目不需要推倒重来。最优路线是：

1. **保留确定性语义编译器和分阶段 Trace。** 这是项目最有价值、也最符合 2026 可信问数趋势的部分。
2. **先做 Phase 0。** 在认证、会话所有权、VQ 列权限和事务持久化修复前，不应接真实业务数据或开放给多用户。
3. **把 QueryIntent 升级为 IntentV2 + CanonicalQueryPlan。** 让时间、类型、权限、缓存、回放、VQ 和 Eval 都围绕同一稳定计划契约工作。
4. **把 Golden Set 从“用例文件”升级成真正的评测系统。** 运行时示例和盲测分离，最终以执行结果正确性和权限安全为主，而不是只看 LLM JSON 是否相似。
5. **保持模块化单体，延后微服务和多 Agent。** 当深度分析、长任务或多团队边界真正出现时再引入 durable workflow 和独立 worker。
6. **对外互操作优先选择只读、受治理的 MCP 语义接口。** 但它属于 Phase 4，不是当前投产前置条件。

完成前三个阶段后，这个项目才会从“设计良好的可信问数原型”升级为“可审计、可回归、可安全交付的企业 Data Agent 基座”。
