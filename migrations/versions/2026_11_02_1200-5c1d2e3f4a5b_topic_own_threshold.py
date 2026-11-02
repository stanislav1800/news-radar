"""topic own similarity threshold

Revision ID: 5c1d2e3f4a5b
Revises: 00e1e7cb7e66
Create Date: 2026-11-02 12:00:00+00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5c1d2e3f4a5b'
down_revision: str | Sequence[str] | None = '00e1e7cb7e66'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Свой порог похожести темы, подстроенный по отметкам; пусто — общий из настроек
    op.add_column('topic', sa.Column('min_similarity', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('topic', 'min_similarity')
