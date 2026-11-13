# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ----------------------------------------------------------------------------- activity + updates
ACTIVITY = {"active": 0, "last": time.time(), "lock": threading.Lock()}
WEB = None            # the running HTTP server, set in start()
RESTART_CODE = 75     # exit code that tells the launcher "start me again" (after an update)
EXIT_CODE = 0


def _tracked(fn):
    """Counts requests in progress, so updates wait until nobody is using Stella."""
    def wrapper(self):
        p = urlparse(self.path).path
        if p == "/health" or p == "/avatar/events" or p.startswith("/avatar/lib/"):
            return fn(self)     # health checks and avatar screens polling don't count as using Stella
        with ACTIVITY["lock"]:
            ACTIVITY["active"] += 1
        try:
            return fn(self)
        finally:
            with ACTIVITY["lock"]:
                ACTIVITY["active"] -= 1
                ACTIVITY["last"] = time.time()
    return wrapper


def llama_busy():
    """True while llama-server is generating (phones talk to it directly, not through the hub)."""
    try:
        slots = _llama("/slots", timeout=5)
        return any(s.get("is_processing") for s in slots) if isinstance(slots, list) else False
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False


def server_idle(quiet=120):
    with ACTIVITY["lock"]:
        busy = ACTIVITY["active"] > 0 or time.time() - ACTIVITY["last"] < quiet
    return not busy and not llama_busy()


def wait_until_idle(max_wait=6 * 3600, quiet=120):
    waited, step = 0, min(30, max(1, quiet))
    while not server_idle(quiet):
        time.sleep(step)
        waited += step
        if waited >= max_wait:
            return False
    return True


def _fetch(url, token="", timeout=60):
    headers = {"User-Agent": "StellAI-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def self_test():
    """Quick check that this file works, run on a downloaded update before it replaces the old one."""
    with tempfile.TemporaryDirectory() as tmp:
        db = ChatDB(os.path.join(tmp, "t.db"))
        db.sync(0, [{"id": "c", "title": "t", "created_at": 1, "updated_at": 1}], [])
        p = db.save_project({"name": "Test"})
        db.add_project_note(p["id"], "note")
        assert db.save_mode({"name": "Test mode", "temperature": 0.7})["id"].startswith("c-")
        r = db.add_reminder("test", int(time.time() * 1000) + 60_000, "daily")
        db.update_reminder(r["id"], done=True)
        db.add_items("Shopping list", ["flux"])
        assert db.lists()["shopping"][0]["text"] == "flux"
        assert "web_search" in SERVER_TOOLS and "github_commit" in APPROVAL_TOOLS
        assert ChatSearch._pieces([("user", "hi", 1), ("assistant", "hello", 2)])
        assert model_file_for("35b", 20, 6)[0].endswith("IQ3_S.gguf")
        assert db.project(p["id"])["notes"]
        assert chunk_text("a\n" * 3000)
        assert "render_diagram" in SERVER_TOOLS
        assert {"blender_project", "blender_step", "blender_inspect"} <= set(SERVER_TOOLS)
        assert not check_bpy_code("import bmesh\nbm = bmesh.new()\nbmesh.ops.create_cube(bm, size=0.1)\n"
                                  "o = new_object('Box', bm)\no.location.z = getattr(o, 'location').z + 1")
        assert check_bpy_code("import os") and check_bpy_code("open('x')") and check_bpy_code("bpy.ops.wm.quit_blender()")
        assert check_bpy_code("o.__class__") and check_bpy_code("bpy.data.texts.new('a')")
        compile(BLENDER_WORKER_PY, "worker", "exec")
        compile(SHAPE_PY, "shape", "exec")
        assert "blender_generate" in SERVER_TOOLS
        assert {"cad_project", "cad_step", "cad_inspect", "pcb_project", "pcb_check", "pcb_step", "pcb_circuit",
                "pcb_library"} <= set(SERVER_TOOLS)
        assert _skidl_check("generate_netlist(file_='/etc/x')") and _skidl_check("import os")
        assert not _skidl_check("r = Part('Device', 'R', value='1k', footprint='Resistor_THT:R')\nNet('VCC') += r[1]"
                                .replace("Net('VCC') += r[1]", "v = Net('VCC')\nv += r[1]"))
        compile(CAD_RUNNER_PY, "cad", "exec")
        assert "switch_mode" in SERVER_TOOLS and _mode_by_name("PCB designer") == "pcb"
        compile(PCB_RUNNER_PY, "pcb", "exec")
        compile(SKIDL_RUNNER_PY, "skidl", "exec")
        if TUNE:
            hw = {"cores": 8, "ram_gb": 32, "avx2": True,
                  "gpus": [{"index": 0, "name": "RTX 3060", "vram_gb": 12, "vendor": "nvidia"}]}
            assert TUNE.rank(hw, 26)["picks"] and TUNE.library(hw, 26, tmp)
        assert not helper_up() and _helper_mode("x", "general", "pcb") == ""
        assert "move_stella" in SERVER_TOOLS and resolve_move_target("http://100.64.0.5:8080/x") == ("100.64.0.5",
                                                                                                  "100.64.0.5")
        assert TOOL_TRIGGERS["move"].search("Stella, move yourself to the server")
        assert check_python_code("import subprocess") and check_python_code("open('/etc/passwd')")
        did = db.create_doc("t1", "Ch 1", "# One\nAlpha\n# Two\nBeta")
        assert db.save_doc(did, "# One\nAlpha!\n# Two\nBeta") == 2 and _find_section(db.doc(did)["text"], "text", "two") == (2, 4) and _find_section("# A\nx\n\n# B\ny", "text", "a") == (0, 2)
        assert not check_python_code("import numpy as np\nprint(np.sqrt(2))\nopen('out.csv', 'w')")
        db.sync(0, [{"id": "t1", "title": "t", "created_at": 1, "updated_at": 1}], [])
        n = db.add_chat_note("t1", "The bracket is 70 mm")
        assert db.chat_notes("t1")[0]["text"] == "The bracket is 70 mm" and db.delete_chat_note("t1", n["id"])
        assert not check_bpy_code("import Part\nadd('Base', box(40, 20, 5).cut(hole(3.2, 10, (5, 10, 6))))",
                                  allowed=CAD_IMPORTS, extra_bad=CAD_BAD_ATTRS)
        assert check_bpy_code("Part.read('/etc/x')", allowed=CAD_IMPORTS, extra_bad=CAD_BAD_ATTRS)
        assert check_bpy_code("import os", allowed=CAD_IMPORTS)
        assert check_bpy_code("pcbnew.SaveBoard('x', board)", allowed=PCB_IMPORTS, extra_bad=PCB_BAD_ATTRS)
        assert check_bpy_code("board.ExportSpecctraDSN('x')", allowed=PCB_IMPORTS, bad_prefixes=("Export",))
        assert not check_bpy_code("move('R1', 10, 5, 90)", allowed=PCB_IMPORTS, extra_bad=PCB_BAD_ATTRS)
        db.db.close()
    pp = prompts_payload()
    assert "{name}" in pp["instructions"]["phone"] and "Android phone" not in pp["instructions"]["web"]
    assert [x["id"] for x in pp["modes"]][:2] == ["general", "programmer"] and len(pp["version"]) == 12
    h = make_http_handler(argparse.Namespace(), None).routes
    assert h.find("GET", "/docs/0123456789/versions/3", "key")[1].group(2) == "3"
    assert h.find("POST", "/tools/web_search", "key")[0] and not h.find("GET", "/nope", "key")[0]
    assert h.find("GET", "/health", "first")[0] and h.find("GET", "/chat", "open")[0]
    print("self-test ok")


RAW_BASE = os.environ.get("STELLAI_UPDATE_BASE", "https://raw.githubusercontent.com")


def check_for_update(repo, token="", path="server/stellai_hub.py"):
    """Downloads the hub from GitHub; if it differs from this file and passes its self-test, installs it.

    Returns True when a new version was installed (the caller restarts)."""
    import hashlib
    me = os.path.abspath(sys.argv[0] if sys.argv[0].endswith(".py") else __file__)
    new = _fetch(f"{RAW_BASE}/{repo}/HEAD/{path}", token)
    with open(me, "rb") as f:
        cur = f.read()
    digest = hashlib.sha256(new).hexdigest()
    if digest == hashlib.sha256(cur).hexdigest():
        return False
    try:
        with open(os.path.join(os.path.dirname(me), "stellai_hub.bad")) as f:
            if digest in f.read().split():
                return False   # this version already failed to start here; wait for a newer one
    except OSError:
        pass
    compile(new, "stellai_hub.py", "exec")                       # must at least be valid Python
    staged = me + ".new"
    with open(staged, "wb") as f:
        f.write(new)
    p = subprocess.run([sys.executable, staged, "--self-test"], capture_output=True, text=True, timeout=120)
    if p.returncode != 0 or "self-test ok" not in p.stdout:
        os.remove(staged)
        detail = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"the new version failed its self-test ({detail[0][:200]}); keeping this one")
    log("update downloaded and checked; installing when Stella is idle")
    if not wait_until_idle(quiet=IDLE_QUIET):
        os.remove(staged)
        return False
    import shutil
    shutil.copy2(me, me + ".bak")                               # the launcher restores this if the update fails
    os.replace(staged, me)
    return True


