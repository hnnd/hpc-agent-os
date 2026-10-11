"""Real filesystem metadata catalog; deliberately separate from synthetic Registry."""

import hashlib
import json
import os
from pathlib import Path
import socket
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from .errors import DomainError
from .models import StrictModel, Identifier, Digest, ErrorCode


class Relocation(StrictModel):
    legacy_root: str
    canonical_root: str
    backup_root: str


class CatalogConfig(StrictModel):
    expected_host: str
    expected_uid: int = Field(gt=0)
    cluster_id: Identifier = "fixture-cluster"
    include_system_packages: bool = False
    public_software_root: str
    module_roots: list[str]
    conda_roots: list[str]
    database_roots: list[str]
    private_roots: list[str]
    relocations: list[Relocation] = Field(default_factory=list, max_length=16)
    max_directory_entries: int = Field(default=2000, ge=1, le=5000)
    max_metadata_files: int = Field(default=12000, ge=1, le=20000)
    scan_timeout_seconds: int = Field(default=120, ge=1, le=300)

    @model_validator(mode="after")
    def validate_relocations(self):
        seen = []
        for item in self.relocations:
            for value in (item.legacy_root, item.canonical_root, item.backup_root):
                path = Path(value)
                if not path.is_absolute() or str(path) != value or ".." in path.parts:
                    raise ValueError("Relocation requires normalized absolute paths")
                if not any(
                    path.is_relative_to(Path(p)) and path != Path(p) for p in self.private_roots
                ):
                    raise ValueError("Relocation must stay in configured private namespaces")
                if any(path.is_relative_to(p) or p.is_relative_to(path) for p in seen):
                    raise ValueError("Overlapping relocation paths")
                seen.append(path)
            if Path(item.backup_root).parent != Path(item.legacy_root).parent:
                raise ValueError("Backup must share the original parent")
            if not any(
                Path(item.canonical_root).is_relative_to(Path(p))
                for p in [*self.database_roots, *self.conda_roots]
            ):
                raise ValueError("Relocation target outside scan roots")
        return self

    def legacy_paths(self, root):
        path = Path(root)
        return [
            str(Path(r.legacy_root) / path.relative_to(r.canonical_root))
            for r in self.relocations
            if path.is_relative_to(r.canonical_root)
        ]


class CatalogRecord(StrictModel):
    id: Identifier
    kind: Literal["software", "database"]
    name: Identifier
    version: str = Field(max_length=128)
    root: str = Field(max_length=1024)
    legacy_paths: list[str] = Field(default_factory=list, max_length=1)
    manager: Literal["existing", "conda", "data"]
    type: str = Field(max_length=64)
    visibility: Literal["public-metadata", "owner-only"]
    owner_uid: int | None
    source: Literal["filesystem-inventory"] = "filesystem-inventory"
    status: Literal["discovered"] = "discovered"
    verified: Literal[False] = False
    integrity: Literal["not-checked"] = "not-checked"
    content_digest: Digest | None = None
    version_evidence: Literal["directory-name", "package-metadata", "manifest-declared", "unknown"]
    license: str = Field(default="unknown", max_length=256)
    observed_at: datetime
    evidence_paths: list[str] = Field(default_factory=list, max_length=8)
    executables: list[str] = Field(default_factory=list, max_length=8)
    module_names: list[str] = Field(default_factory=list, max_length=16)
    declared_checksums: dict[str, str] = Field(default_factory=dict, max_length=8)
    size_bytes: int | None = Field(default=None, ge=0)
    size_scope: Literal["not-measured", "listed-files-only"] = "not-measured"
    notes: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_record(self):
        for path in [self.root, *self.legacy_paths, *self.evidence_paths, *self.executables]:
            if not Path(path).is_absolute() or ".." in Path(path).parts or "//" in path:
                raise ValueError("Canonical absolute paths required")
        if self.visibility == "owner-only" and self.owner_uid is None:
            raise ValueError("Private metadata needs an owner")
        if self.observed_at.tzinfo is None:
            raise ValueError("Timezone required")
        if self.content_digest is not None:
            raise ValueError("Discovery does not compute content digests")
        return self


