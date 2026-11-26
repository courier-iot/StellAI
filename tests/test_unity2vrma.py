"""tools/unity2vrma.py: Unity Humanoid clips become .vrma that move the right way, and the shipped conversions match."""
import io
import json
import os
import struct
import sys
import tarfile
import tempfile

import numpy as np

from helpers import ROOT, check, done

sys.path.insert(0, os.path.join(ROOT, "tools"))
import unity2vrma as u  # noqa: E402

STELLA = os.path.join(ROOT, "server", "avatar_defaults", "Stella.vrm")
SK = u.vrm_skeleton(STELLA)


def anim(values, name="clip", times=(0,)):
    """A minimal Unity .anim with one key per time for each muscle (same value at every key)."""
    curves = "".join(f"""  - serializedVersion: 2
    curve:
      serializedVersion: 2
      m_Curve:
{''.join(f'''      - serializedVersion: 3
        time: {t}
        value: {v}
        inSlope: 0
        outSlope: 0
''' for t in times)}      m_PreInfinity: 2
    attribute: {a}
    path:
""" for a, v in values.items())
    return f"%YAML 1.1\n--- !u!74 &7400000\nAnimationClip:\n  m_Name: {name}\n  m_FloatCurves:\n{curves}  m_PPtrCurves: []\n  m_LoopTime: 0\n"


