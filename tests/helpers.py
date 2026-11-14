"""Shared bits for the StellAI tests: start a hub with throw-away folders, and simple checks.

Each test file runs in its own Python process (run_tests.py starts them), because the hub keeps its state in
module globals. The stand-in programs in tests/fakes (Blender, FreeCAD, KiCad, a model server, …) are copied to a
temporary folder and put on PATH by run_tests.py; FAKES points at that folder.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
FAKES = os.environ.get("STELLAI_TEST_FAKES", os.path.join(ROOT, "tests", "fakes"))
sys.path.insert(0, SERVER)

FAILURES = []


def _crash(kind, value, tb):
    """An unexpected error ends the test file at once (background threads and helper programs can't keep it alive)."""
    import traceback
    traceback.print_exception(kind, value, tb)
    sys.stdout.flush()
    os._exit(1)


sys.excepthook = _crash


def check(cond, what, detail=None):
    """Records a failed check (with what was seen) instead of stopping, so one run shows every problem."""
    if cond:
        return True
    FAILURES.append(f"{what}" + (f": got {detail!r}"[:400] if detail is not None else ""))
    print("FAIL:", FAILURES[-1], flush=True)
    return False


def done():
    """Ends a test file: exit code 1 if any check failed."""
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed", flush=True)
        sys.exit(1)
    print("ok", flush=True)


def hub_args(tmp, key="s", llama="http://127.0.0.1:9", extra=()):
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "k"), "w") as f:
        f.write(key)
    return ["--key-file", os.path.join(tmp, "k"), "--db", os.path.join(tmp, "c.db"), "--diagram-dir", os.path.join(tmp, "d"),
            "--image-dir", os.path.join(tmp, "i"), "--wakeword-dir", os.path.join(tmp, "w"), "--llama", llama,
            "--app-port", "0", *extra]


def start_hub(extra=(), llama="http://127.0.0.1:9", serve=True):
    """(hub module, web server, temp folder, port). The web server answers in a background thread."""
    import stellai_hub as h
    tmp = tempfile.mkdtemp()
    web = h.start(h.parse_args(hub_args(tmp, llama=llama, extra=extra)))
    if serve:
        threading.Thread(target=web.serve_forever, daemon=True).start()
    return h, web, tmp, web.server_address[1]


def fake_model(port, ctx=2048):
    """Starts the stand-in model server (tests/fakes/fake_llama.py) on a port; returns the process."""
    p = subprocess.Popen([sys.executable, os.path.join(FAKES, "fake_llama.py"), str(port), str(ctx)])
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    return p


def call(port, method, path, body=None, key="s"):
    """(status, parsed JSON or raw bytes) for one request to a hub."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as f:
            status, raw = f.status, f.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw
