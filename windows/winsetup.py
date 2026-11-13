"""StellAI for Windows: hardware detection, install plan, downloads and server processes.

Kept free of any GUI code so the logic can be tested on its own.
"""
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

try:      # the model catalog, ranking and speed test shared with the Linux setup and the hub (server/)
    import stellai_tune as tune
except ImportError:
    tune = None


def _trust_more_certificates():
    """Trust Mozilla's certificate list (certifi) as well as Windows' own: Windows only fetches some root
    certificates when its own programs need them, so Python downloads can otherwise fail on a fresh PC."""
    try:
        import ssl
        import certifi
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(certifi.where())
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)))
    except Exception:   # noqa: BLE001 - fall back to Python's default
        pass


_trust_more_certificates()

try:
    from _version import VERSION as APP_VERSION   # written by the GitHub build
except ImportError:
    APP_VERSION = "0.0.0-dev"                      # running from source: no auto-updates
UA = f"StellAI-Windows/{APP_VERSION}"
LLAMA_PORT, HUB_PORT, WHISPER_PORT, EMBED_PORT, SEARXNG_PORT = 8080, 8092, 8093, 8094, 8888
EMBED_FILE = "nomic-embed-text-v1.5.Q8_0.gguf"   # small model for meaning-based file search (~140 MB, CPU)
EMBED_URL = f"https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/{EMBED_FILE}"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ABOVE_NORMAL = 0x00008000 if os.name == "nt" else 0   # ABOVE_NORMAL_PRIORITY_CLASS

LLAMA_REPO = "ggml-org/llama.cpp"
WHISPER_REPO = "ggml-org/whisper.cpp"
HF = "https://huggingface.co"


def data_dir():
    if os.environ.get("STELLAI_HOME"):
        return os.environ["STELLAI_HOME"]
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "StellAI")


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ============================================================================ hardware
def _ram_gb():
    if os.name == "nt":
        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMSTAT()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return round(m.ullTotalPhys / 2**30)
    try:
        with open("/proc/meminfo") as f:
            kb = int(re.search(r"MemTotal:\s+(\d+)", f.read()).group(1))
        return round(kb / 2**20)
    except (OSError, AttributeError):
        return 8


def _cpu():
    name = ""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
        out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"])
        cores = int(out.strip()) if out.strip().isdigit() else 0
    else:
        cores = 0
    logical = os.cpu_count() or 4
    if not cores:
        cores = max(1, logical // 2) if logical >= 4 else logical
    return name or "Unknown CPU", cores, logical


def _vendor(text):
    t = text.upper()
    if "VEN_10DE" in t or "NVIDIA" in t:
        return "nvidia"
    if "VEN_1002" in t or "AMD" in t or "RADEON" in t:
        return "amd"
    if "VEN_8086" in t or "INTEL" in t:
        return "intel"
    return "other"


def _registry_gpus():
    """Display adapters from the registry: name, vendor and dedicated memory (GB)."""
    gpus = []
    if os.name != "nt":
        return gpus
    import winreg
    cls = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, cls)
    except OSError:
        return gpus
    for i in range(64):
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        if not sub.isdigit():
            continue
        try:
            with winreg.OpenKey(root, sub) as k:
                def val(n):
                    try:
                        return winreg.QueryValueEx(k, n)[0]
                    except OSError:
                        return None
                name = val("DriverDesc") or ""
                mem = val("HardwareInformation.qwMemorySize")
                if mem is None:
                    mem = val("HardwareInformation.MemorySize")
                if isinstance(mem, (bytes, bytearray)):
                    mem = int.from_bytes(mem[:8], "little")
                ids = val("MatchingDeviceId") or ""
        except OSError:
            continue
        if not name or "basic display" in name.lower() or "remote" in name.lower():
            continue
        gpus.append({"name": name, "vendor": _vendor(ids + " " + name),
                     "vram_gb": round((mem or 0) / 2**30, 1)})
    return gpus


def _nvidia():
    """NVIDIA GPU details from nvidia-smi (installed with the driver)."""
    smi = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    if not (os.path.isfile(smi) or shutil.which("nvidia-smi")):
        return None
    out = _run([smi, "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader,nounits"])
    if not out.strip():
        return None
    parts = [p.strip() for p in out.strip().splitlines()[0].split(",")]
    head = _run([smi])
    m = re.search(r"CUDA Version:\s*([\d.]+)", head)
    try:
        cc = float(parts[2]) if len(parts) > 2 else 0.0
    except ValueError:
        cc = 0.0
    return {"name": parts[0], "vram_gb": round(int(float(parts[1])) / 1024, 1), "compute_cap": cc,
            "cuda": m.group(1) if m else ""}


def _version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:2]) or (0,)


def choose_backend(gpus, nvidia):
    """Returns (backend, gpu, cuda_label). backend is cuda, vulkan or cpu."""
    if nvidia and nvidia["vram_gb"] >= 3.5:
        drv = _version_tuple(nvidia["cuda"])
        if drv >= (13, 0) and nvidia["compute_cap"] >= 7.5:
            return "cuda", nvidia, "13"
        if drv >= (12, 4):
            return "cuda", nvidia, "12"
        return "vulkan", nvidia, ""        # driver too old for the CUDA builds
    for g in gpus:
        if g["vendor"] in ("amd", "intel", "nvidia") and g["vram_gb"] >= 3.5:
            return "vulkan", g, ""
    return "cpu", None, ""


def _system_dll(name):
    return os.path.isfile(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", name))


VCREDIST_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def vcredist_present():
    """The Microsoft Visual C++ runtime that llama.cpp and whisper.cpp are built against."""
    return os.name != "nt" or all(_system_dll(d) for d in VCREDIST_DLLS)


def vulkan_present():
    """The Vulkan loader, installed with any current AMD, Intel or NVIDIA graphics driver."""
    return os.name != "nt" or _system_dll("vulkan-1.dll")


def _avx2():
    if os.name == "nt":
        try:
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))   # PF_AVX2_INSTRUCTIONS_AVAILABLE
        except (AttributeError, OSError):
            return True
    try:
        with open("/proc/cpuinfo") as f:
            return " avx2 " in f.read()
    except OSError:
        return True


def detect():
    cpu, cores, logical = _cpu()
    gpus = _registry_gpus()
    nvidia = _nvidia()
    backend, gpu, cuda = choose_backend(gpus, nvidia)
    notes = []
    if backend == "vulkan" and not vulkan_present():
        notes.append(f"{gpu['name'] if gpu else 'The graphics card'} needs a newer graphics driver for Vulkan; "
                     "using the CPU until it's updated")
        backend, gpu, cuda = "cpu", None, ""
    gpu_list = []
    if backend == "cuda" and tune:
        gpu_list = tune.nvidia_gpus()
    if not gpu_list and gpu and backend != "cpu":
        gpu_list = [{"index": 0, "name": gpu["name"], "vram_gb": gpu["vram_gb"], "free_gb": gpu["vram_gb"],
                     "vendor": gpu.get("vendor", "nvidia")}]
    try:
        disk = round(shutil.disk_usage(data_dir() if os.path.isdir(data_dir()) else os.path.expanduser("~")).free
                     / 2**30, 1)
    except OSError:
        disk = 0.0
    return {"notes": notes, "vcredist": vcredist_present(),"ram_gb": _ram_gb(), "cpu": cpu, "cores": cores, "threads": logical,
            "gpu_list": gpu_list, "avx2": _avx2(), "disk_free_gb": disk,
            "gpus": [g["name"] for g in gpus] or ([nvidia["name"]] if nvidia else []),
            "backend": backend, "gpu": gpu["name"] if gpu else "", "vram_gb": gpu["vram_gb"] if gpu else 0,
            "cuda": cuda, "cuda_driver": nvidia["cuda"] if nvidia else ""}


# ============================================================================ models
MODELS = {
    "4b": {"label": "Qwen3.5 4B", "repo": "unsloth/Qwen3.5-4B-GGUF", "file": "Qwen3.5-4B-Q4_K_M.gguf",
           "size_gb": 2.7, "about": "Fast, good for everyday help. Best for 8 GB laptops."},
    "9b": {"label": "Qwen3.5 9B", "repo": "unsloth/Qwen3.5-9B-GGUF", "file": "Qwen3.5-9B-Q4_K_M.gguf",
           "size_gb": 5.7, "about": "Smarter at code and reasoning. Needs 16 GB RAM or a 6 GB GPU."},
    "35b": {"label": "Qwen3.5 35B-A3B", "repo": "unsloth/Qwen3.5-35B-A3B-GGUF",
            "file": "Qwen3.5-35B-A3B-UD-IQ3_S.gguf", "size_gb": 13.6,
            "about": "The strongest option. Needs about 20 GB RAM plus a 4 GB+ GPU."},
}


