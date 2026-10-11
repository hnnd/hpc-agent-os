"""Developer skill helper validates real plugins with synthetic cases, never a backend."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / ".agents/skills/hpc-plugin-developer/scripts/check_plugin.py"
spec = importlib.util.spec_from_file_location("plugin_skill_checker", HELPER)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
PLUGIN = ROOT / "application/plugins/example.json"


def cases_file(tmp_path, cases):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    return path


def test_structure_only_is_not_behavior_validation():
    report = checker.check(ROOT, PLUGIN)
    assert report["valid"] and not report["behavior_tested"] and report["cases"] == []


def test_positive_negative_and_unknown_evidence(tmp_path):
    path = cases_file(
        tmp_path,
        [
            {
                "id": "positive",
                "task": {"result": {"scheduler_state": "NODE_FAIL"}},
                "expected_rule_ids": ["lab:node-failure"],
            },
            {
                "id": "negative",
                "task": {"result": {"scheduler_state": "COMPLETED"}},
                "expected_rule_ids": [],
            },
            {"id": "missing", "task": {}, "expected_rule_ids": []},
        ],
    )
    report = checker.check(ROOT, PLUGIN, path)
    assert report["valid"] and report["behavior_tested"] and len(report["cases"]) == 3


def test_mismatch_returns_nonzero(tmp_path, capsys):
    path = cases_file(
        tmp_path, [{"id": "wrong", "task": {}, "expected_rule_ids": ["lab:node-failure"]}]
    )
    assert checker.main(["--root", str(ROOT), "--plugin", str(PLUGIN), "--cases", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["cases"][0]["passed"] is False


def test_boolean_and_string_evidence_are_distinct(tmp_path):
    plugin = tmp_path / "bool.json"
    plugin.write_text(
        json.dumps(
            {
                "api_version": "1.0",
                "id": "sample",
                "version": "1",
                "diagnostics": [
                    {
                        "id": "reconcile",
                        "field": "reconciliation_required",
                        "equals": True,
                        "severity": "warning",
                        "explanation": "Unknown state",
                        "next_step": "Inspect",
                    }
                ],
            }
        )
    )
    cases = cases_file(
        tmp_path,
        [
            {
                "id": "boolean",
                "task": {"reconciliation_required": True},
                "expected_rule_ids": ["sample:reconcile"],
            },
            {"id": "string", "task": {"reconciliation_required": "true"}, "expected_rule_ids": []},
        ],
    )
    assert checker.check(ROOT, plugin, cases)["valid"]


@pytest.mark.parametrize(
    "cases",
    [
        [],
        [{"id": "unknown-rule", "task": {}, "expected_rule_ids": ["other:missing"]}],
        [{"id": "logs", "task": {"logs": "private marker"}, "expected_rule_ids": []}],
        [{"id": "nested-state", "task": {"state": {}}, "expected_rule_ids": []}],
    ],
)
def test_invalid_cases_rejected(tmp_path, cases):
    with pytest.raises(ValueError):
        checker.check(ROOT, PLUGIN, cases_file(tmp_path, cases))


def test_invalid_plugin_redacts_details(tmp_path, capsys):
    plugin = tmp_path / "bad.json"
    plugin.write_text(json.dumps({"id": "bad", "version": "1", "command": "private marker"}))
    assert checker.main(["--root", str(ROOT), "--plugin", str(plugin)]) == 2
    output = capsys.readouterr().out
    assert "private marker" not in output and not json.loads(output)["valid"]
