"""Bounded local metadata and artifact IO; never return file contents to callers."""

import hashlib
import json
import os
from pathlib import Path
import stat


def canonical(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Canonical absolute path required; symlinks forbidden")
    return path


def read_bytes(path, limit=1048576):
    path = canonical(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Expected bounded regular file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("File size limit exceeded")
        return content
    finally:
        os.close(fd)


def read_json(path):
    return json.loads(read_bytes(path))


def write_new(path, data):
    path = canonical(path)
    encoded = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def artifact_check(work, spec):
    path = canonical(work / spec.path)
    if not path.is_relative_to(work):
        raise ValueError("Artifact outside task")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError("Artifact must be owned regular file")
        if not spec.min_bytes <= before.st_size <= spec.max_bytes:
            return {"passed": False, "reason": "size_out_of_bounds", "size_bytes": before.st_size}
        sha, total = hashlib.sha256(), 0
        while chunk := os.read(fd, min(1048576, spec.max_bytes + 1 - total)):
            total += len(chunk)
            if total > spec.max_bytes:
                raise ValueError("Artifact grew beyond budget")
            sha.update(chunk)
        after = os.fstat(fd)

        def signature(s):
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

        if signature(before) != signature(after) or signature(after) != signature(path.stat()):
            raise ValueError("Artifact changed during verification")
        value = sha.hexdigest()
        return {
            "passed": spec.sha256 is None or spec.sha256 == value,
            "sha256": value,
            "size_bytes": total,
            "criterion": "sha256_and_size" if spec.sha256 else "size_only",
        }
    finally:
        os.close(fd)
