import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from hpc_agent_os.operations import Planner, Policy
from hpc_agent_os_app.application import App
from hpc_agent_os_app.backend import Backend
from hpc_agent_os_app.cli import main
from hpc_agent_os_app.files import read_json
from hpc_agent_os_app.plugins import Artifact, Plugin, Plugins, Verification


class FakeBackend:
    """Only uses the real planner on a synthetic owned script; never runs Slurm."""

    def __init__(self, root):
        self.root = root
        self.policy = Policy(
            expected_host="fixture",
            expected_uid=os.getuid(),
            software_root=str(root),
            database_root=str(root),
            allowed_download_hosts=[],
            jobs_autoexecute=True,
        )
        self.planner = Planner(root, self.policy)
        self.tasks = {}
        self.executions = 0
        self.refreshes = 0

    def compile(self, request):
        return self.planner.compile(request)

    def propose(self, plan):
        self.tasks.setdefault(plan["id"], {"plan": plan, "state": "proposed", "result": None})
        return self.tasks[plan["id"]]

    def get(self, task_id):
        return self.tasks[task_id]

    def execute(self, task_id):
        self.executions += 1
        task = self.get(task_id)
        task.update(state="submitted", result={"job_id": "123"})
        return task

    def refresh(self, task_id):
        self.refreshes += 1
        return self.get(task_id)

    def audit(self, task_id):
        return {"status": "unavailable", "events": None}


@pytest.fixture
def setup(tmp_path):
    root = tmp_path.resolve()
    inbox = root / ".runtime/phase4/inbox"
    inbox.mkdir(parents=True)
    script = inbox / "test.sh"
    script.write_text("#!/bin/bash\necho synthetic-only\n")
    request = {
        "operation": "job.submit",
        "idempotency_key": "test",
        "script_path": str(script),
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    }
    backend = FakeBackend(root)
    app = App(root, backend, Plugins())
    return app, backend, request


def test_immutable_plan_and_execution_binding(setup):
    app, backend, request = setup
    task = app.plan(request)
    assert app.plan(request) == task
    with pytest.raises(ValueError, match="binding"):
        app.plan(request, verification=Verification(artifacts=[Artifact(path="report.tsv")]))
    with pytest.raises(ValueError, match="operation"):
        app.execute(task["plan"]["id"], "job.cancel")
    assert backend.executions == 0
    app.execute(task["plan"]["id"], "job.submit")
    assert backend.executions == 1


def test_code_drift_blocks_execution(setup, monkeypatch):
    app, backend, request = setup
    task = app.plan(request)
    monkeypatch.setattr("hpc_agent_os_app.application.code_digest", lambda: "drift")
    with pytest.raises(ValueError, match="code changed"):
        app.execute(task["plan"]["id"], "job.submit")
    assert backend.executions == 0


def test_plugin_drift_blocks_execution(setup):
    app, backend, request = setup
    task = app.plan(request)
    template, manifest = app.plugins.templates["core:owned-script"]
    app.plugins.templates["core:owned-script"] = (template, {**manifest, "version": "2"})
    with pytest.raises(ValueError, match="Plugin changed"):
        app.execute(task["plan"]["id"], "job.submit")
    assert backend.executions == 0


@pytest.mark.parametrize(
    "path", ["../outside", "/absolute", "a/../../b", "a//b", ".", "a/./b", "a\\b"]
)
def test_artifact_escape_rejected(path):
    with pytest.raises(ValidationError):
        Artifact(path=path)


def test_plugin_rejects_code_and_duplicates(tmp_path):
    with pytest.raises(ValidationError):
        Plugin.model_validate({"id": "x", "version": "1", "command": "evil"})
    registry = Plugins()
    with pytest.raises(ValueError, match="Duplicate"):
        registry.register(Plugin(id="core", version="2"))
    assert len(registry.manifests) == 1


def test_no_artifacts_does_not_mean_verified(setup):
    app, _, request = setup
    task = app.plan(request)
    task.update(state="succeeded", result={"scheduler_state": "COMPLETED", "exit_code": "0:0"})
    result = app.verify(task["plan"]["id"])
    assert result["artifact_success"] is None and result["verified"] is False


