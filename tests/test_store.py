"""Тесты хранилища: круговой обход саммари и состояние чата."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vidsum import store  # noqa: E402
from vidsum.models import Chapter, Facts, Summary, Thesis  # noqa: E402
from vidsum.source import AuthorChapter, VideoMeta  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "db_path", str(tmp_path / "test.db"))
    store.init()


def _meta() -> VideoMeta:
    return VideoMeta(
        video_id="abc123",
        url="https://youtu.be/abc123",
        title="Векторный поиск",
        channel="Инженерная кухня",
        duration=184,
        upload_date="2026-03-01",
        extractor="youtube",
        chapters=[AuthorChapter(start=0, title="Интро")],
    )


def _summary() -> Summary:
    return Summary(
        video_type="interview",
        verdict="Вердикт",
        watch_advice="Смотреть с 00:20.",
        theses=[Thesis(text="Тезис", basis=None, start=31, speaker="Марина")],
        chapters=[Chapter(start=0, title="Интро", skippable=True)],
        facts=Facts(numbers=["+30%"], names=[], tools=[], links=[], terms=[]),
        practical=[],
        caveats=["Бенчмарк внутренний."],
        disagreements=[],
        sponsored=[],
        unclear=[],
    )


def test_save_and_get_roundtrip():
    video_id = store.save(_meta(), _summary(), "[00:00] текст", "субтитры автора (ru)")
    stored = store.get(video_id)

    assert stored is not None
    assert stored.meta.title == "Векторный поиск"
    assert stored.meta.chapters[0].title == "Интро"  # вложенные главы переживают JSON
    assert stored.summary.theses[0].speaker == "Марина"
    assert stored.summary.caveats == ["Бенчмарк внутренний."]
    assert stored.transcript == "[00:00] текст"
    assert stored.meta.timecode_url(31) == "https://youtu.be/abc123?t=31"


def test_get_missing_returns_none():
    assert store.get("нет-такого") is None


def test_find_by_url_uses_cache_window():
    store.save(_meta(), _summary(), "текст", "субтитры")
    assert store.find_by_url("https://youtu.be/abc123") is not None
    assert store.find_by_url("https://youtu.be/abc123", max_age_days=0) is None
    assert store.find_by_url("https://youtu.be/other") is None


def test_chat_state_transitions():
    video_id = store.save(_meta(), _summary(), "текст", "субтитры")

    assert store.get_state(42) == (None, False)

    store.set_current(42, video_id)
    assert store.get_state(42) == (video_id, False)

    store.set_awaiting_qa(42, video_id, True)
    assert store.get_state(42) == (video_id, True)

    # Новое видео сбрасывает режим вопросов.
    other = store.save(_meta(), _summary(), "текст", "субтитры")
    store.set_current(42, other)
    assert store.get_state(42) == (other, False)
