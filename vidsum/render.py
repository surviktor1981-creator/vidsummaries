"""Отрисовка саммари: короткое сообщение для Telegram и полная версия файлом."""

from __future__ import annotations

import re
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


def _bullets(items: list[str], limit: int | None = None) -> list[str]:
    shown = items[:limit] if limit else items
    return [f"• {_e(item)}" for item in shown]


# --- Короткое сообщение ----------------------------------------------------


def short_message(summary: Summary, meta: VideoMeta) -> str:
    """Основное сообщение. Собирается по приоритету и ужимается под лимит."""
    header = (
        f"🎬 <b>{_e(meta.title)}</b>\n"
        f"<i>{_e(meta.channel)} · {fmt_ts(meta.duration)} · "
        f"{TYPE_LABEL.get(summary.video_type, 'видео')}</i>"
    )

    # (блок, можно ли ужимать) — блоки собираются сверху вниз, пока есть место.
    blocks: list[tuple[str, bool]] = [(header, False)]

    blocks.append((f"📌 <b>ВЕРДИКТ</b>\n{_e(summary.verdict)}\n\n▸ {_e(summary.watch_advice)}", False))

    if summary.theses:
        lines = []
        previous_speaker: str | None = None
        for i, thesis in enumerate(summary.theses, start=1):
            # Имя показываем только когда голос сменился: в интервью с одним
            # основным спикером подпись у каждого пункта — шум.
            if thesis.speaker and thesis.speaker != previous_speaker:
                line = f"{i}. <b>{_e(thesis.speaker)}:</b> {_e(thesis.text)}"
            else:
                line = f"{i}. {_e(thesis.text)}"
            previous_speaker = thesis.speaker or previous_speaker
            if thesis.basis:
                line += f"\n    <i>— {_e(thesis.basis)}</i>"
            lines.append(line)
        blocks.append(("🔑 <b>ТЕЗИСЫ</b>\n" + "\n".join(lines), True))

    if summary.chapters:
        lines = []
        for chapter in summary.chapters:
            url = meta.timecode_url(chapter.start)
            mark = " <i>(можно пропустить)</i>" if chapter.skippable else ""
            lines.append(f'<a href="{url}">{fmt_ts(chapter.start)}</a> {_e(chapter.title)}{mark}')
        blocks.append(("🕐 <b>ПО ТАЙМКОДАМ</b>\n" + "\n".join(lines), True))

    facts = summary.facts
    fact_lines = []
    for label, values in (
        ("Цифры", facts.numbers),
        ("Имена", facts.names),
        ("Инструменты", facts.tools),
        ("Термины", facts.terms),
        ("Ссылки", facts.links),
    ):
        if values:
            fact_lines.append(f"<b>{label}:</b> {_e('; '.join(values))}")
    if fact_lines:
        blocks.append(("🔢 <b>ФАКТУРА</b>\n" + "\n".join(fact_lines), True))

    if summary.practical:
        blocks.append(("🛠 <b>ПРАКТИКА</b>\n" + "\n".join(_bullets(summary.practical)), True))

    if summary.disagreements:
        blocks.append(
            ("⚖️ <b>РАЗНОГЛАСИЯ</b>\n" + "\n".join(_bullets(summary.disagreements)), True)
        )

    warn = list(summary.caveats) + [f"реклама: {s}" for s in summary.sponsored]
    if summary.unclear:
        warn.append(f"неразборчиво: {'; '.join(summary.unclear)}")
    if warn:
        blocks.append(("⚠️ <b>ОГОВОРКИ</b>\n" + "\n".join(_bullets(warn)), True))

    return _fit(blocks)


def _fit(blocks: list[tuple[str, bool]]) -> str:
    """Собирает блоки под лимит: сжимаемые обрезаются по строкам, затем выкидываются."""
    text = "\n\n".join(block for block, _ in blocks)
    if len(text) <= SHORT_TARGET:
        return text

    # Сначала подрезаем самые длинные сжимаемые блоки по строкам.
    working = list(blocks)
    for _ in range(200):
        text = "\n\n".join(block for block, _ in working)
        if len(text) <= SHORT_TARGET:
            return text
        longest = max(
            (i for i, (_, squeezable) in enumerate(working) if squeezable),
            key=lambda i: len(working[i][0]),
            default=None,
        )
        if longest is None:
            break
        body, squeezable = working[longest]
        lines = body.split("\n")
        if len(lines) <= 2:
            working.pop(longest)
            continue
        working[longest] = ("\n".join(lines[:-1]), squeezable)

    text = "\n\n".join(block for block, _ in working)
    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 40].rsplit("\n", 1)[0] + "\n…"
    return text + "\n\n<i>Подрезано под лимит Telegram — полная версия в файле.</i>"


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
