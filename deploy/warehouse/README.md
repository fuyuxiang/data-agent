# Reference Trino/Iceberg/Spark-Livy data plane

This pinned Compose stack is an integration target, not evidence of a customer
PB/EB deployment. It starts a Trino coordinator with two workers, an Iceberg
REST catalog over MinIO, a Spark master with two workers, and Livy Batch.

Run `docker compose -f deploy/warehouse/docker-compose.yml up --build -d`, seed
an Iceberg table, then execute `python scripts/verify_advanced_agent.py
--profile warehouse-reference`. Credentials in this directory are intentionally
reference-only and must not be reused in production.

The Livy endpoint accepts only `deploy/warehouse/jobs/meridian_spark_job.py` and
the six reviewed JobSpec methods. The application must register Trino with
`native_limits_confirmed=true`; Spark inputs and outputs must stay inside the
configured `s3a://warehouse/` prefix. Customer deployments must replace static
keys with platform identities and enforce equivalent Trino resource groups,
Iceberg snapshot retention, object-prefix ACLs, and Spark network policy.
