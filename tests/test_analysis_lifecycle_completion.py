from __future__ import annotations

from dataclasses import asdict
import json
import threading
import time

import pytest

from backend.agent.contracts import ModelResponse, ModelToolCall, TaskContract, ToolSpec
from backend.agent.loop import AgentLoop
from backend.agent.store import RunStore
from backend.agent.tools import ToolExecutor, ToolRegistry
from backend.core.database import utcnow
from backend.services import advanced_agent, jobs
from backend.services.data_plane import factory
from backend.services.data_plane.livy import LivyBatchAdapter, LivyConfig
from backend.services.data_plane.trino import TrinoAdapter, TrinoConfig
from backend.services.results.manifests import ResultService


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("analysis lifecycle did not settle")


def confirmed_run(app, client, allowed=()):
    response = client.post("/api/analyses", json={"objective": "核验分析生命周期"})
    assert response.status_code == 201
    run = response.get_json()["item"]
    store = RunStore(app.extensions["meridian_db"])
    store.add_contract(
        run["id"], TaskContract.from_payload(run["contract"]["payload"]),
        expected_version=1, confirmed_by="local-default",
    )
    with store.db.transaction() as connection:
        connection.execute("UPDATE agent_runs SET allowed_tool_ids=? WHERE id=?", (json.dumps(list(allowed)), run["id"]))
    return store, store.get_run(run["id"])


class Response:
    def __init__(self, payload=None, status=200):
        self.payload = payload or {}
        self.status_code = status

    def json(self):
        return self.payload


class Model:
    def __init__(self, calls=()):
        self.calls = calls

    def complete(self, *_args, **_kwargs):
        return ModelResponse(
            "fixture", "fixture", "已验证的结论。" if not self.calls else "",
            self.calls, "tool_calls" if self.calls else "stop", None, {"total_tokens": 1},
        )


def publish_fixture(store, run_id, answer="已验证的结论。"):
    run = store.get_run(run_id)
    contract = store.latest_contract(run_id)
    manifest_id = store.db.new_id("manifest")
    with store.db.transaction() as connection:
        connection.execute(
            "INSERT INTO result_manifests(id,workspace_id,run_id,version,status,payload,created_at) VALUES(?,?,?,?,?,?,?)",
            (manifest_id, run["workspace_id"], run_id, 1, "validated_draft",
             json.dumps({"summary": answer, "contract": contract["payload"], "tables": [], "charts": []}), utcnow()),
        )
    publication = ResultService(store.db).publish(
        run, contract, {"id": manifest_id}, {"quality_score": 1, "coverage": 1},
    )
    return {"published": True, "publication_id": publication["id"], "manifest_id": manifest_id}


