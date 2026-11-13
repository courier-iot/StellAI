# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- PCB Designer: KiCad -------------------------------------------------------------------------------
PCB = Workspaces("pcb", "", "PCB")
PCB_MARK = "@@PCB@@"
PCB_RUNNER_PY = r'''# StellAI PCB runner: one step on a KiCad board with KiCad's Python (pcbnew). The job comes as JSON in the
# file named by STELLAI_JOB. Written by stellai_hub.py: edits here are overwritten.
import contextlib
import io
import json
import math
import os
import sys
import traceback

import pcbnew

MARK = "@@PCB@@"
OUT = sys.stdout


def mm(v):
    return round(pcbnew.ToMM(v), 3)


def report(board):
    fps = []
    for fp in board.GetFootprints():
        p = fp.GetPosition()
        fps.append({"ref": fp.GetReference(), "value": fp.GetValue(), "footprint": fp.GetFPID().GetUniStringLibId(),
                    "x_mm": mm(p.x), "y_mm": mm(p.y), "rot_deg": round(fp.GetOrientationDegrees(), 2),
                    "side": "bottom" if fp.IsFlipped() else "top"})
    tracks = vias = 0
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            vias += 1
        else:
            tracks += 1
    out = {"footprints": fps, "tracks": tracks, "vias": vias, "zones": len(list(board.Zones())),
           "nets": board.GetNetCount(), "copper_layers": board.GetCopperLayerCount()}
    try:
        bb = board.GetBoardEdgesBoundingBox()
        if bb.GetWidth() > 0:
            out["board_mm"] = [mm(bb.GetWidth()), mm(bb.GetHeight())]
            out["board_origin_mm"] = [mm(bb.GetX()), mm(bb.GetY())]
    except Exception:
        pass
    return out


def namespace(board):
    V = pcbnew.VECTOR2I
    F = pcbnew.FromMM

    def fp(ref):
        f = board.FindFootprintByReference(ref)
        if f is None:
            raise KeyError(f"no footprint {ref!r}; footprints: {[x.GetReference() for x in board.GetFootprints()][:60]}")
        return f

    def move(ref, x, y, rot=None):
        """Puts footprint ref at x, y mm (board coordinates, Y grows downwards), optionally turned to rot degrees."""
        f = fp(ref)
        f.SetPosition(V(F(x), F(y)))
        if rot is not None:
            f.SetOrientationDegrees(float(rot))

    def layer(name):
        lid = board.GetLayerID(name)
        if lid < 0:
            raise KeyError(f"no layer {name!r}")
        return lid

    def line(x1, y1, x2, y2, layer_name="Edge.Cuts", width=0.1):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(V(F(x1), F(y1)))
        s.SetEnd(V(F(x2), F(y2)))
        s.SetLayer(layer(layer_name))
        s.SetWidth(F(width))
        board.Add(s)
        return s

    def outline_rect(w, h, x=0.0, y=0.0, replace=True):
        """A w by h mm board outline with its top-left corner at x, y (replaces the old outline)."""
        if replace:
            for d in list(board.GetDrawings()):
                if d.GetLayer() == pcbnew.Edge_Cuts:
                    board.Remove(d)
        line(x, y, x + w, y)
        line(x + w, y, x + w, y + h)
        line(x + w, y + h, x, y + h)
        line(x, y + h, x, y)

    def track(x1, y1, x2, y2, width=0.25, layer_name="F.Cu", net=None):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(V(F(x1), F(y1)))
        t.SetEnd(V(F(x2), F(y2)))
        t.SetWidth(F(width))
        t.SetLayer(layer(layer_name))
        if net:
            n = board.FindNet(net)
            if n is None:
                raise KeyError(f"no net {net!r}")
            t.SetNet(n)
        board.Add(t)
        return t

    def text(s, x, y, size=1.0, layer_name="F.SilkS"):
        t = pcbnew.PCB_TEXT(board)
        t.SetText(s)
        t.SetPosition(V(F(x), F(y)))
        t.SetLayer(layer(layer_name))
        t.SetTextSize(V(F(size), F(size)))
        board.Add(t)
        return t

    return {"pcbnew": pcbnew, "board": board, "math": math, "mm": F, "to_mm": pcbnew.ToMM, "VECTOR2I": V,
            "fp": fp, "move": move, "line": line, "outline_rect": outline_rect, "track": track, "text": text,
            "__name__": "stellai_step"}


# ---- a board from a netlist (circuit as code) ------------------------------------------------------
def _sexp(text):
    """A tiny S-expression reader for KiCad netlists."""
    tokens, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "()":
            tokens.append(c)
            i += 1
        elif c.isspace():
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            tokens.append(("s", "".join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            tokens.append(("s", text[i:j]))
            i = j
    stack, cur = [], []
    for t in tokens:
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            done, cur = cur, stack.pop()
            cur.append(done)
        else:
            cur.append(t[1])
    return cur[0] if cur else []


def _find(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def _val(node, key, default=""):
    f = _find(node, key)
    return f[0][1] if f and len(f[0]) > 1 and isinstance(f[0][1], str) else default


def read_netlist(path):
    with open(path, encoding="utf-8") as f:
        tree = _sexp(f.read())
    comps = []
    for blk in _find(tree, "components"):
        for c in _find(blk, "comp"):
            comps.append({"ref": _val(c, "ref"), "value": _val(c, "value"), "footprint": _val(c, "footprint")})
    nets = []
    for blk in _find(tree, "nets"):
        for nt in _find(blk, "net"):
            nodes = [(_val(nd, "ref"), _val(nd, "pin")) for nd in _find(nt, "node")]
            nets.append({"name": _val(nt, "name"), "nodes": nodes})
    return comps, nets


def _fp_dirs(job):
    dirs = [d for d in (job.get("footprint_dirs") or []) if d and os.path.isdir(d)]
    for k in ("KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR"):
        if os.environ.get(k) and os.path.isdir(os.environ[k]):
            dirs.append(os.environ[k])
    return dirs


def _load_fp(dirs, lib_id):
    lib, _, name = lib_id.partition(":")
    for d in dirs:
        p = os.path.join(d, lib + ".pretty")
        if os.path.isdir(p):
            fp = pcbnew.FootprintLoad(p, name)
            if fp is not None:
                return fp
    raise KeyError(f"footprint {lib_id} not found in KiCad's libraries")


def _bbox(fp):
    """A footprint's outline box without its text (the call changed between KiCad versions)."""
    try:
        return fp.GetBoundingBox(False, False)
    except TypeError:
        return fp.GetBoundingBox()


def build_board(board, job):
    """Footprints from the netlist, connected, on a wide grid; then a board outline around them."""
    comps, nets = read_netlist(job["netlist"])
    dirs = _fp_dirs(job)
    V, F = pcbnew.VECTOR2I, pcbnew.FromMM
    for fp in list(board.GetFootprints()):
        board.Remove(fp)
    for t in list(board.GetTracks()):
        board.Remove(t)
    netinfo = {}
    for n in nets:
        ni = pcbnew.NETINFO_ITEM(board, n["name"])
        board.Add(ni)
        netinfo[n["name"]] = ni
    pad_net = {(r, p): n["name"] for n in nets for r, p in n["nodes"]}
    fps = []
    for c in comps:
        fp = _load_fp(dirs, c["footprint"])
        fp.SetReference(c["ref"])
        fp.SetValue(c["value"])
        board.Add(fp)
        for pad in fp.Pads():
            name = pad_net.get((c["ref"], pad.GetNumber()))
            if name:
                pad.SetNet(netinfo[name])
        fps.append(fp)
    # Wide, simple placement: connectors down the left edge, everything else on a grid in reference order,
    # with room between parts for thick tracks and hand soldering.
    gap = float(job.get("spacing_mm") or 8)

    def size(fp):
        bb = _bbox(fp)
        return pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())

    def key(fp):
        ref = fp.GetReference()
        letters = "".join(ch for ch in ref if ch.isalpha())
        num = "".join(ch for ch in ref if ch.isdigit())
        order = {"U": 0, "Q": 1, "D": 2, "SW": 3, "R": 4, "C": 5, "L": 6}
        return (order.get(letters, 7), letters, int(num or 0))

    conns = sorted([f for f in fps if f.GetReference().startswith(("J", "P"))], key=key)
    rest = sorted([f for f in fps if f not in conns], key=key)
    cell_w = max([size(f)[0] for f in rest] + [5]) + gap
    cell_h = max([size(f)[1] for f in rest] + [5]) + gap
    cols = max(1, int(round(len(rest) ** 0.5 + 0.49)))
    x0, y0 = 100.0, 100.0
    left = max([size(f)[0] for f in conns] + [0])
    y = y0
    for f in conns:
        w, h = size(f)
        f.SetPosition(V(F(x0 + w / 2), F(y + h / 2)))
        y += h + gap
    gx = x0 + (left + gap if conns else 0)
    for i, f in enumerate(rest):
        r, c = divmod(i, cols)
        f.SetPosition(V(F(gx + c * cell_w + cell_w / 2), F(y0 + r * cell_h + cell_h / 2)))
    # outline: around the parts with a margin, or the size asked for
    bb = None
    for f in fps:
        b = _bbox(f)
        if bb is None:
            bb = b
        else:
            bb.Merge(b)
    margin = float(job.get("margin_mm") or 5)
    ns = namespace(board)
    if bb is not None:
        bx, by = pcbnew.ToMM(bb.GetX()) - margin, pcbnew.ToMM(bb.GetY()) - margin
        bw, bh = pcbnew.ToMM(bb.GetWidth()) + 2 * margin, pcbnew.ToMM(bb.GetHeight()) + 2 * margin
        bw, bh = max(bw, float(job.get("width_mm") or 0)), max(bh, float(job.get("height_mm") or 0))
        ns["outline_rect"](round(bw, 1), round(bh, 1), round(bx, 1), round(by, 1))
    # wide tracks by default
    width = F(float(job.get("track_mm") or 0.5))
    try:
        ds = board.GetDesignSettings()
        try:
            nc = ds.m_NetSettings.GetDefaultNetclass()
        except Exception:
            nc = ds.m_NetSettings.m_DefaultNetClass
        nc.SetTrackWidth(width)
        nc.SetClearance(F(float(job.get("clearance_mm") or 0.3)))
    except Exception:
        pass
    return {"placed": len(fps), "nets": len(nets)}


def autoroute(board, job):
    dsn, ses = job["dsn"], job["ses"]
    pcbnew.ExportSpecctraDSN(board, dsn)
    return os.path.exists(dsn)


def import_routes(board, job):
    try:
        ok = pcbnew.ImportSpecctraSES(board, job["ses"])
    except TypeError:
        ok = pcbnew.ImportSpecctraSES(job["ses"])
    return bool(ok)


def main():
    with open(os.environ["STELLAI_JOB"], encoding="utf-8") as f:
        job = json.load(f)
    for k, v in (job.get("env") or {}).items():
        os.environ[k] = v
    out = {"ok": False}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if job.get("new"):
                board = pcbnew.NewBoard(job["board"]) if hasattr(pcbnew, "NewBoard") else pcbnew.BOARD()
            else:
                board = pcbnew.LoadBoard(job["board"])
            if job.get("netlist"):
                out["before"] = report(board)
                out["built"] = build_board(board, job)
            if job.get("dsn"):
                out["dsn"] = autoroute(board, job)
            if job.get("import_ses"):
                out["routed"] = import_routes(board, job)
            if job.get("code"):
                out["before"] = report(board)
                exec(compile(job["code"], "<step>", "exec"), namespace(board))
            out["after"] = report(board)
            if job.get("save"):
                pcbnew.SaveBoard(job["board"], board)
        out["ok"] = True
    except Exception as e:
        tb = traceback.extract_tb(sys.exc_info()[2])
        out.update(error=f"{type(e).__name__}: {e}"[:1500],
                   where=[f"line {f.lineno}: {(f.line or '').strip()}" for f in tb if f.filename == "<step>"][-3:])
    out["output"] = buf.getvalue()[-3000:]
    OUT.write(MARK + json.dumps(out) + "\n")
    OUT.flush()


main()
'''
PCB_IMPORTS = {"pcbnew", "math", "random", "itertools", "functools", "collections", "statistics"}
PCB_BAD_ATTRS = {"LoadBoard", "NewBoard", "CreateEmptyBoard", "WriteDRCReport", "PLOT_CONTROLLER", "Load", "Plot"}
KICAD = {"python": None}


