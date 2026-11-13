"""Auto mode: Stella switches modes with the topic, learns from corrections, asks first when told to, folds old topics."""
import os
import tempfile

from helpers import check, done
import stellai_hub as h

h.AUTO_STATE["path"] = os.path.join(tempfile.mkdtemp(), "auto.json")
flow = [
    ("I want to start making an app for tracking my plants", "Programmer", True),
    ("it should have a reminder screen and a photo gallery", "Programmer", False),
    ("what database should the app use?", "Programmer", False),
    ("ok, different project: can you help me design a bracket to hold a 60mm fan on my server?", "CAD Designer", True),
    ("make it 3 mm thick", "CAD Designer", False),
    ("add two M3 holes 50 mm apart", "CAD Designer", False),
    ("why 3.2 mm for the holes?", "CAD Designer", False),
    ("now let's design a cyberpunk PCB for an LED strip controller", "PCB Designer", True),
    ("add a silkscreen logo in the corner", "PCB Designer", False),
    ("use 0.5 mm traces", "PCB Designer", False),
    ("then can you start editing my VRoid avatar model in Blender?", "3D Modeler", True),
    ("make the halo glow brighter", "3D Modeler", False),
    ("back to the app: which screens did we plan?", "Programmer", True),
    ("what's the weather like?", "Programmer", False),
]
for text, want, switch in flow:
    r = h.route_mode({"chat_id": "chat1", "text": text, "level": "auto"})
    check(r["name"] == want and r["switched"] == switch, f"Auto mode for “{text[:40]}”", (r["name"], r["switched"]))
check(h.route_mode({"chat_id": "c2", "text": "design a holder for my phone", "forced": "cad"})["mode"] == "cad", "a forced mode")
check(h.route_mode({"chat_id": "c3", "text": "design a holder for my tablet phone"})["name"] == "CAD Designer", "corrections are learned")
r = h.route_mode({"chat_id": "c4", "text": "check my kicad board for drc errors", "level": "ask"})
check(not r["switched"] and r["suggest"] == "pcb", "the ask level suggests instead of switching", r)
msgs = [{"role": "user", "content": flow[0][0]}, {"role": "assistant", "content": "Use Room for the database. We decided on 3 screens."},
        {"role": "user", "content": flow[3][0]}, {"role": "assistant", "content": "Bracket base is 70 mm x 70 mm, 3 mm thick; saved as v2."},
        {"role": "user", "content": flow[7][0]}, {"role": "assistant", "content": "Board 80 x 40 mm."}]
for m in msgs:
    if m["role"] == "user":
        h.route_mode({"chat_id": "fold", "text": m["content"]})
drop, note = h.fold_lanes("fold", msgs)
check(sorted(drop) == [0, 1, 2, 3] and "We decided on 3 screens" in note, "earlier topics are folded with their facts", (drop, note))
done()
