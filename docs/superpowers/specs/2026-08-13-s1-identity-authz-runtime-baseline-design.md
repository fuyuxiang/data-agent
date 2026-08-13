# S1 身份、鉴权与运行时基线 设计文档

> 上游依据：`fangan.md`（审计基线 `main@ffd5ef3d`）第 5.1、8.1、8.2、8.4、11.1 节。
> 子项目定位：升级路线 S1，是 S2～S7 的地基。
> 覆盖问题：P0-01、P0-02、P0-03、P0-05、P0-06、P0-09、P0-10。

## 1. 目标与非目标

### 目标

消除阻断投产的越权与数据丢失，使后续子项目可以在一个可信的身份与事务基础上开展：

- 身份不可伪造：OIDC/PKCE + JWT 验签取代 `X-Username` 请求头。
- 权限主键不可变：内部 `user_id` 取代用户名作为一切鉴权依据。
- 对象级鉴权无缺口：会话、Turn、Trace、反馈、语义管理面全部按 principal 校验。
- 元数据真正落库：请求级事务提交，失败留痕也持久化。
- 数据库层强制只读：独立最小权限 role + 只读事务 + 超时 + SQL 函数白名单。
- Trace 与错误按级别可见：普通用户看不到物理 SQL、策略与异常栈。
- 生产弱配置无法启动：默认密钥、默认 DSN、dev 认证在 production 下 fail-fast。

### 非目标（本子项目明确不做）

| 项 | 去向 |
|---|---|
| Verified Query 从 Canonical Plan 重编译（P0-04 的最终解） | S3。本轮先关闭 VQ 直接执行路径，见 8.2 |
| `state_version` 乐观锁的实际使用 | S7（API v2 并发澄清） |
| Trace 保留期清理任务的调度 | S6（Job 状态机）。本轮只写 `expires_at` 值 |
| 时钟、TimeResolver、Intent 校验（P0-07/08） | S2 |
| Alembic 正式接入 | S7。本轮迁移以脚本形式落盘，见 7.2 |
| token 撤销、登出全链路、并发会话限制 | 不属于 P0，后续按需 |
| 多租户实际隔离 | 本轮只建 `tenant_id` 列并填默认值 |

## 2. 核心设计决策

### 2.1 PKCE 的 code 交换放前端，后端只验 Bearer

前端完成 Authorization Code + PKCE 的授权码获取；`client_secret` 不进浏览器，code 换 token 经后端 `/api/auth/token` 代理。后端业务路径只做一件事：验证 Bearer token。

理由：机密客户端的密钥必须留在服务端，而把整个 OIDC 会话状态搬到后端会引入 server session 与 CSRF 面，收益不抵成本。

### 2.2 `username` 降级为展示字段

OIDC `sub` 映射到 `users.oidc_subject`，`user_id` 是唯一权限主键。用户名可变、可重复注册、可被 IdP 复用，作为权限主键是 P0-01 的根因之一。

### 2.3 复用已有的所有权语义，而非新建

`app/observability/service.py` 的 `_owned_conversation` / `_owned_turn` 已经做对了"不存在与无权限都返回 404"。本轮把它提升为 `app/auth/ownership.py` 的公共能力，让 orchestrator 写路径复用同一函数——P0-02 的漏洞正是写路径绕过了这套已有机制。

### 2.4 Trace payload 按级别分列存储，而非读取时过滤

字段级过滤漏一个 key 就是一次泄漏。分列存储让"漏标"退化为"根本不返回"。

### 2.5 SQL 函数白名单默认拒绝

未在白名单中的函数名直接拒绝，而非维护黑名单。白名单初始值由扫描编译器实际产出的函数集合得到。

### 2.6 只读靠数据库强制，不靠 AST 守卫

AST 守卫与数据库权限同时存在，互不替代。应用层可能有解析盲区，数据库层不会。

## 3. 身份与 PrincipalContext

### 3.1 新增 `app/auth/`

替代 `app/core/security.py`（删除）。

