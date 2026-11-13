# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Speed test: real numbers for this computer ---------------------------------------------
SPEED_FILE = None     # JSON list of past results, set in start()
_SPEED_LOCK = threading.Lock()
_SPEED_WORDS = ("the server reads this passage to measure how quickly it takes in text before it starts to answer "
                "stars planets orbit circuit resistor voltage current signal memory window garden river mountain "
                "morning coffee letter number station bridge engine market school music colour paper").split()


def _speed_prompt(n_words=1100):
    import random
    rnd = random.Random(time.time_ns())        # different every run, so nothing is answered from the cache
    return f"Passage {rnd.randrange(10**9)}. " + " ".join(rnd.choice(_SPEED_WORDS) for _ in range(n_words)) + "\n\nSummary:"


def _resample_wav(wav, rate=16000):
    """Any mono/stereo 16-bit WAV -> 16 kHz mono (what Whisper expects)."""
    import array
    with wave.open(io.BytesIO(wav)) as w:
        src_rate, ch, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("16-bit audio only")
    a = array.array("h")
    a.frombytes(frames[:len(frames) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    if ch > 1:
        a = a[::ch]
    if src_rate != rate and len(a) > 1:
        n = int(len(a) * rate / src_rate)
        step = (len(a) - 1) / max(1, n - 1)
        out = array.array("h", bytes(2 * n))
        for i in range(n):
            x = i * step
            j = int(x)
            f = x - j
            out[i] = int(a[j] * (1 - f) + a[min(j + 1, len(a) - 1)] * f)
        a = out
    if sys.byteorder == "big":
        a.byteswap()
    return pcm_to_wav(a.tobytes(), rate), len(a) / rate


def _speed_history():
    try:
        with open(SPEED_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
        return []


def run_speed_test(whisper_url="", language="en"):
    """Measures this server as it is set up now: how fast the model reads and writes, how long it takes to wake,
    and how long hearing and speaking take. Takes 10-60 s and uses the model, so it only runs when Stella is idle."""
    if not _SPEED_LOCK.acquire(blocking=False):
        return {"error": "A speed test is already running."}
    try:
        if llama_busy():
            return {"error": "Stella is answering someone right now. Try again when she's idle."}
        started = time.time()
        out = {"at": int(started * 1000), "model": "", "results": []}

        def add(key, name, value, unit, detail=""):
            out["results"].append({"key": key, "name": name, "value": round(value, 2), "unit": unit, "detail": detail})

        # -- the model: wake-up, reading, writing
        try:
            props = _llama("/props", timeout=10)
            out["model"] = os.path.basename(str(props.get("model_path") or ""))
            asleep = bool(props.get("is_sleeping"))
            t0 = time.time()
            r = _llama("/completion", {"prompt": _speed_prompt(), "n_predict": 96, "cache_prompt": False,
                                       "ignore_eos": True, "temperature": 0.7, "stream": False}, timeout=600)
            total = time.time() - t0
            tm = r.get("timings") or {}
            pn, ps = int(tm.get("prompt_n") or 0), float(tm.get("prompt_per_second") or 0)
            gn, gs = int(tm.get("predicted_n") or 0), float(tm.get("predicted_per_second") or 0)
            if asleep:
                work = (float(tm.get("prompt_ms") or 0) + float(tm.get("predicted_ms") or 0)) / 1000
                add("wake", "Waking up", max(0.0, total - work), "s", "from asleep to ready (the model had been unloaded)")
            if ps:
                add("read", "Reading", ps, "tokens/s", f"{pn} tokens read; a full page of text takes about {700 / ps:.1f} s")
            if gs:
                add("write", "Writing", gs, "tokens/s", f"about {gs * 0.75:.0f} words a second; a 150-word answer takes about {200 / gs:.0f} s")
            if not ps and not gs:
                out["results"].append({"key": "model", "name": "AI model", "error": "it answered without timing figures"})
        except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
            out["results"].append({"key": "model", "name": "AI model", "error": f"not reachable ({e})"})

        # -- speaking, then hearing that same sentence back (so the transcript can be checked too)
        sentence = "Testing, one two three. The quick brown fox jumps over the lazy dog."
        spoken = None
        try:
            ac = avatar_config()
            engine = "Piper"
            t0 = time.time()
            if ac["engine"] == "kokoro" and KOKORO.ready():
                spoken, engine = KOKORO.say(sentence, ac["kokoro_voice"], 1.0), "Kokoro"
            elif os.path.isfile(_avatar_path("voices", ac["voice"] + ".onnx")):
                TTS.say("Ready.", ac["voice"], 1.0)          # loads the voice first, as in normal use
                t0 = time.time()
                spoken = TTS.say(sentence, ac["voice"], 1.0)
            if spoken:
                took = time.time() - t0
                _, seconds = _resample_wav(spoken)
                add("speak", "Speaking", took, "s", f"{engine} made {seconds:.1f} s of speech"
                    + (f" ({seconds / took:.0f}x faster than real time)" if took > 0 else ""))
        except Exception as e:   # noqa: BLE001 - one part failing shouldn't lose the other figures
            out["results"].append({"key": "speak", "name": "Speaking", "error": str(e)[:200]})
        if whisper_url:
            try:
                if spoken:
                    wav, seconds = _resample_wav(spoken)
                else:   # no voice installed: time Whisper on a tone of the same length
                    import array
                    seconds = 4.0
                    wav = pcm_to_wav(array.array("h", [int(3000 * math.sin(i / 9)) for i in range(64000)]).tobytes(), 16000)
                t0 = time.time()
                heard = clean_transcript(transcribe(whisper_url, wav, language))
                took = time.time() - t0
                ok = not spoken or len(set(_terms(heard)) & {"quick", "brown", "fox", "lazy", "dog", "testing"}) >= 3
                add("hear", "Hearing", took, "s", f"for {seconds:.1f} s of speech"
                    + (f'; heard: "{heard[:80]}"' if spoken else "") + ("" if ok else " (that's not what was said)"))
            except Exception as e:   # noqa: BLE001
                out["results"].append({"key": "hear", "name": "Hearing", "error": str(e)[:200]})

        if EMBEDDER.url:
            try:
                t0 = time.time()
                EMBEDDER.embed([" ".join(_SPEED_WORDS * 4)] * 8, EMBEDDER.DOC)
                add("search", "File search", (time.time() - t0) / 8 * 1000, "ms", "per page indexed")
            except Exception as e:   # noqa: BLE001
                out["results"].append({"key": "search", "name": "File search", "error": str(e)[:200]})

        out["took_s"] = round(time.time() - started, 1)
        # compare with the last run on the same model, then remember this one (keeps 30)
        hist = _speed_history()
        prev = next((h for h in reversed(hist) if h.get("model") == out["model"]), None)
        if prev:
            old = {x.get("key"): x.get("value") for x in prev.get("results", []) if "value" in x}
            for x in out["results"]:
                if x.get("value") and old.get(x["key"]):
                    x["change_pct"] = round((x["value"] - old[x["key"]]) / old[x["key"]] * 100)
            out["compared_to"] = prev.get("at")
        if SPEED_FILE and any("value" in x for x in out["results"]):
            try:
                with open(SPEED_FILE + ".tmp", "w") as f:
                    json.dump((hist + [out])[-30:], f)
                os.replace(SPEED_FILE + ".tmp", SPEED_FILE)
            except OSError:
                pass
        log("speed test: " + ", ".join(f"{x['name'].lower()} {x['value']} {x['unit']}" for x in out["results"] if "value" in x))
        return out
    finally:
        _SPEED_LOCK.release()


def speed_text(report):
    if report.get("error"):
        return report["error"]
    lines = ["StellAI speed test" + (f" — {report['model']}" if report.get("model") else ""), ""]
    for x in report.get("results", []):
        if "error" in x:
            lines.append(f"  ✗ {x['name']}: {x['error']}")
            continue
        better_when_higher = x["unit"].endswith("/s")
        ch = x.get("change_pct")
        change = ""
        if ch:
            change = f"  ({'+' if ch > 0 else ''}{ch}% vs last time: {'faster' if (ch > 0) == better_when_higher else 'slower'})"
        lines.append(f"  {x['name']}: {x['value']:g} {x['unit']}{change}")
        if x.get("detail"):
            lines.append(f"      {x['detail']}")
    lines.append("")
    lines.append(f"Took {report.get('took_s', 0):g} s.")
    return "\n".join(lines)


# ---- Health check ("doctor") --------------------------------------------------------------
DOCTOR = {"whisper": "", "embed": "", "update": ""}


def _probe(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError) as e:
        return 0, str(getattr(e, "reason", e))


def gpu_driver_check():
    """(state, detail, fix) for NVIDIA cards, or None without any. Catches the classic Proxmox container problem:
    the host's driver was updated and the container's driver programs no longer match it."""
    import shutil
    smi = shutil.which("nvidia-smi")
    if not smi and not os.path.exists("/dev/nvidiactl"):
        return None
    if not smi:
        return ("fail", "the cards are there but nvidia-smi isn't installed",
                "Run stellai-setup.sh again: in a Proxmox container it installs the host's driver version.")
    try:
        p = subprocess.run([smi, "--query-gpu=name,driver_version", "--format=csv,noheader"], capture_output=True,
                           text=True, timeout=20, creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as e:
        return ("warn", f"nvidia-smi didn't answer ({e})", "")
    text = (p.stdout or "") + (p.stderr or "")
    if "mismatch" in text.lower():
        return ("fail", "driver/library version mismatch: Stella's model is running on the processor",
                "The NVIDIA driver on the Proxmox host changed. Run stellai-setup.sh again in the container (it "
                "installs the matching version), then restart StellAI. Or reboot the host if it was just updated.")
    if p.returncode != 0:
        return ("warn", "nvidia-smi failed: " + " ".join(text.split())[:160], "Check the graphics driver.")
    rows = [r.split(",") for r in p.stdout.strip().splitlines() if r.strip()]
    return ("ok", f"{len(rows)} card(s), driver {rows[0][1].strip() if rows and len(rows[0]) > 1 else '?'}", "")


def run_doctor():
    """Checks every part of StellAI and says what to do about problems."""
    import shutil
    checks = []

    def add(name, state, detail, fix=""):
        checks.append({"name": name, "state": state, "detail": detail, "fix": fix})

    code, err = _probe(LLAMA_URL.rstrip("/") + "/health")
    if code == 200:
        try:
            props = _llama("/props", timeout=5)
            n = (props.get("default_generation_settings") or {}).get("n_ctx")
            if props.get("is_sleeping"):
                add("AI model", "ok", "asleep to free memory; it wakes up on the next message (a few seconds)")
            else:
                add("AI model", "ok", f"{os.path.basename(str(props.get('model_path') or 'loaded'))}, {n} tokens of context")
        except (urllib.error.URLError, OSError, ValueError):
            add("AI model", "ok", "running")
    elif code == 503:
        add("AI model", "warn", "still loading", "Wait a minute; big models take a while to load.")
    else:
        add("AI model", "fail", f"not reachable ({err or code})", "Start StellAI Server; if it stops right away, check llama.log / the Activity box.")
    gpu = gpu_driver_check()
    if gpu:
        add("Graphics cards", *gpu)
    if DOCTOR["whisper"]:
        code, err = _probe(DOCTOR["whisper"].rstrip("/") + "/health")
        add("Voice recognition", "ok" if code in (200, 404) else "fail",
            "running" if code in (200, 404) else f"not reachable ({err or code})",
            "" if code in (200, 404) else "Voice commands won't work. Re-run setup to install Whisper.")
    if HELPER["url"]:
        code, err = _probe(HELPER["url"].rstrip("/") + "/health")
        add("Helper model", "ok" if code == 200 else "warn",
            "running (memory learning and mode checks)" if code == 200 else "not running; the main model does its jobs",
            "" if code == 200 else "Check helper.log; re-run setup to download the helper model again.")
    if EMBEDDER.url:
        code, err = _probe(EMBEDDER.url + "/health")
        add("Meaning search", "ok" if code == 200 else "warn", "running" if code == 200 else "not running; keyword search only",
            "" if code == 200 else "Re-run setup to download the search model.")
    else:
        add("Meaning search", "warn", "not set up; keyword search only", "Re-run setup (it downloads a 140 MB search model).")
    if SEARXNG_URL:
        code, err = _probe(SEARXNG_URL.rstrip("/") + "/healthz")
        add("Web search", "ok" if code == 200 else "warn", "SearXNG running" if code == 200 else "SearXNG not answering; using DuckDuckGo",
            "" if code == 200 else "Start Docker (or re-run the SearXNG step of setup).")
    else:
        add("Web search", "ok", "DuckDuckGo (SearXNG not set up)")
    try:
        with CHATS.lock:
            ok = CHATS.db.execute("PRAGMA quick_check").fetchone()[0]
            n = CHATS.db.execute("SELECT COUNT(*) FROM conversations WHERE deleted=0").fetchone()[0]
            mem = CHATS.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            pend = CHATS.db.execute("SELECT COUNT(*) FROM chat_dirty").fetchone()[0]
        add("Chat database", "ok" if ok == "ok" else "fail", f"{n} chats, {mem} memories" + (f", {pend} chats waiting to be indexed" if pend else ""),
            "" if ok == "ok" else "Restore yesterday's backup (see Backups).")
    except sqlite3.Error as e:
        add("Chat database", "fail", str(e), "Restore a backup.")
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(CHATS_PATH))).free / 2**30
    add("Disk space", "ok" if free > 10 else "warn" if free > 3 else "fail", f"{free:.1f} GB free",
        "" if free > 10 else "Free up space; backups and downloads need room.")
    st = backups_status()
    age = (time.time() * 1000 - st["last"]) / 86400_000 if st["last"] else None
    if st["error"]:
        add("Backups", "fail", st["error"], "Check the backup folder in Server → Backups.")
    elif age is None:
        add("Backups", "warn", "none yet", "One is made automatically tonight; or tap Back up now.")
    else:
        add("Backups", "ok" if age < 2 else "warn", f"last {age * 24:.0f} hours ago, {len(st['backups'])} kept" +
            (f", copied to {st['copy_to']}" if st["copy_to"] else ""), "" if age < 2 else "Tap Back up now.")
    try:
        import pypdf  # noqa: F401
        add("PDF reading", "ok", "pypdf")
    except ImportError:
        add("PDF reading", "ok" if shutil.which("pdftotext") else "warn",
            "pdftotext" if shutil.which("pdftotext") else "no PDF reader", "" if shutil.which("pdftotext") else "pip install pypdf")
    add("Mermaid diagrams", "ok" if mermaid_command() else "warn", "installed" if mermaid_command() else "not installed (flowcharts use schemdraw)")
    gh = github_status()
    add("GitHub", "ok" if gh.get("user") else "warn" if gh.get("token_set") else "ok",
        f"connected as {gh['user']}" if gh.get("user") else gh.get("error") or "no token (public repos only)",
        "Check the token in Settings → GitHub." if gh.get("token_set") and not gh.get("user") else "")
    ts = shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe" if os.name == "nt" else "")
    ip = ""
    if ts and os.path.exists(ts):
        try:
            ip = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=5,
                                creationflags=0x08000000 if os.name == "nt" else 0).stdout.strip().splitlines()[0]
        except (OSError, IndexError, subprocess.SubprocessError):
            ip = ""
    add("Tailscale", "ok" if ip else "warn", ip or "not connected", "" if ip else "Install/sign in to use Stella away from home.")
    if AVATAR["dir"]:
        st = AVATAR["status"].get("voice", "")
        voice = avatar_config()["voice"]
        kst = AVATAR["status"].get("kokoro", "")
        if avatar_config().get("engine") == "kokoro" and kst.startswith("failed"):
            add("Avatar voice", "warn", "Kokoro " + kst + " (using Piper)", "Check the internet connection, then pick Kokoro again.")
        elif piper_exe() and os.path.isfile(_avatar_path("voices", voice + ".onnx")):
            add("Avatar voice", "ok", "ready: " + (("Kokoro " + (kst or "preparing")) if avatar_config().get("engine") == "kokoro"
                                                  else VOICES.get(voice, voice).split(":")[0]))
        elif st.startswith("failed"):
            add("Avatar voice", "warn", st, "Check the internet connection, then open the avatar screen again.")
        elif st:
            add("Avatar voice", "ok", st + "…")
        else:
            add("Avatar voice", "ok", "not set up yet: downloads (~90 MB) the first time you open the avatar screen")
    if DOCTOR["update"]:
        add("Updates", "ok", DOCTOR["update"])
    worst = "fail" if any(c["state"] == "fail" for c in checks) else "warn" if any(c["state"] == "warn" for c in checks) else "ok"
    return {"state": worst, "checks": checks, "time": int(time.time() * 1000)}


def doctor_text(report):
    icon = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = ["StellAI health check — " + {"ok": "all good", "warn": "working, with warnings", "fail": "problems found"}[report["state"]], ""]
    for c in report["checks"]:
        lines.append(f"  {icon[c['state']]} {c['name']}: {c['detail']}")
        if c["fix"] and c["state"] != "ok":
            lines.append(f"      → {c['fix']}")
    return "\n".join(lines) + "\n"


IMAGE_DIR = None
WAKEWORD_DIR = None
WAKEWORD_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,59}")


def shrink_result(result, max_chars):
    """Shortens the longest text fields of a tool result until it fits in max_chars (the space left in context)."""
    max_chars = max(1200, max_chars)
    for _ in range(300):
        size = len(json.dumps(result))
        if size <= max_chars:
            break
        strings = []

        def walk(o, path):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, path + [k])
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, path + [i])
            elif isinstance(o, str) and len(o) > 200:
                strings.append((len(o), path))
        walk(result, [])
        if not strings:
            # drop list items from the end (e.g. too many search results)
            lists = [v for v in result.values() if isinstance(v, list) and len(v) > 1]
            if not lists:
                break
            max(lists, key=len).pop()
            continue
        length, path = max(strings)
        target = max(200, length - (size - max_chars) - 40)
        o = result
        for k in path[:-1]:
            o = o[k]
        o[path[-1]] = o[path[-1]][:target] + " […shortened to fit Stella's memory]"
        result["truncated"] = True
    return result


