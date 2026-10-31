"""Loopback-only web UI over the same execution/state APIs as the CLI."""

import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from urllib.parse import urlparse

from .execution import ExecutionService, git_snapshot, process
from .store import ConflictError, CorruptStateError, WorkflowStore, _atomic_write, _json_bytes, _validate


def command_args(line):
    if not isinstance(line, str) or not line.strip():
        raise ValueError("검증 명령을 입력하세요.")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        count = ctypes.c_int()
        parse = ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        parse.restype = ctypes.POINTER(wintypes.LPWSTR)
        pointer = parse(line.strip(), ctypes.byref(count))
        if not pointer:
            raise ValueError("검증 명령을 해석할 수 없습니다.")
        try:
            return [pointer[i] for i in range(count.value)]
        finally:
            free = ctypes.windll.kernel32.LocalFree
            free.argtypes = [ctypes.c_void_p]
            free(pointer)
    return shlex.split(line)


def command_text(argv):
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


class WebController:
    def __init__(self, project, settings=None, adapter=None, restore_project=True):
        self.mutex = threading.RLock()
        self.settings = Path(settings) if settings else None
        if restore_project and self.settings and self.settings.exists():
            try:
                saved = json.loads(self.settings.read_text(encoding="utf-8"))["project"]
                if Path(saved).is_dir():
                    project = saved
            except (OSError, ValueError, KeyError):
                pass
        if not Path(project).is_dir():
            raise ValueError("존재하는 프로젝트 폴더를 선택하세요.")
        self.service = ExecutionService(project, adapter)
        if self.settings:
            _atomic_write(self.settings, _json_bytes({"project": str(self.service.store.project)}))
        self.worker = None
        self.worker_error = None
        self.operation = None

    def _idle(self):
        if self.worker and self.worker.is_alive():
            raise ConflictError("작업 처리 중입니다. 완료 또는 중단 후 다시 시도하세요.")

    def open_project(self, path):
        with self.mutex:
            self._idle()
            # Do not detach from an execution started by a different CLI process.
            with self.service.guard._lock(timeout=0):
                project = Path(path).expanduser().resolve()
                if not project.is_dir():
                    raise ValueError("존재하는 프로젝트 폴더를 선택하세요.")
                self.service = ExecutionService(project, self.service.adapter)
                self.worker_error = None
                if self.settings:
                    _atomic_write(self.settings, _json_bytes({"project": str(project)}))
            return self.snapshot()

    def environment(self):
        executable = shutil.which("codex")
        result = {"codex_installed": bool(executable), "authenticated": None, "git": False,
                  "instructions": (self.service.store.project / "AGENTS.md").exists()}
        try:
            git_snapshot(self.service.store.project)
            result["git"] = True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        if executable:
            try:
                login = subprocess.run([executable, "login", "status"], capture_output=True, timeout=10)
                result["authenticated"] = login.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        return result

    def snapshot(self):
        with self.mutex:
            service = self.service
            info = {"project": str(service.store.project), "worker_error": self.worker_error,
                    "operation": self.operation, "busy": bool(self.worker and self.worker.is_alive())}
            try:
                status = service.status()
                info.update(status, initialized=True)
            except FileNotFoundError:
                existing = None
                if service.store.progress.exists():
                    external = service.store.progress.read_bytes()
                    existing = {"text": external.decode("utf-8", errors="replace"),
                                "sha256": hashlib.sha256(external).hexdigest()}
                return {**info, "initialized": False, "state": None, "ready": [], "execution": None, "existing_progress": existing}
            except ConflictError:
                view = service.store.inspect_progress()
                info.update(initialized=True, state=view.pop("state"), conflict=view, ready=[], execution=service._journal())
            record = info.get("execution")
            if record:
                record.pop("proposed", None)
            try:
                with service.guard._lock(timeout=0):
                    info["active"] = False
            except TimeoutError:
                info["active"] = True
            if info["active"] or info["busy"]:
                info["ready"] = []
            state = info["state"]
            for task in state["tasks"]:
                task["command_lines"] = [command_text(c) for c in task.get("verification_commands", [])]
            info["context_chars"] = {}
            if not info.get("conflict"):
                for task_id in info.get("ready", []):
                    try:
                        info["context_chars"][task_id] = len(json.dumps(service.store.context(task_id), ensure_ascii=False))
                    except ValueError:
                        pass
            return info

    def suggest(self, goal):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("목표를 입력하세요.")
        lines = [s.strip(" -\t") for s in goal.splitlines() if s.strip()][:20]
        commands = []
        project = self.service.store.project
        if (project / "tests").is_dir():
            commands = [command_text([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])]
        elif (project / "package.json").is_file():
            try:
                package = json.loads((project / "package.json").read_text(encoding="utf-8"))
                if package.get("scripts", {}).get("test"):
                    commands = ["npm.cmd test" if sys.platform == "win32" else "npm test"]
            except (OSError, ValueError):
                pass
        return {"goal": goal.strip(), "tasks": [{"id": "task-" + uuid.uuid4().hex[:8],
                 "title": line, "acceptance_criteria": [line], "constraints": [], "dependencies": [],
                 "command_lines": commands, "user_notes": ""} for line in lines], "policy": {"max_retries": 1}}

    def save_plan(self, data):
        with self.mutex:
            self._idle()
            service = self.service
            with service.guard._lock(timeout=0):
                record = service._journal()
                if record and record["phase"] != "recorded":
                    raise ConflictError("먼저 실행 결과 기록 또는 중단 복구를 완료하세요.")
                try:
                    state = service.store.read()
                    if state["revision"] != data.get("revision"):
                        raise ConflictError("계획이 변경됐습니다. 최신 상태를 불러온 뒤 다시 검토하세요.")
                    fresh = False
                except FileNotFoundError:
                    state = {"schema_version": 1, "revision": 0, "project_id": str(uuid.uuid4()),
                             "tasks": [], "goal": "", "policy": {}, "decisions": [], "open_questions": []}
                    fresh = True
                proposed = copy.deepcopy(state)
                goal = data.get("goal")
                if not isinstance(goal, str) or not goal.strip():
                    raise ValueError("목표를 입력하세요.")
                tasks = data.get("tasks")
                if not isinstance(tasks, list) or not tasks or len(tasks) > 100:
                    raise ValueError("작업을 1~100개 입력하세요.")
                if not isinstance(data.get("policy", {}), dict):
                    raise ValueError("실행 한도 형식을 확인하세요.")
                by_id = {t["id"]: t for t in state["tasks"]}
                updated = []
                for item in tasks:
                    if not isinstance(item, dict):
                        raise ValueError("작업 형식을 확인하세요.")
                    task_id = item.get("id") or "task-" + uuid.uuid4().hex[:8]
                    old = by_id.get(task_id)
                    task = copy.deepcopy(old) if old else {"id": task_id, "status": "ready", "verification": [],
                        "changed_files": [], "failed_approaches": [], "next_action": "작업 실행"}
                    for key in ("title", "user_notes"):
                        if not isinstance(item.get(key, ""), str):
                            raise ValueError("작업 제목과 답변은 텍스트여야 합니다.")
                    for key in ("acceptance_criteria", "constraints", "dependencies", "command_lines"):
                        if not isinstance(item.get(key, []), list) or not all(isinstance(x, str) for x in item.get(key, [])):
                            raise ValueError("작업 항목 형식을 확인하세요.")
                    task.update(title=item["title"].strip(), acceptance_criteria=item.get("acceptance_criteria", []),
                                constraints=item.get("constraints", []), dependencies=item.get("dependencies", []),
                                verification_commands=[command_args(c) for c in item.get("command_lines", []) if c.strip()],
                                user_notes=item.get("user_notes", ""))
                    if not task["title"] or not task["acceptance_criteria"] or not all(c.strip() for c in task["acceptance_criteria"]):
                        raise ValueError("각 작업의 제목과 완료 기준을 입력하세요.")
                    service._validate_execution(task, data.get("policy", {}))
                    if old and old["status"] == "completed" and any(task.get(k) != old.get(k) for k in
                            ("title", "acceptance_criteria", "constraints", "dependencies", "verification_commands", "user_notes")):
                        task.update(status="unverified", verification=[], next_action="변경된 기준을 검토하고 다시 실행")
                    updated.append(task)
                present = {t["id"] for t in updated}
                if any(t.get("attempts", 0) and t["id"] not in present for t in state["tasks"]):
                    raise ValueError("실행 이력이 있는 작업은 삭제할 수 없습니다.")
                proposed.update(goal=goal.strip(), tasks=updated, policy=data.get("policy", {}))
                if fresh and data.get("progress_note"):
                    if not isinstance(data["progress_note"], str):
                        raise ValueError("진행 기록 메모를 확인하세요.")
                    proposed["decisions"].append(data["progress_note"].strip())
                _validate(proposed)
                for task in updated:
                    WorkflowStore._build_handoff(proposed, task["id"])
                if fresh:
                    git_snapshot(service.store.project)
                    service.store.initialize(proposed["goal"], proposed["project_id"],
                        imported_progress_sha256=data.get("existing_progress_sha256"), progress_note=data.get("progress_note"))
                service.store.update(proposed, state["revision"], str(uuid.uuid4()))
                self.worker_error = None
            return self.snapshot()

    def start(self, action, data):
        with self.mutex:
            self._idle()
            self.worker_error = None
            self.operation = action
            service = self.service
            def login():
                executable = shutil.which("codex")
                if not executable:
                    raise ValueError("Codex 설치를 확인하세요.")
                result = process([executable, "login"], service.store.project, 300)
                if result["exit_code"] != 0:
                    raise ValueError("계정 연결이 완료되지 않았습니다. 다시 시도하세요.")
            methods = {"run": lambda: service.run(data.get("task_id"), data["revision"]),
                       "verify": lambda: service.verify(data["task_id"], data["revision"]), "record": service.record,
                       "recover": lambda: service.recover(data.get("processes_checked") is True),
                       "reconcile": lambda: service.reconcile_record(data["revision"]), "login": login}
            if action not in methods:
                raise ValueError("알 수 없는 실행 동작입니다.")
            def work():
                try:
                    methods[action]()
                except Exception as exc:
                    with self.mutex:
                        self.worker_error = str(exc) or type(exc).__name__
                finally:
                    with self.mutex:
                        self.operation = None
            self.worker = threading.Thread(target=work, name="workflow-" + action, daemon=True)
            self.worker.start()
            return {"accepted": True, "action": action}

    def resolve(self, data):
        with self.mutex:
            self._idle()
            with self.service.guard._lock(timeout=0):
                self.service.store.resolve_progress(data["revision"], data["external_sha256"], data.get("note"))
            self.worker_error = None
            return self.snapshot()

    def diff(self):
        with self.mutex:
            project = self.service.store.project
            snapshot = git_snapshot(project)
            args = ["git", "diff", "--no-ext-diff", "--no-textconv"]
            if snapshot["head"]:
                args += ["HEAD"]
            text = process(args + ["--"], project, 15)["stdout"][:250000]
            if not snapshot["head"]:
                text += process(args + ["--cached", "--"], project, 15)["stdout"][:50000]
            for name, entry in snapshot["files"].items():
                if entry["status"] != "??" or len(text) >= 300000:
                    continue
                path = project / name
                if not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(project):
                    with path.open("rb") as stream:
                        data = stream.read(20000)
                    body = "[바이너리 파일]" if b"\0" in data else data.decode("utf-8", errors="replace")
                    text += "\n--- 새 파일: " + name + " ---\n" + body
            return {"text": text[:300000], "limit_chars": 300000}


class WorkflowHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, controller):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        self.assets = Path(__file__).parent / "ui"
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not write local goals, query strings, or credentials into access logs.

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != host:
            self._send(403, {"error": "허용되지 않은 호스트입니다."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            self._send(403, {"error": "다른 사이트의 요청은 허용하지 않습니다."})
            return False
        if urlparse(self.path).path.startswith("/api/") and not secrets.compare_digest(
                self.headers.get("X-Workflow-Token", ""), self.server.token):
            self._send(403, {"error": "화면을 새로고침해 연결을 복구하세요."})
            return False
        return True

    def do_GET(self):
        if not self._authorized():
            return
        path = urlparse(self.path).path
        try:
            if path in ("/", "/app.js", "/style.css"):
                name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
                body = (self.server.assets / name).read_bytes()
                if path == "/":
                    body = body.replace(b"__WORKFLOW_TOKEN__", self.server.token.encode("ascii"))
                kind = "text/html" if path == "/" else "text/javascript" if path.endswith(".js") else "text/css"
                self._send(200, body, kind + "; charset=utf-8")
            elif path == "/api/status":
                self._send(200, self.server.controller.snapshot())
            elif path == "/api/environment":
                self._send(200, self.server.controller.environment())
            elif path == "/api/diff":
                self._send(200, self.server.controller.diff())
            else:
                self._send(404, {"error": "페이지를 찾을 수 없습니다."})
        except (ConflictError, CorruptStateError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self._send(409, {"error": str(exc)})

    def do_POST(self):
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise ValueError("요청 크기는 1MiB 이하여야 합니다.")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("JSON 요청이 필요합니다.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("잘못된 요청입니다.")
            path = urlparse(self.path).path
            controller = self.server.controller
            if path == "/api/open":
                result = controller.open_project(data["project"])
            elif path == "/api/choose-folder":
                import tkinter
                from tkinter import filedialog
                window = tkinter.Tk()
                window.withdraw()
                window.attributes("-topmost", True)
                try:
                    selected = filedialog.askdirectory(title="프로젝트 폴더 선택", parent=window)
                finally:
                    window.destroy()
                result = {"project": selected or None}
            elif path == "/api/suggest":
                result = controller.suggest(data["goal"])
            elif path == "/api/plan":
                result = controller.save_plan(data)
            elif path == "/api/stop":
                result = controller.service.cancel()
            elif path == "/api/resolve":
                result = controller.resolve(data)
            elif path in ("/api/run", "/api/verify", "/api/record", "/api/recover", "/api/reconcile", "/api/login"):
                result = controller.start(path.rsplit("/", 1)[1], data)
            else:
                self._send(404, {"error": "동작을 찾을 수 없습니다."})
                return
            self._send(200, result)
        except (ConflictError, CorruptStateError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            self._send(409, {"error": str(exc)})


def serve(project, port=8765, open_browser=True, settings=None, restore_project=True):
    server = WorkflowHTTPServer(("127.0.0.1", port), WebController(project, settings, restore_project=restore_project))
    url = f"http://127.0.0.1:{server.server_port}/"
    print("Workflow UI: " + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
