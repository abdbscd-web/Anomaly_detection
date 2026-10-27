import pytest
from starlette.testclient import TestClient

from anomaly_mcp import dashboard as dash
from anomaly_mcp import server as srv
from anomaly_mcp.monitor import Monitor
from anomaly_mcp.simulator import Simulator
from anomaly_mcp.store import SensorStore

TEMP = "ETCH03_CHA_TEMP"


@pytest.fixture
def sim_env():
    store = SensorStore()
    sim = Simulator(store, auto_fault_every=None, seed=7)
    sim.backfill(300)
    return store, sim


def _check_on(store):
    def check(sensor_id, **kw):
        orig = srv.store
        srv.store = store
        try:
            return srv.check_sensor(sensor_id, **kw)
        finally:
            srv.store = orig
    return check


def test_clean_simulation_stays_ok(sim_env):
    store, sim = sim_env
    mon = Monitor(store, _check_on(store))
    mon.evaluate()
    assert all(s["status"] == "OK" for s in mon.snapshot()["sensors"].values())


@pytest.mark.parametrize("kind", ["spike", "drift", "shift", "stuck", "noise"])
def test_injected_faults_are_detected(sim_env, kind):
    store, sim = sim_env
    mon = Monitor(store, _check_on(store))
    sim.inject_fault(TEMP, kind)
    for _ in range(200):
        sim.step()
        mon.evaluate()
        if mon.snapshot()["sensors"][TEMP]["status"] != "OK":
            break
    assert mon.snapshot()["sensors"][TEMP]["status"] != "OK"
    assert any(e["target"] == TEMP for e in mon.list_events(min_status="WARNING"))


def test_drift_ends_in_spec_alarm(sim_env):
    store, sim = sim_env
    mon = Monitor(store, _check_on(store))
    sim.inject_fault(TEMP, "drift", magnitude=5.0, duration=60)
    for _ in range(60):
        sim.step()
    mon.evaluate()
    assert mon.snapshot()["sensors"][TEMP]["status"] == "ALARM"


def test_multivariate_detects_correlation_break(sim_env):
    store, sim = sim_env
    mon = Monitor(store, _check_on(store))
    # 가스 유량은 그대로인데 압력만 이동 → 개별로는 작아도 상관관계가 깨짐
    sim.inject_fault("ETCH03_CHA_PRES", "shift", magnitude=0.6, duration=40)
    for _ in range(30):
        sim.step()
    mon.evaluate()
    eq = mon.snapshot()["equipment"]["ETCH-03/CH-A"]
    assert eq["status"] == "WARNING"
    assert eq["top_contributors"][0] == "ETCH03_CHA_PRES"


@pytest.fixture
def client(monkeypatch):
    store = SensorStore()
    monkeypatch.setattr(srv, "store", store)
    monkeypatch.setattr(dash, "store", store)
    sim = Simulator(store, auto_fault_every=None, seed=1)
    sim.backfill(300)
    monkeypatch.setattr(dash, "simulator", sim)
    monkeypatch.setattr(dash, "monitor", Monitor(store, srv.check_sensor))
    dash.monitor.evaluate()
    return TestClient(dash.create_app())   # lifespan 없이 REST 라우트만 테스트


def test_overview_and_detail(client):
    o = client.get("/api/overview").json()
    assert o["summary"]["OK"] == 8 and len(o["sensors"]) == 8
    d = client.get(f"/api/sensors/{TEMP}").json()
    assert len(d["values"]) == 300 and d["check"]["status"] == "OK"
    assert client.get("/api/sensors/NOPE").status_code == 404
    assert client.get("/").status_code == 200


def test_simulator_control(client):
    r = client.post("/api/simulator", json={"action": "fault", "sensor_id": TEMP, "kind": "stuck"})
    assert r.json()["active_faults"][TEMP]["kind"] == "stuck"
    assert client.post("/api/simulator", json={"action": "fault", "sensor_id": TEMP, "kind": "bad"}).status_code == 400
    assert client.post("/api/simulator", json={"action": "clear"}).json()["active_faults"] == {}


def test_analyze_endpoint_records_result(client, monkeypatch):
    fake = {"severity": "high", "summary": "s", "pattern": "drift", "likely_sensor_fault": False,
            "root_causes": [], "recommended_actions": []}
    monkeypatch.setattr(srv.analyzer, "api_key", "test")
    monkeypatch.setattr(srv.analyzer, "analyze", lambda payload: fake)
    r = client.post(f"/api/sensors/{TEMP}/analyze", json={"context": "PM 직후"})
    assert r.status_code == 200 and r.json()["analysis"]["severity"] == "high"
    assert client.get(f"/api/sensors/{TEMP}").json()["analysis"]["context"] == "PM 직후"


def test_mcp_tools_registered():
    import asyncio
    names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    assert {"get_monitor_status", "list_alarm_events", "simulate_fault", "check_sensor"} <= names
