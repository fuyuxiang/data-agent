# S1 身份、鉴权与运行时基线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭 P0 全部安全与运行时缺口：接入 OIDC/PKCE 真实身份、`user_id` 成为唯一权限键、元数据事务真正提交、只读执行与函数白名单、production 配置 fail-fast、Trace 四级分级与错误脱敏。

**Architecture:** 身份验证是一道请求入口的独立层（`app/auth/`），产出不可变 `PrincipalContext` 后下游只消费它，不再各自 `load_principal(username)`。对象级鉴权复用 `observability/service.py` 已有且正确的 `_owned_conversation` 语义，抽成共享函数供 orchestrator 使用——这不是新建鉴权，是把已有的正确实现推广到漏掉的写路径。Trace 分级在**写入时**打标并分列存储，读取时只做拼接决策，不做过滤判断：忘记标级别应当是类型错误而非静默降级。

**Tech Stack:** Python 3.13、FastAPI、SQLAlchemy 2.0、sqlglot 25.x、PostgreSQL 15+、python-jose、pytest；前端 Vue 3 + TypeScript

## Global Constraints

约束来自 `docs/superpowers/specs/2026-08-13-s1-identity-authz-runtime-baseline-design.md`，每个任务隐含包含本节：

- **LLM 不参与任何授权决策**。权限全部是代码层确定性判断。
- **`user_id` 是唯一权限键**，`username` 降级为展示字段。任何用 `username` 做权限判断的路径都是缺陷。
- **"不存在"与"非本人"都返回 404**，不泄漏对象存在性，不解释原因。
- 越权拒答不得泄漏元数据：不出现表名、列名、数据集名。
- 算法白名单只允许 `RS256`/`ES256`，显式拒绝 `alg=none` 与 HS* 混淆。
- 函数白名单是白名单：未登记的函数名默认拒绝。
- 只读靠数据库 role 强制，AST 守卫是第二层而非唯一层。
- 安全测试必须 100% 通过，这是发布门禁。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

依赖既有计划 01～06 的产出（`app.semantic.*`、`app.compiler.*`、`app.security.*`、`app.observability.*`、`app.pipeline.orchestrator`）。本计划是其上的下一阶段改造，不替代它们。

改造前测试基线：**360 passed / 1 failed**（`test_aggregate_intent_requires_at_least_one_metric`，属 P0-08，归 S2）。该用例在本计划完成后仍可为红。

---

### Task 1: OIDC 验证与 PrincipalContext

**Files:**
- Create: `backend/app/auth/__init__.py`
- Create: `backend/app/auth/oidc.py`
- Create: `backend/app/auth/principal.py`
- Create: `backend/app/auth/provisioning.py`
- Create: `backend/app/auth/dependencies.py`
- Create: `backend/app/auth/dev.py`
- Create: `backend/tests/auth/__init__.py`
- Create: `backend/tests/auth/factories.py`
- Create: `backend/tests/auth/test_oidc.py`
- Create: `backend/tests/auth/test_provisioning.py`
- Modify: `backend/app/core/config.py`
- Delete: `backend/app/core/security.py`

**Interfaces:**
- Consumes: `app.core.config.Settings`、`app.security.orm.UserRow`、`app.security.principal.load_principal`
- Produces:
  - `app.auth.principal.PrincipalContext` — frozen dataclass `user_id`/`tenant_id`/`username`/`subject`/`roles`/`groups`/`attributes`/`auth_time`
  - `app.auth.oidc.JwksClient` — 按 `kid` 缓存公钥带 TTL；未知 `kid` 重取一次
  - `app.auth.oidc.verify_token(token, settings) -> dict` — 验 `iss`/`aud`/`exp`/`nbf`/签名，算法白名单
  - `app.auth.provisioning.provision_user(session, claims) -> UserRow` — 按 `oidc_subject` 幂等建用户
  - `app.auth.dependencies.get_principal` — FastAPI 依赖，每请求解析一次并缓存于 `request.state`
  - `app.auth.dependencies.require_roles(*names)` — 角色守卫依赖工厂
  - `app.auth.dev.dev_principal` — `AUTH_MODE=dev` 下的 `X-Username` 回退，仅该模式注册
  - `Settings` 新增 `environment`、`auth_mode`、`oidc_issuer`、`oidc_audience`、`oidc_client_id`、`oidc_client_secret`、`cors_origins`

