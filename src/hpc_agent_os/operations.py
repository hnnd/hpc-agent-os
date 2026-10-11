"""Phase 4 single-user operations: immutable proposals, human approvals, bounded executor."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
from typing import Literal
from uuid import UUID, uuid5, NAMESPACE_URL

import psycopg
from psycopg.types.json import Jsonb
from pydantic import Field

from .models import StrictModel, Identifier, Digest
from .process import BoundedRunner


class Resources(StrictModel):
    partition: Literal["cpu", "fat"] = "cpu"
    cpus: int = Field(default=1, ge=1, le=8)
    memory_mib: int = Field(default=1024, ge=256, le=16384)
    minutes: int = Field(default=10, ge=1, le=120)


class OperationRequest(StrictModel):
    operation: Literal["job.submit", "job.cancel", "software.install", "database.update"]
    idempotency_key: Identifier
    script_path: str | None = Field(default=None, max_length=1024)
    script_sha256: Digest | None = None
    job_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,17}$")
    recipe_id: Identifier | None = None
    resources: Resources = Field(default_factory=Resources)


class Recipe(StrictModel):
    operation: Literal["software.install", "database.update"]
    name: Identifier
    version: Identifier
    url: str = Field(max_length=2048)
    sha256: Digest
    size_bytes: int = Field(gt=0, le=100_000_000_000)
    filename: Identifier
    license: str = Field(min_length=1, max_length=128)


class Policy(StrictModel):
    expected_host: str
    expected_uid: int = Field(gt=0)
    software_root: str
    database_root: str
    allowed_download_hosts: list[str]
    recipes: dict[str, Recipe] = Field(default_factory=dict)
    max_pending: int = Field(default=20, ge=1, le=100)
    maintenance_autoexecute: bool = False
    jobs_autoexecute: bool = False


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def checked_path(path, root, exists=True):
    path, root = Path(path), Path(root)
    if not path.is_absolute() or path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Noncanonical or out-of-scope path")
    if exists and not path.exists():
        raise ValueError("Required path does not exist")
    return path


def read_script(path, root):
    path = checked_path(path, root)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        import stat

        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_size > 1024 * 1024
        ):
            raise ValueError("Script must be an owned regular file <=1MiB")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024 or b"\x00" in data:
            raise ValueError("Invalid script")
        return data
    finally:
        os.close(fd)


def job_identity(job_id, uid, runner):
    text = runner.run(("/usr/bin/squeue", "--noheader", "--jobs", job_id, "--format=%i|%U|%V"))
    rows = [row.strip().split("|") for row in text.splitlines() if row.strip()]
    if len(rows) != 1 or len(rows[0]) != 3 or rows[0][0] != job_id or rows[0][1] != str(uid):
        raise ValueError("Job absent, unsupported or not owned by current identity")
    return rows[0]


class Planner:
    def __init__(self, root, policy, runner=None):
        self.root, self.policy = Path(root), policy
        self.state = self.root / ".runtime/phase4"
        self.runner = runner or BoundedRunner(timeout=10)

    def compile(self, request):
        request = OperationRequest.model_validate(request)
        p = self.policy
        if os.getuid() != p.expected_uid:
            raise ValueError("Operation identity differs from configured owner")
        task_id = str(uuid5(NAMESPACE_URL, f"hpc:{p.expected_uid}:{request.idempotency_key}"))
        work = self.state / "tasks" / task_id
        source = Path(__file__).resolve().parent
        code_digest = hashlib.sha256(
            b"".join(
                (source / name).read_bytes()
                for name in (
                    "operations.py",
                    "operations_cli.py",
                    "publish.py",
                    "process.py",
                    "models.py",
                )
            )
        ).hexdigest()
        revision = (
            BoundedRunner(timeout=3)
            .run(("/usr/bin/git", "-C", str(self.root), "rev-parse", "--verify", "HEAD"))
            .strip()
            if (self.root / ".git").exists()
            else "unversioned-test"
        )
        common = {
            "executor_code_digest": code_digest,
            "source_revision": revision,
            "id": task_id,
            "uid": p.expected_uid,
            "request": request.model_dump(),
            "policy_digest": digest(p.model_dump()),
            "work": str(work),
            "blockers": [],
        }
        if request.operation == "job.cancel":
            if (
                not request.job_id
                or request.script_path
                or request.script_sha256
                or request.recipe_id
            ):
                raise ValueError("Cancellation requires only job_id")
            common.update(
                argv=["/usr/bin/scancel", "--user", str(p.expected_uid), "--", request.job_id],
                job_identity=job_identity(request.job_id, p.expected_uid, self.runner),
                impact="Terminates exactly this owned job; partial output may remain",
                rollback="Cancellation is irreversible; resubmission requires a new approved proposal",
                acceptance="scancel acceptance only; confirm terminal state independently",
            )
        else:
            if request.job_id:
                raise ValueError("job_id is cancellation-only")
            if request.operation == "job.submit":
                if not request.script_path or not request.script_sha256 or request.recipe_id:
                    raise ValueError("Submission requires an enrolled script path and digest")
                content = read_script(request.script_path, self.state / "inbox")
                if hashlib.sha256(content).hexdigest() != request.script_sha256:
                    raise ValueError("Script digest mismatch")
                common["script_sha256"] = request.script_sha256
                executable = "/bin/bash " + shlex.quote(str(work / "input.sh"))
            else:
                if request.script_path or request.script_sha256 or not request.recipe_id:
                    raise ValueError("Software/database operation requires a recipe id only")
                recipe = p.recipes.get(request.recipe_id)
                if recipe is None or recipe.operation != request.operation:
                    raise ValueError(
                        "Recipe not enrolled; specify version/source/checksum/license first"
                    )
                from urllib.parse import urlsplit

                url = urlsplit(recipe.url)
                if (
                    url.scheme != "https"
                    or url.hostname not in p.allowed_download_hosts
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                    or url.port not in (None, 443)
                ):
                    raise ValueError("Recipe URL is not approved credential-free HTTPS")
                root = Path(
                    p.software_root if request.operation == "software.install" else p.database_root
                )
                checked_path(root, root)
                if not root.is_dir():
                    raise ValueError("Approved release root is absent; no escalation")
                target = checked_path(root / recipe.name / recipe.version, root, exists=False)
                common.update(recipe=recipe.model_dump(), target=str(target))
                executable = (
                    "/usr/bin/env -i PATH=/usr/bin:/bin PYTHONPATH="
                    + shlex.quote(str(Path(__file__).resolve().parent.parent))
                    + " "
                    + shlex.quote(str(self.root / ".venv/bin/python"))
                    + " -m hpc_agent_os.publish "
                    + shlex.quote(str(work / "plan.json"))
                )
            r = request.resources
            common.update(
                argv=[
                    "/usr/bin/sbatch",
                    "--parsable",
                    "--export=NIL",
                    "--nodes=1",
                    "--ntasks=1",
                    f"--cpus-per-task={r.cpus}",
                    f"--mem={r.memory_mib}M",
                    f"--time={r.minutes}",
                    "--partition=" + r.partition,
                    "--job-name=hpcop-" + task_id[:12],
                    "--chdir=" + str(work),
                    "--output=" + str(work / "slurm-%j.out"),
                    "--error=" + str(work / "slurm-%j.err"),
                    "--wrap=exec " + executable,
                ],
                impact="Consumes explicitly bounded Slurm allocation; approved script/recipe effects apply",
                rollback="Cancel the returned owned job via a separate approved request; retain outputs and old versions",
                acceptance="Job submission is not workload/install success; verify final result and artifact digest",
            )
        common["digest"] = digest(common)
        return common


DDL = """
CREATE TABLE IF NOT EXISTS operation_task (
 id uuid PRIMARY KEY, uid bigint NOT NULL, idem text NOT NULL, digest text NOT NULL,
 plan jsonb NOT NULL, state text NOT NULL DEFAULT 'proposed', result jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(uid,idem));
