# Verification: ci

Overall: **BLOCKED**

| Check | Status | Detail |
|---|---|---|
| python-compile | PASS | exit=0 |
| ruff | PASS | exit=0 |
| ruff-security | PASS | exit=0 |
| pytest | PASS | exit=0; 102 passed, 2 deselected in 80.34s |
| agent-core-coverage | PASS | exit=0 |
| database-integration | BLOCKED | 真实 PostgreSQL/MySQL 验证未配置：MERIDIAN_TEST_POSTGRES_URL, MERIDIAN_TEST_MYSQL_URL |
| python-dependency-audit | PASS | exit=0 |
| frontend-check | PASS | exit=0 |
| frontend-build | PASS | exit=0 |
| frontend-dependency-audit | PASS | exit=0 |
| production-compose-config | PASS | exit=0 |
| warehouse-compose-config | PASS | exit=0 |
| eval-contracts | PASS | exit=0 |
| sandbox-integration | BLOCKED | failed to connect to the docker API at unix:///Users/fuyuxiang/.docker/run/docker.sock; check if the path is correct and if the daemon is running: dial unix /Users/fuyuxiang/.docker/run/docker.sock: connect: no such file or directory |
| browser-integration | PASS | exit=0; 2 passed |
