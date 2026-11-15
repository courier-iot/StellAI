# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Background jobs: things Stella does on a schedule while you're away -------------------------------------------
# A job is a request run at set times ("every weekday at 7:30: summarize last night's GitHub builds"). Each run is a
# normal reply in the job's own chat (so you can read it and carry on there), and a reminder rings on your phones
# when it's done. Ask Stella ("every morning at 7, ...") or use Settings → Background jobs. Times are this server's.
JOB_REPEATS = ("once", "hourly", "daily", "weekdays", "weekly")
JOBS_MAX = 30
JOBS = {"lock": threading.Lock(), "running": set()}
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _jobs_path():
    return os.path.join(MOVE["base"] or ".", "jobs.json")


def jobs_list():
    try:
        with open(_jobs_path(), encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        items = []
    for j in items:
        j["running"] = j["id"] in JOBS["running"]
        j["next_run"] = job_next_run(j)
    return items


def _jobs_save(items):
    keep = [{k: v for k, v in j.items() if k not in ("running", "next_run")} for j in items]
    with open(_jobs_path() + ".tmp", "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=1)
    os.replace(_jobs_path() + ".tmp", _jobs_path())


def job_next_run(job, now=None):
    """The next time (unix seconds, server's local time) this job is due: the first scheduled time after its last
    run (or after it was made). None when it's off or a one-off that has run."""
    if not job.get("enabled", True):
        return None
    rep = job.get("repeat", "daily")
    last = (job.get("last_run") or 0) / 1000
    ref = last or (job.get("created_at") or 0) / 1000 or (now or time.time())
    hh, mm = (int(x) for x in str(job.get("time") or "08:00").split(":")[:2])
    if rep == "once":
        if last:
            return None
        d = job.get("date") or time.strftime("%Y-%m-%d", time.localtime(ref))
        y, mo, da = (int(x) for x in d.split("-"))
        when = time.mktime((y, mo, da, hh, mm, 0, 0, 0, -1))
        return when if when > ref else time.mktime((y, mo, da + 1, hh, mm, 0, 0, 0, -1))
    t = time.localtime(ref)
    if rep == "hourly":
        minute = int(job.get("minute") or 0)
        when = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, minute, 0, 0, 0, -1))
        return when if when > ref else when + 3600
    for add in range(0, 9):
        when = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + add, hh, mm, 0, 0, 0, -1))
        wday = time.localtime(when).tm_wday
        if rep == "weekdays" and wday >= 5 or rep == "weekly" and wday != int(job.get("weekday") or 0):
            continue
        if when > ref:
            return when
    return None


def job_save(data):
    items = jobs_list()
    jid = str(data.get("id") or "")
    job = next((j for j in items if j["id"] == jid), None) if jid else None
    if not job:
        if len(items) >= JOBS_MAX:
            raise ValueError(f"there are already {JOBS_MAX} jobs: delete one first")
        job = {"id": uuid.uuid4().hex[:10], "created_at": int(time.time() * 1000), "enabled": True, "last_run": 0}
        items.append(job)
    name = " ".join(str(data.get("name") or job.get("name") or "").split())[:60]
    prompt = str(data.get("prompt") or job.get("prompt") or "").strip()[:2000]
    if not name or not prompt:
        raise ValueError("a job needs a short name and what to do")
    rep = str(data.get("repeat") or job.get("repeat") or "daily").lower()
    if rep not in JOB_REPEATS:
        raise ValueError("repeat is once, hourly, daily, weekdays or weekly")
    tm = str(data.get("time") or job.get("time") or "08:00").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?", tm, re.I)
    if not m:
        raise ValueError("time is like 07:30")
    h = int(m.group(1)) % 12 + (12 if m.group(3) and m.group(3)[0].lower() == "p" else 0) if m.group(3) else int(m.group(1))
    if h > 23 or int(m.group(2)) > 59:
        raise ValueError("time is like 07:30")
    wd = data.get("weekday", job.get("weekday", 0))
    if isinstance(wd, str) and not wd.isdigit():
        wd = next((i for i, d in enumerate(DAYS) if d.startswith(wd.strip().lower()[:3])), 0)
    date = str(data.get("date") or job.get("date") or "")
    if rep == "once" and date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("date is like 2026-12-24")
    job.update(name=name, prompt=prompt, repeat=rep, time=f"{h:02d}:{m.group(2)}", weekday=int(wd) % 7, date=date,
               minute=int(data.get("minute", job.get("minute", 0)) or 0) % 60,
               enabled=bool(data.get("enabled", job.get("enabled", True))), notify=bool(data.get("notify", job.get("notify", True))))
    _jobs_save(items)
    return next(j for j in jobs_list() if j["id"] == job["id"])


