import hashlib
import os
from pathlib import Path

import psycopg
import pytest

from fixture_postgres import postgres_fixture
from operations_schema import initialize_schema
from hpc_agent_os.operations import Planner, Policy, Store, execute, refresh, withdraw


@pytest.fixture(scope="module")
def database():
    with postgres_fixture() as pg:
        with psycopg.connect(
            host=pg["socket"], dbname="hpc_fixture", user="hpc_fixture_admin"
        ) as conn:
            initialize_schema(conn)
        yield pg


def connect(pg, role="hpc_fixture_admin"):
    return psycopg.connect(host=pg["socket"], dbname="hpc_fixture", user=role, connect_timeout=3)


def proposal(tmp_path, key):
    inbox = tmp_path / ".runtime/phase4/inbox"
    inbox.mkdir(parents=True)
    script = inbox / "job.sh"
    script.write_text("#!/bin/bash\necho synthetic-test-never-submit\n")
    policy = Policy(
        expected_host="test",
        expected_uid=os.getuid(),
        software_root=str(tmp_path),
        database_root=str(tmp_path),
        allowed_download_hosts=[],
    )
    planner = Planner(tmp_path, policy)
    request = {
        "operation": "job.submit",
        "idempotency_key": key,
        "script_path": str(script),
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    }
    return planner, planner.compile(request)


def test_approval_claim_replay_and_terminal_reconciliation(database, tmp_path):
    store = Store(lambda: connect(database), os.getuid())
    planner, plan = proposal(tmp_path, "lifecycle")
    assert store.propose(plan)["state"] == "proposed"
    assert store.propose(plan)["plan"]["digest"] == plan["digest"]
    with pytest.raises(ValueError):
        store.claim(plan["id"])
    with pytest.raises(ValueError):
        store.approve(plan["id"], "0" * 64)
    store.approve(plan["id"], plan["digest"])

    class Runner:
        calls = 0

        def run(self, argv):
            self.calls += 1
            assert argv[0] == "/usr/bin/sbatch"
            assert "--export=NIL" in argv
            return "12345\n"

    runner = Runner()
    result = execute(plan["id"], planner, store, runner)
    assert result["state"] == "submitted" and runner.calls == 1
    with pytest.raises(ValueError):
        execute(plan["id"], planner, store, runner)
    assert runner.calls == 1

    class Accounting:
        def run(self, argv):
            assert argv[0] == "/usr/bin/sacct"
            return f"12345|{os.getuid()}|hpcop-{plan['id'][:12]}|COMPLETED|0:0\n"

    assert refresh(plan["id"], store, Accounting())["state"] == "succeeded"


def test_expired_approval_renewal_and_withdraw(database, tmp_path):
    store = Store(lambda: connect(database), os.getuid())
    _, plan = proposal(tmp_path, "renewal")
    store.propose(plan)
    store.approve(plan["id"], plan["digest"])
    with connect(database) as conn:
        conn.execute(
            "UPDATE operation_approval SET expires_at=now()-interval '1 second' WHERE task_id=%s",
            (plan["id"],),
        )
    with pytest.raises(ValueError):
        store.claim(plan["id"])
    store.approve(plan["id"], plan["digest"])
    assert store.claim(plan["id"]) == plan
    # A separate unused proposal can be withdrawn, not an in-flight one.
    with pytest.raises(ValueError):
        withdraw(plan["id"], store)


def test_uncertain_result_never_resubmitted(database, tmp_path):
    store = Store(lambda: connect(database), os.getuid())
    planner, plan = proposal(tmp_path, "uncertain")
    store.propose(plan)
    store.approve(plan["id"], plan["digest"])

    class Runner:
        calls = 0

        def run(self, argv):
            self.calls += 1
            raise TimeoutError()

    runner = Runner()
    with pytest.raises(ValueError, match="uncertain"):
        execute(plan["id"], planner, store, runner)
    assert store.get(plan["id"])["state"] == "uncertain"
    with pytest.raises(ValueError):
        execute(plan["id"], planner, store, runner)
    assert runner.calls == 1


def test_api_role_cannot_approve_or_change_plans(database):
    with connect(database, "hpc_ops_api") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM operation_approval")
    with connect(database, "hpc_ops_api") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE operation_task SET state='succeeded'")
    with connect(database, "hpc_ops_api") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO operation_approval(task_id,digest,approver,expires_at) VALUES(gen_random_uuid(),'fake',1,now())"
            )


def test_mutation_and_script_drift_rejects_approval(database, tmp_path):
    store = Store(lambda: connect(database), os.getuid())
    planner, plan = proposal(tmp_path, "drift")
    store.propose(plan)
    store.approve(plan["id"], plan["digest"])
    Path(plan["request"]["script_path"]).write_text("echo changed")
    with pytest.raises(ValueError, match="digest"):
        execute(plan["id"], planner, store)
    assert store.get(plan["id"])["state"] == "proposed"
    assert withdraw(plan["id"], store)["state"] == "withdrawn"


def test_restricted_roles_complete_approval_and_claim(database, tmp_path):
    planner, plan = proposal(tmp_path, "real-roles")
    api = Store(lambda: connect(database, "hpc_ops_api"), os.getuid())
    approver = Store(lambda: connect(database, "hpc_ops_approver"), os.getuid())
    worker = Store(lambda: connect(database, "hpc_ops_executor"), os.getuid())
    api.propose(plan)
    approver.approve(plan["id"], plan["digest"])
    assert worker.claim(plan["id"]) == plan
    with pytest.raises(ValueError):
        worker.claim(plan["id"])


