from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sito.log import LogFormat, LogLevel


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    database_url: str

    # Логи
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "text"  # json — для Docker и сборщиков логов
    log_sql: bool = False           # печатать каждый SQL-запрос

    # Веб
    # Подписывает cookie сессий (позже и шифрует учётные данные подключений). Без него веб не стартует.
    # Сгенерировать: python -c "import secrets; print(secrets.token_urlsafe(32))"
    secret_key: SecretStr | None = Field(default=None, validation_alias="SITO_SECRET_KEY")
    session_max_age_days: int = 30
    session_cookie_secure: bool = False  # включить, если приложение открыто через HTTPS-прокси
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    # Каким прокси доверять заголовки X-Forwarded-For и X-Forwarded-Proto (адреса через запятую или *)
    forwarded_allow_ips: str = "127.0.0.1"
    login_max_failures: int = 5       # неудачных попыток входа подряд...
    login_lockout_minutes: int = 5    # ...после которых пауза
    feed_page_size: int = 30
    top_min_sources: int = 5  # вкладка «Главное»: сюжеты, которые подхватили столько изданий
    # «Похожие» на странице сюжета: близкие по смыслу сюжеты, которые не склеились с этим
    related_min_similarity: float = 0.86
    related_window_hours: float = 48
    related_limit: int = 6

    @field_validator("secret_key", mode="before")
    @classmethod
    def _secret_key(cls, value: object) -> object:
        # Пустая строка (SITO_SECRET_KEY= в .env) — то же, что ключ не задан
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str) and len(value) < 16:
            raise ValueError("SITO_SECRET_KEY слишком короткий: нужно не меньше 16 символов")
        return value

    # Worker: опрос источников
    worker_tick_seconds: float = 30   # как часто проверять, кому пора на опрос
    worker_concurrency: int = 5       # сколько источников опрашивать одновременно
    fetch_timeout: float = 60         # на весь сбор одного источника
    http_timeout: float = 15          # на одну операцию HTTP (подключение, чтение)
    http_user_agent: str = "Sito/0.1 (news radar)"
    # После неудач подряд пауза перед опросом источника удваивается, но не дольше этого
    source_max_backoff_hours: float = 6
    # Как часто сверять подключения в базе с работающими (Telegram): новые, выключенные, изменённые
    connector_check_seconds: float = 15

    # Worker: очистка текста (шаг 3)
    clean_batch_size: int = 100       # постов за одну транзакцию
    clean_tick_seconds: float = 5     # пауза, когда новых постов нет

    # Worker: эмбеддинги (шаг 4)
    embedding_model: str = "intfloat/multilingual-e5-base"
    embedding_prefix: str = "query: "  # e5 требует префикс; для сравнения пост-с-постом — query
    embedding_batch_size: int = 32
    embedding_max_chars: int = 1000    # сколько текста после заголовка подавать в модель
    embedding_tick_seconds: float = 5

    # Worker: склейка в сюжеты (шаг 5). Пороги подобраны по разметке в eval/
    cluster_cross_source_min_similarity: float = 0.90
    cluster_same_source_min_similarity: float = 0.94
    cluster_max_gap_hours: float = 6
    cluster_min_story_similarity: float = 0.92  # и с центром сюжета: не даёт крупным сюжетам расползаться
    # Пост, который ссылается на статью другого издания (или на который она ссылается), идёт в её сюжет,
    # если между ними не больше стольких часов. Ссылки на своё же издание — «читайте также», не склеиваем
    cluster_link_max_gap_hours: float = 24
    cluster_batch_size: int = 100
    cluster_tick_seconds: float = 5

    # Темы (шаг 7). Сходство сюжета с 👍-примером, с которого сюжет попадает в тему.
    # Замер на живых данных: выше 0.88 соседи примера обычно по его теме, ниже начинается шум
    # (у «добычи угля» на 0.87 уже экспорт нефти и сбор зерна)
    topic_example_min_similarity: float = 0.88
    # 👎 отсекает шире, чем 👍 добавляет: сюжеты, похожие на 👎-пример хотя бы на столько (и не ближе
    # к какому-нибудь 👍), из темы убираются. Замер: после 👎 на гаджет с «углом обзора» в теме «уголь»
    # другие гаджеты похожи на него на 0.83–0.875, а сюжеты про уголь — не больше 0.82
    topic_dislike_min_similarity: float = 0.85
    # Порог похожести каждой темы подстраивается под отметки пользователя, но в этих пределах:
    # ниже 0.83 соседи уже чаще не по теме, выше 0.95 тема почти ничего не находит по похожести
    topic_similarity_floor: float = 0.83
    topic_similarity_ceiling: float = 0.95
    topic_backfill_days: int = 30          # за сколько дней пересопоставить сюжеты после изменения темы
    topic_match_tick_seconds: float = 10   # как часто worker сопоставляет новые сюжеты с темами
    topic_suggestions_limit: int = 10      # «Ещё может подойти» в конце ленты темы
    # Тема шире этого (сюжетов в день за topic_backfill_days) — при создании предлагается уточнить запрос
    topic_wide_per_day: float = 3
    topic_ask_inline: int = 2              # сколько спорных сюжетов спросить «Подходит?» прямо в ленте темы

    # Хранение: тексты и векторы постов старше этого удаляются (заголовок, ссылка и сюжеты остаются)
    post_retention_days: int = 90

    # Поиск
    search_limit: int = 20                 # результатов в каждом разделе: «со словами» и «по смыслу»
    # Модель для поиска по смыслу загружается и в процесс веба (около 1 ГБ памяти).
    # false — веб не грузит модель, поиск только по словам
    search_by_meaning: bool = True


settings = Settings()
