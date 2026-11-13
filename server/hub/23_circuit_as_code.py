# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Circuit as code: a SKiDL script -> netlist + schematic -> wide placement -> Freerouting -> checks -----------
# Stella writes the circuit (parts and connections) as Python; KiCad-compatible files come out of it. Boards are
# laid out wide and simple (through-hole friendly, thick tracks) so early designs are easy to check and solder.
# A small library on the server keeps sub-circuits that worked (recipes), what went wrong (notes) and pinouts the
# user confirmed, so she gets better over time instead of guessing the same things again.
SKIDL_MARK = "@@SKIDL@@"
SKIDL_RUNNER_PY = r'''# StellAI circuit runner: runs a circuit written with SKiDL, checks it and writes a KiCad netlist (and a
# schematic when SKiDL can draw one). The job comes as JSON in the file named by STELLAI_JOB. Written by
# stellai_hub.py: edits here are overwritten.
import contextlib
import io
import json
import os
import sys
import traceback

MARK = "@@SKIDL@@"
OUT = sys.stdout


def main():
    with open(os.environ["STELLAI_JOB"], encoding="utf-8") as f:
        job = json.load(f)
    for k, v in (job.get("env") or {}).items():      # where KiCad's symbol and footprint libraries are
        os.environ[k] = v
    out = {"ok": False}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            import skidl
            ns = {"__name__": "stellai_circuit"}
            exec("from skidl import *", ns)
            names = [job.get("tool") or "KICAD9", "KICAD9", "KICAD8", "KICAD10", "KICAD7"]
            for tname in names:
                if hasattr(skidl, tname):
                    try:
                        skidl.set_default_tool(getattr(skidl, tname))
                    except Exception:
                        pass
                    break
            exec(compile(job["code"], "<circuit>", "exec"), ns)
            circuit = skidl.default_circuit
            erc_buf = io.StringIO()
            with contextlib.redirect_stdout(erc_buf), contextlib.redirect_stderr(erc_buf):
                try:
                    skidl.ERC()
                except Exception as e:
                    print(f"ERC couldn't run: {e}")
            parts = []
            for p in circuit.parts:
                parts.append({"ref": p.ref, "name": getattr(p, "name", ""), "value": str(getattr(p, "value", "") or ""),
                              "footprint": str(getattr(p, "footprint", "") or ""),
                              "pins": [f"{pin.num}:{pin.name}" for pin in p.pins][:40],
                              "unconnected": [f"{pin.num}:{pin.name}" for pin in p.pins if not pin.is_connected()][:20]})
            nets = []
            for n in circuit.get_nets():
                nets.append({"name": n.name, "pins": [f"{pin.part.ref}.{pin.num}({pin.name})" for pin in n.pins][:30]})
            missing_fp = [p["ref"] for p in parts if not p["footprint"]]
            out.update(parts=parts, nets=nets, erc=erc_buf.getvalue()[-3000:], missing_footprints=missing_fp)
            if missing_fp:
                raise ValueError("every part needs a footprint (footprint='Library:Name'); missing on: "
                                 + ", ".join(missing_fp))
            skidl.generate_netlist(file_=job["netlist"])
            if job.get("schematic_dir"):
                try:
                    skidl.generate_schematic(filepath=job["schematic_dir"], top_name=job.get("name", "circuit"))
                    out["schematic"] = True
                except Exception as e:
                    out["schematic"] = False
                    out["schematic_note"] = f"no schematic drawing ({type(e).__name__}: {str(e)[:200]})"
        out["ok"] = True
    except Exception as e:
        tb = traceback.extract_tb(sys.exc_info()[2])
        out.update(error=f"{type(e).__name__}: {e}"[:1500],
                   where=[f"line {f.lineno}: {(f.line or '').strip()}" for f in tb if f.filename == "<circuit>"][-3:])
    out["output"] = buf.getvalue()[-3000:]
    OUT.write(MARK + json.dumps(out) + "\n")
    OUT.flush()


main()
'''
SKIDL_IMPORTS = {"skidl", "math", "itertools", "functools", "collections"}
SKIDL_BAD = {"lib_search_paths", "footprint_search_paths", "set_default_tool", "backup_parts", "load_backup_lib",
             "set_query_backend", "ERC", "reset"}
