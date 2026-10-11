import hashlib
import os
from pathlib import Path

import pytest

from hpc_agent_os.operations import read_script
from hpc_agent_os.publish import publish


def test_script_open_is_nonblocking(tmp_path, monkeypatch):
    path = tmp_path / "script.sh"
    path.write_text("echo ok")
    original = os.open

    def checked(path, flags, *args):
        assert flags & os.O_NONBLOCK
        return original(path, flags, *args)

    monkeypatch.setattr(os, "open", checked)
    assert read_script(path, tmp_path) == b"echo ok"


def test_download_disables_curl_config(tmp_path, monkeypatch):
    import hpc_agent_os.publish as module

    blob = b">x\nACGT\n"
    monkeypatch.setattr(
        module.socket, "getaddrinfo", lambda *a: [(None, None, None, None, ("93.184.216.34", 443))]
    )
    recipe = {
        "operation": "database.update",
        "name": "test",
        "version": "v1",
        "url": "https://example.org/data.fa",
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size_bytes": len(blob),
        "filename": "data.fa",
        "license": "test",
    }

    class Runner:
        def run(self, argv):
            assert argv[:2] == ("/usr/bin/curl", "--disable")
            Path(argv[argv.index("--output") + 1]).write_bytes(blob)
            return ""

    result = publish(
        {"id": "test-task", "recipe": recipe, "target": str(tmp_path / "test/v1")}, Runner()
    )
    assert result["published"]


