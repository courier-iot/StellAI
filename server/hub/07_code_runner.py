# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Code runner: Python on the server ---------------------------------------------------------------------------
# Stella runs short Python scripts to check maths, test code, simulate circuits and analyse files, instead of
# guessing. Each run gets an empty folder (plus any files it asked for), a time limit, a memory limit and, where
# Linux allows it, no network. Pictures it saves (plots) show in the chat. A guard rail, not a sandbox: the code
# is checked before it runs, and it can't reach past its folder through the usual file and process functions.
CODE_RUNNER_CMD = None    # e.g. ["StellAI.exe", "--python-runner"] in the packaged Windows app
CODE_BAD_IMPORTS = {"subprocess", "socket", "ctypes", "multiprocessing", "requests", "urllib", "http", "ftplib",
                    "smtplib", "telnetlib", "asyncio", "ssl", "webbrowser", "pty", "pexpect", "paramiko", "winreg",
                    "shutil", "importlib", "signal", "resource", "sqlite3"}
CODE_BAD_CALLS = {"system", "popen", "spawnl", "spawnv", "spawnve", "spawnvp", "execv", "execve", "execl", "execlp",
                  "execvp", "fork", "forkpty", "kill", "killpg", "remove", "unlink", "rmdir", "removedirs", "rename",
                  "replace", "chmod", "chown", "symlink", "link", "putenv", "chdir", "startfile", "__import__",
                  "eval", "exec", "compile", "breakpoint", "rmtree", "setrlimit"}
CODE_UNSHARE = {"checked": False, "ok": False}


