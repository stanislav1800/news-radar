import pytest

from sito.utils.urls import content_hash, normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Одна статья в разных видах → один адрес
        ("https://tass.ru/politika/123", "https://tass.ru/politika/123"),
        ("http://tass.ru/politika/123", "https://tass.ru/politika/123"),
        ("https://www.TASS.ru/politika/123/", "https://tass.ru/politika/123"),
        ("https://tass.ru/politika/123#comments", "https://tass.ru/politika/123"),
        ("https://tass.ru/politika/123?utm_source=rss&utm_medium=feed", "https://tass.ru/politika/123"),
        ("https://habr.com/ru/news/1/?from=telegram", "https://habr.com/ru/news/1"),
        ("https://ria.ru/a?ysclid=abc&yclid=1&fbclid=2", "https://ria.ru/a"),
        ("  https://lenta.ru/news/1  ", "https://lenta.ru/news/1"),
        # Параметры по делу остаются, порядок не важен
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        # Порт по умолчанию убирается, нестандартный остаётся
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com:8080/a", "https://example.com:8080/a"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_keeps_path_case() -> None:
    # /News и /news на многих сайтах — разные страницы
    assert normalize_url("https://example.com/News/Item") == "https://example.com/News/Item"


def test_normalize_url_strips_www_only_from_host() -> None:
    assert normalize_url("https://ngs42.ru/text/www.test/") == "https://ngs42.ru/text/www.test"


@pytest.mark.parametrize("raw", ["", "/relative/path", "tass.ru/news"])
def test_normalize_url_rejects_relative(raw: str) -> None:
    with pytest.raises(ValueError, match="Не абсолютный URL"):
        normalize_url(raw)


def test_content_hash_is_stable() -> None:
    assert content_hash("Заголовок", "Текст") == content_hash("Заголовок", "Текст")
    assert len(content_hash("Заголовок", "Текст")) == 64


def test_content_hash_ignores_whitespace() -> None:
    assert content_hash("Уголь  в\nКузбассе", " Текст\xa0новости ") == content_hash("Уголь в Кузбассе", "Текст новости")


def test_content_hash_detects_changes() -> None:
    assert content_hash("Заголовок", "Текст") != content_hash("Заголовок", "Текст исправлен")
    assert content_hash("Заголовок", "Текст") != content_hash("Заголовок исправлен", "Текст")


def test_content_hash_accepts_missing_text() -> None:
    assert content_hash("Заголовок", None) == content_hash("Заголовок", "")


def test_content_hash_separator_is_unambiguous() -> None:
    # Граница между заголовком и текстом не должна «переезжать»
    assert content_hash("А Б", "В") != content_hash("А", "Б В")
