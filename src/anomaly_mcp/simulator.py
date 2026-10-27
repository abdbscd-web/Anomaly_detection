"""데모용 실시간 반도체 설비 센서 시뮬레이터.

실제 FDC/센서 수집기가 없을 때 대시보드와 MCP 서버를 시연하기 위한 데이터 소스다.
운영 환경에서는 이 모듈 대신 수집기가 store.ingest()를 호출하면 된다.

센서 간 물리적 결합을 흉내 낸다 (다변량 탐지 시연용):
  - 챔버 온도는 RF Power를 따라 움직인다
  - 챔버 압력은 가스 유량을 따라 움직인다

inject_fault()로 이상 패턴을 주입할 수 있다:
  spike  : 단발성 튐                       (mad, rolling_zscore)
  drift  : 서서히 증가 → 스펙 이탈           (cusum, ewma, spec)
  shift  : 계단형 평균 이동                  (cusum, multivariate)
  stuck  : 값 고정                          (stuck)
  noise  : 노이즈 증가 (접촉 불량, 아킹 등)   (mad, rolling_zscore)
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .store import SensorMeta, SensorStore


@dataclass
class SimSensor:
    meta: SensorMeta
    nominal: float
    noise: float                     # 센서 고유 노이즈 (σ)
    follows: str | None = None       # 결합된 상위 센서 ID
    gain: float = 0.0                # 상위 센서 편차 → 이 센서 편차 계수


SENSORS = [
    SimSensor(SensorMeta("ETCH03_CHA_RF_PWR", "W", "ETCH-03", "CH-A", 1150, 1250, "Source RF forward power"), 1200, 4.0),
    SimSensor(SensorMeta("ETCH03_CHA_TEMP", "°C", "ETCH-03", "CH-A", 60, 68, "Chamber wall temperature"), 65, 0.08,
              follows="ETCH03_CHA_RF_PWR", gain=0.03),
    SimSensor(SensorMeta("ETCH03_CHA_GAS_CF4", "sccm", "ETCH-03", "CH-A", 48, 52, "CF4 mass flow"), 50, 0.2),
    SimSensor(SensorMeta("ETCH03_CHA_PRES", "mTorr", "ETCH-03", "CH-A", 28.5, 31.5, "Chamber pressure"), 30, 0.06,
              follows="ETCH03_CHA_GAS_CF4", gain=0.8),
    SimSensor(SensorMeta("ETCH03_CHA_ESC_HE", "Torr", "ETCH-03", "CH-A", 9.6, 10.4, "ESC backside He pressure"), 10, 0.04),
    SimSensor(SensorMeta("CVD01_CHB_HTR_TEMP", "°C", "CVD-01", "CH-B", 395, 405, "Heater temperature"), 400, 0.5),
    SimSensor(SensorMeta("CVD01_CHB_SIH4", "sccm", "CVD-01", "CH-B", 195, 205, "SiH4 mass flow"), 200, 0.8),
    SimSensor(SensorMeta("CVD01_CHB_PRES", "Torr", "CVD-01", "CH-B", 4.85, 5.15, "Chamber pressure"), 5.0, 0.01,
              follows="CVD01_CHB_SIH4", gain=0.01),
]

FAULT_KINDS = ("spike", "drift", "shift", "stuck", "noise")
DEFAULT_DURATION = {"spike": 1, "drift": 180, "shift": 120, "stuck": 40, "noise": 60}


@dataclass
class _Fault:
    kind: str
    magnitude: float
    duration: int
    elapsed: int = 0
    held: float | None = None   # stuck 값


class Simulator:
    def __init__(self, store: SensorStore, interval: float = 1.0, auto_fault_every: float | None = 150,
                 seed: int | None = None):
        self.store = store
        self.interval = interval
        self.auto_fault_every = auto_fault_every   # 초 단위. None이면 자동 고장 주입 안 함
        self.sensors = {s.meta.sensor_id: s for s in SENSORS}
        self.rng = np.random.default_rng(seed)
        self._pyrng = random.Random(seed)
        self.faults: dict[str, _Fault] = {}
        self.fault_log: list[dict] = []
        self.running = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._clock: datetime | None = None
        self._since_auto = 0.0
        self._last: dict[str, float] = {}
        for s in SENSORS:
            store.register(s.meta)

    # -------------------------------------------------------------- data generation

    def _total_std(self, sid: str) -> float:
        s = self.sensors[sid]
        parent = self._total_std(s.follows) if s.follows else 0.0
        return float(np.hypot(s.noise, s.gain * parent))

    def _sample(self) -> dict[str, float]:
        """한 시점의 모든 센서 값을 생성 (상위 센서부터 계산)."""
        dev: dict[str, float] = {}   # 정상 편차 (결합 전파용)
        out: dict[str, float] = {}
        for sid, s in self.sensors.items():
            d = self.rng.normal(0, s.noise)
            if s.follows:
                d += s.gain * dev[s.follows]
            dev[sid] = d
            out[sid] = s.nominal + d
        with self._lock:
            for sid, f in list(self.faults.items()):
                out[sid] = self._apply(sid, f, out[sid])
                f.elapsed += 1
                if f.elapsed >= f.duration:
                    del self.faults[sid]
        self._last = out
        return out

    def _apply(self, sid: str, f: _Fault, v: float) -> float:
        if f.kind == "spike":
            return v + f.magnitude
        if f.kind == "drift":
            return v + f.magnitude * (f.elapsed + 1) / f.duration
        if f.kind == "shift":
            return v + f.magnitude
        if f.kind == "stuck":
            if f.held is None:
                f.held = self._last.get(sid, v)
            return f.held
        if f.kind == "noise":
            return v + self.rng.normal(0, self._total_std(sid) * f.magnitude)
        return v

    def _emit(self, n: int) -> None:
        values: dict[str, list[float]] = {sid: [] for sid in self.sensors}
        ts: list[str] = []
        for _ in range(n):
            self._clock += timedelta(seconds=self.interval)
            ts.append(self._clock.isoformat(timespec="seconds"))
            for sid, v in self._sample().items():
                values[sid].append(round(v, 5))
        for sid, vals in values.items():
            self.store.ingest(sid, vals, ts)

    def backfill(self, n: int = 600) -> None:
        """과거 n 샘플을 미리 채워 대시보드가 비어 있지 않도록 한다."""
        self._clock = datetime.now().replace(microsecond=0) - timedelta(seconds=n * self.interval)
        self._emit(n)

    def step(self) -> None:
        if self._clock is None:
            self._clock = datetime.now().replace(microsecond=0)
        self._emit(1)
        if self.auto_fault_every:
            self._since_auto += self.interval
            if self._since_auto >= self.auto_fault_every:
                self._since_auto = 0.0
                idle = [sid for sid in self.sensors if sid not in self.faults]
                if idle:
                    self.inject_fault(self._pyrng.choice(idle), self._pyrng.choice(FAULT_KINDS), source="auto")

    # -------------------------------------------------------------- faults

    def inject_fault(self, sensor_id: str, kind: str, magnitude: float | None = None,
                     duration: int | None = None, source: str = "manual") -> dict:
        if sensor_id not in self.sensors:
            raise KeyError(f"simulated sensor '{sensor_id}' not found")
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind '{kind}'. available: {', '.join(FAULT_KINDS)}")
        s, sd = self.sensors[sensor_id], self._total_std(sensor_id)
        meta = s.meta
        if magnitude is None:
            sign = self._pyrng.choice((-1, 1))
            magnitude = {
                "spike": 8 * sd * sign,
                # 스펙 한계를 20% 넘어서는 지점까지 서서히 이동
                "drift": ((meta.usl - s.nominal) if sign > 0 else (meta.lsl - s.nominal)) * 1.2,
                "shift": 4 * sd * sign,
                "stuck": 0.0,
                "noise": 5.0,   # 노이즈 배수
            }[kind]
        duration = int(duration or DEFAULT_DURATION[kind])
        with self._lock:
            self.faults[sensor_id] = _Fault(kind, float(magnitude), duration)
        entry = {
            "time": (self._clock or datetime.now()).isoformat(timespec="seconds"),
            "sensor_id": sensor_id, "kind": kind, "magnitude": round(float(magnitude), 5),
            "duration": duration, "source": source,
        }
        self.fault_log.append(entry)
        del self.fault_log[:-200]
        return entry

    def clear_faults(self) -> None:
        with self._lock:
            self.faults.clear()

    def state(self) -> dict:
        with self._lock:
            active = {sid: {"kind": f.kind, "elapsed": f.elapsed, "duration": f.duration}
                      for sid, f in self.faults.items()}
        return {
            "running": self.running,
            "interval": self.interval,
            "auto_fault_every": self.auto_fault_every,
            "active_faults": active,
            "fault_log": self.fault_log[-20:][::-1],
            "fault_kinds": list(FAULT_KINDS),
        }

    # -------------------------------------------------------------- thread

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if self.running:
                self.step()

    def start(self) -> None:
        self.running = True
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="sensor-simulator", daemon=True)
            self._thread.start()

    def pause(self) -> None:
        self.running = False

    def shutdown(self) -> None:
        self._stop.set()