def find_kicad_cli():
    env = os.environ.get("STELLAI_KICAD_CLI", "")
    if env and os.path.isfile(env):
        return env
    w = shutil.which("kicad-cli")
    if w:
        return w
    if os.name == "nt":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            found = sorted(glob.glob(os.path.join(root, "KiCad", "*", "bin", "kicad-cli.exe")), reverse=True)
            if found:
                return found[0]
    for c in ("/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"):
        if os.path.isfile(c):
            return c
    return ""


def find_kicad_python():
    """A Python that can import pcbnew (KiCad's own on Windows), or "" (checked once per run)."""
    if KICAD["python"] is not None:
        return KICAD["python"]
    cands = [os.environ.get("STELLAI_KICAD_PYTHON", "")]
    cli = find_kicad_cli()
    if cli and os.name == "nt":
        cands.append(os.path.join(os.path.dirname(cli), "python.exe"))
    cands += ["/usr/bin/python3", shutil.which("python3") or ""]
    KICAD["python"] = ""
    for c in cands:
        if c and os.path.isfile(c):
            try:
                if subprocess.run([c, "-c", "import pcbnew"], capture_output=True, timeout=60,
                                  creationflags=0x08000000 if os.name == "nt" else 0).returncode == 0:
                    KICAD["python"] = c
                    break
            except (OSError, subprocess.TimeoutExpired):
                pass
    return KICAD["python"]


