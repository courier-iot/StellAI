"""StellAI Server for Windows: setup wizard, server control panel and tray icon.

Run from source:  python windows/stellai_app.py
Packaged:         StellAI.exe  (built by .github/workflows/windows.yml)
"""
import os
import sys


def _reattach_console():
    """A windowed PyInstaller app has no stdout/stderr; reconnect to pipes the parent gave us."""
    if os.name != "nt" or (sys.stdout and sys.stderr):
        return
    try:
        import ctypes
        import msvcrt
        k = ctypes.windll.kernel32
        for num, name in ((-11, "stdout"), (-12, "stderr")):
            if getattr(sys, name) is None:
                h = k.GetStdHandle(num)
                if h and h != -1:
                    setattr(sys, name, os.fdopen(msvcrt.open_osfhandle(h, 0), "w", buffering=1))
    except Exception:
        pass


HERE = os.path.dirname(os.path.abspath(__file__))
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(HERE, "..", "server"))
RES = getattr(sys, "_MEIPASS", HERE)

# The packaged app also runs Stella's Python scripts (the code runner, see stellai_hub.tool_run_python): it has
# numpy and matplotlib inside, and Windows PCs usually have no Python of their own.
if len(sys.argv) == 3 and sys.argv[1] == "--python-runner":
    import runpy
    sys.argv = [sys.argv[2]]
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)

# The packaged app doubles as the sandboxed schemdraw renderer (see stellai_hub.render_schemdraw).
if len(sys.argv) == 4 and sys.argv[1] == "--schemdraw-runner":
    _reattach_console()
    import traceback
    import stellai_hub
    try:
        stellai_hub.run_schemdraw_runner(sys.argv[2], sys.argv[3])
    except SystemExit:
        raise
    except BaseException as e:
        traceback.print_exc()
        sys.exit(f"Error: {type(e).__name__}: {e}")
    sys.exit(0)

import queue
import socket
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import messagebox, ttk

import stellai_hub
import winsetup as ws

BG, PANEL, LINE, TEXT, MUTED = "#070B1A", "#0E1530", "#1E2A55", "#E6ECFF", "#8A96C0"
ACCENT, GREEN, AMBER, RED = "#5CE1FF", "#4ADE80", "#FBBF24", "#F87171"
SHOW_PORT = 48092   # a second launch asks the running app to show its window


