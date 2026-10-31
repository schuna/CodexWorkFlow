"""Register an already reviewed follow-up plan without running any task."""

import argparse
import copy
import json
from pathlib import Path
import sys
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflow_core import ExecutionService, ConflictError, WorkflowStore
from workflow_core.store import _validate


def register_followups(project, plan, expected_revision, python_executable=None):
    service = ExecutionService(project)
    with service.guard._lock(timeout=0):
        record = service._journal()
        if record and record["phase"] != "recorded":
            raise ConflictError("finish recording or recovery before registering follow-ups")
        state = service.store.read()
        if state["project_id"] != plan["project_id"]:
            raise ValueError("follow-up plan belongs to another project")
        existing = {t["id"] for t in state["tasks"]}
        missing = [item for item in plan["tasks"] if item["id"] not in existing]
        if not missing:
            return state, []
        if state["revision"] != expected_revision:
            raise ConflictError("revision changed; review the current plan before registering")
        proposed = copy.deepcopy(state)
        for item in missing:
            task = {"status": "ready", "verification": [], "changed_files": [],
                    "failed_approaches": [], "next_action": "완료 기준·선행 조건을 확인한 뒤 작업 시작",
                    **copy.deepcopy(item)}
            task["verification_commands"] = [[python_executable or sys.executable if arg == "{python}" else arg
                                              for arg in command] for command in task.get("verification_commands", [])]
            service._validate_execution(task, state["policy"])
            proposed["tasks"].append(task)
        proposed["goal"] = plan["goal"]
        _validate(proposed)
        for task in proposed["tasks"]:
            WorkflowStore._build_handoff(proposed, task["id"])
        result = service.store.update(proposed, state["revision"], str(uuid.uuid4()))
        return result, [t["id"] for t in missing]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--plan", default=str(Path(__file__).resolve().parents[1] / "FOLLOWUP_PLAN.json"))
    parser.add_argument("--expected-revision", required=True, type=int)
    args = parser.parse_args()
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
        state, added = register_followups(args.project, plan, args.expected_revision)
        print(json.dumps({"revision": state["revision"], "added_tasks": added, "executed_tasks": 0}, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, ConflictError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
