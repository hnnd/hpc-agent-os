#!/usr/bin/env bash
# Explicitly invoked local dependency setup; never installs system packages or starts services.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export UV_CACHE_DIR="$PWD/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
export UV_PYTHON_BIN_DIR="$PWD/.runtime/bin"
export CONDA_PKGS_DIRS="$PWD/.cache/conda"
export MAMBA_ROOT_PREFIX="$PWD/.runtime/mamba"
command -v uv >/dev/null
: "${MAMBA_BIN:?Set MAMBA_BIN to an existing mamba executable}"
timeout 600 uv python install 3.12.13
timeout 600 uv sync --locked
if .venv/bin/python automation/check_postgres_lock.py; then
  printf '%s\n' 'PostgreSQL packages already match the lock; no changes.'
elif [[ -f .runtime/postgres/conda-meta/history ]]; then
  printf '%s\n' 'Existing PostgreSQL environment differs from the lock; refusing automatic replacement.' >&2
  exit 1
else
  timeout 600 "$MAMBA_BIN" --no-rc create --yes --prefix "$PWD/.runtime/postgres" --file config/postgres-linux-64.lock
fi
