import argparse
import os
from pathlib import Path
import sys
from pydantic import ValidationError
from .errors import DomainError
from .models import Config
from .registry import RegistryReader, fixture_engine
from .server import build_server
from .service import Application
from .slurm import CachedSlurm, MockAdapter


def main():
    parser = argparse.ArgumentParser(description="Phase 2 isolated read-only MCP (stdio only)")
    parser.add_argument("service", choices=["slurm", "software", "database"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    engine = None
    try:
        config = Config.model_validate_json(args.config.read_text())
        reader = None
        if args.service != "slurm":
            dsn = os.environ.get(config.registry.dsn_secret_ref)
            if not dsn:
                raise ValueError("Missing fixture DSN")
            engine = fixture_engine(dsn, Path.cwd())
            reader = RegistryReader(engine)
        slurm = CachedSlurm(
            MockAdapter(),
            ttl=config.slurm.cache_ttl_seconds,
            max_stale=config.slurm.max_stale_seconds,
            min_interval=0,
        )
        # Mock has no upstream. CLIAdapter's global 30s gate is tested separately.
        build_server(args.service, Application(config, slurm=slurm, registry=reader)).run()
    except (ValidationError, DomainError, ValueError, OSError):
        print("Startup rejected: invalid config or isolated fixture dependency", file=sys.stderr)
        raise SystemExit(2) from None
    finally:
        if engine:
            engine.dispose()


if __name__ == "__main__":
    main()
