# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Reminders and lists ---------------------------------------------------------
REPEATS = ("none", "daily", "weekdays", "weekly", "monthly")


def _parse_when(text):
    """ISO 8601 date-time (ideally with a UTC offset, as the phone's clock gives) -> epoch ms."""
    from datetime import datetime
    t = str(text or "").strip().replace(" ", "T", 1)
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise ValueError("give the time as ISO 8601, e.g. 2026-11-04T17:00:00-06:00")
    if dt.tzinfo is None:
        dt = dt.astimezone()   # no offset given: the server's local time
    return int(dt.timestamp() * 1000)


def _next_due(due, repeat, now=None):
    """Next occurrence after `now` for a repeating reminder."""
    from datetime import datetime, timedelta
    now = now or int(time.time() * 1000)
    dt = datetime.fromtimestamp(due / 1000).astimezone()
    while int(dt.timestamp() * 1000) <= now:
        if repeat == "daily":
            dt += timedelta(days=1)
        elif repeat == "weekly":
            dt += timedelta(weeks=1)
        elif repeat == "weekdays":
            dt += timedelta(days=1)
            while dt.weekday() >= 5:
                dt += timedelta(days=1)
        elif repeat == "monthly":
            month = dt.month % 12 + 1
            year = dt.year + (1 if dt.month == 12 else 0)
            day = min(dt.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31,
                               30, 31, 31, 30, 31, 30, 31][month - 1])
            dt = dt.replace(year=year, month=month, day=day)
        else:
            break
    return int(dt.timestamp() * 1000)


def _fmt_time(ms):
    from datetime import datetime
    return datetime.fromtimestamp(ms / 1000).astimezone().strftime("%a %b %d, %H:%M")


