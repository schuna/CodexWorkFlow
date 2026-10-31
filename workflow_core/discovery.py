"""Locate Codex without depending on the launcher's inherited PATH."""

import os
from pathlib import Path
import shutil
import sys


def _matches(root, patterns):
    for pattern in patterns:
        try:
            yield from root.glob(pattern)
        except OSError:
            # An inaccessible optional installation directory is not a failure.
            continue


def _newest(paths):
    candidates = []
    for path in paths:
        try:
            if path.is_file():
                candidates.append((path.stat().st_mtime_ns, str(path.resolve())))
        except OSError:
            continue
    return max(candidates, default=(None, None))[1]


def find_codex(executable=None):
    """Prefer an explicit executable, then PATH, then bounded Windows locations.

    No processes are launched and no whole-drive recursive search is performed.
    Re-evaluate on each use so installing/updating Codex needs no PATH refresh.
    """
    requested = os.fspath(executable) if executable is not None else "codex"
    if requested.lower() not in ("codex", "codex.exe"):
        expanded = os.path.expandvars(os.path.expanduser(requested))
        path = Path(expanded)
        if path.is_file():
            return str(path.resolve())
        return shutil.which(expanded)
    found = shutil.which(requested)
    if found:
        return str(Path(found).resolve())
    if sys.platform != "win32":
        return None

    home = Path.home()
    # LOCALAPPDATA can be redirected by a sandbox or packaged launcher. Include
    # the real user's conventional directories independently of that variable.
    local_roots = [home / "AppData" / "Local"]
    roaming_roots = [home / "AppData" / "Roaming"]
    if os.environ.get("LOCALAPPDATA"):
        local_roots.insert(0, Path(os.environ["LOCALAPPDATA"]))
    if os.environ.get("APPDATA"):
        roaming_roots.insert(0, Path(os.environ["APPDATA"]))

    desktop = []
    for root in dict.fromkeys(local_roots):
        desktop.extend(_matches(root, (
            "OpenAI/Codex/bin/*/codex.exe", "OpenAI/Codex/bin/codex.exe",
            "Programs/Codex/resources/codex.exe", "Programs/Codex/resources/bin/codex.exe",
        )))
    found = _newest(desktop)
    if found:
        return found

    npm = []
    for root in dict.fromkeys(roaming_roots):
        npm.extend(_matches(root / "npm" / "node_modules" / "@openai", (
            "codex/vendor/*/codex/codex.exe",
            "codex/node_modules/@openai/codex-win32-*/vendor/*/codex/codex.exe",
            "codex-win32-*/vendor/*/codex/codex.exe",
        )))
    found = _newest(npm)
    if found:
        return found

    extensions = []
    for folder in (".vscode", ".vscode-insiders"):
        extensions.extend(_matches(home / folder / "extensions", (
            "openai.chatgpt-*/bin/windows-*/codex.exe",
        )))
    return _newest(extensions)
