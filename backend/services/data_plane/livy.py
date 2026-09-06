from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ...core.database import Database, utcnow
from ..security import safe_http_request, validate_outbound_url
from .contracts import DatasetRef, DatasetRefStore


SUPPORTED_METHODS = frozenset({
    "filter_project_aggregate", "window_features", "authorized_join",
    "grouped_trend_anomaly", "mllib_logistic_regression", "mllib_kmeans",
})


@dataclass(frozen=True)
class LivyConfig:
    engine_id: str
    endpoint: str
    job_file: str
    proxy_user: str
    queue: str
    result_prefix: str
    input_prefixes: tuple[str, ...]
    driver_memory: str = "1g"
    executor_memory: str = "2g"
    executor_cores: int = 1
    num_executors: int = 2
    timeout_seconds: int = 60
    max_log_lines: int = 500

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LivyConfig":
        endpoint = validate_outbound_url(str(value.get("endpoint") or "").rstrip("/"))
        result_prefix = str(value.get("result_prefix") or "").rstrip("/") + "/"
        if not result_prefix.startswith(("s3://", "s3a://", "hdfs://", "abfs://", "gs://")):
            raise ValueError("Livy 结果前缀必须是受支持的远端对象或 HDFS URI")
        inputs = tuple(str(item).rstrip("/") + "/" for item in value.get("input_prefixes") or [])
        if not inputs:
            raise ValueError("Livy 必须配置至少一个授权输入前缀")
        return cls(
            engine_id=str(value.get("engine_id") or "spark-livy-reference")[:128], endpoint=endpoint,
            job_file=str(value.get("job_file") or "").strip(), proxy_user=str(value.get("proxy_user") or "").strip(),
            queue=str(value.get("queue") or "default")[:128], result_prefix=result_prefix,
            input_prefixes=inputs, driver_memory=str(value.get("driver_memory") or "1g")[:16],
            executor_memory=str(value.get("executor_memory") or "2g")[:16],
            executor_cores=max(1, min(int(value.get("executor_cores") or 1), 16)),
            num_executors=max(2, min(int(value.get("num_executors") or 2), 1000)),
            timeout_seconds=max(1, min(int(value.get("timeout_seconds") or 60), 600)),
            max_log_lines=max(1, min(int(value.get("max_log_lines") or 500), 2000)),
        )


