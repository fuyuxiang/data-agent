from __future__ import annotations

from backend.services.agent_tools import AgentToolContext, execute_tool


def test_business_canvas_templates_blocks_diagram_and_revisions(client):
    session = client.post("/api/sessions", json={"name": "画布测试"}).get_json()["item"]
    base = f"/api/session/{session['id']}/business-canvas"
    templates = client.get(f"{base}/templates").get_json()["templates"]
    assert {item["id"] for item in templates} == {
        "blank_canvas", "business_model_canvas", "bcg_matrix", "swot_analysis", "value_proposition",
    }
    assert all(item["diagram_xml"].startswith("<mxfile>") for item in templates)

    created = client.post(
        f"{base}/projects", json={"template_id": "swot_analysis", "title": "市场 SWOT"},
    )
    assert created.status_code == 201
    project = created.get_json()["project"]
    assert project["rendering_mode"] == "diagram"
    assert {item["key"] for item in project["blocks"]} == {
        "strengths", "weaknesses", "opportunities", "threats",
    }

    changed = client.patch(
        f"{base}/projects/{project['id']}/blocks/strengths",
        json={
            "content": {
                "summary": "稳定的企业客户基础", "assumptions": "留存口径不变",
                "evidence_refs": ["query_result:qr_1"], "risks": ["样本偏差"],
                "next_actions": ["按行业分层复核"],
            },
            "actor_type": "agent", "actor_label": "strategy", "reason": "根据数据证据更新",
        },
    ).get_json()["project"]
    block = next(item for item in changed["blocks"] if item["key"] == "strengths")
    assert block["content"]["assumptions"] == ["留存口径不变"]
    revisions = client.get(f"{base}/projects/{project['id']}/revisions").get_json()["revisions"]
    assert revisions[0]["before"]["summary"] == ""
    assert revisions[0]["after"]["summary"] == "稳定的企业客户基础"
    assert revisions[0]["actor_label"] == "strategy"

    blocked = client.patch(
        f"{base}/projects/{project['id']}/diagram",
        json={"diagram_xml": '<!DOCTYPE x [<!ENTITY read SYSTEM "file:///etc/passwd">]><mxfile>&read;</mxfile>'},
    )
    assert blocked.status_code == 400
    assert "DTD" in blocked.get_json()["error"]


def test_agent_drawio_tools_fill_and_edit_exact_cells(app, client):
    session = client.post("/api/sessions", json={"name": "Agent 画布"}).get_json()["item"]
    with app.app_context():
        context = AgentToolContext(
            database=app.extensions["meridian_db"], workspace_id="default",
            session_id=session["id"], source_ids=[],
        )
        displayed, events = execute_tool(
            "display_diagram",
            {
                "title": "竞争力 SWOT", "template_id": "swot_analysis",
                "content": {"strengths": "产品稳定", "weaknesses": "渠道单一"},
            },
            context,
        )
        project = displayed["project"]
        assert "产品稳定" in displayed["xml"]
        assert events[0][0] == "diagram"
        assert project["id"] in context.diagram_ids

        current, _ = execute_tool("get_diagram", {"project_id": project["id"]}, context)
        assert current["template_id"] == "swot_analysis"
        edited, _ = execute_tool(
            "edit_diagram",
            {
                "project_id": project["id"],
                "operations": [{
                    "operation": "update", "cell_id": "3",
                    "new_xml": '<mxCell id="3" value="产品稳定且客户黏性高" style="text;html=1;" vertex="1" parent="2"><mxGeometry x="10" y="40" width="350" height="150" as="geometry"/></mxCell>',
                }],
            },
            context,
        )
        assert "客户黏性高" in edited["xml"]
        shape, _ = execute_tool("get_shape_library", {"library": "flowchart"}, context)
        assert "flowchart" in shape["library"]
        assert shape["content"]


def test_vendored_drawio_is_same_origin_and_csp_is_isolated(client):
    response = client.get("/static/drawio/index.html")
    assert response.status_code == 200
    assert b"draw" in response.data.lower()
    policy = response.headers["Content-Security-Policy"]
    assert "'unsafe-eval'" in policy
    workbench_policy = client.get("/").headers["Content-Security-Policy"]
    assert "'unsafe-eval'" not in workbench_policy
