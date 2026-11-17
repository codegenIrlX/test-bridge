"""Добавляет срок хранения архивов неуспешных импортов."""

import sqlalchemy as sa
from alembic import op

revision = "0002_failed_archive_retention"
down_revision = "0001_imports_orm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Добавляет дату сбоя и даёт старым неуспешным импортам полный срок хранения."""
    op.add_column(
        "imports",
        sa.Column(
            "failed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Время перехода импорта в состояние failed",
        ),
    )
    imports = sa.table("imports", sa.column("status"), sa.column("failed_at"))
    op.execute(
        imports.update().where(imports.c.status == "failed")
        .values(failed_at=sa.func.statement_timestamp())
    )


def downgrade() -> None:
    """Удаляет время сбоя из записей импорта."""
    op.drop_column("imports", "failed_at")
