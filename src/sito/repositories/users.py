from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import User


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
