# 智能问数平台 (Data Chat Bot)

一个面向业务分析场景的自然语言问数系统。  
你可以上传 CSV 或配置数据源，创建数据集后直接用自然语言提问，系统会通过 LangGraph 编排执行 NL2SQL，并以流式方式返回思考日志与结果。

- 前端：Vue 3 + TypeScript + Element Plus + ECharts
- 后端：FastAPI + SQLAlchemy + SQLite + DuckDB
- 查询编排：LangGraph
- NL2SQL：规则引擎 + 可选 LLM 双轨

## 功能特性

### 1. 账户与工作空间
- 用户注册、登录、JWT 鉴权。
- 登录后自动绑定默认工作空间。
- 支持在系统设置中新增工作空间。

### 2. 数据源与数据集管理
- 支持数据源类型：`mysql`、`postgresql`、`sqlserver`、`duckdb`、`csv`。
- 支持 CSV 文件上传，自动解析行列信息。
- 支持数据源 Schema 刷新与字段列表查看。
- 支持创建数据集并绑定多个数据源（`data_source_ids`）。

### 3. 智能问数与流式返回
- 查询接口支持 SSE (`/api/v1/queries/stream`)。
- 执行过程按节点流式输出：意图识别、SQL 校验、执行、结果整理。
- 支持意图类型：`chat`、`list`、`count`、`search`。
- 前端支持查看生成 SQL、结果明细、图表切换（柱状/折线/饼图）。

### 4. 安全控制
- SQL Guardrail：只允许 `SELECT`，拦截危险关键词与多语句注入。
- 工作空间维度访问校验（核心管理接口均依赖当前用户）。
- 数据源密码使用 Fernet 加密存储。

## 系统架构

```text
┌─────────────────────────────────────────────────────────────┐
│                        Frontend (Vue)                      │
│  Login / DataConfig / Query / History / Settings           │
└───────────────────────┬─────────────────────────────────────┘
                        │ HTTP + SSE (/api/v1/queries/stream)
┌───────────────────────▼─────────────────────────────────────┐
│                      FastAPI Backend                        │
│  auth | data_sources | datasets | queries | history         │
└───────────────────────┬─────────────────────────────────────┘
                        │
        ┌───────────────▼──────────────────────┐
        │      LangGraph Orchestrator          │
        │ parse → validate → execute → format  │
        └───────────────┬──────────────────────┘
                        │
        ┌───────────────▼──────────────┐
        │ DuckDB Execution Engine       │
        │ + CSV load + SQL guardrail    │
        └───────────────┬──────────────┘
                        │
        ┌───────────────▼─────────────────────────────────────┐
        │ SQLite (meta) + DuckDB (query) + CSV files          │
        │ backend/data/chatbot.db / backend/data/duckdb.duckdb│
        └───────────────────────────────────────────────────────┘
```

## 核心查询链路

`POST /api/v1/queries/stream` 的执行路径：

1. 校验 `dataset_id`（可选）。
2. 将数据集关联的 CSV 文件加载到 DuckDB 临时/持久表。
3. 初始化 LangGraph 状态并开始流式执行。
4. 各节点依次执行：
   - `parse_question`：规则 + 可选 LLM 双轨生成 SQL。
   - `validate_sql`：SQL 安全校验。
   - `execute_sql`：DuckDB 执行。
   - `semantic_enhance`：list 场景下可选语义增强。
   - `format_answer`：统一输出格式。
5. 按 SSE 事件持续推送日志和最终结果。

## 技术栈与版本要求

### 后端
- Python `>=3.10`
- FastAPI `>=0.109`
- SQLAlchemy `>=2.0`
- DuckDB `>=1.0`

### 前端
- Node.js `>=18`
- npm `>=9`
- Vite `5.x`

## 目录结构

```text
data-chat-bot/
├── backend/
│   ├── app/
│   │   ├── api/                  # 认证、数据源、数据集、查询、历史
│   │   ├── core/                 # 配置、数据库、安全、日志
│   │   ├── models/               # SQLAlchemy 模型
│   │   ├── schemas/              # Pydantic 请求/响应模型
│   │   ├── services/             # datasource、nl2sql、guardrails 等
│   │   ├── orchestrator_graph.py # LangGraph 状态图
│   │   └── orchestrator_duckdb.py
│   ├── data/                     # sqlite、duckdb、uploads
│   ├── init_db.py
│   └── main.py
├── frontend/
│   ├── src/
│   │   ├── api/
│   │   ├── layouts/
│   │   ├── router/
│   │   ├── stores/
│   │   └── views/
│   ├── package.json
│   └── vite.config.ts
├── logs/
├── start.sh
├── stop.sh
├── restart.sh
└── deploy/
```

## 快速开始

### 方式 A：一键脚本（推荐）

```bash
./start.sh
```

脚本行为：
- 自动检测 Python / Node / npm。
- 缺依赖时自动安装：
  - `backend/requirements.txt`
  - `frontend/node_modules`
- 启动服务并写入 PID/日志到 `logs/`。

默认地址：
- 前端：`http://localhost:50803`
- 后端：`http://localhost:50805`
- OpenAPI：`http://localhost:50805/docs`
- 健康检查：`http://localhost:50805/health`

停止与重启：

```bash
./stop.sh
./restart.sh
```

### 方式 B：手动启动

1. 初始化并启动后端

```bash
cd backend
python3 -m pip install -r requirements.txt
python3 init_db.py
uvicorn main:app --host 0.0.0.0 --port 50805 --reload
```

2. 启动前端

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 50803
```

## 首次使用

当前前端未提供注册页，首次需调用后端注册接口创建账号。

```bash
curl -X POST "http://localhost:50805/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "admin123",
    "email": "admin@example.com",
    "full_name": "Admin"
  }'
```

登录页地址：`http://localhost:50803/login`

## 配置说明

后端使用 `pydantic-settings`，默认读取 `backend/.env`。  
配置来源优先级：环境变量 > `.env` > 代码默认值。

### 核心配置项（API 服务）

定义位置：`backend/app/core/config.py`

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PROJECT_NAME` | `智能问数平台` | FastAPI 项目名称 |
| `DEBUG` | `True` | 调试模式，影响日志级别 |
| `SECRET_KEY` | `your-secret-key-change-in-production` | JWT 签名密钥 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | Token 过期时间（分钟） |
| `DATABASE_URL` | `sqlite:///./data/chatbot.db` | 元数据数据库连接串 |
| `LLM_MODEL` | `None` | LLM 模型名 |
| `LLM_BASE_URL` | `None` | LLM 网关地址 |
| `LLM_API_KEY` | `None` | LLM API Key |
| `CORS_ORIGINS` | `["http://localhost:5173","http://localhost:3000","*"]` | CORS 白名单 |
| `MAX_QUERY_ROWS` | `10000` | 查询最大返回行 |
| `QUERY_TIMEOUT_SECONDS` | `60` | 查询超时时间 |
| `ENCRYPTION_KEY` | `None` | Fernet 密钥（数据源密码加密） |

### 编排器配置项（查询引擎）

定义位置：`backend/app/orchestrator_config.py`

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DUCKDB_PATH` | `:memory:` | DuckDB 路径（当前引擎有自定义默认文件） |
| `DUCKDB_MAX_LIMIT` | `1000` | DuckDB 查询限制 |
| `SQL_MAX_LIMIT` | `1000` | SQL 最大限制 |
| `SQL_DEFAULT_LIMIT` | `10` | 默认 LIMIT |
| `ENABLE_SQL_GUARDRAILS` | `True` | 是否启用 SQL 安全护栏 |
| `MAX_RETRIES` | `3` | SQL 修正最大重试次数 |

### 建议 `.env` 示例

```env
DEBUG=true
SECRET_KEY=change-this-in-production
ACCESS_TOKEN_EXPIRE_MINUTES=1440

LLM_BASE_URL=https://your-openai-compatible-endpoint
LLM_API_KEY=your-api-key
LLM_MODEL=your-model-name

