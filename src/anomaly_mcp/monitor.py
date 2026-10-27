"""상시 모니터링: 주기적으로 모든 센서를 평가하고 상태 전이를 알람 이벤트로 기록한다.

대시보드와 MCP 클라이언트가 같은 상태/이벤트를 보도록 서버 프로세스 안에서 돈다.
상시 감시는 데이터가 계속 들어오므로 오탐을 줄이기 위해 대화형 check_sensor보다
보수적인 threshold(PROFILE)를 사용한다.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Callable

from . import detectors as det
from .store import SensorStore

# 상시 감시용 탐지기 설정: method -> (threshold, params)
PROFILE: dict[str, tuple[float, dict]] = {
    "mad": (4.5, {}),
    "rolling_zscore": (5.0, {"window": 30}),
    "cusum": (12.0, {"warmup": 150}),   # 슬라이딩 윈도우 기준선 흔들림 대비 h 상향
    "stuck": (10, {}),
}
MV_THRESHOLD = 6.0
SEVERITY = {"OK": 0, "WARNING": 1, "ALARM": 2}


class Monitor:
    def __init__(self, store: SensorStore, check_fn: Callable[..., dict], window: int = 300,
                 interval: float = 2.0, recent: int = 60):
        self.store = store
        self.check_fn = check_fn
        self.window = window        # 평가에 쓰는 최근 샘플 수
        self.interval = interval    # 평가 주기 (초)
        self.recent = recent        # 다변량 이상은 최근 N 샘플 안에 있을 때만 활성으로 본다
        self.state: dict[str, dict] = {}
        self.equipment: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=500)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- evaluation

    def _check(self, sensor_id: str) -> dict:
        return self.check_fn(
            sensor_id,
            methods=list(PROFILE),
            last_n=self.window,
            thresholds={m: t for m, (t, _) in PROFILE.items()},
            params={m: p for m, (_, p) in PROFILE.items()},
        )

    def _event(self, scope: str, target: str, prev: str, new: str, reasons: list[str], ts) -> None:
        self.events.appendleft({
            "time": ts or datetime.now().isoformat(timespec="seconds"),
            "scope": scope, "target": target, "from": prev, "to": new, "reasons": reasons,
        })

    def evaluate(self) -> None:
        sensors = self.store.list()
        groups: dict[str, list[str]] = {}
        for meta in sensors:
            sid = meta["sensor_id"]
            if meta["points"] < 30:
                continue
            try:
                r = self._check(sid)
            except (KeyError, ValueError):
                continue
            reasons = [f"spec x{len(r['spec_violations'])}"] if r["spec_violations"] else []
            reasons += [f"{m} x{v['anomaly_count']}" for m, v in r["results"].items() if v.get("anomaly_count")]
            _, _, ts = self.store.get(sid, 1)
            with self._lock:
                old = self.state.get(sid, {})
                prev = old.get("status", "OK")
                since = old.get("since") if prev == r["status"] and old.get("since") else ts[-1]
                self.state[sid] = {"status": r["status"], "since": since, "reasons": reasons,
                                   "flagged": r["flagged_indices_total"]}
                if prev != r["status"]:
                    self._event("sensor", sid, prev, r["status"], reasons, ts[-1])
            key = f"{meta['equipment']}/{meta['chamber']}"
            groups.setdefault(key, []).append(sid)

        for key, sids in groups.items():
            self._evaluate_equipment(key, sids)

    def _evaluate_equipment(self, key: str, sids: list[str]) -> None:
        """설비/챔버 단위 다변량 탐지. 앞 절반을 정상 기준 구간으로 사용."""
        if len(sids) < 2:
            return
        series = [self.store.get(s, self.window) for s in sids]
        n = min(len(v) for _, v, _ in series)
        if n < len(sids) + 20:
            return
        matrix = [[series[j][1][-n:][i] for j in range(len(sids))] for i in range(n)]
        ts = series[0][2][-n:]
        try:
            anoms = det.mahalanobis(matrix, MV_THRESHOLD, warmup=n // 2)
        except ValueError:
            return
        active = [a for a in anoms if a.index >= n - self.recent]
        contributors: dict[str, int] = {}
        for a in active:
            col = int(a.detail.split("=")[1])
            contributors[sids[col]] = contributors.get(sids[col], 0) + 1
        status = "WARNING" if active else "OK"
        with self._lock:
            prev = self.equipment.get(key, {}).get("status", "OK")
            self.equipment[key] = {
                "status": status, "sensors": sids, "anomaly_count": len(active),
                "top_contributors": sorted(contributors, key=contributors.get, reverse=True),
                "max_distance": round(max((a.score for a in active), default=0.0), 3),
            }
            if prev != status:
                reasons = [f"mahalanobis x{len(active)}"] + [f"top {c}" for c in self.equipment[key]["top_contributors"][:2]]
                self._event("equipment", key, prev, status, reasons if active else [], ts[-1])

    # -------------------------------------------------------------- queries

    def snapshot(self) -> dict:
        with self._lock:
            return {"sensors": dict(self.state), "equipment": dict(self.equipment)}

    def list_events(self, limit: int = 50, min_status: str = "OK") -> list[dict]:
        floor = SEVERITY.get(min_status, 0)
        with self._lock:
            evs = [e for e in self.events if max(SEVERITY[e["to"]], SEVERITY[e["from"]]) >= floor]
        return evs[:limit]

    # -------------------------------------------------------------- thread

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.evaluate()
            except Exception as e:  # 감시 루프는 죽지 않아야 한다
                print(f"[monitor] evaluate failed: {type(e).__name__}: {e}")

    def start(self) -> None:
        self.evaluate()
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="anomaly-monitor", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
