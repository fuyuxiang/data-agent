# S7 API v2、前端与交付工程 设计文档

> 上游依据：`fangan.md` 第 10 章（10.1 API v2、10.2 工作台）、第 11 章（11.1 后端、11.2 前端、11.3 CI/CD 与供应链）、第 12 章（目录演进）、5.4（P3 全部条目）。
> 子项目定位：升级路线 S7，依赖 S1（OIDC 与对象级鉴权）、S2（IntentV2）、S3（CanonicalQueryPlan 与 patch 能力）、S4（语义注册中心）、S5（结构化澄清编排器能力 `patch_slot`）、S6（Job/Run 状态机、`error_code`、trace_id）。
> 覆盖问题：P3 全部，以及各子项目委派到本轮的 API 与前端接入项。

## 1. 目标与非目标

### 目标

工程基线目前几乎为空：无 `pyproject.toml`、无锁文件、无 Alembic、无 Docker、无 CI 配置、无 ESLint/Playwright，前端把 `orders` 硬编码在两处、身份靠 `X-Username` 明文头。本子项目把前面六个子项目的能力暴露成可用产品，并建立可交付的工程基线：

- API v2 落地：query-runs、结构化澄清、SSE 进度、取消、幂等、乐观锁、稳定 `error_code`。
- OpenAPI 生成 TypeScript client，消除手工类型同步。
- 工作台按权限路由数据 Agent，澄清与条件面板提交结构化 patch。
- 后端工程基线：pyproject、锁文件、Alembic、lifespan 校验、探针拆分、进程分离、测试 marker。
- 前端工程基线：版本固定、lint/typecheck/coverage、按需加载、bundle 预算、Playwright。
- CI/CD 流水线与供应链扫描，产出首轮 SBOM 与漏洞基线。

### 非目标

| 项 | 去向 |
|---|---|
| 多步深度分析的业务能力 | 不在本轮（S6 只建承载框架） |
| 生产环境的实际部署与灰度执行 | 本轮建流水线与配置，实际发布由运维决策 |
| 观测平台看板搭建 | 不在本轮 |
| 语义管理的完整可视化编辑器 | 本轮只做必要的发布/审批界面，完整编辑器后续 |
| 多租户的完整隔离改造 | 本轮只保留 `tenant_id` 字段贯通，隔离策略后续 |
| 移动端适配 | 不在本轮 |

## 2. 核心设计决策

### 2.1 v1 保留兼容层，不做破坏式切换

`/api/chat/*` 保留。前端逐步迁移到 v2，两套并存到前端完全切换后再决定下线。一次性切换会让前后端必须同步发布，风险不必要。

### 2.2 澄清与条件面板提交结构化 patch，不回炉 LLM

当前澄清把选项 label 重新交给 LLM 猜（P3），条件面板拼自然语言后重新识别。两者都改为直接 patch —— 复用 S5 已实现的 `patch_slot` 与 S3 的 CanonicalQueryPlan patch。这既是准确性问题也是成本问题：确定性操作不该花一次模型调用。

### 2.3 错误响应不泄漏内部细节

统一 `{error_code, message, trace_id}`。`message` 是用户安全文案，内部 detail 不返回。`error_code` 来自 S6 的分类，前端按 code 决策而非按文案匹配。当前 `client.ts` 的 `readDetail` 直接展示后端 `detail`，包含 FastAPI 校验细节，属泄漏面。

### 2.4 大结果不进 Turn JSONB

分页结果按引用存储，可重新拉取。当前 `TurnRow.answer` 是 JSONB，无限存大结果会撑爆行并拖慢会话列表。

### 2.5 物理 SQL 按角色可见

普通用户看逻辑计划与口径，物理 SQL 只对有权限角色显示。这与 S1 的 Trace 四级可见性同源，前端不做自行判断——后端按级别返回，前端只渲染收到的内容。

### 2.6 锁文件不可选

`requirements.txt` 全是 `>=`，构建不可复现。锁文件是"同一 commit 两次构建结果相同"的前提，也是供应链扫描有意义的前提。

### 2.7 生产禁止 DROP TABLE

`scripts/init_db.py` 仅用于本地样本。Alembic 接管 schema 后，init_db 加环境守卫，生产环境执行直接拒绝。

## 3. API v2

### 3.1 端点

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

澄清端点是 S5 `patch_slot` 的薄封装（S5 §4 已约定签名与语义），支持一次提交多个澄清项。

### 3.2 幂等

所有 POST 支持 `Idempotency-Key`。语义：相同 key + 相同请求体 → 返回首次结果，不重复执行；相同 key + 不同请求体 → 409。结果记入 S6 的 `idempotency_outcome`，便于排查"用户以为没提交但其实已执行"。

