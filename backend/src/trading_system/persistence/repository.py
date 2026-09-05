from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, desc, select

from trading_system.config import RUNTIME_CONFIG_FIELDS, Settings
from trading_system.domain.enums import PositionSide, SystemMode
from trading_system.domain.models import (
    AccountState,
    MarketSnapshot,
    OrderState,
    PortfolioDecision,
    PortfolioPlan,
    PortfolioPlanAction,
    PositionState,
    RiskDecision,
    TradeSignal,
)
from trading_system.persistence.database import Database
from trading_system.persistence.records import (
    AuditEventRecord,
    EquityCheckpointRecord,
    EquityHistoryRecord,
    IncomeLedgerRecord,
    MarketFeatureRecord,
    ModelReplayCacheRecord,
    OrderRecord,
    PortfolioAllocationRecord,
    PortfolioDecisionRecord,
    PortfolioExecutionRecord,
    PositionRecord,
    ReplayRunRecord,
    RiskDecisionRecord,
    RuntimeConfigRecord,
    SignalRecord,
    SystemStateRecord,
)

REASON_LABELS_ZH: dict[str, str] = {
    "symbol_not_trading": "合约当前不可交易",
    "listing_too_recent": "上市时间不足",
    "spread_too_wide": "买卖价差过大",
    "funding_rate_abnormal": "资金费率异常",
    "basis_abnormal": "基差异常",
    "insufficient_book_depth": "盘口深度不足",
    "extreme_volatility": "波动率过高",
    "volatile_regime": "市场处于极端波动状态",
    "uncertain_regime": "市场状态不确定",
    "market_regime_not_trending": "当前不是可开仓的趋势市场",
    "trend_strength_below_minimum": "ADX低于最低趋势强度",
    "invalid_volatility_risk_multiplier": "波动率风险系数无效",
    "atr_invalid": "ATR无效，无法计算止损距离",
    "trend_not_aligned": "1小时与4小时趋势未对齐",
    "no_aligned_entry_trigger": "没有符合策略的入场触发",
    "strong_trend_entry_override": "强劲上升趋势策略放行（仅保留硬资金安全检查）",
    "model_returned_no_trade": "模型判断暂不交易",
    "signal_expired": "信号已过期",
    "signal_snapshot_symbol_mismatch": "信号与行情合约不一致",
    "confidence_below_minimum": "置信度低于最低要求",
    "entry_direction_not_allowed": "开仓方向不在当前策略允许范围",
    "position_count_limit_reached": "已达到最大持仓数量",
    "available_balance_insufficient": "账户可用余额不足",
    "same_direction_limit_reached": "已达到同方向持仓上限",
    "existing_symbol_position": "该合约已有持仓，系统不会重复开仓",
    "net_reward_risk_below_minimum": "净盈亏比低于最低要求",
    "portfolio_risk_capacity_exhausted": "组合风险额度已用尽",
    "quantity_below_exchange_minimum": "数量低于交易所最小值",
    "notional_below_exchange_minimum": "名义价值低于交易所最小值",
    "stop_too_close": "止损距离过近",
    "stop_too_far": "止损距离过远",
    "all_hard_limits_passed": "已通过全部硬风控",
    "trend_aligned": "多周期趋势一致",
    "breakout_confirmation": "15分钟突破得到确认",
    "pullback_confirmation": "15分钟回踩得到确认",
    "volume_confirmation": "成交量得到确认",
    "open_interest_confirmation": "持仓量变化得到确认",
    "funding_acceptable": "资金费率处于可接受范围",
    "late_entry": "入场位置偏晚",
    "signals_conflict": "多个信号相互冲突",
    "liquidity_weak": "流动性不足",
    "risk_deteriorated": "风险条件恶化",
    "invalid_price_geometry": "入场、止损和止盈价格关系无效",
    "rounded_stop_invalid": "按交易所精度处理后止损无效",
    "system_mode_disallows_entries": "当前系统模式不允许开仓",
    "system_mode_disallows_risk_increase": "当前系统模式仅允许降风险动作",
    "HOLD_POSITION": "已有仓位，当前周期继续持有",
    "HOLD_EXISTING_POSITION": "已有仓位，当前周期继续持有",
    "HOLD_VALID": "原有交易逻辑仍然有效",
    "POSITION_EXISTS": "该合约已有持仓，系统不会重复开仓",
    "NO_NEW_TRIGGER": "当前没有新的入场触发",
    "NO_NEW_SIGNAL": "当前没有新的入场信号",
    "NO_CONFIRMED_TRIGGER": "当前没有确认的入场触发",
    "NO_CONFIRMED_15M_CONTINUATION": "15分钟没有确认的延续触发",
    "TREND_CONFIRMATION_PENDING": "趋势确认仍在等待",
    "TREND_CONTINUATION": "趋势仍在延续",
    "TREND_BIAS_LONG": "趋势偏向做多",
    "TREND_ALIGNED_1H_4H": "1小时与4小时趋势一致",
    "ALIGNED_1H_4H_UPTREND": "1小时与4小时均为上升趋势",
    "ALIGNED_1H_4H_DOWNTREND": "1小时与4小时均为下降趋势",
    "BEARISH_1H_4H_ALIGNMENT": "1小时与4小时空头趋势一致",
    "1H_4H_ALIGNED_UP": "1小时与4小时多头趋势一致",
    "1H_4H_TREND_ALIGNMENT": "1小时与4小时趋势一致",
    "ALIGNED_TRENDS": "多周期趋势一致",
    "ALIGNED_TRENDS_BUT_NO_15M_BREAKOUT": "多周期趋势一致，但15分钟尚未突破",
    "NO_15M_BREAKOUT": "15分钟尚未确认突破",
    "NO_15M_BREAKOUT_CONFIRMATION": "15分钟尚未确认突破",
    "NO_15M_BREAKOUT_TRIGGER": "15分钟没有突破触发",
    "15M_BREAKOUT": "15分钟突破已出现",
    "15M_BREAKOUT_CONFIRMED": "15分钟突破得到确认",
    "BREAKOUT_15M": "15分钟突破已出现",
    "BEARISH_15M_BREAKOUT": "15分钟出现空头突破",
    "BREAKOUT_WITHOUT_PULLBACK": "突破后尚未形成回踩",
    "BREAKOUT_WITHOUT_VOLUME": "突破缺少成交量确认",
    "BREAKOUT_WITHOUT_STRONG_TREND_CONFIRMATION": "突破缺少强趋势确认",
    "PULLBACK_TRIGGER": "15分钟回踩触发已出现",
    "PULLBACK_15M": "15分钟回踩已出现",
    "15M_PULLBACK": "15分钟回踩已出现",
    "PULLBACK_CONFIRMATION": "15分钟回踩得到确认",
    "NO_PULLBACK_TRIGGER": "当前没有回踩触发",
    "NO_PULLBACK_CONFIRMATION": "当前没有确认的回踩",
    "PULLBACK_UNCONFIRMED": "回踩尚未确认",
    "PULLBACK_WITHOUT_TRIGGER": "回踩尚未形成有效触发",
    "PULLBACK_WITHOUT_CONTINUATION": "回踩后尚未出现延续",
    "PULLBACK_WITHOUT_CONTINUATION_CONFIRMATION": "回踩后尚未确认延续",
    "WEAK_VOLUME_CONFIRMATION": "成交量确认偏弱",
    "LOW_VOLUME_CONFIRMATION": "成交量确认不足",
    "NEGATIVE_VOLUME_CONFIRMATION": "成交量未支持当前方向",
    "NO_VOLUME_CONFIRMATION": "没有成交量确认",
    "SEVERELY_NEGATIVE_VOLUME_ZSCORE": "成交量明显弱于正常水平",
    "VOLUME_CONFIRMED": "成交量得到确认",
    "VOLUME_CONFIRMATION": "成交量得到确认",
    "OPEN_INTEREST_RISING": "持仓量正在增加",
    "POSITIVE_OPEN_INTEREST": "持仓量变化支持当前方向",
    "POSITIVE_OI": "持仓量变化支持当前方向",
    "OPEN_INTEREST_CONFIRMATION": "持仓量变化得到确认",
    "WEAK_TREND_STRENGTH": "趋势强度偏弱",
    "WEAK_1H_TREND": "1小时趋势偏弱",
    "WEAK_1H_TREND_STRENGTH": "1小时趋势强度偏弱",
    "LOW_ADX": "ADX偏低，趋势强度不足",
    "LOW_1H_ADX": "1小时ADX偏低，趋势强度不足",
    "VERY_LOW_1H_ADX": "1小时ADX很低，趋势强度不足",
    "STRONG_1H_ADX": "1小时ADX较强",
    "ADX_HIGH": "ADX较高，趋势强度充足",
    "MISSING_STRUCTURAL_LEVELS": "缺少结构性支撑阻力位",
    "INSUFFICIENT_STRUCTURAL_LEVELS": "结构性价位不足",
    "STRUCTURAL_LEVELS_UNAVAILABLE": "无法取得结构性价位",
    "MISSING_STRUCTURAL_INVALIDATION": "缺少明确的结构性失效位",
    "INSUFFICIENT_STRUCTURAL_CONFIRMATION": "结构性确认不足",
    "INSUFFICIENT_ENTRY_STRUCTURE": "入场结构不完整",
    "INSUFFICIENT_ENTRY_DEFINITION": "入场条件定义不足",
    "INSUFFICIENT_RISK_LEVELS": "止损止盈风险价位不足",
    "MISSING_MARKET_DATA": "缺少行情数据",
    "NO_MARKET_DATA": "当前没有可用行情数据",
    "DATA_INSUFFICIENT": "行情或指标数据不足",
    "INSUFFICIENT_HISTORY": "历史数据长度不足",
    "INSUFFICIENT_CONFIDENCE": "模型置信度不足",
    "NO_CONFIRMED_SIGNAL": "当前没有确认信号",
    "SETUP_NOT_CONFIRMED": "交易形态尚未确认",
    "SETUP_NOT_STRUCTURALLY_DEFINED": "交易形态的结构边界尚未明确",
    "SETUP_LATE": "交易形态已经偏晚",
    "LATE_BREAKOUT_RISK": "突破位置偏晚，存在追价风险",
    "CHASE_RISK": "存在追价风险",
    "BREAKOUT_CHASE_RISK": "突破追价风险较高",
    "ELEVATED_VOLATILITY": "波动率偏高",
    "EXTREME_VOLATILITY": "波动率极高",
    "VOLATILITY_EXTREME": "波动率极高",
    "VOLATILITY_ELEVATED": "波动率偏高",
    "LOW_VOLATILITY": "波动率处于较低水平",
    "FUNDING_HEADWIND": "资金费率对当前方向不利",
    "POSITIVE_FUNDING_AGAINST_SHORT": "正资金费率不利于做空",
    "ELEVATED_POSITIVE_FUNDING": "正资金费率偏高",
    "ELEVATED_FUNDING": "资金费率偏高",
    "EXTREME_FUNDING": "资金费率极端",
    "FUNDING_ACCEPTABLE": "资金费率处于可接受范围",
    "LIQUIDITY_ADEQUATE": "流动性充足",
    "LIQUID_BOOK": "盘口流动性良好",
    "DEEP_LIQUIDITY": "盘口深度充足",
    "WIDE_SPREAD": "买卖价差过大",
    "LIQUIDITY_RISK": "流动性风险偏高",
    "FORCED_TESTNET_VALIDATION": "测试网强制验证信号，仅用于链路测试",
    "forced_testnet_validation": "测试网强制验证信号，仅用于链路测试",
    "DETERMINISTIC_SCREEN": "已完成确定性筛选",
}

