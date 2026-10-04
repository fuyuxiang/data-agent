from __future__ import annotations

import json
import subprocess

import pandas as pd
import pytest

from backend.services.data_plane.livy import LivyBatchAdapter, LivyConfig
from backend.services.data_plane.local_analysis import LocalAnalysisRunner
from backend.services.data_plane.reviewed_analysis import _parquet_safe_frame
from backend.services.data_plane.trino import TrinoAdapter, TrinoConfig


class Response:
    def __init__(self, value, status_code: int = 200, *, content: bytes | None = None):
        self.value = value
        self.status_code = status_code
        self.content = content if content is not None else (
            b"invalid" if isinstance(value, Exception) else json.dumps(value).encode()
        )
        self.headers = {"Content-Type": "application/json"}
        self.text = self.content.decode(errors="replace")
        self.ok = status_code < 400

    def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_reviewed_describe_result_with_categorical_values_is_parquet_safe(tmp_path):
    frame = pd.DataFrame({"group": ["a", "a", "b"], "value": [1, 2, 3]})

    result = _parquet_safe_frame(frame.describe(include="all").reset_index())
    target = tmp_path / "result.parquet"
    result.to_parquet(target, index=False)
    restored = pd.read_parquet(target)

    assert str(result["group"].dtype) == "string"
    assert restored.loc[2, "group"] == "a"
    assert pd.isna(restored.loc[4, "group"])


def test_reviewed_grouped_result_has_flat_unique_parquet_columns():
    frame = pd.DataFrame({"group": ["a", "a", "b"], "value": [1, 2, 3]})
    grouped = frame.groupby("group", dropna=False).agg(["count", "mean"]).reset_index()

    result = _parquet_safe_frame(grouped)

    assert result.columns.tolist() == ["group", "value_count", "value_mean"]
    collision = pd.DataFrame([[1, 2]], columns=pd.MultiIndex.from_tuples([
        ("value", "count"), ("value_count", ""),
    ]))
    with pytest.raises(ValueError, match="duplicate column names"):
        _parquet_safe_frame(collision)


def test_trino_statement_protocol_catalog_query_materialization_and_cancel(app, monkeypatch):
    database = app.extensions["meridian_db"]
    adapter = TrinoAdapter(database, "default", TrinoConfig(
        engine_id="trino", endpoint="https://trino.example.test", user="actor",
        catalog="lake", schema="analytics", scratch_catalog="lake", scratch_schema="scratch",
        max_preview_rows=3,
    ))

    def small(sql: str, *, limit: int, validate: bool = True):
        assert limit > 0
        if sql == "SHOW CATALOGS":
            return [["system"], ["lake"], ["lake"]]
        if sql.startswith("SHOW SCHEMAS"):
            return [["analytics"], ["scratch"]]
        if sql.startswith("SHOW TABLES"):
            return [["orders"], ["customers"]]
        if sql.startswith("DESCRIBE"):
            return [["amount", "decimal(18,2)", ""], ["region", "varchar", "partition key"]]
        if sql.startswith("EXPLAIN"):
            return [[json.dumps({"catalog": "lake", "estimated": True})]]
        return [["analytics", "orders"], ["analytics", "order_items"]]

    monkeypatch.setattr(adapter, "_execute_small", small)
    assert adapter.discover(limit=1) == {"items": ["analytics"], "next_cursor": "analytics"}
    assert adapter.discover(catalog="lake", limit=10)["items"] == ["analytics", "scratch"]
    assert adapter.discover(catalog="lake", schema="analytics", limit=10)["items"] == ["customers", "orders"]
    assert adapter.search("order", limit=1)["limited"] is True
    assert adapter.describe("lake", "analytics", "orders")["columns"][1]["extra"] == "partition key"
    assert adapter.estimate("SELECT 1")["raw_json"]["estimated"] is True
    catalog_adapter = TrinoAdapter(database, "default", TrinoConfig(
        engine_id="trino-catalogs", endpoint="https://trino.example.test", user="actor",
        catalog="", schema="",
    ))
    monkeypatch.setattr(catalog_adapter, "_execute_small", small)
    assert catalog_adapter.discover(limit=1) == {"items": ["lake"], "next_cursor": "lake"}

    posted: list[str] = []
    post_count = 0

    def request(method: str, url: str, **kwargs):
        nonlocal post_count
        if method == "POST":
            post_count += 1
            posted.append(kwargs["data"].decode())
            if post_count == 1:
                return Response({
                    "id": "q-preview", "nextUri": "https://trino.example.test/q-preview/1",
                    "columns": [{"name": "amount"}], "data": [[1]],
                    "stats": {"outputPositions": 2, "processedRows": 20},
                })
            if post_count == 2:
                return Response({"id": "q-materialized", "updateCount": 20, "stats": {"outputPositions": 1}})
            return Response({"id": "q-cancel", "nextUri": "https://trino.example.test/q-cancel/1"})
        if method == "GET":
            return Response({
                "id": "q-preview", "data": [[2]], "stats": {"outputPositions": 2, "processedRows": 20},
            })
        return Response({}, 204)

    monkeypatch.setattr(adapter, "_request", request)
    preview = adapter.submit("SELECT amount FROM orders", run_id="run-1", action_id="action-1", source_refs=["src"])
    assert preview["status"] == "ACCEPTED"
    finished = adapter.poll(preview["query_id"])
    assert finished["status"] == "finished"
    assert adapter.read_page(preview["query_id"], limit=1)["next_offset"] == 1
    assert adapter.stats(preview["query_id"])["raw"]["processedRows"] == 20
    database.patch("warehouse_queries", preview["query_id"], {"run_id": None}, workspace_id="default")
    ref = adapter.result_ref(
        preview["query_id"], owner_id="actor", contract_version=1, policy_version="policy-v1",
    )
    assert ref.kind == "logical_relation" and ref.row_count == 2

    materialized = adapter.submit(
        "SELECT region, sum(amount) FROM orders GROUP BY region",
        run_id="run-1", action_id="action-2", result_mode="materialize", source_refs=["src"],
    )
    assert posted[-1].startswith('CREATE TABLE "lake"."scratch"."meridian_run_')
    database.patch("warehouse_queries", materialized["query_id"], {"run_id": None}, workspace_id="default")
    durable = adapter.result_ref(
        materialized["query_id"], owner_id="actor", contract_version=1, policy_version="policy-v1",
    )
    assert durable.kind == "remote_table" and durable.row_count == 20

    cancelling = adapter.submit("SELECT 3", run_id="run-1", action_id="action-3")
    assert adapter.cancel(cancelling["query_id"])["cancel_requested"] is True
    assert adapter.cancel(preview["query_id"])["cancel_requested"] is False
    with pytest.raises(ValueError, match="result_mode"):
        adapter.submit("SELECT 1", run_id="run-1", action_id="bad", result_mode="download")
    with pytest.raises(FileNotFoundError):
        adapter.poll("missing")


