"""Verify public snapshot files against the source manifest, without reading private sources."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {
    ".git",
    ".runtime",
    ".venv",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
}


def main():
    manifest = json.loads((ROOT / "SOURCE-MANIFEST.json").read_text())
    seen = set()
    failures = []
    for record in manifest["files"]:
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts or str(relative) in seen:
            failures.append("Invalid or duplicate manifest path")
            continue
        seen.add(str(relative))
        path = ROOT / relative
        if not path.is_file() or path.is_symlink() or path.resolve() != path:
            failures.append(f"Missing or noncanonical file: {relative}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != record["published_sha256"]:
            failures.append(f"Published digest mismatch: {relative}")
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED for part in relative.parts):
            continue
        if path.is_file() and str(relative) not in seen and str(relative) != "SOURCE-MANIFEST.json":
            failures.append(f"Unlisted file: {relative}")
    for failure in failures:
        print(failure)
    if not failures:
        print(f"Source manifest matches {len(seen)} published files")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
