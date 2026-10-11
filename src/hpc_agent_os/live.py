"""Single-identity, local-only Slurm bridge for the authorized Phase 3 trial."""

import argparse
import json
import os
from pathlib import Path
import socket
import struct
import sys
import time
from datetime import datetime
from typing import Literal

from pydantic import Field, ValidationError

from .errors import DomainError
from .models import ErrorCode, Identifier, Job, Node, Partition, StrictModel, utcnow
from .process import BoundedRunner
from .server import build_server
from .service import Application
from .slurm import CachedSlurm, CLIAdapter, Snapshot

MAX_WIRE = 2 * 1024 * 1024


class LiveConfig(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    phase: Literal["phase3-live-readonly"] = "phase3-live-readonly"
    cluster_id: Identifier
    expected_host: str
    expected_uid: int = Field(gt=0)
    transport: Literal["stdio"] = "stdio"
    allow_mutations: Literal[False] = False
    command_timeout_seconds: float = Field(default=10.0, gt=0, le=10)
    minimum_interval_seconds: float = Field(default=30.0, ge=30)
    cache_ttl_seconds: float = Field(default=60.0, ge=30, le=60)
    max_stale_seconds: float = Field(default=300.0, ge=60, le=300)

    def check_identity(self):
        if (
            self.expected_uid != os.getuid()
            or self.expected_host != socket.gethostname().split(".")[0]
        ):
            raise DomainError(ErrorCode.FORBIDDEN, "Trial identity mismatch")

    @property
    def address(self):
        # Abstract AF_UNIX: no unlink/replacement, kernel enforces a single binder.
        return "\0hpc-agent-os-live-" + str(self.expected_uid)


class BridgeRequest(StrictModel):
    kind: Literal["nodes", "partitions", "jobs"]


def peer_uid(conn):
    return struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def receive(conn, maximum):
    data = bytearray()
    deadline = time.monotonic() + (conn.gettimeout() or 2.0)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Bridge message deadline exceeded")
        conn.settimeout(remaining)
        chunk = conn.recv(min(65536, maximum + 1 - len(data)))
        if not chunk:
            raise DomainError(ErrorCode.UNAVAILABLE, "Incomplete bridge response")
        data.extend(chunk)
        if len(data) > maximum:
            raise DomainError(ErrorCode.OUTPUT_LIMIT, "Bridge message exceeds limit")
        if data.endswith(b"\n"):
            return bytes(data)


def dispatch(payload, uid, cfg, cache):
    if uid != cfg.expected_uid:
        raise DomainError(ErrorCode.FORBIDDEN, "Bridge identity rejected")
    try:
        request = BridgeRequest.model_validate_json(payload)
    except ValidationError:
        raise DomainError(ErrorCode.INVALID_ARGUMENT, "Invalid bridge request") from None
    snap = cache.get(cfg.cluster_id, request.kind, uid)
    return {
        "data": snap.data,
        "observed_at": snap.observed_at.isoformat(),
        "stale": snap.stale,
        "error": {"code": snap.error.code, "message": snap.error.message} if snap.error else None,
    }


def handle_connection(conn, cfg, cache):
    uid = peer_uid(conn)
    if uid != cfg.expected_uid:
        raise DomainError(ErrorCode.FORBIDDEN, "Bridge identity rejected")
    return dispatch(receive(conn, 256), uid, cfg, cache)


def serve(cfg):
    cfg.check_identity()
    cache = CachedSlurm(
        CLIAdapter(BoundedRunner(timeout=cfg.command_timeout_seconds)),
        ttl=cfg.cache_ttl_seconds,
        max_stale=cfg.max_stale_seconds,
        min_interval=cfg.minimum_interval_seconds,
    )
    # Restarts cannot bypass the shared controller gate; no automatic polling.
    cache.last_attempt = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(cfg.address)
        server.listen(8)
        while True:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                try:
                    result = handle_connection(conn, cfg, cache)
                except DomainError as exc:
                    result = {"data": None, "error": {"code": exc.code, "message": exc.message}}
                except (OSError, ValueError):
                    continue
                try:
                    encoded = json.dumps(result).encode() + b"\n"
                    if len(encoded) > MAX_WIRE:
                        encoded = b'{"data":null,"error":{"code":"OUTPUT_LIMIT","message":"Bridge response exceeds limit"}}\n'
                    conn.sendall(encoded)
                except OSError:
                    pass


class BridgeSlurm:
    source = "slurm-live-bridge"

    def __init__(self, cfg):
        self.cfg = cfg
        self.adapter = self

    def get(self, cluster, kind, uid):
        if cluster != self.cfg.cluster_id or uid != os.getuid() or uid != self.cfg.expected_uid:
            raise DomainError(ErrorCode.FORBIDDEN, "Bridge identity rejected")
        request = BridgeRequest(kind=kind)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                # Includes up to one bounded query ahead of us; never waits indefinitely.
                conn.settimeout(25)
                conn.connect(self.cfg.address)
                if peer_uid(conn) != self.cfg.expected_uid:
                    raise DomainError(ErrorCode.FORBIDDEN, "Bridge server identity rejected")
                conn.sendall(request.model_dump_json().encode() + b"\n")
                result = json.loads(receive(conn, MAX_WIRE))
            error = result.get("error")
            failure = DomainError(ErrorCode(error["code"]), error["message"]) if error else None
            if result["data"] is None:
                raise failure or DomainError(ErrorCode.UNAVAILABLE, "Bridge data unavailable")
            model = {"nodes": Node, "partitions": Partition, "jobs": Job}[kind]
            rows = [model.model_validate(row).model_dump() for row in result["data"]]
            observed = datetime.fromisoformat(result["observed_at"])
            age = (utcnow() - observed).total_seconds()
            if age < -5 or age > self.cfg.max_stale_seconds:
                raise DomainError(ErrorCode.STALE_DATA, "Bridge observation expired")
            return Snapshot(rows, observed, bool(result["stale"]), failure)
        except (OSError, ValueError, KeyError, TypeError):
            raise DomainError(
                ErrorCode.UNAVAILABLE, "Bridge unavailable or invalid response"
            ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("bridge", "mcp"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        cfg = LiveConfig.model_validate_json(args.config.read_text())
        cfg.check_identity()
        if args.mode == "bridge":
            serve(cfg)
        else:
            build_server("slurm", Application(cfg, slurm=BridgeSlurm(cfg))).run()
    except (OSError, ValueError, DomainError):
        print(
            "Live entrypoint rejected: identity, configuration or bridge failure", file=sys.stderr
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
