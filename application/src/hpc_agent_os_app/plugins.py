"""Versioned, data-only plugins. No imports, subprocesses or expression evaluation."""

from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator
from hpc_agent_os.models import StrictModel, Identifier, Digest
from hpc_agent_os.operations import Resources, digest


class Artifact(StrictModel):
    path: str = Field(min_length=1, max_length=256)
    min_bytes: int = Field(default=1, ge=0, le=1073741824)
    max_bytes: int = Field(default=67108864, ge=1, le=1073741824)
    sha256: Digest | None = None

    @model_validator(mode="after")
    def safe_relative(self):
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or str(path) != self.path
            or any(part in (".", "..") for part in path.parts)
            or self.path in (".", "")
            or "\\" in self.path
            or any(ord(c) < 32 for c in self.path)
            or self.min_bytes > self.max_bytes
        ):
            raise ValueError("Invalid artifact constraint")
        return self


class Verification(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    artifacts: list[Artifact] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique(self):
        if len({a.path for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("Duplicate artifact")
        if sum(a.max_bytes for a in self.artifacts) > 1073741824:
            raise ValueError("Total verification budget exceeds 1 GiB")
        return self


class ResourceReference(StrictModel):
    kind: Literal["software", "database", "input"]
    id: Identifier
    version: Identifier
    sha256: Digest | None = None


class Provenance(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    resources: list[ResourceReference] = Field(default_factory=list, max_length=64)
    evidence: Literal["user_declared"] = "user_declared"


class Template(StrictModel):
    id: Identifier
    description: str = Field(min_length=1, max_length=256)
    adapter: Literal["owned-script-v1"] = "owned-script-v1"
    resources: Resources = Field(default_factory=Resources)
    verification: Verification = Field(default_factory=Verification)


class Rule(StrictModel):
    id: Identifier
    field: Literal["state", "scheduler_state", "exit_code", "reconciliation_required"]
    equals: str | bool
    severity: Literal["info", "warning", "error"]
    explanation: str = Field(min_length=1, max_length=512)
    next_step: str = Field(min_length=1, max_length=512)


class Plugin(StrictModel):
    api_version: Literal["1.0"] = "1.0"
    id: Identifier
    version: Identifier
    templates: list[Template] = Field(default_factory=list, max_length=32)
    diagnostics: list[Rule] = Field(default_factory=list, max_length=64)


class Plugins:
    def __init__(self, paths=()):
        from .files import read_json

        self.templates, self.rules, self.manifests = {}, {}, []
        builtin = Plugin(
            id="core",
            version="1.0",
            templates=[Template(id="owned-script", description="Existing owner inbox script")],
            diagnostics=[
                Rule(
                    id="uncertain",
                    field="state",
                    equals="uncertain",
                    severity="error",
                    explanation="Execution result is uncertain; submission may have occurred.",
                    next_step="Inspect and explicitly reconcile; never automatically resubmit.",
                ),
                Rule(
                    id="oom",
                    field="scheduler_state",
                    equals="OUT_OF_MEMORY",
                    severity="error",
                    explanation="Accounting reports an out-of-memory termination.",
                    next_step="Review memory use and prepare a new bounded plan if appropriate.",
                ),
                Rule(
                    id="timeout",
                    field="scheduler_state",
                    equals="TIMEOUT",
                    severity="error",
                    explanation="Accounting reports a time-limit termination.",
                    next_step="Review workload and requested time; do not automatically retry.",
                ),
                Rule(
                    id="accounting",
                    field="reconciliation_required",
                    equals=True,
                    severity="warning",
                    explanation="Job identity or accounting needs reconciliation.",
                    next_step="Inspect the specific owned job before explicit reconciliation.",
                ),
            ],
        )
        self.register(builtin)
        for path in paths:
            self.register(Plugin.model_validate(read_json(path)))

    def register(self, plugin):
        if any(p["id"] == plugin.id for p in self.manifests):
            raise ValueError("Duplicate plugin ID")
        # Fully validate before mutating registry.
        for items in (plugin.templates, plugin.diagnostics):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Duplicate plugin member ID")
        manifest = {
            "id": plugin.id,
            "version": plugin.version,
            "sha256": digest(plugin.model_dump()),
        }
        for item in plugin.templates:
            self.templates[f"{plugin.id}:{item.id}"] = (item, manifest)
        for item in plugin.diagnostics:
            self.rules[f"{plugin.id}:{item.id}"] = item
        self.manifests.append(manifest)

    def describe(self):
        return {
            "plugins": self.manifests,
            "templates": {k: t.model_dump() for k, (t, _) in self.templates.items()},
            "diagnostics": {k: r.model_dump() for k, r in self.rules.items()},
        }

    def diagnose(self, task):
        result = task.get("result") or {}
        facts = {
            "state": task.get("state"),
            "scheduler_state": task.get("scheduler_state", result.get("scheduler_state")),
            "exit_code": result.get("exit_code"),
            "reconciliation_required": task.get("reconciliation_required"),
        }
        matches = []
        for key, rule in self.rules.items():
            value = facts.get(rule.field)
            if type(value) is type(rule.equals) and value == rule.equals:
                matches.append(
                    {
                        "rule": key,
                        "evidence": {rule.field: value},
                        "severity": rule.severity,
                        "explanation": rule.explanation,
                        "next_step": rule.next_step,
                    }
                )
        return {
            "facts": facts,
            "findings": matches,
            "conclusion": "matched_rules" if matches else "unknown",
            "plugins": self.manifests,
        }
