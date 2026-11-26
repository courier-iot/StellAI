"""Tests for the Windows setup logic. Run: python windows/test_winsetup.py (works on Windows and Linux)."""
import http.server
import io
import os
import sys
import tempfile
import time
import threading
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))   # stellai_tune
import winsetup as ws

TAG = "b11960"
NAMES = [f"llama-{TAG}-bin-win-cpu-x64.zip", f"llama-{TAG}-bin-win-cpu-arm64.zip",
         f"llama-{TAG}-bin-win-cuda-12.4-x64.zip", f"llama-{TAG}-bin-win-cuda-13.4-x64.zip",
         f"llama-{TAG}-bin-win-cuda-13.4-arm64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip",
         "cudart-llama-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-arm64.zip",
         f"llama-{TAG}-bin-win-vulkan-x64.zip", f"llama-{TAG}-bin-win-vulkan-arm64.zip",
         f"llama-{TAG}-bin-win-openvino-2026.4.1-x64.zip", f"llama-{TAG}-bin-win-sycl-x64.zip",
         f"llama-{TAG}-bin-ubuntu-x64.tar.gz", f"llama-{TAG}-bin-win-rocm-10.0-x64.zip"]
ASSETS = [{"name": n, "browser_download_url": "https://x/" + n} for n in NAMES]


def names(lst):
    return [a["name"] for a in lst]


def test_assets():
    assert names(ws.pick_llama_assets(ASSETS, "cuda", "13")) == [
        f"llama-{TAG}-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip"]
    assert names(ws.pick_llama_assets(ASSETS, "cuda", "12")) == [
        f"llama-{TAG}-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"]
    assert names(ws.pick_llama_assets(ASSETS, "vulkan")) == [f"llama-{TAG}-bin-win-vulkan-x64.zip"]
    assert names(ws.pick_llama_assets(ASSETS, "cpu")) == [f"llama-{TAG}-bin-win-cpu-x64.zip"]
    # No CUDA 13 build in an older release: fall back to 12
    old = [a for a in ASSETS if "13.4" not in a["name"]]
    assert names(ws.pick_llama_assets(old, "cuda", "13"))[0].endswith("cuda-12.4-x64.zip")
    # No CUDA at all: Vulkan
    nocuda = [a for a in ASSETS if "cuda" not in a["name"]]
    assert names(ws.pick_llama_assets(nocuda, "cuda", "12")) == [f"llama-{TAG}-bin-win-vulkan-x64.zip"]
    # Driver supports CUDA 13.0 only: the 13.4 build would fail ("unsupported toolchain"), use 12.4
    assert names(ws.pick_llama_assets(ASSETS, "cuda", "13", driver="13.0"))[0].endswith("cuda-12.4-x64.zip")
    assert names(ws.pick_llama_assets(ASSETS, "cuda", "13", driver="13.4"))[0].endswith("cuda-13.4-x64.zip")
    assert names(ws.pick_llama_assets(ASSETS, "cuda", "13", skip=["13.4"]))[0].endswith("cuda-12.4-x64.zip")
    # Nothing left: Vulkan
    assert names(ws.pick_llama_assets(ASSETS, "cuda", "13", skip=["13.4", "12.4"])) == [f"llama-{TAG}-bin-win-vulkan-x64.zip"]
    assert names(ws.pick_llama_assets(ASSETS, "cuda", "12", driver="12.2")) == [f"llama-{TAG}-bin-win-vulkan-x64.zip"]
    try:
        ws.pick_llama_assets([{"name": "nightly-tag.txt"}], "cpu")
        raise AssertionError("expected an error")
    except RuntimeError as e:
        assert "nightly-tag.txt" in str(e)


