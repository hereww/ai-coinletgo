from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tests.factories import position, snapshot
from trading_system.ai.client import (
    ModelRelayRequestError,
    ModelUnavailableError,
    ResponsesModelClient,
)
from trading_system.config import Settings


def model_settings(tmp_path: object) -> Settings:
    path = tmp_path  # pytest Path typing is intentionally kept out of runtime code
    path.joinpath("model_api_key").write_text("test-key", encoding="utf-8")
    return Settings(
        secret_dir=path,
        model_base_url="https://model.example",
    )


def proxy_model_settings(tmp_path: object) -> Settings:
    path = tmp_path
    path.joinpath("model_api_key").write_text("test-key", encoding="utf-8")
    path.joinpath("http_proxy_url").write_text("http://proxy.example:8080", encoding="utf-8")
    return Settings(
        secret_dir=path,
        model_base_url="https://model.example",
        http_proxy_enabled=True,
    )


def multi_profile_settings(tmp_path: object) -> Settings:
    path = tmp_path
    path.joinpath("model_api_key").write_text("relay-key", encoding="utf-8")
    path.joinpath("vllm_model_api_key").write_text("vllm-key", encoding="utf-8")
    return Settings(
        secret_dir=path,
        model_base_url="https://relay.example/v1",
        model_name="gpt-5.6",
        vllm_model_base_url="http://vllm.example/v1",
        vllm_model_name="Qwen/Qwen3.8-27B-FP8",
    )


def valid_output() -> dict[str, object]:
    return {
        "signals": [],
        "position_reviews": [],
        "market_regime": "UNCERTAIN",
        "summary": "No trade",
    }


@pytest.mark.asyncio
async def test_valid_structured_response_and_sanitized_payload(tmp_path: object) -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": json.dumps(valid_output())})

    client = ResponsesModelClient(
        model_settings(tmp_path),
        httpx.MockTransport(handler),
    )
    try:
        result = await client.analyze(
            [snapshot()], [position()], datetime.now(UTC) + timedelta(minutes=15)
        )
    finally:
        await client.close()

    assert result.market_regime == "UNCERTAIN"
    payload_text = json.dumps(captured[0])
    assert "test-key" not in payload_text
    assert "equity" not in payload_text
    assert "available_balance" not in payload_text
    assert captured[0]["model"] == "gpt-5.6"
    assert captured[0]["store"] is False
    assert "previous_response_id" not in captured[0]
    assert "fc_" not in payload_text
    assert "ctc_" not in payload_text
    system_text = captured[0]["input"][0]["content"][0]["text"]  # type: ignore[index]
    assert "Selected strategy profile: trend_following" in system_text


@pytest.mark.asyncio
async def test_portfolio_prompt_keeps_opportunity_floor_for_aligned_trends(
    tmp_path: object,
) -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        output = {
            "market_regime": "RANGING",
            "portfolio_risk_budget_fraction": 0,
            "allocations": [],
            "summary": "当前无合格组合机会",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        }
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        await client.analyze_portfolio(
            [snapshot(symbol="BTCUSDT")],
            [],
            datetime.now(UTC) + timedelta(minutes=15),
        )
    finally:
        await client.close()

    system_text = captured[0]["input"][0]["content"][0]["text"]  # type: ignore[index]
    assert "机会下限" in system_text
    assert "ADX_1h >= 20" in system_text
    assert "没有同向触发时必须FLAT" in system_text
    assert "entry_range_min_width_abs" in system_text
    assert "不得只把当时的 best_bid、best_ask 原样复制成入场区间" in system_text

    context = json.loads(captured[0]["input"][1]["content"][0]["text"])  # type: ignore[index]
    sent_candidate = context["candidates"][0]
    assert sent_candidate["entry_range_reference_price"] == "100.0"
    assert Decimal(sent_candidate["entry_range_min_width_abs"]) == Decimal("0.20")
    assert Decimal(sent_candidate["stop_distance_min_abs"]) == Decimal("0.800")
    assert Decimal(sent_candidate["stop_distance_max_abs"]) == Decimal("2.500")