_UNKNOWN_REASON_TERMS = {
    "HOLD": "持有",
    "POSITION": "仓位",
    "EXISTS": "已存在",
    "NO": "没有",
    "NEW": "新的",
    "TRIGGER": "触发",
    "SIGNAL": "信号",
    "TREND": "趋势",
    "CONFIRMATION": "确认",
    "CONFIRMED": "已确认",
    "PENDING": "等待中",
    "WEAK": "偏弱",
    "LOW": "偏低",
    "MISSING": "缺少",
    "INSUFFICIENT": "不足",
    "BREAKOUT": "突破",
    "PULLBACK": "回踩",
    "VOLUME": "成交量",
    "RISK": "风险",
    "STRUCTURAL": "结构性",
    "LEVELS": "价位",
    "MARKET": "行情",
    "DATA": "数据",
    "POSITION_EXISTS": "已有仓位",
}


def _unknown_reason_zh(code: str) -> str:
    words = [part for part in code.upper().split("_") if part]
    translated = "、".join(_UNKNOWN_REASON_TERMS.get(word, "条件") for word in words)
    return f"有一项交易条件未满足（{translated}）"


def _raw_reason_codes(payload: dict[str, Any]) -> list[str]:
    raw_codes = payload.get("reason_codes", [])
    return _raw_codes(raw_codes)


def _raw_codes(raw_codes: object) -> list[str]:
    if not isinstance(raw_codes, list):
        return []
    return [str(code) for code in raw_codes if str(code).strip()]


