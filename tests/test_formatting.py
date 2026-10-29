from datetime import UTC, datetime, timedelta

import pytest

from sito.web.formatting import ago, local_time, plural, safe_url

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)  # 15:00 по Москве


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=20), "только что"),
        (timedelta(minutes=5), "5 мин назад"),
        (timedelta(hours=3, minutes=40), "3 ч назад"),
        (timedelta(hours=16), "вчера, 23:00"),
        (timedelta(days=3), "26 окт, 15:00"),
        (timedelta(days=400), "24 сен 2025, 15:00"),
    ],
)
def test_ago(delta: timedelta, expected: str) -> None:
    assert ago(NOW - delta, "Europe/Moscow", NOW) == expected


def test_local_time_uses_user_timezone() -> None:
    assert local_time(NOW, "Europe/Moscow", NOW) == "15:00"
    assert local_time(NOW, "Asia/Novosibirsk", NOW) == "19:00"
    assert local_time(NOW, "Not/AZone", NOW) == "12:00"  # неизвестный пояс — UTC


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1 издание"), (2, "2 издания"), (4, "4 издания"), (5, "5 изданий"),
     (11, "11 изданий"), (14, "14 изданий"), (21, "21 издание"), (22, "22 издания"), (111, "111 изданий")],
)
def test_plural(count: int, expected: str) -> None:
    assert plural(count, "издание", "издания", "изданий") == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://tass.ru/1", "https://tass.ru/1"),
        ("HTTP://example.com", "HTTP://example.com"),
        ("javascript:alert(1)", "#"),
        ("JavaScript://evil.example/%0aalert(1)", "#"),
        ("data:text/html,<script>", "#"),
        ("/relative", "#"),
    ],
)
def test_safe_url(url: str, expected: str) -> None:
    assert safe_url(url) == expected
