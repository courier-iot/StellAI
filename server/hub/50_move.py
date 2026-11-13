# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Moving Stella to another server ---------------------------------------------------------------------------
# "Stella, move yourself to the server": this server packs everything (a normal backup plus the API key and Auto
# mode's memory), sends it over Tailscale to the other StellAI server, which restores it and restarts as Stella.
# This one becomes a client: its model and voice stop, and it stays up as a signpost that points phones, browsers
# and avatar screens to the new address, so they follow by themselves. Models don't travel: each computer keeps
# the ones that suit its own hardware. The receiving server only accepts a move from a device on the same
# Tailscale account (or with its API key).
MOVE = {"state": "", "target": "", "error": "", "tokens": {}, "signpost": None, "arrived": None, "base": "",
        "lock": threading.Lock(), "restart_hook": None, "client_hook": None}
MOVE_TTL = 300            # a confirmation token is valid for 5 minutes
MOVE_RESTART_CODE = 76   # "start me again" after a move arrived (not 75: that one means "after an update")
MOVE_OPEN = ("/health", "/move/hello", "/move/receive", "/move/status")


def _move_path(*parts):
    return os.path.join(MOVE["base"] or os.path.dirname(os.path.abspath(CHATS_PATH or ".")), *parts)


def move_id():
    """This installation's id (so the old server can recognise its own arrival on the new one)."""
    p = _move_path("move-id.txt")
    try:
        with open(p) as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    v = uuid.uuid4().hex
    try:
        with open(p, "w") as f:
            f.write(v)
    except OSError:
        pass
    return v


def _tailscale_exe():
    import shutil
    w = shutil.which("tailscale")
    if w:
        return w
    for c in (r"C:\Program Files\Tailscale\tailscale.exe", "/usr/bin/tailscale", "/usr/sbin/tailscale"):
        if os.path.isfile(c):
            return c
    return ""


