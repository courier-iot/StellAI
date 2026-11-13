"""Moving Stella: confirm first, copy everything to the other server, the old one becomes a signpost.

The target server runs as a real hub process on port 8092 (moves always use that port), restarted the way the Linux
launcher does when it exits with 75/76."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from helpers import SERVER, check, done, hub_args
import stellai_hub as h

B = tempfile.mkdtemp()
argsB = hub_args(B, key="keyB", extra=["--avatar-dir", os.path.join(B, "av")])
argsB[argsB.index("--app-port") + 1] = "8092"
stop = threading.Event()


def launcher():
    while not stop.is_set():
        p = subprocess.run([sys.executable, os.path.join(SERVER, "stellai_hub.py")] + argsB, capture_output=True, text=True)
        open(os.path.join(B, "hub.log"), "a").write(p.stdout + p.stderr + f"\n== exit {p.returncode}\n")
        if p.returncode not in (75, 76):
            break


threading.Thread(target=launcher, daemon=True).start()
for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:8092/health", timeout=1)
        break
    except Exception:
        time.sleep(0.5)
A = tempfile.mkdtemp()
web = h.start(h.parse_args(hub_args(A, key="keyA", extra=["--avatar-dir", os.path.join(A, "av")])))
threading.Thread(target=web.serve_forever, daemon=True).start()
h.CHATS.add_memory("Owns a DL380p Gen8 with a Tesla T4", "user")
check(h._hello("127.0.0.1")["stellai"], "the other server answers hello")
r, s = h.run_server_tool("move_stella", {"target": "127.0.0.1"})
check(r.get("needs_confirmation"), "a move needs a yes first", r)
r, s = h.run_server_tool("move_stella", {"target": "127.0.0.1", "confirm_token": r["confirm_token"], "user_reply": "no wait"})
check(not r.get("ok") and "hasn't confirmed" in str(r.get("error")), "no means no", r)
r, s = h.run_server_tool("move_stella", {"target": "127.0.0.1"})
r, s = h.run_server_tool("move_stella", {"target": "127.0.0.1", "confirm_token": r["confirm_token"], "user_reply": "yes, do it",
                                         "target_key": "keyB"})
for _ in range(120):
    if h.MOVE["signpost"] or h.MOVE["error"]:
        break
    time.sleep(1)
check(h.MOVE["signpost"] and not h.MOVE["error"], "the move finished", (h.MOVE["signpost"], h.MOVE["error"]))
check(open(os.path.join(B, "k")).read() == "keyA", "the new server took over the API key")
req = urllib.request.Request("http://127.0.0.1:8092/memories", headers={"Authorization": "Bearer keyA"})
mems = [m["text"] for m in json.loads(urllib.request.urlopen(req).read()).get("memories", [])]
check(mems == ["Owns a DL380p Gen8 with a Tesla T4"], "memories moved", mems)
port = web.server_address[1]
try:
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/memories", headers={"Authorization": "Bearer keyA"}))
    check(False, "the old server answers as a signpost")
except urllib.error.HTTPError as e:
    check(e.code == 410 and json.loads(e.read())["moved_to"]["hub_url"] == "http://127.0.0.1:8092", "410 with the new address")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


try:
    urllib.request.build_opener(NoRedirect).open(f"http://127.0.0.1:{port}/avatar")
    check(False, "pages redirect")
except urllib.error.HTTPError as e:
    check(e.code == 302 and e.headers["Location"].startswith("http://127.0.0.1:8092/avatar?moved_from="), "pages redirect")
check(h._hello("127.0.0.1").get("arrived"), "the new server knows where it came from")
stop.set()
web.shutdown()
subprocess.run(["pkill", "-f", f"stellai_hub.py.*{B}"])
done()