@pytest.mark.parametrize("kind", ["livy", "trino"])
def test_remote_analysis_cancel_waits_for_actual_confirmation(app, client, monkeypatch, kind):
    tool = "warehouse_spark_submit" if kind == "livy" else "warehouse_query"
    store, run = confirmed_run(app, client, allowed=(tool,))
    database = store.db
    confirmed = threading.Event()
    calls = []
    if kind == "livy":
        adapter = LivyBatchAdapter(database, "default", LivyConfig(
            engine_id="cancel-livy", endpoint="https://livy.example.test", job_file="local:/job.py",
            proxy_user="test", queue="test", result_prefix="s3a://results/",
            input_prefixes=("s3a://inputs/",),
        ))
        collection, remote_id = "remote_batches", "cancel-livy:7"
        def submit(_args):
            return adapter.submit({
                "method": "grouped_trend_anomaly", "input_refs": [{"uri": "s3a://inputs/orders/"}],
            }, run_id=run["id"], action_id="remote-action")

        def request(method, url, **_kwargs):
            calls.append((method, url))
            if method == "POST":
                return Response({"id": 7, "state": "running"}, 201)
            if method == "DELETE":
                return Response({"msg": "cancel accepted"}, 202)
            return Response({"id": 7, "state": "dead" if confirmed.is_set() else "running"})

        monkeypatch.setattr(factory, "livy_adapter", lambda *_args, **_kwargs: adapter)
    else:
        adapter = TrinoAdapter(database, "default", TrinoConfig(
            engine_id="cancel-trino", endpoint="https://trino.example.test", user="test",
            catalog="catalog", schema="schema",
        ))
        collection, remote_id = "warehouse_queries", "cancel-query"

        def submit(_args):
            result = adapter.submit("SELECT 1", run_id=run["id"], action_id="remote-action")
            return {**result, "job_id": result["query_id"]}

        def request(method, url, **_kwargs):
            calls.append((method, url))
            if method == "DELETE":
                return Response(status=202)
            payload = {"id": remote_id}
            if method == "POST" or not confirmed.is_set():
                payload["nextUri"] = "https://trino.example.test/next"
            return Response(payload)

        monkeypatch.setattr(factory, "trino_adapter", lambda *_args, **_kwargs: adapter)
    monkeypatch.setattr(adapter, "_request", request)
    registry = ToolRegistry()
    registry.register(ToolSpec(id=tool, description="remote fixture", input_schema={"type": "object"}), submit)

    def handler(_app, spec, _progress, cancel):
        return asdict(AgentLoop(
            store=store, model=Model((ModelToolCall("remote", tool, {}),)),
            tools=ToolExecutor(store, registry), finalizer=lambda *_args: {},
        ).run(spec["run_id"], runner_id="remote-test", history=[], should_cancel=cancel.is_set))

    monkeypatch.setitem(jobs._HANDLERS, "remote_cancel_fixture", handler)
    manager = jobs.JobManager(app, max_workers=1)
    app.extensions["meridian_jobs"] = manager
    try:
        job = manager.submit_spec(
            workspace_id="default", session_id=run["session_id"], run_id=run["id"],
            job_type="remote_cancel_fixture", title="remote cancellation", spec={"run_id": run["id"]},
        )
        wait_for(lambda: database.get("jobs", job["id"])["status"] == "completed")
        assert store.get_run(run["id"])["execution_status"] == "waiting_job"
        response = client.post(f"/api/analyses/{run['id']}/control", json={"action": "cancel"})
        assert response.status_code == 200
        wait_for(lambda: database.get(collection, remote_id).get("cancel_dispatched_at"))
        assert store.get_run(run["id"])["execution_status"] == "cancelling"
        assert any(method == "DELETE" for method, _url in calls)
        confirmed.set()
        wait_for(lambda: store.get_run(run["id"])["execution_status"] == "cancelled")
        assert store.actions(run["id"])[0]["status"] == "cancelled"
        assert sum(method == "POST" for method, _url in calls) == 1
        assert ResultService(database).publication(run["id"], workspace_id="default") is None
    finally:
        confirmed.set()
        manager.shutdown()
        if manager._reconcile_thread:
            manager._reconcile_thread.join(3)


def test_remote_cancel_failure_is_visible_and_recovered_after_restart(app, client, monkeypatch):
    store, run = confirmed_run(app, client)
    adapter = LivyBatchAdapter(store.db, "default", LivyConfig(
        engine_id="recover-livy", endpoint="https://livy.example.test", job_file="local:/job.py",
        proxy_user="test", queue="test", result_prefix="s3a://results/", input_prefixes=("s3a://inputs/",),
    ))
    failing = threading.Event()
    failing.set()
    requests = []

    def request(method, path, **_kwargs):
        requests.append(method)
        if method == "POST":
            return Response({"id": 9, "state": "running"}, 201)
        if method == "DELETE" and failing.is_set():
            raise ConnectionError("engine temporarily unavailable")
        if method == "DELETE":
            return Response(status=204)
        return Response({"id": 9, "state": "dead"})

    monkeypatch.setattr(adapter, "_request", request)
    monkeypatch.setattr(factory, "livy_adapter", lambda *_args, **_kwargs: adapter)
    adapter.submit({"method": "grouped_trend_anomaly", "input_refs": [{"uri": "s3a://inputs/a/"}]}, run_id=run["id"], action_id="action")
    store.update_status(run["id"], "waiting_job")
    manager = jobs.JobManager(app)
    app.extensions["meridian_jobs"] = manager
    try:
        assert client.post(f"/api/analyses/{run['id']}/control", json={"action": "cancel"}).status_code == 200
        wait_for(lambda: store.get_run(run["id"])["stop_reason"] == "cancel_retry_pending")
        detail = client.get(f"/api/analyses/{run['id']}").get_json()["item"]
        assert detail["execution_status"] == "cancelling"
        assert "temporarily unavailable" in detail["cancel_errors"][0]["message"]
    finally:
        manager.shutdown()
        if manager._reconcile_thread:
            manager._reconcile_thread.join(3)
    failing.clear()
    replacement = jobs.JobManager(app)
    app.extensions["meridian_jobs"] = replacement
    try:
        wait_for(lambda: store.get_run(run["id"])["execution_status"] == "cancelled")
        assert requests.count("POST") == 1
        assert requests.count("DELETE") >= 2
        assert store.db.get("remote_batches", "recover-livy:9")["cancel_dispatched_at"]
    finally:
        replacement.shutdown()
        if replacement._reconcile_thread:
            replacement._reconcile_thread.join(3)


