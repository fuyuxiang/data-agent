# S7 API v2、前端与交付工程 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把前面六个子项目的能力暴露成可用产品，并建立可交付的工程基线：API v2（幂等、乐观锁、SSE、取消、稳定 `error_code`）、OpenAPI 生成 TS client、按权限路由的工作台、后端/前端工程基线、CI/CD 与供应链扫描。

**Architecture:** 工程基线目前几乎为空：无 `pyproject.toml`、无锁文件、无 Alembic、无 Docker、无 CI 配置、无 ESLint/Playwright，前端把 `orders` 硬编码在两处、身份靠 `X-Username` 明文头。实施顺序有一个刻意的分岔：**工程基线（Task 1~4、11~13）可与 S1~S6 并行推进，API/前端部分（Task 5~10）必须在后**。先做工程基线还有一层收益——Alembic 基线一旦立起来，S1~S6 引入的表变更就能各自成 migration，而不是继续堆 `scripts/init_db.py`。

**Tech Stack:** Python 3.13、FastAPI、Alembic、uv、ruff、mypy、Vue 3、Vite、TypeScript、Playwright、Docker、GitHub Actions

## Global Constraints

约束来自 `docs/superpowers/specs/2026-08-13-s7-api-v2-frontend-delivery-design.md`：

- **v1 保留兼容层，不做破坏式切换**。且 **v2 为唯一实现，v1 改为薄适配层调用 v2 服务层，不复制逻辑**——否则并存期两套逻辑必然漂移。
- **澄清与条件面板提交结构化 patch，不回炉 LLM**。既是准确性问题也是成本问题：确定性操作不该花一次模型调用。
- **错误响应不泄漏内部细节**。统一 `{error_code, message, trace_id}`；前端按 code 决策而非按文案匹配。
- **大结果不进 Turn JSONB**，按引用存储。
- **物理 SQL 按角色可见**，后端按级别返回，**前端不做自行判断**。
- **锁文件不可选**。锁文件是"同一 commit 两次构建结果相同"的前提，也是供应链扫描有意义的前提。
- **生产禁止 DROP TABLE**。`init_db` 加环境守卫。
- 无权限 `agent_id` 返回 **404 而非 403**——避免探测数据域是否存在。
- **重组只移动不改逻辑**，重组前后测试集必须完全相同且全绿。
- **首轮必须产出基线 SBOM 与漏洞报告**。本次审计未获得完整 npm/pip 审计报告，「未完成」不等于「无漏洞」；基线报告出来前不做安全性结论。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

API/前端部分依赖 S1（OIDC 与对象级鉴权）、S2（IntentV2）、S3（CanonicalQueryPlan 与 patch 能力）、S4（语义注册中心）、S5（`patch_slot`）、S6（Job/Run 状态机、`error_code`、trace_id）。

工程基线部分（Task 1~4、11~13）可与 S1~S6 并行。

Task 2 的 Alembic 基线是 S6 Eval Run 迁库的前置。

---

### Task 1: 依赖与工具链基线

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/uv.lock`
- Delete: `backend/requirements.txt`（迁移后）
- Create: `backend/ruff.toml`
- Modify: `backend/pytest.ini`

`requirements.txt` 13 个依赖**全是 `>=`**，构建不可复现。

- [ ] **Step 1: 建 `pyproject.toml` 与锁文件**

运行/开发依赖分组；Python 版本固定支持范围（3.12/3.13）。

- [ ] **Step 2: 接入 ruff、mypy（或 pyright）、coverage**

- [ ] **Step 3: 测试 marker 分层**

`pytest.ini` 当前只有 `pythonpath`/`testpaths`/`addopts = -ra`。加 marker：unit / integration / contract / security / golden / load。**真实模型测试为受控 CI job**，不在默认集合。

---

### Task 2: Alembic 接管 schema

**Files:**
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/versions/0001_baseline.py`
- Modify: `backend/scripts/init_db.py`
- Create: `backend/tests/test_init_db_guard.py`

- [ ] **Step 1: 首个 migration 以当前 ORM 为基线**

用 autogenerate 后**人工核对**。本地库**重建而非在线迁移**，避免基线与现有本地库不一致。

- [ ] **Step 2: 写失败的生产守卫测试**

