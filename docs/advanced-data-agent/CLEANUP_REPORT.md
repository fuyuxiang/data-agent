# 收敛清理报告

本次清理以职责和消费者核验为依据。旧对话 Agent 循环被新的唯一内核替换；draw.io 创作平台及其数千静态资源、业务画布 API、GPU/SSH 部署链、远程 runner、shape libraries、宿主改写/Shell/Git 工具、命令 Hook 和专属依赖已从源码、路由、前端菜单、构建及打包路径移除。

未删除用户上传、历史数据库集合、工作流/团队记录、知识/记忆/Skill 内容或备份。清理器 `scripts/audit_repository.py` 是只读扫描器；其 `--self-test-negative` 会注入虚拟残留并确认检测器确实失败，不能生成恒真的 PASS。

原 `tests/test_agent_runtime.py` 与 `tests/test_agent_tool_catalog.py` 的有效意图已迁移到 `tests/test_advanced_agent.py`：单循环、模型协议、Action/预算、租约、发布门禁、远端引用和退役工具负断言。GPU 与业务画布测试随对应范围外功能退役。

清理明细见 `CLEANUP_MANIFEST.json`。实际 Git 删除数量以仓库状态和 `artifacts/verification/repository-audit.json` 为准；本文不硬编码一个会随提交变化的数字。