def _tailscale_json(*args, timeout=15):
    exe = _tailscale_exe()
    if not exe:
        return None
    try:
        p = subprocess.run([exe, *args, "--json"], capture_output=True, text=True, timeout=timeout,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def tailscale_devices():
    """(your devices on Tailscale, this device's user id). Each: name, dns, ip, online, os, user."""
    st = _tailscale_json("status")
    if not st:
        return [], None
    me = (st.get("Self") or {}).get("UserID")
    out = []
    for p in (st.get("Peer") or {}).values():
        ips = p.get("TailscaleIPs") or []
        out.append({"name": p.get("HostName", ""), "dns": (p.get("DNSName") or "").split(".")[0],
                    "ip": next((i for i in ips if "." in i), ips[0] if ips else ""), "online": bool(p.get("Online")),
                    "os": p.get("OS", ""), "user": p.get("UserID"), "mine": p.get("UserID") == me})
    return out, me


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def resolve_move_target(target):
    """(address, display name) for a Tailscale device name ("the server", "dl380p") or an address."""
    t = str(target or "").strip()
    t = re.sub(r"^(https?://)", "", t).split("/")[0]
    t = re.sub(r":\d+$", "", t)
    if not t:
        raise ValueError("say which computer to move to: its Tailscale name or its address")
    if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", t) or "." in t:
        return t, t
    devices, _ = tailscale_devices()
    mine = [d for d in devices if d["mine"]]
    want = _norm_name(re.sub(r"^(the|my)\s+", "", t, flags=re.I))
    exact = [d for d in mine if want in (_norm_name(d["name"]), _norm_name(d["dns"]))]
    near = exact or [d for d in mine if want and (want in _norm_name(d["name"]) or want in _norm_name(d["dns"]))]
    if len(near) == 1:
        return near[0]["ip"], near[0]["name"] or near[0]["dns"]
    names = ", ".join(sorted(d["name"] or d["dns"] for d in mine)) or "none found (is Tailscale installed here?)"
    if len(near) > 1:
        raise ValueError(f"'{t}' matches several of your devices ({', '.join(d['name'] for d in near)}); say which one")
    raise ValueError(f"no Tailscale device of yours is called '{t}'. Your devices: {names}")


def _hello(addr, timeout=6):
    host = f"[{addr}]" if ":" in addr and not addr.startswith("[") else addr
    with urllib.request.urlopen(f"http://{host}:8092/move/hello", timeout=timeout) as r:
        return json.loads(r.read())


def move_hello():
    import socket
    return {"stellai": True, "name": socket.gethostname(), "id": move_id(), "state": MOVE["state"] or "ready",
            "signpost": bool(MOVE["signpost"]), "arrived": MOVE["arrived"], "accepts": not MOVE["state"]}


def tool_move_stella(args):
    target, token = str(args.get("target", "")).strip(), str(args.get("confirm_token", "")).strip()
    if MOVE["state"] == "moving":
        return {"moving": True, "target": MOVE["target"]}, "Already moving"
    if MOVE["signpost"]:
        raise ValueError(f"Stella already moved from here to {MOVE['signpost'].get('name')}; this computer is a client")
    if token:
        with MOVE["lock"]:
            t = MOVE["tokens"].pop(token, None)
        if not t or time.time() - t["at"] > MOVE_TTL:
            raise ValueError("that confirmation expired: ask again where to move, and confirm")
        reply = str(args.get("user_reply", "")).lower()
        if not re.search(r"\b(yes|yeah|yep|sure|ok|okay|go|do it|confirm|move)\b", reply) or \
                re.search(r"\b(no|don't|do not|stop|cancel|wait)\b", reply):
            raise ValueError("the user hasn't confirmed: only move after they say yes")
        threading.Thread(target=_run_move, args=(t["addr"], t["name"], str(args.get("target_key", "") or "")),
                         daemon=True, name="move").start()
        return {"started": True, "target": t["name"],
                "say": f"Moving to {t['name']} now. It takes a few minutes; I'll say when I'm there."}, \
            f"Moving to {t['name']}"
    addr, name = resolve_move_target(target)
    try:
        h = _hello(addr)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ValueError(f"{name} doesn't answer as a StellAI server ({e}). Install StellAI there first (any model) "
                         "and make sure it's running.")
    if h.get("id") == move_id():
        raise ValueError("that's this computer")
    if not h.get("accepts"):
        raise ValueError(f"{h.get('name', name)} is busy ({h.get('state')}); try again in a few minutes")
    tok = uuid.uuid4().hex[:12]
    with MOVE["lock"]:
        MOVE["tokens"] = {k: v for k, v in MOVE["tokens"].items() if time.time() - v["at"] < MOVE_TTL}
        MOVE["tokens"][tok] = {"addr": addr, "name": h.get("name") or name, "at": time.time()}
    return {"needs_confirmation": True, "confirm_token": tok, "target": h.get("name") or name, "address": addr,
            "moves": "chats, memories, projects, reminders and lists, photos, diagrams, wake words, the avatar and "
                     "its settings, modes, the API key",
            "stays": "the models (the other computer uses its own); this computer becomes a client of it",
            "ask": f"Ask the user to confirm moving to {h.get('name') or name}. Only after they say yes, call "
                   "move_stella again with confirm_token and their exact words as user_reply."}, \
        f"Ready to move to {h.get('name') or name} (needs a yes)"


def _move_package():
    """A normal backup plus what a move also needs: the API key and Auto mode's topics."""
    import shutil
    import zipfile
    src = backup_now()
    pkg = _move_path(f"move-{uuid.uuid4().hex[:8]}.zip")
    shutil.copyfile(src, pkg)
    with zipfile.ZipFile(pkg, "a", zipfile.ZIP_DEFLATED) as z:
        import socket
        z.writestr("move.json", json.dumps({"api_key": API_KEY, "from": socket.gethostname(), "from_id": move_id(),
                                            "at": int(time.time())}))
        if AUTO_STATE.get("path") and os.path.isfile(AUTO_STATE["path"]):
            z.write(AUTO_STATE["path"], "files/auto-modes.json")
    return pkg


def _announce(text):
    try:
        AVATAR_BUS.post({"state": "reply", "text": text, "source": "server-move", "device": "server"})
    except Exception:
        pass


def _run_move(addr, name, target_key=""):
    MOVE.update(state="moving", target=name, error="")
    _announce(f"Moving to {name}. Give me a few minutes; this screen follows me there by itself.")
    log(f"move: packing up for {name} ({addr})")
    pkg = ""
    try:
        time.sleep(3)                    # anything already on its way lands before the snapshot
        pkg = _move_package()
        size = os.path.getsize(pkg)
        host = f"[{addr}]" if ":" in addr else addr
        log(f"move: sending {size // 1048576} MB to {name}")
        with open(pkg, "rb") as f:
            req = urllib.request.Request(f"http://{host}:8092/move/receive", data=f, method="POST",
                                         headers={"Content-Type": "application/zip", "Content-Length": str(size),
                                                  **({"Authorization": f"Bearer {target_key}"} if target_key else {})})
            with urllib.request.urlopen(req, timeout=max(300, size // 200_000)) as r:
                json.loads(r.read() or b"{}")
        log(f"move: {name} accepted it; waiting for it to restart as Stella")
        me, deadline = move_id(), time.time() + 900
        while time.time() < deadline:
            time.sleep(5)
            try:
                h = _hello(addr, timeout=5)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if (h.get("arrived") or {}).get("from_id") == me:
                break
        else:
            raise RuntimeError(f"{name} took the data but didn't come back within 15 minutes; check it there")
        info = {"name": name, "address": addr, "llama_url": f"http://{host}:8080", "hub_url": f"http://{host}:8092",
                "at": int(time.time())}
        with open(_move_path("moved.json"), "w") as f:
            json.dump(info, f)
        _announce(f"Done: I'm on {name} now. This computer is a client; this screen switches over in a moment.")
        log(f"move: Stella is on {name} now; this server becomes a client")
        time.sleep(15)                   # let screens show and say it before they're redirected
        MOVE.update(signpost=info, state="")
        _become_client(info)
    except Exception as e:
        MOVE.update(state="", error=str(e))
        log(f"move failed: {e}")
        _announce(f"The move to {name} didn't work, so I'm staying here. {str(e)[:200]}")
    finally:
        if pkg:
            _quiet_remove(pkg)


def _become_client(info):
    """Frees this computer: the model and voice stop; the hub stays as a signpost to the new address."""
    if MOVE["client_hook"]:              # the Windows app switches itself to client mode
        try:
            MOVE["client_hook"](info)
        except Exception as e:
            log(f"move: couldn't switch the app to client mode: {e}")
        return
    pid_file = getattr(SERVER_CONTROL, "pid_file", "") if SERVER_CONTROL else ""
    if pid_file:                         # the Linux launcher sees moved.json and doesn't start the model again
        try:
            with open(pid_file) as f:
                os.kill(int(f.read().strip()), 15)
        except (OSError, ValueError):
            pass


def _peer_is_mine(ip):
    """Is the computer at this address one of the same person's Tailscale devices?"""
    who = _tailscale_json("whois", ip)
    st = _tailscale_json("status")
    if not who or not st:
        return False
    me = (st.get("Self") or {}).get("UserID")
    peer = (who.get("UserProfile") or {}).get("ID")
    return bool(me) and me == peer


def receive_move(handler, cfg):
    """POST /move/receive: saves the package, checks it, and restarts this server as Stella."""
    import zipfile
    ip = handler.client_address[0]
    if not (handler.authorized() or _peer_is_mine(ip)):
        return handler.send_json(403, {"error": "moves are only accepted from your own Tailscale devices, or with "
                                                "this server's API key"})
    if MOVE["state"]:
        return handler.send_json(409, {"error": f"busy ({MOVE['state']})"})
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0 or n > 40 * 2**30:
        return handler.send_json(400, {"error": "send the move package (up to 40 GB)"})
    MOVE["state"] = "receiving"
    dest = _move_path("incoming-move.zip")
    try:
        left = n
        with open(dest + ".part", "wb") as f:
            while left > 0:
                buf = handler.rfile.read(min(left, 1 << 20))
                if not buf:
                    raise OSError("the transfer stopped early")
                f.write(buf)
                left -= len(buf)
        with zipfile.ZipFile(dest + ".part") as z:
            names = z.namelist()
            if "chats.db" not in names or "move.json" not in names:
                raise ValueError("that isn't a StellAI move package")
            info = json.loads(z.read("move.json"))
            if z.testzip() is not None:
                raise ValueError("the package arrived damaged")
        os.replace(dest + ".part", dest)
        with open(_move_path("pending-move.json"), "w") as f:
            json.dump({"zip": dest, "from": info.get("from", "?"), "from_id": info.get("from_id", ""),
                       "at": int(time.time())}, f)
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        MOVE["state"] = ""
        _quiet_remove(dest + ".part")
        return handler.send_json(400, {"error": f"move not accepted: {e}"})
    log(f"move: received Stella from {info.get('from')} ({n // 1048576} MB); restarting to take over")
    _announce(f"Stella is moving here from {info.get('from')}. One moment…")
    handler.send_json(200, {"accepted": True})
    threading.Thread(target=_restart_for_move, daemon=True).start()


def _restart_for_move():
    time.sleep(2)
    if MOVE["restart_hook"]:             # the Windows app restarts its servers
        MOVE["restart_hook"]()
        return
    global EXIT_CODE
    if MOVE["signpost"]:                 # this was a client: the launcher starts everything again
        _quiet_remove(_move_path("moved.json"))
        EXIT_CODE = 0
    else:
        EXIT_CODE = MOVE_RESTART_CODE     # the launcher starts the hub again in a few seconds
    if WEB:
        WEB.shutdown()


def apply_pending_move(cfg):
    """At start-up, before anything else: puts a received move in place (data, API key, Auto mode topics)."""
    import zipfile
    base = os.path.dirname(os.path.abspath(cfg.db))
    pend = os.path.join(base, "pending-move.json")
    if not os.path.isfile(pend):
        return None
    try:
        with open(pend) as f:
            info = json.load(f)
        zpath = info["zip"]
        log(f"move: restoring what came from {info.get('from')}")
        restore_backup(zpath, cfg)
        with zipfile.ZipFile(zpath) as z:
            meta = json.loads(z.read("move.json"))
            if "files/auto-modes.json" in z.namelist():
                with open(os.path.join(base, "auto-modes.json"), "wb") as out:
                    out.write(z.read("files/auto-modes.json"))
        key = str(meta.get("api_key") or "").strip()
        if key:
            with open(cfg.key_file, "w") as f:
                f.write(key)
        _quiet_remove(os.path.join(base, "moved.json"))   # whatever this computer was, it's Stella now
        info["arrived_at"] = int(time.time())
        with open(os.path.join(base, "arrived.json"), "w") as f:
            json.dump(info, f)
        # the model server must use the API key that came along (Linux launcher: it re-reads the key file)
        conf, pid_file = getattr(cfg, "model_conf", ""), getattr(cfg, "llama_pid", "")
        if conf and pid_file and os.path.isfile(pid_file):
            open(os.path.join(os.path.dirname(conf), ".switch-model"), "w").close()
            try:
                with open(pid_file) as f:
                    os.kill(int(f.read().strip()), 15)
            except (OSError, ValueError):
                pass
        log(f"move: Stella moved here from {info.get('from')}")
        return info
    except Exception as e:
        log(f"move: couldn't put the received data in place ({e}); keeping what was here")
        return None
    finally:
        _quiet_remove(pend)
        try:
            _quiet_remove(info["zip"])
        except (NameError, KeyError, TypeError):
            pass


SERVER_TOOLS["move_stella"] = (tool_move_stella, _fn(
    "move_stella",
    "Move Stella (all chats, memories, projects, settings, avatar, API key) from this server to another StellAI "
    "server on the user's Tailscale network, e.g. from a desktop to the home server. This computer then becomes a "
    "client and the phones, browsers and avatar screens follow her by themselves. Models stay: the other computer "
    "uses its own. Two calls: first with target only (it checks the other server and returns confirm_token), "
    "then ask the user to confirm and call again with confirm_token and their exact reply as user_reply. Never "
    "skip asking.",
    {"target": _p("The other computer: its Tailscale name ('server', 'dl380p') or address"),
     "confirm_token": _p("From the first call, after the user said yes"),
     "user_reply": _p("The user's exact words confirming the move"),
     "target_key": _p("Only if the other server isn't on the user's Tailscale account: its API key")},
    ["target"]))