断言：`init_db.py` 在生产环境配置下**拒绝执行**。`scripts/init_db.py` 仅用于本地样本，Alembic 接管后它的 DROP TABLE 在生产是灾难。

- [ ] **Step 3: S1~S6 的表变更各自成 migration**

S6 委派的 Eval Run 迁库排在此基线之后。

---

### Task 3: 启动、探针与进程分离

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/app/logging_setup.py`
- Create: `backend/app/middleware/request_id.py`
- Create: `backend/worker/main.py`
- Create: `backend/tests/test_probes.py`

- [ ] **Step 1: 写失败的探针语义测试**

断言：**依赖不可用时 `/readyz` 红而 `/livez` 绿**。当前只有单一 `/api/health`。

同时断言：**lifespan 配置校验失败时进程不进入 ready**（复用 S1 已定的配置校验）。

- [ ] **Step 2: lifespan 与探针拆分**

lifespan 做配置校验、连接池预热、优雅 shutdown。

- [ ] **Step 3: 结构化日志与 request/trace id 中间件**

统一 error taxonomy（S6 的 `error_code`）。

- [ ] **Step 4: CORS 配置化**

`main.py` 硬编码 `allow_origins=["http://localhost:5173"]`。改配置化，**生产不允许通配**。

- [ ] **Step 5: API server 与离线 worker 分进程**

**Eval 不跑在 web worker 内。**

---

### Task 4: 前端工程基线

**Files:**
- Modify: `frontend/package.json`
- Create: `frontend/eslint.config.js`
- Create: `frontend/.prettierrc`
- Create: `frontend/playwright.config.ts`
- Modify: `frontend/vite.config.ts`

- [ ] **Step 1: 固定 Node LTS 与包管理器版本**

`package.json` 无 `engines`、无 `packageManager`。

- [ ] **Step 2: 接入 ESLint 与 Prettier**

- [ ] **Step 3: 独立 `typecheck` script**

当前 `vue-tsc --noEmit` 塞在 `build` 里，CI 无法单独 gate。

- [ ] **Step 4: coverage 门槛，warning 视作失败**

- [ ] **Step 5: bundle 拆分与预算**

Element Plus 按需加载；拆分 Element Plus、管理面与 Trace；路由级预取。

**先测量当前值再定阈值，首轮设为「不劣化」而非绝对值**——200 kB gzip 是目标不是首轮门槛。断言 bundle 超预算时构建失败。

---

### Task 5: API v2 端点

**Files:**
- Create: `backend/app/api/v2/query_runs.py`
- Create: `backend/app/api/v2/conversations.py`
- Create: `backend/app/services/query_run.py`
- Create: `backend/tests/api/v2/test_query_runs.py`
- Modify: `backend/app/api/chat.py`（改为薄适配层）

**Interfaces:**
```
POST /api/v2/query-runs
  body: {agent_id, question, conversation_id?, idempotency_key}
  return: {run_id, status, events_url}

POST /api/v2/query-runs/{run_id}/clarifications
  body: {target, selected_value, expected_state_version}

