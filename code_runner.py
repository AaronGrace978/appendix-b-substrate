"""Restricted Python runner for model-written verification code.

Zero dependencies. Each run is a fresh `python -I` child process in a new
temporary working directory with:
  - a wall-clock timeout (default 15 s) and capped stdout/stderr
  - network blocked (socket creation and DNS raise PermissionError)
  - process spawning blocked (subprocess, os.system/exec*/spawn*, ctypes)
  - file reads, writes, listings, deletes and renames allowed only inside
    the temp directory (so code cannot read your files and send them back
    to a cloud model)
  - on POSIX, CPU and memory limits via `resource`

This is a guard-rail for honest mistakes, NOT a security sandbox: determined
code can still escape it. Only run models you trust on a machine you own.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TIMEOUT = 15.0
OUTPUT_CAP = 20000
MAX_CODE = 50000

GUARD = r'''
import builtins, io, os, sys
_ROOT = os.path.realpath(os.getcwd())
def _deny(*a, **k):
    raise PermissionError("blocked by the Appendix B code runner")
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
except Exception:
    pass
import socket
for _n in ("socket", "create_connection", "getaddrinfo", "gethostbyname", "socketpair", "fromfd"):
    if hasattr(socket, _n):
        setattr(socket, _n, _deny)
import subprocess
for _n in ("Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
    setattr(subprocess, _n, _deny)
for _n in ("system", "popen", "execv", "execve", "execl", "execle", "execlp", "execlpe", "execvp", "execvpe",
           "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
           "startfile", "fork", "forkpty", "kill", "killpg", "posix_spawn", "posix_spawnp",
           "chmod", "chown", "link", "symlink", "truncate"):
    if hasattr(os, _n):
        setattr(os, _n, _deny)
def _inside(p):
    if isinstance(p, int):
        return True  # file descriptors only come from guarded opens
    try:
        r = os.path.realpath(os.fspath(p))
    except TypeError:
        return False
    return r == _ROOT or r.startswith(_ROOT + os.sep)
def _guard_paths(fn):
    def g(*args, **k):
        for p in list(args[:2]) + [k.get("src"), k.get("dst")]:
            if isinstance(p, (str, bytes, os.PathLike)) and not _inside(p):
                _deny()
        return fn(*args, **k)
    return g
for _n in ("remove", "unlink", "rmdir", "removedirs", "rename", "renames", "replace", "mkdir", "makedirs", "open"):
    if hasattr(os, _n):
        setattr(os, _n, _guard_paths(getattr(os, _n)))
import shutil
for _n in ("rmtree", "move", "copy", "copy2", "copyfile", "copytree"):
    setattr(shutil, _n, _guard_paths(getattr(shutil, _n)))
_open = builtins.open
def _safe_open(file, mode="r", *a, **k):
    if not _inside(file):
        _deny()
    return _open(file, mode, *a, **k)
for _n in ("listdir", "scandir", "walk"):
    if hasattr(os, _n):
        _orig = getattr(os, _n)
        def _wrap(path=".", *a, _f=_orig, **k):
            if not _inside(path):
                _deny()
            return _f(path, *a, **k)
        setattr(os, _n, _wrap)
builtins.open = _safe_open
io.open = _safe_open
for _m in ("ctypes", "_ctypes", "multiprocessing", "_posixsubprocess", "_winapi", "winreg", "pty"):
    sys.modules[_m] = None
del _n, _m
__code = _open(os.path.join(_ROOT, "snippet.py"), encoding="utf-8").read()
exec(compile(__code, "snippet.py", "exec"), {"__name__": "__main__", "__builtins__": builtins})
'''

RUN_BLOCK = re.compile(r"```[ \t]*(?:python|py)[ \t]+run[ \t]*\n(.*?)```", re.S | re.I)


def find_runs(text: str) -> list[str]:
    """Return code from ```python run blocks, in order."""
    return [m.group(1) for m in RUN_BLOCK.finditer(text or "") if m.group(1).strip()]


def _cap(s: str) -> str:
    return s if len(s) <= OUTPUT_CAP else s[:OUTPUT_CAP] + f"\n… [truncated {len(s) - OUTPUT_CAP} characters]"


def run_code(code: str, timeout: float = TIMEOUT, python: str | None = None) -> dict:
    """Run code in a restricted child process and return its outcome."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code required")
    if len(code) > MAX_CODE:
        raise ValueError("code too long")
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="appendixb-run-") as work:
        Path(work, "snippet.py").write_text(code, encoding="utf-8")
        Path(work, "guard.py").write_text(GUARD, encoding="utf-8")
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "WINDIR")}
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*"})
        try:
            proc = subprocess.run([python or sys.executable, "-I", "-S", "guard.py"], cwd=work, env=env,
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                  creationflags=flags)
            out, err, code_ = proc.stdout, proc.stderr, proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            out, err, code_, timed_out = exc.stdout or b"", exc.stderr or b"", None, True
    stdout = _cap(out.decode("utf-8", "replace"))
    stderr = _cap(err.decode("utf-8", "replace"))
    # Hide guard frames from tracebacks so the model sees its own line numbers.
    stderr = re.sub(r'  File "[^"]*guard\.py", line \d+, in <module>\n(?:    .*\n)?', "", stderr)
    return {"code": code, "stdout": stdout, "stderr": stderr, "exit": code_, "timed_out": timed_out,
            "ok": code_ == 0 and not timed_out, "seconds": round(time.monotonic() - start, 3)}


def result_message(results: list[dict]) -> str:
    """Format run results as the next user turn for the model."""
    parts = []
    for i, r in enumerate(results, 1):
        status = "timed out" if r["timed_out"] else ("ok" if r["ok"] else f"exit code {r['exit']}")
        body = f"[code run {i}: {status}, {r['seconds']} s]\nstdout:\n```\n{r['stdout'] or '(empty)'}\n```"
        if r["stderr"].strip():
            body += f"\nstderr:\n```\n{r['stderr']}\n```"
        parts.append(body)
    return ("Results from running your code (computed by the app, not written by the user):\n\n"
            + "\n\n".join(parts)
            + "\n\nContinue from where you stopped; do not repeat sections you already wrote. "
              "Use these results, run more code only if you need it, and finish with your final answer.")