- `oidc.py`：JWKS 客户端。按 `kid` 缓存公钥，带 TTL；遇未知 `kid` 重取一次（应对轮转），重取仍失败则拒绝。验证 `iss`、`aud`、`exp`、`nbf`、`signature`。算法白名单仅 `RS256`/`ES256`——显式拒绝 `alg=none` 与 HS* 混淆攻击。
- `principal.py`：`PrincipalContext(user_id, tenant_id, username, subject, roles, groups, attributes, auth_time)`。
- `provisioning.py`：JIT provisioning。首次登录按 `oidc_subject` 建 `UserRow`，幂等；已存在则更新 `display_name`。
- `dependencies.py`：`get_principal()` 每请求解析一次，缓存于 `request.state`；`require_roles(*names)` 生成角色守卫依赖。
- `dev.py`：`AUTH_MODE=dev` 下的 `X-Username` 回退，仅在该模式注册。

### 3.2 角色集合

按 `fangan.md` 8.1 拆分，`RoleRow` 表结构无需改动，只增加种子数据：

`semantic_viewer`、`semantic_editor`、`semantic_approver`、`security_admin`、`trace_auditor`、`eval_operator`。

多角色列策略合并保持现有 lattice（DENY > MASK > ALLOW），已有单测覆盖，本轮不改语义。

### 3.3 下游签名迁移

`load_principal(session, username)` → `load_principal(session, user_id)`。

`app/observability/service.py` 的 `list_conversations`、`list_turns`、`save_feedback`、`get_trace`、`replay_turn` 的 `username: str` 参数改为 `principal: PrincipalContext`，不再在服务层重复加载 principal。约 15 处调用点与对应测试同步迁移。

### 3.4 测试

JWKS 轮转、过期 token、错误 `aud`、错误 `iss`、`alg=none` 拒绝、HS256 伪造拒绝、dev 头在 production 下启动失败、JIT provisioning 幂等。

## 4. 对象级鉴权与会话所有权

### 4.1 `app/auth/ownership.py`

```
def owned_conversation(session, principal, conversation_id, *, dataset_name) -> ConversationRow
```

除 `user_id` 匹配外，校验 `row.dataset_name == dataset_name`。不匹配返回 404，不解释原因——用 A 数据集的会话 id 去问 B 数据集会继承错误的 `slot_state`，且不自动新建会话。

`QueryOrchestrator._conversation()` 改为调用该函数。

### 4.2 语义管理面鉴权（P0-03）

`app/api/semantic.py` 四个端点全部加 `Depends(get_principal)`：

| 端点 | 要求 |
|---|---|
| `GET /datasets` | 按可见数据集过滤（该 principal 至少有一个非 DENY 列） |
| `GET /datasets/{name}` | 按角色选择 response model，见 4.3 |
| `GET /datasets/{name}/lint` | `require_roles("semantic_editor", "semantic_approver")` |
| `POST /datasets/{name}/publish` | `require_roles("semantic_approver")` |

### 4.3 响应模型按角色拆分

`app/semantic/schemas.py` 拆出两个模型，而非在同一模型中置 `None`：

- `DatasetDetailOut`：业务视图。业务名、描述、口径、字段业务名。
- `DatasetDetailAdminOut`：追加 `physical_table`、`physical_column`、`sensitivity`。仅 `semantic_viewer` 及以上可得。

### 4.4 测试

跨用户 POST 会话（404 且不写入 Turn）、跨数据集复用会话 id（404）、匿名调 publish（401）、`semantic_viewer` 调 publish（403）、普通用户读 dataset 详情不含 `physical_table`。

## 5. 事务边界

### 5.1 元数据事务提交（P0-05）

