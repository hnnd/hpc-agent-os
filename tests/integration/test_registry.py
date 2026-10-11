import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from hpc_agent_os.errors import DomainError
from hpc_agent_os.models import Config, Manifest
from hpc_agent_os.registry import RegistryReader, fixture_engine, import_manifest
from hpc_agent_os.service import Application

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.postgres


@pytest.fixture
def reader(pg):
    engine = fixture_engine(pg["query"], ROOT)
    yield RegistryReader(engine)
    engine.dispose()


def test_postgres_version_migration_repeat(pg):
    engine = create_engine(pg["admin"])
    try:
        with engine.begin() as conn:
            assert conn.execute(text("SHOW server_version")).scalar().startswith("17.")
            assert conn.execute(text("SHOW listen_addresses")).scalar() == ""
            cfg = AlembicConfig(str(ROOT / "alembic.ini"))
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0001"
    finally:
        engine.dispose()


def test_two_versions_index_and_filter(reader):
    rows, cursor = reader.search("fixture-cluster", "software", limit=1)
    assert len(rows) == 1 and cursor == "1"
    second, _ = reader.search("fixture-cluster", "software", offset=1)
    assert second[0]["version"] != rows[0]["version"]
    rows, _ = reader.search("fixture-cluster", "database", release_id="database-1.0")
    assert rows[0]["indexes"][0]["tool_release_id"] == "software-1.0"
    assert reader.search("fixture-cluster", "software", query="' OR 1=1 --")[0] == []
    assert reader.search("other-cluster", "software")[0] == []
    assert reader.search("fixture-cluster", "database", type="sequence")[0]
    assert reader.search("fixture-cluster", "software", platform="wrong")[0] == []


def test_import_idempotent_atomic_and_immutable(pg, manifest, reader):
    engine = fixture_engine(pg["import"], ROOT, role="hpc_import")
    try:
        assert import_manifest(engine, manifest) == 0
        assert import_manifest(engine, manifest) == 0
        data = manifest.model_dump(mode="json")
        new = {**data["releases"][0], "id": "software-new", "version": "3.0"}
        bad = {**data["releases"][0], "content_digest": "e" * 64}
        data["releases"] = [new, bad]
        with pytest.raises(DomainError):
            import_manifest(engine, Manifest.model_validate_json(json.dumps(data)))
        assert reader.search("fixture-cluster", "software", release_id="software-new")[0] == []
        rows, _ = reader.search("fixture-cluster", "software", release_id="software-1.0")
        assert rows[0]["content_digest"] == "a" * 64
    finally:
        engine.dispose()


def test_bad_index_rolls_back(pg, manifest, reader):
    data = manifest.model_dump(mode="json")
    data["releases"] = [{**data["releases"][0], "id": "uncommitted", "version": "9.0"}]
    data["indexes"][0]["id"] = "bad-index"
    data["indexes"][0]["tool_release_id"] = "database-1.0"
    engine = fixture_engine(pg["import"], ROOT, role="hpc_import")
    try:
        with pytest.raises(DomainError):
            import_manifest(engine, Manifest.model_validate_json(json.dumps(data)))
        assert reader.search("fixture-cluster", "software", release_id="uncommitted")[0] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM release",
        "DELETE FROM release",
        "CREATE TABLE forbidden(id int)",
        "INSERT INTO cluster VALUES ('evil')",
    ],
)
def test_reader_database_permissions(pg, statement):
    engine = fixture_engine(pg["query"], ROOT)
    try:
        with engine.connect() as conn:
            with pytest.raises(DBAPIError):
                conn.execute(text(statement))
    finally:
        engine.dispose()


def test_importer_no_ddl_or_update(pg):
    engine = fixture_engine(pg["import"], ROOT, role="hpc_import")
    try:
        for sql in [
            "CREATE TABLE forbidden(id int)",
            "UPDATE release SET version='bad'",
            "DELETE FROM release",
        ]:
            with engine.connect() as conn:
                with pytest.raises(DBAPIError):
                    conn.execute(text(sql))
    finally:
        engine.dispose()


def test_registry_app_contract_and_unavailable(reader, pg):
    app = Application(Config(), registry=reader)
    result = app.call("search_software", {"cluster_id": "fixture-cluster"})
    assert len(result["data"]) == 2 and result["source"] == "postgresql"
    assert result["observed_at"] and result["error"] is None
    assert (
        app.call("get_database_release", {"cluster_id": "fixture-cluster", "release_id": "absent"})[
            "data"
        ]
        is None
    )
    broken = fixture_engine(pg["query"].replace("/s", "/missing-socket"), ROOT)
    try:
        result = Application(Config(), registry=RegistryReader(broken)).call(
            "search_software", {"cluster_id": "fixture-cluster"}
        )
        assert result["data"] is None and result["error"]["code"] == "UNAVAILABLE"
        assert pg["socket"] not in json.dumps(result)
    finally:
        broken.dispose()