class TaskStoreMixin:
    """Reminders (ring on every phone) and simple named lists (shopping, parts, to-do)."""

    def _tasks_schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reminders(
                id TEXT PRIMARY KEY, text TEXT NOT NULL, due_at INTEGER NOT NULL, repeat TEXT NOT NULL DEFAULT 'none',
                done INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS list_items(
                id TEXT PRIMARY KEY, list TEXT NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS list_name ON list_items(list);
        """)

    def reminders(self, include_done_days=3):
        now = int(time.time() * 1000)
        with self.lock:
            # Repeating reminders that went by without being marked done move on to their next time
            for rid, due, rep in self.db.execute(
                    "SELECT id, due_at, repeat FROM reminders WHERE done=0 AND repeat!='none' AND due_at < ?",
                    (now - 6 * 3600_000,)).fetchall():
                self.db.execute("UPDATE reminders SET due_at=?, updated_at=? WHERE id=?",
                                (_next_due(due, rep, now), now, rid))
            self.db.execute("DELETE FROM reminders WHERE done=1 AND updated_at < ?", (now - 30 * 86400_000,))
            self.db.commit()
            rows = self.db.execute(
                "SELECT id, text, due_at, repeat, done, updated_at FROM reminders WHERE done=0 OR updated_at > ? "
                "ORDER BY due_at", (now - include_done_days * 86400_000,)).fetchall()
        return [{"id": r[0], "text": r[1], "due_at": r[2], "repeat": r[3], "done": bool(r[4]), "updated_at": r[5]}
                for r in rows]

    def add_reminder(self, text, due_at, repeat="none"):
        text = " ".join(str(text).split())[:300]
        if not text:
            raise ValueError("what should the reminder say?")
        repeat = repeat if repeat in REPEATS else "none"
        now = int(time.time() * 1000)
        if due_at < now - 60_000:
            if repeat == "none":
                raise ValueError(f"that time ({_fmt_time(due_at)}) has already passed")
            due_at = _next_due(due_at, repeat, now)
        rid = uuid.uuid4().hex[:10]
        with self.lock:
            self.db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,?)", (rid, text, int(due_at), repeat, 0, now, now))
            self.db.commit()
        return {"id": rid, "text": text, "due_at": int(due_at), "repeat": repeat, "done": False, "updated_at": now}

    def update_reminder(self, rid, done=None, snooze_minutes=None, cancel=False):
        now = int(time.time() * 1000)
        with self.lock:
            r = self.db.execute("SELECT due_at, repeat FROM reminders WHERE id=?", (rid,)).fetchone()
            if not r:
                return None
            due, rep = r
            if cancel:
                self.db.execute("UPDATE reminders SET done=1, repeat='none', updated_at=? WHERE id=?", (now, rid))
            elif snooze_minutes:
                self.db.execute("UPDATE reminders SET due_at=?, done=0, updated_at=? WHERE id=?",
                                (now + int(snooze_minutes) * 60_000, now, rid))
            elif done:
                if rep != "none":
                    self.db.execute("UPDATE reminders SET due_at=?, updated_at=? WHERE id=?",
                                    (_next_due(max(due, now), rep, now), now, rid))
                else:
                    self.db.execute("UPDATE reminders SET done=1, updated_at=? WHERE id=?", (now, rid))
            self.db.commit()
        return next((x for x in self.reminders() if x["id"] == rid), None)

    def find_reminders(self, words):
        w = str(words).strip().lower()
        return [r for r in self.reminders(0) if not r["done"] and (r["id"] == w or (w and w in r["text"].lower()))]

    # lists
    def lists(self, include_done=True):
        with self.lock:
            rows = self.db.execute("SELECT id, list, text, done FROM list_items ORDER BY list, done, created_at").fetchall()
        out = {}
        for rid, name, text, done in rows:
            if done and not include_done:
                continue
            out.setdefault(name, []).append({"id": rid, "text": text, "done": bool(done)})
        return out

    @staticmethod
    def _list_name(name):
        n = " ".join(str(name or "").lower().split())[:40]
        n = re.sub(r"\s+list$", "", n) or "to-do"
        return n

    def add_items(self, name, items):
        name = self._list_name(name)
        now = int(time.time() * 1000)
        added = []
        with self.lock:
            existing = {r[0].lower(): r[1] for r in self.db.execute(
                "SELECT text, id FROM list_items WHERE list=?", (name,))}
            for it in items:
                t = " ".join(str(it).split())[:200]
                if not t:
                    continue
                if t.lower() in existing:   # already there: un-tick it
                    self.db.execute("UPDATE list_items SET done=0, updated_at=? WHERE id=?", (now, existing[t.lower()]))
                else:
                    self.db.execute("INSERT INTO list_items VALUES(?,?,?,?,?,?)",
                                    (uuid.uuid4().hex[:10], name, t, 0, now, now))
                added.append(t)
            self.db.commit()
        return name, added

    def set_item(self, iid, done=None, delete=False):
        now = int(time.time() * 1000)
        with self.lock:
            if delete:
                cur = self.db.execute("DELETE FROM list_items WHERE id=?", (iid,))
            else:
                cur = self.db.execute("UPDATE list_items SET done=?, updated_at=? WHERE id=?", (1 if done else 0, now, iid))
            self.db.commit()
        return cur.rowcount > 0

    def check_items(self, name, words, delete=False):
        name = self._list_name(name)
        changed = []
        for item in self.lists().get(name, []):
            for w in words:
                w = str(w).strip().lower()
                if w and (w in item["text"].lower() or item["text"].lower() in w):
                    self.set_item(item["id"], done=True, delete=delete)
                    changed.append(item["text"])
                    break
        return name, changed

    def clear_done(self, name):
        with self.lock:
            cur = self.db.execute("DELETE FROM list_items WHERE list=? AND done=1", (self._list_name(name),))
            self.db.commit()
        return cur.rowcount


CHATS_PATH = "chats.db"
LIBRARY_ID = "library"   # the shared document library is stored as a pseudo-project


class ProjectStoreMixin:
    """Projects live in the same database as chats. Mixed into ChatDB."""

    MAX_NOTES = 200

    def _bump(self, pid):
        self.project_versions[pid] = self.project_versions.get(pid, 0) + 1

    def project_version(self, pid):
        return self.project_versions.get(pid, 0)

    def projects(self):
        with self.lock:
            rows = self.db.execute("SELECT id FROM projects ORDER BY updated_at DESC").fetchall()
        return [self.project(r[0]) for r in rows]

    def project(self, pid):
        if pid == LIBRARY_ID:
            return self.library()
        with self.lock:
            r = self.db.execute("SELECT id, name, color, instructions, created_at, updated_at FROM projects WHERE id=?",
                                (str(pid),)).fetchone()
            if not r:
                return None
            notes = self.db.execute("SELECT id, text, source, created_at FROM project_notes WHERE project_id=? "
                                    "ORDER BY created_at", (r[0],)).fetchall()
            files = self.db.execute("SELECT id, name, kind, size, tokens, chunks, note, created_at FROM project_files "
                                    "WHERE project_id=? ORDER BY created_at", (r[0],)).fetchall()
            chats = self.db.execute("SELECT COUNT(*) FROM conversations WHERE project_id=? AND deleted=0",
                                    (r[0],)).fetchone()[0]
        return {"id": r[0], "name": r[1], "color": r[2], "instructions": r[3], "created_at": r[4], "updated_at": r[5],
                "chats": chats,
                "notes": [{"id": n[0], "text": n[1], "source": n[2], "created_at": n[3]} for n in notes],
                "files": [{"id": f[0], "name": f[1], "kind": f[2], "size": f[3], "tokens": f[4], "chunks": f[5],
                           "note": f[6], "created_at": f[7]} for f in files]}

    def library(self):
        """The shared document library: files any chat can search (stored like a project's files)."""
        with self.lock:
            files = self.db.execute("SELECT id, name, kind, size, tokens, chunks, note, created_at FROM project_files "
                                    "WHERE project_id=? ORDER BY created_at", (LIBRARY_ID,)).fetchall()
        return {"id": LIBRARY_ID, "name": "Library", "color": "cyan", "instructions": "", "created_at": 0,
                "updated_at": 0, "chats": 0, "notes": [],
                "files": [{"id": f[0], "name": f[1], "kind": f[2], "size": f[3], "tokens": f[4], "chunks": f[5],
                           "note": f[6], "created_at": f[7]} for f in files]}

    def chunk_texts(self, fid):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT text FROM project_chunks WHERE file_id=? ORDER BY idx", (fid,))]

    def file_project(self, fid):
        with self.lock:
            r = self.db.execute("SELECT project_id FROM project_files WHERE id=?", (fid,)).fetchone()
        return r[0] if r else None

    def save_vectors(self, fid, vecs):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO chunk_vecs VALUES(?,?,?)",
                                [(fid, i, v) for i, v in enumerate(vecs)])
            self.db.commit()
            pid = self.db.execute("SELECT project_id FROM project_files WHERE id=?", (fid,)).fetchone()
            if pid:
                self._bump(pid[0])

    def vectors(self, pid):
        """{(file_id, idx): blob} for a project's chunks."""
        with self.lock:
            rows = self.db.execute("""SELECT v.file_id, v.idx, v.vec FROM chunk_vecs v JOIN project_files f
                                      ON f.id = v.file_id WHERE f.project_id=?""", (pid,)).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def files_without_vectors(self):
        with self.lock:
            return [r[0] for r in self.db.execute(
                """SELECT f.id FROM project_files f WHERE f.chunks > 0 AND
                   (SELECT COUNT(*) FROM chunk_vecs v WHERE v.file_id = f.id) < f.chunks ORDER BY f.created_at""")]

    def save_project(self, data):
        name = " ".join(str(data.get("name", "")).split())[:60]
        if not name:
            raise ValueError("the project needs a name")
        color = str(data.get("color") or "cyan")
        color = color if color in COLORS else "cyan"
        instructions = str(data.get("instructions", ""))[:6000]
        pid = str(data.get("id") or "")
        if pid and not re.fullmatch(r"[0-9a-f]{12}", pid):
            raise ValueError("bad project id")
        now = int(time.time() * 1000)
        with self.lock:
            exists = pid and self.db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone()
            if exists:
                self.db.execute("UPDATE projects SET name=?, color=?, instructions=?, updated_at=? WHERE id=?",
                                (name, color, instructions, now, pid))
            else:
                pid = pid or uuid.uuid4().hex[:12]
                self.db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)", (pid, name, color, instructions, now, now))
            self.db.commit()
            self._bump(pid)
        return self.project(pid)

    def delete_project(self, pid):
        now = int(time.time() * 1000)
        with self.lock:
            files = [r[0] for r in self.db.execute("SELECT id FROM project_files WHERE project_id=?", (pid,))]
            cur = self.db.execute("DELETE FROM projects WHERE id=?", (pid,))
            self.db.execute("DELETE FROM project_notes WHERE project_id=?", (pid,))
            for fid in files:
                self.db.execute("DELETE FROM project_chunks WHERE file_id=?", (fid,))
                self.db.execute("DELETE FROM chunk_vecs WHERE file_id=?", (fid,))
            self.db.execute("DELETE FROM project_files WHERE project_id=?", (pid,))
            # Its chats stay, outside any project (synced to every device)
            for (cid,) in self.db.execute("SELECT id FROM conversations WHERE project_id=?", (pid,)).fetchall():
                self.db.execute("UPDATE conversations SET project_id='', updated_at=?, seq=? WHERE id=?",
                                (now, self._next(), cid))
            self.db.commit()
            self._bump(pid)
        for fid in files:
            _remove_project_file(fid)
        return cur.rowcount > 0

    def add_project_note(self, pid, text, source="user"):
        text = " ".join(str(text).split())[:1000]
        if len(text) < 2:
            raise ValueError("the note is empty")
        now = int(time.time() * 1000)
        with self.lock:
            if not self.db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                raise ValueError("no such project")
            dup = self.db.execute("SELECT id FROM project_notes WHERE project_id=? AND lower(text)=lower(?)",
                                  (pid, text)).fetchone()
            nid = dup[0] if dup else uuid.uuid4().hex[:8]
            if not dup:
                self.db.execute("INSERT INTO project_notes VALUES(?,?,?,?,?)", (nid, pid, text, source, now))
                self.db.execute("""DELETE FROM project_notes WHERE project_id=? AND id NOT IN
                                   (SELECT id FROM project_notes WHERE project_id=? ORDER BY created_at DESC LIMIT ?)""",
                                (pid, pid, self.MAX_NOTES))
            self.db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, pid))
            self.db.commit()
            self._bump(pid)
        return {"id": nid, "text": text, "source": source, "created_at": now}

    def delete_project_note(self, pid, nid):
        with self.lock:
            cur = self.db.execute("DELETE FROM project_notes WHERE id=? AND project_id=?", (nid, pid))
            self.db.commit()
            self._bump(pid)
        return cur.rowcount > 0

    def add_project_file(self, pid, name, kind, size, text, tokens, chunks, note):
        fid = uuid.uuid4().hex[:16]
        now = int(time.time() * 1000)
        with self.lock:
            if pid != LIBRARY_ID and not self.db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                raise ValueError("no such project")
            self.db.execute("INSERT INTO project_files VALUES(?,?,?,?,?,?,?,?,?)",
                            (fid, pid, name, kind, size, tokens, len(chunks), note, now))
            self.db.executemany("INSERT INTO project_chunks VALUES(?,?,?)", [(fid, i, c) for i, c in enumerate(chunks)])
            self.db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, pid))
            self.db.commit()
            self._bump(pid)
        return fid

    def delete_project_file(self, pid, fid):
        with self.lock:
            cur = self.db.execute("DELETE FROM project_files WHERE id=? AND project_id=?", (fid, pid))
            if cur.rowcount:
                self.db.execute("DELETE FROM project_chunks WHERE file_id=?", (fid,))
                self.db.execute("DELETE FROM chunk_vecs WHERE file_id=?", (fid,))
            self.db.commit()
            self._bump(pid)
        if cur.rowcount:
            _remove_project_file(fid)
        return cur.rowcount > 0

    # ---- Custom modes ("mindsets"): built-in ones live in the app ----
    def modes(self):
        with self.lock:
            rows = self.db.execute("SELECT id, name, icon, prompt, temperature, thinking, tools, style, updated_at "
                                   "FROM modes ORDER BY created_at").fetchall()
        return [{"id": r[0], "name": r[1], "icon": r[2], "prompt": r[3], "temperature": r[4], "thinking": r[5],
                 "tools": r[6], "style": r[7], "updated_at": r[8]} for r in rows]

    def save_mode(self, data):
        name = " ".join(str(data.get("name", "")).split())[:40]
        if not name:
            raise ValueError("the mode needs a name")
        icon = str(data.get("icon") or "✦").strip()[:4] or "✦"
        prompt = str(data.get("prompt", ""))[:6000]
        temp = data.get("temperature")
        temp = None if temp in (None, "") else max(0.0, min(1.5, float(temp)))
        thinking = data.get("thinking") if data.get("thinking") in ("on", "off", "default") else "default"
        tools = data.get("tools") if data.get("tools") in ("all", "minimal") else "all"
        style = data.get("style") if data.get("style") in ("work", "story") else "work"
        mid = str(data.get("id") or "")
        if mid and not re.fullmatch(r"c-[0-9a-f]{10}", mid):
            raise ValueError("bad mode id")
        now = int(time.time() * 1000)
        with self.lock:
            if mid and self.db.execute("SELECT 1 FROM modes WHERE id=?", (mid,)).fetchone():
                self.db.execute("UPDATE modes SET name=?, icon=?, prompt=?, temperature=?, thinking=?, tools=?, style=?, "
                                "updated_at=? WHERE id=?", (name, icon, prompt, temp, thinking, tools, style, now, mid))
            else:
                mid = mid or "c-" + uuid.uuid4().hex[:10]
                self.db.execute("INSERT INTO modes VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (mid, name, icon, prompt, temp, thinking, tools, style, now, now))
            self.db.commit()
        return next(m for m in self.modes() if m["id"] == mid)

    def delete_mode(self, mid):
        with self.lock:
            cur = self.db.execute("DELETE FROM modes WHERE id=?", (str(mid),))
            self.db.commit()
        return cur.rowcount > 0

    def project_chunks(self, pid):
        with self.lock:
            return self.db.execute(
                """SELECT c.file_id, f.name, c.idx, f.chunks, c.text FROM project_chunks c
                   JOIN project_files f ON f.id = c.file_id WHERE f.project_id=? ORDER BY f.created_at, c.idx""",
                (pid,)).fetchall()


# ---- Documents beside the chat -----------------------------------------------------------------------------------
# Long code, chapters and write-ups live in documents next to a chat instead of in it. Stella edits them a section
# (or an exact passage) at a time, every change is a numbered version, the user can read and edit them in a side
# panel on any device, and the chat only keeps a short reference ("Updated “Chapter 3” (v4) [doc:id]"), so a
# 2,000-line program doesn't fill her memory every time it changes.
DOC_MAX = 400_000
DOC_KEEP_VERSIONS = 60
DOC_REF = "[doc:{id}]"
DOC_TRIGGER = re.compile(r"\b(write|draft|start|create|make|continue|rewrite|edit|revise)\b.{0,40}\b(chapter|story|"
                         r"novel|essay|article|report|document|doc|readme|script|program|module|class|file|letter|"
                         r"spec|plan|outline)\b|\bin (a|the) (document|doc|side panel)\b", re.I)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
CODE_DEF = re.compile(r"^\s*(def |class |async def |fun |function |func |fn |pub fn |void |int |public |private |"
                      r"protected |static |struct |impl |interface |enum |module |export (default )?function )")


class DocStoreMixin:
    def _docs_schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS docs(
                id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'text',
                language TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL, text TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS docs_conv ON docs(conv_id);
            CREATE TABLE IF NOT EXISTS doc_versions(
                doc_id TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL, by TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, PRIMARY KEY(doc_id, version));
        """)

    def docs(self, conv_id):
        with self.lock:
            rows = self.db.execute("SELECT id, title, kind, language, version, length(text), updated_at FROM docs "
                                   "WHERE conv_id=? AND deleted=0 ORDER BY created_at", (str(conv_id),)).fetchall()
        return [{"id": r[0], "title": r[1], "kind": r[2], "language": r[3], "version": r[4], "chars": r[5],
                 "updated_at": r[6]} for r in rows]

    def doc(self, did):
        with self.lock:
            r = self.db.execute("SELECT id, conv_id, title, kind, language, version, text, updated_at FROM docs "
                                "WHERE id=? AND deleted=0", (str(did),)).fetchone()
            if not r:
                return None
            vs = self.db.execute("SELECT version, by, note, created_at, length(text) FROM doc_versions WHERE doc_id=? "
                                 "ORDER BY version DESC LIMIT 30", (r[0],)).fetchall()
        return {"id": r[0], "conv_id": r[1], "title": r[2], "kind": r[3], "language": r[4], "version": r[5],
                "text": r[6], "updated_at": r[7],
                "versions": [{"version": v[0], "by": v[1], "note": v[2], "at": v[3], "chars": v[4]} for v in vs]}

    def doc_version_text(self, did, version):
        with self.lock:
            r = self.db.execute("SELECT text FROM doc_versions WHERE doc_id=? AND version=?",
                                (str(did), int(version))).fetchone()
        return r[0] if r else None

    def create_doc(self, conv_id, title, text, kind="text", language="", by="stella"):
        title = " ".join(str(title or "").split())[:120] or "Untitled"
        text = str(text or "")
        if len(text) > DOC_MAX:
            raise ValueError(f"documents hold up to {DOC_MAX:,} characters")
        did = uuid.uuid4().hex[:10]
        now = int(time.time() * 1000)
        with self.lock:
            self.db.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,0)",
                            (did, str(conv_id), title, kind, str(language or "")[:30], 1, text, now, now))
            self.db.execute("INSERT INTO doc_versions VALUES(?,?,?,?,?,?)", (did, 1, text, by, "created", now))
            self.db.commit()
        return did

    def save_doc(self, did, text, by="stella", note="", title=None, base_version=None):
        """A new version of a document. base_version: refuse if someone else changed it meanwhile."""
        text = str(text)
        if len(text) > DOC_MAX:
            raise ValueError(f"documents hold up to {DOC_MAX:,} characters")
        now = int(time.time() * 1000)
        with self.lock:
            r = self.db.execute("SELECT version, text FROM docs WHERE id=? AND deleted=0", (str(did),)).fetchone()
            if not r:
                raise ValueError("no such document")
            if base_version is not None and int(base_version) != r[0]:
                raise ValueError(f"the document changed meanwhile (now v{r[0]}); reload it and try again")
            if r[1] == text and title is None:
                return r[0]
            v = r[0] + 1
            self.db.execute("UPDATE docs SET text=?, version=?, updated_at=?" + (", title=?" if title else "") +
                            " WHERE id=?", (text, v, now) + ((" ".join(str(title).split())[:120],) if title else ()) +
                            (str(did),))
            self.db.execute("INSERT INTO doc_versions VALUES(?,?,?,?,?,?)", (str(did), v, text, by, note[:200], now))
            self.db.execute("DELETE FROM doc_versions WHERE doc_id=? AND version <= ?", (str(did), v - DOC_KEEP_VERSIONS))
            self.db.commit()
        return v

    def delete_doc(self, did):
        with self.lock:
            cur = self.db.execute("UPDATE docs SET deleted=1, updated_at=? WHERE id=?", (int(time.time() * 1000), str(did)))
            self.db.commit()
        return cur.rowcount > 0


class ChatDB(ProjectStoreMixin, TaskStoreMixin, DocStoreMixin):
    """Chat history shared by all devices.

    Every change gets a increasing sequence number; devices ask for "everything
    after the last number I saw". Conflicts resolve by newest updated_at wins.
    """

    ROLES = {"user", "assistant", "tool", "summary"}
    PAGE = 500

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS conversations(
                id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '', reasoning TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, seq INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS conv_seq ON conversations(seq);
            CREATE INDEX IF NOT EXISTS msg_seq ON messages(seq);
            CREATE INDEX IF NOT EXISTS msg_conv ON messages(conv_id);
            CREATE TABLE IF NOT EXISTS memories(
                id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'user',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS projects(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT 'cyan',
                instructions TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS project_notes(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'user', created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS project_files(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                size INTEGER NOT NULL, tokens INTEGER NOT NULL, chunks INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS project_chunks(
                file_id TEXT NOT NULL, idx INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(file_id, idx));
            CREATE TABLE IF NOT EXISTS modes(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '✦',
                prompt TEXT NOT NULL DEFAULT '', temperature REAL, thinking TEXT NOT NULL DEFAULT 'default',
                tools TEXT NOT NULL DEFAULT 'all', style TEXT NOT NULL DEFAULT 'work',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_chunks(
                id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, idx INTEGER NOT NULL, text TEXT NOT NULL,
                ts INTEGER NOT NULL, vec BLOB);
            CREATE INDEX IF NOT EXISTS chat_chunk_conv ON chat_chunks(conv_id);
            CREATE TABLE IF NOT EXISTS chat_dirty(conv_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS memory_suggestions(
                id TEXT PRIMARY KEY, text TEXT NOT NULL, replaces TEXT NOT NULL DEFAULT '',
                conv_id TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_notes(
                id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'user',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS chat_note_conv ON chat_notes(conv_id);
            CREATE TABLE IF NOT EXISTS chunk_vecs(
                file_id TEXT NOT NULL, idx INTEGER NOT NULL, vec BLOB NOT NULL, PRIMARY KEY(file_id, idx));
            CREATE INDEX IF NOT EXISTS note_proj ON project_notes(project_id);
            CREATE INDEX IF NOT EXISTS file_proj ON project_files(project_id);
            """
        )
        ccols = {r[1] for r in self.db.execute("PRAGMA table_info(conversations)")}
        if "project_id" not in ccols:
            self.db.execute("ALTER TABLE conversations ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
        if "mode" not in ccols:
            self.db.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT ''")
        self.project_versions = {}
        self._tasks_schema()
        self._docs_schema()
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "deleted" not in cols:  # older databases
            self.db.execute("ALTER TABLE messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        row = self.db.execute("SELECT v FROM meta WHERE k='server_id'").fetchone()
        if row:
            self.server_id = row[0]
        else:
            self.server_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO meta VALUES('server_id', ?)", (self.server_id,))
        self.seq = max(
            self.db.execute("SELECT COALESCE(MAX(seq),0) FROM conversations").fetchone()[0],
            self.db.execute("SELECT COALESCE(MAX(seq),0) FROM messages").fetchone()[0],
        )
        self.db.commit()

    @staticmethod
    def _id(v):
        v = str(v or "")
        if not v or len(v) > 64:
            raise ValueError("bad id")
        return v

    @staticmethod
    def _ts(v):
        return int(v)

    def _next(self):
        self.seq += 1
        return self.seq

    def sync(self, since, convs, msgs):
        with self.lock:
            for c in convs:
                cid, upd = self._id(c.get("id")), self._ts(c.get("updated_at"))
                row = self.db.execute("SELECT updated_at, project_id, mode FROM conversations WHERE id=?",
                                      (cid,)).fetchone()
                if row and row[0] >= upd:
                    continue
                deleted = 1 if c.get("deleted") else 0
                # Older apps don't send project_id: keep what the server has
                project = str(c.get("project_id") or "")[:64] if "project_id" in c else (row[1] if row else "")
                mode = str(c.get("mode") or "")[:64] if "mode" in c else (row[2] if row else "")
                self.db.execute(
                    """INSERT OR REPLACE INTO conversations
                       (id, title, created_at, updated_at, deleted, seq, project_id, mode) VALUES(?,?,?,?,?,?,?,?)""",
                    (cid, str(c.get("title", ""))[:200], self._ts(c.get("created_at", upd)), upd, deleted,
                     self._next(), project, mode),
                )
                if deleted:
                    self.db.execute("DELETE FROM messages WHERE conv_id=?", (cid,))
            for m in msgs:
                mid, upd = self._id(m.get("id")), self._ts(m.get("updated_at"))
                cid = self._id(m.get("conv_id"))
                role = str(m.get("role", ""))
                if role not in self.ROLES:
                    continue
                gone = self.db.execute("SELECT 1 FROM conversations WHERE id=? AND deleted=1", (cid,)).fetchone()
                if gone:
                    continue
                row = self.db.execute("SELECT updated_at FROM messages WHERE id=?", (mid,)).fetchone()
                if row and row[0] >= upd:
                    continue
                gone_msg = 1 if m.get("deleted") else 0
                self.db.execute(
                    """INSERT OR REPLACE INTO messages
                       (id, conv_id, role, text, reasoning, created_at, updated_at, seq, deleted)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (mid, cid, role, "" if gone_msg else str(m.get("text", ""))[:100000],
                     "" if gone_msg else str(m.get("reasoning", ""))[:100000],
                     self._ts(m.get("created_at", upd)), upd, self._next(), gone_msg),
                )
            # Conversations whose messages changed get re-indexed for "search past chats"
            touched = {str(c.get("id")) for c in convs} | {str(m.get("conv_id")) for m in msgs}
            self.db.executemany("INSERT OR IGNORE INTO chat_dirty VALUES(?)", [(t,) for t in touched if t])
            self.db.commit()

            rows = self.db.execute(
                """SELECT 'c', id, title, created_at, updated_at, deleted, project_id, mode, NULL, NULL, seq
                     FROM conversations WHERE seq > ?
                   UNION ALL
                   SELECT 'm', id, NULL, created_at, updated_at, deleted, conv_id, role, text, reasoning, seq
                     FROM messages WHERE seq > ?
                   ORDER BY seq LIMIT ?""",
                (since, since, self.PAGE),
            ).fetchall()

        out_c, out_m = [], []
        for kind, rid, title, created, updated, deleted, conv_id, role, text, reasoning, seq in rows:
            if kind == "c":
                out_c.append({"id": rid, "title": title, "created_at": created,
                              "updated_at": updated, "deleted": bool(deleted), "project_id": conv_id or "",
                              "mode": role or ""})
            else:
                out_m.append({"id": rid, "conv_id": conv_id, "role": role, "text": text,
                              "reasoning": reasoning, "created_at": created, "updated_at": updated,
                              "deleted": bool(deleted)})
        more = len(rows) == self.PAGE
        cursor = rows[-1][-1] if more else max(self.seq, since)
        return {"server_id": self.server_id, "cursor": cursor, "more": more,
                "conversations": out_c, "messages": out_m}

    # ---- Pinned notes per chat ----------------------------------------------------------
    MAX_CHAT_NOTES = 40

    def chat_notes(self, conv_id):
        with self.lock:
            rows = self.db.execute("SELECT id, text, source, created_at, updated_at FROM chat_notes WHERE conv_id=? "
                                   "ORDER BY created_at", (str(conv_id),)).fetchall()
        return [{"id": r[0], "text": r[1], "source": r[2], "created_at": r[3], "updated_at": r[4]} for r in rows]

    def add_chat_note(self, conv_id, text, source="user"):
        text = " ".join(str(text).split())[:400]
        if len(text) < 2:
            raise ValueError("the note is empty")
        now = int(time.time() * 1000)
        with self.lock:
            dup = self.db.execute("SELECT id FROM chat_notes WHERE conv_id=? AND lower(text)=lower(?)",
                                  (str(conv_id), text)).fetchone()
            if dup:
                nid = dup[0]
            else:
                if self.db.execute("SELECT COUNT(*) FROM chat_notes WHERE conv_id=?", (str(conv_id),)).fetchone()[0] \
                        >= self.MAX_CHAT_NOTES:
                    raise ValueError(f"this chat already has {self.MAX_CHAT_NOTES} pinned notes; remove or merge some")
                nid = uuid.uuid4().hex[:6]
                self.db.execute("INSERT INTO chat_notes VALUES(?,?,?,?,?,?)", (nid, str(conv_id), text, source, now, now))
            self.db.commit()
        return {"id": nid, "text": text, "source": source}

    def update_chat_note(self, conv_id, nid, text):
        text = " ".join(str(text).split())[:400]
        if len(text) < 2:
            raise ValueError("the note is empty")
        with self.lock:
            cur = self.db.execute("UPDATE chat_notes SET text=?, updated_at=? WHERE conv_id=? AND id=?",
                                  (text, int(time.time() * 1000), str(conv_id), str(nid)))
            self.db.commit()
        if not cur.rowcount:
            raise ValueError("no pinned note with that id")
        return {"id": nid, "text": text}

    def delete_chat_note(self, conv_id, nid):
        with self.lock:
            cur = self.db.execute("DELETE FROM chat_notes WHERE conv_id=? AND id=?", (str(conv_id), str(nid)))
            self.db.commit()
        return cur.rowcount > 0

    # ---- Suggested memories (waiting for the user to keep or drop) ---------------------
    def memory_suggestions(self):
        with self.lock:
            rows = self.db.execute("SELECT id, text, replaces, conv_id, created_at FROM memory_suggestions "
                                   "ORDER BY created_at DESC LIMIT 100").fetchall()
            known = {r[0]: r[1] for r in self.db.execute("SELECT id, text FROM memories")}
        return [{"id": r[0], "text": r[1], "replaces": r[2], "replaces_text": known.get(r[2], ""), "conv_id": r[3],
                 "created_at": r[4]} for r in rows]

    def suggest_memory(self, text, replaces="", conv_id=""):
        text = " ".join(str(text).split())[:300]
        if len(text) < 3:
            return None
        with self.lock:
            if self.db.execute("SELECT 1 FROM memories WHERE lower(text)=lower(?)", (text,)).fetchone() or \
                    self.db.execute("SELECT 1 FROM memory_suggestions WHERE lower(text)=lower(?)", (text,)).fetchone():
                return None
            sid = uuid.uuid4().hex[:8]
            self.db.execute("INSERT INTO memory_suggestions VALUES(?,?,?,?,?)",
                            (sid, text, str(replaces or ""), str(conv_id or ""), int(time.time() * 1000)))
            self.db.execute("DELETE FROM memory_suggestions WHERE id NOT IN (SELECT id FROM memory_suggestions "
                            "ORDER BY created_at DESC LIMIT 100)")
            self.db.commit()
        return sid

    def resolve_suggestion(self, sid, accept, text=None):
        with self.lock:
            r = self.db.execute("SELECT text, replaces FROM memory_suggestions WHERE id=?", (sid,)).fetchone()
            if not r:
                raise ValueError("no such suggestion")
            self.db.execute("DELETE FROM memory_suggestions WHERE id=?", (sid,))
            self.db.commit()
        if not accept:
            return None
        if r[1]:
            self.delete_memory(r[1])
        if r[0].startswith("(forget) "):
            return None
        mid, _ = self.add_memory(text if text and str(text).strip() else r[0], "learned")
        return mid

    # ---- Long-term memory -----------------------------------------------------------
    MAX_MEMORIES = 200

    def memories(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, text, source, created_at, updated_at FROM memories ORDER BY updated_at DESC").fetchall()
        return [{"id": r[0], "text": r[1], "source": r[2], "created_at": r[3], "updated_at": r[4]} for r in rows]

    def add_memory(self, text, source="user"):
        text = " ".join(str(text).split())[:300]
        if len(text) < 3:
            raise ValueError("memory is empty")
        now = int(time.time() * 1000)
        with self.lock:
            dup = self.db.execute("SELECT id FROM memories WHERE lower(text)=lower(?)", (text,)).fetchone()
            if dup:
                self.db.execute("UPDATE memories SET updated_at=? WHERE id=?", (now, dup[0]))
                self.db.commit()
                return dup[0], False
            mid = uuid.uuid4().hex[:8]
            self.db.execute("INSERT INTO memories VALUES(?,?,?,?,?)", (mid, text, source, now, now))
            # Keep the newest MAX_MEMORIES
            self.db.execute("""DELETE FROM memories WHERE id NOT IN
                               (SELECT id FROM memories ORDER BY updated_at DESC LIMIT ?)""", (self.MAX_MEMORIES,))
            self.db.commit()
            return mid, True

    def delete_memory(self, mid):
        with self.lock:
            cur = self.db.execute("DELETE FROM memories WHERE id=?", (str(mid),))
            self.db.commit()
            return cur.rowcount > 0

    def find_memories(self, query):
        q = str(query).strip().lower()
        if not q:
            return []
        return [m for m in self.memories() if m["id"] == q or q in m["text"].lower()]


def _remove_project_file(fid):
    for ext in (".txt", ".orig"):
        try:
            os.remove(os.path.join(PROJECT_DIR, fid + ext))
        except OSError:
            pass


def add_project_file(pid, name, data):
    name = os.path.basename(str(name).replace("\\", "/")).strip()[:120] or "file"
    kind, text, note = extract_text(name, data)
    text = text.replace("\x00", "")[:MAX_FILE_CHARS]
    if not text.strip():
        raise ValueError(f"no text found in '{name}' (scanned PDFs need OCR first)")
    chunks = chunk_text(text)
    tokens = count_tokens(text) if len(text) < 400_000 else int(len(text) / 3.3)
    fid = CHATS.add_project_file(pid, name, kind, len(data), text, tokens, chunks, note)
    with open(os.path.join(PROJECT_DIR, fid + ".txt"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(PROJECT_DIR, fid + ".orig"), "wb") as f:
        f.write(data)
    EMBEDDER.queue(fid)
    return fid


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(whisper_url: str, wav: bytes, language: str) -> str:
    boundary = "----lai" + uuid.uuid4().hex
    fields = {"response_format": "json", "temperature": "0.0"}
    if language:
        fields["language"] = language
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode()
        + wav
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        whisper_url.rstrip("/") + "/inference",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()
    if "error" in data:
        raise RuntimeError(data["error"])
    return str(data.get("text", "")).strip()


def has_speech(pcm: bytes, rate: int) -> bool:
    """False only when a recording is plainly silence, so Whisper (the slow part of voice) isn't run on it.
    Cautious on purpose: only loudness counts, with a low bar, so a quiet voice, or a clip that is speech from
    start to end, always goes to Whisper. Room noise above the bar goes too, as it always did."""
    import array
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    frame = max(1, rate // 50)                      # 20 ms
    if len(samples) < frame * 6:
        return len(samples) > 0
    loud = 0
    for i in range(0, len(samples) - frame + 1, frame):
        part = samples[i:i + frame:2]               # every other sample is plenty for a loudness estimate
        if sum(x * x for x in part) > 120 * 120 * len(part):   # 120 of 32768 is about -49 dB: below a whisper
            loud += 1
            if loud >= 6:                           # 0.12 s in total, anywhere in the clip
                return True
    return False


def clean_transcript(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]", " ", text)  # [BLANK_AUDIO], [MUSIC] ...
    text = re.sub(r"\s+", " ", text).strip()
    if NOISE_PATTERNS.match(text):
        return ""
    return text


_WRONG_KEYS = {}   # address -> [count since last log line, time of last log line]


def note_wrong_key(addr):
    """Someone used a wrong API key: say so in the activity log (at most once a minute per address), so a
    stranger knocking, or a phone still holding an old key, doesn't go unnoticed."""
    now = time.time()
    entry = _WRONG_KEYS.setdefault(addr, [0, 0.0])
    entry[0] += 1
    if now - entry[1] >= 60:
        log(f"wrong API key from {addr}" + (f" ({entry[0]} tries)" if entry[0] > 1 else "")
            + ": if that's one of your devices, pair it again (Settings → Scan to connect)")
        entry[0], entry[1] = 0, now
    if len(_WRONG_KEYS) > 500:
        _WRONG_KEYS.clear()


def key_ok(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