def recommend(hw):
    vram = hw["vram_gb"] if hw["backend"] != "cpu" else 0
    ram = hw["ram_gb"]
    if vram >= 5 and ram >= 18:
        return "35b"
    if vram >= 5 or ram >= 14:
        return "9b"
    return "4b"


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


def plan(hw, model_key, vision=True, whisper=None):
    """Everything needed to install and run: files, URLs and llama-server arguments."""
    m = dict(MODELS[model_key])
    vram = hw["vram_gb"] if hw["backend"] != "cpu" else 0
    ctx = 8192
    # On a GPU, llama-server's --fit (on by default) places as many layers / MoE experts on the card as fit,
    # so no fixed -ngl / --cpu-moe; a bigger micro-batch reads long prompts faster.
    gpu_args = ["-ub", "1024"] if hw["backend"] != "cpu" else []
    if model_key == "9b":
        if 4.5 <= vram < 7.5:
            m["file"] = "Qwen3.5-9B-Q3_K_M.gguf"   # 3-bit so it fits entirely on a 6 GB card
            m["size_gb"] = 4.6
    elif model_key == "35b":
        total = hw["ram_gb"] + vram
        # Best quality that fits RAM + GPU memory (more bits = more accurate, especially for code)
        if total >= 40:
            m["file"], m["size_gb"] = "Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf", 22.2
        elif total >= 30:
            m["file"], m["size_gb"] = "Qwen3.5-35B-A3B-UD-IQ4_XS.gguf", 17.5
    ctx = context_for(model_key, m["size_gb"], hw["ram_gb"], vram, hw["cores"])
    if whisper is None:
        whisper = "small" if hw["ram_gb"] >= 14 and hw["cores"] >= 6 else "base"
    wname = f"ggml-{whisper}.en.bin"
    mmproj = m["file"].replace(".gguf", ".mmproj-F16.gguf")
    return {
        "model_key": model_key, "label": m["label"], "backend": hw["backend"], "cuda": hw.get("cuda", ""),
        "cuda_driver": hw.get("cuda_driver", ""),
        "model_file": m["file"], "model_url": f"{HF}/{m['repo']}/resolve/main/{m['file']}",
        "model_gb": m["size_gb"],
        "vision": bool(vision), "mmproj_file": mmproj,
        "mmproj_url": f"{HF}/{m['repo']}/resolve/main/mmproj-F16.gguf",
        "whisper_file": wname, "whisper_url": f"{HF}/ggerganov/whisper.cpp/resolve/main/{wname}",
        "gpu_args": gpu_args, "context": ctx, "threads": max(1, hw["cores"]),
        "threads_batch": max(1, hw.get("threads") or hw["cores"]),
    }


# ---- Plans from the shared catalog (stellai_tune): any model that fits, picked on the setup screen ------------
def tune_hw(hw):
    """This PC in the form stellai_tune ranks for."""
    return {"cpu": hw.get("cpu", ""), "cores": hw.get("cores") or 2, "threads": hw.get("threads") or 4,
            "ram_gb": hw.get("ram_gb") or 8, "avx2": hw.get("avx2", True),
            "gpus": (hw.get("gpu_list") or []) if hw.get("backend") != "cpu" else []}


