import numpy as np
import pytest

from anomaly_mcp import detectors as det

rng = np.random.default_rng(0)
BASE = (10 + rng.normal(0, 0.1, 200)).tolist()


def with_spike(i=100, size=5.0):
    v = list(BASE)
    v[i] += size
    return v


@pytest.mark.parametrize("method", ["zscore", "mad", "iqr", "rolling_zscore"])
def test_point_detectors_find_spike(method):
    idx = {a.index for a in det.detect(with_spike(), method)}
    assert 100 in idx


def test_clean_series_has_few_alarms():
    assert len(det.detect(BASE, "zscore")) <= 2


def test_cusum_finds_drift():
    v = np.array(BASE)
    v[120:] += np.linspace(0, 1.0, 80)
    anoms = det.detect(v.tolist(), "cusum")
    assert anoms and min(a.index for a in anoms) > 100


def test_ewma_finds_shift():
    v = np.array(BASE)
    v[150:] += 0.3
    assert any(a.index >= 150 for a in det.detect(v.tolist(), "ewma"))


def test_stuck():
    v = list(BASE)
    v[50:70] = [v[50]] * 20
    anoms = det.detect(v, "stuck")
    assert len(anoms) == 1 and anoms[0].index == 50 and anoms[0].score == 20


def test_spec_limits():
    anoms = det.spec_limits([1, 5, 10], lsl=2, usl=8)
    assert [a.index for a in anoms] == [0, 2]


def test_mahalanobis_correlation_break():
    x = rng.normal(0, 1, 300)
    y = 2 * x + rng.normal(0, 0.05, 300)
    y[250] = -2 * x[250]  # 개별 범위는 정상, 관계만 붕괴
    idx = {a.index for a in det.mahalanobis(np.column_stack([x, y]), warmup=200)}
    assert 250 in idx


def test_unknown_method():
    with pytest.raises(ValueError):
        det.detect(BASE, "nope")
