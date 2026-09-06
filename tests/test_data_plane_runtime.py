from __future__ import annotations

import json
import subprocess

import pytest
import requests

from backend.services.data_plane.livy import LivyBatchAdapter, LivyConfig
from backend.services.data_plane.sandbox import SandboxLimits, SandboxRunner, SandboxUnavailable
from backend.services.data_plane.sandbox_client import SandboxClient
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


def test_sandbox_runner_enforces_container_flags_and_validates_outputs(tmp_path, monkeypatch):
    from backend.services.data_plane import sandbox as module

    input_root = tmp_path / "inputs"
    output_root = tmp_path / "outputs"
    input_dir = input_root / "task"
    input_dir.mkdir(parents=True)
    output_root.mkdir()
    (input_dir / "input.csv").write_text("value\n1\n", encoding="utf-8")
    runner = SandboxRunner(
        image="meridian-sandbox:py311-20260906", input_root=input_root, output_root=output_root,
        limits=SandboxLimits(output_bytes=1024),
    )
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/local/bin/docker")
    monkeypatch.setattr(runner, "capability", lambda: {"available": True})
    commands: list[list[str]] = []

    class Process:
        returncode = 0

        def __init__(self, command):
            commands.append(command)
            output = output_root / "run-1"
            (output / "result.parquet").write_bytes(b"parquet")
            (output / "manifest.json").write_text(json.dumps({
                "files": [{"path": "result.parquet"}], "metrics": {"output_rows": 1},
            }), encoding="utf-8")

        def communicate(self, timeout=None):
            return "ok", ""

    monkeypatch.setattr(module.subprocess, "Popen", lambda command, **_kwargs: Process(command))
    result = runner.execute({"input": "input.csv", "method": "describe"}, input_dir=input_dir, run_id="run-1")
    assert result["status"] == "SUCCEEDED"
    assert result["files"][0]["sha256"]
    command = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--user") + 1] == "65534:65534"
    assert "--read-only" in command and "no-new-privileges:true" in command
    assert not list((output_root / "run-1").glob("meridian-sandbox-*.json"))

    volume_runner = SandboxRunner(
        image="meridian-sandbox:py311-20260906", input_root=input_root,
        output_root=output_root, docker_volume="meridian-data",
    )
    monkeypatch.setattr(volume_runner, "capability", lambda: {"available": True})

    class VolumeProcess(Process):
        def __init__(self, command):
            commands.append(command)
            output = output_root / "run-volume"
            (output / "result.parquet").write_bytes(b"parquet")
            (output / "manifest.json").write_text(json.dumps({
                "files": [{"path": "result.parquet"}], "metrics": {},
            }), encoding="utf-8")

    monkeypatch.setattr(module.subprocess, "Popen", lambda command, **_kwargs: VolumeProcess(command))
    volume_runner.execute({}, input_dir=input_dir, run_id="run-volume")
    mounts = [commands[-1][index + 1] for index, value in enumerate(commands[-1]) if value == "--mount"]
    assert mounts == [
        "type=volume,src=meridian-data,dst=/input,volume-subpath=workspaces/sandbox-inputs/task,readonly",
        "type=volume,src=meridian-data,dst=/output,volume-subpath=exports/sandbox/run-volume",
    ]

    with pytest.raises(ValueError, match="latest"):
        SandboxRunner(image="sandbox:latest", input_root=input_root, output_root=output_root)
    with pytest.raises(ValueError, match="volume"):
        SandboxRunner(
            image="sandbox:v1", input_root=input_root, output_root=output_root,
            docker_volume="bad/volume",
        )
    unavailable = SandboxRunner(image="sandbox:v1", input_root=input_root, output_root=output_root)
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    with pytest.raises(SandboxUnavailable):
        unavailable.execute({}, input_dir=input_dir, run_id="closed")