def test_release_lookup():
    stable = {"tag_name": "v0.8.0", "assets": [{"name": "nightly-tag.txt", "browser_download_url": "u://tag"}]}
    nightly = {"tag_name": TAG, "assets": ASSETS}
    calls = []

    def get(url):
        calls.append(url)
        if url.endswith("/releases/latest"):
            return stable
        if url.endswith(f"/releases/tags/{TAG}"):
            return nightly
        raise AssertionError(url)
    assert ws.llama_release(get, lambda u: TAG + "\n")["tag_name"] == TAG
    # latest already has binaries
    assert ws.llama_release(lambda u: nightly, None)["tag_name"] == TAG

    # no nightly-tag.txt: scan the list
    def get2(url):
        if url.endswith("/releases/latest"):
            return {"tag_name": "v0.9", "assets": []}
        return [{"tag_name": "b2", "assets": []}, nightly]
    assert ws.llama_release(get2, None)["tag_name"] == TAG

    rel = {"tag_name": "v1.8.2", "assets": [{"name": "whisper-bin-x64.zip", "browser_download_url": "w"}]}
    assert ws.whisper_asset(lambda u: rel)[0] == "v1.8.2"


def hw(ram, cores=4, backend="cpu", vram=0, cuda=""):
    return {"ram_gb": ram, "cores": cores, "threads": cores * 2, "backend": backend, "vram_gb": vram,
            "cuda": cuda, "cpu": "x", "gpu": "", "gpus": []}


def test_backend_and_plan():
    nv = {"name": "NVIDIA GeForce RTX 2060", "vram_gb": 6.0, "compute_cap": 7.5, "cuda": "13.1"}
    assert ws.choose_backend([], nv)[0::2] == ("cuda", "13")
    assert ws.choose_backend([], dict(nv, cuda="12.8"))[0::2] == ("cuda", "12")
    assert ws.choose_backend([], dict(nv, cuda="13.0", compute_cap=6.1))[0::2] == ("cuda", "12")
    assert ws.choose_backend([], dict(nv, cuda="11.8"))[0] == "vulkan"
    amd = [{"name": "AMD Radeon RX 6600", "vendor": "amd", "vram_gb": 8.0}]
    assert ws.choose_backend(amd, None)[0] == "vulkan"
    igpu = [{"name": "Intel(R) HD Graphics 620", "vendor": "intel", "vram_gb": 0.1}]
    assert ws.choose_backend(igpu, None)[0] == "cpu"

    # ThinkPad T470: 8 GB, 2 cores, Intel iGPU
    t470 = hw(8, cores=2)
    assert ws.recommend(t470) == "4b"
    p = ws.plan(t470, "4b")
    assert p["gpu_args"] == [] and p["context"] == 8192 and p["whisper_file"] == "ggml-base.en.bin"
    # Desktop: Ryzen 5 5500 (6 cores), 20 GB, RTX 2060 6 GB
    desk = hw(20, cores=6, backend="cuda", vram=6.0, cuda="13")
    assert ws.recommend(desk) == "35b"
    p = ws.plan(desk, "35b")
    assert p["gpu_args"] == ["-ub", "1024"] and p["context"] == 65536 and p["model_file"].endswith("UD-IQ3_S.gguf")
    assert p["whisper_file"] == "ggml-small.en.bin"
    p9 = ws.plan(desk, "9b")
    assert p9["model_file"] == "Qwen3.5-9B-Q3_K_M.gguf" and p9["gpu_args"] == ["-ub", "1024"]
    assert p9["mmproj_file"] == "Qwen3.5-9B-Q3_K_M.mmproj-F16.gguf"
    assert p9["mmproj_url"].endswith("unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-F16.gguf")
    assert ws.plan(hw(32, 8), "9b")["model_file"] == "Qwen3.5-9B-Q4_K_M.gguf"
    # More memory -> better 35B quality: 32 GB RAM + 6 GB GPU, and a 16 GB Tesla T4 server
    assert ws.plan(hw(32, 6, "cuda", 6.0, "13"), "35b")["model_file"].endswith("UD-IQ4_XS.gguf")
    assert ws.plan(hw(32, 8, "cuda", 15.0, "13"), "35b")["model_file"].endswith("UD-Q4_K_XL.gguf")
    # Context windows: as long as fits without pushing the model off the graphics card; short without one
    assert p9["context"] == 16384 and ws.plan(desk, "4b")["context"] == 32768
    assert ws.plan(hw(32, 8, "cuda", 15.0, "13"), "9b")["context"] == 65536
    assert ws.plan(hw(16, 6), "9b")["context"] == 16384 and ws.plan(hw(32, 8), "35b")["context"] == 16384
    assert ws.is_weak(t470) and not ws.is_weak(desk)


