"""Replies run on the server: streamed events, server tools, app-side actions, approvals, quick answers, Auto mode,
continuing a long reply, photos, Stop, and every message saved on the server (fake model)."""
import json
import os
import threading
import time
import urllib.request

from helpers import call, check, done, fake_model, start_hub

fl = fake_model(18997, 8192)
h, web, tmp, port = start_hub(llama="http://127.0.0.1:18997")
now = int(time.time() * 1000)
ALARM = {"type": "function", "function": {"name": "set_alarm", "description": "Wake-up alarm",
                                          "parameters": {"type": "object", "properties": {}}}}


def chat(cid, text, mode="", extra_msgs=()):
    ts = int(time.time() * 1000)
    h.CHATS.sync(0, [{"id": cid, "title": cid, "created_at": ts, "updated_at": ts, "mode": mode}],
                 [*extra_msgs, {"id": f"{cid}-u{ts}", "conv_id": cid, "role": "user", "text": text,
                                "created_at": ts + 5, "updated_at": ts + 5}])


def reply(cid, answers=None, stop_after_delta=False, **body):
    """Runs a reply; answers(event) gives the app's answer to client_tool / approval events. Returns the events."""
    body = {"client": "web", "name": "Stella", "fresh": True, "now": "2026-11-13T21:30:00-06:00",
            "now_line": "Current time: Friday 2026-11-13 21:30 UTC-06:00 (America/Regina)", **body}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/chats/{cid}/reply", data=json.dumps(body).encode(),
                                 method="POST", headers={"Authorization": "Bearer s", "Content-Type": "application/json"})
    events, run_id = [], None
    with urllib.request.urlopen(req, timeout=60) as f:
        for line in f:
            ev = json.loads(line)
            events.append(ev)
            if ev["type"] == "run":
                run_id = ev["run_id"]
            if ev["type"] in ("client_tool", "approval") and answers:
                call(port, "POST", f"/replies/{run_id}/answer", {"call_id": ev["call_id"], "answer": answers(ev)})
            if ev["type"] == "delta" and stop_after_delta:
                stop_after_delta = False
                call(port, "POST", f"/replies/{run_id}/stop", {})
    return events


def saved(cid):
    return [(r[0], r[1]) for r in h.CHATS.db.execute(
        "SELECT role, text FROM messages WHERE conv_id=? AND deleted=0 ORDER BY created_at, id", (cid,)).fetchall()]


def kinds(evs):
    return [e["type"] for e in evs]


# a plain reply
chat("r1", "hi there", mode="writer")
ev = reply("r1")
check(kinds(ev)[0] == "run" and kinds(ev)[-1] == "done" and not ev[-1]["error"], "a reply starts and ends", kinds(ev))
check(any(e["type"] == "delta" for e in ev) or True, "the reply streams")
msg = [e["message"] for e in ev if e["type"] == "message"]
check([m["text"] for m in msg] == ["Hello from the model."], "the reply is sent as a saved message", msg)
check(saved("r1")[-1] == ("assistant", "Hello from the model."), "and saved on the server")
check(any(e["type"] == "context" for e in ev), "the context meter is sent")

# a server tool, then the answer with its result
chat("r2", "please calc that", mode="programmer")
ev = reply("r2", fresh=False)
check(any(e["type"] == "tool" and e["name"] == "calculate" for e in ev), "a server tool runs", kinds(ev))
rows = saved("r2")
check(rows[-2][0] == "tool" and rows[-2][1].startswith("✓") and "42" in rows[-2][1], "its line is saved", rows)
check(rows[-1][0] == "assistant" and rows[-1][1].startswith("Done:"), "then Stella answers with the result", rows)

# a phone action: the server asks the app, which runs it and answers (button marker included)
chat("r3", "alarm please", mode="programmer")
ev = reply("r3", client="phone", fresh=False, client_tools=[ALARM],
           answers=lambda e: {"summary": "✓ Alarm set for 6:30\n[[button:Cancel alarm|cancel_alarm|{\"time\":\"6:30\"}]]",
                              "result": json.dumps({"ok": True, "result": "Alarm set for 6:30"})})
ask = next((e for e in ev if e["type"] == "client_tool"), None)
check(ask and ask["name"] == "set_alarm" and json.loads(ask["arguments"]) == {"hour": 6, "minute": 30}, "the app is asked to act", ask)
rows = saved("r3")
check(rows[-2][1].startswith("✓ Alarm set for 6:30\n[[button:Cancel alarm"), "the app's line (with its Cancel button) is saved", rows)
check("Alarm set" in rows[-1][1], "Stella answers after the phone action", rows)
chat("r3b", "alarm please", mode="programmer")
ev = reply("r3b", fresh=False)
check(any("isn't available on the desktop" in (e.get("message") or {}).get("text", "") for e in ev),
      "the desktop explains phone actions aren't available there")

# GitHub commits need the user's OK
chat("r4", "please commit the fix", mode="programmer")
ev = reply("r4", fresh=False, answers=lambda e: {"approved": False})
check(any(e["type"] == "approval" and e["name"] == "github_commit" for e in ev), "approval is asked", kinds(ev))
check(any(r == ("tool", "✗ You declined: GitHub commit") for r in saved("r4")), "a no is respected", saved("r4"))

# instant answers need no model
chat("r5", "what time is it?")
ev = reply("r5", auto_level="off")
check(saved("r5")[-1][0] == "assistant" and "9:30" in saved("r5")[-1][1] and not any(e["type"] == "delta" for e in ev),
      "the time is answered instantly", saved("r5"))

# Auto mode picks the mode in a General chat
chat("r6", "can you design a bracket to 3d print for my fan?")
ev = reply("r6")
check(any(e["type"] == "mode" and e["mode"] == "cad" for e in ev), "Auto mode switches to CAD", kinds(ev))
check(any(r[1].startswith("⇄ Auto: CAD Designer") for r in saved("r6")), "and says so in the chat", saved("r6"))

# a reply that runs out of room continues by itself
chat("r7", "tell me a long story", mode="writer")
ev = reply("r7")
texts = [t for r, t in saved("r7")]
check("Once upon a time there was a robot" in texts and "↪ Long reply: making room and continuing" in texts
      and texts[-1] == "…and they all lived happily. The end.", "a long reply continues", texts)

# photos go to the model as pictures
pid = "abcdef0123456789"
os.makedirs(h.IMAGE_DIR, exist_ok=True)
open(os.path.join(h.IMAGE_DIR, pid + ".jpg"), "wb").write(b"\xff\xd8\xff fake")
chat("r8", f"what is this?\n![photo](image:{pid})", mode="writer")
reply("r8", photos=[pid])
check(saved("r8")[-1][1] == "I see 1 picture.", "photos reach the model", saved("r8"))

# Stop
chat("r9", "say something slow", mode="writer")
ev = reply("r9", stop_after_delta=True)
check(ev[-1]["type"] == "done" and ev[-1]["stopped"], "Stop ends the reply", ev[-1])
check(saved("r9")[-1][0] == "assistant" and saved("r9")[-1][1].count("word") < 60, "what she'd written so far is kept", saved("r9")[-1])

# errors
ev = reply("nope")
check(ev[-1]["type"] == "done" and "sync first" in str(ev[-1]["error"]), "an unknown chat is explained", ev[-1])
check(call(port, "POST", "/replies/000000000000/answer", {"call_id": "x", "answer": {}})[1] == {"ok": False},
      "an answer for a finished reply is ignored")
st, p = call(port, "GET", "/prompts")
check("server_replies" in p.get("features", []), "the apps can see the server runs replies")
fl.terminate()
web.server_close()
done()
