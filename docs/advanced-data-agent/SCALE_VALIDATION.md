# 规模验证计划与当前结论

当前结论：`BLOCKED_EXTERNAL_EVIDENCE`。本次实现建立了分布式路径、边界和验证脚本，但没有获得授权 PB/EB 数据及目标生产平台，因此不声明 PB/EB 已上线或达到性能目标。

## 分级验证

1. 本地协议层：验证 DatasetRef 不隐式转 DataFrame、系统截断传播、分页未完成传播、作业 UNKNOWN、租约 fencing、Action/预算与发布门禁。
2. 参考集群层：启动固定 Compose，记录真实版本和拓扑；用 Iceberg 分区表执行 Trino estimate/query/scratch CTAS、Livy 两 executor 作业、稳定分页、取消、Runner 重启恢复。
3. 渐进性能层：在授权数据上按 10GB、100GB、1TB 或平台许可档位记录文件数、分区、扫描字节、shuffle、峰值内存、传输、排队、冷热缓存与并发；不外推为 PB/EB。
4. 目标平台层：使用客户目录、身份与资源组重复验证，并附审批、数据范围和 SLO。

## 通过标准

- 大结果不进入 API 进程或浏览器全量数组；仅稳定 DatasetRef/分页和聚合图表。
- Trino 至少两 worker、Spark 至少两 executor 的真实 UI/API 证据。
- 分区裁剪、扫描预算、并发上限、取消与恢复均由引擎状态证明。
- 任一任务缺快照、完整性、授权或校验时不可产生 Publication。
- 证据记录真实数据规模；合成字段、估算值或配置文件存在均不能替代实测。

执行：`python scripts/verify_advanced_agent.py --profile warehouse-reference`、`--profile scale`、`--profile target-platform`。
