# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Diagrams ------------------------------------------------------------------
DIAGRAM_DIR = None   # set in main()
PY_FOR_DIAGRAMS = sys.executable
MMDC = "mmdc"

SCHEMDRAW_ALLOWED_IMPORTS = {"schemdraw", "schemdraw.elements", "schemdraw.logic", "schemdraw.dsp",
                             "schemdraw.flow", "schemdraw.segments", "math"}
SCHEMDRAW_BLOCKED_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
                           "getattr", "setattr", "delattr", "input", "breakpoint", "help", "exit", "quit",
                           "memoryview", "os", "sys", "subprocess", "shutil", "socket", "pathlib", "importlib"}


def check_schemdraw_code(code):
    """Only schemdraw drawing code: no file, network or system access."""
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in SCHEMDRAW_ALLOWED_IMPORTS:
                    raise ValueError(f"import of {a.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") not in SCHEMDRAW_ALLOWED_IMPORTS or node.level:
                raise ValueError(f"import from {node.module} is not allowed")
        elif isinstance(node, ast.Name) and node.id in SCHEMDRAW_BLOCKED_NAMES:
            raise ValueError(f"'{node.id}' is not allowed in diagram code")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private attributes are not allowed in diagram code")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise ValueError("global statements are not allowed")


SCHEMDRAW_RUNNER = r'''
import sys
try:
    import resource  # Linux/macOS only; Windows relies on the wall-clock timeout instead
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
except ImportError:
    pass
import matplotlib
matplotlib.use("Agg")
import schemdraw
import schemdraw.elements as elm
code = open(sys.argv[1]).read()
out = sys.argv[2]
ns = {"schemdraw": schemdraw, "elm": elm}
_orig_draw = schemdraw.Drawing.draw
drawings = []
def _capture(self, *a, **k):
    drawings.append(self)
    k["show"] = False
    return _orig_draw(self, *a, **k)
schemdraw.Drawing.draw = _capture
_orig_exit = schemdraw.Drawing.__exit__
def _exit(self, *a):
    drawings.append(self)
    try:
        return _orig_exit(self, *a)
    except Exception:
        return None
schemdraw.Drawing.__exit__ = _exit
exec(compile(code, "diagram", "exec"), ns)
d = ns.get("d") or (drawings[-1] if drawings else None)
if d is None:
    for v in ns.values():
        if isinstance(v, schemdraw.Drawing):
            d = v
if d is None:
    sys.exit("no schemdraw.Drawing found (assign it to d)")
d.save(out, transparent=False, dpi=160)
'''


DIAGRAM_RUNNER_CMD = None   # e.g. ["StellAI.exe", "--schemdraw-runner"] in the packaged Windows app


def run_schemdraw_runner(src, out):
    """Entry point for packaged apps: run the schemdraw sandbox in this (fresh) process."""
    sys.argv = ["runner", src, out]
    exec(compile(SCHEMDRAW_RUNNER, "runner", "exec"), {"__name__": "__main__"})