def update_companions(repo, token=""):
    """Files that live next to the hub and update with it: the model catalog (stellai_tune.py). Best effort."""
    if getattr(sys, "frozen", False):
        return
    here = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv[0].endswith(".py") else __file__))
    for name in ("stellai_tune.py",):
        try:
            new = _fetch(f"{RAW_BASE}/{repo}/HEAD/server/{name}", token)
            compile(new, name, "exec")
            dest = os.path.join(here, name)
            try:
                with open(dest, "rb") as f:
                    if f.read() == new:
                        continue
            except OSError:
                pass
            with open(dest + ".tmp", "wb") as f:
                f.write(new)
            os.replace(dest + ".tmp", dest)
            log(f"updated {name}")
        except Exception as e:
            log(f"couldn't update {name}: {e}")


UPDATE_INTERVAL = int(os.environ.get("STELLAI_UPDATE_INTERVAL", "1800"))   # 30 minutes
IDLE_QUIET = int(os.environ.get("STELLAI_IDLE_QUIET", "120"))             # no requests for 2 minutes


def updater_loop(repo, token="", interval=None):
    interval = interval or UPDATE_INTERVAL
    time.sleep(min(90, interval))
    while True:
        try:
            update_companions(repo, token)
            if check_for_update(repo, token):
                log("installed an update from GitHub; restarting the hub (chats and settings are kept)")
                global EXIT_CODE
                EXIT_CODE = RESTART_CODE
                if WEB:
                    WEB.shutdown()
                return
        except Exception as e:  # never let a failed check stop the hub
            log(f"update check failed: {e}")
        time.sleep(interval)


OPTIONAL_PACKAGES = {"pypdf": "pypdf"}   # import name -> pip package; installed into the hub's venv if missing


def ensure_packages():
    import importlib.util
    if getattr(sys, "frozen", False) or sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        return   # only inside our own virtualenv
    for mod, pkg in OPTIONAL_PACKAGES.items():
        if importlib.util.find_spec(mod) is None:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", pkg], capture_output=True, timeout=600)
            log(f"installed {pkg}" if r.returncode == 0 else f"couldn't install {pkg}")