def test_extract_and_download():
    tmp = tempfile.mkdtemp()
    zp = os.path.join(tmp, "w.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("Release/whisper-server.exe", b"exe")
        z.writestr("Release/ggml.dll", b"dll")
        z.writestr("../evil.txt", b"no")
    ws.extract_flat(zp, os.path.join(tmp, "out"))
    assert sorted(os.listdir(os.path.join(tmp, "out"))) == ["evil.txt", "ggml.dll", "whisper-server.exe"]
    assert not os.path.exists(os.path.join(tmp, "evil.txt"))

    blob = os.urandom(3_000_000)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            start = 0
            rng = self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                self.send_response(206)
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(blob) - start))
            self.end_headers()
            self.wfile.write(blob[start:])

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/m.gguf"
    dest = os.path.join(tmp, "models", "m.gguf")
    os.makedirs(os.path.dirname(dest))
    with open(dest + ".part", "wb") as f:      # pretend an earlier download stopped half way
        f.write(blob[:1_000_000])
    seen = []
    ws.download(url, dest, lambda d, t: seen.append((d, t)))
    with open(dest, "rb") as f:
        assert f.read() == blob
    assert seen[-1] == (len(blob), len(blob)) and seen[0][0] > 1_000_000
    srv.shutdown()


def test_cuda_recovery():
    log = ("0.15.757.561 E D:\\a\\llama.cpp\\ggml-cuda.cu:111: CUDA error\n"
           "CUDA error: the provided PTX was compiled with an unsupported toolchain.\n")
    assert ws.cuda_build_failed(log)
    assert not ws.cuda_build_failed("CUDA error: out of memory")
    c = ws.Config(tempfile.mkdtemp())
    c.set(llama_build=f"{TAG}:cuda:llama-{TAG}-bin-win-cuda-13.4-x64.zip",
          plan={"backend": "cuda", "cuda": "13", "cuda_driver": "13.0"}, hardware={"backend": "cuda"})
    assert ws.current_cuda_build(c) == "13.4"
    got = {}
    orig_rel, orig_dl, orig_ex, orig_nv = ws.llama_release, ws.download, ws.extract_flat, ws._nvidia
    try:
        ws.llama_release = lambda *a, **k: {"tag_name": TAG, "assets": ASSETS}
        ws._nvidia = lambda: None
        def fake_download(url, dest, *a, **k):
            got.setdefault("dl", []).append(os.path.basename(dest))
            n = len(got["dl"])
            if n == 1:                          # first download arrives damaged: must be fetched again
                with open(dest, "wb") as f:
                    f.write(b"PK\x03\x04 broken")
                return dest
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr("llama-server.exe", "exe")
            return dest
        ws.download = fake_download
        msg = ws.recover_from_cuda_failure(c)
        assert c.get("bad_cuda") == ["13.4"] and ws.current_cuda_build(c) == "12.4", (c.get("bad_cuda"), c.get("llama_build"))
        assert "CUDA 12.4" in msg and c.get("plan")["backend"] == "cuda"
        assert os.path.isfile(c.p("bin", "llama", "llama-server.exe")) and not os.path.exists(c.p("bin", "llama.new"))
        assert got["dl"][0] == got["dl"][1], got   # the damaged zip was downloaded again
        msg = ws.recover_from_cuda_failure(c)   # 12.4 fails too: Vulkan
        assert "Vulkan" in msg and c.get("plan")["backend"] == "vulkan" and c.get("hardware")["backend"] == "vulkan"
    finally:
        ws.llama_release, ws.download, ws.extract_flat, ws._nvidia = orig_rel, orig_dl, orig_ex, orig_nv


