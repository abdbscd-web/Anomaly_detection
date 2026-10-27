"""대시보드 채팅 에이전트 (MCP 호스트).

채팅창 자체는 MCP 서버가 아니라 MCP를 '사용하는' 쪽이다.
요청마다 MCP 서버(기본: 이 프로세스의 /mcp)에 MCP 클라이언트로 접속해 tool 목록을 받아
Claude에 넘기고, Claude가 요청한 tool을 MCP로 실행해 결과를 돌려주는 루프를 돈다.

    브라우저 ──SSE── /api/chat ── ChatAgent ──MCP(HTTP)──► /mcp (이 서버, + 외부 MCP 서버들)
                                     └──── Claude API (tool use, streaming)

그래서 Claude Code가 쓰는 tool과 대시보드 채팅이 쓰는 tool이 항상 같다.
CHAT_MCP_SERVERS에 URL을 추가하면 다른 MCP 서버(MES, 설비 이력 등)의 tool도 함께 쓴다.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import OrderedDict
from contextlib import AsyncExitStack
from typing import AsyncIterator

from mcp import ClientSession

try:  # MCP SDK 2.x
    from mcp.client.streamable_http import streamable_http_client as _http_client
except ImportError:  # 1.x
    from mcp.client.streamable_http import streamablehttp_client as _http_client

from .llm import FALLBACK_BETA

SYSTEM_PROMPT = """당신은 반도체 FDC(Fault Detection & Classification) 이상감지 대시보드의 어시스턴트입니다.
엔지니어가 대시보드에 보이는 센서·설비 상태에 대해 질문하면, 제공된 tool로 실제 데이터를 조회해서 답합니다.

원칙:
- 수치·상태·시각은 반드시 tool 결과에 근거해서 말하고, 추측은 추측이라고 밝힙니다.
- 전체 현황은 get_monitor_status, 최근 이력은 list_alarm_events, 특정 센서는 check_sensor,
  설비 내 센서 간 관계는 detect_multivariate를 우선 사용합니다.
- check_sensor는 대시보드와 같은 기준을 쓰려면 last_n=300, thresholds={"mad": 4.5, "rolling_zscore": 5,
  "cusum": 12}, params={"rolling_zscore": {"window": 30}, "cusum": {"warmup": 150}}로 호출합니다.
