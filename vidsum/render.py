"""Отрисовка саммари: короткое сообщение для Telegram и полная версия файлом."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape

from .models import Summary
from .source import VideoMeta
from .transcript import fmt_ts

TG_LIMIT = 4096
SHORT_TARGET = 3300  # запас до лимита Telegram

TYPE_LABEL = {
    "lecture": "лекция",
    "interview": "интервью",
    "tutorial": "туториал",
    "news": "новости",
    "review": "обзор",
    "stream": "стрим",
    "other": "видео",
}


def _e(text: str) -> str:
    return escape(str(text), quote=False)


def _bullets(items: list[str]) -> list[str]:
    return [f"• {_e(item)}" for item in items]


@dataclass
class _Block:
    """Секция сообщения. order — очередь на подрезку, 0 = не резать никогда."""

    header: str
    items: list[str] = field(default_factory=list)
    order: int = 0


def _fit(blocks: list[_Block]) -> str:
    """Ужимает сообщение под лимит Telegram, начиная с наименее важных секций.

    Резать молча нельзя: если карта глав обрывается на середине видео,
    читатель решит, что там оно и заканчивается. Поэтому у подрезанной секции
    остаётся счётчик остатка со ссылкой на полную версию.
    """
    kept = [len(block.items) for block in blocks]

    def assemble() -> str:
        parts: list[str] = []
        for block, shown in zip(blocks, kept):
            if not block.items:
                parts.append(block.header)
                continue
            body = block.header + "\n" + "\n".join(block.items[:shown])
            lost = len(block.items) - shown
            if lost:
                body += f"\n<i>… ещё {lost} — в полной версии</i>"
            parts.append(body)
        return "\n\n".join(parts)

    text = assemble()
    for _ in range(500):
        if len(text) <= SHORT_TARGET:
            return text
        # Хотя бы один пункт в секции оставляем: иначе исчезнет и счётчик остатка.
        candidates = [i for i, block in enumerate(blocks) if block.order > 0 and kept[i] > 1]
        if not candidates:
            break
        last_in_queue = max(blocks[i].order for i in candidates)
        victim = max(
            (i for i in candidates if blocks[i].order == last_in_queue), key=lambda i: kept[i]
        )
        kept[victim] -= 1
        text = assemble()

    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 60].rsplit("\n", 1)[0]
        text += "\n\n<i>Подрезано под лимит Telegram — полная версия в файле.</i>"
    return text


# --- Короткое сообщение ----------------------------------------------------


def short_message(summary: Summary, meta: VideoMeta) -> str:
    """Основное сообщение: блоки собираются по приоритету и ужимаются под лимит."""
    header = (
        f"🎬 <b>{_e(meta.title)}</b>\n"
        f"<i>{_e(meta.channel)} · {fmt_ts(meta.duration)} · "
        f"{TYPE_LABEL.get(summary.video_type, 'видео')}</i>"
    )

    # (заголовок, пункты, очередь на подрезку). Очередь: 0 — не резать никогда,
    # дальше чем больше число, тем раньше блок начинают резать. Первой уходит
    # карта глав — она целиком есть в файле; тезисы режутся последними.
    blocks: list[_Block] = [
        _Block(header, [], 0),
        _Block(
            "📌 <b>ВЕРДИКТ</b>",
            [_e(summary.verdict), "", f"▸ {_e(summary.watch_advice)}"],
            0,
        ),
    ]

    if summary.theses:
        items = []
        previous_speaker: str | None = None
        for i, thesis in enumerate(summary.theses, start=1):
            # Имя показываем только когда голос сменился: в интервью с одним
            # основным спикером подпись у каждого пункта — шум.
            if thesis.speaker and thesis.speaker != previous_speaker:
                item = f"{i}. <b>{_e(thesis.speaker)}:</b> {_e(thesis.text)}"
            else:
                item = f"{i}. {_e(thesis.text)}"
            previous_speaker = thesis.speaker or previous_speaker
            if thesis.basis:
                item += f"\n    <i>— {_e(thesis.basis)}</i>"
            items.append(item)
        blocks.append(_Block("🔑 <b>ТЕЗИСЫ</b>", items, 1))

    if summary.chapters:
        items = []
        for chapter in summary.chapters:
            url = meta.timecode_url(chapter.start)
            mark = " <i>(можно пропустить)</i>" if chapter.skippable else ""
            items.append(f'<a href="{url}">{fmt_ts(chapter.start)}</a> {_e(chapter.title)}{mark}')
        blocks.append(_Block("🕐 <b>ПО ТАЙМКОДАМ</b>", items, 5))

    facts = summary.facts
    fact_items = [
        f"<b>{label}:</b> {_e('; '.join(values))}"
        for label, values in (
            ("Цифры", facts.numbers),
            ("Имена", facts.names),
            ("Инструменты", facts.tools),
            ("Термины", facts.terms),
            ("Ссылки", facts.links),
        )
        if values
    ]
    if fact_items:
        blocks.append(_Block("🔢 <b>ФАКТУРА</b>", fact_items, 3))

    if summary.practical:
        blocks.append(_Block("🛠 <b>ПРАКТИКА</b>", _bullets(summary.practical), 4))

    if summary.disagreements:
        blocks.append(_Block("⚖️ <b>РАЗНОГЛАСИЯ</b>", _bullets(summary.disagreements), 2))

    warn = list(summary.caveats) + [f"реклама: {s}" for s in summary.sponsored]
    if summary.unclear:
        warn.append(f"неразборчиво: {'; '.join(summary.unclear)}")
    if warn:
        blocks.append(_Block("⚠️ <b>ОГОВОРКИ</b>", _bullets(warn), 2))

    return _fit(blocks)


# --- Полная версия (Markdown-файл) -----------------------------------------


def full_markdown(summary: Summary, meta: VideoMeta) -> str:
    out: list[str] = [
        f"# {meta.title}",
        "",
        f"**Канал:** {meta.channel}  ",
        f"**Длительность:** {fmt_ts(meta.duration)}  ",
        f"**Опубликовано:** {meta.upload_date or 'неизвестно'}  ",
        f"**Тип:** {TYPE_LABEL.get(summary.video_type, 'видео')}  ",
        f"**Ссылка:** {meta.url}",
        "",
        "## Вердикт",
        "",
        summary.verdict,
        "",
        f"**Смотреть?** {summary.watch_advice}",
        "",
    ]

    if summary.theses:
        out += ["## Тезисы", ""]
        for i, thesis in enumerate(summary.theses, start=1):
            head = f"{i}. "
            if thesis.speaker:
                head += f"**{thesis.speaker}:** "
            head += thesis.text
            if thesis.start is not None:
                head += f" ([{fmt_ts(thesis.start)}]({meta.timecode_url(thesis.start)}))"
            out.append(head)
            if thesis.basis:
                out.append(f"   - _Основание:_ {thesis.basis}")
        out.append("")

    if summary.chapters:
        out += ["## Карта видео", ""]
        for chapter in summary.chapters:
            mark = " _(можно пропустить)_" if chapter.skippable else ""
            out.append(
                f"- [{fmt_ts(chapter.start)}]({meta.timecode_url(chapter.start)}) "
                f"{chapter.title}{mark}"
            )
        out.append("")

    facts = summary.facts
    sections = [
        ("Цифры", facts.numbers),
        ("Люди и компании", facts.names),
        ("Инструменты и материалы", facts.tools),
        ("Термины", facts.terms),
        ("Ссылки", facts.links),
    ]
    if any(values for _, values in sections):
        out += ["## Фактура", ""]
        for label, values in sections:
            if values:
                out += [f"**{label}**", ""]
                out += [f"- {value}" for value in values]
                out.append("")

    for title, items in (
        ("Практика", summary.practical),
        ("Разногласия", summary.disagreements),
        ("Оговорки и условия", summary.caveats),
        ("Реклама и интеграции", summary.sponsored),
        ("Неразборчивые места", summary.unclear),
    ):
        if items:
            out += [f"## {title}", ""] + [f"- {item}" for item in items] + [""]

    return "\n".join(out)


def full_markdown_bytes(summary: Summary, meta: VideoMeta) -> bytes:
    """Готовит файл к отправке в Telegram.

    BOM обязателен: без него Telegram и Windows читают кириллицу в .md как
    cp1251 и показывают кракозябры вместо текста.
    """
    return b"\xef\xbb\xbf" + full_markdown(summary, meta).encode("utf-8")


_UNSAFE_IN_NAME = re.compile(r"[^\w\s-]", re.UNICODE)


def document_name(meta: VideoMeta) -> str:
    """Имя файла из названия видео — чтобы сохранённые саммари различались."""
    base = _UNSAFE_IN_NAME.sub("", meta.title).strip()
    base = re.sub(r"\s+", "-", base)[:60].strip("-")
    return f"{base or meta.video_id or 'summary'}.md"


def split_for_telegram(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет длинный текст по абзацам/строкам, не ломая разметку посреди тега."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return parts
