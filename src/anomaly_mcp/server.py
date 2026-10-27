"""이상감지 MCP 서버.

Tools
  - list_methods            : 사용 가능한 탐지 알고리즘 목록
  - detect_anomalies        : 값 배열을 직접 넘겨 이상 탐지
  - register_sensor         : 센서 메타데이터(단위, 설비, 스펙) 등록
  - ingest_readings         : 센서 데이터 적재
  - check_sensor            : 적재된 센서에 여러 탐지기 + 스펙 체크 일괄 실행
  - detect_multivariate     : 여러 센서 동시 (Mahalanobis) 탐지
  - analyze_with_claude     : 탐지 결과를 Claude로 원인 분석
Resources
  - sensors://list          : 등록 센서 목록
Prompts
  - investigate_sensor      : 센서 이상 조사 워크플로
"""

from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime

from dotenv import load_dotenv
try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP

from . import detectors as det
from .llm import ClaudeAnalyzer
from .store import SensorMeta, SensorStore

load_dotenv()

DEFAULT_METHOD = os.getenv("DEFAULT_METHOD", "zscore")
_env_thr = os.getenv("DEFAULT_THRESHOLD")
DEFAULT_THRESHOLD = float(_env_thr) if _env_thr else None
MAX_ANOMALIES_RETURNED = 200  # 응답 크기 제한

mcp = FastMCP("anomaly-detection")
store = SensorStore()
analyzer = ClaudeAnalyzer()
analysis_log: deque[dict] = deque(maxlen=50)  # 최근 Claude 분석 결과 (대시보드 표시용)


def _attach_ts(anoms: list[det.Anomaly], timestamps: list | None) -> list[dict]:
    out = []
    for a in anoms[:MAX_ANOMALIES_RETURNED]:
        d = a.to_dict()
        if timestamps and a.index < len(timestamps) and timestamps[a.index] is not None:
            d["timestamp"] = timestamps[a.index]
        out.append(d)
    return out


def _run_detection(values, timestamps, method, threshold, params) -> dict:
    thr = threshold if threshold is not None else (DEFAULT_THRESHOLD if method == DEFAULT_METHOD else None)
    anoms = det.detect(values, method=method, threshold=thr, **(params or {}))
    return {
        "method": method,
        "threshold": thr if thr is not None else det.METHODS[method][1],
        "stats": det.summarize(values),
        "anomaly_count": len(anoms),
        "anomaly_ratio": round(len(anoms) / len(values), 4),
        "anomalies": _attach_ts(anoms, timestamps),
        "truncated": len(anoms) > MAX_ANOMALIES_RETURNED,
    }


# ------------------------------------------------------------------ tools

@mcp.tool()
def list_methods() -> dict:
    """사용 가능한 이상 탐지 알고리즘과 기본 threshold, 설명을 반환합니다."""
    return {
        name: {"default_threshold": thr, "description": desc}
        for name, (_, thr, desc) in det.METHODS.items()
    }


@mcp.tool()
def detect_anomalies(
    values: list[float],
    method: str = DEFAULT_METHOD,
    threshold: float | None = None,
    timestamps: list[str] | None = None,
    params: dict | None = None,
) -> dict:
    """숫자 배열에서 이상치를 탐지합니다.

    Args:
        values: 시계열 값 (최소 3개)
        method: zscore | mad | iqr | rolling_zscore | ewma | cusum | stuck (list_methods 참고)
        threshold: 탐지 임계값. 생략 시 알고리즘 기본값
        timestamps: values와 같은 길이의 타임스탬프 (선택)
        params: 알고리즘별 추가 인자. 예) {"window": 30}, {"alpha": 0.1}
    """
    return _run_detection(values, timestamps, method, threshold, params)


@mcp.tool()
def register_sensor(
    sensor_id: str,
    unit: str = "",
    equipment: str = "",
    chamber: str = "",
    lsl: float | None = None,
    usl: float | None = None,
    description: str = "",
) -> dict:
    """센서 메타데이터를 등록/수정합니다. lsl/usl은 공정 스펙 하한/상한입니다."""
    store.register(SensorMeta(sensor_id, unit, equipment, chamber, lsl, usl, description))
    return {"ok": True, "sensor_id": sensor_id}


@mcp.tool()
def ingest_readings(sensor_id: str, values: list[float], timestamps: list[str] | None = None) -> dict:
    """센서 측정값을 저장소에 추가합니다. 미등록 센서는 자동 생성됩니다."""
    total = store.ingest(sensor_id, values, timestamps)
    return {"ok": True, "sensor_id": sensor_id, "added": len(values), "total_points": total}


