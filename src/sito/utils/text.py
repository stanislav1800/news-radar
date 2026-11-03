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

# Подпись в конце постов Telegram-каналов: «Илья Середюк в МАКС. Подписаться», «👉 Подписаться | Прислать новость».
# Одинаковая у всех постов канала, склейку она бы только путала. Короткая строка со словом «подписаться»
_SIGNATURE_LINE = re.compile(r"^.{0,80}\bподписаться\b.{0,60}$", re.IGNORECASE)

# Маркировка рекламы по закону о рекламе: токен erid; в Telegram ещё и хэштег #реклама
_AD_MARK = re.compile(r"\berid\b|#реклама\b", re.IGNORECASE)


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
    text = "\n".join(
        line for line in lines
        if line and not _JUNK_LINE.match(line) and not _SIGNATURE_LINE.match(line) and not _URL.fullmatch(line)
    )
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


# --- Посты без заголовка (Telegram) -----------------------------------------------------------------

# Эмодзи и значки: в заголовке и в тексте для смыслового отпечатка они только шум
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u200D\u20E3\u2190-\u21FF]+"
)
_HASHTAG = re.compile(r"(?<!\w)#\w+")
_URL = re.compile(r"https?://\S+")
_BOLD = re.compile(r"</?(?:b|strong)>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
HEADLINE_MAX_CHARS = 200


def split_headline(raw: str) -> tuple[str, str] | None:
    """Заголовок, если он у поста явно есть: первая строка жирным или отделённая пустой строкой.

    Возвращает (заголовок в HTML, остальной текст). None — явного заголовка нет, текст сплошной.
    """
    lines = raw.strip().split("\n")
    first = lines[0]
    plain = _TAG.sub("", first).strip()
    if not plain or len(plain) > HEADLINE_MAX_CHARS:
        return None
    letters = sum(ch.isalpha() for ch in plain)
    bold = "".join(re.findall(r"<(?:b|strong)>(.*?)</(?:b|strong)>", first, re.IGNORECASE | re.DOTALL))
    bold_letters = sum(ch.isalpha() for ch in _TAG.sub("", bold))
    is_bold = letters > 0 and bold_letters >= 0.8 * letters
    separated = len(lines) > 1 and not lines[1].strip()
    if not (is_bold or separated):
        return None
    return first, "\n".join(lines[1:])


def tidy_title(title: str) -> str:
    """Заголовок поста Telegram как у новости: без эмодзи и хэштегов, КАПС — обычными буквами.

    Аббревиатуры (ГОФ, ДНР, МЧС) остаются заглавными: обычные слова узнаёт словарь.
    """
    from sito.utils.lemmas import (
        is_common_word,  # словарь грузится при первом обращении
    )

    title = _HASHTAG.sub("", _EMOJI.sub(" ", title))
    title = " ".join(title.split()).strip(" -–—|:•·")
    letters = [ch for ch in title if ch.isalpha()]
    if len(letters) >= 10 and sum(ch.isupper() for ch in letters) >= 0.7 * len(letters):
        def fix(match: re.Match[str]) -> str:
            word = match.group(0)
            return word.lower() if word.isupper() and is_common_word(word) else word
        title = re.sub(r"\w+", fix, title)
        # С заглавной: начало, после «?!.» и после открывающей кавычки (название)
        title = re.sub(r"(^|[.!?…]\s+|«)(\w)", lambda m: m.group(1) + m.group(2).upper(), title)
    return title


def embedding_plain(text: str, max_paragraphs: int = 3) -> str:
    """Текст для смыслового отпечатка: начало новости без эмодзи, хэштегов и ссылок.

    Как у RSS — заголовок и пара абзацев: рассуждения в конце длинного поста отпечаток только размывают.
    """
    paragraphs = []
    for line in text.splitlines():
        line = " ".join(_URL.sub(" ", _HASHTAG.sub(" ", _EMOJI.sub(" ", line))).split())
        if sum(ch.isalpha() for ch in line) >= 3:
            paragraphs.append(line)
        if len(paragraphs) >= max_paragraphs:
            break
    return " ".join(paragraphs)


def is_chatter(text: str) -> bool:
    """Короткий пост без новости: «Доброе утро)», «Ке акукека мака!». Только для постов без заголовка (Telegram).

    Новость, даже короткая, почти всегда называет место, человека или организацию (слово с заглавной
    не в начале фразы) или число: «Взрывы в Белгороде», «Курс доллара — 92 рубля».
    """
    plain = _URL.sub(" ", _HASHTAG.sub(" ", _EMOJI.sub(" ", text)))
    if sum(ch.isalpha() for ch in plain) >= CHATTER_MAX_LETTERS or any(ch.isdigit() for ch in plain):
        return False
    words = re.findall(r"[^\W\d_]+", plain)
    starts = {0} | {i + 1 for i, w in enumerate(words[:-1]) if re.search(rf"{re.escape(w)}[.!?…]", plain)}
    return not any(w[0].isupper() for i, w in enumerate(words) if i not in starts)


CHATTER_MAX_LETTERS = 60

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
