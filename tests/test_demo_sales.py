"""演示数据必须真的能回答产品承诺的那几个问题。

如果演示数据只是"看起来像数据"，验收场景就是假的。这套测试把
§67 里承诺的每个问题都变成一次真实查询。
"""

from __future__ import annotations

import pytest

from backend.services.demo_sales import (
    CATEGORIES, CHANNELS, REGIONS, SAMPLE_SEED_ID, build_frame, sample_questions, summary,
)

FRAME = build_frame()


def test_the_frame_is_a_real_fact_table():
    info = summary(FRAME)
    assert info["months"] >= 24, "同比与预测至少需要两年数据"
    assert info["rows"] > 1000
    assert set(info["regions"]) == set(REGIONS)
    assert set(info["categories"]) == set(CATEGORIES)
    assert set(info["channels"]) == set(CHANNELS)


def test_the_data_is_deterministic():
    """每次安装必须得到同样的数字，否则文档里的答案会漂移。"""
    again = build_frame()
    assert again.equals(FRAME)


def test_every_documented_question_is_answerable():
    assert sample_questions() == [
        "本月销售额是多少？",
        "华东销售同比怎么样？",
        "哪个城市下降最多？",
        "为什么华东销售下降？",
        "哪些商品表现异常？",
        "预测下个月销售额。",
        "生成经营分析报告。",
        "生成经营分析 PPT。",
    ]


def test_sales_amount_can_be_answered_with_yoy(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    latest = FRAME["统计年月"].max()
    previous = f"{int(latest[:4]) - 1}{latest[4:]}"

    def revenue(year_month: str, **filters: str) -> float:
        body = {
            "workspace_id": "default", "metric": "sales_amount",
            "time_range": {"start": f"{year_month}-01", "end": f"{year_month}-28"},
            "filters": [
                {"dimension": name, "op": "=", "value": value}
                for name, value in filters.items()
            ],
        }
        payload = client.post("/api/semantic/query", json=body).get_json()
        return payload["result"]["data"][0]["sales_amount"]

    assert revenue(latest) > 0
    # 华东同比为负，这正是归因场景存在的原因
    assert revenue(latest, 区域="华东") < revenue(previous, 区域="华东")
    # 其他区域仍在增长，华东是唯一走弱的地方
    for region in ("华北", "华南", "华中", "西南"):
        assert revenue(latest, 区域=region) > revenue(previous, 区域=region), region


def test_a_city_can_be_named_as_the_worst_decliner(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    latest = FRAME["统计年月"].max()
    previous = f"{int(latest[:4]) - 1}{latest[4:]}"
    payload = client.post("/api/semantic/query", json={
        "workspace_id": "default", "metric": "sales_amount", "group_by": ["城市"],
        "time_range": {"start": f"{previous}-01", "end": f"{previous}-28"},
    }).get_json()["result"]["data"]
    prior = {row["城市"]: row["sales_amount"] for row in payload}

    current = client.post("/api/semantic/query", json={
        "workspace_id": "default", "metric": "sales_amount", "group_by": ["城市"],
        "time_range": {"start": f"{latest}-01", "end": f"{latest}-28"},
    }).get_json()["result"]["data"]
    changes = [
        (row["sales_amount"] / prior[row["城市"]] - 1, row["城市"])
        for row in current if row["城市"] in prior and prior[row["城市"]]
    ]
    changes.sort()
    assert changes and changes[0][0] < 0
    assert changes[0][1] in {city for region in REGIONS for city in REGIONS[region]["provinces"]}


def test_the_attribution_story_is_visible_in_the_data():
    """华东的下降必须能在品类和渠道上看出具体落点，而不是均匀下滑。"""
    east = FRAME[FRAME["区域"] == "华东"]
    latest = east["统计年月"].max()
    month = east[east["统计年月"] == latest]

    by_category = month.groupby("品类")["销售额"].sum()
    by_channel = month.groupby("渠道")["销售额"].sum()
    assert by_category.nunique() == 5
    assert by_channel.nunique() == 3
    # 家电是华东客单价最高的品类，它的塌陷是归因的主要落点
    assert "家电" in by_category.index


def test_an_outlier_category_month_exists(client):
    """异常识别要有真东西可找。"""
    fresh = FRAME[(FRAME["城市"] == "深圳") & (FRAME["品类"] == "生鲜")]
    series = fresh.groupby("统计年月")["销售额"].sum().sort_index()
    zscore = (series - series.mean()) / series.std()
    assert zscore.max() > 3, "深圳生鲜的异常月没有被生成出来"


def test_demo_seed_creates_metrics_knowledge_and_the_super_agent(client):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    sources = client.get("/api/sources").get_json()["items"]
    seeded = next(item for item in sources if item["sample_seed"]["id"] == SAMPLE_SEED_ID)
    assert seeded["tables"][0]["name"] == "sales_monthly"
    assert seeded["tables"][0]["rows"] > 1000

    metrics = client.get("/api/semantic/metrics").get_json()["items"]
    approved = {item["name"] for item in metrics if item["status"] == "approved"}
    assert {"sales_amount", "order_count", "average_order_value", "net_sales_amount"} <= approved

    agents = client.get("/api/agents").get_json()["items"]
    assert any(item["id"] == "agent-superskill" for item in agents)
    assert next(item for item in agents if item["id"] == "agent-superskill")["suggested_questions"]


def test_seeding_twice_changes_nothing(client):
    first = client.post("/api/demo/seed", json={"workspace_id": "default"}).get_json()
    second = client.post("/api/demo/seed", json={"workspace_id": "default"}).get_json()
    assert second["created"] == []
    assert second["source"]["id"] == first["source"]["id"]
    assert second["summary"] == first["summary"]


def test_derived_metric_divides_after_aggregating(client):
    """客单价必须先汇总再相除，否则平均数会被行数加权错。"""
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    payload = client.post("/api/semantic/query", json={
        "workspace_id": "default", "metric": "average_order_value", "group_by": ["区域"],
    }).get_json()
    assert "SUM" in payload["plan"]["sql"]
    for row in payload["result"]["data"]:
        assert 100 < row["average_order_value"] < 1000


@pytest.mark.parametrize("metric", ["sales_amount", "order_count", "customer_count"])
def test_each_core_metric_executes(client, metric):
    client.post("/api/demo/seed", json={"workspace_id": "default"})
    response = client.post("/api/semantic/query", json={
        "workspace_id": "default", "metric": metric, "group_by": ["区域"],
    })
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["result"]["data"]
