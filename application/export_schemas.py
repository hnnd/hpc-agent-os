"""Export application schemas for the public software snapshot."""

import json
from pathlib import Path
from hpc_agent_os_app.plugins import Plugin, Verification, Provenance

ROOT = Path(__file__).resolve().parent
for name, model in (("plugin", Plugin), ("verification", Verification), ("provenance", Provenance)):
    (ROOT / "schemas" / f"{name}.schema.json").write_text(
        json.dumps(model.model_json_schema(), indent=2) + "\n"
    )
