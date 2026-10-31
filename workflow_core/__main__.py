"""python -m workflow_core --project PATH <command>."""

import argparse
import json
from pathlib import Path
import sys
import uuid

from .execution import ExecutionService, CodexAdapter
from .store import WorkflowStore, ConflictError, CorruptStateError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Codex workflow CLI")
    parser.add_argument("--project", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize from a reviewed JSON plan")
    init.add_argument("plan")
    run = commands.add_parser("run")
    run.add_argument("task", nargs="?")
    run.add_argument("--model")
    commands.add_parser("status")
    commands.add_parser("stop", help="request cancellation of the active execution")
    gui = commands.add_parser("gui", help="open the local web UI")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true")
    for name in ("verify", "handoff"):
        commands.add_parser(name).add_argument("task")
    commands.add_parser("record", help="retry result recording without executing code")
    commands.add_parser("recover").add_argument("--processes-checked", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "gui":
        from .web import serve
        try:
            explicit_project = any(item == "--project" or item.startswith("--project=") for item in (argv if argv is not None else sys.argv[1:]))
            serve(args.project, args.port, not args.no_browser, Path.cwd() / ".workflow-ui.json", not explicit_project)
            return 0
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    service = ExecutionService(args.project, CodexAdapter(model=getattr(args, "model", None)))
    try:
        if args.command == "init":
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
            # Validate the complete plan before creating the project.
            from .store import _validate
            candidate = {"schema_version": 1, "revision": 0, "project_id": str(uuid.uuid4()),
                         "goal": plan["goal"], "tasks": plan["tasks"], "policy": plan.get("policy", {}),
                         "decisions": plan.get("decisions", []), "open_questions": plan.get("open_questions", [])}
            _validate(candidate)
            for task in candidate["tasks"]:
                WorkflowStore._build_handoff(candidate, task["id"])
            state = service.store.initialize(candidate["goal"], project_id=candidate["project_id"])
            candidate["revision"] = state["revision"]
            result = service.store.update(candidate, state["revision"], str(uuid.uuid4()))
        elif args.command == "status":
            result = service.status()
            # Internal recovery snapshots are not a user-facing status payload.
            if result["execution"]:
                result["execution"].pop("proposed", None)
        elif args.command == "run":
            result = service.run(args.task)
        elif args.command == "verify":
            result = service.verify(args.task)
        elif args.command == "handoff":
            result = service.store.handoff(args.task)
        elif args.command == "record":
            result = service.record()
        elif args.command == "stop":
            result = service.cancel()
        else:
            result = service.recover(args.processes_checked)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command in ("run", "verify", "recover"):
            task_id = getattr(args, "task", None) or service._journal()["task_id"]
            return 0 if service._task(result, task_id)["status"] == "completed" else 1
        return 0
    except (OSError, ValueError, KeyError, StopIteration, ConflictError, CorruptStateError) as exc:
        print(json.dumps({"error": str(exc) or type(exc).__name__,
                          "action": "inspect status; retry record for saved results; recover interrupted executions"},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
