"""Тесты отрисовки: лимит Telegram, экранирование, полнота полной версии."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vidsum import render  # noqa: E402
from vidsum.models import Chapter, Facts, Summary, Thesis  # noqa: E402
from vidsum.source import VideoMeta  # noqa: E402


def _meta(**overrides) -> VideoMeta:
    base = dict(
        video_id="abc123",
        url="https://youtu.be/abc123",
        title="Векторный поиск",
        channel="Инженерная кухня",
        duration=184,
        upload_date="2026-03-01",
        extractor="youtube",
    )
    base.update(overrides)
    return VideoMeta(**base)


def _summary(**overrides) -> Summary:
    base = dict(
        video_type="interview",
        verdict="Разбор перевода поиска на векторную базу.",
        watch_advice="Смотреть с 00:20.",
        theses=[Thesis(text="Чанкинг важнее модели.", basis="Внутренний бенчмарк", start=31)],
        chapters=[
            Chapter(start=0, title="Интро", skippable=True),
            Chapter(start=31, title="Чанкинг", skippable=False),
        ],
        facts=Facts(
            numbers=["+30% recall"],
            names=["Марина Дьяченко"],
            tools=["Qdrant"],
            links=[],
            terms=[],
        ),
        practical=["Сначала бенчмарк."],
        caveats=["Бенчмарк внутренний."],
        disagreements=[],
        sponsored=["01:58 — интеграция"],
        unclear=[],
    )
    base.update(overrides)
    return Summary(**base)


def test_short_message_has_every_section():
    text = render.short_message(_summary(), _meta())
    for marker in ("ВЕРДИКТ", "ТЕЗИСЫ", "ПО ТАЙМКОДАМ", "ФАКТУРА", "ПРАКТИКА", "ОГОВОРКИ"):
        assert marker in text
    assert "youtu.be/abc123?t=31" in text  # таймкод кликабельный
    assert "реклама: 01:58" in text  # интеграции не растворяются


def test_short_message_escapes_html():
    summary = _summary(verdict='Автор про <script> и "кавычки" & амперсанд')
    text = render.short_message(summary, _meta(title="A <b>bold</b> title"))
    assert "&lt;script&gt;" in text
    assert "<b>bold</b>" not in text  # заголовок экранирован, а не выполнен как разметка
    assert "&amp;" in text


def test_short_message_fits_telegram_limit():
    """Разговорчивое видео не должно ронять отправку превышением лимита."""
    summary = _summary(
        verdict="Очень длинный вердикт. " * 30,
        theses=[
            Thesis(text=f"Тезис номер {i}: " + "содержательный текст " * 12, basis="обоснование " * 8)
            for i in range(20)
        ],
        chapters=[Chapter(start=i * 60, title=f"Глава {i} " * 6, skippable=False) for i in range(40)],
        practical=[f"Шаг {i} " * 20 for i in range(15)],
        caveats=[f"Оговорка {i} " * 20 for i in range(15)],
    )
    text = render.short_message(summary, _meta())
    assert len(text) <= render.TG_LIMIT
    assert "ВЕРДИКТ" in text  # верхние блоки переживают подрезку


def test_timecode_url_for_non_youtube():
    meta = _meta(extractor="rutube", url="https://rutube.ru/video/xyz/")
    assert meta.timecode_url(90) == "https://rutube.ru/video/xyz/?t=90"
    meta_q = _meta(extractor="vk", url="https://vk.com/video?z=1")
    assert meta_q.timecode_url(90) == "https://vk.com/video?z=1&t=90"


def test_full_markdown_keeps_everything():
    summary = _summary(
        disagreements=["Антон не согласен с приоритетом нарезки."],
        unclear=["02:15 — не разобрано название библиотеки"],
    )
    text = render.full_markdown(summary, _meta())
    for marker in (
        "# Векторный поиск",
        "## Вердикт",
        "## Тезисы",
        "## Карта видео",
        "## Фактура",
        "## Практика",
        "## Разногласия",
        "## Оговорки и условия",
        "## Реклама и интеграции",
        "## Неразборчивые места",
    ):
        assert marker in text
    assert "(https://youtu.be/abc123?t=31)" in text
    assert "_(можно пропустить)_" in text


def test_theses_only_is_compact():
    text = render.theses_only(_summary())
    assert "Чанкинг важнее модели." in text
    assert "ФАКТУРА" not in text
    assert len(text) <= render.TG_LIMIT


def test_split_for_telegram():
    text = "\n\n".join(f"Абзац {i} " * 50 for i in range(40))
    parts = render.split_for_telegram(text)
    assert len(parts) > 1
    assert all(len(p) <= render.TG_LIMIT for p in parts)
    # Ничего не потеряли по дороге.
    assert "".join("".join(part.split()) for part in parts) == "".join(text.split())


def test_split_short_text_is_single_part():
    assert render.split_for_telegram("коротко") == ["коротко"]
