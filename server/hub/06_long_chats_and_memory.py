# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Long chats: pinned notes, recall, and summaries made in the background ------------------------------------
# Pinned notes are facts for one chat (goals, decisions, names, story facts) that always ride along with it and
# are never summarized away. Recall finds the exact words in the part of a chat that was summarized to make room.
# The background summarizer keeps busy chats from ever filling up: while Stella is idle it folds the oldest part
# of any chat that's past ~60% of her context into its rolling summary, so nobody waits for "Refreshing context".
NOTE_TRIGGER = re.compile(r"\b(pin|note (that|this|down)|remember (that|this) for (this|the) chat|don'?t forget|"
                          r"we decided|let'?s go with|from now on|keep in mind|the (main )?character|is called|"
                          r"our goal)\b", re.I)
SUMMARIZER = {"last_run": 0.0, "made": 0, "error": "", "busy": False}
SUMMARIZE_AT = 0.60          # start folding at 60% of the context window
SUMMARIZE_KEEP = 0.35        # keep about 35% of it as recent, word-for-word history


def chat_notes_section(conv_id):
    notes = CHATS.chat_notes(conv_id) if conv_id else []
    if not notes:
        return ""
    return ("## Pinned notes for this chat\nAlways true here unless the user changes them (pinned by the user or "
            "you; update with pin_note):\n" + "\n".join(f"- [{n['id']}] {n['text']}" for n in notes))


def last_summary_ts(conv_id):
    with CHATS.lock:
        r = CHATS.db.execute("SELECT MAX(created_at) FROM messages WHERE conv_id=? AND role='summary' AND deleted=0",
                             (conv_id,)).fetchone()
    return int(r[0]) if r and r[0] else 0


def tool_pin_note(args):
    cid = str(args.get("conversation_id") or "")
    if not cid:
        raise ValueError("pinned notes need a chat (this app is too old to send it)")
    action = str(args.get("action", "add")).lower()
    if action == "list":
        return {"notes": CHATS.chat_notes(cid)}, "Read the pinned notes"
    if action == "remove":
        ok = CHATS.delete_chat_note(cid, str(args.get("note_id", "")).strip("[] "))
        return {"removed": ok}, "Unpinned a note" if ok else "No such note"
    if action == "update":
        n = CHATS.update_chat_note(cid, str(args.get("note_id", "")).strip("[] "), args.get("text", ""))
        return {"updated": n}, f"Updated a pinned note: {n['text']}"
    n = CHATS.add_chat_note(cid, args.get("text", ""), "stella")
    return {"pinned": n}, f"Pinned: {n['text']}"


def tool_recall_chat(args):
    cid = str(args.get("conversation_id") or "")
    query = str(args.get("query", "")).strip()
    if not cid or not query:
        raise ValueError("give query (and this chat)")
    cut = last_summary_ts(cid)
    CHAT_SEARCH.index_now(cid)
    hits = CHAT_SEARCH.search(query, k=4, only=cid, before=cut or None)
    if not hits and cut:
        hits = CHAT_SEARCH.search(query, k=3, only=cid)
    return {"found": len(hits), "summarized_before": cut or None,
            "passages": [{"when": h["date"], "text": h["excerpt"]} for h in hits],
            "note": "These are the exact earlier words of this chat; trust them over the summary."}, \
        f"Looked back in this chat for “{query}”: {len(hits)} passage{'s' if len(hits) != 1 else ''}"


SERVER_TOOLS["pin_note"] = (tool_pin_note, _fn(
    "pin_note",
    "Pin a short fact to THIS chat so it's never forgotten or summarized away: a goal, a decision, a name, a "
    "number, a story fact, a rule the user set for this chat. Pin when something is decided or must stay true; "
    "one fact per note. action: add (default), update (note_id + text), remove (note_id), list.",
    {"text": _p("The fact, one sentence, e.g. 'The bracket is 70 x 70 mm, 3 mm PETG'"),
     "action": _p("add, update, remove or list"), "note_id": _p("For update/remove: the id in brackets")}, []))