def _signal_reason_zh(status: str, action: str, reason_codes: list[str]) -> str:
    if reason_codes:
        details = "；".join(reason_codes)
        if status == "APPROVED":
            return f"本轮允许{_action_label_zh(action)}，已满足：{details}。"
        return f"本轮未开仓，原因：{details}。"
    if status == "APPROVED":
        return f"本轮允许{_action_label_zh(action)}，信号已通过硬风控。"
    if status == "NO_TRADE" or action == "NO_TRADE":
        return "本轮未开仓，当前没有同时满足趋势、触发和风险条件。"
    if status.startswith("REJECTED"):
        return "本轮未开仓，信号未通过确定性硬风控。"
    return "信号已记录，等待进一步风控处理。"


def _action_label_zh(action: str) -> str:
    return {"OPEN_LONG": "开多", "OPEN_SHORT": "开空"}.get(action, "开仓")


def _signal_recommendation_zh(
    status: str, action: str, raw_codes: list[str]
) -> str:
    codes = {code.upper() for code in raw_codes}
    if "FORCED_TESTNET_VALIDATION" in codes:
        return "仅用于测试网链路验证；正式策略仍需等待完整信号，不能据此放宽风控。"
    if codes & {
        "HOLD_POSITION",
        "HOLD_VALID",
        "HOLD_EXISTING_POSITION",
        "POSITION_EXISTS",
        "EXISTING_SYMBOL_POSITION",
    }:
        return "建议等待现有仓位平仓或风险审计确认后再评估，不要重复开同一合约。"
    if codes & {
        "NO_MARKET_DATA",
        "MISSING_MARKET_DATA",
        "DATA_INSUFFICIENT",
        "INSUFFICIENT_HISTORY",
    }:
        return "建议先恢复行情和指标数据，再进行下一轮分析；数据不完整时不要开仓。"
    if codes & {
        "MISSING_STRUCTURAL_LEVELS",
        "INSUFFICIENT_STRUCTURAL_LEVELS",
        "STRUCTURAL_LEVELS_UNAVAILABLE",
        "MISSING_STRUCTURAL_INVALIDATION",
        "INSUFFICIENT_STRUCTURAL_CONFIRMATION",
        "INSUFFICIENT_ENTRY_STRUCTURE",
        "INSUFFICIENT_ENTRY_DEFINITION",
        "INSUFFICIENT_RISK_LEVELS",
        "INVALID_PRICE_GEOMETRY",
        "ROUNDED_STOP_INVALID",
    }:
        return "建议等待明确失效位，并重新计算入场、止损、止盈和净盈亏比。"
    if codes & {
        "WEAK_VOLUME_CONFIRMATION",
        "LOW_VOLUME_CONFIRMATION",
        "NEGATIVE_VOLUME_CONFIRMATION",
        "NO_VOLUME_CONFIRMATION",
        "SEVERELY_NEGATIVE_VOLUME_ZSCORE",
        "BREAKOUT_WITHOUT_VOLUME",
    }:
        return "建议等待成交量与持仓量同步确认，不要为了触发入场而降低最低置信度。"
    if codes & {
        "NO_NEW_TRIGGER",
        "NO_NEW_SIGNAL",
        "NO_CONFIRMED_TRIGGER",
        "NO_CONFIRMED_15M_CONTINUATION",
        "TREND_CONFIRMATION_PENDING",
        "NO_15M_BREAKOUT",
        "NO_15M_BREAKOUT_CONFIRMATION",
        "NO_15M_BREAKOUT_TRIGGER",
        "NO_PULLBACK_TRIGGER",
        "NO_PULLBACK_CONFIRMATION",
        "PULLBACK_UNCONFIRMED",
        "PULLBACK_WITHOUT_TRIGGER",
        "PULLBACK_WITHOUT_CONTINUATION",
        "PULLBACK_WITHOUT_CONTINUATION_CONFIRMATION",
    }:
        return "建议等待15分钟K线收盘确认突破或回踩触发，不建议追价开仓。"
    if codes & {
        "LOW_ADX",
        "LOW_1H_ADX",
        "VERY_LOW_1H_ADX",
        "WEAK_TREND_STRENGTH",
        "WEAK_1H_TREND",
        "WEAK_1H_TREND_STRENGTH",
    }:
        return "建议等待趋势强度改善并完成多周期确认，不建议仅凭单根K线开仓。"
    if codes & {
        "EXTREME_VOLATILITY",
        "VOLATILITY_EXTREME",
        "ELEVATED_VOLATILITY",
        "VOLATILITY_ELEVATED",
    }:
        return "建议等波动率回到可接受区间，避免在极端波动中追单。"
    if codes & {
        "FUNDING_HEADWIND",
        "POSITIVE_FUNDING_AGAINST_SHORT",
        "ELEVATED_POSITIVE_FUNDING",
        "ELEVATED_FUNDING",
        "EXTREME_FUNDING",
    }:
        return "建议等待资金费率回落，或只选择资金费率与交易方向一致的机会。"
    if codes & {
        "LATE_ENTRY",
        "SETUP_LATE",
        "LATE_BREAKOUT_RISK",
        "CHASE_RISK",
        "BREAKOUT_CHASE_RISK",
    }:
        return "建议等待下一次回踩再评估，不追高、不在形态末端强行入场。"
    if codes & {"SPREAD_TOO_WIDE", "WIDE_SPREAD", "LIQUIDITY_WEAK", "LIQUIDITY_RISK"}:
        return "建议优先选择流动性更好的白名单合约，并等待价差收窄。"
    if codes & {"CONFIDENCE_BELOW_MINIMUM", "INSUFFICIENT_CONFIDENCE"}:
        return "建议保持当前最低置信度，不要通过降低门槛来制造入场信号。"
    if status == "APPROVED":
        return "建议按本轮信号的入场区间执行，并同步提交止损和止盈保护单；AI建议不能绕过硬风控。"
    if action == "NO_TRADE" or status.startswith("REJECTED"):
        return "建议继续观察下一个15分钟周期，只有趋势、触发、成交量和结构性止损同时满足时再评估。"
    return "建议保持观察，等待下一轮周期给出更完整的趋势和风险信息。"


def _reason_codes_zh(payload: dict[str, Any], labels: dict[str, str]) -> list[str]:
    return [labels.get(code, _unknown_reason_zh(code)) for code in _raw_reason_codes(payload)]


def _reason_codes_zh_from_codes(
    codes: list[str], labels: dict[str, str]
) -> list[str]:
    return [labels.get(code, _unknown_reason_zh(code)) for code in codes]


def _market_context_payload(snapshot: MarketSnapshot) -> dict[str, object]:
    return snapshot.model_dump(
        mode="json",
        include={
            "timestamp",
            "mark_price",
            "spread_pct",
            "funding_rate",
            "open_interest_change_pct",
            "atr_15m",
            "adx_1h",
            "trend_1h",
            "trend_4h",
            "breakout_15m",
            "pullback_15m",
            "volume_zscore",
            "market_regime",
            "volatility_percentile",
            "volatility_risk_multiplier",
        },
    )


