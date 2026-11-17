"""Создаёт таблицы импортов, результатов и шагов Allure."""

import sqlalchemy as sa
from alembic import op

revision = "0001_imports_orm"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создаёт исходную схему хранения импортов."""
    op.create_table(
        "imports",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Идентификатор импорта и имя архива"),
        sa.Column(
            "status", sa.Text(), server_default="pending", nullable=False,
            comment="Состояние обработки архива",
        ),
        sa.Column("error_message", sa.Text(), comment="Безопасное описание ошибки обработки"),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False,
            comment="Число начатых попыток обработки",
        ),
        sa.Column(
            "archive_deleted", sa.Boolean(), server_default=sa.false(), nullable=False,
            comment="Признак удаления обработанного архива",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
            comment="Время регистрации импорта",
        ),
        sa.Column(
            "archive_cleanup_after", sa.DateTime(timezone=True),
            comment="Время следующей попытки удаления архива",
        ),
        sa.Column("idempotency_key", sa.Text(), comment="Ключ повторной отправки архива из CI"),
        sa.Column(
            "archive_sha256", sa.Text(),
            comment="Контрольная сумма SHA-256 архива с ключом повторной отправки",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__imports")),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name=op.f("ck__imports__status"),
        ),
        comment="Принятые архивы Allure и состояние их обработки",
    )
    op.create_index(
        op.f("ix__imports__idempotency_key"), "imports", ["idempotency_key"],
        unique=True, postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "test_results",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Идентификатор результата теста"),
        sa.Column("import_id", sa.Uuid(), nullable=False, comment="Импорт, содержащий результат"),
        sa.Column("position", sa.Integer(), nullable=False, comment="Порядок результата в архиве"),
        sa.Column("name", sa.Text(), nullable=False, comment="Название теста из Allure"),
        sa.Column("started_at_ms", sa.BigInteger(), comment="Начало выполнения в миллисекундах Unix"),
        sa.Column(
            "stopped_at_ms", sa.BigInteger(), comment="Окончание выполнения в миллисекундах Unix"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__test_results")),
        sa.ForeignKeyConstraint(
            ["import_id"], ["imports.id"], name=op.f("fk__test_results__import_id__imports")
        ),
        sa.UniqueConstraint("import_id", "position", name=op.f("uq__test_results__import_id_position")),
        comment="Результаты тестов из принятых архивов Allure",
    )
    op.create_table(
        "test_steps",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Идентификатор шага"),
        sa.Column("test_id", sa.Uuid(), nullable=False, comment="Тест, которому принадлежит шаг"),
        sa.Column("parent_id", sa.Uuid(), comment="Родительский шаг, если шаг вложен"),
        sa.Column("position", sa.Integer(), nullable=False, comment="Порядок шага среди соседних шагов"),
        sa.Column("name", sa.Text(), nullable=False, comment="Название шага из Allure"),
        sa.Column("started_at_ms", sa.BigInteger(), comment="Начало выполнения в миллисекундах Unix"),
        sa.Column(
            "stopped_at_ms", sa.BigInteger(), comment="Окончание выполнения в миллисекундах Unix"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__test_steps")),
        sa.ForeignKeyConstraint(
            ["test_id"], ["test_results.id"], name=op.f("fk__test_steps__test_id__test_results")
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["test_steps.id"], name=op.f("fk__test_steps__parent_id__test_steps")
        ),
        comment="Шаги тестов Allure и связи между вложенными шагами",
    )


def downgrade() -> None:
    """Удаляет таблицы импортов, результатов и шагов вместе с данными."""
    op.drop_table("test_steps")
    op.drop_table("test_results")
    op.drop_table("imports")