def ram_budgets(ram):
    """(label, GB) choices for how much memory StellAI may use."""
    most = max(2, ram - (6 if ram >= 64 else 4 if ram >= 24 else 3))
    return [(f"{most} GB: nearly all (a dedicated server)", most), (f"{ram * 3 // 4} GB: three quarters", ram * 3 // 4),
            (f"{ram // 2} GB: half", ram // 2)]


def cache_ram_mb(budget_gb, model_gb):
    spare = budget_gb * 1024 - model_gb * 1024 - 2048
    return 8192 if spare >= 12288 else 4096 if spare >= 6144 else 2048 if spare >= 3072 else 0


def plan_tuned(hw, choice, vision=True, whisper=None, ram_budget=None, gpu_ids=None, helper=None, try_mtp=True,
               context=None):
    """A plan for a catalog model. choice: {"key", "quant", "size_gb", "context", "mtp", "label"} from
    stellai_tune.rank()/options(), or {"custom_url": ...}. The exact files are looked up when installing."""
    budget = ram_budget or hw.get("ram_gb", 8)
    gpu_args = ["-ub", "1024"] if hw["backend"] != "cpu" else []
    if whisper is None:
        whisper = "small" if hw["ram_gb"] >= 14 and hw["cores"] >= 6 else "base"
    wname = f"ggml-{whisper}.en.bin"
    all_ids = [g["index"] for g in hw.get("gpu_list") or []]
    llama_gpus = "" if gpu_ids is None or sorted(gpu_ids) == sorted(all_ids) else (
        ",".join(str(i) for i in gpu_ids) if gpu_ids else "-1")
    if choice.get("custom_url"):
        name = choice["custom_url"].split("?")[0].rsplit("/", 1)[-1]
        label, mid, gb, ctx = "Custom: " + name[:-5], "custom", float(choice.get("size_gb") or 8), 16384
        files = [{"name": name, "url": choice["custom_url"]}]
    else:
        label = f"{choice['label']} {choice['quant']}"
        mid, gb, ctx, files, name = f"{choice['key']}|{choice['quant']}", choice["size_gb"], choice["context"], [], ""
    return {
        "model_key": mid, "tuned_choice": not choice.get("custom_url"), "label": label, "backend": hw["backend"],
        "cuda": hw.get("cuda", ""), "cuda_driver": hw.get("cuda_driver", ""),
        "model_file": name, "model_files": files, "model_url": files[0]["url"] if files else "", "model_gb": gb,
        "vision": bool(vision) and not choice.get("custom_url"), "mmproj_file": "", "mmproj_url": "",
        "whisper_file": wname, "whisper_url": f"{HF}/ggerganov/whisper.cpp/resolve/main/{wname}",
        "gpu_args": gpu_args, "context": int(context or ctx), "threads": max(1, hw["cores"]),
        "threads_batch": max(1, hw.get("threads") or hw["cores"]),
        "try_mtp": bool(try_mtp and choice.get("mtp")), "mtp": False, "spec_args": [],
        "llama_gpus": llama_gpus, "ram_budget_gb": budget, "cache_ram_mb": cache_ram_mb(budget, gb),
        "helper": helper, "helper_gpu": (helper or {}).get("gpu"), "helper_file": "",
    }


def resolve_plan(pl, status=lambda t: None):
    """Fills in the exact files of a catalog plan from Hugging Face (falls back to the files StellAI knew)."""
    if not pl.get("tuned_choice") or not tune:
        return pl
    key, _, quant = pl["model_key"].partition("|")
    status("Looking up the model files on Hugging Face…")
    try:
        r = tune.resolve(key, quant, mtp=pl.get("try_mtp"))
    except (RuntimeError, urllib.error.URLError, OSError, ValueError) as e:
        status(f"Couldn't look the files up ({e}); using a model StellAI knew before")
        old = {"qwen3.5-4b": "4b", "qwen3.5-9b": "9b"}.get(key, "35b" if "35b" in key or "27b" in key or "122b" in key
                                                         else "4b")
        legacy = plan({"backend": pl["backend"], "ram_gb": pl["ram_budget_gb"], "cores": pl["threads"],
                       "threads": pl["threads_batch"], "vram_gb": 0, "cuda": pl.get("cuda", "")}, old)
        pl.update({k: legacy[k] for k in ("model_key", "label", "model_file", "model_url", "model_gb", "mmproj_file",
                                          "mmproj_url")}, tuned_choice=False, try_mtp=False,
                  model_files=[{"name": legacy["model_file"], "url": legacy["model_url"]}])
        return pl
    pl.update(model_key=f"{r['key']}|{r['quant']}", label=f"{r['label']} {r['quant']}" + (" (MTP)" if r["mtp"] else ""),
              model_file=r["model_file"], model_files=[{"name": f["name"], "url": f["url"]} for f in r["files"]],
              model_url=r["files"][0]["url"], model_gb=r["size_gb"], mtp=r["mtp"], resolved=r,
              mmproj_file=(r["mmproj"] or {}).get("name", ""), mmproj_url=(r["mmproj"] or {}).get("url", ""))
    if not r["mmproj"]:
        pl["vision"] = False
    return pl


def tune_now(cfg, log=lambda t: None):
    """Measures the installed model (servers must be stopped): the context that loads and whether MTP helps.
    Updates the plan and returns a short report."""
    pl = dict(cfg.get("plan") or {})
    exe = cfg.p("bin", "llama", "llama-server.exe" if os.name == "nt" else "llama-server")
    model = cfg.p("models", pl.get("model_file", ""))
    if not (tune and os.path.isfile(exe) and os.path.isfile(model)):
        raise RuntimeError("nothing to test yet: install a model first")
    flags = llama_flags(cfg)
    args = ["-t", str(cfg.get("threads") or pl["threads"]), "-tb", str(pl.get("threads_batch") or pl["threads"])]
    args += pl.get("gpu_args") or []
    if pl.get("backend", "cpu") != "cpu":
        args += ["--fit-target", str(1024 + (1000 if pl.get("vision") else 0) + (700 if cfg.get("whisper_gpu") else 0))]
    args += ["-fa", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--jinja"]
    env = {"CUDA_VISIBLE_DEVICES": pl["llama_gpus"]} if pl.get("llama_gpus") else None
    res = tune.tune(exe, model, args, int(pl.get("context") or 16384), env=env, try_mtp=bool(pl.get("mtp")),
                    has_opt=lambda o: o in flags, log=log)
    if res.get("error"):
        raise RuntimeError("the model didn't load: " + str(res["error"])[:300])
    pl["context"] = res["context"]
    pl["spec_args"] = list(tune.MTP_ARGS) if res.get("mtp") else []
    pl["tuned"] = {"context": res["context"], "mtp": res.get("mtp", False), "tok_s": res.get("gen_tps")}
    cfg.set(plan=pl, context=None)
    man = tune.read_manifest(cfg.p("models"))
    if pl.get("model_key") in man:
        man[pl["model_key"]]["tuned"] = pl["tuned"]
        tune.write_manifest(cfg.p("models"), man)
    lines = [f"Writes {res.get('gen_tps', 0):g} tokens/s, reads prompts at {res.get('prompt_tps', 0):g} tokens/s.",
             f"Conversation window: {res['context'] // 1024}K tokens."]
    if pl.get("mtp"):
        lines.append(f"MTP speed-up: {'on' if res.get('mtp') else 'off'} "
                     f"({res.get('mtp_tps', 0):g} tokens/s with it{'' if res.get('mtp') else ': not faster here'}).")
    return " ".join(lines)


# ============================================================================ GitHub releases
def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode().strip()


def pick_llama_assets(assets, backend, cuda="", driver="", skip=()):
    """From a llama.cpp release's asset names, pick the zips for this backend (x64 Windows).

    For CUDA: the newest build whose CUDA version the driver supports (driver = the "CUDA Version"
    nvidia-smi reports, e.g. "13.0"), skipping versions that already failed on this PC (skip, e.g.
    ["13.1"]). A build newer than the driver fails to load with "PTX was compiled with an unsupported
    toolchain". Falls back to Vulkan when no CUDA build fits.
    Returns a list of asset dicts, main build first; for CUDA the matching runtime DLL zip too."""
    names = {a["name"]: a for a in assets}

    def find(pattern):
        rx = re.compile(pattern)
        hits = sorted((n for n in names if rx.search(n)), reverse=True)
        return names[hits[0]] if hits else None

    if backend == "cuda":
        majors = ["12"] if cuda == "12" else ["13", "12"] if cuda == "13" else ["12"]
        limit = _version_tuple(driver) if driver else None
        builds = []
        for n in names:
            m = re.match(r"^llama-.*-bin-win-cuda-(\d+)\.(\d+)-x64\.zip$", n)
            if not m or m.group(1) not in majors or f"{m.group(1)}.{m.group(2)}" in skip:
                continue
            ver = (int(m.group(1)), int(m.group(2)))
            if limit and limit != (0,) and ver > limit:
                continue
            builds.append((ver, n))
        if builds:
            ver, main = max(builds)
            v = f"{ver[0]}.{ver[1]}"
            rt = find(rf"^cudart-llama-.*bin-win-cuda-{re.escape(v)}-x64\.zip$")
            return [names[main]] + ([rt] if rt else [])
        backend = "vulkan"
    if backend == "vulkan":
        main = find(r"^llama-.*-bin-win-vulkan-x64\.zip$")
        if main:
            return [main]
    main = find(r"^llama-.*-bin-win-cpu-x64\.zip$") or find(r"^llama-.*-bin-win-(avx2|noavx)-x64\.zip$")
    if not main:
        raise RuntimeError("no Windows build found in this llama.cpp release. Files: "
                           + ", ".join(sorted(names)[:30]))
    return [main]


def has_win_builds(release):
    return any(re.search(r"-bin-win-.*x64\.zip$", a["name"]) for a in release.get("assets", []))


def llama_release(get=http_json, text=http_text):
    """The newest llama.cpp release that ships Windows builds.

    Stable vX.Y releases may only carry nightly-tag.txt pointing at the build tag with the binaries."""
    api = f"https://api.github.com/repos/{LLAMA_REPO}"
    try:
        latest = get(f"{api}/releases/latest")
        if has_win_builds(latest):
            return latest
        tagfile = next((a for a in latest.get("assets", []) if a["name"] == "nightly-tag.txt"), None)
        if tagfile:
            tag = text(tagfile["browser_download_url"]).split()[0]
            rel = get(f"{api}/releases/tags/{tag}")
            if has_win_builds(rel):
                return rel
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        pass
    for rel in get(f"{api}/releases?per_page=15"):
        if has_win_builds(rel):
            return rel
    raise RuntimeError("couldn't find a llama.cpp release with Windows builds")


def whisper_asset(get=http_json, gpu=False):
    """(tag, asset): the CUDA build (bundles its own CUDA DLLs) for NVIDIA cards, else the CPU build."""
    if gpu:
        try:
            for rel in get(f"https://api.github.com/repos/{WHISPER_REPO}/releases?per_page=10"):
                for a in rel.get("assets", []):
                    if re.fullmatch(r"whisper-bin-win-cuda-12\.[\d.]+-x64\.zip", a["name"]):
                        return rel["tag_name"], a
        except (urllib.error.URLError, OSError, ValueError):
            pass
    api = f"https://api.github.com/repos/{WHISPER_REPO}"
    rels = []
    try:
        rels.append(get(f"{api}/releases/latest"))
    except (urllib.error.URLError, OSError, ValueError):
        pass
    if not rels or not any(a["name"] == "whisper-bin-x64.zip" for a in rels[0].get("assets", [])):
        rels = get(f"{api}/releases?per_page=10")
    for rel in rels:
        for a in rel.get("assets", []):
            if a["name"] == "whisper-bin-x64.zip":
                return rel["tag_name"], a
    raise RuntimeError("couldn't find whisper-bin-x64.zip in the whisper.cpp releases")


# ============================================================================ downloads
class Cancelled(Exception):
    pass


def download(url, dest, progress=None, cancel=None, chunk=1 << 20):
    """Download with resume (dest + '.part'). progress(done_bytes, total_bytes)."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    part = dest + ".part"
    err = None
    have = os.path.getsize(part) if os.path.exists(part) else 0
    for attempt in range(5):
        headers = {"User-Agent": UA}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:   # server ignored the range: start over
                    have = 0
                length = int(r.headers.get("Content-Length") or 0)
                total = have + length if length else 0
                with open(part, "ab" if have else "wb") as f:
                    while True:
                        if cancel and cancel.is_set():
                            raise Cancelled()
                        buf = r.read(chunk)
                        if not buf:
                            break
                        f.write(buf)
                        have += len(buf)
                        if progress:
                            progress(have, total)
            if total and have < total:
                raise urllib.error.URLError("connection closed early")
            os.replace(part, dest)
            return dest
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:       # already complete
                os.replace(part, dest)
                return dest
            if e.code in (401, 403, 404):
                raise RuntimeError(f"download failed ({e.code}): {url}")
            err = e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            err = e
        have = os.path.getsize(part) if os.path.exists(part) else 0
        time.sleep(2 + attempt * 3)
    raise RuntimeError(f"download failed after retries: {err}")


def extract_flat(zip_path, dest_dir):
    """Extract a zip so the .exe/.dll files land directly in dest_dir (drops a top folder like Release/)."""
    os.makedirs(dest_dir, exist_ok=True)
    real = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = os.path.basename(info.filename.replace("\\", "/"))
            if not name:
                continue
            target = os.path.realpath(os.path.join(dest_dir, name))
            if os.path.dirname(target) != real:
                continue
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


# ============================================================================ config
class Config:
    def __init__(self, root=None):
        self.root = root or data_dir()
        self.path = os.path.join(self.root, "config.json")
        self.data = {}
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass

    def get(self, k, default=None):
        return self.data.get(k, default)

    def set(self, **kw):
        self.data.update(kw)
        os.makedirs(self.root, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def p(self, *parts):
        return os.path.join(self.root, *parts)


def api_key(cfg):
    path = cfg.p("api-key.txt")
    try:
        with open(path) as f:
            k = f.read().strip()
        if k:
            return k
    except OSError:
        pass
    import secrets
    k = secrets.token_urlsafe(36).replace("-", "").replace("_", "")[:48]
    os.makedirs(cfg.root, exist_ok=True)
    with open(path, "w") as f:
        f.write(k)
    return k


def new_api_key(cfg):
    """Replaces the API key (stop the servers first). Every phone, tablet and browser must be paired again."""
    import secrets
    k = secrets.token_urlsafe(36).replace("-", "").replace("_", "")[:48]
    os.makedirs(cfg.root, exist_ok=True)
    tmp = cfg.p("api-key.txt.new")
    with open(tmp, "w") as f:
        f.write(k)
    os.replace(tmp, cfg.p("api-key.txt"))
    return k


# ---- Firewall: Stella's ports are open to the home network and Tailscale only
FIREWALL_RULE = "StellAI"
FIREWALL_FROM = "LocalSubnet,100.64.0.0/10"     # this PC's own network(s) + Tailscale's address range


def firewall_add_args():
    return (f'advfirewall firewall add rule name="{FIREWALL_RULE}" dir=in action=allow protocol=TCP '
            f'localport={LLAMA_PORT},{HUB_PORT} profile=private,domain remoteip={FIREWALL_FROM}')


def firewall_state(show=None):
    """'limited' (home network + Tailscale only), 'open' (the older rule: any address on a private network),
    'none' (no rule: Windows asks or blocks) or 'unknown'. Reading the rule needs no administrator rights."""
    if show is None:
        if os.name != "nt":
            return "unknown"
        try:
            r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}"],
                               capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
            show = (r.stdout or "") if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return "unknown"
    if "100.64.0.0" in show:
        return "limited"
    return "open" if re.search(r"\b8092\b", show) else "none"


def limit_firewall():
    """Replaces the firewall rule with the limited one. Windows asks for administrator approval (one prompt)."""
    import ctypes
    cmd = (f'/c netsh advfirewall firewall delete rule name="{FIREWALL_RULE}" & netsh {firewall_add_args()}')
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd, None, 0) > 32


def is_installed(cfg):
    pl = cfg.get("plan")
    return bool(pl) and os.path.isfile(cfg.p("bin", "llama", "llama-server.exe")) and \
        os.path.isfile(cfg.p("models", pl["model_file"]))


def cuda_driver_version(pl=None):
    """The newest CUDA version the installed NVIDIA driver supports ("13.0"), or ""."""
    try:
        nv = _nvidia()
    except Exception:
        nv = None
    return (nv or {}).get("cuda") or (pl or {}).get("cuda_driver", "")


def _zip_ok(path, size=0, digest=""):
    """True if path is a complete, readable zip (matching the release's size and SHA-256 when known)."""
    import hashlib
    try:
        if size and os.path.getsize(path) != size:
            return False
        if digest.startswith("sha256:"):
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            if h.hexdigest() != digest.split(":", 1)[1].lower():
                return False
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None
    except (OSError, zipfile.BadZipFile, ValueError, RuntimeError, EOFError):
        return False


def fetch_zip(asset, folder, progress=None, cancel=None):
    """Downloads a release zip and checks it; a damaged or stale copy is deleted and fetched again."""
    dest = os.path.join(folder, asset["name"])
    size, digest = int(asset.get("size") or 0), str(asset.get("digest") or "")
    for attempt in range(2):
        if os.path.exists(dest) and not _zip_ok(dest, size, digest):
            os.remove(dest)
        if attempt and os.path.exists(dest + ".part"):
            os.remove(dest + ".part")         # don't resume a download that produced a bad file
        download(asset["browser_download_url"], dest, progress, cancel)
        if _zip_ok(dest, size, digest):
            return dest
    try:
        os.remove(dest)
    except OSError:
        pass
    raise RuntimeError(f"{asset['name']} was damaged twice while downloading; check the connection and try again")


def install_llama(cfg, pl, status, prog, cancel):
    """Downloads the llama.cpp build for plan pl (if it isn't there yet). Returns its assets.

    The new build is unpacked next to the old one and only swapped in once it's complete, so a failed
    download never leaves StellAI without a working engine."""
    bin_llama, dl = cfg.p("bin", "llama"), cfg.p("downloads")
    os.makedirs(dl, exist_ok=True)
    status("Finding the latest llama.cpp build…")
    rel = llama_release()
    assets = pick_llama_assets(rel["assets"], pl["backend"], pl.get("cuda", ""),
                               driver=cuda_driver_version(pl) if pl["backend"] == "cuda" else "",
                               skip=cfg.get("bad_cuda") or [])
    want_tag = f"{rel['tag_name']}:{pl['backend']}:{assets[0]['name']}"
    if cfg.get("llama_build") != want_tag or not os.path.isfile(os.path.join(bin_llama, "llama-server.exe")):
        new = bin_llama + ".new"
        shutil.rmtree(new, ignore_errors=True)
        for a in assets:
            z = fetch_zip(a, dl, prog(a["name"]), cancel)
            status(f"Unpacking {a['name']}…")
            extract_flat(z, new)
        if not os.path.isfile(os.path.join(new, "llama-server.exe")):
            shutil.rmtree(new, ignore_errors=True)
            raise RuntimeError("llama-server.exe wasn't in the download")
        old = bin_llama + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(bin_llama):
            os.replace(bin_llama, old)
        os.replace(new, bin_llama)
        shutil.rmtree(old, ignore_errors=True)
        cfg.set(llama_build=want_tag)
    return assets


# Errors that mean "this CUDA build can't run with this driver/GPU" (not out-of-memory or a bad model)
CUDA_BUILD_ERRORS = re.compile(
    r"PTX was compiled with an unsupported toolchain|no kernel image is available|"
    r"CUDA driver version is insufficient|cudaErrorUnsupportedPtxVersion|"
    r"cudaErrorInsufficientDriver|ggml_cuda_init: failed to initialize CUDA", re.I)


def cuda_build_failed(log_text):
    return bool(CUDA_BUILD_ERRORS.search(log_text or ""))


def current_cuda_build(cfg):
    """CUDA version of the installed llama.cpp build ("13.1"), or "" for Vulkan/CPU."""
    m = re.search(r"-bin-win-cuda-(\d+\.\d+)-x64\.zip", cfg.get("llama_build") or "")
    return m.group(1) if m else ""


def recover_from_cuda_failure(cfg, status=lambda t: None, cancel=None):
    """After llama-server failed with a CUDA build/driver error: remember that build as bad and
    install the next one down (an older CUDA build, then Vulkan). Returns a sentence for the log."""
    pl = dict(cfg.get("plan") or {})
    bad = list(cfg.get("bad_cuda") or [])
    ver = current_cuda_build(cfg)
    if ver and ver not in bad:
        bad.append(ver)
    cfg.set(bad_cuda=bad)
    if pl.get("backend") == "cuda" and ver == "":
        pl["backend"] = "vulkan"      # not a CUDA build after all: nothing lower to try
    attempts = cfg.get("cuda_recoveries", 0) + 1
    cfg.set(cuda_recoveries=attempts)
    if attempts > 4:
        pl["backend"] = "vulkan"
    def prog(label):
        return lambda done, total: status(f"{label}: {done / 2**20:.0f} MB")
    try:
        assets = install_llama(cfg, pl, status, prog, cancel)
    except RuntimeError:
        if pl.get("backend") != "cuda":
            raise
        pl["backend"] = "vulkan"             # couldn't get a CUDA build: Vulkan still uses the GPU
        assets = install_llama(cfg, pl, status, prog, cancel)
    if "-cuda-" not in assets[0]["name"]:
        pl["backend"] = "vulkan"
        hw = dict(cfg.get("hardware") or {})
        if hw:
            hw["backend"] = "vulkan"
            cfg.set(hardware=hw)
    cfg.set(plan=pl)
    used = assets[0]["name"]
    return (f"Your NVIDIA driver can't run the CUDA {ver or ''} build, so StellAI switched to "
            + (f"the CUDA {current_cuda_build(cfg)} build." if "-cuda-" in used else "Vulkan (still uses the GPU).")
            + " Updating the NVIDIA driver lets it use the newest build.")


def packages(hw):
    """What setup will install on this PC and why: [(name, detail)], plus tips. Shown before installing."""
    out = []
    be = hw.get("backend", "cpu")
    drv = hw.get("cuda_driver", "")
    if be == "cuda":
        newest = "13.x" if hw.get("cuda") == "13" else "12.x"
        out.append(("AI engine", f"llama.cpp, NVIDIA CUDA build: the newest CUDA {newest} build your driver "
                                 f"(CUDA {drv or '?'}) can run, with its CUDA runtime"))
        out.append(("Voice", "whisper.cpp CUDA 12.4 build (bundles its own CUDA files)"))
    elif be == "vulkan":
        out.append(("AI engine", f"llama.cpp, Vulkan build for {hw.get('gpu') or 'the graphics card'}"))
        out.append(("Voice", "whisper.cpp CPU build"))
    else:
        out.append(("AI engine", "llama.cpp CPU build (uses AVX2/AVX-512 automatically when the processor has it)"))
        out.append(("Voice", "whisper.cpp CPU build"))
    out.append(("Visual C++ runtime", "already installed" if hw.get("vcredist", True)
                else "Microsoft Visual C++ runtime (Windows asks for permission once)"))
    tips = list(hw.get("notes") or [])
    if drv and _version_tuple(drv) < (13, 0) and hw.get("gpu"):
        tips.append(f"Your NVIDIA driver supports CUDA {drv}. Updating it (nvidia.com or the NVIDIA app) lets "
                    "StellAI use the newest, fastest CUDA build.")
    return out, tips


def ensure_vcredist(cfg, status, cancel=None):
    """Installs the Microsoft Visual C++ runtime if it's missing (one UAC prompt). Returns True when present."""
    if vcredist_present():
        return True
    status("Installing the Microsoft Visual C++ runtime (Windows will ask for permission)…")
    exe = cfg.p("downloads", "vc_redist.x64.exe")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    try:
        download(VCREDIST_URL, exe, None, cancel)
        with open(exe, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("the download isn't a Windows program")
        ps = ("$p = Start-Process -FilePath '" + exe.replace("'", "''") + "' -ArgumentList '/install','/quiet',"
              "'/norestart' -Verb RunAs -Wait -PassThru; exit $p.ExitCode")
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, timeout=600, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        if p.returncode not in (0, 1638, 3010):     # 1638: a newer one is installed; 3010: restart pending
            raise RuntimeError(f"installer exit code {p.returncode}")
    except Exception as e:
        status(f"Couldn't install the Visual C++ runtime ({e}). If the AI engine won't start, install it from "
               "https://aka.ms/vs/17/release/vc_redist.x64.exe")
        return False
    finally:
        try:
            os.remove(exe)
        except OSError:
            pass
    return vcredist_present()


def install(cfg, pl, status, progress, cancel):
    """Download engines and models for plan pl. status(text), progress(fraction or None)."""
    bin_llama, bin_whisper, models = cfg.p("bin", "llama"), cfg.p("bin", "whisper"), cfg.p("models")
    dl = cfg.p("downloads")
    os.makedirs(dl, exist_ok=True)

    def prog(label):
        def f(done, total):
            if total:
                progress(done / total)
                status(f"{label}: {done / 2**30:.2f} of {total / 2**30:.2f} GB")
            else:
                progress(None)
                status(f"{label}: {done / 2**20:.0f} MB")
        return f

    ensure_vcredist(cfg, status, cancel)
    assets = install_llama(cfg, pl, status, prog, cancel)

    if not os.path.isfile(os.path.join(bin_whisper, "whisper-server.exe")):
        status("Finding the latest whisper.cpp build…")
        try:
            tag, a = whisper_asset(gpu=pl["backend"] == "cuda")
            z = fetch_zip(a, dl, prog("Whisper"), cancel)
            extract_flat(z, bin_whisper)
            cfg.set(whisper_gpu="cuda" in a["name"])
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            status(f"Voice recognition skipped: {e}")

    pl = resolve_plan(pl, status)
    files = pl.get("model_files") or [{"name": pl["model_file"], "url": pl["model_url"]}]
    todo = [("Speech model", pl["whisper_url"], pl["whisper_file"])]
    todo += [(pl["label"] + (f" (part {i + 1} of {len(files)})" if len(files) > 1 else ""), f["url"], f["name"])
             for i, f in enumerate(files)]
    if pl["vision"] and pl.get("mmproj_url"):
        todo.append(("Vision add-on", pl["mmproj_url"], pl["mmproj_file"]))
    for label, url, name in todo:
        download(url, os.path.join(models, name), prog(label), cancel)
    if pl.get("helper") and tune:
        try:
            h = tune.resolve(pl["helper"]["key"], pl["helper"].get("quant", "Q8_0"))
            download(h["files"][0]["url"], os.path.join(models, h["model_file"]), prog("Helper model"), cancel)
            pl["helper_file"] = h["model_file"]
        except (RuntimeError, urllib.error.URLError, OSError, ValueError) as e:
            status(f"The helper model was skipped ({e}); the main model does its jobs")
    if tune and pl.get("resolved"):
        tune.record_model(models, pl.pop("resolved"), pl["mmproj_file"] if pl["vision"] else "")
    try:
        download(EMBED_URL, os.path.join(models, EMBED_FILE), prog("Search model"), cancel)
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        status(f"File search will use keywords only: {e}")

    for d in ("diagrams", "images", "wakewords", "logs"):
        os.makedirs(cfg.p(d), exist_ok=True)
    api_key(cfg)
    # Keep only the zips of the current build to save space
    keep = {a["name"] for a in assets}
    for f in os.listdir(dl):
        if f.endswith(".zip") and f not in keep:
            try:
                os.remove(os.path.join(dl, f))
            except OSError:
                pass
    cfg.set(plan=pl, installed_at=time.strftime("%Y-%m-%d %H:%M"))
    progress(1.0)
    status("Installed.")


# ============================================================================ processes
class Job:
    """A Windows job object: child processes die with the app, even if it crashes."""

    def __init__(self):
        self.h = None
        if os.name != "nt":
            return
        try:
            k = ctypes.windll.kernel32
            self.h = k.CreateJobObjectW(None, None)

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                            ("SchedulingClass", ctypes.c_uint32)]

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rt", "wt", "ot")]

            class EXT(ctypes.Structure):
                _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]
            info = EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k.SetInformationJobObject(self.h, 9, ctypes.byref(info), ctypes.sizeof(info))
        except Exception:
            self.h = None

    def add(self, proc):
        if self.h:
            try:
                k = ctypes.windll.kernel32
                ph = k.OpenProcess(0x1F0FFF, False, proc.pid)
                k.AssignProcessToJobObject(self.h, ph)
                k.CloseHandle(ph)
            except Exception:
                pass


_LLAMA_HELP = {"tag": None, "text": ""}
SLEEP_IDLE_MIN = 30      # the model leaves memory after this long without a message (0 in the config = never)
SMALL_GPU_GB = 8         # below this, the vision add-on runs on the CPU so the GPU memory goes to the model


def llama_flags(cfg):
    """This llama-server build's --help text (read once per installed build). New options are only passed when
    the build lists them: an unknown option would stop the model from starting at all."""
    exe = cfg.p("bin", "llama", "llama-server.exe")
    try:
        tag = (os.path.getmtime(exe), os.path.getsize(exe))
    except OSError:
        return ""
    if _LLAMA_HELP["tag"] != tag:
        try:
            r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
                               stdin=subprocess.DEVNULL, cwd=os.path.dirname(exe), errors="replace")
            text = (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError):
            text = ""
        _LLAMA_HELP.update(tag=tag, text=text)
    return _LLAMA_HELP["text"]


def vision_on_cpu(cfg, pl, flags):
    """On small graphics cards the photo add-on (about 1 GB) runs on the CPU: photos take a few seconds longer to
    read, and that GPU memory holds more of the model for every other reply. Config "vision_gpu": true undoes it."""
    if pl.get("backend", "cpu") == "cpu" or cfg.get("vision_gpu") or "--no-mmproj-offload" not in flags:
        return False
    vram = (cfg.get("hardware") or {}).get("vram_gb") or 0
    return 0 < vram < SMALL_GPU_GB


def launch_context(cfg, pl):
    """The context window to start the model with. Worked out from the hardware at every start, so servers
    installed before the bigger windows get them without reinstalling. Config "context": 16384 overrides it."""
    if cfg.get("context"):
        return int(cfg.get("context"))
    hw = cfg.get("hardware")
    key = pl.get("model_key")
    if not hw or key not in MODELS:
        return int(pl["context"])
    vram = (hw.get("vram_gb") or 0) if pl.get("backend", "cpu") != "cpu" else 0
    return context_for(key, pl.get("model_gb") or MODELS[key]["size_gb"], hw.get("ram_gb") or 8, vram, hw.get("cores") or 2)


def llama_args(cfg, pl, key):
    ctx = launch_context(cfg, pl)
    a = [cfg.p("bin", "llama", "llama-server.exe"), "-m", cfg.p("models", pl["model_file"]),
         "-c", str(ctx), "-t", str(cfg.get("threads") or pl["threads"]),
         "-tb", str(pl.get("threads_batch") or pl["threads"])]   # all threads for reading prompts
    a += pl["gpu_args"]
    flags = llama_flags(cfg)
    mm = cfg.p("models", pl["mmproj_file"])
    vision = pl.get("vision") and os.path.isfile(mm)
    cpu_vision = bool(vision) and vision_on_cpu(cfg, pl, flags)
    if vision:
        a += ["--mmproj", mm] + (["--no-mmproj-offload"] if cpu_vision else [])
    if pl.get("backend", "cpu") != "cpu":
        # GPU memory --fit leaves free for what it doesn't know about: the vision add-on and GPU Whisper
        a += ["--fit-target", str(1024 + (1000 if vision and not cpu_vision else 0) + (700 if cfg.get("whisper_gpu") else 0))]
    # Free the model's RAM and GPU memory after a quiet spell; the next message loads it again in a few seconds
    idle = cfg.get("sleep_idle_min")
    idle = SLEEP_IDLE_MIN if idle is None else int(idle)
    if idle > 0 and "--sleep-idle-seconds" in flags:
        a += ["--sleep-idle-seconds", str(idle * 60)]
    # Cap thinking so it can't fill the context
    a += ["--reasoning-budget", "1024" if ctx <= 8192 else "2048",
          "--reasoning-budget-message", "I have thought enough; time to answer."]
    # The speed-up the speed test chose (MTP), and a prompt cache in RAM sized to the memory budget
    if pl.get("spec_args") and "draft-mtp" in flags:
        a += list(pl["spec_args"])
    if pl.get("cache_ram_mb") is not None and "--cache-ram" in flags:
        a += ["--cache-ram", str(int(pl["cache_ram_mb"]))]
    a += ["-fa", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--jinja",
          "--api-key", key, "--host", "0.0.0.0", "--port", str(LLAMA_PORT)]
    return a


def llama_env(pl):
    """Which graphics cards Stella's model may use ("" = all, "-1" = none)."""
    g = (pl or {}).get("llama_gpus")
    return {"CUDA_VISIBLE_DEVICES": g} if g else None


HELPER_PORT = 8095


def helper_args(cfg, pl):
    """The small helper model for quick background jobs, on the processor or a spare card."""
    name = (pl or {}).get("helper_file")
    exe = cfg.p("bin", "llama", "llama-server.exe")
    if not name or not (os.path.isfile(exe) and os.path.isfile(cfg.p("models", name))):
        return None
    on_gpu = pl.get("helper_gpu") not in (None, "")
    return [exe, "-m", cfg.p("models", name), "-c", "8192", "-np", "2", "-t", "4" if pl["threads"] >= 8 else "2",
            "-ngl", "99" if on_gpu else "0", "-fa", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
            "--jinja", "--host", "127.0.0.1", "--port", str(HELPER_PORT)]


def whisper_args(cfg, pl):
    exe = cfg.p("bin", "whisper", "whisper-server.exe")
    model = cfg.p("models", pl["whisper_file"])
    if not (os.path.isfile(exe) and os.path.isfile(model)):
        return None
    return [exe, "-m", model, "-t", str(max(1, min(4, pl["threads"]))), "-bo", "1",
            "--host", "127.0.0.1", "--port", str(WHISPER_PORT)]


def embed_args(cfg):
    exe = cfg.p("bin", "llama", "llama-server.exe")
    model = cfg.p("models", EMBED_FILE)
    if not (os.path.isfile(exe) and os.path.isfile(model)):
        return None
    return [exe, "-m", model, "--embedding", "--pooling", "mean", "-ngl", "0", "-c", "8192", "-b", "2048", "-ub", "2048",
            "-np", "1", "-t", "2", "--host", "127.0.0.1", "--port", str(EMBED_PORT)]


def hub_args(cfg):
    args = ["--key-file", cfg.p("api-key.txt"), "--db", cfg.p("chats.db"), "--diagram-dir", cfg.p("diagrams"),
            "--image-dir", cfg.p("images"), "--wakeword-dir", cfg.p("wakewords"),
            "--llama", f"http://127.0.0.1:{LLAMA_PORT}", "--whisper", f"http://127.0.0.1:{WHISPER_PORT}",
            "--mmdc", shutil.which("mmdc") or "mmdc", "--app-port", str(HUB_PORT), "--avatar-dir", cfg.p("avatar")]
    if os.path.isfile(cfg.p("models", EMBED_FILE)):
        args += ["--embed", f"http://127.0.0.1:{EMBED_PORT}"]
    if helper_args(cfg, cfg.get("plan") or {}):
        args += ["--helper", f"http://127.0.0.1:{HELPER_PORT}"]
    if cfg.get("searxng"):
        args += ["--searxng", f"http://127.0.0.1:{SEARXNG_PORT}"]
    return args


def web_chat_url(cfg):
    from urllib.parse import quote
    return f"http://127.0.0.1:{HUB_PORT}/#key={quote(api_key(cfg), safe='')}"


def avatar_url(cfg):
    from urllib.parse import quote
    return f"http://127.0.0.1:{HUB_PORT}/avatar#key={quote(api_key(cfg), safe='')}"


def open_avatar(cfg):
    """Stella's avatar screen, full screen in Edge/Chrome's app mode (allowed to talk without a click)."""
    url = avatar_url(cfg)
    pf = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for exe in [shutil.which("msedge"), shutil.which("chrome")] + \
               [os.path.join(b, *rel) for b in pf if b for rel in (("Microsoft", "Edge", "Application", "msedge.exe"),
                                                                    ("Google", "Chrome", "Application", "chrome.exe"))]:
        if exe and os.path.isfile(exe):
            subprocess.Popen([exe, f"--app={url}", "--start-fullscreen", "--autoplay-policy=no-user-gesture-required",
                              f"--user-data-dir={cfg.p('avatar-browser')}", "--no-first-run"], close_fds=True)
            return
    import webbrowser
    webbrowser.open(url)


# ---- Private web search: SearXNG in Docker Desktop (optional; DuckDuckGo otherwise) ----
def docker_exe():
    return shutil.which("docker") or (r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
                                      if os.path.isfile(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe") else None)


def setup_searxng(cfg, log):
    """Creates the SearXNG container (bound to this PC only). Returns True when it's running."""
    import secrets
    exe = docker_exe()
    if not exe:
        raise RuntimeError("Docker Desktop isn't installed")
    folder = cfg.p("searxng")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "settings.yml"), "w") as f:
        f.write("use_default_settings: true\nserver:\n  secret_key: \"%s\"\n  limiter: false\n  image_proxy: false\n"
                "search:\n  safe_search: 0\n  formats:\n    - html\n    - json\n" % secrets.token_hex(24))
    _run([exe, "rm", "-f", "stellai-searxng"], timeout=60)
    out = subprocess.run([exe, "run", "-d", "--name", "stellai-searxng", "--restart", "unless-stopped",
                          "-p", f"127.0.0.1:{SEARXNG_PORT}:8080", "-v", f"{folder}:/etc/searxng", "searxng/searxng:latest"],
                         capture_output=True, text=True, timeout=900, creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip().splitlines()[-1][:300] if (out.stderr or out.stdout) else
                           "docker run failed")
    cfg.set(searxng=True)
    log("Web search (SearXNG) is set up")
    return True


def start_searxng(cfg):
    exe = docker_exe()
    if exe and cfg.get("searxng"):
        _run([exe, "start", "stellai-searxng"], timeout=60)


class Servers:
    """Starts and stops llama-server, whisper-server and the in-process hub."""

    def __init__(self, cfg, hub_module, log):
        self.cfg, self.hub, self.log = cfg, hub_module, log
        self.procs = {}
        self.web = None
        self.job = Job()
        self.lock = threading.Lock()
        self.wanted = False
        self._state = {"llama": "stopped", "whisper": "stopped", "hub": "stopped"}
        self._monitor = None

    def _spawn(self, name, args, env=None):
        logf = open(self.cfg.p("logs", f"{name}.log"), "w", encoding="utf-8", errors="replace")
        # Voice recognition runs a notch above the model, so Stella hears you promptly even while she's writing
        prio = ABOVE_NORMAL if name == "whisper" else 0
        p = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             creationflags=NO_WINDOW | prio, cwd=os.path.dirname(args[0]),
                             env=dict(os.environ, **env) if env else None)
        self.job.add(p)
        self.procs[name] = (p, logf)
        self.log(f"started {name} (pid {p.pid})")

    def start(self):
        with self.lock:
            if self.wanted:
                return
            self.wanted = True
            pl = self.cfg.get("plan")
            key = api_key(self.cfg)
            os.makedirs(self.cfg.p("logs"), exist_ok=True)
            self._spawn("llama", llama_args(self.cfg, pl, key), llama_env(pl))
            ha = helper_args(self.cfg, pl)
            if ha:
                self._spawn("helper", ha, {"CUDA_VISIBLE_DEVICES": str(pl.get("helper_gpu")) if
                                           pl.get("helper_gpu") not in (None, "") else "-1"})
            wa = whisper_args(self.cfg, pl)
            if wa:
                self._spawn("whisper", wa)
            else:
                self.log("voice recognition not installed; voice commands won't work")
            ea = embed_args(self.cfg)
            if ea:
                self._spawn("embed", ea)
            if self.cfg.get("searxng"):
                threading.Thread(target=start_searxng, args=(self.cfg,), daemon=True).start()
            if getattr(sys, "frozen", False):
                self.hub.DIAGRAM_RUNNER_CMD = [sys.executable, "--schemdraw-runner"]
                self.hub.CODE_RUNNER_CMD = [sys.executable, "--python-runner"]
            self.hub.LOG_HOOK = self.log
            self.web = self.hub.start(self.hub.parse_args(hub_args(self.cfg)))
            self.hub.SERVER_CONTROL = WinServerControl(self)   # lets the phone app switch models
            self._move_hooks()
            threading.Thread(target=self.web.serve_forever, daemon=True, name="hub").start()

    # ---- moving Stella to another server, and back ------------------------------------------------------------
    on_moved = None        # set by the app: called with the new server's info after Stella moved away
    on_arrived = None      # set by the app: called after Stella moved here (servers restarted)

    def _move_hooks(self):
        mv = getattr(self.hub, "MOVE", None)
        if mv is None:
            return
        mv["client_hook"] = self._became_client
        mv["restart_hook"] = lambda: threading.Thread(target=self._restart_after_move, daemon=True).start()

    def _became_client(self, info):
        """Stella moved away: stop the model, voice and helpers; the hub stays as a signpost."""
        with self.lock:
            for name in ("llama", "whisper", "embed", "helper"):
                entry = self.procs.pop(name, None)
                if entry:
                    p, logf = entry
                    if p.poll() is None:
                        p.terminate()
                    logf.close()
        self.cfg.set(client={"server": info.get("address", ""), "key": api_key(self.cfg)}, moved=info)
        self.log(f"Stella moved to {info.get('name')}; this PC is a client now")
        if self.on_moved:
            self.on_moved(info)

    def start_signpost(self):
        """After a move away: only the hub runs, pointing phones and browsers to Stella's new server."""
        with self.lock:
            if self.web:
                return
            self.hub.LOG_HOOK = self.log
            self.web = self.hub.start(self.hub.parse_args(hub_args(self.cfg)))
            self._move_hooks()
            threading.Thread(target=self.web.serve_forever, daemon=True, name="hub").start()

    def _restart_after_move(self):
        """A move arrived (the hub saved it): restart everything so it's put in place, as Stella."""
        time.sleep(1)
        self.stop()
        self.cfg.set(client=None, moved=None)
        try:
            os.remove(self.cfg.p("moved.json"))
        except OSError:
            pass
        self.start()
        if self.on_arrived:
            self.on_arrived()

    def restart_llama(self):
        """Restarts only the AI model (after switching models)."""
        with self.lock:
            entry = self.procs.pop("llama", None)
            if entry:
                p, logf = entry
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(10)
                    except subprocess.TimeoutExpired:
                        p.kill()
                logf.close()
            if self.wanted:
                pl = self.cfg.get("plan")
                self._spawn("llama", llama_args(self.cfg, pl, api_key(self.cfg)), llama_env(pl))

    def stop(self):
        with self.lock:
            self.wanted = False
            if self.web:
                try:
                    self.web.shutdown()
                    self.web.server_close()
                except Exception:
                    pass
                self.web = None
            for name, (p, logf) in list(self.procs.items()):
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(8)
                    except subprocess.TimeoutExpired:
                        p.kill()
                logf.close()
                self.log(f"stopped {name}")
            self.procs.clear()
            self._state = {"llama": "stopped", "whisper": "stopped", "hub": "stopped"}

    def state(self):
        """Last known status, name -> 'running' | 'starting' | 'stopped' | 'crashed'. Never blocks."""
        if not self._monitor:
            self._monitor = threading.Thread(target=self._watch, daemon=True, name="monitor")
            self._monitor.start()
        return dict(self._state)

    def _watch(self):
        while True:
            try:
                self._state = self._probe()
            except Exception:
                pass
            time.sleep(2)

    def _probe(self):
        out = {}
        procs = dict(self.procs)
        for name in ("llama", "whisper"):
            if name not in procs:
                out[name] = "stopped"
                continue
            p = procs[name][0]
            if p.poll() is not None:
                out[name] = "crashed"
            else:
                port = LLAMA_PORT if name == "llama" else WHISPER_PORT
                out[name] = "running" if _healthy(port) else "starting"
        out["hub"] = "running" if self.web else "stopped"
        return out

    def log_tail(self, name, lines=12):
        try:
            with open(self.cfg.p("logs", f"{name}.log"), encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except OSError:
            return ""


def _healthy(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code in (401, 404)   # up, just strict about the path or key
    except (urllib.error.URLError, OSError):
        return False


# ============================================================================ network / Windows
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # no packet is sent; just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def tailscale_ip():
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    if not (os.path.isfile(exe) or shutil.which("tailscale")):
        return ""
    out = _run([exe, "ip", "-4"], timeout=5).strip().splitlines()
    return out[0].strip() if out else ""


def find_servers(timeout=2.0):
    """StellAI servers among your own Tailscale devices: [(address, name)]. Empty without Tailscale."""
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    if not (os.path.isfile(exe) or shutil.which("tailscale")):
        return []
    try:
        st = json.loads(_run([exe, "status", "--json"], timeout=10) or "{}")
    except ValueError:
        return []
    me = (st.get("Self") or {}).get("UserID")
    peers = [p for p in (st.get("Peer") or {}).values() if p.get("Online") and p.get("UserID") == me]

    def probe(p):
        ip = next((i for i in p.get("TailscaleIPs") or [] if "." in i), "")
        for path in ("/move/hello", "/health"):
            try:
                with urllib.request.urlopen(f"http://{ip}:{HUB_PORT}{path}", timeout=timeout) as r:
                    d = json.loads(r.read() or b"{}")
                if d.get("stellai") or d.get("ok"):
                    if d.get("signpost"):
                        return None       # Stella moved away from that one: it's a client now
                    return ip, p.get("HostName") or ip
            except (urllib.error.URLError, OSError, ValueError):
                pass
        return None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(16) as ex:
        return [r for r in ex.map(probe, peers) if r]


def pair_link(url, key):
    from urllib.parse import quote
    return f"stellai://pair?url={quote(url, safe='')}&key={quote(key, safe='')}"


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart(enable=None):
    """Get (enable=None) or set the 'start with Windows' entry for the current user."""
    if os.name != "nt":
        return False
    import winreg
    exe = sys.executable if getattr(sys, "frozen", False) else f'{sys.executable}" "{os.path.abspath(sys.argv[0])}'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if enable is None:
            try:
                winreg.QueryValueEx(k, "StellAI")
                return True
            except OSError:
                return False
        if enable:
            winreg.SetValueEx(k, "StellAI", 0, winreg.REG_SZ, f'"{exe}" --minimized')
        else:
            try:
                winreg.DeleteValue(k, "StellAI")
            except OSError:
                pass
        return enable


# ============================================================================ self-update
UPDATE_REPO = "courier-iot/StellAI"
UPDATE_EVERY = 30 * 60   # seconds
DETACHED = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def can_self_update():
    return bool(getattr(sys, "frozen", False)) and not APP_VERSION.endswith("-dev")


def latest_update(repo=UPDATE_REPO, get=http_json):
    """Newest Windows build published as a GitHub release (tag windows-vX.Y.Z), or None."""
    best = None
    for rel in get(f"https://api.github.com/repos/{repo}/releases?per_page=30"):
        tag = rel.get("tag_name", "")
        if rel.get("draft") or not tag.startswith("windows-v"):
            continue
        assets = {a["name"]: a for a in rel.get("assets", [])}
        exe = next((a for n, a in assets.items() if n.lower().endswith(".exe")), None)
        sha = assets.get(exe["name"] + ".sha256") if exe else None
        if not exe or not sha:
            continue
        ver = tag[len("windows-v"):]
        if best is None or version_tuple(ver) > version_tuple(best["version"]):
            best = {"version": ver, "url": exe["browser_download_url"], "name": exe["name"],
                    "sha_url": sha["browser_download_url"], "notes": (rel.get("body") or "")[:2000]}
    return best


def update_available(current=None, repo=UPDATE_REPO, get=http_json):
    info = latest_update(repo, get)
    if info and version_tuple(info["version"]) > version_tuple(current or APP_VERSION):
        return info
    return None


def download_update(cfg, info, progress=None, cancel=None, text=http_text):
    """Downloads the installer and checks its SHA-256 against the one published with the release."""
    import hashlib
    folder = cfg.p("updates")
    os.makedirs(folder, exist_ok=True)
    for f in os.listdir(folder):                 # old installers
        if f != info["name"] and not f.startswith(info["name"]):
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass
    dest = os.path.join(folder, info["name"])
    want = text(info["sha_url"]).split()[0].lower()
    for attempt in range(2):
        download(info["url"], dest, progress, cancel)
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            head = f.read(2)
            f.seek(0)
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if head == b"MZ" and h.hexdigest() == want:
            return dest
        os.remove(dest)                            # corrupted: try once more from scratch
    raise RuntimeError("the downloaded update didn't match its checksum")


def run_installer(path, cfg):
    """Starts the installer silently; it replaces the app and starts it again. Data is untouched."""
    log = cfg.p("logs", "update-install.log")
    subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                      f"/LOG={log}"], creationflags=DETACHED, close_fds=True, cwd=os.path.dirname(path))


# ============================================================================ client mode
def is_weak(hw):
    """Too slow to run Stella well: better to use a stronger StellAI server (same chats, faster answers)."""
    return (hw["backend"] == "cpu" and hw["cores"] <= 2) or hw["ram_gb"] < 8


def client_url(cfg):
    c = cfg.get("client") or {}
    from urllib.parse import quote
    return f"http://{c.get('server', '')}:{HUB_PORT}/#key={quote(c.get('key', ''), safe='')}"


def check_server(server, timeout=5):
    try:
        with urllib.request.urlopen(f"http://{server}:{HUB_PORT}/health", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ============================================================================ model switching from the phone
class WinServerControl:
    """The hub's server-control interface for the Windows app: list, download and switch models."""

    def __init__(self, servers):
        self.servers = servers
        self.cfg = servers.cfg
        self.download = {}

    def _hw(self):
        return self.cfg.get("hardware") or {"ram_gb": 8, "cores": 2, "threads": 4, "backend": "cpu", "vram_gb": 0, "cuda": ""}

    def _plan(self, key):
        cur = self.cfg.get("plan") or {}
        whisper = (cur.get("whisper_file") or "ggml-base.en.bin").replace("ggml-", "").replace(".en.bin", "")
        return plan(self._hw(), key, vision=cur.get("vision", False), whisper=whisper)

    def _tuned(self):
        pl = self.cfg.get("plan") or {}
        hw = self._hw()
        ids = None
        if pl.get("llama_gpus") == "-1":
            ids = []
        elif pl.get("llama_gpus"):
            ids = [int(x) for x in pl["llama_gpus"].split(",") if x.isdigit()]
        return tune_hw(hw), pl.get("ram_budget_gb") or hw.get("ram_gb", 8), ids

    def catalog(self):
        cur = (self.cfg.get("plan") or {}).get("model_file")
        out = []
        if tune:
            try:
                thw, budget, ids = self._tuned()
                out = tune.library(thw, budget, self.cfg.p("models"), cur, ids, self.download)
            except Exception:
                out = []
        for key, m in MODELS.items():
            pl = self._plan(key)
            path = self.cfg.p("models", pl["model_file"])
            have = os.path.isfile(path) and os.path.getsize(path) > 100_000_000
            if out and not have:
                continue        # with the full catalog, the old three only show when they're downloaded
            out.append({"key": key, "label": m["label"], "about": m["about"], "file": pl["model_file"],
                        "size_gb": pl["model_gb"], "context": pl["context"], "installed": have,
                        "current": pl["model_file"] == cur, "download": self.download.get(key)})
        return out

    def status(self):
        pl = self.cfg.get("plan") or {}
        return {"model": pl.get("label", ""), "file": pl.get("model_file", ""), "backend": pl.get("backend", ""),
                "can_switch": True}

    def switch(self, key):
        if tune and "|" in key:
            e = tune.read_manifest(self.cfg.p("models")).get(key)
            if not e or not tune.installed(self.cfg.p("models"), e):
                raise ValueError("download it first")
            row = next((r for r in self.catalog() if r["key"] == key), {})
            pl = dict(self.cfg.get("plan") or {})
            tuned = e.get("tuned") or {}
            pl.update(model_key=key, label=e.get("label", key), model_file=e["model_file"], model_gb=e.get("size_gb", 0),
                      model_files=[{"name": n, "url": ""} for n in e.get("files", [])], mtp=e.get("mtp", False),
                      mmproj_file=e.get("mmproj", ""), context=tuned.get("context") or row.get("context") or 16384,
                      spec_args=list(tune.MTP_ARGS) if tuned.get("mtp") else [], tuned_choice=True)
            pl["vision"] = bool(pl.get("vision") and pl["mmproj_file"])
            self.cfg.set(plan=pl)
            threading.Thread(target=self.servers.restart_llama, daemon=True).start()
            return f"switching to {pl['label']}; it takes a minute to load"
        if key not in MODELS:
            raise ValueError("unknown model")
        pl = self._plan(key)
        if not os.path.isfile(self.cfg.p("models", pl["model_file"])):
            raise ValueError("download it first")
        self.cfg.set(plan=pl)
        threading.Thread(target=self.servers.restart_llama, daemon=True).start()
        return f"switching to {pl['label']}; it takes a minute to load"

    def start_download(self, key):
        if tune and "|" in key:
            state = {"state": "downloading", "done": 0, "total": 0, "error": ""}
            self.download[key] = state
            vision = bool((self.cfg.get("plan") or {}).get("vision"))

            def fetch(url, dest, st):
                download(url, dest, lambda done, total: st.__setitem__("done", done))

            def work_tuned():
                try:
                    tune.download_model(key, self.cfg.p("models"), state, fetch, vision=vision)
                    state["state"] = "ready"
                except Exception as e:
                    state.update(state="failed", error=str(e))
            threading.Thread(target=work_tuned, daemon=True).start()
            return "download started"
        if key not in MODELS:
            raise ValueError("unknown model")
        pl = self._plan(key)
        state = {"state": "downloading", "done": 0, "total": 0, "error": ""}
        self.download[key] = state

        def prog(done, total):
            state["done"], state["total"] = done, total

        def work():
            try:
                download(pl["model_url"], self.cfg.p("models", pl["model_file"]), prog)
                if pl["vision"]:
                    download(pl["mmproj_url"], self.cfg.p("models", pl["mmproj_file"]), prog)
                state["state"] = "ready"
            except Exception as e:
                state.update(state="failed", error=str(e))
        threading.Thread(target=work, daemon=True).start()
        return "download started"
