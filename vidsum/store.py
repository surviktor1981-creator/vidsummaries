"""Хранилище: разобранные видео и состояние чатов.

Транскрипт держим, чтобы отвечать на вопросы по видео и не перекачивать его
заново; готовое саммари — чтобы повторная ссылка возвращалась мгновенно.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .config import config
from .models import Summary
from .source import AuthorChapter, VideoMeta

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    meta_json    TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    transcript   TEXT NOT NULL,
    source       TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS videos_url ON videos (url);

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id     INTEGER PRIMARY KEY,
    video_id    TEXT,
    awaiting_qa INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class StoredVideo:
    id: str
    meta: VideoMeta
    summary: Summary
    transcript: str
    source: str
    created_at: float


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _meta_to_json(meta: VideoMeta) -> str:
    payload = meta.__dict__.copy()
    payload["chapters"] = [ch.__dict__ for ch in meta.chapters]
    return json.dumps(payload, ensure_ascii=False)


def _meta_from_json(raw: str) -> VideoMeta:
    payload = json.loads(raw)
    chapters = [AuthorChapter(**ch) for ch in payload.pop("chapters", [])]
    return VideoMeta(**payload, chapters=chapters)


def save(meta: VideoMeta, summary: Summary, transcript: str, source: str) -> str:
    video_id = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO videos (id, url, meta_json, summary_json, transcript, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                video_id,
                meta.url,
                _meta_to_json(meta),
                summary.model_dump_json(),
                transcript,
                source,
                time.time(),
            ),
        )
    return video_id


def _row_to_video(row: sqlite3.Row) -> StoredVideo:
    return StoredVideo(
        id=row["id"],
        meta=_meta_from_json(row["meta_json"]),
        summary=Summary.model_validate_json(row["summary_json"]),
        transcript=row["transcript"],
        source=row["source"],
        created_at=row["created_at"],
    )


def get(video_id: str) -> StoredVideo | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    return _row_to_video(row) if row else None


def find_by_url(url: str, *, max_age_days: float = 30.0) -> StoredVideo | None:
    cutoff = time.time() - max_age_days * 86400
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE url = ? AND created_at > ?"
            " ORDER BY created_at DESC LIMIT 1",
            (url, cutoff),
        ).fetchone()
    return _row_to_video(row) if row else None


# --- Состояние чата --------------------------------------------------------


def set_current(chat_id: int, video_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_state (chat_id, video_id, awaiting_qa) VALUES (?, ?, 0)"
            " ON CONFLICT(chat_id) DO UPDATE SET video_id = excluded.video_id, awaiting_qa = 0",
            (chat_id, video_id),
        )


def set_awaiting_qa(chat_id: int, video_id: str, awaiting: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_state (chat_id, video_id, awaiting_qa) VALUES (?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET video_id = excluded.video_id,"
            " awaiting_qa = excluded.awaiting_qa",
            (chat_id, video_id, int(awaiting)),
        )


def get_state(chat_id: int) -> tuple[str | None, bool]:
    """Возвращает (video_id последнего видео, ждём ли вопрос)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT video_id, awaiting_qa FROM chat_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    if not row:
        return None, False
    return row["video_id"], bool(row["awaiting_qa"])
