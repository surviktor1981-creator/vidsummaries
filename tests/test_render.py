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


def test_speaker_shown_only_when_voice_changes():
    """В интервью с одним спикером подпись у каждого пункта — шум."""
    summary = _summary(
        theses=[
            Thesis(text="Первое утверждение.", speaker="Шульман"),
            Thesis(text="Второе утверждение.", speaker="Шульман"),
            Thesis(text="Возражение ведущего.", speaker="Ведущий Дождя"),
            Thesis(text="Ответ на возражение.", speaker="Шульман"),
        ],
    )
    text = render.short_message(summary, _meta())
    assert text.count("Шульман:") == 2  # первое вхождение и возврат после смены голоса
    assert "Ведущий Дождя:" in text
    assert "2. Второе утверждение." in text  # повтор имени не печатается


def test_speaker_absent_for_single_voice_video():
    summary = _summary(theses=[Thesis(text="Утверждение без спикера.", speaker=None)])
    text = render.short_message(summary, _meta())
    assert "1. Утверждение без спикера." in text


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


def test_trimming_is_visible_and_spares_theses():
    """Молчаливая подрезка врёт: обрыв карты глав читается как конец видео."""
    summary = _summary(
        theses=[Thesis(text=f"Тезис {i}: " + "смысл " * 15) for i in range(9)],
        chapters=[Chapter(start=i * 60, title=f"Глава {i} " * 8, skippable=False) for i in range(60)],
        facts=Facts(numbers=["+30% recall"], names=[], tools=[], links=[], terms=[]),
    )
    text = render.short_message(summary, _meta())

    assert len(text) <= render.TG_LIMIT
    assert "… ещё" in text  # остаток посчитан, а не выброшен молча
    # Тезисы важнее навигации — режется карта, а не они.
    assert text.count("Тезис ") == 9
    assert "ФАКТУРА" in text  # сводные списки переживают подрезку глав
    assert "ПО ТАЙМКОДАМ" in text  # секция не исчезает целиком


def test_no_trim_marker_when_everything_fits():
    text = render.short_message(_summary(), _meta())
    assert "… ещё" not in text


def test_section_order_is_stable():
    text = render.short_message(
        _summary(disagreements=["Ведущий не согласен."]), _meta()
    )
    positions = [
        text.index(marker)
        for marker in ("ВЕРДИКТ", "ТЕЗИСЫ", "ПО ТАЙМКОДАМ", "ФАКТУРА", "ПРАКТИКА", "РАЗНОГЛАСИЯ", "ОГОВОРКИ")
    ]
    assert positions == sorted(positions)


def test_facts_category_is_capped():
    """Одна категория — одна строка, подрезать её изнутри нельзя: нужен потолок."""
    summary = _summary(
        facts=Facts(
            numbers=[f"число {i}" for i in range(25)],
            names=["Сергей"], tools=[], links=[], terms=[],
        )
    )
    text = render.short_message(summary, _meta())
    assert text.count("число ") == render._FACTS_PER_CATEGORY
    assert f"(+{25 - render._FACTS_PER_CATEGORY} в полной версии)" in text
    assert "Имена:" in text  # соседние категории не пострадали


def test_bloated_facts_do_not_starve_other_sections():
    """Раньше раздутая фактура съедала сообщение и остальное резалось до одного пункта."""
    summary = _summary(
        theses=[Thesis(text=f"Тезис {i}: " + "смысл " * 10) for i in range(9)],
        facts=Facts(
            numbers=[f"показатель {i} с пояснением" for i in range(30)],
            names=[f"имя {i}" for i in range(30)],
            tools=[f"инструмент {i}" for i in range(30)],
            links=[], terms=[],
        ),
        practical=[f"Шаг {i}: рекомендация" for i in range(12)],
        caveats=[f"Оговорка {i}: условие" for i in range(13)],
    )
    text = render.short_message(summary, _meta())
    assert len(text) <= render.TG_LIMIT
    assert text.count("Тезис ") == 9  # тезисы целы
    assert text.count("Шаг ") >= 6  # практика не ужата до одного пункта
    assert text.count("Оговорка ") >= 6


def test_asr_source_is_marked_in_header():
    """На распознанной речи имена и термины грязнее — читатель должен знать."""
    from_speech = render.short_message(_summary(), _meta(), "распознавание речи (base)")
    from_subs = render.short_message(_summary(), _meta(), "субтитры автора (ru)")
    assert "распознано с речи" in from_speech
    assert "распознано с речи" not in from_subs
    assert "распознано с речи" not in render.short_message(_summary(), _meta())


def test_full_markdown_records_transcript_source():
    text = render.full_markdown(_summary(), _meta(), "распознавание речи (base)")
    assert "**Расшифровка:** распознавание речи (base)" in text
    assert "**Расшифровка:**" not in render.full_markdown(_summary(), _meta())


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


def test_markdown_bytes_start_with_bom():
    """Без BOM Telegram и Windows читают кириллицу как cp1251 — кракозябры."""
    data = render.full_markdown_bytes(_summary(), _meta())
    assert data.startswith(b"\xef\xbb\xbf")
    assert data[3:].decode("utf-8").startswith("# Векторный поиск")
    assert "Чанкинг важнее модели." in data.decode("utf-8-sig")


def test_document_name_from_title():
    assert render.document_name(_meta()) == "Векторный-поиск.md"
    # Символы, недопустимые в именах файлов, вычищаются.
    assert "/" not in render.document_name(_meta(title="RAG: часть 1/2 — «итоги»"))
    # Пустое после чистки название не должно давать файл без имени.
    assert render.document_name(_meta(title="!!!", video_id="abc123")) == "abc123.md"


def test_split_for_telegram():
    text = "\n\n".join(f"Абзац {i} " * 50 for i in range(40))
    parts = render.split_for_telegram(text)
    assert len(parts) > 1
    assert all(len(p) <= render.TG_LIMIT for p in parts)
    # Ничего не потеряли по дороге.
    assert "".join("".join(part.split()) for part in parts) == "".join(text.split())


def test_split_short_text_is_single_part():
    assert render.split_for_telegram("коротко") == ["коротко"]