class Repository:
    def __init__(self, database: Database, timezone_name: str = "UTC") -> None:
        self.database = database
        self.timezone_name = timezone_name

    async def get_mode(
        self,
        default: SystemMode = SystemMode.TESTNET,
        environment: str = "testnet",
    ) -> SystemMode:
        async with self.database.sessions() as session:
            record = await session.get(SystemStateRecord, 1)
            if record is None:
                record = SystemStateRecord(
                    id=1,
                    mode=default.value,
                    environment=environment,
                    entries_enabled=default == SystemMode.TESTNET,
                )
                session.add(record)
                await session.commit()
            elif record.environment != environment:
                record.environment = environment
                record.mode = SystemMode.RECONCILIATION_REQUIRED.value
                record.entries_enabled = False
                record.halt_reason = "runtime environment changed; reconciliation required"
                await session.commit()
            return SystemMode(record.mode)

    async def get_mode_state(
        self,
        default: SystemMode = SystemMode.TESTNET,
        environment: str = "testnet",
    ) -> dict[str, Any]:
        """Return the persisted mode together with its safety explanation.

        The dashboard previously exposed only ``mode``.  That made a paused
        system look indistinguishable from a model or worker interruption even
        when the persisted halt reason already identified the real blocker.
        Keep the normal ``get_mode`` behavior (including environment-change
        reconciliation) and then return a small read-only state envelope.
        """

        await self.get_mode(default, environment)
        async with self.database.sessions() as session:
            record = await session.get(SystemStateRecord, 1)
            if record is None:
                return {
                    "mode": default.value,
                    "environment": environment,
                    "entries_enabled": default
                    in {SystemMode.TESTNET, SystemMode.LIVE_ENABLED},
                    "halt_reason": None,
                    "updated_at": None,
                }
            return {
                "mode": record.mode,
                "environment": record.environment,
                "entries_enabled": record.entries_enabled,
                "halt_reason": record.halt_reason,
                "updated_at": record.updated_at,
            }

    async def set_mode(self, mode: SystemMode, *, halt_reason: str | None = None) -> SystemMode:
        async with self.database.sessions() as session:
            record = await session.get(SystemStateRecord, 1)
            if record is None:
                record = SystemStateRecord(id=1, mode=mode.value)
                session.add(record)
            record.mode = mode.value
            record.entries_enabled = mode in {SystemMode.TESTNET, SystemMode.LIVE_ENABLED}
            record.halt_reason = halt_reason
            record.updated_at = datetime.now(UTC)
            await session.commit()
        return mode

    async def apply_runtime_config(self, settings: Settings) -> dict[str, object]:
        async with self.database.sessions() as session:
            record = await session.get(RuntimeConfigRecord, 1)
            values = dict(record.values) if record is not None else {}
        updates = {key: value for key, value in values.items() if key in RUNTIME_CONFIG_FIELDS}
        if not updates:
            return values
        # Runtime settings are persisted as JSON and normally bypass Pydantic's
        # constructor.  Re-validate the merged object so a stale testnet-only
        # aggressive setting cannot silently leak into live mode after a restart.
        validated = type(settings).model_validate(
            {**settings.model_dump(), **updates}
        )
        for key in updates:
            setattr(settings, key, getattr(validated, key))
        return values

    async def save_runtime_config(self, updates: Mapping[str, object]) -> dict[str, object]:
        sanitized = {key: value for key, value in updates.items() if key in RUNTIME_CONFIG_FIELDS}
        async with self.database.sessions() as session:
            record = await session.get(RuntimeConfigRecord, 1)
            if record is None:
                record = RuntimeConfigRecord(id=1, values={})
                session.add(record)
            values = dict(record.values)
            values.update(sanitized)
            record.values = values
            record.updated_at = datetime.now(UTC)
            await session.commit()
            return values

    async def audit(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        detail: dict[str, object] | None = None,
        ip_address: str | None = None,
    ) -> None:
        async with self.database.sessions() as session:
            session.add(
                AuditEventRecord(
                    actor=actor,
                    action=action,
                    resource=resource,
                    outcome=outcome,
                    detail=detail or {},
                    ip_address=ip_address,
                )
            )
            await session.commit()

    async def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(AuditEventRecord).order_by(desc(AuditEventRecord.created_at)).limit(limit)
            )
            return [
                {
                    "id": record.id,
                    "actor": record.actor,
                    "action": record.action,
                    "resource": record.resource,
                    "outcome": record.outcome,
                    "detail": record.detail,
                    "ip_address": record.ip_address,
                    "created_at": record.created_at,
                }
                for record in result.scalars()
            ]

    async def apply_equity_checkpoints(
        self, account: AccountState, *, record_history: bool = True
    ) -> AccountState:
        today = datetime.now(ZoneInfo(self.timezone_name)).date()
        async with self.database.sessions() as session:
            record = await session.get(EquityCheckpointRecord, 1)
            if record is None or record.trading_day != today:
                if record is None:
                    record = EquityCheckpointRecord(
                        id=1,
                        trading_day=today,
                        day_start_equity=account.equity,
                        high_water_mark=account.equity,
                    )
                    session.add(record)
                else:
                    record.trading_day = today
                    record.day_start_equity = account.equity
                    record.high_water_mark = max(
                        Decimal(str(record.high_water_mark)), account.equity
                    )
            elif account.equity > Decimal(str(record.high_water_mark)):
                record.high_water_mark = account.equity
            account.day_start_equity = Decimal(str(record.day_start_equity))
            account.high_water_mark = Decimal(str(record.high_water_mark))
            if record_history:
                session.add(
                    EquityHistoryRecord(
                        equity=account.equity,
                        drawdown=account.drawdown_pct,
                    )
                )
            await session.commit()
        return account

    async def list_equity_curve(self, limit: int = 500) -> list[dict[str, str]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(EquityHistoryRecord)
                .order_by(desc(EquityHistoryRecord.created_at))
                .limit(limit)
            )
            rows = list(result.scalars())
        return [
            {
                "time": record.created_at.isoformat(),
                "equity": str(record.equity),
                "drawdown": str(record.drawdown),
            }
            for record in reversed(rows)
        ]

    async def save_market_snapshots(self, snapshots: list[MarketSnapshot]) -> None:
        async with self.database.sessions() as session:
            await session.execute(
                delete(MarketFeatureRecord).where(
                    MarketFeatureRecord.timestamp < datetime.now(UTC) - timedelta(days=365)
                )
            )
            session.add_all(
                [
                    MarketFeatureRecord(
                        symbol=snapshot.symbol,
                        interval="15m-cycle",
                        payload=snapshot.model_dump(mode="json", exclude={"recent_returns_1h"}),
                        timestamp=snapshot.timestamp,
                    )
                    for snapshot in snapshots
                ]
            )
            await session.commit()

    async def save_signal(
        self,
        signal: TradeSignal,
        *,
        status: str,
        prompt_version: str,
        model_name: str,
        input_hash: str,
        market_snapshot: MarketSnapshot | None = None,
    ) -> None:
        async with self.database.sessions() as session:
            existing = await session.get(SignalRecord, str(signal.signal_id))
            if existing is None:
                payload = signal.model_dump(mode="json")
                if market_snapshot is not None:
                    payload["market_context"] = _market_context_payload(market_snapshot)
                session.add(
                    SignalRecord(
                        id=str(signal.signal_id),
                        symbol=signal.symbol,
                        action=signal.action.value,
                        status=status,
                        payload=payload,
                        prompt_version=prompt_version,
                        model_name=model_name,
                        input_hash=input_hash,
                    )
                )
                await session.commit()

    async def save_risk_decision(self, decision: RiskDecision) -> None:
        async with self.database.sessions() as session:
            existing = await session.get(RiskDecisionRecord, str(decision.decision_id))
            if existing is None:
                session.add(
                    RiskDecisionRecord(
                        id=str(decision.decision_id),
                        signal_id=str(decision.signal_id),
                        status=decision.status.value,
                        reasons=decision.reasons,
                        payload=decision.model_dump(mode="json"),
                    )
                )
                await session.commit()

    async def save_portfolio_decision(
        self,
        decision: PortfolioDecision,
        *,
        status: str,
        prompt_version: str,
        model_name: str,
        input_hash: str,
        replay_context: dict[str, object] | None = None,
        compiled_plan: PortfolioPlan | None = None,
    ) -> None:
        async with self.database.sessions() as session:
            existing = await session.get(PortfolioDecisionRecord, str(decision.decision_id))
            if existing is None:
                payload = decision.model_dump(mode="json")
                if replay_context is not None and compiled_plan is not None:
                    # Keep this immutable decision-time envelope with the model
                    # intent.  It lets a future replay use the exact account,
                    # filters, limits, snapshots and compiler clock, without a
                    # new network/model request or current-state contamination.
                    payload["portfolio_replay_context"] = replay_context
                    payload["portfolio_compiled_plan"] = compiled_plan.model_dump(
                        mode="json"
                    )
                session.add(
                    PortfolioDecisionRecord(
                        id=str(decision.decision_id),
                        status=status,
                        market_regime=decision.market_regime,
                        risk_budget_fraction=decision.portfolio_risk_budget_fraction,
                        payload=payload,
                        prompt_version=prompt_version,
                        model_name=model_name,
                        input_hash=input_hash,
                    )
                )
            # Persist the model's raw intent even when the deterministic
            # compiler returns early (for example because the decision is
            # expired, the system is paused, or a required position/filter is
            # missing).  The compiler later upgrades these rows in-place using
            # the same allocation_id, so the audit view can always compare
            # AI target -> risk result -> execution outcome without losing an
            # otherwise valid model response.
            for allocation in decision.allocations:
                allocation_id = str(allocation.allocation_id)
                allocation_record = await session.get(
                    PortfolioAllocationRecord, allocation_id
                )
                if allocation_record is None:
                    session.add(
                        PortfolioAllocationRecord(
                            id=allocation_id,
                            decision_id=str(decision.decision_id),
                            symbol=allocation.symbol,
                            status="MODEL_INTENT",
                            payload={
                                **allocation.model_dump(mode="json"),
                                "allocation_id": allocation_id,
                                "source": "ai_intent",
                            },
                        )
                    )
            await session.commit()

    async def get_portfolio_replay_input(
        self, decision_id: str
    ) -> dict[str, object] | None:
        """Return the immutable Portfolio-v1 replay envelope, if it was captured.

        Older decisions predate the capture field and intentionally cannot be
        reconstructed from current exchange state; returning ``None`` makes that
        distinction explicit to the caller.
        """

        async with self.database.sessions() as session:
            record = await session.get(PortfolioDecisionRecord, decision_id)
            if record is None:
                return None
            payload = dict(record.payload)
            context = payload.get("portfolio_replay_context")
            compiled_plan = payload.get("portfolio_compiled_plan")
            if not isinstance(context, dict) or not isinstance(compiled_plan, dict):
                return None
            return {
                "decision": payload,
                "context": context,
                "compiled_plan": compiled_plan,
            }

    async def save_portfolio_plan(self, plan: PortfolioPlan) -> None:
        async with self.database.sessions() as session:
            for action in plan.actions:
                record = await session.get(PortfolioAllocationRecord, str(action.action_id))
                payload = action.model_dump(mode="json")
                payload["plan_status"] = plan.status.value
                payload["plan_reasons"] = plan.reasons
                status = "REJECTED" if action.action.value == "REJECTED" else "PLANNED"
                if record is None:
                    session.add(
                        PortfolioAllocationRecord(
                            id=str(action.action_id),
                            decision_id=str(plan.decision_id),
                            symbol=action.symbol,
                            status=status,
                            payload=payload,
                        )
                    )
                else:
                    record.status = status
                    record.payload = payload
                    record.updated_at = datetime.now(UTC)
            await session.commit()

    async def save_portfolio_execution(
        self,
        plan: PortfolioPlan,
        action: PortfolioPlanAction,
        *,
        status: str,
        orders: list[OrderState],
        detail: str | None = None,
    ) -> None:
        async with self.database.sessions() as session:
            record = await session.get(PortfolioExecutionRecord, str(action.action_id))
            payload = action.model_dump(mode="json")
            if detail:
                payload["detail"] = detail
            if record is None:
                session.add(
                    PortfolioExecutionRecord(
                        id=str(action.action_id),
                        decision_id=str(plan.decision_id),
                        allocation_id=str(action.allocation_id) if action.allocation_id else None,
                        action=action.action.value,
                        status=status,
                        order_ids=[item.client_order_id for item in orders],
                        payload=payload,
                    )
                )
            else:
                record.status = status
                record.order_ids = [item.client_order_id for item in orders]
                record.payload = payload
                record.updated_at = datetime.now(UTC)
            allocation = await session.get(PortfolioAllocationRecord, str(action.action_id))
            if allocation is not None:
                allocation.status = status
                allocation.updated_at = datetime.now(UTC)
            await session.commit()

    async def list_portfolio_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(PortfolioDecisionRecord)
                .order_by(desc(PortfolioDecisionRecord.created_at))
                .limit(limit)
            )
            decisions = list(result.scalars())
            if not decisions:
                return []
            ids = [item.id for item in decisions]
            allocation_rows = list(
                (
                    await session.execute(
                        select(PortfolioAllocationRecord).where(
                            PortfolioAllocationRecord.decision_id.in_(ids)
                        )
                    )
                ).scalars()
            )
            execution_rows = list(
                (
                    await session.execute(
                        select(PortfolioExecutionRecord).where(
                            PortfolioExecutionRecord.decision_id.in_(ids)
                        )
                    )
                ).scalars()
            )
            allocation_by_decision: dict[str, list[dict[str, Any]]] = {}
            for row in allocation_rows:
                allocation_by_decision.setdefault(row.decision_id, []).append(
                    {**row.payload, "action_id": row.id, "status": row.status}
                )
            # Older Portfolio-v1 rows were written before raw AI allocations
            # were persisted in the child table.  Reconstruct those intents
            # from the immutable decision payload for the read-only audit API;
            # this keeps historical decisions explainable without mutating
            # production data during a GET request.
            for record in decisions:
                existing_ids = {
                    str(item.get("allocation_id") or item.get("action_id"))
                    for item in allocation_by_decision.get(record.id, [])
                }
                raw_allocations = record.payload.get("allocations", [])
                if not isinstance(raw_allocations, list):
                    continue
                for raw in raw_allocations:
                    if not isinstance(raw, dict):
                        continue
                    allocation_id = str(raw.get("allocation_id", ""))
                    if not allocation_id or allocation_id in existing_ids:
                        continue
                    allocation_by_decision.setdefault(record.id, []).append(
                        {
                            **raw,
                            "action_id": allocation_id,
                            "status": "MODEL_INTENT",
                            "source": "decision_payload",
                        }
                    )
                    existing_ids.add(allocation_id)
            execution_by_action = {
                row.id: {
                    "status": row.status,
                    "order_ids": row.order_ids,
                    "detail": row.payload.get("detail"),
                }
                for row in execution_rows
            }
            return [
                {
                    "id": record.id,
                    "status": record.status,
                    "market_regime": record.market_regime,
                    "portfolio_risk_budget_fraction": str(record.risk_budget_fraction),
                    "payload": record.payload,
                    "prompt_version": record.prompt_version,
                    "model_name": record.model_name,
                    "created_at": record.created_at,
                    "allocations": [
                        {**item, "execution": execution_by_action.get(item["action_id"])}
                        for item in allocation_by_decision.get(record.id, [])
                    ],
                }
                for record in decisions
            ]

    async def save_orders(self, orders: list[OrderState]) -> None:
        async with self.database.sessions() as session:
            for order in orders:
                record = await session.get(OrderRecord, order.client_order_id)
                if record is None:
                    record = OrderRecord(
                        client_order_id=order.client_order_id,
                        exchange_order_id=order.exchange_order_id,
                        symbol=order.symbol,
                        status=order.status.value,
                        portfolio_decision_id=(
                            str(order.portfolio_decision_id)
                            if order.portfolio_decision_id is not None
                            else None
                        ),
                        portfolio_allocation_id=(
                            str(order.portfolio_allocation_id)
                            if order.portfolio_allocation_id is not None
                            else None
                        ),
                        action_sequence=order.action_sequence,
                        payload=order.model_dump(mode="json"),
                    )
                    session.add(record)
                else:
                    record.exchange_order_id = order.exchange_order_id
                    record.status = order.status.value
                    record.portfolio_decision_id = (
                        str(order.portfolio_decision_id)
                        if order.portfolio_decision_id is not None
                        else None
                    )
                    record.portfolio_allocation_id = (
                        str(order.portfolio_allocation_id)
                        if order.portfolio_allocation_id is not None
                        else None
                    )
                    record.action_sequence = order.action_sequence
                    record.payload = order.model_dump(mode="json")
                    record.updated_at = datetime.now(UTC)
            await session.commit()

    def _income_ledger_window(self, start_date: date, end_date: date) -> tuple[datetime, datetime]:
        timezone = ZoneInfo(self.timezone_name)
        start = datetime(
            start_date.year,
            start_date.month,
            start_date.day,
            tzinfo=timezone,
        ).astimezone(UTC)
        end_day = end_date + timedelta(days=1)
        end = datetime(
            end_day.year,
            end_day.month,
            end_day.day,
            tzinfo=timezone,
        ).astimezone(UTC)
        return start, end

    @staticmethod
    def _income_event_time(record: IncomeLedgerRecord) -> datetime:
        event_time = record.event_time
        return event_time if event_time.tzinfo is not None else event_time.replace(tzinfo=UTC)

    async def _income_ledger_records(
        self, start_date: date, end_date: date
    ) -> list[IncomeLedgerRecord]:
        start, end = self._income_ledger_window(start_date, end_date)
        async with self.database.sessions() as session:
            result = await session.execute(
                select(IncomeLedgerRecord)
                .where(
                    IncomeLedgerRecord.event_time >= start,
                    IncomeLedgerRecord.event_time < end,
                )
                .order_by(desc(IncomeLedgerRecord.event_time), desc(IncomeLedgerRecord.id))
            )
            return list(result.scalars())

    async def save_income_ledger(self, rows: list[dict[str, object]]) -> int:
        if not rows:
            return 0
        inserted = 0
        async with self.database.sessions() as session:
            for row in rows:
                income_id = str(row["income_id"])
                if await session.get(IncomeLedgerRecord, income_id) is not None:
                    continue
                event_time = row.get("event_time")
                if not isinstance(event_time, datetime):
                    raise ValueError("income ledger event_time must be a datetime")
                payload = row.get("payload", {})
                if not isinstance(payload, dict):
                    payload = {}
                session.add(
                    IncomeLedgerRecord(
                        id=income_id,
                        symbol=str(row.get("symbol", "")),
                        income_type=str(row["income_type"]),
                        income=Decimal(str(row["income"])),
                        asset=str(row.get("asset", "USDT")),
                        trade_id=str(row["trade_id"]) if row.get("trade_id") else None,
                        event_time=event_time,
                        payload=payload,
                    )
                )
                inserted += 1
            await session.commit()
        return inserted

    async def list_income_ledger(
        self, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        records = await self._income_ledger_records(start_date, end_date)
        return [
            {
                "income_id": record.id,
                "symbol": record.symbol,
                "income_type": record.income_type,
                "income": str(record.income),
                "asset": record.asset,
                "trade_id": record.trade_id,
                "event_time": self._income_event_time(record),
            }
            for record in records
        ]

    async def list_daily_pnl(
        self, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        records = await self._income_ledger_records(start_date, end_date)
        supported_types = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}
        timezone = ZoneInfo(self.timezone_name)
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for record in records:
            if record.income_type not in supported_types:
                continue
            local_date = self._income_event_time(record).astimezone(timezone).date().isoformat()
            key = (local_date, record.asset)
            row = grouped.setdefault(
                key,
                {
                    "date": local_date,
                    "asset": record.asset,
                    "realized_pnl": Decimal("0"),
                    "commission": Decimal("0"),
                    "funding_fee": Decimal("0"),
                    "event_count": 0,
                },
            )
            if record.income_type == "REALIZED_PNL":
                row["realized_pnl"] += record.income
            elif record.income_type == "COMMISSION":
                row["commission"] += record.income
            else:
                row["funding_fee"] += record.income
            row["event_count"] += 1
        result: list[dict[str, Any]] = []
        for row in grouped.values():
            realized = row["realized_pnl"]
            commission = row["commission"]
            funding = row["funding_fee"]
            result.append(
                {
                    "date": row["date"],
                    "asset": row["asset"],
                    "realized_pnl": str(realized),
                    "commission": str(commission),
                    "funding_fee": str(funding),
                    "net_pnl": str(realized + commission + funding),
                    "event_count": row["event_count"],
                }
            )
        return sorted(result, key=lambda row: (row["date"], row["asset"]), reverse=True)

    async def list_trade_pnl(
        self, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        records = await self._income_ledger_records(start_date, end_date)
        supported_types = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for record in records:
            if record.income_type not in supported_types or not record.trade_id:
                continue
            key = (record.symbol, record.trade_id, record.asset)
            event_time = self._income_event_time(record)
            row = grouped.setdefault(
                key,
                {
                    "symbol": record.symbol,
                    "trade_id": record.trade_id,
                    "asset": record.asset,
                    "realized_pnl": Decimal("0"),
                    "commission": Decimal("0"),
                    "funding_fee": Decimal("0"),
                    "event_count": 0,
                    "first_event_at": event_time,
                    "last_event_at": event_time,
                },
            )
            if record.income_type == "REALIZED_PNL":
                row["realized_pnl"] += record.income
            elif record.income_type == "COMMISSION":
                row["commission"] += record.income
            else:
                row["funding_fee"] += record.income
            row["event_count"] += 1
            row["first_event_at"] = min(row["first_event_at"], event_time)
            row["last_event_at"] = max(row["last_event_at"], event_time)
        result: list[dict[str, Any]] = []
        for row in grouped.values():
            realized = row["realized_pnl"]
            commission = row["commission"]
            funding = row["funding_fee"]
            result.append(
                {
                    "symbol": row["symbol"],
                    "trade_id": row["trade_id"],
                    "asset": row["asset"],
                    "realized_pnl": str(realized),
                    "commission": str(commission),
                    "funding_fee": str(funding),
                    "net_pnl": str(realized + commission + funding),
                    "event_count": row["event_count"],
                    "first_event_at": row["first_event_at"],
                    "last_event_at": row["last_event_at"],
                }
            )
        return sorted(result, key=lambda row: row["last_event_at"], reverse=True)

    async def sync_positions(self, positions: list[PositionState]) -> None:
        active_ids = {position.position_id for position in positions}
        async with self.database.sessions() as session:
            result = await session.execute(
                select(PositionRecord).where(PositionRecord.status == "OPEN")
            )
            for record in result.scalars():
                if record.id not in active_ids:
                    record.status = "CLOSED"
                    record.closed_at = datetime.now(UTC)
            for position in positions:
                existing_position = await session.get(PositionRecord, position.position_id)
                if existing_position is None:
                    session.add(
                        PositionRecord(
                            id=position.position_id,
                            symbol=position.symbol,
                            side=position.side.value,
                            status="OPEN",
                            payload=position.model_dump(mode="json"),
                            opened_at=position.opened_at,
                        )
                    )
                else:
                    existing_position.status = "OPEN"
                    existing_position.payload = position.model_dump(mode="json")
                    existing_position.closed_at = None
            await session.commit()

    async def hydrate_positions(self, positions: list[PositionState]) -> list[PositionState]:
        if not positions:
            return []
        async with self.database.sessions() as session:
            records = {
                record.id: record
                for record in (
                    await session.execute(
                        select(PositionRecord).where(PositionRecord.status == "OPEN")
                    )
                ).scalars()
            }
        hydrated: list[PositionState] = []
        for position in positions:
            record = records.get(position.position_id)
            if record is not None:
                previous = PositionState.model_validate(record.payload)
                original_stop = previous.original_stop_price or previous.stop_price
                initial_quantity = previous.initial_quantity or previous.quantity
                risk_per_unit = abs(position.entry_price - original_stop)
                direction = Decimal("1") if position.side == PositionSide.LONG else Decimal("-1")
                current_r = (
                    (position.mark_price - position.entry_price) * direction / risk_per_unit
                    if risk_per_unit > 0
                    else Decimal("0")
                )
                if position.tp1_status_known:
                    tp1_completed = position.tp1_completed
                elif position.tp1_price is not None:
                    tp1_completed = False
                elif position.tp2_price is not None:
                    tp1_completed = True
                else:
                    # Binance can briefly omit both Algo TP orders after a
                    # fill/cancellation.  Preserve the last exchange-verified
                    # stage only while this snapshot is explicitly unknown.
                    tp1_completed = previous.tp1_completed
                position = position.model_copy(
                    update={
                        "initial_quantity": initial_quantity,
                        "original_stop_price": original_stop,
                        "initial_risk_usdt": initial_quantity * risk_per_unit,
                        "current_r": current_r,
                        "opened_at": previous.opened_at,
                        "tp1_completed": tp1_completed,
                    }
                )
            hydrated.append(position)
        return hydrated

    async def known_open_position_keys(self) -> set[tuple[str, str]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(PositionRecord).where(PositionRecord.status == "OPEN")
            )
            return {(record.symbol, record.side) for record in result.scalars()}

    async def list_signals(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            result = await session.execute(select(SignalRecord))
            signal_records = list(result.scalars())
            signal_rows: list[tuple[SignalRecord, str | datetime, datetime]] = []
            for record in signal_records:
                payload = dict(record.payload)
                persisted_time = record.created_at
                if persisted_time.tzinfo is None:
                    persisted_time = persisted_time.replace(tzinfo=UTC)
                signal_created_at: str | datetime = record.created_at
                sort_time = persisted_time
                raw_signal_time = payload.get("created_at")
                if isinstance(raw_signal_time, str) and raw_signal_time.strip():
                    try:
                        parsed_signal_time = datetime.fromisoformat(
                            raw_signal_time.replace("Z", "+00:00")
                        )
                        if parsed_signal_time.tzinfo is None:
                            parsed_signal_time = parsed_signal_time.replace(tzinfo=UTC)
                        if abs(parsed_signal_time - persisted_time) <= timedelta(hours=2):
                            signal_created_at = raw_signal_time
                            sort_time = parsed_signal_time
                    except ValueError:
                        pass
                signal_rows.append((record, signal_created_at, sort_time))
            # Persistence can lag the model response, so database insertion
            # order is not reliable for the legacy timeline. Sort by the
            # effective signal-generation time before applying the API limit.
            signal_rows.sort(key=lambda item: item[2], reverse=True)
            signal_rows = signal_rows[:limit]
            signal_records = [item[0] for item in signal_rows]
            signal_ids = [record.id for record in signal_records]
            risk_by_signal: dict[str, RiskDecisionRecord | None] = {}
            if signal_ids:
                risk_result = await session.execute(
                    select(RiskDecisionRecord)
                    .where(RiskDecisionRecord.signal_id.in_(signal_ids))
                    .order_by(desc(RiskDecisionRecord.created_at))
                )
                for risk_record in risk_result.scalars():
                    risk_by_signal.setdefault(risk_record.signal_id, risk_record)
            rows: list[dict[str, Any]] = []
            for record, signal_created_at, _ in signal_rows:
                payload = dict(record.payload)
                raw_codes = _raw_reason_codes(record.payload)
                reason_codes_zh = _reason_codes_zh(record.payload, REASON_LABELS_ZH)
                raw_risk_flags = _raw_codes(payload.get("risk_flags", []))
                reason_zh = _signal_reason_zh(
                    record.status,
                    str(record.payload.get("action", "")),
                    reason_codes_zh,
                )
                recommendation_zh = _signal_recommendation_zh(
                    record.status,
                    str(record.payload.get("action", "")),
                    raw_codes,
                )
                risk_record_for_signal = risk_by_signal.get(record.id)
                risk_decision: dict[str, Any] | None = None
                if risk_record_for_signal is not None:
                    risk_payload = dict(risk_record_for_signal.payload)
                    risk_reasons = _raw_codes(risk_record_for_signal.reasons)
                    risk_payload.update(
                        {
                            "decision_id": risk_record_for_signal.id,
                            "signal_id": risk_record_for_signal.signal_id,
                            "status": risk_record_for_signal.status,
                            "reasons": risk_reasons,
                            "reasons_zh": _reason_codes_zh_from_codes(
                                risk_reasons, REASON_LABELS_ZH
                            ),
                        }
                    )
                    risk_decision = risk_payload
                rows.append(
                    {
                        **payload,
                        "id": record.id,
                        "result": record.status,
                        "entry_min": payload.get("entry_min"),
                        "entry_max": payload.get("entry_max"),
                        "invalidation_price": payload.get("invalidation_price"),
                        "target_price": payload.get("target_price"),
                        "horizon_minutes": payload.get("horizon_minutes", 240),
                        "expires_at": payload.get("expires_at"),
                        "thesis": payload.get("thesis", ""),
                        "reason_codes": raw_codes,
                        "reason_codes_zh": reason_codes_zh,
                        "risk_flags": raw_risk_flags,
                        "risk_flags_zh": _reason_codes_zh_from_codes(
                            raw_risk_flags, REASON_LABELS_ZH
                        ),
                        "reason_zh": reason_zh,
                        "reason": reason_zh,
                        "recommendation_zh": recommendation_zh,
                        "ai_advice": recommendation_zh,
                        "market_context": payload.get("market_context")
                        if isinstance(payload.get("market_context"), dict)
                        else None,
                        "risk_decision": risk_decision,
                        "created_at": signal_created_at,
                    }
                )
            return rows

    async def get_model_replay_cache(self, input_hash: str) -> dict[str, object] | None:
        async with self.database.sessions() as session:
            record = await session.get(ModelReplayCacheRecord, input_hash)
            return dict(record.output) if record is not None else None

    async def save_model_replay_cache(
        self,
        input_hash: str,
        *,
        prompt_version: str,
        model_name: str,
        output: dict[str, object],
    ) -> None:
        async with self.database.sessions() as session:
            record = await session.get(ModelReplayCacheRecord, input_hash)
            if record is None:
                session.add(
                    ModelReplayCacheRecord(
                        input_hash=input_hash,
                        prompt_version=prompt_version,
                        model_name=model_name,
                        output=output,
                    )
                )
                await session.commit()

    async def list_orders(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(OrderRecord).order_by(desc(OrderRecord.updated_at)).limit(limit)
            )
            return [
                {
                    **record.payload,
                    "client_order_id": record.client_order_id,
                    "status": record.status,
                    "portfolio_decision_id": record.portfolio_decision_id,
                    "portfolio_allocation_id": record.portfolio_allocation_id,
                    "action_sequence": record.action_sequence,
                    "updated_at": record.updated_at,
                }
                for record in result.scalars()
            ]

    async def latest_market(self, limit: int = 30) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(MarketFeatureRecord)
                .order_by(desc(MarketFeatureRecord.timestamp))
                .limit(limit * 4)
            )
            rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            for record in result.scalars():
                if record.symbol in seen:
                    continue
                seen.add(record.symbol)
                rows.append({**record.payload, "timestamp": record.timestamp})
                if len(rows) >= limit:
                    break
            return rows

    async def create_replay(self, parameters: dict[str, object]) -> str:
        async with self.database.sessions() as session:
            record = ReplayRunRecord(status="QUEUED", parameters=parameters, metrics={})
            session.add(record)
            await session.commit()
            return record.id

    async def set_replay_running(self, replay_id: str) -> None:
        async with self.database.sessions() as session:
            record = await session.get(ReplayRunRecord, replay_id)
            if record is not None:
                record.status = "RUNNING"
                await session.commit()

    async def complete_replay(self, replay_id: str, metrics: dict[str, object]) -> None:
        async with self.database.sessions() as session:
            record = await session.get(ReplayRunRecord, replay_id)
            if record is not None:
                record.status = "COMPLETED"
                record.metrics = metrics
                record.completed_at = datetime.now(UTC)
                await session.commit()

    async def fail_replay(self, replay_id: str, reason: str) -> None:
        async with self.database.sessions() as session:
            record = await session.get(ReplayRunRecord, replay_id)
            if record is not None:
                record.status = "FAILED"
                record.metrics = {"error": reason}
                record.completed_at = datetime.now(UTC)
                await session.commit()

    async def list_replays(self, limit: int = 30) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(ReplayRunRecord).order_by(desc(ReplayRunRecord.created_at)).limit(limit)
            )
            return [
                {
                    "id": record.id,
                    "status": record.status,
                    "parameters": record.parameters,
                    "metrics": record.metrics,
                    "created_at": record.created_at,
                    "completed_at": record.completed_at,
                }
                for record in result.scalars()
            ]
