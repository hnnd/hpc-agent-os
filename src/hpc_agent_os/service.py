"""Application authorization, validation, pagination and redacted response envelopes."""

import json
import os
import sys
from datetime import datetime

from pydantic import ValidationError

from .errors import DomainError
from .models import (
    ClusterRequest,
    Error,
    ErrorCode,
    GetJobRequest,
    GetReleaseRequest,
    JobRequest,
    NodeRequest,
    PageRequest,
    Response,
    SearchRequest,
    utcnow,
)

SLURM_TOOLS = {
    "cluster_summary": ClusterRequest,
    "list_nodes": NodeRequest,
    "list_partitions": PageRequest,
    "list_my_jobs": JobRequest,
    "get_my_job": GetJobRequest,
}
REGISTRY_TOOLS = {
    "search_software": SearchRequest,
    "get_software_release": GetReleaseRequest,
    "search_databases": SearchRequest,
    "get_database_release": GetReleaseRequest,
}


class Audit:
    def emit(self, *, uid, tool, request_id, decision):
        try:
            # Deliberately omit arguments, user-supplied strings, results and exception text.
            print(
                json.dumps(
                    {
                        "actor_uid": uid,
                        "action": tool,
                        "request_id": request_id,
                        "decision": decision,
                        "timestamp": utcnow().isoformat(),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
        except OSError:
            raise DomainError(ErrorCode.UNAVAILABLE, "Audit unavailable") from None


class Application:
    def __init__(self, config, slurm=None, registry=None, audit=None, uid=None):
        self.config = config
        self.slurm = slurm
        self.registry = registry
        # The stdio entrypoint never accepts a supplied uid. Injection is for in-process tests.
        self.uid = os.getuid() if uid is None else uid
        self.audit = audit or Audit()

    def call(self, tool: str, arguments: dict) -> dict:
        source = self.slurm.adapter.source if tool in SLURM_TOOLS and self.slurm else "postgresql"
        result = Response(cluster_id=self.config.cluster_id, source=source)
        try:
            model = (SLURM_TOOLS | REGISTRY_TOOLS).get(tool)
            if model is None:
                raise DomainError(ErrorCode.FORBIDDEN, "Tool not allowlisted")
            request = model.model_validate(arguments)
            if request.cluster_id != self.config.cluster_id:
                raise DomainError(ErrorCode.FORBIDDEN, "Cluster not authorized")
            self.audit.emit(
                uid=self.uid, tool=tool, request_id=result.request_id, decision="authorized"
            )
            if tool in SLURM_TOOLS:
                self._slurm(tool, request, result)
            else:
                self._registry(tool, request, result)
        except ValidationError:
            result.error = Error(code=ErrorCode.INVALID_ARGUMENT, message="Invalid tool arguments")
        except DomainError as exc:
            result.error = Error(code=exc.code, message=exc.message)
        try:
            self.audit.emit(
                uid=self.uid,
                tool=tool if tool in SLURM_TOOLS | REGISTRY_TOOLS else "unknown",
                request_id=result.request_id,
                decision=result.error.code if result.error else "success",
            )
        except DomainError as exc:
            result.data = None
            result.error = Error(code=exc.code, message=exc.message)
        return result.model_dump(mode="json")

    def _slurm(self, tool, request, result):
        if self.slurm is None:
            raise DomainError(ErrorCode.UNAVAILABLE, "Slurm adapter unavailable")
        kind = (
            "jobs"
            if tool in ("list_my_jobs", "get_my_job")
            else "partitions"
            if tool == "list_partitions"
            else "nodes"
        )
        snap = self.slurm.get(request.cluster_id, kind, self.uid)
        result.observed_at, result.stale = snap.observed_at, snap.stale
        if snap.error:
            result.error = Error(code=snap.error.code, message=snap.error.message)
        rows = snap.data
        if kind == "jobs":
            rows = [r for r in rows if r["owner_uid"] == self.uid]
        if tool == "cluster_summary":
            result.data = {
                "nodes": len(rows),
                "cpus": sum(r["cpus"] for r in rows),
                "memory_mib": sum(r["memory_mib"] for r in rows),
                "partitions": sorted({p for r in rows for p in r["partitions"]}),
            }
        elif tool == "get_my_job":
            found = [r for r in rows if r["job_id"] == request.job_id]
            if not found:
                # No distinction between missing and unauthorized job ids.
                raise DomainError(ErrorCode.FORBIDDEN, "Job not available to this identity")
            result.data = {k: v for k, v in found[0].items() if k != "owner_uid"}
        else:
            if getattr(request, "state", None):
                rows = [r for r in rows if r["state"] == request.state]
            offset = int(request.cursor or "0")
            result.data = [
                {k: v for k, v in row.items() if k != "owner_uid"}
                for row in rows[offset : offset + request.limit]
            ]
            if offset + request.limit < len(rows):
                result.next_cursor = str(offset + request.limit)

    def _registry(self, tool, request, result):
        if self.registry is None:
            raise DomainError(ErrorCode.UNAVAILABLE, "Registry unavailable")
        kind = "software" if "software" in tool else "database"
        params = request.model_dump(exclude={"cluster_id", "cursor"})
        if isinstance(request, SearchRequest):
            params["offset"] = int(request.cursor or "0")
        rows, cursor = self.registry.search(request.cluster_id, kind, **params)
        result.data = (
            rows[0]
            if isinstance(request, GetReleaseRequest) and rows
            else None
            if isinstance(request, GetReleaseRequest)
            else rows
        )
        result.next_cursor = cursor
        result.observed_at = min(
            (datetime.fromisoformat(r["observed_at"]) for r in rows), default=utcnow()
        )
