# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Getting better over time: ratings, her test set, lessons from corrections, checking long answers -------------
#  · Thumbs up/down on a reply: a thumbs-down (and a correction like "no, that's wrong") becomes a test case;
#    a thumbs-up keeps a good answer as one that must keep working.
#  · The test set replays every case against the current model (and the bigger one, if set up) and a judge scores
#    the new answer, so a change is measured instead of guessed. It runs when asked, and once a night while idle
#    after the cases or the server changed.
#  · Corrections become lessons: one sentence she reads next time (in a skill's guide, or in her instructions).
#  · Long answers are checked once before they stand: missing parts, contradicting numbers, code that can't run.
EVAL_MAX_CASES = 60
LESSONS_MAX = 40
LESSONS_IN_PROMPT = 8
CORRECTION = re.compile(r"^\s*(no[,.!]|nope\b|wrong\b|that'?s (wrong|not (right|it|what i))|not (right|what i (asked|meant|wanted))|"
                        r"actually[,]?\s|you (got|made|missed|forgot|misread)|incorrect\b|that isn'?t (right|correct)|"
                        r"you'?re wrong|that'?s incorrect)", re.I)
LEARN_STATE = {"tables": False, "eval": None}


def _learn_tables():
    if LEARN_STATE["tables"]:
        return
    with CHATS.lock:
        CHATS.db.executescript("""
            CREATE TABLE IF NOT EXISTS ratings(msg_id TEXT PRIMARY KEY, conv_id TEXT, rating INTEGER, note TEXT,
                created_at INTEGER);
            CREATE TABLE IF NOT EXISTS eval_cases(id TEXT PRIMARY KEY, kind TEXT, conv_id TEXT, msg_id TEXT, mode TEXT,
                history TEXT, reply TEXT, note TEXT, created_at INTEGER, enabled INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS eval_runs(id TEXT PRIMARY KEY, at INTEGER, lane TEXT, model TEXT, passed INTEGER,
                total INTEGER, details TEXT);
        """)
        CHATS.db.commit()
    LEARN_STATE["tables"] = True


def _exchange_before(conv_id, msg_id, turns=3):
    """(mode, history [{role, content}] up to the user message that led to this reply, the reply's text)."""
    msgs = _reply_messages(conv_id)
    idx = next((i for i, m in enumerate(msgs) if m["id"] == msg_id), -1)
    if idx < 0:
        raise ValueError("no such message")
    reply = msgs[idx]["text"]
    hist = [{"role": h["role"], "content": h["content"]} for h in _reply_history(msgs[:idx])]
    users = [i for i, h in enumerate(hist) if h["role"] == "user"]
    hist = hist[users[-turns]:] if len(users) >= turns else hist
    while hist and hist[-1]["role"] != "user":
        hist.pop()
    conv = _reply_conversation(conv_id)
    lanes = _auto_db().get("chats", {}).get(conv_id, {}).get("lanes", [])
    mode = (conv[1] if conv and conv[1] else "") or (lanes[-1]["mode"] if lanes else "general")
    return mode, hist, reply


def add_eval_case(kind, conv_id, msg_id, note=""):
    _learn_tables()
    mode, hist, reply = _exchange_before(conv_id, msg_id)
    if not hist:
        return None
    cid = uuid.uuid4().hex[:10]
    with CHATS.lock:
        CHATS.db.execute("DELETE FROM eval_cases WHERE msg_id=?", (msg_id,))
        CHATS.db.execute("INSERT INTO eval_cases VALUES(?,?,?,?,?,?,?,?,?,1)",
                         (cid, kind, conv_id, msg_id, mode, json.dumps(hist), reply, note[:600], int(time.time() * 1000)))
        CHATS.db.execute("DELETE FROM eval_cases WHERE id NOT IN (SELECT id FROM eval_cases ORDER BY created_at DESC "
                         "LIMIT ?)", (EVAL_MAX_CASES,))
        CHATS.db.commit()
    return cid


def rate_message(msg_id, rating, note=""):
    """Thumbs up (1), down (-1) or none (0) on one of her replies."""
    _learn_tables()
    row = CHATS.db.execute("SELECT conv_id, role FROM messages WHERE id=? AND deleted=0", (msg_id,)).fetchone()
    if not row or row[1] != "assistant":
        raise ValueError("only Stella's replies can be rated")
    rating = 1 if int(rating) > 0 else -1 if int(rating) < 0 else 0
    note = " ".join(str(note or "").split())[:600]
    with CHATS.lock:
        if rating == 0:
            CHATS.db.execute("DELETE FROM ratings WHERE msg_id=?", (msg_id,))
            CHATS.db.execute("DELETE FROM eval_cases WHERE msg_id=?", (msg_id,))
        else:
            CHATS.db.execute("INSERT OR REPLACE INTO ratings VALUES(?,?,?,?,?)",
                             (msg_id, row[0], rating, note, int(time.time() * 1000)))
        CHATS.db.commit()
    case = None
    if rating:
        case = add_eval_case("good" if rating > 0 else "bad", row[0], msg_id, note)
    if rating < 0 and note:
        threading.Thread(target=learn_lesson, args=(row[0], msg_id, note), daemon=True).start()
    return {"rating": rating, "case": case}


