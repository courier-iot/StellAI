#!/usr/bin/env python3
"""StellAI tuning: the model catalog, which models fit this computer (ranked), the exact files to download from
Hugging Face, and speed tests that decide the speed-ups. One file used three ways:

  Linux setup:   python3 stellai_tune.py detect | rank | resolve | bench ...   (see --help)
  Windows app:   import stellai_tune
  StellAI hub:   import stellai_tune (optional)

Everything here is an estimate until bench() has measured this computer; the setup screens say so.
"""
import argparse
import glob
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HF = "https://huggingface.co"
UA = "StellAI-setup"

# ---- The catalog ---------------------------------------------------------------------------------------------
# quants: best first, (tag in the file name, approximate download in GB). The exact files and sizes are looked up
# on Hugging Face at install time (resolve()), so a missing quant falls back to the nearest one that exists.
# kv_mb: conversation memory per 1,000 tokens with the 8-bit cache StellAI uses (Qwen3.5/3.6 keep a per-token
# cache in only one layer out of four, so long chats are cheap). quality: relative, for ranking only.
CATALOG = [
    {"key": "qwen3.5-0.8b", "label": "Qwen3.5 0.8B", "repo": "unsloth/Qwen3.5-0.8B-GGUF",
     "params_b": 0.8, "active_b": 0.8, "moe": False, "kv_mb": 6, "quality": 10, "roles": ["helper"],
     "strengths": ["tiny helper"], "quants": [("Q8_0", 0.8), ("Q4_K_M", 0.53)]},
    {"key": "qwen3.5-2b", "label": "Qwen3.5 2B", "repo": "unsloth/Qwen3.5-2B-GGUF",
     "params_b": 2.0, "active_b": 2.0, "moe": False, "kv_mb": 8, "quality": 18, "roles": ["helper", "main"],
     "strengths": ["helper", "very old laptops"], "quants": [("Q8_0", 2.0), ("Q4_K_M", 1.3)]},
    {"key": "qwen3.5-4b", "label": "Qwen3.5 4B", "repo": "unsloth/Qwen3.5-4B-GGUF",
     "mtp_repo": "unsloth/Qwen3.5-4B-MTP-GGUF",
     "params_b": 4.0, "active_b": 4.0, "moe": False, "kv_mb": 12, "quality": 30, "roles": ["main"],
     "strengths": ["quick everyday help"], "quants": [("Q8_0", 4.5), ("Q6_K", 3.5), ("Q4_K_M", 2.7)]},
    {"key": "qwen3.5-9b", "label": "Qwen3.5 9B", "repo": "unsloth/Qwen3.5-9B-GGUF",
     "mtp_repo": "unsloth/Qwen3.5-9B-MTP-GGUF",
     "params_b": 9.0, "active_b": 9.0, "moe": False, "kv_mb": 20, "quality": 45, "roles": ["main"],
     "strengths": ["code", "reasoning"],
     "quants": [("Q8_0", 9.5), ("Q6_K", 7.5), ("Q5_K_M", 6.6), ("Q4_K_M", 5.7), ("Q3_K_M", 4.6)]},
    {"key": "qwen3.6-27b", "label": "Qwen3.6 27B", "repo": "unsloth/Qwen3.6-27B-GGUF",
     "mtp_repo": "unsloth/Qwen3.6-27B-MTP-GGUF",
     "params_b": 27.0, "active_b": 27.0, "moe": False, "kv_mb": 40, "quality": 74, "roles": ["main"],
     "strengths": ["best coder", "agentic tasks"],
     "quants": [("UD-Q6_K_XL", 24.0), ("UD-Q5_K_XL", 20.0), ("UD-Q4_K_XL", 17.5), ("UD-Q3_K_XL", 14.0),
                ("UD-Q2_K_XL", 11.0)]},
    {"key": "qwen3.6-35b-a3b", "label": "Qwen3.6 35B-A3B", "repo": "unsloth/Qwen3.6-35B-A3B-GGUF",
     "mtp_repo": "unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
     "params_b": 35.0, "active_b": 3.0, "moe": True, "shared_gb": 2.5, "kv_mb": 30, "quality": 70, "roles": ["main"],
     "strengths": ["smart and fast", "code"],
     "quants": [("UD-Q6_K_XL", 31.0), ("UD-Q5_K_XL", 26.0), ("UD-Q4_K_XL", 22.2), ("UD-IQ4_XS", 17.5),
                ("UD-Q3_K_XL", 16.5), ("UD-IQ3_S", 13.6), ("UD-Q2_K_XL", 12.0)]},
    {"key": "qwen3.5-35b-a3b", "label": "Qwen3.5 35B-A3B", "repo": "unsloth/Qwen3.5-35B-A3B-GGUF",
     "params_b": 35.0, "active_b": 3.0, "moe": True, "shared_gb": 2.5, "kv_mb": 30, "quality": 64, "roles": ["main"],
     "strengths": ["earlier version"], "legacy": True,
     "quants": [("UD-Q4_K_XL", 22.2), ("UD-IQ4_XS", 17.5), ("UD-IQ3_S", 13.6)]},
    {"key": "qwen3.5-122b-a10b", "label": "Qwen3.5 122B-A10B", "repo": "unsloth/Qwen3.5-122B-A10B-GGUF",
     "mtp_repo": "unsloth/Qwen3.5-122B-A10B-MTP-GGUF",
     "params_b": 122.0, "active_b": 10.0, "moe": True, "shared_gb": 6.0, "kv_mb": 60, "quality": 84,
     "roles": ["main"], "strengths": ["smartest", "big-RAM servers"],
     "quants": [("UD-Q4_K_XL", 72.0), ("UD-Q3_K_XL", 56.0), ("UD-IQ3_S", 46.6), ("UD-Q2_K_XL", 41.8),
                ("UD-IQ2_M", 39.1)]},
]
BY_KEY = {m["key"]: m for m in CATALOG}
# the setup's old choices (MODEL_CHOICE=1/2/3) and the Windows app's old keys
LEGACY_KEYS = {"1": "qwen3.5-4b", "2": "qwen3.5-9b", "3": "qwen3.6-35b-a3b", "4b": "qwen3.5-4b",
               "9b": "qwen3.5-9b", "35b": "qwen3.6-35b-a3b"}
