# 外部来源与验收输入要求

## 真实模型

需要一个支持 Chat Completions 流式工具调用或原生 Responses function_call 的提供商、模型 ID、上下文/输出限制和有效凭据。`MERIDIAN_LIVE_MODEL_EVIDENCE` 指向 JSON 对象，必须包含 `provider`、`model`、`prompt_version`、`tool_version`、`skill_versions`、`thresholds.complete_success_rate` 和 `runs`。`runs` 至少覆盖 12 个不同 `task_id`，每个至少 3 次；每次记录 `status`、`safety_pass` 和 `published_value_match`，完整成功率门槛不低于 0.9。评估证据记录协议、版本、token 与失败，不得保存密钥。

## 参考数据面

`deploy/warehouse/docker-compose.yml` 固定 Trino 466、Spark 3.5.3、Livy 0.8.0-incubating、Iceberg REST 1.6.0 和 MinIO。至少验证两台 Trino worker、两台 Spark worker/executor、Iceberg snapshot、scratch CTAS、Livy manifest、取消与恢复。`MERIDIAN_WAREHOUSE_EVIDENCE` 指向 JSON，必须包含 `reference_version`、`trino_worker_count>=2`、`spark_executor_count>=2`、`iceberg_snapshot`、`spark_manifest`、`cancel_recovery`、`native_limits`、`authorization`、`notification_received` 和 `chart_count=4`。

## 客户目标平台

`MERIDIAN_TARGET_PLATFORM_EVIDENCE` 指向经审批的 JSON 对象，验收器必需字段为 `platform`、`engine`、`version`、`identity`、`authorization`、`query`、`materialization`、`cancellation`、`recovery` 和 `controlled_load`。报告内还应给出目录与 SQL 方言、行列权限、结果区、快照/保留、队列/预算、网络/TLS、SLO 和负责人。缺失时目标平台状态必须 BLOCKED。

## 规模、通知、迁移

- `MERIDIAN_SCALE_EVIDENCE`：JSON 必须包含 `authorized_scope=true`、正数 `dataset_bytes/scanned_bytes/processed_rows/concurrency/p50_seconds/p95_seconds`、`cluster`、包含 `cold` 和 `warm` 的 `cache_modes`、`failure_recovery` 与 `cost`；另记录文件/分区、倾斜、shuffle、传输与资源曲线。
- `MERIDIAN_NOTIFICATION_EVIDENCE`：JSON 必须包含 `transport`、`recipient`、`message_id`、`received=true` 和非空 `attachment_hashes`，由本地测试邮箱或真实 SMTP 收件端产生。
- `MERIDIAN_MIGRATION_EVIDENCE`：JSON 必须包含 `backup_sha256`、相同的 `source_counts/restored_counts`、`integrity_pass=true`、`restore_pass=true` 和 `rollback_tested=true`，使用脱敏旧库副本生成。

任何证据文件都不得包含长期凭据、未脱敏个人数据或未经授权的大表内容。
