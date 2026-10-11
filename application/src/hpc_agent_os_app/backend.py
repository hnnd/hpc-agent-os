"""Reuse the frozen service, catalog and operation boundaries; no parallel executor."""

import os
from hpc_agent_os import operations as ops
from hpc_agent_os.catalog import CatalogReader, connect, load_config
from hpc_agent_os.catalog_cli import CatalogApplication
from hpc_agent_os.live import LiveConfig, BridgeSlurm
from hpc_agent_os.models import Config
from hpc_agent_os.service import Application
from hpc_agent_os.slurm import CachedSlurm, MockAdapter

from .files import read_json


class Backend:
    def __init__(self, root, mode):
        self.root, self.mode = root, mode

    def cluster(self):
        if self.mode == "mock":
            app = Application(
                Config(cluster_id="mock-cluster"), slurm=CachedSlurm(MockAdapter(), min_interval=0)
            )
        else:
            cfg = LiveConfig.model_validate(read_json(self.root / "config/live.json"))
            cfg.check_identity()
            app = Application(cfg, slurm=BridgeSlurm(cfg))
        return app.call("cluster_summary", {"cluster_id": app.config.cluster_id})

    def resource(self, kind, action, parameters):
        if self.mode != "live":
            return {
                "source": "synthetic",
                "data": None,
                "stale": False,
                "error": {"code": "UNAVAILABLE", "message": "Mock catalog not configured"},
            }
        cfg = load_config(self.root)
        reader = CatalogReader(lambda: connect(self.root), os.getuid())
        app = CatalogApplication(Config(cluster_id=cfg.cluster_id), registry=reader)
        tool = {
            ("software", "search"): "search_software",
            ("software", "show"): "get_software_release",
            ("database", "search"): "search_databases",
            ("database", "show"): "get_database_release",
        }[kind, action]
        return app.call(tool, {"cluster_id": cfg.cluster_id, **parameters})

    def components(self, executor=False):
        if self.mode != "live":
            raise ValueError("Operations require explicit live mode")
        policy = ops.load(self.root)
        role = "hpc_ops_executor" if executor else "hpc_ops_api"
        return ops.Planner(self.root, policy), ops.Store(
            lambda: ops.connection(self.root, role), os.getuid()
        )

    def recipes(self):
        planner, _ = self.components()
        return {
            "kind": "maintenance_recipe",
            "recipes": {k: v.model_dump() for k, v in planner.policy.recipes.items()},
        }

    def compile(self, request):
        planner, store = self.components(executor=True)
        if request["operation"] == "job.cancel":
            store.reserve_scheduler_read()
        return planner.compile(request)

    def propose(self, plan):
        planner, store = self.components()
        return store.propose(plan, planner.policy.max_pending)

    def get(self, task_id):
        _, store = self.components()
        return store.get(task_id)

    def execute(self, task_id):
        planner, store = self.components(executor=True)
        if store.get(task_id)["plan"]["request"]["operation"] not in ("job.submit", "job.cancel"):
            raise ValueError("Job command cannot execute maintenance")
        return ops.execute(task_id, planner, store)

    def refresh(self, task_id, job_id=None):
        _, store = self.components(executor=True)
        return ops.refresh(task_id, store, reconcile_job_id=job_id)

    def audit(self, task_id):
        _, store = self.components()
        store.get(task_id)  # Owner scoped, before audit read.
        # Frozen deployment grants INSERT, but not SELECT, on the audit table.
        # Do not silently escalate to an admin role or change production grants.
        try:
            with store.connect() as conn:
                rows = conn.execute(
                    "SELECT action,at FROM operation_audit WHERE task_id=%s "
                    "AND actor=%s ORDER BY at,id LIMIT 1001",
                    (task_id, store.uid),
                ).fetchall()
        except Exception:
            return {
                "status": "unavailable",
                "events": None,
                "reason": "Audit read unavailable under current role; not an empty audit",
            }
        return {
            "status": "available",
            "events": [{"action": row[0], "at": row[1].isoformat()} for row in rows[:1000]],
            "truncated": len(rows) > 1000,
        }

    def doctor(self):
        checks = []
        for name, check in (
            ("slurm_bridge", self.cluster),
            ("catalog", lambda: self.resource("software", "search", {"limit": 1})),
            ("operations", self.check_operations),
        ):
            try:
                result = check()
                error = result.get("error")
                checks.append(
                    {
                        "component": name,
                        "status": "unavailable"
                        if error
                        else "stale"
                        if result.get("stale")
                        else "ok",
                        "source": result.get("source"),
                        "observed_at": result.get("observed_at"),
                        "error_code": error.get("code") if error else None,
                        "capabilities": result.get("capabilities"),
                    }
                )
            except Exception:
                checks.append(
                    {"component": name, "status": "unavailable", "error_code": "CHECK_FAILED"}
                )
        return {
            "mode": self.mode,
            "synthetic": self.mode == "mock",
            "checks": checks,
            "healthy": all(c["status"] == "ok" for c in checks),
        }

    def check_operations(self):
        planner, store = self.components()
        with store.connect() as conn:
            conn.execute(
                "SELECT plan,state,result FROM operation_task WHERE uid=%s LIMIT 0", (store.uid,)
            ).fetchall()
        return {
            "source": "postgresql-operations",
            "capabilities": {
                "jobs_autoexecute": planner.policy.jobs_autoexecute,
                "inbox_access": planner.state.joinpath("inbox").is_dir()
                and os.access(planner.state / "inbox", os.W_OK | os.X_OK),
                "write_probe_performed": False,
            },
        }
