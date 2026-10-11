"""Unified application CLI; defaults to mock and requires explicit live selection."""

import argparse
import json
from pathlib import Path
import sys

from .application import App
from .backend import Backend
from .files import canonical, read_json
from .plugins import Plugins, Verification, Provenance


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Never echo user arguments (which may accidentally contain private data).
        raise ValueError("Invalid command arguments; use --help")


def parser():
    p = Parser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--mode", choices=("mock", "live"), default="mock")
    p.add_argument("--json", action="store_true")
    p.add_argument("--plugin", type=Path, action="append", default=[])
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("status")
    commands.add_parser("plugins")
    for kind in ("software", "database"):
        group = commands.add_parser(kind).add_subparsers(dest="action", required=True)
        search = group.add_parser("search")
        search.add_argument("query", nargs="?", default="")
        search.add_argument("--limit", type=int, default=100)
        search.add_argument("--cursor")
        search.add_argument("--version")
        search.add_argument("--platform")
        group.add_parser("show").add_argument("release_id")
    commands.add_parser("recipe").add_subparsers(dest="action", required=True).add_parser("list")
    jobs = commands.add_parser("job").add_subparsers(dest="action", required=True)
    plan = jobs.add_parser("plan")
    plan.add_argument("--request", type=Path, required=True)
    plan.add_argument("--template", default="core:owned-script")
    plan.add_argument("--verification", type=Path)
    plan.add_argument("--provenance", type=Path)
    for name in ("submit", "cancel", "status", "refresh", "wait", "explain", "reconcile"):
        cmd = jobs.add_parser(name)
        cmd.add_argument("task_id")
        if name == "wait":
            cmd.add_argument("--timeout", type=int, default=300)
            cmd.add_argument("--interval", type=int, default=30)
        if name == "reconcile":
            cmd.add_argument("--job-id", required=True)
    runs = commands.add_parser("run").add_subparsers(dest="action", required=True)
    runs.add_parser("verify").add_argument("task_id")
    export = runs.add_parser("export")
    export.add_argument("task_id")
    export.add_argument("--output", type=Path, required=True)
    return p


def dispatch(args, backend=None):
    root = canonical(args.root)
    backend = backend or Backend(root, args.mode)
    plugins = Plugins(args.plugin)
    app = App(root, backend, plugins)
    command = args.command
    if command == "plugins":
        return plugins.describe(), 0
    if command == "doctor":
        result = backend.doctor()
        return result, 0 if result["healthy"] else 3
    if command == "status":
        result = backend.cluster()
    elif command in ("software", "database"):
        params = (
            {"release_id": args.release_id}
            if args.action == "show"
            else {k: getattr(args, k) for k in ("query", "limit", "cursor", "version", "platform")}
        )
        result = backend.resource(command, args.action, params)
    elif command == "recipe":
        result = backend.recipes()
    elif command == "job":
        action = args.action
        if action == "plan":
            verification = (
                Verification.model_validate(read_json(args.verification))
                if args.verification
                else None
            )
            provenance = (
                Provenance.model_validate(read_json(args.provenance)) if args.provenance else None
            )
            result = app.plan(read_json(args.request), args.template, verification, provenance)
        elif action in ("submit", "cancel"):
            result = app.execute(args.task_id, "job." + action)
        elif action == "wait":
            result = app.wait(args.task_id, args.timeout, args.interval)
            task = result["task"]
            return result, (
                0
                if task["state"] == "succeeded"
                else 5
                if task["state"] in ("failed", "canceled", "withdrawn")
                else 4
            )
        elif action == "status":
            result = backend.get(args.task_id)
        elif action == "explain":
            result = plugins.diagnose(backend.get(args.task_id))
        else:
            result = backend.refresh(args.task_id, args.job_id if action == "reconcile" else None)
    else:
        if args.action == "verify":
            result = app.verify(args.task_id)
            return result, 0 if result["verified"] else 5
        result = app.export(args.task_id, args.output)
    return result, 3 if result.get("error") or result.get("stale") else 0


def main(argv=None):
    json_output = "--json" in (sys.argv[1:] if argv is None else argv)
    args = None
    try:
        args = parser().parse_args(argv)
        result, code = dispatch(args)
        envelope = {
            "schema_version": "1.0",
            "mode": args.mode,
            "synthetic": args.mode == "mock",
            "data": result,
            "error": None,
        }
    except KeyboardInterrupt:
        envelope, code = (
            {
                "data": None,
                "error": {
                    "code": "INTERRUPTED",
                    "message": "Interrupted locally; task/job state may have changed. Inspect status and reconcile; never automatically retry",
                },
            },
            130,
        )
    except Exception:
        envelope, code = (
            {
                "data": None,
                "error": {
                    "code": "REQUEST_REJECTED_OR_UNAVAILABLE",
                    "message": "Check arguments, owner policy, bindings and service availability. After execution uncertainty inspect task status; never automatically resubmit.",
                },
            },
            2,
        )
    envelope.setdefault("schema_version", "1.0")
    envelope.setdefault("mode", args.mode if args else None)
    envelope.setdefault("synthetic", args.mode == "mock" if args else None)
    if json_output:
        print(json.dumps(envelope, ensure_ascii=True, sort_keys=True))
    else:
        # JSON escaping also prevents terminal-control injection in human output.
        print(json.dumps(envelope, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