- [ ] **Step 1: 写失败的 token 验证测试**

`backend/tests/auth/factories.py` 提供本地 RSA/EC 密钥对与签发工具（`issue_token(claims, *, alg="RS256", kid=...)`），以及可控 JWKS 端点桩。

`backend/tests/auth/test_oidc.py` 覆盖：
- 合法 RS256 token 通过，claims 正确解析
- 过期 token（`exp` 已过）被拒
- `nbf` 未到被拒
- 错误 `aud` 被拒
- 错误 `iss` 被拒
- `alg=none` 被拒
- HS256 用公钥当密钥伪造被拒（算法混淆）
- 未知 `kid` 触发一次 JWKS 重取；重取后仍未知则拒绝
- JWKS 缓存 TTL 内不重复请求

- [ ] **Step 2: 实现 `oidc.py` 与 `principal.py`**

`JwksClient` 内部 `dict[kid, key]` + 取回时间戳。`verify_token` 先解 header 取 `alg`/`kid`，`alg` 不在 `{RS256, ES256}` 立即拒绝（在取密钥之前，避免为伪造算法做无用工作）。

`PrincipalContext` 为 frozen dataclass。`roles` 用 `frozenset[str]`。

- [ ] **Step 3: 写失败的 JIT provisioning 测试**

`test_provisioning.py`：首次登录建 `UserRow` 且 `oidc_subject` 落值；同一 subject 二次调用不新建（幂等）；`display_name` 变化时更新；两个不同 subject 不冲突；`oidc_subject` 唯一约束生效。

- [ ] **Step 4: 实现 `provisioning.py` 与 `dependencies.py`**

`get_principal` 流程：取 `Authorization: Bearer` → `verify_token` → `provision_user` → `load_principal(session, user_id)` 合并角色 → 缓存 `request.state.principal`。缺 token 返回 401。

`require_roles` 返回的依赖在角色不足时返回 403（这是"已知身份但权限不足"，与 404 的对象级语义不同）。

- [ ] **Step 5: 实现 `dev.py` 并删除 `core/security.py`**

`dev.py` 只在 `auth_mode == "dev"` 时被 `dependencies` 装配。删除 `app/core/security.py`（`get_current_username`），其调用点在 Task 2、Task 3 迁移。

- [ ] **Step 6: 补角色种子数据**

`semantic_viewer`、`semantic_editor`、`semantic_approver`、`security_admin`、`trace_auditor`、`eval_operator` 六个角色写入种子。`RoleRow` 表结构不变。多角色列策略合并保持现有 lattice（DENY > MASK > ALLOW），语义不改。

---

### Task 2: `load_principal` 与服务层签名迁移

**Files:**
- Modify: `backend/app/security/principal.py`
- Modify: `backend/app/observability/service.py`
- Modify: `backend/app/api/chat.py`
- Modify: `backend/app/api/trace.py`
- Modify: `backend/app/pipeline/orchestrator.py`
- Modify: 对应测试（`backend/tests/observability/`、`backend/tests/api/`、`backend/tests/pipeline/`）

**Interfaces:**
- Changed: `load_principal(session, username)` → `load_principal(session, user_id)`
- Changed: `observability.service` 的 `list_conversations`、`list_turns`、`save_feedback`、`get_trace`、`replay_turn` 的 `username: str` → `principal: PrincipalContext`，服务层不再重复加载 principal

**这是一次机械迁移，不与其他改动混在同一提交**（约 15 处调用点 + 测试）。

- [ ] **Step 1: 改 `load_principal` 签名并修红**

