"""Типы источников: что нужно подключению и источнику. По этим описаниям строятся формы в вебе.

Новый тип (Telegram, VK) — это сборщик и описание здесь; страницы администратора не меняются.
Поля SecretStr в форме подключения — секреты: хранятся зашифрованными и в вебе не показываются.
У формы подключения обязательно поле name — название подключения.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from sito.models import SourceType
from sito.services.collector.telegram import TelegramConnectorForm, TelegramSourceForm


class RssSourceForm(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        title="Название", description="Как источник подписан в ленте, например «ТАСС»", min_length=1, max_length=200
    )
    url: HttpUrl = Field(title="Адрес ленты", description="Ссылка на RSS или Atom")
    poll_interval: int = Field(
        300, title="Опрашивать не реже, секунд",
        description="Активные ленты опрашиваются чаще, по частоте их новостей, но не чаще раза в минуту",
        ge=60, le=86_400
    )


@dataclass(frozen=True, slots=True)
class CollectorSpec:
    source_type: SourceType
    title: str
    description: str
    builtin: bool                    # встроенное подключение: есть всегда, без учётных данных
    # Как приходят посты: polled — worker опрашивает источники по расписанию (RSS);
    # иначе подключение слушает само и заводит источники с первым постом (Telegram)
    polled: bool
    source_form: type[BaseModel] | None = None     # источник добавляет администратор (RSS)
    connector_form: type[BaseModel] | None = None  # подключение добавляет администратор (Telegram)
    trial_fetch: bool = False        # умеет пробный сбор перед сохранением
    login: bool = False              # после сохранения подключения нужен вход (мастер: код, пароль)


COLLECTORS: dict[SourceType, CollectorSpec] = {
    SourceType.RSS: CollectorSpec(
        source_type=SourceType.RSS,
        title="RSS",
        description="Ленты RSS и Atom. Встроенное подключение, учётные данные не нужны.",
        builtin=True,
        polled=True,
        source_form=RssSourceForm,
        trial_fetch=True,
    ),
    SourceType.TELEGRAM: CollectorSpec(
        source_type=SourceType.TELEGRAM,
        title="Telegram",
        description="Служебный аккаунт Telegram: посты всех каналов, на которые он подписан, приходят сразу. "
                    "Канал можно добавить ссылкой — аккаунт подпишется сам. Нужен отдельный аккаунт, не личный.",
        builtin=False,
        polled=False,
        source_form=TelegramSourceForm,
        connector_form=TelegramConnectorForm,
        login=True,
    ),
}