def ratings_for(conv_id):
    _learn_tables()
    return {r[0]: r[1] for r in CHATS.db.execute("SELECT msg_id, rating FROM ratings WHERE conv_id=?", (conv_id,))}


def eval_cases():
    _learn_tables()
    rows = CHATS.db.execute("SELECT id, kind, conv_id, msg_id, mode, history, reply, note, created_at, enabled FROM "
                            "eval_cases ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        hist = json.loads(r[5] or "[]")
        req = next((h["content"] for h in reversed(hist) if h["role"] == "user"), "")
        req = re.sub(r"^\[Automatic context[\s\S]*?\[End of context\]\s*", "", str(req))
        out.append({"id": r[0], "kind": r[1], "conv_id": r[2], "msg_id": r[3], "mode": r[4], "request": req[:400],
                    "reply": (r[6] or "")[:400], "note": r[7] or "", "created_at": r[8], "enabled": bool(r[9]),
                    "_history": hist, "_reply": r[6] or ""})
    return out


def eval_runs(limit=10):
    _learn_tables()
    rows = CHATS.db.execute("SELECT id, at, lane, model, passed, total, details FROM eval_runs ORDER BY at DESC LIMIT ?",
                            (limit,)).fetchall()
    return [{"id": r[0], "at": r[1], "lane": r[2], "model": r[3], "passed": r[4], "total": r[5],
             "details": json.loads(r[6] or "[]")} for r in rows]


def delete_eval_case(cid):
    _learn_tables()
    with CHATS.lock:
        n = CHATS.db.execute("DELETE FROM eval_cases WHERE id=?", (str(cid),)).rowcount
        CHATS.db.commit()
    return n > 0


JUDGE_SCHEMA = {"type": "object", "properties": {"pass": {"type": "boolean"}, "score": {"type": "integer"},
                                                 "why": {"type": "string"}}, "required": ["pass", "score", "why"]}


def _model_once(url, messages, max_tokens=1200, schema=None, tools=None, think=False, timeout=600):
    body = {"model": "local", "messages": messages, "max_tokens": max_tokens, "temperature": 0.3 if schema else 0.6,
            "chat_template_kwargs": {"enable_thinking": think}}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        msg = (json.loads(r.read()).get("choices") or [{}])[0].get("message") or {}
    text = REPLY_THINK.sub("", msg.get("content") or "").strip()
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        text += f"\n[calls the {fn.get('name')} tool with {str(fn.get('arguments'))[:400]}]"
    return text.strip()


def _judge(url, case, answer):
    req = case["_history"][-1]["content"] if case["_history"] else ""
    if case["kind"] == "good":
        task = ("The user was happy with the earlier reply below. PASS if the new reply is still correct and about as "
                "helpful (it may be worded differently).")
    else:
        task = ("The user marked the earlier reply below as bad" + (f", saying: {case['note']}" if case["note"] else "")
                + ". PASS only if the new reply avoids that problem and answers the request well.")
    prompt = (f"{task}\n\nRequest:\n{str(req)[-3000:]}\n\nEarlier reply:\n{case['_reply'][:3000]}\n\nNew reply:\n"
              f"{answer[:4000]}\n\nScore the new reply 0-10. Reply with JSON only.")
    raw = _model_once(url, [{"role": "system", "content": "You are a strict, fair reviewer of an assistant's replies."},
                            {"role": "user", "content": prompt}], 300, JUDGE_SCHEMA)
    m = re.search(r"\{[\s\S]*\}", raw)
    v = json.loads(m.group(0)) if m else {}
    return bool(v.get("pass")), int(v.get("score") or 0), str(v.get("why") or "")[:300]


def run_evals(lane="fast"):
    """Replays the test set (in the background); results in eval_runs. Returns the run's summary."""
    _learn_tables()
    state = {"state": "running", "done": 0, "total": 0, "lane": lane, "started": int(time.time() * 1000)}
    LEARN_STATE["eval"] = state
    try:
        url = deep_ensure() if lane == "deep" else LLAMA_URL
        cases = [c for c in eval_cases() if c["enabled"]]
        state["total"] = len(cases)
        details, passed = [], 0
        model = deep_settings().get("label", "deep") if lane == "deep" else (_llama_model_name() or "model")
        for c in cases:
            mode = _reply_mode(c["mode"] or "general")
            system = _reply_system("web", "Stella", "", "", mode, "")
            names = select_tools(c["_history"][-1]["content"] if c["_history"] else "", mode["id"],
                                 mode.get("tools") or "all")
            tools = [SERVER_TOOLS[n][1] for n in names if n in SERVER_TOOLS]
            try:
                answer = _model_once(url, [{"role": "system", "content": system}, *c["_history"]], 1500, None, tools)
                ok, score, why = _judge(url, c, answer)
            except (urllib.error.URLError, OSError, ValueError, RuntimeError, KeyError) as e:
                answer, ok, score, why = "", False, 0, f"couldn't run: {e}"
            passed += ok
            details.append({"case": c["id"], "kind": c["kind"], "request": c["request"][:160], "pass": ok,
                            "score": score, "why": why, "answer": answer[:600]})
            state["done"] += 1
        rid = uuid.uuid4().hex[:10]
        with CHATS.lock:
            CHATS.db.execute("INSERT INTO eval_runs VALUES(?,?,?,?,?,?,?)",
                             (rid, int(time.time() * 1000), lane, model, passed, len(cases), json.dumps(details)))
            CHATS.db.execute("DELETE FROM eval_runs WHERE id NOT IN (SELECT id FROM eval_runs ORDER BY at DESC LIMIT 30)")
            CHATS.db.execute("INSERT OR REPLACE INTO meta VALUES('evals:last', ?)", (_evals_fingerprint(),))
            CHATS.db.commit()
        state.update(state="done", passed=passed)
        return {"passed": passed, "total": len(cases), "model": model}
    except Exception as e:
        state.update(state="failed", error=str(e)[:300])
        return {"error": str(e)}


def _llama_model_name():
    try:
        return os.path.basename(str(_llama("/props", timeout=5).get("model_path") or "")).replace(".gguf", "")
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def _evals_fingerprint():
    _learn_tables()
    n = CHATS.db.execute("SELECT COUNT(*), MAX(created_at) FROM eval_cases WHERE enabled=1").fetchone()
    return f"{n[0]}:{n[1]}:{_llama_model_name()}"


def start_evals(lane="fast"):
    if LEARN_STATE["eval"] and LEARN_STATE["eval"].get("state") == "running":
        return False
    threading.Thread(target=run_evals, args=(lane,), daemon=True, name="evals").start()
    return True


def nightly_evals():
    """Called by the jobs loop at night: replays the test set if the cases or the model changed since last time."""
    _learn_tables()
    row = CHATS.db.execute("SELECT v FROM meta WHERE k='evals:last'").fetchone()
    if CHATS.db.execute("SELECT COUNT(*) FROM eval_cases WHERE enabled=1").fetchone()[0] and \
            (not row or row[0] != _evals_fingerprint()):
        run_evals("fast")


# ---- Lessons from corrections ---------------------------------------------------------------------------------
LESSON_SCHEMA = {"type": "object", "properties": {"lesson": {"type": "string"}, "skill": {"type": "string"}},
                 "required": ["lesson", "skill"]}
LESSON_PROMPT = """The user corrected an AI assistant. Write ONE short lesson (one sentence, an instruction to the assistant
itself) that would have avoided the mistake and will help with similar tasks later, e.g. "Use 3.2 mm clearance holes for
M3 screws unless the user says otherwise." or "Check that FreeCAD is installed before planning a CAD design.".
If the user only changed their mind or preference for this one task, or there's no real mistake, reply with an empty
lesson. "skill": the name of the skill it belongs to, from this list, or "general": {skills}
Reply with JSON only."""


def _lessons_path():
    return os.path.join(MOVE["base"] or ".", "lessons.json")


def lessons():
    try:
        with open(_lessons_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_lessons(items):
    with open(_lessons_path() + ".tmp", "w", encoding="utf-8") as f:
        json.dump(items[-LESSONS_MAX:], f, indent=1)
    os.replace(_lessons_path() + ".tmp", _lessons_path())


def add_lesson(text, skill="general", conv_id=""):
    text = " ".join(str(text or "").split())[:300]
    if len(text) < 8:
        return None
    items = lessons()
    if any(_same_fact(text, x["text"]) for x in items):
        return None
    names = {s["name"] for s in skills_list()}
    item = {"id": uuid.uuid4().hex[:8], "text": text, "skill": skill if skill in names else "general",
            "conv_id": conv_id, "created_at": int(time.time() * 1000)}
    _save_lessons(items + [item])
    return item


def delete_lesson(lid):
    items = lessons()
    keep = [x for x in items if x["id"] != str(lid)]
    if len(keep) == len(items):
        return False
    _save_lessons(keep)
    return True


def lessons_section():
    general = [x["text"] for x in lessons() if x["skill"] == "general"][-LESSONS_IN_PROMPT:]
    return ("## Lessons from past mistakes\n" + "\n".join(f"- {t}" for t in general)) if general else ""


def skill_lessons(name):
    return [x["text"] for x in lessons() if x["skill"] == name][-12:]


def learn_lesson(conv_id, bad_msg_id, correction):
    """Writes a lesson from a correction (and keeps the corrected exchange as a test case)."""
    try:
        _, hist, bad = _exchange_before(conv_id, bad_msg_id)
        req = re.sub(r"^\[Automatic context[\s\S]*?\[End of context\]\s*", "", str(hist[-1]["content"])) if hist else ""
        skills = ", ".join(s["name"] for s in skills_list())
        body = {"model": "local", "temperature": 0.2, "max_tokens": 200,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema", "json_schema": {"name": "lesson", "schema": LESSON_SCHEMA}},
                "messages": [{"role": "system", "content": LESSON_PROMPT.replace("{skills}", skills)},
                             {"role": "user", "content": f"Request:\n{req[-2000:]}\n\nAssistant's reply:\n{bad[:2500]}"
                                                         f"\n\nThe user's correction:\n{correction[:1500]}"}]}
        content = _quick_model(body, timeout=120)["choices"][0]["message"].get("content") or "{}"
        m = re.search(r"\{[\s\S]*\}", content)
        v = json.loads(m.group(0)) if m else {}
        item = add_lesson(v.get("lesson"), str(v.get("skill") or "general"), conv_id)
        if item:
            log(f"lesson learned: {item['text']}")
        return item
    except Exception as e:      # learning is a bonus: never let it break anything
        log(f"lesson skipped: {e}")
        return None


def note_correction(run):
    """After a reply: if the user's message corrected her previous reply, learn from it (in the background)."""
    msgs = _reply_messages(run.conv)
    users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
    if len(users) < 2:
        return False
    last = msgs[users[-1]]
    if not CORRECTION.search(last["text"] or ""):
        return False
    prev = next((m for m in reversed(msgs[:users[-1]]) if m["role"] == "assistant"), None)
    if not prev:
        return False
    try:
        add_eval_case("correction", run.conv, prev["id"], last["text"])
    except (ValueError, sqlite3.Error):
        pass
    threading.Thread(target=learn_lesson, args=(run.conv, prev["id"], last["text"]), daemon=True).start()
    return True


# ---- Checking long answers once ---------------------------------------------------------------------------------
CHECK_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"},
                                                 "problems": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
                "required": ["ok", "problems"]}
CHECK_PROMPT = """You check an assistant's reply before the user reads it. Flag only concrete, objective problems:
a part of the request that wasn't answered, numbers or units that contradict each other or are clearly wrong, code that
can't run as written (syntax, undefined names, missing imports), or steps in the wrong order. Do NOT flag style, length,
wording or things that are a matter of taste. When in doubt, it's ok. Reply with JSON only."""
SELF_CHECK_MIN = 1500


def wants_check(text, mode_id, voice=False):
    if voice or not text:
        return False
    code_lines = sum(b.count("\n") for b in re.findall(r"```[\s\S]*?```", text))
    return len(text) >= SELF_CHECK_MIN or code_lines >= 40 or (mode_id in DEEP_DESIGN_MODES and len(text) >= 800)


def check_reply(request, reply):
    """[problems] found in a reply ([] = fine). Uses Stella's everyday model, which is free once she has answered."""
    raw = _model_once(LLAMA_URL, [{"role": "system", "content": CHECK_PROMPT},
                                  {"role": "user", "content": f"Request:\n{request[-3000:]}\n\nReply:\n{reply[:9000]}"}],
                      300, CHECK_SCHEMA, timeout=120)
    m = re.search(r"\{[\s\S]*\}", raw)
    v = json.loads(m.group(0)) if m else {"ok": True}
    probs = [" ".join(str(p).split())[:200] for p in v.get("problems") or [] if str(p).strip()]
    return [] if v.get("ok", True) and not probs else probs[:3]


def self_check_settings():
    try:
        with open(os.path.join(MOVE["base"] or ".", "self-check.json"), encoding="utf-8") as f:
            return {"enabled": bool(json.load(f).get("enabled", True))}
    except (OSError, ValueError):
        return {"enabled": True}


def save_self_check_settings(data):
    with open(os.path.join(MOVE["base"] or ".", "self-check.json"), "w", encoding="utf-8") as f:
        json.dump({"enabled": bool(data.get("enabled", True))}, f)
    return self_check_settings()
