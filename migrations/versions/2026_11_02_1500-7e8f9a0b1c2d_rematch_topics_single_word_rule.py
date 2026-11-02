"""rematch topics for the single word rule

Revision ID: 7e8f9a0b1c2d
Revises: 5c1d2e3f4a5b
Create Date: 2026-11-02 15:00:00+00:00

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7e8f9a0b1c2d'
down_revision: str | Sequence[str] | None = '5c1d2e3f4a5b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Тема из одного слова теперь строже (слово в заголовке или дважды в тексте). Сброс отметки —
    # worker пересчитает совпадения всех тем по новому правилу; примеры и порог тем не трогаются
    op.execute("UPDATE topic SET matched_until = NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE topic SET matched_until = NULL")
