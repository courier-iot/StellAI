# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- The avatar "bin": one shared folder on the server with every character and animation.
#   bin/characters/*.vrm               characters (the newest is used unless you pick one)
#   bin/animations/*.vrma | *.fbx      animations; "name@greeting.fbx" sets when it plays (idle, greeting, quiet, happy,
#                                      kiss, dance, farewell, wake, surprise, sleep / sleeping / sleepend);
#                                      "name@greeting+farewell.vrma" plays at both
# Drop files in (file manager, network share, scp) or upload them on any avatar screen. Every screen checks the
# bin's revision about once a second and loads what's new; deleted files disappear from every screen.
ANIM_WHEN = ("idle", "greeting", "quiet", "happy", "kiss", "dance", "farewell", "wake", "surprise",
             "sleeping", "sleepend", "sleep", "off")
_WHEN_RX = "|".join(w for w in ANIM_WHEN)


def valid_when(when):
    """One moment ("dance") or several joined with + ("greeting+farewell"); "off" only on its own."""
    parts = str(when or "").lower().split("+")
    if parts == ["off"]:
        return "off"
    parts = [p for p in dict.fromkeys(parts) if p in ANIM_WHEN and p != "off"]
    return "+".join(parts) if parts else ""
BIN_NAME = re.compile(r"[^/\\\x00]{1,120}")
_REV_CACHE = {"t": 0.0, "rev": ""}


def _safe_bin_name(name):
    return bool(BIN_NAME.fullmatch(name)) and name not in (".", "..") and not name.startswith(".")


def _bin_files(kind, exts):
    d = _avatar_path("bin", kind)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        full = os.path.join(d, n)
        if _safe_bin_name(n) and n.lower().endswith(exts) and os.path.isfile(full):
            st = os.stat(full)
            out.append((n, int(st.st_mtime), st.st_size))
    return sorted(out, key=lambda x: -x[1])


_HASHES = {}


def _content_hash(kind, name, v, size):
    """sha256 of a bin file, cached by name, time and size (a few MB each; only read again when they change)."""
    key = (kind, name, v, size)
    if key not in _HASHES:
        try:
            _HASHES[key] = _sha256_file(_avatar_path("bin", kind, name))
        except OSError:
            _HASHES[key] = key
    return _HASHES[key]


def _unique(kind, files):
    """Files with the same contents as another are hidden (the one that ships with Stella is kept, otherwise the
    newest), so the same animation uploaded twice doesn't play twice as often or crowd the settings."""
    shipped = {rel.split("/", 1)[1] for rel in DEFAULT_AVATAR if rel.startswith(kind + "/")}
    keep, seen = [], set()
    for n, v, size in sorted(files, key=lambda f: (f[0] not in shipped, -f[1])):
        h_ = _content_hash(kind, n, v, size)
        if h_ in seen:
            continue
        seen.add(h_)
        keep.append((n, v, size))
    return sorted(keep, key=lambda f: -f[1])


def list_characters():
    return [{"file": n, "name": os.path.splitext(n)[0].replace("_", " "), "v": v, "size": size}
            for n, v, size in _unique("characters", _bin_files("characters", (".vrm",)))]


def _anim_meta():
    try:
        with open(_avatar_path("animations.json")) as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_anim_meta(meta):
    os.makedirs(AVATAR["dir"], exist_ok=True)
    with open(_avatar_path("animations.json.part"), "w") as f:
        json.dump(meta, f, indent=1)
    os.replace(_avatar_path("animations.json.part"), _avatar_path("animations.json"))


def _anim_id(name, v, size):
    import hashlib
    return hashlib.sha1(f"{name}|{v}|{size}".encode()).hexdigest()[:12]


def list_animations():
    meta, out = _anim_meta(), []
    for n, v, size in _unique("animations", _bin_files("animations", (".vrma", ".fbx"))):
        base, ext = os.path.splitext(n)
        m = re.fullmatch(rf"(.*?)@((?:{_WHEN_RX})(?:\+(?:{_WHEN_RX}))*)", base, re.I)
        when = valid_when((meta.get(n) or {}).get("when")) or (valid_when(m.group(2)) if m else "") or "idle"
        out.append({"id": _anim_id(n, v, size), "file": n, "type": ext[1:].lower(), "v": v,
                    "name": (m.group(1) if m else base).replace("_", " ").strip() or base, "when": when})
    return sorted(out, key=lambda a: a["name"].lower())


