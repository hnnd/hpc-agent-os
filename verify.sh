#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
case "${1:-}" in
  ""|--unit) ;;
  *) printf '%s\n' 'Usage: bash verify.sh [--unit]' >&2; exit 2 ;;
esac
export PYTHONPATH="$PWD/src:$PWD/application/src:$PWD/automation"
.venv/bin/ruff check src tests automation migrations application .agents/skills/hpc-plugin-developer/scripts
.venv/bin/ruff format --check src tests automation migrations application .agents/skills/hpc-plugin-developer/scripts
if [[ "${1:-}" == --unit ]]; then
  .venv/bin/python -m pytest -q tests/unit application/tests/test_application.py application/tests/test_plugin_skill.py
else
  .venv/bin/python automation/fixture_preflight.py
  .venv/bin/python -m pytest -q tests application/tests
fi
