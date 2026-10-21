# 开发指南

[返回 README](../README.md) · [架构设计](architecture.md) · [部署与运维](operations.md)

## 安装开发依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
```

`requirements-dev.in` 保存直接开发依赖，`requirements-dev.txt` 是兼容安装入口。修改 `.in` 后使用
`requirements-dev.lock` 文件头记录的命令重新生成锁文件。

## 后端质量检查

```bash
ruff check backend scripts tests app.py
ruff check --select S backend scripts app.py
python -m compileall -q backend scripts app.py
pytest -q -m "not database_integration" \
  --cov=backend/agent --cov=backend/api --cov=backend/core --cov=backend/services \
  --cov-report=term --cov-fail-under=60
coverage report --include='backend/agent/*' --fail-under=85
pip-audit -r requirements.lock --no-deps --disable-pip
```

## 前端检查与构建

```bash
npm ci
npx playwright install chromium
npm audit --audit-level=high
npm run check
npm run build
npm run test:browser
```

`npm run check` 会验证本地前端依赖完整性和 JavaScript 语法；`npm run build` 会生成不包含开发时动态模板编译器的 `frontend/dist/` 生产静态资产。完整产品仍需要 Python API 服务。

## 集成测试

PostgreSQL 和 MySQL 连接器测试需要临时数据库，环境变量与执行方式可参考 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)。CI 还会执行覆盖率门槛、锁定依赖安全审计、本地审核分析和浏览器测试。

以下验证配置分别检查仓库代码和依赖真实外部环境的能力：

```bash
python scripts/verify_advanced_agent.py --profile ci
python scripts/verify_advanced_agent.py --profile repository-audit
python scripts/verify_advanced_agent.py --profile warehouse-reference
python scripts/verify_advanced_agent.py --profile target-platform
python scripts/verify_advanced_agent.py --profile scale
python scripts/verify_advanced_agent.py --profile live-model
python scripts/verify_advanced_agent.py --profile notification
python scripts/verify_advanced_agent.py --profile migration-restore
python scripts/verify_advanced_agent.py --profile release
```

缺少真实模型、集群、SMTP、迁移、目标平台或规模证据时，相应 profile 返回 `BLOCKED` 和非零状态，不会把“未执行”写成 PASS。

## 可选：深度学习依赖

基础依赖不强制安装 PyTorch。需要 Torch MLP 或 GRU 时，先根据运行主机的 CPU / CUDA 环境安装适合的 PyTorch wheel，再安装：

```bash
python -m pip install -r requirements-dl.txt
```

## 核心目录

```text
.
├── app.py                       # 应用入口；默认使用 Waitress
├── backend/
│   ├── api/                     # HTTP/SSE 接口与边界校验
│   ├── core/                    # 配置、SQLite 存储、观测性和实例锁
│   ├── agent/                   # 唯一模型协议、AgentLoop、RunStore 与 ToolExecutor
│   ├── skills/                  # Skill 运行时：registry / loader / resolver / executor
│   ├── services/                # 数据面、验证、结果、MCP 与交付服务
│   │   └── results/             # 正式成果验证与交付
│   ├── analysis_modules/        # 统计、机器学习与时序分析实现
│   └── data_cleaning/           # 可追溯的数据处理能力
├── frontend/src/
│   ├── components/              # 统一基础组件、图标、图表、执行状态、结果区块
│   ├── views/                   # 用户端与后台页面
│   ├── styles/                  # 设计令牌与分层样式
│   ├── store.js / router.js     # 全局状态与信息架构
├── skills/                      # 内置 Skill 包（SKILL.md + manifest.yaml）
├── scripts/                     # 构建、前端检查、UI 验证、仓库审计、备份与恢复
├── tests/                       # API、安全、Skill 运行时、演示数据与浏览器测试
├── storage/                     # 本地运行数据（不纳入 Git）
└── docs/                        # 技术架构图与业务数据流图
```