def avatar_rev():
    """Changes whenever anything in the bin (or the avatar settings) changes."""
    now = time.time()
    if now - _REV_CACHE["t"] < 1.0 and _REV_CACHE["rev"]:
        return _REV_CACHE["rev"]
    import hashlib
    h = hashlib.sha1()
    for kind, exts in (("characters", (".vrm",)), ("animations", (".vrma", ".fbx"))):
        for item in _bin_files(kind, exts):
            h.update(repr((kind,) + item).encode())
    for f in ("config.json", "animations.json"):
        try:
            h.update(str(os.path.getmtime(_avatar_path(f))).encode())
        except OSError:
            pass
    _REV_CACHE.update(t=now, rev=h.hexdigest()[:12])
    return _REV_CACHE["rev"]


# ---- Lighter characters for phones and small screens ------------------------------------------
# VRoid characters carry 2048-4096 px textures: most of the download, and hundreds of MB of graphics memory once
# loaded. Phones and light-mode screens get a copy with textures capped at 1024 px (made once, kept in
# avatar/lite). The original file is never changed, and big screens still get it.
LITE_TEXTURE = 1024
_LITE = {"lock": threading.Lock(), "busy": set(), "failed": set()}


def slim_vrm(data, max_px=LITE_TEXTURE):
    """A copy of a .vrm/.glb with oversized textures scaled down, or None when there's nothing to gain."""
    import struct
    from PIL import Image
    if len(data) < 28 or data[:4] != b"glTF":
        return None
    version, total = struct.unpack_from("<II", data, 4)
    jlen, jtype = struct.unpack_from("<II", data, 12)
    if jtype != 0x4E4F534A:
        return None
    doc = json.loads(data[20:20 + jlen].decode("utf-8"))
    off = 20 + jlen
    if off + 8 > len(data):
        return None
    blen, btype = struct.unpack_from("<II", data, off)
    if btype != 0x004E4942:
        return None
    blob = memoryview(data)[off + 8:off + 8 + blen]
    views = doc.get("bufferViews") or []
    if len(doc.get("buffers") or []) != 1 or any(v.get("buffer", 0) != 0 for v in views):
        return None
    replaced = {}
    for img in doc.get("images") or []:
        vi = img.get("bufferView")
        if vi is None or vi in replaced or not 0 <= vi < len(views):
            continue
        v = views[vi]
        raw = bytes(blob[v.get("byteOffset", 0):v.get("byteOffset", 0) + v["byteLength"]])
        try:
            im = Image.open(io.BytesIO(raw))
            fmt = im.format
            if fmt not in ("PNG", "JPEG") or max(im.size) <= max_px:
                continue
            scale = max_px / max(im.size)
            size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            if im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGBA" if "A" in im.mode or "transparency" in im.info else "RGB")
            small = im.resize(size, Image.LANCZOS)
            out = io.BytesIO()
            if fmt == "JPEG":
                small.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
            else:
                small.save(out, "PNG", optimize=True)
        except Exception:   # noqa: BLE001 - an image Pillow can't read stays as it is
            continue
        if out.tell() < len(raw):
            replaced[vi] = out.getvalue()
    if not replaced:
        return None
    # Rebuild the binary part: every view in its original order, 4-byte aligned, with the new image bytes
    parts, pos = [], 0
    for i in sorted(range(len(views)), key=lambda k: views[k].get("byteOffset", 0)):
        v = views[i]
        chunk = replaced.get(i)
        if chunk is None:
            chunk = bytes(blob[v.get("byteOffset", 0):v.get("byteOffset", 0) + v["byteLength"]])
        pad = (-pos) % 4
        parts.append(b"\0" * pad)
        pos += pad
        v["byteOffset"], v["byteLength"] = pos, len(chunk)
        parts.append(chunk)
        pos += len(chunk)
    parts.append(b"\0" * ((-pos) % 4))
    binary = b"".join(parts)
    doc["buffers"][0]["byteLength"] = len(binary)
    js = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    js += b" " * ((-len(js)) % 4)
    return (b"glTF" + struct.pack("<II", version, 12 + 8 + len(js) + 8 + len(binary)) +
            struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(binary), 0x004E4942) + binary)


