"""Automatic memory with a review list, the Python code runner, and continuing in a new chat (fake model server)."""
import time

from helpers import check, done, fake_model, start_hub

fl = fake_model(18998, 4096)
h, web, tmp, port = start_hub(llama="http://127.0.0.1:18998")
check(h.memory_settings()["mode"] == "review", "memory starts in review mode")
r = h.learn_memories([{"role": "user", "text": "I always print brackets in PETG"}], conv_id="c9")
check(r["suggested"] == ["Likes PETG for brackets"] and not h.CHATS.memories(), "facts become suggestions", r)
sug = h.CHATS.memory_suggestions()
h.CHATS.resolve_suggestion(sug[0]["id"], True)
check([m["text"] for m in h.CHATS.memories()] == ["Likes PETG for brackets"], "a kept suggestion becomes a memory")
check(h.learn_memories([{"role": "user", "text": "PETG again"}])["suggested"] == [], "duplicates are skipped")
h.save_memory_settings({"mode": "off"})
check(h.learn_memories([{"role": "user", "text": "x"}])["mode"] == "off", "memory can be turned off")
h.save_memory_settings({"mode": "review"})

r, s = h.run_server_tool("run_python", {"code": "import math\nprint(round(1/(2*math.pi*1e3*100e-9), 1))"})
check(r.get("stdout", "").strip() == "1591.5", "the code runner runs maths", r)
try:
    import matplotlib  # noqa: F401
    code = ("import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\nplt.plot([1,2,3],[1,4,9])\n"
            "plt.savefig('plot.png')\nopen('out.csv','w').write('a,b\\n1,2')\nprint('ok')")
    r, s = h.run_server_tool("run_python", {"code": code})
    check(r.get("image") and list(r.get("file_previews", {})) == ["out.csv"], "plots come back as pictures, files as previews", r)
except ImportError:
    print("matplotlib not installed: plot check skipped")
r, s = h.run_server_tool("run_python", {"code": "import subprocess"})
check("isn't allowed" in str(r.get("error")), "blocked imports", r)
r, s = h.run_server_tool("run_python", {"code": "while True: pass", "timeout": 5})
check("time limit" in r.get("stderr", ""), "the time limit", r)
r, s = h.run_server_tool("run_python", {"code": "x = 1/0"})
check("ZeroDivisionError" in r.get("stderr", ""), "errors are shown", r)
r, s = h.run_server_tool("run_python", {"code": "a = bytearray(3 * 1024**3)"})
check("MemoryError" in r.get("stderr", ""), "the memory cap", r)

now = int(time.time() * 1000)
h.CHATS.sync(0, [{"id": "c9", "title": "Bracket", "created_at": now, "updated_at": now, "mode": "cad"}],
             [{"id": "m1", "conv_id": "c9", "role": "user", "text": "make the bracket 70 mm", "created_at": now, "updated_at": now},
              {"id": "m2", "conv_id": "c9", "role": "assistant", "text": "ok 70 mm", "created_at": now + 1, "updated_at": now + 1}])
h.CHATS.add_chat_note("c9", "Material: PETG")
nc = h.continue_in_new_chat("c9")
rows = h.CHATS.db.execute("SELECT role, text FROM messages WHERE conv_id=?", (nc["conversation_id"],)).fetchall()
check(nc["title"] == "Bracket (continued)" and rows[0][0] == "summary" and "Bracket" in rows[0][1], "continue in a new chat", rows)
check([n["text"] for n in h.CHATS.chat_notes(nc["conversation_id"])] == ["Material: PETG"], "pinned notes carry over")
check("run_python" in h.select_tools("hi", "programmer") and h.TOOL_TRIGGERS["code"].search("can you plot the step response"),
      "the code runner is offered when it fits")
fl.terminate()
web.server_close()
done()