@pytest.mark.parametrize("operation", ["software.install", "database.update"])
def test_owner_maintenance_executes_without_human_approval(database, tmp_path, operation):
    from hpc_agent_os.operations import Recipe

    policy = Policy(
        expected_host="test",
        expected_uid=os.getuid(),
        software_root=str(tmp_path),
        database_root=str(tmp_path),
        allowed_download_hosts=["example.org"],
        maintenance_autoexecute=True,
        recipes={
            "fixture": Recipe(
                operation=operation,
                name="fixture",
                version="v1",
                url="https://example.org/fixture",
                sha256="a" * 64,
                size_bytes=1,
                filename="fixture.sif" if operation == "software.install" else "fixture.fa",
                license="synthetic",
            )
        },
    )
    planner = Planner(tmp_path, policy)
    plan = planner.compile(
        {"operation": operation, "recipe_id": "fixture", "idempotency_key": "owner-" + operation}
    )
    api = Store(lambda: connect(database, "hpc_ops_api"), os.getuid())
    worker = Store(lambda: connect(database, "hpc_ops_executor"), os.getuid())
    api.propose(plan)

    class Runner:
        calls = 0

        def run(self, argv):
            self.calls += 1
            assert argv[0] == "/usr/bin/sbatch"
            return "54321"

    runner = Runner()
    disabled = policy.model_copy(update={"maintenance_autoexecute": False})
    with pytest.raises(ValueError, match="approval"):
        worker.claim(plan["id"], disabled)
    assert execute(plan["id"], planner, worker, runner)["state"] == "submitted"
    with pytest.raises(ValueError):
        execute(plan["id"], planner, worker, runner)
    assert runner.calls == 1
    with connect(database) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM operation_approval WHERE task_id=%s", (plan["id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM operation_audit WHERE task_id=%s AND action='owner_policy_execution_claimed'",
                (plan["id"],),
            ).fetchone()[0]
            == 1
        )


def test_owner_policy_cannot_bypass_job_approval(database, tmp_path):
    planner, old = proposal(tmp_path, "job-no-bypass")
    planner.policy.maintenance_autoexecute = True
    plan = planner.compile(old["request"])
    store = Store(lambda: connect(database), os.getuid())
    store.propose(plan)
    with pytest.raises(ValueError, match="approval"):
        execute(plan["id"], planner, store)
    assert store.get(plan["id"])["state"] == "proposed"
    wrong = Store(lambda: connect(database), os.getuid() + 1)
    with pytest.raises(ValueError):
        wrong.get(plan["id"])


@pytest.mark.parametrize("operation", ["job.submit", "job.cancel"])
def test_owner_jobs_without_approval(database, tmp_path, operation):
    planner, old = proposal(tmp_path, "auto-" + operation)
    planner.policy.jobs_autoexecute = True

    class Identity:
        def run(self, argv):
            return f"6789|{os.getuid()}|2026-10-11T00:00:00\n"

    planner.runner = Identity()
    request = (
        old["request"]
        if operation == "job.submit"
        else {"operation": operation, "job_id": "6789", "idempotency_key": "auto-" + operation}
    )
    plan = planner.compile(request)
    api = Store(lambda: connect(database, "hpc_ops_api"), os.getuid())
    worker = Store(lambda: connect(database, "hpc_ops_executor"), os.getuid())
    api.propose(plan)

    class Runner:
        calls = 0

        def run(self, argv):
            self.calls += 1
            if operation == "job.cancel":
                assert argv == ("/usr/bin/scancel", "--user", str(os.getuid()), "--", "6789")
                return ""
            assert argv[0] == "/usr/bin/sbatch"
            return "6789"

    runner = Runner()
    with connect(database) as conn:
        conn.execute("DELETE FROM operation_gate")
    result = execute(plan["id"], planner, worker, runner)
    assert result["state"] == ("submitted" if operation == "job.submit" else "cancel_requested")
    with pytest.raises(ValueError):
        execute(plan["id"], planner, worker, runner)
    assert runner.calls == 1
    with connect(database) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM operation_approval WHERE task_id=%s", (plan["id"],)
            ).fetchone()[0]
            == 0
        )


def test_owner_cancel_rechecks_identity_before_claim(database, tmp_path):
    planner, _ = proposal(tmp_path, "cancel-identity")
    planner.policy.jobs_autoexecute = True

    class Identity:
        uid = os.getuid()

        def run(self, argv):
            return f"6789|{self.uid}|2026-10-11T00:00:00\n"

    identity = Identity()
    planner.runner = identity
    plan = planner.compile(
        {"operation": "job.cancel", "job_id": "6789", "idempotency_key": "cancel-identity"}
    )
    store = Store(lambda: connect(database), os.getuid())
    store.propose(plan)
    identity.uid += 1
    with connect(database) as conn:
        conn.execute("DELETE FROM operation_gate")
    with pytest.raises(ValueError, match="not owned"):
        execute(plan["id"], planner, store)
    assert store.get(plan["id"])["state"] == "proposed"
