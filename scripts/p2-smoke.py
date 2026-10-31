"""One real Codex invocation in an isolated repository; retain only summary/state.

Run from the repository root: python scripts/p2-smoke.py
"""

import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflow_core import ExecutionService


project = Path.cwd() / ("p2-results-" + time.strftime("%Y%m%d-%H%M%S"))
project.mkdir()
subprocess.run(["git", "init", "-q", str(project)], check=True)
service = ExecutionService(project)
state = service.store.initialize("P2 adapter smoke test")
state["policy"] = {"max_seconds": 30, "verification_seconds": 10, "max_retries": 0}
state["tasks"] = [{"id": "smoke", "title": "Read-only response check",
    "status": "ready", "dependencies": [], "acceptance_criteria": ["No source changes are needed"],
    "constraints": ["Do not modify files or use tools. Report the criterion as satisfied."],
    "verification": [], "verification_commands": [[sys.executable, "-c", "pass"]],
    "changed_files": [], "failed_approaches": [], "next_action": "respond"}]
service.store.update(state, 0, "smoke-plan")
try:
    result = service.run()
    task = result["tasks"][0]
    summary = {"status": task["status"], "execution": task.get("executions"),
               "verification": task["verification"]}
except Exception as exc:
    summary = {"error": type(exc).__name__, "message": str(exc)}
summary["project"] = str(project)
(project / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
sys.exit(0 if summary.get("status") == "completed" else 1)