CIRCUIT = {"python": None, "lock": threading.Lock()}


def _kicad_libs():
    """KiCad's symbol and footprint folders, and environment variables that point SKiDL at them."""
    roots = ["/usr/share/kicad", "/usr/local/share/kicad"]
    cli = find_kicad_cli()
    if cli:
        roots.insert(0, os.path.normpath(os.path.join(os.path.dirname(cli), "..", "share", "kicad")))
    sym = fpd = ""
    for r in roots:
        if not sym and os.path.isdir(os.path.join(r, "symbols")):
            sym = os.path.join(r, "symbols")
        if not fpd and os.path.isdir(os.path.join(r, "footprints")):
            fpd = os.path.join(r, "footprints")
    sym = os.environ.get("STELLAI_KICAD_SYMBOLS", sym)
    fpd = os.environ.get("STELLAI_KICAD_FOOTPRINTS", fpd)
    env = {}
    for v in ("", "7", "8", "9", "10"):
        if sym:
            env[f"KICAD{v}_SYMBOL_DIR"] = sym
        if fpd:
            env[f"KICAD{v}_FOOTPRINT_DIR"] = fpd
    return sym, fpd, env


def find_skidl_python():
    """A Python that can import skidl (its own folder from the setup script first), or "" (checked once)."""
    if CIRCUIT["python"] is not None:
        return CIRCUIT["python"]
    base = os.path.dirname(CHATS_PATH) if CHATS_PATH else ""
    cands = [os.environ.get("STELLAI_SKIDL_PYTHON", "")]
    if base:
        cands += [os.path.join(base, "skidl-venv", "bin", "python3"),
                  os.path.join(base, "skidl-venv", "Scripts", "python.exe")]
    cands += [find_kicad_python(), sys.executable, shutil.which("python3") or ""]
    CIRCUIT["python"] = ""
    for c in cands:
        if c and os.path.isfile(c):
            try:
                if subprocess.run([c, "-c", "import skidl"], capture_output=True, timeout=90,
                                  creationflags=0x08000000 if os.name == "nt" else 0).returncode == 0:
                    CIRCUIT["python"] = c
                    break
            except (OSError, subprocess.TimeoutExpired):
                pass
    return CIRCUIT["python"]


def find_freerouting():
    """(java, freerouting jar) or ("", "")."""
    java = os.environ.get("STELLAI_JAVA", "") or shutil.which("java") or ""
    jar = os.environ.get("STELLAI_FREEROUTING", "")
    if not (jar and os.path.isfile(jar)):
        dirs = [os.path.join(os.path.dirname(CHATS_PATH), "freerouting")] if CHATS_PATH else []
        dirs.append(os.path.join(BLENDER["root"], "freerouting"))
        jars = [j for d in dirs for j in sorted(glob.glob(os.path.join(d, "freerouting*.jar")), reverse=True)]
        jar = jars[0] if jars else ""
    return (java, jar) if java and jar else ("", "")


def _kicad_major():
    try:
        m = re.search(r"(\d+)\.", _kicad_cli(["version"], timeout=30)[1])
        return int(m.group(1)) if m else 9
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return 9


# the library: recipes (sub-circuits that worked), notes (lessons) and confirmed pinouts
def _lib_path():
    return os.path.join(BLENDER["root"], "pcb", "library.json")


def _lib():
    try:
        with open(_lib_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("recipes", {})
    d.setdefault("pinouts", {})
    d.setdefault("notes", [])
    return d


def _lib_save(d):
    os.makedirs(os.path.dirname(_lib_path()), exist_ok=True)
    tmp = _lib_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, _lib_path())


def _part_key(name, footprint):
    return f"{str(name).strip()}|{str(footprint).strip()}".lower()


def _skidl_check(code):
    return check_bpy_code(code, allowed=SKIDL_IMPORTS, extra_bad=SKIDL_BAD, bad_prefixes=("generate_",))


