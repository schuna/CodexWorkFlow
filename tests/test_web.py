import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workflow_core import ExecutionService, ConflictError
from workflow_core.execution import process
from workflow_core.web import WebController, WorkflowHTTPServer, command_args, command_text


class Adapter:
    def __init__(self):
        self.calls = 0
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()

    def execute(self, project, context, timeout):
        self.calls += 1
        self.entered.set()
        self.release.wait(10)
        (Path(project) / "result.txt").write_text("GUI execution", encoding="utf-8")
        return {"ok": True, "answer": {"summary": "완료", "criteria_met": [True], "needs_input": False}, "usage": None}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.adapter = Adapter()
        self.app = WebController(self.project, adapter=self.adapter)
        self.plan = self.app.suggest("첫 작업\n두 번째 작업")
        for task in self.plan["tasks"]:
            task["command_lines"] = [command_text([sys.executable, "-c", "pass"])]
        self.plan["tasks"][1]["dependencies"] = [self.plan["tasks"][0]["id"]]
        self.server = WorkflowHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.adapter.release.set()
        if self.app.worker:
            self.app.worker.join(15)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, path, data=None, token=True, origin=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Workflow-Token"] = self.server.token
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        connection.request("POST" if data is not None else "GET", path,
                           json.dumps(data).encode("utf-8") if data is not None else None, headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        result = json.loads(raw) if response.getheader("Content-Type").startswith("application/json") else raw
        status = response.status
        connection.close()
        return status, result

    def setup_plan(self):
        status, body = self.request("/api/plan", self.plan)
        self.assertEqual(status, 200, body)
        return body

    def wait_worker(self):
        self.app.worker.join(20)
        self.assertFalse(self.app.worker.is_alive())
        return self.app.snapshot()

    def test_request_boundaries_and_assets(self):
        self.assertEqual(self.request("/api/status", token=False)[0], 403)
        self.assertEqual(self.request("/api/status", origin="https://other.example")[0], 403)
        self.assertEqual(self.request("/api/status", host="other.example")[0], 403)
        status, html = self.request("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.server.token, html)
        self.assertNotIn("__WORKFLOW_TOKEN__", html)
        self.assertEqual(self.request("/../store.py")[0], 404)

    def test_gui_execution_cli_state_and_next_task(self):
        view = self.setup_plan()
        first, second = self.plan["tasks"]
        self.assertEqual(view["ready"], [first["id"]])
        code, _ = self.request("/api/run", {"task_id": first["id"], "revision": view["state"]["revision"]})
        self.assertEqual(code, 200)
        view = self.wait_worker()
        self.assertIsNone(view["worker_error"])
        self.assertEqual(view["ready"], [second["id"]])
        cli = subprocess.run([sys.executable, "-m", "workflow_core", "--project", str(self.project), "status"], capture_output=True, encoding="utf-8")
        self.assertEqual(cli.returncode, 0, cli.stderr)
        cli_state = json.loads(cli.stdout)["state"]
        self.assertEqual(cli_state["revision"], view["state"]["revision"])
        self.assertEqual(cli_state["tasks"][0]["status"], "completed")
        self.assertIsNone(cli_state["tasks"][0]["executions"][0]["tokens"])
        self.assertIn("result.txt", self.app.diff()["text"])

    def test_duplicate_worker_and_stale_start_do_not_repeat(self):
        view = self.setup_plan()
        self.adapter.release.clear()
        args = {"task_id": self.plan["tasks"][0]["id"], "revision": view["state"]["revision"]}
        self.request("/api/run", args)
        self.assertTrue(self.adapter.entered.wait(10))
        self.assertEqual(self.request("/api/run", args)[0], 409)
        self.assertEqual(self.request("/api/plan", {**self.plan, "revision": view["state"]["revision"]})[0], 409)
        self.adapter.release.set()
        self.wait_worker()
        self.request("/api/run", args)
        view = self.wait_worker()
        self.assertIn("revision changed", view["worker_error"])
        self.assertEqual(self.adapter.calls, 1)

    def test_progress_conflict_requires_matching_version_and_keeps_backup(self):
        view = self.setup_plan()
        progress = self.project / "PROGRESS.md"
        progress.write_text("외부에서 추가한 중요한 제약", encoding="utf-8")
        view = self.app.snapshot()
        self.assertTrue(view["conflict"]["conflict"])
        self.assertEqual(view["ready"], [])
        args = {"revision": view["state"]["revision"], "external_sha256": view["conflict"]["external_sha256"], "note": "제약 보존"}
        progress.write_text("외부에서 재수정", encoding="utf-8")
        self.assertEqual(self.request("/api/resolve", args)[0], 409)
        current = self.app.snapshot()
        args["external_sha256"] = current["conflict"]["external_sha256"]
        self.assertEqual(self.request("/api/resolve", args)[0], 200)
        self.assertIn("제약 보존", self.app.service.store.read()["decisions"])
        backup = next((self.project / ".codex-workflow" / "conflicts").glob("*.md"))
        self.assertEqual(backup.read_text(encoding="utf-8"), "외부에서 재수정")

    def test_plan_round_trip_preserves_attempts_and_requires_revision(self):
        view = self.setup_plan()
        self.app.start("run", {"task_id": self.plan["tasks"][0]["id"], "revision": view["state"]["revision"]})
        view = self.wait_worker()
        self.assertEqual(self.request("/api/plan", {**self.plan, "revision": 0})[0], 409)
        update = {**self.plan, "revision": view["state"]["revision"]}
        update["tasks"][0]["title"] = "기준 변경"
        self.assertEqual(self.request("/api/plan", update)[0], 200)
        task = self.app.service.store.read()["tasks"][0]
        self.assertEqual(task["status"], "unverified")
        self.assertEqual(task["attempts"], 1)
        self.assertEqual(len(task["executions"]), 1)

    def test_record_retry_from_gui_does_not_repeat_execution(self):
        view = self.setup_plan()
        with patch.object(self.app.service.store, "handoff", side_effect=OSError("disk full")):
            self.app.start("run", {"task_id": self.plan["tasks"][0]["id"], "revision": view["state"]["revision"]})
            view = self.wait_worker()
        self.assertEqual(view["execution"]["phase"], "recording")
        self.app.start("record", {})
        self.assertEqual(self.wait_worker()["execution"]["phase"], "recorded")
        self.assertEqual(self.adapter.calls, 1)

    def test_cancel_marker_is_shared_with_cli_service(self):
        self.setup_plan()
        self.adapter.release.clear()
        self.app.start("run", {"task_id": self.plan["tasks"][0]["id"], "revision": 1})
        self.assertTrue(self.adapter.entered.wait(10))
        other = ExecutionService(self.project)
        result = other.cancel()
        self.assertTrue(self.app.service._cancel_requested(result["run_id"]))
        self.adapter.release.set()
        self.wait_worker()

    def test_pre_cancelled_process_never_starts(self):
        target = self.project / "should-not-exist"
        result = process([sys.executable, "-c", "from pathlib import Path; Path('should-not-exist').touch()"],
                         self.project, cancel_check=lambda: True)
        self.assertEqual(result["reason"], "cancelled")
        self.assertFalse(target.exists())

    def test_command_paths_with_spaces_round_trip(self):
        args = [r"C:\Program Files\Python\python.exe", "-c", 'print("한글")']
        self.assertEqual(command_args(command_text(args)), args)

    def test_reopen_remembers_project_without_starting_execution(self):
        settings = self.project / "ui-settings.json"
        app = WebController(self.project, settings=settings)
        app.open_project(self.project)
        reopened = WebController(self.project.parent, settings=settings)
        self.assertEqual(reopened.service.store.project, self.project)
        self.assertIsNone(reopened.worker)

    def test_initial_progress_is_reviewed_imported_and_backed_up(self):
        progress = self.project / "PROGRESS.md"
        progress.write_text("기존 프로젝트 제약", encoding="utf-8")
        view = self.app.snapshot()
        self.assertFalse(view["initialized"])
        self.assertEqual(view["existing_progress"]["text"], "기존 프로젝트 제약")
        self.assertEqual(self.request("/api/plan", self.plan)[0], 409)
        args = {**self.plan, "existing_progress_sha256": view["existing_progress"]["sha256"], "progress_note": "원래 제약 보존"}
        self.assertEqual(self.request("/api/plan", args)[0], 200)
        self.assertIn("원래 제약 보존", self.app.service.store.read()["decisions"])
        backup = next((self.project / ".codex-workflow" / "conflicts").glob("*.md"))
        self.assertEqual(backup.read_text(encoding="utf-8"), "기존 프로젝트 제약")

    def test_running_process_observes_cancellation(self):
        stop = threading.Event()
        results = []
        started = self.project / "started.flag"
        worker = threading.Thread(target=lambda: results.append(process(
            [sys.executable, "-u", "-c", "from pathlib import Path; import time; Path('started.flag').touch(); print('partial'); time.sleep(30)"],
            self.project, cancel_check=stop.is_set)))
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(started.exists())
        finally:
            stop.set()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["reason"], "cancelled")
        self.assertIn("partial", results[0]["stdout"])

    def test_reverification_preserves_execution_summary_and_changed_files(self):
        view = self.setup_plan()
        task_id = self.plan["tasks"][0]["id"]
        self.app.service.run(task_id)
        before = self.app.service.store.read()["tasks"][0]
        self.app.service.verify(task_id)
        after = self.app.service.store.read()["tasks"][0]
        self.assertEqual(after["last_result"], before["last_result"])
        self.assertEqual(after["changed_files"], before["changed_files"])
        self.assertEqual(len(after["executions"]), 1)

    def test_reviewed_stale_result_is_failed_and_never_reexecutes(self):
        self.setup_plan()
        original = self.adapter.execute
        def change_during_run(*args):
            state = self.app.service.store.read()
            state["goal"] = "수정된 목표"
            self.app.service.store.update(state, state["revision"], "edit-during-execution")
            return original(*args)
        with patch.object(self.adapter, "execute", side_effect=change_during_run):
            with self.assertRaises(ConflictError):
                self.app.service.run(self.plan["tasks"][0]["id"])
        state = self.app.service.store.read()
        result = self.app.service.reconcile_record(state["revision"])
        self.assertEqual(result["tasks"][0]["status"], "failed")
        self.assertEqual(result["goal"], "수정된 목표")
        self.assertEqual(self.adapter.calls, 1)

    def test_reached_limit_is_excluded_from_shared_recommendations(self):
        self.plan["policy"]["max_retries"] = 0
        self.setup_plan()
        original = self.adapter.execute
        with patch.object(self.adapter, "execute", side_effect=lambda *args: {**original(*args), "ok": False}):
            self.app.service.run(self.plan["tasks"][0]["id"])
        view = self.app.snapshot()
        self.assertEqual(view["ready"], [])
        self.assertEqual(view["run_blockers"][self.plan["tasks"][0]["id"]], "task retry limit reached")

    def test_ten_independent_tasks_do_not_accumulate_completed_context(self):
        plan = self.app.suggest("\n".join("Independent unit " + str(i) for i in range(10)))
        plan["goal"] = "Finish independent work"
        for task in plan["tasks"]:
            task["command_lines"] = [command_text([sys.executable, "-c", "pass"])]
        self.app.save_plan(plan)
        (self.project / "AGENTS.md").write_text("INSTRUCTION_CONTENT_MUST_NOT_BE_COPIED", encoding="utf-8")
        contexts = []
        original = self.adapter.execute
        def capture(project, context, timeout):
            contexts.append(json.dumps(context, ensure_ascii=False))
            return original(project, context, timeout)
        with patch.object(self.adapter, "execute", side_effect=capture):
            for task in plan["tasks"]:
                self.app.service.run(task["id"])
        self.assertEqual(len(contexts), 10)
        for i, serialized in enumerate(contexts):
            self.assertIn(plan["tasks"][i]["id"], serialized)
            for previous in plan["tasks"][:i]:
                self.assertNotIn(previous["id"], serialized)
                self.assertNotIn(previous["title"], serialized)
            self.assertNotIn("INSTRUCTION_CONTENT_MUST_NOT_BE_COPIED", serialized)
            self.assertNotIn('"events"', serialized)
            self.assertNotIn('"executions"', serialized)
        self.assertLessEqual(max(map(len, contexts)) - min(map(len, contexts)), 4)

    def test_usage_endpoint_counts_failed_attempts_and_does_not_invent_missing_values(self):
        self.setup_plan()
        service = self.app.service
        state = service.store.read()
        state["tasks"][0].update(attempts=2, executions=[
            {"tokens": 12, "usage": {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 8}},
            {"tokens": None, "usage": None}])
        service.store.update(state, state["revision"], "usage-fixture")
        code, view = self.request("/api/status")
        self.assertEqual(code, 200)
        totals = view["task_usage"][self.plan["tasks"][0]["id"]]
        self.assertIsNone(totals["total_tokens"])
        self.assertEqual(totals["observed_tokens"], 12)
        self.assertEqual(totals["missing_run_count"], 1)
        self.assertEqual(view["project_usage"]["run_count"], 2)

    def test_choose_folder_runs_in_isolated_subprocess_and_handles_unicode_and_errors(self):
        class Completed:
            def __init__(self, returncode, stdout):
                self.returncode = returncode
                self.stdout = stdout
        payload = json.dumps({"project": "C:/프로젝트/폴더"}, ensure_ascii=True).encode("ascii")
        with patch("workflow_core.web.subprocess.run", return_value=Completed(0, payload)) as run_mock:
            code, body = self.request("/api/choose-folder", {})
            self.assertEqual(code, 200)
            self.assertEqual(body, {"project": "C:/프로젝트/폴더"})
            self.assertEqual(run_mock.call_args[0][0][0], sys.executable)
        with patch("workflow_core.web.subprocess.run", return_value=Completed(0, b'{"project": null}')):
            code, body = self.request("/api/choose-folder", {})
            self.assertEqual(code, 200)
            self.assertEqual(body, {"project": None})
        with patch("workflow_core.web.subprocess.run", return_value=Completed(1, b"")):
            code, body = self.request("/api/choose-folder", {})
            self.assertEqual(code, 409)
            self.assertIn("폴더 선택 창", body["error"])

    def test_ai_plan_suggest_uses_read_only_sandbox_and_normalizes_dag(self):
        (self.project / "tests").mkdir(exist_ok=True)
        ai_tasks = [
            {"id": "step-1", "title": "기초 설계", "acceptance_criteria": ["설계 문서 작성"],
             "constraints": ["읽기 전용 확인"], "dependencies": ["step-2", "nonexistent"], "command_lines": []},
            {"id": "step-2", "title": "기능 구현", "acceptance_criteria": ["구현 완료"],
             "constraints": [], "dependencies": ["step-1"], "command_lines": ["python -m unittest"]},
        ]
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"tasks": ai_tasks})}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ]
        proc_out = {"exit_code": 0, "reason": None, "output_truncated": False,
                    "stdout": "\n".join(json.dumps(e) for e in events)}
        with patch("workflow_core.web.find_codex", return_value="codex.exe"), \
             patch("workflow_core.web.process", return_value=proc_out) as proc_mock:
            code, body = self.request("/api/suggest", {"goal": "복합 프로젝트 목표", "use_ai": True})
            self.assertEqual(code, 200, body)
            argv = proc_mock.call_args[0][0]
            self.assertIn("read-only", argv)
            self.assertTrue(body["ai_generated"])
            self.assertEqual(len(body["tasks"]), 2)
            self.assertEqual(body["tasks"][0]["dependencies"], [])
            self.assertTrue(body["tasks"][0]["command_lines"])
            self.assertEqual(body["tasks"][1]["dependencies"], ["step-1"])
            self.assertEqual(self.request("/api/plan", body)[0], 200)


if __name__ == "__main__":
    unittest.main()
