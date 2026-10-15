"""Regression coverage for the user feedback → operations statistics contract."""


def test_evaluations_count_the_ratings_saved_by_the_analysis_feedback_api(app, client):
    database = app.extensions["meridian_db"]
    for index, rating in enumerate(("correct", "partially_correct", "incorrect")):
        database.put("analysis_feedback", {
            "id": f"journey-feedback-{index}", "workspace_id": "default",
            "run_id": f"run-{index}", "actor_id": "local-default",
            "rating": rating, "comment": "核对统计口径", "status": "open",
        }, workspace_id="default")
    response = client.get("/api/admin/evaluations")
    assert response.status_code == 200
    totals = response.get_json()["totals"]
    assert totals["feedback"] == 3
    assert totals["positive"] == 1
    # 部分正确仍需改进，不能计为完全认可。
    assert totals["negative"] == 2
    assert totals["satisfaction"] == 0.3333


def test_evaluations_do_not_claim_satisfaction_without_feedback(client):
    totals = client.get("/api/admin/evaluations").get_json()["totals"]
    assert totals["feedback"] == 0
    assert totals["satisfaction"] is None
