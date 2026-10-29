"""Прочитанное и «новое с прошлого визита» для пользователя."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import User
from sito.repositories.stories import StoryRepository

# Заходы без перерыва дольше этого — один визит. Иначе разделитель «Вы были здесь»
# уезжал бы при каждом обновлении страницы.
VISIT_GAP = timedelta(minutes=30)


class ReadingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def register_visit(user: User, now: datetime | None = None) -> datetime | None:
        """Отметить заход в ленту. Возвращает границу «нового»: когда пользователь смотрел ленту в прошлый визит."""
        now = now or datetime.now(UTC)
        if user.last_seen_at is None or now - user.last_seen_at > VISIT_GAP:
            user.previous_visit_at = user.last_seen_at
        user.last_seen_at = now
        return user.previous_visit_at

    async def mark_read(self, user: User, story_id: int) -> None:
        await StoryRepository(self.session).mark_read(user.id, story_id)

    @staticmethod
    def mark_all_read(user: User, now: datetime | None = None) -> None:
        user.read_all_before = now or datetime.now(UTC)
