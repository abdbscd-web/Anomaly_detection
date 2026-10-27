"""Claude API를 이용한 이상 원인 분석.

통계 탐지 결과(어디가 이상한지)를 Claude에게 넘겨
'왜 이상한지 / 무엇을 점검해야 하는지'를 구조화된 JSON으로 받는다.
정형 출력(output_config.format)으로 스키마를 고정해서 파싱 실패가 없도록 했다.
"""

from __future__ import annotations

import json
import os

SYSTEM_PROMPT = """당신은 반도체 공정/설비 엔지니어링 전문가입니다.
센서 시계열의 통계적 이상 탐지 결과를 받아 다음을 수행합니다.
- 이상 패턴을 분류합니다 (spike, drift, stuck, 스펙 이탈, 센서 간 상관 붕괴 등).
- 실제 공정 이상인지, 센서/계측 오류일 가능성이 높은지 판단합니다.
- 가능한 근본 원인을 공정 지식(챔버 상태, PM 주기, 가스/RF/진공 계통 등)에 기반해 추정합니다.
- 엔지니어가 바로 실행할 수 있는 점검 항목을 우선순위대로 제시합니다.
데이터로 뒷받침되지 않는 추측은 confidence를 낮게 표시하고, 근거를 명확히 쓰세요.
응답은 한국어로 작성합니다."""

ANALYSIS_TOOL = {
    "name": "report_anomaly_analysis",
    "description": "이상 탐지 결과에 대한 분석 보고서를 제출한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": ["normal", "low", "medium", "high", "critical"]},
            "summary": {"type": "string", "description": "2~3문장 요약"},
            "pattern": {"type": "string", "description": "관찰된 이상 패턴 유형"},
            "likely_sensor_fault": {"type": "boolean", "description": "공정 이상이 아닌 센서 오류일 가능성이 높은가"},
            "root_causes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "cause": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "evidence": {"type": "string"},
                    },
                    "required": ["cause", "confidence", "evidence"],
                    "additionalProperties": False,
                },
            },
            "recommended_actions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["severity", "summary", "pattern", "likely_sensor_fault",
                     "root_causes", "recommended_actions"],
        "additionalProperties": False,
    },
}

# 거절(refusal) 시 서버가 분류에 맞는 다른 모델로 자동 재시도
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeAnalyzer:
    def __init__(self, api_key: str | None = None, model: str | None = None, max_tokens: int | None = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
        # thinking 토큰도 max_tokens에 포함되므로 넉넉하게
        self.max_tokens = int(max_tokens or os.getenv("CLAUDE_MAX_TOKENS", "16000"))
        self.effort = os.getenv("CLAUDE_EFFORT", "high")
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set (.env 파일을 확인하세요)")
            from anthropic import Anthropic  # 지연 import: 통계 기능은 SDK 없이도 동작
            self._client = Anthropic(api_key=self.api_key)
        return self._client

    def build_prompt(self, payload: dict) -> str:
        return (
            "다음은 센서 데이터와 통계적 이상 탐지 결과입니다. 분석해 주세요.\n\n"
            f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n```"
        )

    def analyze(self, payload: dict) -> dict:
        client = self._get_client()
        resp = client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # 반복 호출 시 프롬프트 캐싱
            }],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": ANALYSIS_TOOL["input_schema"]},
            },
            betas=[FALLBACK_BETA],
            fallbacks="default",
            messages=[{"role": "user", "content": self.build_prompt(payload)}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude가 요청을 거절했습니다 (refusal)")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("응답이 max_tokens에서 잘렸습니다. CLAUDE_MAX_TOKENS를 늘리세요.")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise RuntimeError("Claude did not return a structured analysis")
        result = json.loads(text)
        result["_model"] = resp.model
        result["_usage"] = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }
        return result