CONTEXTS = [8192, 16384, 32768, 65536, 131072]
MTP_ARGS = ["--spec-type", "draft-mtp", "--spec-draft-n-max", "2"]

# memory bandwidth guesses (GB/s) by graphics card name: decoding speed is mostly reading the model each token
GPU_BW = [("h100", 2000), ("a100", 1500), ("v100", 900), ("p100", 700), ("p40", 340), ("t4", 300), ("p4", 190),
          ("a2", 200), ("a4000", 450), ("a5000", 768), ("a6000", 768), ("l4", 300), ("5090", 1790),
          ("4090", 1000), ("4080", 717), ("4070", 504), ("4060", 272), ("3090", 936), ("3080", 760),
          ("3070", 448), ("3060", 360), ("2080", 448), ("2070", 448), ("2060", 336), ("1080", 320), ("1070", 256),
          ("1660", 192), ("1650", 128), ("7900", 800), ("6900", 512), ("6800", 512), ("7800", 624),
          ("7700", 432), ("6700", 384), ("7600", 288), ("6600", 224), ("mi50", 1000), ("mi25", 480)]


def _gpu_bw(name, vendor="nvidia"):
    n = (name or "").lower()
    for k, bw in GPU_BW:
        if k in n:
            return bw
    return 300 if vendor == "nvidia" else 220