def test_sandbox_runner_propagates_real_cancellation(tmp_path, monkeypatch):
    from backend.services.data_plane import sandbox as module

    input_root = tmp_path / "inputs"
    output_root = tmp_path / "outputs"
    input_root.mkdir()
    output_root.mkdir()
    runner = SandboxRunner(image="sandbox:v1", input_root=input_root, output_root=output_root)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/local/bin/docker")
    monkeypatch.setattr(runner, "capability", lambda: {"available": True})

    class WaitingProcess:
        returncode = None
        killed = False

        def communicate(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired("docker", timeout)
            return "", ""

        def kill(self):
            self.killed = True

    process = WaitingProcess()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: Response({}, 0))
    with pytest.raises(InterruptedError, match="取消"):
        runner.execute({}, input_dir=input_root, run_id="cancelled", should_cancel=lambda: True)
    assert process.killed is True


def test_sandbox_client_authentication_execution_capability_and_failures(tmp_path, monkeypatch):
    input_root = tmp_path / "inputs"
    output_root = tmp_path / "outputs"
    input_dir = input_root / "task"
    input_dir.mkdir(parents=True)
    output_root.mkdir()
    token = "sandbox-token-that-is-definitely-long-enough"
    expected_image = "sandbox:v1"
    state = {"mode": "ok"}

    class Session:
        trust_env = True

        def post(self, _url, **kwargs):
            assert self.trust_env is False
            assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
            mode = state["mode"]
            if mode == "network":
                raise requests.ConnectionError("offline")
            if mode == "server":
                return Response({"error": "rejected"}, 400)
            run_id = kwargs["json"]["run_id"]
            (output_root / run_id).mkdir(exist_ok=True)
            image = "sandbox:wrong" if mode == "image" else expected_image
            return Response({"status": "SUCCEEDED", "image": image, "files": []})

        def get(self, _url, **_kwargs):
            if state["mode"] == "network":
                raise requests.ConnectionError("offline")
            return Response({"available": True, "host_fallback": False})

        def delete(self, _url, **_kwargs):
            if state["mode"] == "network":
                raise requests.ConnectionError("offline")
            return Response({"accepted": True}, 202)

        def close(self):
            return None

    monkeypatch.setattr("backend.services.data_plane.sandbox_client.requests.Session", Session)
    client = SandboxClient(
        endpoint="http://127.0.0.1:8090", token=token, input_root=input_root,
        output_root=output_root, timeout_seconds=5, expected_image=expected_image,
    )
    result = client.execute({"method": "describe"}, input_dir=input_dir, run_id="run-1")
    assert result["output_dir"] == str(output_root / "run-1")
    assert client.capability()["available"] is True
    client.cancel("run-1")

    state["mode"] = "image"
    with pytest.raises(RuntimeError, match="镜像版本"):
        client.execute({}, input_dir=input_dir, run_id="run-2")
    state["mode"] = "server"
    with pytest.raises(RuntimeError, match="rejected"):
        client.execute({}, input_dir=input_dir, run_id="run-3")
    state["mode"] = "network"
    with pytest.raises(SandboxUnavailable, match="不可用"):
        client.execute({}, input_dir=input_dir, run_id="run-4")
    assert client.capability()["available"] is False
    client.cancel("run-4")

    with pytest.raises(PermissionError, match="输入"):
        client.execute({}, input_dir=input_root, run_id="bad-path")
    with pytest.raises(SandboxUnavailable, match="URL"):
        SandboxClient(
            endpoint="", token=token, input_root=input_root, output_root=output_root,
            timeout_seconds=5, expected_image=expected_image,
        )
    with pytest.raises(SandboxUnavailable, match="HTTPS"):
        SandboxClient(
            endpoint="http://remote.example.test", token=token, input_root=input_root,
            output_root=output_root, timeout_seconds=5, expected_image=expected_image,
        )
    with pytest.raises(SandboxUnavailable, match="32"):
        SandboxClient(
            endpoint="http://localhost:8090", token="weak", input_root=input_root,
            output_root=output_root, timeout_seconds=5, expected_image=expected_image,
        )
