"""Bounded metadata-only discovery. Never source modulefiles or execute discovered tools."""

from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import stat
import time

from .catalog import CatalogRecord, CatalogSnapshot
from .process import BoundedRunner


def identifier(value):
    value = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-_.")
    return value[:64] or "unknown"


class Discovery:
    def __init__(self, cfg):
        self.cfg = cfg
        self.started = time.monotonic()
        self.observed = datetime.now(timezone.utc)
        self.coverage, self.records = [], []
        self.files = 0

    def budget(self):
        if time.monotonic() - self.started > self.cfg.scan_timeout_seconds:
            raise TimeoutError("Metadata scan time budget exhausted")

    def entries(self, path):
        self.budget()
        path = Path(path)
        if path.resolve() != path:
            self.coverage.append({"path": str(path), "state": "symlink-skipped"})
            return []
        try:
            with os.scandir(path) as stream:
                items = list(itertools.islice(stream, self.cfg.max_directory_entries + 1))
            complete = len(items) <= self.cfg.max_directory_entries
            self.coverage.append(
                {"path": str(path), "state": "complete" if complete else "truncated"}
            )
            excluded = {r.backup_root for r in self.cfg.relocations}
            # A copied target is inactive until the old path is switched. This also
            # makes a filesystem rollback immediately discoverable without duplicate IDs.
            excluded.update(
                r.canonical_root
                for r in self.cfg.relocations
                if Path(r.legacy_root).is_dir() and not Path(r.legacy_root).is_symlink()
            )
            return sorted(
                [
                    Path(x.path)
                    for x in items[: self.cfg.max_directory_entries]
                    if x.path not in excluded
                ],
                key=str,
            )
        except OSError as exc:
            self.coverage.append({"path": str(path), "state": type(exc).__name__})
            return []

    def metadata(self, path):
        self.budget()
        self.files += 1
        if self.files > self.cfg.max_metadata_files:
            raise ValueError("Metadata file budget exhausted")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                raise ValueError("Metadata must be a bounded regular file")
            with os.fdopen(os.dup(fd), "rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Metadata grew beyond limit")
            return raw.decode("utf-8")
        finally:
            os.close(fd)

    def record(self, root, name, version="unknown", **fields):
        root = Path(root)
        private = any(root.is_relative_to(Path(p)) for p in self.cfg.private_roots)
        name = identifier(name)
        legacy = self.cfg.legacy_paths(root)
        identity = hashlib.sha256(
            ((legacy[0] if legacy else str(root)) + "\0" + name).encode()
        ).hexdigest()[:24]
        values = dict(
            id="observed-" + identity,
            kind="software",
            name=name,
            version=version[:128],
            root=str(root),
            legacy_paths=legacy,
            manager="existing",
            type="installation",
            visibility="owner-only" if private else "public-metadata",
            owner_uid=self.cfg.expected_uid if private else None,
            observed_at=self.observed,
            version_evidence="unknown",
            evidence_paths=[str(root)],
        )
        values.update(fields)
        self.records.append(CatalogRecord(**values))

    def public_software(self):
        for package in self.entries(self.cfg.public_software_root):
            if (
                package.name in ("modulefiles", "users")
                or package.is_symlink()
                or not package.is_dir()
            ):
                continue
            children = self.entries(package)
            versions = [
                p
                for p in children
                if not p.is_symlink() and p.is_dir() and re.search(r"(^|[-_])\d+\.\d+", p.name)
            ]
            for root in versions or [package]:
                bins = self.entries(root / "bin") if (root / "bin").is_dir() else []
                executables = [str(p) for p in bins if p.is_file() and os.access(p, os.X_OK)][:8]
                notes = ["Executable presence is not a runtime compatibility test."]
                if not executables:
                    notes.append(
                        "No bin executable observed; may be an archive, collection, or incomplete installation."
                    )
                self.record(
                    root,
                    package.name,
                    root.name if versions else "unknown",
                    version_evidence="directory-name" if versions else "unknown",
                    executables=executables,
                    notes=notes,
                )

    def modules(self):
        for directory in self.cfg.module_roots:
            for group in self.entries(directory):
                if group.is_symlink() or not group.is_dir():
                    continue
                for path in self.entries(group):
                    if path.name.startswith(".") or path.is_symlink() or not path.is_file():
                        continue
                    try:
                        content = self.metadata(path)
                    except (OSError, ValueError, UnicodeError):
                        self.coverage.append({"path": str(path), "state": "metadata-unreadable"})
                        continue
                    matched = False
                    for record in self.records:
                        # Conservative literal root match only; Tcl/Lua is never evaluated.
                        literal = re.compile(
                            r"^\s*set\s+\w+\s+(?:"
                            + re.escape(record.root)
                            + r'|"'
                            + re.escape(record.root)
                            + r'"|\{'
                            + re.escape(record.root)
                            + r"\})\s*(?:#.*)?$",
                            re.MULTILINE,
                        )
                        if literal.search(content) and len(record.module_names) < 16:
                            matched = True
                            record.module_names.append(group.name + "/" + path.name)
                            if (
                                str(path) not in record.evidence_paths
                                and len(record.evidence_paths) < 8
                            ):
                                record.evidence_paths.append(str(path))
                            note = "Module literal root reference only; module not executed."
                            if note not in record.notes:
                                record.notes.append(note)
                    if not matched:
                        self.record(
                            path,
                            group.name,
                            path.name,
                            type="module-entry",
                            version_evidence="directory-name",
                            module_names=[group.name + "/" + path.name],
                            notes=[
                                "Module file found in configured search root; Tcl/Lua not evaluated.",
                                "Module filename version only; runtime and dependencies untested.",
                            ],
                        )

    def conda(self):
        roots = []
        for configured in self.cfg.conda_roots:
            base = Path(configured)
            roots.append(base)
            roots.extend(
                p for p in self.entries(base / "envs") if not p.is_symlink() and p.is_dir()
            )
        for root in roots:
            if root.resolve() != root or not (root / "conda-meta").is_dir():
                continue
            self.record(
                root,
                "environment-" + root.name,
                manager="conda",
                type="environment",
                notes=[
                    "Environment label is not a software version; individual CLI packages are separate records."
                ],
            )
            for path in self.entries(root / "conda-meta"):
                if path.suffix != ".json" or path.is_symlink():
                    continue
                try:
                    package = json.loads(self.metadata(path))
                    name = package["name"]
                    version = package["version"]
                    if not isinstance(name, str) or not isinstance(version, str):
                        raise ValueError("Invalid package name/version")
                    # Register command-bearing packages, not every dependency library.
                    bins = []
                    for relative in package.get("files", []):
                        if (
                            not isinstance(relative, str)
                            or not relative.startswith("bin/")
                            or ".." in Path(relative).parts
                        ):
                            continue
                        candidate = root / relative
                        if candidate.is_file() and os.access(candidate, os.X_OK):
                            bins.append(str(candidate))
                            if len(bins) == 8:
                                break
                    if not bins:
                        continue
                    self.record(
                        root,
                        name,
                        version,
                        manager="conda",
                        type="cli-package",
                        version_evidence="package-metadata",
                        evidence_paths=[str(path)],
                        executables=bins,
                        license=str(package.get("license") or "unknown")[:256],
                        notes=["Conda metadata version; executables not run."],
                    )
                except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                    self.coverage.append({"path": str(path), "state": "metadata-unreadable"})

    def databases(self):
        for configured in self.cfg.database_roots:
            base = Path(configured)
            entries = self.entries(base)
            manifest = {}
            metadata = base / "CONTENTS.json"
            if metadata in entries:
                try:
                    manifest = json.loads(self.metadata(metadata))
                    if not isinstance(manifest, dict):
                        manifest = {}
                except (ValueError, OSError, UnicodeError):
                    self.coverage.append({"path": str(metadata), "state": "metadata-unreadable"})
            for path in entries:
                if path.is_symlink():
                    continue
                if path.is_file():
                    if ".partial" in path.name:
                        self.coverage.append(
                            {
                                "path": str(path),
                                "state": "partial-download-excluded",
                                "size_bytes": path.stat().st_size,
                            }
                        )
                    continue
                if not path.is_dir():
                    continue
                files = [p for p in self.entries(path) if p.is_file() and not p.is_symlink()]
                sequences = [
                    p
                    for p in files
                    if p.suffix.lower()
                    in (
                        ".dmnd",
                        ".fa",
                        ".fasta",
                        ".fna",
                        ".faa",
                        ".pin",
                        ".nin",
                        ".hmm",
                        ".h3f",
                        ".h3i",
                        ".h3m",
                        ".h3p",
                        ".k2d",
                    )
                ]
                if not sequences:
                    self.coverage.append(
                        {"path": str(path), "state": "database-candidate-unclassified"}
                    )
                    continue
                known = path.name == "CheckM2_database"
                kraken = any(p.suffix == ".k2d" for p in sequences)
                if kraken and not {"hash.k2d", "opts.k2d", "taxo.k2d"} <= {
                    p.name for p in sequences
                }:
                    self.coverage.append({"path": str(path), "state": "incomplete-kraken2-index"})
                    continue
                declared = {}
                for file in sequences[:8]:
                    value = manifest.get("/" + path.name + "/" + file.name)
                    if isinstance(value, str) and re.fullmatch("[0-9a-fA-F]{32}", value):
                        declared[file.name] = "md5:" + value.lower()
                version = str(manifest.get("version", "unknown")) if known else "unknown"
                evidence = "manifest-declared" if known and version != "unknown" else "unknown"
                dated = re.search(r"_(\d{8})$", path.name) if kraken else None
                if dated:
                    version, evidence = dated[1], "directory-name"
                self.record(
                    path,
                    "checkm2" if known else path.name,
                    version,
                    kind="database",
                    manager="data",
                    type="diamond-index"
                    if any(p.suffix == ".dmnd" for p in sequences)
                    else "kraken2-index"
                    if kraken
                    else "hmm-profiles"
                    if any(p.suffix == ".hmm" for p in sequences)
                    else "sequence-files",
                    version_evidence=evidence,
                    evidence_paths=([str(metadata)] if known and metadata.is_file() else [])
                    + [str(p) for p in sequences[:7]],
                    declared_checksums=declared,
                    size_bytes=sum(p.stat().st_size for p in sequences),
                    size_scope="listed-files-only",
                    notes=[
                        "Sequence contents and full-file hashes not read; declared checksum is not verification.",
                        "Index builder version and source release not established.",
                    ],
                )

    def system_packages(self):
        if not self.cfg.include_system_packages:
            return
        tools = {
            "apptainer": "/usr/bin/apptainer",
            "environment-modules": "/usr/bin/modulecmd",
            "slurm": "/usr/bin/sinfo",
        }
        try:
            output = BoundedRunner(timeout=5, max_bytes=8192).run(
                ("/usr/bin/rpm", "-q", "--qf", "%{NAME}|%{VERSION}|%{RELEASE}\n", *tools)
            )
            for line in output.splitlines():
                name, version, release = line.split("|")
                if name in tools and Path(tools[name]).is_file():
                    self.record(
                        Path(tools[name]),
                        name,
                        version,
                        type="host-tool",
                        version_evidence="package-metadata",
                        executables=[tools[name]],
                        notes=[
                            "Local RPM metadata only; release " + release + "; binary not executed."
                        ],
                    )
        except Exception:
            self.coverage.append({"path": "/usr/bin/rpm", "state": "system-package-query-failed"})

    def scan(self):
        self.system_packages()
        self.public_software()
        self.modules()
        self.conda()
        self.databases()
        result = CatalogSnapshot(
            cluster_id=self.cfg.cluster_id,
            owner_uid=self.cfg.expected_uid,
            observed_at=self.observed,
            records=self.records,
            coverage=self.coverage,
        )
        return CatalogSnapshot.model_validate_json(result.model_dump_json())
