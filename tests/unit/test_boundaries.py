import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from hpc_agent_os.errors import DomainError
from hpc_agent_os.models import Config, GetJobRequest, Manifest, SearchRequest
from hpc_agent_os.process import BoundedRunner
from hpc_agent_os.registry import fixture_engine


@pytest.mark.parametrize(
    "value", ["1;id", "--help", "1$(id)", "0", "1_foo", "1\n2", "1+2", "[1-9]"]
)
def test_invalid_job_ids(value):
    with pytest.raises(ValidationError):
        GetJobRequest(cluster_id="fixture-cluster", job_id=value)


@pytest.mark.parametrize(
    "args",
    [{"limit": 501}, {"limit": "1"}, {"cursor": "-1"}, {"user": "root"}, {"query": "x" * 129}],
)
def test_strict_search(args):
    with pytest.raises(ValidationError):
        SearchRequest(cluster_id="fixture-cluster", **args)


@pytest.mark.parametrize(
    "change",
    [
        {"allow_mutations": True},
        {"transport": "streamable-http"},
        {"slurm": {"mode": "cli"}},
        {"slurm": {"live_access_enabled": True}},
        {"uid": 0},
    ],
)
def test_production_config_rejected(change):
    with pytest.raises(ValidationError):
        Config.model_validate(change)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql+psycopg://hpc_query@production/hpc_fixture",
        "postgresql+psycopg://root@/postgres?host=/tmp",
        "postgresql+psycopg://hpc_query@/hpc_fixture?host=/tmp",
        "postgresql+psycopg://hpc_query@/hpc_fixture?host=/tmp&service=production",
    ],
)
def test_production_dsn_rejected(dsn):
    with pytest.raises(DomainError):
        fixture_engine(dsn, Path.cwd())


@pytest.mark.parametrize(
    "code,expected",
    [
        ("import sys;sys.exit(9)", "UNAVAILABLE"),
        ("import time;time.sleep(5)", "TIMEOUT"),
        ("print('x'*20000)", "OUTPUT_LIMIT"),
        ("import sys;sys.stderr.write('SECRET'*5000)", "OUTPUT_LIMIT"),
        ("import os;os.write(1,b'\\xff')", "PARSE_ERROR"),
    ],
)
def test_bounded_process_errors(code, expected):
    with pytest.raises(DomainError) as error:
        BoundedRunner(timeout=0.2, max_bytes=1024).run((sys.executable, "-c", code))
    assert error.value.code == expected
    assert "SECRET" not in str(error.value)


def test_child_retaining_pipe_is_terminated():
    start = time.monotonic()
    with pytest.raises(DomainError) as error:
        BoundedRunner(timeout=0.2).run(
            (sys.executable, "-c", "import os,time\nif os.fork()==0: time.sleep(30)")
        )
    assert error.value.code == "TIMEOUT"
    assert time.monotonic() - start < 2


def test_success_and_clean_environment(monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "SECRET")
    result = BoundedRunner().run(
        (sys.executable, "-c", "import os;print(os.environ.get('PRIVATE_KEY','absent'))")
    )
    assert result.strip() == "absent"


def test_invalid_manifest_rollout(manifest):
    data = manifest.model_dump(mode="json")
    data["releases"][0]["root"] = "/fixture/../../etc"
    with pytest.raises(ValidationError):
        Manifest.model_validate_json(__import__("json").dumps(data))


@pytest.mark.parametrize(
    "suffix",
    [
        ",db.example.invalid",
        "%2Cdb.example.invalid",
        "%0Ahostaddr=192.0.2.1",
        "&host=db.example.invalid",
    ],
)
def test_libpq_multihost_rejected(suffix):
    root = Path.cwd()
    dsn = f"postgresql+psycopg://hpc_query@/hpc_fixture?host={root}/.runtime/missing{suffix}"
    with pytest.raises(DomainError):
        fixture_engine(dsn, root)


@pytest.mark.parametrize(
    "name", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGPORT", "PGOPTIONS"]
)
def test_libpq_environment_override_rejected(monkeypatch, name):
    root = Path.cwd()
    monkeypatch.setenv(name, "untrusted-override")
    dsn = f"postgresql+psycopg://hpc_query@/hpc_fixture?host={root}/.runtime/socket"
    with pytest.raises(DomainError):
        fixture_engine(dsn, root)


def test_libpq_environment_checked_on_lazy_connect(monkeypatch):
    root = Path.cwd()
    engine = fixture_engine(
        f"postgresql+psycopg://hpc_query@/hpc_fixture?host={root}/.runtime/socket", root
    )
    try:
        monkeypatch.setenv("PGHOSTADDR", "192.0.2.1")
        with pytest.raises(DomainError):
            engine.connect()
    finally:
        engine.dispose()
