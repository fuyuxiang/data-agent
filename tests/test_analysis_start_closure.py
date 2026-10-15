"""Preparation/confirmation regressions; no external model is simulated as evidence."""
from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend.agent.store import RunStore
from backend.agent.contracts import TaskContract
from backend.core.database import Database, utcnow
from backend.services.advanced_agent import _run_knowledge_ids


@pytest.fixture()
def submitted_jobs(monkeypatch):
    jobs = []

    def submit_spec(**spec):
        job = {"id": f"test-job-{len(jobs)}", "typed": True, **spec}
        jobs.append(job)
        return job

    monkeypatch.setattr(
        "backend.api.analyses.get_job_manager", lambda _app: SimpleNamespace(submit_spec=submit_spec),
    )
    return jobs


def test_simple_question_starts_once_after_preparation(client, source, submitted_jobs):
    run = client.post("/api/analyses", json={
        "objective": "本月销售额是多少？", "source_ids": [source["id"]], "attachment_count": 0,
    }).get_json()["item"]
    assert not submitted_jobs
    assert not run["contract"]["confirmed_at"]
    endpoint = f"/api/analyses/{run['id']}/start"
    stale = client.post(endpoint, json={"expected_version": 0})
    assert stale.status_code == 400
    assert not submitted_jobs
    started = client.post(endpoint, json={"expected_version": 1})
    assert started.status_code == 200, started.get_json()
    assert started.get_json()["auto_confirmed"] is True
    assert started.get_json()["item"]["contract"]["confirmed_at"]
    assert submitted_jobs[0]["job_type"] == "analysis_run"
    assert submitted_jobs[0]["spec"] == {"run_id": run["id"]}
    assert client.post(endpoint, json={"expected_version": 1}).get_json()["already_confirmed"]
    assert len(submitted_jobs) == 1
    refreshed = client.get(f"/api/analyses/{run['id']}").get_json()["item"]
    assert refreshed["contract"]["confirmed_by"] == "local-default"


@pytest.mark.parametrize("extra", [
    {"objective": "为什么华东销售下降？"},
    {"objective": "预测下个月销售额"},
    {"objective": "生成经营分析报告"},
    {"objective": "哪些商品表现异常？"},
    {"objective": "本月销售额是多少？", "execution_mode": "deep"},
    {"objective": "本月销售额是多少？", "confirm_required": True},
    {"objective": "本月销售额是多少？", "unresolved": ["确认币种"]},
])
def test_complex_or_unresolved_question_keeps_confirmation(client, source, submitted_jobs, extra):
    run = client.post("/api/analyses", json={
        "source_ids": [source["id"]], "attachment_count": 0, **extra,
    }).get_json()["item"]
    response = client.post(f"/api/analyses/{run['id']}/start", json={"expected_version": 1})
    assert response.status_code == 200
    assert response.get_json()["auto_confirmed"] is False
    assert not response.get_json()["item"]["contract"]["confirmed_at"]
    assert not submitted_jobs


def test_all_promised_attachments_must_be_bound_before_any_confirmation(client, submitted_jobs):
    run = client.post("/api/analyses", json={
        "objective": "汇总附件中的销售额", "source_ids": [], "attachment_count": 2, "auto_confirm": True,
    }).get_json()["item"]
    assert not run["contract"]["confirmed_at"]
    for index in range(2):
        blocked = client.post(f"/api/analyses/{run['id']}/start", json={"expected_version": 1})
        assert blocked.status_code == 400
        assert "附件尚未全部" in blocked.get_json()["error"]
        assert client.post(
            f"/api/analyses/{run['id']}/contract/confirm", json={"expected_version": 1},
        ).status_code == 400
        assert not submitted_jobs
        assert client.post(
            f"/api/analyses/{run['id']}/attachments",
            data={"files": (io.BytesIO(f"销售额：{index + 10} 元".encode()), f"sales-{index}.txt")},
            content_type="multipart/form-data",
        ).status_code == 201
    response = client.post(f"/api/analyses/{run['id']}/start", json={"expected_version": 1})
    assert response.status_code == 200
    assert response.get_json()["auto_confirmed"]
    assert len(submitted_jobs) == 1
    assert client.post(
        f"/api/analyses/{run['id']}/attachments",
        data={"files": (io.BytesIO(b"late"), "late.txt")}, content_type="multipart/form-data",
    ).status_code == 400


