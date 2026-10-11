"""CLI adapter is dependency-injectable and never enabled by the Phase 2 entrypoint."""

import copy
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from pydantic import ValidationError

from .errors import DomainError
from .models import ErrorCode, Job, Node, Partition, utcnow
from .process import BoundedRunner

NODE_ARGV = ("/usr/bin/sinfo", "--Node", "--noheader", "--format=%N|%P|%T|%c|%m|%G")
PARTITION_ARGV = ("/usr/bin/sinfo", "--noheader", "--format=%P|%a|%l")
SAFE_REASONS = {"Priority", "Resources", "Dependency", "QOSMaxJobsPerUserLimit", "None"}


def state(value: str) -> str:
    value = value.upper().rstrip("*~#$@!%+-")
    aliases = {"ALLOC": "ALLOCATED", "MIX": "MIXED", "DRAINED": "DRAIN", "DRAINING": "DRAIN"}
    value = aliases.get(value, value)
    return (
        value
        if value
        in {
            "IDLE",
            "ALLOCATED",
            "MIXED",
            "DOWN",
            "DRAIN",
            "PENDING",
            "RUNNING",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
        }
        else "UNKNOWN"
    )


def parse_nodes(text: str) -> list[dict]:
    nodes = {}
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            name, partition, status, cpus, memory, gres = line.strip().split("|")
            n = Node(
                name=name,
                partitions=[partition.rstrip("*")],
                state=state(status),
                cpus=int(cpus),
                memory_mib=int(memory),
                gres="" if gres in ("(null)", "N/A") else gres,
            )
            if name in nodes:
                old = nodes[name]
                if (old.cpus, old.memory_mib, old.state, old.gres) != (
                    n.cpus,
                    n.memory_mib,
                    n.state,
                    n.gres,
                ):
                    raise ValueError("conflicting duplicate node")
                old.partitions = sorted(set(old.partitions + n.partitions))
            else:
                nodes[name] = n
        return [nodes[k].model_dump() for k in sorted(nodes)]
    except (ValueError, ValidationError):
        raise DomainError(ErrorCode.PARSE_ERROR, "Invalid node response") from None


def parse_partitions(text: str) -> list[dict]:
    partitions = {}
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            name, availability, limit = line.strip().split("|")
            p = Partition(
                name=name.rstrip("*"),
                availability=availability if availability in ("up", "down") else "unknown",
                max_time=limit,
            )
            if p.name in partitions and partitions[p.name] != p.model_dump():
                raise ValueError("conflicting partition")
            partitions[p.name] = p.model_dump()
        return [partitions[k] for k in sorted(partitions)]
    except (ValueError, ValidationError):
        raise DomainError(ErrorCode.PARSE_ERROR, "Invalid partition response") from None


def parse_jobs(text: str, uid: int) -> list[dict]:
    jobs = {}
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            job_id, owner, status, cpus, partition, reason = line.strip().split("|")
            job = Job(
                job_id=job_id,
                owner_uid=int(owner),
                state=state(status),
                cpus=int(cpus),
                partition=partition,
                reason=reason if reason in SAFE_REASONS else "REDACTED",
            )
            if job.owner_uid != uid:
                continue
            if job.job_id in jobs:
                raise ValueError("duplicate job")
            jobs[job.job_id] = job.model_dump()
        return [jobs[k] for k in sorted(jobs)]
    except (ValueError, ValidationError):
        raise DomainError(ErrorCode.PARSE_ERROR, "Invalid job response") from None


class CLIAdapter:
    source = "slurm-cli"

    def __init__(self, runner: BoundedRunner):
        self.runner = runner

    def fetch(self, kind: str, uid: int) -> list[dict]:
        if kind == "nodes":
            return parse_nodes(self.runner.run(NODE_ARGV))
        if kind == "partitions":
            return parse_partitions(self.runner.run(PARTITION_ARGV))
        if kind == "jobs" and type(uid) is int and uid >= 0:
            argv = (
                "/usr/bin/squeue",
                "--noheader",
                "--array",
                "--user",
                str(uid),
                "--format=%i|%U|%T|%C|%P|%r",
            )
            return parse_jobs(self.runner.run(argv), uid)
        raise DomainError(ErrorCode.FORBIDDEN, "Operation not allowlisted")


class MockAdapter:
    source = "synthetic-slurm"

    def __init__(self):
        self.calls = 0
        self.failure = None

    def fetch(self, kind: str, uid: int) -> list[dict]:
        self.calls += 1
        if self.failure:
            raise self.failure
        if kind == "nodes":
            return parse_nodes(
                "fixture01|cpu*|idle|8|16384|(null)\nfixture02|cpu|mixed|16|65536|gpu:fixture:1\nfixture02|gpu|mixed|16|65536|gpu:fixture:1"
            )
        if kind == "partitions":
            return parse_partitions("cpu*|up|1-00:00:00\ngpu|up|04:00:00")
        if kind == "jobs":
            return parse_jobs(
                f"100|{uid}|RUNNING|2|cpu|None\n101_2|{uid}|PENDING|1|cpu|Priority", uid
            )
        raise DomainError(ErrorCode.FORBIDDEN, "Unknown read operation")


@dataclass
class Snapshot:
    data: list[dict]
    observed_at: datetime
    stale: bool = False
    error: DomainError | None = None


class CachedSlurm:
    """Single-flight cache; identity-aware jobs, aggregate cache shared safely.

    One upstream request per interval across all operations, no immediate retries.
    Fresh data on another endpoint is never overwritten by a failed query.
    """

    def __init__(
        self,
        adapter,
        ttl=60.0,
        max_stale=300.0,
        min_interval=30.0,
        clock: Callable = time.monotonic,
    ):
        self.adapter = adapter
        self.ttl, self.max_stale, self.min_interval = ttl, max_stale, min_interval
        self.clock = clock
        self.cache = {}
        self.last_attempt = float("-inf")
        self.lock = threading.Lock()

    def get(self, cluster: str, kind: str, uid: int) -> Snapshot:
        key = (cluster, kind, uid if kind == "jobs" else "aggregate")
        with self.lock:
            now = self.clock()
            previous = self.cache.get(key)
            if previous and now - previous[0] <= self.ttl:
                return copy.deepcopy(previous[1])
            error = None
            if now - self.last_attempt < self.min_interval:
                error = DomainError(ErrorCode.UNAVAILABLE, "Upstream refresh rate limited")
            else:
                self.last_attempt = now
                try:
                    snap = Snapshot(self.adapter.fetch(kind, uid), utcnow())
                    self.cache[key] = (self.clock(), snap)
                    return copy.deepcopy(snap)
                except DomainError as exc:
                    error = exc
            if previous and now - previous[0] <= self.max_stale:
                snap = copy.deepcopy(previous[1])
                snap.stale, snap.error = True, error
                return snap
            raise error