def test_packages():
    hw = {"backend": "cuda", "cuda": "13", "cuda_driver": "13.0", "gpu": "NVIDIA GeForce RTX 2060", "vcredist": False}
    items, tips = ws.packages(hw)
    d = dict(items)
    assert "CUDA 13.0" in d["AI engine"] and "CUDA 12.4" in d["Voice"] and "permission" in d["Visual C++ runtime"]
    assert not tips
    items, tips = ws.packages({**hw, "cuda": "12", "cuda_driver": "12.6", "vcredist": True})
    assert dict(items)["Visual C++ runtime"] == "already installed" and any("12.6" in t for t in tips)
    items, tips = ws.packages({"backend": "cpu", "notes": ["needs a newer graphics driver"]})
    assert "CPU build" in dict(items)["AI engine"] and tips == ["needs a newer graphics driver"]
    assert ws.vcredist_present() and ws.vulkan_present()      # not Windows: nothing to install
    c = ws.Config(tempfile.mkdtemp())
    assert ws.ensure_vcredist(c, lambda t: None)


def test_misc():
    link = ws.pair_link("http://100.101.1.2:8080", "abc123")
    assert link == "stellai://pair?url=http%3A%2F%2F100.101.1.2%3A8080&key=abc123"
    c = ws.Config(tempfile.mkdtemp())
    k = ws.api_key(c)
    assert len(k) >= 32 and ws.api_key(c) == k
    c.set(plan={"model_file": "m.gguf", "mmproj_file": "mm.gguf", "context": 8192, "threads": 4,
                "gpu_args": ["-ub", "1024"], "vision": True, "whisper_file": "w.bin", "backend": "cuda"})
    assert ws.Config(c.root).get("plan")["context"] == 8192
    args = ws.llama_args(c, c.get("plan"), k)
    assert "--mmproj" not in args          # vision file not downloaded yet
    assert args[args.index("--api-key") + 1] == k and "-tb" in args
    assert args[args.index("--fit-target") + 1] == "1024"   # no vision file, CPU whisper
    assert ws.whisper_args(c, c.get("plan")) is None
    assert not ws.is_installed(c)
    # Security: a replaced key is new and saved; the firewall rule is limited to the home network and Tailscale
    k2 = ws.new_api_key(c)
    assert k2 != k and len(k2) >= 32 and ws.api_key(c) == k2 and not os.path.exists(c.p("api-key.txt.new"))
    k = k2
    assert "remoteip=LocalSubnet,100.64.0.0/10" in ws.firewall_add_args() and "localport=8080,8092" in ws.firewall_add_args()
    assert ws.firewall_state("LocalPort: 8080,8092\nRemoteIP: LocalSubnet,100.64.0.0/10\n") == "limited"
    assert ws.firewall_state("LocalPort: 8080,8092\nRemoteIP: Any\n") == "open" and ws.firewall_state("") == "none"
    # New llama-server options are only used when the installed build lists them in --help
    assert "--sleep-idle-seconds" not in args and ws.llama_flags(c) == ""
    os.makedirs(c.p("models"), exist_ok=True)
    open(c.p("models", "mm.gguf"), "w").close()
    c.set(hardware={"ram_gb": 20, "cores": 6, "threads": 12, "backend": "cuda", "vram_gb": 6.0, "cuda": "13"})
    real = ws.llama_flags
    try:
        ws.llama_flags = lambda cfg: "--sleep-idle-seconds N\n--mmproj-offload, --no-mmproj-offload\n"
        args = ws.llama_args(c, c.get("plan"), k)
        assert args[args.index("--sleep-idle-seconds") + 1] == "1800"
        assert args[args.index("--mmproj") + 2] == "--no-mmproj-offload"      # 6 GB card: vision on the CPU...
        assert args[args.index("--fit-target") + 1] == "1024"                 # ...so no GPU memory is held back for it
        c.set(sleep_idle_min=0, vision_gpu=True)
        args = ws.llama_args(c, c.get("plan"), k)
        assert "--sleep-idle-seconds" not in args and "--no-mmproj-offload" not in args
        assert args[args.index("--fit-target") + 1] == "2024"
        c.set(sleep_idle_min=10, vision_gpu=False, hardware={"backend": "cuda", "vram_gb": 15.0})
        args = ws.llama_args(c, c.get("plan"), k)                             # 16 GB Tesla T4: vision stays on the GPU
        assert args[args.index("--sleep-idle-seconds") + 1] == "600" and "--no-mmproj-offload" not in args
        # the context window is worked out at every start, so an install from before bigger windows gets them
        c.set(plan=dict(c.get("plan"), model_key="35b", model_gb=13.6), hardware={"ram_gb": 20, "cores": 6, "backend": "cuda", "vram_gb": 6.0})
        args = ws.llama_args(c, c.get("plan"), k)
        assert args[args.index("-c") + 1] == "65536" and args[args.index("--reasoning-budget") + 1] == "2048"
        c.set(context=16384)
        assert ws.llama_args(c, c.get("plan"), k)[ws.llama_args(c, c.get("plan"), k).index("-c") + 1] == "16384"
        c.set(context=0, hardware=None)
        assert ws.launch_context(c, c.get("plan")) == 8192          # nothing known about the PC: the saved value
        ws.llama_flags = lambda cfg: "usage: an older build\n"
        args = ws.llama_args(c, c.get("plan"), k)
        assert "--sleep-idle-seconds" not in args and "--no-mmproj-offload" not in args
    finally:
        ws.llama_flags = real


