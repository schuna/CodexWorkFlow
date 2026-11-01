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
        for attempt in range(6):
            try:
                os.replace(name, path)
                break
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                    raise
                time.sleep(0.02 * (2 ** attempt))
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
        self.archive_dir = self.root / "archive"
        self.archive_manifest = self.archive_dir / "manifest.json"

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

    def _read_archive_manifest(self):
        if not self.archive_manifest.exists():
            return {"archived_through_revision": -1, "archived_operation_ids": [], "archives": []}
        try:
            manifest = json.loads(self.archive_manifest.read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict)
                    or type(manifest.get("archived_through_revision")) is not int
                    or manifest["archived_through_revision"] < 0
                    or not isinstance(manifest.get("archived_operation_ids"), list)
                    or not isinstance(manifest.get("archives"), list)):
                raise ValueError("invalid archive manifest")
            return manifest
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CorruptStateError("invalid archive manifest") from exc

    @staticmethod
    def _parse_event_line(line):
        try:
            event = json.loads(line)
            payload = event["payload"]
            if event["sha256"] != hashlib.sha256(_json_bytes(payload)).hexdigest():
                raise ValueError("checksum mismatch")
            _validate(payload["state"])
            if not isinstance(payload.get("operation_id"), str) or not payload["operation_id"]:
                raise ValueError("invalid operation id")
            return payload
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise CorruptStateError("invalid committed event") from exc

    def _read_tail_pair(self):
        if not self.events.exists():
            return None
        with open(self.events, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            pos = size
            buf = b""
            while pos > 0 and buf.count(b"\n") < 3:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
        if not buf.endswith(b"\n"):
            return None
        lines = [line for line in buf.splitlines() if line]
        if not lines:
            return None
        last_payload = self._parse_event_line(lines[-1])
        prev_payload = self._parse_event_line(lines[-2]) if len(lines) > 1 else None
        if prev_payload is not None:
            if (last_payload["state"]["revision"] != prev_payload["state"]["revision"] + 1
                    or last_payload["operation_id"] == prev_payload["operation_id"]):
                raise CorruptStateError("invalid committed event")
        else:
            manifest = self._read_archive_manifest()
            if last_payload["state"]["revision"] != manifest["archived_through_revision"] + 1:
                raise CorruptStateError("invalid committed event")
        return last_payload["state"], (prev_payload["state"] if prev_payload is not None else None)

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
        manifest = self._read_archive_manifest()
        base_revision = manifest["archived_through_revision"] + 1
        seen_ops = set(manifest["archived_operation_ids"])
        records = []
        for line in data[:complete].splitlines():
            payload = self._parse_event_line(line)
            if payload["state"]["revision"] != base_revision + len(records):
                raise CorruptStateError("invalid committed event")
            if payload["operation_id"] in seen_ops:
                raise CorruptStateError("invalid committed event")
            seen_ops.add(payload["operation_id"])
            records.append(payload)
        return records

    def _materialize(self, state, previous=None):
        desired = _render(state)
        current = None
        if self.progress.exists():
            current = self.progress.read_bytes()
            if current != desired and (previous is None or current != _render(previous)):
                raise ConflictError("PROGRESS.md was changed externally; resolve before continuing")
        serialized = _json_bytes(state) + b"\n"
        if not self.state_file.exists() or self.state_file.read_bytes() != serialized:
            _atomic_write(self.state_file, serialized)
        if current != desired:
            observed = self.progress.read_bytes() if self.progress.exists() else None
            if observed != current:
                raise ConflictError("PROGRESS.md changed while materializing; review external edits")
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

    def _load_fast(self):
        if not self.events.exists():
            if self.state_file.exists():
                raise CorruptStateError("state exists without event log")
            return None
        if self.state_file.exists():
            pair = self._read_tail_pair()
            if pair is not None:
                state, previous = pair
                if self.state_file.read_bytes() == _json_bytes(state) + b"\n":
                    self._materialize(state, previous)
                    return state
        state, _ = self._load()
        return state

    def _append(self, state, operation_id, kind):
        payload = {"event_id": str(uuid.uuid4()), "operation_id": operation_id,
                   "type": kind, "timestamp": time.time(), "state": state}
        event = {"payload": payload, "sha256": hashlib.sha256(_json_bytes(payload)).hexdigest()}
        with open(self.events, "ab") as f:
            f.write(_json_bytes(event) + b"\n")
            f.flush()
            os.fsync(f.fileno())

    def _compact_unlocked(self, retain):
        raw_lines = self.events.read_bytes().splitlines() if self.events.exists() else []
        manifest = self._read_archive_manifest()
        if len(raw_lines) <= retain:
            return {"compacted": 0, "retained": len(raw_lines),
                    "archived_through_revision": manifest["archived_through_revision"]}
        events = self._read_events()
        cut = len(events) - retain
        archived_events = events[:cut]
        archive_lines = raw_lines[:cut]
        keep_lines = raw_lines[cut:]
        start_rev = archived_events[0]["state"]["revision"]
        end_rev = archived_events[-1]["state"]["revision"]
        archive_name = f"events-{start_rev}-{end_rev}.jsonl"
        _atomic_write(self.archive_dir / archive_name, b"\n".join(archive_lines) + b"\n")
        manifest["archived_through_revision"] = end_rev
        manifest["archived_operation_ids"] = (
            manifest["archived_operation_ids"] + [e["operation_id"] for e in archived_events]
        )[-1000:]
        manifest["archives"].append({"file": archive_name, "start_revision": start_rev,
                                     "end_revision": end_rev, "count": cut})
        _atomic_write(self.archive_manifest, _json_bytes(manifest) + b"\n")
        _atomic_write(self.events, b"\n".join(keep_lines) + b"\n")
        return {"compacted": cut, "retained": len(keep_lines), "archived_through_revision": end_rev}

    def compact_events(self, retain=50):
        if type(retain) is not int or retain < 2:
            raise ValueError("retain must be an integer >= 2")
        with self._lock():
            state, _ = self._load()
            if state is None:
                raise FileNotFoundError("workflow project is not initialized")
            return self._compact_unlocked(retain)

    def initialize(self, goal, project_id=None, operation_id=None, imported_progress_sha256=None, progress_note=None):
        with self._lock():
            state, _ = self._load()
            if state is not None:
                raise ConflictError("project already initialized")
            if self.progress.exists():
                external = self.progress.read_bytes()
                if imported_progress_sha256 is None:
                    raise ConflictError("existing PROGRESS.md requires manual import")
                if hashlib.sha256(external).hexdigest() != imported_progress_sha256:
                    raise ConflictError("existing PROGRESS.md changed; review again")
                _atomic_write(self.root / "conflicts" / (str(uuid.uuid4()) + ".md"), external)
            elif imported_progress_sha256 is not None:
                raise ConflictError("existing PROGRESS.md changed; review again")
            if progress_note is not None and not isinstance(progress_note, str):
                raise ValueError("progress_note must be text")
            state = {"schema_version": 1, "revision": 0, "project_id": project_id or str(uuid.uuid4()),
                     "goal": goal, "tasks": [], "decisions": [progress_note.strip()] if progress_note and progress_note.strip() else [], "open_questions": [], "policy": {}}
            _validate(state)
            self._append(state, operation_id or str(uuid.uuid4()), "initialized")
            if imported_progress_sha256 is not None:
                if hashlib.sha256(self.progress.read_bytes()).hexdigest() != imported_progress_sha256:
                    raise ConflictError("existing PROGRESS.md changed while importing; review again")
                _atomic_write(self.progress, _render(state))
            self._materialize(state)
            return copy.deepcopy(state)

    def read(self):
        with self._lock():
            state = self._load_fast()
            if state is None:
                raise FileNotFoundError("workflow project is not initialized")
            return copy.deepcopy(state)

    def inspect_progress(self):
        """Read authoritative state and both document versions without overwriting edits."""
        with self._lock():
            pair = self._read_tail_pair() if self.state_file.exists() else None
            if pair is not None and self.state_file.read_bytes() == _json_bytes(pair[0]) + b"\n":
                state = pair[0]
            else:
                events = self._read_events()
                if not events:
                    raise FileNotFoundError("workflow project is not initialized")
                state = events[-1]["state"]
            external = self.progress.read_bytes() if self.progress.exists() else b""
            generated = _render(state)
            return {"state": copy.deepcopy(state), "generated": generated.decode("utf-8"),
                    "external": external.decode("utf-8", errors="replace"),
                    "external_sha256": hashlib.sha256(external).hexdigest(),
                    "conflict": external != generated}

    def resolve_progress(self, expected_revision, external_sha256, note=None):
        with self._lock():
            events = self._read_events()
            if not events:
                raise FileNotFoundError("workflow project is not initialized")
            current = events[-1]["state"]
            external = self.progress.read_bytes() if self.progress.exists() else b""
            if current["revision"] != expected_revision or hashlib.sha256(external).hexdigest() != external_sha256:
                raise ConflictError("document or state changed; review both versions again")
            if note is not None and not isinstance(note, str):
                raise ValueError("note must be text")
            # Back up the exact edited bytes before the explicitly authorized replacement.
            _atomic_write(self.root / "conflicts" / (str(uuid.uuid4()) + ".md"), external)
            proposed = copy.deepcopy(current)
            if note and note.strip():
                proposed["decisions"].append(note.strip())
            proposed["revision"] += 1
            _validate(proposed)
            observed = self.progress.read_bytes() if self.progress.exists() else b""
            if hashlib.sha256(observed).hexdigest() != external_sha256:
                raise ConflictError("document changed while resolving; review again")
            _atomic_write(self.progress, _render(current))
            self._append(proposed, str(uuid.uuid4()), "progress_conflict_resolved")
            self._materialize(proposed, current)
            return copy.deepcopy(proposed)

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
            if operation_id in self._read_archive_manifest()["archived_operation_ids"]:
                raise ConflictError("operation id already archived")
            if current["revision"] != expected_revision:
                raise ConflictError("revision changed")
            proposed = copy.deepcopy(state)
            if proposed.get("project_id") != current["project_id"]:
                raise ValueError("project_id cannot change")
            proposed["revision"] = current["revision"] + 1
            _validate(proposed)
            self._append(proposed, operation_id, "state_updated")
            self._materialize(proposed, current)
            max_retained = proposed.get("policy", {}).get("max_events_retained")
            if type(max_retained) is int and max_retained >= 2 and len(events) + 1 > max_retained:
                self._compact_unlocked(max_retained)
            return copy.deepcopy(proposed)

    def handoff(self, task_id):
        with self._lock():
            state = self._load_fast()
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
        """Return a compact task specification and a single copy of handoff evidence."""
        return self.build_context(self.read(), task_id)

    @classmethod
    def build_context(cls, state, task_id):
        task = next((item for item in state["tasks"] if item["id"] == task_id), None)
        if task is None:
            raise KeyError(task_id)
        handoff = copy.deepcopy(cls._build_handoff(state, task_id))
        # Runtime accounting stays in the authoritative state, not the next prompt.
        spec = {key: copy.deepcopy(task[key]) for key in
                ("id", "title", "status", "acceptance_criteria", "dependencies", "verification_commands", "user_notes")
                if key in task}
        failures = handoff["failed_approaches"]
        recent = []
        for failure in failures[-3:]:
            if not isinstance(failure, dict):
                recent.append(str(failure)[:500])
                continue
            compact = {key: copy.deepcopy(failure[key]) for key in
                       ("run_id", "status", "reason", "exit_code", "summary", "verification_failures") if key in failure}
            if any(check.get("run_id") == failure.get("run_id") for check in handoff["verification"]):
                compact.pop("verification_failures", None)
                compact["verification_ref"] = "handoff.verification"
            if isinstance(compact.get("summary"), str) and len(compact["summary"]) > 500:
                compact["summary"] = compact["summary"][:500]
                compact["summary_truncated"] = True
            recent.append(compact)
        handoff["failed_approaches"] = recent
        for check in handoff["verification"]:
            diagnostics = check.get("diagnostics")
            if isinstance(diagnostics, dict) and len(diagnostics.get("excerpt", "")) > 1200:
                diagnostics["excerpt"] = diagnostics["excerpt"][-1200:]
                diagnostics["truncated"] = True
        by_id = {item["id"]: item for item in state["tasks"] if isinstance(item, dict) and "id" in item}
        dependency_results = []
        for dep_id in task.get("dependencies", []):
            dep = by_id.get(dep_id)
            if dep is None:
                continue
            last_result = dep.get("last_result") if isinstance(dep.get("last_result"), dict) else {}
            summary = last_result.get("summary")
            entry = {"id": dep["id"], "title": dep.get("title", ""), "status": dep.get("status", ""),
                     "changed_files": copy.deepcopy(dep.get("changed_files", [])), "summary": summary}
            if isinstance(summary, str) and len(summary) > 500:
                entry["summary"] = summary[:500]
                entry["summary_truncated"] = True
            dependency_results.append(entry)
        return {"task": spec, "handoff": handoff, "dependency_results": dependency_results,
                "sources": {"state_revision": state["revision"], "instructions": "loaded by execution adapter",
                            "full_task_history": {"path": ".codex-workflow/state.json", "task_id": task_id},
                            "omitted_execution_count": len(task.get("executions", [])),
                            "omitted_failure_count": max(0, len(failures) - len(recent))}}
