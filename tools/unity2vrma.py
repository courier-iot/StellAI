"""Unity Humanoid .anim (muscle curves, text YAML) -> VRM Animation (.vrma), on a VRM character's skeleton.

    python3 tools/unity2vrma.py clip.anim|package.unitypackage server/avatar_defaults/Stella.vrm out_folder [--hold 3]

A .unitypackage is read without unpacking it to disk; every Humanoid .anim inside is converted. A clip that is a
single held pose (one key) becomes a clip of --hold seconds. Needs numpy. Checked against motion-captured .vrma
clips (standing poses) and the creator's preview video; see docs in server/avatar_defaults/README.md.

Muscle m in [-1, 1] turns one axis of a bone through Unity's default range: angle = m * (max if m >= 0 else -min).
Muscle 0 is Unity's "muscle zero" pose, not the T-pose, so each axis is measured from its T-pose value (TPOSE)
and turned about the matching axis of the VRM normalized skeleton (glTF: +y up, she faces +z, her left is +x).
Swing and twist are combined the way Unity does it: swing (axis-angle of the two bend axes) * twist (bone axis).
"""
import json
import math
import re
import struct
import sys

import numpy as np


# muscle -> (min, max) degrees (Unity defaults, HumanTrait.GetMuscleDefaultMin/Max)
RANGE = {
    "Spine": (-40, 40), "Chest": (-40, 40), "UpperChest": (-20, 20), "Neck": (-40, 40), "Head": (-40, 40),
    "Upper Leg Front-Back": (-90, 50), "Upper Leg In-Out": (-60, 60), "Upper Leg Twist In-Out": (-60, 60),
    "Lower Leg Stretch": (-80, 80), "Lower Leg Twist In-Out": (-90, 90),
    "Foot Up-Down": (-50, 50), "Foot Twist In-Out": (-30, 30), "Toes Up-Down": (-50, 50),
    "Shoulder Down-Up": (-15, 30), "Shoulder Front-Back": (-15, 15),
    "Arm Down-Up": (-60, 100), "Arm Front-Back": (-100, 100), "Arm Twist In-Out": (-90, 90),
    "Forearm Stretch": (-80, 80), "Forearm Twist In-Out": (-90, 90),
    "Hand Down-Up": (-80, 80), "Hand In-Out": (-40, 40),
    "Thumb.1 Stretched": (-20, 20), "Thumb.Spread": (-25, 25), "Thumb.2 Stretched": (-40, 35), "Thumb.3 Stretched": (-40, 35),
    "Index.1 Stretched": (-50, 50), "Index.Spread": (-20, 20), "Index.2 Stretched": (-45, 45), "Index.3 Stretched": (-45, 45),
    "Middle.1 Stretched": (-50, 50), "Middle.Spread": (-7.5, 7.5), "Middle.2 Stretched": (-45, 45), "Middle.3 Stretched": (-45, 45),
    "Ring.1 Stretched": (-50, 50), "Ring.Spread": (-7.5, 7.5), "Ring.2 Stretched": (-45, 45), "Ring.3 Stretched": (-45, 45),
    "Little.1 Stretched": (-50, 50), "Little.Spread": (-20, 20), "Little.2 Stretched": (-45, 45), "Little.3 Stretched": (-45, 45),
}
# muscle values of the T-pose (where the VRM normalized skeleton has no rotation)
TPOSE = {"Arm Down-Up": 0.4, "Arm Front-Back": 0.3, "Forearm Stretch": 1.0, "Upper Leg Front-Back": 0.6,
         "Lower Leg Stretch": 1.0}
FINGER_TPOSE = {"Stretched": 1.0, "Spread": 0.0}
X, Y, Z = np.eye(3)


def deg(name, m):
    lo, hi = next(v for k, v in RANGE.items() if name.endswith(k) or k in name)
    return m * (hi if m >= 0 else -lo)


