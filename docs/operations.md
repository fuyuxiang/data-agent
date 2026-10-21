# 部署与运维

[返回 README](../README.md) · [架构设计](architecture.md) · [开发指南](development.md)

当前交付形态为单节点私有化部署。本文说明环境配置、生产启动、运行限制、备份恢复与安全控制。

## 配置

完整模板见 [`.env.example`](../.env.example)。本机开发可以使用默认值；生产环境必须显式注入密钥和信任边界。

> [!IMPORTANT]
> `app.py` 不会自动加载 `.env` 文件。请通过 Shell、进程管理器或密钥管理服务注入环境变量。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MERIDIAN_ENV` | `development` | `development` / `production` / `test` |
| `MERIDIAN_HOST` / `MERIDIAN_PORT` | `127.0.0.1` / `5001` | HTTP 监听地址与端口 |
| `MERIDIAN_STORAGE_DIR` | `./storage` | SQLite、上传文件、知识、交付物和回收站根目录 |
| `MERIDIAN_SECRET_KEY` | 开发环境自动生成 | 会话签名密钥；生产环境至少 32 字符 |
| `MERIDIAN_ENCRYPTION_KEY` | 开发环境复用会话密钥 | 外部凭据静态加密密钥；生产必须独立持久化 |
| `MERIDIAN_BACKUP_KEY` | 空 | 备份加密密钥；生产必须与前两个密钥不同 |
| `MERIDIAN_BOOTSTRAP_TOKEN` | 空 | 生产首位系统所有者的一次性初始化令牌，至少 32 字符 |
| `MERIDIAN_METRICS_TOKEN` | 空 | Prometheus `/api/metrics` Bearer Token；生产必须至少 32 字符 |
| `MERIDIAN_TRUSTED_HOSTS` | 空 | 允许访问应用的 Host；生产必填 |
| `MERIDIAN_ALLOWED_ORIGINS` | 本机开发地址 | 允许携带凭据调用 API 的 Origin |
| `MERIDIAN_OUTBOUND_HOST_ALLOWLIST` | 空 | 模型、MCP、HTTP 数据源、Webhook 等出站目标域名；生产必填 |
| `MERIDIAN_DATABASE_HOST_ALLOWLIST` | 空 | 允许登记的外部数据库域名 |
| `MERIDIAN_ALLOW_PRIVATE_NETWORK` | `0` | 是否允许一般 HTTP 出站访问内网地址 |
| `MERIDIAN_DATABASE_ALLOW_PRIVATE_NETWORK` | 开发 `1` / 生产 `0` | 是否允许数据库连接访问本机或内网地址；可用 `0`/`1` 显式覆盖 |
| `MERIDIAN_COOKIE_SECURE` | 生产为 `1` | 仅允许在 HTTPS 上发送会话 Cookie |
| `MERIDIAN_MAX_UPLOAD_MB` | `100` | 单次上传大小上限 |
| `MERIDIAN_MAX_QUERY_ROWS` | `10000` | 服务端查询结果行上限 |
| `MERIDIAN_MAX_QUERY_MB` | `20` | 单次查询物化结果的编码/内存字节上限（MB） |
| `MERIDIAN_MAX_QUERY_CELL_KB` | `1024` | 单个字符串或二进制结果单元格上限（KB） |
| `MERIDIAN_QUERY_TIMEOUT_SECONDS` | `30` | 外部数据库查询超时 |
| `MERIDIAN_DATABASE_QUERY_CONCURRENCY` | `4` | 单应用内每个数据库端点的原生查询并发上限 |
| `MERIDIAN_DATABASE_QUERY_QUEUE_SECONDS` | `10` | 等待数据库查询额度的最大秒数 |
| `MERIDIAN_DATABASE_MAX_SCAN_ROWS` | `50000000` | 执行计划预计扫描行数预算；简单有界预览按返回上限检查 |
| `MERIDIAN_DATABASE_MAX_SCAN_MB` | `2048` | 无筛选查询的元数据估计扫描大小预算（MB） |
| `MERIDIAN_DATABASE_QUERY_MEMORY_MB` | `512` | Doris / StarRocks 会话 `exec_mem_limit`（MB）；实际资源隔离仍需数据库工作负载配置 |
| `MERIDIAN_DAILY_TOKEN_LIMIT` | `5000000` | 每工作空间每日模型 token 配额 |
| `MERIDIAN_LOCAL_ANALYSIS_TIMEOUT_SECONDS` | `120` | 固定审核分析方法的子进程超时秒数；不执行生成的 Python 代码 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | 参见模板 | 可选的环境级 OpenAI-Compatible 默认服务 |

与资源边界相关的行数、单元格数、分析规模、Agent 迭代、任务队列、会话时长、SMTP 和嵌入模型变量，请直接查看 [`.env.example`](../.env.example)。

## 生产部署

在运行主机上安装 Python 依赖并构建前端，然后至少配置以下环境变量：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
npm ci
npm run check
npm run build

# 以下密钥仅在首次部署时生成；重启与升级时应复用并妥善保管。
export MERIDIAN_ENV=production
export MERIDIAN_FRONTEND_DIR="$PWD/frontend/dist"
export MERIDIAN_SECRET_KEY="$(openssl rand -hex 32)"
export MERIDIAN_ENCRYPTION_KEY="$(openssl rand -hex 32)"
export MERIDIAN_BACKUP_KEY="$(openssl rand -hex 32)"
export MERIDIAN_BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"
export MERIDIAN_METRICS_TOKEN="$(openssl rand -hex 32)"
export MERIDIAN_TRUSTED_HOSTS="analytics.example.com"
export MERIDIAN_ALLOWED_ORIGINS="https://analytics.example.com"
export MERIDIAN_OUTBOUND_HOST_ALLOWLIST="api.openai.com"
python app.py
```

