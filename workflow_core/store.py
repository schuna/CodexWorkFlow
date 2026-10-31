"""A single-writer, event-backed store for workflow state.

The event log is authoritative. Each committed event contains a full snapshot,
so a crash after append can be recovered without replaying application logic.
"""

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


class ConflictError(Exception):
    """The expected revision or generated document changed."""


class CorruptStateError(Exception):
    """A committed event or its snapshot cannot be validated."""


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".workflow-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _validate(state):
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError("unsupported state schema")
    if not isinstance(state.get("revision"), int) or state["revision"] < 0:
        raise ValueError("invalid revision")
    for field in ("project_id", "goal"):
        if not isinstance(state.get(field), str) or (field == "project_id" and not state[field]):
            raise ValueError(f"invalid {field}")
    for field in ("tasks", "decisions", "open_questions"):
        if not isinstance(state.get(field), list):
            raise ValueError(f"invalid {field}")
    if not isinstance(state.get("policy"), dict):
        raise ValueError("invalid policy")
    ids = set()
    for task in state["tasks"]:
        if not isinstance(task, dict):
            raise ValueError("invalid task")
        for field in ("id", "title", "status"):
            if not isinstance(task.get(field), str) or not task[field]:
                raise ValueError(f"invalid task {field}")
        if task["id"] in ids:
            raise ValueError("duplicate task id")
        ids.add(task["id"])
        for field in ("acceptance_criteria", "dependencies"):
            if not isinstance(task.get(field), list) or not all(isinstance(x, str) for x in task[field]):
                raise ValueError(f"invalid task {field}")
        if not isinstance(task.get("verification"), list):
            raise ValueError("invalid task verification")
    for task in state["tasks"]:
        if task["id"] in task["dependencies"] or not set(task["dependencies"]).issubset(ids):
            raise ValueError("invalid task dependency")
    visiting, visited = set(), set()
    by_id = {task["id"]: task for task in state["tasks"]}
    def visit(task_id):
        if task_id in visiting:
            raise ValueError("cyclic task dependencies")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dep in by_id[task_id]["dependencies"]:
            visit(dep)
        visiting.remove(task_id)
        visited.add(task_id)
    for task_id in ids:
        visit(task_id)


def _render(state):
    lines = ["# Project progress", "", f"Goal: {state['goal']}", "", "## Tasks"]
    for task in state["tasks"]:
        lines.append(f"- [{task['status']}] {task['title']} ({task['id']})")
        for check in task["verification"]:
            lines.append("  - Verification: " + json.dumps(check, ensure_ascii=False, sort_keys=True))
    lines += ["", "## Decisions"] + [f"- {x}" for x in state["decisions"]]
    lines += ["", "## Open questions"] + [f"- {x}" for x in state["open_questions"]]
    lines += ["", f"Revision: {state['revision']}", ""]
    return "\n".join(lines).encode("utf-8")


