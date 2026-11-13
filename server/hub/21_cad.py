# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Versioned workspaces for the CAD and PCB designers ----------------------------------------------
class Workspaces:
    """Numbered versions of one design: a file per version (CAD) or a folder per version (a KiCad project).
    Lives next to the 3D Modeler's folders, under the same root."""

    def __init__(self, kind, ext, label):
        self.kind, self.ext, self.label = kind, ext, label      # ext "" = one folder per version

    def root(self, *parts):
        return os.path.join(BLENDER["root"], self.kind, *parts)

    def name(self, name):
        name = str(name or "").strip().lower()
        if not name:
            try:
                with open(self.root("active.txt"), encoding="utf-8") as f:
                    name = f.read().strip()
            except OSError:
                name = ""
            if not name:
                raise ValueError(f"no {self.label} workspace yet: start one with action new")
        if not WS_NAME.fullmatch(name):
            raise ValueError("workspace names use lowercase letters, digits and dashes, e.g. fan-duct")
        return name

    def dir(self, name, *parts):
        return self.root("workspaces", name, *parts)

    def versions(self, name):
        d = self.dir(name, "versions")
        pat = re.compile(r"v(\d{4,})" + (re.escape(self.ext) if self.ext else ""))
        out = []
        for f in os.listdir(d) if os.path.isdir(d) else []:
            m = pat.fullmatch(f)
            if m and (self.ext or os.path.isdir(os.path.join(d, f))):
                out.append(int(m.group(1)))
        return sorted(out)

    def path(self, name, v):
        return self.dir(name, "versions", f"v{v:04d}{self.ext}")

    def latest(self, name):
        vs = self.versions(name)
        if not vs:
            raise ValueError(f"workspace {name} has no saved versions")
        return vs[-1]

    def next(self, name):
        v = (self.versions(name) or [0])[-1] + 1
        return v, self.path(name, v)

    def activate(self, name):
        os.makedirs(self.root(), exist_ok=True)
        with open(self.root("active.txt"), "w", encoding="utf-8") as f:
            f.write(name)

    def history(self, name):
        try:
            with open(self.dir(name, "history.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def record(self, name, v, what):
        hist = self.history(name) + [{"version": v, "what": str(what)[:300], "at": int(time.time())}]
        with open(self.dir(name, "history.json"), "w", encoding="utf-8") as f:
            json.dump(hist[-500:], f, indent=1)
        keep = set(self.versions(name)[-BLENDER_KEEP:]) | {1}
        for old in self.versions(name):
            if old not in keep:
                p = self.path(name, old)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else _quiet_remove(p)

    def list(self):
        d = self.root("workspaces")
        names = sorted(os.listdir(d)) if os.path.isdir(d) else []
        return [{"name": n, "latest_version": self.versions(n)[-1]}
                for n in names if WS_NAME.fullmatch(n) and self.versions(n)]

    def start(self, name):
        name = str(name or "").strip().lower()
        if not WS_NAME.fullmatch(name):
            raise ValueError("give a name: lowercase letters, digits and dashes, e.g. fan-duct")
        if self.versions(name):
            raise ValueError(f"workspace {name} already exists; use action open, or pick another name")
        for sub in ("versions", "exports", "refs"):
            os.makedirs(self.dir(name, sub), exist_ok=True)
        return name

    # reference pictures, the same as the 3D Modeler's
    def refs(self, name):
        try:
            with open(self.dir(name, "refs", "refs.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def ref_images(self, name, limit=3):
        refs = [r for r in self.refs(name) if os.path.isfile(self.dir(name, "refs", r["file"]))]
        order = {l: i for i, l in enumerate(REF_LABELS)}
        refs.sort(key=lambda r: order.get(r.get("label") or "", 99))
        return [(r.get("label") or "reference", self.dir(name, "refs", r["file"])) for r in refs[:limit]]

    def refs_action(self, action, args):
        name = self.name(args.get("name"))
        refs = self.refs(name)
        os.makedirs(self.dir(name, "refs"), exist_ok=True)
        if action == "references":
            return {"workspace": name, "references": refs}, f"{len(refs)} reference picture(s)"
        if action == "clear_references":
            for r in refs:
                _quiet_remove(self.dir(name, "refs", r["file"]))
            refs = []
        else:
            labels = args.get("labels") or []
            if isinstance(labels, str):
                labels = [l for l in re.split(r"[,\s]+", labels) if l]
            for i, src in enumerate(_photo_files(args.get("photos") or "latest")):
                label = str(labels[i]).lower() if i < len(labels) else ""
                if label and label not in REF_LABELS:
                    raise ValueError(f"labels are {', '.join(REF_LABELS)}")
                if label:
                    refs = [r for r in refs if r.get("label") != label]
                fname = f"ref-{uuid.uuid4().hex[:8]}.jpg"
                shutil.copyfile(src, self.dir(name, "refs", fname))
                refs.append({"file": fname, "label": label, "photo": os.path.basename(src)[:-4], "at": int(time.time())})
        with open(self.dir(name, "refs", "refs.json"), "w", encoding="utf-8") as f:
            json.dump(refs[-8:], f, indent=1)
        return {"workspace": name, "references": refs[-8:]}, f"{len(refs[-8:])} reference picture(s)"


def _quiet_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _pick_inbox_file(src, exts):
    """A file (or folder) the user dropped into the 3D inbox, by name."""
    src = os.path.basename(str(src or "").strip())
    path = os.path.join(_inbox(), src)
    if not src or not os.path.exists(path):
        have = [f for f in sorted(os.listdir(_inbox())) if f.rsplit(".", 1)[-1].lower() in exts
                or os.path.isdir(os.path.join(_inbox(), f))]
        raise ValueError(f"no {src!r} in the 3D inbox ({_inbox()}); there: {have[:30]}")
    return path


def _run_marked(cmd, mark, job, timeout, env_extra=None, cwd=None):
    """Runs a runner script with a JSON job; returns its JSON answer. Raises RuntimeError if it never answers."""
    wdir = os.path.join(BLENDER["root"], ".worker")
    os.makedirs(wdir, exist_ok=True)
    jpath = os.path.join(wdir, f"job-{uuid.uuid4().hex[:8]}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(job, f)
    env = dict(os.environ, STELLAI_JOB=jpath, **(env_extra or {}))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=env, cwd=cwd or wdir, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the step took longer than {timeout} s, so StellAI stopped it. Nothing was saved.")
    finally:
        _quiet_remove(jpath)
    lines = (p.stdout or "").splitlines() + (p.stderr or "").splitlines()
    for l in reversed(lines):
        if l.startswith(mark):
            try:
                return json.loads(l[len(mark):])
            except ValueError:
                break
    tail = " / ".join(l.strip() for l in lines[-5:] if l.strip())[:700]
    raise RuntimeError("the program stopped without an answer" + (f": {tail}" if tail else ""))


def _diff_by(before, after, key, fields):
    b = {o[key]: o for o in before}
    a = {o[key]: o for o in after}
    changed = []
    for k in a:
        if k in b:
            d = {f: {"from": b[k].get(f), "to": a[k].get(f)} for f in fields if b[k].get(f) != a[k].get(f)}
            if d:
                changed.append({key: k, **d})
    return {"added": [a[k] for k in a if k not in b][:20], "removed": [k for k in b if k not in a][:20],
            "changed": changed[:25]}


def _check_and_render_stl(stl, goal, facts, refs, views=None, look="solid"):
    """Renders an exported STL with the server's Blender and runs the visual check. (image, info, check text)."""
    if not find_blender():
        return None, {"note": "no pictures: Blender isn't installed on the server (measurements only)"}, None
    with BLENDER["lock"]:
        blender_call("import", timeout=300, path=stl)
        img, info = _blender_render(views, "", look)
    check = _vision_check(img, goal, facts, info.get("layout", ""), refs=refs) if goal else None
    return img, info, check


# ---- CAD Designer: FreeCAD ----------------------------------------------------------------------------
CAD = Workspaces("cad", ".FCStd", "CAD")
CAD_MARK = "@@CAD@@"
CAD_RUNNER_PY = r'''# StellAI CAD runner: one step inside headless FreeCAD (freecadcmd this_file). The job comes as JSON in the
# file named by STELLAI_JOB. Written by stellai_hub.py: edits here are overwritten.
import contextlib
import io
import json
import math
import os
import sys
import traceback

import FreeCAD as App
import Part

MARK = "@@CAD@@"
OUT = sys.stdout


def rnd(v, n=3):
    return [round(float(x), n) for x in v]


def shaped(doc):
    return [o for o in doc.Objects if hasattr(o, "Shape") and not o.Shape.isNull()]


def report(doc, full=True):
    objs, issues = [], {}
    lo, hi = None, None
    for o in shaped(doc):
        s = o.Shape
        bb = s.BoundBox
        used = [p.Name for p in o.InList]
        e = {"name": o.Name, "label": o.Label, "type": o.TypeId, "size_mm": rnd((bb.XLength, bb.YLength, bb.ZLength)),
             "min": rnd((bb.XMin, bb.YMin, bb.ZMin)), "max": rnd((bb.XMax, bb.YMax, bb.ZMax)),
             "solids": len(s.Solids), "faces": len(s.Faces)}
        if used:
            e["used_by"] = used[:5]     # an input to another feature, not a final part
        try:
            e["volume_mm3"] = round(s.Volume, 2)
        except Exception:
            pass
        if not getattr(o, "Visibility", True):
            e["hidden"] = True
        if full and not used:
            found = []
            try:
                if not s.isValid():
                    found.append("shape is not valid (bad geometry)")
            except Exception:
                pass
            if not s.Solids:
                found.append("no solid (only faces or edges): not printable or exportable as a part")
            elif any(not sol.isClosed() for sol in s.Solids):
                found.append("a solid isn't closed")
            if len(s.Solids) > 1:
                found.append(f"{len(s.Solids)} separate solids (fuse them if they're meant to be one part)")
            if found:
                issues[o.Name] = found
        if not used and getattr(o, "Visibility", True):
            lo = rnd((bb.XMin, bb.YMin, bb.ZMin)) if lo is None else [min(a, b) for a, b in zip(lo, (bb.XMin, bb.YMin, bb.ZMin))]
            hi = rnd((bb.XMax, bb.YMax, bb.ZMax)) if hi is None else [max(a, b) for a, b in zip(hi, (bb.XMax, bb.YMax, bb.ZMax))]
        objs.append(e)
    return {"objects": objs, "issues": issues,
            "size_mm": rnd([b - a for a, b in zip(lo, hi)]) if lo else None, "min": lo, "max": hi}


def finals(doc):
    """The parts: shaped, visible, and not an input to another feature."""
    return [o for o in shaped(doc) if not o.InList and getattr(o, "Visibility", True)]


# ---- helpers for step code (millimetres) ----------------------------------------------------------
def namespace(doc):
    V = App.Vector

    def get(name):
        o = doc.getObject(name) or next((x for x in doc.Objects if x.Label == name), None)
        if o is None:
            raise KeyError(f"no object {name!r}; objects: {[x.Name for x in doc.Objects][:30]}")
        return o

    def add(name, shape):
        """Puts a shape in the document as a part called name (replacing one with that name)."""
        old = doc.getObject(name)
        if old is not None:
            doc.removeObject(old.Name)
        o = doc.addObject("Part::Feature", name)
        o.Shape = shape
        return o

    def remove(name):
        doc.removeObject(get(name).Name)

    def box(x, y, z, at=(0, 0, 0)):
        """A box x by y by z mm with its corner at `at`."""
        return Part.makeBox(x, y, z, V(*at))

    def cylinder(r, h, at=(0, 0, 0), axis=(0, 0, 1)):
        return Part.makeCylinder(r, h, V(*at), V(*axis))

    def hole(d, depth, at, axis=(0, 0, -1)):
        """A cylinder to cut a hole of diameter d from point `at` along axis."""
        return Part.makeCylinder(d / 2, depth, V(*at), V(*axis))

    def fillet(shape, r, edges=None):
        return shape.makeFillet(r, edges or shape.Edges)

    def chamfer(shape, d, edges=None):
        return shape.makeChamfer(d, edges or shape.Edges)

    def hide(name):
        get(name).Visibility = False

    return {"App": App, "FreeCAD": App, "Part": Part, "Vector": V, "Placement": App.Placement,
            "Rotation": App.Rotation, "doc": doc, "math": math, "get": get, "add": add, "remove": remove,
            "box": box, "cylinder": cylinder, "hole": hole, "fillet": fillet, "chamfer": chamfer, "hide": hide,
            "__name__": "stellai_step"}


def export(doc, path, fmt):
    parts = finals(doc)
    if not parts:
        raise ValueError("nothing to export: no visible solid parts")
    if fmt == "stl":
        import Mesh
        import MeshPart
        meshes = [MeshPart.meshFromShape(Shape=o.Shape, LinearDeflection=0.05, AngularDeflection=0.3) for o in parts]
        m = meshes[0]
        for extra in meshes[1:]:
            m.addMesh(extra)
        m.write(path)
    elif fmt in ("step", "stp"):
        import Import
        Import.export(parts, path)
    elif fmt == "brep":
        Part.makeCompound([o.Shape for o in parts]).exportBrep(path)
    elif fmt in ("iges", "igs"):
        Part.makeCompound([o.Shape for o in parts]).exportIges(path)
    else:
        raise ValueError("format must be stl, step, brep or iges")


def main():
    with open(os.environ["STELLAI_JOB"], encoding="utf-8") as f:
        job = json.load(f)
    out = {"ok": False}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            doc = App.openDocument(job["open"]) if job.get("open") else App.newDocument("Model")
            if job.get("import"):
                src = job["import"]
                ext = src.rsplit(".", 1)[-1].lower()
                if ext in ("step", "stp", "iges", "igs"):
                    import Import
                    Import.insert(src, doc.Name)
                elif ext == "brep":
                    o = doc.addObject("Part::Feature", "Imported")
                    o.Shape = Part.read(src)
                elif ext == "stl":     # a mesh: turned into a solid so it can be cut, measured and exported
                    import Mesh
                    mesh = Mesh.Mesh(src)
                    shell = Part.Shape()
                    shell.makeShapeFromMesh(mesh.Topology, 0.05)
                    solid = Part.makeSolid(shell)
                    o = doc.addObject("Part::Feature", "Imported")
                    o.Shape = solid.removeSplitter() if solid.isValid() else shell
                else:
                    raise ValueError(f"can't import .{ext} files")
                doc.recompute()
            if job.get("code"):
                out["before"] = report(doc, full=False)
                code = compile(job["code"], "<step>", "exec")
                exec(code, namespace(doc))
            doc.recompute()
            bad = [o.Name for o in doc.Objects if "Invalid" in o.State or "Error" in o.State]
            out["after"] = report(doc, full=True)
            if bad:
                out["after"]["recompute_errors"] = bad
            if job.get("save"):
                doc.saveAs(job["save"])
            for path, fmt in job.get("exports", []):
                export(doc, path, fmt)
        out["ok"] = True
    except Exception as e:
        tb = traceback.extract_tb(sys.exc_info()[2])
        out.update(error=f"{type(e).__name__}: {e}"[:1500],
                   where=[f"line {f.lineno}: {(f.line or '').strip()}" for f in tb if f.filename == "<step>"][-3:])
    out["output"] = buf.getvalue()[-3000:]
    OUT.write(MARK + json.dumps(out) + "\n")
    OUT.flush()


main()
os._exit(0)
'''
CAD_IMPORTS = {"FreeCAD", "Part", "Sketcher", "PartDesign", "Draft", "Mesh", "MeshPart", "math", "random",
               "itertools", "functools", "collections", "statistics", "numpy"}
CAD_BAD_ATTRS = {"openDocument", "newDocument", "closeDocument", "loadFile", "exportStep", "exportStl", "exportBrep",
                 "exportIges", "read", "export", "insert", "addModule", "runCommand", "open", "importStep", "setActiveDocument"}
CAD_FILE_EXTS = ("fcstd", "step", "stp", "stl", "iges", "igs", "brep")


def find_freecad():
    """The command that runs headless FreeCAD, as a list, or []."""
    env = os.environ.get("STELLAI_FREECAD", "")
    if env and os.path.isfile(env):
        return [env]
    appimage = os.path.expanduser(os.path.join("~", "llm", "freecad", "FreeCAD.AppImage"))
    if os.path.isfile(appimage) and os.access(appimage, os.X_OK):
        return [appimage, "freecadcmd"]
    for n in ("freecadcmd", "FreeCADCmd", "freecad.cmd"):
        w = shutil.which(n)
        if w:
            return [w]
    if os.name == "nt":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            for exe in ("FreeCADCmd.exe", "freecadcmd.exe"):
                found = sorted(glob.glob(os.path.join(root, "FreeCAD*", "bin", exe)), reverse=True)
                if found:
                    return [found[0]]
    for c in ("/usr/bin/freecadcmd", "/usr/bin/FreeCADCmd", "/snap/bin/freecad.cmd"):
        if os.path.isfile(c):
            return [c]
    return []


def _cad(job, timeout=300):
    cmd = find_freecad()
    if not cmd:
        raise RuntimeError("FreeCAD isn't installed on the server. Linux: re-run stellai-setup.sh and say yes to "
                           "FreeCAD. Windows: install it from freecad.org. Then try again.")
    script = os.path.join(BLENDER["root"], ".worker", "stellai_cad.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write(CAD_RUNNER_PY)
    return _run_marked(cmd + [script], CAD_MARK, job, timeout)


def _cad_summary(rep):
    return {"parts": [o for o in rep.get("objects", []) if not o.get("used_by")][:40],
            "inputs_used_by_other_features": len([o for o in rep.get("objects", []) if o.get("used_by")]),
            "overall_size_mm": rep.get("size_mm"), "min": rep.get("min"), "max": rep.get("max"),
            "problems": rep.get("issues") or {}, "recompute_errors": rep.get("recompute_errors")}


def tool_cad_project(args):
    action = str(args.get("action", "status")).strip().lower()
    if action == "status":
        return {"freecad": " ".join(find_freecad()) or "not installed", "blender_for_pictures": bool(find_blender()),
                "inbox_folder": _inbox(),
                "files_you_can_import": [f for f in sorted(os.listdir(_inbox()))
                                         if f.rsplit(".", 1)[-1].lower() in CAD_FILE_EXTS][:40],
                "workspaces": CAD.list(), "active": _safe(lambda: CAD.name(""))}, "Checked CAD workspaces"
    if action == "new":
        name = CAD.start(args.get("name"))
        v, path = CAD.next(name)
        job = {"save": path}
        if args.get("file"):
            src = _pick_inbox_file(args.get("file"), CAD_FILE_EXTS)
            if src.lower().endswith(".fcstd"):
                job["open"] = src
            else:
                job["import"] = src
        res = _cad(job)
        if not res.get("ok"):
            shutil.rmtree(CAD.dir(name), ignore_errors=True)
            raise RuntimeError(res.get("error") or "FreeCAD couldn't start the workspace")
        CAD.record(name, v, f"start: {args.get('file') or 'empty'}")
        CAD.activate(name)
        return {"workspace": name, "version": v, "design": _cad_summary(res.get("after", {}))}, \
            f"Started CAD workspace {name} (v{v})"
    if action in ("add_reference", "references", "clear_references"):
        return CAD.refs_action(action, args)
    name = CAD.name(args.get("name"))
    if action == "open":
        CAD.activate(name)
        return {"workspace": name, "latest_version": CAD.latest(name), "recent": CAD.history(name)[-8:]}, f"Opened {name}"
    if action == "history":
        return {"workspace": name, "versions_on_disk": CAD.versions(name), "history": CAD.history(name)[-25:]}, \
            f"History of {name}"
    if action == "revert":
        target = _int_version(args.get("version"))
        if target not in CAD.versions(name):
            raise ValueError(f"version {target} isn't on disk; available: {CAD.versions(name)[-15:]}")
        v, path = CAD.next(name)
        shutil.copyfile(CAD.path(name, target), path)
        CAD.record(name, v, f"back to v{target}")
        return {"workspace": name, "version": v, "same_as": target}, f"Went back to v{target} (saved as v{v})"
    if action == "export":
        fmt = str(args.get("format", "step")).lower().strip(". ")
        v = CAD.latest(name)
        if fmt == "blender":     # hand the part to the 3D Modeler
            out = os.path.join(_inbox(), f"{name}-v{v:04d}.stl")
            fmt_real = "stl"
        else:
            if fmt not in ("step", "stl", "fcstd", "brep", "iges"):
                raise ValueError("format must be step, stl, fcstd, brep, iges or blender")
            os.makedirs(CAD.dir(name, "exports"), exist_ok=True)
            out = CAD.dir(name, "exports", f"{name}-v{v:04d}.{fmt}")
            fmt_real = fmt
        if fmt_real == "fcstd":
            shutil.copyfile(CAD.path(name, v), out)
        else:
            res = _cad({"open": CAD.path(name, v), "exports": [[out, fmt_real]]})
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "export failed")
        note = ("It's in the 3D inbox now: in the 3D Modeler, start a workspace with this file."
                if fmt == "blender" else "The file is in this folder on the server computer.")
        return {"workspace": name, "version": v, "file": out, "bytes": os.path.getsize(out), "note": note}, \
            f"Exported {name} v{v} as {fmt}"
    raise ValueError("action must be status, new, open, history, revert, export, add_reference, references or "
                     "clear_references")


def _safe(fn):
    try:
        return fn()
    except (ValueError, OSError):
        return None


def _int_version(v):
    try:
        return int(str(v).lstrip("vV"))
    except ValueError:
        raise ValueError("give the version number, e.g. 7")


def tool_cad_step(args):
    code, goal = str(args.get("code", "")), str(args.get("goal", "")).strip()
    if not goal or not code.strip():
        raise ValueError("give goal (with millimetres) and code")
    probs = check_bpy_code(code, allowed=CAD_IMPORTS, extra_bad=CAD_BAD_ATTRS)
    if probs:
        return {"ok": False, "error": "the code was not run: " + "; ".join(probs)}, "Step blocked"
    name = CAD.name(args.get("workspace"))
    base = CAD.latest(name)
    v, path = CAD.next(name)
    stl = os.path.join(BLENDER["root"], ".worker", f"cad-{uuid.uuid4().hex[:8]}.stl")
    res = _cad({"open": CAD.path(name, base), "code": code, "save": path, "exports": [[stl, "stl"]]},
               timeout=max(30, min(600, int(args.get("timeout") or 300))))
    if not res.get("ok"):
        _quiet_remove(path)
        _quiet_remove(stl)
        return {"ok": False, "error": res.get("error"), "where": res.get("where"), "output": res.get("output", ""),
                "note": f"Nothing was saved; the design is still v{base}."}, "Step failed"
    fields = ("label", "size_mm", "min", "volume_mm3", "solids", "faces", "hidden", "used_by")
    diff = _diff_by(res["before"]["objects"], res["after"]["objects"], "name", fields)
    if not (diff["added"] or diff["removed"] or diff["changed"]):
        _quiet_remove(path)
        _quiet_remove(stl)
        return {"ok": False, "error": "the code ran but nothing in the design changed", "output": res.get("output", ""),
                "note": f"Nothing was saved; the design is still v{base}."}, "Step changed nothing"
    CAD.record(name, v, goal)
    out = {"version": v, "previous": base, "changes": diff, "design": _cad_summary(res["after"]),
           "output": res.get("output", "")[-1500:]}
    if os.path.exists(stl):
        try:
            check = str(args.get("check", True)).lower() not in ("false", "0", "no")
            img, info, vc = _check_and_render_stl(stl, goal if check else "", diff, CAD.ref_images(name),
                                                  args.get("views") if isinstance(args.get("views"), list) else None)
            if img:
                out.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
            if vc:
                out["visual_check"] = vc
            if info.get("note"):
                out["note"] = info["note"]
        except RuntimeError as e:
            out["render_error"] = str(e)
        _quiet_remove(stl)
    return out, f"Saved {name} v{v}"


def tool_cad_inspect(args):
    name = CAD.name(args.get("workspace"))
    v = CAD.latest(name)
    want = bool(args.get("render"))
    stl = os.path.join(BLENDER["root"], ".worker", f"cad-{uuid.uuid4().hex[:8]}.stl")
    res = _cad({"open": CAD.path(name, v), "exports": [[stl, "stl"]] if want else []})
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "FreeCAD couldn't read the design")
    out = {"workspace": name, "version": v, "design": _cad_summary(res["after"])}
    if want and os.path.exists(stl):
        refs = CAD.ref_images(name)
        goal = "compare the design as it is now with the reference pictures" if refs else ""
        try:
            img, info, vc = _check_and_render_stl(stl, goal, {"size_mm": res["after"].get("size_mm")}, refs,
                                                  args.get("views") if isinstance(args.get("views"), list) else None)
            if img:
                out.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
            if vc:
                out["comparison"] = vc
            if info.get("note"):
                out["note"] = info["note"]
        except RuntimeError as e:
            out["render_error"] = str(e)
        _quiet_remove(stl)
    return out, f"Inspected {name} v{v}"