def test_verify_hash_missing_and_failed_job(setup):
    app, _, request = setup
    content = b"synthetic report\n"
    spec = Verification(
        artifacts=[Artifact(path="report.tsv", sha256=hashlib.sha256(content).hexdigest())]
    )
    task = app.plan(request, verification=spec)
    task.update(state="succeeded", result={"scheduler_state": "COMPLETED", "exit_code": "0:0"})
    tid = task["plan"]["id"]
    assert not app.verify(tid)["verified"]
    work = Path(task["plan"]["work"])
    work.mkdir(parents=True)
    (work / "report.tsv").write_bytes(content)
    assert app.verify(tid)["verified"]
    task.update(state="failed", result={"scheduler_state": "FAILED", "exit_code": "1:0"})
    assert not app.verify(tid)["verified"]
    task.update(state="succeeded", result={"scheduler_state": "COMPLETED", "exit_code": "0:0"})
    (work / "report.tsv").write_bytes(b"tampered")
    assert not app.verify(tid)["verified"]


def test_symlink_artifact_not_followed(setup):
    app, _, request = setup
    task = app.plan(request, verification=Verification(artifacts=[Artifact(path="report.tsv")]))
    work = Path(task["plan"]["work"])
    work.mkdir(parents=True)
    target = app.root / "private"
    target.write_text("not-for-export")
    (work / "report.tsv").symlink_to(target)
    check = app.verify(task["plan"]["id"])["checks"][0]
    assert check["passed"] is False and "sha256" not in check


def test_wait_is_bounded_and_does_not_retry_uncertain(setup):
    app, backend, request = setup
    task = app.plan(request)
    tid = task["plan"]["id"]
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    task["state"] = "submitted"
    result = app.wait(tid, timeout=61, interval=30, clock=lambda: now[0], sleep=sleep)
    assert result["stopped"] == "timeout" and backend.refreshes == 2 and now[0] == 61
    task["state"] = "uncertain"
    assert app.wait(tid)["stopped"] == "action_or_reconciliation_required"
    assert backend.executions == 0


def test_diagnostics_are_evidence_bound():
    plugins = Plugins()
    assert plugins.diagnose({"state": "succeeded"})["conclusion"] == "unknown"
    found = plugins.diagnose({"state": "failed", "result": {"scheduler_state": "OUT_OF_MEMORY"}})
    assert found["findings"][0]["rule"] == "core:oom"
    assert plugins.diagnose({"reconciliation_required": "True"})["findings"] == []


def test_export_redacts_paths_and_no_overwrite(setup):
    app, _, request = setup
    task = app.plan(request)
    output = app.root / "export.json"
    result = app.export(task["plan"]["id"], output)
    encoded = output.read_text()
    assert result["exported"] and str(app.root) not in encoded
    assert "synthetic-only" not in encoded and "argv" not in encoded
    assert read_json(output)["audit"]["events"] is None
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        app.export(task["plan"]["id"], output)


def test_mock_rejects_operations(tmp_path):
    with pytest.raises(ValueError, match="explicit live"):
        Backend(tmp_path, "mock").components()


def test_cli_redacts_invalid_input(capsys):
    assert main(["--json", "--secret-marker"]) == 2
    assert "secret-marker" not in capsys.readouterr().out


def test_cli_plugins_and_status(capsys):
    assert main(["--json", "plugins"]) == 0
    assert json.loads(capsys.readouterr().out)["synthetic"]
    assert main(["--json", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["source"] == "synthetic-slurm"


def test_schema_exports_match():
    from hpc_agent_os_app.plugins import Provenance

    root = Path(__file__).resolve().parents[1]
    for name, model in (
        ("plugin", Plugin),
        ("verification", Verification),
        ("provenance", Provenance),
    ):
        assert (
            json.loads((root / "schemas" / f"{name}.schema.json").read_text())
            == model.model_json_schema()
        )
    Plugins([root / "plugins/example.json"])


def test_interrupt_never_claims_jobs_unchanged(monkeypatch, capsys):
    def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr("hpc_agent_os_app.cli.dispatch", interrupted)
    assert main(["--json", "plugins"]) == 130
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "unchanged" not in message
    assert "never automatically retry" in message
