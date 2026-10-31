"""Shared synchronous execution service. No shell interpolation or automatic reruns."""

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from .store import WorkflowStore, ConflictError, _atomic_write, _json_bytes
from .discovery import find_codex
from .diagnostics import failure_evidence
from .telemetry import project_usage


def process(argv, cwd, timeout=None, stdin=None, cancel_check=None):
    """Spool transient output to disk; retain only bounded output in memory."""
    if cancel_check and cancel_check():
        return {"exit_code": None, "reason": "cancelled", "stdout": "",
                "output_truncated": False, "requires_process_review": False}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                 stdout=out, stderr=err, start_new_session=os.name != "nt")
        reason = None
        process_review = False
        started = time.monotonic()
        try:
            data = stdin.encode("utf-8") if stdin is not None else None
            while True:
                if cancel_check and cancel_check():
                    reason = "cancelled"
                    raise subprocess.TimeoutExpired(argv, timeout)
                if timeout is not None and time.monotonic() - started >= timeout:
                    reason = "timeout"
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    child.communicate(data, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    data = None
        except (subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as exc:
            reason = reason or ("timeout" if isinstance(exc, subprocess.TimeoutExpired) else "cancelled" if isinstance(exc, KeyboardInterrupt) else "process_control_error")
            if os.name == "nt":
                try:
                    killed = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=5)
                    process_review = killed.returncode != 0
                except (OSError, subprocess.TimeoutExpired):
                    process_review = True
                if child.poll() is None:
                    child.kill()
            else:
                import signal
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        out.seek(0, 2)
        size = out.tell()
        out.seek(max(0, size - 4 * 1024 * 1024))
        stdout = out.read().decode("utf-8", errors="replace")
        err.seek(0)
        err.seek(0, os.SEEK_END)
        err.seek(max(0, err.tell() - 64 * 1024))
        stderr = err.read().decode("utf-8", errors="replace")
        # Callers persist only selected, sanitized verification evidence.
        return {"exit_code": child.returncode, "reason": reason, "stdout": stdout,
                "stderr": stderr,
                "output_truncated": size > 4 * 1024 * 1024, "requires_process_review": process_review}


def git_snapshot(project):
    def git(*args):
        result = subprocess.run(["git", *args], cwd=project, capture_output=True, timeout=15)
        return result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else None
    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status is None:
        raise ValueError("execution requires a Git working tree")
    files = {}
    # Include hashes so modifications to an already dirty file are observable.
    entries = iter(status.rstrip("\0").split("\0"))
    for entry in entries:
        if not entry:
            continue
        name = entry[3:]
        if "R" in entry[:2] or "C" in entry[:2]:
            next(entries, None)
        if name.startswith(".codex-workflow/") or name == "PROGRESS.md":
            continue
        path = Path(project) / name
        files[name] = {"status": entry[:2], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
                       if path.is_file() and not path.is_symlink() else None}
    return {"head": head.strip() if head else None, "files": files}


class CodexAdapter:
    def __init__(self, executable="codex", model=None):
        self.executable, self.model = executable, model
        self.cancel_check = None

    def execute(self, project, context, timeout):
        executable = find_codex(self.executable)
        if not executable:
            raise FileNotFoundError("Codex 실행 파일을 찾지 못했습니다. Codex 설치 위치와 접근 권한을 확인하세요.")
        criteria = context["task"]["acceptance_criteria"]
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"summary": {"type": "string"}, "needs_input": {"type": "boolean"},
                                 "criteria_met": {"type": "array", "items": {"type": "boolean"}}},
                  "required": ["summary", "needs_input", "criteria_met"]}
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            argv = [executable, "exec", "--ephemeral", "--json", "--sandbox", "workspace-write",
                    "--output-schema", str(schema_path)]
            if self.model:
                argv += ["--model", self.model]
            argv += ["-"]
            prompt = ("Perform this task. Follow applicable AGENTS.md instructions. Do not modify "
                      ".codex-workflow or PROGRESS.md. Do not create Git commits. Return criteria_met in acceptance_criteria order; "
                      "report needs_input for unanswered questions or permissions.\n" + json.dumps(context, ensure_ascii=False))
            result = process(argv, project, timeout, prompt, cancel_check=self.cancel_check)
            result.pop("stderr", None)
        completed, failed, final, usage, thread = False, False, None, None, None
        for line in result.pop("stdout").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                failed = True
                continue
            if not isinstance(event, dict):
                failed = True
                continue
            kind = event.get("type")
            if kind == "thread.started":
                thread = event.get("thread_id")
            elif kind in ("turn.failed", "error"):
                failed = True
            elif kind == "turn.completed":
                completed, usage = True, event.get("usage")
            elif kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                final = event["item"].get("text")
        try:
            answer = json.loads(final)
            valid = (isinstance(answer, dict) and isinstance(answer.get("summary"), str)
                     and type(answer.get("needs_input")) is bool
                     and isinstance(answer.get("criteria_met"), list)
                     and len(answer["criteria_met"]) == len(criteria)
                     and all(type(x) is bool for x in answer["criteria_met"]))
        except (TypeError, ValueError):
            answer, valid = None, False
        known_usage = None
        if isinstance(usage, dict):
            known_usage = {key: usage.get(key) if type(usage.get(key)) is int and usage[key] >= 0 else None
                           for key in ("input_tokens", "cached_input_tokens", "output_tokens")}
        if not result["reason"]:
            result["reason"] = ("nonzero_exit" if result["exit_code"] != 0 else
                                "output_too_large" if result["output_truncated"] else
                                "execution_failed" if failed else "incomplete_response" if not completed else
                                "invalid_response" if not valid else None)
        return {**result, "ok": result["exit_code"] == 0 and completed and not failed and valid
                and not result["output_truncated"] and not result["reason"],
                "answer": answer if valid else None, "usage": known_usage,
                "usage_source": "codex turn.completed", "model": self.model, "thread_id": thread}


