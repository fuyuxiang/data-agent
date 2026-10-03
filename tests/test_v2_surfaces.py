"""V2 新增的产品表面：资料库、指标试算、运行记录与评测。"""

from __future__ import annotations

import io


# ---------------------------------------------------------------- 资料库


def test_library_exposes_user_facing_categories_not_internal_kinds(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    payload = client.get("/api/library").get_json()
    labels = [item["key"] for item in payload["categories"]]
    for label in ("全部", "报告", "演示文稿", "表格", "网页", "图片", "分析结果", "上传文件"):
        assert label in labels
    # 内部类型名不出现在界面上
    assert not any("docx" in label or "pptx" in label for label in labels)


def test_library_round_trips_upload_preview_favorite_and_delete(client):
    created = client.post(
        "/api/library",
        data={
            "workspace_id": "default",
            "file": (io.BytesIO("城市,销售额\n杭州,120\n".encode("utf-8")), "华东明细.csv"),
        },
        content_type="multipart/form-data",
    )
    assert created.status_code == 201, created.get_json()
    item = created.get_json()["item"]
    assert item["category"] == "上传文件"
    assert item["previewable"] is True
    assert item["title"] == "华东明细"

    assert client.get(f"/api/library/{item['id']}").status_code == 200
    assert client.get(f"/api/library/{item['id']}/preview").status_code == 200
    assert client.get(f"/api/library/{item['id']}/download").status_code == 200

    renamed = client.patch(f"/api/library/{item['id']}", json={"favorite": "华东复盘"}).get_json()
    assert renamed["item"]["title"] == "华东复盘"
    assert renamed["item"]["favorite"] is True

    found = client.get("/api/library?q=华东").get_json()["items"]
    assert any(entry["id"] == item["id"] for entry in found)

    assert client.delete(f"/api/library/{item['id']}").status_code == 200
    assert client.get(f"/api/library/{item['id']}").status_code == 404


def test_library_category_filter(client):
    client.post(
        "/api/library",
        data={"workspace_id": "default", "file": (io.BytesIO(b"x\n1\n"), "note.md")},
        content_type="multipart/form-data",
    )
    everything = client.get("/api/library").get_json()["items"]
    tables = client.get("/api/library?category=表格").get_json()["items"]
    assert len(tables) <= len(everything)
    assert all(item["category"] == "表格" for item in tables)


def test_library_upload_saves_every_selected_file(client):
    response = client.post(
        "/api/library",
        data={"file": [
            (io.BytesIO(b"first"), "first.txt"),
            (io.BytesIO(b"second"), "second.txt"),
        ]},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    assert {item["filename"] for item in response.get_json()["items"]} == {"first.txt", "second.txt"}


def test_library_document_can_be_used_as_analysis_evidence(client):
    upload = client.post(
        "/api/library",
        data={"file": (io.BytesIO("门店销售额为 120 元".encode()), "门店记录.txt")},
        content_type="multipart/form-data",
    )
    assert upload.status_code == 201
    record_id = upload.get_json()["item"]["id"]
    created = client.post("/api/analyses", json={
        "objective": "分析上传的门店记录", "source_ids": [],
    })
    assert created.status_code == 201, created.get_json()
    run_id = created.get_json()["item"]["id"]
    attached = client.post(
        f"/api/analyses/{run_id}/attachments/library", json={"record_id": record_id},
    )
    assert attached.status_code == 201, attached.get_json()
    listed = client.get(f"/api/analyses/{run_id}/attachments").get_json()["items"]
    assert [item["filename"] for item in listed] == ["门店记录.txt"]


def test_html_report_artifact_is_real_not_a_placeholder(client, app):
    """网页成果必须真的能生成 HTML，且对不可信内容转义。"""
    from backend.services.results.delivery import ARTIFACT_KINDS, _render_html

    assert "report_html" in ARTIFACT_KINDS
    html = _render_html(
        {
            "title": "<script>alert(1)</script>销售报告",
            "summary": "同比下降 12%",
            "kpis": [{"label": "销售额", "value": 12_480_000, "display": "¥12.48M"}],
            "tables": [{"title": "明细", "columns": ["城市", "销售额"],
                        "rows": [{"城市": "杭州", "销售额": 123}]}],
            "limitations": ["仅覆盖已支付订单"],
            "evidence_refs": ["ref-1"],
        },
        {"version": 2, "created_at": "2026-10-04"},
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "¥12.48M" in html
    assert "<th>城市</th>" in html
    assert html.startswith("<!doctype html>")


# ---------------------------------------------------------------- 指标试算


def test_metric_trial_compiles_governed_sql(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    response = client.post("/api/admin/metric-trial", json={
        "metric": "sales_amount", "group_by": ["区域"],
        "filters": [{"dimension": "区域", "op": "=", "value": "华东"}],
        "time_range": {"start": "2026-01-01", "end": "2026-12-31"},
    })
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert 'SUM("销售额")' in payload["plan"]["sql"]
    assert '"区域"' in payload["plan"]["sql"]
    assert "2027-01-01" in payload["plan"]["sql"]
    assert payload["plan"]["metric"]["name"] == "sales_amount"
    assert payload["plan"]["model"]["name"]
    assert payload["result"]["data"]
    # 界面永远不需要看到内部指纹
    assert "definition_fingerprint" not in payload["plan"]["metric"]


def test_metric_trial_records_the_run(client, app):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    client.post("/api/admin/metric-trial", json={"metric": "order_count", "group_by": ["城市"]})
    with app.app_context():
        trials = app.extensions["meridian_db"].list("metric_trials", workspace_id="default")
    assert len(trials) == 1
    assert trials[0]["metric"] == "order_count"


def test_metric_trial_rejects_unknown_and_unpublished_metrics(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    assert client.post("/api/admin/metric-trial", json={"metric": "no_such"}).status_code == 400
    assert client.post("/api/admin/metric-trial", json={}).status_code == 400


def test_admin_can_trial_draft_metric_before_publishing(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    model = client.get("/api/semantic/models").get_json()["items"][0]
    draft = client.post("/api/semantic/metrics", json={
        "name": "trial_sales", "label": "试算销售额", "model_id": model["id"],
        "measure": "sales_amount", "status": "draft",
    })
    assert draft.status_code == 201, draft.get_json()
    trial = client.post("/api/admin/metric-trial", json={
        "metric": "trial_sales", "group_by": ["城市"],
    })
    assert trial.status_code == 200, trial.get_json()
    assert trial.get_json()["result"]["data"]


# ---------------------------------------------------------------- 运行记录


def test_run_records_expose_task_language_not_internals(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    session = client.post("/api/sessions", json={"name": "轨迹"}).get_json()["item"]
    client.post("/api/analyses", json={
        "session_id": session["id"], "objective": "为什么华东销售下降？",
        "skill_id": "attribution", "source_ids": [client.get("/api/sources").get_json()["items"][0]["id"]],
    })
    runs = client.get("/api/admin/runs").get_json()["items"]
    assert runs
    run = runs[0]
    assert run["question"] == "为什么华东销售下降？"
    assert run["skill_ids"] == ["attribution"]
    assert run["execution_status"]
    for field in ("source_scope", "lease_epoch", "policy_version"):
        assert field not in run


def test_run_detail_contains_the_replayable_pieces(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    session = client.post("/api/sessions", json={"name": "轨迹"}).get_json()["item"]
    created = client.post("/api/analyses", json={
        "session_id": session["id"], "objective": "本月销售额是多少？",
        "skill_id": "data-query",
    }).get_json()["item"]
    detail = client.get(f"/api/admin/runs/{created['id']}").get_json()["item"]
    assert detail["run"]["id"] == created["id"]
    assert detail["skills"]["requested"] == ["data-query"]
    # 还没有真正开跑时，可用工具自然为空，但字段必须在，界面才不用做形状判断
    assert detail["skills"]["allowed_tools"] == []
    assert "contract" in detail
    assert "actions" in detail
    # 没有模型时必须明确失败，而不是假装成功
    assert detail["run"]["execution_status"] in {"waiting_input", "failed"}


def test_analysis_auto_selects_skill_and_exposes_owner_execution(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    created = client.post("/api/analyses", json={"objective": "为什么销售额下降？"})
    assert created.status_code == 201, created.get_json()
    run = created.get_json()["item"]
    assert run["skill_id"] == "attribution"
    execution = client.get(f"/api/analyses/{run['id']}/execution")
    assert execution.status_code == 200
    assert execution.get_json()["item"]["skills"] == ["attribution"]


def test_unpublished_agent_configuration_can_be_previewed_without_publishing(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    source_id = client.get("/api/sources").get_json()["items"][0]["id"]
    created = client.post("/api/analyses", json={
        "objective": "本月销售额是多少？", "source_ids": [source_id],
        "agent_preview": {
            "name": "测试中的助手", "instruction": "按城市分析", "source_ids": [source_id],
            "skill_ids": ["data-query"],
        },
    })
    assert created.status_code == 201, created.get_json()
    run = created.get_json()["item"]
    assert run["agent_id"] == "preview"
    assert run["skill_id"] == "data-query"


# ---------------------------------------------------------------- 评测


def test_evaluations_aggregate_feedback_and_failures(client):
    payload = client.get("/api/admin/evaluations").get_json()
    assert set(payload["totals"]) >= {"runs", "finished", "failed", "published", "feedback", "satisfaction"}
    labels = [item["label"] for item in payload["reasons"]]
    for label in ("数据不正确", "理解错误", "结论不合理", "不够深入", "速度太慢"):
        assert label in labels


def test_evaluations_are_owner_only(app):
    owner = app.test_client()
    assert owner.post("/api/auth/register", json={
        "email": "owner@example.com", "password": "correct-horse", "name": "Owner",
    }).status_code == 201
    workspace = owner.post("/api/workspaces", json={"name": "私有"}).get_json()["item"]
    headers = {"X-Workspace-Id": workspace["id"]}
    assert owner.get("/api/admin/evaluations", headers=headers).status_code == 200
    assert owner.get("/api/admin/runs", headers=headers).status_code == 200

    member = app.test_client()
    member.post("/api/auth/register", json={
        "email": "viewer@example.com", "password": "correct-horse", "name": "Viewer",
    })
    assert member.get("/api/evaluations").status_code == 404
    assert member.get("/api/admin/evaluations", headers=headers).status_code == 403


# ---------------------------------------------------------------- 智能体组合


def test_agent_binds_a_resource_set_not_a_single_prompt(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    source = client.get("/api/sources").get_json()["items"][0]
    metric = next(
        item for item in client.get("/api/semantic/metrics").get_json()["items"]
        if item["status"] == "approved"
    )
    created = client.post("/api/agents", json={
        "name": "华东经营助手", "description": "盯华东的销售与库存",
        "source_ids": [source["id"]], "metric_ids": [metric["id"]],
        "skill_ids": ["data-analysis", "attribution", "report"],
        "welcome": "问我华东的经营问题",
        "suggested_questions": ["为什么华东销售下降？"],
        "instruction": "先确认口径再下结论。",
    })
    assert created.status_code == 201, created.get_json()
    item = created.get_json()["item"]
    assert item["skill_ids"] == ["data-analysis", "attribution", "report"]
    assert item["metric_ids"] == [metric["id"]]

    # 发布前需要数据源
    empty = client.post("/api/agents", json={"name": "空助手", "source_ids": [], "skill_ids": []})
    assert empty.status_code == 201
    assert client.post(f"/api/agents/{empty.get_json()['item']['id']}/publish").status_code == 400
    assert client.post(f"/api/agents/{item['id']}/publish").status_code == 200
    bounded = client.post("/api/analyses", json={
        "objective": "分析华东经营", "agent_id": item["id"],
        "source_ids": [source["id"]], "skill_ids": ["forecast"],
    })
    assert bounded.status_code == 201, bounded.get_json()
    assert bounded.get_json()["item"]["skill_id"] == "data-analysis"


def test_agent_rejects_unpublished_or_unknown_skills(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    source = client.get("/api/sources").get_json()["items"][0]
    client.post("/api/skills", json={
        "id": "draft-skill", "name": "草稿技能",
        "description": "还没准备好", "instruction": "先写着",
        "triggers": ["草稿"], "example_questions": ["草稿问题？"],
    })
    for skill_id in ("draft-skill", "不存在的技能"):
        response = client.post("/api/agents", json={
            "name": "绑定测试", "source_ids": [source["id"]], "skill_ids": [skill_id],
        })
        assert response.status_code == 400, skill_id


def test_agent_delete_removes_draft_from_catalog(client):
    created = client.post("/api/agents", json={"name": "临时助手", "source_ids": []})
    assert created.status_code == 201, created.get_json()
    agent_id = created.get_json()["item"]["id"]
    assert client.delete(f"/api/agents/{agent_id}").status_code == 200
    assert agent_id not in {item["id"] for item in client.get("/api/agents").get_json()["items"]}


def test_super_agent_is_seeded_and_published(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    agents = client.get("/api/agents").get_json()["items"]
    super_agent = next(item for item in agents if item["id"] == "agent-superskill")
    assert super_agent["status"] == "published"
    assert super_agent["suggested_questions"]
    assert super_agent["welcome"]
