import json
from pathlib import Path

import pytest
from hpc_agent_os.models import Config, Manifest, Response
from hpc_agent_os.server import build_server
from hpc_agent_os.service import Application
from hpc_agent_os.slurm import CachedSlurm, MockAdapter

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "name,model", [("config", Config), ("manifest", Manifest), ("response", Response)]
)
def test_exported_schema_matches_model(name, model):
    assert (
        json.loads((ROOT / f"registry/schemas/{name}.schema.json").read_text())
        == model.model_json_schema()
    )


@pytest.mark.parametrize("kind", ["slurm", "software", "database"])
async def test_exported_mcp_contract_matches_implementation(kind):
    server = build_server(kind, Application(Config(), slurm=CachedSlurm(MockAdapter())))
    tools = await server.list_tools()
    assert json.loads((ROOT / f"mcp-servers/{kind}.tools.json").read_text()) == [
        t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools
    ]


def test_invalid_server_kind():
    with pytest.raises(ValueError):
        build_server("exec", Application(Config()))