CREATE TABLE IF NOT EXISTS operation_approval (
 task_id uuid PRIMARY KEY REFERENCES operation_task(id), digest text NOT NULL,
 approver bigint NOT NULL, expires_at timestamptz NOT NULL, consumed_at timestamptz);
CREATE TABLE IF NOT EXISTS operation_gate (uid bigint PRIMARY KEY, next_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS operation_audit (
 id bigserial PRIMARY KEY, task_id uuid NOT NULL, actor bigint NOT NULL,
 action text NOT NULL, at timestamptz NOT NULL DEFAULT now());
"""


class Store:
    def __init__(self, connect, uid):
        self.connect, self.uid = connect, uid

    def reserve_scheduler_read(self):
        with self.connect() as conn:
            row = conn.execute(
                "INSERT INTO operation_gate(uid,next_at) VALUES(%s,now()+interval '30 seconds') ON CONFLICT(uid) DO UPDATE SET next_at=EXCLUDED.next_at WHERE operation_gate.next_at<=now() RETURNING uid",
                (self.uid,),
            ).fetchone()
            if not row:
                raise ValueError("Scheduler read gate: wait at least 30 seconds")

    def get(self, task_id):
        UUID(task_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT plan,state,result FROM operation_task WHERE id=%s AND uid=%s",
                (task_id, self.uid),
            ).fetchone()
            if not row:
                raise ValueError("Operation not available to this identity")
            return {"plan": row[0], "state": row[1], "result": row[2]}

    def propose(self, plan, maximum=20):
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (self.uid,))
            old = conn.execute(
                "SELECT digest FROM operation_task WHERE id=%s", (plan["id"],)
            ).fetchone()
            if old:
                if old[0] != plan["digest"]:
                    raise ValueError("Idempotency key conflicts with another proposal")
            else:
                count = conn.execute(
                    "SELECT count(*) FROM operation_task WHERE uid=%s AND state IN ('proposed','running','submitted')",
                    (self.uid,),
                ).fetchone()[0]
                if count >= maximum and plan["request"]["operation"] != "job.cancel":
                    raise ValueError("Pending operation budget reached")
                conn.execute(
                    "INSERT INTO operation_task(id,uid,idem,digest,plan) VALUES(%s,%s,%s,%s,%s)",
                    (
                        plan["id"],
                        self.uid,
                        plan["request"]["idempotency_key"],
                        plan["digest"],
                        Jsonb(plan),
                    ),
                )
                conn.execute(
                    "INSERT INTO operation_audit(task_id,actor,action) VALUES(%s,%s,'proposed')",
                    (plan["id"], self.uid),
                )
        return self.get(plan["id"])

    def approve(self, task_id, expected_digest):
        # Human terminal only. No MCP tool exposes this method.
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (self.uid,))
            row = conn.execute(
                "SELECT digest,state FROM operation_task WHERE id=%s AND uid=%s",
                (task_id, self.uid),
            ).fetchone()
            if not row or row != (expected_digest, "proposed"):
                raise ValueError("Approval must match an unexecuted immutable proposal")
            conn.execute(
                "INSERT INTO operation_approval(task_id,digest,approver,expires_at) VALUES(%s,%s,%s,now()+interval '1 hour') ON CONFLICT(task_id) DO UPDATE SET expires_at=EXCLUDED.expires_at, approver=EXCLUDED.approver WHERE operation_approval.consumed_at IS NULL",
                (task_id, expected_digest, self.uid),
            )
            conn.execute(
                "INSERT INTO operation_audit(task_id,actor,action) VALUES(%s,%s,'human_approved')",
                (task_id, self.uid),
            )

    def claim(self, task_id, policy=None):
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (self.uid,))
            candidate = conn.execute(
                "SELECT plan FROM operation_task WHERE id=%s AND uid=%s AND state='proposed' FOR UPDATE",
                (task_id, self.uid),
            ).fetchone()
            if (
                candidate
                and policy is not None
                and self.uid == os.getuid() == policy.expected_uid == candidate[0]["uid"]
                and candidate[0]["policy_digest"] == digest(policy.model_dump())
                and (
                    (
                        policy.maintenance_autoexecute
                        and candidate[0]["request"]["operation"]
                        in ("software.install", "database.update")
                    )
                    or (
                        policy.jobs_autoexecute
                        and candidate[0]["request"]["operation"] in ("job.submit", "job.cancel")
                    )
                )
            ):
                conn.execute(
                    "UPDATE operation_task SET state='running',updated_at=now() WHERE id=%s",
                    (task_id,),
                )
                conn.execute(
                    "INSERT INTO operation_audit(task_id,actor,action) VALUES(%s,%s,'owner_policy_execution_claimed')",
                    (task_id, self.uid),
                )
                return candidate[0]
            row = conn.execute(
                "SELECT t.plan FROM operation_task t JOIN operation_approval a ON a.task_id=t.id WHERE t.id=%s AND t.uid=%s AND t.state='proposed' AND a.digest=t.digest AND a.expires_at>now() AND a.consumed_at IS NULL FOR UPDATE OF t,a",
                (task_id, self.uid),
            ).fetchone()
            if not row:
                raise ValueError(
                    "Valid unused approval required; never automatically retry uncertain execution"
                )
            conn.execute(
                "UPDATE operation_approval SET consumed_at=now() WHERE task_id=%s", (task_id,)
            )
            conn.execute(
                "UPDATE operation_task SET state='running',updated_at=now() WHERE id=%s", (task_id,)
            )
            conn.execute(
                "INSERT INTO operation_audit(task_id,actor,action) VALUES(%s,%s,'execution_claimed')",
                (task_id, self.uid),
            )
            return row[0]

    def finish(self, task_id, state, result):
        with self.connect() as conn:
            conn.execute(
                "UPDATE operation_task SET state=%s,result=%s,updated_at=now() WHERE id=%s AND uid=%s",
                (state, Jsonb(result), task_id, self.uid),
            )
            conn.execute(
                "INSERT INTO operation_audit(task_id,actor,action) VALUES(%s,%s,%s)",
                (task_id, self.uid, state),
            )


def execute(task_id, planner, store, runner=None):
    runner = runner or BoundedRunner(timeout=10)
    plan = store.get(task_id)["plan"]
    if plan["request"]["operation"] == "job.cancel":
        store.reserve_scheduler_read()
    # Recompile before consuming approval: binds recipe, paths, script, resource budget and policy.
    if planner.compile(plan["request"]) != plan:
        raise ValueError("Proposal drift; create a new proposal")
    if store.uid != os.getuid() or store.uid != planner.policy.expected_uid:
        raise ValueError("Executor identity differs from configured owner")
    plan = store.claim(task_id, planner.policy)
    try:
        work = checked_path(plan["work"], planner.state / "tasks", exists=False)
        work.mkdir(parents=True, mode=0o700, exist_ok=True)
        frozen = work / "plan.json"
        encoded = json.dumps(plan, sort_keys=True).encode()
        if frozen.exists():
            if frozen.read_bytes() != encoded:
                raise ValueError("Existing task manifest differs")
        else:
            with frozen.open("xb") as stream:
                stream.write(encoded)
        if plan["request"]["operation"] == "job.submit":
            content = read_script(plan["request"]["script_path"], planner.state / "inbox")
            if hashlib.sha256(content).hexdigest() != plan["script_sha256"]:
                raise ValueError("Script changed after approval")
            with (work / "input.sh").open("xb") as stream:
                stream.write(content)
        # Any uncertainty after the durable claim remains non-retryable.
        output = runner.run(tuple(plan["argv"])).strip()
        if plan["request"]["operation"] == "job.cancel":
            result, state = (
                {"cancel_request_accepted": True, "job_id": plan["request"]["job_id"]},
                "cancel_requested",
            )
        else:
            if not re.fullmatch(r"[1-9][0-9]{0,17}(?:;[A-Za-z0-9_.-]+)?", output):
                raise ValueError("Ambiguous sbatch response")
            result, state = {"job_id": output.split(";")[0], "workload_success": None}, "submitted"
        store.finish(task_id, state, result)
        return store.get(task_id)
    except Exception:
        store.finish(
            task_id,
            "uncertain",
            {
                "message": "Execution interrupted or result ambiguous; inspect scheduler before any new submission"
            },
        )
        raise ValueError("Execution uncertain; automatic retry prohibited") from None


def load(root):
    root = Path(root)
    policy = Policy.model_validate_json((root / "config/phase4-policy.json").read_text())
    if (
        policy.expected_uid != os.getuid()
        or policy.expected_host != socket.gethostname().split(".")[0]
    ):
        raise ValueError("Phase 4 identity mismatch")
    return policy


def connection(root, role):
    if role not in ("hpc_ops_api", "hpc_ops_approver", "hpc_ops_executor"):
        raise ValueError("Role not allowlisted")
    root = Path(root)
    path = checked_path(root / ".runtime/phase3/socket", root / ".runtime")
    if any(k.startswith("PG") for k in os.environ):
        raise ValueError("PG environment overrides prohibited")
    return psycopg.connect(
        host=str(path),
        dbname="hpc_operations",
        user=role,
        connect_timeout=3,
        options="-c statement_timeout=5000",
        passfile=str(root / ".runtime/disabled-passfile"),
    )


def withdraw(task_id, store):
    UUID(task_id)
    with store.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (store.uid,))
        row = conn.execute(
            "UPDATE operation_task SET state='withdrawn',updated_at=now() WHERE id=%s AND uid=%s AND state='proposed' RETURNING id",
            (task_id, store.uid),
        ).fetchone()
        if not row:
            raise ValueError("Only unexecuted proposals may be withdrawn")
        conn.execute(
            "INSERT INTO operation_audit(task_id,actor,action) VALUES(%s,%s,'withdrawn')",
            (task_id, store.uid),
        )
    return store.get(task_id)


def refresh(task_id, store, runner=None, reconcile_job_id=None):
    task = store.get(task_id)
    if task["state"] not in ("submitted", "running", "uncertain", "cancel_requested"):
        return task
    if task["state"] == "running" and not reconcile_job_id:
        # Cannot know whether a previous executor is still alive. Never reclaim it here.
        return {
            **task,
            "reconciliation_required": True,
            "message": "Executor in flight or interrupted; inspect before explicit reconciliation",
        }
    plan = task["plan"]
    job_id = reconcile_job_id or (task["result"] or {}).get("job_id")
    if not job_id or not re.fullmatch(r"[1-9][0-9]{0,17}", job_id):
        return {**task, "reconciliation_required": True}
    store.reserve_scheduler_read()
    output = (runner or BoundedRunner(timeout=10)).run(
        (
            "/usr/bin/sacct",
            "--noheader",
            "--parsable2",
            "--jobs",
            job_id,
            "--format=JobIDRaw,UID,JobName%64,State,ExitCode",
        )
    )
    rows = [line.strip().split("|") for line in output.splitlines() if line.strip()]
    rows = [row for row in rows if len(row) == 5 and row[0] == job_id and row[1] == str(store.uid)]
    if len(rows) != 1:
        return {
            **task,
            "reconciliation_required": True,
            "message": "Accounting record unavailable; do not resubmit automatically",
        }
    row = rows[0]
    if plan["request"]["operation"] != "job.cancel" and row[2] != "hpcop-" + plan["id"][:12]:
        raise ValueError("Job correlation mismatch")
    state = row[3].split()[0]
    final = {
        "COMPLETED",
        "CANCELLED",
        "FAILED",
        "TIMEOUT",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "BOOT_FAIL",
        "DEADLINE",
        "REVOKED",
    }
    if state not in final:
        if reconcile_job_id and task["state"] in ("running", "uncertain"):
            store.finish(task_id, "submitted", {"job_id": job_id, "workload_success": None})
        return {**store.get(task_id), "scheduler_state": state}
    success = state == "COMPLETED" and row[4] == "0:0"
    result = {
        "job_id": job_id,
        "scheduler_state": state,
        "exit_code": row[4],
        "workload_success": success,
    }
    if plan["request"]["operation"] in ("software.install", "database.update"):
        path = Path(plan["work"]) / "result.json"
        if not path.is_file():
            success = False
        else:
            receipt = json.loads(path.read_text())
            success = (
                success
                and receipt.get("plan_digest") == plan["digest"]
                and receipt.get("result", {}).get("published") is True
            )
            if success:
                manifest = Path(plan["target"]) / "manifest.json"
                success = manifest.is_file() and json.loads(manifest.read_text()).get(
                    "recipe_digest"
                ) == digest(plan["recipe"])
        result["published"] = success
    store.finish(
        task_id,
        "succeeded" if success else "canceled" if state == "CANCELLED" else "failed",
        result,
    )
    return store.get(task_id)
