"""Pure schema and role initialization for caller-owned database connections."""

import psycopg

from hpc_agent_os.operations import DDL


def initialize_schema(conn):
    conn.execute(DDL)
    for role in ("hpc_ops_api", "hpc_ops_approver", "hpc_ops_executor"):
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
            psycopg.sql.SQL("GRANT SELECT ON operation_task,operation_approval TO {}").format(
                psycopg.sql.Identifier(role)
            )
        )
        conn.execute(
            psycopg.sql.SQL("GRANT INSERT ON operation_audit TO {}").format(
                psycopg.sql.Identifier(role)
            )
        )
        conn.execute(
            psycopg.sql.SQL("GRANT USAGE ON SEQUENCE operation_audit_id_seq TO {}").format(
                psycopg.sql.Identifier(role)
            )
        )
    conn.execute("GRANT SELECT,INSERT,UPDATE ON operation_gate TO hpc_ops_api,hpc_ops_executor")
    conn.execute("GRANT INSERT ON operation_task TO hpc_ops_api")
    conn.execute("GRANT INSERT,UPDATE ON operation_approval TO hpc_ops_approver")
    conn.execute("GRANT UPDATE(state,result,updated_at) ON operation_task TO hpc_ops_executor")
    conn.execute("GRANT UPDATE(consumed_at) ON operation_approval TO hpc_ops_executor")
