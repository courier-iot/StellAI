"""The bigger model (deep lane), ratings and the test set, lessons from corrections, checking long answers, background
jobs, the browser tool and saved chat memory (fake models)."""
import json
import os
import stat
import sys
import tempfile
import time
import urllib.request

from helpers import call, check, done, fake_model, start_hub

fl = fake_model(18981, 8192)
fd = fake_model(18982, 8192, name="deep")
h, web, tmp, port = start_hub(llama="http://127.0.0.1:18981")


def chat(cid, text, mode="", extra_msgs=()):
    ts = int(time.time() * 1000)
    h.CHATS.sync(0, [{"id": cid, "title": cid, "created_at": ts, "updated_at": ts, "mode": mode}],
                 [*extra_msgs, {"id": f"{cid}-u{ts}", "conv_id": cid, "role": "user", "text": text,
                                "created_at": ts + 5, "updated_at": ts + 5}])


def reply(cid, **body):
    body = {"client": "web", "name": "Stella", "fresh": False, "now": "2026-11-14T10:00:00-06:00",
            "now_line": "Current time: Saturday 2026-11-14 10:00", "auto_level": "off", **body}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/chats/{cid}/reply", data=json.dumps(body).encode(),
                                 method="POST", headers={"Authorization": "Bearer s", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as f:
        return [json.loads(line) for line in f]


def saved(cid):
    return [(r[0], r[1]) for r in h.CHATS.db.execute(
        "SELECT role, text FROM messages WHERE conv_id=? AND deleted=0 ORDER BY created_at, id", (cid,)).fetchall()]


def seen(p):
    with urllib.request.urlopen(f"http://127.0.0.1:{p}/seen", timeout=5) as f:
        return json.loads(f.read())


def wait_for(fn, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


# ---- the deep lane ------------------------------------------------------------------------------------------------
st, d = call(port, "GET", "/deep")
check(st == 200 and not d["available"] and d["lane"] == "auto", "the bigger model starts switched off", d)
st, d = call(port, "POST", "/deep", {"enabled": True, "url": "http://127.0.0.1:18982", "lane": "design"})
check(st == 200 and d["available"] and d["running"], "it can use a llama-server on another computer", d)
chat("d1", "design a mounting plate 80 by 60 mm with four M3 holes in the corners", mode="cad")
ev = reply("d1")
check(any(e["type"] == "lane" and e["lane"] == "deep" for e in ev) and saved("d1")[-1][1].startswith("[deep]"),
      "design work goes to the bigger model", saved("d1")[-1])
chat("d2", "hi there", mode="writer")
reply("d2")
check(not saved("d2")[-1][1].startswith("[deep]"), "everyday chat stays on the quick model", saved("d2")[-1])
chat("d3", "can you think harder about this one", mode="writer")
reply("d3")
check(saved("d3")[-1][1].startswith("[deep]"), "asking her to think harder uses it", saved("d3")[-1])
call(port, "POST", "/deep", {"url": "http://127.0.0.1:9"})
chat("d4", "design a bracket with two holes 40 mm apart for a fan", mode="cad")
ev = reply("d4")
check(not ev[-1]["error"] and not saved("d4")[-1][1].startswith("[deep]"),
      "if the bigger model is down, the quick one answers", (ev[-1], saved("d4")[-1]))
call(port, "POST", "/deep", {"enabled": False, "url": ""})
st, r = call(port, "POST", "/deep", {"lane": "sometimes"})
check(st == 400, "a bad lane is refused")

c14 = h.deep_candidates(14, 16384, 8)
c20 = h.deep_candidates(20, 16384, 8)
check(c14 and c14[0]["size_gb"] <= 12.6 and "35b" in c14[0]["key"], "14 GB free: the biggest model that still fits", c14[:2])
check(c20 and c20[0]["size_gb"] > c14[0]["size_gb"], "more free memory: a better version", c20[:2])
check(h.deep_candidates(8, 16384, 8) == [], "8 GB free: nothing fits")

# finding one: downloads and measures; tries the next one when one doesn't load
md = os.path.join(tmp, "models")
os.makedirs(md, exist_ok=True)
exe = os.path.join(tmp, "llama-server")
open(exe, "w").close()
h.DEEP.update(models_dir=md, exe=exe, flags="")
h.free_ram_gb = lambda: 22.0
fetched = []


def fake_download(mid, models_dir, state, fetch, vision=True, mtp=True):
    name = mid.replace("|", "-") + ".gguf"
    open(os.path.join(models_dir, name), "wb").write(b"x" * 10)
    fetched.append(mid)
    return {"model_file": name}


h.TUNE.download_model = fake_download
runs = []


def fake_bench(server, model, args, ctx, **kw):
    runs.append((os.path.basename(model), args))
    if len(runs) == 1:
        return {"ok": False, "error": "failed to allocate buffer: out of memory"}
    return {"ok": True, "gen_tps": 6.5, "prompt_tps": 40, "load_s": 30}


job = h.deep_find(bench=fake_bench)
st = h.deep_settings()
check(job["state"] == "done" and st["enabled"] and st["tok_s"] == 6.5 and len(fetched) == 2,
      "it tries the best fit, then the next one when that doesn't load", (job, st, fetched))
check("-ngl" in runs[0][1] and runs[0][1][runs[0][1].index("-ngl") + 1] == "0", "measured on the processor", runs[0][1])
runs.clear()
job = h.deep_find(bench=lambda *a, **k: {"ok": True, "gen_tps": 1.0})
check(job["state"] == "failed" and not h.deep_settings()["enabled"] and "fast enough" in job["text"],
      "too slow everywhere: it stays off and says why", job)
h.free_ram_gb = lambda: 6.0
job = h.deep_find(bench=fake_bench)
check(job["state"] == "failed" and "GB of memory is free" in job["text"], "not enough free memory is explained", job)

# ---- ratings, the test set, lessons ------------------------------------------------------------------------------
chat("e1", "say hello please", mode="writer")
reply("e1")
mid = h.CHATS.db.execute("SELECT id FROM messages WHERE conv_id='e1' AND role='assistant'").fetchone()[0]
st, r = call(port, "POST", f"/messages/{mid}/rate", {"rating": -1, "note": "M3 clearance holes are 3.2 mm"})
check(st == 200 and r["case"], "a thumbs-down becomes a test case", r)
check(wait_for(lambda: any("3.2 mm" in x["text"] for x in h.lessons())), "and a lesson", h.lessons())
st, r = call(port, "GET", f"/chats/e1/ratings")
check(r["ratings"] == {mid: -1}, "the rating is kept for the apps", r)
st, r = call(port, "GET", "/evals")
check(len(r["cases"]) == 1 and r["cases"][0]["request"] == "say hello please" and r["cases"][0]["kind"] == "bad",
      "the test set lists the request", r["cases"])
res = h.run_evals("fast")
check(res.get("total") == 1 and res.get("passed") == 1, "the test set replays it and a reviewer scores it", res)
st, r = call(port, "GET", "/evals")
check(r["runs"] and r["runs"][0]["passed"] == 1 and r["runs"][0]["details"][0]["pass"], "the run is kept", r["runs"][:1])
lesson = next(x for x in h.lessons() if "3.2 mm" in x["text"])
check(lesson["skill"] == "cad-part" and "3.2 mm" in h.tool_use_skill({"name": "cad-part"})[0]["steps"],
      "a lesson tied to a skill is read with that skill")
h.add_lesson("Always say which units you used.", "general")
check("Always say which units you used." in h._reply_system("web", "Stella", "", "", h.BUILT_IN_MODES[0], ""),
      "general lessons are in her instructions")
st, r = call(port, "DELETE", f"/lessons/{lesson['id']}")
check(st == 200 and not any("3.2 mm" in x["text"] for x in r["lessons"]), "a lesson can be forgotten")
check(call(port, "POST", "/messages/nope/rate", {"rating": 1})[0] == 400, "only her replies can be rated")

# a correction in the chat itself
ts = int(time.time() * 1000) - 10000
chat("e2", "no, that's wrong: M3 clearance is 3.2 mm, not 3", mode="writer", extra_msgs=[
    {"id": "e2-u0", "conv_id": "e2", "role": "user", "text": "what size hole for an M3 screw?", "created_at": ts, "updated_at": ts},
    {"id": "e2-a0", "conv_id": "e2", "role": "assistant", "text": "Use a 3 mm hole.", "created_at": ts + 1, "updated_at": ts + 1}])
reply("e2")
check(wait_for(lambda: any(c["kind"] == "correction" and c["msg_id"] == "e2-a0" for c in h.eval_cases())),
      "a correction is kept as a test case")
check(h.CORRECTION.search("Actually, it's the blue one") and not h.CORRECTION.search("Now make it blue"),
      "corrections are recognised")

# ---- checking long answers --------------------------------------------------------------------------------------
chat("c1", "check-me: answer both parts", mode="writer")
ev = reply("c1")
rows = saved("c1")
check(rows[-2] == ("assistant", "Fixed: both parts answered.") and rows[-1][1].startswith("✓ Checked and fixed"),
      "a long answer with a problem is fixed in place", rows[-2:])
call(port, "POST", "/self-check", {"enabled": False})
chat("c2", "check-me: answer both parts", mode="writer")
reply("c2")
check(saved("c2")[-1][1].startswith("Part one answered."), "the check can be turned off")
call(port, "POST", "/self-check", {"enabled": True})
check(not h.wants_check("short", "general") and h.wants_check("```py\n" + "x = 1\n" * 45 + "```", "general"),
      "only long answers or long code are checked")

# ---- background jobs ----------------------------------------------------------------------------------------------
base = time.mktime((2026, 11, 14, 10, 0, 0, 0, 0, -1))      # a Saturday, 10:00
j = {"repeat": "daily", "time": "07:30", "created_at": base * 1000, "enabled": True}
check(time.localtime(h.job_next_run(j))[2:5] == (15, 7, 30), "daily at 7:30, made at 10:00: tomorrow 7:30")
j = {"repeat": "weekdays", "time": "07:30", "created_at": base * 1000, "enabled": True}
check(time.localtime(h.job_next_run(j))[2] == 16, "weekdays: skips Sunday to Monday")
j = {"repeat": "weekly", "time": "18:00", "weekday": 5, "created_at": base * 1000, "enabled": True}
check(time.localtime(h.job_next_run(j))[2:4] == (14, 18), "weekly on Saturday at 18:00: tonight")
j = {"repeat": "once", "time": "09:00", "date": "2026-11-20", "created_at": base * 1000, "enabled": True, "last_run": base * 1000}
check(h.job_next_run(j) is None, "a one-off that ran doesn't run again")
st, r = call(port, "POST", "/jobs", {"name": "Morning hello", "prompt": "Say hello", "repeat": "daily", "time": "7:30 am"})
check(st == 200 and r["job"]["time"] == "07:30" and r["job"]["next_run"], "a job is added", r)
jid = r["job"]["id"]
result = h.run_job(jid)
cid = "job-" + jid
check(any(t.startswith("(Scheduled job") for role, t in saved(cid)) and saved(cid)[-1][0] == "assistant" and result,
      "running it puts the request and her reply in the job's own chat", saved(cid))
check(any("Morning hello is ready" in x["text"] for x in h.CHATS.reminders()), "and a reminder rings")
out, summ = h.tool_schedule_job({"action": "add", "name": "Build check", "prompt": "Check builds", "repeat": "weekly",
                                 "time": "08:00", "weekday": "friday"})
check(out["saved"] == "Build check" and "Fri" in out["first_run"], "Stella can schedule a job herself", out)
out, _ = h.tool_schedule_job({"action": "remove", "job": "build"})
check(out["deleted"] == "Build check", "and remove it")
check(call(port, "POST", "/jobs", {"name": "x", "prompt": "y", "time": "25:00"})[0] == 400, "a bad time is refused")

# ---- the browser tool ---------------------------------------------------------------------------------------------
fake_browser = os.path.join(tmp, "fake-browser")
with open(fake_browser, "w") as f:
    f.write("#!" + sys.executable + "\nimport sys\nargs = sys.argv[1:]\n"
            "shot = [a for a in args if a.startswith('--screenshot=')]\n"
            "if shot:\n    open(shot[0].split('=', 1)[1], 'wb').write(b'\\x89PNG' + b'0' * 2000)\n"
            "else:\n    print('<html><head><title>Shop</title></head><body><p>Price: 42 dollars, built by script</p>'"
            " + '<p>' + 'more text ' * 50 + '</p></body></html>')\n")
os.chmod(fake_browser, os.stat(fake_browser).st_mode | stat.S_IEXEC)
os.environ["STELLAI_BROWSER"] = fake_browser
h.BROWSER["exe"] = None
h._public_host = lambda host: True
out, summ = h.tool_browse_page({"url": "https://shop.example/item", "screenshot": True})
check("Price: 42 dollars" in out["text"] and out["title"] == "Shop" and out.get("image"), "a page is read after its scripts run, with a picture", out)
try:
    h.tool_browse_page({"url": "file:///etc/passwd"})
    check(False, "only web addresses are opened")
except ValueError:
    check(True, "only web addresses are opened")

# ---- saved chat memory --------------------------------------------------------------------------------------------
h.SLOT_MIN_TOKENS = 0
h.SLOTS.update(current="", off_until=0, saved={})
for cid2 in ("s1", "s2", "s1"):
    chat(cid2, "hello again", mode="writer")
    reply(cid2)
sl = seen(18981)["slots"]
check(["save", "stellai-s1.bin"] in sl and ["save", "stellai-s2.bin"] in sl and ["restore", "stellai-s1.bin"] in sl,
      "switching chats saves the one left and restores the one returned to", sl)
check(0 in seen(18981)["id_slot"], "replies use the saved slot")

fl.terminate()
fd.terminate()
web.server_close()
done()
