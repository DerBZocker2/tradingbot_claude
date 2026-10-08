from fastapi.testclient import TestClient

from conftest import make_cfg
from tradebot.api import create_app


def test_dashboard_api_smoke_and_auth(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("TRADEBOT_DASHBOARD_TOKEN", "S3cr3t-T0ken-XYZ")
    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        s = c.get("/api/status").json()
        assert s["mode"] == "PAPER" and s["synthetic_data"] and s["mcp"]["order_execution_in_dashboard"] is False
        assert c.post("/api/cycle").status_code == 401
        assert c.post("/api/cycle", headers={"X-Dashboard-Token": "S3cr3t-T0ken-XYZ"}).status_code == 200
        assert c.post("/api/mode", json={"mode": "LIVE_APPROVAL"}, headers={"X-Dashboard-Token": "S3cr3t-T0ken-XYZ"}).status_code == 409
        assert c.post("/api/kill", json={"on": True}, headers={"X-Dashboard-Token": "S3cr3t-T0ken-XYZ"}).json()["kill_switch"] is True
        r = c.post("/api/backtest", json={"symbol": "AAA"}, headers={"X-Dashboard-Token": "S3cr3t-T0ken-XYZ"})
        assert r.status_code == 200 and r.json()["unvalidated"]
        assert len(c.get("/api/chart/AAA").json()["dates"]) > 100
        assert c.get("/").status_code == 200
        assert "S3cr3t-T0ken-XYZ" not in c.get("/api/status").text and "S3cr3t-T0ken-XYZ" not in c.get("/api/events").text