@pytest.mark.parametrize("count", [-1, 21, True, "1", 1.5])
def test_invalid_attachment_declaration_does_not_create_sessions(client, count):
    before = client.get("/api/sessions").get_json()["items"]
    assert client.post("/api/analyses", json={
        "objective": "读取文件", "attachment_count": count,
    }).status_code == 400
    assert client.get("/api/sessions").get_json()["items"] == before


def test_parent_must_belong_to_same_actor_and_session(app, client, source):
    parent = client.post("/api/analyses", json={
        "objective": "本月销售额", "source_ids": [source["id"]],
        "time_range": {"start": "2026-01-01", "end": "2026-02-01"},
    }).get_json()["item"]
    other_session = client.post("/api/sessions", json={"name": "另一个会话"}).get_json()["item"]
    assert client.post("/api/analyses", json={
        "objective": "继续分析", "session_id": other_session["id"], "parent_run_id": parent["id"],
    }).status_code == 400
    database = app.extensions["meridian_db"]
    private, _ = RunStore(database).create_run(
        workspace_id="default", session_id=parent["session_id"], actor_id="another-user",
        source_scope=[], allowed_tool_ids=[],
    )
    assert client.post("/api/analyses", json={
        "objective": "读取他人分析", "session_id": parent["session_id"], "parent_run_id": private["id"],
    }).status_code == 404
    child = client.post("/api/analyses", json={
        "objective": "哪个区域最高？", "session_id": parent["session_id"], "parent_run_id": parent["id"],
        "agent_id": "",
    }).get_json()["item"]
    assert child["source_scope"] == parent["source_scope"]
    assert child["contract"]["payload"]["time_range"] == parent["contract"]["payload"]["time_range"]
    assert child["contract"]["payload"]["objective"] == "哪个区域最高？"
    assert _run_knowledge_ids(database, child) == []


def test_duplicate_creation_keeps_one_run_and_one_question(client, submitted_jobs):
    session = client.post("/api/sessions", json={"name": "重复提交测试"}).get_json()["item"]
    payload = {
        "objective": "本月销售额是多少？", "session_id": session["id"],
        "attachment_count": 0, "idempotency_key": "same-question",
    }
    first = client.post("/api/analyses", json=payload).get_json()
    second = client.post("/api/analyses", json=payload).get_json()
    assert first["created"] is True
    assert second["created"] is False
    assert first["item"]["id"] == second["item"]["id"]
    assert len(client.get(f"/api/sessions/{session['id']}").get_json()["messages"]) == 1