改为按 `user_id` 查询。运行测试，逐个修复调用点，不改任何行为语义。

- [ ] **Step 2: 服务层改为接收 `PrincipalContext`**

`service.py` 内的 `load_principal(session, username)` 调用全部删除——principal 由 API 层注入。`_owned_conversation` 改为接收 `principal`。

- [ ] **Step 3: API 层注入 principal**

`chat.py`、`trace.py`、`semantic.py` 的 `Depends(get_current_username)` 换为 `Depends(get_principal)`。

- [ ] **Step 4: 全量回归**

确认除 P0-08 那 1 个已知红用例外全绿。

---

### Task 3: 对象级鉴权与语义管理面

**Files:**
- Create: `backend/app/auth/ownership.py`
- Create: `backend/tests/auth/test_ownership.py`
- Modify: `backend/app/pipeline/orchestrator.py`
- Modify: `backend/app/api/semantic.py`
- Modify: `backend/app/semantic/schemas.py`
- Modify: `backend/tests/api/test_semantic_api.py`

**Interfaces:**
- Produces:
  - `app.auth.ownership.owned_conversation(session, principal, conversation_id, *, dataset_name) -> ConversationRow`
  - `app.semantic.schemas.DatasetDetailOut` — 业务视图（业务名、描述、口径、字段业务名）
  - `app.semantic.schemas.DatasetDetailAdminOut` — 追加 `physical_table`、`physical_column`、`sensitivity`

- [ ] **Step 1: 写失败的所有权测试**

`test_ownership.py`：
- 本人本数据集会话 → 返回该行
- 他人会话 id → 404，**且不写入任何 Turn**（断言 Turn 表无新增）
- 不存在的 id → 404
- 本人但属于另一数据集的会话 id → 404（不自动新建会话，避免继承错误 `slot_state`）

- [ ] **Step 2: 实现 `ownership.py` 并接入 orchestrator**

`orchestrator._conversation()`（`app/pipeline/orchestrator.py:350` 附近）现在只 `session.get(ConversationRow, conversation_id)` 而不校验归属与数据集，改为调用 `owned_conversation`。这是 P0-02 的实际形态——`observability/service.py` 的鉴权本就正确，缺口只在这条写路径。

- [ ] **Step 3: 写失败的语义管理面鉴权测试**

`test_semantic_api.py` 追加：匿名调 `publish` → 401；`semantic_viewer` 调 `publish` → 403；非 `semantic_editor`/`semantic_approver` 调 `lint` → 403；普通用户读 dataset 详情响应中**不含** `physical_table` 键；`GET /datasets` 只返回该 principal 至少有一个非 DENY 列的数据集。

- [ ] **Step 4: 四个端点加鉴权并拆响应模型**

| 端点 | 要求 |
|---|---|
| `GET /datasets` | 按可见数据集过滤 |
| `GET /datasets/{name}` | 按角色选 response model |
| `GET /datasets/{name}/lint` | `require_roles("semantic_editor", "semantic_approver")` |
| `POST /datasets/{name}/publish` | `require_roles("semantic_approver")` |

响应模型**拆成两个类**，不在同一模型里把敏感字段置 `None`——后者容易在序列化配置变化时泄漏。

---

### Task 4: 元数据事务边界

**Files:**
- Modify: `backend/app/core/db.py`
- Create: `backend/tests/core/test_transaction_boundary.py`

**Interfaces:**
- Changed: `get_meta_session()` 在正常退出时 `commit()`，异常时 `rollback()` 后重抛

- [ ] **Step 1: 写失败的持久化测试**

`test_transaction_boundary.py`：
- 经真实请求创建 Conversation/Turn/Trace/Feedback 后，**用新 session** 能读到（现状读不到，因为 `get_meta_session` 从不 commit——这是 P0-05）
- 业务查询失败产生的"失败 Turn + Trace"同样能在新 session 读到
- 抛到 FastAPI 的异常（如 `SemanticError` → 404）路径回滚，无残留写入

