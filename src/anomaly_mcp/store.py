"""센서 데이터 저장소 (템플릿: 인메모리).

실제 운영에서는 이 클래스를 InfluxDB / TimescaleDB / 사내 FDC 시스템 조회로
교체하면 된다. 서버 코드는 SensorStore 인터페이스만 사용한다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class SensorMeta:
    sensor_id: str
    unit: str = ""
    equipment: str = ""        # 예: ETCH-03
    chamber: str = ""          # 예: CH-A
    lsl: float | None = None   # 하한 스펙
    usl: float | None = None   # 상한 스펙
    description: str = ""


@dataclass
class _Series:
    meta: SensorMeta
    values: deque = field(default_factory=deque)
    timestamps: deque = field(default_factory=deque)


class SensorStore:
    def __init__(self, max_points: int = 10_000):
        self.max_points = max_points
        self._data: dict[str, _Series] = {}
        self._lock = Lock()

    def register(self, meta: SensorMeta) -> None:
        with self._lock:
            if meta.sensor_id in self._data:
                self._data[meta.sensor_id].meta = meta
            else:
                self._data[meta.sensor_id] = _Series(
                    meta, deque(maxlen=self.max_points), deque(maxlen=self.max_points)
                )

    def ingest(self, sensor_id: str, values: list[float], timestamps: list[str] | None = None) -> int:
        if timestamps is not None and len(timestamps) != len(values):
            raise ValueError("timestamps and values must have the same length")
        with self._lock:
            if sensor_id not in self._data:
                self._data[sensor_id] = _Series(
                    SensorMeta(sensor_id), deque(maxlen=self.max_points), deque(maxlen=self.max_points)
                )
            s = self._data[sensor_id]
            s.values.extend(float(v) for v in values)
            s.timestamps.extend(timestamps if timestamps else [None] * len(values))
            return len(s.values)

    def get(self, sensor_id: str, last_n: int | None = None) -> tuple[SensorMeta, list[float], list]:
        with self._lock:
            if sensor_id not in self._data:
                raise KeyError(f"sensor '{sensor_id}' not found")
            s = self._data[sensor_id]
            vals, ts = list(s.values), list(s.timestamps)
        if last_n:
            vals, ts = vals[-last_n:], ts[-last_n:]
        return s.meta, vals, ts

    def list(self) -> list[dict]:
        with self._lock:
            return [
                {**vars(s.meta), "points": len(s.values)} for s in self._data.values()
            ]

    def clear(self, sensor_id: str) -> None:
        with self._lock:
            self._data.pop(sensor_id, None)