def test_cancellation_wins_over_model_error_in_actual_agent_loop(app, client, monkeypatch):
    store, run = confirmed_run(app, client)
    started, release = threading.Event(), threading.Event()

    class FailingModel:
        def complete(self, *_args, **_kwargs):
            started.set()
            assert release.wait(5)
            raise ConnectionError("model stream disconnected")

    def handler(_app, spec, _progress, cancel):
        return asdict(AgentLoop(
            store=store, model=FailingModel(), tools=ToolExecutor(store, ToolRegistry()),
            finalizer=lambda *_args: {},
        ).run(spec["run_id"], runner_id="model-failure", history=[], should_cancel=cancel.is_set))

    monkeypatch.setitem(jobs._HANDLERS, "model_cancel_fixture", handler)
    manager = jobs.JobManager(app)
    app.extensions["meridian_jobs"] = manager
    try:
        job = manager.submit_spec(workspace_id="default", session_id=run["session_id"], run_id=run["id"],
                                  job_type="model_cancel_fixture", title="cancel", spec={"run_id": run["id"]})
        assert started.wait(5)
        response = client.post(f"/api/analyses/{run['id']}/control", json={"action": "cancel"})
        assert response.status_code == 200
        assert response.get_json()["item"]["execution_status"] == "cancelling"
        release.set()
        wait_for(lambda: store.get_run(run["id"])["execution_status"] == "cancelled")
        assert store.get_run(run["id"])["outcome"] == "cancelled"
        assert store.db.get("jobs", job["id"])["status"] == "cancelled"
    finally:
        release.set()
        manager.shutdown()


@pytest.mark.parametrize("status", ["finished", "failed", "cancelled"])
def test_terminal_cancel_is_idempotent_and_cannot_restart(app, client, status):
    store, run = confirmed_run(app, client)
    finished = store.update_status(run["id"], status, outcome="complete" if status == "finished" else status)
    response = client.post(f"/api/analyses/{run['id']}/control", json={"action": "cancel"})
    assert response.status_code == 200
    assert response.get_json()["idempotent"] is True
    assert store.get_run(run["id"]) == finished
    assert store.update_status(run["id"], "queued") == finished
    with pytest.raises(ValueError, match="不可修改契约"):
        store.add_contract(run["id"], TaskContract.from_payload(store.latest_contract(run["id"])["payload"]), expected_version=2)
    app.extensions["meridian_jobs"].shutdown()


def test_cancellation_fences_publication_and_external_reconciliation(app, client):
    store, run = confirmed_run(app, client, allowed=("warehouse_query",))
    context = store.acquire_lease(run["id"], "cancel-publication")
    response = ModelResponse("fixture", "fixture", "", (), "stop", None, {})
    decision = store.record_decision(run["id"], response)
    action, attempt = store.begin_action(run["id"], decision["id"], "external", "warehouse_query", {}, lease_epoch=context.lease_epoch)
    store.finish_action(run["id"], action["id"], attempt["id"], attempt["reservation_id"],
                        lease_epoch=context.lease_epoch, status="accepted", result={}, external_job_id="remote-id")
    store.update_status(run["id"], "cancelling", stop_reason="cancel_requested")
    store.complete_external_action(run["id"], "remote-id", status="succeeded", result={})
    assert store.get_run(run["id"])["execution_status"] == "cancelling"
    with pytest.raises(InterruptedError):
        store.begin_action(run["id"], decision["id"], "late-action", "warehouse_query", {}, lease_epoch=context.lease_epoch)
    with pytest.raises(InterruptedError):
        publish_fixture(store, run["id"])
    assert ResultService(store.db).publication(run["id"], workspace_id="default") is None


