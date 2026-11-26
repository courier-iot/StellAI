"""Puts Stella's avatar inside the phone app, so the avatar screen works without the server.

    python3 tools/make_offline_avatar.py [--no-libs]

Run by GitHub Actions before the APK is built (needs Pillow). Writes into app/src/main/assets/avatar/ (next to the
committed offline.html):
  avatar.js           the same avatar code the server serves (server/avatar.js)
  lib/...             three.js and three-vrm, the same versions and files the server downloads (AVATAR_LIBS)
  Stella.vrm          the phone-sized copy of her (textures at most 1024 px, as the server gives phones)
  animations/*.vrma   her shipped animations, without exact duplicates
  anims.json          their names and moments, worked out the same way as on the server
--no-libs skips the downloads (for tests; the page then can't run).
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "app", "src", "main", "assets", "avatar")
LIBS = ("build/three.module.js", "build/three.core.js", "examples/jsm/loaders/GLTFLoader.js",
        "examples/jsm/utils/BufferGeometryUtils.js", "three-vrm.module.min.js", "three-vrm-animation.module.min.js")


def load_hub():
    spec = importlib.util.spec_from_file_location("stellai_hub", os.path.join(ROOT, "server", "stellai_hub.py"))
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)
    return hub


def fetch(urls):
    err = None
    for u in urls:
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "StellAI-build"}), timeout=120) as r:
                return r.read()
        except Exception as e:   # noqa: BLE001 - try the next mirror
            err = e
    raise RuntimeError(f"couldn't download {urls[0]}: {err}")


def main(argv):
    hub = load_hub()
    defaults = os.path.join(ROOT, "server", "avatar_defaults")
    os.makedirs(OUT, exist_ok=True)
    for old in ("lib", "animations"):
        shutil.rmtree(os.path.join(OUT, old), ignore_errors=True)
    if "--no-libs" not in argv:
        for rel in LIBS:
            dest = os.path.join(OUT, "lib", *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(fetch(hub.AVATAR_LIBS[rel]))
    shutil.copyfile(os.path.join(ROOT, "server", "avatar.js"), os.path.join(OUT, "avatar.js"))
    with open(os.path.join(defaults, "Stella.vrm"), "rb") as f:
        full = f.read()
    lite = hub.slim_vrm(full) or full
    with open(os.path.join(OUT, "Stella.vrm"), "wb") as f:
        f.write(lite)
    # the animations, listed exactly as the server lists them (names, moments, duplicates hidden)
    with tempfile.TemporaryDirectory() as tmp:
        hub.AVATAR["dir"] = tmp
        bin_anims = os.path.join(tmp, "bin", "animations")
        shutil.copytree(os.path.join(defaults, "animations"), bin_anims)
        anims = hub.list_animations()
        os.makedirs(os.path.join(OUT, "animations"), exist_ok=True)
        out = []
        for a in anims:
            shutil.copyfile(os.path.join(bin_anims, a["file"]), os.path.join(OUT, "animations", a["file"]))
            out.append({k: a[k] for k in ("id", "file", "type", "name", "when")})
    with open(os.path.join(OUT, "anims.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    size = sum(os.path.getsize(os.path.join(d, n)) for d, _, ns in os.walk(OUT) for n in ns)
    print(f"offline avatar: {len(out)} animations, Stella {len(lite) // 1024} KB, {size // 1024} KB in all -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
