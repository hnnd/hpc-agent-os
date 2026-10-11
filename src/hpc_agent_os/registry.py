"""PostgreSQL-only Registry with immutable releases and least-privilege readers."""

import hashlib
import json
import os
import re
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from .errors import DomainError
from .models import ErrorCode, Manifest

metadata = MetaData()
clusters = Table("cluster", metadata, Column("id", String(64), primary_key=True))
releases = Table(
    "release",
    metadata,
    Column("cluster_id", String(64), ForeignKey("cluster.id"), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("name", String(64), nullable=False),
    Column("version", String(64), nullable=False),
    Column("platform", String(64), nullable=False),
    Column("build_id", String(64), nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("manifest_digest", String(64), nullable=False),
    Column("payload", JSONB, nullable=False),
    CheckConstraint("kind IN ('software', 'database')"),
    UniqueConstraint("cluster_id", "kind", "name", "version", "platform", "build_id"),
)
indexes = Table(
    "database_index",
    metadata,
    Column("cluster_id", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("release_id", String(64), nullable=False),
    Column("tool_release_id", String(64), nullable=False),
    Column("parameters_digest", String(64), nullable=False),
    Column("index_digest", String(64), nullable=False),
    ForeignKeyConstraint(["cluster_id", "release_id"], ["release.cluster_id", "release.id"]),
    ForeignKeyConstraint(["cluster_id", "tool_release_id"], ["release.cluster_id", "release.id"]),
    UniqueConstraint("cluster_id", "release_id", "tool_release_id", "parameters_digest"),
)
channels = Table(
    "channel",
    metadata,
    Column("cluster_id", String(64), primary_key=True),
    Column("name", String(64), primary_key=True),
    Column("release_id", String(64), nullable=False),
    Column("revision", BigInteger, nullable=False),
    ForeignKeyConstraint(["cluster_id", "release_id"], ["release.cluster_id", "release.id"]),
)


def fixture_engine(dsn: str, project_root: Path, *, role: str = "hpc_query"):
    """Refuse TCP, production databases, alternate libpq parameters and external sockets."""

    def validate_environment():
        if any(name.startswith("PG") for name in os.environ):
            raise DomainError(ErrorCode.FORBIDDEN, "libpq environment overrides are prohibited")

    validate_environment()
    try:
        url = make_url(dsn)
        host = url.query.get("host", "")
        if not isinstance(host, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", host):
            raise ValueError("Host must be one canonical socket directory")
        socket = Path(host)
        project_root = project_root.resolve()
        allowed = project_root / ".runtime"
        if allowed.resolve() != allowed or str(socket.resolve()) != host:
            raise ValueError("Socket path must not traverse symlinks or dot components")
        if (
            url.drivername != "postgresql+psycopg"
            or url.host
            or url.port
            or url.username != role
            or url.password is not None
            or url.database != "hpc_fixture"
            or set(url.query) != {"host"}
            or not socket.is_absolute()
            or not socket.resolve().is_relative_to(allowed)
        ):
            raise ValueError("Not an isolated fixture DSN")
        engine = create_engine(
            url,
            pool_size=2,
            max_overflow=0,
            pool_timeout=3,
            connect_args={
                "connect_timeout": 3,
                "passfile": str(project_root / ".runtime/disabled-passfile"),
                "options": "-c statement_timeout=3000 -c lock_timeout=2000",
            },
            hide_parameters=True,
        )

        @event.listens_for(engine, "do_connect")
        def validate_before_connect(dialect, connection_record, args, kwargs):
            validate_environment()
            if str(socket.resolve()) != host:
                raise DomainError(ErrorCode.FORBIDDEN, "Socket path changed")

        return engine
    except (ValueError, TypeError):
        raise DomainError(
            ErrorCode.FORBIDDEN, "Only project-local fixture PostgreSQL is allowed"
        ) from None


def import_manifest(engine, manifest: Manifest) -> int:
    """No I/O to release paths; one atomic transaction, immutable digest conflict rejection."""
    inserted = 0
    try:
        with engine.begin() as conn:
            conn.execute(insert(clusters).values(id=manifest.cluster_id).on_conflict_do_nothing())
            for release in manifest.releases:
                payload = release.model_dump(mode="json")
                digest = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                values = {
                    k: payload[k]
                    for k in (
                        "id",
                        "kind",
                        "name",
                        "version",
                        "platform",
                        "build_id",
                        "content_digest",
                    )
                }
                result = conn.execute(
                    insert(releases)
                    .values(
                        cluster_id=manifest.cluster_id,
                        manifest_digest=digest,
                        payload=payload,
                        **values,
                    )
                    .on_conflict_do_nothing()
                    .returning(releases.c.id)
                )
                if result.scalar_one_or_none() is not None:
                    inserted += 1
                else:
                    old = conn.execute(
                        select(releases.c.manifest_digest).where(
                            releases.c.cluster_id == manifest.cluster_id,
                            releases.c.id == release.id,
                        )
                    ).scalar_one_or_none()
                    if old != digest:
                        raise DomainError(ErrorCode.INVALID_ARGUMENT, "Immutable release conflict")
            for index in manifest.indexes:
                for release_id, kind in (
                    (index.release_id, "database"),
                    (index.tool_release_id, "software"),
                ):
                    actual = conn.execute(
                        select(releases.c.kind).where(
                            releases.c.cluster_id == manifest.cluster_id,
                            releases.c.id == release_id,
                        )
                    ).scalar_one_or_none()
                    if actual != kind:
                        raise DomainError(
                            ErrorCode.INVALID_ARGUMENT, "Invalid index release reference"
                        )
                values = index.model_dump()
                result = conn.execute(
                    insert(indexes)
                    .values(cluster_id=manifest.cluster_id, **values)
                    .on_conflict_do_nothing()
                    .returning(indexes.c.id)
                )
                if result.scalar_one_or_none() is None:
                    old = (
                        conn.execute(
                            select(indexes).where(
                                indexes.c.cluster_id == manifest.cluster_id,
                                indexes.c.id == index.id,
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if old is None or any(old[k] != v for k, v in values.items()):
                        raise DomainError(ErrorCode.INVALID_ARGUMENT, "Immutable index conflict")
        return inserted
    except SQLAlchemyError:
        raise DomainError(ErrorCode.UNAVAILABLE, "Registry import transaction failed") from None


class RegistryReader:
    def __init__(self, engine):
        self.engine = engine

    def search(
        self,
        cluster: str,
        kind: str,
        *,
        query="",
        version=None,
        platform=None,
        type=None,
        offset=0,
        limit=100,
        release_id=None,
    ):
        # Views expose only schema-validated public metadata. No arbitrary SQL or URL fetch.
        sql = "SELECT payload FROM registry_public WHERE cluster_id=:cluster AND kind=:kind"
        params = {"cluster": cluster, "kind": kind, "limit": limit + 1, "offset": offset}
        for key, value in (("version", version), ("platform", platform), ("id", release_id)):
            if value is not None:
                sql += f" AND {key}=:{key}"
                params[key] = value
        if type is not None:
            sql += " AND payload->>'type'=:type"
            params["type"] = type
        if query:
            sql += " AND strpos(lower(name), lower(:query)) > 0"
            params["query"] = query
        sql += " ORDER BY name, version, id LIMIT :limit OFFSET :offset"
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                result = [dict(row[0]) for row in conn.execute(text(sql), params)]
                for row in result[:limit]:
                    if row["kind"] == "database":
                        row["indexes"] = [
                            dict(x)
                            for x in conn.execute(
                                text(
                                    "SELECT id, tool_release_id, parameters_digest, index_digest "
                                    "FROM registry_indexes WHERE cluster_id=:cluster AND release_id=:id ORDER BY id"
                                ),
                                {"cluster": cluster, "id": row["id"]},
                            ).mappings()
                        ]
                return result[:limit], str(offset + limit) if len(result) > limit else None
        except SQLAlchemyError:
            raise DomainError(ErrorCode.UNAVAILABLE, "Registry unavailable") from None
