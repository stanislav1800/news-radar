from pydantic_settings import BaseSettings, SettingsConfigDict

from sito.log import LogFormat, LogLevel


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    database_url: str

    # Логи
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "text"  # json — для Docker и сборщиков логов
    log_sql: bool = False           # печатать каждый SQL-запрос

    # Worker: опрос источников
    worker_tick_seconds: float = 30   # как часто проверять, кому пора на опрос
    worker_concurrency: int = 5       # сколько источников опрашивать одновременно
    fetch_timeout: float = 60         # на весь сбор одного источника
    http_timeout: float = 15          # на одну операцию HTTP (подключение, чтение)
    http_user_agent: str = "Sito/0.1 (news radar)"

    # Worker: очистка текста (шаг 3)
    clean_batch_size: int = 100       # постов за одну транзакцию
    clean_tick_seconds: float = 5     # пауза, когда новых постов нет

    # Worker: эмбеддинги (шаг 4)
    embedding_model: str = "intfloat/multilingual-e5-base"
    embedding_prefix: str = "query: "  # e5 требует префикс; для сравнения пост-с-постом — query
    embedding_batch_size: int = 32
    embedding_max_chars: int = 1000    # сколько текста после заголовка подавать в модель
    embedding_tick_seconds: float = 5


settings = Settings()