def test_fifo_and_symlink_script_rejected(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        read_script(fifo, tmp_path)
    outside = tmp_path.parent / "outside-script"
    outside.write_text("echo private")
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        read_script(link, tmp_path)


def test_failed_staging_can_retry_and_preserves_existing(tmp_path, monkeypatch):
    import hpc_agent_os.publish as module

    blob = b"database bytes"
    monkeypatch.setattr(
        module.socket, "getaddrinfo", lambda *a: [(None, None, None, None, ("93.184.216.34", 443))]
    )
    recipe = {
        "operation": "database.update",
        "name": "db",
        "version": "v1",
        "url": "https://example.org/db",
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size_bytes": len(blob),
        "filename": "db.fa",
        "license": "test",
    }
    target = tmp_path / "db/v1"

    class Runner:
        def __init__(self, data):
            self.data = data

        def run(self, argv):
            Path(argv[argv.index("--output") + 1]).write_bytes(self.data)
            return ""

    with pytest.raises(ValueError):
        publish({"id": "failed", "target": str(target), "recipe": recipe}, Runner(b"wrong"))
    assert not target.exists()
    assert (target.parent / ".hpc-stage-failed").exists()
    result = publish({"id": "retry", "target": str(target), "recipe": recipe}, Runner(blob))
    assert result["changed"] and (target / "db.fa").read_bytes() == blob
    result = publish(
        {"id": "repeat", "target": str(target), "recipe": recipe}, Runner(b"never-used")
    )
    assert not result["changed"]
    assert (target / "db.fa").read_bytes() == blob


def test_cancel_requires_owned_identity_and_no_option_job_ids(tmp_path):
    from hpc_agent_os.operations import Planner, Policy

    policy = Policy(
        expected_host="test",
        expected_uid=os.getuid(),
        software_root=str(tmp_path),
        database_root=str(tmp_path),
        allowed_download_hosts=[],
    )

    class Runner:
        def run(self, argv):
            return "123|0|2026-01-01T00:00:00\n"

    planner = Planner(tmp_path, policy, Runner())
    with pytest.raises(ValueError):
        planner.compile({"operation": "job.cancel", "idempotency_key": "cancel", "job_id": "123"})
    with pytest.raises(ValueError):
        planner.compile({"operation": "job.cancel", "idempotency_key": "cancel", "job_id": "--all"})


def test_unknown_recipe_does_not_create_public_directories(tmp_path):
    from hpc_agent_os.operations import Planner, Policy

    policy = Policy(
        expected_host="test",
        expected_uid=os.getuid(),
        software_root=str(tmp_path / "missing"),
        database_root=str(tmp_path / "missing"),
        allowed_download_hosts=[],
    )
    with pytest.raises(ValueError, match="enrolled"):
        Planner(tmp_path, policy).compile(
            {"operation": "software.install", "idempotency_key": "missing", "recipe_id": "unknown"}
        )
    assert not (tmp_path / "missing").exists()


@pytest.mark.asyncio
async def test_mcp_catalog_exposes_only_scoped_maintenance_execution(tmp_path):
    from mcp import Client
    from hpc_agent_os.operations import Planner, Policy
    from hpc_agent_os.operations_cli import build_operations_server

    policy = Policy(
        expected_host="test",
        expected_uid=os.getuid(),
        software_root=str(tmp_path),
        database_root=str(tmp_path),
        allowed_download_hosts=[],
    )
    async with Client(build_operations_server(Planner(tmp_path, policy), None)) as client:
        response = await client.list_tools()
        tools = response.tools if hasattr(response, "tools") else response
        assert {tool.name for tool in tools} == {
            "prepare_operation",
            "get_operation",
            "list_operation_recipes",
            "execute_maintenance",
            "execute_my_job",
        }
        result = await client.call_tool("list_operation_recipes", {})
        assert result.structured_content["recipes"] == {}
        result = await client.call_tool(
            "prepare_operation",
            {
                "request": {
                    "operation": "database.update",
                    "recipe_id": "not-enrolled",
                    "idempotency_key": "test",
                }
            },
        )
        assert result.structured_content["error"] == "PROPOSAL_REJECTED"


@pytest.mark.parametrize("interrupted", [False, True])
def test_gpfs_fallback_uses_exclusive_manifest_commit(tmp_path, monkeypatch, interrupted):
    import ctypes
    import errno
    import hpc_agent_os.publish as module

    blob = b"validated fixture"

    class Unsupported:
        def __call__(self, *args):
            ctypes.set_errno(errno.EINVAL)
            return -1

    class Library:
        renameat2 = Unsupported()

    monkeypatch.setattr(module.ctypes, "CDLL", lambda *a, **kw: Library())
    monkeypatch.setattr(
        module.socket, "getaddrinfo", lambda *a: [(None, None, None, None, ("93.184.216.34", 443))]
    )
    recipe = {
        "operation": "database.update",
        "name": "db",
        "version": "v1",
        "url": "https://example.org/db",
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size_bytes": len(blob),
        "filename": "db.fa",
        "license": "test",
    }
    target = tmp_path / "db/v1"

    class Runner:
        def run(self, argv):
            Path(argv[argv.index("--output") + 1]).write_bytes(blob)
            return ""

    if interrupted:
        link = os.link

        def interrupt_marker(source, destination):
            if Path(destination) == target / ".publication-intent.json":
                raise OSError("simulated interruption after mkdir")
            return link(source, destination)

        monkeypatch.setattr(os, "link", interrupt_marker)
        with pytest.raises(OSError, match="simulated interruption"):
            publish({"id": "interrupted", "target": str(target), "recipe": recipe}, Runner())
        assert target.is_dir() and not any(target.iterdir())
        assert list(target.parent.glob(".hpc-reservation-*"))
        monkeypatch.setattr(os, "link", link)
    assert publish({"id": "one", "target": str(target), "recipe": recipe}, Runner())["changed"]
    assert (target / "manifest.json").is_file()
    assert (target / ".publication-intent.json").is_file()
    assert not publish({"id": "two", "target": str(target), "recipe": recipe}, Runner())["changed"]
    changed = {**recipe, "sha256": "0" * 64}
    with pytest.raises(ValueError):
        publish({"id": "three", "target": str(target), "recipe": changed}, Runner())
    assert (target / "db.fa").read_bytes() == blob


def test_maintenance_wrong_uid_rejected_before_planning(tmp_path):
    from hpc_agent_os.operations import Planner, Policy

    policy = Policy(
        expected_host="test",
        expected_uid=os.getuid() + 1,
        software_root=str(tmp_path),
        database_root=str(tmp_path),
        allowed_download_hosts=[],
        maintenance_autoexecute=True,
    )
    with pytest.raises(ValueError, match="identity"):
        Planner(tmp_path, policy).compile(
            {
                "operation": "database.update",
                "recipe_id": "unused",
                "idempotency_key": "wrong-owner",
            }
        )


@pytest.mark.asyncio
async def test_maintenance_mcp_rejects_jobs_even_when_owner_policy_enabled(tmp_path):
    from mcp import Client
    from hpc_agent_os.operations import Planner, Policy
    from hpc_agent_os.operations_cli import build_operations_server

    policy = Policy(
        expected_host="test",
        expected_uid=os.getuid(),
        software_root=str(tmp_path),
        database_root=str(tmp_path),
        allowed_download_hosts=[],
        maintenance_autoexecute=True,
    )

    class Store:
        def get(self, task_id):
            return {"plan": {"request": {"operation": "job.submit"}}}

    async with Client(
        build_operations_server(Planner(tmp_path, policy), Store(), object())
    ) as client:
        result = await client.call_tool("execute_maintenance", {"task_id": "job"})
        assert result.structured_content["error"] == "MAINTENANCE_NOT_EXECUTED_OR_UNCERTAIN"


@pytest.mark.asyncio
async def test_maintenance_mcp_host_preflight_blocks_before_claim(tmp_path, monkeypatch):
    import time
    from mcp import Client
    from hpc_agent_os.operations import Planner, Policy
    import hpc_agent_os.operations_cli as module

    policy = Policy(
        expected_host="test",
        expected_uid=os.getuid(),
        software_root=str(tmp_path),
        database_root=str(tmp_path),
        allowed_download_hosts=[],
        maintenance_autoexecute=True,
    )

    class Store:
        def get(self, task_id):
            return {
                "state": "proposed",
                "plan": {
                    "request": {"operation": "database.update"},
                    "target": str(tmp_path / "db/v1"),
                },
            }

    calls = []
    monkeypatch.setattr(module, "execute", lambda *a: calls.append(a) or {"state": "submitted"})
    info = tmp_path.stat()
    entry = {
        "writable": False,
        "identity": [
            info.st_dev,
            info.st_ino,
            info.st_uid,
            info.st_gid,
            info.st_mode,
            info.st_ctime_ns,
        ],
    }
    evidence = {"uid": os.getuid(), "observed_at": time.time(), "roots": {str(tmp_path): entry}}
    async with Client(
        module.build_operations_server(Planner(tmp_path, policy), Store(), object(), evidence)
    ) as client:
        result = await client.call_tool("execute_maintenance", {"task_id": "fixture"})
        assert "error" in result.structured_content and calls == []
        entry["writable"] = True
        evidence["observed_at"] = time.time() - 301
        result = await client.call_tool("execute_maintenance", {"task_id": "fixture"})
        assert "error" in result.structured_content and calls == []
        evidence["observed_at"] = time.time()
        result = await client.call_tool("execute_maintenance", {"task_id": "fixture"})
        assert result.structured_content["state"] == "submitted" and len(calls) == 1


@pytest.mark.asyncio
async def test_job_mcp_owner_routing_and_scope(tmp_path, monkeypatch):
    from mcp import Client
    from hpc_agent_os.operations import Planner, Policy
    import hpc_agent_os.operations_cli as module

    policy = Policy(
        expected_host="test",
        expected_uid=os.getuid(),
        software_root=str(tmp_path),
        database_root=str(tmp_path),
        allowed_download_hosts=[],
        jobs_autoexecute=True,
    )

    class Store:
        operation = "job.submit"

        def get(self, task_id):
            return {"plan": {"request": {"operation": self.operation}}}

    store = Store()
    calls = []
    monkeypatch.setattr(module, "execute", lambda *a: calls.append(a) or {"state": "submitted"})
    async with Client(
        module.build_operations_server(Planner(tmp_path, policy), store, object())
    ) as client:
        result = await client.call_tool("execute_my_job", {"task_id": "fixture"})
        assert result.structured_content["state"] == "submitted" and len(calls) == 1
        store.operation = "software.install"
        result = await client.call_tool("execute_my_job", {"task_id": "fixture"})
        assert "error" in result.structured_content and len(calls) == 1
        store.operation = "job.submit"
        policy.jobs_autoexecute = False
        result = await client.call_tool("execute_my_job", {"task_id": "fixture"})
        assert "error" in result.structured_content and len(calls) == 1
