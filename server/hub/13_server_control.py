# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Server status and switching models ---------------------------------------------
try:      # the model catalog shared with the setup (stellai_tune.py sits next to this file)
    import stellai_tune as TUNE
except ImportError:
    TUNE = None

MODEL_CATALOG = [
    {"key": "4b", "label": "Qwen3.5 4B", "repo": "unsloth/Qwen3.5-4B-GGUF", "about": "Fast; fine for everyday help."},
    {"key": "9b", "label": "Qwen3.5 9B", "repo": "unsloth/Qwen3.5-9B-GGUF", "about": "Smarter at code and reasoning."},
    {"key": "35b", "label": "Qwen3.5 35B-A3B", "repo": "unsloth/Qwen3.5-35B-A3B-GGUF",
     "about": "The strongest; needs about 20 GB RAM plus a GPU."},
]


def detect_memory():
    """(ram_gb, vram_gb) of this computer."""
    ram = 0
    try:
        if os.name == "nt":
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("x", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in "abcdefg"]
            m = M()
            m.l = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            ram = round(m.a / 2**30)
        else:
            with open("/proc/meminfo") as f:
                ram = round(int(re.search(r"MemTotal:\s+(\d+)", f.read()).group(1)) / 2**20)
    except (OSError, AttributeError, ValueError):
        pass
    vram = 0
    try:
        import shutil
        smi = shutil.which("nvidia-smi")
        if smi:
            out = subprocess.run([smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"], capture_output=True,
                                 text=True, timeout=10, creationflags=0x08000000 if os.name == "nt" else 0).stdout
            vram = round(int(out.split()[0]) / 1024) if out.strip() else 0
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return ram, vram


# ---- How long a conversation the model is started with (its context window, in tokens)
# Qwen3.5 only keeps a per-token cache in one layer out of four, so long contexts are cheap: the 9B measures
# about 17 MB per 1,000 tokens with the 8-bit cache StellAI uses. The planning figures below add working
# buffers on top. The 4B and 35B figures are estimates on the same basis, not measurements.
CTX_TARGET = {"4b": 32768, "9b": 65536, "35b": 65536}
CTX_GB_PER_1K = {"4b": 0.02, "9b": 0.02, "35b": 0.03}


def context_for(key, model_gb, ram, vram, cores):
    """The biggest context that keeps the model where it runs fastest on this computer.

    - No usable graphics card: memory isn't the limit, re-reading is (a CPU reads a few dozen tokens a second,
      so a long chat would take many minutes to load after a restart). Short contexts stay.
    - 4B / 9B on a graphics card: model + context must fit on the card, or part of the model falls back to the
      CPU and every reply gets slower.
    - 35B: its always-used core (about 2.5 GB) and the context sit on the card, the experts in RAM."""
    if vram < 4:
        if key == "35b":
            return 16384
        return 16384 if cores >= 6 and ram >= 12 else 8192
    floor = (32768 if vram >= 4.5 else 16384) if key == "35b" else 8192
    gpu = vram - (1.0 if vram < 8 else 2.0)        # what --fit leaves free (voice, and vision on 8 GB+ cards)
    ctx = CTX_TARGET.get(key, 8192)
    while ctx > floor:
        kv = ctx / 1000 * CTX_GB_PER_1K.get(key, 0.03)
        if key == "35b":
            fits = kv + 2.5 <= gpu and model_gb + kv + 4 <= ram + vram
        else:
            fits = model_gb + kv <= gpu
        if fits:
            break
        ctx //= 2
    return max(ctx, floor)


def physical_cores():
    """Real processor cores (not threads), as the setup counts them."""
    try:
        with open("/proc/cpuinfo") as f:
            text = f.read()
        pairs = set(zip(re.findall(r"^physical id\s*:\s*(\d+)", text, re.M), re.findall(r"^core id\s*:\s*(\d+)", text, re.M)))
        if pairs:
            return len(pairs)
    except OSError:
        pass
    return max(1, (os.cpu_count() or 2) // 2)


def model_file_for(key, ram, vram):
    """Same choices as the setup: the best version that fits this computer, and its context window."""
    if key == "4b":
        name, gb = "Qwen3.5-4B-Q4_K_M.gguf", 2.7
    elif key == "9b":
        name, gb = ("Qwen3.5-9B-Q3_K_M.gguf", 4.6) if 5 <= vram < 8 else ("Qwen3.5-9B-Q4_K_M.gguf", 5.7)
    elif ram + vram >= 40:
        name, gb = "Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf", 22.2
    elif ram + vram >= 30:
        name, gb = "Qwen3.5-35B-A3B-UD-IQ4_XS.gguf", 17.5
    else:
        name, gb = "Qwen3.5-35B-A3B-UD-IQ3_S.gguf", 13.6
    return name, gb, context_for(key, gb, ram, vram, physical_cores())


class LinuxServerControl:
    """Model switching for the Linux launcher: it re-reads model.conf whenever llama-server stops and a
    .switch-model file is present."""

    def __init__(self, conf, models_dir, pid_file):
        self.conf, self.models_dir, self.pid_file = conf, models_dir, pid_file
        self.download = {}   # key -> {"done", "total", "error", "state"}
        self.started = time.time()

    def _read_conf(self):
        out = {}
        try:
            with open(self.conf) as f:
                for line in f:
                    m = re.match(r'\s*([A-Z_]+)="?(.*?)"?\s*$', line)
                    if m:
                        out[m.group(1)] = m.group(2)
        except OSError:
            pass
        return out

    def _tune_hw(self, conf):
        hw = TUNE.detect(self.models_dir)
        budget = float(conf.get("RAM_BUDGET_GB") or 0) or max(4.0, hw["ram_gb"] - 4)
        ids = [] if conf.get("LLAMA_GPUS") == "-1" else \
            [int(x) for x in conf.get("LLAMA_GPUS", "").split(",") if x.strip().isdigit()] or None
        return hw, budget, ids

    def catalog(self):
        ram, vram = detect_memory()
        conf = self._read_conf()
        current = os.path.basename(conf.get("MODEL_FILE", ""))
        out = []
        if TUNE:
            try:
                hw, budget, ids = self._tune_hw(conf)
                out = TUNE.library(hw, budget, self.models_dir, current, ids, self.download)
            except Exception as e:      # never lose the model list over the catalog
                log(f"model catalog: {e}")
                out = []
        for m in MODEL_CATALOG:
            fname, gb, ctx = model_file_for(m["key"], ram, vram)
            path = os.path.join(self.models_dir, fname)
            have = os.path.isfile(path) and os.path.getsize(path) > 100_000_000
            if out and not have:
                continue        # with the full catalog, the old three only show when they're downloaded
            out.append({**{k: m[k] for k in ("key", "label", "about")}, "file": fname, "size_gb": gb, "context": ctx,
                        "installed": have, "current": fname == current, "download": self.download.get(m["key"])})
        return out

    def status(self):
        conf = self._read_conf()
        return {"model": conf.get("MODEL_LABEL", ""), "file": os.path.basename(conf.get("MODEL_FILE", "")),
                "backend": conf.get("BACKEND", ""), "can_switch": True}

    def switch(self, key):
        entry = next((m for m in self.catalog() if m["key"] == key), None)
        if not entry:
            raise ValueError("unknown model")
        if not entry["installed"]:
            raise ValueError("download it first")
        conf = self._read_conf()
        mmproj, spec = "", ""
        man = TUNE.read_manifest(self.models_dir).get(key, {}) if TUNE and "|" in key else {}
        if conf.get("MMPROJ_FILE") or conf.get("VISION", "") == "1":
            cand = os.path.join(self.models_dir, man["mmproj"]) if man.get("mmproj") else \
                os.path.join(self.models_dir, entry["file"].replace(".gguf", ".mmproj-F16.gguf"))
            mmproj = cand if os.path.isfile(cand) else ""
        if (man.get("tuned") or {}).get("mtp"):
            spec = " ".join(TUNE.MTP_ARGS)     # the setup's speed test showed the MTP speed-up helps here
        ctx = (man.get("tuned") or {}).get("context") or entry["context"]
        conf.update(MODEL_LABEL=entry["label"], MODEL_FILE=os.path.join(self.models_dir, entry["file"]),
                    NET_CONTEXT=str(ctx), MMPROJ_FILE=mmproj, SPEC_ARGS=spec)
        with open(self.conf + ".tmp", "w") as f:
            for k, v in conf.items():
                f.write(f'{k}="{v}"\n')
        os.replace(self.conf + ".tmp", self.conf)
        open(os.path.join(os.path.dirname(self.conf), ".switch-model"), "w").close()
        try:
            with open(self.pid_file) as f:
                os.kill(int(f.read().strip()), 15)
        except (OSError, ValueError):
            raise RuntimeError("couldn't restart the model server; restart StellAI Server to use the new model")
        return f"switching to {entry['label']}; it takes a minute to load"

    def start_download(self, key):
        entry = next((m for m in self.catalog() if m["key"] == key), None)
        if not entry:
            raise ValueError("unknown model")
        state = {"state": "downloading", "done": 0, "total": 0, "error": ""}
        self.download[key] = state
        if TUNE and "|" in key:
            vision = bool(self._read_conf().get("MMPROJ_FILE"))

            def work_tuned():
                try:
                    TUNE.download_model(key, self.models_dir, state, _download_file, vision=vision)
                    state["state"] = "ready"
                except Exception as e:
                    state.update(state="failed", error=str(e))
            threading.Thread(target=work_tuned, daemon=True).start()
            return "download started"
        repo = next(m["repo"] for m in MODEL_CATALOG if m["key"] == key)

        def work():
            files = [(f"https://huggingface.co/{repo}/resolve/main/{entry['file']}", entry["file"])]
            if self._read_conf().get("MMPROJ_FILE"):
                files.append((f"https://huggingface.co/{repo}/resolve/main/mmproj-F16.gguf",
                              entry["file"].replace(".gguf", ".mmproj-F16.gguf")))
            try:
                for url, name in files:
                    _download_file(url, os.path.join(self.models_dir, name), state)
                state["state"] = "ready"
            except Exception as e:
                state.update(state="failed", error=str(e))
        threading.Thread(target=work, daemon=True).start()
        return "download started"


def _download_file(url, dest, state):
    """Resumable download with progress in `state` (used for model downloads from the app)."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "StellAI", **({"Range": f"bytes={have}-"} if have else {})})
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.URLError as e:
        # A Windows PC that hasn't fetched this site's root certificate yet: let Windows download it instead
        if os.name == "nt" and _is_cert_error(e):
            log(f"download: certificate check failed for {urlparse(url).netloc}, using Windows to download it")
            if os.path.exists(part):
                os.remove(part)
            _windows_download(url, part)
            os.replace(part, dest)
            return
        raise
    with r:
        if have and r.status != 206:
            have = 0
        state["total"] = have + int(r.headers.get("Content-Length") or 0)
        with open(part, "ab" if have else "wb") as f:
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                have += len(buf)
                state["done"] = have
    os.replace(part, dest)


SERVER_CONTROL = None   # LinuxServerControl, or the Windows app's own controller
STARTED_AT = time.time()


def server_status():
    st = {"uptime_s": int(time.time() - STARTED_AT), "model": "", "context": 0, "loaded": False, "can_switch": False,
          "models": []}
    try:
        props = _llama("/props", timeout=5)
        st["context"] = int((props.get("default_generation_settings") or {}).get("n_ctx") or 0)
        st["model_path"] = os.path.basename(str(props.get("model_path") or ""))
        st["loaded"] = True
        st["sleeping"] = bool(props.get("is_sleeping"))   # unloaded after a quiet spell; the next message wakes it
    except (urllib.error.URLError, OSError, ValueError):
        pass
    if SERVER_CONTROL:
        try:
            st.update(SERVER_CONTROL.status())
            st["models"] = SERVER_CONTROL.catalog()
        except Exception as e:
            st["error"] = str(e)
    ram, vram = detect_memory()
    st.update(ram_gb=ram, vram_gb=vram)
    return st