def unity_angle(name, m, tpose=0.0):
    return math.radians(deg(name, m) - deg(name, tpose))


def axis_angle(v):
    a = np.linalg.norm(v)
    if a < 1e-9:
        return np.array([0, 0, 0, 1.0])
    s = math.sin(a / 2) / a
    return np.array([v[0] * s, v[1] * s, v[2] * s, math.cos(a / 2)])


def qmul(a, b):
    ax, ay, az, aw = a; bx, by, bz, bw = b
    return np.array([aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz])


def bone_rotations(mv):
    """mv: {muscle name: value} -> {vrm bone: quaternion xyzw} in the normalized (T-pose = identity) space."""
    out = {}

    def g(n, d=0.0):
        return mv.get(n, d)

    def put(bone, twist_axis, swing_vec, twist_angle):
        out[bone] = qmul(axis_angle(swing_vec), axis_angle(twist_axis * twist_angle))

    # spine family: Front-Back (-1 = front), Left-Right (-1 = left), Twist/Turn (-1 = left)
    for unity, bone, fb, lr, tw in (("Spine", "spine", "Spine Front-Back", "Spine Left-Right", "Spine Twist Left-Right"),
                                    ("Chest", "chest", "Chest Front-Back", "Chest Left-Right", "Chest Twist Left-Right"),
                                    ("UpperChest", "upperChest", "UpperChest Front-Back", "UpperChest Left-Right", "UpperChest Twist Left-Right"),
                                    ("Neck", "neck", "Neck Nod Down-Up", "Neck Tilt Left-Right", "Neck Turn Left-Right"),
                                    ("Head", "head", "Head Nod Down-Up", "Head Tilt Left-Right", "Head Turn Left-Right")):
        a_fb, a_lr, a_tw = unity_angle(fb, g(fb)), unity_angle(lr, g(lr)), unity_angle(tw, g(tw))
        # forward bend = +x rotation; bend to her left = -z; turn to her left = +y
        put(bone, Y, X * (-a_fb) + Z * a_lr, -a_tw)
    for side, s in (("Left", 1), ("Right", -1)):
        sd = side.lower()
        # legs (bone axis y). Front-Back: -1 = front (flex: -x rotation is forward... see sign below)
        fb = unity_angle("Upper Leg Front-Back", g(f"{side} Upper Leg Front-Back", 0.6), TPOSE["Upper Leg Front-Back"])
        io = unity_angle("Upper Leg In-Out", g(f"{side} Upper Leg In-Out"))
        tw = unity_angle("Upper Leg Twist In-Out", g(f"{side} Upper Leg Twist In-Out"))
        put(sd + "UpperLeg", Y, X * fb + Z * (s * io), s * tw)
        bend = -unity_angle("Lower Leg Stretch", g(f"{side} Lower Leg Stretch", 1.0), TPOSE["Lower Leg Stretch"])
        tw = unity_angle("Lower Leg Twist In-Out", g(f"{side} Lower Leg Twist In-Out"))
        put(sd + "LowerLeg", Y, X * bend, s * tw)
        ud = unity_angle("Foot Up-Down", g(f"{side} Foot Up-Down"))
        tw = unity_angle("Foot Twist In-Out", g(f"{side} Foot Twist In-Out"))
        put(sd + "Foot", Y, X * ud, s * tw)
        put(sd + "Toes", Y, X * unity_angle("Toes Up-Down", g(f"{side} Toes Up-Down")), 0)
        # arms (bone axis x, pointing +x on her left, -x on her right)
        du = unity_angle("Shoulder Down-Up", g(f"{side} Shoulder Down-Up"))
        fb = unity_angle("Shoulder Front-Back", g(f"{side} Shoulder Front-Back"))
        put(sd + "Shoulder", X, Z * (s * du) + Y * (s * fb), 0)
        du = unity_angle("Arm Down-Up", g(f"{side} Arm Down-Up", 0.4), TPOSE["Arm Down-Up"])
        fb = unity_angle("Arm Front-Back", g(f"{side} Arm Front-Back", 0.3), TPOSE["Arm Front-Back"])
        tw = unity_angle("Arm Twist In-Out", g(f"{side} Arm Twist In-Out"))
        put(sd + "UpperArm", X, Z * (s * du) + Y * (s * fb), -s * tw)
        bend = -unity_angle("Forearm Stretch", g(f"{side} Forearm Stretch", 1.0), TPOSE["Forearm Stretch"])
        tw = unity_angle("Forearm Twist In-Out", g(f"{side} Forearm Twist In-Out"))
        put(sd + "LowerArm", X, Y * (-s * bend), -s * tw)
        du = unity_angle("Hand Down-Up", g(f"{side} Hand Down-Up"))
        io = unity_angle("Hand In-Out", g(f"{side} Hand In-Out"))
        put(sd + "Hand", X, Z * (s * du) + Y * (-s * io), 0)
        # fingers: Stretched curls toward the palm (-z on her left), Spread fans them (y)
        for f, vrm in (("Index", "Index"), ("Middle", "Middle"), ("Ring", "Ring"), ("Little", "Little")):
            sp = unity_angle(f"{f}.Spread", g(f"{side}Hand.{f}.Spread"), FINGER_TPOSE["Spread"])
            for i, part in ((1, "Proximal"), (2, "Intermediate"), (3, "Distal")):
                curl = -unity_angle(f"{f}.{i} Stretched", g(f"{side}Hand.{f}.{i} Stretched", 1.0), FINGER_TPOSE["Stretched"])
                put(f"{sd}{vrm}{part}", X, Z * (-s * curl) + (Y * (s * sp) if i == 1 else 0 * Y), 0)
        sp = unity_angle("Thumb.Spread", g(f"{side}Hand.Thumb.Spread"))
        for i, part in ((1, "Metacarpal"), (2, "Proximal"), (3, "Distal")):
            curl = -unity_angle(f"Thumb.{i} Stretched", g(f"{side}Hand.Thumb.{i} Stretched", 1.0), FINGER_TPOSE["Stretched"])
            put(f"{sd}Thumb{part}", X, Y * (-s * curl * 0.6) + Z * (-s * curl * 0.4) + (Z * (s * sp) if i == 1 else 0 * Z), 0)
    return out


