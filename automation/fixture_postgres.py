"""Own and stop only a new project-local, Unix-socket-only test PostgreSQL process.

No production DSN, system services, TCP listeners, existing data directories or cleanup
of existing files. Test directories are retained for inspection, never recursively removed.
"""

from contextlib import contextmanager
from pathlib import Path
import subprocess
import os
import tempfile

import psycopg
from sqlalchemy import create_engine
from alembic.config import Config
from alembic import command

ROOT = Path(__file__).resolve().parents[1]


def run(argv):
    result = subprocess.run([str(x) for x in argv], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Fixture PostgreSQL command failed; inspect local fixture logs")
    return result


@contextmanager
def postgres_fixture():
    if any(name.startswith("PG") for name in os.environ):
        raise RuntimeError("Clear libpq PG environment overrides before isolated testing")
    binary = ROOT / ".runtime/postgres/bin"
    if not (binary / "postgres").is_file():
        raise RuntimeError("Provision project-local PostgreSQL using docs/development.md")
    base = ROOT / ".runtime/p"
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="", dir=base))
    socket = directory / "s"
    socket.mkdir(mode=0o700)
    if len(str(socket)) + 16 >= 108:
        raise RuntimeError("Project path too long for PostgreSQL Unix sockets")
    data = directory / "data"
    run(
        [
            binary / "initdb",
            "-D",
            data,
            "--auth-local=trust",
            "--auth-host=reject",
            "--no-locale",
            "--encoding=UTF8",
            "--username=hpc_fixture_admin",
        ]
    )
    try:
        run(
            [
                binary / "pg_ctl",
                "-D",
                data,
                "-l",
                directory / "postgres.log",
                "-w",
                "-t",
                "20",
                "-o",
                f"-h '' -k {socket} -p 5432 -c unix_socket_permissions=0700 -c max_connections=20 -c shared_buffers=32MB",
                "start",
            ]
        )
        with psycopg.connect(
            host=str(socket), dbname="postgres", user="hpc_fixture_admin", autocommit=True
        ) as conn:
            conn.execute("CREATE DATABASE hpc_fixture")
            conn.execute("CREATE ROLE hpc_query LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
            conn.execute("CREATE ROLE hpc_import LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")

        def dsn(role):
            return f"postgresql+psycopg://{role}@/hpc_fixture?host={socket}"

        engine = create_engine(dsn("hpc_fixture_admin"))
        try:
            with engine.begin() as conn:
                cfg = Config(str(ROOT / "alembic.ini"))
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "head")
                conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO hpc_query, hpc_import")
                conn.exec_driver_sql(
                    "GRANT SELECT ON registry_public, registry_indexes TO hpc_query"
                )
                conn.exec_driver_sql(
                    "GRANT SELECT, INSERT ON cluster, release, database_index TO hpc_import"
                )
                conn.exec_driver_sql("ALTER ROLE hpc_query SET default_transaction_read_only = on")
                conn.exec_driver_sql("ALTER ROLE hpc_query SET statement_timeout = '3s'")
            yield {
                "admin": dsn("hpc_fixture_admin"),
                "query": dsn("hpc_query"),
                "import": dsn("hpc_import"),
                "socket": str(socket),
                "directory": str(directory),
            }
        finally:
            engine.dispose()
    finally:
        if (data / "postmaster.pid").exists():
            # pg_ctl targets only the newly created data directory, never a system service.
            run([binary / "pg_ctl", "-D", data, "-m", "fast", "-w", "-t", "20", "stop"])
