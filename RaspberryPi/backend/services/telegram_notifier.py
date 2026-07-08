from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_PATH = _BACKEND_DIR / "config" / "telegram_config.json"
_DEFAULT_EXAMPLE_PATH = _BACKEND_DIR / "config" / "telegram_config.example.json"

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 5


class TelegramNotifier:
    """Sends trigger alerts to a Telegram chat via the Bot API.

    Reads `bot_token`/`chat_id` from `config/telegram_config.json` (auto-created,
    gitignored). `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` environment variables
    take precedence when set. No-ops (with a one-time logged warning) until both
    values are configured, so the rest of the system works before Telegram is set up.
    """

    def __init__(
        self,
        config_path: str | Path = _DEFAULT_CONFIG_PATH,
        example_path: str | Path = _DEFAULT_EXAMPLE_PATH,
    ) -> None:
        self._config_path = Path(config_path)
        self._example_path = Path(example_path)
        self._warned_unconfigured = False
        self.bot_token, self.chat_id = self._load_credentials()

    def _load_credentials(self) -> tuple[str, str]:
        if not self._config_path.exists() and self._example_path.exists():
            os.makedirs(self._config_path.parent, exist_ok=True)
            shutil.copyfile(self._example_path, self._config_path)

        bot_token = ""
        chat_id = ""
        if self._config_path.exists():
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                bot_token = str(payload.get("bot_token") or "")
                chat_id = str(payload.get("chat_id") or "")
            except (OSError, ValueError):
                pass

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", bot_token)
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", chat_id)
        return bot_token, chat_id

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _send_sync(self, message: str) -> bool:
        url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
        try:
            response = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": message},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                logger.warning("Telegram sendMessage failed (%s): %s", response.status_code, response.text)
                return False
            return True
        except requests.RequestException:
            logger.exception("Telegram sendMessage request failed")
            return False

    async def send(self, message: str) -> bool:
        if not self.is_configured:
            if not self._warned_unconfigured:
                logger.warning(
                    "Telegram bot_token/chat_id not configured; skipping alert. "
                    "Fill in %s (or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID env vars) to enable notifications.",
                    self._config_path,
                )
                self._warned_unconfigured = True
            return False
        return await asyncio.to_thread(self._send_sync, message)