def job_delete(jid):
    items = jobs_list()
    keep = [j for j in items if j["id"] != str(jid)]
    if len(keep) == len(items):
        return False
    _jobs_save(keep)
    return True


def _job_chat(job):
    """The job's own chat (made on its first run, or again if you deleted it)."""
    cid = job.get("conv_id")
    if cid and _reply_conversation(cid) is not None:
        return cid
    cid = "job-" + job["id"]
    ts = int(time.time() * 1000)
    CHATS.sync(0, [{"id": cid, "title": "Job: " + job["name"], "created_at": ts, "updated_at": ts, "mode": ""}], [])
    return cid


def run_job(jid, name="Stella"):
    """Runs one job now (in this thread). Returns its result text."""
    items = jobs_list()
    job = next((j for j in items if j["id"] == str(jid)), None)
    if not job:
        raise ValueError("no such job")
    with JOBS["lock"]:
        if job["id"] in JOBS["running"]:
            raise ValueError("that job is already running")
        JOBS["running"].add(job["id"])
    try:
        cid = _job_chat(job)
        ts = _reply_ts(cid)
        now = time.strftime("%Y-%m-%dT%H:%M:%S") + time.strftime("%z")[:3] + ":" + time.strftime("%z")[3:]
        text = (f"(Scheduled job “{job['name']}”, running by itself: nobody is watching, so don't ask questions; do "
                f"the work and give the result.)\n\n{job['prompt']}")
        CHATS.sync(0, [], [{"id": str(uuid.uuid4()), "conv_id": cid, "role": "user", "text": text, "created_at": ts,
                            "updated_at": ts, "deleted": False}])
        errors = []
        start_reply(cid, {"client": "web", "name": name, "fresh": True, "now": now, "auto_level": "auto",
                          "now_line": "Current time: " + time.strftime("%A %Y-%m-%d %H:%M") + " (this server's time)"},
                    lambda ev: errors.append(ev.get("error")) if ev.get("type") == "done" and ev.get("error") else None)
        reply = next((m["text"] for m in reversed(_reply_messages(cid)) if m["role"] == "assistant"), "")
        result = ("✗ " + errors[0]) if errors else (reply.strip().splitlines() or [""])[0][:200]
        items = jobs_list()
        for j in items:
            if j["id"] == job["id"]:
                j.update(last_run=int(time.time() * 1000), last_result=result, conv_id=cid)
                if j.get("repeat") == "once":
                    j["enabled"] = False
        _jobs_save(items)
        if job.get("notify", True):
            try:
                CHATS.add_reminder(f"{job['name']} is ready" + (": couldn't finish" if errors else ""),
                                   int(time.time() * 1000) + 5000, "none")
            except (ValueError, sqlite3.Error) as e:
                log(f"job: couldn't add the reminder ({e})")
        log(f"job {job['name']}: {result[:80]}")
        return result
    finally:
        JOBS["running"].discard(job["id"])


