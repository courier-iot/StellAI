# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- The deep lane: a bigger "thinking" model in system RAM -------------------------------------------------------
# Stella's everyday model lives on the graphics card: quick, but a graphics card holds only a small model. System RAM
# is bigger and cheap, and a mixture-of-experts model (35B of knowledge, only ~3B used per word) runs from it at a
# usable speed on the processor. Design work, multi-step plans and anything the quick model fails at twice go to it.
#
#  · Finding one: Server → Bigger brain → "Find one that fits" measures the memory that's actually free right now,
#    tries the best model that fits (downloading it), loads it and measures it; if it doesn't load or is too slow it
#    tries the next smaller one. Nothing fits: the deep lane stays off, and says why.
#  · Running it: it loads the first time it's needed (20-60 s) and leaves memory after a quiet spell, so the RAM is
#    only taken while it's useful. Or point it at a llama-server on another computer (url).
# Settings (deep.json next to the chat database): enabled, lane (auto | design | always | off), model_file, label,
# context, tok_s, idle_min, url.
DEEP_PORT = 8096
DEEP_LANES = ("auto", "design", "always", "off")
DEEP_MIN_TPS = 2.5          # slower than this isn't worth waiting for
DEEP = {"exe": "", "models_dir": "", "proc": None, "log": None, "last_used": 0.0, "lock": threading.Lock(),
        "finding": None, "flags": None}
DEEP_ASK = re.compile(r"\b(think (harder|deeper|it through|carefully)|big(ger)? brain|deep (model|lane|brain)|"
                      r"use (the|your) (big|bigger|smart|smarter) model)\b", re.I)
DEEP_DESIGN_MODES = {"cad", "modeler", "pcb"}


def _deep_path():
    return os.path.join(MOVE["base"] or ".", "deep.json")


def deep_settings():
    try:
        with open(_deep_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("enabled", False)
    if d.get("lane") not in DEEP_LANES:
        d["lane"] = "auto"
    d.setdefault("idle_min", 15)
    return d


def save_deep_settings(data):
    d = deep_settings()
    for k in ("enabled", "lane", "idle_min", "url", "model_file", "label", "context", "tok_s", "key", "quant",
              "threads", "note"):
        if k in data:
            d[k] = data[k]
    if d["lane"] not in DEEP_LANES:
        raise ValueError("lane is auto, design, always or off")
    d["idle_min"] = max(0, min(240, int(d.get("idle_min") or 0)))
    url = str(d.get("url") or "").strip()
    if url and not re.match(r"https?://", url):
        raise ValueError("the address starts with http:// (a llama-server on another computer)")
    d["url"] = url
    with open(_deep_path() + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(_deep_path() + ".tmp", _deep_path())
    return deep_status()


def deep_exe():
    """llama-server, as the Windows app passes it or next to the models folder on Linux (~/llm/llama.cpp/...)."""
    if DEEP["exe"] and os.path.isfile(DEEP["exe"]):
        return DEEP["exe"]
    md = DEEP["models_dir"]
    if md:
        for c in (os.path.join(os.path.dirname(md), "llama.cpp", "build", "bin", "llama-server"),
                  os.path.join(os.path.dirname(md), "bin", "llama", "llama-server.exe")):
            if os.path.isfile(c):
                return c
    return ""


def free_ram_gb():
    """Memory free for a new program right now (GB), or None if unknown."""
    try:
        if os.name == "nt":
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("x", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in "abcdefg"]
            m = M()
            m.l = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.b / 2**30, 1)
        with open("/proc/meminfo") as f:
            return round(int(re.search(r"MemAvailable:\s+(\d+)", f.read()).group(1)) / 2**20, 1)
    except (OSError, AttributeError, ValueError):
        return None


def deep_candidates(free_gb, ctx=16384, cores=4):
    """Mixture-of-experts models that fit in free_gb, best first: (key, label, quant, size_gb, est tok/s)."""
    if not TUNE:
        return []
    out = []
    for m in TUNE.CATALOG:
        if not m.get("moe") or m.get("legacy"):
            continue
        for tag, gb in m["quants"]:
            need = gb + ctx / 1000 * m["kv_mb"] / 1000 + 1.0
            if need > free_gb:
                continue
            hw = {"cores": cores, "avx2": True, "gpus": []}
            e = TUNE.estimate(m, gb, hw, free_gb, [], ctx) or {"tok_s": 0}
            out.append({"key": m["key"], "label": m["label"], "quant": tag, "size_gb": gb,
                        "quality": TUNE.quality(m, gb), "tok_s": e["tok_s"]})
    out.sort(key=lambda o: (-o["quality"], -o["tok_s"]))
    return out


def _deep_flags(exe):
    if DEEP["flags"] is None:
        try:
            r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=30, errors="replace",
                               stdin=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0)
            DEEP["flags"] = (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError):
            DEEP["flags"] = ""
    return DEEP["flags"]