启动后可从另一个终端执行 `curl --fail http://127.0.0.1:5001/api/health`。默认仅监听 `127.0.0.1`。首次打开页面时还必须输入部署时生成的 `MERIDIAN_BOOTSTRAP_TOKEN`；创建首位所有者后注册入口自动关闭。请在前方配置 HTTPS 反向代理，并将对外域名精确写入 `MERIDIAN_TRUSTED_HOSTS` 和 `MERIDIAN_ALLOWED_ORIGINS`。所有实际使用的模型、MCP、HTTP 数据源和通知服务域名都应纳入出站白名单。

`app.py` 在非 debug 模式下使用 Waitress。生产环境禁止启用 `MERIDIAN_DEBUG=1`，且会拒绝不完整的前端产物或弱密钥配置。有界分析以本地子进程执行固定审核方法；生成的任意 Python 代码会被拒绝。生产部署还应使用专用低权限系统账号和进程管理器。

### 运行边界

> [!WARNING]
> 当前生产形态是**单节点**：控制面使用 SQLite，进程会对 `storage/.instance.lock` 加独占锁。不要将多个应用副本指向同一个 `storage` 目录。如需多副本高可用，必须先将控制面、任务队列和文件存储迁移到可共享的外部服务。

### 健康检查

- `GET /api/health`：进程与数据库基础健康状态。
- `GET /api/ready`：业务就绪检查；生产环境在所有者、模型和本地审核分析能力任一未就绪时返回 503，并列出缺失项。

## 备份与恢复

备份工具会使用 SQLite Online Backup API 创建一致性快照，连同上传文件、知识和交付物归档；设置 `MERIDIAN_BACKUP_KEY` 后使用 AES-GCM 加密并输出 SHA-256。

```bash
export MERIDIAN_BACKUP_KEY="<与应用密钥分离保管的至少-32-字符密钥>"
python scripts/backup.py --storage storage
```

恢复时应先停止应用，将备份验证并解包到一个**空目录**：

```bash
python scripts/restore.py storage/backups/meridian-YYYYMMDDTHHMMSSZ.tar.gz.enc \
  --destination /path/to/empty-restore-root \
  --sha256 <backup-sha256>
```

恢复命令会拒绝路径越界、链接或非法归档成员，并对数据库执行 `PRAGMA integrity_check`。验证通过后，再由运维流程将 `<destination>/storage` 替换为实际数据目录。

## 安全边界

应用实现以下访问与执行控制：

- **SQL 只读：** 使用 sqlglot 解析单条 `SELECT` / `WITH` / 集合查询，禁止 DDL/DML、外部文件/网络函数、多语句、导出和锁定读取；拒绝覆盖资源限制的查询提示。支持的数据库会话启用只读事务与超时，查询还受扫描、并发、行数和字节预算约束；数据库账号应独立配置只读权限。
- **派生数据与删除：** 清洗结果作为新派生数据集保存；常规删除进入可恢复的归档/回收站，永久删除需显式确认。
- **秘密保护：** 模型、数据源、MCP 和通知凭据使用应用主密钥加密落库，API 只返回脱敏状态。
- **出站防护：** 外部 HTTP 请求校验 scheme、域名白名单和解析后 IP，默认禁止本机、内网、链路本地与保留地址，并限制重定向和响应体大小。
- **身份与隔离：** 生产环境强制登录，首位所有者需初始化令牌；工作空间角色、数据源成员白名单和私有会话所有权同时生效，且结果、任务、快照与导出会重新检查当前数据授权；写请求受 Origin 和 CSRF 校验保护。
- **受控执行：** stdio MCP 默认关闭；Agent 不具备宿主写改删、Shell/Git、自改 Hook 或任意远程代码能力；有界分析仅运行固定审核方法，并对输入、输出和执行时间设限。
- **可追溯：** 查询、分析、工具调用、快照恢复和交付动作保留审计证据。

安全控制不代替部署环境的 TLS、网络分区、最小权限数据库账号、密钥托管、异地备份和安全监控。生产上线前应根据组织的数据分类分级和合规要求完成独立评审。
