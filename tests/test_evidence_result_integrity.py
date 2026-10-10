from __future__ import annotations

import json

import pandas as pd
import pytest

from backend.agent.contracts import TaskContract
from backend.agent.store import RunStore
from backend.services.datasets import frame_records
from backend.services.results.manifests import ResultService


@pytest.mark.parametrize("format_name", ["csv", "parquet"])
def test_evidence_cells_replay_late_typed_rows_with_multiline_text(app, source, format_name):
    database = app.extensions["meridian_db"]
    records = [{
        "code": f"{index:05d}", "note": f"第一行\n第二行 {index}",
        "amount": 745 if index == 500 else index + 1000,
        "missing": None, "token": "NA", "enabled": index != 500,
    } for index in range(620)]
    frame = pd.DataFrame(records)
    path = app.config["SETTINGS"].export_dir / f"evidence.{format_name}"
    if format_name == "csv":
        frame.to_csv(path, index=False)
    else:
        frame.to_parquet(path, index=False)
    session = database.put("sessions", {
        "id": database.new_id("ses"), "workspace_id": "default", "owner_id": "local-default",
    }, workspace_id="default")
    store = RunStore(database)
    run, _ = store.create_run(workspace_id="default", session_id=session["id"],
                              actor_id="local-default", source_scope=[source["id"]], allowed_tool_ids=["query", "validate"])
    store.add_contract(run["id"], TaskContract.from_payload({
        "objective": "核对销售额", "coverage": "所选数据的全部记录",
        "dimensions": ["code"],
        "source_scope": [source["id"]], "deliverables": ["summary"],
    }), expected_version=0, confirmed_by="local-default")
    result = database.put("query_results", {
        "id": database.new_id("qry"), "workspace_id": "default", "actor_id": "local-default",
        "source_ids": [source["id"]], "path": str(path), "rows": len(frame),
        "columns": list(frame.columns), "data": frame_records(frame, 300), "completeness": "complete",
    }, workspace_id="default")
    evidence = [{
        "tool": tool, "status": "SUCCEEDED", "refs": [result["id"]], "completeness": "complete",
        "validation_status": "PASS" if tool == "validate_result" else "not_evaluated",
    } for tool in ["query_data", "validate_result"]]
    with app.app_context():
        service = ResultService(database)
        published = service.finalize(run["id"], "销售额为 745 元。", evidence)
        assert published["published"] is True
        claim = service.claims(run["id"], workspace_id="default")[-1]
    response = app.test_client().get(f"/api/analyses/{run['id']}/evidence/claims/{claim['id']}/cells/0")
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = json.loads(response.get_data(as_text=True), parse_constant=lambda value: pytest.fail(value))
    assert payload["item"]["row_index"] == 500
    assert payload["item"]["row"] == records[500]
    assert payload["item"]["value"] == 745
    assert payload["item"]["result_id"] == result["id"]

    # Replay must detect changed evidence rather than trusting the stored claim.
    frame.loc[500, "amount"] = 746
    if format_name == "csv":
        frame.to_csv(path, index=False)
    else:
        frame.to_parquet(path, index=False)
    changed = app.test_client().get(f"/api/analyses/{run['id']}/evidence/claims/{claim['id']}/cells/0")
    assert changed.status_code == 400
    assert "证据结果已变化" in changed.get_json()["error"]
