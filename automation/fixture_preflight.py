"""Check local fixture prerequisites without starting any database."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    errors = []
    if os.getuid() == 0:
        errors.append("Run PostgreSQL fixture as a non-root user")
    if any(key.startswith("PG") for key in os.environ):
        errors.append("Clear PG environment overrides before fixture tests")
    if ROOT.resolve() != ROOT or any(path.is_symlink() for path in [ROOT, *ROOT.parents]):
        errors.append("Fixture project root must be a canonical nonsymlink path")
    # tempfile.mkdtemp adds eight characters; fixture reserves 16 for socket name.
    if len(str(ROOT / ".runtime/p/xxxxxxxx/s")) + 16 >= 108:
        errors.append("Project path too long for isolated PostgreSQL Unix sockets")
    for name in ("postgres", "initdb", "pg_ctl"):
        binary = ROOT / ".runtime/postgres/bin" / name
        if not binary.is_file() or not os.access(binary, os.X_OK):
            errors.append(f"Missing project-local executable: {name}")
    for error in errors:
        print(error)
    if not errors:
        print("Local fixture prerequisites present; no server started")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
