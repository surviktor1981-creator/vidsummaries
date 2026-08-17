"""Получение метаданных и расшифровки видео через yt-dlp.

Порядок: субтитры автора → автосубтитры → локальный ASR (если включён).
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

from .config import config
from .transcript import Segment, fmt_ts, parse_subtitles

log = logging.getLogger(__name__)

# Форматы субтитров в порядке предпочтения: json3 у YouTube самый чистый,
# в vtt автосубтитры приходят «бегущей строкой» с повторами.
_EXT_PRIORITY = ("json3", "vtt", "srv3", "srv1", "ttml")

_URL_RE = re.compile(r"https?://\S+")


def find_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    return match.group(0).rstrip(".,);") if match else None


class FetchError(RuntimeError):
    """Понятная пользователю ошибка получения видео."""


@dataclass
class AuthorChapter:
    start: int
    title: str


@dataclass
class VideoMeta:
    video_id: str
    url: str
    title: str
    channel: str
    duration: int  # секунды
    upload_date: str  # YYYY-MM-DD или ""
    extractor: str
    chapters: list[AuthorChapter] = field(default_factory=list)

    def timecode_url(self, seconds: int) -> str:
        """Ссылка на момент в видео. Для YouTube — короткая, иначе ?t=."""
        if self.extractor.startswith("youtube"):
            return f"https://youtu.be/{self.video_id}?t={int(seconds)}"
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}t={int(seconds)}"


@dataclass
class FetchedVideo:
    meta: VideoMeta
    segments: list[Segment]
    transcript_source: str  # «субтитры автора (ru)», «автосубтитры (en)», «ASR whisper»


def _ydl(extra: dict | None = None) -> yt_dlp.YoutubeDL:
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "extract_flat": False,
    }
    opts.update(_access_opts())
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def _access_opts() -> dict:
    """Обход блокировок с серверных IP.

    С датацентровых адресов YouTube регулярно требует подтвердить, что вы не
    бот. Лечится cookies залогиненного аккаунта, в тяжёлых случаях — прокси.
    """
    opts: dict = {}
    if config.cookies_file:
        opts["cookiefile"] = config.cookies_file
    if config.ytdlp_proxy:
        opts["proxy"] = config.ytdlp_proxy
    return opts


def _pick_track(
    tracks: dict[str, list[dict]], preferred: tuple[str, ...]
) -> tuple[str, dict] | None:
    """Выбирает дорожку субтитров: сначала по языку, потом по формату."""
    if not tracks:
        return None

    def by_lang(lang_prefix: str) -> list[str]:
        return [
            code
            for code in tracks
            if code.split("-")[0].removeprefix("a.").lower() == lang_prefix
        ]

    ordered: list[str] = []
    for lang in preferred:
        ordered.extend(by_lang(lang))
    ordered.extend(code for code in tracks if code not in ordered)

    for code in ordered:
        formats = tracks.get(code) or []
        for ext in _EXT_PRIORITY:
            for fmt in formats:
                if fmt.get("ext") == ext and fmt.get("url"):
                    return code, fmt
    return None


def _download_text(ydl: yt_dlp.YoutubeDL, url: str) -> str:
    with ydl.urlopen(url) as response:
        return response.read().decode("utf-8", errors="replace")


def _meta_from_info(info: dict) -> VideoMeta:
    upload = info.get("upload_date") or ""
    if len(upload) == 8:
        upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
    chapters = [
        AuthorChapter(start=int(ch.get("start_time") or 0), title=str(ch.get("title") or ""))
        for ch in (info.get("chapters") or [])
        if ch.get("title")
    ]
    return VideoMeta(
        video_id=str(info.get("id") or ""),
        url=str(info.get("webpage_url") or info.get("original_url") or ""),
        title=str(info.get("title") or "Без названия"),
        channel=str(info.get("channel") or info.get("uploader") or "—"),
        duration=int(info.get("duration") or 0),
        upload_date=upload,
        extractor=str(info.get("extractor_key") or info.get("extractor") or "").lower(),
        chapters=chapters,
    )


def fetch(url: str, on_asr_start: Callable[[int], None] | None = None) -> FetchedVideo:
    """Скачивает метаданные и расшифровку. Бросает FetchError с текстом для пользователя.

    on_asr_start вызывается с оценкой в минутах, если дело дошло до
    распознавания речи: оно идёт минутами, и пользователь должен об этом знать,
    а не смотреть в замерший экран.
    """
    with _ydl() as ydl:
        return _fetch_with(ydl, url, on_asr_start)


def _fetch_with(
    ydl: yt_dlp.YoutubeDL, url: str, on_asr_start: Callable[[int], None] | None = None
) -> FetchedVideo:
    try:
        info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise FetchError(_explain_download_error(str(exc))) from exc

    if info is None:
        raise FetchError("Не удалось прочитать эту ссылку.")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise FetchError("По ссылке плейлист без доступных видео.")
        info = entries[0]

    meta = _meta_from_info(info)
    if info.get("is_live"):
        raise FetchError("Это идущая прямо сейчас трансляция — расшифровки ещё нет.")

    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    for tracks, label in ((manual, "субтитры автора"), (auto, "автосубтитры")):
        picked = _pick_track(tracks, config.subtitle_langs)
        if not picked:
            continue
        lang, fmt = picked
        try:
            raw = _download_text(ydl, fmt["url"])
            segments = parse_subtitles(raw, fmt["ext"])
        except Exception:  # noqa: BLE001 — дорожка битая, пробуем следующий вариант
            log.warning("Не удалось разобрать дорожку %s/%s", label, lang, exc_info=True)
            continue
        if segments:
            return FetchedVideo(meta, segments, f"{label} ({lang})")

    if config.asr_backend != "off":
        segments = _transcribe(url, meta, on_asr_start)
        if segments:
            return FetchedVideo(meta, segments, f"распознавание речи ({config.whisper_model})")

    raise FetchError(_no_transcript_message(manual, auto))


def _no_transcript_message(manual: dict, auto: dict) -> str:
    """Объясняет, почему расшифровки нет, и что с этим делать."""
    if manual or auto:
        # Дорожки есть, но ни одна не разобралась — это наша проблема, не видео.
        found = ", ".join(sorted(set(manual) | set(auto))[:8])
        return (
            "У видео есть дорожки субтитров, но ни одну не удалось прочитать.\n"
            f"Найдены языки: {found}.\nПришлите ссылку ещё раз — если повторится, "
            "это баг разбора, а не проблема видео."
        )
    if config.asr_backend == "off":
        return (
            "У видео нет ни субтитров автора, ни автоматических — YouTube их не "
            "сделал.\n\nЧтобы такие видео тоже разбирались, включите распознавание "
            "речи: <code>ASR_BACKEND=faster-whisper</code> в .env на сервере, "
            "затем <code>docker compose up -d</code>."
        )
    return (
        "У видео нет субтитров, и распознать речь не удалось. Подробности в логах "
        "сервера: <code>docker compose logs --tail 50</code>."
    )


# Скорость распознавания на одном ядре относительно реального времени: во
# сколько раз быстрее звука работает модель. Числа грубые, нужны только чтобы
# назвать пользователю порядок ожидания.
_ASR_SPEED_PER_CORE = {
    "tiny": 5.0,
    "base": 2.5,
    "small": 1.0,
    "medium": 0.35,
    "large-v3": 0.15,
}


def asr_eta_minutes(duration: int, model: str | None = None, cores: int | None = None) -> int:
    """Оценка времени распознавания в минутах, с запасом в сторону пессимизма."""
    model = model or config.whisper_model
    cores = cores or (os.cpu_count() or 1)
    per_core = _ASR_SPEED_PER_CORE.get(model, 1.0)
    # Ядра помогают не линейно, а после четвёртого почти не помогают.
    speed = per_core * min(cores, 4) * 0.8
    return max(1, math.ceil(duration / speed / 60))


def _explain_download_error(message: str) -> str:
    low = message.lower()
    if "private" in low:
        return "Видео приватное — доступа к нему нет."
    if "members-only" in low or "join this channel" in low:
        return "Видео только для подписчиков канала."
    if "age" in low and "confirm" in low:
        return "Видео с возрастным ограничением — нужны cookies авторизованного аккаунта."
    if "unavailable" in low or "removed" in low:
        return "Видео недоступно или удалено."
    if "unsupported url" in low:
        return "Не знаю, как читать этот сайт."
    if "sign in to confirm" in low or "bot" in low:
        return (
            "YouTube просит подтвердить, что запрос не от бота — так бывает с "
            "серверных адресов.\nЛечится файлом cookies: положите его на сервер "
            "и укажите COOKIES_FILE в .env (см. DEPLOY.md)."
        )
    return f"Не удалось получить видео: {message.splitlines()[0][:300]}"


_model_cache: dict[str, object] = {}


def _whisper_model():
    """Модель грузится секунды и весит сотни мегабайт — держим одну на процесс."""
    if config.whisper_model in _model_cache:
        return _model_cache[config.whisper_model]
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise FetchError(
            "ASR_BACKEND=faster-whisper, но пакет не установлен. "
            "Обновите образ: docker compose up -d --build"
        ) from exc

    log.info("Загружаю модель распознавания %s", config.whisper_model)
    model = WhisperModel(
        config.whisper_model,
        device="cpu",
        compute_type=config.whisper_compute_type,
        cpu_threads=os.cpu_count() or 1,
    )
    _model_cache[config.whisper_model] = model
    return model


def _transcribe(
    url: str, meta: VideoMeta, on_asr_start: Callable[[int], None] | None = None
) -> list[Segment]:
    """Распознаёт речь, когда субтитров нет."""
    if config.asr_backend != "faster-whisper":
        raise FetchError(f"Неизвестный ASR_BACKEND: {config.asr_backend}")

    if meta.duration and meta.duration > config.asr_max_duration:
        raise FetchError(
            f"У видео нет субтитров, а его длина ({fmt_ts(meta.duration)}) выше "
            f"потолка распознавания ({fmt_ts(config.asr_max_duration)}).\n"
            "Распознавание идёт медленнее реального времени, и такое видео заняло бы "
            "бота на часы. Поднять потолок: ASR_MAX_DURATION в .env."
        )

    eta = asr_eta_minutes(meta.duration)
    if on_asr_start:
        on_asr_start(eta)
    log.info("Субтитров нет, распознаю речь: %s (%s, оценка %s мин)", meta.title, fmt_ts(meta.duration), eta)

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "audio.%(ext)s"
        # Без перекодирования в mp3: модель читает исходный поток сама, а
        # лишний проход ffmpeg на слабом сервере стоит дороже, чем экономит.
        opts = {
            "skip_download": False,
            "format": "bestaudio/best",
            "outtmpl": str(target),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            **_access_opts(),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as exc:
            raise FetchError(_explain_download_error(str(exc))) from exc

        files = [p for p in Path(tmp).iterdir() if p.is_file() and p.stat().st_size > 0]
        if not files:
            raise FetchError("Не удалось скачать аудиодорожку для распознавания.")

        started = time.monotonic()
        chunks, info = _whisper_model().transcribe(str(files[0]), vad_filter=True)
        segments = [Segment(start=c.start, text=c.text.strip()) for c in chunks if c.text.strip()]
        log.info(
            "Распознано: %s сегментов, язык %s, заняло %.0f c (оценка была %s мин)",
            len(segments),
            getattr(info, "language", "?"),
            time.monotonic() - started,
            eta,
        )
        if not segments:
            raise FetchError(
                "Распознавание не нашло речи — возможно, в видео только музыка или шум."
            )
        return segments