def lite_character(name):
    """Path of the lighter copy of a character, or None while it's being made (or when it can't be). The first
    request starts the work in the background and gets the original, so nobody waits."""
    src = _avatar_path("bin", "characters", name)
    try:
        st = os.stat(src)
    except OSError:
        return None
    tag = f"{int(st.st_mtime)}-{st.st_size}"
    folder = _avatar_path("lite")
    dest = os.path.join(folder, f"{name}.{tag}.vrm")
    if os.path.isfile(dest):
        return dest if os.path.getsize(dest) > 0 else None    # empty marker: nothing to gain for this file
    key = (name, tag)
    with _LITE["lock"]:
        if key in _LITE["busy"] or key in _LITE["failed"]:
            return None
        _LITE["busy"].add(key)

    def work():
        try:
            started = time.time()
            with open(src, "rb") as f:
                data = f.read()
            small = slim_vrm(data)
            os.makedirs(folder, exist_ok=True)
            for old in os.listdir(folder):               # copies of earlier versions of this character
                if old.startswith(name + ".") and old != os.path.basename(dest):
                    try:
                        os.remove(os.path.join(folder, old))
                    except OSError:
                        pass
            with open(dest + ".part", "wb") as f:
                f.write(small or b"")
            os.replace(dest + ".part", dest)
            if small:
                log(f"avatar: lighter copy of {name} for phones: {len(data) / 1e6:.1f} MB -> {len(small) / 1e6:.1f} MB "
                    f"({time.time() - started:.1f}s)")
        except ImportError:
            _LITE["failed"].add(key)
        except Exception as e:   # noqa: BLE001 - screens simply keep using the original
            _LITE["failed"].add(key)
            log(f"avatar: couldn't make a lighter copy of {name}: {e}")
        finally:
            _LITE["busy"].discard(key)

    threading.Thread(target=work, daemon=True).start()
    return None


def _free_name(folder, name):
    base, ext = os.path.splitext(name)
    cand, i = name, 2
    while os.path.exists(os.path.join(folder, cand)):
        cand, i = f"{base} ({i}){ext}", i + 1
    return cand


def _clean_name(name, ext):
    name = re.sub(r"[^\w .()@-]+", "_", os.path.basename(name or "")).strip(" .") or "file"
    if not name.lower().endswith("." + ext):
        name += "." + ext
    return name[-120:]


def add_character(data, name):
    if not data or data[:4] != b"glTF":
        raise ValueError("send a .vrm file (VRoid Studio exports these), under 120 MB")
    folder = _avatar_path("bin", "characters")
    os.makedirs(folder, exist_ok=True)
    fname = _free_name(folder, _clean_name(name or "character", "vrm"))
    with open(os.path.join(folder, fname + ".part"), "wb") as f:
        f.write(data)
    os.replace(os.path.join(folder, fname + ".part"), os.path.join(folder, fname))
    _REV_CACHE["t"] = 0
    return fname


def add_animation(data, name, when, ext):
    if ext not in ("vrma", "fbx"):
        raise ValueError("animations must be .vrma (VRM Animation) or .fbx (Mixamo)")
    if ext == "vrma" and data[:4] != b"glTF":
        raise ValueError("that isn't a .vrma file")
    if ext == "fbx" and not (data[:18] == b"Kaydara FBX Binary" or b"FBXHeaderExtension" in data[:2000]):
        raise ValueError("that isn't an .fbx file")
    folder = _avatar_path("bin", "animations")
    os.makedirs(folder, exist_ok=True)
    fname = _free_name(folder, _clean_name(name or "animation", ext))
    with open(os.path.join(folder, fname), "wb") as f:
        f.write(data)
    meta = _anim_meta()
    meta[fname] = {"when": valid_when(when) or "idle"}
    _save_anim_meta(meta)
    _REV_CACHE["t"] = 0
    return list_animations()


def update_animation(aid, change):
    for a in list_animations():
        if a["id"] != aid:
            continue
        meta = _anim_meta()
        if change.get("delete"):
            try:
                os.remove(_avatar_path("bin", "animations", a["file"]))
            except OSError:
                pass
            meta.pop(a["file"], None)
        elif valid_when(change.get("when")):
            meta.setdefault(a["file"], {})["when"] = valid_when(change["when"])
        _save_anim_meta(meta)
        _REV_CACHE["t"] = 0
        return list_animations()
    raise KeyError("no such animation")


