# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Compact tool descriptions -----------------------------------------------------
# Tool definitions are sent with every message and cost context tokens, so they're kept short here.
_Q = "e.g. 4.7k, 3.3V, 20mA"
COMPACT_TOOLS = {
    "calculate": ("Exact maths; always use instead of mental arithmetic. SI values (4.7k, 10u, 3.3V), sqrt ln log10 exp "
                  "sin cos tan abs round min max, parallel(a,b,…), db(ratio), pi, e.",
                  {"expression": "e.g. 12*4.7k/(4.7k+10k)", "unit": "optional unit of the answer"}),
    "ohms_law": ("Ohm's law and power: give any two of voltage, current, resistance, power.",
                 {"voltage": _Q, "current": _Q, "resistance": _Q, "power": _Q}),
    "voltage_divider": ("Voltage divider (R2 to ground). Give three of vin, vout, r1, r2, or vin+vout for the best "
                        "standard pair.", {"vin": _Q, "vout": _Q, "r1": _Q, "r2": _Q, "series": "E12, E24 or E96"}),
    "led_resistor": ("LED series resistor with standard value and power.",
                     {"supply_voltage": _Q, "led_voltage": "forward voltage, e.g. 2.0 red, 3.1 blue",
                      "current_ma": "mA", "leds_in_series": "default 1"}),
    "rc_filter": ("RC filter cutoff fc=1/(2πRC): give two of resistance, capacitance, frequency.",
                  {"resistance": _Q, "capacitance": _Q, "frequency": _Q}),
    "combine_resistors": ("Resistors in series or parallel.", {"values": "e.g. ['10k','4.7k']", "mode": "series or parallel"}),
    "standard_resistor": ("Nearest standard resistor values.", {"value": _Q, "series": "E12, E24 or E96"}),
    "remember": ("Save a lasting fact about the user for future chats.", {"fact": "one short third-person sentence"}),
    "forget": ("Delete a saved fact about the user.", {"memory": "its id or words from it"}),
    "project_note": ("Save a note (decision, spec, pinout, TODO) to this chat's project.", {"note": "one or two sentences"}),
    "project_search": ("Search this project's files.", {"query": "keywords, e.g. ADC reference voltage"}),
    "library_search": ("Search the user's document library (any chat).", {"query": "plain words"}),
    "set_reminder": ("Reminder that rings on all the user's phones. (Wake-up alarms: set_alarm.)",
                     {"text": "short, e.g. Reflow the board",
                      "when": "ISO 8601 with the user's UTC offset, e.g. 2026-11-04T17:00:00-06:00",
                      "repeat": "none, daily, weekdays, weekly or monthly"}),
    "list_reminders": ("List upcoming reminders.", {}),
    "cancel_reminder": ("Cancel a reminder.", {"reminder": "id or words"}),
    "add_to_list": ("Add items to a named list (shopping, parts, to-do…); created as needed.",
                    {"list": "list name", "items": "items"}),
    "show_list": ("Show a list, or all list names if empty.", {"list": "list name"}),
    "check_off": ("Tick off (or remove) list items.", {"list": "list name", "items": "items or words",
                                                       "remove": "delete instead of ticking"}),
    "web_search": ("Search the web for current info (docs, prices, news, parts, errors).",
                   {"query": "query", "count": "1-10, default 6"}),
    "fetch_page": ("Read a public web page or PDF as text.", {"url": "http(s) URL", "find": "optional words to jump to",
                                                               "max_chars": "default 12000"}),
    "github_repos": ("List the user's GitHub repositories.", {}),
    "github_read": ("Read a file or list a folder on GitHub.", {"repo": "owner/name (optional)", "path": "path; empty = root",
                                                                "ref": "branch/tag/commit (optional)",
                                                                "start_line": "for long files"}),
    "github_search": ("Search code in a GitHub repo.", {"repo": "owner/name (optional)", "query": "what to find"}),
    "github_history": ("Recent commits, optionally of one file.", {"repo": "owner/name (optional)", "path": "optional",
                                                                   "count": "max 30"}),
    "github_issues": ("Issues and pull requests.", {"repo": "owner/name (optional)", "state": "open, closed or all"}),
    "github_builds": ("Recent GitHub Actions builds with the latest failure's error log.", {"repo": "owner/name (optional)"}),
    "github_commit": ("Commit full file contents to a new or existing branch (never the default branch). Needs the user's "
                      "approval. Read files first.", {"repo": "owner/name (optional)", "branch": "e.g. stella/fix-build",
                                                     "message": "commit message", "files": "files to create or replace",
                                                     "delete": "paths to delete (optional)"}),
    "github_pull_request": ("Open a pull request from a branch. Needs the user's approval.",
                            {"repo": "owner/name (optional)", "branch": "branch", "title": "title", "body": "description"}),
}


