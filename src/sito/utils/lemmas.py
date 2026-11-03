"""Слова в начальной форме (леммы) для поиска по словам.

Стеммер Postgres для русского ошибается: «уголь» и «угля» у него разные слова, а «уголь» и «угол» — одно.
Поэтому слова приводит к начальной форме pymorphy3 (словарная морфология), а Postgres ищет уже по леммам
без своей обработки (конфигурация simple). Предлоги, союзы, частицы и местоимения отбрасываются:
«уголь в кузбассе» — это «уголь кузбасс».
"""

import re
from dataclasses import dataclass, field
from functools import cache, lru_cache

import pymorphy3

_WORD = re.compile(r"[a-zа-я0-9]+")
# Части речи, которые не несут смысла для поиска: предлоги, союзы, частицы, междометия, местоимения
_SKIP_POS = frozenset({"PREP", "CONJ", "PRCL", "INTJ", "NPRO"})


@cache
def _morph() -> pymorphy3.MorphAnalyzer:
    # Словари грузятся около секунды: один раз на процесс и только когда понадобятся
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=200_000)
def _lemma(word: str) -> str | None:
    if len(word) < 2 and not word.isdigit():
        return None
    parsed = _morph().parse(word)[0]
    if parsed.tag.POS in _SKIP_POS:
        return None
    return str(parsed.normal_form).replace("ё", "е")


@lru_cache(maxsize=50_000)
def is_common_word(word: str) -> bool:
    """Обычное слово из словаря, а не аббревиатура: «под» — да, «ГОФ», «ДНР», «МЧС» — нет."""
    lower = word.lower()
    if not _morph().word_is_known(lower):
        return False
    return "Abbr" not in _morph().parse(lower)[0].tag


def lemmatize(text: str | None) -> list[str]:
    """Значимые слова текста в начальной форме, в исходном порядке."""
    if not text:
        return []
    words = _WORD.findall(text.lower().replace("ё", "е"))
    return [lemma for word in words if (lemma := _lemma(word))]


def lemma_text(text: str | None) -> str:
    """Леммы через пробел: так их хранит пост, Postgres строит из них поисковый вектор."""
    return " ".join(lemmatize(text))


# --- Запрос -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Term:
    lemmas: tuple[str, ...]  # одно слово или фраза из нескольких
    negated: bool = False

    def tsquery(self) -> str:
        body = self.lemmas[0] if len(self.lemmas) == 1 else "(" + " <-> ".join(self.lemmas) + ")"
        return f"!{body}" if self.negated else body


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Разобранный запрос. Обычный текст — просто слова; для знающих работают -слово, or и "фраза"."""

    groups: list[list[Term]] = field(default_factory=list)  # группы через «и», внутри группы — «или»

    @property
    def words(self) -> list[str]:
        """Искомые леммы без повторов: по ним ранжирует поиск."""
        seen: dict[str, None] = {}
        for group in self.groups:
            for term in group:
                if not term.negated:
                    seen.update(dict.fromkeys(term.lemmas))
        return list(seen)

    @property
    def excluded(self) -> list[str]:
        return [lemma for g in self.groups for t in g if t.negated for lemma in t.lemmas]

    @property
    def single_word(self) -> list[str] | None:
        """Запрос — одно слово (или его варианты через or): леммы этих вариантов, иначе None.

        Такую тему проверяют строже: одно слово часто просто мелькает в тексте или значит другое.
        """
        required = [[t for t in g if not t.negated] for g in self.groups]
        required = [g for g in required if g]
        if len(required) != 1 or any(len(t.lemmas) > 1 for t in required[0]):
            return None
        return [t.lemmas[0] for t in required[0]]

    def tsquery_title(self) -> str | None:
        """Запрос «слово в заголовке» для темы из одного слова: уголь:A | угли:A."""
        words = self.single_word
        return " | ".join(f"{w}:A" for w in words) if words else None

    def tsquery_all(self) -> str | None:
        """Запрос Postgres «есть все слова» (для темы): уголь & кузбасс. None — искать нечего."""
        if not self.words:
            return None
        parts = [g[0].tsquery() if len(g) == 1 else "(" + " | ".join(t.tsquery() for t in g) + ")" for g in self.groups]
        return " & ".join(parts)


_TOKEN = re.compile(r'(-?)"([^"]*)"|(\S+)')


def parse_query(query: str) -> SearchQuery:
    terms: list[Term | str] = []  # «or» остаётся строкой-связкой
    for match in _TOKEN.finditer(query.lower()):
        negated_phrase, phrase, word = match.groups()
        if word is not None and word == "or":
            terms.append("or")
            continue
        negated = bool(negated_phrase) if phrase is not None else word.startswith("-") and len(word) > 1
        raw = phrase if phrase is not None else word.lstrip("-") if negated else word
        lemmas = tuple(lemmatize(raw))
        if lemmas:
            # Слово через дефис или несколько слов в кавычках — фраза: слова подряд
            terms.append(Term(lemmas, negated))

    groups: list[list[Term]] = []
    join_next = False
    for item in terms:
        if item == "or":
            join_next = bool(groups)
            continue
        assert isinstance(item, Term)
        if join_next:
            groups[-1].append(item)
        else:
            groups.append([item])
        join_next = False
    return SearchQuery(groups)
