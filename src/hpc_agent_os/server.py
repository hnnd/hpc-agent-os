"""Three explicit read-only tool catalogs, using the official MCP Python SDK."""

from typing import Any
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from .models import (
    ClusterRequest,
    GetJobRequest,
    GetReleaseRequest,
    JobRequest,
    NodeRequest,
    PageRequest,
    SearchRequest,
)

ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def build_server(kind, app, version="0.2.0"):
    server = MCPServer(name=f"hpc-{kind}", version=version, log_level="ERROR")
    if kind == "slurm":

        @server.tool(annotations=ANNOTATIONS, structured_output=True)
        def cluster_summary(request: ClusterRequest) -> dict[str, Any]:
            """Read timestamped aggregate cluster resources; errors never imply zero capacity."""
            return app.call("cluster_summary", request.model_dump())

        @server.tool(annotations=ANNOTATIONS, structured_output=True)
        def list_nodes(request: NodeRequest) -> dict[str, Any]:
            """Read paginated node resources."""
            return app.call("list_nodes", request.model_dump())

        @server.tool(annotations=ANNOTATIONS, structured_output=True)
        def list_partitions(request: PageRequest) -> dict[str, Any]:
            """Read paginated partition availability."""
            return app.call("list_partitions", request.model_dump())

        @server.tool(annotations=ANNOTATIONS, structured_output=True)
        def list_my_jobs(request: JobRequest) -> dict[str, Any]:
            """Read only the authenticated local user's job summaries."""
            return app.call("list_my_jobs", request.model_dump())

        @server.tool(annotations=ANNOTATIONS, structured_output=True)
        def get_my_job(request: GetJobRequest) -> dict[str, Any]:
            """Read an owned job; unavailable or foreign jobs are denied identically."""
            return app.call("get_my_job", request.model_dump())
    elif kind == "software":

        @server.tool(annotations=ANNOTATIONS, structured_output=True)
        def search_software(request: SearchRequest) -> dict[str, Any]:
            """Search versioned software metadata; never install or execute software."""
            return app.call("search_software", request.model_dump())

        @server.tool(annotations=ANNOTATIONS, structured_output=True)
        def get_software_release(request: GetReleaseRequest) -> dict[str, Any]:
            """Read one software release and its verification status."""
            return app.call("get_software_release", request.model_dump())
    elif kind == "database":

        @server.tool(annotations=ANNOTATIONS, structured_output=True)
        def search_databases(request: SearchRequest) -> dict[str, Any]:
            """Search database versions; never download or publish data."""
            return app.call("search_databases", request.model_dump())

        @server.tool(annotations=ANNOTATIONS, structured_output=True)
        def get_database_release(request: GetReleaseRequest) -> dict[str, Any]:
            """Read a database release and tool-bound index digests."""
            return app.call("get_database_release", request.model_dump())
    else:
        raise ValueError("Unknown service")
    return server
