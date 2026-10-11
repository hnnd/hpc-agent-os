"""End-to-end CLI service boundary with project-only PostgreSQL and fake Slurm."""

import hashlib
import os
from pathlib import Path

import psycopg
import pytest

from fixture_postgres import postgres_fixture
from operations_schema import initialize_schema
from hpc_agent_os import operations as ops
from hpc_agent_os_app.application import App
from hpc_agent_os_app.backend import Backend
from hpc_agent_os_app.plugins import Artifact, Plugins, Verification


@pytest.fixture(scope="module")
def pg():
    with postgres_fixture() as fixture:
        with psycopg.connect(
            host=fixture["socket"], dbname="hpc_fixture", user="hpc_fixture_admin"
        ) as conn:
            initialize_schema(conn)
        yield fixture


def build(tmp_path, pg, monkeypatch, key):
    root = tmp_path.resolve()
    policy = ops.Policy(
        expected_host="test",
        expected_uid=os.getuid(),
        software_root=str(root),
        database_root=str(root),
        allowed_download_hosts=[],
        jobs_autoexecute=True,
    )
    planner = ops.Planner(root, policy)
    backend = Backend(root, "live")

    def components(executor=False):
        role = "hpc_ops_executor" if executor else "hpc_ops_api"
        return planner, ops.Store(
            lambda: psycopg.connect(host=pg["socket"], dbname="hpc_fixture", user=role), os.getuid()
        )

    monkeypatch.setattr(backend, "components", components)
    script = root / ".runtime/phase4/inbox/test.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\necho fixture-only\n")
    request = {
        "operation": "job.submit",
        "idempotency_key": key,
        "script_path": str(script),
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    }
    return App(root, backend, Plugins()), backend, request


def clear_gate(pg):
    with psycopg.connect(host=pg["socket"], dbname="hpc_fixture", user="hpc_fixture_admin") as conn:
        conn.execute("UPDATE operation_gate SET next_at=now()-interval '1 second'")


def test_real_store_claim_refresh_verify_export(pg, tmp_path, monkeypatch):
    app, backend, request = build(tmp_path, pg, monkeypatch, "app-lifecycle")
    task = app.plan(request, verification=Verification(artifacts=[Artifact(path="report.tsv")]))
    tid = task["plan"]["id"]
    calls = []

    def run(self, argv):
        calls.append(argv)
        if argv[0] == "/usr/bin/sbatch":
            return "4321\n"
        assert argv[0] == "/usr/bin/sacct"
        return f"4321|{os.getuid()}|hpcop-{tid[:12]}|COMPLETED|0:0\n"

    monkeypatch.setattr(ops.BoundedRunner, "run", run)
    assert app.execute(tid, "job.submit")["state"] == "submitted"
    with pytest.raises(ValueError):
        app.execute(tid, "job.submit")
    assert len(calls) == 1
    clear_gate(pg)
    assert backend.refresh(tid)["state"] == "succeeded"
    assert not app.verify(tid)["verified"]
    (Path(task["plan"]["work"]) / "report.tsv").write_text("synthetic\n")
    assert app.verify(tid)["verified"]
    assert backend.audit(tid)["status"] == "unavailable"  # No SELECT grant; honest export.
    assert app.export(tid, tmp_path / "export.json")["exported"]
    assert len(calls) == 2


def test_unknown_submission_never_retried(pg, tmp_path, monkeypatch):
    app, backend, request = build(tmp_path, pg, monkeypatch, "app-unknown")
    tid = app.plan(request)["plan"]["id"]
    calls = []

    def run(self, argv):
        calls.append(argv)
        raise TimeoutError("synthetic private error")

    monkeypatch.setattr(ops.BoundedRunner, "run", run)
    with pytest.raises(ValueError, match="uncertain"):
        app.execute(tid, "job.submit")
    assert backend.get(tid)["state"] == "uncertain"
    assert app.wait(tid)["stopped"] == "action_or_reconciliation_required"
    with pytest.raises(ValueError):
        app.execute(tid, "job.submit")
    assert len(calls) == 1


def test_cancel_uses_separate_intent_and_shared_gate(pg, tmp_path, monkeypatch):
    app, backend, _ = build(tmp_path, pg, monkeypatch, "unused")
    calls = []

    def run(self, argv):
        calls.append(argv)
        if argv[0] == "/usr/bin/squeue":
            return f"4321|{os.getuid()}|2026-10-11T00:00:00\n"
        assert argv[0] == "/usr/bin/scancel"
        return ""

    monkeypatch.setattr(ops.BoundedRunner, "run", run)
    clear_gate(pg)
    task = app.plan({"operation": "job.cancel", "idempotency_key": "app-cancel", "job_id": "4321"})
    tid = task["plan"]["id"]
    with pytest.raises(ValueError, match="operation"):
        app.execute(tid, "job.submit")
    with pytest.raises(ValueError, match="gate"):
        app.execute(tid, "job.cancel")
    assert len(calls) == 1
    clear_gate(pg)
    assert app.execute(tid, "job.cancel")["state"] == "cancel_requested"
    assert [call[0] for call in calls] == ["/usr/bin/squeue", "/usr/bin/squeue", "/usr/bin/scancel"]
