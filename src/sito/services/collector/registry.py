"""Типы источников: что нужно подключению и источнику. По этим описаниям строятся формы в вебе.

Новый тип (Telegram, VK) — это коллектор и описание здесь; страницы администратора не меняются.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from sito.models import SourceType


class RssSourceForm(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        title="Название", description="Как источник подписан в ленте, например «ТАСС»", min_length=1, max_length=200
    )
    url: HttpUrl = Field(title="Адрес ленты", description="Ссылка на RSS или Atom")
    poll_interval: int = Field(
        300, title="Как часто опрашивать, секунд", description="Не чаще раза в минуту", ge=60, le=86_400
    )


@dataclass(frozen=True, slots=True)
class CollectorSpec:
    source_type: SourceType
    title: str
    description: str
    builtin: bool                    # встроенное подключение: есть всегда, без учётных данных
    source_form: type[BaseModel]     # что нужно, чтобы добавить источник
    trial_fetch: bool                # умеет пробный сбор перед сохранением


COLLECTORS: dict[SourceType, CollectorSpec] = {
    SourceType.RSS: CollectorSpec(
        source_type=SourceType.RSS,
        title="RSS",
        description="Ленты RSS и Atom. Встроенное подключение, учётные данные не нужны.",
        builtin=True,
        source_form=RssSourceForm,
        trial_fetch=True,
    ),
}