class ExecutionService:
    def __init__(self, project, adapter=None):
        self.store = WorkflowStore(project)
        self.adapter = adapter or CodexAdapter()
        self.guard = WorkflowStore(project)
        self.guard.lock_file = self.store.root / "execution.lock"
        self.journal = self.store.root / "execution.json"
        self.cancel_file = self.store.root / "cancel.json"

    def _cancel_requested(self, run_id):
        try:
            return json.loads(self.cancel_file.read_text(encoding="utf-8")).get("run_id") == run_id
        except (FileNotFoundError, ValueError):
            return False

    def cancel(self):
        record = self._journal()
        if not record or record["phase"] not in ("running", "verifying"):
            raise ValueError("no running process to stop")
        try:
            with self.guard._lock(timeout=0):
                raise ValueError("execution is interrupted; review processes and recover")
        except TimeoutError:
            pass
        _atomic_write(self.cancel_file, _json_bytes({"run_id": record["run_id"], "requested": time.time()}))
        return {"run_id": record["run_id"], "stop_requested": True}

    def _save(self, record):
        _atomic_write(self.journal, _json_bytes(record) + b"\n")

    def _journal(self):
        return json.loads(self.journal.read_text(encoding="utf-8")) if self.journal.exists() else None

    def status(self):
        state = self.store.read()
        record = self._journal()
        blocked = record and record["phase"] != "recorded"
        candidates = [t for t in state["tasks"] if self._ready(state, t)]
        blockers = {t["id"]: reason for t in candidates if (reason := self._run_blocker(state, t))}
        ready = [] if blocked else [t["id"] for t in candidates if t["id"] not in blockers]
        action = ("record" if record["phase"] == "recording" else "inspect execution; recover only after process review") if blocked else "run" if ready else "review project"
        task_usage, totals = project_usage(state)
        return {"state": state, "execution": record, "ready": ready, "next_action": action, "run_blockers": blockers,
                "task_usage": task_usage, "project_usage": totals}

    @classmethod
    def _run_blocker(cls, state, task):
        try:
            cls._validate_execution(task, state["policy"])
        except ValueError as exc:
            return str(exc)
        if task.get("attempts", 0) >= 1 + state["policy"].get("max_retries", 1):
            return "task retry limit reached"
        cap = state["policy"].get("task_token_limit")
        history = task.get("executions", [])
        if cap is not None:
            if any(x.get("tokens") is None for x in history):
                return "usage unknown; cannot enforce task token limit"
            if sum(x["tokens"] for x in history) >= cap:
                return "task token limit reached"
        return None

    @staticmethod
    def _validate_execution(task, policy):
        if type(policy.get("max_retries", 1)) is not int or policy.get("max_retries", 1) < 0:
            raise ValueError("invalid max_retries")
        for key in ("max_seconds", "verification_seconds", "task_token_limit", "context_char_limit"):
            value = policy.get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError("invalid policy " + key)
        commands = task.get("verification_commands", [])
        if not isinstance(commands, list) or any(not isinstance(c, list) or not c or
                not all(isinstance(a, str) and a for a in c) for c in commands):
            raise ValueError("verification_commands must be argument arrays")
        return commands

    @staticmethod
    def _ready(state, task):
        done = {t["id"] for t in state["tasks"] if t["status"] == "completed"}
        return task["status"] in ("ready", "failed", "needs_input", "unverified", "interrupted") and set(task["dependencies"]) <= done

    @staticmethod
    def _task(state, task_id):
        return next(t for t in state["tasks"] if t["id"] == task_id)

    def run(self, task_id=None, expected_revision=None):
        with self.guard._lock(timeout=0):
            previous = self._journal()
            if previous and previous["phase"] != "recorded":
                raise ConflictError("unfinished execution: use record or recover; execution will not repeat")
            state = self.store.read()
            if expected_revision is not None and state["revision"] != expected_revision:
                raise ConflictError("revision changed; refresh before starting")
            choices = [t for t in state["tasks"] if self._ready(state, t) and (task_id is None or t["id"] == task_id)]
            if len(choices) != 1:
                raise ValueError("select one ready task: " + ", ".join(t["id"] for t in choices))
            task = choices[0]
            policy = state["policy"]
            commands = self._validate_execution(task, policy)
            if not task["acceptance_criteria"]:
                raise ValueError("acceptance criteria required before execution")
            attempts = task.get("attempts", 0)
            blocker = self._run_blocker(state, task)
            if blocker:
                raise ValueError(blocker)
            context = self.store.context(task["id"])
            size = len(json.dumps(context, ensure_ascii=False))
            if policy.get("context_char_limit") is not None and size > policy["context_char_limit"]:
                raise ValueError("context character limit reached")
            record = {"run_id": str(uuid.uuid4()), "task_id": task["id"], "phase": "running",
                      "started": time.time(), "source_revision": state["revision"],
                      "context_chars": size, "baseline": git_snapshot(self.store.project)}
            self._save(record)
            task["status"], task["attempts"] = "running", attempts + 1
            started = self.store.update(state, state["revision"], record["run_id"] + ":start")
            record["started_revision"] = started["revision"]
            self._save(record)
            try:
                if isinstance(self.adapter, CodexAdapter):
                    self.adapter.cancel_check = lambda: self._cancel_requested(record["run_id"])
                result = self.adapter.execute(self.store.project, context, policy.get("max_seconds"))
            except (OSError, ValueError) as exc:
                result = {"ok": False, "reason": type(exc).__name__, "usage": None, "answer": None}
            record["result"] = result
            record["phase"] = "verifying"
            self._save(record)
            checks = self._verify(commands, record, policy) if result["ok"] else []
            record.update(checks=checks, after=git_snapshot(self.store.project), phase="recording", finished=time.time())
            if result.get("requires_process_review") or any(c.get("requires_process_review") for c in checks):
                record["phase"] = "needs_process_review"
                self._save(record)
                raise ConflictError("process tree cleanup unconfirmed; inspect processes then recover")
            self._save(record)
            return self._record(record)

    def _verify(self, commands, record, policy):
        checks = []
        for command in commands:
            try:
                result = process(command, self.store.project, policy.get("verification_seconds", 300),
                                 cancel_check=lambda: self._cancel_requested(record["run_id"]))
                passed = result["exit_code"] == 0 and result["reason"] is None
                code, reason = result["exit_code"], result["reason"]
                process_review = result.get("requires_process_review", False)
                diagnostics = failure_evidence(result.get("stdout", ""), result.get("stderr", ""), reason) if not passed else None
            except OSError as exc:
                passed, code, reason = False, None, type(exc).__name__
                process_review = False
                diagnostics = failure_evidence("", str(exc), reason)
            checks.append({"command": command, "passed": passed, "exit_code": code, "reason": reason,
                           "run_id": record["run_id"], "source_revision": record["source_revision"],
                           "timestamp": time.time(), "requires_process_review": process_review})
            if diagnostics:
                checks[-1]["diagnostics"] = diagnostics
            if reason in ("cancelled", "timeout"):
                break
        return checks

    def _record(self, record):
        # Persist a deterministic proposed state before committing. A retry uses the same
        # operation ID and content even if append succeeded but materialization failed.
        if "proposed" not in record:
            state = self.store.read()
            if state["revision"] != record.get("started_revision", record["source_revision"] + 1):
                raise ConflictError("state changed during execution; resolve revision conflict before recording")
            task = self._task(state, record["task_id"])
            result = record["result"]
            answer = result.get("answer") or {}
            status = "failed"
            if result.get("reason") in ("interrupted", "cancelled"):
                status = "interrupted"
            elif record.get("force_failed"):
                status = "failed"
            elif any(c.get("reason") == "cancelled" for c in record["checks"]):
                status = "interrupted"
            elif record.get("verify_only"):
                status = task["status"]
                if not record["checks"]:
                    status = "unverified"
                elif not all(c["passed"] for c in record["checks"]):
                    status = "failed"
            elif result.get("ok"):
                status = "needs_input" if answer.get("needs_input") else "failed"
                if not answer.get("needs_input") and task["acceptance_criteria"] and all(answer.get("criteria_met", [])):
                    status = "completed" if record["checks"] and all(x["passed"] for x in record["checks"]) else "unverified" if not record["checks"] else "failed"
            before, after = record["baseline"]["files"], record["after"]["files"]
            changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
            if record.get("verify_only"):
                changed.update(task["changed_files"])
            task.update(status=status, verification=record["checks"],
                        changed_files=sorted(changed),
                        next_action="select next ready task" if status == "completed" else "review result before retry")
            if not record.get("verify_only") or "last_result" not in task:
                task["last_result"] = {"summary": answer.get("summary"), "reason": result.get("reason"),
                                       "exit_code": result.get("exit_code"), "run_id": record["run_id"]}
            failed_checks = [c for c in record["checks"] if not c["passed"]]
            if status == "failed" and failed_checks:
                task["last_result"]["reason"] = "verification_failed"
            if status in ("failed", "interrupted", "needs_input"):
                failure = {**task["last_result"], "run_id": record["run_id"], "status": status}
                if failed_checks:
                    failure["verification_failures"] = [
                        {"command": c["command"], "exit_code": c["exit_code"],
                         "summary": c.get("diagnostics", {}).get("summary") or c.get("reason") or "Verification failed"}
                        for c in failed_checks]
                task["failed_approaches"].append(failure)
            usage = result.get("usage") or {}
            tokens = (usage["input_tokens"] + usage["output_tokens"]
                      if usage.get("input_tokens") is not None and usage.get("output_tokens") is not None else None)
            execution = {"run_id": record["run_id"], "tokens": tokens,
                "status": status,
                "usage": result.get("usage"), "usage_source": result.get("usage_source"),
                "model": result.get("model"), "timestamp": record["finished"],
                "elapsed_seconds": record["finished"] - record["started"],
                "over_token_limit": tokens is not None and state["policy"].get("task_token_limit") is not None
                and sum((x.get("tokens") or 0) for x in task.get("executions", [])) + tokens > state["policy"]["task_token_limit"]}
            if not record.get("verify_only"):
                task.setdefault("executions", []).append(execution)
            record["proposed"] = state
            record["phase"] = "recording"
            self._save(record)
        state = self.store.update(record["proposed"], record["proposed"]["revision"], record["run_id"] + ":record")
        self.store.handoff(record["task_id"])
        record["phase"] = "recorded"
        self._save(record)
        return state

    def record(self):
        with self.guard._lock(timeout=0):
            record = self._journal()
            if not record or record["phase"] not in ("recording", "recorded"):
                raise ValueError("no completed result to record; use recover for interrupted execution")
            return self._record(record)

    def reconcile_record(self, expected_revision):
        """User-reviewed rebase records a stale execution as failed, never completed."""
        with self.guard._lock(timeout=0):
            record = self._journal()
            if not record or record["phase"] != "recording":
                raise ValueError("no result awaiting recording")
            state = self.store.read()
            if state["revision"] != expected_revision:
                raise ConflictError("revision changed")
            record.pop("proposed", None)
            record["started_revision"] = state["revision"]
            record["force_failed"] = True
            record["result"].update(ok=False, reason="state_changed; recorded after user review")
            self._save(record)
            return self._record(record)

    def recover(self, processes_checked=False):
        with self.guard._lock(timeout=0):
            record = self._journal()
            if not record or record["phase"] in ("recording", "recorded"):
                raise ValueError("use record for a finished result")
            if not processes_checked:
                raise ValueError("check remaining processes and partial changes, then pass --processes-checked")
            if "started_revision" not in record:
                revision = self.store.read()["revision"]
                if revision in (record["source_revision"], record["source_revision"] + 1):
                    # Start may have failed before or just after its event commit.
                    record["started_revision"] = revision
            record.update(result={**record.get("result", {}), "ok": False, "reason": "interrupted"},
                          checks=record.get("checks", []),
                          after=git_snapshot(self.store.project), finished=time.time(), phase="recording")
            self._save(record)
            return self._record(record)

    def verify(self, task_id, expected_revision=None):
        """Re-run checks without asserting acceptance or silently completing a task."""
        with self.guard._lock(timeout=0):
            record = self._journal()
            if record and record["phase"] != "recorded":
                raise ConflictError("finish recording or recovery first")
            state = self.store.read()
            if expected_revision is not None and state["revision"] != expected_revision:
                raise ConflictError("revision changed; refresh before verifying")
            task = self._task(state, task_id)
            commands = self._validate_execution(task, state["policy"])
            record = {"run_id": str(uuid.uuid4()), "task_id": task_id, "phase": "verifying",
                      "baseline": git_snapshot(self.store.project), "source_revision": state["revision"],
                      "started_revision": state["revision"], "started": time.time(), "verify_only": True,
                      "result": {"ok": True}}
            self._save(record)
            checks = self._verify(commands, record, state["policy"])
            record.update(checks=checks, after=git_snapshot(self.store.project), finished=time.time(), phase="recording")
            if any(c.get("requires_process_review") for c in checks):
                record["phase"] = "needs_process_review"
                self._save(record)
                raise ConflictError("verification process tree cleanup unconfirmed; inspect processes then recover")
            self._save(record)
            return self._record(record)
