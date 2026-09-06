# Verification: release

Overall: **BLOCKED**

| Check | Status | Detail |
|---|---|---|
| python-compile | PASS | exit=0 |
| ruff | PASS | exit=0 |
| ruff-security | PASS | exit=0 |
| pytest | PASS | exit=0; 102 passed, 2 deselected in 77.86s |
| agent-core-coverage | PASS | exit=0 |
| database-integration | BLOCKED | 真实 PostgreSQL/MySQL 验证未配置：MERIDIAN_TEST_POSTGRES_URL, MERIDIAN_TEST_MYSQL_URL |
| python-dependency-audit | PASS | exit=0 |
| sandbox-proxy-dependency-audit | PASS | exit=0 |
| frontend-check | PASS | exit=0 |
| frontend-build | PASS | exit=0 |
| frontend-dependency-audit | PASS | exit=0 |
| production-compose-config | PASS | exit=0 |
| warehouse-compose-config | PASS | exit=0 |
| eval-contracts | PASS | exit=0 |
| sandbox-integration | BLOCKED | failed to connect to the docker API at unix:///Users/fuyuxiang/.docker/run/docker.sock; check if the path is correct and if the daemon is running: dial unix /Users/fuyuxiang/.docker/run/docker.sock: connect: no such file or directory |
| browser-integration | PASS | exit=0; 2 passed |
| repository-audit | PASS | exit=0 |
| audit-negative-fixture | PASS | exit=0 |
| trino-cluster | BLOCKED | 无法连接真实端点 http://127.0.0.1:8080/v1/info: HTTP Error 502: Bad Gateway |
| trino-workers | BLOCKED | 无法连接真实端点 http://127.0.0.1:8080/v1/node: HTTP Error 502: Bad Gateway |
| livy-batches | BLOCKED | 无法连接真实端点 http://127.0.0.1:8998/batches?from=0&size=1: HTTP Error 502: Bad Gateway |
| warehouse-e2e-evidence | BLOCKED | MERIDIAN_WAREHOUSE_EVIDENCE 未指定；不能证明 Iceberg 物化、2 workers/2 executors、取消恢复与四图邮件链路 |
| target-platform | BLOCKED | MERIDIAN_TARGET_PLATFORM_EVIDENCE 未配置；不会用参考 Trino 自动替代客户目标平台 |
| scale | BLOCKED | MERIDIAN_SCALE_EVIDENCE 未配置；不以合成字节字段冒充 PB/EB 实测 |
| live-model | BLOCKED | MERIDIAN_LIVE_MODEL_EVIDENCE 未配置；必须对至少 12 个任务各运行 3 次真实模型评估 |
| notification | BLOCKED | MERIDIAN_NOTIFICATION_EVIDENCE 未配置；必须由真实外部环境产生 |
| migration-restore | BLOCKED | MERIDIAN_MIGRATION_EVIDENCE 未配置；必须由真实外部环境产生 |
| acceptance-matrix-structure | PASS | 已解析 173 / 173 个唯一验收项；异常：[] |
| acceptance-matrix-gates | BLOCKED | 实现状态=['IMPLEMENTED']；验证状态=['BLOCKED_EXTERNAL', 'PASS_LOCAL'] |
