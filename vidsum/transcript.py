"""Транскрипт: модель сегмента, парсеры субтитров, нарезка на окна."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass


@dataclass
class Segment:
    """Кусок расшифровки с привязкой ко времени."""

    start: float  # секунды от начала видео
    text: str


def fmt_ts(seconds: float) -> str:
    """123.4 -> '02:03', 3723 -> '1:02:03'."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# --- json3 (родной формат YouTube, самый чистый) ---------------------------


def parse_json3(raw: str) -> list[Segment]:
    data = json.loads(raw)
    segments: list[Segment] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs)
        text = text.replace("​", "").strip()
        if not text or text == "\n":
            continue
        start = event.get("tStartMs", 0) / 1000.0
        segments.append(Segment(start=start, text=text))
    return _dedupe(segments)


# --- WebVTT / SRT ----------------------------------------------------------

_VTT_TIME = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
_TAG = re.compile(r"<[^>]+>")


def _vtt_seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_vtt(raw: str) -> list[Segment]:
    """Парсит WebVTT или SRT. Теги вроде <c> и <00:00:01.000> вычищаются."""
    segments: list[Segment] = []
    start: float | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal start, buf
        if start is not None and buf:
            text = " ".join(buf).strip()
            text = re.sub(r"\s+", " ", text)
            if text:
                segments.append(Segment(start=start, text=text))
        start, buf = None, []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            flush()
            continue
        match = _VTT_TIME.search(line)
        if match:
            flush()
            start = _vtt_seconds(*match.groups()[:4])
            continue
        if line in ("WEBVTT",) or line.startswith(("NOTE", "Kind:", "Language:")):
            continue
        if start is None and line.isdigit():
            continue  # порядковый номер в SRT
        if start is not None:
            buf.append(html.unescape(_TAG.sub("", line)))
    flush()
    return _dedupe(segments)


def _dedupe(segments: list[Segment]) -> list[Segment]:
    """Убирает «бегущую строку» автосубтитров: повторы и куски-префиксы."""
    out: list[Segment] = []
    for seg in segments:
        if not out:
            out.append(seg)
            continue
        prev = out[-1]
        if seg.text == prev.text:
            continue
        # Автосубтитры повторяют предыдущую строку целиком, дописывая хвост.
        if seg.text.startswith(prev.text) and len(prev.text) > 8:
            out[-1] = Segment(start=prev.start, text=seg.text)
            continue
        if prev.text.endswith(seg.text) and len(seg.text) > 8:
            continue
        out.append(seg)
    return out


def parse_subtitles(raw: str, ext: str) -> list[Segment]:
    if ext == "json3":
        return parse_json3(raw)
    if ext in ("vtt", "srt", "srv1", "srv2", "srv3", "ttml"):
        return parse_vtt(raw)
    raise ValueError(f"Неизвестный формат субтитров: {ext}")


# --- Подготовка текста для модели ------------------------------------------


def to_prompt_text(segments: list[Segment], *, stamp_every: float = 30.0) -> str:
    """Склеивает сегменты, вставляя таймкоды не чаще, чем раз в stamp_every секунд.

    Таймкоды нужны, чтобы модель могла сослаться на момент в видео; вставлять
    их у каждой строки — лишние токены и шум.
    """
    lines: list[str] = []
    next_stamp = 0.0
    for seg in segments:
        if seg.start >= next_stamp:
            lines.append(f"[{fmt_ts(seg.start)}] {seg.text}")
            next_stamp = seg.start + stamp_every
        else:
            lines.append(seg.text)
    return "\n".join(lines)


def duration_of(segments: list[Segment]) -> float:
    return segments[-1].start if segments else 0.0


def chunk_segments(
    segments: list[Segment], *, chars_per_chunk: int, overlap_chars: int = 2000
) -> list[list[Segment]]:
    """Режет транскрипт на окна по объёму текста, с небольшим перехлёстом.

    Перехлёст нужен, чтобы мысль, начатая в конце окна, не потерялась.
    """
    if not segments:
        return []
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    fresh = 0  # сегментов, добавленных после последней нарезки
    for seg in segments:
        current.append(seg)
        size += len(seg.text) + 1
        fresh += 1
        if size >= chars_per_chunk:
            chunks.append(current)
            tail: list[Segment] = []
            tail_size = 0
            for prev in reversed(current):
                if tail_size >= overlap_chars:
                    break
                tail.insert(0, prev)
                tail_size += len(prev.text) + 1
            current = list(tail)
            size = tail_size
            fresh = 0
    if fresh:  # хвост, а не только перехлёст предыдущего окна
        chunks.append(current)
    return chunks
