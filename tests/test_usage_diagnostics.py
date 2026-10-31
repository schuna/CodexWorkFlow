import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workflow_core import ExecutionService, WorkflowStore
from workflow_core.diagnostics import failure_evidence
from workflow_core.telemetry import project_usage, usage_totals


def entry(input_tokens, output_tokens, cached=0):
    return {"tokens": input_tokens + output_tokens, "usage": {
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cached_input_tokens": cached}}


class UsageTests(unittest.TestCase):
    def test_all_retries_count_cache_is_not_added_again(self):
        result = usage_totals([entry(407058, 4913, 350976), entry(409003, 2864, 358656), entry(430870, 5540, 381184)], 3)
        self.assertEqual(result["total_tokens"], 1260248)
        self.assertEqual(result["input_tokens"], 1246931)
        self.assertEqual(result["cached_input_tokens"], 1090816)
        self.assertEqual(result["output_tokens"], 13317)
        self.assertTrue(result["complete"])

    def test_unknown_usage_stays_unknown_with_observed_subtotal(self):
        result = usage_totals([entry(100, 20), {"tokens": None, "usage": None}], 2)
        self.assertIsNone(result["total_tokens"])
        self.assertIsNone(result["input_tokens"])
        self.assertEqual(result["observed_tokens"], 120)
        self.assertEqual(result["missing_run_count"], 1)
        self.assertFalse(result["complete"])

    def test_pending_attempt_is_missing_not_zero_and_no_runs_is_distinct(self):
        self.assertEqual(usage_totals([entry(0, 0)], 2)["missing_run_count"], 1)
        self.assertIsNone(usage_totals([])["total_tokens"])
        self.assertEqual(usage_totals([entry(0, 0)])["total_tokens"], 0)

    def test_project_totals_include_other_tasks_and_partial_usage(self):
        tasks, total = project_usage({"tasks": [
            {"id": "one", "attempts": 1, "executions": [entry(100, 10)]},
            {"id": "two", "attempts": 2, "executions": [entry(20, 5), {"tokens": None}]}]})
        self.assertEqual(tasks["one"]["total_tokens"], 110)
        self.assertIsNone(total["total_tokens"])
        self.assertEqual(total["observed_tokens"], 135)
        self.assertEqual(total["run_count"], 3)


class DiagnosticsTests(unittest.TestCase):
    def test_sensitive_values_removed_before_excerpt_and_summary(self):
        secrets = ["password-value", "api-value", "bearer-value", "query-value", "url-password",
                   "sk-abcdefghijklmnopqrs", "private-body"]
        text = ("PASSWORD=password-value\nAPI_KEY=api-value\nAuthorization: Bearer bearer-value\n"
                "https://example.test/?token=query-value\nhttps://user:url-password@example.test/\n"
                "sk-abcdefghijklmnopqrs\n-----BEGIN PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----\n"
                "UnicodeDecodeError: codec cannot decode bytes\nFAILED (errors=1)")
        result = failure_evidence("", text)
        encoded = json.dumps(result)
        for secret in secrets:
            self.assertNotIn(secret, encoded)
        self.assertTrue(result["redacted"])
        self.assertIn("UnicodeDecodeError", result["summary"])

    def test_bounded_evidence_preserves_useful_failure_tail(self):
        result = failure_evidence("x" * 20000 + "\nAssertionError: expected result", "")
        self.assertLessEqual(len(result["excerpt"]), 3000)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["summary"], "AssertionError: expected result")

    def test_real_verification_failure_is_saved_and_available_once_in_next_context(self):
        class Adapter:
            def execute(self, *args):
                return {"ok": True, "usage": None, "answer": {"criteria_met": [True], "needs_input": False, "summary": "work done"}}
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            service = ExecutionService(tmp, Adapter())
            state = service.store.initialize("goal")
            state["tasks"] = [{"id": "one", "title": "work", "status": "ready", "dependencies": [],
                "acceptance_criteria": ["works"], "constraints": ["keep-important-constraint"], "verification": [],
                "changed_files": [], "failed_approaches": [], "next_action": "run",
                "verification_commands": [[sys.executable, "-c", "import os, sys; print('API_TOKEN=' + os.environ['WORKFLOW_TEST_KEY'], file=sys.stderr); raise RuntimeError('specific verification failure')"]]}]
            service.store.update(state, 0, "plan")
            with patch.dict(os.environ, {"WORKFLOW_TEST_KEY": "secret-value"}):
                task = service.run("one")["tasks"][0]
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["last_result"]["reason"], "verification_failed")
            self.assertIn("RuntimeError", task["verification"][0]["diagnostics"]["summary"])
            self.assertNotIn("secret-value", service.store.events.read_text(encoding="utf-8"))
            self.assertNotIn("secret-value", service.journal.read_text(encoding="utf-8"))
            context = service.store.context("one")
            self.assertNotIn("verification", context["task"])
            self.assertNotIn("executions", context["task"])
            self.assertNotIn("failed_approaches", context["task"])
            self.assertIn("RuntimeError", context["handoff"]["verification"][0]["diagnostics"]["summary"])
            self.assertEqual(context["handoff"]["constraints"], ["keep-important-constraint"])
            self.assertNotIn("verification_failures", context["handoff"]["failed_approaches"][-1])

    def test_compaction_keeps_full_history_in_state_and_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(tmp)
            state = store.initialize("goal")
            state["tasks"] = [{"id": "one", "title": "work", "status": "failed", "dependencies": [],
                "acceptance_criteria": ["works"], "constraints": ["critical " * 300], "verification": [],
                "changed_files": ["source.py"], "next_action": "resolve error",
                "failed_approaches": [{"run_id": str(i), "summary": "failure " + str(i) + "x" * 2000} for i in range(8)],
                "executions": [entry(10000, 100) for _ in range(8)], "user_notes": "answer-to-question"}]
            state = store.update(state, 0, "plan")
            before = copy.deepcopy(state)
            compact = store.context("one")
            self.assertEqual(len(compact["handoff"]["failed_approaches"]), 3)
            self.assertEqual(compact["sources"]["omitted_failure_count"], 5)
            self.assertEqual(compact["sources"]["omitted_execution_count"], 8)
            self.assertEqual(compact["task"]["user_notes"], "answer-to-question")
            self.assertEqual(compact["handoff"]["constraints"], state["tasks"][0]["constraints"])
            self.assertEqual(len(store.handoff("one")["failed_approaches"]), 8)
            self.assertEqual(store.read(), before)


if __name__ == "__main__":
    unittest.main()
