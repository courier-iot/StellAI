"""Circuit as code (fake SKiDL, KiCad and Freerouting): build, rebuild, guard rails, recipes, the library."""
import os

from helpers import FAKES, check, done, start_hub

os.environ["STELLAI_SKIDL_PYTHON"] = os.path.join(FAKES, "skpython")
os.environ["STELLAI_KICAD_PYTHON"] = os.path.join(FAKES, "kpython-circuit")
os.environ["STELLAI_JAVA"] = os.path.join(FAKES, "java")
h, web, tmp, port = start_hub(extra=["--blender", os.path.join(FAKES, "blender")])
os.makedirs(os.path.join(tmp, "freerouting"))
jar = os.path.join(tmp, "freerouting", "freerouting-2.1.0.jar")
open(jar, "w").write("jar")
run = h.run_server_tool
check(run("pcb_library", {"action": "list"})[0]["tools"] == {"skidl": True, "kicad_python": True, "autorouter": True},
      "the circuit tools are found")
code = '''vcc, gnd = Net('VCC'), Net('GND')
r = Part('Device', 'R', value='330', footprint='Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal')
d = Part('Device', 'LED', footprint='LED_THT:LED_D5.0mm')
j = Part('Connector', 'Conn_01x02', footprint='Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical')
vcc += r[1], j[1]; r[2] & d['A']; d['K'] += gnd, j[2]
'''
r, s = run("pcb_circuit", {"goal": "LED with resistor on a 2-pin header", "code": code, "workspace": "cyber-led"})
check(r.get("version") == 1 and r.get("parts") == 3 and r["routing"]["routed"] and r.get("image"), "build a board from a circuit", r)
check([u["ref"] for u in r.get("verify_with_user", [])] == ["J3"], "unconfirmed pinouts are flagged", r)
r, s = run("pcb_circuit", {"goal": "again", "code": code})
check(r.get("version") == 2 and r["placed"]["kept_places"] == 1, "a rebuild keeps placed parts", r)
r, s = run("pcb_circuit", {"goal": "bad", "code": "boom()"})
check("NameError" in str(r.get("error")) and "still v2" in str(r.get("note")), "a failing circuit saves nothing", r)
r, s = run("pcb_circuit", {"goal": "evil", "code": "generate_netlist(file_='/etc/x')"})
check("isn't allowed" in str(r.get("error")), "netlist writing is blocked", r)
r, s = run("pcb_circuit", {"goal": "evil2", "code": "import os\nos.system('x')"})
check("isn't allowed" in str(r.get("error")), "unsafe circuit code is blocked", r)
run("pcb_library", {"action": "save_recipe", "name": "led-indicator", "description": "LED + 330R", "code": "@subcircuit\ndef led_indicator(a, k):\n    r = Part('Device','R',value='330',footprint='Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal')\n    a += r[1]\n"})
run("pcb_library", {"action": "confirm_recipe", "name": "led-indicator"})
run("pcb_library", {"action": "confirm_pinout", "part": "Connector:Conn_01x02", "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "pins": "1=VCC 2=GND"})
run("pcb_library", {"action": "note", "text": "LED footprint pin 1 is K on LED_D5.0mm"})
r, s = run("pcb_circuit", {"goal": "with recipe", "code": code, "recipes": ["led-indicator"]})
check(r.get("version") == 3 and not r.get("verify_with_user"), "confirmed pinouts aren't flagged again", r)
L = run("pcb_library", {"action": "list"})[0]
check(L["recipes"][0]["uses"] == 1 and len(L["confirmed_pinouts"]) == 1 and len(L["notes"]) == 1, "the library", L)
check(not run("pcb_circuit", {"goal": "x", "code": "x", "recipes": ["nope"]})[0]["ok"], "unknown recipes are refused")
os.remove(jar)
r, s = run("pcb_circuit", {"goal": "no router", "code": code})
check(not r["routing"]["routed"] and "isn't installe" in r["routing"]["note"], "without the autorouter the board is still built", r)
h._blender_stop()
web.server_close()
done()
