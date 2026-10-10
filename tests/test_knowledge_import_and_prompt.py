from __future__ import annotations

import io
import time
from types import SimpleNamespace

import pandas as pd


def _metric_workbook() -> bytes:
    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        pd.DataFrame([
            {
                "指标名称": "GMV", "别名": "商品交易总额", "定义": "已支付订单金额之和",
                "SQL模板": "SUM(paid_amount)", "备注": "不含取消订单",
            },
            {
                "指标名称": "AOV", "别名": "客单价", "定义": "GMV / 已支付订单数",
                "SQL模板": "SUM(paid_amount)/COUNT(*)", "备注": "",
            },
        ]).to_excel(writer, index=False, sheet_name="指标字典")
    return stream.getvalue()


def test_knowledge_file_preview_requires_confirmation_before_indexing(client):
    parsed = client.post(
        "/api/knowledge/parse",
        data={"workspace_id": "default", "file": (io.BytesIO(_metric_workbook()), "指标字典.xlsx")},
        content_type="multipart/form-data",
    )
    assert parsed.status_code == 200
    preview = parsed.get_json()
    assert preview["format"] == "structured"
    assert [item["name"] for item in preview["preview"]] == ["GMV", "AOV"]
    assert client.get("/api/knowledge/documents").get_json()["items"] == []
    assert client.get("/api/knowledge/metrics").get_json() == []

    confirmed = client.post(
        "/api/knowledge/confirm",
        json={"filename": preview["filename"], "records": preview["preview"], "category_id": "default"},
    )
    assert confirmed.status_code == 200
    result = confirmed.get_json()
    assert result["inserted"] == {"metrics": 2, "rules": 0, "notes": 0}
    assert result["rag"]["chunks"] > 0
    assert len(client.get("/api/knowledge/documents").get_json()["items"]) == 1

    metrics = client.get("/api/knowledge/metrics").get_json()
    assert {item["name"] for item in metrics} == {"GMV", "AOV"}
    assert next(item for item in metrics if item["name"] == "GMV")["sql_template"] == "SUM(paid_amount)"
    search = client.get("/api/knowledge/search?q=GMV").get_json()
    assert search["metrics"]

    gm = next(item for item in metrics if item["name"] == "GMV")
    toggled = client.post(f"/api/knowledge/metrics/{gm['id']}/toggle").get_json()
    assert toggled["enabled"] is False
    updated = client.put(
        f"/api/knowledge/metrics/{gm['id']}", json={"definition": "最终支付商品金额"},
    ).get_json()
    assert updated["definition"] == "最终支付商品金额"

    files = client.get("/api/knowledge/files").get_json()
    assert files[0]["filename"] == preview["filename"]
    assert client.delete(f"/api/knowledge/files/{preview['filename']}").status_code == 200
    assert client.get("/api/knowledge/documents").get_json()["items"] == []


class _PromptCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        delta = SimpleNamespace(content="已完成", tool_calls=[])
        return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)])


def test_agent_instruction_strips_reasoning_and_reaches_the_system_prompt(client, source, monkeypatch):
    """An operator instruction reaches the model, its private reasoning does not.

    The V1 session-level temporary instruction is gone; the same guarantee now
    belongs to the Agent role instruction, which is the only operator-authored
    text that reaches the system prompt.
    """
    agent = client.post("/api/agents", json={
        "name": "万元口径智能体",
        "source_ids": [source["id"]],
        "instruction": "<think>不应注入的思考</think>\n所有金额使用万元。",
    }).get_json()["item"]
    assert "不应注入的思考" not in agent["instruction"]
    assert agent["instruction"] == "所有金额使用万元。"
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200

    completions = _PromptCompletions()
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(
        "backend.services.advanced_agent.resolve_provider",
        lambda _provider_id=None, _workspace_id="default": (
            {"model": "fake", "temperature": 0, "protocol": "chat_completions"}, fake,
        ),
    )
    created = client.post("/api/analyses", json={
        "session_id": "welcome", "objective": "汇报", "agent_id": agent["id"],
        "source_ids": [source["id"]],
    }).get_json()["item"]
    _confirm_and_wait(client, created["id"])
    system_prompt = completions.calls[-1]["messages"][0]["content"]
    assert "所有金额使用万元" in system_prompt
    assert "不应注入的思考" not in system_prompt

    # An unpublished edit must not interrupt service or alter the live prompt.
    client.patch(f"/api/agents/{agent['id']}", json={"instruction": "所有金额使用元。"})
    live = client.post("/api/analyses", json={
        "session_id": "welcome", "objective": "再次汇报", "agent_id": agent["id"],
        "source_ids": [source["id"]],
    })
    assert live.status_code == 201, live.get_json()
    _confirm_and_wait(client, live.get_json()["item"]["id"])
    live_prompt = completions.calls[-1]["messages"][0]["content"]
    assert "所有金额使用万元。" in live_prompt
    assert "所有金额使用元。" not in live_prompt


def _wait_for_job(client, job_id: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["item"]
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError("分析任务未在预期时间内结束")


def _confirm_and_wait(client, run_id: str) -> dict:
    confirmed = client.post(
        f"/api/analyses/{run_id}/contract/confirm", json={"expected_version": 1},
    ).get_json()
    return _wait_for_job(client, confirmed["job"]["id"])