def migrate_avatar_files():
    """Earlier versions kept model.vrm and anims/ next to the settings; move them into the bin."""
    try:
        old = _avatar_path("model.vrm")
        if os.path.isfile(old):
            os.makedirs(_avatar_path("bin", "characters"), exist_ok=True)
            os.replace(old, os.path.join(_avatar_path("bin", "characters"), _free_name(_avatar_path("bin", "characters"), "character.vrm")))
        try:
            with open(_avatar_path("animations.json")) as f:
                old_list = json.load(f)
        except (OSError, ValueError):
            old_list = None
        if isinstance(old_list, list):
            new_meta = {}
            os.makedirs(_avatar_path("bin", "animations"), exist_ok=True)
            for a in old_list:
                src = _avatar_path("anims", a.get("file", ""))
                if os.path.isfile(src):
                    fname = _free_name(_avatar_path("bin", "animations"), _clean_name(a.get("name"), a.get("type", "fbx")))
                    os.replace(src, os.path.join(_avatar_path("bin", "animations"), fname))
                    new_meta[fname] = {"when": a.get("when", "idle")}
            _save_anim_meta(new_meta)
    except OSError as e:
        log(f"avatar: couldn't move old files into the bin: {e}")


# ---- Stella's own character and animations, shipped with StellAI ----------------------------------------
# They live in the repo (server/avatar_defaults/) and in the Windows app. Each server copies them into its bin once:
# from a local copy when there is one, otherwise downloaded from GitHub. A file you delete stays deleted, and a file
# you replaced (same name, different contents) is left alone. When a newer version ships, an untouched copy is
# swapped for it. The first time Stella arrives she becomes the character on every screen.
DEFAULT_AVATAR = {   # bin path -> sha256 of the shipped file (tests check these match server/avatar_defaults)
    "characters/Stella.vrm": "d48851d4af1d21ae0ddd00ec42be72dbbbaf2e8484452ec9eb88bca6444343b1",
    "animations/Peace@greeting+farewell.vrma": "6f66b6b5c0214a7b825498050380ea43a3f782b5ea24ce46f7096acf15866d75",
    "animations/Twirl@dance.vrma": "f45abc44800c647fa106537a2e31a84cb83b74cf3dd8a0ee71c6915f1555ac57",
    "animations/Pull_the_trigger@quiet.vrma": "c40c1a7b0dbcac5aa9582f45498474255e4b0ca5642a71ffaf43878c27bedf70",
    "animations/Jumpscare@wake.vrma": "6322bbe2df7716529dd53e2a61816e67b946416d3761c672b8d50ee2829825bb",
    # converted from Unity Humanoid clips (tools/unity2vrma.py)
    "animations/Cute_surprise@surprise.vrma": "60fdb54127f50bd9dd7c3398108cb2de570f7d44fc727e73a54e28aaec31d0e6",
    "animations/Sleeping_waiting_start@sleep.vrma": "6b4f2c8112960e677cd57c740c11aaa6ab1bf912e2762e200916de5ef8dd1979",
    "animations/Sleeping_waiting@sleeping.vrma": "5b16c87a021ba36cb4555fd1dbc31d361b7b86bf71b9acee3667002e73890613",
    "animations/Sleeping_waiting_end@sleepend.vrma": "215ec3a59e2b18cd68c88d58214ced8cdc4e63ae554a0cf9b4f6549f35fd6a0f",
}
DEFAULT_AVATAR_REPO = "courier-iot/StellAI"
DEFAULT_CHARACTER = "Stella.vrm"


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _default_avatar_sources():
    """Folders that may hold a local copy of server/avatar_defaults (repo checkout, setup folder, Windows app)."""
    here = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0].endswith(".py") else __file__))
    out = [os.path.join(here, "avatar_defaults"), os.path.join(os.path.dirname(here), "server", "avatar_defaults")]
    if getattr(sys, "_MEIPASS", ""):
        out.insert(0, os.path.join(sys._MEIPASS, "avatar_defaults"))
    return [d for d in out if os.path.isdir(d)]