@pytest.mark.parametrize("question,expected_range", [
    ("改看2026年2月的销售额", {"start": "2026-02-01", "end": "2026-02-28"}),
    ("改看2024年二月的销售额", {"start": "2024-02-01", "end": "2024-02-29"}),
    ("改看旺季的销售额", {}),
])
def test_changed_followup_period_never_confirms_the_old_month(client, source, submitted_jobs, question, expected_range):
    parent = client.post("/api/analyses", json={
        "objective": "查看2026年1月销售额", "source_ids": [source["id"]],
        "time_range": {"start": "2026-01-01", "end": "2026-01-31"},
        "coverage": "2026年1月全部销售记录", "dimensions": ["区域"], "metrics": ["销售额"],
    }).get_json()["item"]
    child = client.post("/api/analyses", json={
        "objective": question, "session_id": parent["session_id"], "parent_run_id": parent["id"],
        "source_ids": [source["id"]], "agent_id": "", "attachment_count": 0,
    }).get_json()["item"]
    assert child["contract"]["payload"]["time_range"] == expected_range
    assert "2026年1月" not in child["contract"]["payload"]["coverage"]
    started = client.post(f"/api/analyses/{child['id']}/start", json={"expected_version": 1}).get_json()
    assert started["auto_confirmed"] is False
    assert not submitted_jobs
    if not expected_range:
        denied = client.post(f"/api/analyses/{child['id']}/contract/confirm", json={"expected_version": 1})
        assert denied.status_code == 400
        assert "时间范围" in denied.get_json()["error"]
    contract = {**child["contract"]["payload"], "time_range": {"start": "2026-02-01", "end": "2026-02-28"}}
    confirmed = client.post(f"/api/analyses/{child['id']}/contract/confirm", json={
        "expected_version": 1, "contract": contract,
    })
    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["item"]["contract"]["payload"]["time_range"] == contract["time_range"]
    assert not confirmed.get_json()["item"]["contract"]["payload"]["unresolved"]
    assert len(submitted_jobs) == 1


def test_branch_followup_uses_the_new_period_too(client, submitted_jobs):
    parent = client.post("/api/analyses", json={
        "objective": "查看1月销售额", "time_range": {"start": "2026-01-01", "end": "2026-01-31"},
    }).get_json()["item"]
    response = client.post(f"/api/analyses/{parent['id']}/branch", json={
        "mode": "followup", "prompt": "改看2026年2月的销售额",
    })
    assert response.status_code == 201, response.get_json()
    child = response.get_json()["item"]
    assert child["contract"]["payload"]["time_range"] == {"start": "2026-02-01", "end": "2026-02-28"}
    assert client.post(f"/api/analyses/{child['id']}/start", json={"expected_version": 1}).get_json()["auto_confirmed"] is False
    assert not submitted_jobs


def test_old_analysis_context_survives_more_than_500_later_messages(app, client):
    from backend.services.advanced_agent import _analysis_history

    database = app.extensions["meridian_db"]
    parent = client.post("/api/analyses", json={
        "objective": "原分析：一月华东销售额", "coverage": "华东全部销售记录",
        "time_range": {"start": "2026-01-01", "end": "2026-01-31"},
    }).get_json()["item"]
    later = client.post("/api/analyses", json={
        "session_id": parent["session_id"], "objective": "后来无关的华西分析",
    }).get_json()["item"]
    for index in range(505):
        database.add_message(parent["session_id"], "assistant", f"后来无关的内容 {index}", {"run_id": later["id"]})
    child = client.post("/api/analyses", json={
        "session_id": parent["session_id"], "parent_run_id": parent["id"], "objective": "改看2026年2月的销售额",
    }).get_json()["item"]
    history = _analysis_history(database, child)
    assert [message["content"] for message in history if message["role"] == "user"] == [
        "原分析：一月华东销售额", "改看2026年2月的销售额",
    ]
    assert any("华东全部销售记录" in message["content"] for message in history if message["role"] == "system")
    assert all("后来无关" not in message["content"] for message in history)


def test_followup_retains_non_time_scope_when_model_context_is_compacted(app, client):
    from backend.agent.context import ContextBuilder
    from backend.services.advanced_agent import _analysis_history

    database = app.extensions["meridian_db"]
    parent = client.post("/api/analyses", json={
        "objective": "查看一月数据", "coverage": "华东已结算订单，排除退款",
        "time_range": {"start": "2026-01-01", "end": "2026-01-31"},
    }).get_json()["item"]
    for index in range(70):
        database.add_message(parent["session_id"], "assistant", f"分析过程 {index} " + "历史内容" * 200,
                             {"run_id": parent["id"]})
    child = client.post("/api/analyses", json={
        "session_id": parent["session_id"], "parent_run_id": parent["id"], "objective": "改看2026年2月的数据",
    }).get_json()["item"]
    messages = ContextBuilder(context_window=4096, max_output_tokens=512).build(
        system="本次契约优先", contract=child["contract"]["payload"], plan=None,
        history=_analysis_history(database, child), evidence_summary=[], skills=[], remaining_budget={},
    )
    assert any("华东已结算订单，排除退款" in message["content"] for message in messages)
    assert messages[-1]["content"] == "改看2026年2月的数据"