SERVER_TOOLS["recall_chat"] = (tool_recall_chat, _fn(
    "recall_chat",
    "Look up the exact earlier words of THIS chat (code, numbers, names, what was agreed) from the part that was "
    "summarized to free memory. Use it when the summary is too vague for what the user is asking.",
    {"query": _p("Keywords to look for, e.g. 'resistor values for the LED'")}, ["query"]))


def chat_tools_for(chat_id, query):
    """The long-chat tools that make sense for this chat right now."""
    if not chat_id:
        return []
    out = []
    with CHATS.lock:
        users = CHATS.db.execute("SELECT COUNT(*) FROM messages WHERE conv_id=? AND role='user' AND deleted=0",
                                 (chat_id,)).fetchone()[0]
    if users >= 4 or CHATS.chat_notes(chat_id) or NOTE_TRIGGER.search(query or ""):
        out.append("pin_note")
    if last_summary_ts(chat_id):
        out.append("recall_chat")
    return out


def _chat_style(conv_mode):
    if conv_mode == "writer":
        return "story"
    if conv_mode.startswith("c-"):
        with CHATS.lock:
            r = CHATS.db.execute("SELECT style FROM modes WHERE id=?", (conv_mode,)).fetchone()
        return r[0] if r and r[0] in ("work", "story") else "work"
    return "work"


def summarize_in_background(conv_id, n_ctx=None, force=False):
    """Folds the oldest part of one chat into its rolling summary if it's past SUMMARIZE_AT. True if it did."""
    n_ctx = n_ctx or context_size()
    with CHATS.lock:
        conv = CHATS.db.execute("SELECT mode FROM conversations WHERE id=? AND deleted=0", (conv_id,)).fetchone()
        if not conv:
            return False
        rows = CHATS.db.execute(
            "SELECT id, role, text, created_at FROM messages WHERE conv_id=? AND deleted=0 ORDER BY created_at, id",
            (conv_id,)).fetchall()
    last = max((i for i, r in enumerate(rows) if r[1] == "summary"), default=-1)
    previous = rows[last][2] if last >= 0 else ""
    after = [r for r in rows[last + 1:] if r[1] in ("user", "assistant", "tool")]
    est = [int(len(r[2]) / 3.3) + 6 for r in after]
    total = sum(est) + int(len(previous) / 3.3)
    if not force and total < n_ctx * SUMMARIZE_AT:
        return False
    # keep the newest ~35% word for word (at least the last two exchanges); fold everything before it
    keep, i = 0, len(after)
    while i > 0 and keep + est[i - 1] <= n_ctx * SUMMARIZE_KEEP:
        i -= 1
        keep += est[i]
    users = [j for j, r in enumerate(after) if r[1] == "user"]
    if len(users) < 3:
        return False
    cut = min(i, users[-2])
    while cut > 0 and after[cut][1] != "user":    # never split a question from its answer
        cut -= 1
    if cut <= 0:
        return False
    part = after[:cut]
    msgs = [{"role": r[1], "content": re.sub(r"!\[[^\]]*\]\((image|diagram):[0-9a-f]+\)", "[picture]", r[2])}
            for r in part]
    notes = chat_notes_section(conv_id)
    if notes:   # pinned notes stay pinned: the summary needn't repeat them
        msgs.insert(0, {"role": "summary", "content": "(Pinned separately, don't repeat:) " + notes})
    summary = summarize_chat(previous, msgs, _chat_style(conv[0] or ""))
    prev_ts, next_ts = part[-1][3], after[cut][3]
    created = (prev_ts + next_ts) // 2 if next_ts - prev_ts >= 2 else next_ts - 1
    now = int(time.time() * 1000)
    with CHATS.lock:
        upd = max(now, (CHATS.db.execute("SELECT COALESCE(MAX(updated_at),0) FROM messages WHERE conv_id=?",
                                         (conv_id,)).fetchone()[0] or 0) + 1)
        CHATS.db.execute(
            "INSERT INTO messages (id, conv_id, role, text, reasoning, created_at, updated_at, seq, deleted) "
            "VALUES(?,?,?,?,?,?,?,?,0)", (uuid.uuid4().hex, conv_id, "summary", summary.strip(), "", created, upd,
                                         CHATS._next()))
        CHATS.db.commit()
    log(f"summarized the oldest {len(part)} messages of a chat in the background ({total} → ~{keep} tokens)")
    return True