def test_updates():
    import hashlib
    rels = [
        {"tag_name": "windows-v0.1.9", "draft": False, "assets": [
            {"name": "StellAI-Setup-0.1.9.exe", "browser_download_url": "u9"},
            {"name": "StellAI-Setup-0.1.9.exe.sha256", "browser_download_url": "s9"}]},
        {"tag_name": "windows-v0.1.12", "draft": False, "assets": [
            {"name": "StellAI-Setup-0.1.12.exe", "browser_download_url": "u12"},
            {"name": "StellAI-Setup-0.1.12.exe.sha256", "browser_download_url": "s12"}]},
        {"tag_name": "windows-v0.1.13", "draft": True, "assets": []},
        {"tag_name": "windows-v0.1.14", "draft": False, "assets": [   # no checksum: ignored
            {"name": "StellAI-Setup-0.1.14.exe", "browser_download_url": "u14"}]},
        {"tag_name": "v0.9.0", "draft": False, "assets": []},
    ]
    get = lambda url: rels
    assert ws.latest_update("me/repo", get)["version"] == "0.1.12"     # 0.1.12 > 0.1.9 numerically
    assert ws.update_available("0.1.12", "me/repo", get) is None
    assert ws.update_available("0.1.10", "me/repo", get)["url"] == "u12"
    assert ws.version_tuple("0.1.12") > ws.version_tuple("0.1.9")

    # download + checksum against a local server
    blob = b"MZ" + os.urandom(200_000)
    good = hashlib.sha256(blob).hexdigest()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = blob if self.path.endswith(".exe") else (good + "  file\n").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    c = ws.Config(tempfile.mkdtemp())
    os.makedirs(c.p("updates"))
    open(c.p("updates", "StellAI-Setup-0.1.5.exe"), "wb").write(b"old")
    info = {"version": "0.1.12", "name": "StellAI-Setup-0.1.12.exe", "url": base + "/a.exe", "sha_url": base + "/a.sha256"}
    path = ws.download_update(c, info)
    assert open(path, "rb").read() == blob
    assert os.listdir(c.p("updates")) == ["StellAI-Setup-0.1.12.exe"]       # old installer cleaned up
    good_saved = good
    good = "0" * 64                                                          # now a wrong checksum
    os.remove(path)
    try:
        ws.download_update(c, info)
        raise AssertionError("bad checksum accepted")
    except RuntimeError:
        pass
    assert not os.path.exists(path)
    srv.shutdown()