def _child_env():
    keep = ["PATH", "HOME"]
    if os.name == "nt":
        keep += ["SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
                 "PATHEXT", "COMSPEC", "WINDIR", "PROGRAMFILES"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("HOME", tempfile.gettempdir())
    env["MPLBACKEND"] = "Agg"
    return env


def _run(cmd, timeout):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flashing up
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_child_env(),
                           creationflags=flags, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the diagram took too long to draw (over {timeout} s)")
    if p.returncode != 0:
        if p.returncode in (-9, -24, 137, 152):  # killed by the CPU-time limit
            raise RuntimeError("the diagram took too long to draw (over 20 s of CPU)")
        lines = [ln.strip() for ln in (p.stderr or p.stdout or "").splitlines()
                 if ln.strip() and not ln.strip().startswith("at ") and "(" not in ln[:1]]
        # Prefer the first "Error: ..." line plus the line after it (e.g. Mermaid's "Expecting ...")
        for i, ln in enumerate(lines):
            if ln.startswith("Error") or "Error:" in ln:
                detail = next((x for x in lines[i + 1:] if x.startswith("Expecting")), "")
                raise RuntimeError(f"{ln} {detail}".strip()[:300])
        raise RuntimeError((lines or [f"exit code {p.returncode}"])[-1][:300])


def render_schemdraw(code, out_png):
    check_schemdraw_code(code)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "diagram.py")
        runner = os.path.join(tmp, "runner.py")
        with open(src, "w") as f:
            f.write(code)
        with open(runner, "w") as f:
            f.write(SCHEMDRAW_RUNNER)
        if DIAGRAM_RUNNER_CMD:
            _run(list(DIAGRAM_RUNNER_CMD) + [src, out_png], timeout=40)
        else:
            _run([PY_FOR_DIAGRAMS, "-I", runner, src, out_png], timeout=40)


def mermaid_command():
    """Full path to the Mermaid CLI, or None when it isn't installed (Mermaid is optional)."""
    import shutil
    if MMDC and os.path.isfile(MMDC):
        return MMDC
    return shutil.which(MMDC) if MMDC else None


def render_mermaid(source, out_png):
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "diagram.mmd")
        with open(src, "w") as f:
            f.write(source)
        mmdc = mermaid_command()
        if not mmdc:
            raise ValueError("Mermaid isn't installed on this server; draw block diagrams with "
                             "kind='schemdraw' (schemdraw.flow) instead")
        _run([mmdc, "-i", src, "-o", out_png, "-b", "white", "-s", "2"], timeout=60)


def _wave_segments(wave):
    """Expand a WaveDrom wave string into per-cycle states."""
    states, last = [], "x"
    for ch in wave:
        if ch == "." or ch == "|":
            states.append(("cont", last))
        else:
            last = ch
            states.append(("new", ch))
    return states


