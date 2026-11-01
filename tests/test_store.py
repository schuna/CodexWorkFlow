import copy
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workflow_core import ConflictError, CorruptStateError, WorkflowStore


def task(task_id="one"):
    return {"id": task_id, "title": "Implement", "status": "ready", "acceptance_criteria": ["works"],
            "dependencies": [], "verification": [], "constraints": [], "changed_files": [],
            "failed_approaches": [], "next_action": "run"}


def competing_update(path, gate, queue, label):
    store = WorkflowStore(path)
    state = store.read()
    state["goal"] = label
    gate.wait()
    try:
        store.update(state, 0, label)
        queue.put("ok")
    except ConflictError:
        queue.put("conflict")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkflowStore(self.temp.name)
        self.state = self.store.initialize("goal", project_id="test")

    def test_revision_and_idempotency(self):
        self.state["tasks"].append(task())
        result = self.store.update(self.state, 0, "operation-1")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(self.store.update(self.state, 0, "operation-1"), result)
        changed = copy.deepcopy(self.state)
        changed["goal"] = "different"
        with self.assertRaises(ConflictError):
            self.store.update(changed, 0, "operation-1")
        with self.assertRaises(ConflictError):
            self.store.update(changed, 0, "operation-2")

    def test_recovery_after_event_commit_and_incomplete_tail(self):
        self.state["tasks"].append(task())
        materialize = self.store._materialize
        calls = 0
        def interrupted(state, previous=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated crash after event append")
            return materialize(state, previous)
        with patch.object(self.store, "_materialize", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.update(self.state, 0, "operation-1")
        with open(self.store.events, "ab") as stream:
            stream.write(b'{"incomplete":')
        restored = self.store.read()
        self.assertEqual(restored["revision"], 1)
        self.assertEqual(json.loads(self.store.state_file.read_text(encoding="utf-8")), restored)
        self.assertTrue(self.store.events.read_bytes().endswith(b"\n"))

    def test_external_edit_is_not_overwritten(self):
        self.store.progress.write_text("manual edit", encoding="utf-8")
        with self.assertRaises(ConflictError):
            self.store.read()
        self.assertEqual(self.store.progress.read_text(encoding="utf-8"), "manual edit")

    def test_external_edit_during_materialization_is_not_overwritten(self):
        from workflow_core.store import _atomic_write
        self.state["goal"] = "new goal"
        def concurrent_edit(path, data):
            if path == self.store.state_file:
                self.store.progress.write_text("new external edit", encoding="utf-8")
            return _atomic_write(path, data)
        with patch("workflow_core.store._atomic_write", side_effect=concurrent_edit):
            with self.assertRaises(ConflictError):
                self.store.update(self.state, 0, "edit-during-materialization")
        self.assertEqual(self.store.progress.read_text(encoding="utf-8"), "new external edit")
        self.assertEqual(self.store.inspect_progress()["state"]["revision"], 1)

    def test_corrupt_committed_event_is_not_discarded(self):
        with open(self.store.events, "ab") as stream:
            stream.write(b'{"bad":true}\n')
        with self.assertRaises(CorruptStateError):
            self.store.read()
        self.assertIn(b'"bad"', self.store.events.read_bytes())

    def test_handoff_and_context(self):
        self.state["tasks"].append(task("../unsafe"))
        self.store.update(self.state, 0, "add")
        handoff = self.store.handoff("../unsafe")
        self.assertEqual(handoff["source_revision"], 1)
        self.assertEqual(len(list((self.store.root / "handoffs").glob("*/*.json"))), 1)
        self.assertNotIn("events", self.store.context("../unsafe"))
        current = self.store.read()
        del current["tasks"][0]["constraints"]
        self.store.update(current, 1, "remove")
        with self.assertRaises(ValueError):
            self.store.handoff("../unsafe")

    def test_concurrent_processes_cannot_lose_update(self):
        ctx = multiprocessing.get_context("spawn")
        gate, queue = ctx.Event(), ctx.Queue()
        # Each process receives the same revision before the gate is released.
        workers = [ctx.Process(target=competing_update, args=(self.temp.name, gate, queue, label)) for label in ("a", "b")]
        for process in workers:
            process.start()
        gate.set()
        outcomes = [queue.get(timeout=20) for _ in workers]
        for process in workers:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(outcomes, ["ok", "conflict"])
        self.assertEqual(self.store.read()["revision"], 1)

    def test_fast_path_read_and_event_compaction_preserve_integrity(self):
        for i in range(6):
            current = self.store.read()
            current["goal"] = f"goal-{i}"
            self.store.update(current, i, f"op-{i}")
        with patch.object(self.store, "_read_events", side_effect=AssertionError("full scan should not run")):
            fast_state = self.store.read()
            self.assertEqual(fast_state["revision"], 6)
            self.assertEqual(fast_state["goal"], "goal-5")
            self.assertFalse(self.store.inspect_progress()["conflict"])
        summary = self.store.compact_events(retain=3)
        self.assertEqual(summary, {"compacted": 4, "retained": 3, "archived_through_revision": 3})
        self.assertEqual(len(self.store.events.read_bytes().splitlines()), 3)
        self.assertTrue((self.store.archive_dir / "events-0-3.jsonl").is_file())
        with self.assertRaises(ConflictError):
            self.store.update(fast_state, 6, "op-0")
        fast_state["policy"]["max_events_retained"] = 2
        updated = self.store.update(fast_state, 6, "op-auto-compact")
        self.assertEqual(updated["revision"], 7)
        self.assertEqual(len(self.store.events.read_bytes().splitlines()), 2)
        self.assertEqual(self.store.read()["revision"], 7)


if __name__ == "__main__":
    unittest.main()
