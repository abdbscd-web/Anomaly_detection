"""통계 기반 이상 탐지기 모음.

모든 탐지기는 1차원 시계열(list[float])을 받아 Anomaly 리스트를 반환한다.
반도체 공정 센서(온도, 압력, RF Power, 가스 유량 등)를 염두에 두고
spike / drift / stuck(값 고정) 같은 패턴 탐지기도 포함했다.

새 탐지기를 추가하려면:
  1. (values, threshold, **kwargs) -> list[Anomaly] 형태의 함수를 작성
  2. 아래 METHODS 딕셔너리에 등록
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

import numpy as np


@dataclass
class Anomaly:
    index: int
    value: float
    score: float          # 탐지기별 점수 (클수록 이상)
    method: str
    kind: str = "point"   # point | drift | stuck | spec_violation | multivariate
    detail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["value"] = round(float(d["value"]), 6)
        d["score"] = round(float(d["score"]), 4)
        return d


def _arr(values) -> np.ndarray:
    a = np.asarray(values, dtype=float)
    if a.ndim != 1:
        raise ValueError("values must be a 1-D list of numbers")
    if a.size < 3:
        raise ValueError("at least 3 data points are required")
    return a


def _warmup(warmup: int | None, n: int) -> int:
    """기준(정상) 구간 길이. 미지정 시 전체의 1/4 (20~100개)."""
    if warmup is None:
        warmup = min(100, max(20, n // 4))
    return max(3, min(int(warmup), n - 1))


# ---------------------------------------------------------------- point detectors

def zscore(values, threshold: float = 3.0, **_) -> list[Anomaly]:
    """평균/표준편차 기반. 정규분포에 가까운 데이터에 적합."""
    a = _arr(values)
    std = a.std()
    if std == 0:
        return []
    z = np.abs((a - a.mean()) / std)
    return [Anomaly(int(i), a[i], z[i], "zscore") for i in np.where(z > threshold)[0]]


def mad(values, threshold: float = 3.5, **_) -> list[Anomaly]:
    """중앙값/MAD 기반 Robust Z-score. 이상치가 섞인 데이터에 강함."""
    a = _arr(values)
    med = np.median(a)
    m = np.median(np.abs(a - med))
    if m == 0:
        return []
    z = np.abs(0.6745 * (a - med) / m)
    return [Anomaly(int(i), a[i], z[i], "mad") for i in np.where(z > threshold)[0]]


def iqr(values, threshold: float = 1.5, **_) -> list[Anomaly]:
    """사분위 범위 기반 (threshold = IQR 배수, 보통 1.5 또는 3.0)."""
    a = _arr(values)
    q1, q3 = np.percentile(a, [25, 75])
    r = q3 - q1
    if r == 0:
        return []
    lo, hi = q1 - threshold * r, q3 + threshold * r
    out = []
    for i in np.where((a < lo) | (a > hi))[0]:
        dist = (lo - a[i]) if a[i] < lo else (a[i] - hi)
        out.append(Anomaly(int(i), a[i], dist / r, "iqr", detail=f"bounds=[{lo:.4g}, {hi:.4g}]"))
    return out


def rolling_zscore(values, threshold: float = 3.0, window: int = 20, **_) -> list[Anomaly]:
    """직전 window 구간 대비 Z-score. 추세가 있는 시계열의 국소 spike 탐지."""
    a = _arr(values)
    window = max(3, min(int(window), a.size - 1))
    out = []
    for i in range(window, a.size):
        ref = a[i - window:i]
        std = ref.std()
        if std == 0:
            continue
        z = abs(a[i] - ref.mean()) / std
        if z > threshold:
            out.append(Anomaly(i, a[i], z, "rolling_zscore", detail=f"window={window}"))
    return out


def ewma(values, threshold: float = 3.0, alpha: float = 0.2, warmup: int | None = None, **_) -> list[Anomaly]:
    """EWMA 관리도 (SPC). 작은 평균 이동(shift)에 민감."""
    a = _arr(values)
    warmup = _warmup(warmup, a.size)
    mu0, sigma = a[:warmup].mean(), a[:warmup].std(ddof=1)
    if sigma == 0:
        return []
    out, s = [], mu0
    for i in range(a.size):
        s = alpha * a[i] + (1 - alpha) * s
        # 정상상태 EWMA 표준편차 근사
        lim = threshold * sigma * np.sqrt(alpha / (2 - alpha) * (1 - (1 - alpha) ** (2 * (i + 1))))
        if i >= warmup and lim > 0 and abs(s - mu0) > lim:
            out.append(Anomaly(i, a[i], abs(s - mu0) / lim * threshold, "ewma", kind="drift",
                               detail=f"ewma={s:.4g}, center={mu0:.4g}"))
    return out


# ---------------------------------------------------------------- pattern detectors

def stuck(values, threshold: float = 10, tolerance: float = 0.0, **_) -> list[Anomaly]:
    """센서 값 고정(stuck) 탐지: threshold 개 이상 연속으로 같은 값이면 이상."""
    a = _arr(values)
    min_run = int(threshold)
    out, start = [], 0
    for i in range(1, a.size + 1):
        if i == a.size or abs(a[i] - a[start]) > tolerance:
            run = i - start
            if run >= min_run:
                out.append(Anomaly(start, a[start], run, "stuck", kind="stuck",
                                   detail=f"same value for {run} samples (idx {start}-{i - 1})"))
            start = i
    return out


def cusum(values, threshold: float = 8.0, k: float = 0.5, warmup: int | None = None, **_) -> list[Anomaly]:
    """CUSUM: 누적합 기반 평균 drift 탐지. threshold=h(σ 단위), k=허용 편차(σ 단위)."""
    a = _arr(values)
    warmup = _warmup(warmup, a.size)
    mu0, sigma = a[:warmup].mean(), a[:warmup].std(ddof=1)
    if sigma == 0:
        return []
    z = (a - mu0) / sigma
    out, sp, sn = [], 0.0, 0.0
    for i in range(a.size):
        sp = max(0.0, sp + z[i] - k)
        sn = max(0.0, sn - z[i] - k)
        if sp > threshold or sn > threshold:
            direction = "up" if sp > threshold else "down"
            out.append(Anomaly(i, a[i], max(sp, sn), "cusum", kind="drift",
                               detail=f"mean shift {direction}"))
            sp = sn = 0.0  # 알람 후 리셋
    return out


# ---------------------------------------------------------------- spec / multivariate

def spec_limits(values, lsl: float | None = None, usl: float | None = None) -> list[Anomaly]:
    """공정 스펙(LSL/USL) 이탈 체크."""
    a = np.asarray(values, dtype=float)
    out = []
    for i, v in enumerate(a):
        if usl is not None and v > usl:
            out.append(Anomaly(i, v, v - usl, "spec", kind="spec_violation", detail=f"> USL {usl}"))
        elif lsl is not None and v < lsl:
            out.append(Anomaly(i, v, lsl - v, "spec", kind="spec_violation", detail=f"< LSL {lsl}"))
    return out


def mahalanobis(matrix, threshold: float = 3.5, warmup: int | None = None) -> list[Anomaly]:
    """다변량(여러 센서 동시) 이상 탐지. matrix: (n_samples, n_sensors).

    개별 센서는 정상 범위여도 센서 간 상관관계가 깨지면 탐지된다
    (예: RF Power는 그대로인데 챔버 온도만 오르는 경우).
    """
    X = np.asarray(matrix, dtype=float)
    if X.ndim != 2 or X.shape[0] < X.shape[1] + 2:
        raise ValueError("matrix must be (n_samples, n_sensors) with n_samples > n_sensors + 1")
    ref = X[:warmup] if warmup else X
    mu = ref.mean(axis=0)
    cov = np.cov(ref, rowvar=False) + np.eye(X.shape[1]) * 1e-9
    inv = np.linalg.pinv(cov)
    diff = X - mu
    d = np.sqrt(np.einsum("ij,jk,ik->i", diff, inv, diff))
    out = []
    for i in np.where(d > threshold)[0]:
        contrib = np.abs(diff[i] * (inv @ diff[i]))
        top = int(np.argmax(contrib))
        out.append(Anomaly(int(i), float(d[i]), d[i], "mahalanobis", kind="multivariate",
                           detail=f"top_contributor_col={top}"))
    return out


METHODS: dict[str, tuple[Callable[..., list[Anomaly]], float, str]] = {
    # name: (function, default threshold, description)
    "zscore": (zscore, 3.0, "전체 평균/표준편차 기반 Z-score"),
    "mad": (mad, 3.5, "중앙값/MAD 기반 Robust Z-score (이상치에 강함)"),
    "iqr": (iqr, 1.5, "사분위 범위(IQR) 기반"),
    "rolling_zscore": (rolling_zscore, 3.0, "이동 구간 Z-score (국소 spike), kwargs: window"),
    "ewma": (ewma, 3.0, "EWMA 관리도 (작은 평균 이동), kwargs: alpha, warmup(기준구간)"),
    "cusum": (cusum, 8.0, "CUSUM 누적합 (점진적 drift), kwargs: k, warmup(기준구간)"),
    "stuck": (stuck, 10, "값 고정 센서 탐지 (threshold=최소 연속 개수), kwargs: tolerance"),
}


def detect(values, method: str = "zscore", threshold: float | None = None, **kwargs) -> list[Anomaly]:
    if method not in METHODS:
        raise ValueError(f"unknown method '{method}'. available: {', '.join(METHODS)}")
    fn, default, _ = METHODS[method]
    return fn(values, threshold=default if threshold is None else threshold, **kwargs)


def summarize(values) -> dict:
    a = np.asarray(values, dtype=float)
    return {
        "count": int(a.size),
        "mean": round(float(a.mean()), 6),
        "std": round(float(a.std()), 6),
        "min": round(float(a.min()), 6),
        "max": round(float(a.max()), 6),
        "median": round(float(np.median(a)), 6),
    }
