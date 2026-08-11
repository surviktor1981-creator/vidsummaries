#!/usr/bin/env python3
"""Telegram-бот: ссылка на видео → текстовое саммари."""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from vidsum import render, store, summarize
from vidsum.config import config
from vidsum.source import FetchError, fetch, find_url

log = logging.getLogger("vidsum.bot")
dp = Dispatcher()

_TAG_RE = re.compile(r"<[^>]+>")

# Разбор видео упирается в сеть и модель; больше двух параллельно — лишний риск
# упереться в лимиты, а бот всё равно личный.
_workers = asyncio.Semaphore(2)

HELP = (
    "Пришлите ссылку на видео — верну текстовое саммари.\n\n"
    "В саммари: вердикт, ключевые тезисы, карта по таймкодам, фактура "
    "(цифры, имена, инструменты), практические шаги и оговорки.\n\n"
    "Кнопки под ответом:\n"
    "• <b>Подробно</b> — полная версия файлом\n"
    "• <b>Тезисы</b> — только выжимка\n"
    "• <b>Спросить</b> — вопрос по содержанию видео\n\n"
    "Команды: /help — эта справка."
)


def _allowed(user_id: int | None) -> bool:
    return not config.allowed_user_ids or user_id in config.allowed_user_ids


async def _send_html(message: Message, text: str, **kwargs) -> None:
    """Отправляет с разметкой; если Telegram её не принял — отправляет как есть.

    Подрезка под лимит теоретически может оборвать тег, и тогда Telegram
    отвечает ошибкой. Потерять саммари из-за одного тега было бы обидно.
    """
    try:
        await message.answer(text, **kwargs)
    except TelegramBadRequest:
        log.warning("Telegram не принял разметку, отправляю текстом", exc_info=True)
        await message.answer(_TAG_RE.sub("", text), parse_mode=None, **kwargs)


def _keyboard(video_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📄 Подробно", callback_data=f"full:{video_id}"),
                InlineKeyboardButton(text="⚡ Тезисы", callback_data=f"short:{video_id}"),
            ],
            [InlineKeyboardButton(text="❓ Спросить по видео", callback_data=f"ask:{video_id}")],
        ]
    )


@dp.message(CommandStart())
@dp.message(Command("help"))
async def on_start(message: Message) -> None:
    if not _allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer(HELP)


@dp.message(F.text)
async def on_text(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not _allowed(user_id):
        await message.answer("Этот бот приватный.")
        return

    video_id, awaiting = store.get_state(message.chat.id)
    url = find_url(message.text or "")

    if awaiting and not url:
        await _answer_question(message, video_id, message.text or "")
        return
    if not url:
        await message.answer("Нужна ссылка на видео. /help — как это работает.")
        return

    await _process_video(message, url)


async def _process_video(message: Message, url: str) -> None:
    cached = await asyncio.to_thread(store.find_by_url, url)
    if cached:
        store.set_current(message.chat.id, cached.id)
        await _send_html(
            message,
            render.short_message(cached.summary, cached.meta),
            reply_markup=_keyboard(cached.id),
        )
        return

    status = await message.answer("⏳ Читаю видео…")
    try:
        async with _workers:
            await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            try:
                video = await asyncio.to_thread(fetch, url)
            except FetchError as exc:
                await status.edit_text(f"⚠️ {exc}")
                return

            await status.edit_text(
                f"⏳ <b>{escape(video.meta.title, quote=False)}</b>\n"
                f"<i>{video.transcript_source} · {len(video.segments)} реплик · "
                f"разбираю…</i>"
            )
            summary = await asyncio.to_thread(summarize.summarize, video)
    except Exception:  # noqa: BLE001 — пользователю нужен ответ, а не тишина
        log.exception("Не удалось разобрать %s", url)
        await status.edit_text("⚠️ Что-то пошло не так при разборе. Попробуйте ещё раз.")
        return

    transcript = summarize.transcript_text(video.segments)
    video_id = await asyncio.to_thread(
        store.save, video.meta, summary, transcript, video.transcript_source
    )
    store.set_current(message.chat.id, video_id)

    await status.delete()
    await _send_html(
        message, render.short_message(summary, video.meta), reply_markup=_keyboard(video_id)
    )


async def _answer_question(message: Message, video_id: str | None, question: str) -> None:
    stored = await asyncio.to_thread(store.get, video_id) if video_id else None
    if not stored:
        store.set_awaiting_qa(message.chat.id, video_id or "", False)
        await message.answer("Видео потерялось. Пришлите ссылку заново.")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        answer = await asyncio.to_thread(
            summarize.ask,
            stored.meta.title,
            stored.meta.channel,
            stored.meta.duration,
            stored.transcript,
            question,
        )
    except Exception:  # noqa: BLE001
        log.exception("Вопрос по видео %s не отработал", video_id)
        await message.answer("⚠️ Не получилось ответить. Попробуйте переспросить.")
        return

    for part in render.split_for_telegram(answer):
        await message.answer(part, parse_mode=None)
    await message.answer(
        "Можно спросить ещё — просто напишите следующий вопрос.",
        reply_markup=_keyboard(stored.id),
    )


@dp.callback_query(F.data.startswith("full:"))
async def on_full(callback: CallbackQuery) -> None:
    stored = await _stored_for(callback)
    if not stored:
        return
    await callback.answer()
    document = render.full_markdown(stored.summary, stored.meta).encode("utf-8")
    name = f"{stored.meta.video_id or 'summary'}.md"
    await callback.message.answer_document(
        BufferedInputFile(document, filename=name),
        caption=f"Полная версия: {escape(stored.meta.title, quote=False)}"[:1024],
    )


@dp.callback_query(F.data.startswith("short:"))
async def on_theses(callback: CallbackQuery) -> None:
    stored = await _stored_for(callback)
    if not stored:
        return
    await callback.answer()
    await _send_html(callback.message, render.theses_only(stored.summary))


@dp.callback_query(F.data.startswith("ask:"))
async def on_ask(callback: CallbackQuery) -> None:
    stored = await _stored_for(callback)
    if not stored:
        return
    store.set_awaiting_qa(callback.message.chat.id, stored.id, True)
    await callback.answer()
    await callback.message.answer(
        f"Спрашивайте по видео «{escape(stored.meta.title, quote=False)}» — "
        "следующим сообщением."
    )


async def _stored_for(callback: CallbackQuery):
    if not _allowed(callback.from_user.id if callback.from_user else None):
        await callback.answer("Этот бот приватный.", show_alert=True)
        return None
    video_id = (callback.data or "").split(":", 1)[1]
    stored = await asyncio.to_thread(store.get, video_id)
    if not stored:
        await callback.answer("Это видео уже не в базе — пришлите ссылку заново.", show_alert=True)
        return None
    return stored


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.check(need_telegram=True)
    store.init()
    bot = Bot(
        token=config.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    me = await bot.get_me()
    log.info("Бот запущен: @%s, модель %s", me.username, config.model)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
