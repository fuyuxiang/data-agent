from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text


CASES = (
    ("postgresql", "MERIDIAN_TEST_POSTGRES_URL"),
    ("mysql", "MERIDIAN_TEST_MYSQL_URL"),
)


@pytest.mark.database_integration
@pytest.mark.parametrize(("driver", "environment_name"), CASES)
def test_external_database_registration_discovery_and_read_only_query(client, driver, environment_name):
    url = os.getenv(environment_name, "").strip()
    if not url:
        pytest.skip(f"{environment_name} is not configured")

    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP VIEW IF EXISTS meridian_smoke_view"))
            connection.execute(text("DROP TABLE IF EXISTS meridian_smoke"))
            connection.execute(text("CREATE TABLE meridian_smoke (region VARCHAR(40), amount INTEGER)"))
            connection.execute(text("INSERT INTO meridian_smoke(region, amount) VALUES ('north', 12), ('south', 8)"))
            connection.execute(text("CREATE VIEW meridian_smoke_view AS SELECT region, amount FROM meridian_smoke"))

        connected = client.post(
            "/api/sources/database",
            json={"name": f"{driver} smoke", "url": url, "ssl_mode": "disabled" if driver == "mysql" else "disable"},
        )
        assert connected.status_code == 201, connected.get_json()
        source = connected.get_json()["item"]
        discovered = {(item["source_name"], item["object_type"]) for item in source["tables"]}
        assert ("meridian_smoke", "table") in discovered
        assert ("meridian_smoke_view", "view") in discovered

        queried = client.post(
            "/api/query",
            json={
                "source_ids": [source["id"]],
                "sql": "SELECT region, SUM(amount) AS amount FROM meridian_smoke GROUP BY region ORDER BY region",
            },
        )
        assert queried.status_code == 200, queried.get_json()
        assert queried.get_json()["result"]["rows"] == 2

        rejected = client.post(
            "/api/query",
            json={"source_ids": [source["id"]], "sql": "DELETE FROM meridian_smoke"},
        )
        assert rejected.status_code == 400
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP VIEW IF EXISTS meridian_smoke_view"))
            connection.execute(text("DROP TABLE IF EXISTS meridian_smoke"))
        engine.dispose()
