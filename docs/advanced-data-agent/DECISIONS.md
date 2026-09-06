# 关键实现决议

| ID | 决议 | 原因 |
|---|---|---|
| D01 | 保留 Flask、Vue 3 原生模块和 SQLite 控制面 | 遵守增量重构边界；避免无必要框架迁移 |
| D02 | 仅保留一个 AgentLoop | 消除对话、团队的第二推理循环和不同完成语义 |
| D03 | 确定性工作流不改成 LLM 循环，但节点进入 ToolExecutor/Action/预算/发布门禁 | 保留可复用 DAG，同时消除旁路 |
| D04 | 不提供无模型本地伪 Agent 回答 | 无配置时返回明确失败；确定性浏览仍可用 |
| D05 | query id 不是稳定大结果引用 | Trino 大结果必须物化到受管 scratch 表；小结果需完整预览证明 |
| D06 | Spark 接受方法白名单 JobSpec，不接受任意代码 | 保持分布式能力同时封闭远程代码执行 |
| D07 | Web/Agent 只调用独立鉴权 sandbox 代理，代理固定镜像/挂载/限额并独占 Docker socket；不可用时 fail closed | 生成代码不能控制 Docker 参数，Web 容器不持有宿主管理面，且不回退宿主 Python 执行 |
| D08 | ResultManifest 是唯一正式发布源 | 页面、DOCX、PNG、EML、SMTP 不各自重新生成数字 |
| D09 | Skill 编辑、回滚均创建新 candidate | 历史发布版本不可静默修改；必须重新测试审批 |
| D10 | 退役 draw.io、GPU/SSH 部署、宿主写改删/Shell/Git、外部业务 CRUD | 不属于数据分析产品范围，且扩大攻击面与打包体积 |
| D11 | 历史用户数据不由清理脚本删除 | 代码清理与数据迁移分开；审计器只读 |
| D12 | 参考环境和目标生产验收分级 | 不用参考 Compose 替代客户平台，也不把规模外推当实测 |