class CatalogSnapshot(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    cluster_id: Identifier
    owner_uid: int
    observed_at: datetime
    records: list[CatalogRecord] = Field(max_length=20000)
    coverage: list[dict] = Field(max_length=500)

    @model_validator(mode="after")
    def unique_records(self):
        if len({r.id for r in self.records}) != len(self.records):
            raise ValueError("Duplicate record")
        if any(r.owner_uid not in (None, self.owner_uid) for r in self.records):
            raise ValueError("Foreign owner metadata")
        return self


def load_config(root):
    cfg = CatalogConfig.model_validate_json((Path(root) / "config/catalog.json").read_text())
    if cfg.expected_uid != os.getuid() or cfg.expected_host != socket.gethostname().split(".")[0]:
        raise ValueError("Catalog identity mismatch")
    return cfg


def connect(root, role="hpc_catalog_query"):
    if role not in ("hpc_catalog_query", "hpc_catalog_import", "hpc_fixture_admin"):
        raise ValueError("Catalog role forbidden")
    if any(k.startswith("PG") for k in os.environ):
        raise ValueError("PostgreSQL environment override forbidden")
    root = Path(root)
    path = root / ".runtime/phase3/socket"
    if path.resolve() != path or not path.is_dir():
        raise ValueError("Catalog socket must be project local and canonical")
    return psycopg.connect(
        host=str(path),
        dbname="hpc_catalog",
        user=role,
        connect_timeout=3,
        options="-c statement_timeout=5000 -c lock_timeout=2000",
        passfile=str(root / ".runtime/disabled-passfile"),
    )


DDL = """
CREATE TABLE IF NOT EXISTS catalog_snapshot (
 digest text PRIMARY KEY, cluster_id text NOT NULL, owner_uid bigint NOT NULL,
 observed_at timestamptz NOT NULL, payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_record (
 snapshot_digest text REFERENCES catalog_snapshot(digest), id text NOT NULL,
 kind text NOT NULL, name text NOT NULL, version text NOT NULL, owner_uid bigint,
 payload jsonb NOT NULL, PRIMARY KEY(snapshot_digest,id));
CREATE TABLE IF NOT EXISTS catalog_active (
 cluster_id text NOT NULL, owner_uid bigint NOT NULL,
 snapshot_digest text REFERENCES catalog_snapshot(digest),
 PRIMARY KEY(cluster_id,owner_uid));
"""


def import_snapshot(connect_fn, snapshot, cfg):
    if snapshot.owner_uid != cfg.expected_uid or snapshot.cluster_id != cfg.cluster_id:
        raise ValueError("Snapshot scope mismatch")
    allowed = [Path(p) for p in [cfg.public_software_root, *cfg.conda_roots, *cfg.database_roots]]
    if cfg.include_system_packages:
        allowed.extend(
            Path(p) for p in ("/usr/bin/apptainer", "/usr/bin/modulecmd", "/usr/bin/sinfo")
        )
    private = [Path(p) for p in cfg.private_roots]
    for record in snapshot.records:
        path = Path(record.root)
        if record.legacy_paths != cfg.legacy_paths(path):
            raise ValueError("Unregistered legacy path")
        if any(path.is_relative_to(r.backup_root) for r in cfg.relocations):
            raise ValueError("Migration backup cannot be registered")
        if record.legacy_paths and (
            record.visibility != "owner-only" or record.owner_uid != cfg.expected_uid
        ):
            raise ValueError("Relocated resource requires current owner")
        if record.legacy_paths:
            expected = (
                "observed-"
                + hashlib.sha256(
                    (record.legacy_paths[0] + "\0" + record.name).encode()
                ).hexdigest()[:24]
            )
            if record.id != expected:
                raise ValueError("Relocation identity changed")
        if not any(path.is_relative_to(p) for p in allowed):
            raise ValueError("Record outside catalog roots")
        if any(path.is_relative_to(p) for p in private) and record.visibility != "owner-only":
            raise ValueError("Personal resource cannot be public")
    payload = snapshot.model_dump(mode="json")
    checksum = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with connect_fn() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (cfg.expected_uid,))
        added = conn.execute(
            "INSERT INTO catalog_snapshot VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING digest",
            (
                checksum,
                snapshot.cluster_id,
                snapshot.owner_uid,
                snapshot.observed_at,
                Jsonb(payload),
            ),
        ).fetchone()
        if added:
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO catalog_record VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            checksum,
                            r.id,
                            r.kind,
                            r.name,
                            r.version,
                            r.owner_uid,
                            Jsonb(r.model_dump(mode="json")),
                        )
                        for r in snapshot.records
                    ],
                )
        conn.execute(
            "INSERT INTO catalog_active VALUES(%s,%s,%s) ON CONFLICT(cluster_id,owner_uid) DO UPDATE SET snapshot_digest=EXCLUDED.snapshot_digest",
            (snapshot.cluster_id, snapshot.owner_uid, checksum),
        )
    return {"snapshot_digest": checksum, "records": len(snapshot.records), "inserted": bool(added)}


class CatalogReader:
    def __init__(self, connect_fn, uid):
        self.connect, self.uid = connect_fn, uid

    def search(self, *args, **kwargs):
        rows, cursor, _ = self.search_snapshot(*args, **kwargs)
        return rows, cursor

    def search_snapshot(
        self,
        cluster,
        kind,
        *,
        query="",
        version=None,
        platform=None,
        type=None,
        offset=0,
        limit=100,
        release_id=None,
    ):
        sql = """SELECT r.payload FROM catalog_record r JOIN catalog_active a
                 ON r.snapshot_digest=a.snapshot_digest
                 WHERE a.cluster_id=%s AND a.owner_uid=%s AND r.kind=%s
                 AND (r.owner_uid IS NULL OR r.owner_uid=%s)"""
        params = [cluster, self.uid, kind, self.uid]
        for expression, value in [
            ("r.version", version),
            ("r.id", release_id),
            ("r.payload->>'type'", type),
        ]:
            if value is not None:
                sql += f" AND {expression}=%s"
                params.append(value)
        if platform is not None:
            # Architecture has not been verified; never imply a matching platform.
            sql += " AND false"
        if query:
            sql += " AND (strpos(lower(r.name),lower(%s))>0 OR strpos(lower(r.payload->>'root'),lower(%s))>0 OR strpos(lower(COALESCE(r.payload->>'legacy_paths','')),lower(%s))>0)"
            params.extend([query, query, query])
        sql += " ORDER BY r.name,r.version,r.id LIMIT %s OFFSET %s"
        params.extend([limit + 1, offset])
        try:
            with self.connect() as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                active = conn.execute(
                    "SELECT s.observed_at FROM catalog_active a JOIN catalog_snapshot s ON s.digest=a.snapshot_digest WHERE a.cluster_id=%s AND a.owner_uid=%s",
                    (cluster, self.uid),
                ).fetchone()
                rows = [r[0] for r in conn.execute(sql, params)]
            return (
                rows[:limit],
                str(offset + limit) if len(rows) > limit else None,
                active[0] if active else None,
            )
        except psycopg.Error:
            raise DomainError(ErrorCode.UNAVAILABLE, "Real catalog unavailable") from None
