import json
import os
import socket
import threading
from datetime import timedelta

import pytest
from pydantic import ValidationError

from hpc_agent_os.errors import DomainError
from hpc_agent_os.live import BridgeSlurm, LiveConfig, dispatch, peer_uid, receive
from hpc_agent_os.models import ErrorCode, utcnow
from hpc_agent_os.slurm import CachedSlurm, MockAdapter


def config():
    return LiveConfig(
        cluster_id="live-test",
        expected_uid=os.getuid(),
        expected_host=socket.gethostname().split(".")[0],
    )


@pytest.mark.parametrize(
    "payload", [b'{"kind":"jobs","uid":0}', b'{"kind":"shell"}', b"{}", b"[]", b"bad"]
)
def test_request_rejected_before_query(payload):
    adapter = MockAdapter()
    with pytest.raises(DomainError):
        dispatch(payload, os.getuid(), config(), CachedSlurm(adapter))
    assert adapter.calls == 0


def test_peer_identity_and_global_gate():
    adapter = MockAdapter()
    cache = CachedSlurm(adapter)
    cfg = config()
    with pytest.raises(DomainError, match="identity"):
        dispatch(b'{"kind":"jobs"}', os.getuid() + 1, cfg, cache)
    assert adapter.calls == 0
    first = dispatch(b'{"kind":"nodes"}', os.getuid(), cfg, cache)
    second = dispatch(b'{"kind":"nodes"}', os.getuid(), cfg, cache)
    assert first == second
    with pytest.raises(DomainError, match="rate limited"):
        dispatch(b'{"kind":"jobs"}', os.getuid(), cfg, cache)
    assert adapter.calls == 1


def test_stale_preserves_observation_and_expires():
    now = [0.0]
    adapter = MockAdapter()
    cache = CachedSlurm(adapter, clock=lambda: now[0])
    cfg = config()
    first = dispatch(b'{"kind":"nodes"}', os.getuid(), cfg, cache)
    now[0] = 61
    adapter.failure = DomainError(ErrorCode.UNAVAILABLE, "Offline")
    stale = dispatch(b'{"kind":"nodes"}', os.getuid(), cfg, cache)
    assert stale["stale"] and stale["observed_at"] == first["observed_at"]
    now[0] = 301
    with pytest.raises(DomainError):
        dispatch(b'{"kind":"nodes"}', os.getuid(), cfg, cache)


def test_peer_credentials_and_wire_limit():
    left, right = socket.socketpair()
    with left, right:
        assert peer_uid(left) == os.getuid()
        right.sendall(b"too large\n")
        with pytest.raises(DomainError, match="limit"):
            receive(left, 4)


@pytest.mark.parametrize(
    "field,value",
    [
        ("allow_mutations", True),
        ("minimum_interval_seconds", 0.0),
        ("expected_uid", 0),
        ("arbitrary_command", "srun"),
    ],
)
def test_config_rejects_unsafe_controls(field, value):
    values = config().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        LiveConfig.model_validate(values)


def test_wrong_host_and_request_identity():
    cfg = config()
    cfg.expected_host = "other-host"
    with pytest.raises(DomainError):
        cfg.check_identity()
    with pytest.raises(DomainError):
        BridgeSlurm(cfg).get(cfg.cluster_id, "jobs", os.getuid() + 1)


@pytest.mark.parametrize("mode", ["valid", "expired", "invalid", "missing", "stale"])
def test_socket_client_validates_response(tmp_path, monkeypatch, mode):
    cfg = config()
    address = str(tmp_path / "bridge.sock")
    monkeypatch.setattr(LiveConfig, "address", property(lambda self: address))
    if mode == "missing":
        with pytest.raises(DomainError):
            BridgeSlurm(cfg).get(cfg.cluster_id, "nodes", os.getuid())
        return
    response = dispatch(b'{"kind":"nodes"}', os.getuid(), cfg, CachedSlurm(MockAdapter()))
    if mode == "expired":
        response["observed_at"] = (utcnow() - timedelta(seconds=301)).isoformat()
    if mode == "invalid":
        response["data"] = [{"name": "bad"}]
    if mode == "stale":
        response["stale"] = True
        response["error"] = {"code": "UNAVAILABLE", "message": "Offline"}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(address)
        server.listen(1)

        def reply():
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                receive(conn, 256)
                conn.sendall(json.dumps(response).encode() + b"\n")

        worker = threading.Thread(target=reply)
        worker.start()
        try:
            if mode in ("expired", "invalid"):
                with pytest.raises(DomainError):
                    BridgeSlurm(cfg).get(cfg.cluster_id, "nodes", os.getuid())
            else:
                snap = BridgeSlurm(cfg).get(cfg.cluster_id, "nodes", os.getuid())
                assert len(snap.data) == 2
                assert snap.stale == (mode == "stale")
        finally:
            worker.join(timeout=3)
        assert not worker.is_alive()


def test_receive_has_total_deadline(monkeypatch):
    import hpc_agent_os.live as live

    ticks = iter([0.0, 0.1, 0.5, 1.1])
    monkeypatch.setattr(live.time, "monotonic", lambda: next(ticks))

    class SlowConnection:
        def gettimeout(self):
            return 1.0

        def settimeout(self, value):
            assert 0 < value <= 1.0

        def recv(self, size):
            return b"x"

    with pytest.raises(TimeoutError):
        receive(SlowConnection(), 256)


def test_foreign_peer_rejected_before_read(monkeypatch):
    import hpc_agent_os.live as live

    monkeypatch.setattr(live, "peer_uid", lambda _: os.getuid() + 1)
    monkeypatch.setattr(live, "receive", lambda *_: pytest.fail("Read before authorization"))
    with pytest.raises(DomainError, match="identity"):
        live.handle_connection(object(), config(), None)


def test_exported_live_schema_matches():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert (
        json.loads((root / "registry/schemas/live-config.schema.json").read_text())
        == LiveConfig.model_json_schema()
    )
