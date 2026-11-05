from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin
from sito.models.post import EMBEDDING_DIM


class Topic(IdMixin, TimestampMixin, Base):
    """Тема пользователя: сохранённый поиск, который учится на 👍/👎 (шаг 7 пайплайна).

    Сюжет попадает в тему, если в нём есть слова запроса или он похож на 👍-пример,
    и при этом он не ближе к 👎-примеру, чем к 👍. Правило — в TopicRepository.rematch.
    """

    __tablename__ = "topic"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user_account.id", ondelete="CASCADE"), index=True)
    name: Mapped[str]
    query: Mapped[str | None]  # слова для поиска; у темы «следить за такими» запроса нет, только примеры
    paused: Mapped[bool] = mapped_column(server_default="false")
    # Секрет в адресе RSS-ленты темы: RSS-читалка не умеет входить по паролю
    rss_token: Mapped[str] = mapped_column(unique=True)
    # До какого story.updated_at worker уже сопоставил сюжеты с темой
    matched_until: Mapped[datetime | None]
    # Свой порог похожести на 👍-примеры, подстроенный по отметкам пользователя; None — общий
    min_similarity: Mapped[float | None]
    # Искать только в этих источниках (тема «IT» — Хабр и OpenNET); пусто — во всех
    source_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), server_default="{}")
    description: Mapped[str | None]  # что входит в тему и что нет — своими словами (тема по описанию, LLM)
    llm_check: Mapped[bool] = mapped_column(server_default="false")  # LLM проверяет новые совпадения по описанию
    llm_prompt: Mapped[str | None]  # пожелания пользователя к сводкам и ответам LLM по теме
    # Дайджест по расписанию (LLM): daily, weekdays, weekly или None — выключен
    digest_schedule: Mapped[str | None]
    digest_time: Mapped[str] = mapped_column(server_default="08:00")   # ЧЧ:ММ в часовом поясе пользователя
    digest_weekday: Mapped[int] = mapped_column(server_default="0")    # для weekly: 0 — понедельник
    digest_format: Mapped[str] = mapped_column(server_default="short") # short — список событий, full — обзор
    digest_min_stories: Mapped[int] = mapped_column(server_default="3")  # меньше новых сюжетов — не делать
    digest_prompt: Mapped[str | None]                                  # пожелания именно к дайджесту
    digest_last_at: Mapped[datetime | None]  # когда последний раз проверяли расписание (сделан или пропущен)


class TopicExample(IdMixin, Base):
    """👍 или 👎 на сюжете в теме.

    Вектор и заголовок сюжета сохраняются копией: сюжеты пересобираются (sito pipeline recluster,
    удаление источника), а обучение темы терять нельзя.
    """

    __tablename__ = "topic_example"
    __table_args__ = (UniqueConstraint("topic_id", "story_id"),)

    topic_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("topic.id", ondelete="CASCADE"))
    story_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("story.id", ondelete="SET NULL"), index=True
    )
    liked: Mapped[bool]  # True — 👍, False — 👎
    title: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TopicMatch(Base):
    """Сюжет подошёл к теме, и почему: на какой 👍-пример похож или совпали слова запроса."""

    __tablename__ = "topic_match"
    __table_args__ = (Index("ix_topic_match_story_id", "story_id"),)

    topic_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("topic.id", ondelete="CASCADE"), primary_key=True)
    story_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("story.id", ondelete="CASCADE"), primary_key=True)
    by_words: Mapped[bool]
    example_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("topic_example.id", ondelete="SET NULL")
    )
    similarity: Mapped[float | None]  # с ближайшим 👍-примером
    matched_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TopicCheck(Base):
    """Решение LLM: подходит ли сюжет к теме по её описанию. Отклонённый в тему больше не попадает,
    пока пользователь не вернёт его (тогда он становится 👍-примером)."""

    __tablename__ = "topic_check"

    topic_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("topic.id", ondelete="CASCADE"), primary_key=True)
    story_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("story.id", ondelete="CASCADE"), primary_key=True)
    fits: Mapped[bool]
    reason: Mapped[str | None]
    checked_at: Mapped[datetime] = mapped_column(server_default=func.now())