def _compact_tools():
    for name, (desc, params) in COMPACT_TOOLS.items():
        if name not in SERVER_TOOLS:
            continue
        fn = SERVER_TOOLS[name][1]["function"]
        fn["description"] = desc
        for key, prop in fn["parameters"]["properties"].items():
            if key in params:
                if params[key]:
                    prop["description"] = params[key]
                else:
                    prop.pop("description", None)
            if prop.get("type") == "array" and isinstance(prop.get("items"), dict):
                prop["items"].pop("description", None)


_compact_tools()



# ---- Searching past conversations ---------------------------------------------------
class ChatSearch:
    """Finds earlier conversations by keywords and by meaning. Conversations are cut into small pieces
    (a few messages each) and indexed in the background after they change."""

    def __init__(self):
        self.version = 0
        self.cache = None   # (version, docs, df, avgdl, keys, matrix)
        self.lock = threading.Lock()
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        with CHATS.lock:
            if not CHATS.db.execute("SELECT 1 FROM chat_chunks LIMIT 1").fetchone():   # first run: index everything
                CHATS.db.execute("INSERT OR IGNORE INTO chat_dirty SELECT id FROM conversations WHERE deleted=0")
                CHATS.db.commit()
        threading.Thread(target=self._loop, daemon=True, name="chat-index").start()

    def _loop(self):
        time.sleep(10)
        while True:
            try:
                self.index_dirty()
            except Exception as e:   # never stop indexing for one bad chat
                log(f"chat search: indexing problem ({e})")
            time.sleep(45)

    @staticmethod
    def _pieces(rows):
        """Groups messages into pieces of up to ~1200 characters (a question with its answer, usually)."""
        pieces, cur, ts = [], [], 0
        for role, text, created in rows:
            text = re.sub(r"!\[[^\]]*\]\((?:image|diagram):[0-9a-f]+\)", "[picture]", text or "").strip()
            if not text:
                continue
            who = {"user": "User", "assistant": "Stella", "tool": "Action"}.get(role, role)
            line = f"{who}: {text[:1500]}"
            if cur and (sum(len(x) for x in cur) + len(line) > 1200 or role == "user" and len(cur) >= 4):
                pieces.append(("\n".join(cur), ts))
                cur = []
            if not cur:
                ts = created
            cur.append(line)
        if cur:
            pieces.append(("\n".join(cur), ts))
        return pieces

    def index_dirty(self, limit=50):
        with CHATS.lock:
            dirty = [r[0] for r in CHATS.db.execute("SELECT conv_id FROM chat_dirty LIMIT ?", (limit,))]
        if not dirty:
            return
        for cid in dirty:
            with CHATS.lock:
                alive = CHATS.db.execute("SELECT 1 FROM conversations WHERE id=? AND deleted=0", (cid,)).fetchone()
                rows = CHATS.db.execute(
                    "SELECT role, text, created_at FROM messages WHERE conv_id=? AND deleted=0 AND role IN "
                    "('user','assistant','tool') ORDER BY created_at, id", (cid,)).fetchall() if alive else []
            pieces = self._pieces(rows)
            vecs = [None] * len(pieces)
            if pieces and EMBEDDER.url:
                try:
                    vecs = EMBEDDER.embed([t for t, _ in pieces], EMBEDDER.DOC)
                except (urllib.error.URLError, OSError, ValueError, KeyError):
                    vecs = [None] * len(pieces)   # keywords now; meaning added on a later pass
            with CHATS.lock:
                CHATS.db.execute("DELETE FROM chat_chunks WHERE conv_id=?", (cid,))
                CHATS.db.executemany("INSERT INTO chat_chunks VALUES(?,?,?,?,?,?)",
                                     [(f"{cid}:{i}", cid, i, t, ts, v) for i, ((t, ts), v) in enumerate(zip(pieces, vecs))])
                if EMBEDDER.url and pieces and vecs[0] is None:
                    pass   # stay dirty so meaning-vectors get added when the search model is up
                else:
                    CHATS.db.execute("DELETE FROM chat_dirty WHERE conv_id=?", (cid,))
                CHATS.db.commit()
        self.version += 1

    def _load(self):
        import math as _m
        from collections import Counter
        with self.lock:
            if self.cache and self.cache[0] == self.version:
                return self.cache
            with CHATS.lock:
                rows = CHATS.db.execute(
                    "SELECT c.conv_id, c.idx, c.text, c.ts, c.vec, v.title FROM chat_chunks c "
                    "JOIN conversations v ON v.id = c.conv_id AND v.deleted = 0").fetchall()
            docs, df = [], Counter()
            for cid, idx, text, ts, vec, title in rows:
                tf = Counter(_terms(text))
                docs.append({"conv": cid, "idx": idx, "text": text, "ts": ts, "vec": vec, "title": title or "Untitled",
                             "tf": tf, "len": sum(tf.values()) or 1})
                df.update(tf.keys())
            avgdl = (sum(d["len"] for d in docs) / len(docs)) if docs else 1
            self.cache = (self.version, docs, df, avgdl)
            return self.cache

    def index_now(self, conv_id):
        """Re-index one chat right away if it changed (recall inside the chat you're in)."""
        with CHATS.lock:
            dirty = CHATS.db.execute("SELECT 1 FROM chat_dirty WHERE conv_id=?", (conv_id,)).fetchone()
            if dirty:
                CHATS.db.execute("DELETE FROM chat_dirty WHERE conv_id=?", (conv_id,))
                CHATS.db.execute("INSERT INTO chat_dirty VALUES(?)", (conv_id,))
                CHATS.db.commit()
        if dirty:
            with CHATS.lock:
                others = [r[0] for r in CHATS.db.execute("SELECT conv_id FROM chat_dirty WHERE conv_id<>?", (conv_id,))]
                CHATS.db.execute("DELETE FROM chat_dirty WHERE conv_id<>?", (conv_id,))
                CHATS.db.commit()
            try:
                self.index_dirty(limit=1)
            finally:
                with CHATS.lock:
                    CHATS.db.executemany("INSERT OR IGNORE INTO chat_dirty VALUES(?)", [(o,) for o in others])
                    CHATS.db.commit()

    def search(self, query, k=6, exclude="", only="", before=None):
        import array
        import math as _m
        _, docs, df, avgdl = self._load()
        docs = [d for d in docs if d["conv"] != exclude and (not only or d["conv"] == only)
                and (before is None or (d["ts"] or 0) < before)]
        if not docs or not query.strip():
            return []
        q = set(_terms(query))
        N = len(docs)
        kw = []
        for i, d in enumerate(docs):
            sc = 0.0
            for t in q:
                f = d["tf"].get(t)
                if f:
                    sc += _m.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * d["len"] / avgdl))
            if sc > 0:
                kw.append((sc, i))
        kw.sort(reverse=True)
        ranked = [[i for _, i in kw[:60]]]
        if EMBEDDER.url:
            try:
                qv = array.array("f", EMBEDDER.embed([query], EMBEDDER.QUERY)[0])
                scored = []
                for i, d in enumerate(docs):
                    if d["vec"]:
                        v = array.array("f", d["vec"])
                        scored.append((sum(a * b for a, b in zip(v, qv)), i))
                scored.sort(reverse=True)
                ranked.append([i for sc, i in scored[:60] if sc > 0.35])
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                pass
        fused = {}
        for lst in ranked:
            for r, i in enumerate(lst):
                fused[i] = fused.get(i, 0) + 1.0 / (60 + r)
        best, seen = [], set()
        for i in sorted(fused, key=lambda i: -fused[i]):
            d = docs[i]
            if d["conv"] in seen and not only:
                continue
            seen.add(d["conv"])
            best.append({"conversation_id": d["conv"], "title": d["title"], "when": d["ts"],
                         "date": time.strftime("%Y-%m-%d", time.localtime(d["ts"] / 1000)), "excerpt": d["text"][:900]})
            if len(best) >= k:
                break
        return best


CHAT_SEARCH = ChatSearch()


def tool_search_chats(args):
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("what should I look for?")
    hits = CHAT_SEARCH.search(query, k=5, exclude=str(args.get("current_conversation") or ""))
    return {"results": [{k: h[k] for k in ("title", "date", "excerpt")} for h in hits],
            "note": "Excerpts from the user's earlier chats; mention which chat when you use them."}, \
        f"Searched past chats for “{query}”: {len(hits)} found"


SERVER_TOOLS["search_chats"] = (tool_search_chats, _fn(
    "search_chats", "Search the user's earlier conversations (what was said, decided or made before).",
    {"query": _p("what to look for")}, ["query"]))
TOOL_GROUPS["history"] = ["search_chats"]
TOOL_TRIGGERS["history"] = re.compile(
    r"\b(we|i|you) (talked|discussed|chatted|said|decided|did|made|wrote|figured)\b|last time|earlier chat|"
    r"other chat|previous (chat|conversation)|remember when|did (we|i) ever|what was (that|the)|before\b|"
    r"\bago\b|yesterday|last (week|month|night)", re.I)


