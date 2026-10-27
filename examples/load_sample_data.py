"""데모용 반도체 식각(Etch) 챔버 센서 데이터 생성 + 서버 기능 실행.

    python examples/load_sample_data.py          # 통계 탐지만
    python examples/load_sample_data.py --claude # Claude 분석까지 (API 키 필요)

생성되는 이상 패턴:
  - CHAMBER_TEMP : 300번째 샘플부터 점진적 drift (히터 열화 시나리오)
  - CHAMBER_PRES : 150번째 spike
  - RF_POWER     : 400~430 구간 값 고정 (센서 stuck)
  - GAS_FLOW     : 450 이후 USL 초과
"""

import json
import sys
from datetime import datetime, timedelta

import numpy as np

from anomaly_mcp import server as s

rng = np.random.default_rng(42)
N = 500
t0 = datetime(2026, 10, 27, 9, 0, 0)
ts = [(t0 + timedelta(seconds=i)).isoformat() for i in range(N)]

temp = 65 + rng.normal(0, 0.15, N)
temp[300:] += np.linspace(0, 2.5, N - 300)

pres = 30 + rng.normal(0, 0.2, N)
pres[150] += 3.0

rf = 1200 + rng.normal(0, 4, N)
rf[400:430] = rf[400]

gas = 50 + rng.normal(0, 0.3, N)
gas[450:] += 2.0

sensors = {
    "CHAMBER_TEMP": (temp, "°C", 60, 68),
    "CHAMBER_PRES": (pres, "mTorr", 27, 33),
    "RF_POWER": (rf, "W", 1150, 1250),
    "GAS_FLOW": (gas, "sccm", 48, 51.5),
}
for sid, (vals, unit, lsl, usl) in sensors.items():
    s.register_sensor(sid, unit=unit, equipment="ETCH-03", chamber="CH-A", lsl=lsl, usl=usl)
    s.ingest_readings(sid, vals.tolist(), ts)

for sid in sensors:
    r = s.check_sensor(sid)
    counts = {m: v.get("anomaly_count") for m, v in r["results"].items()}
    print(f"{sid:13s} status={r['status']:7s} spec={len(r['spec_violations']):3d} {counts}")

mv = s.detect_multivariate(list(sensors), threshold=4.0, warmup=250)
print(f"\nmultivariate anomalies: {mv['anomaly_count']}")

if "--claude" in sys.argv:
    res = s.analyze_with_claude("CHAMBER_TEMP", context="지난주 히터 교체 PM 진행. 레시피 변경 없음.")
    print(json.dumps(res, ensure_ascii=False, indent=2))
