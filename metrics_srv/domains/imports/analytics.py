"""Срез завершённых импортов и результатов Allure для сбора метрик."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import Numeric, Select, case, cast, func, literal, select, union_all
from sqlalchemy.orm import Session, sessionmaker

from metrics_srv.config import Settings
from metrics_srv.domains.imports.models import Import, TestResult

_STATUSES = ("passed", "failed", "broken", "skipped", "unknown")


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class MetricsReader:
    """Читает согласованный срез данных для одного scrape."""

    def __init__(self, settings: Settings, sessions: sessionmaker[Session]) -> None:
        self._projects = settings.metrics_projects
        self._sessions = sessions

    def render(self) -> str:
        """Возвращает агрегаты в текстовом формате Prometheus."""
        project = case(
            (func.nullif(func.btrim(Import.project), "").is_(None), "unknown"),
            (Import.project.in_(self._projects), Import.project),
            else_="other",
        )
        completed = (
            select(Import.id, project.label("project"))
            .where(Import.status == "completed")
            .cte("completed")
        )
        history_id = func.nullif(TestResult.history_id, "")
        duration = case((
            TestResult.stopped_at_ms >= TestResult.started_at_ms,
            (cast(TestResult.stopped_at_ms, Numeric) - cast(TestResult.started_at_ms, Numeric))
            / 1000,
        ))
        ranked = select(
            completed.c.project,
            case((TestResult.status.in_(_STATUSES), TestResult.status), else_="unknown").label(
                "status"
            ),
            duration.label("duration"),
            func.row_number().over(
                partition_by=(
                    TestResult.import_id, history_id,
                    case((history_id.is_(None), TestResult.id)),
                ),
                order_by=(
                    TestResult.stopped_at_ms.desc().nulls_last(),
                    TestResult.started_at_ms.desc().nulls_last(),
                    TestResult.position.desc(),
                ),
            ).label("rank"),
        ).join_from(TestResult, completed, completed.c.id == TestResult.import_id).cte("ranked")

        def results(kind: str) -> Select:
            return select(
                literal(kind), ranked.c.project, ranked.c.status, func.count(),
                func.count(ranked.c.duration), func.coalesce(func.sum(ranked.c.duration), 0),
            ).group_by(ranked.c.project, ranked.c.status)

        statement = union_all(
            select(
                literal("run"), completed.c.project, literal(""), func.count(),
                literal(0), literal(0),
            ).group_by(completed.c.project),
            results("attempt"),
            results("final").where(ranked.c.rank == 1),
        )
        output = [
            "# TYPE testbridge_runs gauge",
            "# TYPE testbridge_test_results gauge",
            "# TYPE testbridge_test_duration_seconds_sum gauge",
            "# TYPE testbridge_test_duration_seconds_count gauge",
        ]
        durations: dict[tuple[str, str], float] = defaultdict(float)
        duration_counts: dict[tuple[str, str], int] = defaultdict(int)
        with self._sessions() as session:
            rows = sorted(session.execute(statement), key=lambda row: (row[0], row[1], row[2]))
            for kind, name, status, count, duration_count, seconds in rows:
                labels = f'project="{_label(name)}"'
                if kind == "run":
                    output.append(f"testbridge_runs{{{labels}}} {count}")
                else:
                    labels += f',kind="{kind}"'
                    output.append(f'testbridge_test_results{{{labels},status="{status}"}} {count}')
                    durations[name, kind] += float(seconds)
                    duration_counts[name, kind] += duration_count
        for (name, kind), count in sorted(duration_counts.items()):
            labels = f'project="{_label(name)}",kind="{kind}"'
            output.append(f"testbridge_test_duration_seconds_sum{{{labels}}} {durations[name, kind]}")
            output.append(f"testbridge_test_duration_seconds_count{{{labels}}} {count}")
        return "\n".join(output) + "\n"
