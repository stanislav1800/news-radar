import pytest

from sito.utils.text import clean_html, clean_line, title_from_text

HABR = (
    '<img src="https://habrastorage.org/x.jpg" />'
    '<p>Японский фотограф <a href="https://x.com/photo/status/1?utm_source=habr">получил</a>'
    " упоминание в&nbsp;фотоконкурсе.</p>"
    "<p>Второй абзац.<br>С переносом.</p> "
    '<a href="https://habr.com/ru/articles/1100270/#habracut">Читать далее</a>'
)
HABR_URL = "https://habr.com/ru/articles/1100270"


def test_html_to_paragraphs() -> None:
    result = clean_html(HABR, own_url=HABR_URL)

    # Ссылка внутри абзаца не разрывает предложение; <p> и <br> дают новые строки
    assert result.text == "Японский фотограф получил упоминание в фотоконкурсе.\nВторой абзац.\nС переносом."


def test_links_exclude_own_article() -> None:
    result = clean_html(HABR, own_url=HABR_URL)

    # #habracut ведёт на саму статью и отброшен; внешняя ссылка нормализована
    assert result.links == ["https://x.com/photo/status/1"]


def test_links_skip_non_http_and_repeats() -> None:
    html = (
        '<a href="/relative">a</a><a href="mailto:x@y.ru">b</a><a href="javascript:void(0)">c</a>'
        '<a href="https://news.example/1">d</a><a href="http://www.news.example/1/">e</a>'
    )

    assert clean_html(html).links == ["https://news.example/1"]


def test_plain_text_passes_through() -> None:
    assert clean_html("Здание повреждено по всей площади").text == "Здание повреждено по всей площади"


def test_special_spaces_and_invisible_chars() -> None:
    raw = "<p>Уголь\xa0в\u2009Кузбассе\u200b  вырос\xad</p>"

    assert clean_html(raw).text == "Уголь в Кузбассе вырос"


def test_junk_lines_removed() -> None:
    raw = (
        "<p>Новость дня.</p><p>Подписывайтесь на наш канал!</p>"
        "<p>Читайте также: другая новость</p><p>Подробнее →</p>"
    )

    assert clean_html(raw).text == "Новость дня."


def test_junk_words_inside_sentence_are_kept() -> None:
    # Удаляются только строки-хвосты целиком, а не слова в тексте
    raw = "<p>Подробнее о решении рассказал министр.</p>"

    assert clean_html(raw).text == "Подробнее о решении рассказал министр."


def test_ad_detected_by_erid() -> None:
    assert clean_html("<p>Скидки!</p><p>Реклама. ООО «Ромашка». erid: 2VtzqxA</p>").is_ad
    assert not clean_html("<p>Обычная новость про Эридан</p>").is_ad


def test_empty_input() -> None:
    result = clean_html(None)

    assert (result.text, result.links, result.is_ad) == ("", [], False)


def test_clean_line() -> None:
    assert clean_line("«Акрон»\xa0— «Динамо»:\u200b счёт &amp; ход") == "«Акрон» — «Динамо»: счёт & ход"
    assert clean_line(None) == ""


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("Шахта «Распадская» возобновила добычу. Подробности позже.", "Шахта «Распадская» возобновила добычу"),
        ("Срочно!\nВторая строка", "Срочно!"),
        ("Что будет с углём? Разбираемся.", "Что будет с углём?"),
        ("\n\n  Первая непустая строка без точки\nещё", "Первая непустая строка без точки"),
        ("", ""),
    ],
)
def test_title_from_text(text: str, title: str) -> None:
    assert title_from_text(text) == title


def test_long_title_is_cut_by_word() -> None:
    text = "Очень " * 40 + "длинная первая фраза без точек"

    title = title_from_text(text, max_chars=50)

    assert len(title) <= 50 and title.endswith("…") and not title.endswith(" …")
    assert title.removesuffix("…").split()[-1] == "Очень"