- [ ] **Step 2: 修 `get_meta_session`**

```
yield session
session.commit()
except: session.rollback(); raise
finally: session.close()
```

- [ ] **Step 3: 确认 orchestrator 无需改动**

失败查询走"写失败 Turn 后正常返回 `TurnOutcome(status=failed)`"，不抛异常，因此走 commit 路径、失败留痕。真正的异常才回滚。这个结构现有代码已正确，本 Step 只用测试锁住它，不改代码。

---

### Task 5: 只读执行与函数白名单

**Files:**
- Modify: `backend/app/core/db.py`
- Modify: `backend/app/security/whitelist.py`
- Create: `backend/scripts/create_reader_role.sql`
- Create: `backend/tests/security/test_function_whitelist.py`
- Modify: `backend/tests/security/test_whitelist.py`

**Interfaces:**
- Changed: `get_sample_connection()` 连接参数追加只读与超时选项
- Produces: `app.security.whitelist` 的函数校验；`AstRejectedError` 用于未登记函数

- [ ] **Step 1: 扫描编译器实际产出的函数集合**

遍历现有编译器与 Golden Set 产出的 SQL，收集实际使用的函数名，作为白名单初始值。**先扫描再定白名单**，否则必然误拦正常查询。

- [ ] **Step 2: 写失败的函数白名单测试**

`test_function_whitelist.py`：`pg_sleep(1)` 被拒；`pg_read_file`、`dblink`、`lo_import`、`pg_catalog` 管理函数被拒；未登记的任意函数名默认被拒；Step 1 扫出的全部函数通过。

- [ ] **Step 3: 实现函数校验**

遍历 AST 的 `exp.Anonymous` 与已知函数节点，比对白名单（聚合、日期、字符串、数学的固定集合）。默认拒绝。

- [ ] **Step 4: 只读连接参数**

```
-c default_transaction_read_only=on
-c statement_timeout=<query_timeout_seconds * 1000>
-c lock_timeout=2000
-c idle_in_transaction_session_timeout=10000
```

- [ ] **Step 5: 只读 DB role 脚本**

`create_reader_role.sql`：`data_agent_reader` 仅 `CONNECT` + 目标 schema `USAGE`/`SELECT`；显式 `REVOKE CREATE ON SCHEMA public`、`REVOKE TEMPORARY ON DATABASE`。`sample_database_url` 用该 role，与 `meta_database_url` 凭据分离。附文档说明。

- [ ] **Step 6: 验证纵深防御**

测试：只读连接上 `INSERT` 报错（即便绕过应用 AST 也写不进去）。

- [ ] **Step 7: Golden Set 全量回归**

确认函数白名单未拦掉任何现有正常查询。

---

### Task 6: Trace 分级与错误脱敏

**Files:**
- Modify: `backend/app/observability/orm.py`
- Modify: `backend/app/observability/trace.py`
- Modify: `backend/app/observability/service.py`
- Modify: `backend/app/observability/schemas.py`
- Create: `backend/app/core/errors.py`
- Create: `backend/migrations/001_trace_payload_split.sql`
- Create: `backend/tests/observability/test_trace_visibility.py`
- Create: `backend/tests/core/test_error_taxonomy.py`
- Modify: 全部 `stage_timer` 调用点（`app/pipeline/orchestrator.py` 等）

**Interfaces:**
- Changed: `TraceStageRow` 的单一 `input_payload`/`output_payload` 拆为 `public_payload`/`user_payload`/`sensitive_payload`（可 NULL），增加 `visibility`、`expires_at`
- Changed: `TraceRecorder.stage_timer()` **要求调用方显式指定每个 payload 的级别，无默认值**
- Produces: `app.core.errors` 的 `error_code` 枚举与用户安全文案映射

- [ ] **Step 1: 写失败的分级测试**