保留窗口有限（建议 24h），过期后视为新请求。

### 3.3 乐观锁

`Conversation` 增加 `state_version`。澄清与条件 patch 必须带 `expected_state_version`，不匹配 → 409 并返回当前状态。防止并发问数互相覆盖 `slot_state`（现有 `ConversationRow.slot_state` 无任何并发保护）。

### 3.4 SSE 进度事件

事件序列对应 §4.2 的五个用户可见阶段。事件内容只含 public/user 级信息（S1 分级），断线可按 `run_id` 重连并拉取当前状态。SSE 失败时前端降级为轮询 `GET /query-runs/{run_id}`，不因推送不可用而卡住。

### 3.5 鉴权

全部 v2 端点走 S1 的 OIDC 与对象级鉴权：不存在与非本人一律 404。`agent_id` 需校验当前 principal 是否有该数据域权限，无权限同样 404（不返回 403，避免探测数据域是否存在）。

### 3.6 生成 TypeScript client

OpenAPI → TS client 由构建产出，禁止手工维护类型。当前 `src/api/types.ts` 是手写的，与 Pydantic 模型漂移无人察觉。生成物纳入版本控制并在 CI 校验"重新生成后无 diff"，否则漂移会以另一种形式回来。

## 4. 工作台

### 4.1 数据 Agent 路由

`orders` 硬编码在 `frontend/src/views/WorkbenchView.vue:19` 与 `frontend/src/stores/session.ts:26`，两处都移除。登录后按权限加载可用数据 Agent 列表，单个则自动路由，多个则选择。

按 `fangan.md` P3 建议，起步做「销售问数 Agent」「运营问数 Agent」这类专业化 Agent，而不是一个全局 Agent —— 专业化 Agent 的语义范围更窄，准确率更高。

### 4.2 进度与取消

五个用户可见阶段：理解问题 / 解析口径 / 权限与成本检查 / 查询数据 / 生成答案。支持取消（调 `cancel`，对应 S6 的协作式取消）与重试。

### 4.3 澄清卡与条件面板

- 澄清卡：直接提交结构化选择，多个澄清项可一次确认
- 条件面板：提交 CanonicalQueryPlan patch，不再拼自然语言重新识别

两者都带 `expected_state_version`。

### 4.4 会话恢复

恢复完整 Answer（引证、假设、警告、列、分页结果引用、Trace 链接）与 slot 状态。分页结果按引用重新拉取（§2.4）。

### 4.5 SQL 可见性

普通用户看逻辑计划与口径；物理 SQL 只对有权限角色显示，由后端按级别返回（§2.5）。这同时修掉 S1 记录的 `replay_turn` 向所有 owner 无条件返回 `sql`/`display_sql` 的问题。

### 4.6 目录与打包

`frontend/src/` 现按技术类型分层（api / components / stores / views），组件已按 `chat`/`trace`/`workbench`/`admin` 分组但跨层关联靠人记。按 `fangan.md` 第 12 章演进为 features 划分（workbench / trace / semantic-admin / auth），每个 feature 内自带 api / components / store。

Bundle 拆分 Element Plus、管理面与 Trace；Element Plus 按需加载；主 chunk 设 gzip 预算（起步 200 kB，以真实网络性能预算校准）；路由级预取。

## 5. 后端工程基线

| 项 | 现状 | 目标 |
|---|---|---|
| 依赖声明 | `requirements.txt` 全 `>=` | `pyproject.toml` + 锁文件（uv.lock 或等价），运行/开发依赖分组 |
| Python 版本 | 未固定 | 固定支持范围（3.12/3.13） |
| 工具链 | 无 | ruff、mypy 或 pyright、pytest、coverage |
| schema 管理 | `scripts/init_db.py` 建表 | Alembic 管理；init_db 仅本地样本 + 生产守卫 |
| 启动 | `main.py` 直接建 app | lifespan 做配置校验（S1 已定）、连接池预热、优雅 shutdown |
| 探针 | 单一 `/api/health` | 拆 `/livez`（进程活着）与 `/readyz`（依赖就绪） |
| 进程 | 单进程 | API server 与离线 worker 分进程；Eval 不跑在 web worker 内 |
| 日志 | 无结构化 | 结构化日志 + 统一 error taxonomy（S6 的 `error_code`）+ request/trace id 中间件 |
| 测试分层 | `pytest.ini` 仅 `-ra` | marker 分层：unit / integration / contract / security / golden / load；真实模型测试为受控 CI job |
| CORS | 硬编码 `localhost:5173` | 配置化，生产不允许通配 |