def test_trino_small_paging_and_response_validation(app, monkeypatch):
    adapter = TrinoAdapter(app.extensions["meridian_db"], "default", TrinoConfig(
        engine_id="trino", endpoint="https://trino.example.test", user="actor", catalog="lake", schema="default",
    ))
    responses = iter([
        Response({"data": [[1]], "nextUri": "https://trino.example.test/q/1"}),
        Response({"data": [[2]], "nextUri": "https://trino.example.test/q/2"}),
        Response({}, 204),
    ])
    calls: list[str] = []

    def request(method: str, _url: str, **_kwargs):
        calls.append(method)
        return next(responses)

    monkeypatch.setattr(adapter, "_request", request)
    assert adapter._execute_small("SELECT value FROM t", limit=2) == [[1], [2]]
    assert calls == ["POST", "GET", "DELETE"]
    with pytest.raises(ConnectionError, match="HTTP 500"):
        adapter._payload(Response({}, 500))
    with pytest.raises(ConnectionError, match="无效 JSON"):
        adapter._payload(Response(ValueError("bad")))
    with pytest.raises(ConnectionError, match="对象格式"):
        adapter._payload(Response([]))


def test_livy_trusted_job_lifecycle_manifest_and_result_ref(app, monkeypatch):
    database = app.extensions["meridian_db"]
    adapter = LivyBatchAdapter(database, "default", LivyConfig(
        engine_id="livy", endpoint="https://livy.example.test", job_file="local:/opt/jobs/runner.py",
        proxy_user="analysis-user", queue="analytics", result_prefix="s3a://results/meridian/",
        input_prefixes=("s3a://authorized/",), num_executors=2,
    ))
    submitted: list[dict] = []

    def request(method: str, path: str, **kwargs):
        if method == "POST":
            submitted.append(kwargs["json"])
            return Response({"id": 7, "state": "starting"}, 201)
        if path.endswith("/log"):
            manifest = {
                "uri": "s3a://results/meridian/run-1/action-1/manifest.json",
                "row_count": 40, "encoded_bytes": 1024, "completeness": "complete",
                "accuracy": "exact", "snapshot_set": {"orders": "snapshot-1"},
            }
            return Response({"from": 0, "total": 2, "log": ["started", f"MERIDIAN_RESULT_MANIFEST={json.dumps(manifest)}"]})
        if method == "GET":
            return Response({"id": 7, "state": "success", "appId": "application-7"})
        return Response({"msg": "deleted"})

    monkeypatch.setattr(adapter, "_request", request)
    created = adapter.submit({
        "method": "grouped_trend_anomaly",
        "input_refs": [{"ref_id": "input-1", "uri": "s3a://authorized/orders/"}],
        "parameters": {"group": "region"}, "contract_version": 2, "policy_version": "policy-v1",
    }, run_id="run-1", action_id="action-1")
    assert created["status"] == "ACCEPTED"
    assert submitted[0]["proxyUser"] == "analysis-user"
    assert submitted[0]["numExecutors"] == 2
    job_id = created["job_id"]
    assert adapter.poll(job_id)["state"] == "success"
    assert adapter.logs(job_id, size=9999)["total"] == 2
    manifest = adapter.result_manifest(job_id)
    database.patch("remote_batches", job_id, {"run_id": None}, workspace_id="default")
    ref = adapter.result_ref(
        job_id, owner_id="actor", contract_version=2, policy_version="policy-v1", manifest=manifest,
    )
    assert ref.kind == "remote_objects" and ref.row_count == 40
    assert adapter.cancel(job_id)["cancel_requested"] is True

    with pytest.raises(ValueError, match="不支持"):
        adapter.submit({"method": "arbitrary", "input_refs": [{"uri": "s3a://authorized/a"}]}, run_id="r", action_id="a")
    with pytest.raises(PermissionError, match="授权前缀"):
        adapter.submit({"method": "mllib_kmeans", "input_refs": [{"uri": "s3a://other/a"}]}, run_id="r", action_id="a")
    with pytest.raises(PermissionError, match="服务端配置"):
        adapter.submit({
            "method": "mllib_kmeans", "input_refs": [{"uri": "s3a://authorized/a"}],
            "parameters": {"queue": "escape"},
        }, run_id="r", action_id="a")
    with pytest.raises(PermissionError, match="不属于"):
        database.patch("remote_batches", job_id, {"state": "success"}, workspace_id="default")
        adapter.result_ref(
            job_id, owner_id="actor", contract_version=2, policy_version="policy-v1",
            manifest={"uri": "s3a://attacker/result"},
        )
    with pytest.raises(FileNotFoundError):
        adapter.poll("missing")