@pytest.mark.asyncio
async def test_portfolio_prompt_uses_runtime_reward_risk_floor(tmp_path: object) -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        output = {
            "market_regime": "UNCERTAIN",
            "portfolio_risk_budget_fraction": 0,
            "allocations": [],
            "summary": "当前无合格组合机会",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        }
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    settings = model_settings(tmp_path)
    settings.min_net_reward_risk = 1.8
    client = ResponsesModelClient(settings, transport=httpx.MockTransport(handler))
    try:
        await client.analyze_portfolio([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()

    system_text = captured[0]["input"][0]["content"][0]["text"]  # type: ignore[index]
    assert "最低净盈亏比为 1.8R" in system_text
    context = json.loads(captured[0]["input"][1]["content"][0]["text"])  # type: ignore[index]
    assert context["entry_policy"]["min_net_reward_risk"] == 1.8


@pytest.mark.asyncio
async def test_portfolio_numeric_markdown_markers_are_normalized_without_repair(
    tmp_path: object,
) -> None:
    calls = 0
    expires_at = datetime.now(UTC) + timedelta(minutes=15)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        output = {
            "market_regime": "TRENDING",
            "portfolio_risk_budget_fraction": 0.4,
            "allocations": [{
                "symbol": "BTCUSDT",
                "target_side": "LONG",
                "allocation_fraction": 1,
                "priority": 1,
                "confidence": 0.8,
                "entry_min": "# 99.8",
                "entry_max": "# 100.2",
                "stop_price": "# 98.8",
                "target_price": "# 103.0",
                "thesis": "多头趋势延续",
                "reason_codes": [],
                "risk_flags": [],
            }],
            "summary": "组合探测",
            "expires_at": expires_at.isoformat(),
        }
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        decision = await client.analyze_portfolio([snapshot()], [], expires_at)
    finally:
        await client.close()

    assert calls == 1
    allocation = decision.allocations[0]
    assert allocation.entry_min == Decimal("99.8")
    assert allocation.entry_max == Decimal("100.2")
    assert allocation.stop_price == Decimal("98.8")
    assert allocation.target_price == Decimal("103.0")


@pytest.mark.asyncio
async def test_balanced_portfolio_prompt_requires_a_directional_trigger(tmp_path: object) -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        output = {
            "market_regime": "RANGING",
            "portfolio_risk_budget_fraction": 0,
            "allocations": [],
            "summary": "当前无合格组合机会",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        }
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    settings = model_settings(tmp_path)
    settings.strategy_profile = "balanced"
    client = ResponsesModelClient(settings, transport=httpx.MockTransport(handler))
    try:
        await client.analyze_portfolio(
            [snapshot(symbol="BTCUSDT")],
            [],
            datetime.now(UTC) + timedelta(minutes=15),
        )
    finally:
        await client.close()

    system_text = captured[0]["input"][0]["content"][0]["text"]  # type: ignore[index]
    assert "同向15分钟" in system_text
    assert "没有同向触发时必须FLAT" in system_text


@pytest.mark.asyncio
async def test_portfolio_prompt_defines_flat_as_immediate_close_not_hold(
    tmp_path: object,
) -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        output = {
            "market_regime": "UNCERTAIN",
            "portfolio_risk_budget_fraction": 0,
            "allocations": [
                {
                    "symbol": "BTCUSDT",
                    "target_side": "FLAT",
                    "allocation_fraction": 0,
                    "priority": 1,
                    "confidence": 0.7,
                    "entry_min": None,
                    "entry_max": None,
                    "stop_price": None,
                    "target_price": None,
                    "thesis": "立即全部平仓",
                    "reason_codes": ["THESIS_INVALIDATED"],
                    "risk_flags": [],
                }
            ],
            "summary": "平仓退出",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        }
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        await client.analyze_portfolio(
            [],
            [position(symbol="BTCUSDT")],
            datetime.now(UTC) + timedelta(minutes=15),
        )
    finally:
        await client.close()

    system_text = captured[0]["input"][0]["content"][0]["text"]  # type: ignore[index]
    assert "唯一含义是：本周期立即按市价全部平仓" in system_text
    assert "绝不表示“保持仓位”" in system_text
    assert "必须返回与当前仓位相同的 LONG/SHORT 方向" in system_text
    assert "包括已有仓位和新开仓目标，不只是新增风险" in system_text


@pytest.mark.asyncio
async def test_portfolio_prompt_and_context_define_existing_tp2_boundary(
    tmp_path: object,
) -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        output = {
            "market_regime": "TRENDING",
            "portfolio_risk_budget_fraction": 0.4,
            "allocations": [
                {
                    "symbol": "DOGEUSDT",
                    "target_side": "SHORT",
                    "allocation_fraction": 1,
                    "priority": 1,
                    "confidence": 0.8,
                    "entry_min": "0.0800",
                    "entry_max": "0.0810",
                    "stop_price": "0.0830",
                    "target_price": "0.0770",
                    "thesis": "沿用有效的最终止盈",
                    "reason_codes": ["HOLD_EXISTING_POSITION"],
                    "risk_flags": [],
                }
            ],
            "summary": "继续持有空头",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        }
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    current = position(
        symbol="DOGEUSDT",
        side="SHORT",
        entry_price=Decimal("0.0810"),
        mark_price=Decimal("0.0800"),
        stop_price=Decimal("0.0830"),
        tp1_price=Decimal("0.0790"),
        tp2_price=Decimal("0.0770"),
        tp1_completed=False,
    )
    client = ResponsesModelClient(
        model_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        await client.analyze_portfolio(
            [], [current], datetime.now(UTC) + timedelta(minutes=15)
        )
    finally:
        await client.close()

    system_text = captured[0]["input"][0]["content"][0]["text"]  # type: ignore[index]
    assert "target_price 表示最终止盈 TP2，不是第一档止盈 TP1" in system_text
    assert "LONG 的新 TP2 必须严格高于现有 TP1" in system_text
    assert "SHORT 的新 TP2" in system_text
    assert "必须严格低于现有 TP1" in system_text
    assert "必须原样沿用当前 tp2_price" in system_text

    context = json.loads(captured[0]["input"][1]["content"][0]["text"])  # type: ignore[index]
    sent_position = context["positions"][0]
    assert sent_position["tp1_price"] == "0.0790"
    assert sent_position["tp2_price"] == "0.0770"
    assert sent_position["tp1_completed"] is False
    assert sent_position["tp2_ordering_boundary"] == "0.0790"
    assert sent_position["tp2_required_relation"] == "below_boundary"


@pytest.mark.asyncio
async def test_portfolio_context_uses_entry_as_tp2_boundary_after_tp1_completion(
    tmp_path: object,
) -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        output = {
            "market_regime": "TRENDING",
            "portfolio_risk_budget_fraction": 0.5,
            "allocations": [
                {
                    "symbol": "BTCUSDT",
                    "target_side": "LONG",
                    "allocation_fraction": 1,
                    "priority": 1,
                    "confidence": 0.82,
                    "entry_min": "99",
                    "entry_max": "101",
                    "stop_price": "98",
                    "target_price": "106",
                    "thesis": "第一档已完成，保留最终止盈",
                    "reason_codes": ["TP1_COMPLETED"],
                    "risk_flags": [],
                }
            ],
            "summary": "继续持有多头",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        }
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    current = position(
        symbol="BTCUSDT",
        entry_price=Decimal("100"),
        mark_price=Decimal("103"),
        stop_price=Decimal("100.5"),
        tp1_price=None,
        tp2_price=Decimal("106"),
        tp1_completed=True,
    )
    client = ResponsesModelClient(
        model_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        await client.analyze_portfolio(
            [], [current], datetime.now(UTC) + timedelta(minutes=15)
        )
    finally:
        await client.close()

    context = json.loads(captured[0]["input"][1]["content"][0]["text"])  # type: ignore[index]
    sent_position = context["positions"][0]
    assert sent_position["tp1_completed"] is True
    assert sent_position["tp2_ordering_boundary"] == "100"
    assert sent_position["tp2_required_relation"] == "above_boundary"


@pytest.mark.asyncio
async def test_portfolio_market_contract_repairs_one_spread_entry_range(
    tmp_path: object,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    invalid = {
        "market_regime": "TRENDING",
        "portfolio_risk_budget_fraction": 0.35,
        "allocations": [
            {
                "symbol": "DOGEUSDT",
                "target_side": "SHORT",
                "allocation_fraction": 1,
                "priority": 1,
                "confidence": 0.8,
                "entry_min": "0.082880",
                "entry_max": "0.082900",
                "stop_price": "0.083590",
                "target_price": "0.081000",
                "thesis": "高周期空头趋势延续",
                "reason_codes": ["TREND_ALIGNED"],
                "risk_flags": [],
            }
        ],
        "summary": "尝试建立空头",
        "expires_at": expires_at.isoformat(),
    }
    repaired = {
        **invalid,
        "allocations": [
            {
                **invalid["allocations"][0],
                "entry_min": "0.082840",
                "entry_max": "0.082940",
                "thesis": "使用可执行的有界入场区间",
            }
        ],
    }
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        output = invalid if len(captured) == 1 else repaired
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    candidate = snapshot(
        symbol="DOGEUSDT",
        mark_price=Decimal("0.08289628"),
        index_price=Decimal("0.08291833"),
        best_bid=Decimal("0.082880"),
        best_ask=Decimal("0.082900"),
        atr_15m=Decimal("0.000355"),
        trend_1h=-1,
        trend_4h=-1,
        breakout_15m=-1,
    )
    client = ResponsesModelClient(
        model_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        decision = await client.analyze_portfolio([candidate], [], expires_at)
    finally:
        await client.close()

    assert len(captured) == 2
    repair_text = captured[1]["input"][-1]["content"][0]["text"]  # type: ignore[index]
    assert "DOGEUSDT:entry_range_too_narrow" in repair_text
    assert decision.allocations[0].entry_min == Decimal("0.082840")
    assert decision.allocations[0].entry_max == Decimal("0.082940")


@pytest.mark.asyncio
async def test_portfolio_market_contract_repairs_stop_beyond_atr_limit(
    tmp_path: object,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    invalid = {
        "market_regime": "TRENDING",
        "portfolio_risk_budget_fraction": 0.4,
        "allocations": [
            {
                "symbol": "BTCUSDT",
                "target_side": "LONG",
                "allocation_fraction": 1,
                "priority": 1,
                "confidence": 0.85,
                "entry_min": "99.8",
                "entry_max": "100.2",
                "stop_price": "97.0",
                "target_price": "107.0",
                "thesis": "多头趋势延续",
                "reason_codes": ["TREND_ALIGNED"],
                "risk_flags": [],
            }
        ],
        "summary": "尝试建立多头",
        "expires_at": expires_at.isoformat(),
    }
    repaired = {
        **invalid,
        "allocations": [
            {
                **invalid["allocations"][0],
                "stop_price": "98.2",
                "thesis": "止损距离修正到 ATR 上限内",
            }
        ],
    }
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        output = invalid if len(captured) == 1 else repaired
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    client = ResponsesModelClient(
        model_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        decision = await client.analyze_portfolio([snapshot()], [], expires_at)
    finally:
        await client.close()

    assert len(captured) == 2
    repair_text = captured[1]["input"][-1]["content"][0]["text"]  # type: ignore[index]
    assert "BTCUSDT:stop_too_far" in repair_text
    assert decision.allocations[0].stop_price == Decimal("98.2")


@pytest.mark.asyncio
async def test_relay_output_message_text_is_parsed_without_repair(tmp_path: object) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        del request
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "reasoning", "content": []},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "```json\n" + json.dumps(valid_output()) + "\n```",
                            }
                        ],
                    },
                ],
            },
        )

    client = ResponsesModelClient(
        model_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        result = await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()
    assert result.market_regime == "UNCERTAIN"
    assert calls == 1


@pytest.mark.asyncio
async def test_portfolio_response_is_structured_and_identity_is_stable(tmp_path: object) -> None:
    captured: list[dict[str, object]] = []
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    output = {
        "market_regime": "TRENDING",
        "portfolio_risk_budget_fraction": 0.8,
        "allocations": [
            {
                "symbol": "BTCUSDT",
                "target_side": "LONG",
                "allocation_fraction": 1,
                "priority": 1,
                "confidence": 0.9,
                "entry_min": "99.9",
                "entry_max": "100.1",
                "stop_price": "98",
                "target_price": "106",
                "thesis": "趋势延续",
                "reason_codes": ["TREND_ALIGNED"],
                "risk_flags": [],
            }
        ],
        "summary": "保留趋势暴露",
        "expires_at": expires_at.isoformat(),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    client = ResponsesModelClient(
        model_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    candidate = snapshot()
    try:
        first = await client.analyze_portfolio(
            [candidate], [], expires_at, portfolio_context={"risk_cap": "7.5"}
        )
        second = await client.analyze_portfolio(
            [candidate], [], expires_at, portfolio_context={"risk_cap": "7.5"}
        )
    finally:
        await client.close()
    assert first.decision_id == second.decision_id
    assert first.allocations[0].allocation_id == second.allocations[0].allocation_id
    assert first.model_name == "gpt-5.6"
    assert first.prompt_version == "portfolio-v1.3"
    assert captured[0]["text"]["format"]["name"] == "portfolio_decision"  # type: ignore[index]
    payload_text = json.dumps(captured[0])
    assert "equity" not in payload_text


@pytest.mark.asyncio
async def test_portfolio_short_geometry_is_repaired_instead_of_reaching_compiler(
    tmp_path: object,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    invalid = {
        "market_regime": "TRENDING",
        "portfolio_risk_budget_fraction": 0.4,
        "allocations": [
            {
                "symbol": "TUTUSDT",
                "target_side": "SHORT",
                "allocation_fraction": 0.25,
                "priority": 1,
                "confidence": 0.86,
                "entry_min": "0.0398",
                "entry_max": "0.0402",
                "stop_price": "0.0387",
                "target_price": "0.0440",
                "thesis": "错误的空头价格结构",
                "reason_codes": [],
                "risk_flags": [],
            }
        ],
        "summary": "修复空头价格结构",
        "expires_at": expires_at.isoformat(),
    }
    valid = {
        **invalid,
        "allocations": [
            {
                **invalid["allocations"][0],
                "stop_price": "0.0413",
                "target_price": "0.0368",
                "thesis": "空头止损高于入场区间，目标低于入场区间",
            }
        ],
    }
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        del request
        output = invalid if calls == 1 else valid
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    client = ResponsesModelClient(
        model_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    candidate = snapshot(
        symbol="TUTUSDT",
        mark_price=Decimal("0.04000"),
        index_price=Decimal("0.04000"),
        best_bid=Decimal("0.03999"),
        best_ask=Decimal("0.04001"),
        atr_15m=Decimal("0.00060"),
        trend_1h=-1,
        trend_4h=-1,
        breakout_15m=-1,
    )
    try:
        decision = await client.analyze_portfolio(
            [candidate],
            [],
            expires_at,
        )
    finally:
        await client.close()

    assert calls == 2
    assert decision.allocations[0].target_side.value == "SHORT"
    assert decision.allocations[0].target_price < decision.allocations[0].entry_min
    assert decision.allocations[0].entry_max < decision.allocations[0].stop_price


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_allocation", "expected_detail"),
    [
        (
            {
                "symbol": "TUTUSDT",
                "target_side": "SHORT",
                "allocation_fraction": 0.25,
                "priority": 3,
                "confidence": 0.86,
                "entry_min": "0.0398",
                "entry_max": "0.0402",
                "stop_price": "0.0387",
                "target_price": "0.0440",
                "thesis": "错误的空头价格结构",
                "reason_codes": [],
                "risk_flags": [],
            },
            "allocations.2:value_error:Value error, SHORT geometry requires",
        ),
        (
            {
                "symbol": "TUTUSDT",
                "target_side": "SHORT",
                "allocation_fraction": 0.25,
                "priority": 3,
                "confidence": 0.86,
                "entry_min": "0.0398",
                "entry_max": "0.0402",
                "stop_price": "0.0413",
                "thesis": "缺少目标价格",
                "reason_codes": [],
                "risk_flags": [],
            },
            "allocations.2:value_error:Value error, non-flat allocation requires "
            "entry_min, entry_max, stop_price, and target_price (missing: target_price)",
        ),
        (
            {
                "symbol": "TUTUSDT",
                "target_side": "SIDEWAYS",
                "allocation_fraction": 0.25,
                "priority": 3,
                "confidence": 0.86,
                "entry_min": "0.0398",
                "entry_max": "0.0402",
                "stop_price": "0.0413",
                "target_price": "0.0368",
                "thesis": "非法方向",
                "reason_codes": [],
                "risk_flags": [],
            },
            "allocations.2.target_side:enum",
        ),
    ],
)
async def test_portfolio_repair_receives_specific_first_validation_error(
    tmp_path: object,
    invalid_allocation: dict[str, object],
    expected_detail: str,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    flat = {
        "target_side": "FLAT",
        "allocation_fraction": 0,
        "priority": 1,
        "confidence": 0.7,
        "entry_min": None,
        "entry_max": None,
        "stop_price": None,
        "target_price": None,
        "thesis": "保持空仓",
        "reason_codes": [],
        "risk_flags": [],
    }
    invalid = {
        "market_regime": "TRENDING",
        "portfolio_risk_budget_fraction": 0.4,
        "allocations": [
            {**flat, "symbol": "BTCUSDT"},
            {**flat, "symbol": "ETHUSDT", "priority": 2},
            invalid_allocation,
        ],
        "summary": "首次输出需要修复",
        "expires_at": expires_at.isoformat(),
    }
    repaired = {
        **invalid,
        "portfolio_risk_budget_fraction": 0,
        "allocations": [
            {**flat, "symbol": "BTCUSDT"},
            {**flat, "symbol": "ETHUSDT", "priority": 2},
            {**flat, "symbol": "TUTUSDT", "priority": 3},
        ],
        "summary": "已修复为完整合法输出",
    }
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        output = invalid if len(captured) == 1 else repaired
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        result = await client.analyze_portfolio([], [], expires_at)
    finally:
        await client.close()

    assert result.summary == "已修复为完整合法输出"
    repair_text = captured[1]["input"][-1]["content"][0]["text"]  # type: ignore[index]
    assert "具体错误" in repair_text
    assert expected_detail in repair_text


@pytest.mark.asyncio
async def test_structured_schema_includes_partial_close_action(tmp_path: object) -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": json.dumps(valid_output())})

    client = ResponsesModelClient(
        model_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()
    review_schema = captured[0]["text"]["format"]["schema"]["properties"][
        "position_reviews"
    ]["items"]  # type: ignore[index]
    assert "PARTIAL_CLOSE" in review_schema["properties"]["action"]["enum"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_manual_entry_advice_is_stateless_and_structured(tmp_path: object) -> None:
    captured: list[dict[str, object]] = []
    output = {
        "reply": "趋势一致，但只建议测试网小仓位。",
        "action": "OPEN_LONG",
        "confidence": 0.81,
        "stop_distance_pct": 1.2,
        "tp1_r": 1,
        "tp2_r": 2.5,
        "leverage": 2,
        "risk_notes": ["严格止损"],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        result = await client.advise_manual_entry(
            symbol="BTCUSDT",
            side="LONG",
            leverage=2,
            stop_distance_pct=Decimal("1"),
            tp1_r=Decimal("1"),
            tp2_r=Decimal("2"),
            messages=[{"role": "user", "content": "现在适合做多吗？"}],
            market={"mark_price": "100"},
            positions=[],
        )
    finally:
        await client.close()
    assert result.action == "OPEN_LONG"
    assert captured[0]["store"] is False
    assert captured[0]["text"]["format"]["name"] == "manual_entry_advice"  # type: ignore[index]


@pytest.mark.asyncio
async def test_relay_http_errors_keep_provider_code_and_message(tmp_path: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_request_error",
                    "message": "Invalid input item id; expected ctc_ prefix",
                }
            },
        )

    client = ResponsesModelClient(
        model_settings(tmp_path), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ModelRelayRequestError, match="invalid_request_error") as error:
            await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()
    assert error.value.status_code == 400
    assert "ctc_" in str(error.value)


@pytest.mark.asyncio
async def test_invalid_json_repairs_once(tmp_path: object) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"output_text": "not-json"})
        return httpx.Response(200, json={"output_text": json.dumps(valid_output())})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        result = await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()
    assert result.summary == "No trade"
    assert calls == 2


@pytest.mark.asyncio
async def test_schema_failure_detail_is_bounded_and_non_sensitive(tmp_path: object) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"output_text": json.dumps({"signals": ["bad"]})})
        return httpx.Response(200, json={"output_text": "not-json"})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(
            ModelUnavailableError, match=r"schema contract: (validation|invalid_json)"
        ) as error:
            await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()
    message = str(error.value)
    assert len(message) < 240
    assert "test-key" not in message
    assert "bad" not in message


@pytest.mark.asyncio
async def test_schema_failures_log_bounded_redacted_diagnostics(
    tmp_path: object, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "output_text": (
                    '{"token":"should-not-leak","signals":["bad"]}'
                    if calls == 1
                    else "not-json"
                )
            },
        )

    caplog.set_level(logging.WARNING, logger="trading_system.ai.client")
    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelUnavailableError):
            await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()

    messages = [record.getMessage() for record in caplog.records]
    assert any("phase=signal-primary" in message for message in messages)
    assert any("phase=signal-repair" in message for message in messages)
    assert "should-not-leak" not in " ".join(messages)
    assert "[REDACTED]" in " ".join(messages)
    assert all(len(message) < 1000 for message in messages)


@pytest.mark.asyncio
async def test_timeout_does_not_create_a_daily_request_lockout(tmp_path: object) -> None:
    calls = 0

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("relay timeout", request=request)

    client = ResponsesModelClient(
        model_settings(tmp_path), transport=httpx.MockTransport(timeout_handler)
    )
    try:
        with pytest.raises(ModelUnavailableError, match="request failed"):
            await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
        with pytest.raises(ModelUnavailableError, match="request failed"):
            await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()
    assert calls == 2


@pytest.mark.asyncio
async def test_transient_relay_errors_are_retried_without_schema_repair(
    tmp_path: object,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"output_text": json.dumps(valid_output())})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        result = await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()
    assert result.summary == "No trade"
    assert calls == 3