def test_tuned_plans():
    """The shared catalog: ranked picks, plans with cards and memory, multi-part files, MTP and the helper."""
    from unittest import mock
    assert ws.tune, "stellai_tune should import"
    hw = {"ram_gb": 64, "cores": 12, "threads": 24, "backend": "cuda", "cuda": "12", "vram_gb": 15, "avx2": True,
          "gpu_list": [{"index": 0, "name": "Tesla T4", "vram_gb": 15, "vendor": "nvidia"},
                       {"index": 1, "name": "Tesla T4", "vram_gb": 15, "vendor": "nvidia"}]}
    res = ws.tune.rank(ws.tune_hw(hw), 58, [0])
    assert res["picks"] and res["picks"][0]["pick"] == "best"
    pl = ws.plan_tuned(hw, res["picks"][0], ram_budget=58, gpu_ids=[0], helper={"key": "qwen3.5-2b", "quant": "Q8_0",
                                                                              "label": "Qwen3.5 2B", "gpu": 1})
    assert pl["llama_gpus"] == "0" and pl["helper_gpu"] == 1 and pl["model_key"].count("|") == 1
    assert ws.plan_tuned(hw, res["picks"][0], gpu_ids=[0, 1])["llama_gpus"] == ""      # all cards = no limit
    assert ws.plan_tuned(hw, res["picks"][0], gpu_ids=[])["llama_gpus"] == "-1"         # none = processor only
    r = {"key": "qwen3.5-122b-a10b", "label": "Qwen3.5 122B-A10B", "quant": "UD-Q2_K_XL", "mtp": False, "repo": "r",
         "files": [{"name": f"m-0000{i}-of-00002.gguf", "url": f"u{i}", "size": 1} for i in (1, 2)],
         "model_file": "m-00001-of-00002.gguf", "size_gb": 41.8, "mmproj": None}
    with mock.patch.object(ws.tune, "resolve", return_value=r):
        got = ws.resolve_plan(dict(pl))
    assert len(got["model_files"]) == 2 and got["model_file"] == "m-00001-of-00002.gguf" and not got["vision"]
    with mock.patch.object(ws.tune, "resolve", side_effect=RuntimeError("offline")):
        got = ws.resolve_plan(dict(pl))
    assert got["model_key"] in ws.MODELS and got["model_file"].endswith(".gguf")
    with tempfile.TemporaryDirectory() as d:
        cfg = ws.Config(d)
        os.makedirs(cfg.p("models"))
        os.makedirs(cfg.p("bin", "llama"))
        open(cfg.p("bin", "llama", "llama-server.exe"), "w").write("x")
        open(cfg.p("models", "h.gguf"), "w").write("x")
        pl.update(model_file="m.gguf", spec_args=list(ws.tune.MTP_ARGS), helper_file="h.gguf")
        with mock.patch.object(ws, "llama_flags", return_value="--spec-type draft-mtp --cache-ram"):
            a = ws.llama_args(cfg, pl, "k")
        assert "draft-mtp" in a and "--cache-ram" in a
        with mock.patch.object(ws, "llama_flags", return_value=""):      # an older build: no new options
            a = ws.llama_args(cfg, pl, "k")
        assert "draft-mtp" not in a and "--cache-ram" not in a
        cfg.set(plan=pl)
        assert ws.helper_args(cfg, pl) and "--helper" in ws.hub_args(cfg)