def _pcb_runner_script():
    script = os.path.join(BLENDER["root"], ".worker", "stellai_pcb.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write(PCB_RUNNER_PY)
    return script


def _autoroute(board, passes, timeout, env):
    """Board -> Specctra DSN -> Freerouting -> SES -> back onto the board. Returns a short report."""
    java, jar = find_freerouting()
    if not java:
        return {"routed": False, "note": "the autorouter (Freerouting + Java) isn't installed on the server; parts "
                                         "are placed and connected by ratsnest lines. Re-run the setup and say yes "
                                         "to the autorouter, or route by hand in KiCad."}
    py = find_kicad_python()
    work = os.path.join(BLENDER["root"], ".worker", f"route-{uuid.uuid4().hex[:8]}")
    os.makedirs(work)
    dsn, ses = os.path.join(work, "board.dsn"), os.path.join(work, "board.ses")
    try:
        r = _run_marked([py, _pcb_runner_script()], PCB_MARK, {"board": board, "dsn": dsn, "ses": ses, "env": env}, 180)
        if not r.get("ok") or not os.path.isfile(dsn):
            return {"routed": False, "note": "KiCad couldn't export the board for the autorouter: "
                                             + str(r.get("error") or "no file")[:300]}
        base_cmd = [java, "-Djava.awt.headless=true", "-jar", jar, "-de", dsn, "-do", ses, "-mp", str(passes)]
        tail = ""
        for extra in (["--gui.enabled=false"], []):      # Freerouting 2.x needs the first, 1.x refuses it
            try:
                p = subprocess.run(base_cmd + extra, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout, cwd=work, stdin=subprocess.DEVNULL,
                                   creationflags=0x08000000 if os.name == "nt" else 0)
                tail = ((p.stdout or "") + (p.stderr or ""))[-400:]
            except subprocess.TimeoutExpired:
                return {"routed": False, "note": f"the autorouter ran longer than {timeout} s and was stopped; the "
                                                 "board is saved unrouted. Try more spacing or fewer parts."}
            if os.path.isfile(ses):
                break
        if not os.path.isfile(ses):
            return {"routed": False, "note": "the autorouter didn't produce a result: " + tail.strip()[-300:]}
        r = _run_marked([py, _pcb_runner_script()], PCB_MARK,
                        {"board": board, "ses": ses, "import_ses": True, "save": True, "env": env}, 180)
        if not r.get("ok") or not r.get("routed"):
            return {"routed": False, "note": "KiCad couldn't bring the routes back: " + str(r.get("error") or "")[:300]}
        a = r.get("after", {})
        return {"routed": True, "tracks": a.get("tracks"), "vias": a.get("vias")}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def tool_pcb_circuit(args):
    code, goal = str(args.get("code", "")), str(args.get("goal", "")).strip()
    if not goal or not code.strip():
        raise ValueError("give goal (what the circuit does) and code (the SKiDL script)")
    lib = _lib()
    used = []
    pre = []
    for rname in args.get("recipes") or []:
        rec = lib["recipes"].get(str(rname).strip().lower())
        if not rec:
            raise ValueError(f"no recipe called {rname}; pcb_library list shows the saved ones")
        pre.append(f"# ---- recipe: {rname}\n" + rec["code"])
        used.append(str(rname).strip().lower())
    full = "\n\n".join(pre + [code])
    probs = _skidl_check(full)
    if probs:
        return {"ok": False, "error": "the script was not run: " + "; ".join(probs)}, "Circuit blocked"
    sp = find_skidl_python()
    if not sp:
        raise RuntimeError("SKiDL (circuit as code) isn't installed on the server. Linux: re-run stellai-setup.sh "
                           "and say yes to the PCB Designer; Windows: pip install skidl into KiCad's Python.")
    if not find_kicad_python():
        raise RuntimeError("building a board needs KiCad's Python (pcbnew) on the server")
    # workspace: a new name starts one, otherwise the active (or named) one gets a new version
    wanted = str(args.get("workspace") or "").strip().lower()
    if wanted and not PCB.versions(wanted):
        name = PCB.start(wanted)
    else:
        name = PCB.name(wanted)
    prev = PCB.versions(name)
    v, path = PCB.next(name)
    os.makedirs(path)
    stem = name
    sym, fpd, env = _kicad_libs()
    major = _kicad_major()
    try:
        net = os.path.join(path, stem + ".net")
        res = _run_marked([sp, _skidl_script()], SKIDL_MARK,
                          {"code": full, "netlist": net, "name": stem, "tool": f"KICAD{major}", "env": env,
                           "schematic_dir": path if str(args.get("schematic", True)).lower() not in ("false", "0", "no")
                           else ""}, max(30, min(600, int(args.get("timeout") or 240))))
        if not res.get("ok"):
            shutil.rmtree(path, ignore_errors=True)
            return {"ok": False, "error": res.get("error"), "where": res.get("where"), "erc": res.get("erc", ""),
                    "output": res.get("output", "")[-1500:],
                    "note": "Nothing was saved." + (f" The board is still v{prev[-1]}." if prev else "")}, \
                "Circuit failed"
        board = os.path.join(path, stem + ".kicad_pcb")
        job = {"board": board, "new": True, "netlist": net, "save": True, "env": env, "footprint_dirs": [fpd],
               "spacing_mm": args.get("spacing_mm") or 8, "track_mm": args.get("track_mm") or 0.5,
               "clearance_mm": args.get("clearance_mm") or 0.3, "margin_mm": args.get("margin_mm") or 5,
               "width_mm": args.get("width_mm") or 0, "height_mm": args.get("height_mm") or 0}
        if prev and str(args.get("keep_placement", True)).lower() not in ("false", "0", "no"):
            old_board, _ = _project_files(PCB.path(name, prev[-1]))
            if old_board:
                job["keep_from"] = old_board
        b = _run_marked([find_kicad_python(), _pcb_runner_script()], PCB_MARK, job, 300)
        if not b.get("ok"):
            shutil.rmtree(path, ignore_errors=True)
            return {"ok": False, "error": "the circuit is fine but the board couldn't be built: " + str(b.get("error")),
                    "parts": res.get("parts"), "note": "Nothing was saved. A wrong footprint name is the usual cause "
                                                       "(Library:Footprint, as in KiCad)."}, "Board failed"
        out = {"workspace": name, "version": v, "parts": len(res.get("parts", [])), "nets": len(res.get("nets", [])),
               "placed": b.get("built"), "erc": (res.get("erc") or "").strip()[-1500:] or "no problems reported",
               "schematic": res.get("schematic", False)}
        if res.get("schematic_note"):
            out["schematic_note"] = res["schematic_note"]
        if str(args.get("route", True)).lower() not in ("false", "0", "no"):
            out["routing"] = _autoroute(board, max(5, min(200, int(args.get("passes") or 40))),
                                        max(60, min(1800, int(args.get("route_timeout") or 600))), env)
        else:
            out["routing"] = {"routed": False, "note": "not routed (route=false)"}
    except Exception:
        shutil.rmtree(path, ignore_errors=True)
        raise
    with open(os.path.join(path, "circuit.py"), "w", encoding="utf-8") as f:   # the source, for the next change
        f.write(full)
    PCB.record(name, v, "circuit: " + goal)
    PCB.activate(name)
    # parts whose pinout nobody has confirmed yet: the user should check them against the datasheet
    with CIRCUIT["lock"]:
        lib = _lib()
        for u in used:
            lib["recipes"][u]["uses"] = lib["recipes"][u].get("uses", 0) + 1
        if used:
            _lib_save(lib)
    simple = ("device:r", "device:c", "device:c_polarized", "device:led", "device:d", "device:l")
    unknown = []
    for p in res.get("parts", []):
        if _part_key(p.get("name"), p.get("footprint")) in lib["pinouts"]:
            continue
        if any(str(p.get("name", "")).lower() == s.split(":")[1] for s in simple):
            continue
        unknown.append({"ref": p["ref"], "part": p.get("name"), "footprint": p.get("footprint"),
                        "pins": p.get("pins", [])[:24]})
    if unknown:
        out["verify_with_user"] = unknown[:12]
        out["verify_note"] = ("These parts' pin numbers aren't confirmed yet. Show the user the pins and ask them to "
                              "check against the datasheet or the real part; when they say it's right, save it with "
                              "pcb_library confirm_pinout so it's trusted next time.")
    floating = [f"{p['ref']}: {', '.join(p['unconnected'][:6])}" for p in res.get("parts", []) if p.get("unconnected")]
    if floating:
        out["unconnected_pins"] = floating[:15]
    out["checks"] = _pcb_checks(path)
    img, why = _pcb_render(path, str(args.get("side", "top")).lower())
    if img:
        out.update(image=img, title=f"{name} v{v}")
        if str(args.get("check", True)).lower() not in ("false", "0", "no"):
            out["visual_check"] = _vision_check(img, goal, {"parts": out["parts"], "nets": out["nets"]},
                                                "one picture of the board from the top", refs=PCB.ref_images(name))
    elif why:
        out["picture"] = why
    out["library_tip"] = ("If the user confirms this works (tested or looks right), offer to save the useful part as a "
                          "recipe with pcb_library save_recipe; if something was wrong, save what you learned with "
                          "pcb_library note.")
    return out, f"Built {name} v{v} from the circuit"


def _skidl_script():
    script = os.path.join(BLENDER["root"], ".worker", "stellai_skidl.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write(SKIDL_RUNNER_PY)
    return script


def tool_pcb_library(args):
    action = str(args.get("action", "list")).strip().lower()
    with CIRCUIT["lock"]:
        lib = _lib()
        name = str(args.get("name", "")).strip().lower()
        if action == "list":
            q = str(args.get("query", "")).lower()
            recs = [{"name": n, "description": r.get("description", ""), "confirmed": r.get("confirmed", False),
                     "uses": r.get("uses", 0)} for n, r in sorted(lib["recipes"].items())
                    if not q or q in n or q in r.get("description", "").lower()]
            pins = [{"part": p.get("part"), "footprint": p.get("footprint"), "source": p.get("source", "")}
                    for p in lib["pinouts"].values() if not q or q in json.dumps(p).lower()]
            notes = [n for n in lib["notes"] if not q or q in n.get("text", "").lower()][-15:]
            return {"recipes": recs[:60], "confirmed_pinouts": pins[:80], "notes": notes,
                    "tools": {"skidl": bool(find_skidl_python()), "kicad_python": bool(find_kicad_python()),
                              "autorouter": bool(find_freerouting()[0])}}, "Read the PCB library"
        if action == "get":
            rec = lib["recipes"].get(name)
            if not rec:
                raise ValueError(f"no recipe called {name}")
            return {"name": name, **rec}, f"Read recipe {name}"
        if action == "save_recipe":
            code = str(args.get("code", ""))
            if not WS_NAME.fullmatch(name or "-") or not code.strip():
                raise ValueError("give name (lowercase-with-dashes) and code (SKiDL, ideally a @subcircuit function)")
            probs = _skidl_check(code)
            if probs:
                return {"ok": False, "error": "not saved: " + "; ".join(probs)}, "Recipe blocked"
            old = lib["recipes"].get(name, {})
            lib["recipes"][name] = {"description": str(args.get("description", old.get("description", "")))[:400],
                                    "code": code, "confirmed": False, "uses": old.get("uses", 0),
                                    "notes": old.get("notes", []), "saved_at": int(time.time())}
            _lib_save(lib)
            return {"saved": name, "confirmed": False,
                    "note": "Saved as a draft; mark it confirmed once the user says it works."}, f"Saved recipe {name}"
        if action == "confirm_recipe":
            if name not in lib["recipes"]:
                raise ValueError(f"no recipe called {name}")
            lib["recipes"][name]["confirmed"] = True
            _lib_save(lib)
            return {"confirmed": name}, f"Confirmed recipe {name}"
        if action == "note":
            text = str(args.get("text", "")).strip()
            if not text:
                raise ValueError("give text: what went wrong and what fixed it")
            entry = {"text": text[:600], "at": int(time.time())}
            if name and name in lib["recipes"]:
                lib["recipes"][name].setdefault("notes", []).append(entry)
            else:
                lib["notes"] = (lib["notes"] + [entry])[-200:]
            _lib_save(lib)
            return {"noted": text[:120]}, "Saved a PCB note"
        if action == "confirm_pinout":
            part, fp = str(args.get("part", "")).strip(), str(args.get("footprint", "")).strip()
            if not part or not fp:
                raise ValueError("give part (the symbol, e.g. Regulator_Linear:AMS1117-3.3 or its name) and footprint")
            lib["pinouts"][_part_key(part.split(":")[-1], fp)] = {
                "part": part, "footprint": fp, "pins": str(args.get("pins", ""))[:1500],
                "source": str(args.get("source", "the user confirmed it"))[:200], "at": int(time.time())}
            _lib_save(lib)
            return {"confirmed": f"{part} on {fp}"}, f"Confirmed pinout of {part}"
        if action == "forget":
            gone = lib["recipes"].pop(name, None) is not None
            _lib_save(lib)
            return {"forgot": name if gone else None}, f"Forgot recipe {name}" if gone else "Nothing to forget"
    raise ValueError("action must be list, get, save_recipe, confirm_recipe, note, confirm_pinout or forget")


SERVER_TOOLS["pcb_circuit"] = (tool_pcb_circuit, _fn(
    "pcb_circuit",
    "PCB Designer, circuit as code: write the whole circuit as a SKiDL Python script (parts + connections) and get a "
    "KiCad netlist, a schematic, and a board with every part placed on a wide grid (connectors on the left), thick "
    "tracks and an outline, autorouted with Freerouting, then checked (ERC, DRC) and pictured. Each run is a new "
    "version; parts keep the places they had. Script basics (from skidl import * is done): "
    "r = Part('Device', 'R', value='1k', footprint='Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal'); "
    "vcc, gnd = Net('VCC'), Net('GND'); vcc += r[1]; r[2] & led['A']; led['K'] += gnd. EVERY part needs a real KiCad "
    "footprint (prefer through-hole: _THT, PinHeader_1x02_P2.54mm_Vertical for connectors). Name nets; give power "
    "nets a power flag or a connector. Start simple; reuse recipes from pcb_library.",
    {"goal": _p("What the circuit does, e.g. '5 V USB in, 3.3 V regulator, power LED, 2-pin header out'"),
     "code": _p("The SKiDL script"),
     "workspace": _p("Workspace name: a new name starts one; default: the active workspace"),
     "recipes": {"type": "array", "items": {"type": "string"},
                 "description": "Saved recipe names to include before the script (their functions become usable)"},
     "route": _p("false skips autorouting", "boolean"), "spacing_mm": _p("Gap between parts (default 8)", "number"),
     "track_mm": _p("Track width (default 0.5)", "number"), "width_mm": _p("Minimum board width", "number"),
     "height_mm": _p("Minimum board height", "number"),
     "keep_placement": _p("false re-places every part on the grid", "boolean"),
     "check": _p("false skips the visual check", "boolean")},
    ["goal", "code"]))
SERVER_TOOLS["pcb_library"] = (tool_pcb_library, _fn(
    "pcb_library",
    "PCB Designer's memory of what works. list (query optional): saved recipes (sub-circuits as SKiDL functions), "
    "confirmed pinouts, lessons. get: a recipe's code. save_recipe: keep a working sub-circuit (only after it built "
    "cleanly; marked draft). confirm_recipe: the user said it works. note: what went wrong and the fix (name ties it "
    "to a recipe). confirm_pinout: the user checked a part's pins (part, footprint, pins, source). forget: a recipe.",
    {"action": _p("list, get, save_recipe, confirm_recipe, note, confirm_pinout or forget"),
     "name": _p("Recipe name (lowercase-with-dashes)"), "description": _p("For save_recipe: what it does, values"),
     "code": _p("For save_recipe: SKiDL code, ideally @subcircuit def name(...)"),
     "text": _p("For note"), "query": _p("For list: filter words"),
     "part": _p("For confirm_pinout: the symbol"), "footprint": _p("For confirm_pinout"),
     "pins": _p("For confirm_pinout: e.g. 1=GND 2=VOUT 3=VIN"), "source": _p("For confirm_pinout: datasheet etc.")},
    ["action"]))


import hashlib

