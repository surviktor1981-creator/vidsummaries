"""Тесты вызова модели без похода в сеть.

Проверяют форму запроса (схема, effort, промпты) и разбор ответа —
то, что ломается при обновлении SDK и молча портит саммари.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anthropic
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vidsum import summarize  # noqa: E402
from vidsum.models import Summary  # noqa: E402
from vidsum.source import AuthorChapter, FetchedVideo, VideoMeta  # noqa: E402
from vidsum.transcript import Segment  # noqa: E402

VALID_SUMMARY = {
    "video_type": "interview",
    "verdict": "Разбор перевода поиска на векторную базу.",
    "watch_advice": "Смотреть с 00:20, до этого представления.",
    "theses": [
        {
            "text": "Чанкинг важнее выбора модели эмбеддингов: +30% recall.",
            "basis": "Внутренний бенчмарк на 30 тыс. документов.",
            "start": 31,
            "speaker": "Марина",
        }
    ],
    "chapters": [{"start": 0, "title": "Интро", "skippable": True}],
    "facts": {
        "numbers": ["+30% recall", "200-400 токенов", "$0.13 за 1000 запросов"],
        "names": ["Марина Дьяченко (Retexo)"],
        "tools": ["Cohere Rerank v3", "pgvector", "Qdrant"],
        "links": [],
        "terms": ["чанкинг — нарезка документов перед индексацией"],
    },
    "practical": ["Сначала собрать бенчмарк, потом менять поиск."],
    "caveats": ["Бенчмарк внутренний, публично не выкладывался."],
    "disagreements": ["Антон считает, что модель важнее нарезки."],
    "sponsored": ["01:58 — интеграция Логтрейл"],
    "unclear": [],
}


def _video() -> FetchedVideo:
    meta = VideoMeta(
        video_id="abc123",
        url="https://youtu.be/abc123",
        title="Векторный поиск",
        channel="Инженерная кухня",
        duration=184,
        upload_date="2026-03-01",
        extractor="youtube",
        chapters=[AuthorChapter(start=0, title="Интро")],
    )
    segments = [
        Segment(start=0.0, text="Привет, это подкаст."),
        Segment(start=31.0, text="Чанкинг оказался важнее модели."),
    ]
    return FetchedVideo(meta, segments, "субтитры автора (ru)")


def _mock_client(captured: list[httpx.Request], payload: dict) -> anthropic.Anthropic:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 500_000})
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    return anthropic.Anthropic(
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_single_pass_request_shape(monkeypatch):
    captured: list[httpx.Request] = []
    monkeypatch.setattr(summarize, "_client", lambda: _mock_client(captured, VALID_SUMMARY))

    result = summarize.summarize(_video())

    assert isinstance(result, Summary)
    assert result.facts.numbers[0] == "+30% recall"
    assert result.theses[0].speaker == "Марина"

    assert len(captured) == 1
    body = json.loads(captured[0].content)
    assert body["model"] == "claude-opus-5"
    assert body["output_config"]["effort"]
    # Схема формата должна доехать до API — иначе секции начнут теряться.
    assert body["output_config"]["format"]["type"] == "json_schema"
    properties = body["output_config"]["format"]["schema"]["properties"]
    for section in ("verdict", "theses", "chapters", "facts", "caveats", "sponsored"):
        assert section in properties, f"секция {section} потерялась из схемы"

    prompt = body["messages"][0]["content"]
    assert "Векторный поиск" in prompt
    assert "[00:00]" in prompt  # таймкоды доехали до модели
    assert "author_chapters" in prompt  # главы автора переданы как опора


def test_long_video_goes_through_map_reduce(monkeypatch):
    captured: list[httpx.Request] = []
    client = _mock_client(captured, VALID_SUMMARY)
    monkeypatch.setattr(summarize, "_client", lambda: client)
    monkeypatch.setattr(summarize.config, "max_single_pass_tokens", 200)
    monkeypatch.setattr(summarize.config, "chunk_tokens", 100)

    video = _video()
    video.segments = [Segment(start=float(i * 5), text="слово " * 30) for i in range(200)]

    result = summarize.summarize(video)

    assert isinstance(result, Summary)
    assert len(captured) > 2, "длинное видео должно резаться на окна"
    # Последний запрос — сборка: со схемой; предыдущие — выжимки: без схемы.
    last = json.loads(captured[-1].content)
    first = json.loads(captured[0].content)
    assert "format" in last["output_config"]
    assert "format" not in first.get("output_config", {})
    assert "Фрагмент 1/" in last["messages"][0]["content"]


def test_refusal_raises_readable_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [],
                "stop_reason": "refusal",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        )

    client = anthropic.Anthropic(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(summarize, "_client", lambda: client)

    with pytest.raises(RuntimeError, match="отказалась"):
        summarize.summarize(_video())


def test_qa_caches_transcript(monkeypatch):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "На 01:47 — Cohere Rerank v3."}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    client = anthropic.Anthropic(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(summarize, "_client", lambda: client)

    answer = summarize.ask("Заголовок", "Канал", 184, "[00:00] текст", "какой реранкер?")

    assert "Cohere" in answer
    body = json.loads(captured[0].content)
    # Транскрипт кэшируется — иначе каждый вопрос перечитывает его за полную цену.
    assert body["system"][0]["cache_control"]["type"] == "ephemeral"