def jobs_loop():
    """Runs due jobs (waiting a little if Stella is busy with a reply), and the test set at night."""
    time.sleep(60)
    night_done = ""
    while True:
        try:
            now = time.time()
            for j in jobs_list():
                nxt = j.get("next_run")
                if nxt and nxt <= now and not j["running"]:
                    late = now - nxt
                    with ACTIVITY["lock"]:
                        busy = ACTIVITY["active"] > 0
                    if busy and late < 600:
                        continue          # someone's mid-reply: try again shortly
                    if late > 6 * 3600 and j.get("repeat") != "once":
                        items = jobs_list()   # long missed (server was off): skip to the next time
                        for x in items:
                            if x["id"] == j["id"]:
                                x["last_run"] = int(now * 1000)
                        _jobs_save(items)
                        continue
                    threading.Thread(target=run_job, args=(j["id"],), daemon=True, name="job").start()
            day = time.strftime("%Y-%m-%d")
            if time.localtime().tm_hour in (2, 3, 4) and night_done != day and server_idle(600):
                night_done = day
                threading.Thread(target=nightly_evals, daemon=True, name="evals").start()
        except Exception as e:     # never stop the loop for one bad job
            log(f"jobs: {e}")
        time.sleep(30)


def tool_schedule_job(args):
    action = str(args.get("action") or "add").lower()
    if action == "list":
        items = jobs_list()
        return {"jobs": [{"id": j["id"], "name": j["name"], "repeat": j["repeat"], "time": j["time"],
                          "next_run": time.strftime("%a %Y-%m-%d %H:%M", time.localtime(j["next_run"])) if j.get("next_run") else "off",
                          "last_result": j.get("last_result", "")} for j in items]}, f"{len(items)} background jobs"
    if action in ("remove", "delete", "cancel", "run"):
        key = str(args.get("job") or args.get("name") or "").lower()
        hit = [j for j in jobs_list() if j["id"] == key or key and key in j["name"].lower()]
        if len(hit) != 1:
            raise ValueError("which job? " + ", ".join(j["name"] for j in jobs_list()) if not hit else
                             "more than one job matches: " + ", ".join(j["name"] for j in hit))
        if action == "run":
            threading.Thread(target=run_job, args=(hit[0]["id"],), daemon=True).start()
            return {"started": hit[0]["name"], "note": "It runs in its own chat; a reminder rings when it's done."}, \
                f"Started {hit[0]['name']}"
        job_delete(hit[0]["id"])
        return {"deleted": hit[0]["name"]}, f"Deleted the job {hit[0]['name']}"
    j = job_save(args)
    when = time.strftime("%a %Y-%m-%d %H:%M", time.localtime(j["next_run"])) if j.get("next_run") else "never"
    return {"saved": j["name"], "repeat": j["repeat"], "time": j["time"], "first_run": when,
            "note": "Times are the server's. The result goes into the chat 'Job: " + j["name"] + "' and a reminder "
                    "rings when it's done."}, f"Scheduled {j['name']} ({j['repeat']} {j['time']})"


SERVER_TOOLS["schedule_job"] = (tool_schedule_job, _fn(
    "schedule_job",
    "Background jobs: something you do by yourself at set times while the user is away (a morning summary, checking "
    "builds, watching a price). Each run goes into the job's own chat and a reminder rings when it's done. For a "
    "plain reminder use set_reminder instead.",
    {"action": _p("add (default), list, remove or run"),
     "name": _p("Short name, e.g. Morning briefing"),
     "prompt": _p("What to do each time, written as a full request to yourself"),
     "repeat": {"type": "string", "enum": list(JOB_REPEATS), "description": "How often"},
     "time": _p("24-hour time, e.g. 07:30 (server time)"),
     "weekday": _p("For weekly: monday … sunday"),
     "date": _p("For once: YYYY-MM-DD"),
     "job": _p("For remove/run: its name or id")}, ["action"]))
TOOL_GROUPS["jobs"] = ["schedule_job"]
TOOL_TRIGGERS["jobs"] = re.compile(
    r"\b(every|each) (day|morning|evening|night|week|weekday|hour|monday|tuesday|wednesday|thursday|friday|saturday|"
    r"sunday)\b|\bschedul|\bdaily\b|\bweekly\b|\bhourly\b|background jobs?|\bjobs?\b.*\b(list|cancel|delete|stop)", re.I)
TOOL_GROUP_LABELS["jobs"] = "background jobs that run on a schedule"