Alembic 首个 migration 以当前 ORM 为基线，之后 S1~S6 引入的表与字段变更各自成 migration。S6 委派的 Eval Run 迁库排在此基线之后。

## 6. 前端工程基线

| 项 | 现状 | 目标 |
|---|---|---|
| 版本固定 | 无 `engines`、无 `packageManager` | 固定 Node LTS 与包管理器版本 |
| lint | 无 ESLint、无 Prettier | 两者接入，CI 校验 |
| typecheck | `build` 里跑 `vue-tsc --noEmit` | 独立 `typecheck` script，CI 单独 gate |
| 测试 | Vitest 有，无 coverage 门槛 | coverage 门槛 + **warning 视作失败** |
| E2E | 无 | Playwright，覆盖 OIDC 登录、问数、澄清、越权 404、会话恢复、取消、导出 |
| 打包 | Element Plus 全量引入 | 按需加载 + chunk 拆分 + bundle 预算 |

Playwright 的「越权 404」用例是 S1 对象级鉴权的端到端守卫，必须有。

## 7. CI/CD 与供应链

流水线（`fangan.md` §11.3）：

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

与 S5 门禁的衔接：`security golden` 与 stub 全量在 PR 阶段跑；`real-model canary eval` 用受控密钥与预算，按 S5 §8 的每日/发布前时机。`eval diff` 用 S5 的回归检测，`regressed` 非空即阻断。

Docker：多阶段构建、非 root 运行、不可变镜像标签（commit sha，不用 `latest`）。

依赖更新用 Renovate 或 Dependabot，每个更新 PR 自动跑 Golden 与构建。

**首轮必须产出基线 SBOM 与漏洞报告**。`fangan.md` 明确提醒：本次审计未获得完整 npm/pip 审计报告，「未完成」不等于「无漏洞」。基线报告出来前不做安全性结论。

## 8. 测试

- v2 端点对非本人 / 不存在 run 返回 404
- 无权限 `agent_id` 返回 404 而非 403（不泄漏数据域存在性）
- 相同 `Idempotency-Key` + 相同体不重复执行；+ 不同体返回 409
- `expected_state_version` 不匹配返回 409 并附当前状态
- 并发两次澄清只有一次生效，`slot_state` 不被覆盖
- 错误响应只含 `{error_code, message, trace_id}`，不含内部 detail
- SSE 断线重连可续；SSE 不可用时轮询降级路径可用
- 澄清与条件 patch 不触发 LLM 调用（断言调用次数为 0）
- 物理 SQL 对无权限角色不返回（含 `replay_turn` 路径）
- 前端不含 `orders` 硬编码
- 生成的 TS client 重新生成后无 diff
- `init_db.py` 在生产环境配置下拒绝执行
- `/livez` 与 `/readyz` 语义区分：依赖不可用时 readyz 红而 livez 绿
- lifespan 配置校验失败时进程不进入 ready
- Playwright：OIDC 登录、问数、澄清、越权 404、会话恢复、取消、导出
- bundle 超预算时构建失败

## 9. 验收标准

1. v2 六个端点落地，v1 `/api/chat/*` 兼容层保留。
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
16. Playwright 覆盖 §6 七个场景。
17. CI 流水线按 §7 建立，首轮基线 SBOM 与漏洞报告产出。
18. 后端测试基线不回退，新增测试全绿。

## 10. 风险

| 风险 | 处置 |
|---|---|
| v1/v2 并存期两套逻辑漂移 | v2 为唯一实现，v1 改为薄适配层调用 v2 服务层，不复制逻辑 |
| Alembic 基线与现有本地库不一致 | 首个 migration 用 autogenerate 后人工核对；本地库重建而非在线迁移 |
| 前端 features 重组是大范围移动，易掉测试 | 重组只移动不改逻辑，重组前后测试集必须完全相同且全绿 |
| 生成 TS client 后大量类型不兼容 | 分批迁移，按 feature 切换；旧手写类型在对应 feature 迁完后删除 |
| SSE 在反向代理下被缓冲 | 明确代理配置要求；轮询降级路径作为兜底并有测试 |
| bundle 预算 200 kB 可能不现实 | 先测量当前值再定阈值，首轮设为「不劣化」而非绝对值 |
| 首轮 SBOM 可能暴露大量待修漏洞 | 预期结果。产出基线并按严重度分级排期，不阻塞本轮交付 |
| S7 依赖前面六个子项目全部完成 | 工程基线部分（§5、§6、§7）可与 S1~S6 并行推进；API/前端部分必须在后 |
| 幂等键保留窗口内的存储增长 | 24h 窗口 + 定期清理，纳入 S6 的调度任务 |
