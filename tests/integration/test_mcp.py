"""Two independent clients: official SDK and raw newline JSON-RPC over real stdio."""

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

from mcp import Client
from mcp.client.stdio import StdioServerParameters
import pytest

ROOT = Path(__file__).resolve().parents[2]
CATALOGS = {
    "slurm": {"cluster_summary", "list_nodes", "list_partitions", "list_my_jobs", "get_my_job"},
    "software": {"search_software", "get_software_release"},
    "database": {"search_databases", "get_database_release"},
}
pytestmark = pytest.mark.protocol


def args(kind):
    return ["-m", "hpc_agent_os.cli", kind, "--config", str(ROOT / "config/phase2.mock.json")]


def environment(pg):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HPC_REGISTRY_DSN": pg["query"]}


def tool_args(name):
    request = {"cluster_id": "fixture-cluster"}
    if name == "get_my_job":
        request["job_id"] = "100"
    if name == "get_software_release":
        request["release_id"] = "software-1.0"
    if name == "get_database_release":
        request["release_id"] = "database-1.0"
    return {"request": request}


@pytest.mark.parametrize("kind", list(CATALOGS))
async def test_official_sdk_full_catalog(pg, kind):
    params = StdioServerParameters(
        command=sys.executable, args=args(kind), cwd=str(ROOT), env=environment(pg)
    )
    async with Client(params, read_timeout_seconds=10) as client:
        result = await client.list_tools()
        tools = result.tools if hasattr(result, "tools") else result
        assert {t.name for t in tools} == CATALOGS[kind]
        for tool in tools:
            assert tool.annotations.read_only_hint is True
            response = await client.call_tool(tool.name, tool_args(tool.name))
            assert not response.is_error
            data = response.structured_content
            assert data["error"] is None and data["data"] is not None
            assert data["source"] in ("synthetic-slurm", "postgresql")
        denied = await client.call_tool(
            sorted(CATALOGS[kind])[0], {"request": {"cluster_id": "other"}}
        )
        assert denied.is_error or denied.structured_content["error"]["code"] in (
            "FORBIDDEN",
            "INVALID_ARGUMENT",
        )


class RawClient:
    def __init__(self, kind, pg):
        self.proc = subprocess.Popen(
            [sys.executable, *args(kind)],
            cwd=ROOT,
            env=environment(pg),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.pending = b""
        self.next_id = 0

    def send(self, payload):
        self.proc.stdin.write(json.dumps(payload).encode() + b"\n")
        self.proc.stdin.flush()

    def call(self, method, params):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        deadline = time.monotonic() + 10
        with selectors.DefaultSelector() as sel:
            sel.register(self.proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                while b"\n" in self.pending:
                    line, self.pending = self.pending.split(b"\n", 1)
                    msg = json.loads(line)
                    if msg.get("id") == self.next_id:
                        return msg
                if sel.select(0.1):
                    chunk = os.read(self.proc.stdout.fileno(), 65536)
                    if not chunk:
                        raise AssertionError("MCP process exited")
                    self.pending += chunk
        raise AssertionError("MCP response timed out")

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc.stdout.close()


@pytest.mark.parametrize("kind", list(CATALOGS))
def test_independent_jsonrpc_catalog(pg, kind):
    client = RawClient(kind, pg)
    try:
        init = client.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "independent-hpc-test", "version": "1.0"},
            },
        )
        assert "result" in init
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = client.call("tools/list", {})["result"]["tools"]
        assert {t["name"] for t in tools} == CATALOGS[kind]
        for tool in tools:
            response = client.call(
                "tools/call", {"name": tool["name"], "arguments": tool_args(tool["name"])}
            )["result"]
            assert not response.get("isError", False)
            assert response["structuredContent"]["error"] is None
        rejected = client.call("tools/call", {"name": "exec", "arguments": {"command": "id"}})
        assert "error" in rejected or rejected["result"]["isError"]
    finally:
        client.close()


async def test_protocol_rejects_identity_injection_and_invalid_arguments(pg):
    params = StdioServerParameters(
        command=sys.executable, args=args("slurm"), cwd=str(ROOT), env=environment(pg)
    )
    async with Client(params, read_timeout_seconds=10) as client:
        for tool, request in [
            ("list_my_jobs", {"cluster_id": "fixture-cluster", "user": "root"}),
            ("get_my_job", {"cluster_id": "fixture-cluster", "job_id": "1;id"}),
            ("list_nodes", {"cluster_id": "fixture-cluster", "limit": 501}),
        ]:
            result = await client.call_tool(tool, {"request": request})
            assert result.is_error
        result = await client.call_tool(
            "get_my_job", {"request": {"cluster_id": "fixture-cluster", "job_id": "999"}}
        )
        assert result.structured_content["error"]["code"] == "FORBIDDEN"
