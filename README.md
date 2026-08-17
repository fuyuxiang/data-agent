<div align="center">

# 企业级数据智能体 / Data Agent

### Governed natural-language analytics with deterministic planning, secure execution, and verifiable answers.

**把"自然语言问数"从 Demo 变成生产系统。** 企业级数据智能体：在业务语义、指标口径、用户权限的强约束下，把业务人员的问题转成可审计、可重放、可评测的数据答案。

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-14%2B-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![SQLGlot](https://img.shields.io/badge/SQL%20AST-SQLGlot-6366F1)](https://sqlglot.com/)
[![License](https://img.shields.io/badge/License-MIT-22c55e)](./LICENSE)
[![Tests](https://img.shields.io/badge/Backend_Tests-908%2b-22c55e?logo=pytest&logoColor=white)](#测试与评测)
[![Frontend](https://img.shields.io/badge/Frontend-12%20specs-22c55e?logo=vitest&logoColor=white)](#测试与评测)

[核心特性](#核心特性) · [vs 同类方案](#vs-同类方案) · [架构](#系统架构) · [快速开始](#快速开始) · [API](#api-v2) · [测试](#测试与评测) · [License](#license)

</div>

---

## 为什么选 Data Agent？

当业务人员对数据库说"本月华东销售额环比如何"——

- **LangChain SQL Agent / Vanna / Chat2DB** 路线：让 LLM 直接生成 SQL。Demo 上跑得通，上了规模就出 P0：幻觉 SQL 跑坏数据、列权限被绕过、口径每次都不一样、没有审计口径。
- **WrenAI** 路线：MDL 语义层 + 多轮对话。偏 BI 自助分析，企业级行级权限与可重放能力偏弱。
- **Data Agent（本项目）路线**：把 LLM 锁在语言理解层，**指标、维度、时间、表关联、权限、SQL 全部走代码**。同样的语义版本 + 同样的权限上下文，产出稳定、可比较、可重放、可追溯。

### 三段式价值 · Generate · Govern · Verify

| 阶段 | 含义 | 你拿到什么 |
|---|---|---|
| **Generate** | 自然语言 → 结构化意图 | `IntentV2` JSON，每个槽位带置信度；模型拿不到 SQL 权限 |
| **Govern** | 意图 → 规范计划 → 鉴权计划 → 受限 SQL | Canonical Plan 携带 lineage；Secured Plan 绑定 Principal/Policy；AST Allowlist + EXPLAIN 预算 |
| **Verify** | 执行 → 答案 + 引证 + Trace | 逐阶段 Trace 可重放；Golden Set 分层评测；答案自带版本、时间口径、警告、下钻入口 |

---

## vs 同类方案

| 维度 | LangChain SQL Agent | Vanna | Chat2DB | WrenAI | **Data Agent** |
|---|---|---|---|---|---|
| 意图解析 | LLM 直出 | RAG + LLM | LLM 直出 | MDL + LLM | **Structured Outputs + IntentV2 + 置信度** |
| SQL 生成 | LLM 自由生成 | LLM 自由生成 | LLM 自由生成 | 模板驱动 | **SQLGlot 确定性编译，禁止 LLM 生成物理 SQL** |
| 安全模型 | 应用层基本校验 | 列级脱敏 | 应用层 | 行/列级 | **RBAC+ABAC+行级+列级+AST Allowlist+EXPLAIN 预算+Fail-closed** |
| 语义层 | 无 | 表级 RAG | 弱 | MDL（强） | **Metric DAG + Join Graph + Revision + Trusted Asset** |
| 时间口径 | 不处理 | 不处理 | 不处理 | 弱 | **本期/上期/同比/环比/财务年/月未/闰年/DST/IANA** |
| 可观测 | 链路日志 | 无 | 无 | Stage Trace | **逐阶段 Trace + Plan/SQL Hash + Replay + 三层可见性分级** |
| 评测体系 | 无 | 无 | 无 | 简单 | **Golden/Holdout/Security/Multiturn/Robustness/Temporal 六类分层评测** |
| 部署形态 | 单体 | 单体 | 单体 | 云优先 | **模块化单体 + 异步评测 Worker，独立只读 DB 角色** |

> **一句话区分**：同类项目把 LLM 当 SQL 工程师；本项目把 LLM 当业务翻译，SQL 工程师由确定性编译器担任。

---

## 适合谁用 / 不适合谁用

**适合**

- 企业内部数据团队，需要把"问数"从分析师 1v1 服务变成业务自助
- 数据安全/合规要求严格（行级权限、列级脱敏、审计、只读副本、最小权限账号）
- 已经或即将建立指标体系（需要版本管理、口径回放、上下游 lineage）
- 期望把 LLM 限制在语言理解层，不让模型触碰生产数据库

**不适合**

- 只是想跑个 PoC 看 SQL 生成效果（Vanna、LangChain SQL Agent 更轻）
- 没有 PostgreSQL 与 OIDC 基础设施
- 数据源是非结构化（图片/文档/音视频），本项目只服务结构化数据

---

## 核心特性

### 1. 受治理的自然语言理解

- Structured Outputs 把模型输出强约束到 `IntentV2`，**模型拿不到 SQL 生成权**
- 单独识别指标、维度、过滤、时间、比较、排序、Top N 与查询类型，每槽位独立置信度
- Prompt 仅注入当前用户可见的数据域、指标、字段、枚举；拒绝 SQL 片段与越权引用
- 多轮槽位继承 + 结构化 Plan Patch + 会话状态版本控制

### 2. 确定性时间语义

- 解析本期 / 上期 / 近 N 天 / 月初至今 / 季度 / 年度等相对表达式
- 同比、环比、固定基期、自定义比较窗口
- 正确处理闰年、月末、跨年、DST、IANA 时区
- 支持自然年、财务年、自定义财务日历
- 时间范围绑定指标自身 `time_basis`，不默认套用数据集任意日期字段

### 3. 企业语义层 + Trusted Asset 复用

- 数据域、字段、指标（原子 / 派生 / 比率 / 半可加）、枚举、词表、关系统一管理
- Metric DAG 解析复合指标依赖与单位运算；Join Graph 管理关联键、基数、fanout
- Semantic Lint 检查字段引用、循环依赖、聚合合法性、Join 基数、枚举冲突
- Semantic Revision 支持草稿 / 校验 / 审批 / 发布 / Diff / 回滚
- Trusted Asset 版本化复用，命中后重新执行时间解析、权限编译、方言编译与安全检查；语义升级、策略变化、数据漂移、资产过期自动触发失效与重验

### 4. 纵深安全防御

- OIDC/OAuth 2.0、企业 SSO、JWKS 轮转、不可变 `PrincipalContext`
- RBAC + ABAC + 租户隔离 + 对象 / 行 / 列级策略；列权限遵循 `DENY > MASK > ALLOW`
- SQL AST 强制 SELECT-only、表 / 列 / 函数 Allowlist、强制 LIMIT、EXPLAIN 预算
- 独立只读数据库账户 + 连接级 read-only + statement/lock timeout
- 无法解析权限、计划超预算、结果异常、证据不完整时 **fail closed**

### 5. 可验证答案 + 完整 Trace

- 返回自然语言结论、结果表、图表数据、下钻建议；每个答案关联 conversation / turn / run / trace
- 引证指标名与版本、时间范围、`time_basis`、业务过滤；区分"用户过滤"与"权限附加"
- Decimal 全链路保持精度，不使用浮点替代财务数值
- Trace 包含阶段输入输出、模型、Token、耗时、Plan hash、SQL hash；支持原始意图 / 版本 / SQL 重放与结果 Diff

### 6. 端到端可观测 + 分层评测

- 逐阶段记录 verified recall / intent / resolve / compile / security / execute / answer
- 普通用户、数据所有者、Trace 审计员具有不同的 Trace 与物理 SQL 可见范围
- 支持 Golden / Holdout / Security / Multiturn / Robustness / Temporal 六类数据集
- 语义 / 策略 / 编译器变更自动触发回归评测；记录准确率、澄清率、拒答率、延迟、Token、扫描量、执行成本

---

## 系统架构

![企业级数据智能体系统架构](docs/assets/readme/data-agent-architecture.png)

### 查询执行流程

1. 验证用户身份，构造包含 `tenant` / `role` / `group` / `attribute` 的 `PrincipalContext`
2. 根据用户权限筛选可见的数据域、指标、字段、枚举与 Trusted Asset
3. Domain Router 选择对应专业数据域，LLM 输出受约束的 `IntentV2`
4. Time Resolver、实体解析器、语义解析器补全确定性值；歧义时返回澄清请求
5. 构建 Canonical Query Plan，固定语义版本、指标依赖、时间口径、Join 路径
6. Policy Compiler 根据用户、租户、策略版本生成 Secured Execution Plan
7. 编译数据源方言 SQL，依次执行 AST / Allowlist / LIMIT / EXPLAIN / 预算检查
8. 通过只读连接执行查询，校验结果结构、精度、完整性与比较周期
9. 生成答案、引证、警告、下钻建议，并持久化完整 Trace 与反馈上下文

---

## 快速开始

### 环境要求

- Python 3.12+
- Node.js LTS + npm
- PostgreSQL 14+
- OpenAI 兼容模型服务（支持 Structured Outputs）

### 30 秒跑通 Sample（推荐新用户体验）

```bash
# 1. 克隆并准备数据库
git clone <repo-url> data-agent && cd data-agent
createdb data_agent_demo
python backend/scripts/init_db.py           # 创建元数据表 + 装载示例语义
psql -d data_agent_demo -f backend/scripts/sample_data.sql   # 装载示例业务数据

# 2. 启动后端（dev 模式）
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                        # 默认连接本地 postgres
uvicorn app.main:app --reload --port 8000

# 3. 启动前端
cd ../frontend && npm ci && npm run dev
```

打开 <http://localhost:5173> 进入工作台，问一句"本月华东销售额按省份排名"即可看到端到端结果。

### 完整生产部署

```bash
# 1. 创建两个数据库账号
psql -U postgres -c "CREATE ROLE data_agent_writer LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB;"
psql -U postgres -c "CREATE ROLE data_agent_reader LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB;"
psql -d data_agent -f backend/scripts/create_reader_role.sql

# 2. 后端（生产配置）
cd backend
pip install -r requirements.txt
ENVIRONMENT=production AUTH_MODE=oidc \
  META_DATABASE_URL=... SAMPLE_DATABASE_URL=... \
  OIDC_ISSUER=... OIDC_AUDIENCE=... OIDC_JWKS_URL=... \
  LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=gpt-4o-mini \
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

访问地址：

- Web 工作台：<http://localhost:5173>
- OpenAPI 文档：<http://localhost:8000/docs>
- Liveness：<http://localhost:8000/livez>
- Readiness：<http://localhost:8000/readyz>

> 生产环境必须使用 OIDC Bearer Token。`AUTH_MODE=dev` 仅供本地开发。

---

## 核心数据契约

### IntentV2

```json
{
  "kind": "aggregate",
  "metrics": ["sales_revenue"],
  "dimensions": ["province"],
  "filters": [
    {"field": "region_code", "operator": "in", "spoken_values": ["华东"]}
  ],
  "time_expression": {
    "kind": "relative", "unit": "month", "offset": 0, "expression": "本月"
  },
  "comparison": "mom",
  "sort": {"by": "sales_revenue", "descending": true, "limit": 10},
  "confidence": {
    "overall": 0.96, "metric": 0.99, "time": 0.94,
    "dimension": 0.98, "filter": 0.97
  }
}
```

### Canonical Query Plan

```json
{
  "dataset": "orders",
  "semantic_revision": "rev_20260813_01",
  "metrics": [{"name": "sales_revenue", "version": 3}],
  "dimensions": ["province"],
  "time_windows": {
    "current": ["2026-08-01", "2026-08-31"],
    "comparison": ["2026-07-01", "2026-07-31"],
    "time_field": "completed_date"
  },
  "predicates": [{"field": "region_code", "operator": "in", "values": ["EC"]}],
  "sort": {"by": "sales_revenue", "direction": "desc"},
  "limit": 10
}
```

### Secured Execution Plan

```json
{
  "canonical_plan_hash": "sha256:...",
  "principal_id": "user_123",
  "tenant_id": "tenant_a",
  "policy_revision": "policy_42",
  "row_filters": [{"field": "region_code", "operator": "in", "values": ["EC"]}],
  "column_decisions": {"customer_name": "mask", "cost": "deny"},
  "dialect": "postgres",
  "max_rows": 1000,
  "timeout_seconds": 30
}
```

---

## 查询能力

| 类型 | 示例 | 编译行为 |
|---|---|---|
| 单指标聚合 | 本月销售额 | 指标聚合 + 指标 `time_basis` + 固定口径 |
| 多指标 | 销售额、订单量、毛利率 | 统一依赖解析，比率指标重新计算 |
| 多维分组 | 按大区、渠道看销售额 | 维度投影 + 分组 + 枚举业务值映射 |
| 趋势 | 近 12 个月销售趋势 | 时间粒度展开 + 空周期处理 + 稳定排序 |
| 排名 / Top N | 销售额最高的 10 个省份 | 指标排序 + 限制 + 并列处理 |
| 同比 / 环比 | 本月销售额同比 | 当前期与基期独立窗口 + 结果对齐 + 变化率 |
| 明细查询 | 华东未完成订单明细 | 可查询字段投影 + 强制 LIMIT + 列权限 |
| 复合指标 | 毛利率按渠道对比 | Metric DAG 展开 + 单位检查 + 比率重算 |
| 多表分析 | 客户分层与订单贡献 | Join Graph 选路 + 基数验证 + fanout 防护 |
| 多轮追问 | 再按省份拆开，只看线上 | 继承上一轮 Plan + 应用结构化 Patch |

---

## 安全模型

| 层级 | 控制点 |
|---|---|
| Identity | OIDC Token、issuer/audience、JWKS、用户状态、tenant |
| Context | 不可见数据域、指标、字段、枚举不进入 Prompt 与检索候选 |
| Semantic | 指标/维度/过滤/排序/Join 字段基于 lineage 授权 |
| Plan | Principal、策略版本、行过滤、列决策、执行预算绑定 |
| SQL AST | SELECT-only、表/列/函数 Allowlist、无副作用、强制 LIMIT |
| Runtime | 独立只读账户、RLS、statement/lock timeout、资源配额 |
| Result | 脱敏、截断、敏感字段检查、导出权限 |
| Trace | public / user-private / sensitive / admin-only 分级可见与保留 |

### 生产环境 Checklist

启动时会校验下列项，任何一项未通过则拒绝启动：

- [ ] JWT 密钥强度足够
- [ ] 数据库凭证非默认值
- [ ] `AUTH_MODE != dev`
- [ ] CORS 白名单非通配
- [ ] OIDC issuer/audience/JWKS 已配置
- [ ] 元数据写入与业务查询使用独立 DB 账号
- [ ] 业务数据库已启用只读视图或只读角色

---

## API v2

API v2 用持久化 Query Run 表达一次完整数据任务，支持幂等创建、状态查询、SSE 进度、澄清、取消、结果恢复。

### 创建 Query Run

```bash
curl -X POST http://localhost:8000/api/v2/query-runs \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Idempotency-Key: demo-001" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "sales-intelligence", "question": "本月华东销售额环比如何？"}'
```

```json
{
  "run_id": "qr_01JY8K4M2X",
  "status": "running",
  "events_url": "/api/v2/query-runs/qr_01JY8K4M2X/events",
  "state_version": 1
}
```

### 核心端点

```text
POST /api/v2/query-runs                          创建 Query Run
GET  /api/v2/query-runs/{run_id}                 状态查询
GET  /api/v2/query-runs/{run_id}/events          SSE 进度
POST /api/v2/query-runs/{run_id}/clarifications  提交澄清
POST /api/v2/query-runs/{run_id}/cancel          取消
GET  /api/v2/conversations/{id}                  会话与轮次

GET  /api/semantic/datasets                      数据集列表
GET  /api/semantic/datasets/{name}               数据集详情
GET  /api/semantic/datasets/{name}/lint          语义体检
POST /api/semantic/datasets/{name}/publish       发布语义版本

GET  /api/trace/turns/{turn_id}                  查看 Trace
POST /api/trace/turns/{turn_id}/replay           重放
```

所有写请求支持幂等键；会话与 Query Run 使用 `state_version` 乐观锁；错误响应提供稳定 `error_code`、安全文案与 `trace_id`。

---

## 配置

| 变量 | 说明 | 默认 |
|---|---|---|
| `ENVIRONMENT` | `development` / `test` / `production` | `development` |
| `AUTH_MODE` | `dev` / `oidc` | `dev` |
| `META_DATABASE_URL` | 元数据、语义、会话、Trace DB | 本地 PostgreSQL |
| `SAMPLE_DATABASE_URL` | 业务数据只读连接 | 本地 PostgreSQL |
| `OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL` | OIDC 配置 | — |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 模型服务 | OpenAI / `gpt-4o-mini` |
| `LLM_TIMEOUT_SECONDS` | 模型请求超时 | `30` |
| `CLARIFY_CONFIDENCE_THRESHOLD` | 触发澄清的置信度阈值 | `0.7` |
| `CLARIFY_MAX_ROUNDS` | 最大澄清轮数 | `2` |
| `MAX_RESULT_ROWS` | 最大结果行数 | `1000` |
| `QUERY_TIMEOUT_SECONDS` | 数据查询超时 | `30` |
| `COST_WARN_ROWS` / `COST_REJECT_ROWS` | 扫描量告警/拒绝阈值 | `1M` / `50M` |
| `EXECUTION_RETRY_ATTEMPTS` | 瞬时执行错误重试 | `2` |

---

## 测试与评测

后端 **908+ 测试** 覆盖意图解析、时间语义、语义层、规划、编译、安全、执行、Trace、Golden Set 全链路；前端 12 个 Vitest spec 覆盖工作台、澄清、答案卡片、语义管理、Trace 视图。

```bash
cd backend && python -m pytest -q            # 后端
cd frontend && npm test && npm run build     # 前端
```

分层评测（`backend/app/evals/`）：

- **Intent**：意图字段、置信度、Schema 合法性
- **Resolve**：时间、实体、语义槽位解析
- **Plan**：Canonical Plan 稳定性、签名去重
- **SQL**：AST 合法性、Allowlist、方言稳定性
- **Result**：行数、精度、排序、当前期/比较期对齐
- **Answer**：引证完整性、警告合理性、Decimal 一致性

数据集类型：**Golden** / Security / Multiturn / Robustness / Temporal / Holdout。语义、策略、编译器变更自动触发回归。

---

## 技术栈

| 职责 | 技术 |
|---|---|
| 前后端 | Vue 3 + TypeScript + Pinia + Element Plus + Vite；FastAPI + Pydantic + SSE + OpenAPI |
| LLM 与编译 | OpenAI Compatible + Structured Outputs + SQLGlot AST + 确定性 SQL 编译器 |
| 存储与语义 | PostgreSQL + SQLAlchemy + Metric DAG + Join Graph + Semantic Revision + Trusted Asset |
| 安全 | OIDC/OAuth2 + RBAC/ABAC + AST Allowlist + 行级 / 列级 + RLS + Fail-closed |
| 质量与可观测 | Pytest + Vitest + Golden Set + 分层评测；Stage Trace + Replay + OpenTelemetry |

---

## 项目结构

```text
backend/app/
├── api/             # Chat、Query Run、Semantic、Eval、Trace API
├── auth/            # OIDC/JWT、PrincipalContext、对象级鉴权
├── core/            # 配置、数据库连接、Clock、错误基类
├── intent/          # IntentV2、Prompt、Structured Outputs、注入防护
├── temporal/        # 时区、财务日历、确定性时间解析
├── semantic/        # Registry、Metric DAG、Join Graph、Revision、Trusted Asset
├── planning/        # Canonical Query Plan、字段 lineage、稳定序列化
├── compiler/        # 指标、谓词、时间窗口、SQL 编译
├── security/        # Policy Compiler、AST Guardrails、行列权限、成本控制
├── execution/       # 只读执行、重试、结果校验
├── pipeline/        # 查询编排、解析、澄清、答案生成
├── observability/   # Query Run、Trace、Replay、Cache、Audit
└── evals/           # 分层评测、回归运行、质量报告

frontend/src/
├── api/             # API Client 与类型契约
├── components/      # 问数、澄清、结果、引证、Trace 组件
├── stores/          # 会话与 Query Run 状态
└── views/           # 工作台、语义管理、评测、Trace 页面
```

---

## 部署

在线查询链路采用模块化单体，减少关键路径的网络跳转；异步评测、批量分析、定时任务由独立 Worker 执行。控制面元数据与业务数据连接相互分离，业务数据库只暴露只读视图或只读角色。

```text
Load Balancer / API Gateway
             │
       Data Agent API
             │
   ┌─────────┴──────────┐
   │                    │
Metadata PostgreSQL   Read-only Warehouse
   │
Redis / Job Queue
   │
Eval / Async / Scheduled Workers
```

服务提供 `/livez` 与 `/readyz` 探针，可通过 OpenTelemetry 接入现有日志、指标、Trace、告警平台。

---

## 扩展

- 新增数据源：实现方言编译器、连接适配器、函数 Allowlist
- 新增业务域：注册数据集、指标、枚举、Join Graph、领域词表
- 新增分析能力：以类型化 Skill 声明输入、输出、权限、副作用
- 新增模型服务：实现统一 LLM Provider 接口与失败分类
- 外部 Agent：通过 Governed Semantic MCP 或 OpenAPI 复用相同语义与权限边界

---

## Roadmap

**已交付**

- S1~S7 七层升级路线：身份基座、时间基座、确定性编译、语义层、可信答案、可观测、交付
- IntentV2 + Structured Outputs + Prompt Injection 防护
- Canonical Plan + Secured Plan + 稳定序列化 + 字段 lineage
- Metric DAG / Join Graph / Semantic Revision / Lint
- Trusted Asset 版本化复用与失效重验
- 分层评测（Intent / Resolve / Plan / SQL / Result / Answer）+ Golden / Holdout 数据集
- OIDC、行列权限、AST Allowlist、EXPLAIN 预算、Fail-closed
- 完整 Trace 与 Replay，三层可见性分级
- Vue 3 工作台 + 语义管理 + Trace 与 Eval 视图

**计划中**

- ClickHouse / Doris 方言适配器
- 多租户资源配额与速率限制
- Governed Semantic MCP Server 公开版
- SQL 编辑器内联 Plan 预览
- 实时协作追问（多人同会话）

---

## FAQ

**和 LangChain SQL Agent 区别？**
LangChain 让 LLM 自由生成 SQL，灵活但不可控。本项目把 LLM 锁在语言理解层，SQL 由确定性编译器产出，可测试、可缓存、可审计。

**和 Vanna 区别？**
Vanna 是 RAG + LLM 生成 SQL，适合个人/小团队。本项目定位企业级：语义版本管理、行级权限、Trace 重放、Golden Set 评测。

**和 WrenAI 区别？**
WrenAI 强项是 MDL 语义层与 BI 自助分析；本项目强项是企业安全、可观测、可重放、可评测，适合合规要求严格的内部数据团队。

**为什么不让 LLM 直接生成 SQL？**
企业生产环境的 SQL 错误成本高（删库、越权、口径漂移）。LLM 适合语言模糊，SQL 适合确定编译。

**必须用 PostgreSQL 吗？**
核心运行时元数据依赖 PostgreSQL；业务数据方言支持 PostgreSQL，其他方言可通过扩展点接入。

**支持私有部署吗？**
支持。所有组件可内网部署，无外部依赖（除可选的 LLM API）。

---

## 贡献 / 社区 / License

**贡献** — 提交改动时请说明：

- 影响的语义契约（IntentV2 / Canonical Plan / Secured Plan）
- 影响的失败边界（澄清、拒答、Fail-closed）
- 影响的权限路径（行列 / 行级 / 租户）
- 影响的 Trace 字段
- 为确定性逻辑、安全边界、最终结果补充测试或 Golden Case

**社区** — Issues 用于 Bug 报告与功能请求；Discussions 用于架构讨论与方案咨询。欢迎 Star ⭐ 与 Fork。

**License** — 本项目基于 [MIT License](./LICENSE) 开源。Copyright © 2026 付玉祥。