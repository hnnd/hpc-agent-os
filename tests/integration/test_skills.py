"""Executable scenarios behind Skills; does not claim to test any model's reasoning."""

from pathlib import Path
import pytest
from hpc_agent_os.errors import DomainError
from hpc_agent_os.models import Config, ErrorCode
from hpc_agent_os.registry import RegistryReader, fixture_engine
from hpc_agent_os.service import Application
from hpc_agent_os.slurm import CachedSlurm, MockAdapter

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("tool", ["cluster_summary", "search_software", "search_databases"])
def test_skill_queries_success_denial_and_outage(pg, tool):
    engine = fixture_engine(pg["query"], ROOT)
    adapter = MockAdapter()
    app = Application(
        Config(), slurm=CachedSlurm(adapter, min_interval=0), registry=RegistryReader(engine)
    )
    try:
        good = app.call(tool, {"cluster_id": "fixture-cluster"})
        assert good["observed_at"] and good["request_id"] and good["error"] is None
        if tool == "cluster_summary":
            assert good["source"] == "synthetic-slurm"
        else:
            assert all(r["source"] == "synthetic" and not r["verified"] for r in good["data"])
        denied = app.call(tool, {"cluster_id": "unauthorized"})
        assert denied["error"]["code"] == "FORBIDDEN" and denied["data"] is None

        class Offline:
            def search(self, *args, **kwargs):
                raise DomainError(ErrorCode.UNAVAILABLE, "Registry unavailable")

        adapter.failure = DomainError(ErrorCode.UNAVAILABLE, "Slurm unavailable")
        app = Application(Config(), slurm=CachedSlurm(adapter), registry=Offline())
        failed = app.call(tool, {"cluster_id": "fixture-cluster"})
        assert failed["data"] is None and failed["error"]["code"] == "UNAVAILABLE"
    finally:
        engine.dispose()
