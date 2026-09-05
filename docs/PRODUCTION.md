# 生产运行手册

## 支持边界

当前版本支持部门级、单节点生产部署：一个应用实例、一个持久化存储卷，并由 HTTPS 反向代理提供入口。进程锁会拒绝两个实例同时操作同一 SQLite 存储目录。

当前不支持多副本水平扩容或跨机房高可用。这类部署需要先把元数据迁移到 PostgreSQL，把任务队列迁移到外部队列，把文件和成果迁移到对象存储。

## 上线准备

1. 生成两个独立的随机密钥，长度至少 32 字符：

   ```bash
   openssl rand -base64 48
   openssl rand -base64 48
   ```

2. 配置 `MERIDIAN_SECRET_KEY` 和 `MERIDIAN_ENCRYPTION_KEY`。后者必须持久保存，遗失后已加密的模型、数据库、MCP 和 SSH 凭据无法恢复。
3. 把 `MERIDIAN_TRUSTED_HOSTS` 设为对外域名，把 `MERIDIAN_ALLOWED_ORIGINS` 设为实际 HTTPS Origin。生产启动会拒绝空的可信 Host 配置。
4. 默认保持 `MERIDIAN_COOKIE_SECURE=1`，并通过 HTTPS 反向代理访问。只有本机明文 HTTP 验收时才临时设为 `0`。
5. 将 `MERIDIAN_OUTBOUND_HOST_ALLOWLIST` 限制为模型、Webhook 和 HTTP 数据源必需的域名，例如 `api.openai.com,docs.google.com,open.feishu.cn`。该配置在生产模式下不能为空。不要在公网部署中开启私网访问或 stdio MCP。
6. 所有分析数据库连接使用专用只读账号，并在数据库侧设置行级权限、语句超时和并发限制。
7. 配置 SMTP 后开启 `MERIDIAN_REQUIRE_EMAIL_CODE=1`，对注册邮箱执行验证。密码重置始终需要有效邮箱验证码。

## 启动与验收

```bash
docker compose up --build -d
docker compose ps
curl --fail https://analytics.example.com/api/health
curl --fail https://analytics.example.com/api/ready
```

首次打开页面时创建的账号是系统所有者。此后自助注册默认关闭；工作空间所有者通过 `POST /api/workspaces/{id}/invitations` 生成 24 小时一次性邀请。入站集成通过 `POST /api/workspaces/{id}/integration-token` 轮换独立令牌。

`/api/health` 表示进程存活，`/api/ready` 同时检查 SQLite 和存储目录可写性。编排平台应把流量只发送给 readiness 返回 200 的实例。日志为 JSON，每个 API 响应都包含 `X-Request-ID`。

## 备份与恢复

在低写入时段创建在线备份：

```bash
docker compose exec analytics-workbench python scripts/backup.py
docker compose cp analytics-workbench:/app/storage/backups ./backups
sha256sum backups/*.tar.gz
```

备份脚本使用 SQLite Backup API 生成一致的数据库快照，并将上传、知识、工作空间和成果文件一并归档。备份应定期复制到异机存储并执行恢复演练。

恢复时先停止服务，将归档解压到空的持久化卷，再以原 `MERIDIAN_ENCRYPTION_KEY` 启动。不要把归档覆盖到正在运行的存储目录。

## 容量与运维

- 根据内存设置 `MERIDIAN_MAX_INGEST_ROWS`、`MERIDIAN_MAX_INGEST_CELLS`、`MERIDIAN_SOURCE_SAMPLE_ROWS`、`MERIDIAN_MAX_QUERY_ROWS` 和上传上限。大数据量应使用数据库连接并在源端聚合，不要作为本地文件整表载入。
- 用 `MERIDIAN_DAILY_TOKEN_LIMIT` 限制工作空间每日模型 Token 用量，用模型配置的输出上限限制单次请求。
- 监控 HTTP 5xx、readiness、磁盘使用率、任务失败率、模型费用和数据库查询超时。
- 升级前备份存储卷，在预发环境执行全量测试和关键工作流回放。
- 队列与调度器在进程内运行；异常重启后会保留状态并将未完成通用任务标记失败，工作流需要显式恢复。
