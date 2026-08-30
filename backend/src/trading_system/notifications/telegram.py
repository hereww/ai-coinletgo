from __future__ import annotations

import httpx

from trading_system.config import Settings


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.token = settings.read_secret("telegram_bot_token")
        self.chat_id = settings.read_secret("telegram_chat_id")
        self.enabled = settings.telegram_enabled and bool(self.token and self.chat_id)
        self.http = httpx.AsyncClient(timeout=10)

    async def close(self) -> None:
        await self.http.aclose()

    async def send(self, title: str, message: str) -> bool:
        if not self.enabled:
            return False
        response = await self.http.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": f"{title}\n{message}",
                "disable_web_page_preview": True,
            },
        )
        return response.is_success