def test_queue_rejection_restores_editable_request_and_can_retry(client, source, submitted_jobs, monkeypatch):
    run = client.post("/api/analyses", json={
        "objective": "本月销售额是多少？", "source_ids": [source["id"]], "attachment_count": 0,
    }).get_json()["item"]
    from backend.api import analyses
    working_manager = analyses.get_job_manager

    def reject(**_spec):
        raise ValueError("后台任务队列已满，请稍后重试")

    monkeypatch.setattr(analyses, "get_job_manager", lambda _app: SimpleNamespace(submit_spec=reject))
    response = client.post(f"/api/analyses/{run['id']}/start", json={"expected_version": 1})
    assert response.status_code == 400
    restored = client.get(f"/api/analyses/{run['id']}").get_json()["item"]
    assert restored["execution_status"] == "waiting_input"
    assert not restored["contract"]["confirmed_at"]
    assert not submitted_jobs
    monkeypatch.setattr(analyses, "get_job_manager", working_manager)
    response = client.post(f"/api/analyses/{run['id']}/start", json={
        "expected_version": restored["contract"]["version"],
    })
    assert response.status_code == 200
    assert response.get_json()["auto_confirmed"]
    assert len(submitted_jobs) == 1


def test_concurrent_job_acceptance_prevents_confirmation_rollback(app, client):
    run = client.post("/api/analyses", json={"objective": "本月销售额"}).get_json()["item"]
    database = app.extensions["meridian_db"]
    store = RunStore(database)
    store.add_contract(
        run["id"], TaskContract.from_payload(run["contract"]["payload"]),
        expected_version=1, confirmed_by="local-default",
    )
    # A separate Database instance represents another request/process. Keep its
    # acceptance transaction open while the rollback tries to acquire its lock.
    accepting = threading.Event()
    release = threading.Event()
    rolling_back = threading.Event()

    def accept():
        other = Database(database.path)
        with other.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO typed_jobs(id,workspace_id,run_id,job_type,spec,spec_hash,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("accepted-concurrently", "default", run["id"], "analysis_run", "{}", "test-hash", "queued", utcnow(), utcnow()),
            )
            accepting.set()
            assert release.wait(5)

    def rollback():
        rolling_back.set()
        return store.rollback_unsubmitted_confirmation(run["id"], expected_version=2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = pool.submit(accept)
        assert accepting.wait(5)
        rolled_back = pool.submit(rollback)
        assert rolling_back.wait(5)
        release.set()
        accepted.result(timeout=5)
        assert rolled_back.result(timeout=5) is False
    refreshed = store.get_run(run["id"])
    assert refreshed["execution_status"] == "queued"
    assert refreshed["contract_version"] == 2
    assert store.latest_contract(run["id"])["confirmed_at"]
    assert not [event for event in store.events(run["id"]) if event["type"] == "analysis.start_failed"]


@pytest.mark.parametrize("status", ["paused", "running", "cancelling", "cancelled"])
def test_confirmation_rollback_preserves_concurrent_state_changes(app, client, status):
    run = client.post("/api/analyses", json={"objective": "本月销售额"}).get_json()["item"]
    store = RunStore(app.extensions["meridian_db"])
    store.add_contract(
        run["id"], TaskContract.from_payload(run["contract"]["payload"]),
        expected_version=1, confirmed_by="local-default",
    )
    store.update_status(run["id"], status)
    assert store.rollback_unsubmitted_confirmation(run["id"], expected_version=2) is False
    assert store.get_run(run["id"])["execution_status"] == status
    assert store.latest_contract(run["id"])["confirmed_at"]
