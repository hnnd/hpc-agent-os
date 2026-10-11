from datetime import datetime, timezone, timedelta
import os

import psycopg
import pytest
from mcp import Client

from catalog_schema import initialize_schema
from fixture_postgres import postgres_fixture
from hpc_agent_os.catalog import (
    CatalogConfig,
    CatalogRecord,
    CatalogSnapshot,
    CatalogReader,
    import_snapshot,
)
from hpc_agent_os.catalog_cli import CatalogApplication
from hpc_agent_os.models import Config
from hpc_agent_os.server import build_server


@pytest.fixture
def catalog(tmp_path):
    with postgres_fixture() as pg:

        def connect(role="hpc_fixture_admin"):
            return psycopg.connect(
                host=pg["socket"], dbname="hpc_fixture", user=role, connect_timeout=3
            )

        with connect() as conn:
            initialize_schema(conn)
        cfg = CatalogConfig(
            expected_host="test",
            expected_uid=os.getuid(),
            cluster_id="test",
            public_software_root=str(tmp_path / "software"),
            module_roots=[],
            conda_roots=[],
            database_roots=[str(tmp_path / "private")],
            private_roots=[str(tmp_path / "private")],
        )
        record = CatalogRecord(
            id="checkm2",
            kind="database",
            name="checkm2",
            version="2",
            root=str(tmp_path / "private/checkm2"),
            manager="data",
            type="diamond-index",
            visibility="owner-only",
            owner_uid=os.getuid(),
            version_evidence="manifest-declared",
            observed_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        snapshot = CatalogSnapshot(
            cluster_id="test",
            owner_uid=os.getuid(),
            observed_at=record.observed_at,
            records=[record],
            coverage=[],
        )
        yield connect, cfg, snapshot


def test_atomic_idempotency_history_visibility_and_acl(catalog):
    connect, cfg, snapshot = catalog

    def importer():
        return connect("hpc_catalog_import")

    assert import_snapshot(importer, snapshot, cfg)["inserted"]
    assert not import_snapshot(importer, snapshot, cfg)["inserted"]
    reader = CatalogReader(lambda: connect("hpc_catalog_query"), os.getuid())
    assert reader.search("test", "database", query="check")[0][0]["version"] == "2"
    assert reader.search("test", "database", query="' OR 1=1 --")[0] == []
    assert (
        CatalogReader(lambda: connect("hpc_catalog_query"), os.getuid() + 1).search(
            "test", "database"
        )[0]
        == []
    )
    new = snapshot.model_copy(deep=True)
    new.records[0].version = "3"
    import_snapshot(importer, new, cfg)
    assert reader.search("test", "database")[0][0]["version"] == "3"
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM catalog_snapshot").fetchone()[0] == 2
    bad = snapshot.model_copy(deep=True)
    bad.records[0].visibility = "public-metadata"
    with pytest.raises(ValueError, match="Personal"):
        import_snapshot(importer, bad, cfg)
    assert reader.search("test", "database")[0][0]["version"] == "3"
    for role, query in [
        ("hpc_catalog_query", "DELETE FROM catalog_record"),
        ("hpc_catalog_query", "UPDATE catalog_active SET snapshot_digest='bad'"),
        ("hpc_catalog_import", "DELETE FROM catalog_snapshot"),
    ]:
        with connect(role) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)


@pytest.mark.asyncio
async def test_real_catalog_protocol_and_staleness(catalog):
    connect, cfg, snapshot = catalog
    import_snapshot(lambda: connect("hpc_catalog_import"), snapshot, cfg)
    reader = CatalogReader(lambda: connect("hpc_catalog_query"), os.getuid())
    app = CatalogApplication(Config(cluster_id="test"), registry=reader)
    async with Client(build_server("database", app)) as client:
        result = await client.call_tool("search_databases", {"request": {"cluster_id": "test"}})
        body = result.structured_content
        assert body["source"] == "postgresql-filesystem-inventory" and body["stale"]
        assert body["data"][0]["integrity"] == "not-checked"
        result = await client.call_tool("search_databases", {"request": {"cluster_id": "other"}})
        assert result.structured_content["error"]["code"] == "FORBIDDEN"


def test_query_timestamp_and_rows_share_snapshot_during_import(catalog):
    connect, cfg, snapshot = catalog
    import_snapshot(lambda: connect("hpc_catalog_import"), snapshot, cfg)
    updated = snapshot.model_copy(deep=True)
    updated.observed_at = datetime.now(timezone.utc)
    updated.records[0].version = "3"
    switched = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            row = self.cursor.fetchone()
            import_snapshot(lambda: connect("hpc_catalog_import"), updated, cfg)
            switched.append(True)
            return row

    class Connection:
        def __enter__(self):
            self.conn = connect("hpc_catalog_query")
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            return Cursor(result) if sql.startswith("SELECT s.observed_at") else result

    reader = CatalogReader(Connection, os.getuid())
    rows, _, observed = reader.search_snapshot("test", "database")
    assert switched and rows[0]["version"] == "2" and observed == snapshot.observed_at
    rows, _, observed = CatalogReader(
        lambda: connect("hpc_catalog_query"), os.getuid()
    ).search_snapshot("test", "database")
    assert rows[0]["version"] == "3" and observed == updated.observed_at


def test_relocated_resource_queryable_by_old_and_new_path(catalog):
    import hashlib
    from pathlib import Path
    from hpc_agent_os.catalog import Relocation

    connect, cfg, snapshot = catalog
    record = snapshot.records[0]
    old = record.root
    new = str(Path(old).parent / "new/checkm2")
    cfg.relocations = [Relocation(legacy_root=old, canonical_root=new, backup_root=old + ".backup")]
    cfg = CatalogConfig.model_validate(cfg.model_dump())
    record.root = new
    record.legacy_paths = [old]
    record.id = "observed-" + hashlib.sha256((old + "\0" + record.name).encode()).hexdigest()[:24]
    import_snapshot(lambda: connect("hpc_catalog_import"), snapshot, cfg)
    reader = CatalogReader(lambda: connect("hpc_catalog_query"), os.getuid())
    for query in (old, new):
        rows, _ = reader.search("test", "database", query=query)
        assert len(rows) == 1 and rows[0]["id"] == record.id
        assert rows[0]["visibility"] == "owner-only"