# ---- Hardware --------------------------------------------------------------------------------------------------
def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def nvidia_gpus():
    """Every NVIDIA card: index, name, memory in GB (total and free now)."""
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if os.name == "nt" else "")
    if not exe:
        return []
    out = _run([exe, "--query-gpu=index,name,memory.total,memory.used", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0].isdigit():
            total = float(parts[2] or 0) / 1024
            used = float(parts[3] or 0) / 1024 if len(parts) > 3 and parts[3].replace(".", "").isdigit() else 0
            gpus.append({"index": int(parts[0]), "name": parts[1], "vram_gb": round(total, 1),
                         "free_gb": round(total - used, 1), "vendor": "nvidia"})
    return gpus


def amd_gpus():
    gpus = []
    for i, d in enumerate(sorted(glob.glob("/sys/class/drm/card[0-9]/device"))):
        try:
            if open(os.path.join(d, "vendor")).read().strip() != "0x1002":
                continue
            vb = int(open(os.path.join(d, "mem_info_vram_total")).read().strip())
        except (OSError, ValueError):
            continue
        if vb >= 4 * 2**30:
            gpus.append({"index": i, "name": "AMD graphics", "vram_gb": round(vb / 2**30, 1),
                         "free_gb": round(vb / 2**30, 1), "vendor": "amd"})
    return gpus


def detect(target_dir="."):
    """This computer, for ranking. Linux reads /proc; Windows callers pass their own detection instead."""
    ram = 8.0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram = int(line.split()[1]) / 2**20
    except OSError:
        pass
    flags, cpu = "", ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("flags") and not flags:
                    flags = line
                elif line.startswith("model name") and not cpu:
                    cpu = line.split(":", 1)[1].strip()
    except OSError:
        pass
    cores = 0
    out = _run(["lscpu", "-p=core,socket"])
    if out:
        cores = len({l for l in out.splitlines() if l and not l.startswith("#")})
    cores = cores or max(1, (os.cpu_count() or 2) // 2)
    try:
        os.makedirs(target_dir, exist_ok=True)
        disk = shutil.disk_usage(target_dir).free / 2**30
    except OSError:
        disk = 0.0
    gpus = nvidia_gpus() or amd_gpus()
    return {"cpu": cpu, "cores": cores, "threads": os.cpu_count() or cores, "ram_gb": round(ram, 1),
            "avx2": " avx2 " in flags + " ", "avx512": " avx512f " in flags + " ", "gpus": gpus,
            "disk_free_gb": round(disk, 1)}


def cpu_bandwidth(hw):
    """A rough guess at RAM speed (GB/s): servers have more memory channels, laptops fewer."""
    cores = hw.get("cores") or 2
    bw = 30 if cores <= 4 else 40 if cores <= 8 else 55 if cores <= 16 else 70
    if not hw.get("avx2", True):
        bw *= 0.6        # no AVX2 (e.g. Xeons before 2014): the CPU can't keep up with its own memory
    return bw


# ---- Estimates -------------------------------------------------------------------------------------------------
def _bits(m, size_gb):
    return size_gb * 8 / m["params_b"]


def quality(m, size_gb):
    q = m["quality"]
    b = _bits(m, size_gb)
    if b < 2.8:
        q *= 0.78
    elif b < 3.4:
        q *= 0.86
    elif b < 4.2:
        q *= 0.94
    elif b < 4.7:
        q *= 0.98
    return round(q, 1)


def estimate(m, size_gb, hw, ram_budget_gb, gpu_ids=None, ctx=32768):
    """Where it would run and about how fast (words... well, tokens per second) on this computer."""
    gpus = [g for g in hw.get("gpus", []) if gpu_ids is None or g["index"] in gpu_ids]
    vram = sum(max(0.0, g["vram_gb"] - (1.0 if g["vram_gb"] < 8 else 1.5)) for g in gpus)
    kv = ctx / 1000 * m["kv_mb"] / 1000
    total = size_gb + kv + 0.6
    if total > ram_budget_gb + vram:
        return None
    cbw = cpu_bandwidth(hw)
    gbw = min((_gpu_bw(g["name"], g["vendor"]) for g in gpus), default=0) * (0.9 if len(gpus) > 1 else 1.0)
    per_token = size_gb * m["active_b"] / m["params_b"]          # GB read for every token
    eff = 0.4 if m["moe"] else 0.55        # MoE models lose some speed to picking their experts
    if not gpus or vram < 2:
        where, tps = "cpu", cbw * eff / per_token
        if total > ram_budget_gb:
            return None
    elif total <= vram:
        where, tps = "gpu", gbw * eff / per_token
    else:
        if m["moe"]:
            shared = m.get("shared_gb", 2.0)
            if shared + kv > vram:
                return None
            on_gpu = min(1.0, (vram - shared - kv) / max(0.1, size_gb - shared))
            gpu_part = per_token * (shared / size_gb + (1 - shared / size_gb) * on_gpu)
            cpu_part = per_token - gpu_part
        else:
            on_gpu = max(0.0, (vram - kv) / size_gb)
            gpu_part, cpu_part = per_token * on_gpu, per_token * (1 - on_gpu)
        if size_gb + kv + 0.6 - vram > ram_budget_gb:
            return None
        where = "split"
        tps = 1 / (gpu_part / (gbw * eff) + cpu_part / (cbw * eff))
    speed = "fast" if tps >= 25 else "good" if tps >= 12 else "usable" if tps >= 6 else "slow" if tps >= 3 \
        else "very slow"
    return {"where": where, "tok_s": round(tps, 1), "speed": speed}


def best_context(m, size_gb, hw, ram_budget_gb, gpu_ids=None, target=65536):
    """The longest conversation that keeps the model where it runs fastest (it never drops below 8K)."""
    first = estimate(m, size_gb, hw, ram_budget_gb, gpu_ids, CONTEXTS[0])
    if not first:
        return 0
    if first["where"] == "cpu":       # memory isn't the limit on a CPU, re-reading a long chat is
        return 16384 if (hw.get("cores", 2) >= 6 and m["active_b"] <= 10) else 8192
    best = CONTEXTS[0]
    for c in CONTEXTS:
        if c > target:
            break
        e = estimate(m, size_gb, hw, ram_budget_gb, gpu_ids, c)
        if e and e["where"] == first["where"] and e["tok_s"] >= first["tok_s"] * 0.9:
            best = c
    return best


def options(hw, ram_budget_gb, gpu_ids=None, include_legacy=False, role="main"):
    """Every model + quant that fits, with its estimate. Sorted by quality."""
    out = []
    for m in CATALOG:
        if role not in m["roles"] or (m.get("legacy") and not include_legacy):
            continue
        for tag, gb in m["quants"]:
            e = estimate(m, gb, hw, ram_budget_gb, gpu_ids, 16384)
            if not e:
                continue
            ctx = best_context(m, gb, hw, ram_budget_gb, gpu_ids)
            out.append({"key": m["key"], "label": m["label"], "quant": tag, "size_gb": gb,
                        "quality": quality(m, gb), "context": ctx, "mtp": bool(m.get("mtp_repo")),
                        "strengths": m["strengths"], **e})
    out.sort(key=lambda o: (-o["quality"], -o["tok_s"]))
    return out


def rank(hw, ram_budget_gb, gpu_ids=None, min_tps=6.0):
    """The picks shown first: best overall, fastest, best coder, and smartest if it's slow. Then all that fit."""
    every = options(hw, ram_budget_gb, gpu_ids)
    if not every:
        return {"picks": [], "all": []}
    # picks avoid the roughest quants (under ~3 bits), unless nothing else fits; "all" still lists them
    opts = [o for o in every if _bits(BY_KEY[o["key"]], o["size_gb"]) >= 3.0
            and not re.search(r"(^|-)I?Q[12]_", o["quant"])] or every
    picks, seen = [], set()

    def add(tag, o, why):
        if o and (o["key"], o["quant"]) not in seen:
            seen.add((o["key"], o["quant"]))
            picks.append(dict(o, pick=tag, why=why))

    usable = [o for o in opts if o["tok_s"] >= min_tps] or [o for o in opts if o["tok_s"] >= 3] or opts
    # per model, the best-quality quant that's still usable; a smaller quant often runs far faster on a GPU
    best = max(usable, key=lambda o: (o["quality"] * min(1.0, o["tok_s"] / 15) ** 0.25, o["tok_s"]))
    add("best", best, "best balance of smart and quick on this computer")
    fast_pool = [o for o in opts if o["quality"] >= best["quality"] * 0.6] or opts
    add("fastest", max(fast_pool, key=lambda o: (o["tok_s"], o["quality"])), "quickest replies that are still good")
    coders = [o for o in usable if "best coder" in o["strengths"] or "code" in o["strengths"]]
    if coders:
        add("coder", max(coders, key=lambda o: (("best coder" in o["strengths"]), o["quality"])),
            "strongest at code, CAD and PCB scripts")
    smart = max(opts, key=lambda o: (o["quality"], o["tok_s"]))
    if smart["quality"] > best["quality"] * 1.08 and smart["tok_s"] >= 2:
        add("smartest", smart, "the smartest that fits, but slower")
    return {"picks": picks, "all": every}


def pick_helper(hw, ram_budget_gb, main_size_gb):
    """The small helper model for quick jobs, or None when there's no room. Runs on the CPU unless a free card."""
    spare = ram_budget_gb - main_size_gb
    for key, tag, gb in (("qwen3.5-2b", "Q8_0", 2.0), ("qwen3.5-0.8b", "Q8_0", 0.8)):
        if spare >= gb + 1.5 and (key != "qwen3.5-2b" or hw.get("cores", 2) >= 4):
            return {"key": key, "label": BY_KEY[key]["label"], "quant": tag, "size_gb": gb}
    return None


# ---- Exact files on Hugging Face -------------------------------------------------------------------------------
def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def repo_files(repo, get=http_json):
    """[(path, size_bytes)] of every file in a Hugging Face model repo."""
    items = get(f"{HF}/api/models/{repo}/tree/main?recursive=true")
    out = []
    for it in items if isinstance(items, list) else []:
        if it.get("type") == "file":
            size = (it.get("lfs") or {}).get("size") or it.get("size") or 0
            out.append((it["path"], int(size)))
    return out


def _quant_files(files, tag):
    pat = re.compile(r"(^|[-_.])" + re.escape(tag) + r"(\.gguf$|-\d{5}-of-\d{5}\.gguf$)", re.I)
    hits = [(p, s) for p, s in files if p.lower().endswith(".gguf") and "mmproj" not in p.lower()
            and pat.search(os.path.basename(p))
            and (tag.upper().startswith("UD-") or ("UD-" + tag).lower() not in os.path.basename(p).lower())]
    return sorted(hits)


def _mmproj(files):
    mm = [(p, s) for p, s in files if "mmproj" in p.lower() and p.lower().endswith(".gguf")]
    for want in ("f16", "bf16", "f32", ""):
        for p, s in mm:
            if want in os.path.basename(p).lower():
                return p, s
    return None


def _mmproj_name(model_path, mm_path):
    """Local name of the vision add-on: next to its model, e.g. Qwen3.6-27B-UD-Q4_K_XL.mmproj-F16.gguf."""
    stem = re.sub(r"-\d{5}-of-\d{5}$", "", os.path.splitext(os.path.basename(model_path))[0])
    base = os.path.basename(mm_path)
    return stem + "." + (base if base.lower().startswith("mmproj") else "mmproj.gguf")


def resolve(key, quant=None, mtp=False, get=http_json):
    """The exact files for a model: parts (big models are split), sizes, and the matching vision add-on."""
    m = BY_KEY[LEGACY_KEYS.get(key, key)]
    order = [t for t, _ in m["quants"]]
    quant = quant or order[0]
    repos = ([m["mtp_repo"]] if mtp and m.get("mtp_repo") else []) + [m["repo"]]
    last_err = ""
    for repo in repos:
        try:
            files = repo_files(repo, get)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = f"{repo}: {e}"
            continue
        # the quant asked for, else the nearest one (smaller first, so it still fits)
        i = order.index(quant) if quant in order else 0
        tries = [quant] + order[i + 1:] + list(reversed(order[:i]))
        for tag in tries:
            parts = _quant_files(files, tag)
            if parts:
                mm = _mmproj(files)
                if not mm and repo != m["repo"]:
                    try:
                        mm = _mmproj(repo_files(m["repo"], get))
                        mm_repo = m["repo"]
                    except (urllib.error.URLError, OSError, ValueError):
                        mm, mm_repo = None, repo
                else:
                    mm_repo = repo
                return {"key": m["key"], "label": m["label"], "repo": repo, "quant": tag, "asked": quant,
                        "mtp": repo == m.get("mtp_repo"),
                        "files": [{"name": os.path.basename(p), "url": f"{HF}/{repo}/resolve/main/{p}", "size": s}
                                  for p, s in parts],
                        "model_file": os.path.basename(parts[0][0]),
                        "size_gb": round(sum(s for _, s in parts) / 2**30, 2),
                        "mmproj": ({"name": _mmproj_name(parts[0][0], mm[0]), "url": f"{HF}/{mm_repo}/resolve/main/{mm[0]}",
                                    "size": mm[1]} if mm else None)}
        last_err = f"{repo} has no {quant} file"
    raise RuntimeError("couldn't find the model files on Hugging Face (" + last_err + ")")


# ---- The model library on this computer (for switching models from the phone app) ------------------------------
MANIFEST = "stellai-models.json"


def model_id(key, quant):
    return f"{key}|{quant}"


def read_manifest(models_dir):
    try:
        with open(os.path.join(models_dir, MANIFEST)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_manifest(models_dir, data):
    os.makedirs(models_dir, exist_ok=True)
    tmp = os.path.join(models_dir, MANIFEST + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, os.path.join(models_dir, MANIFEST))


def record_model(models_dir, r, mmproj_name="", tuned=None):
    """Remembers a downloaded model (from resolve()) so the app can list and switch to it."""
    data = read_manifest(models_dir)
    data[model_id(r["key"], r["quant"])] = {
        "label": f"{r['label']} {r['quant']}", "model_file": r["model_file"], "files": [f["name"] for f in r["files"]],
        "size_gb": r["size_gb"], "mtp": r["mtp"], "repo": r["repo"], "mmproj": mmproj_name or "",
        **({"tuned": tuned} if tuned else {})}
    write_manifest(models_dir, data)


def installed(models_dir, entry):
    return all(os.path.isfile(os.path.join(models_dir, n)) and os.path.getsize(os.path.join(models_dir, n)) > 50_000_000
               for n in entry.get("files") or [entry.get("model_file", "-")])


def library(hw, ram_budget_gb, models_dir, current_file="", gpu_ids=None, downloads=None):
    """What the app's model list shows: the picks for this computer, the best version of every other model that
    fits, and everything already downloaded."""
    downloads = downloads or {}
    man = read_manifest(models_dir)
    res = rank(hw, ram_budget_gb, gpu_ids)
    rows, seen = [], set()

    def add(mid, label, about, size, ctx):
        if mid in seen:
            return
        seen.add(mid)
        e = man.get(mid)
        rows.append({"key": mid, "label": label, "about": about, "size_gb": size, "context": ctx,
                     "file": (e or {}).get("model_file", ""), "installed": bool(e) and installed(models_dir, e),
                     "current": bool(e) and e.get("model_file") == current_file, "download": downloads.get(mid)})

    names = {"best": "Best overall", "fastest": "Fastest", "coder": "Best coder", "smartest": "Smartest"}
    for o in res["picks"]:
        add(model_id(o["key"], o["quant"]), f"{o['label']} {o['quant']}",
            f"{names.get(o['pick'], '')}: {o['why']}. About {o['tok_s']:g} tokens/s here (estimate).", o["size_gb"],
            o["context"])
    best_per = {}
    for o in res["all"]:
        if o["key"] not in best_per and _bits(BY_KEY[o["key"]], o["size_gb"]) >= 3.0:
            best_per[o["key"]] = o
    for o in best_per.values():
        add(model_id(o["key"], o["quant"]), f"{o['label']} {o['quant']}",
            f"{', '.join(o['strengths']).capitalize()}. About {o['tok_s']:g} tokens/s here (estimate).", o["size_gb"],
            o["context"])
    for mid, e in man.items():
        if mid not in seen and installed(models_dir, e):
            k = mid.split("|")[0]
            m = BY_KEY.get(k)
            ctx = best_context(m, e.get("size_gb", 5), hw, ram_budget_gb, gpu_ids) if m else 16384
            add(mid, e.get("label", mid), "Downloaded.", e.get("size_gb", 0), ctx or 8192)
    return rows


def download_model(mid, models_dir, state, fetch, vision=True, mtp=True):
    """Looks up the files and downloads them with fetch(url, dest, state); then records the model."""
    key, _, quant = mid.partition("|")
    r = resolve(key, quant or None, mtp=mtp)
    items = list(r["files"]) + ([r["mmproj"]] if vision and r["mmproj"] else [])
    state["total"] = sum(int(f.get("size") or 0) for f in items) or int(r["size_gb"] * 2**30)
    base = 0

    class _Part(dict):          # one file's progress, added to the whole download's
        def __setitem__(self, k, v):
            if k == "done":
                state["done"] = base + v
            dict.__setitem__(self, k, v)

    for f in items:
        fetch(f["url"], os.path.join(models_dir, f["name"]), _Part())
        base += int(f.get("size") or 0)
    mm = r["mmproj"]["name"] if vision and r["mmproj"] else ""
    record_model(models_dir, r, mm)
    return r


# ---- Speed test ------------------------------------------------------------------------------------------------
BENCH_PROMPTS = [
    "Write a Python function that parses a CSV file of resistor values and returns the ones within 5% of a target. "
    "Include type hints and a short docstring.",
    "Explain in two short paragraphs why a pull-up resistor is needed on an open-drain line.",
]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _post(url, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def bench_once(server, model, args, ctx, env=None, timeout=900, n_predict=192, log=print):
    """Starts llama-server with these options, measures prompt reading and writing speed, stops it.
    Returns {"ok", "gen_tps", "prompt_tps", "load_s"} or {"ok": False, "error"}."""
    port = _free_port()
    cmd = [server, "-m", model, "-c", str(ctx), "--host", "127.0.0.1", "--port", str(port), "-np", "1"] + list(args)
    started = time.time()
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace",
                             env=dict(os.environ, **(env or {})),
                             creationflags=0x08000000 if os.name == "nt" else 0)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    try:
        while time.time() - started < timeout:
            if p.poll() is not None:
                tail = (p.stderr.read() or "")[-600:]
                return {"ok": False, "error": "llama-server stopped: " + " ".join(tail.split()[-40:])}
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(1)
        else:
            return {"ok": False, "error": f"didn't load within {timeout} s"}
        load = time.time() - started
        gens, prompts = [], []
        for text in BENCH_PROMPTS:
            r = _post(f"http://127.0.0.1:{port}/completion",
                      {"prompt": text, "n_predict": n_predict, "temperature": 0, "cache_prompt": False}, timeout)
            t = r.get("timings") or {}
            if t.get("predicted_per_second"):
                gens.append(float(t["predicted_per_second"]))
            if t.get("prompt_per_second"):
                prompts.append(float(t["prompt_per_second"]))
        if not gens:
            return {"ok": False, "error": "no timings in the answer"}
        return {"ok": True, "gen_tps": round(sum(gens) / len(gens), 1),
                "prompt_tps": round(sum(prompts) / len(prompts), 1) if prompts else 0, "load_s": round(load, 1)}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    finally:
        p.terminate()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()


def tune(server, model, base_args, ctx, env=None, try_mtp=False, has_opt=lambda o: True, log=print):
    """The context that actually loads (halving down to 8K if it doesn't), then plain vs MTP speed-up.
    MTP is kept only if it's at least 10% faster here: on some computers it's slower."""
    result = {"context": ctx, "mtp": False, "runs": []}
    c = ctx
    plain = None
    while c >= 8192:
        log(f"Loading the model with a {c // 1024}K conversation window...")
        plain = bench_once(server, model, base_args, c, env)
        result["runs"].append({"what": f"plain {c}", **plain})
        if plain["ok"]:
            break
        log("  didn't fit: " + plain.get("error", "")[:200])
        c //= 2
    if not plain or not plain["ok"]:
        result["error"] = plain.get("error") if plain else "no run"
        return result
    result.update(context=c, gen_tps=plain["gen_tps"], prompt_tps=plain["prompt_tps"])
    log(f"  writes {plain['gen_tps']} tokens/s, reads {plain['prompt_tps']} tokens/s")
    if try_mtp and has_opt("draft-mtp"):
        log("Trying the MTP speed-up...")
        m = bench_once(server, model, base_args + MTP_ARGS, c, env)
        result["runs"].append({"what": "mtp", **m})
        if m["ok"]:
            log(f"  with MTP: {m['gen_tps']} tokens/s")
            result["mtp_tps"] = m["gen_tps"]
            if m["gen_tps"] >= plain["gen_tps"] * 1.1:
                result["mtp"] = True
                result["gen_tps"] = m["gen_tps"]
        else:
            log("  MTP didn't run here: " + m.get("error", "")[:160])
    return result


# ---- Command line (for the Linux setup) ------------------------------------------------------------------------
def _shell(d, prefix=""):
    lines = []
    for k, v in d.items():
        if isinstance(v, bool):
            v = "1" if v else "0"
        elif isinstance(v, (list, dict)):
            v = json.dumps(v)
        lines.append(f"{prefix}{k.upper()}={shlex.quote(str(v))}")
    return "\n".join(lines)


def _hw_from_args(a):
    hw = json.loads(a.hw) if a.hw else detect(a.dir)
    return hw


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("--dir", default=".")
    for name in ("rank", "options", "helper"):
        r = sub.add_parser(name)
        r.add_argument("--hw", default="")
        r.add_argument("--dir", default=".")
        r.add_argument("--ram", type=float, required=True, help="GB of RAM StellAI may use")
        r.add_argument("--gpus", default=None, help="comma-separated card numbers for Stella's model ('' = none)")
        r.add_argument("--main-gb", type=float, default=0)
        r.add_argument("--menu", action="store_true", help="tab-separated lines for menu screens")
    rs = sub.add_parser("resolve")
    rs.add_argument("key")
    rs.add_argument("--quant", default=None)
    rs.add_argument("--mtp", action="store_true")
    rs.add_argument("--shell", action="store_true")
    b = sub.add_parser("bench")
    b.add_argument("--server", required=True)
    b.add_argument("--model", required=True)
    b.add_argument("--ctx", type=int, default=32768)
    b.add_argument("--args", default="", help="llama-server options as one string")
    b.add_argument("--mtp", action="store_true")
    b.add_argument("--out", default="")
    a = ap.parse_args(argv)

    if a.cmd == "detect":
        print(json.dumps(detect(a.dir)))
        return 0
    if a.cmd in ("rank", "options", "helper"):
        hw = _hw_from_args(a)
        ids = None if a.gpus is None else [int(x) for x in a.gpus.split(",") if x.strip().isdigit()]
        if a.cmd == "helper":
            h = pick_helper(hw, a.ram, a.main_gb)
            print(_shell(h, "HELPER_") if h else "HELPER_KEY=''")
            return 0
        res = rank(hw, a.ram, ids)
        if not a.menu:
            print(json.dumps(res if a.cmd == "rank" else res["all"]))
            return 0
        icons = {"best": "Best overall", "fastest": "Fastest", "coder": "Best coder", "smartest": "Smartest"}
        rows = res["picks"] if a.cmd == "rank" else res["all"]
        for o in rows:
            head = icons.get(o.get("pick"), "")
            text = (f"{head + ': ' if head else ''}{o['label']} {o['quant']} · {o['size_gb']:g} GB · "
                    f"~{o['tok_s']:g} tok/s ({o['speed']}, {o['where']}) · {o['context'] // 1024}K")
            print(f"{o['key']}|{o['quant']}|{o['context']}|{int(o['mtp'])}\t{text}")
        return 0
    if a.cmd == "resolve":
        r = resolve(a.key, a.quant, a.mtp)
        if a.shell:
            mm = r["mmproj"] or {}
            print(_shell({"model_label": r["label"] + (" (MTP)" if r["mtp"] else ""), "model_repo": r["repo"],
                          "model_name": r["model_file"], "model_quant": r["quant"],
                          "model_mb": int(r["size_gb"] * 1024), "model_mtp": r["mtp"],
                          "model_urls": " ".join(f["url"] for f in r["files"]),
                          "model_parts": " ".join(f["name"] for f in r["files"]),
                          "mmproj_url": mm.get("url", ""), "mmproj_name": mm.get("name", "")}))
        else:
            print(json.dumps(r))
        return 0
    if a.cmd == "bench":
        help_text = _run([a.server, "--help"], 60)
        res = tune(a.server, a.model, shlex.split(a.args), a.ctx, try_mtp=a.mtp,
                   has_opt=lambda o: o in help_text, log=lambda s: print(s, file=sys.stderr, flush=True))
        if a.out:
            with open(a.out, "w") as f:
                json.dump(res, f, indent=1)
        print(_shell({"tuned_context": res.get("context", a.ctx), "tuned_mtp": res.get("mtp", False),
                      "tuned_tps": res.get("gen_tps", 0), "tuned_prompt_tps": res.get("prompt_tps", 0),
                      "tuned_mtp_tps": res.get("mtp_tps", 0), "tuned_error": res.get("error", "")}))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
