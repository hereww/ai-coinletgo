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
        assert body["entries_enabled"] is True
        assert body["halt_reason"] is None
        assert body["health"]["ready"] is False

        integrations = client.get("/api/v1/integrations")
        assert integrations.status_code == 200
        assert integrations.json()["binance"]["environment"] == "testnet"
        assert integrations.json()["binance"]["configured"] is False
        assert integrations.json()["model"]["api_key_configured"] is False
        assert integrations.json()["proxy"]["enabled"] is False
        assert integrations.json()["proxy"]["configured"] is False

        config = client.get("/api/v1/config")
        assert config.status_code == 200
        assert config.json()["strategy_profile"] == "trend_following"

        resume = client.post(
            "/api/v1/actions/resume-testnet",
            json={"password": ""},
        )
        assert resume.status_code == 200
        assert resume.json()["mode"] == "TESTNET"

        run_cycle = client.post(
            "/api/v1/actions/run-cycle",
            json={"password": ""},
        )
        assert run_cycle.status_code == 409
        assert "Binance credentials" in run_cycle.json()["detail"]


def test_live_unlock_is_rejected_in_testnet_runtime() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/actions/unlock-live",
            json={"password": ""},
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


def test_scan_interval_accepts_the_supported_cadence_set() -> None:
    with TestClient(app) as client:
        for interval in (5, 15, 30, 60):
            accepted = client.patch("/api/v1/config", json={"scan_interval_minutes": interval})
            assert accepted.status_code == 200
            assert accepted.json()["scan_interval_minutes"] == interval
        for interval in (4, 10, 120, 121):
            assert (
                client.patch("/api/v1/config", json={"scan_interval_minutes": interval}).status_code
                == 422
            )


def test_factor_catalog_exposes_research_only_data_boundaries() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/factors/catalog")

    assert response.status_code == 200
    payload = response.json()
    assert payload["live_trading_connected"] is False
    assert payload["market_source"] == "Binance USD-M production public market data"
    assert len(payload["factors"]) == 12
    sources = {item["key"]: item for item in payload["data_sources"]}
    assert sources["funding"]["available"] is True
    assert sources["open_interest"]["available"] is False
    assert sources["basis"]["available"] is False
    assert sources["order_book"]["available"] is False

    shadow = client.get("/api/v1/factors/shadow")
    assert shadow.status_code == 200
    assert shadow.json() == []


def test_factor_research_endpoint_passes_validated_parameters_to_service() -> None:
    captured: dict[str, object] = {}

    class StubFactorResearch:
        async def create(self, parameters: dict[str, object]) -> str:
            captured.update(parameters)
            return "factor-run-1"

        async def execute(self, run_id: str, parameters: dict[str, object]) -> None:
            captured["executed_run_id"] = run_id
            captured["executed_parameters"] = parameters

    with TestClient(app) as client:
        app.state.factor_research_service = StubFactorResearch()
        response = client.post(
            "/api/v1/factors/research",
            json={
                "symbols": ["btcusdt", "ETHUSDT", "SOLUSDT"],
                "start_date": "2025-01-01",
                "end_date": "2025-02-01",
                "interval": "4h",
                "forward_bars": 6,
                "rebalance_bars": 6,
            },
        )

    assert response.status_code == 202
    assert response.json()["id"] == "factor-run-1"
    assert response.json()["status"] == "QUEUED"
    assert captured["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert captured["interval"] == "4h"
    assert captured["forward_bars"] == 6
    assert captured["executed_run_id"] == "factor-run-1"