def test_archive_hides_late_worker_answer_preserves_exports_and_restores(app, client, monkeypatch):
    store, run = confirmed_run(app, client)
    database = store.db
    writing, release = threading.Event(), threading.Event()
    original_add = database.add_message

    def delayed_message(session_id, role, content, metadata=None):
        if role == "assistant" and (metadata or {}).get("run_id") == run["id"]:
            writing.set()
            assert release.wait(5)
        return original_add(session_id, role, content, metadata)

    monkeypatch.setattr(advanced_agent, "resolve_provider", lambda *_args: ({"model": "fixture"}, object()))
    monkeypatch.setattr(advanced_agent, "build_model_adapter", lambda *_args: Model())
    monkeypatch.setattr(advanced_agent, "build_executor", lambda *_args: ToolExecutor(store, ToolRegistry()))
    monkeypatch.setattr(advanced_agent, "_prepare_run_skills", lambda *_args: [])
    monkeypatch.setattr(ResultService, "finalize", lambda _self, run_id, answer, _evidence: publish_fixture(store, run_id, answer))
    monkeypatch.setattr(database, "add_message", delayed_message)
    manager = jobs.JobManager(app)
    app.extensions["meridian_jobs"] = manager
    try:
        job = manager.submit_spec(workspace_id="default", session_id=run["session_id"], run_id=run["id"],
                                  job_type="analysis_run", title="late answer", spec={"run_id": run["id"]})
        assert writing.wait(5)
        assert database.get("jobs", job["id"])["status"] == "running"
        path = app.config["SETTINGS"].export_dir / "retained.txt"
        path.write_text("retained artifact", encoding="utf-8")
        artifact = database.put("artifacts", {
            "id": "retained-artifact", "workspace_id": "default", "run_id": run["id"],
            "path": str(path), "filename": path.name, "source_ids": [], "kind": "report_html", "status": "ready",
        }, workspace_id="default")
        assert client.delete(f"/api/analyses/{run['id']}").status_code == 200
        assert client.get(f"/api/sessions/{run['session_id']}").get_json()["messages"] == []
        # Restoring while the worker still has its answer pending must not
        # synthesize a second copy from the published manifest.
        assert client.post(f"/api/analyses/{run['id']}/restore").status_code == 200
        assert len(database.messages(run["session_id"])) == 1
        release.set()
        wait_for(lambda: database.get("jobs", job["id"])["status"] == "completed")
        assert len(database.messages(run["session_id"])) == 2
        assert client.delete(f"/api/analyses/{run['id']}").status_code == 200
        assert client.get(f"/api/analyses/{run['id']}").status_code == 404
        assert client.get(f"/api/sessions/{run['session_id']}").get_json()["messages"] == []
        assert len(database.messages(run["session_id"], include_archived_runs=True)) == 2
        assert client.get(f"/api/library/{artifact['id']}/download").status_code == 200
        assert client.get(f"/api/artifacts/{artifact['id']}/download").status_code == 200
        assert artifact["id"] in {item["id"] for item in client.get("/api/library").get_json()["items"]}
        assert client.get(f"/api/admin/runs/{run['id']}").status_code == 200
        assert run["id"] in {item["id"] for item in client.get("/api/admin/runs").get_json()["items"]}
        restored = client.post(f"/api/analyses/{run['id']}/restore")
        assert restored.status_code == 200 and restored.get_json()["restored"] is True
        messages = client.get(f"/api/sessions/{run['session_id']}").get_json()["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert store.get_run(run["id"])["execution_status"] == "finished"
    finally:
        release.set()
        manager.shutdown()


def test_restore_legacy_missing_messages_and_archived_personal_session(app, client):
    store, run = confirmed_run(app, client)
    publish_fixture(store, run["id"], "可恢复的已发布回答。")
    store.archive_run(run["id"], workspace_id="default", session_id=run["session_id"])
    store.db.remove_messages_for_run(run["session_id"], run["id"])
    store.db.archive("sessions", run["session_id"], workspace_id="default")
    response = client.post(f"/api/analyses/{run['id']}/restore")
    assert response.status_code == 200
    assert store.db.get("sessions", run["session_id"])
    messages = store.db.messages(run["session_id"])
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "核验分析生命周期"), ("assistant", "可恢复的已发布回答。"),
    ]
    assert client.post(f"/api/analyses/{run['id']}/restore").get_json()["idempotent"] is True
    assert len(store.db.messages(run["session_id"])) == 2