```
def get_meta_session():
    session = MetaSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

### 5.2 失败留痕与异常回滚的区分

orchestrator 在业务查询失败时写"失败 Turn + Trace"后正常返回 `TurnOutcome(status=failed)`，不抛异常——该路径 commit，失败必须留痕。真正抛到 FastAPI 的异常（如 `SemanticError` → 404）回滚。

这是现有代码已有的结构，orchestrator 无需改动，但须以测试锁住：**失败的 turn 必须能在新 session 中读到**。

### 5.3 测试

新 session 读到刚写的 Conversation/Turn/Trace/Feedback；失败 turn 持久化；异常路径回滚。

## 6. 只读执行与 SQL 函数白名单

### 6.1 连接层（P0-06）

`get_sample_connection()` 连接参数追加：

```
-c default_transaction_read_only=on
-c statement_timeout=<query_timeout_seconds * 1000>
-c lock_timeout=2000
-c idle_in_transaction_session_timeout=10000
```

### 6.2 数据库 role

新增 `data_agent_reader`（SQL 脚本 + 文档）：仅 `CONNECT` 与目标 schema 的 `USAGE`/`SELECT`；显式 `REVOKE CREATE ON SCHEMA public`、`REVOKE TEMPORARY ON DATABASE`。`sample_database_url` 使用该 role，与 `meta_database_url` 凭据彻底分离。

### 6.3 函数白名单

`app/security/whitelist.py` 现只做 SELECT-only 与表白名单。增加函数校验：遍历 AST 的 `exp.Anonymous` 及已知函数节点，仅允许聚合、日期、字符串、数学的固定集合。默认拒绝：未在白名单中的函数名直接 `AstRejectedError`。

明确阻断：`pg_sleep`、`pg_read_file`、`dblink`、`lo_*`、全部 `pg_catalog` 管理函数。

白名单初始值由扫描编译器实际产出的函数集合确定，避免拦掉现有正常查询。

### 6.4 测试

`pg_sleep(1)` 被 AST 拒绝；只读连接上 `INSERT` 报错；未知函数默认拒绝；现有 Golden Set 全部查询仍通过白名单。

## 7. 配置校验与探针

### 7.1 production fail-fast（P0-10）

FastAPI lifespan 启动校验。`environment=production` 时下列任一成立即拒绝启动：

1. `jwt_secret` 为默认值
2. DSN 含 `postgres:postgres`
3. CORS 含通配或 localhost
4. `AUTH_MODE=dev`
5. `llm_api_key` 为空
6. `sample_database_url` 与 `meta_database_url` 相同（说明未使用独立 role）
7. 未配置 OIDC issuer/audience

`Settings` 增加 `environment`、`auth_mode`、`oidc_issuer`、`oidc_audience`、`oidc_client_id`、`oidc_client_secret`、`cors_origins`。

### 7.2 迁移脚本落盘

Alembic 正式接入在 S7。本轮的表结构变更（见 7.3）以带序号的 SQL 迁移脚本落在 `backend/migrations/`，S7 接入 Alembic 时纳管。`init_db.py` 保留为本地样本用途，但增加"production 下拒绝执行"的守卫。

### 7.3 表结构变更汇总

| 表 | 变更 |
|---|---|
| `users` | 增加 `oidc_subject`（唯一）、`tenant_id` |
| `conversations` | `user_id` 加外键与索引；增加 `tenant_id`、`state_version` |
| `trace_stages` | payload 分列（见 8.1）；增加 `visibility`、`expires_at` |

### 7.4 探针

`/api/health` 拆为 `/livez`（进程存活）与 `/readyz`（DB 可连 + 语义模型可加载）。

### 7.5 测试

7 个 production 弱配置用例各自导致启动失败；`/readyz` 在 DB 不可用时返回失败。

## 8. Trace 分级与错误脱敏

### 8.1 四级可见性（P0-09）

写入时打标，不在读取时判断。

| 级别 | 内容 | 可见角色 |
|---|---|---|
| `public` | 阶段名、耗时、状态 | 所有 owner |
| `user` | 逻辑计划、口径、假设、澄清、结果行数 | 所有 owner |
| `sensitive` | 物理 SQL、EXPLAIN、行策略、掩码字段、Prompt | `trace_auditor` |
| `admin` | 异常栈、provider 原始响应、内部 error detail | `security_admin` |

`TraceStageRow` 的 payload 从单一 JSONB 改为 `public_payload` / `user_payload` / `sensitive_payload`（可为 NULL），并增加 `visibility` 列。读取时按角色决定是否拼接 `sensitive_payload`。

`TraceRecorder.stage_timer()` 签名要求调用方显式指定每个 payload 的级别，不提供默认值——忘记标级别是类型错误，而非静默降级为 public。

### 8.2 VQ 直接执行路径关闭

`_secure_verified` 把 `{"sql": fixed_sql}` 写入 trace input，且 `secure_verified_sql` 走 `apply_masking`，而后者只重写 `exp.Alias` 下的直接列——`SUM(masked_col)` 逃过掩码。这是 P0-04。

本轮处置：VQ 命中不再执行 `fixed_sql`，降级走正常链路（重新识别意图 → 编译）。用户仍得到答案，代价是失去 VQ 的确定性与一次模型调用的节省。S3 以 Canonical Plan 重编译恢复。

这是本子项目唯一的功能倒退，取舍理由：P0-04 是可被利用的列权限旁路，不能带着它上线。

### 8.3 replay 端点收紧

`replay_turn` 现无条件向 owner 返回 `sql` 与 `display_sql`。改为完整 SQL 仅 `trace_auditor` 可见；普通 owner 只得到逻辑计划与"是否与原查询一致"。

### 8.4 错误 taxonomy

`app/core/errors.py` 定义 `error_code` 枚举与用户安全文案映射。API 响应只返回 `{error_code, message, trace_id}`，`message` 为预写安全文案。内部 detail 与栈只进 `admin` 级 trace 与结构化日志。现有 `_GENERIC_REFUSAL` 等固定文案纳入该 taxonomy。

### 8.5 保留期

`TraceStageRow.expires_at`：`sensitive` 级默认 30 天，其余 180 天，可配置。清理调度在 S6，本轮只写值。

### 8.6 测试

普通 owner 读 trace 不含物理表名字符串；`trace_auditor` 可得；异常不出现在用户响应；每个 stage 均有非空 `visibility`；replay 的 SQL 输出受角色约束。

## 9. 前端认证

### 9.1 新增 `frontend/src/auth/`

- `oidc.ts`：Authorization Code + PKCE。`code_verifier` 用 `crypto.getRandomValues` 生成，`S256` 派生 challenge，verifier 存 `sessionStorage`（非 localStorage，降低 XSS 持久窃取窗口）；校验 `state` 与 `nonce` 防 CSRF 与重放。
- `tokenStore.ts`：access token 仅存内存，不落 storage；refresh 经后端 `/api/auth/token` 代理。刷新失败或 401 触发重新登录。
- `guard.ts`：路由守卫。未登录跳登录页；回调路由处理 code 交换。

### 9.2 请求客户端

`client.ts` 的 `X-Username` 换为 `Authorization: Bearer <token>`；401 自动刷新一次再重试，用单飞 promise 避免刷新风暴。删除 `setUsername`/`getUsername`，迁移现有 6 处调用点（含测试）。

工作台用户名显示改读 token 的 `preferred_username` claim，不再作为身份来源。

### 9.3 本地开发

`docker-compose.dev.yml` 起 Keycloak，附 realm 导入 JSON（预置 6 个角色与测试用户）。`AUTH_MODE=dev` 路径保留，使不起 Keycloak 也能跑测试。

## 10. 验收标准

1. P0 安全测试集 100% 通过，无已知失败。
2. 新 Session 能读取刚创建的 Conversation / Turn / Trace / Feedback，失败 turn 同样持久化。
3. 任意非 `semantic_approver` 无法发布语义；任意非 `trace_auditor` 无法看到物理 SQL。
4. 只读 DB role 下，即便绕过应用 AST 也无法写入数据。
5. 伪造、过期、错误 aud/iss、`alg=none` 的 token 全部被拒。
6. 跨用户与跨数据集的会话复用均返回 404 且无副作用。
7. production 环境下 7 类弱配置各自导致启动失败。
8. 后端测试基线不回退。改造前为 360 passed / 1 failed（`test_aggregate_intent_requires_at_least_one_metric`，属 P0-08，归 S2 处理）；本子项目完成后该用例仍可为红，其余全部为绿，且新增测试全绿。

## 11. 风险

| 风险 | 处置 |
|---|---|
| 函数白名单误拦现有查询 | 白名单初始值由扫描编译器实际产出确定；Golden Set 全量回归 |
| `PrincipalContext` 迁移面广（约 15 处 + 测试） | 一次性机械迁移，不与其他改动混在同一提交 |
| Trace payload 分列需数据迁移 | 现有 trace 为开发数据，迁移脚本按 `visibility=sensitive` 保守归档旧 payload |
| VQ 过渡期功能倒退 | 已与需求方确认接受；S3 恢复 |
| Keycloak 引入本地开发成本 | `AUTH_MODE=dev` 保留，不强制起 IdP |
