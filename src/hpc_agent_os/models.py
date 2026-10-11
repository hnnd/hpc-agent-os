"""Strict, versioned boundary models. Configuration never authorizes production writes."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
JobID = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,17}(?:_[0-9]{1,10})?$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    FORBIDDEN = "FORBIDDEN"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    PARSE_ERROR = "PARSE_ERROR"
    STALE_DATA = "STALE_DATA"


class Error(StrictModel):
    code: ErrorCode
    message: str


class Response(StrictModel):
    schema_version: str = "1.0"
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    cluster_id: str
    source: str
    observed_at: datetime | None = None
    stale: bool = False
    data: Any = None
    next_cursor: str | None = None
    error: Error | None = None


class ClusterRequest(StrictModel):
    cluster_id: Identifier


class PageRequest(ClusterRequest):
    cursor: Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,8})$")] | None = None
    limit: int = Field(default=100, ge=1, le=500)


class NodeRequest(PageRequest):
    state: Literal["IDLE", "ALLOCATED", "MIXED", "DOWN", "DRAIN", "UNKNOWN"] | None = None


class JobRequest(PageRequest):
    state: (
        Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "UNKNOWN"]
        | None
    ) = None


class GetJobRequest(ClusterRequest):
    job_id: JobID


class SearchRequest(PageRequest):
    query: str = Field(default="", max_length=128)
    version: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=64)
    type: str | None = Field(default=None, max_length=64)


class GetReleaseRequest(ClusterRequest):
    release_id: Identifier


class Node(StrictModel):
    name: Identifier
    partitions: list[Identifier]
    state: str
    cpus: int = Field(ge=0)
    memory_mib: int = Field(ge=0)
    gres: str = Field(default="", max_length=256)


class Partition(StrictModel):
    name: Identifier
    availability: Literal["up", "down", "unknown"]
    max_time: str = Field(max_length=64)


class Job(StrictModel):
    job_id: JobID
    owner_uid: int = Field(ge=0)
    state: str
    cpus: int = Field(ge=0)
    partition: Identifier
    reason: str = Field(max_length=64)


class SlurmConfig(StrictModel):
    mode: Literal["mock", "cli"] = "mock"
    live_access_enabled: Literal[False] = False
    command_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    max_output_bytes: int = Field(default=1048576, ge=1024, le=1048576)
    minimum_poll_interval_seconds: float = Field(default=30.0, ge=30)
    cache_ttl_seconds: float = Field(default=60.0, ge=30)
    max_stale_seconds: float = Field(default=300.0, ge=0, le=300)
    max_concurrency: Literal[1] = 1

    @model_validator(mode="after")
    def only_mock_in_phase2(self):
        if self.mode != "mock":
            raise ValueError("Phase 2 entrypoint accepts mock only; CLI adapter is test-only")
        return self


class RegistryConfig(StrictModel):
    backend: Literal["postgresql"] = "postgresql"
    dsn_secret_ref: Literal["HPC_REGISTRY_DSN"] = "HPC_REGISTRY_DSN"


class Config(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    phase: Literal["phase2"] = "phase2"
    cluster_id: Identifier = "fixture-cluster"
    slurm: SlurmConfig = Field(default_factory=SlurmConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    transport: Literal["stdio"] = "stdio"
    allow_mutations: Literal[False] = False


class Release(StrictModel):
    id: Identifier
    kind: Literal["software", "database"]
    name: Identifier
    version: str = Field(min_length=1, max_length=64)
    platform: str = Field(default="linux-x86_64", max_length=64)
    build_id: Identifier = "default"
    type: str = Field(default="generic", max_length=64)
    owner: str = Field(min_length=1, max_length=128)
    license: str = Field(min_length=1, max_length=128)
    manager: Literal["modules", "conda", "apptainer", "spack", "easybuild", "data"]
    root: str = Field(pattern=r"^/fixture/[A-Za-z0-9_./-]+$", max_length=512)
    module_name: str | None = Field(default=None, max_length=128)
    source: Literal["synthetic"] = "synthetic"
    content_digest: Digest
    status: Literal["discovered", "verified", "published", "deprecated"] = "discovered"
    observed_at: datetime
    verified: bool = False
    size_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def safe_fixture(self):
        if ".." in self.root.split("/") or "//" in self.root:
            raise ValueError("noncanonical fixture path")
        if self.observed_at.tzinfo is None:
            raise ValueError("timezone required")
        if self.status in ("verified", "published") and not self.verified:
            raise ValueError("verified status requires evidence flag")
        return self


class DatabaseIndex(StrictModel):
    id: Identifier
    release_id: Identifier
    tool_release_id: Identifier
    parameters_digest: Digest
    index_digest: Digest


class Manifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    synthetic: Literal[True] = True
    cluster_id: Identifier
    releases: list[Release] = Field(max_length=10000)
    indexes: list[DatabaseIndex] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [r.id for r in self.releases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate release id")
        return self
