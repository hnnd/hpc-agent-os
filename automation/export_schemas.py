"""Regenerate model schemas and actual MCP tool catalogs without network or services."""

import asyncio
import json
from pathlib import Path

from mcp import Client
from hpc_agent_os.models import Config, Manifest, Response
from hpc_agent_os.catalog import CatalogConfig, CatalogSnapshot
from hpc_agent_os.live import LiveConfig
from hpc_agent_os.operations import OperationRequest, Policy
from hpc_agent_os.operations_cli import build_operations_server
from hpc_agent_os.server import build_server
from hpc_agent_os.service import Application
from hpc_agent_os.slurm import CachedSlurm, MockAdapter

ROOT = Path(__file__).resolve().parents[1]


async def main():
    for name, model in (
        ("config", Config),
        ("catalog-config", CatalogConfig),
        ("catalog-snapshot", CatalogSnapshot),
        ("live-config", LiveConfig),
        ("operation-request", OperationRequest),
        ("operation-policy", Policy),
        ("manifest", Manifest),
        ("response", Response),
    ):
        (ROOT / f"registry/schemas/{name}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=2) + "\n"
        )
    async with Client(build_operations_server(None, None)) as client:
        response = await client.list_tools()
        tools = response.tools if hasattr(response, "tools") else response
        (ROOT / "mcp-servers/operations.tools.json").write_text(
            json.dumps(
                [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
                indent=2,
            )
            + "\n"
        )
    for kind in ("slurm", "software", "database"):
        app = Application(Config(), slurm=CachedSlurm(MockAdapter(), min_interval=0))
        async with Client(build_server(kind, app)) as client:
            response = await client.list_tools()
            tools = response.tools if hasattr(response, "tools") else response
            (ROOT / f"mcp-servers/{kind}.tools.json").write_text(
                json.dumps(
                    [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
                    indent=2,
                )
                + "\n"
            )


if __name__ == "__main__":
    asyncio.run(main())
