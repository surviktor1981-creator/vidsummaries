"""Конфигурация из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in (p.strip() for p in raw.split(",")) if x]


@dataclass
class Config:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")

    summary_lang: str = os.getenv("SUMMARY_LANG", "ru")
    model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
    effort: str = os.getenv("EFFORT", "high")

    allowed_user_ids: list[int] = field(
        default_factory=lambda: _int_list(os.getenv("ALLOWED_USER_IDS", ""))
    )
    db_path: str = os.getenv("DB_PATH", "vidsum.db")

    asr_backend: str = os.getenv("ASR_BACKEND", "off")
    whisper_model: str = os.getenv("WHISPER_MODEL", "base")
    whisper_compute_type: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    # Распознавание на слабом сервере идёт медленнее реального времени, поэтому
    # у него есть потолок: лучше честно отказать, чем занять бота на два часа.
    asr_max_duration: int = int(os.getenv("ASR_MAX_DURATION", "5400"))

    # Обход блокировок с серверных IP: cookies залогиненного аккаунта и прокси.
    cookies_file: str = os.getenv("COOKIES_FILE", "")
    ytdlp_proxy: str = os.getenv("YTDLP_PROXY", "")

    # Языки субтитров в порядке предпочтения.
    subtitle_langs: tuple[str, ...] = ("ru", "en")

    # Выше этого объёма транскрипта включается map-reduce вместо одного прохода.
    max_single_pass_tokens: int = 180_000

    # Размер окна при map-reduce, в токенах.
    chunk_tokens: int = 40_000

    def check(self, *, need_telegram: bool = True) -> None:
        missing = []
        if need_telegram and not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.anthropic_api_key and not os.getenv("ANTHROPIC_AUTH_TOKEN"):
            # Пустой ключ допустим, если настроен профиль `ant auth login`.
            pass
        if missing:
            raise SystemExit(
                "Не заданы переменные окружения: "
                + ", ".join(missing)
                + ". Скопируйте .env.example в .env и заполните."
            )


config = Config()
