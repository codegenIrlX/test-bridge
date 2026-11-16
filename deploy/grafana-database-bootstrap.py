"""Подготовка аналитических представлений и доступа Grafana к PostgreSQL."""

from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml
from psycopg import sql

from metrics_srv.config import load_grafana_database_password, load_settings
from metrics_srv.repository import Repository

ROLE = "testbridge_grafana"


def main() -> None:
    """Создать доступ к представлениям и подготовить datasource во временном томе."""
    settings = load_settings()
    password = load_grafana_database_password()
    with Repository(settings).connect() as connection:
        connection.execute(Path(__file__).with_name('grafana_analytics.sql').read_text(encoding='utf-8'))
        role = connection.execute(
            'SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls '
            'AS privileged FROM pg_roles WHERE rolname = %s', (ROLE,)
        ).fetchone()
        if role is None:
            connection.execute(sql.SQL(
                'CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT '
                'NOREPLICATION NOBYPASSRLS'
            ).format(sql.Identifier(ROLE)))
        elif role['privileged'] or connection.execute(
            'SELECT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r '
            'ON r.oid = m.member WHERE r.rolname = %s) AS has_memberships', (ROLE,)
        ).fetchone()['has_memberships']:
            raise RuntimeError('Роль Grafana обладает дополнительными привилегиями')
        connection.execute(sql.SQL('ALTER ROLE {} WITH LOGIN PASSWORD {}').format(
            sql.Identifier(ROLE), sql.Literal(password)
        ))
        if connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname !~ '^pg_' "
            "AND n.nspname <> 'information_schema' AND "
            "has_schema_privilege(%s, n.oid, 'CREATE')) AS allowed", (ROLE,)
        ).fetchone()['allowed']:
            raise RuntimeError('Роль Grafana может создавать объекты в пользовательской схеме')
        if connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            "AND NOT (n.nspname = 'testbridge_analytics' AND c.relname IN ('tests', 'runs')) AND "
            "CASE WHEN c.relkind IN ('r', 'p', 'v', 'm', 'f') THEN "
            "has_table_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
            "ELSE false END) "
            "AS allowed", (ROLE,)
        ).fetchone()['allowed']:
            raise RuntimeError('Роль Grafana имеет лишний доступ к данным')
        if connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'testbridge_analytics' AND c.relname IN ('tests', 'runs') AND "
            "has_table_privilege(%s, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')) "
            "AS allowed", (ROLE,)
        ).fetchone()['allowed']:
            raise RuntimeError('Роль Grafana имеет права записи в аналитические представления')
        if connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            "AND CASE WHEN c.relkind = 'S' THEN "
            "has_sequence_privilege(%s, c.oid, 'USAGE,SELECT,UPDATE') ELSE false END) "
            "AS allowed", (ROLE,)
        ).fetchone()['allowed']:
            raise RuntimeError('Роль Grafana имеет доступ к последовательностям приложения')
        if connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            "AND has_function_privilege(%s, p.oid, 'EXECUTE')) AS allowed", (ROLE,)
        ).fetchone()['allowed']:
            raise RuntimeError('Роль Grafana может выполнять пользовательские функции')
        connection.execute(sql.SQL('GRANT USAGE ON SCHEMA testbridge_analytics TO {}').format(
            sql.Identifier(ROLE)
        ))
        connection.execute(sql.SQL(
            'GRANT SELECT ON testbridge_analytics.tests, testbridge_analytics.runs TO {}'
        ).format(sql.Identifier(ROLE)))

    base = yaml.safe_load(Path('deploy/grafana/provisioning/datasources/victoriametrics.yaml').read_text(
        encoding='utf-8'
    ))
    base['datasources'].append({
        'name': 'TestBridge PostgreSQL', 'uid': 'testbridge-postgres', 'type': 'postgres',
        'access': 'proxy', 'url': f'{settings.database_host}:{settings.database_port}',
        'user': ROLE, 'editable': False,
        'jsonData': {'database': settings.database_name, 'sslmode': 'disable', 'postgresVersion': 1700},
        'secureJsonData': {'password': password.replace('$', '$$')},
    })
    target = Path('/run/grafana-auth')
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=target, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            yaml.safe_dump(base, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            temporary.chmod(0o444)
            temporary.replace(target / 'datasources.yaml')
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