def qm(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def fk(mv):
    """World joint positions for a set of muscle values (the same maths three-vrm applies)."""
    r = u.bone_rotations(mv)
    order, P, W = [], {}, {}

    def visit(b):
        order.append(b)
        for c in SK:
            if SK[c]["parent"] == b:
                visit(c)
    visit("hips")
    for b in order:
        p = SK[b]["parent"]
        R = qm(r.get(b, [0, 0, 0, 1]))
        if p is None:
            P[b], W[b] = SK[b]["pos"].copy(), np.eye(3)
        else:
            P[b] = P[p] + W[p] @ (SK[b]["pos"] - SK[p]["pos"]); W[b] = W[p] @ R
    return P


TPOSE = {"Left Arm Down-Up": 0.4, "Right Arm Down-Up": 0.4, "Left Arm Front-Back": 0.3, "Right Arm Front-Back": 0.3,
         "Left Forearm Stretch": 1, "Right Forearm Stretch": 1, "Left Upper Leg Front-Back": 0.6,
         "Right Upper Leg Front-Back": 0.6, "Left Lower Leg Stretch": 1, "Right Lower Leg Stretch": 1}
for f in ("Index", "Middle", "Ring", "Little", "Thumb"):
    for i in (1, 2, 3):
        TPOSE[f"LeftHand.{f}.{i} Stretched"] = TPOSE[f"RightHand.{f}.{i} Stretched"] = 1
r = u.bone_rotations(TPOSE)
check(all(np.allclose(q, [0, 0, 0, 1], atol=1e-6) for q in r.values()), "the T-pose muscle values give the T-pose",
      {k: q for k, q in r.items() if not np.allclose(q, [0, 0, 0, 1], atol=1e-6)})

# directions (glTF: she faces +z, her left is +x, up is +y)
P = fk(dict(TPOSE, **{"Left Arm Down-Up": -0.25, "Right Arm Down-Up": -0.25}))
check(P["leftHand"][1] < P["leftUpperArm"][1] - 0.15 and P["leftHand"][0] > 0, "Arm Down-Up -0.25: left arm hangs down on her left", P["leftHand"])
check(P["rightHand"][1] < P["rightUpperArm"][1] - 0.15 and P["rightHand"][0] < 0, "and the right arm on her right", P["rightHand"])
P = fk(dict(TPOSE, **{"Left Upper Leg Front-Back": -0.4}))
check(P["leftFoot"][2] > 0.3, "Upper Leg Front-Back below the T-pose value lifts the leg forward", P["leftFoot"])
P = fk(dict(TPOSE, **{"Left Lower Leg Stretch": 0.0}))
check(P["leftFoot"][2] < -0.2, "a bent knee takes the foot back", P["leftFoot"])
P = fk(dict(TPOSE, **{"Spine Front-Back": -1}))
check(P["head"][2] > 0.15, "Spine Front-Back -1 bends her forward", P["head"])
P = fk(dict(TPOSE, **{"Spine Left-Right": -1}))
check(P["head"][0] > 0.15, "Spine Left-Right -1 bends her to her left", P["head"])
P = fk(dict(TPOSE, **{"Left Arm Down-Up": -0.25, "Left Arm Front-Back": -0.5}))
check(P["leftHand"][2] > 0.15, "Arm Front-Back below the T-pose value brings the arm forward", P["leftHand"])
P = fk(dict(TPOSE, **{"Left Arm Down-Up": -0.25, "Left Forearm Stretch": -0.5}))
check(P["leftHand"][2] > P["leftLowerArm"][2] + 0.1, "a bent elbow brings the hand forward", (P["leftHand"], P["leftLowerArm"]))

# a clip: Hermite sampling, a held pose, the .vrma it writes, and a .unitypackage read without unpacking
curves, loop = u.read_curves(anim({"Spine Front-Back": 0.5, "RootT.y": 1.0}, times=(0, 1)))
check(not loop and abs(u.sample(curves["Spine Front-Back"], 0.5) - 0.5) < 1e-9, "curves read and sample", curves)
fr = u.frames(u.read_curves(anim({"Head Nod Down-Up": -0.5}))[0], 30, hold=2.0)
check(len(fr) == 61 and abs(fr[-1][0] - 2.0) < 1e-9, "a single pose is held for --hold seconds", len(fr))
tmp = tempfile.mkdtemp()
out = os.path.join(tmp, "x.vrma")
u.write_vrma(out, fr, SK)
with open(out, "rb") as f:
    data = f.read()
n = struct.unpack("<I", data[12:16])[0]
gl = json.loads(data[20:20 + n])
ext = gl["extensions"]["VRMC_vrm_animation"]
check(data[:4] == b"glTF" and struct.unpack("<I", data[8:12])[0] == len(data) and ext["specVersion"] == "1.0"
      and {"hips", "head", "leftHand", "rightToes"} <= set(ext["humanoid"]["humanBones"]), "a valid .vrma", ext)
check(gl["accessors"][0]["max"] == [2.0] and any(c["target"]["path"] == "translation" for c in gl["animations"][0]["channels"]),
      "with times and the hips' position")

pkg = os.path.join(tmp, "p.unitypackage")
with tarfile.open(pkg, "w:gz") as tar:
    for guid, name, body in (("aa11", "Assets/Pose.anim", anim({"Head Nod Down-Up": -0.3}).encode()),
                             ("bb22", "Assets/readme.txt", b"hello")):
        for leaf, content in (("pathname", name.encode()), ("asset", body)):
            ti = tarfile.TarInfo(f"{guid}/{leaf}"); ti.size = len(content)
            tar.addfile(ti, io.BytesIO(content))
found = u.anims_in_package(pkg)
check([n for n, _ in found] == ["Pose"], "the animation clips in a .unitypackage are found", found)

# the shipped conversions are what the tool makes from the creators' packages (kept here when they're uploaded)
for src, made in (("af41ceb5-stand-me.unitypackage", {"stand-me": "Cute_surprise@surprise.vrma"}),
                  ("9345d4e8-USANEKO_AFK_Siusiu.unitypackage", {"SiuSiu_afk_usaneko_init": "Sleeping_waiting_start@sleep.vrma",
                                                                "SiuSiu_afk_usaneko_loop": "Sleeping_waiting@sleeping.vrma",
                                                                "SiuSiu_afk_usaneko_finish": "Sleeping_waiting_end@sleepend.vrma"})):
    path = os.path.join("/mnt/user-data/uploads", src)
    if not os.path.exists(path):
        continue
    for name, text in u.anims_in_package(path):
        dest = os.path.join(tmp, name + ".vrma")
        u.write_vrma(dest, u.frames(u.read_curves(text)[0], 30, 3.0), SK)
        with open(dest, "rb") as a, open(os.path.join(ROOT, "server", "avatar_defaults", "animations", made[name]), "rb") as b:
            check(a.read() == b.read(), f"{made[name]} is what the tool makes from {name}")
done()