def deep_model_args(exe, threads):
    """The deep model's llama-server options, without the model, context and address: all on the processor."""
    a = ["-ngl", "0", "-t", str(int(threads)), "-np", "1", "-fa", "on", "--cache-type-k", "q8_0",
         "--cache-type-v", "q8_0", "--jinja"]
    if "--reasoning-budget" in _deep_flags(exe):
        a += ["--reasoning-budget", "4096", "--reasoning-budget-message", "I have thought enough; time to answer."]
    return a


def deep_args(s, exe):
    threads = int(s.get("threads") or max(2, physical_cores()))
    return (["-m", os.path.join(DEEP["models_dir"], s["model_file"]), "-c", str(int(s.get("context") or 16384))]
            + deep_model_args(exe, threads) + ["--host", "127.0.0.1", "--port", str(DEEP_PORT)])


def deep_url():
    s = deep_settings()
    return s["url"].rstrip("/") if s.get("url") else f"http://127.0.0.1:{DEEP_PORT}"


def deep_available():
    s = deep_settings()
    return bool(s.get("enabled") and s["lane"] != "off" and (s.get("url") or (s.get("model_file") and deep_exe())))


def _deep_healthy(timeout=2):
    try:
        with urllib.request.urlopen(deep_url() + "/health", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def deep_ensure(status=lambda t: None, timeout=240):
    """Starts the deep model if it isn't running (or checks the other computer's). Returns its URL; raises if not."""
    s = deep_settings()
    DEEP["last_used"] = time.time()
    if s.get("url"):
        if not _deep_healthy(5):
            raise RuntimeError("the bigger model on the other computer isn't answering")
        return deep_url()
    with DEEP["lock"]:
        p = DEEP["proc"]
        if p and p.poll() is None and _deep_healthy():
            return deep_url()
        exe = deep_exe()
        if not exe or not s.get("model_file") or not os.path.isfile(os.path.join(DEEP["models_dir"], s["model_file"])):
            raise RuntimeError("the bigger model isn't installed")
        if not (p and p.poll() is None):
            status("Waking the bigger model (up to a minute)…")
            path = os.path.join(MOVE["base"] or ".", "deep-model.log")
            DEEP["log"] = open(path, "w", encoding="utf-8", errors="replace")
            DEEP["proc"] = subprocess.Popen([exe] + deep_args(s, exe), stdout=DEEP["log"], stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL,
                                            creationflags=0x08000000 if os.name == "nt" else 0)
            log(f"deep model: starting {s.get('label') or s['model_file']}")
        started = time.time()
        while time.time() - started < timeout:
            if DEEP["proc"].poll() is not None:
                tail = ""
                try:
                    with open(os.path.join(MOVE["base"] or ".", "deep-model.log"), encoding="utf-8", errors="replace") as f:
                        tail = " ".join(f.read()[-2000:].split()[-30:])
                except OSError:
                    pass
                DEEP["proc"] = None
                raise RuntimeError("the bigger model stopped while loading: " + tail)
            if _deep_healthy():
                DEEP["last_used"] = time.time()
                return deep_url()
            time.sleep(1)
        deep_stop()
        raise RuntimeError("the bigger model took too long to load")


def deep_stop():
    """Stops the deep model (the Windows app calls this when it stops Stella)."""
    p = DEEP.get("proc")
    DEEP["proc"] = None
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
        log("deep model: stopped")
    if DEEP.get("log"):
        try:
            DEEP["log"].close()
        except OSError:
            pass
        DEEP["log"] = None


def deep_idle_loop():
    """Frees the deep model's memory after its quiet spell (it loads again when it's next needed)."""
    while True:
        time.sleep(30)
        try:
            idle = int(deep_settings().get("idle_min") or 0)
            p = DEEP.get("proc")
            if p and p.poll() is None and idle and time.time() - DEEP["last_used"] > idle * 60 \
                    and not any(getattr(r, "lane", "") == "deep" for r in list(REPLY_RUNS.values())):
                deep_stop()
        except Exception as e:      # never let the idle check stop for good
            log(f"deep model idle check: {e}")


def choose_lane(mode_id, text, think=False):
    """'deep' or 'fast' for one reply."""
    if not deep_available():
        return "fast"
    lane = deep_settings()["lane"]
    if lane == "always" or DEEP_ASK.search(text or ""):
        return "deep"
    if lane in ("auto", "design") and mode_id in DEEP_DESIGN_MODES and len(text or "") > 30:
        return "deep"
    if lane == "auto" and (wants_plan(mode_id, text) or (think and mode_id in ("programmer", "electronics", "tutor"))):
        return "deep"
    return "fast"


def deep_status():
    s = deep_settings()
    p = DEEP.get("proc")
    st = {**s, "available": deep_available(), "running": bool(s.get("url") and _deep_healthy(2)) or
          bool(p and p.poll() is None), "exe_found": bool(deep_exe()), "free_ram_gb": free_ram_gb(),
          "finding": DEEP.get("finding")}
    return st


def deep_find(fetch=None, bench=None):
    """Background job: finds the best deep model that loads here (see the top of this part). Progress in
    DEEP["finding"] = {state, text, tried: [...]}."""
    fetch = fetch or _download_file
    bench = bench or (TUNE.bench_once if TUNE else None)
    job = {"state": "running", "text": "Checking free memory…", "tried": [], "done": 0, "total": 0}
    DEEP["finding"] = job
    try:
        exe, md = deep_exe(), DEEP["models_dir"]
        if not (TUNE and bench and exe and md):
            raise RuntimeError("this server can't run a second model (llama-server or the models folder wasn't found)")
        deep_stop()
        free = free_ram_gb()
        if free is None:
            raise RuntimeError("couldn't read how much memory is free")
        main_ctx = context_size() or 16384
        ctx = 32768 if main_ctx > 16384 and free >= 24 else 16384
        cores = physical_cores()
        cands = [c for c in deep_candidates(free - 1.5, ctx, cores) if c["tok_s"] >= DEEP_MIN_TPS * 0.8][:3]
        if not cands:
            raise RuntimeError(f"only {free:g} GB of memory is free with everything else running: a bigger model "
                               "needs about 13 GB. More RAM, a smaller everyday model, or another computer would do it")
        man = TUNE.read_manifest(md)
        for c in cands:
            mid = TUNE.model_id(c["key"], c["quant"])
            job["text"] = f"Trying {c['label']} {c['quant']} ({c['size_gb']:g} GB)…"
            entry = man.get(mid) or {}
            name = entry.get("model_file") or ""
            if not (name and TUNE.installed(md, entry)):
                job["text"] = f"Downloading {c['label']} {c['quant']} ({c['size_gb']:g} GB)…"
                r = TUNE.download_model(mid, md, job, fetch, vision=False, mtp=False)
                name = r["model_file"]
            job["text"] = f"Loading {c['label']} {c['quant']} to measure it (a minute or two)…"
            res = bench(exe, os.path.join(md, name), deep_model_args(exe, cores), ctx, timeout=900, n_predict=96)
            job["tried"].append({"model": f"{c['label']} {c['quant']}", **res})
            if res.get("ok") and res.get("gen_tps", 0) >= DEEP_MIN_TPS:
                save_deep_settings({"enabled": True, "model_file": name, "label": f"{c['label']} {c['quant']}",
                                    "key": c["key"], "quant": c["quant"], "context": ctx, "threads": cores,
                                    "tok_s": res["gen_tps"], "url": "",
                                    "note": f"measured {res['gen_tps']:g} tokens/s with {free:g} GB free"})
                job.update(state="done", text=f"Ready: {c['label']} {c['quant']}, {res['gen_tps']:g} tokens/s. It "
                                               "loads when it's first needed and leaves memory after a quiet spell.")
                return job
        raise RuntimeError("none of the models that should fit loaded fast enough here: " +
                           "; ".join(f"{t['model']}: " + (f"{t.get('gen_tps', 0):g} tokens/s" if t.get("ok")
                                                          else str(t.get("error", ""))[:120]) for t in job["tried"]))
    except Exception as e:
        job.update(state="failed", text=str(e)[:600])
        save_deep_settings({"enabled": False})
        return job


def start_deep_find():
    if DEEP.get("finding") and DEEP["finding"].get("state") == "running":
        return False
    threading.Thread(target=deep_find, daemon=True, name="deep-find").start()
    return True
