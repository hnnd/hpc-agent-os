"""Owner job workflows with immutable application bindings and explicit evidence levels."""

import hashlib
import time
from pathlib import Path
from uuid import UUID, uuid4

from hpc_agent_os.operations import OperationRequest, digest
from hpc_agent_os.models import utcnow
from .files import canonical, read_json, write_new, artifact_check
from .plugins import Verification, Provenance
from . import __version__

TERMINAL = {"succeeded", "failed", "canceled", "withdrawn"}


def code_digest():
    return hashlib.sha256(
        b"".join(
            p.name.encode() + p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py"))
        )
    ).hexdigest()


class App:
    def __init__(self, root, backend, plugins):
        self.root, self.backend, self.plugins = root, backend, plugins

    def record_path(self, task_id):
        UUID(task_id)
        return canonical(self.root / ".runtime/application" / f"{task_id}.json")

    def binding(self, task):
        record = read_json(self.record_path(task["plan"]["id"]))
        if record["plan_digest"] != task["plan"]["digest"]:
            raise ValueError("Application binding differs from operation")
        return record

    def plan(self, request, template_id="core:owned-script", verification=None, provenance=None):
        template, plugin = self.plugins.templates[template_id]
        request = dict(request)
        request.setdefault("resources", template.resources.model_dump())
        request = OperationRequest.model_validate(request).model_dump()
        if request["operation"] not in ("job.submit", "job.cancel"):
            raise ValueError("Use the separate maintenance surface")
        spec = verification if verification is not None else template.verification
        if request["operation"] == "job.cancel" and spec.artifacts:
            raise ValueError("Cancellation cannot assert analysis artifacts")
        plan = self.backend.compile(request)
        record = {
            "schema_version": "1.0",
            "application_version": __version__,
            "application_sha256": code_digest(),
            "task_id": plan["id"],
            "plan_digest": plan["digest"],
            "template": template_id,
            "plugin": plugin,
            "verification": spec.model_dump(),
            "provenance": (provenance or Provenance()).model_dump(),
        }
        path = self.record_path(plan["id"])
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A binding is immutable even if the subsequent DB proposal fails.
        try:
            write_new(path, record)
        except FileExistsError:
            if read_json(path) != record:
                raise ValueError("Idempotency key has a different application binding") from None
        return self.backend.propose(plan)

    def execute(self, task_id, operation):
        task = self.backend.get(task_id)
        if task["plan"]["request"]["operation"] != operation:
            raise ValueError("Command and planned operation differ")
        record = self.binding(task)
        if record["application_sha256"] != code_digest():
            raise ValueError("Application code changed; create a new plan")
        _, plugin = self.plugins.templates[record["template"]]
        if plugin != record["plugin"]:
            raise ValueError("Plugin changed; create a new plan")
        return self.backend.execute(task_id)

    def wait(self, task_id, timeout=300, interval=30, clock=time.monotonic, sleep=time.sleep):
        if not 0 <= timeout <= 3600 or not 30 <= interval <= 300:
            raise ValueError("Wait bounds: timeout 0..3600, interval 30..300")
        deadline = clock() + timeout
        task = self.backend.get(task_id)
        while task["state"] not in TERMINAL:
            if task["state"] in ("proposed", "running", "uncertain") or task.get(
                "reconciliation_required"
            ):
                return {"task": task, "stopped": "action_or_reconciliation_required"}
            remaining = deadline - clock()
            if remaining <= 0:
                return {"task": task, "stopped": "timeout"}
            # Never issue a new query inside the 30s shared scheduler gate.
            sleep(min(interval, remaining))
            if clock() >= deadline:
                return {"task": task, "stopped": "timeout"}
            task = self.backend.refresh(task_id)
        return {"task": task, "stopped": "terminal"}

    def verify(self, task_id):
        task = self.backend.get(task_id)
        record = self.binding(task)
        spec = Verification.model_validate(record["verification"])
        work = canonical(self.root / ".runtime/phase4/tasks" / str(UUID(task_id)))
        if str(work) != task["plan"]["work"]:
            raise ValueError("Task workspace mismatch")
        checks = []
        for artifact in spec.artifacts:
            try:
                check = artifact_check(work, artifact)
            except (ValueError, OSError):
                check = {"passed": False, "reason": "missing_unsafe_or_changed"}
            checks.append({"path": artifact.path, **check})
        result = task.get("result") or {}
        program_success = (
            result.get("scheduler_state") == "COMPLETED" and result.get("exit_code") == "0:0"
        )
        artifact_success = all(c["passed"] for c in checks) if checks else None
        outcome = {
            "schema_version": "1.0",
            "task_id": task_id,
            "plan_digest": task["plan"]["digest"],
            "binding_sha256": digest(record),
            "observed_at": utcnow().isoformat(),
            "scheduler_terminal": task["state"] in {"succeeded", "failed", "canceled"},
            "program_success": program_success if task["state"] in TERMINAL else None,
            "artifact_success": artifact_success,
            "verified": task["state"] == "succeeded"
            and program_success
            and artifact_success is True,
            "checks": checks,
            "scope": "declared_artifact_constraints_not_scientific_validity",
        }
        receipt = self.record_path(task_id).parent / f"{task_id}.verification-{uuid4()}.json"
        write_new(receipt, outcome)
        return outcome

    def export(self, task_id, destination):
        destination = canonical(destination)
        if not destination.is_relative_to(self.root):
            raise ValueError("Export must stay inside project")
        task = self.backend.get(task_id)
        record = self.binding(task)
        plan = task["plan"]
        raw = task.get("result") or {}
        # Allowlist fields: no script, absolute paths, argv, logs, credentials or raw data.
        exported = {
            "schema_version": "1.0",
            "task_id": task_id,
            "exported_at": utcnow().isoformat(),
            "state": task["state"],
            "plan_digest": plan["digest"],
            "policy_digest": plan["policy_digest"],
            "executor_code_digest": plan["executor_code_digest"],
            "source_revision": plan["source_revision"],
            "operation": plan["request"]["operation"],
            "resources": plan["request"]["resources"],
            "script_sha256": plan.get("script_sha256"),
            "application": record,
            "result": {
                k: raw[k]
                for k in ("job_id", "scheduler_state", "exit_code", "workload_success")
                if k in raw
            },
            "verification": self.verify(task_id),
            "audit": self.backend.audit(task_id),
        }
        write_new(destination, exported)
        return {
            "exported": True,
            "sha256": digest(exported),
            "digest_encoding": "canonical_json",
            "contains_script_or_sequence": False,
        }