def test_restore_analysis_in_legacy_unowned_welcome_session(app, client):
    response = client.post("/api/analyses", json={"objective": "恢复历史欢迎会话分析", "session_id": "welcome"})
    assert response.status_code == 201
    run = response.get_json()["item"]
    store = RunStore(app.extensions["meridian_db"])
    # Current creation assigns an owner; simulate the pre-migration seed/run.
    store.db.patch("sessions", "welcome", {"owner_id": None}, workspace_id="default")
    assert not store.db.get("sessions", "welcome").get("owner_id")
    store.update_status(run["id"], "cancelled", outcome="cancelled")
    store.archive_run(run["id"], workspace_id="default", session_id="welcome")
    store.db.archive("sessions", "welcome", workspace_id="default")
    restored = client.post(f"/api/analyses/{run['id']}/restore")
    assert restored.status_code == 200 and restored.get_json()["restored"]
    assert client.get("/api/sessions/welcome").status_code == 200
    assert store.get_run(run["id"])["execution_status"] == "cancelled"


@pytest.mark.parametrize("session_archived", [False, True])
def test_restore_shared_session_analysis_cannot_unarchive_foreign_session(app, client, session_archived):
    store, run = confirmed_run(app, client)
    store.db.patch("sessions", run["session_id"], {"owner_id": "foreign-actor", "visibility": "workspace"})
    store.update_status(run["id"], "cancelled", outcome="cancelled")
    store.archive_run(run["id"], workspace_id="default", session_id=run["session_id"])
    if session_archived:
        store.db.archive("sessions", run["session_id"], workspace_id="default")
    restored = client.post(f"/api/analyses/{run['id']}/restore")
    assert restored.status_code == (403 if session_archived else 200)
    assert bool(store.get_run(run["id"])) is not session_archived
    assert bool(store.db.get("sessions", run["session_id"])) is not session_archived
    if session_archived:
        assert "会话所有者先恢复" in restored.get_json()["error"]


def test_archiving_does_not_hide_independent_saved_session_copy(app, client):
    store, run = confirmed_run(app, client)
    store.update_status(run["id"], "cancelled", outcome="cancelled")
    copy = store.db.put("sessions", {"id": "independent-copy", "owner_id": "local-default"}, workspace_id="default")
    store.db.replace_messages(copy["id"], store.db.messages(run["session_id"]))
    store.archive_run(run["id"], workspace_id="default", session_id=run["session_id"])
    assert store.db.messages(run["session_id"]) == []
    assert len(store.db.messages(copy["id"])) == 1


def test_archive_list_and_restore_are_personal_and_workspace_scoped(app, client):
    store, run = confirmed_run(app, client)
    store.update_status(run["id"], "cancelled", outcome="cancelled")
    store.archive_run(run["id"], workspace_id="default", session_id=run["session_id"])
    other, _ = store.create_run(workspace_id="default", session_id="foreign-session", actor_id="foreign-actor", source_scope=[], allowed_tool_ids=[])
    store.update_status(other["id"], "cancelled", outcome="cancelled")
    store.archive_run(other["id"], workspace_id="default", session_id=other["session_id"])
    assert client.get("/api/analyses").get_json()["items"] == []
    assert [item["id"] for item in client.get("/api/analyses?include_archived=true").get_json()["items"]] == [run["id"]]
    assert client.post(f"/api/analyses/{other['id']}/restore").status_code == 404
    store.db.patch("sessions", run["session_id"], {"owner_id": "foreign-actor"}, workspace_id="default")
    assert client.post(f"/api/analyses/{run['id']}/restore").status_code == 403
    assert store.get_run(run["id"]) is None


@pytest.mark.parametrize("kind", ["livy", "trino"])
def test_disabled_archived_engine_remains_available_only_for_cancellation(app, kind, monkeypatch):
    monkeypatch.setattr("backend.services.security.socket.getaddrinfo", lambda *_args: [(2, 1, 6, "", ("8.8.8.8", 443))])
    database = app.extensions["meridian_db"]
    engine = {
        "id": f"disabled-{kind}", "type": kind, "enabled": False, "endpoint": f"https://{kind}.example.test",
        "catalog": "catalog", "schema": "schema", "job_file": "local:/job.py",
        "result_prefix": "s3a://results/", "input_prefixes": ["s3a://inputs/"],
    }
    database.put("warehouse_engines", engine, workspace_id="default")
    database.archive("warehouse_engines", engine["id"], workspace_id="default")
    constructor = factory.livy_adapter if kind == "livy" else factory.trino_adapter
    with app.app_context():
        with pytest.raises(FileNotFoundError):
            constructor(database, "default", engine["id"])
        assert constructor(database, "default", engine["id"], for_cancellation=True).config.engine_id == engine["id"]


