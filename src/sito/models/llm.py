from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin


class StorySummary(TimestampMixin, Base):
    """Пересказ сюжета от LLM: делается по кнопке и хранится, пока сюжет не пополнится."""

    __tablename__ = "story_summary"

    story_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("story.id", ondelete="CASCADE"), primary_key=True)
    title: Mapped[str]
    summary: Mapped[str]
    model: Mapped[str]
    posts_count: Mapped[int]    # по скольким постам сделан: если постов стало больше — пересказ устарел
    sources_count: Mapped[int]


class LlmUsage(IdMixin, Base):
    """Каждый запрос к LLM: для дневного лимита и счётчика расходов в админке."""

    __tablename__ = "llm_usage"

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    feature: Mapped[str]        # story_summary, …
    model: Mapped[str]
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost: Mapped[float | None]  # если сервис сообщает цену запроса
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user_account.id", ondelete="SET NULL"))


class TopicReport(IdMixin, Base):
    """Сводка по теме за период или ответ на вопрос по теме: история темы."""

    __tablename__ = "topic_report"

    topic_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("topic.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str]               # summary — сводка, question — вопрос, digest — дайджест по расписанию
    days: Mapped[int]               # за сколько дней
    question: Mapped[str | None]
    text: Mapped[str]               # markdown: ## заголовки, списки, ссылки [n] на сюжеты
    story_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))  # [n] в тексте — story_ids[n - 1]
    model: Mapped[str]
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    read_at: Mapped[datetime | None]  # дайджест открыли: для значка «Дайджесты» в шапке


class StoryMergeCheck(Base):
    """Проверка нового сюжета: не то же ли событие, что у очень похожего сюжета рядом (LLM).

    Одна строка на новый сюжет: проверен — больше не проверяется. Если LLM ответила «одно событие»,
    сюжет вливается в other_id и удаляется (вместе с этой строкой)."""

    __tablename__ = "story_merge_check"

    story_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("story.id", ondelete="CASCADE"), primary_key=True)
    other_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("story.id", ondelete="SET NULL"))
    similarity: Mapped[float | None]
    same: Mapped[bool | None]       # None — похожего рядом нет, модель не спрашивали
    reason: Mapped[str | None]
    checked_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StoryMergeLog(IdMixin, Base):
    """Журнал объединений: что во что влито. Без внешних ключей — сюжет-источник уже удалён,
    а запись нужна, чтобы найти и оценить ошибочные объединения."""

    __tablename__ = "story_merge_log"

    source_id: Mapped[int] = mapped_column(BigInteger)   # влитый (удалённый) сюжет
    target_id: Mapped[int] = mapped_column(BigInteger, index=True)
    similarity: Mapped[float]
    source_title: Mapped[str]
    target_title: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