GET  /api/v2/query-runs/{run_id}
GET  /api/v2/query-runs/{run_id}/events        # SSE
POST /api/v2/query-runs/{run_id}/cancel
GET  /api/v2/conversations/{id}?include=turns,answers
```

- [ ] **Step 1: 写失败的鉴权测试**

- v2 端点对**非本人 / 不存在 run 返回 404**
- **无权限 `agent_id` 返回 404 而非 403**（不泄漏数据域存在性）

- [ ] **Step 2: 实现服务层与端点**

澄清端点是 S5 `patch_slot` 的**薄封装**，支持一次提交多个澄清项。

- [ ] **Step 3: v1 改为薄适配层**

`/api/chat/*` 保留兼容，但**调用 v2 服务层，不复制逻辑**。

---

### Task 6: 幂等与乐观锁

**Files:**
- Create: `backend/app/api/idempotency.py`
- Create: `backend/migrations/014_idempotency_state_version.sql`
- Modify: `backend/app/observability/orm.py`（`ConversationRow` 加 `state_version`）
- Create: `backend/tests/api/test_idempotency.py`
- Create: `backend/tests/api/test_optimistic_lock.py`

- [ ] **Step 1: 写失败的幂等测试**

相同 key + 相同请求体 → **返回首次结果，不重复执行**；相同 key + 不同请求体 → **409**。结果记入 S6 的 `idempotency_outcome`，便于排查"用户以为没提交但其实已执行"。

保留窗口 24h，过期视为新请求；定期清理**纳入 S6 的调度任务**。

- [ ] **Step 2: 写失败的乐观锁测试**

`expected_state_version` 不匹配 → **409 并附当前状态**；**并发两次澄清只有一次生效，`slot_state` 不被覆盖**。

现有 `ConversationRow.slot_state` 是 JSONB，**无任何并发保护**。

- [ ] **Step 3: 实现幂等与 `state_version`**

---

### Task 7: SSE、取消与错误响应

**Files:**
- Create: `backend/app/api/v2/events.py`
- Create: `backend/app/api/errors.py`
- Create: `backend/tests/api/test_sse.py`
- Create: `backend/tests/api/test_error_response.py`

- [ ] **Step 1: 写失败的错误响应测试**

断言：错误响应**只含 `{error_code, message, trace_id}`，不含内部 detail**。

`message` 是用户安全文案。当前 `client.ts` 的 `readDetail` 直接展示后端 `detail`，包含 FastAPI 校验细节，属泄漏面。

- [ ] **Step 2: 写失败的 SSE 测试**

- **断线可按 `run_id` 重连并拉取当前状态**
- **SSE 不可用时轮询 `GET /query-runs/{run_id}` 降级路径可用**——不因推送不可用而卡住

SSE 在反向代理下会被缓冲，需明确代理配置要求；轮询降级作为兜底并有测试。

- [ ] **Step 3: 实现 SSE**

事件序列对应五个用户可见阶段；事件内容**只含 public/user 级信息**（S1 分级）。

- [ ] **Step 4: 实现取消端点**

对应 S6 的协作式取消。

---

### Task 8: OpenAPI → TypeScript client

**Files:**
- Create: `frontend/src/api/generated/`（生成物，纳入版本控制）
- Create: `scripts/gen_client.sh`
- Delete: `frontend/src/api/types.ts`（分批迁移后）
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: 建立生成流程**

`src/api/types.ts` 是**手写的**，与 Pydantic 模型漂移无人察觉。

- [ ] **Step 2: CI 校验无漂移**

断言**重新生成后无 diff**——否则漂移会以另一种形式回来。

- [ ] **Step 3: 分批迁移**

按 feature 切换；旧手写类型在对应 feature 迁完后删除。

---

### Task 9: 工作台

**Files:**
- Modify: `frontend/src/views/WorkbenchView.vue`
- Modify: `frontend/src/stores/session.ts`
- Create: `frontend/src/features/workbench/`（features 重组）
- Modify: `backend/app/api/trace.py`（`replay_turn`）
- Create: `backend/tests/api/test_sql_visibility.py`

- [ ] **Step 1: 清除 `orders` 硬编码**

两处：`frontend/src/views/WorkbenchView.vue:19` 的 `const DATASET = 'orders'` 与 `frontend/src/stores/session.ts:26` 的 `const DEFAULT_DATASET = 'orders'`。

登录后按权限加载可用数据 Agent 列表，单个则自动路由，多个则选择。起步做「销售问数 Agent」「运营问数 Agent」这类**专业化 Agent**——语义范围更窄，准确率更高。

- [ ] **Step 2: 进度与取消**

五个用户可见阶段：理解问题 / 解析口径 / 权限与成本检查 / 查询数据 / 生成答案。支持取消与重试。

- [ ] **Step 3: 写失败的 patch 无 LLM 测试**

断言：**澄清与条件 patch 不触发 LLM 调用（调用次数为 0）**。

- [ ] **Step 4: 澄清卡与条件面板改为结构化 patch**

澄清卡直接提交结构化选择（多项可一次确认）；条件面板提交 CanonicalQueryPlan patch，**不再拼自然语言重新识别**。两者都带 `expected_state_version`。

- [ ] **Step 5: 会话恢复**

恢复完整 Answer（引证、假设、警告、列、分页结果引用、Trace 链接）与 slot 状态。**大结果按引用存储**，分页结果重新拉取。

- [ ] **Step 6: 写失败的 SQL 可见性测试**

断言：**物理 SQL 对无权限角色不返回，含 `replay_turn` 路径**。

这修掉 S1 记录的 `replay_turn` 向所有 owner 无条件返回 `sql`/`display_sql` 的问题。后端按级别返回，前端只渲染收到的内容。

- [ ] **Step 7: features 重组**

`frontend/src/` 现按技术类型分层（api / components / stores / views）。改为 features 划分（workbench / trace / semantic-admin / auth），每个 feature 内自带 api / components / store。

**只移动不改逻辑，重组前后测试集必须完全相同且全绿。**

---

### Task 10: Playwright E2E

**Files:**
- Create: `frontend/e2e/`

- [ ] **Step 1: 覆盖七个场景**

OIDC 登录、问数、澄清、**越权 404**、会话恢复、取消、导出。

「越权 404」用例是 S1 对象级鉴权的**端到端守卫，必须有**。

---

### Task 11: Docker 与镜像

**Files:**
- Create: `backend/Dockerfile`
- Create: `frontend/Dockerfile`
- Create: `docker-compose.yml`

- [ ] **Step 1: 多阶段构建、非 root 运行**

- [ ] **Step 2: 不可变镜像标签**

用 commit sha，**不用 `latest`**。

---

### Task 12: CI/CD 流水线

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/cd.yml`
- Create: `renovate.json`

- [ ] **Step 1: 建立流水线**

```
lint/typecheck
  → unit/integration
  → security golden
  → frontend build/e2e
  → real-model canary eval（受控密钥与预算）
  → SAST / secret / dependency / license / SBOM / container scan
  → build immutable image
  → staging migration + smoke + eval diff
  → approval / canary production
  → SLO observation + automated rollback trigger
```

- [ ] **Step 2: 与 S5 门禁衔接**

`security golden` 与 stub 全量在 PR 阶段跑；`real-model canary eval` 用受控密钥与预算，按 S5 的每日/发布前时机。`eval diff` 用 S5 的回归检测，**`regressed` 非空即阻断**。

- [ ] **Step 3: 依赖更新自动化**

Renovate 或 Dependabot，每个更新 PR 自动跑 Golden 与构建。

---

### Task 13: 供应链扫描与 SBOM 基线

**Files:**
- Create: `.github/workflows/supply-chain.yml`
- Create: `docs/security/sbom-baseline.md`

- [ ] **Step 1: 产出首轮基线 SBOM 与漏洞报告**

`fangan.md` 明确提醒：本次审计**未获得完整 npm/pip 审计报告，「未完成」不等于「无漏洞」**。基线报告出来前不做安全性结论。

- [ ] **Step 2: 按严重度分级排期**

首轮可能暴露大量待修漏洞——**预期结果**。产出基线并分级排期，不阻塞本轮交付。

---

## 验收

1. v2 六个端点落地，v1 `/api/chat/*` 兼容层保留且为薄适配层。
2. 幂等、乐观锁、SSE、取消全部可用，含降级路径。
3. 错误响应统一 `{error_code, message, trace_id}`，无内部 detail 泄漏。
4. TS client 由 OpenAPI 生成，CI 校验无漂移。
5. 前端 `orders` 硬编码清除，按权限路由数据 Agent。
6. 澄清与条件面板提交结构化 patch，不触发 LLM。
7. 会话恢复完整答案与 slot；大结果按引用存储。
8. 物理 SQL 按角色可见，`replay_turn` 泄漏修复。
9. 前端按 features 重组，bundle 预算生效。
10. `pyproject.toml` + 锁文件落地，依赖分组，版本范围固定。
11. Alembic 接管 schema，首个 migration 以当前 ORM 为基线；init_db 有生产守卫。
12. lifespan 校验、`/livez` 与 `/readyz` 拆分、结构化日志与 trace id 中间件落地。
13. API server 与 worker 进程分离，Eval 不在 web worker 内。
14. 测试 marker 分层，真实模型测试为受控 job。
15. 前端 lint / typecheck / coverage 接入，warning 视作失败。
16. Playwright 覆盖七个场景。
17. CI 流水线建立，首轮基线 SBOM 与漏洞报告产出。
18. 后端测试基线不回退，新增测试全绿。
