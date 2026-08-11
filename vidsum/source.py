"""Получение метаданных и расшифровки видео через yt-dlp.

Порядок: субтитры автора → автосубтитры → локальный ASR (если включён).
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

from .config import config
from .transcript import Segment, parse_subtitles

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
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


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


def fetch(url: str) -> FetchedVideo:
    """Скачивает метаданные и расшифровку. Бросает FetchError с текстом для пользователя."""
    with _ydl() as ydl:
        return _fetch_with(ydl, url)


def _fetch_with(ydl: yt_dlp.YoutubeDL, url: str) -> FetchedVideo:
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

    segments = _transcribe(url, meta)
    if segments:
        return FetchedVideo(meta, segments, f"ASR ({config.whisper_model})")

    raise FetchError(
        "У видео нет субтитров, а локальное распознавание выключено.\n"
        "Включите его: ASR_BACKEND=faster-whisper в .env "
        "(нужны ffmpeg и пакет faster-whisper)."
    )


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
            "YouTube просит подтвердить, что вы не бот. "
            "Помогает запуск с cookies браузера — см. README."
        )
    return f"Не удалось получить видео: {message.splitlines()[0][:300]}"


def _transcribe(url: str, meta: VideoMeta) -> list[Segment]:
    """Локальное распознавание речи, если субтитров нет и бэкенд включён."""
    if config.asr_backend != "faster-whisper":
        return []
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise FetchError(
            "ASR_BACKEND=faster-whisper, но пакет faster-whisper не установлен: "
            "pip install faster-whisper"
        ) from exc

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "audio.%(ext)s"
        opts = {
            "skip_download": False,
            "format": "bestaudio/best",
            "outtmpl": str(target),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "64"}
            ],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = list(Path(tmp).glob("audio.*"))
        if not files:
            raise FetchError("Не удалось скачать аудиодорожку для распознавания.")

        log.info("Распознаю аудио: %s (%s c)", meta.title, meta.duration)
        model = WhisperModel(config.whisper_model, compute_type="int8")
        chunks, _ = model.transcribe(str(files[0]), vad_filter=True)
        return [Segment(start=c.start, text=c.text.strip()) for c in chunks if c.text.strip()]
