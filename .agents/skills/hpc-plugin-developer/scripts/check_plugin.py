"""Validate explicit JSON plugins and synthetic diagnostic cases without a backend."""

import argparse
import json
from pathlib import Path
import sys


def check(root, plugin, cases=None):
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("Canonical project root required")
    if not (root / "application/schemas/plugin.schema.json").is_file():
        raise ValueError("HPC application project required")
    sys.path[:0] = [str(root / "application/src"), str(root / "src")]
    from hpc_agent_os_app.files import read_json
    from hpc_agent_os_app.plugins import Plugin, Plugins

    parsed = Plugin.model_validate(read_json(plugin))
    registry = Plugins([plugin])
    report = {
        "valid": True,
        "plugin": parsed.id,
        "version": parsed.version,
        "templates": len(parsed.templates),
        "diagnostic_rules": len(parsed.diagnostics),
        "behavior_tested": cases is not None,
        "cases": [],
    }
    if cases is None:
        return report
    scenarios = read_json(cases)
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 128:
        raise ValueError("Expected 1..128 synthetic cases")
    known = {f"{parsed.id}:{rule.id}" for rule in parsed.diagnostics}
    names = set()
    for case in scenarios:
        if (
            not isinstance(case, dict)
            or set(case) != {"id", "task", "expected_rule_ids"}
            or not isinstance(case["id"], str)
            or not 1 <= len(case["id"]) <= 64
            or case["id"] in names
            or not isinstance(case["task"], dict)
            or not isinstance(case["expected_rule_ids"], list)
            or any(not isinstance(k, str) for k in case["expected_rule_ids"])
        ):
            raise ValueError("Invalid synthetic case")
        names.add(case["id"])
        expected = set(case["expected_rule_ids"])
        if not expected <= known or len(expected) != len(case["expected_rule_ids"]):
            raise ValueError("Unknown or duplicate expected rule ID")
        # Refuse arbitrary logs/data in the harness; accept only supported synthetic facts.
        task = case["task"]
        if not set(task) <= {"state", "scheduler_state", "reconciliation_required", "result"}:
            raise ValueError("Unsupported synthetic observation")
        if any(
            v is not None and not isinstance(v, (str, bool))
            for k, v in task.items()
            if k != "result"
        ):
            raise ValueError("Invalid synthetic value")
        result = task.get("result")
        if result is not None and (
            not isinstance(result, dict)
            or not set(result) <= {"scheduler_state", "exit_code"}
            or any(v is not None and not isinstance(v, str) for v in result.values())
        ):
            raise ValueError("Invalid synthetic result")
        actual = {f["rule"] for f in registry.diagnose(task)["findings"] if f["rule"] in known}
        report["cases"].append(
            {
                "id": case["id"],
                "passed": actual == expected,
                "expected": sorted(expected),
                "actual": sorted(actual),
            }
        )
    report["valid"] = all(case["passed"] for case in report["cases"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--cases", type=Path)
    args = parser.parse_args(argv)
    try:
        report = check(args.root, args.plugin, args.cases)
    except Exception:
        print(json.dumps({"valid": False, "error": "PLUGIN_OR_CASES_REJECTED"}))
        return 2
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
