"""3D Modeler: workspaces, steps, guard rails, renders, history, export, reference pictures and photo-to-3D (fake Blender)."""
import json
import os
import threading
import time

from helpers import FAKES, check, done, start_hub

h, web, tmp, port = start_hub(extra=["--blender", os.path.join(FAKES, "blender")])
run = h.run_server_tool

r, s = run("blender_project", {"action": "status"})
check(r["ok"] and r["blender"].endswith("blender") and r["active"] is None, "status", r)
open(os.path.join(h.BLENDER["root"], "inbox", "vela.vrm"), "w").write("x")
r, s = run("blender_project", {"action": "new", "name": "vela-halo", "file": "vela.vrm"})
check(r["ok"] and r.get("version") == 1 and r.get("image"), "new workspace from an imported file", r)
r, s = run("blender_step", {"goal": "add halo", "code": "import bmesh\nbm = bmesh.new()\no = new_object('Halo', bm)"})
check(r["ok"] and r["version"] == 2 and r["changes"]["added"][0]["name"] == "Halo1", "a step saves a new version", r)
r, s = run("blender_step", {"goal": "x", "code": "boom()"})
check(not r["ok"] and "NameError" in r["error"] and "still v2" in r["note"], "a failing step saves nothing", r)
r, s = run("blender_step", {"goal": "x", "code": "x = 1  # nothing"})
check(not r["ok"] and "nothing in the scene" in r["error"], "a step that changes nothing is reported", r)
r, s = run("blender_step", {"goal": "x", "code": "import os\nos.system('ls')"})
check(not r["ok"] and "isn't allowed" in r["error"], "unsafe step code is blocked", r)
r, s = run("blender_inspect", {"render": True})
check(r["version"] == 2 and len(r["scene"]["objects"]) == 2 and r.get("image"), "inspect with a render", r)
r, s = run("blender_project", {"action": "revert", "version": 1})
check(r["ok"] and r["version"] == 3, "going back saves a new version", r)
r, s = run("blender_project", {"action": "history"})
check([x["what"] for x in r["history"]] == ["start: vela.vrm", "add halo", "back to v1"], "history", r)
r, s = run("blender_project", {"action": "export", "format": "glb"})
check(os.path.basename(r["file"]) == "vela-halo-v0003.glb", "export", r)
r, s = run("blender_project", {"action": "new", "name": "Bad Name"})
check(not r["ok"], "bad workspace names are refused", r)
check("blender_step" in h.select_tools("hello", "modeler"), "the Modeler mode gets the Blender tools")
check([t for t in h.select_tools("can you fix the mesh on my vrm") if t.startswith("blender")], "3D words bring the Blender tools")
check(not [t for t in h.select_tools("what's the weather") if t.startswith("blender")], "other messages don't")

# a hung Blender is stopped and restarted
try:
    h.blender_call("hang", timeout=2)
    check(False, "a hung call times out")
except RuntimeError:
    pass
check(h.blender_call("ping")["ok"], "Blender restarts after a hang")

# reference pictures and the photo-to-3D generator
for n in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"):
    open(os.path.join(h.IMAGE_DIR, n + ".jpg"), "wb").write(b"\xff\xd8x")
    time.sleep(0.05)
run("blender_project", {"action": "new", "name": "lamp"})
r, s = run("blender_project", {"action": "add_reference", "photos": ["aaaaaaaaaaaaaaaa", "latest"], "labels": ["front", "left"]})
check([x["label"] for x in r["references"]] == ["front", "left"], "reference pictures", r)
r, s = run("blender_project", {"action": "add_reference", "photos": ["[photo cccccccccccccccc]"]})
check(not r["ok"], "an unknown photo is refused", r)
r, s = run("blender_generate", {})
check(not r["ok"] and "isn't installed" in s, "photo-to-3D without the generator", s)
json.dump({"python": os.path.join(FAKES, "shapepy")}, open(os.path.join(h.BLENDER["root"], "generator.json"), "w"))
r, s = run("blender_generate", {})
check(r.get("ok") and [o["name"] for o in r["new_objects"]] == ["Generated"], "photo-to-3D adds a shape", r)
os.environ["FAKE_FREE"] = "2000"
r, s = run("blender_generate", {"photos": ["latest"]})
check(not r.get("ok") and "not enough free graphics memory" in s, "too little graphics memory is explained", s)
llm = os.path.join(tmp, "llm")
os.makedirs(llm)
open(os.path.join(llm, ".launcher-can-pause"), "w").close()
pid = os.path.join(llm, "llama.pid")
open(pid, "w").write("999999")


class SC:
    conf = os.path.join(llm, "model.conf")
    pid_file = pid


h.SERVER_CONTROL = SC()


def watcher():   # the pretend launcher: when asked to pause Stella's model, the graphics memory frees up
    p = os.path.join(llm, ".pause-model")
    for _ in range(100):
        if os.path.exists(p):
            open("/tmp/fake_paused", "w").close()
            return
        time.sleep(0.1)


threading.Thread(target=watcher, daemon=True).start()
h._wait_model = lambda timeout=0: True
r, s = run("blender_generate", {"photos": ["latest"]})
check(r.get("ok") and r["generator"]["paused_stella"] and not os.path.exists(os.path.join(llm, ".pause-model")),
      "Stella's model is paused for the generator and resumed after", r)
if os.path.exists("/tmp/fake_paused"):
    os.remove("/tmp/fake_paused")
r, s = run("blender_project", {"action": "status"})
check(r["graphics_cards"][0]["name"] == "Tesla T4" and r["reference_pictures"] == 2, "status shows cards and pictures", r)
h._blender_stop()
web.server_close()
done()
