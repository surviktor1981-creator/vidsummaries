#!/usr/bin/env python3
"""Пайплайн без Telegram — для проверки и разовых прогонов.

    python cli.py https://youtu.be/xxxx
    python cli.py https://youtu.be/xxxx --dump-transcript out.txt
    python cli.py --subs local.vtt --title "Название" --channel "Канал"
    python cli.py https://youtu.be/xxxx --ask "какие цифры он называл?"
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from vidsum import render, summarize
from vidsum.config import config
from vidsum.source import FetchError, FetchedVideo, VideoMeta, fetch
from vidsum.transcript import duration_of, parse_subtitles


def _from_file(path: Path, title: str, channel: str) -> FetchedVideo:
    ext = path.suffix.lstrip(".").lower()
    segments = parse_subtitles(path.read_text(encoding="utf-8"), ext)
    if not segments:
        raise SystemExit(f"Из {path} не удалось вытащить ни одного сегмента.")
    meta = VideoMeta(
        video_id="local",
        url=f"file://{path.resolve()}",
        title=title,
        channel=channel,
        duration=int(duration_of(segments)),
        upload_date="",
        extractor="local",
    )
    return FetchedVideo(meta, segments, f"файл {path.name}")


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def main() -> int:
    parser = argparse.ArgumentParser(description="Саммари видео по ссылке")
    parser.add_argument("url", nargs="?", help="ссылка на видео")
    parser.add_argument("--subs", type=Path, help="локальный файл субтитров (.vtt/.srt/.json3)")
    parser.add_argument("--title", default="Локальное видео")
    parser.add_argument("--channel", default="—")
    parser.add_argument("--dump-transcript", type=Path, help="сохранить расшифровку и выйти")
    parser.add_argument("--out", type=Path, help="куда положить полную версию (.md)")
    parser.add_argument("--ask", help="задать вопрос по видео вместо саммари")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.url and not args.subs:
        parser.error("нужна ссылка или --subs")

    try:
        video = _from_file(args.subs, args.title, args.channel) if args.subs else fetch(args.url)
    except FetchError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    print(
        f"→ {video.meta.title} · {video.meta.channel} · "
        f"{len(video.segments)} сегментов · {video.transcript_source}",
        file=sys.stderr,
    )

    transcript = summarize.transcript_text(video.segments)

    if args.dump_transcript:
        args.dump_transcript.write_text(transcript, encoding="utf-8")
        print(f"Расшифровка сохранена: {args.dump_transcript}", file=sys.stderr)
        return 0

    if args.ask:
        print(
            summarize.ask(
                video.meta.title,
                video.meta.channel,
                video.meta.duration,
                transcript,
                args.ask,
            )
        )
        return 0

    summary = summarize.summarize(video)

    print(_strip_tags(render.short_message(summary, video.meta, video.transcript_source)))

    out = args.out or Path(f"{video.meta.video_id or 'summary'}.md")
    out.write_text(
        render.full_markdown(summary, video.meta, video.transcript_source), encoding="utf-8"
    )
    print(f"\nПолная версия: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    config.check(need_telegram=False)
    raise SystemExit(main())
