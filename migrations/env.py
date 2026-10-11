"""Migrations require an explicitly provided fixture connection; no environment DSN fallback."""

from alembic import context
from hpc_agent_os.registry import metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Provide an isolated fixture connection through the test harness")
context.configure(connection=connection, target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
