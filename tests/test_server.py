from types import SimpleNamespace

from anomaly_mcp import server as s


def test_ingest_and_check():
    s.store.clear("T1")
    s.register_sensor("T1", unit="C", lsl=0, usl=100)
    vals = [50.0 + (i % 5) * 0.1 for i in range(100)]
    vals[60] = 150.0
    s.ingest_readings("T1", vals)
    r = s.check_sensor("T1")
    assert r["status"] == "ALARM"
    assert r["spec_violations"][0]["index"] == 60


def test_analyze_with_mock_claude(monkeypatch):
    s.store.clear("T2")
    s.ingest_readings("T2", [1.0, 1.1, 0.9, 1.0, 5.0, 1.0, 1.05, 0.95, 1.0, 1.0] * 3)

    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            block = SimpleNamespace(type="text", text='{"severity": "medium", "summary": "ok"}')
            return SimpleNamespace(content=[block], stop_reason="end_turn", model=kw["model"],
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=5))

    monkeypatch.setattr(s.analyzer, "api_key", "test")
    monkeypatch.setattr(s.analyzer, "_client", SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))

    r = s.analyze_with_claude("T2", context="PM 직후")
    assert r["analysis"]["severity"] == "medium"
    assert "tool_choice" not in captured  # Opus 5.5는 강제 tool 호출을 400으로 거부
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert "PM 직후" in captured["messages"][0]["content"]


def test_analyze_without_key(monkeypatch):
    monkeypatch.setattr(s.analyzer, "api_key", None)
    assert "error" in s.analyze_with_claude(values=[1, 2, 3, 4])
