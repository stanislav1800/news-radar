from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sito.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)
