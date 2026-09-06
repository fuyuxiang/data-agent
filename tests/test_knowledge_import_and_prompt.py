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


def test_temp_prompt_strips_reasoning_toggles_and_controls_agent_injection(client, monkeypatch):
    session = client.post("/api/sessions", json={"name": "临时指令"}).get_json()["item"]
    saved = client.post(
        f"/api/session/{session['id']}/temp-prompt",
        json={"text": "<think>不应注入的思考</think>\n所有金额使用万元。", "raw": True},
    ).get_json()
    assert saved["enabled"] is True
    assert saved["temp_prompt"] == "所有金额使用万元。"

    completions = _PromptCompletions()
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(
        "backend.services.advanced_agent.resolve_provider",
        lambda _provider_id=None, _workspace_id="default": (
            {"model": "fake", "temperature": 0, "protocol": "chat_completions"}, fake,
        ),
    )
    created = client.post("/api/analyses", json={"session_id": session["id"], "objective": "汇报"}).get_json()["item"]
    confirmed = client.post(
        f"/api/analyses/{created['id']}/contract/confirm", json={"expected_version": 1},
    ).get_json()
    deadline = time.time() + 3
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{confirmed['job']['id']}").get_json()["item"]
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    system_prompt = completions.calls[-1]["messages"][0]["content"]
    assert "所有金额使用万元" in system_prompt
    assert "不应注入的思考" not in system_prompt

    disabled = client.post(f"/api/session/{session['id']}/temp-prompt/toggle").get_json()
    assert disabled["enabled"] is False
    created = client.post("/api/analyses", json={"session_id": session["id"], "objective": "再次汇报"}).get_json()["item"]
    confirmed = client.post(
        f"/api/analyses/{created['id']}/contract/confirm", json={"expected_version": 1},
    ).get_json()
    deadline = time.time() + 3
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{confirmed['job']['id']}").get_json()["item"]
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert "所有金额使用万元" not in completions.calls[-1]["messages"][0]["content"]

    cleared = client.post(
        f"/api/session/{session['id']}/temp-prompt", json={"text": "", "raw": True},
    ).get_json()
    assert cleared["temp_prompt"] == ""
    assert cleared["enabled"] is False
    assert client.post(f"/api/session/{session['id']}/temp-prompt/toggle").get_json()["warning"]
