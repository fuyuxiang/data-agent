<div align="center">

# 企业级数据智能体

### Governed natural-language analytics with deterministic planning, secure execution, and verifiable answers

面向企业结构化数据的数据智能体实现：将自然语言问题转换为受治理、可审计、可重放的数据查询与分析结果。

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-14%2B-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![SQLGlot](https://img.shields.io/badge/SQL%20AST-SQLGlot-6366F1)](https://sqlglot.com/)

[核心特性](#核心特性) · [架构](#系统架构) · [查询能力](#查询能力) · [安全](#安全模型) · [API](#api-v2) · [快速开始](#快速开始) · [测试](#测试)

</div>

## 项目简介

本项目实现了一套面向企业数据分析的数据智能体。业务用户使用自然语言提出问题，系统在业务语义、指标口径和用户权限的约束下完成意图识别、时间解析、查询规划、安全编译、只读执行、结果校验和答案生成。

与直接让大模型生成 SQL 的方案不同，本项目将 LLM 限制在语言理解和结构化意图提取阶段。指标计算、时间窗口、表间关系、访问控制和物理 SQL 均由确定性组件处理。相同语义版本和相同规范计划会产生稳定的查询结构，便于测试、缓存、审计和回放。

一次查询不仅返回数字，还返回指标版本、时间口径、用户过滤、权限附加条件、数据更新时间、警告、下钻入口和完整阶段 Trace。

## 核心特性

### 受约束的自然语言理解

- 使用 Structured Outputs 将模型输出约束为严格的 `IntentV2`；
- 分别识别指标、维度、过滤、时间、比较、排序、Top N 和查询类型；
- 为整体意图及各个槽位记录独立置信度；
- 模型返回值必须通过 Pydantic Schema、语义引用和类型校验；
- Prompt 仅包含当前用户可见的数据域、指标、字段和枚举；
- 拒绝模型输出中的 SQL 片段、未知字段和越权语义引用；
- 支持多轮槽位继承、结构化 Plan Patch 和会话状态版本控制；
- 对低置信度、实体歧义、口径冲突和时间缺失返回结构化澄清。

### 确定性时间语义

- 解析本期、上期、近 N 天、月初至今、季度、年度等相对时间表达式；
- 支持同比、环比、固定基期和自定义比较窗口；
- 正确处理月末、闰年、跨年、夏令时和 IANA 时区；
- 支持自然年、财务年和自定义财务日历；
- 时间范围绑定指标自身的 time basis，不默认套用数据集中的任意日期字段；
- 多指标时间口径不一致时分别规划或进入澄清流程；
- 通过可注入 `Clock` 保证测试、回放和历史查询结果稳定。

### 企业语义层

- 统一管理数据域、数据集、实体、字段、指标、枚举、词表和关系；
- 支持原子指标、派生指标、比率指标和半可加指标；
- 指标携带版本、负责人、描述、单位、精度、格式、可加性和时间口径；
- Metric DAG 解析复合指标依赖、单位运算和字段 lineage；
- Join Graph 管理关联键、基数、允许方向和 fanout 策略；
- 枚举支持业务名称、物理值、别名、多语言表达和有效期；
- Semantic Lint 检查字段引用、循环依赖、聚合合法性、时间字段、Join 基数和枚举冲突；
- Semantic Revision 支持草稿、校验、审批、发布、Diff、回滚和历史版本复现。

### 规范查询计划

- `Canonical Query Plan` 表达与用户权限无关的规范语义查询；
- Plan 固定指标版本、维度、时间窗口、类型化过滤、排序、限制和关联路径；
- 所有字段引用携带 lineage，供编译、权限、引证和评测共同使用；
- Plan 采用稳定序列化和 Canonical Signature，可用于比较、缓存和去重；
- `Secured Execution Plan` 在 Canonical Plan 上绑定用户、租户、策略版本和执行预算；
- 语义计划与权限计划分离，防止跨用户复用已施加权限的结果；
- 查询、Plan、SQL、结果和答案均支持结构化 Diff 与重放。

### 确定性 SQL 编译

- 根据语义模型和 Canonical Plan 生成 SQL，不采用模型生成的物理查询；
- 支持聚合、明细、趋势、排名、Top N、多指标和多维分组；
- 支持同比、环比、复合指标、比率重算和 fixed predicate；
- 根据 Join Graph 生成受治理的多表关联，检查一对多 fanout 风险；
- 使用 SQLGlot 构造和检查 AST，业务计划与数据源方言相互分离；
- 参数值与 SQL 结构分离，避免字符串拼接和注入；
- 同一 Plan、语义版本和方言生成稳定、可比较的 SQL AST。

### 安全执行

- OIDC/OAuth 2.0、企业 SSO、JWKS 轮转和不可变 `PrincipalContext`；
- RBAC + ABAC、租户隔离、对象级鉴权、行级策略和列级策略；
- 列权限遵循 `DENY > MASK > ALLOW`，多角色组合不能绕过显式拒绝；
- 基于字段 lineage 检查 measure、dimension、filter、sort 和 join key；
- SQL AST 强制 SELECT-only、表/列/函数 Allowlist 和结果行数上限；
- 执行前通过 EXPLAIN 估算扫描量并应用成本阈值；
- 使用独立只读数据库账户、连接级 read-only、statement timeout 和 lock timeout；
- 无法解析权限、计划超预算、结果异常或证据不完整时 fail closed。

### 可验证答案

- 返回自然语言结论、结构化结果表、图表数据和下钻建议；
- 引证指标名称与版本、时间范围、time basis 和业务过滤条件；
- 区分用户指定过滤与权限自动附加过滤；
- 展示数据更新时间、结果截断、精度处理和异常警告；
- Decimal 全链路保持精度，不使用浮点数替代财务数值；
- 校验结果列、行数、排序、空值、当前期和比较期数据；
- 每个答案关联 conversation、turn、run、request 和 trace 标识。

### Trusted Assets

- 将审核通过的高频查询保存为版本化 Trusted Asset；
- 保存 Canonical Plan 或逻辑语义查询，不固化不可治理的永久物理 SQL；
- 支持触发问题、参数 Schema、适用数据域、语义版本、审核人和有效期；
- 通过精确问题、Canonical Signature 和语义检索进行多路召回；
- 命中后重新执行时间解析、权限编译、方言编译和安全检查；
- 语义升级、策略变化、数据漂移和资产过期自动触发失效与重验；
- 运行时资产与 Holdout 评测集隔离，防止测试数据泄漏到上下文。

### 可观测性与评测

- 逐阶段记录 verified recall、intent、resolve、compile、security、execute 和 answer；
- Trace 包含阶段输入输出、模型、Token、耗时、Plan hash、SQL hash 和错误分类；
- 普通用户、数据所有者和 Trace 审计员具有不同的 Trace 与物理 SQL 可见范围；
- 支持原始意图重放、版本重放、SQL 重编译和结果 Diff；
- 分层评测 Intent、Resolve、Plan、SQL、Result 和 Answer；
- 支持 Golden、Holdout、Security、Multiturn、Robustness 和 Temporal 数据集；
- 模型、Prompt、语义、策略和编译器变更自动触发回归评测；
- 记录准确率、澄清率、拒答率、延迟、Token、扫描量和执行成本。

### Web 工作台与管理端

- 对话式问数、连续追问、结构化澄清、条件编辑和重新运行；
- 答案卡片、结果表、引证、假设、警告、反馈和下钻入口；
- Query Run 实时进度、SSE 事件、取消、失败重试和状态恢复；
- 会话列表、历史轮次、分享、导出和用户级数据隔离；
- 语义数据集、字段、指标、关系、版本、Lint、审批和发布管理；
- Trace 时间线、阶段详情、SQL、成本、重放和版本对比；
- Eval Run、回归报告、失败样本和质量趋势管理。

## 系统架构

![企业级数据智能体系统架构](docs/assets/readme/data-agent-architecture.png)

### 查询执行流程

1. 验证用户身份，构造包含 tenant、role、group 和 attribute 的 `PrincipalContext`。
2. 根据用户权限筛选可见的数据域、指标、字段、枚举和 Trusted Asset。
3. Domain Router 选择对应的专业数据域，LLM 输出受约束的 `IntentV2`。
4. Time Resolver、实体解析器和语义解析器补全确定性值；存在歧义时返回澄清请求。
5. 构建 Canonical Query Plan，固定语义版本、指标依赖、时间口径和 Join 路径。
6. Policy Compiler 根据用户、租户和策略版本生成 Secured Execution Plan。
7. 编译数据源方言 SQL，依次执行 AST、Allowlist、LIMIT、EXPLAIN 和预算检查。
8. 通过只读连接执行查询，校验结果的结构、精度、完整性和比较周期。
9. 生成答案、引证、警告和下钻建议，并持久化完整 Trace 与反馈上下文。

## 核心数据契约

### IntentV2

`IntentV2` 是语言理解层与确定性系统之间的边界。模型只能输出该结构，不能输出 SQL 或授权决策。

```json
{
  "kind": "aggregate",
  "metrics": ["sales_revenue"],
  "dimensions": ["province"],
  "filters": [
    {
      "field": "region_code",
      "operator": "in",
      "spoken_values": ["华东"]
    }
  ],
  "time_expression": {
    "kind": "relative",
    "unit": "month",
    "offset": 0,
    "expression": "本月"
  },
  "comparison": "mom",
  "sort": {
    "by": "sales_revenue",
    "descending": true,
    "limit": 10
  },
  "confidence": {
    "overall": 0.96,
    "metric": 0.99,
    "time": 0.94,
    "dimension": 0.98,
    "filter": 0.97
  }
}
```

### Canonical Query Plan

```json
{
  "dataset": "orders",
  "semantic_revision": "rev_20260813_01",
  "metrics": [
    {"name": "sales_revenue", "version": 3}
  ],
  "dimensions": ["province"],
  "time_windows": {
    "current": ["2026-08-01", "2026-08-31"],
    "comparison": ["2026-07-01", "2026-07-31"],
    "time_field": "completed_date"
  },
  "predicates": [
    {"field": "region_code", "operator": "in", "values": ["EC"]}
  ],
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
  "row_filters": [
    {"field": "region_code", "operator": "in", "values": ["EC"]}
  ],
  "column_decisions": {
    "customer_name": "mask",
    "cost": "deny"
  },
  "dialect": "postgres",
  "max_rows": 1000,
  "timeout_seconds": 30
}
```

## 查询能力

| 查询类型 | 示例 | 计划与编译行为 |
|---|---|---|
| 单指标聚合 | 本月销售额 | 指标聚合 + 指标 time basis + 固定口径 |
| 多指标 | 销售额、订单量和毛利率 | 统一依赖解析，比率指标重新计算 |
| 多维分组 | 按大区和渠道看销售额 | 维度投影、分组和枚举业务值映射 |
| 趋势 | 近 12 个月销售趋势 | 时间粒度展开、空周期处理和稳定排序 |
| 排名 / Top N | 销售额最高的 10 个省份 | 指标排序、限制和并列处理 |
| 同比 / 环比 | 本月销售额同比如何 | 当前期与基期独立窗口、结果对齐和变化率 |
| 明细查询 | 华东未完成订单明细 | 可查询字段投影、强制 LIMIT 和列权限 |
| 复合指标 | 毛利率按渠道对比 | Metric DAG 展开、单位检查和比率重算 |
| 多表分析 | 客户分层与订单贡献 | Join Graph 选路、基数验证和 fanout 防护 |
| 多轮追问 | 再按省份拆开，只看线上 | 继承上一轮 Plan，应用结构化 Patch |

## 安全模型

安全检查不是 API 外层的单点校验，而是贯穿上下文构建、计划、SQL、执行和答案的纵深防御。

| 层级 | 控制点 |
|---|---|
| Identity | OIDC Token、issuer/audience、JWKS、用户状态和 tenant |
| Context | 不可见数据域、指标、字段和枚举不进入 Prompt 与检索候选 |
| Semantic | 指标/维度/过滤/排序/Join 字段基于 lineage 授权 |
| Plan | Principal、策略版本、行过滤、列决策和执行预算绑定 |
| SQL AST | SELECT-only、表列函数 Allowlist、无副作用、强制 LIMIT |
| Runtime | 独立只读账户、RLS、statement timeout、lock timeout 和资源配额 |
| Result | 脱敏、截断、敏感字段检查和导出权限 |
| Trace | public、user-private、sensitive、admin-only 分级可见与保留 |

生产环境启动时会校验弱 JWT 密钥、默认数据库凭证、开发认证模式、宽松 CORS、缺失 OIDC 配置和读写数据库角色未分离等问题。

## API v2

API v2 使用持久化 Query Run 表达一次完整数据任务，支持幂等创建、状态查询、SSE 进度、澄清、取消和结果恢复。

### 创建 Query Run

```bash
curl -X POST http://localhost:8000/api/v2/query-runs \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Idempotency-Key: demo-001" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "sales-intelligence",
    "question": "本月华东销售额环比如何？"
  }'
```

```json
{
  "run_id": "qr_01JY8K4M2X",
  "status": "running",
  "events_url": "/api/v2/query-runs/qr_01JY8K4M2X/events",
  "state_version": 1
}
```

### 端点

```text
POST /api/v2/query-runs
GET  /api/v2/query-runs/{run_id}
GET  /api/v2/query-runs/{run_id}/events
POST /api/v2/query-runs/{run_id}/clarifications
POST /api/v2/query-runs/{run_id}/cancel
GET  /api/v2/conversations/{conversation_id}?include=turns,answers

GET  /api/semantic/datasets
GET  /api/semantic/datasets/{name}
GET  /api/semantic/datasets/{name}/lint
POST /api/semantic/datasets/{name}/publish

GET  /api/trace/turns/{turn_id}
POST /api/trace/turns/{turn_id}/replay
```

所有写请求支持幂等键；会话和 Query Run 使用 `state_version` 乐观锁；错误响应提供稳定的 `error_code`、安全错误文案和 `trace_id`。

## 快速开始

### 环境要求

- Python 3.12 或更高版本
- Node.js LTS 与 npm
- PostgreSQL 14 或更高版本
- 支持 Structured Outputs 的 OpenAI 兼容模型服务

### 1. 配置并启动后端

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，配置数据库和模型服务

python -m scripts.init_db
uvicorn app.main:app --reload
```

### 2. 启动前端

```bash
cd frontend
npm ci
npm run dev
```

访问：

- Web 工作台：<http://localhost:5173>
- OpenAPI：<http://localhost:8000/docs>
- Liveness：<http://localhost:8000/livez>
- Readiness：<http://localhost:8000/readyz>

### 3. 提交一个问题

```bash
curl -X POST http://localhost:8000/api/chat/ask \
  -H 'Content-Type: application/json' \
  -H 'X-Username: admin' \
  -d '{
    "question": "2026 年 8 月华东销售额按省份排名",
    "dataset_name": "orders"
  }'
```

`X-Username` 仅用于本地开发。生产环境使用 `AUTH_MODE=oidc` 和 Bearer Token。

## 配置

主要环境变量：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ENVIRONMENT` | `development`、`test` 或 `production` | `development` |
| `AUTH_MODE` | `dev` 或 `oidc` | `dev` |
| `META_DATABASE_URL` | 元数据、语义、会话和 Trace 数据库 | 本地 PostgreSQL |
| `SAMPLE_DATABASE_URL` | 业务数据只读连接 | 本地 PostgreSQL |
| `OIDC_ISSUER` | OIDC issuer | — |
| `OIDC_AUDIENCE` | Token audience | — |
| `OIDC_JWKS_URL` | JWKS 地址 | — |
| `LLM_BASE_URL` | OpenAI 兼容 API 地址 | OpenAI API |
| `LLM_API_KEY` | 模型服务密钥 | — |
| `LLM_MODEL` | Structured Outputs 模型 | `gpt-4o-mini` |
| `LLM_TIMEOUT_SECONDS` | 模型请求超时 | `30` |
| `CLARIFY_CONFIDENCE_THRESHOLD` | 触发澄清的置信度阈值 | `0.7` |
| `CLARIFY_MAX_ROUNDS` | 最大澄清轮数 | `2` |
| `MAX_RESULT_ROWS` | 最大结果行数 | `1000` |
| `QUERY_TIMEOUT_SECONDS` | 数据查询超时 | `30` |
| `COST_WARN_ROWS` | 扫描行数警告阈值 | `1000000` |
| `COST_REJECT_ROWS` | 扫描行数拒绝阈值 | `50000000` |
| `EXECUTION_RETRY_ATTEMPTS` | 瞬时执行错误重试次数 | `2` |

生产部署应为元数据写入和业务数据查询配置不同的数据库角色，并在业务数据库侧启用最小权限、RLS 或安全视图。

## 测试

### 后端

```bash
cd backend
python -m pytest -q
```

测试覆盖：

- Intent Schema、Structured Outputs 和 Prompt Injection；
- 时区、DST、闰年、月末、财务日历和相对时间；
- Metric DAG、Join Graph、Semantic Lint 和 Revision；
- Canonical Plan、字段 lineage、稳定序列化和 SQL 编译；
- 行列权限、AST Allowlist、成本控制和只读执行；
- 结果校验、答案引证、Trace、Replay 和事务边界；
- Golden Set、分层 Eval、回归判定和安全拒答。

### 前端

```bash
cd frontend
npm test
npm run build
```

前端测试覆盖工作台、澄清、条件编辑、答案卡片、结果表、语义管理和 Trace 页面。

## 技术栈

| 层 | 技术 |
|---|---|
| Web | Vue 3、TypeScript、Pinia、Element Plus、Vite |
| API | FastAPI、Pydantic、SSE、OpenAPI |
| Metadata | SQLAlchemy、PostgreSQL |
| Intent | OpenAI Compatible API、Structured Outputs、IntentV2 |
| Semantic | Semantic Registry、Metric DAG、Join Graph、Revision |
| Planning | Canonical Query Plan、Secured Execution Plan |
| SQL | SQLGlot AST、Deterministic Compiler、Dialect Adapter |
| Security | OIDC/OAuth2、RBAC/ABAC、Policy Compiler、RLS |
| Runtime | Read-only Connection、Cost Control、Result Validation |
| Quality | Pytest、Vitest、Golden Set、Layered Evaluation |
| Observability | Stage Trace、Replay、Audit、OpenTelemetry |
| Extension | OpenAPI、MCP、Webhook、Worker、Custom Skill |

## 项目结构

```text
backend/app/
├── api/             # Chat、Query Run、Semantic、Eval 与 Trace API
├── auth/            # OIDC/JWT、PrincipalContext 与对象级鉴权
├── core/            # 配置、数据库连接、时钟和错误基类
├── intent/          # IntentV2、Prompt、Structured Outputs 与注入防护
├── temporal/        # 时区、财务日历与确定性时间解析
├── semantic/        # Registry、Metric DAG、Join Graph、Revision 与 Trusted Asset
├── planning/        # Canonical Query Plan、字段 lineage 与稳定序列化
├── compiler/        # 指标、谓词、时间窗口和 SQL 编译
├── security/        # Policy Compiler、AST Guardrails、行列权限与成本控制
├── execution/       # 只读执行、重试与结果校验
├── pipeline/        # 查询编排、解析、澄清和答案生成
├── observability/   # Query Run、Trace、Replay、Cache 与 Audit
└── evals/           # 分层评测、回归运行和质量报告

frontend/src/
├── api/             # API Client 与类型契约
├── components/      # 问数、澄清、结果、引证和 Trace 组件
├── stores/          # 会话和 Query Run 状态
└── views/           # 工作台、语义管理、评测与 Trace 页面
```

## 部署

在线查询链路采用模块化单体，减少关键路径中的网络跳转；异步评测、批量分析和定时任务由独立 Worker 执行。控制面元数据与业务数据连接相互分离，业务数据库只暴露只读视图或只读角色。

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

服务提供 `/livez` 和 `/readyz` 探针，并可通过 OpenTelemetry 接入现有日志、指标、Trace 和告警平台。

## 扩展

- 新增数据源时实现方言编译器、连接适配器和函数 Allowlist；
- 新增业务域时注册数据集、指标、枚举、Join Graph 和领域词表；
- 新增分析能力时以类型化 Skill 声明输入、输出、权限和副作用；
- 新增模型服务时实现统一 LLM Provider 接口和失败分类；
- 外部 Agent 可通过 Governed Semantic MCP 或 OpenAPI 复用相同的语义与权限边界。

## 参与贡献

提交改动时，请说明影响的语义契约、失败边界、权限路径和 Trace，并为确定性逻辑、安全边界和最终结果补充测试或 Golden Case。