def render_wavedrom(source, out_png):
    """Draws a WaveDrom (WaveJSON) timing diagram with matplotlib. Supports the common wave
    characters: 0 1 h l p n P N x z = 2-9 . | and data labels."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon

    spec = json.loads(source)
    signals = []

    def collect(items):
        for it in items:
            if isinstance(it, list):
                collect(it[1:] if it and isinstance(it[0], str) else it)
            elif isinstance(it, dict) and "wave" in it:
                signals.append(it)
            elif isinstance(it, dict):
                signals.append(None)  # spacer
    collect(spec.get("signal", []))
    if not signals:
        raise ValueError("no signals found")
    cycles = max(len(s["wave"]) for s in signals if s) or 1
    hscale = float(spec.get("config", {}).get("hscale", 1))
    cw = 0.6 * hscale
    rows = len(signals)
    fig_w = 1.6 + cycles * cw + 0.3
    fig_h = 0.55 * rows + 0.6
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=160)
    ax.set_xlim(-1.6, cycles * cw + 0.3)
    ax.set_ylim(-rows * 0.55 - 0.1, 0.4)
    ax.axis("off")
    for c in range(cycles + 1):
        ax.axvline(c * cw, color="#dddddd", lw=0.6, zorder=0)
    title = (spec.get("head") or {}).get("text") if isinstance(spec.get("head"), dict) else None
    if title:
        ax.text(cycles * cw / 2, 0.25, str(title), ha="center", va="center", fontsize=10, weight="bold")

    bus_colors = {"=": "#ffffff", "2": "#ffffff", "3": "#fff3b0", "4": "#ffd6a5", "5": "#caffbf",
                  "6": "#a0c4ff", "7": "#bdb2ff", "8": "#ffc6ff", "9": "#e0e0e0"}
    for row, sig in enumerate(signals):
        if sig is None:
            continue
        y0 = -row * 0.55 - 0.45
        hi, lo, mid = y0 + 0.36, y0, y0 + 0.18
        ax.text(-0.1, mid, str(sig.get("name", "")), ha="right", va="center", fontsize=9, family="monospace")
        data = sig.get("data", [])
        if isinstance(data, str):
            data = data.split()
        # Group the wave into runs: (start cycle, length, character)
        runs = []
        for c, (kind, ch) in enumerate(_wave_segments(str(sig["wave"]))):
            if kind == "cont" and runs:
                runs[-1][1] += 1
            else:
                runs.append([c, 1, ch])
        di = 0
        level = None  # last drawn line level, for vertical transitions
        for start, span, ch in runs:
            x0, x1 = start * cw, (start + span) * cw
            if ch in "pPnN":
                for k in range(span):
                    a, b = x0 + k * cw, x0 + (k + 1) * cw
                    first, second = (hi, lo) if ch in "pP" else (lo, hi)
                    xm = (a + b) / 2
                    ax.plot([a, a, xm, xm, b], [level if level is not None else first, first, first, second, second],
                            color="black", lw=1.2)
                    level = second
            elif ch in "1h0lz":
                y = hi if ch in "1h" else lo if ch in "0l" else mid
                ax.plot([x0, x0, x1], [level if level is not None else y, y, y],
                        color="#1f6feb" if ch == "z" else "black", lw=1.2)
                level = y
            else:
                # Bus value or undefined: one hexagon per run, label in the middle
                e = min(0.08, (x1 - x0) / 4)
                poly = [(x0 + e, hi), (x1 - e, hi), (x1, mid), (x1 - e, lo), (x0 + e, lo), (x0, mid)]
                if ch == "x":
                    ax.add_patch(Polygon(poly, closed=True, facecolor="#eeeeee", edgecolor="black", lw=1.0, hatch="////"))
                else:
                    ax.add_patch(Polygon(poly, closed=True, facecolor=bus_colors.get(ch, "#ffffff"),
                                         edgecolor="black", lw=1.0))
                    if di < len(data):
                        ax.text((x0 + x1) / 2, mid, str(data[di]), ha="center", va="center", fontsize=8)
                    di += 1
                level = None
    fig.savefig(out_png, bbox_inches="tight", facecolor="white", pad_inches=0.15)
    plt.close(fig)


def tool_render_diagram(args):
    kind = str(args.get("kind", "")).lower().strip()
    source = str(args.get("source", ""))
    title = str(args.get("title", "")).strip() or kind
    if not source.strip():
        raise ValueError("no diagram source")
    if len(source) > 20000:
        raise ValueError("diagram source is too long")
    image_id = uuid.uuid4().hex[:16]
    out = os.path.join(DIAGRAM_DIR, image_id + ".png")
    if kind == "mermaid":
        render_mermaid(source, out)
    elif kind == "schemdraw":
        render_schemdraw(source, out)
    elif kind == "wavedrom":
        render_wavedrom(source, out)
    else:
        raise ValueError("kind must be mermaid, schemdraw or wavedrom")
    if not os.path.exists(out):
        raise RuntimeError("the renderer produced no image")
    return {"image": image_id, "title": title, "note": "The diagram is now shown to the user."}, f"Drew {title}"


def _p(desc, typ="string"):
    return {"type": typ, "description": desc}


QTY = "Number, or a value with SI prefix/unit like '4.7k', '10uF', '3.3V', '20mA'"


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


MERMAID_HINT = "mermaid: block diagrams, flowcharts, state/sequence diagrams. "
NO_MERMAID_HINT = "Flowcharts: schemdraw.flow (flow.Start, flow.Box, flow.Decision, flow.Arrow). "


DIAGRAM_DESC = (
    "Draw and show a diagram. schemdraw: circuit schematics as Python ('import schemdraw', "
    "'import schemdraw.elements as elm', 'with schemdraw.Drawing() as d:', 'd += elm.Resistor().label(\"10kΩ\")'; "
    "no other imports). {MERMAID}wavedrom: timing diagrams as WaveJSON."
)


def refresh_diagram_tool():
    """Only offer Mermaid to the model when the Mermaid CLI is actually installed."""
    hint = MERMAID_HINT if mermaid_command() else NO_MERMAID_HINT
    fn = SERVER_TOOLS["render_diagram"][1]["function"]
    fn["description"] = DIAGRAM_DESC.replace("{MERMAID}", hint)
    fn["parameters"]["properties"]["kind"]["description"] = (
        "schemdraw, mermaid or wavedrom" if mermaid_command() else "schemdraw or wavedrom")


SERVER_TOOLS = {
    "calculate": (tool_calculate, _fn(
        "calculate",
        "Exact maths. ALWAYS use this instead of doing arithmetic yourself. Supports + - * / ^ ( ), "
        "SI values like 4.7k, 10u, 3.3V, 100mA, functions sqrt ln log10 exp sin cos tan abs round min max, "
        "parallel(a,b,...) for parallel resistors, db(ratio), and constants pi e.",
        {"expression": _p("The expression, e.g. '12 * 4.7k / (4.7k + 10k)' or 'parallel(10k, 4.7k)'"),
         "unit": _p("Optional unit for the answer, e.g. 'V', 'Ω', 'Hz', 'A'")}, ["expression"])),
    "ohms_law": (tool_ohms_law, _fn(
        "ohms_law", "Ohm's law and power: give any two of voltage, current, resistance, power.",
        {"voltage": _p(QTY), "current": _p(QTY), "resistance": _p(QTY), "power": _p(QTY)}, [])),
    "voltage_divider": (tool_voltage_divider, _fn(
        "voltage_divider",
        "Resistor voltage divider (Vout = Vin*R2/(R1+R2), R2 to ground). Give three of vin, vout, r1, r2 "
        "to solve the fourth, or just vin and vout to get the best standard-value resistor pair.",
        {"vin": _p(QTY), "vout": _p(QTY), "r1": _p(QTY), "r2": _p(QTY),
         "series": _p("Resistor series for suggestions: E12, E24 (default) or E96")}, [])),
    "led_resistor": (tool_led_resistor, _fn(
        "led_resistor", "Current-limiting resistor for LEDs, with standard values and power rating.",
        {"supply_voltage": _p(QTY), "led_voltage": _p("Forward voltage of one LED, e.g. 2.0 red, 3.1 white/blue"),
         "current_ma": _p("LED current in mA, e.g. 10"),
         "leds_in_series": _p("How many LEDs in series (default 1)", "integer")},
        ["supply_voltage", "led_voltage", "current_ma"])),
    "rc_filter": (tool_rc_filter, _fn(
        "rc_filter", "RC low/high-pass filter: cutoff fc = 1/(2πRC). Give any two of resistance, capacitance, frequency.",
        {"resistance": _p(QTY), "capacitance": _p(QTY), "frequency": _p(QTY)}, [])),
    "combine_resistors": (tool_combine_resistors, _fn(
        "combine_resistors", "Total resistance of resistors in series or parallel.",
        {"values": {"type": "array", "items": {"type": "string"}, "description": "Resistor values, e.g. ['10k','4.7k']"},
         "mode": _p("'series' or 'parallel'")}, ["values", "mode"])),
    "standard_resistor": (tool_standard_resistor, _fn(
        "standard_resistor", "Nearest standard resistor values (E12/E24/E96) to a calculated value.",
        {"value": _p(QTY), "series": _p("E12, E24 (default) or E96")}, ["value"])),
    "render_diagram": (tool_render_diagram, _fn(
        "render_diagram",
        DIAGRAM_DESC.replace("{MERMAID}", MERMAID_HINT),
        {"kind": _p("schemdraw, mermaid or wavedrom"), "source": _p("The diagram code"),
         "title": _p("Short title, e.g. 'LED driver'")}, ["kind", "source"])),
}


