# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- 3D Modeler: Blender on the server -------------------------------------------------------------
# Stella edits 3D models in a hidden Blender that stays running. Every change is one small bpy script: the
# server opens the newest saved version, runs it, saves a new numbered version (nothing is overwritten), renders
# the model from several angles and measures it, then the vision add-on checks the pictures against the goal.

import glob
import queue
import shutil

BLENDER = {"arg": "", "root": "", "exe": "", "proc": None, "q": None, "lock": threading.RLock(), "last": 0.0,
           "info": {}, "log": [], "next": 1, "watcher": False}
BLENDER_MARK = "@@STELLAI@@"
BLENDER_IDLE_S = 15 * 60          # Blender leaves memory after this long without a 3D request
BLENDER_KEEP = 40                 # newest versions kept per workspace (plus the first)
MODEL_EXTS = ("blend", "glb", "gltf", "vrm", "fbx", "obj", "stl")
WS_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
BLENDER_VIEWS = ("front", "right", "back", "left", "top", "three_quarter")

BLENDER_WORKER_PY = r'''# StellAI Blender worker. Runs inside Blender ("blender -b -P this_file") and takes one JSON command per line
# on stdin. Written by stellai_hub.py at start-up: edits here are overwritten.
import bpy, bmesh, mathutils, math, json, sys, io, os, traceback, contextlib, shutil, tempfile
from mathutils import Vector, Matrix, Euler, Quaternion

MARK = "@@STELLAI@@"
OUT = sys.stdout


def reply(msg):
    OUT.write(MARK + json.dumps(msg) + "\n")
    OUT.flush()


def rnd(v, n=4):
    return [round(float(x), n) for x in v]


def has_op(cls_name):
    return hasattr(bpy.types, cls_name)


def has_vrm():
    return has_op("IMPORT_SCENE_OT_vrm")


# ---- helpers that step code can use ---------------------------------------------------------
def obj(name):
    """The object called name, with a helpful error listing similar names."""
    o = bpy.data.objects.get(name)
    if o is None:
        names = sorted(bpy.data.objects.keys())
        near = [n for n in names if name.lower() in n.lower() or n.lower() in name.lower()][:8]
        raise KeyError(f"no object named {name!r}; " + (f"similar: {near}" if near else f"objects: {names[:25]}"))
    return o


def bbox(o):
    """World-space (min, max) corners of an object, modifiers included."""
    o = obj(o) if isinstance(o, str) else o
    dg = bpy.context.evaluated_depsgraph_get()
    e = o.evaluated_get(dg)
    pts = [e.matrix_world @ Vector(c) for c in e.bound_box]
    return (Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts))),
            Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts))))


def new_object(name, bm, collection=None):
    """Turns a bmesh into a new mesh object linked to the scene (the bmesh is freed)."""
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    (collection or bpy.context.scene.collection).objects.link(o)
    return o


def material(name, color=(0.8, 0.8, 0.8), emission=None, strength=1.0, metallic=0.0, roughness=0.5):
    """Gets or makes a Principled material. Colours are 0-1 RGB; emission makes it glow."""
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    p = m.node_tree.nodes.get("Principled BSDF")
    if p:
        p.inputs["Base Color"].default_value = (*color[:3], 1.0)
        p.inputs["Metallic"].default_value = metallic
        p.inputs["Roughness"].default_value = roughness
        key = "Emission Color" if "Emission Color" in p.inputs else "Emission"
        if emission is not None:
            p.inputs[key].default_value = (*emission[:3], 1.0)
            if "Emission Strength" in p.inputs:
                p.inputs["Emission Strength"].default_value = strength
    m.diffuse_color = (*(emission or color)[:3], 1.0)   # the colour the quick renders show
    return m


def assign(o, m, replace=True):
    """Puts material m on object o (replacing its materials unless replace=False)."""
    o = obj(o) if isinstance(o, str) else o
    if replace:
        o.data.materials.clear()
    o.data.materials.append(m)


def bone_world(arm, bone):
    """World-space (head, tail) of a bone in the current pose."""
    a = obj(arm) if isinstance(arm, str) else arm
    if a.type != "ARMATURE":
        raise TypeError(f"{a.name} is not an armature")
    b = a.pose.bones.get(bone)
    if b is None:
        raise KeyError(f"no bone {bone!r} in {a.name}; bones: {[x.name for x in a.data.bones][:60]}")
    return a.matrix_world @ b.head, a.matrix_world @ b.tail


def parent_to_bone(o, arm, bone):
    """Makes o follow an armature bone, keeping where it is now."""
    o = obj(o) if isinstance(o, str) else o
    a = obj(arm) if isinstance(arm, str) else arm
    if bone not in a.data.bones:
        raise KeyError(f"no bone {bone!r} in {a.name}; bones: {[x.name for x in a.data.bones][:60]}")
    mw = o.matrix_world.copy()
    o.parent = a
    o.parent_type = "BONE"
    o.parent_bone = bone
    bpy.context.view_layer.update()
    o.matrix_world = mw


def apply_transform(o):
    """Bakes rotation and scale into the mesh (like Ctrl+A), keeping the location."""
    o = obj(o) if isinstance(o, str) else o
    if o.type != "MESH":
        raise TypeError("apply_transform works on meshes")
    loc, rot, sca = o.matrix_basis.decompose()
    o.data.transform(Matrix.LocRotScale(None, rot, sca))
    o.matrix_basis = Matrix.Translation(loc)
    o.data.update()


def namespace():
    return {"bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math, "Vector": Vector, "Matrix": Matrix,
            "Euler": Euler, "Quaternion": Quaternion, "radians": math.radians, "degrees": math.degrees,
            "pi": math.pi, "scene": bpy.context.scene, "obj": obj, "bbox": bbox, "new_object": new_object,
            "material": material, "assign": assign, "bone_world": bone_world, "parent_to_bone": parent_to_bone,
            "apply_transform": apply_transform, "__name__": "stellai_step"}


# ---- scene report -----------------------------------------------------------------------------
def visible(o):
    return not o.hide_render and o.visible_get()


def light_entry(o):
    e = {"name": o.name, "type": o.type, "loc": rnd(o.matrix_world.translation),
         "rot_deg": rnd([math.degrees(a) for a in o.matrix_world.to_euler()], 2), "scale": rnd(o.scale)}
    if o.parent:
        e["parent"] = o.parent.name + (f" (bone {o.parent_bone})" if o.parent_type == "BONE" else "")
    if o.type in ("MESH", "CURVE", "SURFACE", "META", "FONT"):
        lo, hi = bbox(o)
        e["size"] = rnd(hi - lo)
        e["min"], e["max"] = rnd(lo), rnd(hi)
    if o.type == "MESH":
        e["verts"] = len(o.data.vertices)
        e["mats"] = [m.name for m in o.data.materials if m]
    if not visible(o):
        e["hidden"] = True
    return e


def mesh_issues(o):
    out = []
    bm = bmesh.new()
    bm.from_mesh(o.data)
    multi = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    wire = sum(1 for e in bm.edges if not e.link_faces)
    loose = sum(1 for v in bm.verts if not v.link_edges)
    zero = sum(1 for f in bm.faces if f.calc_area() < 1e-10)
    ngons = sum(1 for f in bm.faces if len(f.verts) > 4)
    bm.free()
    if multi:
        out.append(f"{multi} edges shared by 3+ faces (non-manifold)")
    if wire:
        out.append(f"{wire} loose edges")
    if loose:
        out.append(f"{loose} loose vertices")
    if zero:
        out.append(f"{zero} zero-area faces")
    if ngons:
        out.append(f"{ngons} n-gons (5+ sides)")
    if o.data.polygons and not o.data.uv_layers:
        out.append("no UV map")
    if any(s < 0 for s in o.scale):
        out.append("negative scale (normals may look inside out)")
    elif any(abs(s - 1) > 1e-4 for s in o.scale):
        out.append("unapplied scale")
    return out


def report(msg):
    full = bool(msg.get("full", True))
    only = set(msg.get("names") or [])
    dg = bpy.context.evaluated_depsgraph_get()
    objs, tris, issues = [], 0, {}
    lo_all, hi_all = None, None
    for o in bpy.context.scene.objects:
        if o.name.startswith("__stellai"):
            continue
        e = light_entry(o)
        if o.type == "MESH":
            try:
                me = o.evaluated_get(dg).to_mesh()
                me.calc_loop_triangles()
                e["tris"] = len(me.loop_triangles)
                o.evaluated_get(dg).to_mesh_clear()
            except RuntimeError:
                e["tris"] = 0
            tris += e["tris"]
            e["modifiers"] = [m.type.lower() for m in o.modifiers]
            if full and (not only or o.name in only):
                found = mesh_issues(o)
                if found:
                    issues[o.name] = found
        if o.type == "ARMATURE":
            e["bones"] = len(o.data.bones)
        if "min" in e and visible(o):
            lo = Vector(e["min"]); hi = Vector(e["max"])
            lo_all = lo if lo_all is None else Vector(tuple(map(min, lo_all, lo)))
            hi_all = hi if hi_all is None else Vector(tuple(map(max, hi_all, hi)))
        objs.append(e)
    us = bpy.context.scene.unit_settings
    return {"objects": objs, "issues": issues, "tris": tris, "count": len(objs),
            "scene_size": rnd(hi_all - lo_all) if lo_all is not None else None,
            "scene_min": rnd(lo_all) if lo_all is not None else None,
            "scene_max": rnd(hi_all) if hi_all is not None else None,
            "units": f"{us.system.lower()}, scale {us.scale_length:g}", "file": bpy.data.filepath}


# ---- renders ------------------------------------------------------------------------------------
GLYPHS = {
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    " ": ["00000"] * 7,
}
LABELS = {"front": "FRONT", "right": "RIGHT", "back": "BACK", "left": "LEFT", "top": "TOP", "three_quarter": "3/4"}
ORTHO = {  # camera direction from the centre, rotation, which size axes fill the frame
    "front": ((0, -1, 0), (90, 0, 0), (0, 2)),
    "back": ((0, 1, 0), (90, 0, 180), (0, 2)),
    "right": ((1, 0, 0), (90, 0, 90), (1, 2)),
    "left": ((-1, 0, 0), (90, 0, -90), (1, 2)),
    "top": ((0, 0, 1), (0, 0, 0), (0, 1)),
}


def draw_label(arr, text, x0, y0, s=3):
    """Draws text into a top-down RGBA float array: black shadow, then white."""
    for dx, dy, col in ((1, 1, (0, 0, 0, 1)), (0, 0, (1, 1, 1, 1))):
        x = x0
        for ch in text:
            g = GLYPHS.get(ch, GLYPHS[" "])
            for r, row in enumerate(g):
                for c, bit in enumerate(row):
                    if bit == "1":
                        ys, xs = y0 + (r + dy) * s, x + (c + dx) * s
                        arr[ys:ys + s, xs:xs + s] = col
            x += 6 * s


def targets(focus):
    objs = [o for o in bpy.context.scene.objects if visible(o) and not o.name.startswith("__stellai")
            and o.type in ("MESH", "CURVE", "SURFACE", "META", "FONT")]
    if focus:
        f = obj(focus)
        objs = [o for o in objs if o == f or f in o.parent_recursive] or [f]
    return objs


def render(msg):
    import numpy as np
    scene = bpy.context.scene
    views = [v for v in (msg.get("views") or ["front", "right", "back", "three_quarter"]) if v in LABELS][:6]
    res = max(256, min(1024, int(msg.get("res") or 512)))
    objs = targets(msg.get("focus"))
    if not objs:
        raise ValueError("nothing visible to render")
    lo, hi = bbox(objs[0])
    for o in objs[1:]:
        a, b = bbox(o)
        lo, hi = Vector(tuple(map(min, lo, a))), Vector(tuple(map(max, hi, b)))
    centre, size = (lo + hi) / 2, hi - lo
    span = max(size.length, 0.01)
    made = []
    try:
        cam_data = bpy.data.cameras.new("__stellai_cam")
        cam = bpy.data.objects.new("__stellai_cam", cam_data)
        scene.collection.objects.link(cam)
        made.append(cam)
        sun_data = bpy.data.lights.new("__stellai_sun", "SUN")
        sun_data.energy = 3.0
        sun = bpy.data.objects.new("__stellai_sun", sun_data)
        sun.rotation_euler = Euler((math.radians(50), math.radians(10), math.radians(30)))
        scene.collection.objects.link(sun)
        made.append(sun)
        scene.camera = cam
        r = scene.render
        r.resolution_x = r.resolution_y = res
        r.resolution_percentage = 100
        r.image_settings.file_format = "PNG"
        r.film_transparent = False
        if scene.world is None:
            scene.world = bpy.data.worlds.new("__stellai_world")
        scene.world.color = (0.16, 0.17, 0.19)
        engine = setup_engine(scene, msg.get("look") or "solid")
        tmp = tempfile.mkdtemp(prefix="stellai-r-")
        tiles = []
        for v in views:
            if v == "three_quarter":
                d = Vector((0.75, -1.0, 0.45)).normalized()
                cam_data.type = "PERSP"
                cam_data.lens = 50
                fov = 2 * math.atan(18 / 50)
                dist = (span / 2) / math.sin(fov / 2) * 1.08
                cam.location = centre + d * dist
                cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
            else:
                d, rot, axes = ORTHO[v]
                cam_data.type = "ORTHO"
                cam_data.ortho_scale = max(size[axes[0]], size[axes[1]], 0.01) * 1.12
                cam.location = centre + Vector(d) * (span * 2 + 1)
                cam.rotation_euler = Euler([math.radians(a) for a in rot])
            cam_data.clip_start = 0.001
            cam_data.clip_end = span * 6 + 10
            path = os.path.join(tmp, v + ".png")
            r.filepath = path
            try:
                bpy.ops.render.render(write_still=True)
            except RuntimeError:
                if engine == "CYCLES":
                    raise
                engine = setup_engine(scene, "cycles")
                bpy.ops.render.render(write_still=True)
            img = bpy.data.images.load(path)
            px = np.empty(res * res * 4, dtype=np.float32)
            img.pixels.foreach_get(px)
            bpy.data.images.remove(img)
            tile = np.flipud(px.reshape(res, res, 4)).copy()
            draw_label(tile, LABELS[v], 8, 8)
            tiles.append(tile)
        cols = 1 if len(tiles) == 1 else 2
        rows = (len(tiles) + cols - 1) // cols
        gap = 4
        W, H = cols * res + (cols - 1) * gap, rows * res + (rows - 1) * gap
        sheet = np.zeros((H, W, 4), dtype=np.float32)
        sheet[..., 3] = 1
        for i, t in enumerate(tiles):
            y, x = (i // cols) * (res + gap), (i % cols) * (res + gap)
            sheet[y:y + res, x:x + res] = t
        out = bpy.data.images.new("__stellai_sheet", W, H, alpha=True)
        out.pixels.foreach_set(np.flipud(sheet).ravel())
        out.filepath_raw = msg["out"]
        out.file_format = "PNG"
        out.save()
        bpy.data.images.remove(out)
        shutil.rmtree(tmp, ignore_errors=True)
        layout = ", ".join(f"{'top' if i < cols else 'bottom'}-{'left' if i % cols == 0 else 'right'}: {LABELS[v]}"
                           for i, v in enumerate(views)) if cols == 2 else LABELS[views[0]]
        return {"engine": engine, "views": views, "layout": layout, "centre": rnd(centre), "size": rnd(size)}
    finally:
        for o in made:
            data = o.data
            bpy.data.objects.remove(o, do_unlink=True)
            if isinstance(data, bpy.types.Camera):
                bpy.data.cameras.remove(data)
            elif isinstance(data, bpy.types.Light):
                bpy.data.lights.remove(data)


def setup_engine(scene, look):
    if look == "material":
        for e in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            try:
                scene.render.engine = e
                return e
            except TypeError:
                pass
    if look != "cycles":
        scene.render.engine = "BLENDER_WORKBENCH"
        sh = scene.display.shading
        sh.light = "STUDIO"
        sh.color_type = "TEXTURE"     # image textures where there are some, material colours elsewhere
        sh.show_object_outline = True
        sh.show_cavity = True
        try:
            scene.view_settings.view_transform = "Standard"
        except TypeError:
            pass
        return "BLENDER_WORKBENCH"
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 12
    try:
        scene.cycles.use_denoising = False
    except AttributeError:
        pass
    return "CYCLES"


# ---- files --------------------------------------------------------------------------------------
def op_new(msg):
    bpy.ops.wm.read_homefile(use_empty=True)
    return {}


def op_open(msg):
    bpy.ops.wm.open_mainfile(filepath=msg["path"], load_ui=False)
    return {"file": bpy.data.filepath}


def op_import(msg):
    path = msg["path"]
    ext = path.rsplit(".", 1)[-1].lower()
    note = ""
    keep = bool(msg.get("keep"))          # add to the open scene instead of starting a new one
    before = set(bpy.data.objects.keys())
    if not keep:
        bpy.ops.wm.read_homefile(use_empty=True)
    if ext in ("glb", "gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == "vrm":
        if has_vrm():
            bpy.ops.import_scene.vrm(filepath=path)
        else:
            tmp = os.path.join(tempfile.mkdtemp(prefix="stellai-i-"), "model.glb")
            shutil.copyfile(path, tmp)
            bpy.ops.import_scene.gltf(filepath=tmp)
            note = ("The VRM add-on isn't installed in this Blender, so the file came in as plain glTF: meshes, "
                    "textures and the armature are there, but VRM settings and MToon materials aren't.")
    elif ext == "fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == "obj":
        (bpy.ops.wm.obj_import if has_op("WM_OT_obj_import") else bpy.ops.import_scene.obj)(filepath=path)
    elif ext == "stl":
        (bpy.ops.wm.stl_import if has_op("WM_OT_stl_import") else bpy.ops.import_mesh.stl)(filepath=path)
    else:
        raise ValueError(f"can't import .{ext} files")
    added = [o for o in bpy.data.objects if o.name not in before]
    name = msg.get("rename") or ""
    if name and keep:
        meshes = [o for o in added if o.type == "MESH"]
        for i, o in enumerate(meshes):
            o.name = name if i == 0 else f"{name}.{i:03d}"
            o.data.name = o.name
        for o in added:                   # empties the glTF importer adds around the mesh
            if o.type == "EMPTY" and not o.children:
                bpy.data.objects.remove(o, do_unlink=True)
    return {"note": note, "added": [o.name for o in bpy.data.objects if o.name not in before]}


def op_save(msg):
    bpy.ops.wm.save_as_mainfile(filepath=msg["path"], compress=True)
    return {"file": bpy.data.filepath}


def op_export(msg):
    path, fmt = msg["path"], msg["format"]
    if fmt == "glb":
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB")
    elif fmt == "fbx":
        bpy.ops.export_scene.fbx(filepath=path)
    elif fmt == "obj":
        (bpy.ops.wm.obj_export if has_op("WM_OT_obj_export") else bpy.ops.export_scene.obj)(filepath=path)
    elif fmt == "stl":
        (bpy.ops.wm.stl_export if has_op("WM_OT_stl_export") else bpy.ops.export_mesh.stl)(filepath=path)
    elif fmt == "vrm":
        if not has_op("EXPORT_SCENE_OT_vrm"):
            raise ValueError("the VRM add-on isn't installed in the server's Blender; export glb instead, or install "
                             "'VRM Add-on for Blender' in that Blender")
        bpy.ops.export_scene.vrm(filepath=path)
    else:
        raise ValueError("format must be glb, fbx, obj, stl or vrm")
    if not os.path.exists(path):
        raise RuntimeError("the exporter didn't write a file")
    return {"bytes": os.path.getsize(path)}


def op_run(msg):
    code = compile(msg["code"], "<step>", "exec")
    exec(code, namespace())
    bpy.context.view_layer.update()
    return {}


def step_trace():
    tb = traceback.extract_tb(sys.exc_info()[2])
    lines = [f"line {f.lineno}: {(f.line or '').strip()}" for f in tb if f.filename == "<step>"]
    return lines[-3:]


OPS = {"ping": lambda m: {}, "new": op_new, "open": op_open, "import": op_import, "save": op_save,
       "export": op_export, "run": op_run, "report": report, "render": render}


def main():
    reply({"id": 0, "ok": True, "version": bpy.app.version_string, "vrm": has_vrm()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("op") == "quit":
            break
        mid = msg.get("id")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                res = OPS[msg["op"]](msg)
            reply({"id": mid, "ok": True, **res, "output": buf.getvalue()[-3000:]})
        except Exception as e:
            reply({"id": mid, "ok": False, "error": f"{type(e).__name__}: {e}"[:1500], "where": step_trace(),
                   "output": buf.getvalue()[-3000:]})


main()
'''


