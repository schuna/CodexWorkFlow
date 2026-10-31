import copy
import tempfile
import unittest

from scripts.register_followups import register_followups
from workflow_core import ConflictError, ExecutionService, WorkflowStore


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WorkflowStore(self.tmp.name)
        state = self.store.initialize("original goal", project_id="fixture")
        self.completed = {"id": "report", "title": "report", "status": "completed", "dependencies": [],
            "acceptance_criteria": ["report exists"], "constraints": [], "verification": [],
            "changed_files": ["REPORT.md"], "failed_approaches": [], "next_action": "next", "attempts": 3,
            "executions": [{"tokens": 100, "usage": {"input_tokens": 80, "output_tokens": 20, "cached_input_tokens": 40}}]}
        state["tasks"].append(copy.deepcopy(self.completed))
        self.store.update(state, 0, "existing")
        self.plan = {"project_id": "fixture", "goal": "next goal", "tasks": [
            {"id": "next", "title": "next", "dependencies": ["report"], "acceptance_criteria": ["next exists"],
             "constraints": [], "verification_commands": [["{python}", "-m", "unittest"]]},
            {"id": "later", "title": "later", "dependencies": ["next"], "acceptance_criteria": ["later exists"],
             "constraints": [], "verification_commands": []}]}

    def test_registration_preserves_completed_history_and_is_idempotent(self):
        state, added = register_followups(self.tmp.name, self.plan, 1, python_executable="python")
        self.assertEqual(added, ["next", "later"])
        self.assertEqual(state["tasks"][0], self.completed)
        self.assertEqual(state["tasks"][1]["verification_commands"][0][0], "python")
        service = ExecutionService(self.tmp.name)
        self.assertEqual(service.status()["ready"], ["next"])
        self.assertEqual(service.status()["project_usage"]["observed_tokens"], 100)
        repeated, added = register_followups(self.tmp.name, self.plan, 1)
        self.assertEqual(added, [])
        self.assertEqual(repeated["revision"], state["revision"])

    def test_wrong_project_stale_revision_and_active_execution_are_rejected(self):
        with self.assertRaises(ValueError):
            register_followups(self.tmp.name, {**self.plan, "project_id": "different"}, 1)
        with self.assertRaises(ConflictError):
            register_followups(self.tmp.name, self.plan, 0)
        service = ExecutionService(self.tmp.name)
        with service.guard._lock():
            with self.assertRaises(TimeoutError):
                register_followups(self.tmp.name, self.plan, 1)
        self.assertEqual(self.store.read()["revision"], 1)

    def test_invalid_dependency_does_not_modify_state(self):
        self.plan["tasks"][0]["dependencies"] = ["nonexistent"]
        with self.assertRaises(ValueError):
            register_followups(self.tmp.name, self.plan, 1)
        self.assertEqual(self.store.read()["revision"], 1)


if __name__ == "__main__":
    unittest.main()
