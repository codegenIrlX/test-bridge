"""Добавляет метаданные Jenkins и поля результатов Allure для аналитики."""

import sqlalchemy as sa
from alembic import op

revision = "0003_run_analytics"
down_revision = "0002_failed_archive_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Добавляет поля аналитики к импортам, результатам и шагам."""
    op.add_column(
        "imports", sa.Column("project", sa.String(255), nullable=True, comment="Проект Jenkins")
    )
    op.add_column(
        "imports",
        sa.Column("job", sa.String(255), nullable=True, comment="Полное имя задания Jenkins"),
    )
    op.add_column(
        "imports",
        sa.Column("build_number", sa.String(255), nullable=True, comment="Номер сборки Jenkins"),
    )
    op.add_column(
        "imports",
        sa.Column("build_url", sa.String(2048), nullable=True, comment="Ссылка на сборку Jenkins"),
    )
    op.add_column(
        "imports",
        sa.Column("revision", sa.String(255), nullable=True, comment="Ревизия исходного кода сборки"),
    )
    op.add_column(
        "test_results",
        sa.Column("status", sa.Text(), nullable=True, comment="Статус результата теста из Allure"),
    )
    op.add_column(
        "test_results",
        sa.Column("history_id", sa.Text(), nullable=True, comment="Идентификатор истории теста Allure"),
    )
    op.add_column(
        "test_steps", sa.Column("status", sa.Text(), nullable=True, comment="Статус шага из Allure")
    )


def downgrade() -> None:
    """Удаляет поля аналитики."""
    op.drop_column("test_steps", "status")
    op.drop_column("test_results", "history_id")
    op.drop_column("test_results", "status")
    op.drop_column("imports", "revision")
    op.drop_column("imports", "build_url")
    op.drop_column("imports", "build_number")
    op.drop_column("imports", "job")
    op.drop_column("imports", "project")