def _kicad_cli(args, timeout=300):
    cli = find_kicad_cli()
    if not cli:
        raise RuntimeError("KiCad isn't installed on the server. Linux: re-run stellai-setup.sh and say yes to KiCad. "
                           "Windows: install it from kicad.org. Then try again.")
    p = subprocess.run([cli] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, stdin=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0)
    return p.returncode, ((p.stdout or "") + "\n" + (p.stderr or "")).strip()


def _project_files(d):
    """(board .kicad_pcb, root schematic .kicad_sch or "") inside a version folder."""
    pcbs = sorted(glob.glob(os.path.join(d, "*.kicad_pcb")))
    pros = sorted(glob.glob(os.path.join(d, "*.kicad_pro")))
    stem = os.path.splitext(os.path.basename(pros[0]))[0] if pros else (
        os.path.splitext(os.path.basename(pcbs[0]))[0] if pcbs else "")
    board = os.path.join(d, stem + ".kicad_pcb") if stem and os.path.isfile(os.path.join(d, stem + ".kicad_pcb")) \
        else (pcbs[0] if pcbs else "")
    sch = os.path.join(d, stem + ".kicad_sch") if stem and os.path.isfile(os.path.join(d, stem + ".kicad_sch")) else ""
    return board, sch


def _pcb_run(board, code="", save=False, timeout=180):
    py = find_kicad_python()
    if not py:
        raise RuntimeError("KiCad's Python scripting (pcbnew) isn't available on the server, so board edits can't "
                           "run. Checks, pictures and exports still work. Linux: install KiCad's Python module "
                           "(it comes with the kicad package); Windows: it ships with KiCad.")
    script = os.path.join(BLENDER["root"], ".worker", "stellai_pcb.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write(PCB_RUNNER_PY)
    return _run_marked([py, script], PCB_MARK, {"board": board, "code": code, "save": save}, timeout)


def _summarize_violations(items, limit=12):
    lines, counts = [], {}
    for v in items:
        sev = v.get("severity", "?")
        counts[sev] = counts.get(sev, 0) + 1
        if len(lines) < limit:
            where = "; ".join(i.get("description", "")[:80] for i in (v.get("items") or [])[:2])
            lines.append(f"[{sev}] {v.get('type', '')}: {v.get('description', '')}" + (f" ({where})" if where else ""))
    return counts, lines


def _pcb_checks(d):
    board, sch = _project_files(d)
    out = {}
    tmp = os.path.join(BLENDER["root"], ".worker")
    if board:
        rpt = os.path.join(tmp, f"drc-{uuid.uuid4().hex[:8]}.json")
        args = ["pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "-o", rpt]
        if sch:
            args.append("--schematic-parity")
        _rc, text = _kicad_cli(args + [board])
        try:
            with open(rpt, encoding="utf-8") as f:
                data = json.load(f)
            counts, lines = _summarize_violations(data.get("violations", []))
            out["drc"] = {"counts": counts, "unconnected": len(data.get("unconnected_items", [])),
                          "schematic_mismatches": len(data.get("schematic_parity", [])), "top": lines}
        except (OSError, ValueError):
            out["drc"] = {"error": text[-600:] or "no report"}
        _quiet_remove(rpt)
    if sch:
        rpt = os.path.join(tmp, f"erc-{uuid.uuid4().hex[:8]}.json")
        _rc, text = _kicad_cli(["sch", "erc", "--format", "json", "--severity-all", "-o", rpt, sch])
        try:
            with open(rpt, encoding="utf-8") as f:
                data = json.load(f)
            items = [v for s in data.get("sheets", []) for v in s.get("violations", [])]
            counts, lines = _summarize_violations(items)
            out["erc"] = {"counts": counts, "top": lines}
        except (OSError, ValueError):
            out["erc"] = {"error": text[-600:] or "no report"}
        _quiet_remove(rpt)
    return out


def _pcb_render(d, side="top"):
    board, _ = _project_files(d)
    if not board:
        return None, "no board file to show"
    side = side if side in ("top", "bottom", "front", "back", "left", "right") else "top"
    image_id = uuid.uuid4().hex[:16]
    png = os.path.join(DIAGRAM_DIR, image_id + ".png")
    rc, text = _kicad_cli(["pcb", "render", "--side", side, "--width", "1400", "--height", "1000", "--quality",
                           "basic", "-o", png, board], timeout=240)
    if not os.path.exists(png):       # older kicad-cli: fewer options
        rc, text = _kicad_cli(["pcb", "render", "-o", png, board], timeout=240)
    if not os.path.exists(png):
        return None, "KiCad couldn't draw the board (pictures need KiCad 9 or newer): " + text[-300:]
    return image_id, ""


def tool_pcb_project(args):
    action = str(args.get("action", "status")).strip().lower()
    if action == "status":
        ver = ""
        if find_kicad_cli():
            try:
                ver = _kicad_cli(["version"], timeout=30)[1].splitlines()[0]
            except (OSError, IndexError, subprocess.TimeoutExpired):
                ver = "?"
        return {"kicad_cli": find_kicad_cli() or "not installed", "kicad_version": ver,
                "board_scripting": bool(find_kicad_python()), "inbox_folder": _inbox(),
                "projects_you_can_import": [f for f in sorted(os.listdir(_inbox())) if f.lower().endswith(
                    (".zip", ".kicad_pcb")) or os.path.isdir(os.path.join(_inbox(), f))][:40],
                "workspaces": PCB.list(), "active": _safe(lambda: PCB.name(""))}, "Checked PCB workspaces"
    if action == "new":
        name = PCB.start(args.get("name"))
        v, path = PCB.next(name)
        try:
            if args.get("file"):
                src = _pick_inbox_file(args.get("file"), ("zip", "kicad_pcb"))
                if os.path.isdir(src):
                    shutil.copytree(src, path)
                elif src.lower().endswith(".zip"):
                    import zipfile
                    with zipfile.ZipFile(src) as z:
                        for m in z.namelist():
                            if m.startswith("/") or ".." in m.replace("\\", "/").split("/"):
                                raise ValueError("that zip has unsafe paths in it")
                        z.extractall(path)
                    inner = [e for e in os.listdir(path) if not e.startswith(".")]
                    if len(inner) == 1 and os.path.isdir(os.path.join(path, inner[0])):   # zip of a folder
                        sub = os.path.join(path, inner[0])
                        for e in os.listdir(sub):
                            shutil.move(os.path.join(sub, e), path)
                        os.rmdir(sub)
                else:
                    os.makedirs(path)
                    shutil.copyfile(src, os.path.join(path, os.path.basename(src)))
                    sch = src[:-len(".kicad_pcb")] + ".kicad_sch"
                    if os.path.isfile(sch):
                        shutil.copyfile(sch, os.path.join(path, os.path.basename(sch)))
                if not _project_files(path)[0] and not _project_files(path)[1]:
                    raise ValueError("no .kicad_pcb or .kicad_sch file in it")
            else:
                os.makedirs(path)
                board = os.path.join(path, name + ".kicad_pcb")
                w, h = float(args.get("width_mm") or 50), float(args.get("height_mm") or 50)
                py = find_kicad_python()
                if not py:
                    raise RuntimeError("starting an empty board needs KiCad's Python (pcbnew); import a project "
                                       "from the 3D inbox instead")
                script = os.path.join(BLENDER["root"], ".worker", "stellai_pcb.py")
                with open(script, "w", encoding="utf-8") as f:
                    f.write(PCB_RUNNER_PY)
                res = _run_marked([py, script], PCB_MARK, {"board": board, "new": True, "save": True,
                                                           "code": f"outline_rect({w}, {h}, 100, 100)"}, 180)
                if not res.get("ok"):
                    raise RuntimeError(res.get("error") or "KiCad couldn't make the board")
        except Exception:
            shutil.rmtree(PCB.dir(name), ignore_errors=True)
            raise
        PCB.record(name, v, f"start: {args.get('file') or 'empty board'}")
        PCB.activate(name)
        board, sch = _project_files(path)
        res = {"workspace": name, "version": v, "board": os.path.basename(board) or None,
               "schematic": os.path.basename(sch) or None, "checks": _pcb_checks(path)}
        img, why = _pcb_render(path)
        if img:
            res.update(image=img, title=f"{name} v{v}")
        elif why:
            res["picture"] = why
        return res, f"Started PCB workspace {name} (v{v})"
    if action in ("add_reference", "references", "clear_references"):
        return PCB.refs_action(action, args)
    name = PCB.name(args.get("name"))
    if action == "open":
        PCB.activate(name)
        return {"workspace": name, "latest_version": PCB.latest(name), "recent": PCB.history(name)[-8:]}, f"Opened {name}"
    if action == "history":
        return {"workspace": name, "versions_on_disk": PCB.versions(name), "history": PCB.history(name)[-25:]}, \
            f"History of {name}"
    if action == "revert":
        target = _int_version(args.get("version"))
        if target not in PCB.versions(name):
            raise ValueError(f"version {target} isn't on disk; available: {PCB.versions(name)[-15:]}")
        v, path = PCB.next(name)
        shutil.copytree(PCB.path(name, target), path)
        PCB.record(name, v, f"back to v{target}")
        return {"workspace": name, "version": v, "same_as": target}, f"Went back to v{target} (saved as v{v})"
    if action == "export":
        return _pcb_export(name, str(args.get("format", "fab")).lower().strip(". "))
    raise ValueError("action must be status, new, open, history, revert, export, add_reference, references or "
                     "clear_references")


def _pcb_export(name, fmt):
    import zipfile
    v = PCB.latest(name)
    d = PCB.path(name, v)
    board, sch = _project_files(d)
    outdir = PCB.dir(name, "exports")
    os.makedirs(outdir, exist_ok=True)
    stem = f"{name}-v{v:04d}"
    made, problems = [], []

    def run(args, path):
        rc, text = _kicad_cli(args)
        if os.path.exists(path):
            made.append(path)
        else:
            problems.append(text[-300:] or f"{os.path.basename(path)} wasn't made")

    if fmt in ("fab", "gerbers", "all"):
        if not board:
            raise ValueError("no board file in this project")
        gdir = os.path.join(BLENDER["root"], ".worker", f"gerb-{uuid.uuid4().hex[:8]}")
        os.makedirs(gdir)
        _kicad_cli(["pcb", "export", "gerbers", "-o", gdir + os.sep, board])
        _kicad_cli(["pcb", "export", "drill", "-o", gdir + os.sep, board])
        zpath = os.path.join(outdir, stem + "-fab.zip")
        files = sorted(os.listdir(gdir))
        if files:
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(os.path.join(gdir, f), f)
            made.append(zpath)
        else:
            problems.append("no Gerber or drill files came out")
        shutil.rmtree(gdir, ignore_errors=True)
    if fmt in ("bom", "all") and sch:
        run(["sch", "export", "bom", "-o", os.path.join(outdir, stem + "-bom.csv"), sch], os.path.join(outdir, stem + "-bom.csv"))
    if fmt in ("pos", "placement", "all") and board:
        p = os.path.join(outdir, stem + "-positions.csv")
        run(["pcb", "export", "pos", "--format", "csv", "--units", "mm", "-o", p, board], p)
    if fmt in ("step", "3d", "all") and board:
        p = os.path.join(outdir, stem + ".step")
        run(["pcb", "export", "step", "--subst-models", "-o", p, board], p)
        if os.path.exists(p):   # so the CAD Designer can build an enclosure around it
            shutil.copyfile(p, os.path.join(_inbox(), stem + ".step"))
    if fmt in ("pdf", "schematic", "all") and sch:
        p = os.path.join(outdir, stem + "-schematic.pdf")
        run(["sch", "export", "pdf", "-o", p, sch], p)
    if fmt in ("project", "kicad", "all"):
        zpath = os.path.join(outdir, stem + "-kicad-project.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _dirs, files in os.walk(d):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, d))
        made.append(zpath)
    if not made and not problems:
        raise ValueError("format must be fab (Gerbers + drill), bom, pos, step, pdf, project or all")
    return {"workspace": name, "version": v, "files": made, "problems": problems,
            "note": "The files are in this folder on the server computer. fab is the zip a board maker asks for."}, \
        f"Exported {name} v{v}"


def tool_pcb_check(args):
    name = PCB.name(args.get("workspace"))
    v = PCB.latest(name)
    d = PCB.path(name, v)
    out = {"workspace": name, "version": v, "checks": _pcb_checks(d)}
    board, _ = _project_files(d)
    if board and find_kicad_python():
        try:
            r = _pcb_run(board)
            if r.get("ok"):
                a = r["after"]
                out["board"] = {k: a.get(k) for k in ("board_mm", "copper_layers", "nets", "tracks", "vias", "zones")}
                out["footprints"] = a.get("footprints", [])[:80]
        except RuntimeError as e:
            out["board_error"] = str(e)
    if str(args.get("render", True)).lower() not in ("false", "0", "no"):
        img, why = _pcb_render(d, str(args.get("side", "top")).lower())
        if img:
            out.update(image=img, title=f"{name} v{v} ({args.get('side', 'top')})")
        elif why:
            out["picture"] = why
    return out, f"Checked {name} v{v}"


def tool_pcb_step(args):
    code, goal = str(args.get("code", "")), str(args.get("goal", "")).strip()
    if not goal or not code.strip():
        raise ValueError("give goal (with millimetres and references like R3) and code")
    probs = check_bpy_code(code, allowed=PCB_IMPORTS, extra_bad=PCB_BAD_ATTRS, bad_prefixes=("Export", "Import", "Plot"))
    if probs:
        return {"ok": False, "error": "the code was not run: " + "; ".join(probs)}, "Step blocked"
    name = PCB.name(args.get("workspace"))
    base = PCB.latest(name)
    before_checks = _pcb_checks(PCB.path(name, base))
    v, path = PCB.next(name)
    shutil.copytree(PCB.path(name, base), path)
    board, _ = _project_files(path)
    if not board:
        shutil.rmtree(path, ignore_errors=True)
        raise ValueError("this project has no board file to edit")
    try:
        res = _pcb_run(board, code, save=True, timeout=max(30, min(600, int(args.get("timeout") or 180))))
    except RuntimeError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    if not res.get("ok"):
        shutil.rmtree(path, ignore_errors=True)
        return {"ok": False, "error": res.get("error"), "where": res.get("where"), "output": res.get("output", ""),
                "note": f"Nothing was saved; the board is still v{base}."}, "Step failed"
    b, a = res["before"], res["after"]
    diff = _diff_by(b["footprints"], a["footprints"], "ref", ("x_mm", "y_mm", "rot_deg", "side", "value", "footprint"))
    for k in ("board_mm", "tracks", "vias", "zones", "nets", "copper_layers"):
        if b.get(k) != a.get(k):
            diff.setdefault("board", {})[k] = {"from": b.get(k), "to": a.get(k)}
    if not (diff["added"] or diff["removed"] or diff["changed"] or diff.get("board")):
        shutil.rmtree(path, ignore_errors=True)
        return {"ok": False, "error": "the code ran but nothing on the board changed", "output": res.get("output", ""),
                "note": f"Nothing was saved; the board is still v{base}."}, "Step changed nothing"
    PCB.record(name, v, goal)
    after_checks = _pcb_checks(path)
    out = {"version": v, "previous": base, "changes": diff, "checks_before": before_checks.get("drc", {}).get("counts"),
           "checks_now": after_checks, "output": res.get("output", "")[-1500:]}
    img, why = _pcb_render(path, str(args.get("side", "top")).lower())
    if img:
        out.update(image=img, title=f"{name} v{v}")
        if str(args.get("check", True)).lower() not in ("false", "0", "no"):
            out["visual_check"] = _vision_check(img, goal, diff, "one picture of the board from the "
                                                + str(args.get("side", "top")), refs=PCB.ref_images(name))
    elif why:
        out["picture"] = why
    return out, f"Saved {name} v{v}"


_REF_ARGS = {"photos": {"type": "array", "items": {"type": "string"},
                        "description": "For add_reference: photo ids from [photo ...] in the chat, or 'latest'"},
             "labels": {"type": "array", "items": {"type": "string"},
                        "description": "For add_reference: front, left, back, right, top or detail"}}

SERVER_TOOLS["cad_project"] = (tool_cad_project, _fn(
    "cad_project",
    "CAD Designer workspaces (FreeCAD, millimetres, numbered versions). status: FreeCAD, workspaces, importable files. "
    "new: start from an empty design or a .FCStd/.step/.stl/.iges in the 3D inbox. open, history, revert. export: "
    "step (for other CAD), stl (for 3D printing), fcstd, brep, iges, or blender (sends an STL to the 3D Modeler's "
    "inbox). add_reference / references / clear_references: photos or sketches to compare against.",
    {"action": _p("status, new, open, history, revert, export, add_reference, references or clear_references"),
     "name": _p("Workspace name (default: the active one)"), "file": _p("For new: a file in the 3D inbox"),
     "version": _p("For revert", "integer"), "format": _p("For export: step (default), stl, fcstd, brep, iges, blender"),
     **_REF_ARGS}, ["action"]))
SERVER_TOOLS["cad_step"] = (tool_cad_step, _fn(
    "cad_step",
    "CAD Designer: run ONE small change on the current design as a FreeCAD Python script (headless, millimetres, Z up, "
    "front faces -Y). Saved as a new version only if it works and changes something; then measured (sizes, volume, "
    "solids, broken geometry), rendered and checked against goal and reference pictures. Use Part shapes: "
    "Part.makeBox/makeCylinder/makeCone/makeSphere/makeTorus, shape.cut/fuse/common, makeFillet/makeChamfer, "
    "Part.makePolygon/Face/extrude for profiles. Ready to use: doc, add(name, shape) (makes or replaces a part), "
    "get(name), remove(name), hide(name), box(x, y, z, at), cylinder(r, h, at, axis), hole(d, depth, at, axis), "
    "fillet(shape, r, edges), chamfer(shape, d, edges), Vector, Placement, Rotation. Keep one final part per real "
    "object: build with shapes, then add() the result. No files or other imports.",
    {"goal": _p("What this step should achieve with millimetres, e.g. 'add two M3 holes (3.2 mm) through the base, "
                "30 mm apart, centred'"),
     "code": _p("The FreeCAD script"),
     "views": {"type": "array", "items": {"type": "string"},
               "description": "front, right, back, left, top, three_quarter (default 4)"},
     "check": _p("false skips the visual check", "boolean"), "workspace": _p("Default: the active workspace")},
    ["goal", "code"]))
SERVER_TOOLS["cad_inspect"] = (tool_cad_inspect, _fn(
    "cad_inspect",
    "CAD Designer: read the current design without changing it: parts with sizes (mm), positions, volumes, solids and "
    "geometry problems. render=true adds pictures (and a comparison with reference pictures).",
    {"render": _p("true for pictures", "boolean"),
     "views": {"type": "array", "items": {"type": "string"}, "description": "Render angles"},
     "workspace": _p("Default: the active workspace")}, []))
SERVER_TOOLS["pcb_project"] = (tool_pcb_project, _fn(
    "pcb_project",
    "PCB Designer workspaces (KiCad projects with numbered versions). status: KiCad, workspaces, importable projects. "
    "new: import a KiCad project (.zip, folder or .kicad_pcb in the 3D inbox) or start an empty board of width_mm x "
    "height_mm. open, history, revert. export: fab (Gerbers + drill zip for a board maker), bom, pos (pick and "
    "place), step (3D), pdf (schematic), project (zip of the KiCad files) or all. add_reference etc. as in CAD.",
    {"action": _p("status, new, open, history, revert, export, add_reference, references or clear_references"),
     "name": _p("Workspace name (default: the active one)"), "file": _p("For new: a project in the 3D inbox"),
     "width_mm": _p("For an empty board", "number"), "height_mm": _p("For an empty board", "number"),
     "version": _p("For revert", "integer"), "format": _p("For export: fab (default), bom, pos, step, pdf, project, all"),
     **_REF_ARGS}, ["action"]))
SERVER_TOOLS["pcb_check"] = (tool_pcb_check, _fn(
    "pcb_check",
    "PCB Designer: check the current board without changing it: KiCad's electrical rules check (schematic) and design "
    "rules check (board: clearances, unrouted connections, mismatches with the schematic), board size, layers, nets, "
    "footprint positions, and a picture.",
    {"side": _p("Picture side: top (default) or bottom"), "render": _p("false skips the picture", "boolean"),
     "workspace": _p("Default: the active workspace")}, []))
SERVER_TOOLS["pcb_step"] = (tool_pcb_step, _fn(
    "pcb_step",
    "PCB Designer: run ONE small board change as a KiCad pcbnew script (millimetres; board coordinates, Y grows "
    "downwards). Saved as a new version only if it works and changes something; then the design rules are checked "
    "again and the board is pictured. Ready to use: board, pcbnew, mm(x) (to KiCad units), to_mm, fp(ref), "
    "move(ref, x, y, rot), outline_rect(w, h, x, y), line(x1, y1, x2, y2, layer, width), track(x1, y1, x2, y2, width, "
    "layer, net), text(s, x, y, size, layer). Schematic changes aren't scripted: guide the user in KiCad for those.",
    {"goal": _p("What this step should achieve, e.g. 'move C3 within 2 mm of U1 pin 8 and rotate it 90°'"),
     "code": _p("The pcbnew script"), "side": _p("Picture side: top (default) or bottom"),
     "check": _p("false skips the visual check", "boolean"), "workspace": _p("Default: the active workspace")},
    ["goal", "code"]))


