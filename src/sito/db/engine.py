from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sito.config import settings

# Ни запрос, ни открытая без дела транзакция не висят вечно: зависание превращается в ошибку,
# которую видно в логе и которую переживает цикл worker'а, а соединение возвращается в пул
engine = create_async_engine(
    settings.database_url, pool_pre_ping=True,
    connect_args={"server_settings": {
        "statement_timeout": str(settings.db_statement_timeout_seconds * 1000),
        "idle_in_transaction_session_timeout": str(settings.db_idle_transaction_timeout_seconds * 1000),
    }},
)
async_session = async_sessionmaker(engine, expire_on_commit=False)