def check_python_code(code):
    """Problems that stop a script from running (files outside its folder, processes, network)."""
    if len(code) > 60000:
        return ["the script is too long (60,000 characters max)"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error on line {e.lineno}: {e.msg}"]
    probs = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            probs += [f"import {a.name} isn't allowed here" for a in n.names if a.name.split(".")[0] in CODE_BAD_IMPORTS]
        elif isinstance(n, ast.ImportFrom):
            if n.level or (n.module or "").split(".")[0] in CODE_BAD_IMPORTS:
                probs.append(f"from {n.module} import isn't allowed here")
        elif isinstance(n, ast.Attribute) and (n.attr in CODE_BAD_CALLS or n.attr.startswith("__")):
            probs.append(f".{n.attr} isn't allowed (no processes, deleting files or reaching outside the folder)")
        elif isinstance(n, ast.Name) and n.id in CODE_BAD_CALLS:
            probs.append(f"{n.id}() isn't allowed")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open" and n.args:
            a = n.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and (
                    os.path.isabs(a.value) or ".." in a.value.replace("\\", "/").split("/") or a.value.startswith("~")):
                probs.append(f"open('{a.value}') reaches outside the run's folder; use plain file names")
    return sorted(set(probs))[:8]


def _no_network_prefix():
    """["unshare", "-rn"] when this Linux lets a normal user run without network, else []."""
    if not CODE_UNSHARE["checked"]:
        CODE_UNSHARE["checked"] = True
        import shutil
        u = shutil.which("unshare")
        if u and os.name != "nt":
            try:
                CODE_UNSHARE["ok"] = subprocess.run([u, "-rn", "true"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                CODE_UNSHARE["ok"] = False
        CODE_UNSHARE["cmd"] = [u, "-rn"] if CODE_UNSHARE["ok"] else []
    return list(CODE_UNSHARE.get("cmd") or [])


CODE_PRELUDE = r'''# StellAI code runner: limits, then the script
import os, sys
try:
    import resource
    mb = int(os.environ.get("STELLAI_MEM_MB", "2048"))
    resource.setrlimit(resource.RLIMIT_AS, (mb * 2**20, mb * 2**20))
    secs = int(os.environ.get("STELLAI_CPU_S", "60"))
    resource.setrlimit(resource.RLIMIT_CPU, (secs, secs + 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 2**20, 200 * 2**20))
except Exception:
    pass
os.environ["MPLBACKEND"] = "Agg"
sys.argv = ["script.py"]
'''


def tool_run_python(args):
    code = str(args.get("code", ""))
    if not code.strip():
        raise ValueError("give code: the Python script to run")
    probs = check_python_code(code)
    if probs:
        return {"ok": False, "error": "not run: " + "; ".join(probs)}, "Script blocked"
    timeout = max(5, min(300, int(args.get("timeout") or 60)))
    work = tempfile.mkdtemp(prefix="stellai-run-")
    copied = []
    try:
        for pid in [str(p) for p in (args.get("photos") or [])][:6]:     # chat photos, by their [photo id]
            src = os.path.join(IMAGE_DIR, re.sub(r"[^0-9a-f]", "", pid) + ".jpg")
            if os.path.isfile(src):
                import shutil as _sh
                _sh.copyfile(src, os.path.join(work, os.path.basename(src)))
                copied.append(os.path.basename(src))
        project = str(args.get("project_id") or "")
        for name in [str(n) for n in (args.get("project_files") or [])][:10]:
            text = _project_file_text(project, name)
            if text is not None:
                safe = re.sub(r"[^\w.\- ]", "_", os.path.basename(name))[:80] or "file.txt"
                with open(os.path.join(work, safe), "w", encoding="utf-8") as f:
                    f.write(text)
                copied.append(safe)
        with open(os.path.join(work, "script.py"), "w", encoding="utf-8") as f:
            f.write(CODE_PRELUDE + "\n" + code)
        before = set(os.listdir(work))
        runner = list(CODE_RUNNER_CMD) if CODE_RUNNER_CMD else [sys.executable, "-I"]
        cmd = _no_network_prefix() + runner + [os.path.join(work, "script.py")]
        env = {"PATH": os.environ.get("PATH", ""), "HOME": work, "TMPDIR": work, "MPLBACKEND": "Agg",
               "MPLCONFIGDIR": os.path.join(work, ".mpl"), "PYTHONIOENCODING": "utf-8", "STELLAI_CPU_S": str(timeout),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        started = time.time()
        try:
            p = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout + 5, stdin=subprocess.DEVNULL,
                               creationflags=0x08000000 if os.name == "nt" else 0)
            code_rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            code_rc, out, err = -9, (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else \
                (e.stdout or ""), f"stopped: the script ran longer than {timeout} s"
        took = round(time.time() - started, 2)
        if code_rc != 0 and not err.strip() and (code_rc < 0 or took >= timeout - 0.5):
            err = f"stopped: the script used its {timeout} s time limit"
        made = sorted(n for n in set(os.listdir(work)) - before if not n.startswith("."))
        images, texts = [], {}
        for name in made:
            full = os.path.join(work, name)
            ext = os.path.splitext(name)[1].lower()
            if ext == ".png" and len(images) < 4 and os.path.getsize(full) < 8_000_000:
                iid = uuid.uuid4().hex[:16]
                import shutil as _sh
                _sh.copyfile(full, os.path.join(DIAGRAM_DIR, iid + ".png"))
                images.append(iid)
            elif ext in (".txt", ".csv", ".json", ".md", ".log") and len(texts) < 4:
                with open(full, encoding="utf-8", errors="replace") as f:
                    texts[name] = f.read(4000)
        res = {"ok": code_rc == 0, "exit_code": code_rc, "seconds": took, "stdout": out[-8000:],
               "stderr": err[-4000:], "files_made": made[:20], "file_previews": texts, "inputs": copied,
               "network": "off" if _no_network_prefix() else "not blocked on this computer"}
        if images:
            res["image"] = images[0]
            res["title"] = "Plot"
            if len(images) > 1:
                res["more_images"] = images[1:]
        summary = (f"Ran Python ({took:g} s)" if code_rc == 0 else
                   f"Python stopped with an error ({took:g} s)")
        return res, summary
    finally:
        import shutil as _sh
        _sh.rmtree(work, ignore_errors=True)


def _project_file_text(project_id, name):
    """The extracted text of a project (or library) file by name, or None."""
    p = CHATS.project(project_id) if project_id else None
    for f in (p or {}).get("files", []):
        if f.get("name") == name or os.path.basename(f.get("name", "")) == os.path.basename(name):
            return _file_text(f["id"])
    return None


SERVER_TOOLS["run_python"] = (tool_run_python, _fn(
    "run_python",
    "Run a short Python 3 script on the server and get its output: check maths and unit conversions, test code, "
    "simulate a circuit or a filter, analyse a CSV, make a plot (save it with plt.savefig('plot.png'); pictures "
    "show in the chat). numpy and matplotlib are available. The script starts in an empty folder (plus any "
    "photos or project files you list), may only use plain file names, has no network, and is stopped after the "
    "time limit. print() what you need. Prefer this over doing long arithmetic in your head.",
    {"code": _p("The Python script"),
     "timeout": _p("Seconds, 5-300 (default 60)", "integer"),
     "photos": {"type": "array", "items": {"type": "string"},
                "description": "Photo ids from [photo ...] in the chat, copied in as <id>.jpg"},
     "project_files": {"type": "array", "items": {"type": "string"},
                       "description": "Names of this project's files to copy in (their text)"}},
    ["code"]))


