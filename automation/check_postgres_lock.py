"""Exit zero only when installed package identities exactly match the explicit lock."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def matches(lock_text, records):
    expected = {line for line in lock_text.splitlines() if line and not line.startswith(("#", "@"))}
    actual = {record.get("url", "") + "#" + record.get("md5", "") for record in records}
    return bool(expected) and expected == actual


if __name__ == "__main__":
    lock = (ROOT / "config/postgres-linux-64.lock").read_text()
    records = [
        json.loads(p.read_text()) for p in (ROOT / ".runtime/postgres/conda-meta").glob("*.json")
    ]
    raise SystemExit(0 if matches(lock, records) else 1)