def find_blender():
    cands = [BLENDER.get("arg") or "", os.environ.get("STELLAI_BLENDER", ""),
             os.path.expanduser(os.path.join("~", "llm", "blender", "blender")), shutil.which("blender") or ""]
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
        for root in roots:
            if root:
                cands += sorted(glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe")),
                                reverse=True)
    cands += ["/snap/bin/blender", "/usr/bin/blender", "/usr/local/bin/blender",
              "/var/lib/flatpak/exports/bin/org.blender.Blender"]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def _blender_alive():
    p = BLENDER["proc"]
    return p is not None and p.poll() is None


def _blender_stop():
    p = BLENDER["proc"]
    BLENDER["proc"] = None
    if p is None:
        return
    try:
        if p.poll() is None:
            try:
                p.stdin.write('{"op": "quit"}\n')
                p.stdin.flush()
                p.wait(timeout=10)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                p.kill()
    except OSError:
        pass


def _blender_watch():
    while True:
        time.sleep(60)
        if _blender_alive() and time.time() - BLENDER["last"] > BLENDER_IDLE_S and BLENDER["lock"].acquire(False):
            try:
                _blender_stop()
                log("Blender went to sleep (idle)")
            finally:
                BLENDER["lock"].release()


def _blender_start():
    exe = find_blender()
    if not exe:
        raise RuntimeError("Blender isn't installed on the server. Linux: re-run stellai-setup.sh and say yes to "
                           "Blender. Windows: install Blender from blender.org. Then try again.")
    wdir = os.path.join(BLENDER["root"], ".worker")
    os.makedirs(wdir, exist_ok=True)
    script = os.path.join(wdir, "stellai_blender_worker.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(BLENDER_WORKER_PY)
    proc = subprocess.Popen([exe, "-b", "--disable-autoexec", "--python-exit-code", "1", "-P", script],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=wdir,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=0x08000000 if os.name == "nt" else 0)   # no console window on Windows
    q = queue.Queue()

    def reader():
        for line in proc.stdout:
            if line.startswith(BLENDER_MARK):
                try:
                    q.put(json.loads(line[len(BLENDER_MARK):]))
                except ValueError:
                    pass
            else:
                BLENDER["log"].append(line.rstrip()[:300])
                del BLENDER["log"][:-40]
        q.put(None)

    BLENDER.update(proc=proc, q=q, exe=exe, log=[])
    threading.Thread(target=reader, daemon=True, name="blender-out").start()
    first = _blender_wait(0, 120)
    BLENDER["info"] = {"version": first.get("version", ""), "vrm": bool(first.get("vrm"))}
    log(f"Blender {BLENDER['info']['version']} ready for 3D modeling ({exe})")
    if not BLENDER["watcher"]:
        BLENDER["watcher"] = True
        threading.Thread(target=_blender_watch, daemon=True, name="blender-idle").start()


def _blender_wait(mid, timeout):
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            _blender_stop()
            raise RuntimeError(f"Blender took longer than {int(timeout)} s, so StellAI stopped it. "
                               "Nothing from this step was saved.")
        try:
            msg = BLENDER["q"].get(timeout=left)
        except queue.Empty:
            continue
        if msg is None:
            BLENDER["proc"] = None
            tail = " / ".join(l for l in BLENDER["log"][-4:] if l.strip())
            raise RuntimeError("Blender stopped unexpectedly" + (f": {tail}" if tail else "") +
                               ". Nothing from this step was saved.")
        if msg.get("id") == mid:
            return msg


class BlenderStepError(RuntimeError):
    def __init__(self, msg):
        super().__init__(msg.get("error") or "Blender error")
        self.detail = msg


def blender_call(op, timeout=180, **kw):
    """Runs one worker command and returns its answer. Raises BlenderStepError when Blender reports an error."""
    with BLENDER["lock"]:
        if not _blender_alive():
            _blender_start()
        BLENDER["last"] = time.time()
        mid = BLENDER["next"]
        BLENDER["next"] += 1
        try:
            BLENDER["proc"].stdin.write(json.dumps(dict(kw, id=mid, op=op)) + "\n")
            BLENDER["proc"].stdin.flush()
        except (OSError, ValueError):
            _blender_stop()
            raise RuntimeError("couldn't reach Blender; try again")
        msg = _blender_wait(mid, timeout)
        BLENDER["last"] = time.time()
        if not msg.get("ok"):
            raise BlenderStepError(msg)
        return msg


# ---- Workspaces: numbered versions of one model ---------------------------------------------------
def _ws(name):
    name = str(name or "").strip().lower()
    if not name:
        try:
            with open(os.path.join(BLENDER["root"], "active.txt"), encoding="utf-8") as f:
                name = f.read().strip()
        except OSError:
            name = ""
        if not name:
            raise ValueError("no 3D workspace yet: start one with blender_project action new")
    if not WS_NAME.fullmatch(name):
        raise ValueError("workspace names use lowercase letters, digits and dashes, e.g. vela-halo")
    return name


def _ws_dir(name, *parts):
    return os.path.join(BLENDER["root"], "workspaces", name, *parts)


def _ws_versions(name):
    out = []
    for f in os.listdir(_ws_dir(name, "versions")) if os.path.isdir(_ws_dir(name, "versions")) else []:
        m = re.fullmatch(r"v(\d{4,})\.blend", f)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def _ws_file(name, v):
    return _ws_dir(name, "versions", f"v{v:04d}.blend")


def _ws_history(name):
    try:
        with open(_ws_dir(name, "history.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _ws_record(name, v, what):
    hist = _ws_history(name)
    hist.append({"version": v, "what": str(what)[:300], "at": int(time.time())})
    with open(_ws_dir(name, "history.json"), "w", encoding="utf-8") as f:
        json.dump(hist[-500:], f, indent=1)
    keep = set(_ws_versions(name)[-BLENDER_KEEP:]) | {1}
    for old in _ws_versions(name):
        if old not in keep:
            try:
                os.remove(_ws_file(name, old))
            except OSError:
                pass


def _ws_activate(name):
    with open(os.path.join(BLENDER["root"], "active.txt"), "w", encoding="utf-8") as f:
        f.write(name)


def _ws_latest(name):
    vs = _ws_versions(name)
    if not vs:
        raise ValueError(f"workspace {name} has no saved versions")
    return vs[-1]


def _ws_save_new(name, what):
    v = (_ws_versions(name) or [0])[-1] + 1
    blender_call("save", timeout=180, path=_ws_file(name, v))
    _ws_record(name, v, what)
    return v


def _inbox():
    d = os.path.join(BLENDER["root"], "inbox")
    os.makedirs(d, exist_ok=True)
    return d


def _model_files():
    """Files Stella may import: the 3D inbox, plus the avatar characters already on the server."""
    files = {}
    for f in sorted(os.listdir(_inbox())):
        if f.rsplit(".", 1)[-1].lower() in MODEL_EXTS and os.path.isfile(os.path.join(_inbox(), f)):
            files[f] = os.path.join(_inbox(), f)
    chars = _avatar_path("bin", "characters") if AVATAR.get("dir") else ""
    if chars and os.path.isdir(chars):
        for f in sorted(os.listdir(chars)):
            if f.rsplit(".", 1)[-1].lower() in ("vrm", "glb"):
                files.setdefault("character:" + f, os.path.join(chars, f))
    return files


# ---- Guard rails for the code Stella writes ------------------------------------------------------
BPY_IMPORTS = {"bpy", "bmesh", "mathutils", "math", "random", "itertools", "functools", "collections", "colorsys",
               "bpy_extras", "statistics", "numpy"}
BPY_BAD_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "input", "breakpoint",
                 "exit", "quit", "memoryview", "delattr"}
BPY_BAD_ATTRS = {"texts", "handlers", "timers", "driver_namespace", "utils", "preferences", "as_module", "execfile",
                 "unpack", "write", "tofile", "savetxt", "savez", "fromfile", "addon_utils", "system"}
BPY_BAD_OPS = {"wm", "script", "preferences", "extensions", "file", "export_scene", "export_mesh", "export_anim",
               "import_scene", "import_mesh", "import_anim", "import_curve", "image", "sound", "text", "render",
               "screen", "ed", "workspace", "cachefile", "sequencer", "clip", "buttons", "console", "info"}


def check_bpy_code(code, allowed=None, extra_bad=(), bad_prefixes=()):
    """Problems that stop a step from running: no files, network, shell or program settings, only design edits.
    A guard rail against mistakes, not a sandbox. Defaults are for Blender; the CAD and PCB designers pass their
    own allowed imports and blocked names."""
    allowed = allowed or BPY_IMPORTS
    if len(code) > 40000:
        return ["the script is too long (40,000 characters max); split the change into smaller steps"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error on line {e.lineno}: {e.msg}"]
    probs = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            probs += [f"import {a.name} isn't allowed" for a in n.names if a.name.split(".")[0] not in allowed]
        elif isinstance(n, ast.ImportFrom):
            if n.level or (n.module or "").split(".")[0] not in allowed:
                probs.append(f"from {n.module} import isn't allowed")
        elif isinstance(n, ast.Name) and (n.id in BPY_BAD_NAMES or n.id in extra_bad or
                                          any(n.id.startswith(b) for b in bad_prefixes)):
            probs.append(f"{n.id}() isn't allowed")
        elif isinstance(n, ast.Attribute):
            base_is_ops = (isinstance(n.value, ast.Attribute) and n.value.attr == "ops") or \
                          (isinstance(n.value, ast.Name) and n.value.id == "ops")
            if n.attr.startswith("_"):
                probs.append(f"'.{n.attr}' isn't allowed")
            elif n.attr in BPY_BAD_ATTRS or n.attr in extra_bad or n.attr.lower().startswith("save") or \
                    any(n.attr.startswith(b) for b in bad_prefixes):
                probs.append(f"'.{n.attr}' isn't allowed (no files, scripts or settings)")
            elif base_is_ops and n.attr in BPY_BAD_OPS:
                probs.append(f"bpy.ops.{n.attr} isn't allowed (files, rendering and windows are handled by StellAI)")
            elif isinstance(n.ctx, ast.Store) and n.attr in ("filepath", "filepath_raw", "directory"):
                probs.append(f"setting .{n.attr} isn't allowed")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "setattr", "hasattr"):
            if len(n.args) < 2 or not (isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)
                                       and not n.args[1].value.startswith("_")):
                probs.append(f"{n.func.id}() needs a plain attribute name in quotes")
    return sorted(set(probs))[:8]


# ---- Measuring, comparing and looking --------------------------------------------------------------
def _scene_index(rep):
    return {o["name"]: o for o in rep.get("objects", [])}


def _changes(before, after):
    b, a = _scene_index(before), _scene_index(after)
    added = [_short(a[n]) for n in a if n not in b]
    removed = [n for n in b if n not in a]
    changed = []
    for n in a:
        if n not in b:
            continue
        diff = {}
        for k in ("loc", "rot_deg", "scale", "size", "verts", "mats", "parent", "modifiers", "hidden"):
            if b[n].get(k) != a[n].get(k):
                diff[k] = {"from": b[n].get(k), "to": a[n].get(k)}
        if diff:
            changed.append({"name": n, **diff})
    return {"added": added[:20], "removed": removed[:20], "changed": changed[:20],
            "more": max(0, len(added) - 20) + max(0, len(changed) - 20)}


def _short(o):
    return {k: o[k] for k in ("name", "type", "loc", "rot_deg", "size", "min", "max", "verts", "tris", "mats",
                              "parent", "bones") if k in o}


def _scene_summary(rep, limit=40):
    objs = sorted(rep.get("objects", []), key=lambda o: -(o.get("tris") or 0))
    return {"objects": [_short(o) for o in objs[:limit]], "more_objects": max(0, len(objs) - limit),
            "triangles": rep.get("tris"), "scene_size_m": rep.get("scene_size"), "scene_min": rep.get("scene_min"),
            "scene_max": rep.get("scene_max"), "units": rep.get("units"), "issues": rep.get("issues") or {}}


def _blender_render(views=None, focus="", look="solid", reopen=""):
    """Renders the loaded scene to one picture in the diagram folder. Returns (image id, render info).
    Quick renders need the graphics card; if Blender can't use it (or crashes trying), this falls back to slower
    CPU rendering (Cycles) and remembers that until the server restarts."""
    views = [v for v in (views or ["front", "right", "back", "three_quarter"]) if v in BLENDER_VIEWS][:6] or ["front"]
    image_id = uuid.uuid4().hex[:16]
    out = os.path.join(DIAGRAM_DIR, image_id + ".png")
    if BLENDER.get("cpu_only"):
        look = "cycles"
    kw = dict(out=out, views=views, focus=focus or "", res=512)
    try:
        info = blender_call("render", timeout=420, look=look, **kw)
    except RuntimeError as e:
        if look == "cycles" or "stopped unexpectedly" not in str(e) or not reopen:
            raise
        BLENDER["cpu_only"] = True
        log("Blender crashed rendering with the graphics card; using CPU renders (Cycles) from now on")
        blender_call("open", timeout=180, path=reopen)
        info = blender_call("render", timeout=600, look="cycles", **kw)
    if info.get("engine") == "CYCLES" and look != "cycles":
        BLENDER["cpu_only"] = True
    if not os.path.exists(out):
        raise RuntimeError("Blender rendered no picture")
    return image_id, info


def _vision_check(image_id, goal, facts, layout, workspace="", refs=None):
    """Asks the vision add-on whether the renders match the step's goal (and the workspace's reference pictures).
    Text, or why it couldn't look."""
    import base64
    try:
        with open(os.path.join(DIAGRAM_DIR, image_id + ".png"), "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        if refs is None:
            refs = _ref_images(workspace) if workspace else []
        content = []
        for label, path in refs:
            with open(path, "rb") as f:
                content.append({"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()}})
        ref_text = (f"The first {len(refs)} picture(s) are the user's reference photos ("
                    + ", ".join(l for l, _ in refs) + ") showing what the model should end up looking like; the LAST "
                    "picture is the model now. Compare shape, proportions, colours and details with the references, "
                    "and say what differs most. ") if refs else ""
        prompt = ("You are checking one step of a 3D modeling job in Blender. " + ref_text +
                  f"The model picture shows it from several angles ({layout}); each panel is labelled in its corner. "
                  "FRONT looks at the side facing -Y (a character's face); Z is up. Grey is background.\n"
                  f"The step was meant to: {goal}\n"
                  f"Measured by Blender (metres): {json.dumps(facts)[:2500]}\n"
                  "Judge only what you can see, and trust the measurements over your eyes for sizes. Reply in this form:\n"
                  "VERDICT: looks right | has problems | can't tell\n"
                  "- up to 5 short, concrete observations (which part, where, how it differs from the goal"
                  + (" or the references" if refs else "") + ")")
        content = [{"type": "text", "text": prompt}] + content + [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]
        body = {"model": "local", "temperature": 0.1, "max_tokens": 500,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": content}]}
        out = _llama("/v1/chat/completions", body, timeout=300)["choices"][0]["message"].get("content") or ""
        return re.sub(r"<think>[\s\S]*?</think>", "", out).strip() or "can't tell (empty answer)"
    except urllib.error.HTTPError as e:
        return f"not checked: the model server refused the picture (HTTP {e.code}; is the vision add-on installed?)"
    except (urllib.error.URLError, OSError, KeyError, ValueError, IndexError) as e:
        return f"not checked: the vision add-on couldn't look ({e})"


# ---- The tools ---------------------------------------------------------------------------------------
def tool_blender_project(args):
    action = str(args.get("action", "status")).strip().lower()
    if action == "status":
        exe = find_blender()
        out = {"blender": exe or "not installed", "files_you_can_import": list(_model_files())[:40],
               "inbox_folder": _inbox(), "workspaces": []}
        if exe and BLENDER["info"]:
            out.update(version=BLENDER["info"].get("version"), vrm_addon=BLENDER["info"].get("vrm"))
        wroot = os.path.join(BLENDER["root"], "workspaces")
        for n in sorted(os.listdir(wroot)) if os.path.isdir(wroot) else []:
            if WS_NAME.fullmatch(n) and _ws_versions(n):
                out["workspaces"].append({"name": n, "latest_version": _ws_versions(n)[-1]})
        try:
            out["active"] = _ws("")
            out["reference_pictures"] = len(_refs(out["active"]))
        except ValueError:
            out["active"] = None
        out["photo_to_3d"] = "installed" if _shape_config()["python"] else "not installed"
        out["graphics_cards"] = [{"index": g[0], "name": g[3], "free_gb": round(g[1] / 1024, 1),
                                  "total_gb": round(g[2] / 1024, 1)} for g in _gpus()]
        out["can_pause_stella_for_shape_jobs"] = _can_pause_model()
        return out, "Checked 3D workspaces"
    if action == "new":
        name = str(args.get("name", "")).strip().lower()
        if not WS_NAME.fullmatch(name):
            raise ValueError("give a name: lowercase letters, digits and dashes, e.g. vela-halo")
        if _ws_versions(name):
            raise ValueError(f"workspace {name} already exists; use action open, or pick another name")
        os.makedirs(_ws_dir(name, "versions"), exist_ok=True)
        os.makedirs(_ws_dir(name, "exports"), exist_ok=True)
        src = str(args.get("file", "")).strip()
        with BLENDER["lock"]:
            note = ""
            if src:
                files = _model_files()
                if src not in files:
                    raise ValueError(f"no file {src!r}; files you can import: {list(files)[:30]} (the user can drop "
                                     f"files into {_inbox()})")
                if src.lower().endswith(".blend"):
                    blender_call("open", timeout=180, path=files[src])
                else:
                    note = blender_call("import", timeout=300, path=files[src]).get("note", "")
            else:
                blender_call("new", timeout=60)
            v = _ws_save_new(name, f"start: {src or 'empty scene'}")
            _ws_activate(name)
            rep = blender_call("report", timeout=180, full=True)
            img, info = (None, {})
            if rep.get("scene_size"):
                img, info = _blender_render(reopen=_ws_file(name, v))
        res = {"workspace": name, "version": v, "scene": _scene_summary(rep), "note": note}
        if img:
            res.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
        return res, f"Started 3D workspace {name} (v{v})"
    if action == "open":
        name = _ws(args.get("name"))
        v = _ws_latest(name)
        _ws_activate(name)
        return {"workspace": name, "latest_version": v, "recent": _ws_history(name)[-8:]}, f"Opened {name} (v{v})"
    if action == "history":
        name = _ws(args.get("name"))
        return {"workspace": name, "versions_on_disk": _ws_versions(name), "history": _ws_history(name)[-25:]}, \
            f"History of {name}"
    if action == "revert":
        name = _ws(args.get("name"))
        try:
            target = int(str(args.get("version", "")).lstrip("vV"))
        except ValueError:
            raise ValueError("give the version number to go back to, e.g. 7")
        if target not in _ws_versions(name):
            raise ValueError(f"version {target} isn't on disk; available: {_ws_versions(name)[-15:]}")
        with BLENDER["lock"]:
            blender_call("open", timeout=180, path=_ws_file(name, target))
            v = _ws_save_new(name, f"back to v{target}")
            img, info = _blender_render(reopen=_ws_file(name, v))
        return {"workspace": name, "version": v, "same_as": target, "image": img, "title": f"{name} v{v}",
                "views": info.get("layout")}, f"Went back to v{target} (saved as v{v})"
    if action in ("add_reference", "references", "clear_references"):
        return _refs_action(action, args)
    if action == "export":
        name = _ws(args.get("name"))
        fmt = str(args.get("format", "glb")).lower().strip(". ")
        v = _ws_latest(name)
        os.makedirs(_ws_dir(name, "exports"), exist_ok=True)
        path = _ws_dir(name, "exports", f"{name}-v{v:04d}.{fmt}")
        if fmt == "blend":
            shutil.copyfile(_ws_file(name, v), path)
        else:
            with BLENDER["lock"]:
                blender_call("open", timeout=180, path=_ws_file(name, v))
                blender_call("export", timeout=420, path=path, format=fmt)
        return {"workspace": name, "version": v, "file": path, "bytes": os.path.getsize(path),
                "note": "The file is in this folder on the server computer."}, f"Exported {name} v{v} as {fmt}"
    raise ValueError("action must be status, new, open, history, revert, export, add_reference, references or "
                     "clear_references")


def tool_blender_step(args):
    code = str(args.get("code", ""))
    goal = str(args.get("goal", "")).strip()
    if not goal:
        raise ValueError("say what this step should do in goal (with numbers), so the result can be checked")
    if not code.strip():
        raise ValueError("no code")
    probs = check_bpy_code(code)
    if probs:
        return {"ok": False, "error": "the code was not run: " + "; ".join(probs)}, "Step blocked"
    name = _ws(args.get("workspace"))
    views = args.get("views") if isinstance(args.get("views"), list) else None
    timeout = max(30, min(600, int(args.get("timeout") or 180)))
    with BLENDER["lock"]:
        base = _ws_latest(name)
        blender_call("open", timeout=180, path=_ws_file(name, base))
        before = blender_call("report", timeout=180, full=False)
        try:
            ran = blender_call("run", timeout=timeout, code=code)
        except BlenderStepError as e:
            d = e.detail
            return {"ok": False, "error": d.get("error"), "where": d.get("where"), "output": d.get("output", ""),
                    "version": base, "note": f"Nothing was saved; the model is still v{base}."}, "Step failed"
        after = blender_call("report", timeout=180, full=True)
        diff = _changes(before, after)
        if not (diff["added"] or diff["removed"] or diff["changed"]):
            return {"ok": False, "error": "the code ran but nothing in the scene changed (no object was added, "
                                          "removed, moved, resized or re-materialed)", "output": ran.get("output", ""),
                    "version": base, "note": f"Nothing was saved; the model is still v{base}."}, "Step changed nothing"
        v = _ws_save_new(name, goal)
        try:
            img, info = _blender_render(views, str(args.get("focus", "")), str(args.get("look", "solid")),
                                        reopen=_ws_file(name, v))
        except (RuntimeError, BlenderStepError) as e:
            img, info = None, {"error": str(e)}
    touched = {o["name"] for o in diff["added"]} | {c["name"] for c in diff["changed"]}
    issues = {k: val for k, val in (after.get("issues") or {}).items() if k in touched}
    res = {"version": v, "previous": base, "changes": diff, "new_problems_in_changed_objects": issues,
           "triangles": after.get("tris"), "output": ran.get("output", "")[-1500:]}
    if img:
        res.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
        if str(args.get("check", True)).lower() not in ("false", "0", "no"):
            res["visual_check"] = _vision_check(img, goal, diff, info.get("layout", ""), name)
    else:
        res["render_error"] = info.get("error")
    return res, f"Saved {name} v{v}"


def tool_blender_inspect(args):
    name = _ws(args.get("workspace"))
    focus = str(args.get("focus", "")).strip()
    with BLENDER["lock"]:
        v = _ws_latest(name)
        blender_call("open", timeout=180, path=_ws_file(name, v))
        rep = blender_call("report", timeout=240, full=True, names=[focus] if focus else [])
        img, info = None, {}
        if args.get("render"):
            views = args.get("views") if isinstance(args.get("views"), list) else None
            img, info = _blender_render(views, focus, str(args.get("look", "solid")), reopen=_ws_file(name, v))
    res = {"workspace": name, "version": v, "scene": _scene_summary(rep)}
    if focus:
        res["focus"] = _scene_index(rep).get(focus) or f"no object named {focus!r}"
    if img:
        res.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
        if _ref_images(name) and str(args.get("compare", True)).lower() not in ("false", "0", "no"):
            res["comparison"] = _vision_check(img, "compare the model as it is now with the reference pictures",
                                              {"scene_size_m": rep.get("scene_size")}, info.get("layout", ""), name)
    return res, f"Inspected {name} v{v}"


# ---- Reference pictures: what the model should look like -------------------------------------------
PHOTO_ID = re.compile(r"[0-9a-f]{16}")
REF_LABELS = ("front", "left", "back", "right", "top", "detail")


def _refs(name):
    try:
        with open(_ws_dir(name, "refs", "refs.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_refs(name, refs):
    os.makedirs(_ws_dir(name, "refs"), exist_ok=True)
    with open(_ws_dir(name, "refs", "refs.json"), "w", encoding="utf-8") as f:
        json.dump(refs, f, indent=1)


def _photo_files(photos):
    """Chat photo ids (as shown to Stella, e.g. [photo 0123abcd...]) or "latest" -> file paths."""
    if isinstance(photos, str):
        photos = [p for p in re.split(r"[,\s]+", photos) if p]
    out = []
    for p in photos or []:
        p = str(p).strip().lower().replace("image:", "")
        if p in ("latest", "last", "newest"):
            files = sorted((os.path.join(IMAGE_DIR, f) for f in os.listdir(IMAGE_DIR) if f.endswith(".jpg")),
                           key=os.path.getmtime)
            files = [f for f in files if time.time() - os.path.getmtime(f) < 6 * 3600]
            if not files:
                raise ValueError("no photo was sent in the last few hours; ask the user to send one in the chat")
            out.append(files[-1])
            continue
        m = PHOTO_ID.search(p)
        if not m or not os.path.isfile(os.path.join(IMAGE_DIR, m.group(0) + ".jpg")):
            raise ValueError(f"no chat photo {p!r}; use the id from [photo ...] in the chat, or 'latest'")
        out.append(os.path.join(IMAGE_DIR, m.group(0) + ".jpg"))
    return out


def _ref_images(name, limit=3):
    """Up to limit reference pictures, labelled views first, as (label, path)."""
    refs = [r for r in _refs(name) if os.path.isfile(_ws_dir(name, "refs", r["file"]))]
    order = {l: i for i, l in enumerate(REF_LABELS)}
    refs.sort(key=lambda r: order.get(r.get("label") or "", 99))
    return [(r.get("label") or "reference", _ws_dir(name, "refs", r["file"])) for r in refs[:limit]]


def _refs_action(action, args):
    name = _ws(args.get("name"))
    refs = _refs(name)
    if action == "references":
        return {"workspace": name, "references": refs}, f"{len(refs)} reference picture(s)"
    if action == "clear_references":
        for r in refs:
            try:
                os.remove(_ws_dir(name, "refs", r["file"]))
            except OSError:
                pass
        _save_refs(name, [])
        return {"workspace": name, "references": []}, "Cleared reference pictures"
    files = _photo_files(args.get("photos") or "latest")
    labels = args.get("labels") or []
    if isinstance(labels, str):
        labels = [l for l in re.split(r"[,\s]+", labels) if l]
    added = []
    for i, src in enumerate(files):
        label = str(labels[i]).lower() if i < len(labels) else ""
        if label and label not in REF_LABELS:
            raise ValueError(f"labels are {', '.join(REF_LABELS)}")
        if label:   # one picture per view: a new front replaces the old front
            for r in [r for r in refs if r.get("label") == label]:
                refs.remove(r)
        fname = f"ref-{uuid.uuid4().hex[:8]}.jpg"
        os.makedirs(_ws_dir(name, "refs"), exist_ok=True)
        shutil.copyfile(src, _ws_dir(name, "refs", fname))
        entry = {"file": fname, "label": label, "photo": os.path.basename(src)[:-4], "at": int(time.time())}
        refs.append(entry)
        added.append(entry)
    _save_refs(name, refs[-8:])
    return {"workspace": name, "added": added, "references": refs[-8:],
            "note": "Every visual check now compares the renders with these pictures."}, \
        f"Added {len(added)} reference picture(s)"


# ---- Photo-to-3D shape generator (Hunyuan3D-2, on the graphics card) --------------------------------
SHAPE_MARK = "@@SHAPE@@"
SHAPE_NEED_MB = {"single": 7000, "multi": 9000}   # rough graphics memory a shape job needs
SHAPE_PY = r'''# StellAI photo-to-3D: turns one photo (or front/left/back photos) into a rough 3D mesh with Tencent's open
# Hunyuan3D-2 models, on this computer's graphics card. Written by stellai_hub.py: edits here are overwritten.
#   python stellai_shape.py --out shape.glb --image photo.jpg
#   python stellai_shape.py --out shape.glb --front f.jpg --left l.jpg --back b.jpg --right r.jpg
#   python stellai_shape.py --selftest            (downloads the model the first time and makes a test shape)
import argparse
import json
import os
import sys
import time


def say(**kw):
    print("@@SHAPE@@" + json.dumps(kw), flush=True)


def load(path, remove_bg):
    from PIL import Image
    img = Image.open(path)
    if img.mode == "RGBA" and img.getextrema()[3][0] < 250:
        return img            # already cut out
    img = img.convert("RGB")
    if remove_bg:
        from hy3dgen.rembg import BackgroundRemover
        img = BackgroundRemover()(img)
    return img


def tidy(mesh, faces):
    """Removes floating bits and broken faces, then cuts the face count. Each step is optional."""
    try:
        from hy3dgen.shapegen import FloaterRemover, DegenerateFaceRemover, FaceReducer
    except ImportError:
        return mesh, ["mesh clean-up tools not available in this Hunyuan3D version"]
    notes = []
    for name, fn in (("floaters", lambda m: FloaterRemover()(m)), ("degenerate faces", lambda m: DegenerateFaceRemover()(m)),
                     ("face reduction", lambda m: FaceReducer()(m, max_facenum=faces))):
        try:
            mesh = fn(mesh)
        except Exception as e:      # keep the unreduced mesh rather than fail the whole job
            notes.append(f"skipped {name}: {e}")
    return mesh, notes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="")
    p.add_argument("--image", default="")
    p.add_argument("--front", default="")
    p.add_argument("--left", default="")
    p.add_argument("--back", default="")
    p.add_argument("--right", default="")
    p.add_argument("--steps", type=int, default=30)
    p.add_argument("--octree", type=int, default=256)
    p.add_argument("--faces", type=int, default=40000)
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--keep-background", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    started = time.time()
    import torch
    if not torch.cuda.is_available():
        say(ok=False, error="PyTorch can't see an NVIDIA graphics card")
        sys.exit(2)
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
    if a.selftest:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((96, 96, 416, 416), fill=(200, 120, 60, 255))
        a.out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest.glb")
        a.steps, a.octree = 5, 128
        views = None
        image = img
    else:
        views = {k: getattr(a, k) for k in ("front", "left", "back", "right") if getattr(a, k)}
        if not views and not a.image:
            say(ok=False, error="no picture given")
            sys.exit(2)
        if views and "front" not in views:
            say(ok=False, error="multi-view needs at least a front picture")
            sys.exit(2)
        if not views:
            image = load(a.image, not a.keep_background)
            views = None
        else:
            image = {k: load(v, not a.keep_background) for k, v in views.items()}
    multi = isinstance(image, dict)
    repo, sub = ("tencent/Hunyuan3D-2mv", "hunyuan3d-dit-v2-mv") if multi else \
                ("tencent/Hunyuan3D-2mini", "hunyuan3d-dit-v2-mini")
    say(stage="loading", model=repo)
    pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(repo, subfolder=sub, use_safetensors=True,
                                                            device="cuda")
    say(stage="shaping")
    gen = torch.manual_seed(a.seed)
    mesh = pipe(image=image, num_inference_steps=a.steps, octree_resolution=a.octree, num_chunks=20000,
                generator=gen, output_type="trimesh")[0]
    mesh, notes = tidy(mesh, a.faces)
    mesh.export(a.out)
    say(ok=True, out=a.out, faces=int(len(mesh.faces)), verts=int(len(mesh.vertices)), model=repo,
        seconds=round(time.time() - started), notes=notes,
        vram_peak_mb=round(torch.cuda.max_memory_allocated() / 2**20))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        say(ok=False, error=f"{type(e).__name__}: {e}"[:1500])
        sys.exit(1)
'''


def _shape_config():
    try:
        with open(os.path.join(BLENDER["root"], "generator.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    py = os.environ.get("STELLAI_SHAPE_PYTHON") or cfg.get("python") or ""
    if not py:
        guess = os.path.expanduser(os.path.join("~", "llm", "shapegen", "venv", "bin", "python"))
        py = guess if os.path.isfile(guess) else ""
    return {"python": py if py and os.path.isfile(py) else "", "gpu": cfg.get("gpu", "auto")}


def _shape_script():
    d = os.path.join(BLENDER["root"], ".worker")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "stellai_shape.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(SHAPE_PY)
    return path


def _gpus():
    """[(index, free MB, total MB, name)] from nvidia-smi, or []."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    try:
        out = subprocess.run([smi, "--query-gpu=index,memory.free,memory.total,name", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=0x08000000 if os.name == "nt" else 0).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        try:
            gpus.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3] if len(parts) > 3 else "GPU"))
        except (ValueError, IndexError):
            pass
    return gpus


def _llm_dir():
    return os.path.dirname(SERVER_CONTROL.conf) if SERVER_CONTROL else ""


def _can_pause_model():
    d = _llm_dir()
    return bool(d) and os.path.exists(os.path.join(d, ".launcher-can-pause"))


def _pause_model():
    """Stops Stella's model so its graphics memory is free; the Linux launcher waits until .pause-model goes."""
    d = _llm_dir()
    open(os.path.join(d, ".pause-model"), "w").close()
    try:
        with open(SERVER_CONTROL.pid_file) as f:
            os.kill(int(f.read().strip()), 15)
    except (OSError, ValueError):
        pass
    log("3D: Stella's model paused to free the graphics card for a shape job")


def _resume_model():
    try:
        os.remove(os.path.join(_llm_dir(), ".pause-model"))
        log("3D: shape job done, Stella's model is loading again")
    except OSError:
        pass


def _wait_model(timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        try:
            _llama("/health", timeout=5)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(3)
    return False


def _pick_gpu(need_mb):
    """(gpu index, paused?) for a shape job, pausing Stella's model when that's the only way to fit."""
    gpus = _gpus()
    if not gpus:
        raise RuntimeError("the shape generator needs an NVIDIA graphics card, and nvidia-smi doesn't see one")
    want = _shape_config()["gpu"]
    pool = [g for g in gpus if str(g[0]) == str(want)] if str(want) != "auto" else gpus
    if not pool:
        raise RuntimeError(f"graphics card {want} (from generator.json) isn't there; cards: {[g[0] for g in gpus]}")
    best = max(pool, key=lambda g: g[1])
    if best[1] >= need_mb:
        return best[0], False
    if max(g[2] for g in pool) < need_mb:
        raise RuntimeError(f"the shape generator needs about {need_mb // 1000} GB of graphics memory; the biggest card "
                           f"here has {max(g[2] for g in pool) // 1000} GB")
    if not _can_pause_model():
        raise RuntimeError(f"not enough free graphics memory (about {need_mb // 1000} GB needed, {best[1] // 1000} GB "
                           "free) and StellAI can't pause Stella's model on this setup. Wait until her model sleeps, "
                           "add a second graphics card, or (Linux) re-run stellai-setup.sh to update the launcher.")
    _pause_model()
    end = time.time() + 90
    while time.time() < end:
        time.sleep(3)
        free = {g[0]: g[1] for g in _gpus()}
        cand = max((i for i in free if i in {g[0] for g in pool}), key=lambda i: free[i])
        if free[cand] >= need_mb:
            return cand, True
    _resume_model()
    raise RuntimeError("paused Stella's model, but the graphics memory didn't free up; something else is using it")


def _run_shape(files, views, faces, detail):
    """Runs the generator; returns (glb path, info). views: {"front": path, ...} or None for one picture."""
    cfg = _shape_config()
    if not cfg["python"]:
        raise RuntimeError("the photo-to-3D generator isn't installed. Linux: re-run stellai-setup.sh and say yes to "
                           "'photo-to-3D'. It needs an NVIDIA card with 8 GB or more.")
    out = os.path.join(BLENDER["root"], ".worker", f"shape-{uuid.uuid4().hex[:8]}.glb")
    cmd = [cfg["python"], _shape_script(), "--out", out, "--faces", str(faces),
           "--octree", "384" if detail == "high" else "256", "--steps", "50" if detail == "high" else "30"]
    if views:
        for k, v in views.items():
            cmd += [f"--{k}", v]
    else:
        cmd += ["--image", files[0]]
    gpu, paused = _pick_gpu(SHAPE_NEED_MB["multi" if views else "single"])
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu), PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
    lines = []
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=25 * 60, env=env,
                           cwd=os.path.join(BLENDER["root"], ".worker"), creationflags=0x08000000 if os.name == "nt" else 0)
        lines = [l for l in (p.stdout + "\n" + p.stderr).splitlines()]
    except subprocess.TimeoutExpired:
        raise RuntimeError("the shape generator took longer than 25 minutes, so StellAI stopped it")
    finally:
        if paused:
            _resume_model()
    msgs = []
    for l in lines:
        if l.startswith(SHAPE_MARK):
            try:
                msgs.append(json.loads(l[len(SHAPE_MARK):]))
            except ValueError:
                pass
    final = next((m for m in reversed(msgs) if "ok" in m), None)
    if not final or not final.get("ok") or not os.path.isfile(out):
        tail = " / ".join(l.strip() for l in lines[-4:] if l.strip())[:600]
        raise RuntimeError("the shape generator failed: " + ((final or {}).get("error") or tail or "no output"))
    return out, dict(final, gpu=gpu, paused_stella=paused)


def tool_blender_generate(args):
    name = _ws(args.get("workspace"))
    faces = max(2000, min(200000, int(args.get("faces") or 40000)))
    detail = "high" if str(args.get("detail", "")).lower() == "high" else "normal"
    views = None
    if args.get("photos"):
        files = _photo_files(args.get("photos"))
        labels = args.get("labels") or []
        if isinstance(labels, str):
            labels = [l for l in re.split(r"[,\s]+", labels) if l]
        labelled = {str(l).lower(): f for l, f in zip(labels, files) if str(l).lower() in ("front", "left", "back", "right")}
    else:
        refs = _ref_images(name, limit=8)
        if not refs:
            raise ValueError("no pictures: pass photos (ids from the chat, or 'latest'), or add reference pictures "
                             "first with blender_project add_reference")
        files = [p for _, p in refs]
        labelled = {l: p for l, p in refs if l in ("front", "left", "back", "right")}
    if "front" in labelled and len(labelled) > 1:
        views = labelled
    started = time.time()
    glb, info = _run_shape(files, views, faces, detail)
    with BLENDER["lock"]:
        base = _ws_latest(name)
        blender_call("open", timeout=180, path=_ws_file(name, base))
        imp = blender_call("import", timeout=300, path=glb, keep=True, rename=str(args.get("object") or "Generated"))
        v = _ws_save_new(name, f"generated a shape from {'%d views' % len(views) if views else 'one picture'}")
        rep = blender_call("report", timeout=180, full=True, names=imp.get("added") or [])
        try:
            img, rinfo = _blender_render(focus=(imp.get("added") or [""])[0], reopen=_ws_file(name, v))
        except RuntimeError as e:
            img, rinfo = None, {"error": str(e)}
    try:
        os.remove(glb)
    except OSError:
        pass
    added = [o for o in rep.get("objects", []) if o["name"] in (imp.get("added") or [])]
    res = {"version": v, "previous": base, "new_objects": [_short(o) for o in added],
           "new_problems": {k: val for k, val in (rep.get("issues") or {}).items() if k in (imp.get("added") or [])},
           "generator": {k: info.get(k) for k in ("model", "faces", "seconds", "gpu", "paused_stella", "notes")},
           "minutes": round((time.time() - started) / 60, 1),
           "next": "The shape is rough and unscaled, sits at the origin and may face the wrong way. Next: rotate it so "
                   "its front faces -Y, scale it from a real measurement (ask the user), then clean up and add "
                   "materials step by step."}
    if img:
        res.update(image=img, title=f"{name} v{v}", views=rinfo.get("layout"))
        if info.get("paused_stella"):
            _wait_model()
        res["visual_check"] = _vision_check(img, "a new shape generated from the reference pictures; judge how well "
                                                 "its overall form matches them (scale and colour don't matter yet)",
                                            res["new_objects"], rinfo.get("layout", ""), name)
    else:
        res["render_error"] = rinfo.get("error")
    return res, f"Generated a shape in {name} (v{v})"


BLENDER_STEP_DESC = (
    "3D Modeler: run ONE small change on the current model as a Blender Python (bpy) script. The server opens the "
    "newest version, runs the code, saves it as a new numbered version, renders it, measures what changed and "
    "has the vision add-on check the pictures against goal. If the code fails or changes nothing, nothing is saved. "
    "Metres, Z up, front faces -Y. Use bpy.data and bmesh (bmesh.ops.create_cube/create_uvsphere/create_cone, "
    "bmesh.ops.create_circle...), not edit-mode bpy.ops; no files, imports other than bpy/bmesh/mathutils/math/"
    "random/numpy, or bpy.ops.wm/render/export. Ready to use: obj(name), bbox(o) -> (min, max) world, "
    "new_object(name, bm), material(name, color, emission=None, strength=1), assign(o, mat), "
    "bone_world(armature, bone) -> (head, tail), parent_to_bone(o, armature, bone), apply_transform(o), "
    "Vector, Matrix, Euler, radians, scene. print() output comes back to you.")

SERVER_TOOLS["blender_project"] = (tool_blender_project, _fn(
    "blender_project",
    "3D Modeler workspaces (a model with numbered versions). status: Blender, workspaces, importable files. "
    "new: start a workspace from an empty scene or an importable file. open: switch workspace. history: versions "
    "and what each changed. revert: go back to a version (saved as a new one). export: write glb/fbx/obj/stl/"
    "vrm/blend of the newest version to the workspace's exports folder. add_reference: keep chat photos as "
    "reference pictures (labels front/left/back/right/top/detail); every visual check then compares with them. "
    "references / clear_references: list or remove them.",
    {"action": _p("status, new, open, history, revert or export"),
     "name": _p("Workspace name: lowercase, digits, dashes (default: the active one)"),
     "file": _p("For new: a file from status's files_you_can_import"),
     "version": _p("For revert: the version number", "integer"),
     "format": _p("For export: glb (default), fbx, obj, stl, vrm or blend"),
     "photos": {"type": "array", "items": {"type": "string"},
                "description": "For add_reference: photo ids from [photo ...] in the chat, or 'latest'"},
     "labels": {"type": "array", "items": {"type": "string"},
                "description": "For add_reference: the view of each photo: front, left, back, right, top or detail"}},
    ["action"]))
SERVER_TOOLS["blender_step"] = (tool_blender_step, _fn(
    "blender_step", BLENDER_STEP_DESC,
    {"goal": _p("What this step should achieve, with numbers, e.g. 'add a torus halo, 0.32 m wide, 0.06 m above "
                "the head bone, tilted 8° on X, lime emissive material'"),
     "code": _p("The bpy script"),
     "views": {"type": "array", "items": {"type": "string"},
               "description": "Render angles: front, right, back, left, top, three_quarter (default 4 of them)"},
     "focus": _p("Optional object name to frame the renders on"),
     "look": _p("solid (default, fast and clear) or material (EEVEE, slower)"),
     "check": _p("false skips the visual check (faster)", "boolean"),
     "workspace": _p("Default: the active workspace")}, ["goal", "code"]))
SERVER_TOOLS["blender_generate"] = (tool_blender_generate, _fn(
    "blender_generate",
    "3D Modeler: make a new rough 3D shape from pictures with the local photo-to-3D generator (Hunyuan3D-2) and add "
    "it to the current model as a new version. Uses the workspace's reference pictures unless photos are given; a "
    "front picture plus left/back/right views gives a better shape than one picture. Slow (several minutes; on a "
    "single graphics card Stella's model is paused meanwhile). The result is one unrigged, untextured, unscaled "
    "mesh: scale, orient and clean it up afterwards with blender_step. Best for solid objects and props; rough "
    "for characters; poor for whole rooms.",
    {"photos": {"type": "array", "items": {"type": "string"},
                "description": "Optional photo ids from [photo ...] in the chat, or 'latest'"},
     "labels": {"type": "array", "items": {"type": "string"},
                "description": "Optional view of each photo: front, left, back or right"},
     "faces": _p("Face count to reduce to (default 40000)", "integer"),
     "detail": _p("normal (default) or high (slower)"),
     "object": _p("Name for the new object (default Generated)"),
     "workspace": _p("Default: the active workspace")}, []))
SERVER_TOOLS["blender_inspect"] = (tool_blender_inspect, _fn(
    "blender_inspect",
    "3D Modeler: read the current model without changing it: objects with positions, sizes, rotations, parents, "
    "vertex/triangle counts, materials, bones and mesh problems. render=true also shows pictures. Do this before "
    "the first change and whenever you're unsure of names or sizes.",
    {"focus": _p("Optional object name for details and framing"),
     "render": _p("true to render pictures too", "boolean"),
     "views": {"type": "array", "items": {"type": "string"},
               "description": "front, right, back, left, top, three_quarter"},
     "look": _p("solid (default) or material"),
     "compare": _p("With render and reference pictures: false skips the comparison", "boolean"),
     "workspace": _p("Default: the active workspace")}, []))


