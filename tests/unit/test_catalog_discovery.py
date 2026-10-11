import json
import os
from pathlib import Path

import pytest

from hpc_agent_os.catalog import CatalogConfig, CatalogRecord
from hpc_agent_os.discovery import Discovery


def fixture_config(tmp_path):
    return CatalogConfig(
        expected_host="test",
        expected_uid=os.getuid(),
        public_software_root=str(tmp_path / "software"),
        module_roots=[str(tmp_path / "software/modulefiles")],
        conda_roots=[str(tmp_path / "private/conda")],
        database_roots=[str(tmp_path / "private/databases")],
        private_roots=[str(tmp_path / "private")],
    )


def test_metadata_only_discovery_and_private_database(tmp_path):
    cfg = fixture_config(tmp_path)
    bwa = Path(cfg.public_software_root) / "bwa/0.7.18/bin"
    bwa.mkdir(parents=True)
    tool = bwa / "bwa"
    tool.write_text("must never execute")
    tool.chmod(0o700)
    module = Path(cfg.module_roots[0]) / "bwa/0.7.18"
    module.parent.mkdir(parents=True)
    module.write_text("set root " + str(bwa.parent))
    env = Path(cfg.conda_roots[0])
    (env / "conda-meta").mkdir(parents=True)
    (env / "bin").mkdir()
    exe = env / "bin/checkm2"
    exe.write_text("must never execute")
    exe.chmod(0o700)
    (env / "conda-meta/checkm2.json").write_text(
        json.dumps(
            {
                "name": "checkm2",
                "version": "1.1.0",
                "files": ["bin/checkm2"],
                "url": "https://secret:password@example.org",
                "token": "private",
            }
        )
    )
    db = Path(cfg.database_roots[0])
    (db / "CheckM2_database").mkdir(parents=True)
    artifact = db / "CheckM2_database/uniref100.KO.1.dmnd"
    artifact.write_bytes(b"not-read")
    (db / "CONTENTS.json").write_text(
        json.dumps({"version": "2", "/CheckM2_database/uniref100.KO.1.dmnd": "a" * 32})
    )
    (db / "archive.partial").write_bytes(b"partial")
    snapshot = Discovery(cfg).scan()
    data = [r for r in snapshot.records if r.kind == "database"][0]
    assert data.version == "2" and data.visibility == "owner-only" and not data.verified
    assert data.content_digest is None and data.size_bytes == 8
    assert data.declared_checksums == {"uniref100.KO.1.dmnd": "md5:" + "a" * 32}
    sw = [r for r in snapshot.records if r.name == "bwa"][0]
    assert sw.module_names == ["bwa/0.7.18"]
    encoded = snapshot.model_dump_json()
    assert "password" not in encoded and "token" not in encoded and "not-read" not in encoded
    assert any(c["state"] == "partial-download-excluded" for c in snapshot.coverage)


def test_discovery_does_not_follow_symlinks_or_fifo(tmp_path):
    cfg = fixture_config(tmp_path)
    root = Path(cfg.public_software_root)
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    scan = Discovery(cfg)
    assert not scan.scan().records
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        scan.metadata(fifo)


def test_declared_checksum_never_claims_verified_content(tmp_path):
    from datetime import datetime, timezone

    with pytest.raises(ValueError):
        CatalogRecord(
            id="x",
            kind="database",
            name="x",
            version="unknown",
            root=str(tmp_path),
            manager="data",
            type="sequence",
            visibility="owner-only",
            owner_uid=os.getuid(),
            version_evidence="unknown",
            observed_at=datetime.now(timezone.utc),
            content_digest="a" * 64,
        )


def test_module_exact_literal_and_bounded_evidence(tmp_path):
    cfg = fixture_config(tmp_path)
    root = Path(cfg.public_software_root)
    for v in ("1.2", "1.20"):
        (root / "tool" / v).mkdir(parents=True)
    modules = Path(cfg.module_roots[0]) / "tool"
    modules.mkdir(parents=True)
    for n in range(12):
        (modules / str(n)).write_text(
            "set root " + str(root / "tool/1.20") + "\n# " + str(root / "tool/1.2")
        )
    snapshot = Discovery(cfg).scan()
    old = next(r for r in snapshot.records if r.version == "1.2")
    new = next(r for r in snapshot.records if r.version == "1.20")
    assert old.module_names == [] and len(new.module_names) == 12 and len(new.evidence_paths) <= 8
    from hpc_agent_os.catalog import CatalogSnapshot

    CatalogSnapshot.model_validate_json(snapshot.model_dump_json())