ENCRYPTION_KEY=your-fernet-key
```

生成 Fernet Key：

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 数据存储说明

| 组件 | 路径 | 用途 |
|---|---|---|
| SQLite | `backend/data/chatbot.db` | 用户、工作空间、数据源、数据集等元数据 |
| DuckDB | `backend/data/duckdb.duckdb` | 查询执行引擎 |
| 上传 CSV | `backend/data/uploads/` | CSV 物理文件 |
| 运行日志 | `logs/backend.log`、`logs/frontend.log` | 服务日志 |
| PID 文件 | `logs/backend.pid`、`logs/frontend.pid` | 启停脚本管理 |

## API 概览

基础路径：`/api/v1`

### 认证
- `POST /auth/register`
- `POST /auth/login` (`application/x-www-form-urlencoded`)
- `GET /auth/me`
- `GET /auth/workspaces`
- `POST /auth/workspaces`

### 数据源
- `POST /data-sources`
- `GET /data-sources?workspace_id=...`
- `PATCH /data-sources/{id}`
- `DELETE /data-sources/{id}`
- `POST /data-sources/test`
- `POST /data-sources/upload-csv`
- `GET /data-sources/{id}/schema`
- `POST /data-sources/{id}/refresh-schema`

### 数据集
- `POST /datasets`
- `GET /datasets?workspace_id=...`
- `GET /datasets/{id}`
- `PATCH /datasets/{id}`
- `DELETE /datasets/{id}` (软删除，状态置为 `deprecated`)

### 查询
- `POST /queries/stream` (SSE)
- `POST /queries` (非流式)
- `GET /queries/{trace_id}/replay`

### 历史
- `GET /history?workspace_id=...`
- `GET /history/{history_id}`

## SSE 事件协议（`/queries/stream`）

后端按 `text/event-stream` 返回，每行格式：

```text
data: {"type":"...","...":"..."}
```

常见事件：

- `run_start`：本次运行开始。
- `node_start`：节点开始执行。
- `node_end`：节点结束并附带节点输出。
- `node_error`：节点执行失败。
- `final`：最终结果。
- `done`：流结束标记。

示例：

```text
data: {"type":"run_start","run_id":"a1b2c3d4"}
data: {"type":"node_start","step":"parse_question"}
data: {"type":"node_end","step":"parse_question","outputs":{"intent":"count","sql":"SELECT ..."}}
data: {"type":"final","result":{"type":"count","value":123,"message":"查询结果：共 123 条记录"}}
data: {"type":"done"}
```

## 安全设计

### JWT 认证
- 算法：`HS256`
- 令牌内容包含 `user_id`、`workspace_id`、`exp`
- 认证失败统一返回 `401`

### SQL 安全护栏
位于 `backend/app/services/guardrails.py`，核心策略：
- 仅允许 `SELECT`。
- 拦截 `DROP/DELETE/UPDATE/INSERT/ALTER/CREATE/...`。
- 拦截注释注入与堆叠语句。
- 支持基于表白名单校验。
- 自动补充或约束 `LIMIT`。

### 数据源密码保护
- 数据源密码以 Fernet 加密后存储在 `password_encrypted` 字段。
- 建议生产环境固定配置 `ENCRYPTION_KEY`，避免重启后无法解密历史密码。

## 开发与调试

### 前端

```bash
cd frontend
npm run lint
npm run build
```

前端代理配置在 `frontend/vite.config.ts`：
- `/api` 代理到 `http://localhost:50805`

### 后端

```bash
cd backend
uvicorn main:app --reload --port 50805
```

日志：
- 调试模式下输出可读文本日志。
- 非调试模式下输出 JSON 格式日志。

## 已知限制

- 当前查询主链路面向 DuckDB（尤其是 CSV 加载后的表）；关系型外部数据源尚未全部打通到统一执行链路。
- 历史查询读取接口可用，但查询接口当前未自动写入 `query_histories`。
- 向量检索 `search` 依赖额外包与 LanceDB 数据准备，默认安装不包含该依赖链。
- 仓库内 `deploy/docker-compose.yml` 与本地脚本默认端口不同，使用前请按实际环境统一端口与变量。

## 常见问题

### 1. 登录失败 `401`
- 确认已先调用 `/auth/register` 创建用户。
- 确认前端本地存储中的 `token` 未过期或被污染。

### 2. 查询报“数据集不存在”或无结果
- 确认查询请求中传入了正确 `dataset_id`。
- 确认数据集已绑定数据源并有可读表。

### 3. SQL 执行失败
- 查看 SSE 日志中的 `node_error` 信息。
- 检查问题是否触发 SQL Guardrail（例如敏感关键词或非白名单表）。

### 4. CSV 上传成功但查询不到
- 在“数据配置”中确认数据源 Schema 已刷新。
- 在“智能取数”页重新选择数据集并勾选对应表。

## 部署

仓库提供 `deploy/` 目录作为部署模板：
- `deploy/backend/Dockerfile`
- `deploy/frontend/Dockerfile`
- `deploy/frontend/nginx.conf`
- `deploy/docker-compose.yml`

建议流程：
1. 先本地按本 README 跑通开发版。
2. 再按环境（端口、网关、模型服务、持久化卷）调整部署配置。
3. 生产环境务必替换 `SECRET_KEY`、`LLM_API_KEY` 等敏感项。

## License

MIT
