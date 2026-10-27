"""대시보드 + MCP(Streamable HTTP) 통합 서버.

하나의 프로세스가 같은 SensorStore를 공유하며 다음을 제공한다.

    http://HOST:PORT/          웹 대시보드
    http://HOST:PORT/api/...   대시보드용 REST API
    http://HOST:PORT/mcp       MCP 엔드포인트 (Claude Code / Desktop / 사내 에이전트)
    /api/chat                  대시보드 채팅 — 서버가 MCP 클라이언트로 /mcp에 접속해 Claude에 tool 제공

MCP 클라이언트가 ingest_readings로 넣은 데이터와 analyze_with_claude 결과가
대시보드에 그대로 보이고, 대시보드에서 본 알람 상태를 MCP tool로 조회할 수 있다.

    anomaly-dashboard                      # 시뮬레이터 포함 (데모)
    anomaly-dashboard --no-simulator       # 실데이터만 (수집기가 ingest)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from . import server as srv
from .chat import ChatAgent, new_conversation_id
from .monitor import PROFILE, Monitor
from .simulator import Simulator

STATIC = Path(__file__).parent / "static"
SPARK_POINTS = 120

mcp = srv.mcp
store = srv.store
monitor = Monitor(store, srv.check_sensor,
                  window=int(os.getenv("MONITOR_WINDOW", "300")),
                  interval=float(os.getenv("MONITOR_INTERVAL", "2")))
simulator: Simulator | None = None
chat = ChatAgent()   # server_urls는 main()에서 실제 바인드 주소로 채운다


def _json(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _status_counts(snapshot: dict) -> dict:
    counts = {"OK": 0, "WARNING": 0, "ALARM": 0}
    for st in snapshot["sensors"].values():
        counts[st["status"]] += 1
    return counts


# ------------------------------------------------------------------ dashboard-only MCP tools

@mcp.tool()
def get_monitor_status() -> dict:
    """상시 모니터링 현황: 센서별 OK/WARNING/ALARM 상태, 사유, 설비별 다변량 이상 여부.
    대시보드에 표시되는 것과 같은 상태입니다."""
    snap = monitor.snapshot()
    return {"summary": _status_counts(snap), **snap, "window": monitor.window}


@mcp.tool()
def list_alarm_events(limit: int = 30, min_status: str = "WARNING") -> list[dict]:
    """최근 알람 이벤트(상태 전이) 목록. 최신순.
    min_status: OK(전체) | WARNING | ALARM — 이 등급 이상이 관련된 이벤트만 반환"""
    return monitor.list_events(limit, min_status)


@mcp.tool()
def simulate_fault(sensor_id: str, kind: str, magnitude: float | None = None, duration: int | None = None) -> dict:
    """[데모] 시뮬레이터 센서에 이상 패턴을 주입합니다.
    kind: spike | drift | shift | stuck | noise
    magnitude: 크기 (센서 단위, noise는 배수). 생략 시 자동
    duration: 지속 샘플 수. 생략 시 자동"""
    if simulator is None:
        return {"error": "시뮬레이터가 꺼져 있습니다 (--no-simulator)."}
    try:
        return {"ok": True, **simulator.inject_fault(sensor_id, kind, magnitude, duration)}
    except (KeyError, ValueError) as e:
        return {"error": str(e)}


# ------------------------------------------------------------------ REST API

@mcp.custom_route("/", methods=["GET"])
async def index(_: Request):
    return FileResponse(STATIC / "index.html")


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request):
    return _json({"ok": True})


@mcp.custom_route("/api/overview", methods=["GET"])
async def overview(_: Request):
    snap = monitor.snapshot()
    sensors = []
    for meta in store.list():
        sid = meta["sensor_id"]
        _, vals, ts = store.get(sid, SPARK_POINTS)
        st = snap["sensors"].get(sid, {"status": "OK", "reasons": [], "since": None, "flagged": 0})
        sensors.append({
            **meta, **st,
            "last_value": vals[-1] if vals else None,
            "last_time": ts[-1] if ts else None,
            "spark": vals,
        })
    return _json({
        "summary": _status_counts(snap),
        "sensors": sensors,
        "equipment": snap["equipment"],
        "events": monitor.list_events(60),
        "simulator": simulator.state() if simulator else None,
        "claude": {"available": srv.analyzer.available, "model": srv.analyzer.model},
        "chat": {"available": chat.available and bool(chat.server_urls), "model": chat.model,
                 "mcp_servers": chat.server_urls},
        "analyses": [{k: a.get(k) for k in ("time", "sensor_id")} | {"severity": a["analysis"].get("severity")}
                     for a in list(srv.analysis_log)[:10]],
        "window": monitor.window,
    })


@mcp.custom_route("/api/sensors/{sensor_id}", methods=["GET"])
async def sensor_detail(request: Request):
    sid = request.path_params["sensor_id"]
    last_n = int(request.query_params.get("last_n", monitor.window))
    try:
        meta, vals, ts = store.get(sid, last_n)
    except KeyError as e:
        return _json({"error": str(e)}, 404)
    if len(vals) < 3:
        return _json({"sensor": vars(meta), "values": vals, "timestamps": ts, "check": None})
    check = srv.check_sensor(
        sid, methods=list(PROFILE), last_n=last_n,
        thresholds={m: t for m, (t, _) in PROFILE.items()},
        params={m: p for m, (_, p) in PROFILE.items()},
    )
    analysis = next((a for a in srv.analysis_log if a.get("sensor_id") == sid), None)
    return _json({
        "sensor": vars(meta), "values": vals, "timestamps": ts, "check": check,
        "monitor": monitor.snapshot()["sensors"].get(sid),
        "analysis": analysis,
    })


@mcp.custom_route("/api/sensors/{sensor_id}/analyze", methods=["POST"])
async def sensor_analyze(request: Request):
    sid = request.path_params["sensor_id"]
    body = await request.json() if await request.body() else {}
    res = await run_in_threadpool(srv.analyze_with_claude, sid, None, body.get("context", ""), monitor.window)
    return _json(res, 400 if "error" in res else 200)


@mcp.custom_route("/api/simulator", methods=["POST"])
async def simulator_control(request: Request):
    if simulator is None:
        return _json({"error": "simulator disabled"}, 400)
    body = await request.json()
    action = body.get("action")
    if action == "start":
        simulator.start()
    elif action == "pause":
        simulator.pause()
    elif action == "clear":
        simulator.clear_faults()
    elif action == "auto":
        every = body.get("auto_fault_every")
        simulator.auto_fault_every = float(every) if every else None
    elif action == "fault":
        try:
            simulator.inject_fault(body["sensor_id"], body["kind"], body.get("magnitude"), body.get("duration"))
        except (KeyError, ValueError) as e:
            return _json({"error": str(e)}, 400)
    else:
        return _json({"error": f"unknown action '{action}'"}, 400)
    return _json(simulator.state())


def _chat_context(selected: str | None) -> str:
    """채팅 질문에 붙이는 대시보드 현재 상태 요약 (자세한 건 Claude가 tool로 조회)."""
    snap = monitor.snapshot()
    lines = [f"현재 시각: {datetime.now().isoformat(timespec='seconds')}",
             f"상태 요약: {_status_counts(snap)} (평가 구간 최근 {monitor.window} 샘플)"]
    abnormal = [f"{sid}={st['status']}" for sid, st in snap["sensors"].items() if st["status"] != "OK"]
    if abnormal:
        lines.append("이상 센서: " + ", ".join(abnormal))
    if selected:
        lines.append(f"사용자가 대시보드에서 보고 있는 센서: {selected}")
    return "\n".join(lines)


@mcp.custom_route("/api/chat", methods=["POST"])
async def chat_send(request: Request):
    body = await request.json()
    question = (body.get("message") or "").strip()
    if not question:
        return _json({"error": "message가 비어 있습니다."}, 400)
    if not chat.available:
        return _json({"error": "ANTHROPIC_API_KEY가 설정되지 않았습니다."}, 400)
    cid = body.get("conversation_id") or new_conversation_id()
    context = _chat_context(body.get("selected_sensor"))

    async def events():
        yield f"data: {json.dumps({'type': 'start', 'conversation_id': cid})}\n\n"
        try:
            async for ev in chat.run(cid, question, context):
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        except Exception as e:  # API/MCP 오류를 채팅창에 표시
            # MCP 클라이언트(anyio TaskGroup)가 감싼 ExceptionGroup을 풀어 실제 원인을 보여준다
            while len(getattr(e, "exceptions", ())) == 1:
                e = e.exceptions[0]
            body_err = (getattr(e, "body", None) or {}).get("error", {}) if isinstance(getattr(e, "body", None), dict) else {}
            detail = body_err.get("message") or str(e)
            msg = {"type": "error", "message": f"{type(e).__name__}: {detail}"}
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@mcp.custom_route("/api/chat/reset", methods=["POST"])
async def chat_reset(request: Request):
    body = await request.json()
    if body.get("conversation_id"):
        chat.reset(body["conversation_id"])
    return _json({"ok": True})


# ------------------------------------------------------------------ app

def create_app(host: str = "127.0.0.1"):
    try:  # MCP SDK 2.x
        return mcp.streamable_http_app(host=host)
    except TypeError:  # 1.x: host/transport security는 settings로 설정
        mcp.settings.host = host
        return mcp.streamable_http_app()


def main() -> None:
    global simulator
    ap = argparse.ArgumentParser(description="Anomaly detection dashboard + MCP (Streamable HTTP) server")
    ap.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8000")))
    ap.add_argument("--no-simulator", action="store_true", help="데모 시뮬레이터 끄기 (실데이터 운영)")
    ap.add_argument("--auto-fault", type=float, default=150, help="자동 고장 주입 주기(초). 0이면 끔")
    args = ap.parse_args()

    if not args.no_simulator:
        simulator = Simulator(store, auto_fault_every=args.auto_fault or None)
        simulator.backfill(monitor.window * 2)
        simulator.start()
    monitor.start()

    # 채팅은 이 서버의 /mcp에 MCP 클라이언트로 접속한다 (+ CHAT_MCP_SERVERS의 외부 서버)
    self_host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    chat.server_urls = [f"http://{self_host}:{args.port}/mcp"] + [
        u.strip() for u in os.getenv("CHAT_MCP_SERVERS", "").split(",") if u.strip()]

    import uvicorn
    print(f"  Dashboard : http://{args.host}:{args.port}/")
    print(f"  MCP       : http://{args.host}:{args.port}/mcp")
    uvicorn.run(create_app(args.host), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
