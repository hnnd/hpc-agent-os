"""Owner-bound real software/database MCP, reusing existing read-only contracts."""

import argparse
import os
from pathlib import Path
from datetime import datetime, timezone

from .catalog import CatalogReader, connect, load_config
from .models import Config, GetReleaseRequest, SearchRequest
from .server import build_server
from .service import Application


class CatalogApplication(Application):
    def call(self, tool, arguments):
        result = super().call(tool, arguments)
        result["source"] = "postgresql-filesystem-inventory"
        if result["observed_at"] is None:
            result["stale"] = True
        return result

    def _registry(self, tool, request, result):
        kind = "software" if "software" in tool else "database"
        params = request.model_dump(exclude={"cluster_id", "cursor"})
        if isinstance(request, SearchRequest):
            params["offset"] = int(request.cursor or "0")
        rows, cursor, observed = self.registry.search_snapshot(request.cluster_id, kind, **params)
        result.data = (
            (rows[0] if rows else None) if isinstance(request, GetReleaseRequest) else rows
        )
        result.next_cursor = cursor
        result.observed_at = observed
        result.stale = (
            observed is None or (datetime.now(timezone.utc) - observed).total_seconds() > 86400
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["software", "database"])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    cfg = load_config(args.root)
    reader = CatalogReader(lambda: connect(args.root), os.getuid())
    app = CatalogApplication(Config(cluster_id=cfg.cluster_id), registry=reader)
    build_server(args.kind, app, version="0.5.2").run()


if __name__ == "__main__":
    main()
