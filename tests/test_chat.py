"""채팅 에이전트 통합 테스트: 실제 MCP(HTTP)로 이 서버의 /mcp에 접속하고, Claude API만 가짜로 대체."""

import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn

from anomaly_mcp import dashboard as dash
from anomaly_mcp.chat import ChatAgent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mcp_url():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(dash.create_app(), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    t.join(timeout=5)


class FakeStream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self.events:
                yield e
        return gen()

    async def get_final_message(self):
        return self.final


class FakeClaude:
    """1턴: get_monitor_status 호출 → 2턴: 텍스트 답변."""

    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        if len(self.calls) == 1:
            block = SimpleNamespace(type="tool_use", id="tu_1", name="get_monitor_status", input={})
            ev = SimpleNamespace(type="content_block_start", content_block=block)
            return FakeStream([ev], SimpleNamespace(content=[block], stop_reason="tool_use"))
        text = SimpleNamespace(type="text", text="모든 센서가 정상입니다.")
        return FakeStream([SimpleNamespace(type="text", text=text.text)],
                          SimpleNamespace(content=[text], stop_reason="end_turn"))


def test_chat_uses_mcp_tools(mcp_url):
    agent = ChatAgent([mcp_url], api_key="test")
    fake = FakeClaude()
    agent._client = fake

    async def collect():
        return [ev async for ev in agent.run("c1", "지금 상태 어때?", "현재 시각: 테스트")]

    events = asyncio.run(collect())
    types = [e["type"] for e in events]
    assert types[0] == "tool_pending" and "tool_start" in types and types[-1] == "done"
    assert next(e for e in events if e["type"] == "tool_end")["ok"] is True
    assert "".join(e["text"] for e in events if e["type"] == "text") == "모든 센서가 정상입니다."

    tools = {t["name"] for t in fake.calls[0]["tools"]}
    assert {"get_monitor_status", "check_sensor", "list_alarm_events"} <= tools
    assert not tools & {"ingest_readings", "register_sensor", "simulate_fault"}  # 조회 전용

    # 2턴째 요청에 MCP tool 결과가 tool_result로 들어갔는지
    tool_result = fake.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "summary" in tool_result["content"]
    # 대시보드 컨텍스트가 질문에 붙는지
    assert "dashboard_context" in fake.calls[0]["messages"][0]["content"][0]["text"]
    # 이력: user, assistant(tool_use), user(tool_result), assistant(text)
    assert [m["role"] for m in agent.conversations["c1"]] == ["user", "assistant", "user", "assistant"]