def seed_default_avatar(repo="", fetch=None):
    """Puts Stella's shipped character and animations into the bin (see above). Returns the files added."""
    if not AVATAR["dir"]:
        return []
    state_path = _avatar_path("defaults.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
        state = state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        state = {}
    first_time = not state
    added = []
    for rel, sha in DEFAULT_AVATAR.items():
        dest = _avatar_path("bin", *rel.split("/"))
        had = state.get(rel)
        if had == sha:
            continue                                        # already have this version (or deleted it on purpose)
        if had:
            # an older shipped version: update it only if it's still there and untouched
            if not os.path.isfile(dest) or _sha256_file(dest) != had:
                state[rel] = sha
                continue
        elif os.path.isfile(dest):
            state[rel] = sha if _sha256_file(dest) == sha else "user"     # same name already there: keep theirs
            continue
        data = None
        for folder in _default_avatar_sources():
            src = os.path.join(folder, *rel.split("/")[1:]) if rel.startswith("characters/") else \
                os.path.join(folder, *rel.split("/"))
            if os.path.isfile(src) and _sha256_file(src) == sha:
                with open(src, "rb") as f:
                    data = f.read()
                break
        if data is None:
            url = (f"{RAW_BASE}/{repo or DEFAULT_AVATAR_REPO}/HEAD/server/avatar_defaults/"
                   + "/".join(urllib.parse.quote(x) for x in rel.split("/")[1 if rel.startswith("characters/") else 0:]))
            try:
                data = (fetch or _fetch)(url, "", 300)
            except Exception as e:      # offline, GitHub down: try again next start
                log(f"avatar: couldn't download Stella's {rel.split('/')[-1]}: {e}")
                continue
            import hashlib
            if hashlib.sha256(data).hexdigest() != sha:
                log(f"avatar: the downloaded {rel.split('/')[-1]} didn't match; skipped")
                continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".part", "wb") as f:
            f.write(data)
        os.replace(dest + ".part", dest)
        state[rel] = sha
        added.append(rel)
    os.makedirs(AVATAR["dir"], exist_ok=True)
    with open(state_path + ".part", "w") as f:
        json.dump(state, f, indent=1)
    os.replace(state_path + ".part", state_path)
    if "characters/" + DEFAULT_CHARACTER in added and (first_time or not state.get("chosen")):
        # Stella arrives: she becomes the character, shown in 3D
        try:
            with open(_avatar_path("config.json")) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        cfg.update(character=DEFAULT_CHARACTER, style="vrm")
        with open(_avatar_path("config.json.part"), "w") as f:
            json.dump(cfg, f, indent=1)
        os.replace(_avatar_path("config.json.part"), _avatar_path("config.json"))
        state["chosen"] = True
        with open(state_path, "w") as f:
            json.dump(state, f, indent=1)
    if not state.get("style3d"):
        # Once (0.37.0): Stella in 3D is the default look, also on servers where her file was already there (an
        # earlier upload of the same name, which the step above leaves alone) or that were set to the orb by default.
        chars = [c["file"] for c in list_characters()]
        if chars:
            try:
                with open(_avatar_path("config.json")) as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                cfg = {}
            cfg["style"] = "vrm"
            if DEFAULT_CHARACTER in chars and (not state.get("chosen") or cfg.get("character") not in chars):
                cfg["character"] = DEFAULT_CHARACTER
            with open(_avatar_path("config.json.part"), "w") as f:
                json.dump(cfg, f, indent=1)
            os.replace(_avatar_path("config.json.part"), _avatar_path("config.json"))
            state.update(style3d=True, chosen=True)
            with open(state_path, "w") as f:
                json.dump(state, f, indent=1)
            _REV_CACHE["t"] = 0
    if added:
        _REV_CACHE["t"] = 0
        log("avatar: added Stella's " + ", ".join(r.split("/")[-1] for r in added))
    return added


def _safe_seed(repo):
    try:
        seed_default_avatar(repo)
    except Exception as e:        # never stops the hub from starting
        log(f"avatar: couldn't add Stella's default files: {e}")


AVATAR_JS = EMBED("avatar.js")  # end of avatar script  (build_hub.py puts avatar.js here)