`test_trace_visibility.py`：
- 普通 owner 读 trace，响应中不含任何物理表名字符串
- `trace_auditor` 可得物理 SQL 与 EXPLAIN
- `security_admin` 可得异常栈与 provider 原始响应
- 异常详情不出现在用户响应中
- 每个 stage 均有非空 `visibility`
- `sensitive` 级 `expires_at` 为 30 天，其余 180 天

- [ ] **Step 2: 改 ORM 并写迁移脚本**

四级归属：

| 级别 | 内容 | 可见角色 |
|---|---|---|
| `public` | 阶段名、耗时、状态 | 所有 owner |
| `user` | 逻辑计划、口径、假设、澄清、结果行数 | 所有 owner |
| `sensitive` | 物理 SQL、EXPLAIN、行策略、掩码字段、Prompt | `trace_auditor` |
| `admin` | 异常栈、provider 原始响应、内部 error detail | `security_admin` |

迁移脚本把现有 payload 保守归档为 `visibility=sensitive`（开发数据，宁可过严）。

- [ ] **Step 3: 改 `stage_timer` 签名并迁移全部调用点**

不提供级别默认值——忘记标级别必须是类型错误。同时**保留现有语义:异常路径先 `record` 再 `raise`**，失败 stage 一定落库。这是 Trace 存在的理由，不能在改造中丢失。

- [ ] **Step 4: 实现 `errors.py`**

`error_code` 枚举 + 用户安全文案映射。API 响应只返回 `{error_code, message, trace_id}`。内部 detail 与栈只进 `admin` 级 trace 与结构化日志。现有 `_GENERIC_REFUSAL` 等固定文案纳入 taxonomy。

- [ ] **Step 5: 收紧 `replay_turn`**

现无条件向 owner 返回 `sql`/`display_sql`。改为完整 SQL 仅 `trace_auditor` 可见；普通 owner 只得逻辑计划与"是否与原查询一致"。

---

### Task 7: 关闭 VQ 直接执行路径

**Files:**
- Modify: `backend/app/pipeline/orchestrator.py`
- Modify: `backend/tests/pipeline/` 对应测试

**这是本计划唯一的功能倒退。** 取舍理由：`_secure_verified` 走 `apply_masking`，而后者只重写 `exp.Alias` 下的直接列，`SUM(masked_col)` 逃过掩码——这是可被利用的列权限旁路（P0-04），不能带着它上线。S3 以 Canonical Plan 重编译恢复该能力。

- [ ] **Step 1: 写测试锁住降级行为**

VQ 命中时不执行 `fixed_sql`，改走正常链路（重新识别意图 → 编译），用户仍得到答案。测试断言：命中 VQ 的问题仍返回正确答案，且执行的 SQL 来自编译器而非 `fixed_sql`。

- [ ] **Step 2: 关闭该路径**

`orchestrator.py:143` 附近的 `_secure_verified(recorder, hit.fixed_sql, ...)` 调用移除，降级走正常链路。代价：失去 VQ 的确定性与一次模型调用的节省。

---

