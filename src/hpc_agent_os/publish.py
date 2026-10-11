"""Compute-node recipe runner: checksum-pinned SIF or raw database artifact only."""

import ctypes
import errno
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import resource
import socket
import sys
from urllib.parse import urlsplit

from .operations import Recipe, checked_path, digest
from .process import BoundedRunner


def matches(path, value):
    return not path.is_symlink() and path.is_file() and json.loads(path.read_text()) == value


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(plan, runner=None):
    recipe = Recipe.model_validate(plan["recipe"])
    target = Path(plan["target"])
    checked_path(target, target.parent.parent, exists=False)
    if not target.parent.parent.is_dir() or not os.access(target.parent.parent, os.W_OK):
        raise ValueError("Release root not writable on compute node; no escalation")
    if recipe.operation == "software.install" and not recipe.filename.endswith(".sif"):
        raise ValueError("Initial software adapter supports versioned Apptainer SIF artifacts only")
    manifest = {"recipe": recipe.model_dump(), "recipe_digest": digest(recipe.model_dump())}
    ready = target / "manifest.json"
    intent = {
        "publisher": "hpc-agent-os-v1",
        "recipe_digest": manifest["recipe_digest"],
        "target": str(target),
        "uid": os.getuid(),
    }
    reservation = target.parent / (".hpc-reservation-" + digest(str(target)))

    def recoverable():
        marker = target / ".publication-intent.json"
        return matches(marker, intent) or (
            matches(reservation, intent)
            and target.stat().st_uid == os.getuid()
            and not any(target.iterdir())
        )

    if target.exists():
        if matches(ready, manifest):
            artifact = target / recipe.filename
            if artifact.is_symlink() or artifact.stat().st_size != recipe.size_bytes:
                raise ValueError("Existing release differs")
            with artifact.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != recipe.sha256:
                    raise ValueError("Existing release checksum differs")
            return {"published": True, "changed": False, "target": str(target)}
        if not recoverable():
            raise ValueError(
                "Existing release without matching publication intent is never adopted"
            )
        if set(p.name for p in target.iterdir()) - {recipe.filename, ".publication-intent.json"}:
            raise ValueError("Unexpected files in incomplete release")
    # Unique staging survives failures; a new approved task may retry this version.
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / (".hpc-stage-" + plan["id"])
    staging.mkdir(mode=0o755, exist_ok=False)
    artifact = staging / recipe.filename
    ready = staging / "manifest.json"
    url = urlsplit(recipe.url)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Invalid artifact URL")
    addresses = socket.getaddrinfo(url.hostname, 443, socket.AF_INET, socket.SOCK_STREAM)
    address = addresses[0][4][0]
    if not ipaddress.ip_address(address).is_global:
        raise ValueError("Artifact address must be public")
    # Fixed address and no redirects/proxy prevent DNS rebinding and redirect SSRF.
    argv = (
        "/usr/bin/curl",
        "--disable",
        "--fail",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--noproxy",
        "*",
        "--max-time",
        "1800",
        "--max-filesize",
        str(recipe.size_bytes),
        "--limit-rate",
        "8M",
        "--resolve",
        f"{url.hostname}:443:{address}",
        "--output",
        str(artifact),
        "--",
        recipe.url,
    )
    (runner or BoundedRunner(timeout=1810, max_bytes=8192)).run(argv)
    if artifact.is_symlink() or artifact.stat().st_size != recipe.size_bytes:
        raise ValueError("Artifact size mismatch")
    with artifact.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != recipe.sha256:
            raise ValueError("Artifact checksum mismatch")
    if recipe.operation == "software.install":
        (runner or BoundedRunner(timeout=10)).run(("/usr/bin/apptainer", "inspect", str(artifact)))
    with ready.open("x") as stream:
        json.dump(manifest, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    staged_intent = staging / ".publication-intent.json"
    with staged_intent.open("x") as stream:
        json.dump(intent, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    with artifact.open("rb") as stream:
        os.fsync(stream.fileno())
    sync_directory(staging)
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staging), -100, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        if code not in (
            errno.EINVAL,
            errno.ENOSYS,
            errno.EOPNOTSUPP,
            errno.EEXIST,
            errno.ENOTEMPTY,
        ):
            raise OSError(code, "Atomic no-replace publication failed; staging retained")
        # GPFS may not support renameat2 flags. Each link is exclusive, manifest commits last.
        # Reserve durably before mkdir, so a crash before linking intent is recoverable.
        if target.exists() and not recoverable():
            raise ValueError("Target appeared without matching publication intent")
        try:
            os.link(staged_intent, reservation)
        except FileExistsError:
            if not matches(reservation, intent):
                raise ValueError("Publication reservation differs")
        sync_directory(target.parent)
        try:
            target.mkdir(mode=0o755)
        except FileExistsError:
            pass
        checked_path(target, target.parent.parent)
        marker = target / ".publication-intent.json"
        if not recoverable():
            raise ValueError("Publication target differs; retained for review")
        try:
            os.link(staged_intent, marker)
        except FileExistsError:
            pass
        if not matches(marker, intent):
            raise ValueError("Publication intent differs; target retained")
        sync_directory(target)
        if set(p.name for p in target.iterdir()) - {
            recipe.filename,
            "manifest.json",
            ".publication-intent.json",
        }:
            raise ValueError("Unexpected release contents")
        destination = target / recipe.filename
        try:
            os.link(artifact, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.stat().st_size != recipe.size_bytes:
                raise ValueError("Existing artifact differs")
            with destination.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != recipe.sha256:
                    raise ValueError("Existing artifact checksum differs")
        try:
            os.link(ready, target / "manifest.json")
        except FileExistsError:
            if (target / "manifest.json").is_symlink() or json.loads(
                (target / "manifest.json").read_text()
            ) != manifest:
                raise ValueError("Existing manifest differs")
    sync_directory(target)
    sync_directory(target.parent)
    return {"published": True, "changed": True, "target": str(target)}


def main():
    path = Path(sys.argv[1])
    plan = json.loads(path.read_text())
    original = dict(plan)
    claimed = original.pop("digest")
    if digest(original) != claimed:
        raise ValueError("Plan digest mismatch")
    if plan["uid"] != os.getuid():
        raise ValueError("Recipe identity mismatch")
    # Bound each downloaded file independently even when HTTP omits Content-Length.
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (max(plan["recipe"]["size_bytes"], 4096), max(plan["recipe"]["size_bytes"], 4096)),
    )
    try:
        result = publish(plan)
    except Exception:
        result = {
            "published": False,
            "message": "Recipe failed; retain partial files for review, never auto-delete",
        }
    receipt = {"plan_digest": claimed, "result": result}
    with (path.parent / "result.json").open("x") as stream:
        json.dump(receipt, stream)
    if not result["published"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
