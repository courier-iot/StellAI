"""The avatar inside the phone app: tools/make_offline_avatar.py packs the same code, character and animations the
server uses, and offline.html asks for exactly the library files it packs."""
import json
import os
import re
import subprocess
import sys

from helpers import ROOT, check, done
import stellai_hub as h

sys.path.insert(0, os.path.join(ROOT, "tools"))
import make_offline_avatar as m  # noqa: E402

OUT = m.OUT
p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_offline_avatar.py"), "--no-libs"],
                   capture_output=True, text=True, timeout=300)
check(p.returncode == 0 and "offline avatar:" in p.stdout, "the packing script runs", (p.stdout + p.stderr)[-500:])
with open(os.path.join(OUT, "avatar.js"), "rb") as a, open(os.path.join(ROOT, "server", "avatar.js"), "rb") as b:
    check(a.read() == b.read(), "the app gets the same avatar code as the server")
full = os.path.getsize(os.path.join(ROOT, "server", "avatar_defaults", "Stella.vrm"))
lite = os.path.getsize(os.path.join(OUT, "Stella.vrm"))
check(0 < lite < full * 0.8, "the phone-sized Stella (smaller textures) goes in", (lite, full))
anims = json.load(open(os.path.join(OUT, "anims.json")))
when = {a["name"]: a["when"] for a in anims}
check(when.get("Jumpscare") == "wake" and when.get("Sleeping waiting") == "sleeping" and len(anims) == 8
      and all(os.path.isfile(os.path.join(OUT, "animations", a["file"])) for a in anims), "her animations and moments", when)
check(all(rel in h.AVATAR_LIBS for rel in m.LIBS), "every library file has download addresses on the server's list")
page = open(os.path.join(OUT, "offline.html"), encoding="utf-8").read()
imap = json.loads(re.search(r'<script type="importmap">(.*?)</script>', page, re.S).group(1))["imports"]
wanted = {"./lib/" + rel for rel in m.LIBS if rel.endswith(".js") and "jsm" not in rel and "three.core" not in rel}
check(set(v for v in imap.values() if not v.endswith("/")) == wanted and imap["three/addons/"] == "./lib/examples/jsm/",
      "the page imports exactly what's packed", imap)
for js in re.findall(r'import\("([^"]+)"\)|from "([^"]+)"', open(os.path.join(ROOT, "server", "avatar.js")).read()):
    spec = next(x for x in js if x)
    if spec.startswith("three/addons/"):
        rel = "examples/jsm/" + spec[len("three/addons/"):]
        check(rel in m.LIBS or "FBXLoader" in rel or "fflate" in rel or "NURBS" in rel, f"avatar.js's {spec} is packed", rel)
check("window.stella" in page and "StellaiApp" in page and "anims.json" in page, "the page takes the app's events")
done()