def test_read_only_member_cannot_archive_or_restore_personal_analysis(app, client, monkeypatch):
    store, run = confirmed_run(app, client)
    store.update_status(run["id"], "cancelled", outcome="cancelled")
    # Source authorization remains unchanged so this verifies the explicit
    # mutation role boundary rather than incidental policy-fingerprint denial.
    monkeypatch.setattr("backend.api.analyses.actor_role", lambda *_args: "viewer")
    assert client.delete(f"/api/analyses/{run['id']}").status_code == 403
    store.archive_run(run["id"], workspace_id="default", session_id=run["session_id"])
    assert client.post(f"/api/analyses/{run['id']}/restore").status_code == 403


def test_completed_publication_wins_over_late_cancel(app, client):
    store, run = confirmed_run(app, client)
    publication = publish_fixture(store, run["id"])
    completed = store.get_run(run["id"])
    response = client.post(f"/api/analyses/{run['id']}/control", json={"action": "cancel"})
    assert response.get_json()["idempotent"] is True
    assert store.get_run(run["id"]) == completed
    assert completed["execution_status"] == "finished"
    assert ResultService(store.db).publication(run["id"], workspace_id="default")["id"] == publication["publication_id"]
    app.extensions["meridian_jobs"].shutdown()


def test_cancelled_analysis_cannot_acquire_new_job(app, client):
    store, run = confirmed_run(app, client)
    store.update_status(run["id"], "cancelled", outcome="cancelled")
    manager = jobs.JobManager(app)
    try:
        with pytest.raises(ValueError, match="不在待执行状态"):
            manager.submit_spec(workspace_id="default", session_id=run["session_id"], run_id=run["id"],
                                job_type="analysis_run", title="late enqueue", spec={"run_id": run["id"]})
        assert manager._outstanding == 0
        with store.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM typed_jobs WHERE run_id=?", (run["id"],)).fetchone()[0] == 0
    finally:
        manager.shutdown()


def test_queued_cancel_does_not_wait_for_unrelated_worker_or_start_handler(app, client, monkeypatch):
    store, run = confirmed_run(app, client)
    started, release, invoked = threading.Event(), threading.Event(), threading.Event()

    def handler(_app, spec, _progress, _cancel):
        if spec.get("block"):
            started.set()
            assert release.wait(5)
        else:
            invoked.set()
        return {"ok": True}

    monkeypatch.setitem(jobs._HANDLERS, "queued_cancel_fixture", handler)
    manager = jobs.JobManager(app, max_workers=1, max_pending=1)
    app.extensions["meridian_jobs"] = manager
    try:
        first = manager.submit_spec(workspace_id="default", session_id=None, job_type="queued_cancel_fixture",
                                    title="unrelated worker", spec={"block": True})
        assert started.wait(5)
        queued = manager.submit_spec(workspace_id="default", session_id=run["session_id"], run_id=run["id"],
                                     job_type="queued_cancel_fixture", title="queued", spec={})
        response = client.post(f"/api/analyses/{run['id']}/control", json={"action": "cancel"})
        assert response.status_code == 200
        assert response.get_json()["item"]["execution_status"] == "cancelled"
        assert store.db.get("jobs", queued["id"])["status"] == "cancelled"
        assert manager._outstanding == 1
        release.set()
        wait_for(lambda: store.db.get("jobs", first["id"])["status"] == "completed")
        assert not invoked.is_set()
    finally:
        release.set()
        manager.shutdown()


