import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from workflow_core.discovery import find_codex
from workflow_core.execution import CodexAdapter
from workflow_core.web import WebController


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.local = self.home / "AppData" / "Local"
        self.roaming = self.home / "AppData" / "Roaming"
        for mocked in (patch("workflow_core.discovery.Path.home", return_value=self.home),
                       patch("workflow_core.discovery.sys.platform", "win32"),
                       patch("workflow_core.discovery.shutil.which", return_value=None),
                       patch.dict(os.environ, {"LOCALAPPDATA": str(self.home / "redirected-sandbox"),
                                               "APPDATA": str(self.roaming)})):
            mocked.start()
            self.addCleanup(mocked.stop)

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture executable; never execute")
        return str(path.resolve())

    def test_desktop_install_found_without_path_even_if_localappdata_is_redirected(self):
        expected = self.executable(self.local / "OpenAI/Codex/bin/build-hash/codex.exe")
        self.assertEqual(find_codex(), expected)

    def test_path_precedes_known_installations(self):
        preferred = self.executable(self.home / "on-path/codex.exe")
        self.executable(self.local / "OpenAI/Codex/bin/build-hash/codex.exe")
        with patch("workflow_core.discovery.shutil.which", return_value=preferred):
            self.assertEqual(find_codex(), preferred)

    def test_newest_version_is_selected_and_install_updates_are_observed(self):
        older = self.executable(self.local / "OpenAI/Codex/bin/a/codex.exe")
        os.utime(older, (100, 100))
        self.assertEqual(find_codex(), older)
        newer = self.executable(self.local / "OpenAI/Codex/bin/b/codex.exe")
        os.utime(newer, (200, 200))
        self.assertEqual(find_codex(), newer)

    def test_native_npm_and_editor_locations(self):
        npm = self.executable(self.roaming / "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/codex/codex.exe")
        extension = self.executable(self.home / ".vscode/extensions/openai.chatgpt-1.0.0/bin/windows-x86_64/codex.exe")
        self.assertEqual(find_codex(), npm)
        Path(npm).unlink()
        self.assertEqual(find_codex(), extension)

    def test_explicit_executable_is_preserved_and_missing_override_does_not_fall_back(self):
        installed = self.executable(self.local / "OpenAI/Codex/bin/build/codex.exe")
        custom = self.executable(self.home / "custom tools/custom.exe")
        self.assertEqual(find_codex(custom), custom)
        self.assertIsNone(find_codex(str(self.home / "missing.exe")))
        self.assertEqual(find_codex(), installed)

    def test_inaccessible_optional_directory_is_skipped(self):
        with patch("workflow_core.discovery.Path.glob", side_effect=PermissionError("denied")):
            self.assertIsNone(find_codex())

    def test_probe_login_and_execution_use_the_same_discovered_file(self):
        expected = self.executable(self.local / "OpenAI/Codex/bin/build/codex.exe")
        controller = WebController(self.home)
        with patch("workflow_core.web.git_snapshot", return_value={}), patch("workflow_core.web.subprocess.run") as run:
            run.return_value.returncode = 0
            result = controller.environment()
            self.assertTrue(result["codex_installed"])
            self.assertEqual(result["codex_path"], expected)
            run.assert_called_once_with([expected, "login", "status"], capture_output=True, timeout=10)
        with patch("workflow_core.web.process", return_value={"exit_code": 0}) as login:
            controller.start("login", {})
            controller.worker.join(5)
            self.assertIsNone(controller.worker_error)
            self.assertEqual(login.call_args.args[0], [expected, "login"])
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(
            {"summary": "ok", "needs_input": False, "criteria_met": [True]})}}, {"type": "turn.completed"}]
        output = {"exit_code": 0, "reason": None, "output_truncated": False,
                  "stdout": "\n".join(json.dumps(e) for e in events)}
        with patch("workflow_core.execution.process", return_value=output) as execute:
            self.assertTrue(CodexAdapter().execute(self.home, {"task": {"acceptance_criteria": ["works"]}}, None)["ok"])
            self.assertEqual(execute.call_args.args[0][0], expected)


if __name__ == "__main__":
    unittest.main()
