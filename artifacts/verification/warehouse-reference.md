# Verification: warehouse-reference

Overall: **BLOCKED**

| Check | Status | Detail |
|---|---|---|
| trino-cluster | BLOCKED | 无法连接真实端点 http://127.0.0.1:8080/v1/info: <urlopen error [Errno 1] Operation not permitted> |
| trino-workers | BLOCKED | 无法连接真实端点 http://127.0.0.1:8080/v1/node: <urlopen error [Errno 1] Operation not permitted> |
| livy-batches | BLOCKED | 无法连接真实端点 http://127.0.0.1:8998/batches?from=0&size=1: <urlopen error [Errno 1] Operation not permitted> |
| warehouse-e2e-evidence | BLOCKED | MERIDIAN_WAREHOUSE_EVIDENCE 未指定；不能证明 Iceberg 物化、2 workers/2 executors、取消恢复与四图邮件链路 |