### Task 8: 配置校验与探针

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/scripts/init_db.py`
- Create: `backend/migrations/002_identity_columns.sql`
- Create: `backend/tests/core/test_startup_validation.py`

**Interfaces:**
- Produces: FastAPI lifespan 启动校验；`/livez` 与 `/readyz` 替代单一 `/api/health`

- [ ] **Step 1: 写失败的启动校验测试**

7 类弱配置在 `environment=production` 下各自导致启动失败：
1. `jwt_secret` 为默认值
2. DSN 含 `postgres:postgres`
3. CORS 含通配或 localhost
4. `AUTH_MODE=dev`
5. `llm_api_key` 为空
6. `sample_database_url` 与 `meta_database_url` 相同（未用独立 role）
7. 未配置 OIDC issuer/audience

外加 `/readyz` 在 DB 不可用时返回失败、`/livez` 仍返回成功。

- [ ] **Step 2: 实现 lifespan 校验与探针**

`/livez` 只报进程存活；`/readyz` 检 DB 可连 + 语义模型可加载。CORS 从硬编码 `localhost:5173` 改为配置化。

- [ ] **Step 3: 表结构迁移脚本**

| 表 | 变更 |
|---|---|
| `users` | 增加 `oidc_subject`（唯一）、`tenant_id` |
| `conversations` | `user_id` 加外键与索引；增加 `tenant_id`、`state_version` |

（`trace_stages` 的变更在 Task 6 的 `001_` 脚本。）

Alembic 正式接入在 S7，本轮用带序号 SQL 脚本落在 `backend/migrations/`，S7 纳管。

- [ ] **Step 4: `init_db.py` 加 production 守卫**

生产环境下执行直接拒绝（它含 `DROP TABLE`）。保留本地样本用途。

---

### Task 9: 前端 OIDC 接入

**Files:**
- Create: `frontend/src/auth/oidc.ts`
- Create: `frontend/src/auth/tokenStore.ts`
- Create: `frontend/src/auth/guard.ts`
- Create: `frontend/tests/auth/oidc.spec.ts`
- Create: `frontend/tests/auth/tokenStore.spec.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/router/`、`frontend/src/stores/session.ts`
- Modify: 现有 6 处 `setUsername`/`getUsername` 调用点（含测试）
- Create: `docker-compose.dev.yml`
- Create: `deploy/keycloak/realm-export.json`

**Interfaces:**
- Produces: Authorization Code + PKCE 登录流；`Authorization: Bearer` 请求头
- Removed: `setUsername`/`getUsername`、`X-Username` 头

- [ ] **Step 1: 写失败的 PKCE 与 token 存储测试**

`code_verifier` 由 `crypto.getRandomValues` 生成；`S256` 派生 challenge 正确；`state` 与 `nonce` 校验失败时拒绝回调（防 CSRF 与重放）；verifier 存 `sessionStorage` 而非 `localStorage`；access token 只在内存、不落任何 storage；401 触发单次刷新且并发 401 共享同一刷新 promise（不产生刷新风暴）。

- [ ] **Step 2: 实现 `oidc.ts`、`tokenStore.ts`、`guard.ts`**

code 交换在前端完成（PKCE 无需 client secret），后端只验 Bearer。refresh 经后端 `/api/auth/token` 代理。刷新失败或 401 触发重新登录。`guard.ts` 做路由守卫与回调路由处理。

- [ ] **Step 3: 改 `client.ts`**

`X-Username` 换 `Authorization: Bearer <token>`。删除 `setUsername`/`getUsername`，迁移 6 处调用点。

同时收紧 `readDetail`：不再直接展示后端 `detail`（含 FastAPI 校验细节，属泄漏面），改为消费 Task 6 的 `{error_code, message, trace_id}`。

- [ ] **Step 4: 用户名显示改读 claim**

工作台用户名显示改读 token 的 `preferred_username`，不再作为身份来源。

- [ ] **Step 5: 本地开发环境**

`docker-compose.dev.yml` 起 Keycloak，附 realm 导入 JSON（预置 6 角色与测试用户）。`AUTH_MODE=dev` 路径保留，不起 Keycloak 也能跑测试。

---

## 验收

1. P0 安全测试集 100% 通过。
2. 新 Session 能读取刚创建的 Conversation/Turn/Trace/Feedback，失败 turn 同样持久化。
3. 任意非 `semantic_approver` 无法发布语义；任意非 `trace_auditor` 无法看到物理 SQL。
4. 只读 DB role 下，即便绕过应用 AST 也无法写入。
5. 伪造、过期、错误 aud/iss、`alg=none` token 全部被拒。
6. 跨用户与跨数据集会话复用均 404 且无副作用。
7. production 下 7 类弱配置各自导致启动失败。
8. 测试基线不回退：除 `test_aggregate_intent_requires_at_least_one_metric`（P0-08，归 S2）外全绿，新增测试全绿。
9. 失败 stage 落库的既有保证未被破坏。
