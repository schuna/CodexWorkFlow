import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workflow_core import WorkflowStore, ConflictError
from workflow_core.execution import CodexAdapter, ExecutionService, process


class FakeAdapter:
    def __init__(self, ok=True):
        self.calls = 0
        self.ok = ok

    def execute(self, project, context, timeout):
        self.calls += 1
        (Path(project) / "result.txt").write_text("changed", encoding="utf-8")
        return {"ok": self.ok, "answer": {"criteria_met": [True], "needs_input": False},
                "usage": {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 2}}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        subprocess.run(["git", "init", "-q", self.tmp.name], check=True)
        self.adapter = FakeAdapter()
        self.service = ExecutionService(self.tmp.name, self.adapter)
        self.store = self.service.store
        state = self.store.initialize("goal")
        state["tasks"] = [{"id": "one", "title": "work", "status": "ready", "dependencies": [],
            "acceptance_criteria": ["works"], "constraints": [], "changed_files": [],
            "failed_approaches": [], "next_action": "run", "verification": [],
            "verification_commands": [[sys.executable, "-c", "pass"]]}]
        self.store.update(state, 0, "plan")

    def change(self, fn):
        state = self.store.read()
        fn(state)
        self.store.update(state, state["revision"], "change-" + str(state["revision"]))

    def test_success_records_handoff_changes_and_actual_usage(self):
        result = self.service.run()
        task = result["tasks"][0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["changed_files"], ["result.txt"])
        self.assertEqual(task["executions"][0]["tokens"], 12)
        self.assertEqual(self.service._journal()["phase"], "recorded")
        self.assertEqual(len(list((self.store.root / "handoffs").glob("*/*.json"))), 1)
        self.assertEqual(self.service.status()["ready"], [])

    def test_failures_and_missing_verification_never_complete(self):
        self.adapter.ok = False
        self.assertEqual(self.service.run()["tasks"][0]["status"], "failed")
        self.adapter.ok = True
        self.change(lambda s: s["tasks"][0].update(verification_commands=[]))
        self.assertEqual(self.service.run()["tasks"][0]["status"], "unverified")
        with self.assertRaisesRegex(ValueError, "retry limit"):
            self.service.run()

    def test_failed_check_is_failed(self):
        self.change(lambda s: s["tasks"][0].update(verification_commands=[[sys.executable, "-c", "raise SystemExit(7)"]]))
        task = self.service.run()["tasks"][0]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["verification"][0]["exit_code"], 7)

    def test_recording_retry_never_executes_again_even_after_commit(self):
        original = self.store._materialize
        def fail_final(state, previous=None):
            if state["tasks"] and state["tasks"][0]["status"] == "completed":
                raise OSError("disk unavailable")
            return original(state, previous)
        with patch.object(self.store, "_materialize", side_effect=fail_final):
            with self.assertRaises(OSError):
                self.service.run()
        self.assertEqual(self.service._journal()["phase"], "recording")
        with self.assertRaises(ConflictError):
            self.service.run()
        self.assertEqual(self.service.record()["tasks"][0]["status"], "completed")
        self.service.record()
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(len(self.store.read()["tasks"][0]["executions"]), 1)

    def test_duplicate_start_blocked_by_execution_lock(self):
        other = ExecutionService(self.tmp.name, FakeAdapter())
        with self.service.guard._lock():
            with self.assertRaises(TimeoutError):
                other.run()
        self.assertEqual(other.adapter.calls, 0)

    def test_crash_requires_process_review_and_never_auto_repeats(self):
        with patch.object(self.adapter, "execute", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.service.run()
        with self.assertRaises(ConflictError):
            self.service.run()
        with self.assertRaises(ValueError):
            self.service.recover()
        self.assertEqual(self.service.recover(True)["tasks"][0]["status"], "interrupted")
        self.assertEqual(self.service.run()["tasks"][0]["status"], "completed")

    def test_verify_does_not_complete_unexecuted_task_or_charge_tokens(self):
        task = self.service.verify("one")["tasks"][0]
        self.assertEqual(task["status"], "ready")
        self.assertNotIn("executions", task)
        self.assertEqual(self.adapter.calls, 0)

    def test_uncertain_cleanup_requires_review(self):
        original = self.adapter.execute
        def uncertain(*args):
            return {**original(*args), "ok": False, "requires_process_review": True}
        with patch.object(self.adapter, "execute", side_effect=uncertain):
            with self.assertRaises(ConflictError):
                self.service.run()
        self.assertEqual(self.service._journal()["phase"], "needs_process_review")
        with self.assertRaises(ConflictError):
            self.service.run()
        self.assertEqual(self.service.recover(True)["tasks"][0]["status"], "interrupted")

    def test_revision_change_during_execution_blocks_completion(self):
        original = self.adapter.execute
        def changed(*args):
            self.change(lambda s: s.update(goal="new goal"))
            return original(*args)
        with patch.object(self.adapter, "execute", side_effect=changed):
            with self.assertRaises(ConflictError):
                self.service.run()
        self.assertEqual(self.store.read()["tasks"][0]["status"], "running")

    def test_start_record_failure_before_commit_can_be_recovered(self):
        with patch.object(self.store, "update", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.run()
        self.assertEqual(self.adapter.calls, 0)
        self.assertEqual(self.service.recover(True)["tasks"][0]["status"], "interrupted")

    def test_limits_checked_before_execution(self):
        self.change(lambda s: s["policy"].update(context_char_limit=1))
        with self.assertRaisesRegex(ValueError, "character limit"):
            self.service.run()
        self.assertEqual(self.adapter.calls, 0)

    def test_token_limit_stops_next_call(self):
        self.change(lambda s: s["policy"].update(task_token_limit=1))
        self.adapter.ok = False
        task = self.service.run()["tasks"][0]
        self.assertTrue(task["executions"][0]["over_token_limit"])
        with self.assertRaisesRegex(ValueError, "token limit"):
            self.service.run()
        self.assertEqual(self.adapter.calls, 1)

    def test_timeout_preserves_partial_output(self):
        result = process([sys.executable, "-u", "-c", "import time; print('partial'); time.sleep(30)"],
                         self.tmp.name, 0.5)
        self.assertEqual(result["reason"], "timeout")
        self.assertIn("partial", result["stdout"])

    def test_adapter_requires_completion_and_valid_criteria(self):
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(
            {"summary": "done", "needs_input": False, "criteria_met": [True]})}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}}]
        def output(items):
            return {"exit_code": 0, "reason": None, "output_truncated": False,
                    "stdout": "\n".join(json.dumps(e) for e in items)}
        adapter = CodexAdapter(executable=sys.executable)
        with patch("workflow_core.execution.process", return_value=output(events)):
            result = adapter.execute(self.tmp.name, self.store.context("one"), None)
            self.assertTrue(result["ok"])
            self.assertIsNone(result["usage"]["cached_input_tokens"])
        for items in (events[:1], events + [{"type": "turn.failed"}], [{"type": "turn.completed"}]):
            with patch("workflow_core.execution.process", return_value=output(items)):
                self.assertFalse(adapter.execute(self.tmp.name, self.store.context("one"), None)["ok"])

    def test_cli_status_and_handoff(self):
        for command in (["status"], ["handoff", "one"]):
            result = subprocess.run([sys.executable, "-m", "workflow_core", "--project", self.tmp.name, *command],
                                    capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)

    def test_cli_unicode_json_round_trip_with_ascii_output_encoding(self):
        self.change(lambda state: state.update(goal="한국어 목표 😀"))
        env = {**os.environ, "PYTHONIOENCODING": "ascii"}
        for command in (["status"], ["handoff", "one"], ["handoff", "없는 작업 😀"]):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-m", "workflow_core", "--project", self.tmp.name, *command],
                    capture_output=True, env=env)
                self.assertEqual(result.returncode, 2 if command[-1] == "없는 작업 😀" else 0)
                payload = json.loads((result.stderr if result.returncode else result.stdout).decode("ascii"))
                if result.returncode:
                    self.assertIn("없는 작업 😀", payload["error"])
                elif command[0] == "status":
                    self.assertEqual(payload["state"]["goal"], "한국어 목표 😀")
                else:
                    self.assertEqual(payload["goal"], "한국어 목표 😀")

    def test_process_streams_stdout_lines_and_adapter_reports_redacted_progress(self):
        lines = []
        result = process([sys.executable, "-u", "-c", "import time; print('first', flush=True); time.sleep(0.25); print('second', flush=True)"],
                         self.tmp.name, 5, on_stdout_line=lines.append)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(lines, ["first", "second"])
        events = [
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "curl -H 'Authorization: Bearer secret-bearer-123' https://example.test"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(
                {"summary": "completed safely", "needs_input": False, "criteria_met": [True]})}},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
        ]
        updates = []
        adapter = CodexAdapter(executable=sys.executable)
        adapter.on_progress = updates.append
        with patch("workflow_core.execution.process", return_value={
            "exit_code": 0, "reason": None, "output_truncated": False,
            "stdout": "\n".join(json.dumps(e) for e in events)
        }):
            res = adapter.execute(self.tmp.name, self.store.context("one"), None)
        self.assertTrue(res["ok"])
        self.assertEqual(len(updates), 4)
        self.assertEqual(updates[0]["last_event_type"], "thread.started")
        self.assertIn("명령 실행 중", updates[1]["message"])
        self.assertNotIn("secret-bearer-123", json.dumps(updates))
        self.assertEqual(updates[2]["message"], "completed safely")
        self.assertEqual(updates[-1]["event_count"], 4)


if __name__ == "__main__":
    unittest.main()