# ---- the character's skeleton
def glb(p):
    d=open(p,'rb').read(); l=struct.unpack('<I',d[12:16])[0]
    return json.loads(d[20:20+l]), d[20+l+8:]
def vrm_skeleton(p):
    """Humanoid bone positions (T-pose, world) and parents from a VRM 1.0 file."""
    j,_=glb(p); hb=j['extensions']['VRMC_vrm']['humanoid']['humanBones']
    nodes=j['nodes']; parent={}
    for i,n in enumerate(nodes):
        for c in n.get('children',[]): parent[c]=i
    def qm(q):
        x,y,z,w=q
        return np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],[2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],[2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
    def world(i):
        chain=[]
        while i is not None: chain.append(i); i=parent.get(i)
        R=np.eye(3); t=np.zeros(3)
        for k in reversed(chain):
            n=nodes[k]; t=t+R@np.array(n.get('translation',[0,0,0])); R=R@qm(n.get('rotation',[0,0,0,1]))
        return t
    node2bone={v['node']:k for k,v in hb.items()}
    bones={}
    for k,v in hb.items():
        i=v['node']; p_=parent.get(i)
        while p_ is not None and p_ not in node2bone: p_=parent.get(p_)
        bones[k]={'pos':world(i),'parent':node2bone.get(p_)}
    return bones


# ---- reading the .anim
def read_curves(path_or_text):
    s = path_or_text if "\n" in path_or_text else open(path_or_text, encoding="utf-8").read()
    i = s.index("  m_FloatCurves:")
    j = s.index("  m_PPtrCurves:", i)
    out = {}
    for b in s[i:j].split("  - serializedVersion: 2\n    curve:")[1:]:
        a = re.search(r"\n    attribute: (.*)\n", b).group(1).strip()
        keys = []
        for k in b.split("      - serializedVersion: 3\n")[1:]:
            v = {n: float(re.search(rf"\n?\s*{n}: (\S+)", k).group(1)) for n in ("time", "value", "inSlope", "outSlope")}
            keys.append(v)
        out[a] = keys
    loop = re.search(r"m_LoopTime: (\d)", s)
    return out, bool(loop and loop.group(1) == "1")


def sample(keys, t):
    """Unity's curve: cubic Hermite between keys (constant outside)."""
    if t <= keys[0]["time"]:
        return keys[0]["value"]
    if t >= keys[-1]["time"]:
        return keys[-1]["value"]
    for a, b in zip(keys, keys[1:]):
        if a["time"] <= t <= b["time"]:
            dt = b["time"] - a["time"]
            if dt <= 0:
                return b["value"]
            if math.isinf(a["outSlope"]) or math.isinf(b["inSlope"]):
                return a["value"]          # stepped key
            u = (t - a["time"]) / dt
            h00, h10, h01, h11 = 2 * u**3 - 3 * u**2 + 1, u**3 - 2 * u**2 + u, -2 * u**3 + 3 * u**2, u**3 - u**2
            return h00 * a["value"] + h10 * dt * a["outSlope"] + h01 * b["value"] + h11 * dt * b["inSlope"]
    return keys[-1]["value"]


def frames(curves, fps=30, hold=None):
    end = max(k[-1]["time"] for k in curves.values())
    if end <= 0:                       # a single held pose
        end = hold or 2.5
    n = max(2, int(round(end * fps)) + 1)
    times = [end * i / (n - 1) for i in range(n)]
    out = []
    for t in times:
        mv = {a: sample(k, t) for a, k in curves.items()}
        out.append((t, mv))
    return out


def root_of(mv, hips_h):
    """RootT/RootQ (Unity, left-handed, x = her right) -> hips position and rotation in glTF (x = her left)."""
    t = np.array([-mv.get("RootT.x", 0), mv.get("RootT.y", 1), mv.get("RootT.z", 0)]) * hips_h
    q = np.array([mv.get("RootQ.x", 0), -mv.get("RootQ.y", 0), -mv.get("RootQ.z", 0), mv.get("RootQ.w", 1)])
    return t, q / np.linalg.norm(q)


# ---- writing the .vrma
def write_vrma(path, fr, skel, root_y0=None):
    names = [b for b in skel]
    idx = {b: i for i, b in enumerate(names)}
    hips_h = skel["hips"]["pos"][1]
    nodes = []
    for b in names:
        p = skel[b]["parent"]
        rel = skel[b]["pos"] - (skel[p]["pos"] if p else 0)
        nodes.append({"name": b, "translation": [float(x) for x in rel]})
    for b in names:
        kids = [idx[c] for c in names if skel[c]["parent"] == b]
        if kids:
            nodes[idx[b]]["children"] = kids
    times = np.array([t for t, _ in fr], dtype=np.float32)
    rots = {b: [] for b in names}
    hipT = []
    base = root_y0 if root_y0 is not None else 1.0
    for _, mv in fr:
        r = bone_rotations(mv)
        t, q = root_of(mv, hips_h / base)
        hipT.append(t)
        for b in names:
            rq = r.get(b, np.array([0, 0, 0, 1.0]))
            if b == "hips":
                rq = q
            rots[b].append(rq)
    blob = bytearray(); views = []; accs = []

    def add(arr, typ, minmax=False):
        a = np.ascontiguousarray(arr, dtype=np.float32)
        while len(blob) % 4:
            blob.append(0)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": a.nbytes})
        blob.extend(a.tobytes())
        acc = {"bufferView": len(views) - 1, "componentType": 5126, "count": len(a), "type": typ}
        if minmax:
            acc["min"] = [float(a.min())]; acc["max"] = [float(a.max())]
        accs.append(acc)
        return len(accs) - 1
    tin = add(times, "SCALAR", True)
    samplers, channels = [], []
    for b in names:
        q = np.array(rots[b])
        if np.allclose(q, [0, 0, 0, 1], atol=1e-6) and b != "hips":
            continue
        for k in range(1, len(q)):          # keep neighbouring keys on the same hemisphere
            if np.dot(q[k], q[k - 1]) < 0:
                q[k] = -q[k]
        samplers.append({"input": tin, "output": add(q, "VEC4"), "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": idx[b], "path": "rotation"}})
    samplers.append({"input": tin, "output": add(np.array(hipT), "VEC3"), "interpolation": "LINEAR"})
    channels.append({"sampler": len(samplers) - 1, "target": {"node": idx["hips"], "path": "translation"}})
    gl = {"asset": {"version": "2.0", "generator": "StellAI unity2vrma"},
          "extensionsUsed": ["VRMC_vrm_animation"],
          "extensions": {"VRMC_vrm_animation": {"specVersion": "1.0",
                                                "humanoid": {"humanBones": {b: {"node": idx[b]} for b in names if b not in ("leftEye", "rightEye")}}}},
          "scene": 0, "scenes": [{"nodes": [idx["hips"]]}], "nodes": nodes,
          "animations": [{"name": "animation", "samplers": samplers, "channels": channels}],
          "buffers": [{"byteLength": len(blob)}], "bufferViews": views, "accessors": accs}
    js = json.dumps(gl, separators=(",", ":")).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    while len(blob) % 4:
        blob.append(0)
    out = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(js) + 8 + len(blob))
    out += struct.pack("<I", len(js)) + b"JSON" + js + struct.pack("<I", len(blob)) + b"BIN\x00" + bytes(blob)
    open(path, "wb").write(out)
    return len(out)


