from __future__ import annotations

from types import SimpleNamespace


def _chunk(*, content=None, tool_calls=None, usage=None):
    choices = []
    if content is not None or tool_calls is not None:
        delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
        choices = [SimpleNamespace(delta=delta)]
    return SimpleNamespace(choices=choices, usage=usage)


def _tool_delta(index: int, call_id: str | None, name: str | None, arguments: str | None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeCompletions:
    def __init__(self):
        self.calls = []
        usage = SimpleNamespace(prompt_tokens=20, completion_tokens=5, total_tokens=25)
        self.responses = [
            [
                _chunk(tool_calls=[_tool_delta(0, "call_schema", "get_", "{")]),
                _chunk(tool_calls=[_tool_delta(0, None, "schema", "}")]),
                _chunk(usage=usage),
            ],
            [
                _chunk(tool_calls=[_tool_delta(
                    0, "call_query", "query_data",
                    '{"sql":"SELECT region, SUM(sales) AS sales FROM data GROUP BY region ORDER BY sales DESC"}',
                )]),
                _chunk(usage=usage),
            ],
            [_chunk(content="North 销售领先，结论来自已执行的汇总查询。"), _chunk(usage=usage)],
            [_chunk(content="我记得上一轮结论，并会继续基于同一会话分析。"), _chunk(usage=usage)],
        ]

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.responses.pop(0))


class FakeClient:
    def __init__(self):
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


def test_iterative_agent_executes_tools_and_preserves_history(client, source, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        "backend.services.agent_runtime.resolve_provider",
        lambda _provider_id=None: ({"model": "fake-model", "temperature": 0}, fake),
    )
    session = client.post(
        "/api/sessions",
        json={"name": "工具循环", "source_ids": [source["id"]]},
    ).get_json()["item"]

    response = client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"message": "按地区汇总销售额", "source_ids": [source["id"]], "skill_id": "regression"},
    )
    stream = response.data.decode("utf-8")
    assert response.status_code == 200
    assert '"tool": "get_schema"' in stream
    assert '"tool": "query_data"' in stream
    assert "event: table" in stream
    assert "North 销售领先" in stream
    assert len(fake.completions.calls) == 3
    assert fake.completions.calls[0]["tools"]
    assert {item["function"]["name"] for item in fake.completions.calls[0]["tools"]} == {
        "get_schema", "query_data", "run_analysis", "generate_chart",
    }
    assert any(message["role"] == "tool" and "sources" in message["content"] for message in fake.completions.calls[1]["messages"])
    assert any(message["role"] == "tool" and "North" in message["content"] for message in fake.completions.calls[2]["messages"])

    stored = client.get(f"/api/sessions/{session['id']}/messages").get_json()["items"]
    assert stored[-1]["metadata"]["mode"] == "agent"
    assert [item["name"] for item in stored[-1]["metadata"]["tool_trace"]] == ["get_schema", "query_data"]

    follow_up = client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"message": "上一轮的结论是什么？", "source_ids": [source["id"]]},
    )
    assert follow_up.status_code == 200
    assert "我记得上一轮结论" in follow_up.data.decode("utf-8")
    fourth_messages = fake.completions.calls[3]["messages"]
    assert any(message.get("role") == "assistant" and "North 销售领先" in str(message.get("content")) for message in fourth_messages)
    assert any(message.get("role") == "tool" and "North" in message.get("content", "") for message in fourth_messages)