def test_mermaid():
    sums = ("aa11  node-v22.12.0-darwin-arm64.tar.gz\nBB22  node-v22.12.0-win-x64.zip\n"
            "cc33  node-v22.12.0-win-x86.zip\ndd44  node-v22.12.0-win-x64.7z\n")
    assert ws.node_zip_entry(sums) == ("bb22", "node-v22.12.0-win-x64.zip")
    assert ws.node_zip_entry("nothing here") is None
    tmp = tempfile.mkdtemp()
    cfg = ws.Config(tmp)
    assert ws.mermaid_exe(cfg) is None
    assert "mmdc.cmd" not in " ".join(ws.hub_args(cfg))
    bin_dir = cfg.p("mermaid", "node_modules", ".bin")
    os.makedirs(bin_dir)
    open(os.path.join(bin_dir, "mmdc.cmd"), "w").close()
    args = ws.hub_args(cfg)
    assert args[args.index("--mmdc") + 1] == os.path.join(bin_dir, "mmdc.cmd")
    assert ws.install_mermaid(cfg, lambda m: None) == os.path.join(bin_dir, "mmdc.cmd")   # already there: no download


def test_freecad():
    assets = [{"name": n, "browser_download_url": "http://x/" + n} for n in (
        "FreeCAD_1.1.1-Linux-x86_64-py311.AppImage", "FreeCAD_1.1.1-Windows-x86_64-py311.7z",
        "FreeCAD_1.1.1-Windows-x86_64-py311-installer.exe", "FreeCAD_1.1.1-Windows-x86_64-py311-installer.exe-SHA256.txt")]
    exe, sums = ws.pick_freecad_installer(assets)
    assert exe["name"] == "FreeCAD_1.1.1-Windows-x86_64-py311-installer.exe" and sums["name"].endswith("-SHA256.txt")
    old = [{"name": "FreeCAD_1.0.2-conda-Windows-x86_64-installer-1.exe"}]
    assert ws.pick_freecad_installer(old)[0]["name"] == old[0]["name"] and ws.pick_freecad_installer(old)[1] is None
    assert ws.pick_freecad_installer([{"name": "FreeCAD_1.1.1-macOS-arm64.dmg"}]) == (None, None)
    tmp = tempfile.mkdtemp()
    cfg = ws.Config(tmp)
    assert ws.freecad_exe(cfg) is None
    os.makedirs(cfg.p("freecad", "bin"))
    open(cfg.p("freecad", "bin", "FreeCADCmd.exe"), "w").close()
    assert ws.install_freecad(cfg, lambda m: None) == cfg.p("freecad", "bin", "FreeCADCmd.exe")   # already there
    ws.freecad_to_server(cfg)
    assert os.environ["STELLAI_FREECAD"] == cfg.p("freecad", "bin", "FreeCADCmd.exe")


