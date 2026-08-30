from enum import StrEnum


class SystemMode(StrEnum):
    TESTNET = "TESTNET"
    LIVE_LOCKED = "LIVE_LOCKED"
    LIVE_ENABLED = "LIVE_ENABLED"
    PAUSED = "PAUSED"
    RISK_HALTED = "RISK_HALTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalAction(StrEnum):
    NO_TRADE = "NO_TRADE"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"


class ReviewAction(StrEnum):
    HOLD = "HOLD"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    CLOSE = "CLOSE"
    TIGHTEN_STOP = "TIGHTEN_STOP"


class DecisionStatus(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PortfolioTargetSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class PortfolioPlanActionType(StrEnum):
    OPEN = "OPEN"
    ADD = "ADD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    HOLD = "HOLD"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    REJECTED = "REJECTED"


class PortfolioPlanStatus(StrEnum):
    APPROVED = "APPROVED"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"
    NO_ACTION = "NO_ACTION"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
