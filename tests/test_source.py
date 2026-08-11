"""Тесты выбора дорожки субтитров, разбора ошибок и настроек доступа."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vidsum import source  # noqa: E402


def _tracks(**langs: list[dict]) -> dict[str, list[dict]]:
    return dict(langs)


def test_pick_track_prefers_preferred_language():
    tracks = _tracks(
        en=[{"ext": "vtt", "url": "u-en"}],
        ru=[{"ext": "vtt", "url": "u-ru"}],
    )
    lang, fmt = source._pick_track(tracks, ("ru", "en"))
    assert lang == "ru"
    assert fmt["url"] == "u-ru"


def test_pick_track_prefers_json3_over_vtt():
    tracks = _tracks(
        ru=[{"ext": "vtt", "url": "u-vtt"}, {"ext": "json3", "url": "u-json3"}]
    )
    _, fmt = source._pick_track(tracks, ("ru",))
    assert fmt["ext"] == "json3"


def test_pick_track_matches_language_variants():
    """ru-RU и a.en — это те же ru и en."""
    assert source._pick_track(_tracks(**{"ru-RU": [{"ext": "vtt", "url": "u"}]}), ("ru",))[0] == "ru-RU"
    assert source._pick_track(_tracks(**{"a.en": [{"ext": "vtt", "url": "u"}]}), ("en",))[0] == "a.en"


def test_pick_track_falls_back_to_any_language():
    tracks = _tracks(de=[{"ext": "vtt", "url": "u-de"}])
    lang, _ = source._pick_track(tracks, ("ru", "en"))
    assert lang == "de"


def test_pick_track_skips_formats_without_url():
    tracks = _tracks(ru=[{"ext": "json3"}, {"ext": "vtt", "url": "u-vtt"}])
    _, fmt = source._pick_track(tracks, ("ru",))
    assert fmt["url"] == "u-vtt"


def test_pick_track_empty():
    assert source._pick_track({}, ("ru",)) is None
    assert source._pick_track(_tracks(ru=[]), ("ru",)) is None


def test_find_url():
    assert source.find_url("посмотри https://youtu.be/abc вот") == "https://youtu.be/abc"
    assert source.find_url("https://youtu.be/abc.") == "https://youtu.be/abc"
    assert source.find_url("без ссылки") is None
    assert source.find_url("") is None


def test_explain_download_error_is_human_readable():
    cases = {
        "ERROR: Private video. Sign in": "приватное",
        "This video is unavailable": "недоступно",
        "Unsupported URL: https://example.com": "читать этот сайт",
        "Sign in to confirm you're not a bot": "COOKIES_FILE",
    }
    for raw, expected in cases.items():
        assert expected in source._explain_download_error(raw)


def test_explain_download_error_keeps_unknown_message_short():
    message = source._explain_download_error("ERROR: " + "x" * 1000)
    assert len(message) < 400


def test_access_opts_off_by_default(monkeypatch):
    monkeypatch.setattr(source.config, "cookies_file", "")
    monkeypatch.setattr(source.config, "ytdlp_proxy", "")
    assert source._access_opts() == {}


def test_access_opts_reach_ydl(monkeypatch, tmp_path):
    """Cookies и прокси должны доезжать до yt-dlp — иначе на сервере всё встанет."""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(source.config, "cookies_file", str(cookies))
    monkeypatch.setattr(source.config, "ytdlp_proxy", "socks5://127.0.0.1:1080")

    with source._ydl() as ydl:
        assert ydl.params["cookiefile"] == str(cookies)
        assert ydl.params["proxy"] == "socks5://127.0.0.1:1080"

    # yt-dlp переписывает cookies при закрытии — файл обязан быть записываемым.
    assert cookies.exists()