- analyze_with_claude는 시간이 걸리므로 사용자가 원인 분석을 원할 때만 호출합니다.
- 상태 표기: ALARM=스펙(LSL/USL) 이탈, WARNING=통계적 이상, OK=정상.
- 센서 ID는 원문 그대로(예: ETCH03_CHA_TEMP) 쓰면 대시보드에서 클릭할 수 있습니다.
- 한국어로, 엔지니어가 바로 행동할 수 있게 간결하게 답합니다. 필요하면 짧은 표나 목록을 씁니다."""

# 채팅에서는 데이터를 바꾸는 tool을 기본으로 막는다 (조회 전용)
DEFAULT_BLOCKED = "register_sensor,ingest_readings,simulate_fault"
MAX_TOOL_RESULT_CHARS = 30_000
MAX_TURNS = 12
MAX_CONVERSATIONS = 100


def _attr(obj, snake: str, camel: str):
    """MCP SDK 2.x(snake_case) / 1.x(camelCase) 속성 호환"""
    return getattr(obj, snake, None) if hasattr(obj, snake) else getattr(obj, camel, None)


def _tool_text(result) -> str:
    parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    text = "\n".join(parts) if parts else json.dumps(_attr(result, "structured_content", "structuredContent"), ensure_ascii=False)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + f"\n...(결과가 길어 {MAX_TOOL_RESULT_CHARS}자에서 잘림. 범위를 좁혀 다시 조회하세요)"
    return text


class ChatAgent:
    def __init__(self, server_urls: list[str] | None = None, api_key: str | None = None):
        self.server_urls = server_urls or []
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("CHAT_MODEL") or os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
        self.effort = os.getenv("CHAT_EFFORT", "medium")
        self.max_tokens = int(os.getenv("CHAT_MAX_TOKENS", "32000"))
        self.blocked = {t.strip() for t in os.getenv("CHAT_BLOCKED_TOOLS", DEFAULT_BLOCKED).split(",") if t.strip()}
        # 대화 이력은 서버에 보관 (thinking/tool 블록을 원형 그대로 다시 보내야 하므로)
        self.conversations: OrderedDict[str, list] = OrderedDict()
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=self.api_key)
        return self._client

    def _history(self, conversation_id: str) -> list:
        history = self.conversations.setdefault(conversation_id, [])
        self.conversations.move_to_end(conversation_id)
        while len(self.conversations) > MAX_CONVERSATIONS:
            self.conversations.popitem(last=False)
        return history

    def reset(self, conversation_id: str) -> None:
        self.conversations.pop(conversation_id, None)

    async def _connect(self, stack: AsyncExitStack) -> tuple[list[dict], dict[str, ClientSession]]:
        """설정된 MCP 서버들에 접속해 Claude용 tool 정의와 tool→세션 라우팅 표를 만든다."""
        tools, route = [], {}
        for url in self.server_urls:
            streams = await stack.enter_async_context(_http_client(url))
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
            for t in (await session.list_tools()).tools:
                if t.name in self.blocked or t.name in route:  # 이름이 겹치면 먼저 연결한 서버 우선
                    continue
                route[t.name] = session
                tools.append({
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": _attr(t, "input_schema", "inputSchema"),
                    "eager_input_streaming": True,
                })
        return tools, route

    async def run(self, conversation_id: str, question: str, context: str = "") -> AsyncIterator[dict]:
        """질문 하나를 처리하며 UI용 이벤트를 흘려보낸다.

        이벤트: text{text} | tool_start{id,name,input} | tool_end{id,name,ok} | error{message} | done{}
        """
        history = self._history(conversation_id)
        content = [{"type": "text", "text": f"<dashboard_context>\n{context}\n</dashboard_context>"}] if context else []
        content.append({"type": "text", "text": question})
        history.append({"role": "user", "content": content})
        client = self._get_client()

        async with AsyncExitStack() as stack:
            tools, route = await self._connect(stack)
            json_retries = 0
            for _ in range(MAX_TURNS):
                try:
                    async with client.beta.messages.stream(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=SYSTEM_PROMPT,
                        tools=tools,
                        messages=history,
                        output_config={"effort": self.effort},
                        cache_control={"type": "ephemeral"},
                        betas=[FALLBACK_BETA],
                        fallbacks="default",
                    ) as stream:
                        async for event in stream:
                            if event.type == "text":
                                yield {"type": "text", "text": event.text}
                            elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                                yield {"type": "tool_pending", "id": event.content_block.id,
                                       "name": event.content_block.name}
                        response = await stream.get_final_message()
                    json_retries = 0
                except ValueError:
                    # tool 입력 JSON을 전혀 파싱할 수 없음 → tool_use_id가 없으니 같은 턴을 다시 요청
                    json_retries += 1
                    if json_retries > 2:
                        raise
                    continue

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if response.stop_reason == "refusal":
                    history.append({"role": "assistant", "content": response.content})
                    yield {"type": "error", "message": "Claude가 이 요청에 답할 수 없다고 판단했습니다."}
                    break
                if response.stop_reason == "max_tokens" and tool_uses:
                    yield {"type": "error", "message": "응답이 너무 길어 잘렸습니다. 질문을 좁혀 주세요."}
                    break
                if response.stop_reason == "pause_turn":
                    history.append({"role": "assistant", "content": response.content})
                    continue
                if not tool_uses:
                    history.append({"role": "assistant", "content": response.content})
                    break

                # 병렬 호출된 tool은 결과를 한 user 메시지로 모아 돌려준다
                results = []
                for block in tool_uses:
                    yield {"type": "tool_start", "id": block.id, "name": block.name, "input": block.input}
                    session = route.get(block.name)
                    if session is None or not isinstance(block.input, dict):
                        ok, text = False, f"사용할 수 없는 tool이거나 입력이 잘못되었습니다: {block.name}"
                    else:
                        try:
                            res = await session.call_tool(block.name, block.input)
                            ok, text = not _attr(res, "is_error", "isError"), _tool_text(res)
                        except Exception as e:  # MCP 전송 오류도 Claude가 보고 판단하게
                            ok, text = False, f"{type(e).__name__}: {e}"
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                    **({} if ok else {"is_error": True})})
                    yield {"type": "tool_end", "id": block.id, "name": block.name, "ok": ok}
                # 이력은 assistant(tool_use) + user(tool_result)를 항상 한 쌍으로 추가 (append-only)
                history.append({"role": "assistant", "content": response.content})
                history.append({"role": "user", "content": results})
            else:
                yield {"type": "error", "message": f"tool 호출이 {MAX_TURNS}턴을 넘어 중단했습니다."}
        yield {"type": "done"}


def new_conversation_id() -> str:
    return uuid.uuid4().hex
