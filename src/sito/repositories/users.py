from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Source, User, UserHiddenSource


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        return await self.session.scalar(select(User).where(User.username == username))

    async def list_all(self) -> list[User]:
        return list(await self.session.scalars(select(User).order_by(User.username)))

    async def count(self) -> int:
        return await self.session.scalar(select(func.count()).select_from(User)) or 0

    async def add(self, username: str, password_hash: str, is_admin: bool) -> User:
        user = User(username=username, password_hash=password_hash, is_admin=is_admin)
        self.session.add(user)
        await self.session.flush()
        return user

    # --- Скрытые источники ---------------------------------------------------------------------------

    async def hidden_source_ids(self, user_id: int) -> list[int]:
        return list(await self.session.scalars(
            select(UserHiddenSource.source_id).where(UserHiddenSource.user_id == user_id)
        ))

    async def hidden_sources(self, user_id: int) -> list[Source]:
        return list(await self.session.scalars(
            select(Source).join(UserHiddenSource, UserHiddenSource.source_id == Source.id)
            .where(UserHiddenSource.user_id == user_id).order_by(func.lower(Source.name))
        ))

    async def hide_source(self, user_id: int, source_id: int) -> None:
        await self.session.execute(
            insert(UserHiddenSource).values(user_id=user_id, source_id=source_id).on_conflict_do_nothing()
        )

    async def unhide_source(self, user_id: int, source_id: int) -> None:
        await self.session.execute(delete(UserHiddenSource).where(
            UserHiddenSource.user_id == user_id, UserHiddenSource.source_id == source_id,
        ))
