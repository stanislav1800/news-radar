"""Очистка текста постов (шаг 3): HTML → чистый текст, ссылки, признаки мусора."""

import re
import warnings
from dataclasses import dataclass

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from sito.utils.urls import normalize_url

# BeautifulSoup предупреждает, когда на вход приходит обычный текст без тегов. Для нас это норма.
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

# Символы нулевой ширины и мягкий перенос: str.split() не считает их пробелами
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\xad"), None)

# После этих тегов начинается новая строка. Остальные теги (a, b, span) — внутри строки.
_BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
               "blockquote", "pre", "table", "tr", "figure", "figcaption", "section", "article"]

# Строки-хвосты, которые выбрасываются целиком: «Читать далее», «Подписывайтесь на канал»
_JUNK_LINE = re.compile(
    r"^(читать далее|читать дальше|читать полностью|подробнее|далее"
    r"|подписывайтесь\b.*|подпишитесь\b.*|подпишись\b.*|читайте (также|нас)\b.*)"
    r"[\s.…!:→»>]*$",
    re.IGNORECASE,
)

# Маркировка рекламы по закону о рекламе: токен erid
_AD_MARK = re.compile(r"\berid\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CleanText:
    text: str          # чистый текст, абзацы через \n; пустая строка, если текста нет
    links: list[str]   # нормализованные ссылки из текста, без ссылки на саму статью
    is_ad: bool        # есть маркировка рекламы


def clean_line(raw: str | None) -> str:
    """Для заголовка: одна строка без тегов, особых пробелов и невидимых символов."""
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ")
    return " ".join(text.translate(_INVISIBLE).split())


def clean_html(raw: str | None, own_url: str | None = None) -> CleanText:
    """Для текста поста. own_url — нормализованный адрес самой статьи, его нет в links."""
    if not raw:
        return CleanText("", [], is_ad=False)

    soup = BeautifulSoup(raw, "html.parser")
    links = _extract_links(soup, own_url)  # до очистки: потом тегов уже не будет

    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_after("\n")

    lines = (" ".join(line.split()) for line in soup.get_text().translate(_INVISIBLE).splitlines())
    text = "\n".join(line for line in lines if line and not _JUNK_LINE.match(line))
    return CleanText(text, links, is_ad=bool(_AD_MARK.search(text)))


def _extract_links(soup: BeautifulSoup, own_url: str | None) -> list[str]:
    links: dict[str, None] = {}  # dict вместо set: убирает повторы и сохраняет порядок
    for a in soup.find_all("a", href=True):
        try:
            url = normalize_url(str(a["href"]))
        except ValueError:
            continue  # относительные ссылки, mailto:, javascript:
        if url.startswith("https://") and url != own_url:
            links.setdefault(url, None)
    return list(links)


TITLE_MAX_CHARS = 140
_SENTENCE_END = re.compile(r"[.!?…](?=\s)")


def title_from_text(text: str, max_chars: int = TITLE_MAX_CHARS) -> str:
    """Заголовок для поста без заголовка (Telegram): первая строка, а в ней — первая фраза.

    Длинная фраза обрезается по слову с «…». Пустой текст — пустой заголовок.
    """
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if (match := _SENTENCE_END.search(line)) and match.end() <= max_chars:
        line = line[: match.end()]
    if len(line) <= max_chars:
        return line.rstrip(".")  # точка в конце заголовка не нужна, а «!», «?» и «…» — смысл
    cut = line.rfind(" ", 0, max_chars - 1)
    return line[: cut if cut > 0 else max_chars - 1].rstrip(" ,;:—-") + "…"
