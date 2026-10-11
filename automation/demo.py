"""Short-lived synthetic demo; owns its PostgreSQL process and stops it on exit."""

import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from fixture_postgres import postgres_fixture
from hpc_agent_os.models import Manifest
from hpc_agent_os.registry import fixture_engine, import_manifest

ROOT = Path(__file__).resolve().parents[1]


async def demo(database):
    for kind, name in (
        ("slurm", "cluster_summary"),
        ("software", "search_software"),
        ("database", "get_database_release"),
    ):
        request = {"cluster_id": "fixture-cluster"}
        if kind == "database":
            request["release_id"] = "database-1.0"
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "hpc_agent_os.cli",
                kind,
                "--config",
                str(ROOT / "config/phase2.mock.json"),
            ],
            cwd=str(ROOT),
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HPC_REGISTRY_DSN": database["query"],
            },
        )
        async with Client(params, read_timeout_seconds=10) as client:
            result = await client.call_tool(name, {"request": request})
            print(
                json.dumps({"tool": name, "result": result.structured_content}, ensure_ascii=False)
            )


if __name__ == "__main__":
    with postgres_fixture() as database:
        engine = fixture_engine(database["import"], ROOT, role="hpc_import")
        try:
            import_manifest(
                engine,
                Manifest.model_validate_json((ROOT / "tests/fixtures/registry.json").read_text()),
            )
        finally:
            engine.dispose()
        asyncio.run(demo(database))
