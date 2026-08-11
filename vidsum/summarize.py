"""Саммаризация через Claude.

Короткие и средние видео идут одним проходом. Длинные — через карту:
выжимка по каждому окну, затем сборка в один формат.
"""

from __future__ import annotations

import logging

import anthropic

from . import prompts
from .config import config
from .models import Summary
from .source import FetchedVideo, VideoMeta
from .transcript import Segment, chunk_segments, duration_of, fmt_ts, to_prompt_text

log = logging.getLogger(__name__)

# Ответ со схемой плюс адаптивное рассуждение. Запас нужен, потому что
# max_tokens ограничивает рассуждение и ответ вместе.
_MAX_TOKENS = 24_000
_MAP_MAX_TOKENS = 16_000
_QA_MAX_TOKENS = 4_000

# При max_tokens выше ~21k SDK отказывается делать нестриминговый запрос, пока
# таймаут не задан явно. Задаём — с запасом на длинную выжимку.
_TIMEOUT = 900.0

# Транскрипт грубо: ~3.5 символа на токен. Точный подсчёт делает count_tokens,
# эта оценка — только чтобы решить, звать ли его вообще.
_CHARS_PER_TOKEN = 3.5


def _client() -> anthropic.Anthropic:
    kwargs: dict = {"timeout": _TIMEOUT}
    if config.anthropic_api_key:
        kwargs["api_key"] = config.anthropic_api_key
    return anthropic.Anthropic(**kwargs)


def _count_tokens(client: anthropic.Anthropic, text: str) -> int:
    approx = int(len(text) / _CHARS_PER_TOKEN)
    if approx < config.max_single_pass_tokens * 0.7:
        return approx  # заведомо влезает, точный подсчёт не нужен
    try:
        result = client.messages.count_tokens(
            model=config.model, messages=[{"role": "user", "content": text}]
        )
        return int(result.input_tokens or approx)
    except Exception:  # noqa: BLE001 — оценка не критична, ошибку не эскалируем
        log.warning("count_tokens не сработал, использую оценку", exc_info=True)
        return approx


def _author_chapters_block(meta: VideoMeta) -> str:
    if not meta.chapters:
        return ""
    lines = "\n".join(f"{fmt_ts(ch.start)} — {ch.title}" for ch in meta.chapters)
    return prompts.AUTHOR_CHAPTERS_BLOCK.format(chapters=lines)


def _video_fields(video: FetchedVideo) -> dict[str, str]:
    meta = video.meta
    duration = meta.duration or int(duration_of(video.segments))
    return {
        "title": meta.title,
        "channel": meta.channel,
        "duration": fmt_ts(duration) if duration else "неизвестна",
        "upload_date": meta.upload_date or "неизвестна",
        "transcript_source": video.transcript_source,
        "author_chapters": _author_chapters_block(meta),
    }


def summarize(video: FetchedVideo) -> Summary:
    """Строит саммари по формату. Выбирает один проход или карту по объёму."""
    client = _client()
    transcript = to_prompt_text(video.segments)
    tokens = _count_tokens(client, transcript)
    log.info("Транскрипт: %s сегментов, ~%s токенов", len(video.segments), tokens)

    if tokens > config.max_single_pass_tokens:
        return _summarize_long(client, video)
    return _summarize_single(client, video, transcript)


def _summarize_single(
    client: anthropic.Anthropic, video: FetchedVideo, transcript: str
) -> Summary:
    system = prompts.SUMMARY_SYSTEM.format(lang=prompts.lang_name(config.summary_lang))
    user = prompts.SUMMARY_USER.format(transcript=transcript, **_video_fields(video))

    response = client.messages.parse(
        model=config.model,
        max_tokens=_MAX_TOKENS,
        output_config={"effort": config.effort},
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=Summary,
        timeout=_TIMEOUT,
    )
    return _unwrap(response)


def _summarize_long(client: anthropic.Anthropic, video: FetchedVideo) -> Summary:
    chars = int(config.chunk_tokens * _CHARS_PER_TOKEN)
    windows = chunk_segments(video.segments, chars_per_chunk=chars)
    log.info("Длинное видео: %s окон", len(windows))

    map_system = prompts.MAP_SYSTEM.format(lang=prompts.lang_name(config.summary_lang))
    notes: list[str] = []
    for index, window in enumerate(windows, start=1):
        user = prompts.MAP_USER.format(
            title=video.meta.title,
            index=index,
            total=len(windows),
            start=fmt_ts(window[0].start),
            end=fmt_ts(window[-1].start),
            transcript=to_prompt_text(window),
        )
        response = client.messages.create(
            model=config.model,
            max_tokens=_MAP_MAX_TOKENS,
            output_config={"effort": "medium"},  # выжимка проще сборки
            system=map_system,
            messages=[{"role": "user", "content": user}],
            timeout=_TIMEOUT,
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        notes.append(
            f"=== Фрагмент {index}/{len(windows)} "
            f"({fmt_ts(window[0].start)}–{fmt_ts(window[-1].start)}) ===\n{text}"
        )
        log.info("Окно %s/%s готово", index, len(windows))

    system = prompts.SUMMARY_SYSTEM.format(lang=prompts.lang_name(config.summary_lang))
    user = prompts.REDUCE_USER.format(notes="\n\n".join(notes), **_video_fields(video))
    response = client.messages.parse(
        model=config.model,
        max_tokens=_MAX_TOKENS,
        output_config={"effort": config.effort},
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=Summary,
        timeout=_TIMEOUT,
    )
    return _unwrap(response)


def _unwrap(response) -> Summary:
    if response.stop_reason == "refusal":
        raise RuntimeError(
            "Модель отказалась разбирать это видео "
            f"({getattr(response.stop_details, 'category', 'без категории')})."
        )
    if response.parsed_output is None:
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                "Ответ не поместился в лимит. Видео слишком длинное — "
                "уменьшите max_single_pass_tokens, чтобы включилась обработка по частям."
            )
        raise RuntimeError(f"Не удалось разобрать ответ модели (stop_reason={response.stop_reason}).")
    return response.parsed_output


def ask(video_title: str, channel: str, duration: int, transcript: str, question: str) -> str:
    """Отвечает на вопрос по расшифровке. Транскрипт кэшируется между вопросами."""
    client = _client()
    system = prompts.QA_SYSTEM.format(
        lang=prompts.lang_name(config.summary_lang),
        title=video_title,
        channel=channel,
        duration=fmt_ts(duration) if duration else "неизвестна",
        transcript=transcript,
    )
    response = client.messages.create(
        model=config.model,
        max_tokens=_QA_MAX_TOKENS,
        output_config={"effort": "medium"},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": question}],
        timeout=_TIMEOUT,
    )
    if response.stop_reason == "refusal":
        return "Не могу ответить на этот вопрос."
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or "Пустой ответ модели."


def transcript_text(segments: list[Segment]) -> str:
    return to_prompt_text(segments)