class App:
    def __init__(self, minimized=False, after_update=False):
        self.cfg = ws.Config()
        os.makedirs(self.cfg.root, exist_ok=True)
        self.q = queue.Queue()
        self.servers = ws.Servers(self.cfg, stellai_hub, lambda m: self.q.put(("log", m)))
        self.hw = None
        self.cancel = threading.Event()
        self.installing = False
        self.tray = None
        self.qr_img = None
        self.update_state = "Checking for updates…" if ws.can_self_update() else "Updates: off when running from source"
        self.update_info = None       # newer release found on GitHub
        self.update_file = None       # its installer, downloaded and verified
        self.update_lock = threading.Lock()
        self.force_now = False

        self.root = tk.Tk()
        self.root.title("StellAI Server")
        self.root.configure(bg=BG)
        self.root.geometry("800x800")
        self.root.minsize(640, 560)
        try:
            self.root.iconbitmap(os.path.join(RES, "stellai.ico"))
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._style()

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=18, pady=14)
        self._header()
        self.page = tk.Frame(self.body, bg=BG)
        self.page.pack(fill="both", expand=True)

        threading.Thread(target=self._listen_for_show, daemon=True).start()
        self._start_tray()
        self.root.after(100, self._pump)

        self.servers.on_moved = lambda info: self.q.put(("call", lambda: (
            self.show_client(f"Stella moved to {info.get('name')}. This PC is a client now: phones and browsers "
                             "follow her there by themselves."), self._notify(f"Stella moved to {info.get('name')}"))))
        self.servers.on_arrived = lambda: self.q.put(("call", lambda: (
            self.show_dashboard(), self._notify("Stella moved to this PC"))))
        if self.cfg.get("client"):
            self.show_client()
            if self.cfg.get("moved") and ws.is_installed(self.cfg):
                # keep pointing old addresses at her new server (and ready to take her back)
                self.root.after(300, lambda: threading.Thread(target=self.servers.start_signpost, daemon=True).start())
        elif ws.is_installed(self.cfg):
            self.show_dashboard()
            if after_update:
                start = self.cfg.get("resume_servers", True)
            else:
                start = minimized or self.cfg.get("start_on_open", True)
            if start:
                self.root.after(300, self.start_servers)
        else:
            self.show_setup()
        if minimized and ws.is_installed(self.cfg):
            self.root.withdraw()
        if after_update:
            old = self.cfg.get("updated_from", "")
            self.log(f"Updated to version {ws.APP_VERSION}" + (f" (from {old})" if old else "") +
                     ". Your chats, models and settings were kept.")
            self.root.after(2500, lambda: self._notify(f"StellAI updated to {ws.APP_VERSION}"))
        if ws.can_self_update():
            threading.Thread(target=self._update_loop, daemon=True, name="updater").start()

    # ------------------------------------------------------------------ look
    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, bordercolor=LINE,
                    font=("Segoe UI", 10))
        s.configure("TButton", background=PANEL, foreground=TEXT, padding=(12, 6), borderwidth=1)
        s.map("TButton", background=[("active", LINE), ("disabled", BG)], foreground=[("disabled", MUTED)])
        s.configure("Accent.TButton", background=ACCENT, foreground=BG, font=("Segoe UI Semibold", 10))
        s.map("Accent.TButton", background=[("active", "#9BEEFF"), ("disabled", LINE)])
        s.configure("TRadiobutton", background=PANEL, foreground=TEXT, indicatorcolor=BG)
        s.map("TRadiobutton", background=[("active", PANEL)], indicatorcolor=[("selected", ACCENT)])
        s.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorcolor=PANEL)
        s.map("TCheckbutton", background=[("active", BG)], indicatorcolor=[("selected", ACCENT)])
        s.configure("Panel.TCheckbutton", background=PANEL)
        s.map("Panel.TCheckbutton", background=[("active", PANEL)])
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, bordercolor=LINE)
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT, arrowcolor=ACCENT)

    def _header(self):
        h = tk.Frame(self.body, bg=BG)
        h.pack(fill="x", pady=(0, 10))
        mark = tk.Canvas(h, width=26, height=26, bg=BG, highlightthickness=0)   # Stella's star, drawn as a line icon
        mark.create_polygon(13, 2, 15, 11, 24, 13, 15, 15, 13, 24, 11, 15, 2, 13, 11, 11, outline=ACCENT, fill="",
                            width=1.6, joinstyle="round", smooth=False)
        mark.pack(side="left", padx=(0, 6))
        tk.Label(h, text="STELLAI", bg=BG, fg=ACCENT, font=("Segoe UI Semibold", 18)).pack(side="left")
        tk.Label(h, text="  server for Windows · preview " + ws.APP_VERSION, bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side="left", pady=(6, 0))

    def panel(self, parent, title=None):
        f = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        f.pack(fill="x", pady=6)
        if title:
            tk.Label(f, text=title.upper(), bg=PANEL, fg=ACCENT, font=("Consolas", 9, "bold")).pack(
                anchor="w", padx=12, pady=(8, 2))
        inner = tk.Frame(f, bg=PANEL)
        inner.pack(fill="x", padx=12, pady=(2, 10))
        return inner

    def label(self, parent, text, fg=TEXT, size=10, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=fg, justify="left", anchor="w",
                        font=("Segoe UI Semibold" if bold else "Segoe UI", size), **kw)

    def clear(self):
        for w in self.page.winfo_children():
            w.destroy()

    # ------------------------------------------------------------------ setup page
    def _scroll_page(self):
        """The setup page scrolls: it has more steps than fit the window."""
        canvas = tk.Canvas(self.page, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(self.page, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.winfo_exists() and canvas.yview_scroll(-e.delta // 120, "units"))
        return inner

    def show_setup(self):
        self.clear()
        self.summary_label = None
        p = self.setup_page = self._scroll_page()
        hwp = self.panel(p, "This computer")
        self.hw_label = self.label(hwp, "Checking your hardware…", fg=MUTED)
        self.hw_label.pack(anchor="w")
        self.model_panel = self.panel(p, "Choose Stella's brain")
        self.label(self.model_panel, "Detecting…", fg=MUTED).pack(anchor="w")
        threading.Thread(target=self._detect, daemon=True).start()

    def _detect(self):
        try:
            hw = ws.detect()
        except Exception as e:
            hw = {"ram_gb": 8, "cpu": f"(detection failed: {e})", "cores": 2, "threads": 4, "gpus": [],
                  "backend": "cpu", "gpu": "", "vram_gb": 0, "cuda": ""}
        self.q.put(("call", lambda: self._setup_ready(hw)))

    def _setup_ready(self, hw):
        self.hw = hw
        if not self.hw_label.winfo_exists():
            return   # the user left the setup page meanwhile
        how = {"cuda": f"NVIDIA CUDA {hw['cuda']}.x", "vulkan": "Vulkan GPU", "cpu": "CPU only"}[hw["backend"]]
        gpu = f"{hw['gpu']} ({hw['vram_gb']:g} GB)" if hw["gpu"] else (", ".join(hw["gpus"]) or "none")
        if hw["backend"] == "cpu" and hw["gpus"]:
            gpu += "  — not enough video memory to help, using the CPU"
        cards = hw.get("gpu_list") or []
        if len(cards) > 1:
            gpu = "\n            ".join(f"card {g['index']}: {g['name']} ({g['vram_gb']:g} GB)" for g in cards)
        self.hw_label.config(fg=TEXT, text=(
            f"Processor:  {hw['cpu']}  ({hw['cores']} cores{'' if hw.get('avx2', True) else ', no AVX2'})\n"
            f"Memory:     {hw['ram_gb']} GB\n"
            f"Graphics:   {gpu}\n"
            f"Disk free:  {hw.get('disk_free_gb', 0):g} GB\n"
            f"Will run on: {how}"))

        # What setup will install, matched to this PC (GPU, driver, runtimes)
        pk = self.panel(self.setup_page, "Will install")
        items, tips = ws.packages(hw)
        for name, detail in items:
            self.label(pk, f"{name}:  {detail}", size=9, wraplength=680).pack(anchor="w")
        for t in tips:
            self.label(pk, "Tip: " + t if not t.startswith("Tip") else t, fg=AMBER, size=9, wraplength=680).pack(anchor="w", pady=(4, 0))
        pk.master.pack_configure(before=self.model_panel.master)

        # What this PC is: the server, a client of another server, or both
        cp = self.panel(self.setup_page, "What should this PC be?")
        self.role_var = tk.StringVar(value=self.cfg.get("role") or ("client" if ws.is_weak(hw) else "both"))
        for val, text in (("both", "Server and client: Stella runs here, and you chat with her here too"),
                          ("server", "Server only: Stella runs here for your phones and computers (starts with "
                                     "Windows, in the tray)"),
                          ("client", "Client only: chat with Stella on another computer (nothing big installed here)")):
            ttk.Radiobutton(cp, text=text, variable=self.role_var, value=val).pack(anchor="w")
        if ws.is_weak(hw):
            self.label(cp, "This PC is slow for running Stella itself. As a client of a stronger StellAI server (e.g. "
                           "a desktop with a graphics card) it gets the same chats and much faster answers.",
                       fg=AMBER, size=9, wraplength=680).pack(anchor="w", pady=(4, 0))
        cp.master.pack_configure(before=self.model_panel.master)

        for w in self.model_panel.winfo_children():
            w.destroy()
        prev = self.cfg.get("plan") or {}
        mp = self.model_panel
        if not ws.tune:      # the catalog file is missing: the three models StellAI always knew
            rec = ws.recommend(hw)
            self.model_var = tk.StringVar(value=prev.get("model_key") if prev.get("model_key") in ws.MODELS else rec)
            for key, m in ws.MODELS.items():
                pl = ws.plan(hw, key)
                ttk.Radiobutton(mp, text=m["label"] + ("   (recommended)" if key == rec else ""),
                                variable=self.model_var, value=key).pack(anchor="w")
                self.label(mp, f"     {m['about']}  Download ≈ {pl['model_gb']:g} GB.", fg=MUTED, size=9).pack(anchor="w")
        else:
            self._tuned_setup(hw, prev)

        opts = self.panel(self.setup_page, "Extras")
        self.vision_var = tk.BooleanVar(value=prev.get("vision", hw["ram_gb"] >= 12))
        ttk.Checkbutton(opts, text="Vision: let Stella see photos you send (≈ 0.7–0.9 GB more)",
                        variable=self.vision_var, style="Panel.TCheckbutton").pack(anchor="w")
        row = tk.Frame(opts, bg=PANEL)
        row.pack(anchor="w", pady=(6, 0))
        self.label(row, "Voice recognition:").pack(side="left")
        auto = "small" if hw["ram_gb"] >= 14 and hw["cores"] >= 6 else "base"
        self.whisper_var = tk.StringVar(value=prev.get("whisper_file", f"ggml-{auto}.en.bin")
                                        .replace("ggml-", "").replace(".en.bin", ""))
        ttk.Combobox(row, textvariable=self.whisper_var, values=["base", "small"], width=8,
                     state="readonly").pack(side="left", padx=8)
        self.label(row, "base ≈ 150 MB, faster · small ≈ 470 MB, more accurate", fg=MUTED, size=9).pack(side="left")
        self.mtp_var = tk.BooleanVar(value=True)
        self.test_var = tk.BooleanVar(value=True)
        if ws.tune:
            ttk.Checkbutton(opts, text="Speed-up test: get the Multi-Token Prediction version where there is one, and keep "
                                       "the speed-up only if it's faster here", variable=self.mtp_var,
                            style="Panel.TCheckbutton").pack(anchor="w", pady=(6, 0))
            ttk.Checkbutton(opts, text="Test run after installing: measure the speed and check the conversation length "
                                       "fits (a few minutes)", variable=self.test_var,
                            style="Panel.TCheckbutton").pack(anchor="w")

        self.summary_label = self.label(self.setup_page, "", fg=MUTED, size=9, wraplength=700)
        self.summary_label.pack(anchor="w", pady=(6, 0))
        self._update_summary()

        act = tk.Frame(self.setup_page, bg=BG)
        act.pack(fill="x", pady=(10, 4))
        self.install_btn = ttk.Button(act, text="Install", style="Accent.TButton", command=self.do_install)
        self.install_btn.pack(side="left")
        if ws.is_installed(self.cfg):
            ttk.Button(act, text="Back", command=self.show_dashboard).pack(side="left", padx=8)
        self.cancel_btn = ttk.Button(act, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.prog = ttk.Progressbar(self.setup_page, mode="determinate", maximum=1.0)
        self.prog.pack(fill="x", pady=(8, 2))
        self.prog_label = self.label(self.setup_page, "Files go to " + self.cfg.root, fg=MUTED, size=9)
        self.prog_label.pack(anchor="w")

    # ---- the catalog screens: cards, memory, model, context, helper ----------------------------------------------
    def _tuned_setup(self, hw, prev):
        mp = self.model_panel
        cards = hw.get("gpu_list") or []
        self.gpu_vars = {}
        if hw["backend"] == "cuda" and len(cards) >= 2:
            self.label(mp, "Graphics cards Stella's model may use (cards left out stay free for the helper and 3D jobs):",
                       size=9).pack(anchor="w")
            chosen = prev.get("llama_gpus")
            for g in cards:
                on = not chosen or chosen != "-1" and str(g["index"]) in chosen.split(",")
                v = tk.BooleanVar(value=on)
                ttk.Checkbutton(mp, text=f"card {g['index']}: {g['name']} ({g['vram_gb']:g} GB)", variable=v,
                                style="Panel.TCheckbutton", command=self._refresh_models).pack(anchor="w")
                self.gpu_vars[g["index"]] = v
        row = tk.Frame(mp, bg=PANEL)
        row.pack(anchor="w", pady=(6, 4))
        self.label(row, "Memory StellAI may use:").pack(side="left")
        self.budgets = ws.ram_budgets(hw["ram_gb"])
        self.budget_var = tk.StringVar(value=next((l for l, gb in self.budgets if gb == prev.get("ram_budget_gb")),
                                                  self.budgets[0][0]))
        cb = ttk.Combobox(row, textvariable=self.budget_var, values=[l for l, _ in self.budgets], width=36,
                          state="readonly")
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_models())
        self.show_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(mp, text="Show every model and size that fits", variable=self.show_all,
                        style="Panel.TCheckbutton", command=self._refresh_models).pack(anchor="w")
        self.model_list = tk.Frame(mp, bg=PANEL)
        self.model_list.pack(fill="x", pady=(4, 0))
        self.model_var = tk.StringVar(value="")
        self.model_var.trace_add("write", lambda *a: self._model_changed())
        crow = tk.Frame(mp, bg=PANEL)
        crow.pack(anchor="w", pady=(8, 0))
        self.label(crow, "Conversation length:").pack(side="left")
        self.ctx_var = tk.StringVar(value="")
        self.ctx_box = ttk.Combobox(crow, textvariable=self.ctx_var, width=18, state="readonly")
        self.ctx_box.pack(side="left", padx=8)
        self.ctx_box.bind("<<ComboboxSelected>>", lambda e: self._update_summary())
        hrow = tk.Frame(mp, bg=PANEL)
        hrow.pack(anchor="w", pady=(6, 0))
        self.label(hrow, "Helper model:").pack(side="left")
        self.helper_var = tk.StringVar(value="")
        self.helper_box = ttk.Combobox(hrow, textvariable=self.helper_var, width=46, state="readonly")
        self.helper_box.pack(side="left", padx=8)
        self.helper_box.bind("<<ComboboxSelected>>", lambda e: self._update_summary())
        self.label(mp, "The helper is a small second model for quick background jobs (learning memories, checking which "
                       "mode a message needs), so they don't wait behind Stella. Speeds shown are estimates; the test "
                       "run measures them.", fg=MUTED, size=9, wraplength=680).pack(anchor="w", pady=(2, 0))
        crow2 = tk.Frame(mp, bg=PANEL)
        crow2.pack(anchor="w", pady=(6, 0))
        self.label(crow2, "Or your own model (.gguf link from Hugging Face):", size=9).pack(side="left")
        self.custom_var = tk.StringVar(value="")
        tk.Entry(crow2, textvariable=self.custom_var, width=40, bg=BG, fg=TEXT, insertbackground=TEXT,
                 relief="flat").pack(side="left", padx=6)
        self.custom_var.trace_add("write", lambda *a: self._update_summary())
        self._refresh_models(prev.get("model_key"))

    def _gpu_ids(self):
        if not getattr(self, "gpu_vars", None):
            return None
        return [i for i, v in self.gpu_vars.items() if v.get()]

    def _budget(self):
        return next((gb for l, gb in self.budgets if l == self.budget_var.get()), self.budgets[0][1])

    def _refresh_models(self, want=None):
        hw = self.hw
        res = ws.tune.rank(ws.tune_hw(hw), self._budget(), self._gpu_ids())
        rows = res["all"] if self.show_all.get() else res["picks"]
        self.model_rows = {f"{o['key']}|{o['quant']}": o for o in res["all"] + res["picks"]}
        for w in self.model_list.winfo_children():
            w.destroy()
        names = {"best": "Best overall", "fastest": "Fastest", "coder": "Best coder", "smartest": "Smartest"}
        if not rows:
            self.label(self.model_list, "Nothing fits this much memory; give StellAI more, or use another server.",
                       fg=AMBER, size=9).pack(anchor="w")
        for o in rows:
            mid = f"{o['key']}|{o['quant']}"
            head = names.get(o.get("pick"), "")
            ttk.Radiobutton(self.model_list, text=(head + ": " if head else "") + f"{o['label']} {o['quant']}",
                            variable=self.model_var, value=mid).pack(anchor="w")
            self.label(self.model_list, f"     {o['size_gb']:g} GB · about {o['tok_s']:g} tokens/s ({o['speed']}, "
                                        f"{o['where']}) · {', '.join(o['strengths'])}"
                       + (f" · {o['why']}" if o.get("why") else ""), fg=MUTED, size=9, wraplength=660).pack(anchor="w")
        cur = want if want in self.model_rows else (self.model_var.get() if self.model_var.get() in self.model_rows
                                                    else (f"{rows[0]['key']}|{rows[0]['quant']}" if rows else ""))
        self.model_var.set(cur)
        self._model_changed()

    def _model_changed(self):
        o = getattr(self, "model_rows", {}).get(self.model_var.get())
        if not o or not hasattr(self, "ctx_box"):
            return
        rec = o["context"]
        vals = [f"{rec // 1024}K (recommended)"] + [f"{c // 1024}K" for c in (8192, 16384, 32768, 65536, 131072)
                                                     if c != rec]
        self.ctx_box.config(values=vals)
        self.ctx_var.set(vals[0])
        h = ws.tune.pick_helper(ws.tune_hw(self.hw), self._budget(), o["size_gb"])
        self.helper_opts = {}
        if h:
            self.helper_opts[f"{h['label']} on the processor (recommended)"] = dict(h, gpu=None)
            if h["key"] == "qwen3.5-2b":
                self.helper_opts["Qwen3.5 0.8B on the processor (lighter)"] = {"key": "qwen3.5-0.8b", "quant": "Q8_0",
                                                                              "label": "Qwen3.5 0.8B", "gpu": None}
            ids = self._gpu_ids()
            for g in self.hw.get("gpu_list") or []:
                if ids is not None and g["index"] not in ids:
                    self.helper_opts[f"{h['label']} on card {g['index']} (not used by Stella)"] = dict(h, gpu=g["index"])
        self.helper_opts["No helper"] = None
        self.helper_box.config(values=list(self.helper_opts))
        self.helper_var.set(next(iter(self.helper_opts)))
        self._update_summary()

    def _context(self):
        v = self.ctx_var.get().split("K")[0] if getattr(self, "ctx_var", None) else ""
        return int(v) * 1024 if v.isdigit() else None

    def _selected_plan(self):
        whisper, vision = self.whisper_var.get(), self.vision_var.get()
        if not ws.tune:
            return ws.plan(self.hw, self.model_var.get(), vision, whisper)
        url = self.custom_var.get().strip()
        if url:
            if not (url.startswith("https://huggingface.co/") and url.split("?")[0].endswith(".gguf")):
                raise ValueError("Your own model needs a Hugging Face link ending in .gguf")
            choice = {"custom_url": url}
        else:
            choice = self.model_rows.get(self.model_var.get())
            if not choice:
                raise ValueError("Pick a model first")
        return ws.plan_tuned(self.hw, choice, vision, whisper, self._budget(), self._gpu_ids(),
                             self.helper_opts.get(self.helper_var.get()), self.mtp_var.get(), self._context())

    def _update_summary(self):
        if not getattr(self, "summary_label", None):
            return
        try:
            pl = self._selected_plan()
        except (ValueError, KeyError, AttributeError):
            return
        need = pl["model_gb"] + (0.9 if pl["vision"] else 0) + (2 if pl.get("helper") else 0) + 1.2
        self.summary_label.config(text=(
            f"Will install {pl['label']}, about {need:.0f} GB in all ({self.hw.get('disk_free_gb', 0):g} GB free)"
            + (f", conversation {pl['context'] // 1024}K" if pl.get("context") else "")
            + (f", helper {pl['helper']['label']}" if pl.get("helper") else "") + "."))

    def do_install(self):
        if self.installing:
            return
        if getattr(self, "role_var", None) and self.role_var.get() == "client":
            self.ask_client()
            return
        try:
            pl = self._selected_plan()
        except ValueError as e:
            messagebox.showinfo("StellAI", str(e))
            return
        self.test_after = bool(ws.tune and self.test_var.get())
        need = pl["model_gb"] + (0.9 if pl["vision"] else 0) + 1.2
        try:
            import shutil
            free = shutil.disk_usage(self.cfg.root).free / 2**30
            if free < need:
                if not messagebox.askyesno("StellAI", f"This needs about {need:.0f} GB but only {free:.0f} GB is free. "
                                                     "Try anyway?"):
                    return
        except OSError:
            pass
        restart = bool(self.servers.wanted)
        if restart:
            self.servers.stop()
        self.installing = True
        self.cancel.clear()
        self.install_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.cfg.set(hardware=self.hw)

        def status(t):
            self.q.put(("call", lambda: self.prog_label.config(text=t, fg=TEXT)))

        def progress(f):
            def upd():
                if f is None:
                    self.prog.config(mode="indeterminate")
                    self.prog.step(0.02)
                else:
                    self.prog.config(mode="determinate", value=f)
            self.q.put(("call", upd))

        def work():
            try:
                ws.install(self.cfg, pl, status, progress, self.cancel)
                self.q.put(("call", self._install_done))
            except ws.Cancelled:
                self.q.put(("call", lambda: self._install_failed("Cancelled. Downloads resume where they stopped.")))
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self.q.put(("call", lambda: self._install_failed(msg)))

        threading.Thread(target=work, daemon=True).start()

    def _install_failed(self, msg):
        self.installing = False
        self.install_btn.config(state="normal", text="Try again")
        self.cancel_btn.config(state="disabled")
        self.prog_label.config(text=msg, fg=RED)

    def _install_done(self):
        self.installing = False
        role = self.role_var.get() if getattr(self, "role_var", None) else "both"
        self.cfg.set(role=role)
        if role == "server":           # a server: starts with Windows, quietly in the tray
            try:
                ws.autostart(True)
            except OSError:
                pass
        if getattr(self, "test_after", False):
            self.test_after = False
            self.prog_label.config(text="Test run: measuring Stella's model on this PC (a few minutes)…", fg=TEXT)
            self.prog.config(mode="indeterminate")
            self.prog.start(15)

            def work():
                try:
                    text = ws.tune_now(self.cfg, lambda t: self.q.put(("log", t)))
                except Exception as e:
                    text = f"The test didn't finish ({e}); StellAI uses the planned settings."
                self.q.put(("log", text))

                def done():
                    self.show_dashboard()
                    self.start_servers()
                    messagebox.showinfo("StellAI test run", text)
                self.q.put(("call", done))
            threading.Thread(target=work, daemon=True).start()
            return
        self.show_dashboard()
        self.start_servers()

    # ------------------------------------------------------------------ client mode
    def ask_client(self):
        d = tk.Toplevel(self.root, bg=PANEL)
        d.title("Use another StellAI server")
        d.transient(self.root)
        found_row = tk.Frame(d, bg=PANEL)
        found_row.pack(anchor="w", padx=14, pady=(14, 0))
        found_lbl = self.label(found_row, "Looking for StellAI servers on your Tailscale devices…", fg=MUTED, size=9)
        found_lbl.pack(anchor="w")
        self.label(d, "Server address (its Tailscale address works at home and away):").pack(anchor="w", padx=14, pady=(10, 2))
        srv = tk.Entry(d, width=40, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat")
        srv.pack(anchor="w", padx=14)

        def show_found(found):
            if not found_lbl.winfo_exists():
                return
            if not found:
                found_lbl.config(text="No StellAI server found on Tailscale; type its address below.")
                return
            found_lbl.config(text="Found on your Tailscale devices (click to use):", fg=TEXT)
            for ip, name in found:
                ttk.Button(found_row, text=f"{name}  ({ip})",
                           command=lambda ip=ip: (srv.delete(0, "end"), srv.insert(0, ip))).pack(anchor="w", pady=1)
        threading.Thread(target=lambda: self.q.put(("call", lambda f=ws.find_servers(): show_found(f))),
                         daemon=True).start()
        self.label(d, "API key (shown by StellAI Server on that computer):").pack(anchor="w", padx=14, pady=(10, 2))
        key = tk.Entry(d, width=40, show="•", bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat")
        key.pack(anchor="w", padx=14)
        msg = self.label(d, "", fg=MUTED, size=9)
        msg.pack(anchor="w", padx=14, pady=6)

        def save():
            server = srv.get().strip().replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]
            if not server or not key.get().strip():
                msg.config(text="Enter both.", fg=RED)
                return
            msg.config(text="Checking…", fg=MUTED)
            d.update()
            ok = ws.check_server(server)
            if self.servers.wanted:
                self.servers.stop()
            self.cfg.set(client={"server": server, "key": key.get().strip()})
            d.destroy()
            self.show_client(None if ok else f"Couldn't reach {server} right now; make sure StellAI Server is running there.")
        ttk.Button(d, text="Use this server", style="Accent.TButton", command=save).pack(anchor="e", padx=14, pady=(0, 14))

    def show_client(self, warning=None):
        self.clear()
        c = self.cfg.get("client") or {}
        p = self.panel(self.page, "Using another server")
        self.label(p, f"This PC uses Stella on {c.get('server')}. Chats, projects and reminders are the same on every "
                      "device.", wraplength=680).pack(anchor="w")
        if warning:
            self.label(p, warning, fg=AMBER, size=9, wraplength=680).pack(anchor="w", pady=(6, 0))
        row = tk.Frame(p, bg=PANEL)
        row.pack(anchor="w", pady=(10, 0))
        ttk.Button(row, text="Open chat", style="Accent.TButton",
                   command=lambda: webbrowser.open(ws.client_url(self.cfg))).pack(side="left")

        def run_here():
            if self.cfg.get("moved"):      # Stella moved away earlier: stop the signpost, run her here again
                self.servers.stop()
                try:
                    os.remove(self.cfg.p("moved.json"))
                except OSError:
                    pass
            self.cfg.set(client=None, moved=None)
            self.show_dashboard() if ws.is_installed(self.cfg) else self.show_setup()
        ttk.Button(row, text="Run Stella on this PC instead", command=run_here).pack(side="left", padx=8)

    # ------------------------------------------------------------------ dashboard
    def show_dashboard(self):
        self.clear()
        pl = self.cfg.get("plan")
        top = self.panel(self.page, "Server")
        row = tk.Frame(top, bg=PANEL)
        row.pack(fill="x")
        self.status_dots = {}
        for name, label in (("llama", f"Stella ({pl['label']})"), ("whisper", "Voice"), ("hub", "Sync & tools")):
            f = tk.Frame(row, bg=PANEL)
            f.pack(side="left", padx=(0, 22))
            dot = tk.Label(f, text="●", bg=PANEL, fg=MUTED, font=("Segoe UI", 12))
            dot.pack(side="left")
            txt = self.label(f, f"{label}: stopped", size=9)
            txt.pack(side="left", padx=4)
            self.status_dots[name] = (dot, txt, label)
        btns = tk.Frame(top, bg=PANEL)
        btns.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(btns, text="Start", style="Accent.TButton", command=self.toggle_servers)
        self.start_btn.pack(side="left")
        if self.cfg.get("role") != "server":
            ttk.Button(btns, text="Open chat in browser",
                       command=lambda: webbrowser.open(ws.web_chat_url(self.cfg))).pack(side="left", padx=8)
            ttk.Button(btns, text="Avatar screen", command=lambda: ws.open_avatar(self.cfg)).pack(side="left")
        ttk.Button(btns, text="Avatar files", command=self.open_avatar_bin).pack(side="left", padx=(8, 0))
        backend = {"cuda": "NVIDIA CUDA", "vulkan": "Vulkan GPU", "cpu": "CPU"}.get(pl["backend"], pl["backend"])
        self.label(btns, f"running on {backend}", fg=MUTED, size=9).pack(side="left", padx=6)

        conn = self.panel(self.page, "Connect your phone")
        grid = tk.Frame(conn, bg=PANEL)
        grid.pack(fill="x")
        left = tk.Frame(grid, bg=PANEL)
        left.pack(side="left", fill="both", expand=True)
        self.qr_label = tk.Label(grid, bg="white")
        self.qr_label.pack(side="right", padx=(10, 0))

        self.label(left, "In the StellAI app: Settings → Server. Or scan this code with your phone's camera.",
                   fg=MUTED, size=9, wraplength=380).pack(anchor="w")
        self.addr_var = tk.StringVar()
        self.addr_box = ttk.Combobox(left, textvariable=self.addr_var, state="readonly", width=34)
        self.addr_box.pack(anchor="w", pady=(8, 2))
        self.addr_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_qr())
        self.addr_note = self.label(left, "", fg=MUTED, size=9, wraplength=380)
        self.addr_note.pack(anchor="w")

        key = ws.api_key(self.cfg)
        krow = tk.Frame(left, bg=PANEL)
        krow.pack(anchor="w", pady=(8, 0))
        self.label(krow, "API key:").pack(side="left")
        self.key_var = tk.StringVar(value="•" * 12)
        tk.Entry(krow, textvariable=self.key_var, state="readonly", readonlybackground=BG, fg=TEXT, width=22,
                 relief="flat", font=("Consolas", 10)).pack(side="left", padx=6)
        self.show_key = False

        def toggle():
            self.show_key = not self.show_key
            self.key_var.set(key if self.show_key else "•" * 12)
        ttk.Button(krow, text="Show", command=toggle).pack(side="left")
        ttk.Button(krow, text="Copy", command=lambda: self.copy(key)).pack(side="left", padx=4)
        trow = tk.Frame(left, bg=PANEL)
        trow.pack(anchor="w", pady=(10, 0))
        ttk.Button(trow, text="Get Tailscale (use away from home)",
                   command=lambda: webbrowser.open("https://tailscale.com/download/windows")).pack(side="left")
        self.refresh_addresses()

        opts = self.panel(self.page, "Options")
        self.auto_var = tk.BooleanVar(value=ws.autostart())
        ttk.Checkbutton(opts, text="Start with Windows (in the tray)", variable=self.auto_var, style="Panel.TCheckbutton",
                        command=lambda: ws.autostart(self.auto_var.get())).pack(anchor="w")
        self.open_var = tk.BooleanVar(value=self.cfg.get("start_on_open", True))
        ttk.Checkbutton(opts, text="Start the server when this app opens", variable=self.open_var,
                        style="Panel.TCheckbutton",
                        command=lambda: self.cfg.set(start_on_open=self.open_var.get())).pack(anchor="w")
        orow = tk.Frame(opts, bg=PANEL)
        orow.pack(anchor="w", pady=(8, 0))
        ttk.Button(orow, text="Change model", command=self.show_setup).pack(side="left")
        ttk.Button(orow, text="Open data folder", command=lambda: os.startfile(self.cfg.root)
                   if os.name == "nt" else None).pack(side="left", padx=6)
        ttk.Button(orow, text="Wake words folder", command=lambda: os.startfile(self.cfg.p("wakewords"))
                   if os.name == "nt" else None).pack(side="left")
        crow = tk.Frame(opts, bg=PANEL)
        crow.pack(anchor="w", pady=(8, 0))
        ttk.Button(crow, text="Check everything", command=self.run_doctor).pack(side="left")
        ttk.Button(crow, text="Back up now", command=self.backup_now).pack(side="left", padx=6)
        ttk.Button(crow, text="Restore a backup…", command=self.restore_backup).pack(side="left")

        ttk.Button(crow, text="Speed test", command=self.speed_test).pack(side="left", padx=6)
        if ws.tune:
            ttk.Button(crow, text="Test & tune", command=self.tune_model).pack(side="left")

        srow = tk.Frame(opts, bg=PANEL)
        srow.pack(fill="x", pady=(10, 0))
        ttk.Button(srow, text="New API key…", command=self.new_key).pack(side="left")
        self.fw_btn = ttk.Button(srow, text="Limit firewall", command=self.limit_firewall)
        self.fw_label = self.label(srow, "", fg=MUTED, size=9)
        self.fw_label.pack(side="left", padx=8)
        self._show_firewall_state()

        wrow = tk.Frame(opts, bg=PANEL)
        wrow.pack(fill="x", pady=(10, 0))
        self.searx_label = self.label(wrow, "", fg=MUTED, size=9)
        self.searx_label.pack(side="left")
        self.searx_btn = ttk.Button(wrow, text="Set up private web search", command=self.setup_search)
        self.searx_btn.pack(side="right")
        self._show_search_state()

        urow = tk.Frame(opts, bg=PANEL)
        urow.pack(fill="x", pady=(10, 0))
        self.update_label = self.label(urow, "", fg=MUTED, size=9)
        self.update_label.pack(side="left")
        self.update_btn = ttk.Button(urow, text="Check now", command=self.update_button)
        self.update_btn.pack(side="right")
        self.autoupd_var = tk.BooleanVar(value=self.cfg.get("auto_update", True))
        ttk.Checkbutton(opts, text="Install updates automatically (checks GitHub every 30 minutes, "
                                   "installs when Stella is idle)", variable=self.autoupd_var,
                        style="Panel.TCheckbutton",
                        command=lambda: self.cfg.set(auto_update=self.autoupd_var.get())).pack(anchor="w", pady=(4, 0))
        self._show_update_state()

        logp = self.panel(self.page, "Activity")
        self.log_box = tk.Text(logp, height=8, bg=BG, fg=MUTED, relief="flat", font=("Consolas", 9),
                               insertbackground=TEXT, wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.config(state="disabled")
        if getattr(self, "_tick_id", None):
            self.root.after_cancel(self._tick_id)
        self._tick()

    def refresh_addresses(self):
        ts, lan = ws.tailscale_ip(), ws.lan_ip()
        opts = []
        if ts:
            opts.append(f"http://{ts}:{ws.LLAMA_PORT}   (Tailscale: home and away)")
        opts.append(f"http://{lan}:{ws.LLAMA_PORT}   (home Wi-Fi only)")
        self.addr_box.config(values=opts)
        if self.addr_var.get() not in opts:
            self.addr_var.set(opts[0])
        self.addr_note.config(text="" if ts else
                              "Install Tailscale on this PC and your phone to use Stella away from home.")
        self.refresh_qr()

    def current_url(self):
        return self.addr_var.get().split()[0] if self.addr_var.get() else ""

    def refresh_qr(self):
        try:
            import qrcode
            from PIL import ImageTk
            img = qrcode.make(ws.pair_link(self.current_url(), ws.api_key(self.cfg)), border=2, box_size=6)
            img = img.convert("RGB").resize((200, 200))
            self.qr_img = ImageTk.PhotoImage(img)
            self.qr_label.config(image=self.qr_img, width=200, height=200)
        except Exception as e:
            self.qr_label.config(text=f"QR unavailable\n{e}", image="", fg=BG)

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log("copied to the clipboard")

    # ------------------------------------------------------------------ servers
    def start_servers(self):
        if not ws.is_installed(self.cfg):
            return
        try:
            self.servers.start()
        except OSError as e:
            self.servers.stop()
            messagebox.showerror("StellAI", f"Couldn't start the server: {e}\n\nIs another copy already running?")
        self._update_buttons()

    def toggle_servers(self):
        if self.servers.wanted:
            threading.Thread(target=lambda: (self.servers.stop(), self.q.put(("call", self._update_buttons))),
                             daemon=True).start()
            self.start_btn.config(state="disabled")
        else:
            self.start_servers()

    def _update_buttons(self):
        if hasattr(self, "start_btn") and self.start_btn.winfo_exists():
            self.start_btn.config(state="normal", text="Stop" if self.servers.wanted else "Start")
        self._update_tray()

    def _tick(self):
        if not (hasattr(self, "log_box") and self.log_box.winfo_exists()):
            return
        st = self.servers.state()
        colors = {"running": GREEN, "starting": AMBER, "stopped": MUTED, "crashed": RED}
        words = {"running": "ready", "starting": "loading…", "stopped": "stopped", "crashed": "stopped with an error"}
        for name, (dot, txt, label) in self.status_dots.items():
            s = st.get(name, "stopped")
            dot.config(fg=colors[s])
            txt.config(text=f"{label}: {words[s]}")
            if s == "crashed" and not getattr(self, f"_reported_{name}", False):
                setattr(self, f"_reported_{name}", True)
                self.log(f"{label} stopped. Last lines of its log:\n" + self.servers.log_tail(name))
                if name == "llama" and ws.cuda_build_failed(self.servers.log_tail("llama", 80)):
                    self._recover_cuda()
            elif s != "crashed":
                setattr(self, f"_reported_{name}", False)
        self._tick_id = self.root.after(2000, self._tick)

    def open_avatar_bin(self):
        """The shared folder for avatar characters (.vrm) and animations (.vrma/.fbx)."""
        folder = self.cfg.p("avatar", "bin")
        for kind in ("characters", "animations"):
            os.makedirs(os.path.join(folder, kind), exist_ok=True)
        os.startfile(folder) if hasattr(os, "startfile") else webbrowser.open("file://" + folder)

    def _recover_cuda(self):
        """The CUDA build doesn't work with this NVIDIA driver: install an older build (or Vulkan) and retry."""
        if getattr(self, "_recovering", False):
            return
        self._recovering = True
        self.log("The GPU driver can't run this CUDA build of the AI engine. Downloading one that fits…")

        def work():
            try:
                msg = ws.recover_from_cuda_failure(self.cfg, status=lambda t: None)
                self.q.put(("log", msg))
                self.servers.restart_llama()
            except Exception as e:
                self.q.put(("log", f"Couldn't switch builds automatically ({type(e).__name__}: {e}). "
                                   "Update the NVIDIA driver, or use Change model → re-detect."))
            finally:
                self._recovering = False
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ health check and backups
    def run_doctor(self):
        if not self.servers.web:
            messagebox.showinfo("StellAI", "Start the server first; the check looks at the running parts.")
            return
        self.log("Running the health check…")

        def work():
            text = stellai_hub.doctor_text(stellai_hub.run_doctor())
            self.q.put(("log", text))
            self.q.put(("call", lambda: messagebox.showinfo("StellAI health check", text)))
        threading.Thread(target=work, daemon=True).start()

    def tune_model(self):
        if not messagebox.askyesno("StellAI", "Test & tune stops Stella for a few minutes, loads her model a few times to "
                                              "measure it, keeps the longest conversation that fits and the MTP speed-up "
                                              "only if it helps, then starts her again. Go ahead?"):
            return
        was = bool(self.servers.wanted)
        self.servers.stop()
        self.log("Test & tune: measuring Stella's model…")

        def work():
            try:
                text = ws.tune_now(self.cfg, lambda t: self.q.put(("log", t)))
            except Exception as e:
                text = f"The test didn't finish: {e}"
            self.q.put(("log", text))

            def done():
                if was:
                    self.start_servers()
                messagebox.showinfo("StellAI test & tune", text)
            self.q.put(("call", done))
        threading.Thread(target=work, daemon=True).start()

    def speed_test(self):
        if not self.servers.web:
            messagebox.showinfo("StellAI", "Start the server first; the test measures the running parts.")
            return
        self.log("Running the speed test (up to a minute)…")

        def work():
            pl = self.cfg.get("plan") or {}
            whisper = f"http://127.0.0.1:{ws.WHISPER_PORT}" if ws.whisper_args(self.cfg, pl) else ""
            text = stellai_hub.speed_text(stellai_hub.run_speed_test(whisper, "en"))
            self.q.put(("log", text))
            self.q.put(("call", lambda: messagebox.showinfo("StellAI speed test", text)))
        threading.Thread(target=work, daemon=True).start()

    def new_key(self):
        if not messagebox.askyesno("StellAI", "Replace the API key?\n\nEvery phone, tablet and browser will have to be "
                                   "paired again (scan the new QR code). Use this if a device was lost or someone "
                                   "may have seen the key."):
            return

        def work():
            was_running = self.servers.wanted
            self.servers.stop()
            ws.new_api_key(self.cfg)

            def done():
                self.show_dashboard()
                if was_running:
                    self.start_servers()
                self.log("New API key set: pair your devices again with the QR code.")
            self.q.put(("call", done))
        threading.Thread(target=work, daemon=True).start()

    def _show_firewall_state(self):
        def work():
            state = ws.firewall_state()
            text = {"limited": "Firewall: home network and Tailscale only ✓",
                    "open": "Firewall: any address on a private network may connect",
                    "none": "Firewall: no StellAI rule (phones may not connect)"}.get(state, "")

            def show():
                if not (hasattr(self, "fw_label") and self.fw_label.winfo_exists()):
                    return
                self.fw_label.config(text=text)
                if state in ("open", "none"):
                    self.fw_btn.pack(side="left", padx=(8, 0), before=self.fw_label)
                else:
                    self.fw_btn.pack_forget()
            self.q.put(("call", show))
        threading.Thread(target=work, daemon=True).start()

    def limit_firewall(self):
        if not ws.limit_firewall():
            messagebox.showinfo("StellAI", "The firewall rule wasn't changed (the administrator prompt was declined).")
            return
        self.root.after(3000, self._show_firewall_state)

    def backup_now(self):
        if not self.servers.web:
            messagebox.showinfo("StellAI", "Start the server first.")
            return

        def work():
            try:
                path = stellai_hub.backup_now()
                self.q.put(("log", f"Backup saved: {path}"))
            except Exception as e:
                self.q.put(("log", f"Backup failed: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def restore_backup(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title="Restore a StellAI backup", initialdir=self.cfg.p("backups"),
                                          filetypes=[("StellAI backups", "stellai-backup-*.zip"), ("Zip", "*.zip")])
        if not path:
            return
        if not messagebox.askyesno("StellAI", "Replace the current chats, memories and projects with this backup?\n\n"
                                               "The current chat database is kept as a copy."):
            return
        was_running = self.servers.wanted
        self.servers.stop()
        try:
            msg = stellai_hub.restore_backup(path, stellai_hub.parse_args(ws.hub_args(self.cfg)))
            self.log(msg)
        except Exception as e:
            messagebox.showerror("StellAI", f"Couldn't restore: {e}")
        if was_running:
            self.start_servers()

    # ------------------------------------------------------------------ web search
    def _show_search_state(self, text=None):
        if not (hasattr(self, "searx_label") and self.searx_label.winfo_exists()):
            return
        if text:
            self.searx_label.config(text=text)
        elif self.cfg.get("searxng"):
            self.searx_label.config(text="Web search: private (SearXNG in Docker)")
            self.searx_btn.config(text="Reinstall web search")
        elif ws.docker_exe():
            self.searx_label.config(text="Web search: DuckDuckGo. Docker is installed, so private search is available.")
        else:
            self.searx_label.config(text="Web search: DuckDuckGo. Install Docker Desktop for private search (SearXNG).")
            self.searx_btn.config(text="Get Docker Desktop",
                                  command=lambda: webbrowser.open("https://www.docker.com/products/docker-desktop/"))

    def setup_search(self):
        self.searx_btn.config(state="disabled")
        self._show_search_state("Setting up SearXNG… (first time downloads about 300 MB)")

        def work():
            try:
                ws.setup_searxng(self.cfg, self.log_threadsafe)
                msg = None
                restart = self.servers.wanted
            except Exception as e:
                msg, restart = f"Couldn't set up web search: {e}", False

            def done():
                self.searx_btn.config(state="normal")
                self._show_search_state(msg)
                if restart:   # the hub picks up SearXNG on its next start
                    self.servers.stop()
                    self.start_servers()
            self.q.put(("call", done))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ updates
    def _set_update_state(self, text, info=None):
        self.update_state = text
        self.q.put(("call", self._show_update_state))

    def _show_update_state(self):
        if not (hasattr(self, "update_label") and self.update_label.winfo_exists()):
            return
        self.update_label.config(text=f"Version {ws.APP_VERSION} · {self.update_state}")
        if not ws.can_self_update():
            self.update_btn.config(state="disabled", text="Check now")
        elif self.update_info:
            self.update_btn.config(state="normal", text=f"Install {self.update_info['version']} now")
        else:
            self.update_btn.config(state="normal", text="Check now")

    def update_button(self):
        if self.update_info:
            self.force_now = True   # also wakes a download that's waiting for Stella to be idle
            threading.Thread(target=lambda: self._check_and_install(force=True), daemon=True).start()
        else:
            threading.Thread(target=lambda: self._check_and_install(manual=True), daemon=True).start()

    def _update_loop(self):
        time.sleep(60)
        while True:
            self._check_and_install(manual=False)
            time.sleep(ws.UPDATE_EVERY)

    def _check_and_install(self, manual=False, force=False):
        """Looks for a newer release; downloads it; installs it once Stella is idle (or now, if forced)."""
        if not self.update_lock.acquire(blocking=False):
            return
        try:
            self._set_update_state("checking for updates…")
            try:
                info = ws.update_available(ws.APP_VERSION, self.cfg.get("update_repo") or ws.UPDATE_REPO)
            except Exception as e:
                self._set_update_state(f"couldn't check for updates ({type(e).__name__})")
                return
            self.update_info = info
            if not info:
                self._set_update_state("up to date (checked " + time.strftime("%H:%M") + ")")
                return
            auto = self.cfg.get("auto_update", True)
            force = force or self.force_now
            if not (auto or force):
                self._set_update_state(f"version {info['version']} is available")
                return
            if not self.update_file or info["name"] not in self.update_file:
                def prog(done, total):
                    if total:
                        self._set_update_state(f"downloading {info['version']}… {int(done * 100 / total)}%")
                self.update_file = ws.download_update(self.cfg, info, prog)
            if not force:
                self._set_update_state(f"version {info['version']} ready; installing when Stella is idle")
                while not (self.force_now or self._idle()):
                    time.sleep(30)
                    if not self.cfg.get("auto_update", True):
                        self._set_update_state(f"version {info['version']} is available")
                        return
            self._set_update_state(f"installing {info['version']}…")
            self.q.put(("call", lambda: self._apply_update(self.update_file)))
        except Exception as e:
            self.log_threadsafe(f"Update failed: {e}")
            self._set_update_state("update failed; will try again later")
        finally:
            self.update_lock.release()

    def _idle(self):
        if self.installing:
            return False
        if not self.servers.wanted:
            return True
        try:
            return stellai_hub.server_idle(quiet=120)
        except Exception:
            return False

    def _apply_update(self, path):
        """Stops the servers, starts the silent installer and exits; the installer restarts the app."""
        self.cfg.set(resume_servers=bool(self.servers.wanted), updated_from=ws.APP_VERSION)
        self.log("Installing update… StellAI will restart in a moment.")
        self.servers.stop()
        try:
            ws.run_installer(path, self.cfg)
        except OSError as e:
            self.log(f"Couldn't start the update: {e}")
            self.start_servers() if self.cfg.get("resume_servers") else None
            return
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()
        os._exit(0)

    def log_threadsafe(self, msg):
        self.q.put(("log", msg))

    def _notify(self, text):
        if self.tray:
            try:
                self.tray.notify(text, "StellAI")
            except Exception:
                pass

    # ------------------------------------------------------------------ plumbing
    def log(self, msg):
        if hasattr(self, "log_box") and self.log_box.winfo_exists():
            self.log_box.config(state="normal")
            self.log_box.insert("end", msg.rstrip() + "\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        try:
            with open(self.cfg.p("logs", "app.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg.rstrip() + "\n")
        except OSError:
            pass

    def _pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log(val)
                elif kind == "call":
                    val()
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _listen_for_show(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", SHOW_PORT))
            s.listen(2)
        except OSError:
            return
        while True:
            c, _ = s.accept()
            c.close()
            self.q.put(("call", self.show_window))

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.installing:
            if not messagebox.askyesno("StellAI", "Stop the download? It will resume next time."):
                return
            self.cancel.set()
        if self.servers.wanted and self.tray:
            self.root.withdraw()
            if not self.cfg.get("tray_hint_shown"):
                self.cfg.set(tray_hint_shown=True)
                try:
                    self.tray.notify("Stella is still running in the tray. Right-click the icon to quit.", "StellAI")
                except Exception:
                    pass
            return
        self.quit()

    def quit(self):
        try:
            self.servers.stop()
        finally:
            if self.tray:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            self.root.destroy()

    def _tray_image(self):
        from PIL import Image
        try:
            return Image.open(os.path.join(RES, "stellai.ico"))
        except OSError:
            img = Image.new("RGB", (64, 64), BG)
            from PIL import ImageDraw
            ImageDraw.Draw(img).regular_polygon((32, 32, 22), 4, fill=ACCENT)
            return img

    def _start_tray(self):
        try:
            import pystray
        except ImportError:
            return
        M = pystray.MenuItem
        menu = pystray.Menu(
            M("Show StellAI", lambda: self.q.put(("call", self.show_window)), default=True),
            M(lambda item: "Stop server" if self.servers.wanted else "Start server",
              lambda: self.q.put(("call", self.toggle_servers))),
            M("Open chat in browser", lambda: webbrowser.open(ws.client_url(self.cfg) if self.cfg.get("client")
                                                              else ws.web_chat_url(self.cfg))),
            pystray.Menu.SEPARATOR,
            M("Quit", lambda: self.q.put(("call", self.quit))),
        )
        try:
            self.tray = pystray.Icon("StellAI", self._tray_image(), "StellAI Server", menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def _update_tray(self):
        if self.tray:
            try:
                self.tray.title = "StellAI Server: " + ("running" if self.servers.wanted else "stopped")
                self.tray.update_menu()
            except Exception:
                pass

    def run(self):
        self.root.mainloop()


def already_running():
    try:
        with socket.create_connection(("127.0.0.1", SHOW_PORT), timeout=1):
            return True
    except OSError:
        return False


def selftest(out_dir):
    """CI check: build both pages on a real Windows desktop, save screenshots, report any Tk errors."""
    import json
    import traceback
    errors = []
    os.makedirs(out_dir, exist_ok=True)
    os.environ["STELLAI_HOME"] = os.path.join(out_dir, "home")
    app = App()
    app.root.report_callback_exception = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))

    def grab(name):
        app.root.update()
        try:
            from PIL import ImageGrab
            r = app.root
            ImageGrab.grab(bbox=(r.winfo_rootx(), r.winfo_rooty(), r.winfo_rootx() + r.winfo_width(),
                                 r.winfo_rooty() + r.winfo_height())).save(os.path.join(out_dir, name))
        except Exception as e:
            errors.append(f"screenshot {name}: {e}")

    def step2():
        grab("1-setup.png")
        if app.hw is None:
            errors.append("hardware detection did not finish")
        else:
            with open(os.path.join(out_dir, "hardware.json"), "w") as f:
                json.dump({"hardware": app.hw, "recommended": ws.recommend(app.hw)}, f, indent=2)
        app.cfg.set(plan=ws.plan(app.hw or {"ram_gb": 8, "cores": 2, "backend": "cpu", "vram_gb": 0}, "4b"))
        try:
            app.show_dashboard()
        except Exception:
            errors.append(traceback.format_exc())
        app.root.after(3000, step3)

    def step3():
        grab("2-dashboard.png")
        with open(os.path.join(out_dir, "result.txt"), "w") as f:
            f.write("\n\n".join(errors) or "OK")
        app.quit()

    app.root.after(12000, step2)
    app.run()
    return 1 if errors else 0


def main():
    _reattach_console()
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    if already_running():
        return  # the running copy shows its window
    after = "--after-update" in sys.argv
    App(minimized="--minimized" in sys.argv or after, after_update=after).run()


if __name__ == "__main__":
    main()
