from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading_system.config import Settings
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.notifications.telegram import TelegramNotifier
from trading_system.persistence.repository import Repository


class TelegramReportScheduler:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        exchange: BinanceUSDMarketClient,
        notifier: TelegramNotifier,
        *,
        interval_seconds: float = 60,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.exchange = exchange
        self.notifier = notifier
        self.interval_seconds = interval_seconds
        self.timezone = ZoneInfo(settings.app_timezone)
        self.last_daily: date | None = None
        self.last_weekly: tuple[int, int] | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                pass
            await asyncio.sleep(self.interval_seconds)

    async def run_once(self, now: datetime | None = None) -> None:
        if not self.notifier.enabled or not self.exchange.configured:
            return
        local_now = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
        if local_now.hour < 9:
            return
        if self.last_daily != local_now.date():
            await self._send_daily(local_now)
            self.last_daily = local_now.date()
        week = local_now.isocalendar()[:2]
        if local_now.weekday() == 0 and self.last_weekly != week:
            await self._send_weekly(local_now)
            self.last_weekly = week

    async def _send_daily(self, now: datetime) -> None:
        account = await self.exchange.get_account_state()
        await self.repository.save_income_ledger(self.exchange.last_income_ledger)
        account = await self.repository.apply_equity_checkpoints(account, record_history=False)
        positions = await self.exchange.get_positions()
        daily_pnl = account.equity - account.day_start_equity
        await self.notifier.send(
            "每日交易简报",
            (
                f"北京时间 {now:%Y-%m-%d %H:%M}\n"
                f"净值 {account.equity} USDT\n"
                f"当日净值变动 {daily_pnl} USDT\n"
                f"已实现 PnL {account.realized_pnl_today} USDT\n"
                f"手续费 {account.fees_today} USDT，资金费 {account.funding_today} USDT\n"
                f"回撤 {account.drawdown_pct * Decimal('100'):.2f}%\n"
                f"持仓 {len(positions)} 个"
            ),
        )

    async def _send_weekly(self, now: datetime) -> None:
        replays = await self.repository.list_replays(limit=5)
        audits = await self.repository.list_audit(limit=20)
        await self.notifier.send(
            "每周运行复盘",
            (
                f"第 {now.isocalendar().week} 周\n"
                f"最近回放任务 {len(replays)} 个\n"
                f"最近审计事件 {len(audits)} 条\n"
                "请在控制台检查收益、费用、拒绝分布和故障记录。"
            ),
        )
