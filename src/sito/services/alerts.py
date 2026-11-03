"""Что сломалось: для красной точки у администратора и уведомлений в «Избранное» служебного Telegram.

Каждая проблема — со своим ключом: уведомление приходит, когда проблема появилась и когда починилась,
а не каждые пять минут, пока она есть.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import ConnectorStatus, SourceType
from sito.repositories.connectors import ConnectorRepository
from sito.repositories.pipeline import PipelineRepository
from sito.repositories.sources import SourceRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Problem:
    key: str
    text: str


async def find_problems(session: AsyncSession, now: datetime) -> list[Problem]:
    problems: list[Problem] = []
    for c in await ConnectorRepository(session).list_all():
        if c.enabled and not c.builtin and c.status != ConnectorStatus.ACTIVE:
            what = "нужен вход" if c.status == ConnectorStatus.NEEDS_LOGIN else f"ошибка: {(c.last_error or '')[:150]}"
            problems.append(Problem(f"connector:{c.id}", f"Подключение «{c.name}» — {what}"))
    for s in await SourceRepository(session).list_with_stats():
        if s.failing:
            problems.append(Problem(f"failing:{s.id}", f"«{s.name}» не собирается: {(s.last_error or '')[:150]}"))
        elif s.type == SourceType.RSS and (since := s.silent_since(now)):
            # Telegram-каналы не в счёт: тихих каналов много, это не поломка
            problems.append(Problem(f"silent:{s.id}", f"RSS «{s.name}» молчит с {since:%d.%m %H:%M} UTC"))
    if failed := (await PipelineRepository(session).summary()).by_status.get("failed", 0):
        problems.append(Problem("failed-posts", f"Постов с ошибкой обработки: {failed}"))
    return problems


def alert_text(appeared: list[Problem], fixed: list[Problem]) -> str:
    lines = []
    if appeared:
        lines += ["⚠️ Сито: что-то сломалось", *(f"• {p.text}" for p in appeared)]
    if fixed:
        lines += ["✅ Починилось:", *(f"• {p.text}" for p in fixed)]
    lines.append("Подробности — в Сито: Администрирование → Состояние.")
    return "\n".join(lines)