def anims_in_package(path):
    """(name, text) of every Humanoid AnimationClip in a .unitypackage (a gzipped tar of GUID folders)."""
    import os
    import tarfile
    found, names = {}, {}
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or m.size > 50_000_000:
                continue
            guid, _, leaf = m.name.lstrip("./").partition("/")
            if leaf == "pathname":
                names[guid] = tar.extractfile(m).read().decode("utf-8", "replace").splitlines()[0]
            elif leaf == "asset":
                data = tar.extractfile(m).read()
                if data.startswith(b"%YAML") and b"AnimationClip:" in data[:400] and b"m_FloatCurves:" in data:
                    found[guid] = data.decode("utf-8", "replace")
    return [(os.path.splitext(os.path.basename(names.get(g, g)))[0], text) for g, text in found.items()]


def main(argv):
    import argparse
    import os
    p = argparse.ArgumentParser(description="Unity Humanoid .anim / .unitypackage -> .vrma")
    p.add_argument("source"); p.add_argument("vrm"); p.add_argument("out")
    p.add_argument("--hold", type=float, default=3.0, help="seconds for a single held pose")
    p.add_argument("--fps", type=int, default=30)
    a = p.parse_args(argv)
    skel = vrm_skeleton(a.vrm)
    clips = anims_in_package(a.source) if a.source.endswith(".unitypackage") else \
        [(os.path.splitext(os.path.basename(a.source))[0], open(a.source, encoding="utf-8").read())]
    os.makedirs(a.out, exist_ok=True)
    for name, text in clips:
        curves, _ = read_curves(text)
        fr = frames(curves, a.fps, a.hold)
        dest = os.path.join(a.out, name + ".vrma")
        size = write_vrma(dest, fr, skel)
        print(f"{dest}: {len(fr)} frames, {fr[-1][0]:.2f} s, {size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