@mcp.tool()
def check_sensor(
    sensor_id: str,
    methods: list[str] | None = None,
    last_n: int | None = None,
    thresholds: dict[str, float] | None = None,
    params: dict[str, dict] | None = None,
) -> dict:
    """저장된 센서 데이터에 여러 탐지기와 스펙(LSL/USL) 체크를 한 번에 실행합니다.

    Args:
        sensor_id: 센서 ID
        methods: 실행할 탐지기 목록. 기본 ["mad", "rolling_zscore", "cusum", "stuck"]
        last_n: 최근 N개 데이터만 사용
        thresholds: 탐지기별 임계값. 예) {"mad": 4.5}
        params: 탐지기별 추가 인자. 예) {"rolling_zscore": {"window": 30}}
    """
    meta, values, ts = store.get(sensor_id, last_n)
    methods = methods or ["mad", "rolling_zscore", "cusum", "stuck"]
    thresholds, params = thresholds or {}, params or {}
    results = {}
    for m in methods:
        try:
            r = _run_detection(values, ts, m, thresholds.get(m), params.get(m))
            results[m] = {k: r[k] for k in ("threshold", "anomaly_count", "anomalies", "truncated")}
        except ValueError as e:
            results[m] = {"error": str(e)}

    spec = det.spec_limits(values, meta.lsl, meta.usl) if (meta.lsl is not None or meta.usl is not None) else []
    flagged = {a["index"] for r in results.values() for a in r.get("anomalies", [])} | {a.index for a in spec}
    return {
        "sensor": vars(meta),
        "stats": det.summarize(values),
        "spec_violations": _attach_ts(spec, ts),
        "results": results,
        "flagged_indices_total": len(flagged),
        "status": "ALARM" if spec else ("WARNING" if flagged else "OK"),
    }


@mcp.tool()
def detect_multivariate(
    sensor_ids: list[str],
    threshold: float = 3.5,
    last_n: int | None = None,
    warmup: int | None = None,
) -> dict:
    """여러 센서를 묶어 Mahalanobis 거리로 다변량 이상을 탐지합니다.
    개별 센서는 정상이어도 센서 간 관계가 깨진 시점을 찾습니다.
    warmup: 앞에서 N개를 정상 기준 구간으로 사용 (생략 시 전체)
    """
    series = [store.get(s, last_n) for s in sensor_ids]
    n = min(len(v) for _, v, _ in series)
    matrix = [[series[j][1][-n:][i] for j in range(len(series))] for i in range(n)]
    ts = series[0][2][-n:]
    anoms = det.mahalanobis(matrix, threshold, warmup)
    out = _attach_ts(anoms, ts)
    for d in out:
        col = int(d["detail"].split("=")[1])
        d["top_contributor"] = sensor_ids[col]
        d["readings"] = dict(zip(sensor_ids, matrix[d["index"]]))
    return {"sensors": sensor_ids, "samples": n, "anomaly_count": len(anoms), "anomalies": out}


@mcp.tool()
def analyze_with_claude(
    sensor_id: str | None = None,
    values: list[float] | None = None,
    context: str = "",
    last_n: int | None = 500,
) -> dict:
    """탐지 결과를 Claude API로 보내 원인 분석과 조치 권고를 받습니다.

    sensor_id(저장된 센서) 또는 values(직접 입력) 중 하나를 지정하세요.
    context: 공정 정보, 최근 PM 이력, 레시피 변경 등 추가 맥락 (분석 품질이 크게 올라감)
    """
    if not analyzer.available:
        return {"error": "ANTHROPIC_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요."}

    if sensor_id:
        payload = check_sensor(sensor_id, last_n=last_n)
        _, vals, _ = store.get(sensor_id, last_n)
    elif values:
        vals = values
        payload = {"stats": det.summarize(values), "results": {
            m: _run_detection(values, None, m, None, None) for m in ("mad", "rolling_zscore", "cusum", "stuck")
        }}
    else:
        return {"error": "sensor_id 또는 values 중 하나는 필요합니다."}

    # 토큰 절약: 원본은 다운샘플링해서 형태만 전달
    step = max(1, len(vals) // 200)
    payload["series_sample"] = {"step": step, "values": [round(v, 4) for v in vals[::step]]}
    if context:
        payload["engineer_context"] = context

    try:
        result = {"detection": {k: payload.get(k) for k in ("status", "stats")}, "analysis": analyzer.analyze(payload)}
    except Exception as e:  # API 오류를 MCP 클라이언트에 그대로 전달
        return {"error": f"Claude API 호출 실패: {type(e).__name__}: {e}"}
    analysis_log.appendleft({"time": datetime.now().isoformat(timespec="seconds"),
                             "sensor_id": sensor_id, "context": context, **result})
    return result


# ------------------------------------------------------------------ resources & prompts

@mcp.resource("sensors://list")
def sensors_list() -> str:
    """등록된 센서 목록"""
    return json.dumps(store.list(), ensure_ascii=False, indent=2, default=str)


@mcp.prompt()
def investigate_sensor(sensor_id: str) -> str:
    """센서 이상 조사 워크플로"""
    return (
        f"센서 '{sensor_id}'의 이상 여부를 조사해 주세요.\n"
        f"1. check_sensor로 상태와 탐지 결과를 확인하세요.\n"
        f"2. 이상이 있으면 같은 설비의 다른 센서와 detect_multivariate로 비교하세요.\n"
        f"3. analyze_with_claude로 원인 분석을 받고, 결과를 엔지니어가 읽기 쉽게 요약하세요."
    )


def main() -> None:
    mcp.run()  # stdio transport (Claude Desktop / Claude Code 연결용)


if __name__ == "__main__":
    main()
