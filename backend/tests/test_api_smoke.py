import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

database_path = Path(tempfile.gettempdir()) / f"futures-risk-console-api-{os.getpid()}.db"
database_path.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"

from trading_system.main import app  # noqa: E402


def test_api_starts_in_locked_down_unconfigured_state() -> None:
    with TestClient(app) as client:
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["environment"] == "testnet"

        dashboard = client.get("/api/v1/dashboard")
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["metrics"]["equity"] == "0.00"
        assert body["positions"] == []
        assert body["signals"] == []
        assert body["health"]["ready"] is False

        integrations = client.get("/api/v1/integrations")
        assert integrations.status_code == 200
        assert integrations.json()["binance"]["environment"] == "testnet"
        assert integrations.json()["binance"]["configured"] is False
        assert integrations.json()["model"]["api_key_configured"] is False
        assert integrations.json()["proxy"]["enabled"] is False
        assert integrations.json()["proxy"]["configured"] is False

        resume = client.post("/api/v1/actions/resume-testnet")
        assert resume.status_code == 200
        assert resume.json()["mode"] == "TESTNET"

        run_cycle = client.post(
            "/api/v1/actions/run-cycle",
            json={"totp_code": "", "confirmation": "RUN CYCLE"},
        )
        assert run_cycle.status_code == 409
        assert "Binance credentials" in run_cycle.json()["detail"]


def test_live_unlock_is_rejected_in_testnet_runtime() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/actions/unlock-live",
            json={"totp_code": "", "confirmation": "UNLOCK LIVE"},
        )
        assert response.status_code == 409
        assert "not configured for the live" in response.json()["detail"]


def test_risk_configuration_accepts_30x_and_rejects_more() -> None:
    with TestClient(app) as client:
        accepted = client.patch("/api/v1/config", json={"max_leverage": 30})
        assert accepted.status_code == 200
        assert accepted.json()["max_leverage"] == 30
        rejected = client.patch("/api/v1/config", json={"max_leverage": 31})
        assert rejected.status_code == 422
        reset = client.patch("/api/v1/config", json={"max_leverage": 3})
        assert reset.status_code == 200


def test_scan_interval_is_configurable_from_15_to_120_minutes() -> None:
    with TestClient(app) as client:
        accepted = client.patch("/api/v1/config", json={"scan_interval_minutes": 30})
        assert accepted.status_code == 200
        assert accepted.json()["scan_interval_minutes"] == 30
        assert client.patch("/api/v1/config", json={"scan_interval_minutes": 14}).status_code == 422
        too_slow = client.patch("/api/v1/config", json={"scan_interval_minutes": 121})
        assert too_slow.status_code == 422
        reset = client.patch("/api/v1/config", json={"scan_interval_minutes": 15})
        assert reset.status_code == 200
