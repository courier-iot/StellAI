"""Tests for the Windows setup logic. Run: python windows/test_winsetup.py (works on Windows and Linux)."""
import http.server
import io
import os
import sys
import tempfile
import threading
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
    assert p["gpu_args"] == ["-ub", "1024"] and p["context"] == 32768 and p["model_file"].endswith("UD-IQ3_S.gguf")
    assert p["whisper_file"] == "ggml-small.en.bin"
    p9 = ws.plan(desk, "9b")
    assert p9["model_file"] == "Qwen3.5-9B-Q3_K_M.gguf" and p9["gpu_args"] == ["-ub", "1024"]
    assert p9["mmproj_file"] == "Qwen3.5-9B-Q3_K_M.mmproj-F16.gguf"
    assert p9["mmproj_url"].endswith("unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-F16.gguf")
    assert ws.plan(hw(32, 8), "9b")["model_file"] == "Qwen3.5-9B-Q4_K_M.gguf"
    # More memory -> better 35B quality: 32 GB RAM + 6 GB GPU, and a 16 GB Tesla T4 server
    assert ws.plan(hw(32, 6, "cuda", 6.0, "13"), "35b")["model_file"].endswith("UD-IQ4_XS.gguf")
    assert ws.plan(hw(32, 8, "cuda", 15.0, "13"), "35b")["model_file"].endswith("UD-Q4_K_XL.gguf")
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all tests passed")