NOISY_LLAMA = r"""
import http.server, json, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
sys.stderr.write("load_tensors: layer ready\n" * 40000)   # ~1 MB of log, far more than a pipe holds
sys.stderr.flush()

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def answer(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.answer({"status": "ok"})

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.answer({"content": "hi", "timings": {"predicted_per_second": 20.0, "prompt_per_second": 300.0}})

http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def test_bench_with_a_noisy_log():
    """llama-server writes a lot while loading: the test must not freeze on its log (it once filled a pipe)."""
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "noisy_llama.py"), "w") as f:
        f.write(NOISY_LLAMA)
    started = time.time()
    r = ws.tune.bench_once(sys.executable, "noisy_llama", [], 4096, env={"PYTHONPATH": tmp}, timeout=60, log=lambda m: None)
    assert r["ok"] and r["gen_tps"] == 20.0, r
    assert time.time() - started < 30, "the benchmark was slow: is the log still going to a pipe?"
    with open(os.path.join(tmp, "noisy_llama.py"), "w") as f:
        f.write("import sys\nsys.stderr.write('x' * 200000 + ' error: out of memory')\nsys.exit(1)\n")
    r = ws.tune.bench_once(sys.executable, "noisy_llama", [], 4096, env={"PYTHONPATH": tmp}, timeout=60, log=lambda m: None)
    assert not r["ok"] and "out of memory" in r["error"], r


def test_draft_and_deep_args():
    tmp = tempfile.mkdtemp()
    cfg = ws.Config(tmp)
    args = ws.hub_args(cfg)
    assert args[args.index("--models-dir") + 1] == cfg.p("models") and "--llama-server" in args and "--slot-dir" in args
    pl = {"model_key": "qwen3.6-35b-a3b|UD-IQ3_S", "backend": "cuda"}
    assert ws.draft_model_args(cfg, pl, "--model-draft") is None, "no draft model for a mixture-of-experts model"
    pl = {"model_key": "qwen3.5-9b|Q3_K_M", "backend": "cuda", "helper_file": "Qwen3.5-0.8B-Q8_0.gguf"}
    assert ws.draft_model_args(cfg, pl, "") is None, "not when llama.cpp has no draft models"
    os.makedirs(cfg.p("models"))
    open(cfg.p("models", "Qwen3.5-0.8B-Q8_0.gguf"), "w").close()
    a = ws.draft_model_args(cfg, pl, "--model-draft")
    assert a[:4] == ["-md", cfg.p("models", "Qwen3.5-0.8B-Q8_0.gguf"), "-ngld", "99"], a
    r = ws.tune.tune.__defaults__   # the draft trial is part of the speed test
    assert "draft_args" in ws.tune.tune.__code__.co_varnames


def test_llama_port():
    """Another program on 8080: the model moves to the next free port, remembers it, and everything follows."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    c = ws.Config(tempfile.mkdtemp())
    c.set(plan={"model_file": "m.gguf", "mmproj_file": "mm.gguf", "context": 8192, "threads": 4, "gpu_args": [],
                "whisper_file": "w.bin", "backend": "cpu"})
    assert ws.llama_port(c) == 8080 and ws.pick_llama_port(c, busy=lambda p: False) == (8080, None)
    busy = {8080, 8081}
    assert ws.pick_llama_port(c, busy=lambda p: p in busy) == (8082, 8080) and ws.llama_port(c) == 8082
    assert ws.Config(c.root).get("llama_port") == 8082                       # remembered
    assert ws.pick_llama_port(c, busy=lambda p: p in busy) == (8082, None)   # stays put while it's free
    a = ws.llama_args(c, c.get("plan"), "k")
    assert a[a.index("--port") + 1] == "8082"
    h = ws.hub_args(c)
    assert h[h.index("--llama") + 1] == "http://127.0.0.1:8082"
    # StellAI's own ports are never chosen
    c2 = ws.Config(tempfile.mkdtemp())
    taken = set(range(8080, 8092))
    port, _ = ws.pick_llama_port(c2, busy=lambda p: p in taken)
    assert port not in (ws.HUB_PORT, ws.WHISPER_PORT, ws.EMBED_PORT, ws.HELPER_PORT) and port == 8096, port
    # the firewall rule follows the port, and an old rule without it shows as outdated
    assert "localport=8082,8092" in ws.firewall_add_args(8082)
    assert ws.firewall_state("LocalPort: 8080,8092\nRemoteIP: LocalSubnet,100.64.0.0/10\n", llama=8082) == "outdated"
    assert ws.firewall_state("LocalPort:     8082,8092\nRemoteIP: LocalSubnet,100.64.0.0/10\n", llama=8082) == "limited"

    # real sockets: a program listening on 127.0.0.1 makes the port busy; its 404 isn't a healthy model
    class NotFound(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404); self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), NotFound)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert ws.port_busy(port)
        assert not ws._healthy(port, strict=True) and ws._healthy(port)   # the model must answer; Whisper may 404
    finally:
        srv.shutdown(); srv.server_close()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); free = s.getsockname()[1]; s.close()
    assert not ws.port_busy(free)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all tests passed")
