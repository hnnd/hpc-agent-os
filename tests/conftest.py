from pathlib import Path
import pytest

from fixture_postgres import postgres_fixture
from hpc_agent_os.models import Manifest
from hpc_agent_os.registry import fixture_engine, import_manifest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def manifest():
    return Manifest.model_validate_json((ROOT / "tests/fixtures/registry.json").read_text())


@pytest.fixture(scope="session")
def pg(manifest):
    with postgres_fixture() as database:
        engine = fixture_engine(database["import"], ROOT, role="hpc_import")
        try:
            import_manifest(engine, manifest)
        finally:
            engine.dispose()
        yield database