class LivyBatchAdapter:
    """Remote Spark adapter exposing reviewed methods, never arbitrary model code."""

    capabilities = {
        "protocol": "livy_rest_batches", "submit": True, "poll": True, "cancel": True,
        "reconcile": True, "minimum_executors": 2, "dynamic_arbitrary_code": False,
        "methods": sorted(SUPPORTED_METHODS), "result_manifest": True,
    }

    def __init__(self, database: Database, workspace_id: str, config: LivyConfig):
        self.db = database
        self.workspace_id = workspace_id
        self.config = config

    def submit(self, spec: dict[str, Any], *, run_id: str, action_id: str) -> dict[str, Any]:
        method = str(spec.get("method") or "")
        if method not in SUPPORTED_METHODS:
            raise ValueError(f"不支持的远端分布式方法：{method}")
        input_refs = spec.get("input_refs") or []
        if not isinstance(input_refs, list) or not input_refs:
            raise ValueError("远端作业需要 input_refs")
        for ref in input_refs:
            location = str((ref if isinstance(ref, dict) else {}).get("uri") or "")
            if not any(location == prefix[:-1] or location.startswith(prefix) for prefix in self.config.input_prefixes):
                raise PermissionError("远端作业输入不在授权前缀内")
        output_uri = f"{self.config.result_prefix}{_safe(run_id)}/{_safe(action_id)}/"
        trusted = {
            "method": method, "input_refs": input_refs, "output_uri": output_uri,
            "parameters": _safe_parameters(spec.get("parameters") or {}),
            "contract_version": int(spec.get("contract_version") or 0),
            "policy_version": str(spec.get("policy_version") or ""),
        }
        payload: dict[str, Any] = {
            "file": self.config.job_file,
            "args": ["--job-spec-json", json.dumps(trusted, ensure_ascii=False, separators=(",", ":"))],
            "queue": self.config.queue,
            "driverMemory": self.config.driver_memory,
            "executorMemory": self.config.executor_memory,
            "executorCores": self.config.executor_cores,
            "numExecutors": self.config.num_executors,
            "conf": {
                "spark.dynamicAllocation.enabled": "false",
                "spark.speculation": "false",
            },
        }
        if self.config.proxy_user:
            payload["proxyUser"] = self.config.proxy_user
        response = self._request("POST", "/batches", json=payload)
        value = _payload(response, {200, 201})
        batch_id = value.get("id")
        if batch_id is None:
            raise ConnectionError("Livy 未返回 batch id")
        record_id = f"{self.config.engine_id}:{batch_id}"
        record = self.db.put(
            "remote_batches",
            {
                "id": record_id, "workspace_id": self.workspace_id, "run_id": run_id,
                "action_id": action_id, "engine_id": self.config.engine_id,
                "batch_id": int(batch_id), "app_id": value.get("appId"),
                "state": str(value.get("state") or "starting"), "method": method,
                "spec_hash": hashlib.sha256(json.dumps(trusted, sort_keys=True).encode()).hexdigest(),
                "output_uri": output_uri, "trusted_spec": trusted, "submitted_at": utcnow(),
                "updated_at": utcnow(),
            },
            workspace_id=self.workspace_id,
        )
        return {"status": "ACCEPTED", "job_id": record_id, **self.public(record)}

    def poll(self, job_id: str) -> dict[str, Any]:
        record = self._job(job_id)
        value = _payload(self._request("GET", f"/batches/{record['batch_id']}"), {200})
        state = str(value.get("state") or record.get("state") or "unknown")
        record = self.db.patch(
            "remote_batches", job_id,
            {"state": state, "app_id": value.get("appId") or record.get("app_id"), "updated_at": utcnow()},
            workspace_id=self.workspace_id,
        ) or record
        return self.public(record)

    def logs(self, job_id: str, *, from_line: int = 0, size: int = 100) -> dict[str, Any]:
        record = self._job(job_id)
        size = max(1, min(int(size), self.config.max_log_lines))
        response = self._request("GET", f"/batches/{record['batch_id']}/log", params={"from": max(0, int(from_line)), "size": size})
        value = _payload(response, {200})
        return {"job_id": job_id, "from": value.get("from", from_line), "total": value.get("total"), "log": value.get("log") or []}

    def cancel(self, job_id: str) -> dict[str, Any]:
        record = self._job(job_id)
        response = self._request("DELETE", f"/batches/{record['batch_id']}")
        _payload(response, {200, 201})
        record = self.db.patch(
            "remote_batches", job_id,
            {"state": "cancelling", "cancel_requested_at": utcnow(), "updated_at": utcnow()},
            workspace_id=self.workspace_id,
        ) or record
        return {"job_id": job_id, "cancel_requested": True, **self.public(record)}

    def reconcile(self, job_id: str) -> dict[str, Any]:
        record = self._job(job_id)
        try:
            return self.poll(job_id)
        except (ConnectionError, FileNotFoundError) as exc:
            return {**self.public(record), "state": "unknown", "outcome": "OUTCOME_UNKNOWN", "error": str(exc)}

    def result_manifest(self, job_id: str) -> dict[str, Any]:
        record = self._job(job_id)
        if record.get("state") != "success":
            raise ValueError("Spark 作业未成功，不能读取结果 manifest")
        lines = self.logs(job_id, from_line=0, size=self.config.max_log_lines).get("log") or []
        prefix = "MERIDIAN_RESULT_MANIFEST="
        for line in reversed(lines):
            text = str(line).strip()
            if not text.startswith(prefix):
                continue
            try:
                value = json.loads(text[len(prefix):])
            except json.JSONDecodeError as exc:
                raise ConnectionError("Spark 结果 manifest JSON 无效") from exc
            if not isinstance(value, dict):
                raise ConnectionError("Spark 结果 manifest 必须是对象")
            return value
        raise ConnectionError("Spark 作业已成功但未输出受信任结果 manifest")

    def result_ref(
        self,
        job_id: str,
        *,
        owner_id: str,
        contract_version: int,
        policy_version: str,
        manifest: dict[str, Any],
        retention_until: str | None = None,
    ) -> DatasetRef:
        record = self._job(job_id)
        if record.get("state") != "success":
            raise ValueError("Spark 作业尚未成功，不能登记结果")
        manifest_uri = str(manifest.get("uri") or "")
        if not manifest_uri.startswith(str(record["output_uri"])):
            raise PermissionError("结果 manifest 不属于该作业的授权输出前缀")
        ref = DatasetRef(
            ref_id=self.db.new_id("dsref"), kind="remote_objects", source_refs=tuple(
                str(item.get("ref_id") or item.get("uri") or "") for item in record["trusted_spec"]["input_refs"]
            ), engine_id=self.config.engine_id, location={"object_manifest_ref": manifest_uri, "output_uri": record["output_uri"]},
            snapshot_set=dict(manifest.get("snapshot_set") or {}), source_time=manifest.get("source_time"),
            schema_ref=manifest.get("schema_ref"), grain=manifest.get("grain"), query_id=str(record.get("app_id") or job_id),
            query_hash=record["spec_hash"], contract_version=contract_version, policy_version=policy_version,
            computation_state="complete", result_completeness=str(manifest.get("completeness") or "complete"),
            accuracy=str(manifest.get("accuracy") or "exact"), requested_scope=dict(manifest.get("requested_scope") or {}),
            actual_scope=dict(manifest.get("actual_scope") or {}), sample_metadata=dict(manifest.get("sample_metadata") or {}),
            row_count=manifest.get("row_count"), encoded_bytes=manifest.get("encoded_bytes"),
            preview_ref=manifest.get("preview_ref"), provenance_ref=f"remote_batch:{job_id}",
            retention_until=retention_until, owner_id=owner_id,
            acl={"workspace_id": self.workspace_id, "actor_ids": [owner_id]},
        )
        DatasetRefStore(self.db).put(ref, workspace_id=self.workspace_id, run_id=record.get("run_id"))
        return ref

    def public(self, record: dict[str, Any]) -> dict[str, Any]:
        return {key: record.get(key) for key in (
            "id", "run_id", "action_id", "engine_id", "batch_id", "app_id", "state",
            "method", "output_uri", "submitted_at", "updated_at",
        )} | {"job_id": record["id"]}

    def _job(self, job_id: str) -> dict[str, Any]:
        record = self.db.get("remote_batches", job_id, workspace_id=self.workspace_id)
        if not record:
            raise FileNotFoundError("Livy 作业不存在")
        return record

    def _request(self, method: str, path: str, **kwargs: Any):
        return safe_http_request(
            method, f"{self.config.endpoint}{path}", timeout=self.config.timeout_seconds,
            max_response_bytes=8 * 1024 * 1024, **kwargs,
        )


def _safe(value: str) -> str:
    normalized = "".join(character for character in str(value) if character.isalnum() or character in "-_")
    if not normalized:
        raise ValueError("远端输出标识无效")
    return normalized[:128]


def _safe_parameters(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 100:
        raise ValueError("远端作业 parameters 必须是有界对象")
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) > 50_000:
        raise ValueError("远端作业 parameters 超过大小限制")
    forbidden = {"proxyUser", "queue", "conf", "driverMemory", "executorMemory", "file", "className"}
    if forbidden & set(value):
        raise PermissionError("远端资源、身份和代码入口只能由服务端配置")
    return value


def _payload(response: Any, statuses: set[int]) -> dict[str, Any]:
    if response.status_code not in statuses:
        if response.status_code == 404:
            raise FileNotFoundError("Livy 作业状态已过期")
        raise ConnectionError(f"Livy 请求失败：HTTP {response.status_code}")
    try:
        value = response.json()
    except ValueError as exc:
        raise ConnectionError("Livy 返回了无效 JSON") from exc
    if not isinstance(value, dict):
        raise ConnectionError("Livy 返回对象格式无效")
    return value
