"""Pure schema and role initialization for caller-owned database connections."""

import psycopg

from hpc_agent_os.catalog import DDL


def initialize_schema(conn):
    conn.execute(DDL)
    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    for role in ("hpc_catalog_query", "hpc_catalog_import"):
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            conn.execute(
                psycopg.sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                    psycopg.sql.Identifier(role)
                )
            )
        conn.execute(
            psycopg.sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                psycopg.sql.Identifier(role)
            )
        )
        conn.execute(
            psycopg.sql.SQL(
                "GRANT SELECT ON catalog_snapshot,catalog_record,catalog_active TO {}"
            ).format(psycopg.sql.Identifier(role))
        )
    conn.execute(
        "GRANT INSERT ON catalog_snapshot,catalog_record,catalog_active TO hpc_catalog_import"
    )
    conn.execute("GRANT UPDATE(snapshot_digest) ON catalog_active TO hpc_catalog_import")