class WorkflowStore:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.root = self.project / ".codex-workflow"
        self.events = self.root / "events.jsonl"
        self.state_file = self.root / "state.json"
        self.progress = self.project / "PROGRESS.md"
        self.lock_file = self.root / "writer.lock"

    @contextlib.contextmanager
    def _lock(self, timeout=5):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.lock_file, "a+b") as f:
            if f.seek(0, os.SEEK_END) == 0:
                f.write(b"\0")
                f.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    f.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("workflow writer lock timed out")
                    time.sleep(0.05)
            try:
                yield
            finally:
                f.seek(0)
                if os.name == "nt":
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _read_events(self):
        if not self.events.exists():
            return []
        data = self.events.read_bytes()
        # An incomplete final line was never committed. Drop it under the writer lock.
        complete = data.rfind(b"\n") + 1
        if complete != len(data):
            with open(self.events, "r+b") as f:
                f.truncate(complete)
                f.flush()
                os.fsync(f.fileno())
        records = []
        for line in data[:complete].splitlines():
            try:
                event = json.loads(line)
                payload = event["payload"]
                if event["sha256"] != hashlib.sha256(_json_bytes(payload)).hexdigest():
                    raise ValueError("checksum mismatch")
                _validate(payload["state"])
                if payload["state"]["revision"] != len(records) or not payload["operation_id"]:
                    raise ValueError("invalid event sequence")
                if any(old["operation_id"] == payload["operation_id"] for old in records):
                    raise ValueError("duplicate operation id")
                records.append(payload)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise CorruptStateError("invalid committed event") from exc
        return records

    def _materialize(self, state, previous=None):
        desired = _render(state)
        if self.progress.exists():
            current = self.progress.read_bytes()
            if current != desired and (previous is None or current != _render(previous)):
                raise ConflictError("PROGRESS.md was changed externally; resolve before continuing")
        _atomic_write(self.state_file, _json_bytes(state) + b"\n")
        _atomic_write(self.progress, desired)

    def _load(self):
        events = self._read_events()
        if not events:
            if self.state_file.exists():
                raise CorruptStateError("state exists without event log")
            return None, events
        state = events[-1]["state"]
        previous = events[-2]["state"] if len(events) > 1 else None
        self._materialize(state, previous)
        return state, events

    def _append(self, state, operation_id, kind):
        payload = {"event_id": str(uuid.uuid4()), "operation_id": operation_id,
                   "type": kind, "timestamp": time.time(), "state": state}
        event = {"payload": payload, "sha256": hashlib.sha256(_json_bytes(payload)).hexdigest()}
        with open(self.events, "ab") as f:
            f.write(_json_bytes(event) + b"\n")
            f.flush()
            os.fsync(f.fileno())

    def initialize(self, goal, project_id=None, operation_id=None):
        with self._lock():
            state, _ = self._load()
            if state is not None:
                raise ConflictError("project already initialized")
            if self.progress.exists():
                raise ConflictError("existing PROGRESS.md requires manual import")
            state = {"schema_version": 1, "revision": 0, "project_id": project_id or str(uuid.uuid4()),
                     "goal": goal, "tasks": [], "decisions": [], "open_questions": [], "policy": {}}
            _validate(state)
            self._append(state, operation_id or str(uuid.uuid4()), "initialized")
            self._materialize(state)
            return copy.deepcopy(state)

    def read(self):
        with self._lock():
            state, _ = self._load()
            if state is None:
                raise FileNotFoundError("workflow project is not initialized")
            return copy.deepcopy(state)

    def update(self, state, expected_revision, operation_id):
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id is required")
        with self._lock():
            current, events = self._load()
            if current is None:
                raise FileNotFoundError("workflow project is not initialized")
            for event in events:
                if event["operation_id"] == operation_id:
                    proposed = copy.deepcopy(state)
                    proposed["revision"] = event["state"]["revision"]
                    if proposed != event["state"]:
                        raise ConflictError("operation id already used for different content")
                    return copy.deepcopy(event["state"])
            if current["revision"] != expected_revision:
                raise ConflictError("revision changed")
            proposed = copy.deepcopy(state)
            if proposed.get("project_id") != current["project_id"]:
                raise ValueError("project_id cannot change")
            proposed["revision"] = current["revision"] + 1
            _validate(proposed)
            self._append(proposed, operation_id, "state_updated")
            self._materialize(proposed, current)
            return copy.deepcopy(proposed)

    def handoff(self, task_id):
        with self._lock():
            state, _ = self._load()
            if state is None:
                raise FileNotFoundError("workflow project is not initialized")
            result = self._build_handoff(state, task_id)
            # Hash the user-controlled ID so it can never escape the handoff directory.
            task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
            path = self.root / "handoffs" / task_key / f"{state['revision']}.json"
            _atomic_write(path, _json_bytes(result) + b"\n")
            return result

    @staticmethod
    def _build_handoff(state, task_id):
        task = next((x for x in state["tasks"] if x["id"] == task_id), None)
        if task is None:
            raise KeyError(task_id)
        fields = ("constraints", "changed_files", "failed_approaches", "next_action")
        if any(field not in task for field in fields):
            raise ValueError("handoff has missing required fields")
        if not all(isinstance(task[field], list) for field in fields[:-1]) or not isinstance(task["next_action"], str):
            raise ValueError("invalid handoff fields")
        return {"schema_version": 1, "task_id": task_id, "source_revision": state["revision"],
                "goal": state["goal"], "constraints": task["constraints"], "decisions": state["decisions"],
                "changed_files": task["changed_files"], "verification": task["verification"],
                "failed_approaches": task["failed_approaches"], "open_questions": state["open_questions"],
                "next_action": task["next_action"]}

    def context(self, task_id):
        """Return task-specific context without historical event payloads or instructions."""
        state = self.read()
        task = next((item for item in state["tasks"] if item["id"] == task_id), None)
        if task is None:
            raise KeyError(task_id)
        task_handoff = self._build_handoff(state, task_id)
        return {"task": copy.deepcopy(task), "handoff": task_handoff,
                "sources": {"state_revision": state["revision"], "instructions": "loaded by execution adapter"}}
