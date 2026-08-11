"""Тесты парсинга субтитров и нарезки."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vidsum.transcript import (  # noqa: E402
    Segment,
    chunk_segments,
    fmt_ts,
    parse_json3,
    parse_subtitles,
    parse_vtt,
    to_prompt_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_fmt_ts():
    assert fmt_ts(0) == "00:00"
    assert fmt_ts(123.4) == "02:03"
    assert fmt_ts(3723) == "1:02:03"


def test_parse_vtt_fixture():
    segments = parse_vtt((FIXTURES / "sample.vtt").read_text(encoding="utf-8"))
    assert len(segments) >= 15
    assert segments[0].start == 0.0
    assert "Инженерная кухня" in segments[0].text
    # Заголовки WEBVTT/Kind/Language не должны попадать в текст.
    assert not any(s.text.startswith(("WEBVTT", "Kind:", "Language:")) for s in segments)
    # Таймкоды строго возрастают.
    starts = [s.start for s in segments]
    assert starts == sorted(starts)


def test_parse_srt_with_indices():
    raw = (
        "1\n00:00:01,000 --> 00:00:03,000\nПервая строка\n\n"
        "2\n00:00:03,500 --> 00:00:05,000\nВторая строка\n"
    )
    segments = parse_vtt(raw)
    assert [s.text for s in segments] == ["Первая строка", "Вторая строка"]
    assert segments[1].start == 3.5


def test_vtt_strips_inline_tags():
    raw = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
        '<c.colorE5E5E5>привет</c> <00:00:02.000><c> мир</c>\n'
    )
    segments = parse_vtt(raw)
    assert segments[0].text == "привет мир"


def test_dedupe_rolling_autocaptions():
    """Автосубтитры повторяют предыдущую строку, дописывая хвост."""
    raw = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.000\nмы поменяли чанкинг\n\n"
        "00:00:03.000 --> 00:00:05.000\nмы поменяли чанкинг и recall вырос\n\n"
        "00:00:05.000 --> 00:00:07.000\nна тридцать процентов\n"
    )
    segments = parse_vtt(raw)
    assert len(segments) == 2
    assert segments[0].text == "мы поменяли чанкинг и recall вырос"
    assert segments[0].start == 1.0  # время сохраняется от первого вхождения


def test_parse_json3():
    raw = json.dumps(
        {
            "events": [
                {"tStartMs": 0, "segs": [{"utf8": "первый"}, {"utf8": " кусок"}]},
                {"tStartMs": 1500, "segs": [{"utf8": "\n"}]},
                {"tStartMs": 2000, "segs": [{"utf8": "второй"}]},
                {"tStartMs": 3000},
            ]
        }
    )
    segments = parse_json3(raw)
    assert [s.text for s in segments] == ["первый кусок", "второй"]
    assert segments[1].start == 2.0


def test_parse_subtitles_dispatch():
    assert parse_subtitles("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nтекст\n", "vtt")
    assert parse_subtitles('{"events":[{"tStartMs":0,"segs":[{"utf8":"a"}]}]}', "json3")


def test_to_prompt_text_stamps_periodically():
    segments = [Segment(start=float(i * 10), text=f"строка {i}") for i in range(10)]
    text = to_prompt_text(segments, stamp_every=30.0)
    assert text.count("[") == 4  # 0, 30, 60, 90 секунд
    assert text.startswith("[00:00] строка 0")


def test_chunk_segments_overlaps_and_covers():
    segments = [Segment(start=float(i), text="слово " * 20) for i in range(200)]
    chunks = chunk_segments(segments, chars_per_chunk=4000, overlap_chars=400)
    assert len(chunks) > 1
    # Каждый сегмент попал хотя бы в одно окно.
    covered = {seg.start for chunk in chunks for seg in chunk}
    assert covered == {seg.start for seg in segments}
    # Соседние окна перекрываются.
    assert chunks[1][0].start < chunks[0][-1].start


def test_chunk_segments_no_overlap_only_tail():
    """Хвост из одного перехлёста не должен становиться отдельным окном."""
    segments = [Segment(start=float(i), text="x" * 100) for i in range(40)]
    chunks = chunk_segments(segments, chars_per_chunk=1000, overlap_chars=300)
    assert all(len(chunk) > 3 for chunk in chunks)


def test_chunk_segments_empty():
    assert chunk_segments([], chars_per_chunk=100) == []