@pytest.mark.asyncio
async def test_signal_identity_is_stable_when_model_omits_ids(tmp_path: object) -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    output = valid_output()
    output["signals"] = [
        {
            "symbol": "BTCUSDT",
            "action": "OPEN_LONG",
            "confidence": "0.8",
            "entry_min": "99.9",
            "entry_max": "100.1",
            "invalidation_price": "99",
            "target_price": "103",
            "horizon_minutes": 240,
            "thesis": "trend continuation",
            "expires_at": (expires_at + timedelta(hours=1)).isoformat(),
        }
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": json.dumps(output)})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        first = await client.analyze([], [], expires_at)
        second = await client.analyze([], [], expires_at)
    finally:
        await client.close()

    assert first.signals[0].signal_id == second.signals[0].signal_id
    assert first.signals[0].expires_at == expires_at


@pytest.mark.asyncio
async def test_deep_health_executes_schema_probe(tmp_path: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": json.dumps(valid_output())})

    client = ResponsesModelClient(model_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        assert await client.health_check(deep=False) == (
            True,
            "configured; schema probe required for live unlock",
        )
        healthy, detail = await client.health_check(deep=True)
    finally:
        await client.close()
    assert healthy is True
    assert "probe passed" in detail


@pytest.mark.asyncio
async def test_deep_health_uses_portfolio_schema_when_enabled(tmp_path: object) -> None:
    captured: list[dict[str, object]] = []
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    portfolio_output = {
        "market_regime": "UNCERTAIN",
        "portfolio_risk_budget_fraction": 0,
        "allocations": [],
        "summary": "组合探针",
        "expires_at": expires_at.isoformat(),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"output_text": json.dumps(portfolio_output)})

    settings = model_settings(tmp_path)
    settings.portfolio_strategy_enabled = True
    client = ResponsesModelClient(settings, transport=httpx.MockTransport(handler))
    try:
        healthy, detail = await client.health_check(deep=True)
    finally:
        await client.close()

    assert healthy is True
    assert "probe passed" in detail
    assert captured[0]["text"]["format"]["name"] == "portfolio_decision"  # type: ignore[index]


@pytest.mark.asyncio
async def test_base_url_with_v1_suffix_does_not_duplicate_path(tmp_path: object) -> None:
    requested_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(200, json={"output_text": json.dumps(valid_output())})

    settings = model_settings(tmp_path)
    settings.model_base_url = "https://model.example/v1"
    client = ResponsesModelClient(settings, transport=httpx.MockTransport(handler))
    try:
        await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()
    assert requested_paths == ["/v1/responses"]


@pytest.mark.asyncio
async def test_model_profile_switch_changes_url_model_and_secret_without_restarting(
    tmp_path: object,
) -> None:
    requests: list[tuple[str, str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(
            (
                str(request.url),
                str(payload["model"]),
                request.headers["Authorization"],
            )
        )
        return httpx.Response(200, json={"output_text": json.dumps(valid_output())})

    settings = multi_profile_settings(tmp_path)
    client = ResponsesModelClient(settings, transport=httpx.MockTransport(handler))
    try:
        await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
        settings.model_profile = "vllm"
        await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()

    assert requests == [
        ("https://relay.example/v1/responses", "gpt-5.6", "Bearer relay-key"),
        (
            "http://vllm.example/v1/responses",
            "Qwen/Qwen3.8-27B-FP8",
            "Bearer vllm-key",
        ),
    ]


@pytest.mark.asyncio
async def test_live_mode_rejects_plain_http_model_endpoint(tmp_path: object) -> None:
    path = tmp_path
    path.joinpath("vllm_model_api_key").write_text("vllm-key", encoding="utf-8")
    settings = Settings(
        app_env="production",
        auth_required=True,
        cookie_secure=True,
        binance_environment="live",
        secret_dir=path,
        model_profile="vllm",
        vllm_model_base_url="http://vllm.example/v1",
        max_leverage=3,
        single_trade_risk_pct=0.0025,
        portfolio_risk_pct=0.0075,
        candidate_count=5,
        max_positions=3,
    )
    client = ResponsesModelClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"output_text": json.dumps(valid_output())}
            )
        ),
    )
    try:
        assert await client.health_check(deep=False) == (
            False,
            "live mode requires an HTTPS model endpoint",
        )
        with pytest.raises(ModelUnavailableError, match="HTTPS model endpoint"):
            await client.analyze([], [], datetime.now(UTC) + timedelta(minutes=15))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_proxy_mode_uses_shared_proxy_and_fails_closed_when_missing(
    tmp_path: object,
) -> None:
    settings = proxy_model_settings(tmp_path)
    client = ResponsesModelClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"output_text": json.dumps(valid_output())})
        ),
    )
    try:
        assert client.http._mounts
        assert await client.health_check(deep=False) == (
            True,
            "configured; schema probe required for live unlock",
        )
    finally:
        await client.close()

    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    missing_dir.joinpath("model_api_key").write_text("test-key", encoding="utf-8")
    missing = Settings(
        secret_dir=missing_dir,
        model_base_url="https://model.example",
        http_proxy_enabled=True,
    )
    client = ResponsesModelClient(
        missing,
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
    )
    try:
        healthy, detail = await client.health_check()
    finally:
        await client.close()
    assert healthy is False
    assert "http_proxy_url" in detail
