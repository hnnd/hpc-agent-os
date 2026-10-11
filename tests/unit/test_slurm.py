import concurrent.futures
import time
from unittest.mock import Mock

import pytest
from hpc_agent_os.errors import DomainError
from hpc_agent_os.models import Config, ErrorCode
from hpc_agent_os.service import Application
from hpc_agent_os.slurm import (
    CLIAdapter,
    CachedSlurm,
    MockAdapter,
    parse_jobs,
    parse_nodes,
    parse_partitions,
)


@pytest.mark.parametrize(
    "text",
    [
        "bad",
        "a|cpu|idle|-1|100|",
        "a|cpu|idle|2|invalid|",
        "a|cpu|idle|2|100||unknown",
        "a|cpu|idle|2|100|\na|gpu|idle|3|100|",
    ],
)
def test_parse_failure(text):
    with pytest.raises(DomainError):
        parse_nodes(text)


def test_heterogeneous_dedup_memory_units():
    nodes = parse_nodes("a|cpu*|idle*|2|1024|\na|other|idle*|2|1024|\nb|cpu|mixed|4|2048|gpu:1")
    assert len(nodes) == 2
    assert sum(n["cpus"] for n in nodes) == 6
    assert sum(n["memory_mib"] for n in nodes) == 3072
    assert nodes[0]["partitions"] == ["cpu", "other"]
    assert nodes[0]["state"] == "IDLE"


def test_empty_and_unknown_state():
    assert parse_jobs("", 1) == []
    assert parse_nodes("a|cpu|newstate|1|1|")[0]["state"] == "UNKNOWN"
    assert parse_partitions("cpu*|up|infinite\ncpu|up|infinite") == [
        {"name": "cpu", "availability": "up", "max_time": "infinite"}
    ]


def test_job_owner_reason_and_array():
    result = parse_jobs("12_3|1|PENDING|1|cpu|private-sample-name\n14|2|RUNNING|2|cpu|None", 1)
    assert len(result) == 1 and result[0]["job_id"] == "12_3"
    assert result[0]["reason"] == "REDACTED"


def test_cli_fixed_allowlist():
    runner = Mock()
    runner.run.return_value = ""
    adapter = CLIAdapter(runner)
    assert adapter.fetch("jobs", 123) == []
    assert runner.run.call_args.args[0] == (
        "/usr/bin/squeue",
        "--noheader",
        "--array",
        "--user",
        "123",
        "--format=%i|%U|%T|%C|%P|%r",
    )
    with pytest.raises(DomainError):
        adapter.fetch("shell", 0)


def test_cache_stale_and_rate_limit():
    now = [0.0]
    adapter = MockAdapter()
    cache = CachedSlurm(adapter, clock=lambda: now[0])
    original = cache.get("c", "nodes", 1)
    assert cache.get("c", "nodes", 2).observed_at == original.observed_at
    assert adapter.calls == 1
    with pytest.raises(DomainError):
        cache.get("c", "jobs", 1)
    now[0] = 61
    adapter.failure = DomainError(ErrorCode.TIMEOUT, "Command timed out")
    stale = cache.get("c", "nodes", 1)
    assert stale.stale and stale.error.code == "TIMEOUT"
    now[0] = 62
    assert cache.get("c", "nodes", 1).stale
    assert adapter.calls == 2
    now[0] = 400
    with pytest.raises(DomainError):
        cache.get("c", "nodes", 1)


def test_job_cache_identity():
    cache = CachedSlurm(MockAdapter(), min_interval=0)
    assert cache.get("c", "jobs", 1).data[0]["owner_uid"] == 1
    assert cache.get("c", "jobs", 2).data[0]["owner_uid"] == 2
    assert cache.adapter.calls == 2


def test_errors_not_empty_and_injection(capsys):
    adapter = MockAdapter()
    adapter.failure = DomainError(ErrorCode.UNAVAILABLE, "Command unavailable")
    app = Application(Config(), slurm=CachedSlurm(adapter), uid=1)
    result = app.call("list_nodes", {"cluster_id": "fixture-cluster"})
    assert result["data"] is None and result["error"]["code"] == "UNAVAILABLE"
    assert app.call("list_nodes", {"cluster_id": "other"})["error"]["code"] == "FORBIDDEN"
    assert app.call("exec", {"command": "SECRET"})["error"]["code"] == "FORBIDDEN"
    assert "SECRET" not in capsys.readouterr().err


def test_my_job_enforcement():
    adapter = MockAdapter()
    adapter.fetch = lambda kind, uid: [{"owner_uid": 2, "job_id": "100", "state": "RUNNING"}]
    app = Application(Config(), slurm=CachedSlurm(adapter), uid=1)
    assert (
        app.call("get_my_job", {"cluster_id": "fixture-cluster", "job_id": "100"})["error"]["code"]
        == "FORBIDDEN"
    )


def test_audit_failure_denies_query():
    audit = Mock()
    audit.emit.side_effect = DomainError(ErrorCode.UNAVAILABLE, "Audit unavailable")
    cache = CachedSlurm(MockAdapter())
    result = Application(Config(), slurm=cache, audit=audit).call(
        "list_my_jobs", {"cluster_id": "fixture-cluster"}
    )
    assert result["data"] is None
    assert cache.adapter.calls == 0


def test_10x_single_flight_p95():
    adapter = MockAdapter()
    base = adapter.fetch("nodes", 1)
    adapter.calls = 0

    def fetch(kind, uid):
        adapter.calls += 1
        return [{**base[0], "name": f"node{i}"} for i in range(130)]

    adapter.fetch = fetch
    cache = CachedSlurm(adapter)

    def call(_):
        start = time.monotonic()
        assert len(cache.get("c", "nodes", 1).data) == 130
        return time.monotonic() - start

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        times = list(pool.map(call, range(100)))
    assert adapter.calls == 1
    assert sorted(times)[94] < 1