def test_local_analysis_runner_executes_reviewed_method(tmp_path):
    input_root = tmp_path / "inputs"
    output_root = tmp_path / "outputs"
    input_dir = input_root / "task"
    input_dir.mkdir(parents=True)
    (input_dir / "input.csv").write_text("group,value\na,1\na,2\nb,3\n", encoding="utf-8")
    runner = LocalAnalysisRunner(input_root=input_root, output_root=output_root)

    result = runner.execute(
        {"input": "input.csv", "method": "describe", "parameters": {}},
        input_dir=input_dir, run_id="run-1",
    )
    assert result["status"] == "SUCCEEDED"
    assert result["backend"] == "reviewed-local-worker"
    assert result["files"][0]["sha256"]
    assert result["metrics"]["input_rows"] == 3
    assert (output_root / "run-1" / "manifest.json").is_file()
    assert len(pd.read_parquet(output_root / "run-1" / "result.parquet")) > 0


def test_local_analysis_runner_rejects_generated_code_and_path_escape(tmp_path):
    input_root = tmp_path / "inputs"
    input_dir = input_root / "task"
    input_dir.mkdir(parents=True)
    (input_dir / "input.csv").write_text("value\n1\n", encoding="utf-8")
    runner = LocalAnalysisRunner(input_root=input_root, output_root=tmp_path / "outputs")

    with pytest.raises(ValueError, match="固定的审核"):
        runner.execute(
            {"input": "input.csv", "method": "describe", "code": "print('unsafe')"},
            input_dir=input_dir, run_id="blocked",
        )
    with pytest.raises(ValueError, match="文件名"):
        runner.execute(
            {"input": "../input.csv", "method": "describe"},
            input_dir=input_dir, run_id="escape",
        )
    with pytest.raises(PermissionError, match="受管"):
        runner.execute(
            {"input": "input.csv", "method": "describe"},
            input_dir=input_root, run_id="bad-root",
        )
    assert not (tmp_path / "outputs").exists()


def test_local_analysis_runner_cancels_worker(tmp_path, monkeypatch):
    from backend.services.data_plane import local_analysis as module

    input_root = tmp_path / "inputs"
    input_dir = input_root / "task"
    input_dir.mkdir(parents=True)
    (input_dir / "input.csv").write_text("value\n1\n", encoding="utf-8")
    runner = LocalAnalysisRunner(input_root=input_root, output_root=tmp_path / "outputs")

    class WaitingProcess:
        killed = False

        def communicate(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired("reviewed-analysis", timeout)
            return "", ""

        def kill(self):
            self.killed = True

    process = WaitingProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(InterruptedError, match="取消"):
        runner.execute(
            {"input": "input.csv", "method": "describe"},
            input_dir=input_dir, run_id="cancelled", should_cancel=lambda: True,
        )
    assert process.killed is True