def test_concurrent_queued_cancel_releases_capacity_once(app, client, monkeypatch):
    store, run = confirmed_run(app, client)
    started, release, contender_done = threading.Event(), threading.Event(), threading.Event()
    barrier, finish_lock = threading.Barrier(2), threading.Lock()
    finish_calls, errors = [], []

    def handler(_app, spec, _progress, _cancel):
        assert spec.get("block"), "cancelled queued handler must never execute"
        started.set()
        assert release.wait(5)
        return {"ok": True}

    monkeypatch.setitem(jobs._HANDLERS, "concurrent_queued_cancel_fixture", handler)
    manager = jobs.JobManager(app, max_workers=1, max_pending=1)
    try:
        first = manager.submit_spec(workspace_id="default", session_id=None, job_type="concurrent_queued_cancel_fixture",
                                    title="unrelated worker", spec={"block": True})
        assert started.wait(5)
        queued = manager.submit_spec(workspace_id="default", session_id=run["session_id"], run_id=run["id"],
                                     job_type="concurrent_queued_cancel_fixture", title="queued", spec={})
        original_finish = manager._finish_typed

        def finish(job_id, *args, **kwargs):
            if job_id == queued["id"]:
                with finish_lock:
                    finish_calls.append(job_id)
                    first_call = len(finish_calls) == 1
                if first_call:
                    assert contender_done.wait(5)
                else:
                    contender_done.set()
            return original_finish(job_id, *args, **kwargs)

        monkeypatch.setattr(manager, "_finish_typed", finish)

        def cancel():
            try:
                barrier.wait(3)
                assert manager.cancel(queued["id"], reconcile=False)
            except Exception as exc:
                errors.append(exc)
            finally:
                contender_done.set()

        callers = [threading.Thread(target=cancel) for _ in range(2)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(5)
        assert not errors and all(not caller.is_alive() for caller in callers)
        assert finish_calls == [queued["id"]]
        assert store.db.get("jobs", queued["id"])["status"] == "cancelled"
        assert manager._outstanding == 1
        assert manager._workspace_outstanding == {"default": 1}
        assert store.db.get("jobs", first["id"])["status"] == "running"
        release.set()
        wait_for(lambda: store.db.get("jobs", first["id"])["status"] == "completed")
        wait_for(lambda: manager._outstanding == 0)
    finally:
        release.set()
        contender_done.set()
        manager.shutdown()


def test_missing_external_record_does_not_falsely_confirm_cancel(app, client):
    store, run = confirmed_run(app, client, allowed=("warehouse_query",))
    context = store.acquire_lease(run["id"], "missing-remote")
    decision = store.record_decision(run["id"], Model().complete())
    action, attempt = store.begin_action(run["id"], decision["id"], "missing", "warehouse_query", {}, lease_epoch=context.lease_epoch)
    store.finish_action(run["id"], action["id"], attempt["id"], attempt["reservation_id"],
                        lease_epoch=context.lease_epoch, status="accepted", result={}, external_job_id="lost-remote")
    store.update_status(run["id"], "waiting_job")
    try:
        response = client.post(f"/api/analyses/{run['id']}/control", json={"action": "cancel"})
        assert response.status_code == 200
        assert response.get_json()["item"]["execution_status"] == "cancelling"
        assert response.get_json()["item"]["cancel_errors"][0]["job_id"] == "lost-remote"
        assert client.delete(f"/api/analyses/{run['id']}").status_code == 400
    finally:
        app.extensions["meridian_jobs"].shutdown()


def test_concurrent_publication_and_cancel_have_one_terminal_winner(app, client):
    store, run = confirmed_run(app, client)
    contract = store.latest_contract(run["id"])
    manifest_id = store.db.new_id("manifest")
    with store.db.transaction() as connection:
        connection.execute(
            "INSERT INTO result_manifests(id,workspace_id,run_id,version,status,payload,created_at) VALUES(?,?,?,?,?,?,?)",
            (manifest_id, "default", run["id"], 1, "validated_draft", "{}", utcnow()),
        )
    manager = jobs.JobManager(app)
    barrier = threading.Barrier(2)
    result = {}

    def publish():
        barrier.wait(3)
        try:
            result["publication"] = ResultService(store.db).publish(
                run, contract, {"id": manifest_id}, {"quality_score": 1, "coverage": 1},
            )
        except InterruptedError:
            result["publication"] = None

    def cancel():
        barrier.wait(3)
        result["cancel"] = manager.cancel_run(run["id"])

    publisher, canceller = threading.Thread(target=publish), threading.Thread(target=cancel)
    try:
        publisher.start()
        canceller.start()
        publisher.join(5)
        canceller.join(5)
        assert not publisher.is_alive() and not canceller.is_alive()
        assert "cancel" in result and "publication" in result
        final = store.get_run(run["id"])
        if result["publication"]:
            assert final["execution_status"] == "finished" and final["outcome"] == "complete"
        else:
            assert final["execution_status"] == "cancelled" and final["outcome"] == "cancelled"
            assert ResultService(store.db).publication(run["id"], workspace_id="default") is None
    finally:
        manager.shutdown()