def summarizer_loop():
    """While Stella is idle, keeps every recently used chat under SUMMARIZE_AT of her context window."""
    time.sleep(90)
    while True:
        try:
            if server_idle(120) and _llama_up():
                n_ctx = context_size()
                since = int((time.time() - 3 * 86400) * 1000)
                with CHATS.lock:
                    convs = [r[0] for r in CHATS.db.execute(
                        "SELECT id FROM conversations WHERE deleted=0 AND updated_at > ? ORDER BY updated_at DESC "
                        "LIMIT 30", (since,))]
                    recent = {r[0] for r in CHATS.db.execute(
                        "SELECT DISTINCT conv_id FROM messages WHERE updated_at > ?", (since,))}
                for cid in convs + [c for c in recent if c not in convs][:20]:
                    if not server_idle(120):
                        break
                    SUMMARIZER["busy"] = True
                    try:
                        if summarize_in_background(cid, n_ctx):
                            SUMMARIZER["made"] += 1
                    finally:
                        SUMMARIZER["busy"] = False
                SUMMARIZER["last_run"] = time.time()
        except Exception as e:      # never stop for one chat
            SUMMARIZER["error"] = str(e)[:200]
            log(f"background summary skipped: {e}")
        time.sleep(120)


def _llama_up():
    try:
        with urllib.request.urlopen(LLAMA_URL.rstrip("/") + "/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


# ---- Automatic memory with a review list ------------------------------------------------------------------------
# The server reads chats that have gone quiet and picks out lasting facts (on the helper model when it runs). In
# "review" mode (the default) they wait in a list for you to keep or drop; "auto" saves them straight away; "off"
# doesn't look at all. Every device sees the same list.
MEMORY_MODES = ("review", "auto", "off")
LEARNER = {"made": 0, "error": ""}


def memory_settings():
    try:
        with open(os.path.join(MOVE["base"] or ".", "memory-settings.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    mode = d.get("mode") if d.get("mode") in MEMORY_MODES else "review"
    return {"mode": mode}


def save_memory_settings(data):
    mode = str(data.get("mode", "")).lower()
    if mode not in MEMORY_MODES:
        raise ValueError("mode is review, auto or off")
    with open(os.path.join(MOVE["base"] or ".", "memory-settings.json"), "w") as f:
        json.dump({"mode": mode}, f)
    return memory_settings()


def learner_loop():
    """Every few minutes, while Stella is idle: learn from chats that have been quiet for 20 minutes."""
    time.sleep(180)
    while True:
        try:
            if memory_settings()["mode"] != "off" and server_idle(120) and _llama_up():
                quiet = int((time.time() - 20 * 60) * 1000)
                oldest = int((time.time() - 3 * 86400) * 1000)
                with CHATS.lock:
                    rows = CHATS.db.execute(
                        "SELECT conv_id, MAX(updated_at) FROM messages WHERE deleted=0 AND role='user' "
                        "GROUP BY conv_id HAVING MAX(updated_at) BETWEEN ? AND ? ORDER BY 2 DESC LIMIT 20",
                        (oldest, quiet)).fetchall()
                for cid, last in rows:
                    if not server_idle(120):
                        break
                    with CHATS.lock:
                        done = CHATS.db.execute("SELECT v FROM meta WHERE k=?", ("learned:" + cid,)).fetchone()
                    if done and int(done[0]) >= last:
                        continue
                    with CHATS.lock:
                        msgs = [{"role": r[0], "text": r[1]} for r in reversed(CHATS.db.execute(
                            "SELECT role, text FROM messages WHERE conv_id=? AND deleted=0 AND role IN "
                            "('user','assistant') ORDER BY created_at DESC LIMIT 16", (cid,)).fetchall())]
                    res = learn_memories(msgs, conv_id=cid)
                    LEARNER["made"] += len(res.get("added", [])) + len(res.get("suggested", []))
                    with CHATS.lock:
                        CHATS.db.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", ("learned:" + cid, str(last)))
                        CHATS.db.commit()
        except Exception as e:
            LEARNER["error"] = str(e)[:200]
            log(f"memory learning skipped: {e}")
        time.sleep(300)


