"""Separate human approval/execution CLI and proposal-only MCP surface."""

import argparse
import json
import os
from pathlib import Path
from typing import Any
import time

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .operations import (
    OperationRequest,
    Planner,
    Store,
    connection,
    execute,
    load,
    refresh,
    withdraw,
)


def check_host_preflight(plan, evidence):
    """Validate short-lived host evidence without relaxing the MCP read-only mount."""
    root = Path(plan["target"]).parent.parent
    entry = evidence.get("roots", {}).get(str(root))
    age = time.time() - evidence.get("observed_at", 0)
    if not 0 <= age <= 300 or evidence.get("uid") != os.getuid() or not entry:
        raise ValueError("Restart operations connection for fresh host preflight")
    info = root.stat()
    identity = [info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_ctime_ns]
    if not entry["writable"] or entry["identity"] != identity or root.resolve() != root:
        raise ValueError("Host target unavailable or permissions changed")


def build_operations_server(planner, store, executor_store=None, host_evidence=None):
    server = MCPServer(name="hpc-operations", version="0.4.4", log_level="ERROR")
    write = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    read = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )

    @server.tool(annotations=write, structured_output=True)
    def prepare_operation(request: OperationRequest) -> dict[str, Any]:
        """Create an immutable operation plan. Owner job and maintenance execution follow their separate configured policies."""
        try:
            if request.operation == "job.cancel":
                store.reserve_scheduler_read()
            return store.propose(planner.compile(request.model_dump()), planner.policy.max_pending)
        except Exception:
            # No scripts, credentials, external errors or SQL text enter model output.
            return {
                "error": "PROPOSAL_REJECTED",
                "next_step": "Check required fields, enrolled recipe, script digest, ownership, policy and writable target; no operation executed",
            }

    @server.tool(annotations=read, structured_output=True)
    def get_operation(task_id: str) -> dict[str, Any]:
        """Read own persisted proposal, approval execution state and result; submitted does not mean completed."""
        try:
            return store.get(task_id)
        except Exception:
            return {"error": "OPERATION_UNAVAILABLE"}

    @server.tool(annotations=read, structured_output=True)
    def list_operation_recipes() -> dict[str, Any]:
        """List reviewed software/database recipes. Empty means enrollment is still required."""
        return {
            "recipes": {key: value.model_dump() for key, value in planner.policy.recipes.items()},
            "job_script_inbox": str(planner.state / "inbox"),
            "supported_operations": [
                "job.submit",
                "job.cancel",
                "software.install",
                "database.update",
            ],
            "approval": {
                "jobs": "Owner policy; no human approval"
                if planner.policy.jobs_autoexecute
                else "Separate human terminal approval required",
                "maintenance": "Owner policy; no human approval"
                if planner.policy.maintenance_autoexecute
                else "Separate human terminal approval required",
            },
        }

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    def execute_maintenance(task_id: str) -> dict[str, Any]:
        """Execute own enrolled software/database plan without human approval when owner policy allows. Never executes job submit/cancel plans. Do not retry uncertain execution."""
        try:
            if not planner.policy.maintenance_autoexecute or executor_store is None:
                raise ValueError("Owner maintenance execution disabled")
            task = store.get(task_id)
            if task["plan"]["request"]["operation"] not in (
                "software.install",
                "database.update",
            ):
                raise ValueError("Maintenance operations only")
            check_host_preflight(task["plan"], host_evidence or {})
            return execute(task_id, planner, executor_store)
        except Exception:
            return {
                "error": "MAINTENANCE_NOT_EXECUTED_OR_UNCERTAIN",
                "next_step": "Read get_operation before further action; never automatically retry. Check owner policy, recipe, drift and target permissions.",
            }

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    def execute_my_job(task_id: str) -> dict[str, Any]:
        """Submit or cancel only this OS user's job plan without human approval under owner policy. Cancellation is irreversible. Never executes maintenance; never retry uncertain execution."""
        try:
            if not planner.policy.jobs_autoexecute or executor_store is None:
                raise ValueError("Owner job execution disabled")
            task = store.get(task_id)
            if task["plan"]["request"]["operation"] not in ("job.submit", "job.cancel"):
                raise ValueError("Own job operations only")
            return execute(task_id, planner, executor_store)
        except Exception:
            return {
                "error": "JOB_NOT_EXECUTED_OR_UNCERTAIN",
                "next_step": "Read get_operation before further action; never retry uncertain execution. Check ownership, script digest, policy and drift. Cancellation recheck shares the 30-second scheduler read gate.",
            }

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "mcp",
            "propose",
            "show",
            "approve",
            "execute",
            "refresh",
            "withdraw",
            "reconcile",
        ],
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--digest")
    parser.add_argument("--job-id")
    args = parser.parse_args()
    policy = load(args.root)
    role = (
        "hpc_ops_api"
        if args.action in ("mcp", "propose", "show")
        else "hpc_ops_approver"
        if args.action == "approve"
        else "hpc_ops_executor"
    )
    store = Store(lambda: connection(args.root, role), os.getuid())
    planner = Planner(args.root, policy)
    if args.action == "mcp":
        executor_store = Store(lambda: connection(args.root, "hpc_ops_executor"), os.getuid())
        evidence = json.loads(os.environ.get("HPC_MAINTENANCE_PREFLIGHT", "{}"))
        build_operations_server(planner, store, executor_store, evidence).run()
        return
    if args.action == "propose":
        request = json.loads(args.request.read_text())
        if request.get("operation") == "job.cancel":
            store.reserve_scheduler_read()
        result = store.propose(
            planner.compile(json.loads(args.request.read_text())), policy.max_pending
        )
    elif args.action == "approve":
        store.approve(args.task_id, args.digest)
        result = {"approved": args.task_id, "expires_in_seconds": 3600}
    elif args.action == "execute":
        result = execute(args.task_id, planner, store)
    elif args.action in ("refresh", "reconcile"):
        result = refresh(args.task_id, store, reconcile_job_id=args.job_id)
    elif args.action == "withdraw":
        result = withdraw(args.task_id, store)
    else:
        result = store.get(args.task_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
