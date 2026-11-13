#!/usr/bin/env python3
"""
StellAI hub: speech-to-text and chat sync for the StellAI phone app.

  Phone app --(HTTP, port 8092)--> hub
     POST /transcribe  phone audio (after the wake word) -> Whisper (127.0.0.1:8093) -> text
     POST /sync        chat history sync between devices (SQLite)
     GET  /tools       server-side tools Stella can use (calculator, electronics, diagrams, memory)
     POST /tools/NAME  run one of them
     GET  /diagrams/ID.png  a rendered diagram
     PUT/GET /images/ID.jpg  photos sent in chats
     GET  /memories, POST /memories, DELETE /memories/ID, POST /memories/learn   long-term memory
     GET  /wakewords, GET /wakewords/NAME.onnx   custom wake word models
     GET  /health

Standard library only. Usage:
  python3 stellai_hub.py --key-file ~/llm/api-key.txt --db ~/llm/chats.db
"""

import argparse
import hmac
import io
import json
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def trust_more_certificates():
    """HTTPS downloads trust the system's certificates plus Mozilla's list (certifi, when installed).

    Windows only fetches some root certificates when its own programs need them, so Python on a fresh
    Windows PC can fail with CERTIFICATE_VERIFY_FAILED for sites like python.org. Certificates are still
    checked: this only adds trusted roots."""
    import ssl
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:   # noqa: BLE001 - certifi is optional; the system's certificates still apply
            pass
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)))
    except Exception:       # noqa: BLE001 - keep Python's default if anything goes wrong
        pass


trust_more_certificates()


def _is_cert_error(e):
    return "CERTIFICATE_VERIFY_FAILED" in str(e) or "certificate verify failed" in str(e)


def _windows_download(url, dest):
    """Downloads with Windows' own HTTPS stack, which fetches missing root certificates by itself."""
    env = {**os.environ, "STELLAI_URL": url, "STELLAI_OUT": dest}
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "$ProgressPreference='SilentlyContinue'; "
                        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
                        "Invoke-WebRequest -UseBasicParsing -Uri $env:STELLAI_URL -OutFile $env:STELLAI_OUT"],
                       capture_output=True, text=True, timeout=3600, env=env,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not os.path.isfile(dest):
        raise OSError("download failed: " + (r.stderr or r.stdout).strip()[-300:])

MAX_SECONDS = 60

# Things Whisper tends to "hear" in silence or noise.
NOISE_PATTERNS = re.compile(
    r"^\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*)\s*$|^\s*(thank you\.?|thanks for watching!?|you|\.)\s*$",
    re.IGNORECASE,
)


LOG_HOOK = None     # optional callable(str), used by the Windows app's log window


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except (AttributeError, OSError, ValueError):
        pass  # no console (Windows app without a terminal)
    if LOG_HOOK:
        try:
            LOG_HOOK(line)
        except Exception:
            pass


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


# ----------------------------------------------------------------------------- Server tools
# Tools Stella can call that run here on the server (the phone app forwards them).
# Calculator and electronics helpers are exact; diagrams are rendered to PNG.

import ast
import math
import operator
import os
import subprocess
import tempfile

SI_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0,
             "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1.0}
UNIT_WORDS = r"(?:Hz|ohms?|Ohms?|Ω|V|A|W|F|H|s)"
# 4.7k, 10uF, 4k7, 4R7, 100mA, 3.3V, 1kHz, 2.2µF, 47Ω
QUANTITY_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?:([pnuµμmkKMGR])(\d+)?)?" + UNIT_WORDS + r"?(?![\w(.])"
)


def _quantity_sub(m):
    whole, prefix, frac = m.group(1), m.group(2) or "", m.group(3)
    if frac and "." not in whole and "e" not in whole.lower():
        whole = f"{whole}.{frac}"  # 4k7 -> 4.7k
    return repr(float(whole) * SI_PREFIX[prefix])


def parse_quantity(v):
    """Number, or a string like '4.7k', '10uF', '4k7', '100 mA', '3.3V'."""
    if isinstance(v, bool):
        raise ValueError("expected a number")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if not s:
        raise ValueError("empty value")
    m = QUANTITY_RE.fullmatch(s)
    if m:
        return float(_quantity_sub(m))
    return float(s)


def eng(x, unit=""):
    """Engineering notation with SI prefix: 0.00047 -> '470 µ' + unit."""
    if x == 0 or not math.isfinite(x):
        return f"{x:g} {unit}".strip()
    exp3 = int(math.floor(math.log10(abs(x)) / 3) * 3)
    exp3 = max(-12, min(9, exp3))
    prefix = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[exp3]
    return f"{x / 10 ** exp3:.4g} {prefix}{unit}".strip()


_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _parallel(*xs):
    return 1.0 / sum(1.0 / x for x in xs)


_FUNCS = {
    "sqrt": math.sqrt, "ln": math.log, "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin,
    "acos": math.acos, "atan": math.atan, "atan2": math.atan2, "sinh": math.sinh, "cosh": math.cosh,
    "tanh": math.tanh, "abs": abs, "round": round, "min": min, "max": max, "pow": math.pow,
    "ceil": math.ceil, "floor": math.floor, "hypot": math.hypot, "degrees": math.degrees,
    "radians": math.radians, "parallel": _parallel, "db": lambda r: 20 * math.log10(r),
    "db10": lambda r: 10 * math.log10(r),
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def safe_eval(expr):
    """Evaluate a maths expression without running arbitrary code."""
    text = QUANTITY_RE.sub(_quantity_sub, expr.replace("^", "**").replace("×", "*").replace("÷", "/"))
    tree = ast.parse(text, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError(f"not allowed in a calculation: {ast.unparse(node) if hasattr(ast, 'unparse') else type(node).__name__}")

    result = ev(tree)
    if isinstance(result, complex):
        raise ValueError("result is a complex number")
    return float(result)


# E-series standard resistor values (one decade)
E_SERIES = {
    "E12": [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2],
    "E24": [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1,
            5.6, 6.2, 6.8, 7.5, 8.2, 9.1],
    "E96": [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33, 1.37, 1.40,
            1.43, 1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82, 1.87, 1.91, 1.96, 2.00,
            2.05, 2.10, 2.15, 2.21, 2.26, 2.32, 2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87,
            2.94, 3.01, 3.09, 3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12,
            4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49, 5.62, 5.76, 5.90,
            6.04, 6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45,
            8.66, 8.87, 9.09, 9.31, 9.53, 9.76],
}


def _series_values(series):
    base = E_SERIES.get(series.upper())
    if not base:
        raise ValueError("series must be E12, E24 or E96")
    return [round(b * 10 ** d, 10) for d in range(-1, 8) for b in base]


def nearest_standard(value, series="E24"):
    vals = _series_values(series)
    below = max((v for v in vals if v <= value * (1 + 1e-9)), default=vals[0])
    above = min((v for v in vals if v >= value * (1 - 1e-9)), default=vals[-1])
    nearest = min((below, above), key=lambda v: abs(v - value))
    return below, nearest, above


def _need(args, keys, count):
    got = {k: parse_quantity(args[k]) for k in keys if args.get(k) not in (None, "")}
    if len(got) < count:
        raise ValueError(f"give at least {count} of: {', '.join(keys)}")
    return got


def tool_calculate(args):
    expr = str(args.get("expression", "")).strip()
    if not expr:
        raise ValueError("no expression")
    value = safe_eval(expr)
    unit = str(args.get("unit", "")).strip()
    return {"expression": expr, "value": value, "formatted": eng(value, unit)}, f"{expr} = {eng(value, unit)}"


def tool_ohms_law(args):
    g = _need(args, ["voltage", "current", "resistance", "power"], 2)
    v, i, r, p = g.get("voltage"), g.get("current"), g.get("resistance"), g.get("power")
    if v is not None and i is not None:
        r, p = v / i, v * i
    elif v is not None and r is not None:
        i = v / r; p = v * i
    elif v is not None and p is not None:
        i = p / v; r = v / i
    elif i is not None and r is not None:
        v = i * r; p = v * i
    elif i is not None and p is not None:
        v = p / i; r = v / i
    else:  # r and p
        v = math.sqrt(p * r); i = v / r
    res = {"voltage": v, "current": i, "resistance": r, "power": p}
    summary = f"{eng(v, 'V')}, {eng(i, 'A')}, {eng(r, 'Ω')}, {eng(p, 'W')}"
    return res, summary


def tool_voltage_divider(args):
    vin = parse_quantity(args["vin"]) if args.get("vin") not in (None, "") else None
    vout = parse_quantity(args["vout"]) if args.get("vout") not in (None, "") else None
    r1 = parse_quantity(args["r1"]) if args.get("r1") not in (None, "") else None
    r2 = parse_quantity(args["r2"]) if args.get("r2") not in (None, "") else None
    series = str(args.get("series") or "E24")
    known = sum(x is not None for x in (vin, vout, r1, r2))
    if vin is not None and r1 is not None and r2 is not None:
        vout = vin * r2 / (r1 + r2)
    elif vout is not None and r1 is not None and r2 is not None:
        vin = vout * (r1 + r2) / r2
    elif vin is not None and vout is not None and r1 is not None:
        r2 = r1 * vout / (vin - vout)
    elif vin is not None and vout is not None and r2 is not None:
        r1 = r2 * (vin - vout) / vout
    elif vin is not None and vout is not None and known == 2:
        # Suggest a standard-value pair near 10k total
        if not 0 < vout < vin:
            raise ValueError("vout must be between 0 and vin")
        best = None
        for a in _series_values(series):
            if not 1e3 <= a <= 1e6:
                continue
            _, b, _ = nearest_standard(a * vout / (vin - vout), series)
            err = abs(vin * b / (a + b) - vout)
            score = (round(err, 6), abs(math.log10((a + b) / 2e4)))
            if best is None or score < best[0]:
                best = (score, a, b)
        r1, r2 = best[1], best[2]
        actual = vin * r2 / (r1 + r2)
        res = {"vin": vin, "vout_target": vout, "r1": r1, "r2": r2, "vout_actual": actual,
               "error_percent": (actual - vout) / vout * 100, "series": series}
        return res, f"R1 = {eng(r1, 'Ω')}, R2 = {eng(r2, 'Ω')} → {eng(actual, 'V')} ({res['error_percent']:+.2f}%)"
    else:
        raise ValueError("give three of vin, vout, r1, r2 (or just vin and vout to get a suggested pair)")
    current = vin / (r1 + r2)
    res = {"vin": vin, "vout": vout, "r1": r1, "r2": r2, "current": current}
    return res, f"Vin {eng(vin, 'V')}, Vout {eng(vout, 'V')}, R1 {eng(r1, 'Ω')}, R2 {eng(r2, 'Ω')}, I {eng(current, 'A')}"


def tool_led_resistor(args):
    vs = parse_quantity(args["supply_voltage"])
    vf = parse_quantity(args["led_voltage"])
    n = int(args.get("leds_in_series") or 1)
    i = parse_quantity(args["current_ma"]) / 1000.0
    drop = vs - vf * n
    if drop <= 0:
        raise ValueError("supply voltage is too low for that many LEDs")
    r = drop / i
    _, _, e12_up = nearest_standard(r, "E12")
    _, _, e24_up = nearest_standard(r, "E24")
    actual_i = drop / e24_up
    p = actual_i ** 2 * e24_up
    rating = next(w for w in (0.125, 0.25, 0.5, 1, 2, 5, 10, 1e9) if w >= p * 2)
    res = {"exact_resistance": r, "e24_resistor": e24_up, "e12_resistor": e12_up,
           "actual_current": actual_i, "resistor_power": p, "suggested_rating_w": rating}
    return res, (f"{eng(r, 'Ω')} exact → use {eng(e24_up, 'Ω')} (E24) or {eng(e12_up, 'Ω')} (E12); "
                 f"{eng(actual_i, 'A')}, {eng(p, 'W')} → {rating:g} W resistor")


def tool_rc_filter(args):
    g = _need(args, ["resistance", "capacitance", "frequency"], 2)
    r, c, f = g.get("resistance"), g.get("capacitance"), g.get("frequency")
    if r is not None and c is not None:
        f = 1 / (2 * math.pi * r * c)
    elif r is not None and f is not None:
        c = 1 / (2 * math.pi * r * f)
    else:
        r = 1 / (2 * math.pi * c * f)
    tau = r * c
    res = {"resistance": r, "capacitance": c, "cutoff_frequency": f, "time_constant": tau}
    return res, f"R {eng(r, 'Ω')}, C {eng(c, 'F')}, fc {eng(f, 'Hz')}, τ {eng(tau, 's')}"


def tool_combine_resistors(args):
    vals = [parse_quantity(v) for v in (args.get("values") or [])]
    if len(vals) < 2:
        raise ValueError("give at least two values")
    mode = str(args.get("mode", "parallel")).lower()
    total = sum(vals) if mode == "series" else _parallel(*vals)
    return {"mode": mode, "total": total}, f"{mode}: {eng(total, 'Ω')}"


def tool_standard_resistor(args):
    value = parse_quantity(args["value"])
    series = str(args.get("series") or "E24")
    below, nearest, above = nearest_standard(value, series)
    res = {"value": value, "series": series, "nearest": nearest, "below": below, "above": above,
           "error_percent": (nearest - value) / value * 100}
    return res, f"{series}: nearest {eng(nearest, 'Ω')} ({res['error_percent']:+.1f}%), range {eng(below, 'Ω')}–{eng(above, 'Ω')}"


# ---- Diagrams ------------------------------------------------------------------
DIAGRAM_DIR = None   # set in main()
PY_FOR_DIAGRAMS = sys.executable
MMDC = "mmdc"

SCHEMDRAW_ALLOWED_IMPORTS = {"schemdraw", "schemdraw.elements", "schemdraw.logic", "schemdraw.dsp",
                             "schemdraw.flow", "schemdraw.segments", "math"}
SCHEMDRAW_BLOCKED_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
                           "getattr", "setattr", "delattr", "input", "breakpoint", "help", "exit", "quit",
                           "memoryview", "os", "sys", "subprocess", "shutil", "socket", "pathlib", "importlib"}


def check_schemdraw_code(code):
    """Only schemdraw drawing code: no file, network or system access."""
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in SCHEMDRAW_ALLOWED_IMPORTS:
                    raise ValueError(f"import of {a.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") not in SCHEMDRAW_ALLOWED_IMPORTS or node.level:
                raise ValueError(f"import from {node.module} is not allowed")
        elif isinstance(node, ast.Name) and node.id in SCHEMDRAW_BLOCKED_NAMES:
            raise ValueError(f"'{node.id}' is not allowed in diagram code")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private attributes are not allowed in diagram code")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise ValueError("global statements are not allowed")


SCHEMDRAW_RUNNER = r'''
import sys
try:
    import resource  # Linux/macOS only; Windows relies on the wall-clock timeout instead
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
except ImportError:
    pass
import matplotlib
matplotlib.use("Agg")
import schemdraw
import schemdraw.elements as elm
code = open(sys.argv[1]).read()
out = sys.argv[2]
ns = {"schemdraw": schemdraw, "elm": elm}
_orig_draw = schemdraw.Drawing.draw
drawings = []
def _capture(self, *a, **k):
    drawings.append(self)
    k["show"] = False
    return _orig_draw(self, *a, **k)
schemdraw.Drawing.draw = _capture
_orig_exit = schemdraw.Drawing.__exit__
def _exit(self, *a):
    drawings.append(self)
    try:
        return _orig_exit(self, *a)
    except Exception:
        return None
schemdraw.Drawing.__exit__ = _exit
exec(compile(code, "diagram", "exec"), ns)
d = ns.get("d") or (drawings[-1] if drawings else None)
if d is None:
    for v in ns.values():
        if isinstance(v, schemdraw.Drawing):
            d = v
if d is None:
    sys.exit("no schemdraw.Drawing found (assign it to d)")
d.save(out, transparent=False, dpi=160)
'''


DIAGRAM_RUNNER_CMD = None   # e.g. ["StellAI.exe", "--schemdraw-runner"] in the packaged Windows app


def run_schemdraw_runner(src, out):
    """Entry point for packaged apps: run the schemdraw sandbox in this (fresh) process."""
    sys.argv = ["runner", src, out]
    exec(compile(SCHEMDRAW_RUNNER, "runner", "exec"), {"__name__": "__main__"})


def _child_env():
    keep = ["PATH", "HOME"]
    if os.name == "nt":
        keep += ["SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
                 "PATHEXT", "COMSPEC", "WINDIR", "PROGRAMFILES"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("HOME", tempfile.gettempdir())
    env["MPLBACKEND"] = "Agg"
    return env


def _run(cmd, timeout):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flashing up
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_child_env(),
                           creationflags=flags, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the diagram took too long to draw (over {timeout} s)")
    if p.returncode != 0:
        if p.returncode in (-9, -24, 137, 152):  # killed by the CPU-time limit
            raise RuntimeError("the diagram took too long to draw (over 20 s of CPU)")
        lines = [ln.strip() for ln in (p.stderr or p.stdout or "").splitlines()
                 if ln.strip() and not ln.strip().startswith("at ") and "(" not in ln[:1]]
        # Prefer the first "Error: ..." line plus the line after it (e.g. Mermaid's "Expecting ...")
        for i, ln in enumerate(lines):
            if ln.startswith("Error") or "Error:" in ln:
                detail = next((x for x in lines[i + 1:] if x.startswith("Expecting")), "")
                raise RuntimeError(f"{ln} {detail}".strip()[:300])
        raise RuntimeError((lines or [f"exit code {p.returncode}"])[-1][:300])


def render_schemdraw(code, out_png):
    check_schemdraw_code(code)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "diagram.py")
        runner = os.path.join(tmp, "runner.py")
        with open(src, "w") as f:
            f.write(code)
        with open(runner, "w") as f:
            f.write(SCHEMDRAW_RUNNER)
        if DIAGRAM_RUNNER_CMD:
            _run(list(DIAGRAM_RUNNER_CMD) + [src, out_png], timeout=40)
        else:
            _run([PY_FOR_DIAGRAMS, "-I", runner, src, out_png], timeout=40)


def mermaid_command():
    """Full path to the Mermaid CLI, or None when it isn't installed (Mermaid is optional)."""
    import shutil
    if MMDC and os.path.isfile(MMDC):
        return MMDC
    return shutil.which(MMDC) if MMDC else None


def render_mermaid(source, out_png):
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "diagram.mmd")
        with open(src, "w") as f:
            f.write(source)
        mmdc = mermaid_command()
        if not mmdc:
            raise ValueError("Mermaid isn't installed on this server; draw block diagrams with "
                             "kind='schemdraw' (schemdraw.flow) instead")
        _run([mmdc, "-i", src, "-o", out_png, "-b", "white", "-s", "2"], timeout=60)


def _wave_segments(wave):
    """Expand a WaveDrom wave string into per-cycle states."""
    states, last = [], "x"
    for ch in wave:
        if ch == "." or ch == "|":
            states.append(("cont", last))
        else:
            last = ch
            states.append(("new", ch))
    return states


def render_wavedrom(source, out_png):
    """Draws a WaveDrom (WaveJSON) timing diagram with matplotlib. Supports the common wave
    characters: 0 1 h l p n P N x z = 2-9 . | and data labels."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon

    spec = json.loads(source)
    signals = []

    def collect(items):
        for it in items:
            if isinstance(it, list):
                collect(it[1:] if it and isinstance(it[0], str) else it)
            elif isinstance(it, dict) and "wave" in it:
                signals.append(it)
            elif isinstance(it, dict):
                signals.append(None)  # spacer
    collect(spec.get("signal", []))
    if not signals:
        raise ValueError("no signals found")
    cycles = max(len(s["wave"]) for s in signals if s) or 1
    hscale = float(spec.get("config", {}).get("hscale", 1))
    cw = 0.6 * hscale
    rows = len(signals)
    fig_w = 1.6 + cycles * cw + 0.3
    fig_h = 0.55 * rows + 0.6
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=160)
    ax.set_xlim(-1.6, cycles * cw + 0.3)
    ax.set_ylim(-rows * 0.55 - 0.1, 0.4)
    ax.axis("off")
    for c in range(cycles + 1):
        ax.axvline(c * cw, color="#dddddd", lw=0.6, zorder=0)
    title = (spec.get("head") or {}).get("text") if isinstance(spec.get("head"), dict) else None
    if title:
        ax.text(cycles * cw / 2, 0.25, str(title), ha="center", va="center", fontsize=10, weight="bold")

    bus_colors = {"=": "#ffffff", "2": "#ffffff", "3": "#fff3b0", "4": "#ffd6a5", "5": "#caffbf",
                  "6": "#a0c4ff", "7": "#bdb2ff", "8": "#ffc6ff", "9": "#e0e0e0"}
    for row, sig in enumerate(signals):
        if sig is None:
            continue
        y0 = -row * 0.55 - 0.45
        hi, lo, mid = y0 + 0.36, y0, y0 + 0.18
        ax.text(-0.1, mid, str(sig.get("name", "")), ha="right", va="center", fontsize=9, family="monospace")
        data = sig.get("data", [])
        if isinstance(data, str):
            data = data.split()
        # Group the wave into runs: (start cycle, length, character)
        runs = []
        for c, (kind, ch) in enumerate(_wave_segments(str(sig["wave"]))):
            if kind == "cont" and runs:
                runs[-1][1] += 1
            else:
                runs.append([c, 1, ch])
        di = 0
        level = None  # last drawn line level, for vertical transitions
        for start, span, ch in runs:
            x0, x1 = start * cw, (start + span) * cw
            if ch in "pPnN":
                for k in range(span):
                    a, b = x0 + k * cw, x0 + (k + 1) * cw
                    first, second = (hi, lo) if ch in "pP" else (lo, hi)
                    xm = (a + b) / 2
                    ax.plot([a, a, xm, xm, b], [level if level is not None else first, first, first, second, second],
                            color="black", lw=1.2)
                    level = second
            elif ch in "1h0lz":
                y = hi if ch in "1h" else lo if ch in "0l" else mid
                ax.plot([x0, x0, x1], [level if level is not None else y, y, y],
                        color="#1f6feb" if ch == "z" else "black", lw=1.2)
                level = y
            else:
                # Bus value or undefined: one hexagon per run, label in the middle
                e = min(0.08, (x1 - x0) / 4)
                poly = [(x0 + e, hi), (x1 - e, hi), (x1, mid), (x1 - e, lo), (x0 + e, lo), (x0, mid)]
                if ch == "x":
                    ax.add_patch(Polygon(poly, closed=True, facecolor="#eeeeee", edgecolor="black", lw=1.0, hatch="////"))
                else:
                    ax.add_patch(Polygon(poly, closed=True, facecolor=bus_colors.get(ch, "#ffffff"),
                                         edgecolor="black", lw=1.0))
                    if di < len(data):
                        ax.text((x0 + x1) / 2, mid, str(data[di]), ha="center", va="center", fontsize=8)
                    di += 1
                level = None
    fig.savefig(out_png, bbox_inches="tight", facecolor="white", pad_inches=0.15)
    plt.close(fig)


def tool_render_diagram(args):
    kind = str(args.get("kind", "")).lower().strip()
    source = str(args.get("source", ""))
    title = str(args.get("title", "")).strip() or kind
    if not source.strip():
        raise ValueError("no diagram source")
    if len(source) > 20000:
        raise ValueError("diagram source is too long")
    image_id = uuid.uuid4().hex[:16]
    out = os.path.join(DIAGRAM_DIR, image_id + ".png")
    if kind == "mermaid":
        render_mermaid(source, out)
    elif kind == "schemdraw":
        render_schemdraw(source, out)
    elif kind == "wavedrom":
        render_wavedrom(source, out)
    else:
        raise ValueError("kind must be mermaid, schemdraw or wavedrom")
    if not os.path.exists(out):
        raise RuntimeError("the renderer produced no image")
    return {"image": image_id, "title": title, "note": "The diagram is now shown to the user."}, f"Drew {title}"


def _p(desc, typ="string"):
    return {"type": typ, "description": desc}


QTY = "Number, or a value with SI prefix/unit like '4.7k', '10uF', '3.3V', '20mA'"


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


MERMAID_HINT = "mermaid: block diagrams, flowcharts, state/sequence diagrams. "
NO_MERMAID_HINT = "Flowcharts: schemdraw.flow (flow.Start, flow.Box, flow.Decision, flow.Arrow). "


DIAGRAM_DESC = (
    "Draw and show a diagram. schemdraw: circuit schematics as Python ('import schemdraw', "
    "'import schemdraw.elements as elm', 'with schemdraw.Drawing() as d:', 'd += elm.Resistor().label(\"10kΩ\")'; "
    "no other imports). {MERMAID}wavedrom: timing diagrams as WaveJSON."
)


def refresh_diagram_tool():
    """Only offer Mermaid to the model when the Mermaid CLI is actually installed."""
    hint = MERMAID_HINT if mermaid_command() else NO_MERMAID_HINT
    fn = SERVER_TOOLS["render_diagram"][1]["function"]
    fn["description"] = DIAGRAM_DESC.replace("{MERMAID}", hint)
    fn["parameters"]["properties"]["kind"]["description"] = (
        "schemdraw, mermaid or wavedrom" if mermaid_command() else "schemdraw or wavedrom")


SERVER_TOOLS = {
    "calculate": (tool_calculate, _fn(
        "calculate",
        "Exact maths. ALWAYS use this instead of doing arithmetic yourself. Supports + - * / ^ ( ), "
        "SI values like 4.7k, 10u, 3.3V, 100mA, functions sqrt ln log10 exp sin cos tan abs round min max, "
        "parallel(a,b,...) for parallel resistors, db(ratio), and constants pi e.",
        {"expression": _p("The expression, e.g. '12 * 4.7k / (4.7k + 10k)' or 'parallel(10k, 4.7k)'"),
         "unit": _p("Optional unit for the answer, e.g. 'V', 'Ω', 'Hz', 'A'")}, ["expression"])),
    "ohms_law": (tool_ohms_law, _fn(
        "ohms_law", "Ohm's law and power: give any two of voltage, current, resistance, power.",
        {"voltage": _p(QTY), "current": _p(QTY), "resistance": _p(QTY), "power": _p(QTY)}, [])),
    "voltage_divider": (tool_voltage_divider, _fn(
        "voltage_divider",
        "Resistor voltage divider (Vout = Vin*R2/(R1+R2), R2 to ground). Give three of vin, vout, r1, r2 "
        "to solve the fourth, or just vin and vout to get the best standard-value resistor pair.",
        {"vin": _p(QTY), "vout": _p(QTY), "r1": _p(QTY), "r2": _p(QTY),
         "series": _p("Resistor series for suggestions: E12, E24 (default) or E96")}, [])),
    "led_resistor": (tool_led_resistor, _fn(
        "led_resistor", "Current-limiting resistor for LEDs, with standard values and power rating.",
        {"supply_voltage": _p(QTY), "led_voltage": _p("Forward voltage of one LED, e.g. 2.0 red, 3.1 white/blue"),
         "current_ma": _p("LED current in mA, e.g. 10"),
         "leds_in_series": _p("How many LEDs in series (default 1)", "integer")},
        ["supply_voltage", "led_voltage", "current_ma"])),
    "rc_filter": (tool_rc_filter, _fn(
        "rc_filter", "RC low/high-pass filter: cutoff fc = 1/(2πRC). Give any two of resistance, capacitance, frequency.",
        {"resistance": _p(QTY), "capacitance": _p(QTY), "frequency": _p(QTY)}, [])),
    "combine_resistors": (tool_combine_resistors, _fn(
        "combine_resistors", "Total resistance of resistors in series or parallel.",
        {"values": {"type": "array", "items": {"type": "string"}, "description": "Resistor values, e.g. ['10k','4.7k']"},
         "mode": _p("'series' or 'parallel'")}, ["values", "mode"])),
    "standard_resistor": (tool_standard_resistor, _fn(
        "standard_resistor", "Nearest standard resistor values (E12/E24/E96) to a calculated value.",
        {"value": _p(QTY), "series": _p("E12, E24 (default) or E96")}, ["value"])),
    "render_diagram": (tool_render_diagram, _fn(
        "render_diagram",
        DIAGRAM_DESC.replace("{MERMAID}", MERMAID_HINT),
        {"kind": _p("schemdraw, mermaid or wavedrom"), "source": _p("The diagram code"),
         "title": _p("Short title, e.g. 'LED driver'")}, ["kind", "source"])),
}


# ---- Long-term memory tools ------------------------------------------------------
CHATS = None        # ChatDB, set in main()
LLAMA_URL = "http://127.0.0.1:8080"
API_KEY = ""


def tool_remember(args):
    fact = str(args.get("fact", "")).strip()
    mid, new = CHATS.add_memory(fact, "stella")
    return {"id": mid, "saved": fact, "new": new}, f"Remembered: {fact}" if new else f"Already knew: {fact}"


def tool_forget(args):
    query = str(args.get("memory", "")).strip()
    found = CHATS.find_memories(query)
    if not found:
        raise ValueError(f"no memory matches '{query}'")
    if len(found) > 3:
        raise ValueError(f"{len(found)} memories match '{query}'; be more specific or give the id")
    for m in found:
        CHATS.delete_memory(m["id"])
    return {"forgotten": [m["text"] for m in found]}, "Forgot: " + "; ".join(m["text"] for m in found)


LEARN_SCHEMA = {
    "type": "object",
    "properties": {
        "add": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "remove": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["add", "remove"],
}

LEARN_PROMPT = """You maintain a short list of long-term facts about the user, so their assistant can help them better over time.
Read the conversation and decide what is worth remembering for weeks or months: stable preferences, projects they are working on,
tools/boards/parts/software they use, people and pets in their life, routines and schedules, goals, and explicit "remember" requests.
Do NOT save: one-off questions, small talk, things already in the list, temporary states ("I'm tired"), or anything the assistant said.
Write each fact as one short sentence in third person, e.g. "Uses an ESP32-S3 for most projects." or "Works 8-4 on weekdays."
If the conversation shows an existing fact is now wrong, put its id in "remove" (and the corrected fact in "add").
Reply with JSON only: {"add": [...], "remove": [...]}. Empty lists are fine and are the usual answer."""


def learn_memories(messages, conv_id=""):
    """Ask the local model which facts from a conversation are worth keeping. In review mode they become
    suggestions for the user to keep or drop; in auto mode they're saved; off: nothing."""
    mode = memory_settings()["mode"]
    if mode == "off":
        return {"added": [], "removed": [], "suggested": [], "mode": mode}
    existing = CHATS.memories()
    known = "\n".join(f"[{m['id']}] {m['text']}" for m in existing[:120]) or "(none yet)"
    convo = "\n".join(f"{m.get('role', 'user').upper()}: {str(m.get('text', ''))[:1500]}"
                      for m in messages[-12:] if m.get("role") in ("user", "assistant"))
    if not convo.strip():
        return {"added": [], "removed": []}
    body = {
        "model": "local",
        "messages": [
            {"role": "system", "content": LEARN_PROMPT},
            {"role": "user", "content": f"Known facts:\n{known}\n\nConversation:\n{convo}"},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_schema", "json_schema": {"name": "memory_update", "schema": LEARN_SCHEMA}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    content = _quick_model(body, timeout=180)["choices"][0]["message"].get("content") or "{}"
    m = re.search(r"\{[\s\S]*\}", content)
    update = json.loads(m.group(0)) if m else {}
    removed, added, suggested = [], [], []
    ids = {x["id"] for x in existing}
    gone = [str(mid).strip("[] ") for mid in update.get("remove", [])[:5] if str(mid).strip("[] ") in ids]
    facts = [" ".join(f.split()) for f in update.get("add", [])[:5] if isinstance(f, str) and len(f.strip()) >= 3]
    if mode == "review":
        # a removal paired with a new fact is shown as "replace X with Y"; a lone removal as "forget X?"
        for i, fact in enumerate(facts):
            sid = CHATS.suggest_memory(fact, gone[i] if i < len(gone) else "", conv_id)
            if sid:
                suggested.append(fact)
        for mid in gone[len(facts):]:
            old = next((x["text"] for x in existing if x["id"] == mid), "")
            if old and CHATS.suggest_memory("(forget) " + old, mid, conv_id):
                suggested.append("(forget) " + old)
        return {"added": [], "removed": [], "suggested": suggested, "mode": mode}
    for mid in gone:
        if CHATS.delete_memory(mid):
            removed.append(mid)
    for fact in facts:
        _, new = CHATS.add_memory(fact, "learned")
        if new:
            added.append(fact)
    return {"added": added, "removed": removed, "suggested": [], "mode": mode}


SERVER_TOOLS["remember"] = (tool_remember, _fn(
    "remember",
    "Save a long-term fact about the user (preferences, projects, equipment, people, routines) so you can use it in "
    "future conversations. Use when the user tells you something worth remembering or asks you to remember it.",
    {"fact": _p("One short sentence in third person, e.g. 'Prefers Kotlin for Android apps.'")}, ["fact"]))
SERVER_TOOLS["forget"] = (tool_forget, _fn(
    "forget", "Delete a saved fact about the user, when they ask you to forget it or it is wrong.",
    {"memory": _p("The memory id (e.g. 'a1b2c3d4') or words from it")}, ["memory"]))

# ---- Context window: token counting, summaries -------------------------------------
_TOKEN_CACHE = {}
_NCTX = {"value": 0, "at": 0.0}


def _llama(path, body=None, timeout=60):
    req = urllib.request.Request(
        LLAMA_URL.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---- The helper: a small second model for quick background jobs ------------------------------------------------
# Memory learning and "which mode is this?" checks go to a small model (Qwen3.5 0.8B/2B, on the CPU or a spare
# card) when one runs, so they don't queue behind Stella's main model. Without a helper the main model does them.
HELPER = {"url": "", "ok_until": 0.0, "down_until": 0.0}


def helper_up():
    if not HELPER["url"]:
        return False
    now = time.time()
    if now < HELPER["ok_until"]:
        return True
    if now < HELPER["down_until"]:
        return False
    try:
        with urllib.request.urlopen(HELPER["url"].rstrip("/") + "/health", timeout=2) as r:
            ok = r.status == 200
    except (urllib.error.URLError, OSError):
        ok = False
    HELPER["ok_until" if ok else "down_until"] = now + (60 if ok else 30)
    return ok


def _quick_model(body, timeout=120, helper_timeout=60):
    """A chat completion for a background job: on the helper when it's up, else on the main model."""
    if helper_up():
        try:
            req = urllib.request.Request(HELPER["url"].rstrip("/") + "/v1/chat/completions",
                                         data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=helper_timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"helper model didn't answer ({e}); using the main model")
            HELPER["ok_until"], HELPER["down_until"] = 0.0, time.time() + 30
    return _llama("/v1/chat/completions", body, timeout=timeout)


def count_tokens(text):
    """Exact count from llama-server's tokenizer (cached); a character estimate if it's unreachable."""
    text = str(text or "")
    if not text:
        return 0
    key = hash(text)
    hit = _TOKEN_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        n = len(_llama("/tokenize", {"content": text}, timeout=20).get("tokens", []))
    except (urllib.error.URLError, OSError, ValueError):
        return int(len(text) / 3.3) + 1   # not cached: retry exact counting next time
    if len(_TOKEN_CACHE) > 20000:
        _TOKEN_CACHE.clear()
    _TOKEN_CACHE[key] = n
    return n


def context_size():
    """The model's context window (tokens), from llama-server, cached for a minute."""
    if time.time() - _NCTX["at"] < 60 and _NCTX["value"]:
        return _NCTX["value"]
    n = 0
    try:
        props = _llama("/props", timeout=10)
        n = int((props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx") or 0)
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        pass
    if n:
        _NCTX.update(value=n, at=time.time())
    return n or _NCTX["value"] or 8192


SUMMARY_PROMPT = (
    "You compress a chat between a user and their AI assistant so the assistant can continue it with a fresh, "
    "nearly empty memory. Write a summary that lets the conversation continue seamlessly:\n"
    "- the user's goal and what has been decided or done so far\n"
    "- key facts, numbers, part numbers, file names, commands and code identifiers (keep short code snippets "
    "that are still being worked on)\n"
    "- open questions and what the user asked for last\n"
    "Use short bullet points under these headings: Goal, Done & decided, Key details, Open / next. "
    "Write in English, at most about 350 words. Output only the summary."
)


STORY_SUMMARY_PROMPT = (
    "You compress a creative-writing collaboration between a user and their AI writing partner so the partner can "
    "keep writing with a fresh, nearly empty memory. Preserve everything needed for continuity:\n"
    "- Characters: names, roles, relationships, appearance, voice and what each wants\n"
    "- Setting and world rules\n"
    "- Plot so far, in order, including the most recent scene in some detail\n"
    "- Style: genre, tense, point of view, tone, and any style rules the user gave\n"
    "- Open threads, planned events and what the user asked for next\n"
    "Use short bullet points under those headings. At most about 450 words. Output only the summary."
)


def _summarize_once(previous, convo, max_tokens=800, style="work"):
    user = (f"Summary so far:\n{previous}\n\n" if previous else "") + f"Conversation to add:\n{convo}"
    prompt = STORY_SUMMARY_PROMPT if style == "story" else SUMMARY_PROMPT
    body = {"model": "local", "temperature": 0.2, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            "chat_template_kwargs": {"enable_thinking": False}}
    out = _llama("/v1/chat/completions", body, timeout=300)["choices"][0]["message"].get("content") or ""
    out = re.sub(r"<think>[\s\S]*?</think>", "", out).strip()
    if not out:
        raise RuntimeError("the model returned an empty summary")
    return out


def summarize_chat(previous, messages, style="work"):
    """Rolling summary: long chats are summarized in pieces that fit the model's context."""
    n_ctx = context_size()
    piece_budget = max(1000, int(n_ctx * 0.5) - count_tokens(SUMMARY_PROMPT) - count_tokens(previous) - 800)
    lines = []
    for m in messages:
        role = str(m.get("role", "user"))
        text = str(m.get("content", "")).strip()
        if not text or role not in ("user", "assistant", "tool", "summary"):
            continue
        label = {"user": "USER", "assistant": "ASSISTANT", "tool": "ACTION RESULT", "summary": "EARLIER SUMMARY"}[role]
        max_chars = piece_budget * 3
        if len(text) > max_chars:
            text = text[: max_chars // 2] + "\n[…]\n" + text[-max_chars // 2:]
        lines.append(f"{label}: {text}")
    if not lines:
        return previous or ""
    summary, batch, used = previous or "", [], 0
    for line in lines:
        t = count_tokens(line)
        if batch and used + t > piece_budget:
            summary = _summarize_once(summary, "\n\n".join(batch), style=style)
            batch, used = [], 0
        batch.append(line)
        used += t
    if batch:
        summary = _summarize_once(summary, "\n\n".join(batch), style=style)
    return summary


# ---- Projects: instructions, notes and files that give a group of chats shared context ----
PROJECT_DIR = None   # set in start(); originals and extracted text of project files
MAX_PROJECT_FILE = 25_000_000
MAX_FILE_CHARS = 3_000_000
TEXT_EXTS = {
    "txt", "md", "markdown", "rst", "csv", "tsv", "json", "yaml", "yml", "toml", "ini", "cfg", "conf", "xml", "html",
    "htm", "css", "js", "mjs", "ts", "tsx", "jsx", "py", "kt", "kts", "java", "c", "h", "cpp", "hpp", "cc", "ino",
    "rs", "go", "rb", "php", "sh", "bash", "ps1", "bat", "sql", "gradle", "properties", "v", "sv", "vhd", "vhdl",
    "lua", "swift", "cs", "m", "scad", "gcode", "log", "env", "dts", "ld", "s", "asm", "cmake", "mk", "makefile",
    "kicad_sch", "kicad_pcb", "kicad_pro", "net", "cir", "sp", "spice", "svg", "tex",
}
IMAGE_EXTS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
              "gif": "image/gif", "bmp": "image/bmp"}
COLORS = {"cyan", "blue", "red", "green", "purple", "pink", "orange", "yellow"}


def _pdf_text(data):
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages):
            pages.append(f"[page {i + 1}]\n" + (page.extract_text() or ""))
        return "\n\n".join(pages)
    except ImportError:
        pass
    import shutil
    exe = shutil.which("pdftotext")
    if not exe:
        raise ValueError("can't read PDFs on this server: install pypdf (pip install pypdf) or poppler-utils")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        with open(src, "wb") as f:
            f.write(data)
        p = subprocess.run([exe, "-layout", src, "-"], capture_output=True, timeout=120)
        if p.returncode != 0:
            raise ValueError("couldn't read this PDF")
        return p.stdout.decode("utf-8", "replace").replace("\f", "\n\n[next page]\n")


def _docx_text(data):
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(text)


def describe_image(data, mime):
    """Uses the vision add-on to turn an image into searchable text."""
    import base64
    b64 = base64.b64encode(data).decode()
    body = {"model": "local", "temperature": 0.2, "max_tokens": 700,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe this image in detail so it can be used later without seeing it: "
                                         "all readable text, labels, part numbers, component values, connections, "
                                         "measurements and layout. Plain text, no preamble."},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]}
    out = _llama("/v1/chat/completions", body, timeout=300)["choices"][0]["message"].get("content") or ""
    return re.sub(r"<think>[\s\S]*?</think>", "", out).strip()


def extract_text(name, data):
    """Returns (kind, text, note) for an uploaded project file."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else name.lower()
    if ext == "pdf" or data[:5] == b"%PDF-":
        return "pdf", _pdf_text(data), ""
    if ext == "docx":
        return "doc", _docx_text(data), ""
    if ext in IMAGE_EXTS:
        try:
            desc = describe_image(data, IMAGE_EXTS[ext])
            return "image", f"(Image file. Description made when it was added:)\n{desc}", ""
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            return "image", "(Image file. No description: the vision add-on isn't available on the server.)", \
                f"Stella can't see this image yet ({e})"
    if b"\x00" in data[:8000]:
        raise ValueError(f"'{name}' looks like a binary file; add text, code, PDF, Word or image files")
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return ("code" if ext not in ("txt", "md", "markdown", "rst", "csv", "tsv", "log") else "text"), text, ""


def chunk_text(text, size=1800, overlap=200):
    """Splits text into overlapping pieces on line boundaries."""
    text = text.replace("\r\n", "\n")
    if len(text) <= size:
        return [text] if text.strip() else []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut
        piece = text[start:end]
        if piece.strip():
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


_WORD = re.compile(r"[A-Za-z0-9]+")
_STOP = set("""a an and are as at be but by can do does for from has have how i if in into is it its me my no not of
on or our so that the their them then there these they this to was we what when where which who why will with you
your please tell show about""".split())


def _terms(text):
    return [w for w in (t.lower() for t in _WORD.findall(text)) if w not in _STOP and (len(w) > 1 or w.isdigit())]


class ProjectIndex:
    """BM25 keyword search over a project's file chunks (no extra model or packages needed)."""

    def __init__(self):
        self.cache = {}  # project id -> (version, docs, df, avgdl)
        self.lock = threading.Lock()

    def search(self, db, pid, query, k=8):
        import math as _m
        from collections import Counter
        version = db.project_version(pid)
        with self.lock:
            entry = self.cache.get(pid)
            if not entry or entry[0] != version:
                docs = []
                df = Counter()
                for fid, fname, idx, n, text in db.project_chunks(pid):
                    tf = Counter(_terms(fname + " " + text))
                    docs.append((fid, fname, idx, n, text, tf, sum(tf.values()) or 1))
                    df.update(tf.keys())
                avgdl = (sum(d[6] for d in docs) / len(docs)) if docs else 1
                entry = (version, docs, df, avgdl)
                self.cache[pid] = entry
        _, docs, df, avgdl = entry
        q = set(_terms(query))
        if not docs:
            return []
        if not q:
            return [d[:5] for d in docs[:k]]
        N = len(docs)
        scored = []
        for d in docs:
            tf, dl = d[5], d[6]
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    idf = _m.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl / avgdl))
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        keyword = [d[:5] for _, d in scored[:max(k * 3, 20)]]
        meaning = EMBEDDER.rank(db, pid, version, docs, query, max(k * 3, 20))
        if not meaning:
            return keyword[:k]
        # Reciprocal rank fusion: good matches by keywords *or* by meaning rise to the top
        fused = {}
        for ranked in (keyword, meaning):
            for rank, d in enumerate(ranked):
                key = (d[0], d[2])
                fused.setdefault(key, [0.0, d])[0] += 1.0 / (60 + rank)
        best = sorted(fused.values(), key=lambda x: -x[0])
        return [d for _, d in best[:k]]


PROJECT_INDEX = ProjectIndex()


class Embedder:
    """Meaning-based search: turns file chunks and questions into vectors with a small embedding model
    (nomic-embed-text, run by a second llama-server with --embedding). Optional: without it, search
    falls back to keywords only."""

    DOC, QUERY = "search_document: ", "search_query: "

    def __init__(self):
        self.url = ""
        self.pending = []
        self.cond = threading.Condition()
        self.cache = {}     # project id -> (version, keys, matrix or list)
        self.qcache = {}
        self.started = False
        self.healthy = False

    def start(self, url):
        self.url = (url or "").rstrip("/")
        if not self.url or self.started:
            return
        self.started = True
        threading.Thread(target=self._worker, daemon=True, name="embedder").start()

    def queue(self, fid):
        if not self.url:
            return
        with self.cond:
            if fid not in self.pending:
                self.pending.append(fid)
            self.cond.notify()

    def embed(self, texts, prefix):
        import array
        out = []
        for i in range(0, len(texts), 16):
            batch = [prefix + t[:6000] for t in texts[i:i + 16]]
            req = urllib.request.Request(self.url + "/v1/embeddings",
                                         data=json.dumps({"input": batch, "model": "embed"}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=300) as r:
                data = sorted(json.loads(r.read())["data"], key=lambda d: d["index"])
            for d in data:
                v = d["embedding"]
                norm = sum(x * x for x in v) ** 0.5 or 1.0
                out.append(array.array("f", (x / norm for x in v)).tobytes())
        return out

    def _worker(self):
        time.sleep(5)
        for fid in CHATS.files_without_vectors():   # catch up on files added before the model was installed
            self.queue(fid)
        while True:
            with self.cond:
                while not self.pending:
                    self.cond.wait()
                fid = self.pending.pop(0)
            try:
                chunks = CHATS.chunk_texts(fid)
                if chunks and CHATS.file_project(fid) is not None:
                    CHATS.save_vectors(fid, self.embed(chunks, self.DOC))
                    self.healthy = True
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                self.healthy = False
                log(f"meaning search: couldn't index a file yet ({e}); will retry")
                time.sleep(60)
                self.queue(fid)

    def rank(self, db, pid, version, docs, query, k):
        """Chunks ranked by similarity of meaning to the query (empty if the model isn't available)."""
        import array
        if not self.url or not query.strip() or not docs:
            return []
        entry = self.cache.get(pid)
        if not entry or entry[0] != version:
            blobs = db.vectors(pid)
            keys = [(d[0], d[2]) for d in docs if (d[0], d[2]) in blobs]
            vecs = [array.array("f", blobs[key]) for key in keys]
            try:
                import numpy as np
                mat = np.array(vecs, dtype=np.float32) if vecs else None
            except ImportError:
                mat = vecs
            entry = (version, keys, mat)
            self.cache[pid] = entry
        _, keys, mat = entry
        if not keys:
            return []
        qv = self.qcache.get(query)
        if qv is None:
            try:
                qv = array.array("f", self.embed([query], self.QUERY)[0])
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                return []
            if len(self.qcache) > 200:
                self.qcache.clear()
            self.qcache[query] = qv
        try:
            import numpy as np
            scores = (mat @ np.array(qv, dtype=np.float32)).tolist() if not isinstance(mat, list) else None
        except ImportError:
            scores = None
        if scores is None:
            scores = [sum(a * b for a, b in zip(v, qv)) for v in mat]
        by_key = {(d[0], d[2]): d for d in docs}
        order = sorted(range(len(keys)), key=lambda i: -scores[i])[:k]
        return [by_key[keys[i]][:5] for i in order if scores[i] > 0.3]


EMBEDDER = Embedder()


def _file_text(fid):
    try:
        with open(os.path.join(PROJECT_DIR, fid + ".txt"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def build_project_context(pid, query, budget, split=False):
    """The project section of Stella's instructions, fitted to `budget` tokens.
    With split=True returns (stable_text, excerpts_text, tokens): excerpts change with each message, so clients
    attach them to the newest message instead of the instructions (keeps the model server's cache valid)."""
    p = CHATS.project(pid)
    if not p:
        return ("", "", 0) if split else ("", 0)
    head = [f"## Current project: {p['name']}",
            "This chat belongs to the project above. Use its instructions, notes and files; they apply to the whole project."]
    if p["instructions"].strip():
        head.append("Project instructions (follow these):\n" + p["instructions"].strip())
    if p["notes"]:
        lines, used = [], 0
        for n in p["notes"]:
            line = f"- [{n['id']}] {n['text']}"
            used += len(line)
            if used > budget * 1.2:   # characters ~ tokens/3; keep notes to about 40% of the budget
                lines.append("- (more notes not shown)")
                break
            lines.append(line)
        head.append("Project notes (add new ones with project_note):\n" + "\n".join(lines))
    files = p["files"]
    if files:
        head.append("Project files: " + "; ".join(f"{f['name']} (~{f['tokens']} tokens)" for f in files))
    text = "\n\n".join(head)
    used = count_tokens(text)
    remaining = budget - used
    if not files or remaining < 200:
        if files:
            text += "\n\nThe file contents don't fit right now; call project_search to look things up in them."
        return (text, "", count_tokens(text)) if split else (text, count_tokens(text))
    total = sum(f["tokens"] for f in files)
    blocks = []
    if total <= remaining - 50:
        for f in files:
            blocks.append(f"### File: {f['name']}\n{_file_text(f['id'])}")
        text += "\n\nFull contents of the project files:\n\n" + "\n\n".join(blocks)
        return (text, "", count_tokens(text)) if split else (text, count_tokens(text))
    else:
        whole = set()
        for f in sorted(files, key=lambda f: f["tokens"]):   # small files (pinouts, notes) go in whole
            if f["tokens"] > min(500, remaining * 0.25):
                break
            block = f"### File: {f['name']}\n{_file_text(f['id'])}"
            blocks.append(block)
            whole.add(f["id"])
            remaining -= count_tokens(block)
        if whole:
            text += "\n\nSmall project files:\n\n" + "\n\n".join(blocks)
            blocks = []
        hits = [h for h in PROJECT_INDEX.search(CHATS, pid, query, k=40) if h[0] not in whole]
        for fid, fname, idx, n, chunk in hits:
            block = f"### {fname} (part {idx + 1} of {n})\n{chunk}"
            t = count_tokens(block)
            if t > remaining - 60:
                continue
            blocks.append(block)
            remaining -= t
            if remaining < 150:
                break
        text += ("\n\nThe bigger files don't fit whole: the parts related to the latest message are attached to it; "
                 "call project_search for anything else.")
        excerpts = ("From the project files:\n\n" + "\n\n".join(blocks)) if blocks else ""
        if split:
            return text, excerpts, count_tokens(text) + count_tokens(excerpts)
        return (text + "\n\n" + excerpts).strip(), count_tokens(text) + count_tokens(excerpts)


TOOL_GROUPS = {
    "core": ["calculate", "remember", "set_reminder", "add_to_list", "web_search", "fetch_page"],
    "electronics": ["ohms_law", "voltage_divider", "led_resistor", "rc_filter", "combine_resistors",
                    "standard_resistor", "render_diagram"],
    "lists": ["list_reminders", "cancel_reminder", "show_list", "check_off", "forget"],
    "github": ["github_repos", "github_read", "github_search", "github_history", "github_issues", "github_builds",
               "github_commit", "github_pull_request"],
    "project": ["project_note", "project_search"],
    "library": ["library_search"],
    "blender": ["blender_project", "blender_step", "blender_inspect", "blender_generate"],
    "cad": ["cad_project", "cad_step", "cad_inspect"],
    "pcb": ["pcb_project", "pcb_check", "pcb_circuit", "pcb_library", "pcb_step"],
    "move": ["move_stella"],
    "code": ["run_python"],
}
TOOL_TRIGGERS = {
    "code": re.compile(r"\b(python|script|run (this|the|it|my) code|test (this|the|my) (code|function)|plot|graph|"
                       r"chart|simulat|csv|spreadsheet|analy[sz]e (the |this )?(data|file|numbers)|compute|"
                       r"numpy|fft|monte carlo|regression|curve fit|statistic|integrat|derivative|"
                       r"bode|step response|transfer function)\b", re.I),
    "move": re.compile(r"\b(move|migrate|transfer|relocate)\b.{0,30}\b(yourself|stella|you|over|everything|"
                       r"to (the |my )?(server|desktop|laptop|pc|computer|proxmox))|\bmove (to|over)\b", re.I),
    "electronics": re.compile(r"resist|volt|ohm|\bleds?\b|circuit|schemat|diagram|flow ?chart|capacit|filter|divider|"
                              r"current|\bamps?\b|watt|pcb|timing|waveform|state machine|e12|e24|e96|draw|sketch|"
                              r"inductor|transistor|mosfet|op.?amp|\bmah\b|frequency|\bhz\b", re.I),
    "lists": re.compile(r"remind|\blists?\b|shopping|check(ed)? off|tick|cancel|to.?do|groceries|forget|"
                        r"don'?t remember|wrong about", re.I),
    "github": re.compile(r"github|\brepo|commit|branch|pull request|\bpr\b|actions|\bbuild|workflow|\bissues?\b|"
                         r"source code|my code|codebase", re.I),
    "blender": re.compile(r"blender|\b3d\b|3-d|\bmesh|\bvrm\b|\bglb\b|\bfbx\b|model(l)?ing|sculpt|\buvs?\b|"
                          r"\brig(ged|ging)?\b|armature|topology|polygon|vertices", re.I),
    "cad": re.compile(r"freecad|\bcad\b|\bstep file|\.step\b|\bstl\b|3d print|bracket|enclosure|\bmount\b|"
                      r"fillet|chamfer|\bmm\b|millimet|\bpart\b.*\b(design|model)|fan duct", re.I),
    "pcb": re.compile(r"kicad|\bpcb\b|circuit board|schematic|gerber|footprint|\bdrc\b|\berc\b|\btraces?\b|"
                      r"\bnets?\b|copper|\bbom\b", re.I),
}


def select_tools(query, mode="", mode_tools="all", in_project=False, sticky=()):
    """Picks the server tools worth sending for this message. Every tool definition costs context tokens
    (all of them together are about 4K), so only likely-needed groups go with each request."""
    if mode_tools == "minimal":
        names = ["remember", "set_reminder", "add_to_list", "web_search", "fetch_page"]
        if TOOL_TRIGGERS["lists"].search(query or ""):
            names.append("forget")
    else:
        names = list(TOOL_GROUPS["core"])
        for group, rx in TOOL_TRIGGERS.items():
            if rx.search(query or ""):
                names += TOOL_GROUPS[group]
        if mode == "electronics":
            names += TOOL_GROUPS["electronics"]
        if mode == "programmer":
            names += TOOL_GROUPS["github"]
        if mode in ("programmer", "electronics", "cad", "pcb", "tutor"):
            names += TOOL_GROUPS["code"]
        if mode == "modeler":
            names += TOOL_GROUPS["blender"]
        if mode == "cad":
            names += TOOL_GROUPS["cad"]
        if mode == "pcb":
            names += TOOL_GROUPS["pcb"]
    if in_project:
        names += TOOL_GROUPS["project"]
    if CHATS.library()["files"]:
        names += TOOL_GROUPS["library"]
    # Tools already offered earlier in this chat stay, so the tool list (which sits at the start of the prompt)
    # only grows occasionally instead of changing every message; that lets the model server reuse its cache.
    names += [n for n in sticky if not (n in PROJECT_TOOLS and not in_project)]
    wanted = set(names)
    return [n for n in SERVER_TOOLS if n in wanted]


_MEMVEC = {}


def select_memories(query, limit=8, all_up_to=12):
    """(stable_section, per_message_lines). Few memories: all go in the stable instructions. Many: the newest few
    stay stable and the ones related to this message ride along with it, chosen by meaning (or keywords)."""
    mems = CHATS.memories()
    if not mems:
        return "", ""
    head = "What you remember about the user (id in brackets, for forget). Use naturally; don't recite.\n"
    if len(mems) <= all_up_to:
        return head + "\n".join(f"- [{m['id']}] {m['text']}" for m in mems), ""
    # Always kept: the newest few, plus standing preferences ("prefers short answers") that apply to everything
    pref = re.compile(r"\b(prefers?|likes?|wants?|always|never|don'?t|do not|call(s)? (him|her|them|me)|"
                      r"goes by|name is|pronouns?)\b", re.I)
    stable = mems[:4] + [m for m in mems[4:] if pref.search(m["text"])][:6]
    rest = [m for m in mems if m not in stable]
    scores = None
    if EMBEDDER.url and query.strip():
        try:
            import array
            todo = [m["text"] for m in rest if m["text"] not in _MEMVEC]
            if todo:
                for t, v in zip(todo, EMBEDDER.embed(todo, EMBEDDER.DOC)):
                    _MEMVEC[t] = array.array("f", v)
            q = array.array("f", EMBEDDER.embed([query[-2000:]], EMBEDDER.QUERY)[0])
            scores = [sum(a * b for a, b in zip(_MEMVEC[m["text"]], q)) for m in rest]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            scores = None
    if scores is None:
        qt = set(_terms(query))
        scores = [len(qt & set(_terms(m["text"]))) / (1 + len(qt)) for m in rest]
    ranked = [m for sc, m in sorted(zip(scores, rest), key=lambda x: -x[0]) if sc > (0.35 if EMBEDDER.url else 0)][:limit]
    turn = ("Possibly relevant memories:\n" + "\n".join(f"- [{m['id']}] {m['text']}" for m in ranked)) if ranked else ""
    return head + "\n".join(f"- [{m['id']}] {m['text']}" for m in stable), turn


HARD_QUESTION = re.compile(
    r"```|\bdebug|\bbug\b|\berror\b|exception|traceback|stack ?trace|doesn'?t (work|compile)|won'?t (work|compile)|"
    r"what'?s wrong|fix (this|my|it)|why (does|is|do|did|doesn'?t|isn'?t|won'?t)|\bprove\b|derive|\bsolve\b|"
    r"algorithm|optimi[sz]|refactor|architect|\bdesign (a|an|the|my)\b|trade-?offs?|compare|pros and cons|"
    r"step by step|work out|figure out|\bplan (a|an|my|the)\b|equation|integral|probability|review (my|this)",
    re.I)


def wants_thinking(query):
    """Auto thinking: reason first only when the newest message looks hard (debugging, maths, design, long
    questions), and answer straight away otherwise ("set a timer" shouldn't think)."""
    last = (query or "").strip().splitlines()
    text = query or ""
    return bool(HARD_QUESTION.search(text)) or len(text) > 700 or (len(last) > 12)


def plan_context(data):
    """Works out what fits in the model's context for the next request.

    Input: project_id, query, fixed (system prompt + tool definitions), messages [{role, content}], images.
    Output: the project section, token counts, and keep_from (index of the oldest message that still fits)."""
    n_ctx = context_size()
    reply_reserve = min(4096, max(1024, n_ctx // 5))
    # Thinking: "on"/"off" from the user or mode, or "auto" (decided from the message here)
    pref = str(data.get("thinking") or "")
    think = pref == "on" or (pref == "auto" and wants_thinking(str(data.get("last", "")) or str(data.get("query", ""))))
    if think:
        reply_reserve += 1024 if n_ctx <= 8192 else 2048   # room for the capped reasoning
    tools = None
    if "mode_tools" in data:   # newer clients let the server choose which server tools to send
        tools = select_tools(str(data.get("query", "")), str(data.get("mode", "")), str(data.get("mode_tools")),
                             bool(data.get("project_id")), [str(n) for n in (data.get("sticky") or [])])
    if tools is not None and data.get("auto") and "switch_mode" not in tools:
        tools = list(tools) + ["switch_mode"]      # Auto chats: Stella can switch when the quick check missed
    chat_id = str(data.get("chat_id") or "")
    if tools is not None and chat_id:
        tools = list(tools) + [t for t in chat_tools_for(chat_id, str(data.get("last") or data.get("query") or ""))
                               if t not in tools]
        if "document" not in tools and (CHATS.docs(chat_id) or str(data.get("mode", "")) in ("programmer", "writer")
                                        or DOC_TRIGGER.search(str(data.get("last") or data.get("query") or ""))):
            tools.append("document")
    tools_json = json.dumps([SERVER_TOOLS[n][1] for n in tools]) if tools else ""
    split = bool(data.get("split"))   # newer clients: stable instructions + a per-message context block
    query = str(data.get("query", ""))
    mem_stable, mem_turn = select_memories(query) if split else ("", "")
    notes = chat_notes_section(chat_id) if split else ""
    if notes:   # pinned notes: always included, with the stable instructions (they change rarely)
        mem_stable = (mem_stable + "\n\n" + notes).strip()
    docs = docs_section(chat_id) if split else ""
    if docs:    # documents beside the chat: their names and sections, not their text
        mem_stable = (mem_stable + "\n\n" + docs).strip()
    fixed_t = count_tokens(str(data.get("fixed", "")) + tools_json) + 900 * int(data.get("images") or 0)
    fixed_t += count_tokens(mem_stable) + count_tokens(mem_turn)
    avail = n_ctx - reply_reserve - fixed_t
    proj_text, proj_t, excerpts = "", 0, ""
    pid = str(data.get("project_id") or "")
    if pid:
        share = 0.2 if n_ctx <= 8192 else 0.3
        # capped: excerpts ride on the newest message and are re-read each time, so a 64K window mustn't
        # turn into 19K tokens of excerpts per message (10K is what the 32K window already used)
        budget = max(0, min(int(n_ctx * share), 10000, avail - 600))
        if split:
            proj_text, excerpts, proj_t = build_project_context(pid, query, budget, split=True)
        else:
            proj_text, proj_t = build_project_context(pid, query, budget)
    lib = CHATS.library()["files"]
    if lib:
        names = "; ".join(f["name"] for f in lib[:40]) + ("; …" if len(lib) > 40 else "")
        lib_text = ("## Document library\nThe user's shared library (usable from any chat) contains: " + names +
                    ". Call library_search to look things up in it when it could help.")
        proj_text = (proj_text + "\n\n" + lib_text).strip()
        proj_t += count_tokens(lib_text)
    avail -= proj_t
    msgs = data.get("messages") or []
    drop, lane_note = (fold_lanes(data.get("chat_id"), msgs) if data.get("auto") and data.get("chat_id")
                       else (set(), ""))
    if drop:   # other topics in this chat: left out and summarized (the client drops the same indices)
        msgs = [m for i, m in enumerate(msgs) if i not in drop]
        avail -= count_tokens(lane_note)
    counts = [count_tokens(str(m.get("content", ""))) + 6 for m in msgs]
    keep_from, kept = len(msgs), 0
    for i in range(len(msgs) - 1, -1, -1):
        if kept + counts[i] > avail and keep_from < len(msgs):
            break
        kept += counts[i]
        keep_from = i
    return {"n_ctx": n_ctx, "reply_reserve": reply_reserve, "fixed_tokens": fixed_t, "project_tokens": proj_t,
            "history_tokens": sum(counts), "kept_tokens": kept, "keep_from": keep_from,
            "project_context": proj_text, "tools": tools, "memory_context": mem_stable, "think": think,
            "drop": sorted(drop), "lane_note": lane_note,
            "turn_context": "\n\n".join(x for x in (mem_turn, excerpts, lane_note) if x)}


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


# ---- Code runner: Python on the server ---------------------------------------------------------------------------
# Stella runs short Python scripts to check maths, test code, simulate circuits and analyse files, instead of
# guessing. Each run gets an empty folder (plus any files it asked for), a time limit, a memory limit and, where
# Linux allows it, no network. Pictures it saves (plots) show in the chat. A guard rail, not a sandbox: the code
# is checked before it runs, and it can't reach past its folder through the usual file and process functions.
CODE_RUNNER_CMD = None    # e.g. ["StellAI.exe", "--python-runner"] in the packaged Windows app
CODE_BAD_IMPORTS = {"subprocess", "socket", "ctypes", "multiprocessing", "requests", "urllib", "http", "ftplib",
                    "smtplib", "telnetlib", "asyncio", "ssl", "webbrowser", "pty", "pexpect", "paramiko", "winreg",
                    "shutil", "importlib", "signal", "resource", "sqlite3"}
CODE_BAD_CALLS = {"system", "popen", "spawnl", "spawnv", "spawnve", "spawnvp", "execv", "execve", "execl", "execlp",
                  "execvp", "fork", "forkpty", "kill", "killpg", "remove", "unlink", "rmdir", "removedirs", "rename",
                  "replace", "chmod", "chown", "symlink", "link", "putenv", "chdir", "startfile", "__import__",
                  "eval", "exec", "compile", "breakpoint", "rmtree", "setrlimit"}
CODE_UNSHARE = {"checked": False, "ok": False}


def check_python_code(code):
    """Problems that stop a script from running (files outside its folder, processes, network)."""
    if len(code) > 60000:
        return ["the script is too long (60,000 characters max)"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error on line {e.lineno}: {e.msg}"]
    probs = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            probs += [f"import {a.name} isn't allowed here" for a in n.names if a.name.split(".")[0] in CODE_BAD_IMPORTS]
        elif isinstance(n, ast.ImportFrom):
            if n.level or (n.module or "").split(".")[0] in CODE_BAD_IMPORTS:
                probs.append(f"from {n.module} import isn't allowed here")
        elif isinstance(n, ast.Attribute) and (n.attr in CODE_BAD_CALLS or n.attr.startswith("__")):
            probs.append(f".{n.attr} isn't allowed (no processes, deleting files or reaching outside the folder)")
        elif isinstance(n, ast.Name) and n.id in CODE_BAD_CALLS:
            probs.append(f"{n.id}() isn't allowed")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open" and n.args:
            a = n.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and (
                    os.path.isabs(a.value) or ".." in a.value.replace("\\", "/").split("/") or a.value.startswith("~")):
                probs.append(f"open('{a.value}') reaches outside the run's folder; use plain file names")
    return sorted(set(probs))[:8]


def _no_network_prefix():
    """["unshare", "-rn"] when this Linux lets a normal user run without network, else []."""
    if not CODE_UNSHARE["checked"]:
        CODE_UNSHARE["checked"] = True
        import shutil
        u = shutil.which("unshare")
        if u and os.name != "nt":
            try:
                CODE_UNSHARE["ok"] = subprocess.run([u, "-rn", "true"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                CODE_UNSHARE["ok"] = False
        CODE_UNSHARE["cmd"] = [u, "-rn"] if CODE_UNSHARE["ok"] else []
    return list(CODE_UNSHARE.get("cmd") or [])


CODE_PRELUDE = r'''# StellAI code runner: limits, then the script
import os, sys
try:
    import resource
    mb = int(os.environ.get("STELLAI_MEM_MB", "2048"))
    resource.setrlimit(resource.RLIMIT_AS, (mb * 2**20, mb * 2**20))
    secs = int(os.environ.get("STELLAI_CPU_S", "60"))
    resource.setrlimit(resource.RLIMIT_CPU, (secs, secs + 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 2**20, 200 * 2**20))
except Exception:
    pass
os.environ["MPLBACKEND"] = "Agg"
sys.argv = ["script.py"]
'''


def tool_run_python(args):
    code = str(args.get("code", ""))
    if not code.strip():
        raise ValueError("give code: the Python script to run")
    probs = check_python_code(code)
    if probs:
        return {"ok": False, "error": "not run: " + "; ".join(probs)}, "Script blocked"
    timeout = max(5, min(300, int(args.get("timeout") or 60)))
    work = tempfile.mkdtemp(prefix="stellai-run-")
    copied = []
    try:
        for pid in [str(p) for p in (args.get("photos") or [])][:6]:     # chat photos, by their [photo id]
            src = os.path.join(IMAGE_DIR, re.sub(r"[^0-9a-f]", "", pid) + ".jpg")
            if os.path.isfile(src):
                import shutil as _sh
                _sh.copyfile(src, os.path.join(work, os.path.basename(src)))
                copied.append(os.path.basename(src))
        project = str(args.get("project_id") or "")
        for name in [str(n) for n in (args.get("project_files") or [])][:10]:
            text = _project_file_text(project, name)
            if text is not None:
                safe = re.sub(r"[^\w.\- ]", "_", os.path.basename(name))[:80] or "file.txt"
                with open(os.path.join(work, safe), "w", encoding="utf-8") as f:
                    f.write(text)
                copied.append(safe)
        with open(os.path.join(work, "script.py"), "w", encoding="utf-8") as f:
            f.write(CODE_PRELUDE + "\n" + code)
        before = set(os.listdir(work))
        runner = list(CODE_RUNNER_CMD) if CODE_RUNNER_CMD else [sys.executable, "-I"]
        cmd = _no_network_prefix() + runner + [os.path.join(work, "script.py")]
        env = {"PATH": os.environ.get("PATH", ""), "HOME": work, "TMPDIR": work, "MPLBACKEND": "Agg",
               "MPLCONFIGDIR": os.path.join(work, ".mpl"), "PYTHONIOENCODING": "utf-8", "STELLAI_CPU_S": str(timeout),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        started = time.time()
        try:
            p = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout + 5, stdin=subprocess.DEVNULL,
                               creationflags=0x08000000 if os.name == "nt" else 0)
            code_rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            code_rc, out, err = -9, (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else \
                (e.stdout or ""), f"stopped: the script ran longer than {timeout} s"
        took = round(time.time() - started, 2)
        if code_rc != 0 and not err.strip() and (code_rc < 0 or took >= timeout - 0.5):
            err = f"stopped: the script used its {timeout} s time limit"
        made = sorted(n for n in set(os.listdir(work)) - before if not n.startswith("."))
        images, texts = [], {}
        for name in made:
            full = os.path.join(work, name)
            ext = os.path.splitext(name)[1].lower()
            if ext == ".png" and len(images) < 4 and os.path.getsize(full) < 8_000_000:
                iid = uuid.uuid4().hex[:16]
                import shutil as _sh
                _sh.copyfile(full, os.path.join(DIAGRAM_DIR, iid + ".png"))
                images.append(iid)
            elif ext in (".txt", ".csv", ".json", ".md", ".log") and len(texts) < 4:
                with open(full, encoding="utf-8", errors="replace") as f:
                    texts[name] = f.read(4000)
        res = {"ok": code_rc == 0, "exit_code": code_rc, "seconds": took, "stdout": out[-8000:],
               "stderr": err[-4000:], "files_made": made[:20], "file_previews": texts, "inputs": copied,
               "network": "off" if _no_network_prefix() else "not blocked on this computer"}
        if images:
            res["image"] = images[0]
            res["title"] = "Plot"
            if len(images) > 1:
                res["more_images"] = images[1:]
        summary = (f"Ran Python ({took:g} s)" if code_rc == 0 else
                   f"Python stopped with an error ({took:g} s)")
        return res, summary
    finally:
        import shutil as _sh
        _sh.rmtree(work, ignore_errors=True)


def _project_file_text(project_id, name):
    """The extracted text of a project (or library) file by name, or None."""
    p = CHATS.project(project_id) if project_id else None
    for f in (p or {}).get("files", []):
        if f.get("name") == name or os.path.basename(f.get("name", "")) == os.path.basename(name):
            return _file_text(f["id"])
    return None


SERVER_TOOLS["run_python"] = (tool_run_python, _fn(
    "run_python",
    "Run a short Python 3 script on the server and get its output: check maths and unit conversions, test code, "
    "simulate a circuit or a filter, analyse a CSV, make a plot (save it with plt.savefig('plot.png'); pictures "
    "show in the chat). numpy and matplotlib are available. The script starts in an empty folder (plus any "
    "photos or project files you list), may only use plain file names, has no network, and is stopped after the "
    "time limit. print() what you need. Prefer this over doing long arithmetic in your head.",
    {"code": _p("The Python script"),
     "timeout": _p("Seconds, 5-300 (default 60)", "integer"),
     "photos": {"type": "array", "items": {"type": "string"},
                "description": "Photo ids from [photo ...] in the chat, copied in as <id>.jpg"},
     "project_files": {"type": "array", "items": {"type": "string"},
                       "description": "Names of this project's files to copy in (their text)"}},
    ["code"]))


# ---- Continue in a new chat -----------------------------------------------------------------------------------
def continue_in_new_chat(conv_id):
    """A new chat that starts from this one's summary and pinned notes (same project and mode)."""
    with CHATS.lock:
        c = CHATS.db.execute("SELECT title, project_id, mode FROM conversations WHERE id=? AND deleted=0",
                             (conv_id,)).fetchone()
        rows = CHATS.db.execute("SELECT role, text FROM messages WHERE conv_id=? AND deleted=0 ORDER BY created_at, id",
                                (conv_id,)).fetchall()
    if not c:
        raise ValueError("no such chat")
    last = max((i for i, r in enumerate(rows) if r[0] == "summary"), default=-1)
    previous = rows[last][1] if last >= 0 else ""
    msgs = [{"role": r[0], "content": r[1]} for r in rows[last + 1:] if r[0] in ("user", "assistant", "tool")]
    summary = summarize_chat(previous, msgs, _chat_style(c[2] or "")) if msgs else previous
    if not summary.strip():
        raise ValueError("there's nothing to carry over yet")
    new_id = uuid.uuid4().hex
    now = int(time.time() * 1000)
    title = (c[0] or "Chat")[:150] + " (continued)"
    with CHATS.lock:
        CHATS.db.execute("INSERT INTO conversations (id, title, created_at, updated_at, deleted, seq, project_id, mode) "
                         "VALUES(?,?,?,?,0,?,?,?)", (new_id, title, now, now, CHATS._next(), c[1] or "", c[2] or ""))
        CHATS.db.execute("INSERT INTO messages (id, conv_id, role, text, reasoning, created_at, updated_at, seq, "
                         "deleted) VALUES(?,?,?,?,?,?,?,?,0)",
                         (uuid.uuid4().hex, new_id, "summary", "Carried over from “" + (c[0] or "the previous chat") +
                          "”:\n" + summary.strip(), "", now, now + 1, CHATS._next()))
        CHATS.db.commit()
    for n in CHATS.chat_notes(conv_id):
        try:
            CHATS.add_chat_note(new_id, n["text"], n["source"])
        except ValueError:
            pass
    return {"conversation_id": new_id, "title": title}


def doc_outline(text, kind):
    """[(line number, level, title)] — headings for text, definitions for code."""
    out = []
    in_fence = False
    for i, line in enumerate(text.split("\n"), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if kind != "code" and not in_fence:
            m = HEADING.match(line)
            if m:
                out.append((i, len(m.group(1)), m.group(2).strip()))
        elif kind == "code" and CODE_DEF.match(line) and len(out) < 200:
            out.append((i, 1 + (len(line) - len(line.lstrip())) // 4, line.strip()[:90]))
    return out


def _find_section(text, kind, name):
    """(start, end) line indices (0-based, end exclusive) of the section whose heading matches name."""
    lines = text.split("\n")
    outline = doc_outline(text, kind)
    want = _norm_name(name)
    hit = next((o for o in outline if _norm_name(o[2]) == want), None) or \
        next((o for o in outline if want and want in _norm_name(o[2])), None)
    if not hit:
        names = "; ".join(o[2] for o in outline[:40]) or "none"
        raise ValueError(f"no section called '{name}'. Sections: {names}")
    start = hit[0] - 1
    end = len(lines)
    for o in outline:
        if o[0] > hit[0] and o[1] <= hit[1]:
            end = o[0] - 1
            break
    while end > start + 1 and not lines[end - 1].strip():   # keep the blank lines between sections
        end -= 1
    return start, end


def _doc_ref(d, verb, version, extra=""):
    return f"{verb} “{d['title']}” (v{version}){extra} {DOC_REF.format(id=d['id'])}"


def _resolve_doc(conv_id, ref):
    ref = str(ref or "").strip().strip("[]").replace("doc:", "")
    if ref:
        d = CHATS.doc(ref)
        if d:
            return d
        for x in CHATS.docs(conv_id):
            if _norm_name(x["title"]) == _norm_name(ref):
                return CHATS.doc(x["id"])
    docs = CHATS.docs(conv_id)
    if not ref and len(docs) == 1:
        return CHATS.doc(docs[0]["id"])
    raise ValueError("which document? " + ("; ".join(f"{x['title']} [doc:{x['id']}]" for x in docs) or
                                           "this chat has none yet: create one"))


def _snippet(text, pos, n=3):
    lines = text.split("\n")
    line = text[:pos].count("\n")
    a, b = max(0, line - n), min(len(lines), line + n + 1)
    return "\n".join(f"{i + 1:>5}| {lines[i]}" for i in range(a, b))


def tool_document(args):
    cid = str(args.get("conversation_id") or "")
    action = str(args.get("action", "read")).lower().strip()
    if action == "list":
        return {"documents": CHATS.docs(cid)}, "Listed this chat's documents"
    if action == "create":
        content = str(args.get("content", ""))
        lang = str(args.get("language", "")).lower()
        kind = "code" if lang and lang not in ("markdown", "md", "text", "txt") else "text"
        did = CHATS.create_doc(cid, args.get("title", ""), content, kind, lang)
        d = CHATS.doc(did)
        return {"doc": did, "version": 1, "lines": content.count("\n") + 1,
                "note": "It's shown beside the chat. Refer to it briefly instead of repeating it."}, \
            _doc_ref(d, "Created", 1)
    d = _resolve_doc(cid, args.get("doc"))
    text, kind = d["text"], d["kind"]
    if action in ("read", "outline"):
        out = {"doc": d["id"], "title": d["title"], "version": d["version"], "lines": text.count("\n") + 1,
               "outline": [f"line {o[0]}: {'  ' * (o[1] - 1)}{o[2]}" for o in doc_outline(text, kind)][:120]}
        if args.get("section"):
            a, b = _find_section(text, kind, args["section"])
            out["section"] = "\n".join(text.split("\n")[a:b])
        elif args.get("start_line"):
            lines = text.split("\n")
            a = max(1, int(args["start_line"]))
            b = min(len(lines), int(args.get("end_line") or a + 120))
            out["text"] = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(a, b + 1))
        elif action == "read":
            limit = int(args.get("_max_chars") or 12000)
            out["text"] = text if len(text) <= limit else text[:limit]
            if len(text) > limit:
                out["more"] = "longer than shown: read a section or a line range"
        return out, f"Read “{d['title']}”"
    if action == "replace":
        find, repl = str(args.get("find", "")), str(args.get("replace", ""))
        if not find:
            raise ValueError("give find (the exact text to change, copied from the document) and replace")
        n = text.count(find)
        if n == 0:
            # tolerate differences in surrounding whitespace at line ends
            loose = re.compile(r"[ \t]*\n[ \t]*".join(re.escape(part.strip()) for part in find.strip().split("\n")))
            ms = list(loose.finditer(text))
            if len(ms) != 1:
                raise ValueError("that text isn't in the document exactly (read it first and copy the passage)")
            start, end = ms[0].span()
        elif n > 1:
            raise ValueError(f"that text appears {n} times; include more around it so it's unique")
        else:
            start = text.index(find)
            end = start + len(find)
        new = text[:start] + repl + text[end:]
        v = CHATS.save_doc(d["id"], new, "stella", str(args.get("note", ""))[:200])
        delta = repl.count("\n") - text[start:end].count("\n")
        return {"doc": d["id"], "version": v, "around": _snippet(new, start)}, \
            _doc_ref(d, "Updated", v, f": changed {text[start:end].count(chr(10)) + 1} line(s)" +
                     (f", {delta:+d}" if delta else ""))
    if action in ("set_section", "append", "insert_after"):
        content = str(args.get("content", ""))
        lines = text.split("\n")
        if action == "set_section":
            a, b = _find_section(text, kind, args.get("section", ""))
            new_lines = lines[:a] + content.split("\n") + lines[b:]
            what = f": rewrote section “{args.get('section')}”"
        elif action == "insert_after":
            a, b = _find_section(text, kind, args.get("section", ""))
            new_lines = lines[:b] + content.split("\n") + lines[b:]
            what = f": added after “{args.get('section')}”"
        else:
            new_lines = lines + ([""] if lines and lines[-1].strip() else []) + content.split("\n")
            what = ": added to the end"
        new = "\n".join(new_lines)
        v = CHATS.save_doc(d["id"], new, "stella", str(args.get("note", ""))[:200])
        return {"doc": d["id"], "version": v, "lines": new.count("\n") + 1}, _doc_ref(d, "Updated", v, what)
    if action == "rename":
        v = CHATS.save_doc(d["id"], text, "stella", "renamed", title=args.get("title", ""))
        return {"doc": d["id"], "version": v}, _doc_ref(CHATS.doc(d["id"]), "Renamed", v)
    if action == "revert":
        ver = int(args.get("version") or 0)
        old = CHATS.doc_version_text(d["id"], ver)
        if old is None:
            raise ValueError(f"version {ver} isn't kept; recent: {[x['version'] for x in d['versions'][:10]]}")
        v = CHATS.save_doc(d["id"], old, "stella", f"back to v{ver}")
        return {"doc": d["id"], "version": v}, _doc_ref(d, f"Back to v{ver} in", v)
    raise ValueError("action: create, read, outline, replace, set_section, insert_after, append, rename, revert, list")


SERVER_TOOLS["document"] = (tool_document, _fn(
    "document",
    "Documents beside the chat, for anything long the user will keep: code over ~60 lines, chapters, essays, specs, "
    "READMEs. The user sees them in a side panel; the chat keeps only a one-line reference, so don't paste the "
    "content into your reply. Edit in small pieces instead of rewriting: replace (find = an exact passage copied "
    "from the document, replace = its new text), set_section (a heading's whole section, or a code definition), "
    "insert_after (after a section), append. read/outline show the sections and line numbers first; every change "
    "is a numbered version (revert to undo). action create: title + content (+ language for code).",
    {"action": _p("create, read, outline, replace, set_section, insert_after, append, rename, revert or list"),
     "doc": _p("The document: its id from [doc:...] or its title (optional when the chat has only one)"),
     "title": _p("For create/rename"), "content": _p("For create, set_section, insert_after, append"),
     "language": _p("For create: python, kotlin, cpp, … for code; leave out for text"),
     "find": _p("For replace: the exact text to change"), "replace": _p("For replace: the new text"),
     "section": _p("For read/set_section/insert_after: a heading or definition name"),
     "start_line": _p("For read: first line", "integer"), "end_line": _p("For read: last line", "integer"),
     "version": _p("For revert", "integer"), "note": _p("Optional: what this change does")},
    ["action"]))


def docs_section(conv_id):
    docs = CHATS.docs(conv_id) if conv_id else []
    if not docs:
        return ""
    lines = ["## Documents beside this chat",
             "The user sees these in a side panel. Read or edit them with the document tool; don't paste them."]
    for x in docs[:12]:
        d = CHATS.doc(x["id"])
        heads = [o[2] for o in doc_outline(d["text"], d["kind"])][:12]
        lines.append(f"- “{x['title']}” [doc:{x['id']}] v{x['version']}, {d['text'].count(chr(10)) + 1} lines"
                     + (f"; sections: {', '.join(heads)}" if heads else ""))
    return "\n".join(lines)


def tool_project_note(args):
    pid = str(args.get("project_id", ""))
    if not pid or not CHATS.project(pid):
        raise ValueError("this chat isn't in a project")
    note = CHATS.add_project_note(pid, args.get("note", ""), "stella")
    return {"saved": note["text"], "id": note["id"]}, f"Saved to project notes: {note['text']}"


def tool_project_search(args):
    pid = str(args.get("project_id", ""))
    if not pid or not CHATS.project(pid):
        raise ValueError("this chat isn't in a project")
    query = str(args.get("query", ""))
    hits = PROJECT_INDEX.search(CHATS, pid, query, k=6)
    results = [{"file": f, "part": f"{i + 1}/{n}", "text": t} for _, f, i, n, t in hits]
    return {"results": results, "found": len(results)}, \
        f"Searched project files for “{query}”: {len(results)} match{'es' if len(results) != 1 else ''}"


SERVER_TOOLS["project_note"] = (tool_project_note, _fn(
    "project_note",
    "Save a note to the current project (decisions, specs, part choices, pin assignments, TODOs) so it is "
    "available in every chat of this project. Only available in project chats.",
    {"note": _p("The note, one or two sentences")}, ["note"]))
SERVER_TOOLS["project_search"] = (tool_project_search, _fn(
    "project_search",
    "Search the current project's files (datasheets, code, documents) for a topic, register, pin, function name "
    "etc. Use when the answer may be in the files but isn't shown in your instructions.",
    {"query": _p("Keywords to look for, e.g. 'ADC reference voltage'")}, ["query"]))
PROJECT_TOOLS = {"project_note", "project_search"}


def tool_library_search(args):
    query = str(args.get("query", ""))
    hits = PROJECT_INDEX.search(CHATS, LIBRARY_ID, query, k=6)
    results = [{"file": f, "part": f"{i + 1}/{n}", "text": t} for _, f, i, n, t in hits]
    return {"results": results, "found": len(results)}, \
        f"Searched the library for “{query}”: {len(results)} match{'es' if len(results) != 1 else ''}"


SERVER_TOOLS["library_search"] = (tool_library_search, _fn(
    "library_search",
    "Search the user's shared document library (datasheets, manuals, notes, code they've added) for a topic. "
    "Works in any chat.",
    {"query": _p("What to look for, in plain words, e.g. 'maximum charge current of the TP4056'")}, ["query"]))


def tool_set_reminder(args):
    due = _parse_when(args.get("when", ""))
    r = CHATS.add_reminder(args.get("text", ""), due, str(args.get("repeat") or "none"))
    rep = "" if r["repeat"] == "none" else f", repeats {r['repeat']}"
    return {"id": r["id"], "text": r["text"], "due": _fmt_time(r["due_at"]), "repeat": r["repeat"],
            "note": "It will ring on all the user's phones."}, f"Reminder set for {_fmt_time(r['due_at'])}{rep}: {r['text']}"


def tool_list_reminders(args):
    items = [r for r in CHATS.reminders(0) if not r["done"]]
    return {"reminders": [{"id": r["id"], "text": r["text"], "due": _fmt_time(r["due_at"]), "repeat": r["repeat"]}
                          for r in items]}, f"{len(items)} upcoming reminder{'s' if len(items) != 1 else ''}"


def tool_cancel_reminder(args):
    found = CHATS.find_reminders(args.get("reminder", ""))
    if not found:
        raise ValueError("no matching reminder; call list_reminders to see them")
    if len(found) > 1:
        return {"ambiguous": [{"id": r["id"], "text": r["text"], "due": _fmt_time(r["due_at"])} for r in found]}, \
            "Several reminders match"
    CHATS.update_reminder(found[0]["id"], cancel=True)
    return {"cancelled": found[0]["text"]}, f"Cancelled reminder: {found[0]['text']}"


def tool_add_to_list(args):
    items = args.get("items") or []
    if isinstance(items, str):
        items = [x for x in re.split(r"[,\n]", items)]
    name, added = CHATS.add_items(args.get("list", "to-do"), items)
    if not added:
        raise ValueError("nothing to add")
    return {"list": name, "added": added}, f"Added to {name} list: {', '.join(added)}"


def tool_show_list(args):
    name = str(args.get("list") or "").strip()
    lists = CHATS.lists()
    if not name:
        summary = {n: len([i for i in items if not i["done"]]) for n, items in lists.items()}
        return {"lists": summary}, f"{len(summary)} list{'s' if len(summary) != 1 else ''}"
    key = CHATS._list_name(name)
    items = lists.get(key, [])
    return {"list": key, "items": [("✓ " if i["done"] else "") + i["text"] for i in items]}, \
        f"{key.capitalize()} list: {len([i for i in items if not i['done']])} item(s)"


def tool_check_off(args):
    items = args.get("items") or []
    if isinstance(items, str):
        items = [items]
    name, changed = CHATS.check_items(args.get("list", "to-do"), items, delete=bool(args.get("remove")))
    if not changed:
        raise ValueError(f"didn't find those on the {name} list")
    return {"list": name, "checked": changed}, f"Checked off on {name} list: {', '.join(changed)}"


SERVER_TOOLS["set_reminder"] = (tool_set_reminder, _fn(
    "set_reminder",
    "Set a reminder that rings on all the user's phones at a given time, e.g. 'remind me to reflow the board at 5'. "
    "Use this for reminders; use set_alarm only for wake-up alarms in the phone's clock app.",
    {"text": _p("What to remind about, short, e.g. 'Reflow the board'"),
     "when": _p("Date and time in ISO 8601 with the user's UTC offset, e.g. 2026-11-04T17:00:00-06:00. Work it out "
                "from the current date and time in your instructions."),
     "repeat": _p("none (default), daily, weekdays, weekly or monthly")}, ["text", "when"]))
SERVER_TOOLS["list_reminders"] = (tool_list_reminders, _fn(
    "list_reminders", "List the user's upcoming reminders.", {}, []))
SERVER_TOOLS["cancel_reminder"] = (tool_cancel_reminder, _fn(
    "cancel_reminder", "Cancel a reminder.", {"reminder": _p("Its id or words from it")}, ["reminder"]))
SERVER_TOOLS["add_to_list"] = (tool_add_to_list, _fn(
    "add_to_list",
    "Add items to one of the user's lists (e.g. shopping, parts, to-do, movies). Lists are created as needed and "
    "synced to all devices.",
    {"list": _p("List name, e.g. 'shopping' or 'parts'"),
     "items": {"type": "array", "items": {"type": "string"}, "description": "Items to add"}}, ["list", "items"]))
SERVER_TOOLS["show_list"] = (tool_show_list, _fn(
    "show_list", "Show a list's items, or all list names when no list is given.",
    {"list": _p("List name, or empty for all lists")}, []))
SERVER_TOOLS["check_off"] = (tool_check_off, _fn(
    "check_off", "Tick off (or remove) items on a list.",
    {"list": _p("List name"), "items": {"type": "array", "items": {"type": "string"}, "description": "Items or words"},
     "remove": _p("true to delete them instead of ticking them off", "boolean")}, ["list", "items"]))


# ---- Web search and reading pages -------------------------------------------------
SEARXNG_URL = ""   # e.g. http://127.0.0.1:8888 (set in start())
WEB_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def _public_host(host):
    """True if every address the host resolves to is on the public internet (no LAN, localhost, metadata)."""
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ValueError(f"couldn't look up {host} (is the server online?)")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))):
            return False
    return True


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urlparse(newurl)
        if u.scheme not in ("http", "https") or not _public_host(u.hostname or ""):
            raise urllib.error.URLError("redirect to a private or unsupported address was blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_public(url, data=None, headers=None, timeout=20, max_bytes=3_000_000):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http and https addresses can be opened")
    if not _public_host(u.hostname):
        raise ValueError("that address is on a private network; Stella only reads public web pages")
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": WEB_UA, "Accept-Language": "en", **(headers or {})})
    with opener.open(req, timeout=timeout) as r:
        body = r.read(max_bytes + 1)
        return body[:max_bytes], r.headers.get("Content-Type", ""), r.geturl()


def _html_text(html):
    import html as _h
    title = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form|aside)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article|pre)>", "\n", html)
    html = re.sub(r"(?i)<li[^>]*>", "\n- ", html)
    text = _h.unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return (_h.unescape(title.group(1)).strip() if title else ""), text


def _searx(query, n):
    url = f"{SEARXNG_URL.rstrip('/')}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "StellAI"}), timeout=20) as r:
        data = json.loads(r.read())
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:300]}
            for x in data.get("results", [])[:n]]


def _duckduckgo(query, n):
    import html as _h
    body, _, _ = _open_public("https://html.duckduckgo.com/html/", data=urllib.parse.urlencode({"q": query}).encode(),
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    page = body.decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>([\s\S]*?)'
                         r'(?:class="result__snippet"[^>]*>([\s\S]*?)</a>|<div class="result results_links)', page):
        href = _h.unescape(m.group(1))
        q = parse_qs(urlparse(href).query)
        url = q.get("uddg", [href])[0]
        if url.startswith("//"):
            url = "https:" + url
        if "duckduckgo.com/y.js" in url:   # ads
            continue
        title = _h.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        snippet = _h.unescape(re.sub(r"<[^>]+>", "", m.group(4) or "")).strip()
        out.append({"title": title, "url": url, "snippet": snippet[:300]})
        if len(out) >= n:
            break
    return out


def tool_web_search(args):
    query = " ".join(str(args.get("query", "")).split())[:300]
    if not query:
        raise ValueError("what should I search for?")
    n = max(1, min(10, int(args.get("count") or 6)))
    source = "SearXNG"
    results = []
    if SEARXNG_URL:
        try:
            results = _searx(query, n)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"SearXNG unavailable ({e}); using DuckDuckGo")
    if not results:
        source = "DuckDuckGo"
        results = _duckduckgo(query, n)
    return {"query": query, "results": results, "source": source,
            "note": "Cite the URLs you use. Call fetch_page to read a result in full."}, \
        f"Searched the web for “{query}”: {len(results)} result{'s' if len(results) != 1 else ''}"


def tool_fetch_page(args):
    url = str(args.get("url", "")).strip()
    limit = max(1000, min(30000, int(args.get("max_chars") or 12000)))
    body, ctype, final = _open_public(url)
    if "pdf" in ctype or body[:5] == b"%PDF-":
        title, text = url.rsplit("/", 1)[-1], _pdf_text(body)
    elif "html" in ctype or b"<html" in body[:2000].lower():
        title, text = _html_text(body.decode("utf-8", "replace"))
    else:
        title, text = url, body.decode("utf-8", "replace")
    find = str(args.get("find") or "").strip().lower()
    if find and find in text.lower():
        i = text.lower().index(find)
        text = text[max(0, i - limit // 3):]
    truncated = len(text) > limit
    return {"url": final, "title": title, "text": text[:limit], "truncated": truncated}, \
        f"Read {title or final}"[:200]


SERVER_TOOLS["web_search"] = (tool_web_search, _fn(
    "web_search",
    "Search the web for current information: documentation, news, prices, part availability, error messages. "
    "Returns titles, URLs and snippets.",
    {"query": _p("Search query"), "count": _p("Number of results (1-10, default 6)", "integer")}, ["query"]))
SERVER_TOOLS["fetch_page"] = (tool_fetch_page, _fn(
    "fetch_page",
    "Read a public web page or PDF as text (e.g. a search result, documentation or a datasheet URL).",
    {"url": _p("The http(s) address"), "find": _p("Optional words to jump to in a long page"),
     "max_chars": _p("Maximum characters to return (default 12000)", "integer")}, ["url"]))


# ---- GitHub ------------------------------------------------------------------------
GITHUB_FILE = None      # JSON with token + default repo, set in start()
GITHUB_API = os.environ.get("STELLAI_GITHUB_API", "https://api.github.com")
APPROVAL_TOOLS = {"github_commit", "github_pull_request"}   # clients must ask the user first


def github_config():
    try:
        with open(GITHUB_FILE) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def save_github_config(data):
    cfg = github_config()
    if "token" in data:
        cfg["token"] = str(data.get("token") or "").strip()
    if "default_repo" in data:
        repo = str(data.get("default_repo") or "").strip().strip("/")
        if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("default repository must look like owner/name")
        cfg["default_repo"] = repo
    fd = os.open(GITHUB_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    os.replace(GITHUB_FILE + ".tmp", GITHUB_FILE)
    return github_status()


def _gh(method, path, body=None, accept="application/vnd.github+json", raw=False, timeout=30):
    token = github_config().get("token", "")
    url = path if path.startswith(("https://", "http://")) else GITHUB_API + path
    headers = {"Accept": accept, "User-Agent": "StellAI", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read()).get("message", "")
        except (ValueError, OSError):
            pass
        if e.code in (401, 403) and not token:
            msg += " (add a GitHub token in StellAI's settings to use private repositories)"
        elif e.code == 404:
            msg = "not found (check the repository name and path" + ("" if token else ", or add a GitHub token") + ")"
        raise RuntimeError(f"GitHub {e.code}: {msg}".strip())
    if raw:
        return data
    return json.loads(data) if data else {}


def github_status():
    cfg = github_config()
    out = {"token_set": bool(cfg.get("token")), "default_repo": cfg.get("default_repo", ""), "user": ""}
    if cfg.get("token"):
        try:
            out["user"] = _gh("GET", "/user").get("login", "")
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            out["error"] = str(e)
    return out


def _repo(args):
    repo = str(args.get("repo") or github_config().get("default_repo") or "").strip().strip("/")
    repo = re.sub(r"^https?://github\.com/", "", repo).removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("which repository? Give it as owner/name (or set a default repo in Settings → GitHub)")
    return repo


def _q(path):
    return urllib.parse.quote(str(path).strip("/"))


def tool_github_repos(args):
    if not github_config().get("token"):
        raise ValueError("add a GitHub token in Settings → GitHub to list your repositories")
    repos = _gh("GET", "/user/repos?per_page=50&sort=updated")
    return {"repos": [{"name": r["full_name"], "private": r["private"], "description": r.get("description") or "",
                       "default_branch": r["default_branch"]} for r in repos]}, f"Listed {len(repos)} repositories"


def tool_github_read(args):
    import base64
    repo = _repo(args)
    path = str(args.get("path") or "").strip("/")
    ref = str(args.get("ref") or "")
    data = _gh("GET", f"/repos/{repo}/contents/{_q(path)}" + (f"?ref={urllib.parse.quote(ref)}" if ref else ""))
    if isinstance(data, list):
        items = [x["path"] + ("/" if x["type"] == "dir" else "") for x in data]
        return {"repo": repo, "path": path or "/", "entries": items}, f"Listed {repo}/{path}"
    if data.get("encoding") == "base64" and data.get("content") is not None:
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    elif data.get("download_url"):
        text = _gh("GET", data["download_url"], raw=True).decode("utf-8", "replace")
    else:
        raise ValueError("can't read that file")
    start = max(1, int(args.get("start_line") or 1))
    lines = text.splitlines()
    chunk = "\n".join(lines[start - 1:start - 1 + 1500])
    truncated = len(chunk) > 40000 or start - 1 + 1500 < len(lines)
    return {"repo": repo, "path": path, "sha": data.get("sha"), "lines": len(lines), "from_line": start,
            "text": chunk[:40000], "truncated": truncated,
            "note": "Use start_line to read further." if truncated else ""}, f"Read {repo}/{path}"


def tool_github_search(args):
    repo = _repo(args)
    query = str(args.get("query", "")).strip()
    if not github_config().get("token"):
        raise ValueError("GitHub code search needs a token (Settings → GitHub)")
    data = _gh("GET", "/search/code?per_page=10&q=" + urllib.parse.quote(f"{query} repo:{repo}"),
               accept="application/vnd.github.text-match+json")
    hits = [{"path": i["path"], "matches": [m.get("fragment", "")[:300] for m in i.get("text_matches", [])][:2]}
            for i in data.get("items", [])]
    return {"repo": repo, "results": hits}, f"Searched {repo} for “{query}”: {len(hits)} file(s)"


def tool_github_history(args):
    repo = _repo(args)
    path = str(args.get("path") or "")
    n = max(1, min(30, int(args.get("count") or 10)))
    commits = _gh("GET", f"/repos/{repo}/commits?per_page={n}" + (f"&path={urllib.parse.quote(path)}" if path else ""))
    return {"repo": repo, "commits": [{"sha": c["sha"][:7], "message": c["commit"]["message"][:300],
                                       "author": c["commit"]["author"]["name"], "date": c["commit"]["author"]["date"]}
                                      for c in commits]}, f"Read {len(commits)} commits of {repo}"


def tool_github_issues(args):
    repo = _repo(args)
    state = args.get("state") if args.get("state") in ("open", "closed", "all") else "open"
    items = _gh("GET", f"/repos/{repo}/issues?per_page=20&state={state}")
    return {"repo": repo, "issues": [{"number": i["number"], "title": i["title"], "pull_request": "pull_request" in i,
                                      "state": i["state"], "body": (i.get("body") or "")[:500]} for i in items]}, \
        f"Read {len(items)} {state} issues/PRs of {repo}"


def tool_github_builds(args):
    """Recent GitHub Actions runs, with the end of the log for failed jobs (for fixing build errors)."""
    repo = _repo(args)
    runs = _gh("GET", f"/repos/{repo}/actions/runs?per_page=6").get("workflow_runs", [])
    out = [{"id": r["id"], "workflow": r["name"], "status": r["status"], "result": r.get("conclusion"),
            "branch": r["head_branch"], "commit": r["head_sha"][:7], "title": r.get("display_title", ""),
            "started": r["run_started_at"]} for r in runs]
    failed = next((r for r in runs if r.get("conclusion") == "failure"), None)
    detail = None
    if failed:
        jobs = _gh("GET", f"/repos/{repo}/actions/runs/{failed['id']}/jobs").get("jobs", [])
        detail = []
        for j in jobs:
            if j.get("conclusion") != "failure":
                continue
            steps = [s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"]
            log_tail = ""
            try:
                text = _gh("GET", f"/repos/{repo}/actions/jobs/{j['id']}/logs", raw=True).decode("utf-8", "replace")
                lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", ln) for ln in text.splitlines()]
                errs = [i for i, ln in enumerate(lines) if re.search(r"\berror\b|^e: |FAILED|Traceback", ln, re.I)]
                start = max(0, (errs[0] - 5) if errs else len(lines) - 60)
                log_tail = "\n".join(lines[start:start + 80])
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                log_tail = f"(couldn't read the log: {e})"
            detail.append({"job": j["name"], "failed_steps": steps, "log": log_tail[:8000]})
    return {"repo": repo, "runs": out, "latest_failure": detail}, f"Checked GitHub Actions for {repo}"


def _default_branch(repo):
    return _gh("GET", f"/repos/{repo}")["default_branch"]


def tool_github_commit(args):
    """Commits files to a branch (never the default branch) using the Git Data API. Needs the user's approval."""
    import base64
    if args.get("approved") is not True:
        raise ValueError("this needs the user's approval in the app first")
    if not github_config().get("token"):
        raise ValueError("committing needs a GitHub token with Contents write access (Settings → GitHub)")
    repo = _repo(args)
    branch = str(args.get("branch") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch:
        raise ValueError("give a branch name like 'stella/fix-build'")
    default = _default_branch(repo)
    if branch == default:
        raise ValueError(f"I don't commit straight to {default}; use a new branch and open a pull request")
    message = str(args.get("message") or "").strip() or "Update from Stella"
    files = args.get("files") or []
    deletes = [str(p).strip("/") for p in (args.get("delete") or [])]
    if not files and not deletes:
        raise ValueError("no files to commit")
    try:
        head = _gh("GET", f"/repos/{repo}/git/ref/heads/{_q(branch)}")["object"]["sha"]
        created = False
    except RuntimeError:
        head = _gh("GET", f"/repos/{repo}/git/ref/heads/{_q(default)}")["object"]["sha"]
        created = True
    base_tree = _gh("GET", f"/repos/{repo}/git/commits/{head}")["tree"]["sha"]
    tree = []
    for f in files:
        path = str(f.get("path", "")).strip("/")
        if not path or ".." in path.split("/"):
            raise ValueError(f"bad file path: {path!r}")
        blob = _gh("POST", f"/repos/{repo}/git/blobs",
                   {"content": base64.b64encode(str(f.get("content", "")).encode()).decode(), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    for path in deletes:
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    new_tree = _gh("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]
    commit = _gh("POST", f"/repos/{repo}/git/commits", {"message": message, "tree": new_tree, "parents": [head]})["sha"]
    if created:
        _gh("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    else:
        _gh("PATCH", f"/repos/{repo}/git/refs/heads/{_q(branch)}", {"sha": commit})
    url = f"https://github.com/{repo}/commit/{commit}"
    return {"repo": repo, "branch": branch, "commit": commit[:7], "url": url, "new_branch": created,
            "next": "Offer to open a pull request with github_pull_request."}, \
        f"Committed {len(files) + len(deletes)} file(s) to {repo}:{branch} ({commit[:7]})"


def tool_github_pull_request(args):
    if args.get("approved") is not True:
        raise ValueError("this needs the user's approval in the app first")
    repo = _repo(args)
    branch = str(args.get("branch") or "").strip()
    base = str(args.get("base") or "") or _default_branch(repo)
    pr = _gh("POST", f"/repos/{repo}/pulls", {"title": str(args.get("title") or "Changes from Stella")[:200],
                                               "head": branch, "base": base, "body": str(args.get("body") or "")})
    return {"number": pr["number"], "url": pr["html_url"]}, f"Opened pull request #{pr['number']}: {pr['html_url']}"


SERVER_TOOLS["github_repos"] = (tool_github_repos, _fn(
    "github_repos", "List the user's GitHub repositories.", {}, []))
SERVER_TOOLS["github_read"] = (tool_github_read, _fn(
    "github_read", "Read a file, or list a folder, in a GitHub repository.",
    {"repo": _p("owner/name (default: the user's default repo)"), "path": _p("File or folder path; empty for the root"),
     "ref": _p("Branch, tag or commit (optional)"), "start_line": _p("Line to start from for long files", "integer")},
    ["path"]))
SERVER_TOOLS["github_search"] = (tool_github_search, _fn(
    "github_search", "Search code in a GitHub repository (function names, strings, settings).",
    {"repo": _p("owner/name (optional)"), "query": _p("What to search for")}, ["query"]))
SERVER_TOOLS["github_history"] = (tool_github_history, _fn(
    "github_history", "Recent commits of a repository, optionally for one file.",
    {"repo": _p("owner/name (optional)"), "path": _p("File path (optional)"), "count": _p("How many (max 30)", "integer")},
    []))
SERVER_TOOLS["github_issues"] = (tool_github_issues, _fn(
    "github_issues", "Issues and pull requests of a repository.",
    {"repo": _p("owner/name (optional)"), "state": _p("open (default), closed or all")}, []))
SERVER_TOOLS["github_builds"] = (tool_github_builds, _fn(
    "github_builds",
    "Recent GitHub Actions builds of a repository, including the error log of the latest failed build. Use this when "
    "the user's build fails.",
    {"repo": _p("owner/name (optional)")}, []))
SERVER_TOOLS["github_commit"] = (tool_github_commit, _fn(
    "github_commit",
    "Commit file changes to a branch of a repository (never the default branch; a new branch is created if needed). "
    "The user must approve. Give complete file contents, not diffs; read the file first.",
    {"repo": _p("owner/name (optional)"), "branch": _p("Branch name, e.g. 'stella/fix-diagram-regex'"),
     "message": _p("Commit message"),
     "files": {"type": "array", "description": "Files to create or replace",
               "items": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                         "required": ["path", "content"]}},
     "delete": {"type": "array", "items": {"type": "string"}, "description": "Paths to delete (optional)"}},
    ["branch", "message", "files"]))
SERVER_TOOLS["github_pull_request"] = (tool_github_pull_request, _fn(
    "github_pull_request", "Open a pull request from a branch into the default branch. The user must approve.",
    {"repo": _p("owner/name (optional)"), "branch": _p("Branch with the changes"), "title": _p("Title"),
     "body": _p("Description of the changes")}, ["branch", "title"]))


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


# ---- Backups ----------------------------------------------------------------------------
BACKUP = {"dir": None, "config": None, "paths": {}, "last": 0, "error": "", "running": False}


def backup_config():
    try:
        with open(BACKUP["config"]) as f:
            c = json.load(f)
    except (OSError, ValueError, TypeError):
        c = {}
    return {"copy_to": str(c.get("copy_to") or ""), "keep": max(2, min(60, int(c.get("keep") or 14)))}


def save_backup_config(data):
    c = backup_config()
    if "copy_to" in data:
        path = os.path.expanduser(str(data.get("copy_to") or "").strip())
        if path and not os.path.isdir(path):
            raise ValueError(f"folder not found: {path}")
        c["copy_to"] = path
    if "keep" in data:
        c["keep"] = max(2, min(60, int(data["keep"])))
    with open(BACKUP["config"], "w") as f:
        json.dump(c, f)
    return backups_status()


def _backup_files(folder):
    try:
        return sorted(f for f in os.listdir(folder) if f.startswith("stellai-backup-") and f.endswith(".zip"))
    except OSError:
        return []


def backups_status():
    folder = BACKUP["dir"]
    items = []
    for f in reversed(_backup_files(folder)):
        st = os.stat(os.path.join(folder, f))
        items.append({"name": f, "size": st.st_size, "time": int(st.st_mtime * 1000)})
    cfg = backup_config()
    return {"folder": folder, "copy_to": cfg["copy_to"], "keep": cfg["keep"], "backups": items,
            "last": items[0]["time"] if items else 0, "error": BACKUP["error"], "running": BACKUP["running"]}


def backup_now():
    """Zips chats (consistent SQLite snapshot), memories, projects, photos, diagrams and wake words."""
    import zipfile
    if BACKUP["running"]:
        raise RuntimeError("a backup is already running")
    BACKUP["running"] = True
    try:
        folder = BACKUP["dir"]
        os.makedirs(folder, exist_ok=True)
        name = time.strftime("stellai-backup-%Y-%m-%d_%H%M.zip")
        dest = os.path.join(folder, name)
        with tempfile.TemporaryDirectory() as tmp:
            snap = os.path.join(tmp, "chats.db")
            dst = sqlite3.connect(snap)
            with CHATS.lock:
                CHATS.db.backup(dst)
            dst.close()
            with zipfile.ZipFile(dest + ".part", "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, "chats.db")
                for label, path in BACKUP["paths"].items():
                    if not path or not os.path.isdir(path):
                        continue
                    for root, _, files in os.walk(path):
                        for f in files:
                            full = os.path.join(root, f)
                            z.write(full, os.path.join(label, os.path.relpath(full, path)))
                # the avatar's settings, character and animations (not its downloads)
                for f in ["config.json", "animations.json"] + (
                        ["bin/characters/" + c["file"] for c in list_characters()] +
                        ["bin/animations/" + a["file"] for a in list_animations()] if AVATAR["dir"] else []):
                    if AVATAR["dir"] and os.path.isfile(_avatar_path(*f.split("/"))):
                        z.write(_avatar_path(*f.split("/")), "avatar/" + f)
                gh = github_config()
                gh.pop("token", None)   # never put the GitHub token in a backup
                z.writestr("github.json", json.dumps(gh))
                z.writestr("backup-info.json", json.dumps({
                    "made": time.strftime("%Y-%m-%d %H:%M"), "version": 1,
                    "chats": CHATS.db.execute("SELECT COUNT(*) FROM conversations WHERE deleted=0").fetchone()[0]}))
        os.replace(dest + ".part", dest)
        cfg = backup_config()
        for f in _backup_files(folder)[:-cfg["keep"]]:
            os.remove(os.path.join(folder, f))
        if cfg["copy_to"]:
            try:
                import shutil
                shutil.copy2(dest, os.path.join(cfg["copy_to"], name))
                for f in _backup_files(cfg["copy_to"])[:-cfg["keep"]]:
                    os.remove(os.path.join(cfg["copy_to"], f))
            except OSError as e:
                raise RuntimeError(f"backup saved here, but copying to {cfg['copy_to']} failed: {e}")
        BACKUP["error"] = ""
        log(f"backup saved: {name} ({os.path.getsize(dest) // 1024} KB)")
        return dest
    except Exception as e:
        BACKUP["error"] = str(e)
        raise
    finally:
        BACKUP["running"] = False


def backup_loop():
    """Nightly backup (between 2 and 5 am when Stella is idle; at the latest after 36 hours)."""
    time.sleep(120)
    while True:
        try:
            last = backups_status()["last"]
            age_h = (time.time() * 1000 - last) / 3600_000 if last else 999
            hour = time.localtime().tm_hour
            if age_h > 36 or (age_h > 20 and 2 <= hour < 5 and server_idle(300)):
                backup_now()
        except Exception as e:
            log(f"backup failed: {e}")
        time.sleep(1800)


def restore_backup(zip_path, cfg):
    """Puts a backup back (run while the server is stopped). The current data is kept as *.before-restore."""
    import shutil
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "chats.db" not in names:
            raise ValueError("that isn't a StellAI backup")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        had_db = os.path.exists(cfg.db)
        targets = {"projects": cfg.project_dir or os.path.join(os.path.dirname(os.path.abspath(cfg.db)), "projects"),
                   "images": cfg.image_dir, "diagrams": cfg.diagram_dir, "wakewords": cfg.wakeword_dir,
                   "avatar": getattr(cfg, "avatar_dir", "") or os.path.join(os.path.dirname(os.path.abspath(cfg.db)), "avatar")}
        if os.path.exists(cfg.db):
            os.replace(cfg.db, f"{cfg.db}.before-restore-{stamp}")
            for ext in ("-wal", "-shm"):
                if os.path.exists(cfg.db + ext):
                    os.remove(cfg.db + ext)
        with z.open("chats.db") as src, open(cfg.db, "wb") as out:
            shutil.copyfileobj(src, out)
        for label, target in targets.items():
            target = os.path.abspath(target)
            for n in names:
                if not n.startswith(label + "/") or n.endswith("/"):
                    continue
                rel = n[len(label) + 1:]
                dest = os.path.realpath(os.path.join(target, rel))
                if not dest.startswith(os.path.realpath(target) + os.sep):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    return f"restored {os.path.basename(zip_path)}" + (
        f"; the previous chat database was kept as {cfg.db}.before-restore-{stamp}" if had_db else "")


# ---- Server status and switching models ---------------------------------------------
try:      # the model catalog shared with the setup (stellai_tune.py sits next to this file)
    import stellai_tune as TUNE
except ImportError:
    TUNE = None

MODEL_CATALOG = [
    {"key": "4b", "label": "Qwen3.5 4B", "repo": "unsloth/Qwen3.5-4B-GGUF", "about": "Fast; fine for everyday help."},
    {"key": "9b", "label": "Qwen3.5 9B", "repo": "unsloth/Qwen3.5-9B-GGUF", "about": "Smarter at code and reasoning."},
    {"key": "35b", "label": "Qwen3.5 35B-A3B", "repo": "unsloth/Qwen3.5-35B-A3B-GGUF",
     "about": "The strongest; needs about 20 GB RAM plus a GPU."},
]


def detect_memory():
    """(ram_gb, vram_gb) of this computer."""
    ram = 0
    try:
        if os.name == "nt":
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("x", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in "abcdefg"]
            m = M()
            m.l = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            ram = round(m.a / 2**30)
        else:
            with open("/proc/meminfo") as f:
                ram = round(int(re.search(r"MemTotal:\s+(\d+)", f.read()).group(1)) / 2**20)
    except (OSError, AttributeError, ValueError):
        pass
    vram = 0
    try:
        import shutil
        smi = shutil.which("nvidia-smi")
        if smi:
            out = subprocess.run([smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"], capture_output=True,
                                 text=True, timeout=10, creationflags=0x08000000 if os.name == "nt" else 0).stdout
            vram = round(int(out.split()[0]) / 1024) if out.strip() else 0
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return ram, vram


# ---- How long a conversation the model is started with (its context window, in tokens)
# Qwen3.5 only keeps a per-token cache in one layer out of four, so long contexts are cheap: the 9B measures
# about 17 MB per 1,000 tokens with the 8-bit cache StellAI uses. The planning figures below add working
# buffers on top. The 4B and 35B figures are estimates on the same basis, not measurements.
CTX_TARGET = {"4b": 32768, "9b": 65536, "35b": 65536}
CTX_GB_PER_1K = {"4b": 0.02, "9b": 0.02, "35b": 0.03}


def context_for(key, model_gb, ram, vram, cores):
    """The biggest context that keeps the model where it runs fastest on this computer.

    - No usable graphics card: memory isn't the limit, re-reading is (a CPU reads a few dozen tokens a second,
      so a long chat would take many minutes to load after a restart). Short contexts stay.
    - 4B / 9B on a graphics card: model + context must fit on the card, or part of the model falls back to the
      CPU and every reply gets slower.
    - 35B: its always-used core (about 2.5 GB) and the context sit on the card, the experts in RAM."""
    if vram < 4:
        if key == "35b":
            return 16384
        return 16384 if cores >= 6 and ram >= 12 else 8192
    floor = (32768 if vram >= 4.5 else 16384) if key == "35b" else 8192
    gpu = vram - (1.0 if vram < 8 else 2.0)        # what --fit leaves free (voice, and vision on 8 GB+ cards)
    ctx = CTX_TARGET.get(key, 8192)
    while ctx > floor:
        kv = ctx / 1000 * CTX_GB_PER_1K.get(key, 0.03)
        if key == "35b":
            fits = kv + 2.5 <= gpu and model_gb + kv + 4 <= ram + vram
        else:
            fits = model_gb + kv <= gpu
        if fits:
            break
        ctx //= 2
    return max(ctx, floor)


def physical_cores():
    """Real processor cores (not threads), as the setup counts them."""
    try:
        with open("/proc/cpuinfo") as f:
            text = f.read()
        pairs = set(zip(re.findall(r"^physical id\s*:\s*(\d+)", text, re.M), re.findall(r"^core id\s*:\s*(\d+)", text, re.M)))
        if pairs:
            return len(pairs)
    except OSError:
        pass
    return max(1, (os.cpu_count() or 2) // 2)


def model_file_for(key, ram, vram):
    """Same choices as the setup: the best version that fits this computer, and its context window."""
    if key == "4b":
        name, gb = "Qwen3.5-4B-Q4_K_M.gguf", 2.7
    elif key == "9b":
        name, gb = ("Qwen3.5-9B-Q3_K_M.gguf", 4.6) if 5 <= vram < 8 else ("Qwen3.5-9B-Q4_K_M.gguf", 5.7)
    elif ram + vram >= 40:
        name, gb = "Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf", 22.2
    elif ram + vram >= 30:
        name, gb = "Qwen3.5-35B-A3B-UD-IQ4_XS.gguf", 17.5
    else:
        name, gb = "Qwen3.5-35B-A3B-UD-IQ3_S.gguf", 13.6
    return name, gb, context_for(key, gb, ram, vram, physical_cores())


class LinuxServerControl:
    """Model switching for the Linux launcher: it re-reads model.conf whenever llama-server stops and a
    .switch-model file is present."""

    def __init__(self, conf, models_dir, pid_file):
        self.conf, self.models_dir, self.pid_file = conf, models_dir, pid_file
        self.download = {}   # key -> {"done", "total", "error", "state"}
        self.started = time.time()

    def _read_conf(self):
        out = {}
        try:
            with open(self.conf) as f:
                for line in f:
                    m = re.match(r'\s*([A-Z_]+)="?(.*?)"?\s*$', line)
                    if m:
                        out[m.group(1)] = m.group(2)
        except OSError:
            pass
        return out

    def _tune_hw(self, conf):
        hw = TUNE.detect(self.models_dir)
        budget = float(conf.get("RAM_BUDGET_GB") or 0) or max(4.0, hw["ram_gb"] - 4)
        ids = [] if conf.get("LLAMA_GPUS") == "-1" else \
            [int(x) for x in conf.get("LLAMA_GPUS", "").split(",") if x.strip().isdigit()] or None
        return hw, budget, ids

    def catalog(self):
        ram, vram = detect_memory()
        conf = self._read_conf()
        current = os.path.basename(conf.get("MODEL_FILE", ""))
        out = []
        if TUNE:
            try:
                hw, budget, ids = self._tune_hw(conf)
                out = TUNE.library(hw, budget, self.models_dir, current, ids, self.download)
            except Exception as e:      # never lose the model list over the catalog
                log(f"model catalog: {e}")
                out = []
        for m in MODEL_CATALOG:
            fname, gb, ctx = model_file_for(m["key"], ram, vram)
            path = os.path.join(self.models_dir, fname)
            have = os.path.isfile(path) and os.path.getsize(path) > 100_000_000
            if out and not have:
                continue        # with the full catalog, the old three only show when they're downloaded
            out.append({**{k: m[k] for k in ("key", "label", "about")}, "file": fname, "size_gb": gb, "context": ctx,
                        "installed": have, "current": fname == current, "download": self.download.get(m["key"])})
        return out

    def status(self):
        conf = self._read_conf()
        return {"model": conf.get("MODEL_LABEL", ""), "file": os.path.basename(conf.get("MODEL_FILE", "")),
                "backend": conf.get("BACKEND", ""), "can_switch": True}

    def switch(self, key):
        entry = next((m for m in self.catalog() if m["key"] == key), None)
        if not entry:
            raise ValueError("unknown model")
        if not entry["installed"]:
            raise ValueError("download it first")
        conf = self._read_conf()
        mmproj, spec = "", ""
        man = TUNE.read_manifest(self.models_dir).get(key, {}) if TUNE and "|" in key else {}
        if conf.get("MMPROJ_FILE") or conf.get("VISION", "") == "1":
            cand = os.path.join(self.models_dir, man["mmproj"]) if man.get("mmproj") else \
                os.path.join(self.models_dir, entry["file"].replace(".gguf", ".mmproj-F16.gguf"))
            mmproj = cand if os.path.isfile(cand) else ""
        if (man.get("tuned") or {}).get("mtp"):
            spec = " ".join(TUNE.MTP_ARGS)     # the setup's speed test showed the MTP speed-up helps here
        ctx = (man.get("tuned") or {}).get("context") or entry["context"]
        conf.update(MODEL_LABEL=entry["label"], MODEL_FILE=os.path.join(self.models_dir, entry["file"]),
                    NET_CONTEXT=str(ctx), MMPROJ_FILE=mmproj, SPEC_ARGS=spec)
        with open(self.conf + ".tmp", "w") as f:
            for k, v in conf.items():
                f.write(f'{k}="{v}"\n')
        os.replace(self.conf + ".tmp", self.conf)
        open(os.path.join(os.path.dirname(self.conf), ".switch-model"), "w").close()
        try:
            with open(self.pid_file) as f:
                os.kill(int(f.read().strip()), 15)
        except (OSError, ValueError):
            raise RuntimeError("couldn't restart the model server; restart StellAI Server to use the new model")
        return f"switching to {entry['label']}; it takes a minute to load"

    def start_download(self, key):
        entry = next((m for m in self.catalog() if m["key"] == key), None)
        if not entry:
            raise ValueError("unknown model")
        state = {"state": "downloading", "done": 0, "total": 0, "error": ""}
        self.download[key] = state
        if TUNE and "|" in key:
            vision = bool(self._read_conf().get("MMPROJ_FILE"))

            def work_tuned():
                try:
                    TUNE.download_model(key, self.models_dir, state, _download_file, vision=vision)
                    state["state"] = "ready"
                except Exception as e:
                    state.update(state="failed", error=str(e))
            threading.Thread(target=work_tuned, daemon=True).start()
            return "download started"
        repo = next(m["repo"] for m in MODEL_CATALOG if m["key"] == key)

        def work():
            files = [(f"https://huggingface.co/{repo}/resolve/main/{entry['file']}", entry["file"])]
            if self._read_conf().get("MMPROJ_FILE"):
                files.append((f"https://huggingface.co/{repo}/resolve/main/mmproj-F16.gguf",
                              entry["file"].replace(".gguf", ".mmproj-F16.gguf")))
            try:
                for url, name in files:
                    _download_file(url, os.path.join(self.models_dir, name), state)
                state["state"] = "ready"
            except Exception as e:
                state.update(state="failed", error=str(e))
        threading.Thread(target=work, daemon=True).start()
        return "download started"


def _download_file(url, dest, state):
    """Resumable download with progress in `state` (used for model downloads from the app)."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "StellAI", **({"Range": f"bytes={have}-"} if have else {})})
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.URLError as e:
        # A Windows PC that hasn't fetched this site's root certificate yet: let Windows download it instead
        if os.name == "nt" and _is_cert_error(e):
            log(f"download: certificate check failed for {urlparse(url).netloc}, using Windows to download it")
            if os.path.exists(part):
                os.remove(part)
            _windows_download(url, part)
            os.replace(part, dest)
            return
        raise
    with r:
        if have and r.status != 206:
            have = 0
        state["total"] = have + int(r.headers.get("Content-Length") or 0)
        with open(part, "ab" if have else "wb") as f:
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                have += len(buf)
                state["done"] = have
    os.replace(part, dest)


SERVER_CONTROL = None   # LinuxServerControl, or the Windows app's own controller
STARTED_AT = time.time()


def server_status():
    st = {"uptime_s": int(time.time() - STARTED_AT), "model": "", "context": 0, "loaded": False, "can_switch": False,
          "models": []}
    try:
        props = _llama("/props", timeout=5)
        st["context"] = int((props.get("default_generation_settings") or {}).get("n_ctx") or 0)
        st["model_path"] = os.path.basename(str(props.get("model_path") or ""))
        st["loaded"] = True
        st["sleeping"] = bool(props.get("is_sleeping"))   # unloaded after a quiet spell; the next message wakes it
    except (urllib.error.URLError, OSError, ValueError):
        pass
    if SERVER_CONTROL:
        try:
            st.update(SERVER_CONTROL.status())
            st["models"] = SERVER_CONTROL.catalog()
        except Exception as e:
            st["error"] = str(e)
    ram, vram = detect_memory()
    st.update(ram_gb=ram, vram_gb=vram)
    return st


# ---- Speed test: real numbers for this computer ---------------------------------------------
SPEED_FILE = None     # JSON list of past results, set in start()
_SPEED_LOCK = threading.Lock()
_SPEED_WORDS = ("the server reads this passage to measure how quickly it takes in text before it starts to answer "
                "stars planets orbit circuit resistor voltage current signal memory window garden river mountain "
                "morning coffee letter number station bridge engine market school music colour paper").split()


def _speed_prompt(n_words=1100):
    import random
    rnd = random.Random(time.time_ns())        # different every run, so nothing is answered from the cache
    return f"Passage {rnd.randrange(10**9)}. " + " ".join(rnd.choice(_SPEED_WORDS) for _ in range(n_words)) + "\n\nSummary:"


def _resample_wav(wav, rate=16000):
    """Any mono/stereo 16-bit WAV -> 16 kHz mono (what Whisper expects)."""
    import array
    with wave.open(io.BytesIO(wav)) as w:
        src_rate, ch, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("16-bit audio only")
    a = array.array("h")
    a.frombytes(frames[:len(frames) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    if ch > 1:
        a = a[::ch]
    if src_rate != rate and len(a) > 1:
        n = int(len(a) * rate / src_rate)
        step = (len(a) - 1) / max(1, n - 1)
        out = array.array("h", bytes(2 * n))
        for i in range(n):
            x = i * step
            j = int(x)
            f = x - j
            out[i] = int(a[j] * (1 - f) + a[min(j + 1, len(a) - 1)] * f)
        a = out
    if sys.byteorder == "big":
        a.byteswap()
    return pcm_to_wav(a.tobytes(), rate), len(a) / rate


def _speed_history():
    try:
        with open(SPEED_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
        return []


def run_speed_test(whisper_url="", language="en"):
    """Measures this server as it is set up now: how fast the model reads and writes, how long it takes to wake,
    and how long hearing and speaking take. Takes 10-60 s and uses the model, so it only runs when Stella is idle."""
    if not _SPEED_LOCK.acquire(blocking=False):
        return {"error": "A speed test is already running."}
    try:
        if llama_busy():
            return {"error": "Stella is answering someone right now. Try again when she's idle."}
        started = time.time()
        out = {"at": int(started * 1000), "model": "", "results": []}

        def add(key, name, value, unit, detail=""):
            out["results"].append({"key": key, "name": name, "value": round(value, 2), "unit": unit, "detail": detail})

        # -- the model: wake-up, reading, writing
        try:
            props = _llama("/props", timeout=10)
            out["model"] = os.path.basename(str(props.get("model_path") or ""))
            asleep = bool(props.get("is_sleeping"))
            t0 = time.time()
            r = _llama("/completion", {"prompt": _speed_prompt(), "n_predict": 96, "cache_prompt": False,
                                       "ignore_eos": True, "temperature": 0.7, "stream": False}, timeout=600)
            total = time.time() - t0
            tm = r.get("timings") or {}
            pn, ps = int(tm.get("prompt_n") or 0), float(tm.get("prompt_per_second") or 0)
            gn, gs = int(tm.get("predicted_n") or 0), float(tm.get("predicted_per_second") or 0)
            if asleep:
                work = (float(tm.get("prompt_ms") or 0) + float(tm.get("predicted_ms") or 0)) / 1000
                add("wake", "Waking up", max(0.0, total - work), "s", "from asleep to ready (the model had been unloaded)")
            if ps:
                add("read", "Reading", ps, "tokens/s", f"{pn} tokens read; a full page of text takes about {700 / ps:.1f} s")
            if gs:
                add("write", "Writing", gs, "tokens/s", f"about {gs * 0.75:.0f} words a second; a 150-word answer takes about {200 / gs:.0f} s")
            if not ps and not gs:
                out["results"].append({"key": "model", "name": "AI model", "error": "it answered without timing figures"})
        except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
            out["results"].append({"key": "model", "name": "AI model", "error": f"not reachable ({e})"})

        # -- speaking, then hearing that same sentence back (so the transcript can be checked too)
        sentence = "Testing, one two three. The quick brown fox jumps over the lazy dog."
        spoken = None
        try:
            ac = avatar_config()
            engine = "Piper"
            t0 = time.time()
            if ac["engine"] == "kokoro" and KOKORO.ready():
                spoken, engine = KOKORO.say(sentence, ac["kokoro_voice"], 1.0), "Kokoro"
            elif os.path.isfile(_avatar_path("voices", ac["voice"] + ".onnx")):
                TTS.say("Ready.", ac["voice"], 1.0)          # loads the voice first, as in normal use
                t0 = time.time()
                spoken = TTS.say(sentence, ac["voice"], 1.0)
            if spoken:
                took = time.time() - t0
                _, seconds = _resample_wav(spoken)
                add("speak", "Speaking", took, "s", f"{engine} made {seconds:.1f} s of speech"
                    + (f" ({seconds / took:.0f}x faster than real time)" if took > 0 else ""))
        except Exception as e:   # noqa: BLE001 - one part failing shouldn't lose the other figures
            out["results"].append({"key": "speak", "name": "Speaking", "error": str(e)[:200]})
        if whisper_url:
            try:
                if spoken:
                    wav, seconds = _resample_wav(spoken)
                else:   # no voice installed: time Whisper on a tone of the same length
                    import array
                    seconds = 4.0
                    wav = pcm_to_wav(array.array("h", [int(3000 * math.sin(i / 9)) for i in range(64000)]).tobytes(), 16000)
                t0 = time.time()
                heard = clean_transcript(transcribe(whisper_url, wav, language))
                took = time.time() - t0
                ok = not spoken or len(set(_terms(heard)) & {"quick", "brown", "fox", "lazy", "dog", "testing"}) >= 3
                add("hear", "Hearing", took, "s", f"for {seconds:.1f} s of speech"
                    + (f'; heard: "{heard[:80]}"' if spoken else "") + ("" if ok else " (that's not what was said)"))
            except Exception as e:   # noqa: BLE001
                out["results"].append({"key": "hear", "name": "Hearing", "error": str(e)[:200]})

        if EMBEDDER.url:
            try:
                t0 = time.time()
                EMBEDDER.embed([" ".join(_SPEED_WORDS * 4)] * 8, EMBEDDER.DOC)
                add("search", "File search", (time.time() - t0) / 8 * 1000, "ms", "per page indexed")
            except Exception as e:   # noqa: BLE001
                out["results"].append({"key": "search", "name": "File search", "error": str(e)[:200]})

        out["took_s"] = round(time.time() - started, 1)
        # compare with the last run on the same model, then remember this one (keeps 30)
        hist = _speed_history()
        prev = next((h for h in reversed(hist) if h.get("model") == out["model"]), None)
        if prev:
            old = {x.get("key"): x.get("value") for x in prev.get("results", []) if "value" in x}
            for x in out["results"]:
                if x.get("value") and old.get(x["key"]):
                    x["change_pct"] = round((x["value"] - old[x["key"]]) / old[x["key"]] * 100)
            out["compared_to"] = prev.get("at")
        if SPEED_FILE and any("value" in x for x in out["results"]):
            try:
                with open(SPEED_FILE + ".tmp", "w") as f:
                    json.dump((hist + [out])[-30:], f)
                os.replace(SPEED_FILE + ".tmp", SPEED_FILE)
            except OSError:
                pass
        log("speed test: " + ", ".join(f"{x['name'].lower()} {x['value']} {x['unit']}" for x in out["results"] if "value" in x))
        return out
    finally:
        _SPEED_LOCK.release()


def speed_text(report):
    if report.get("error"):
        return report["error"]
    lines = ["StellAI speed test" + (f" — {report['model']}" if report.get("model") else ""), ""]
    for x in report.get("results", []):
        if "error" in x:
            lines.append(f"  ✗ {x['name']}: {x['error']}")
            continue
        better_when_higher = x["unit"].endswith("/s")
        ch = x.get("change_pct")
        change = ""
        if ch:
            change = f"  ({'+' if ch > 0 else ''}{ch}% vs last time: {'faster' if (ch > 0) == better_when_higher else 'slower'})"
        lines.append(f"  {x['name']}: {x['value']:g} {x['unit']}{change}")
        if x.get("detail"):
            lines.append(f"      {x['detail']}")
    lines.append("")
    lines.append(f"Took {report.get('took_s', 0):g} s.")
    return "\n".join(lines)


# ---- Health check ("doctor") --------------------------------------------------------------
DOCTOR = {"whisper": "", "embed": "", "update": ""}


def _probe(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError) as e:
        return 0, str(getattr(e, "reason", e))


def gpu_driver_check():
    """(state, detail, fix) for NVIDIA cards, or None without any. Catches the classic Proxmox container problem:
    the host's driver was updated and the container's driver programs no longer match it."""
    import shutil
    smi = shutil.which("nvidia-smi")
    if not smi and not os.path.exists("/dev/nvidiactl"):
        return None
    if not smi:
        return ("fail", "the cards are there but nvidia-smi isn't installed",
                "Run stellai-setup.sh again: in a Proxmox container it installs the host's driver version.")
    try:
        p = subprocess.run([smi, "--query-gpu=name,driver_version", "--format=csv,noheader"], capture_output=True,
                           text=True, timeout=20, creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as e:
        return ("warn", f"nvidia-smi didn't answer ({e})", "")
    text = (p.stdout or "") + (p.stderr or "")
    if "mismatch" in text.lower():
        return ("fail", "driver/library version mismatch: Stella's model is running on the processor",
                "The NVIDIA driver on the Proxmox host changed. Run stellai-setup.sh again in the container (it "
                "installs the matching version), then restart StellAI. Or reboot the host if it was just updated.")
    if p.returncode != 0:
        return ("warn", "nvidia-smi failed: " + " ".join(text.split())[:160], "Check the graphics driver.")
    rows = [r.split(",") for r in p.stdout.strip().splitlines() if r.strip()]
    return ("ok", f"{len(rows)} card(s), driver {rows[0][1].strip() if rows and len(rows[0]) > 1 else '?'}", "")


def run_doctor():
    """Checks every part of StellAI and says what to do about problems."""
    import shutil
    checks = []

    def add(name, state, detail, fix=""):
        checks.append({"name": name, "state": state, "detail": detail, "fix": fix})

    code, err = _probe(LLAMA_URL.rstrip("/") + "/health")
    if code == 200:
        try:
            props = _llama("/props", timeout=5)
            n = (props.get("default_generation_settings") or {}).get("n_ctx")
            if props.get("is_sleeping"):
                add("AI model", "ok", "asleep to free memory; it wakes up on the next message (a few seconds)")
            else:
                add("AI model", "ok", f"{os.path.basename(str(props.get('model_path') or 'loaded'))}, {n} tokens of context")
        except (urllib.error.URLError, OSError, ValueError):
            add("AI model", "ok", "running")
    elif code == 503:
        add("AI model", "warn", "still loading", "Wait a minute; big models take a while to load.")
    else:
        add("AI model", "fail", f"not reachable ({err or code})", "Start StellAI Server; if it stops right away, check llama.log / the Activity box.")
    gpu = gpu_driver_check()
    if gpu:
        add("Graphics cards", *gpu)
    if DOCTOR["whisper"]:
        code, err = _probe(DOCTOR["whisper"].rstrip("/") + "/health")
        add("Voice recognition", "ok" if code in (200, 404) else "fail",
            "running" if code in (200, 404) else f"not reachable ({err or code})",
            "" if code in (200, 404) else "Voice commands won't work. Re-run setup to install Whisper.")
    if HELPER["url"]:
        code, err = _probe(HELPER["url"].rstrip("/") + "/health")
        add("Helper model", "ok" if code == 200 else "warn",
            "running (memory learning and mode checks)" if code == 200 else "not running; the main model does its jobs",
            "" if code == 200 else "Check helper.log; re-run setup to download the helper model again.")
    if EMBEDDER.url:
        code, err = _probe(EMBEDDER.url + "/health")
        add("Meaning search", "ok" if code == 200 else "warn", "running" if code == 200 else "not running; keyword search only",
            "" if code == 200 else "Re-run setup to download the search model.")
    else:
        add("Meaning search", "warn", "not set up; keyword search only", "Re-run setup (it downloads a 140 MB search model).")
    if SEARXNG_URL:
        code, err = _probe(SEARXNG_URL.rstrip("/") + "/healthz")
        add("Web search", "ok" if code == 200 else "warn", "SearXNG running" if code == 200 else "SearXNG not answering; using DuckDuckGo",
            "" if code == 200 else "Start Docker (or re-run the SearXNG step of setup).")
    else:
        add("Web search", "ok", "DuckDuckGo (SearXNG not set up)")
    try:
        with CHATS.lock:
            ok = CHATS.db.execute("PRAGMA quick_check").fetchone()[0]
            n = CHATS.db.execute("SELECT COUNT(*) FROM conversations WHERE deleted=0").fetchone()[0]
            mem = CHATS.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            pend = CHATS.db.execute("SELECT COUNT(*) FROM chat_dirty").fetchone()[0]
        add("Chat database", "ok" if ok == "ok" else "fail", f"{n} chats, {mem} memories" + (f", {pend} chats waiting to be indexed" if pend else ""),
            "" if ok == "ok" else "Restore yesterday's backup (see Backups).")
    except sqlite3.Error as e:
        add("Chat database", "fail", str(e), "Restore a backup.")
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(CHATS_PATH))).free / 2**30
    add("Disk space", "ok" if free > 10 else "warn" if free > 3 else "fail", f"{free:.1f} GB free",
        "" if free > 10 else "Free up space; backups and downloads need room.")
    st = backups_status()
    age = (time.time() * 1000 - st["last"]) / 86400_000 if st["last"] else None
    if st["error"]:
        add("Backups", "fail", st["error"], "Check the backup folder in Server → Backups.")
    elif age is None:
        add("Backups", "warn", "none yet", "One is made automatically tonight; or tap Back up now.")
    else:
        add("Backups", "ok" if age < 2 else "warn", f"last {age * 24:.0f} hours ago, {len(st['backups'])} kept" +
            (f", copied to {st['copy_to']}" if st["copy_to"] else ""), "" if age < 2 else "Tap Back up now.")
    try:
        import pypdf  # noqa: F401
        add("PDF reading", "ok", "pypdf")
    except ImportError:
        add("PDF reading", "ok" if shutil.which("pdftotext") else "warn",
            "pdftotext" if shutil.which("pdftotext") else "no PDF reader", "" if shutil.which("pdftotext") else "pip install pypdf")
    add("Mermaid diagrams", "ok" if mermaid_command() else "warn", "installed" if mermaid_command() else "not installed (flowcharts use schemdraw)")
    gh = github_status()
    add("GitHub", "ok" if gh.get("user") else "warn" if gh.get("token_set") else "ok",
        f"connected as {gh['user']}" if gh.get("user") else gh.get("error") or "no token (public repos only)",
        "Check the token in Settings → GitHub." if gh.get("token_set") and not gh.get("user") else "")
    ts = shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe" if os.name == "nt" else "")
    ip = ""
    if ts and os.path.exists(ts):
        try:
            ip = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=5,
                                creationflags=0x08000000 if os.name == "nt" else 0).stdout.strip().splitlines()[0]
        except (OSError, IndexError, subprocess.SubprocessError):
            ip = ""
    add("Tailscale", "ok" if ip else "warn", ip or "not connected", "" if ip else "Install/sign in to use Stella away from home.")
    if AVATAR["dir"]:
        st = AVATAR["status"].get("voice", "")
        voice = avatar_config()["voice"]
        kst = AVATAR["status"].get("kokoro", "")
        if avatar_config().get("engine") == "kokoro" and kst.startswith("failed"):
            add("Avatar voice", "warn", "Kokoro " + kst + " (using Piper)", "Check the internet connection, then pick Kokoro again.")
        elif piper_exe() and os.path.isfile(_avatar_path("voices", voice + ".onnx")):
            add("Avatar voice", "ok", "ready: " + (("Kokoro " + (kst or "preparing")) if avatar_config().get("engine") == "kokoro"
                                                  else VOICES.get(voice, voice).split(":")[0]))
        elif st.startswith("failed"):
            add("Avatar voice", "warn", st, "Check the internet connection, then open the avatar screen again.")
        elif st:
            add("Avatar voice", "ok", st + "…")
        else:
            add("Avatar voice", "ok", "not set up yet: downloads (~90 MB) the first time you open the avatar screen")
    if DOCTOR["update"]:
        add("Updates", "ok", DOCTOR["update"])
    worst = "fail" if any(c["state"] == "fail" for c in checks) else "warn" if any(c["state"] == "warn" for c in checks) else "ok"
    return {"state": worst, "checks": checks, "time": int(time.time() * 1000)}


def doctor_text(report):
    icon = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = ["StellAI health check — " + {"ok": "all good", "warn": "working, with warnings", "fail": "problems found"}[report["state"]], ""]
    for c in report["checks"]:
        lines.append(f"  {icon[c['state']]} {c['name']}: {c['detail']}")
        if c["fix"] and c["state"] != "ok":
            lines.append(f"      → {c['fix']}")
    return "\n".join(lines) + "\n"


IMAGE_DIR = None
WAKEWORD_DIR = None
WAKEWORD_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,59}")


def shrink_result(result, max_chars):
    """Shortens the longest text fields of a tool result until it fits in max_chars (the space left in context)."""
    max_chars = max(1200, max_chars)
    for _ in range(300):
        size = len(json.dumps(result))
        if size <= max_chars:
            break
        strings = []

        def walk(o, path):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, path + [k])
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, path + [i])
            elif isinstance(o, str) and len(o) > 200:
                strings.append((len(o), path))
        walk(result, [])
        if not strings:
            # drop list items from the end (e.g. too many search results)
            lists = [v for v in result.values() if isinstance(v, list) and len(v) > 1]
            if not lists:
                break
            max(lists, key=len).pop()
            continue
        length, path = max(strings)
        target = max(200, length - (size - max_chars) - 40)
        o = result
        for k in path[:-1]:
            o = o[k]
        o[path[-1]] = o[path[-1]][:target] + " […shortened to fit Stella's memory]"
        result["truncated"] = True
    return result


# ---- Instant answers --------------------------------------------------------------
# Simple, unambiguous requests ("what time is it", "add milk to the shopping list", "remind me to call Sam in
# 20 minutes") are answered here without waking the model: instant on any hardware. Anything not matched
# exactly returns None and goes to the model as usual, so this must stay strict rather than clever.
_Q_LEAD = re.compile(r"^(?:(?:hey|ok|okay|hi|hello)[\s,]+)?(?:stella[\s,!.]+)?(?:(?:can|could|would|will) you\s+)?"
                     r"(?:please\s+)?", re.I)
_Q_TAIL = re.compile(r"(?:[\s,]+(?:please|thanks|thank you|for me|stella))*[\s.!?]*$", re.I)
_Q_TIME = re.compile(r"(?:what(?:'s| is)? (?:the )?time(?: is it)?|what time is it|tell me the time)(?: right now| now)?", re.I)
_Q_DATE = re.compile(r"what(?:'s| is) (?:the |today'?s )?date(?: today)?|what day is it(?: today)?|"
                     r"what day of the week is it(?: today)?|what(?:'s| is) today", re.I)
_Q_MATH_LEAD = re.compile(r"^(?:what(?:'s| is)|calculate|compute|how much is|work out)\s+", re.I)
_Q_MATH_WORDS = [(re.compile(p, re.I), r) for p, r in [
    (r"\s+plus\s+", " + "), (r"\s+minus\s+", " - "), (r"\s+(?:times|multiplied by)\s+", " * "),
    (r"\s+(?:divided by|over)\s+", " / "), (r"\s+to the power of\s+", " ^ "), (r"(?<=\d)\s*[x×]\s*(?=\d)", " * "),
    (r"(?<=\d),(?=\d{3}\b)", "")]]
_Q_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(\d+(?:\.\d+)?)", re.I)
_Q_ADD = re.compile(r"(?:add|put)\s+(.+?)\s+(?:to|on|onto)\s+(?:my|the|our)\s+(.+?)\s+list", re.I)
_Q_SHOW = re.compile(r"(?:what(?:'s| is) on|show(?: me)?|read(?: me)?|list)\s+(?:my|the|our)\s+(.+?)\s+list", re.I)
_Q_REMINDERS = re.compile(r"(?:what are|show(?: me)?|list|read(?: me)?)\s+(?:my|the)\s+reminders|"
                          r"what reminders do i have|do i have any reminders", re.I)
_Q_REMIND_IN = re.compile(r"remind me\s+(?:to\s+|about\s+)?(.+?)\s+in\s+(\d+|an?|half an)\s*(min(?:ute)?s?|h(?:ou)?rs?)", re.I)
_Q_REMIND_IN2 = re.compile(r"remind me\s+in\s+(\d+|an?|half an)\s*(min(?:ute)?s?|h(?:ou)?rs?)\s+(?:to\s+|about\s+)?(.+)", re.I)
_Q_REMIND_AT = re.compile(r"remind me\s+(?:to\s+|about\s+)?(.+?)\s+(?:(today|tomorrow)\s+)?at\s+"
                          r"(\d{1,2})(?::(\d\d))?\s*(a\.?m\.?|p\.?m\.?)?(?:\s+(today|tomorrow))?", re.I)
_Q_NUM = r"(\d+(?:\.5)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty five|forty-five|sixty|ninety|half an?)"
_Q_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"
_Q_TIMER = re.compile(r"(?:set|start|begin|make)\s+(?:a|an|the|me a)\s+(?:(.+?)\s+)?timer\s+(?:for|of)\s+" + _Q_NUM + r"\s*" + _Q_UNIT +
                      r"(?:\s+(?:and\s+)?" + _Q_NUM + r"\s*" + _Q_UNIT + r")?|"
                      r"(?:set|start)\s+(?:a|an)\s+" + _Q_NUM + r"[\s-]*" + _Q_UNIT + r"\s+timer|"
                      r"timer\s+(?:for\s+)?" + _Q_NUM + r"\s*" + _Q_UNIT, re.I)
_Q_ALARM = re.compile(r"(?:set|make|create)\s+(?:an?|the|my)\s+alarm\s+(?:for|at)\s+(\d{1,2})(?::(\d\d))?\s*(a\.?m\.?|p\.?m\.?)?"
                      r"(?:\s+(?:tomorrow|tomorrow morning|in the morning|tonight))?|"
                      r"wake me(?: up)?\s+at\s+(\d{1,2})(?::(\d\d))?\s*(a\.?m\.?|p\.?m\.?)?(?:\s+(?:tomorrow|tomorrow morning|in the morning))?", re.I)
_Q_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45, "forty-five": 45, "sixty": 60, "ninety": 90}
_Q_VAGUE = re.compile(r"\b(it|that|this|those|these|them|same|again|another)\b", re.I)


def _quick_now(now_text):
    from datetime import datetime
    if now_text:
        try:
            return datetime.fromisoformat(str(now_text).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now().astimezone()


def _clock(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def _plain_number(v):
    if abs(v - round(v)) < 1e-9 and abs(v) < 1e15:
        return f"{int(round(v)):,}"
    return f"{v:,.6f}".rstrip("0").rstrip(".") if abs(v) >= 1e-4 else f"{v:.6g}"


def _quick_maths(body, asked):
    """asked: the message began with "what is" / "calculate". Without that, only something that can't be a date,
    phone number or part number counts ("12 * 7", "(2+3)^2", "10 - 3" with spaces)."""
    m = _Q_PERCENT.fullmatch(body)
    if m:
        return f"{m.group(1)}% of {m.group(2)} is {_plain_number(float(m.group(1)) * float(m.group(2)) / 100)}."
    expr = body
    for rx, rep in _Q_MATH_WORDS:
        expr = rx.sub(rep, expr)
    expr = expr.strip().rstrip("=").strip()
    # digits and operators only (no names, units or functions: those go to the model and its calculator tool)
    if not re.fullmatch(r"[\d\s.+\-*/^()]+", expr) or not re.search(r"\d\s*[+\-*/^]\s*[\d(.-]", expr):
        return None
    if not asked and not re.search(r"[+*^(]|\d\s+[-/]\s+\d", expr):
        return None
    try:
        value = safe_eval(expr)
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError):
        return None
    shown = " ".join(expr.replace("*", "×").replace("/", "÷").split())
    return f"{shown} = {_plain_number(value)}"


def _q_seconds(amount, unit):
    a = amount.lower()
    n = 0.5 if a.startswith("half") else _Q_WORDNUM[a] if a in _Q_WORDNUM else float(a)
    u = unit.lower()
    return n * (3600 if u.startswith("h") else 60 if u.startswith("m") else 1)


def _q_duration(seconds):
    parts = []
    for size, name in ((3600, "hour"), (60, "minute"), (1, "second")):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return " and ".join(parts) or "0 seconds"


def _quick_phone(body):
    """Timers and alarms run on the phone that asked: this returns the phone tool to run (set_timer / set_alarm,
    the same ones the model would call) with the words to say. Only asked for by clients that are phones."""
    m = _Q_TIMER.fullmatch(body)
    if m:
        g = m.groups()
        label = (g[0] or "").strip()
        pairs = [(g[i], g[i + 1]) for i in (1, 3, 5, 7) if g[i] and g[i + 1]]
        if not pairs or _Q_VAGUE.search(label) or len(label) > 40:
            return None
        seconds = int(round(sum(_q_seconds(a, u) for a, u in pairs)))
        if not 5 <= seconds <= 24 * 3600:
            return None
        args = {"seconds": seconds}
        if label:
            args["label"] = label[0].upper() + label[1:]
        return {"reply": f"{'Timer' if not label else label[0].upper() + label[1:] + ' timer'} set for {_q_duration(seconds)}.",
                "kind": "timer", "changed": "", "phone_tool": {"name": "set_timer", "args": args}}
    m = _Q_ALARM.fullmatch(body)
    if m:
        g = m.groups()
        hour, minute, ampm = (g[0], g[1], g[2]) if g[0] else (g[3], g[4], g[5])
        hour, minute, ampm = int(hour), int(minute or 0), (ampm or "").lower().replace(".", "")
        # An unmistakable time only ("7 am", "6:30 pm", "17:45"); "set an alarm for 7" goes to the model
        if ampm and 1 <= hour <= 12 and minute < 60:
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        elif ampm or not (g[1] or g[4]) or not (13 <= hour <= 23 and minute < 60):
            return None
        from datetime import time as _t
        said = _t(hour, minute).strftime("%I:%M %p").lstrip("0")
        return {"reply": f"Alarm set for {said}.", "kind": "alarm", "changed": "",
                "phone_tool": {"name": "set_alarm", "args": {"hour": hour, "minute": minute}}}
    return None


def quick_reply(text, now_text="", phone=False):
    """An instant answer for a simple request, or None when the model should handle it.

    Returns {"reply": str, "kind": str, "changed": "" | "reminders" | "lists"}, plus "phone_tool" {name, args}
    when the asking phone should run one of its own tools (only when phone=True)."""
    raw = " ".join(str(text or "").split())
    if not raw or len(raw) > 140 or "\n" in str(text):
        return None
    body = _Q_TAIL.sub("", _Q_LEAD.sub("", raw, count=1)).strip()
    if not body:
        return None
    now = _quick_now(now_text)

    if phone:
        hit = _quick_phone(body)
        if hit:
            return hit
    if _Q_TIME.fullmatch(body):
        return {"reply": f"It's {_clock(now)}.", "kind": "time", "changed": ""}
    if _Q_DATE.fullmatch(body):
        return {"reply": f"It's {now.strftime('%A, %B')} {now.day}, {now.year}.", "kind": "date", "changed": ""}

    sums = _Q_MATH_LEAD.sub("", body, count=1)
    maths = _quick_maths(sums, sums != body)
    if maths:
        return {"reply": maths, "kind": "maths", "changed": ""}

    if _Q_REMINDERS.fullmatch(body):
        items = [r for r in CHATS.reminders(0) if not r["done"]]
        if not items:
            return {"reply": "You have no upcoming reminders.", "kind": "reminders", "changed": ""}
        lines = "\n".join(f"- {_fmt_time(r['due_at'])}: {r['text']}" for r in items[:20])
        return {"reply": f"Your upcoming reminders:\n{lines}", "kind": "reminders", "changed": ""}

    m = _Q_SHOW.fullmatch(body)
    if m and not _Q_VAGUE.search(m.group(1)):
        key = CHATS._list_name(m.group(1))
        items = [i for i in CHATS.lists().get(key, []) if not i["done"]]
        if not items:
            return {"reply": f"Your {key} list is empty.", "kind": "list", "changed": ""}
        return {"reply": f"On your {key} list:\n" + "\n".join(f"- {i['text']}" for i in items), "kind": "list", "changed": ""}

    m = _Q_ADD.fullmatch(body)
    if m and not _Q_VAGUE.search(m.group(1)) and not _Q_VAGUE.search(m.group(2)):
        items = [x.strip() for x in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", m.group(1)) if x.strip()]
        name, added = CHATS.add_items(m.group(2), items)
        if added:
            return {"reply": f"Added to your {name} list: {', '.join(added)}.", "kind": "list", "changed": "lists"}

    from datetime import timedelta
    what = due = None
    m = _Q_REMIND_IN.fullmatch(body)
    m2 = None if m else _Q_REMIND_IN2.fullmatch(body)
    if m or m2:
        what, amount, unit = (m.group(1), m.group(2), m.group(3)) if m else (m2.group(3), m2.group(1), m2.group(2))
        n = 0.5 if amount.lower().startswith("half") else 1 if amount.lower() in ("a", "an") else int(amount)
        minutes = n * (60 if unit.lower().startswith("h") else 1)
        if 0 < minutes <= 7 * 24 * 60:
            due = now + timedelta(minutes=minutes)
    else:
        m = _Q_REMIND_AT.fullmatch(body)
        if m:
            what, hour, minute = m.group(1), int(m.group(3)), int(m.group(4) or 0)
            ampm, day = (m.group(5) or "").lower().replace(".", ""), (m.group(2) or m.group(6) or "").lower()
            # Only an unmistakable time: "5 pm", "5:30 pm" or 24-hour "17:30". A bare "at 5" goes to the model.
            if ampm and 1 <= hour <= 12 and minute < 60:
                hour = hour % 12 + (12 if ampm == "pm" else 0)
            elif not (not ampm and m.group(4) and 13 <= hour <= 23 and minute < 60):
                hour = None
            if hour is not None:
                due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if day == "tomorrow" or (not day and due <= now):
                    due += timedelta(days=1)
                elif day == "today" and due <= now:
                    due = None
    if what and due and not _Q_VAGUE.search(what) and len(what) >= 3:
        r = CHATS.add_reminder(what[0].upper() + what[1:], int(due.timestamp() * 1000))
        days = (due.date() - now.date()).days
        when = f"at {_clock(due)}" if days == 0 else f"{'tomorrow' if days == 1 else 'on ' + due.strftime('%A')} at {_clock(due)}"
        return {"reply": f"Okay, I'll remind you {when}: {r['text']}.", "kind": "reminder", "changed": "reminders"}
    return None


# ---- 3D Modeler: Blender on the server -------------------------------------------------------------
# Stella edits 3D models in a hidden Blender that stays running. Every change is one small bpy script: the
# server opens the newest saved version, runs it, saves a new numbered version (nothing is overwritten), renders
# the model from several angles and measures it, then the vision add-on checks the pictures against the goal.

import glob
import queue
import shutil

BLENDER = {"arg": "", "root": "", "exe": "", "proc": None, "q": None, "lock": threading.RLock(), "last": 0.0,
           "info": {}, "log": [], "next": 1, "watcher": False}
BLENDER_MARK = "@@STELLAI@@"
BLENDER_IDLE_S = 15 * 60          # Blender leaves memory after this long without a 3D request
BLENDER_KEEP = 40                 # newest versions kept per workspace (plus the first)
MODEL_EXTS = ("blend", "glb", "gltf", "vrm", "fbx", "obj", "stl")
WS_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
BLENDER_VIEWS = ("front", "right", "back", "left", "top", "three_quarter")

BLENDER_WORKER_PY = r'''# StellAI Blender worker. Runs inside Blender ("blender -b -P this_file") and takes one JSON command per line
# on stdin. Written by stellai_hub.py at start-up: edits here are overwritten.
import bpy, bmesh, mathutils, math, json, sys, io, os, traceback, contextlib, shutil, tempfile
from mathutils import Vector, Matrix, Euler, Quaternion

MARK = "@@STELLAI@@"
OUT = sys.stdout


def reply(msg):
    OUT.write(MARK + json.dumps(msg) + "\n")
    OUT.flush()


def rnd(v, n=4):
    return [round(float(x), n) for x in v]


def has_op(cls_name):
    return hasattr(bpy.types, cls_name)


def has_vrm():
    return has_op("IMPORT_SCENE_OT_vrm")


# ---- helpers that step code can use ---------------------------------------------------------
def obj(name):
    """The object called name, with a helpful error listing similar names."""
    o = bpy.data.objects.get(name)
    if o is None:
        names = sorted(bpy.data.objects.keys())
        near = [n for n in names if name.lower() in n.lower() or n.lower() in name.lower()][:8]
        raise KeyError(f"no object named {name!r}; " + (f"similar: {near}" if near else f"objects: {names[:25]}"))
    return o


def bbox(o):
    """World-space (min, max) corners of an object, modifiers included."""
    o = obj(o) if isinstance(o, str) else o
    dg = bpy.context.evaluated_depsgraph_get()
    e = o.evaluated_get(dg)
    pts = [e.matrix_world @ Vector(c) for c in e.bound_box]
    return (Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts))),
            Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts))))


def new_object(name, bm, collection=None):
    """Turns a bmesh into a new mesh object linked to the scene (the bmesh is freed)."""
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    (collection or bpy.context.scene.collection).objects.link(o)
    return o


def material(name, color=(0.8, 0.8, 0.8), emission=None, strength=1.0, metallic=0.0, roughness=0.5):
    """Gets or makes a Principled material. Colours are 0-1 RGB; emission makes it glow."""
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    p = m.node_tree.nodes.get("Principled BSDF")
    if p:
        p.inputs["Base Color"].default_value = (*color[:3], 1.0)
        p.inputs["Metallic"].default_value = metallic
        p.inputs["Roughness"].default_value = roughness
        key = "Emission Color" if "Emission Color" in p.inputs else "Emission"
        if emission is not None:
            p.inputs[key].default_value = (*emission[:3], 1.0)
            if "Emission Strength" in p.inputs:
                p.inputs["Emission Strength"].default_value = strength
    m.diffuse_color = (*(emission or color)[:3], 1.0)   # the colour the quick renders show
    return m


def assign(o, m, replace=True):
    """Puts material m on object o (replacing its materials unless replace=False)."""
    o = obj(o) if isinstance(o, str) else o
    if replace:
        o.data.materials.clear()
    o.data.materials.append(m)


def bone_world(arm, bone):
    """World-space (head, tail) of a bone in the current pose."""
    a = obj(arm) if isinstance(arm, str) else arm
    if a.type != "ARMATURE":
        raise TypeError(f"{a.name} is not an armature")
    b = a.pose.bones.get(bone)
    if b is None:
        raise KeyError(f"no bone {bone!r} in {a.name}; bones: {[x.name for x in a.data.bones][:60]}")
    return a.matrix_world @ b.head, a.matrix_world @ b.tail


def parent_to_bone(o, arm, bone):
    """Makes o follow an armature bone, keeping where it is now."""
    o = obj(o) if isinstance(o, str) else o
    a = obj(arm) if isinstance(arm, str) else arm
    if bone not in a.data.bones:
        raise KeyError(f"no bone {bone!r} in {a.name}; bones: {[x.name for x in a.data.bones][:60]}")
    mw = o.matrix_world.copy()
    o.parent = a
    o.parent_type = "BONE"
    o.parent_bone = bone
    bpy.context.view_layer.update()
    o.matrix_world = mw


def apply_transform(o):
    """Bakes rotation and scale into the mesh (like Ctrl+A), keeping the location."""
    o = obj(o) if isinstance(o, str) else o
    if o.type != "MESH":
        raise TypeError("apply_transform works on meshes")
    loc, rot, sca = o.matrix_basis.decompose()
    o.data.transform(Matrix.LocRotScale(None, rot, sca))
    o.matrix_basis = Matrix.Translation(loc)
    o.data.update()


def namespace():
    return {"bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math, "Vector": Vector, "Matrix": Matrix,
            "Euler": Euler, "Quaternion": Quaternion, "radians": math.radians, "degrees": math.degrees,
            "pi": math.pi, "scene": bpy.context.scene, "obj": obj, "bbox": bbox, "new_object": new_object,
            "material": material, "assign": assign, "bone_world": bone_world, "parent_to_bone": parent_to_bone,
            "apply_transform": apply_transform, "__name__": "stellai_step"}


# ---- scene report -----------------------------------------------------------------------------
def visible(o):
    return not o.hide_render and o.visible_get()


def light_entry(o):
    e = {"name": o.name, "type": o.type, "loc": rnd(o.matrix_world.translation),
         "rot_deg": rnd([math.degrees(a) for a in o.matrix_world.to_euler()], 2), "scale": rnd(o.scale)}
    if o.parent:
        e["parent"] = o.parent.name + (f" (bone {o.parent_bone})" if o.parent_type == "BONE" else "")
    if o.type in ("MESH", "CURVE", "SURFACE", "META", "FONT"):
        lo, hi = bbox(o)
        e["size"] = rnd(hi - lo)
        e["min"], e["max"] = rnd(lo), rnd(hi)
    if o.type == "MESH":
        e["verts"] = len(o.data.vertices)
        e["mats"] = [m.name for m in o.data.materials if m]
    if not visible(o):
        e["hidden"] = True
    return e


def mesh_issues(o):
    out = []
    bm = bmesh.new()
    bm.from_mesh(o.data)
    multi = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    wire = sum(1 for e in bm.edges if not e.link_faces)
    loose = sum(1 for v in bm.verts if not v.link_edges)
    zero = sum(1 for f in bm.faces if f.calc_area() < 1e-10)
    ngons = sum(1 for f in bm.faces if len(f.verts) > 4)
    bm.free()
    if multi:
        out.append(f"{multi} edges shared by 3+ faces (non-manifold)")
    if wire:
        out.append(f"{wire} loose edges")
    if loose:
        out.append(f"{loose} loose vertices")
    if zero:
        out.append(f"{zero} zero-area faces")
    if ngons:
        out.append(f"{ngons} n-gons (5+ sides)")
    if o.data.polygons and not o.data.uv_layers:
        out.append("no UV map")
    if any(s < 0 for s in o.scale):
        out.append("negative scale (normals may look inside out)")
    elif any(abs(s - 1) > 1e-4 for s in o.scale):
        out.append("unapplied scale")
    return out


def report(msg):
    full = bool(msg.get("full", True))
    only = set(msg.get("names") or [])
    dg = bpy.context.evaluated_depsgraph_get()
    objs, tris, issues = [], 0, {}
    lo_all, hi_all = None, None
    for o in bpy.context.scene.objects:
        if o.name.startswith("__stellai"):
            continue
        e = light_entry(o)
        if o.type == "MESH":
            try:
                me = o.evaluated_get(dg).to_mesh()
                me.calc_loop_triangles()
                e["tris"] = len(me.loop_triangles)
                o.evaluated_get(dg).to_mesh_clear()
            except RuntimeError:
                e["tris"] = 0
            tris += e["tris"]
            e["modifiers"] = [m.type.lower() for m in o.modifiers]
            if full and (not only or o.name in only):
                found = mesh_issues(o)
                if found:
                    issues[o.name] = found
        if o.type == "ARMATURE":
            e["bones"] = len(o.data.bones)
        if "min" in e and visible(o):
            lo = Vector(e["min"]); hi = Vector(e["max"])
            lo_all = lo if lo_all is None else Vector(tuple(map(min, lo_all, lo)))
            hi_all = hi if hi_all is None else Vector(tuple(map(max, hi_all, hi)))
        objs.append(e)
    us = bpy.context.scene.unit_settings
    return {"objects": objs, "issues": issues, "tris": tris, "count": len(objs),
            "scene_size": rnd(hi_all - lo_all) if lo_all is not None else None,
            "scene_min": rnd(lo_all) if lo_all is not None else None,
            "scene_max": rnd(hi_all) if hi_all is not None else None,
            "units": f"{us.system.lower()}, scale {us.scale_length:g}", "file": bpy.data.filepath}


# ---- renders ------------------------------------------------------------------------------------
GLYPHS = {
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    " ": ["00000"] * 7,
}
LABELS = {"front": "FRONT", "right": "RIGHT", "back": "BACK", "left": "LEFT", "top": "TOP", "three_quarter": "3/4"}
ORTHO = {  # camera direction from the centre, rotation, which size axes fill the frame
    "front": ((0, -1, 0), (90, 0, 0), (0, 2)),
    "back": ((0, 1, 0), (90, 0, 180), (0, 2)),
    "right": ((1, 0, 0), (90, 0, 90), (1, 2)),
    "left": ((-1, 0, 0), (90, 0, -90), (1, 2)),
    "top": ((0, 0, 1), (0, 0, 0), (0, 1)),
}


def draw_label(arr, text, x0, y0, s=3):
    """Draws text into a top-down RGBA float array: black shadow, then white."""
    for dx, dy, col in ((1, 1, (0, 0, 0, 1)), (0, 0, (1, 1, 1, 1))):
        x = x0
        for ch in text:
            g = GLYPHS.get(ch, GLYPHS[" "])
            for r, row in enumerate(g):
                for c, bit in enumerate(row):
                    if bit == "1":
                        ys, xs = y0 + (r + dy) * s, x + (c + dx) * s
                        arr[ys:ys + s, xs:xs + s] = col
            x += 6 * s


def targets(focus):
    objs = [o for o in bpy.context.scene.objects if visible(o) and not o.name.startswith("__stellai")
            and o.type in ("MESH", "CURVE", "SURFACE", "META", "FONT")]
    if focus:
        f = obj(focus)
        objs = [o for o in objs if o == f or f in o.parent_recursive] or [f]
    return objs


def render(msg):
    import numpy as np
    scene = bpy.context.scene
    views = [v for v in (msg.get("views") or ["front", "right", "back", "three_quarter"]) if v in LABELS][:6]
    res = max(256, min(1024, int(msg.get("res") or 512)))
    objs = targets(msg.get("focus"))
    if not objs:
        raise ValueError("nothing visible to render")
    lo, hi = bbox(objs[0])
    for o in objs[1:]:
        a, b = bbox(o)
        lo, hi = Vector(tuple(map(min, lo, a))), Vector(tuple(map(max, hi, b)))
    centre, size = (lo + hi) / 2, hi - lo
    span = max(size.length, 0.01)
    made = []
    try:
        cam_data = bpy.data.cameras.new("__stellai_cam")
        cam = bpy.data.objects.new("__stellai_cam", cam_data)
        scene.collection.objects.link(cam)
        made.append(cam)
        sun_data = bpy.data.lights.new("__stellai_sun", "SUN")
        sun_data.energy = 3.0
        sun = bpy.data.objects.new("__stellai_sun", sun_data)
        sun.rotation_euler = Euler((math.radians(50), math.radians(10), math.radians(30)))
        scene.collection.objects.link(sun)
        made.append(sun)
        scene.camera = cam
        r = scene.render
        r.resolution_x = r.resolution_y = res
        r.resolution_percentage = 100
        r.image_settings.file_format = "PNG"
        r.film_transparent = False
        if scene.world is None:
            scene.world = bpy.data.worlds.new("__stellai_world")
        scene.world.color = (0.16, 0.17, 0.19)
        engine = setup_engine(scene, msg.get("look") or "solid")
        tmp = tempfile.mkdtemp(prefix="stellai-r-")
        tiles = []
        for v in views:
            if v == "three_quarter":
                d = Vector((0.75, -1.0, 0.45)).normalized()
                cam_data.type = "PERSP"
                cam_data.lens = 50
                fov = 2 * math.atan(18 / 50)
                dist = (span / 2) / math.sin(fov / 2) * 1.08
                cam.location = centre + d * dist
                cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
            else:
                d, rot, axes = ORTHO[v]
                cam_data.type = "ORTHO"
                cam_data.ortho_scale = max(size[axes[0]], size[axes[1]], 0.01) * 1.12
                cam.location = centre + Vector(d) * (span * 2 + 1)
                cam.rotation_euler = Euler([math.radians(a) for a in rot])
            cam_data.clip_start = 0.001
            cam_data.clip_end = span * 6 + 10
            path = os.path.join(tmp, v + ".png")
            r.filepath = path
            try:
                bpy.ops.render.render(write_still=True)
            except RuntimeError:
                if engine == "CYCLES":
                    raise
                engine = setup_engine(scene, "cycles")
                bpy.ops.render.render(write_still=True)
            img = bpy.data.images.load(path)
            px = np.empty(res * res * 4, dtype=np.float32)
            img.pixels.foreach_get(px)
            bpy.data.images.remove(img)
            tile = np.flipud(px.reshape(res, res, 4)).copy()
            draw_label(tile, LABELS[v], 8, 8)
            tiles.append(tile)
        cols = 1 if len(tiles) == 1 else 2
        rows = (len(tiles) + cols - 1) // cols
        gap = 4
        W, H = cols * res + (cols - 1) * gap, rows * res + (rows - 1) * gap
        sheet = np.zeros((H, W, 4), dtype=np.float32)
        sheet[..., 3] = 1
        for i, t in enumerate(tiles):
            y, x = (i // cols) * (res + gap), (i % cols) * (res + gap)
            sheet[y:y + res, x:x + res] = t
        out = bpy.data.images.new("__stellai_sheet", W, H, alpha=True)
        out.pixels.foreach_set(np.flipud(sheet).ravel())
        out.filepath_raw = msg["out"]
        out.file_format = "PNG"
        out.save()
        bpy.data.images.remove(out)
        shutil.rmtree(tmp, ignore_errors=True)
        layout = ", ".join(f"{'top' if i < cols else 'bottom'}-{'left' if i % cols == 0 else 'right'}: {LABELS[v]}"
                           for i, v in enumerate(views)) if cols == 2 else LABELS[views[0]]
        return {"engine": engine, "views": views, "layout": layout, "centre": rnd(centre), "size": rnd(size)}
    finally:
        for o in made:
            data = o.data
            bpy.data.objects.remove(o, do_unlink=True)
            if isinstance(data, bpy.types.Camera):
                bpy.data.cameras.remove(data)
            elif isinstance(data, bpy.types.Light):
                bpy.data.lights.remove(data)


def setup_engine(scene, look):
    if look == "material":
        for e in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            try:
                scene.render.engine = e
                return e
            except TypeError:
                pass
    if look != "cycles":
        scene.render.engine = "BLENDER_WORKBENCH"
        sh = scene.display.shading
        sh.light = "STUDIO"
        sh.color_type = "TEXTURE"     # image textures where there are some, material colours elsewhere
        sh.show_object_outline = True
        sh.show_cavity = True
        try:
            scene.view_settings.view_transform = "Standard"
        except TypeError:
            pass
        return "BLENDER_WORKBENCH"
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 12
    try:
        scene.cycles.use_denoising = False
    except AttributeError:
        pass
    return "CYCLES"


# ---- files --------------------------------------------------------------------------------------
def op_new(msg):
    bpy.ops.wm.read_homefile(use_empty=True)
    return {}


def op_open(msg):
    bpy.ops.wm.open_mainfile(filepath=msg["path"], load_ui=False)
    return {"file": bpy.data.filepath}


def op_import(msg):
    path = msg["path"]
    ext = path.rsplit(".", 1)[-1].lower()
    note = ""
    keep = bool(msg.get("keep"))          # add to the open scene instead of starting a new one
    before = set(bpy.data.objects.keys())
    if not keep:
        bpy.ops.wm.read_homefile(use_empty=True)
    if ext in ("glb", "gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == "vrm":
        if has_vrm():
            bpy.ops.import_scene.vrm(filepath=path)
        else:
            tmp = os.path.join(tempfile.mkdtemp(prefix="stellai-i-"), "model.glb")
            shutil.copyfile(path, tmp)
            bpy.ops.import_scene.gltf(filepath=tmp)
            note = ("The VRM add-on isn't installed in this Blender, so the file came in as plain glTF: meshes, "
                    "textures and the armature are there, but VRM settings and MToon materials aren't.")
    elif ext == "fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == "obj":
        (bpy.ops.wm.obj_import if has_op("WM_OT_obj_import") else bpy.ops.import_scene.obj)(filepath=path)
    elif ext == "stl":
        (bpy.ops.wm.stl_import if has_op("WM_OT_stl_import") else bpy.ops.import_mesh.stl)(filepath=path)
    else:
        raise ValueError(f"can't import .{ext} files")
    added = [o for o in bpy.data.objects if o.name not in before]
    name = msg.get("rename") or ""
    if name and keep:
        meshes = [o for o in added if o.type == "MESH"]
        for i, o in enumerate(meshes):
            o.name = name if i == 0 else f"{name}.{i:03d}"
            o.data.name = o.name
        for o in added:                   # empties the glTF importer adds around the mesh
            if o.type == "EMPTY" and not o.children:
                bpy.data.objects.remove(o, do_unlink=True)
    return {"note": note, "added": [o.name for o in bpy.data.objects if o.name not in before]}


def op_save(msg):
    bpy.ops.wm.save_as_mainfile(filepath=msg["path"], compress=True)
    return {"file": bpy.data.filepath}


def op_export(msg):
    path, fmt = msg["path"], msg["format"]
    if fmt == "glb":
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB")
    elif fmt == "fbx":
        bpy.ops.export_scene.fbx(filepath=path)
    elif fmt == "obj":
        (bpy.ops.wm.obj_export if has_op("WM_OT_obj_export") else bpy.ops.export_scene.obj)(filepath=path)
    elif fmt == "stl":
        (bpy.ops.wm.stl_export if has_op("WM_OT_stl_export") else bpy.ops.export_mesh.stl)(filepath=path)
    elif fmt == "vrm":
        if not has_op("EXPORT_SCENE_OT_vrm"):
            raise ValueError("the VRM add-on isn't installed in the server's Blender; export glb instead, or install "
                             "'VRM Add-on for Blender' in that Blender")
        bpy.ops.export_scene.vrm(filepath=path)
    else:
        raise ValueError("format must be glb, fbx, obj, stl or vrm")
    if not os.path.exists(path):
        raise RuntimeError("the exporter didn't write a file")
    return {"bytes": os.path.getsize(path)}


def op_run(msg):
    code = compile(msg["code"], "<step>", "exec")
    exec(code, namespace())
    bpy.context.view_layer.update()
    return {}


def step_trace():
    tb = traceback.extract_tb(sys.exc_info()[2])
    lines = [f"line {f.lineno}: {(f.line or '').strip()}" for f in tb if f.filename == "<step>"]
    return lines[-3:]


OPS = {"ping": lambda m: {}, "new": op_new, "open": op_open, "import": op_import, "save": op_save,
       "export": op_export, "run": op_run, "report": report, "render": render}


def main():
    reply({"id": 0, "ok": True, "version": bpy.app.version_string, "vrm": has_vrm()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("op") == "quit":
            break
        mid = msg.get("id")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                res = OPS[msg["op"]](msg)
            reply({"id": mid, "ok": True, **res, "output": buf.getvalue()[-3000:]})
        except Exception as e:
            reply({"id": mid, "ok": False, "error": f"{type(e).__name__}: {e}"[:1500], "where": step_trace(),
                   "output": buf.getvalue()[-3000:]})


main()
'''


def find_blender():
    cands = [BLENDER.get("arg") or "", os.environ.get("STELLAI_BLENDER", ""),
             os.path.expanduser(os.path.join("~", "llm", "blender", "blender")), shutil.which("blender") or ""]
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
        for root in roots:
            if root:
                cands += sorted(glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe")),
                                reverse=True)
    cands += ["/snap/bin/blender", "/usr/bin/blender", "/usr/local/bin/blender",
              "/var/lib/flatpak/exports/bin/org.blender.Blender"]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def _blender_alive():
    p = BLENDER["proc"]
    return p is not None and p.poll() is None


def _blender_stop():
    p = BLENDER["proc"]
    BLENDER["proc"] = None
    if p is None:
        return
    try:
        if p.poll() is None:
            try:
                p.stdin.write('{"op": "quit"}\n')
                p.stdin.flush()
                p.wait(timeout=10)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                p.kill()
    except OSError:
        pass


def _blender_watch():
    while True:
        time.sleep(60)
        if _blender_alive() and time.time() - BLENDER["last"] > BLENDER_IDLE_S and BLENDER["lock"].acquire(False):
            try:
                _blender_stop()
                log("Blender went to sleep (idle)")
            finally:
                BLENDER["lock"].release()


def _blender_start():
    exe = find_blender()
    if not exe:
        raise RuntimeError("Blender isn't installed on the server. Linux: re-run stellai-setup.sh and say yes to "
                           "Blender. Windows: install Blender from blender.org. Then try again.")
    wdir = os.path.join(BLENDER["root"], ".worker")
    os.makedirs(wdir, exist_ok=True)
    script = os.path.join(wdir, "stellai_blender_worker.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(BLENDER_WORKER_PY)
    proc = subprocess.Popen([exe, "-b", "--disable-autoexec", "--python-exit-code", "1", "-P", script],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=wdir,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=0x08000000 if os.name == "nt" else 0)   # no console window on Windows
    q = queue.Queue()

    def reader():
        for line in proc.stdout:
            if line.startswith(BLENDER_MARK):
                try:
                    q.put(json.loads(line[len(BLENDER_MARK):]))
                except ValueError:
                    pass
            else:
                BLENDER["log"].append(line.rstrip()[:300])
                del BLENDER["log"][:-40]
        q.put(None)

    BLENDER.update(proc=proc, q=q, exe=exe, log=[])
    threading.Thread(target=reader, daemon=True, name="blender-out").start()
    first = _blender_wait(0, 120)
    BLENDER["info"] = {"version": first.get("version", ""), "vrm": bool(first.get("vrm"))}
    log(f"Blender {BLENDER['info']['version']} ready for 3D modeling ({exe})")
    if not BLENDER["watcher"]:
        BLENDER["watcher"] = True
        threading.Thread(target=_blender_watch, daemon=True, name="blender-idle").start()


def _blender_wait(mid, timeout):
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            _blender_stop()
            raise RuntimeError(f"Blender took longer than {int(timeout)} s, so StellAI stopped it. "
                               "Nothing from this step was saved.")
        try:
            msg = BLENDER["q"].get(timeout=left)
        except queue.Empty:
            continue
        if msg is None:
            BLENDER["proc"] = None
            tail = " / ".join(l for l in BLENDER["log"][-4:] if l.strip())
            raise RuntimeError("Blender stopped unexpectedly" + (f": {tail}" if tail else "") +
                               ". Nothing from this step was saved.")
        if msg.get("id") == mid:
            return msg


class BlenderStepError(RuntimeError):
    def __init__(self, msg):
        super().__init__(msg.get("error") or "Blender error")
        self.detail = msg


def blender_call(op, timeout=180, **kw):
    """Runs one worker command and returns its answer. Raises BlenderStepError when Blender reports an error."""
    with BLENDER["lock"]:
        if not _blender_alive():
            _blender_start()
        BLENDER["last"] = time.time()
        mid = BLENDER["next"]
        BLENDER["next"] += 1
        try:
            BLENDER["proc"].stdin.write(json.dumps(dict(kw, id=mid, op=op)) + "\n")
            BLENDER["proc"].stdin.flush()
        except (OSError, ValueError):
            _blender_stop()
            raise RuntimeError("couldn't reach Blender; try again")
        msg = _blender_wait(mid, timeout)
        BLENDER["last"] = time.time()
        if not msg.get("ok"):
            raise BlenderStepError(msg)
        return msg


# ---- Workspaces: numbered versions of one model ---------------------------------------------------
def _ws(name):
    name = str(name or "").strip().lower()
    if not name:
        try:
            with open(os.path.join(BLENDER["root"], "active.txt"), encoding="utf-8") as f:
                name = f.read().strip()
        except OSError:
            name = ""
        if not name:
            raise ValueError("no 3D workspace yet: start one with blender_project action new")
    if not WS_NAME.fullmatch(name):
        raise ValueError("workspace names use lowercase letters, digits and dashes, e.g. vela-halo")
    return name


def _ws_dir(name, *parts):
    return os.path.join(BLENDER["root"], "workspaces", name, *parts)


def _ws_versions(name):
    out = []
    for f in os.listdir(_ws_dir(name, "versions")) if os.path.isdir(_ws_dir(name, "versions")) else []:
        m = re.fullmatch(r"v(\d{4,})\.blend", f)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def _ws_file(name, v):
    return _ws_dir(name, "versions", f"v{v:04d}.blend")


def _ws_history(name):
    try:
        with open(_ws_dir(name, "history.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _ws_record(name, v, what):
    hist = _ws_history(name)
    hist.append({"version": v, "what": str(what)[:300], "at": int(time.time())})
    with open(_ws_dir(name, "history.json"), "w", encoding="utf-8") as f:
        json.dump(hist[-500:], f, indent=1)
    keep = set(_ws_versions(name)[-BLENDER_KEEP:]) | {1}
    for old in _ws_versions(name):
        if old not in keep:
            try:
                os.remove(_ws_file(name, old))
            except OSError:
                pass


def _ws_activate(name):
    with open(os.path.join(BLENDER["root"], "active.txt"), "w", encoding="utf-8") as f:
        f.write(name)


def _ws_latest(name):
    vs = _ws_versions(name)
    if not vs:
        raise ValueError(f"workspace {name} has no saved versions")
    return vs[-1]


def _ws_save_new(name, what):
    v = (_ws_versions(name) or [0])[-1] + 1
    blender_call("save", timeout=180, path=_ws_file(name, v))
    _ws_record(name, v, what)
    return v


def _inbox():
    d = os.path.join(BLENDER["root"], "inbox")
    os.makedirs(d, exist_ok=True)
    return d


def _model_files():
    """Files Stella may import: the 3D inbox, plus the avatar characters already on the server."""
    files = {}
    for f in sorted(os.listdir(_inbox())):
        if f.rsplit(".", 1)[-1].lower() in MODEL_EXTS and os.path.isfile(os.path.join(_inbox(), f)):
            files[f] = os.path.join(_inbox(), f)
    chars = _avatar_path("bin", "characters") if AVATAR.get("dir") else ""
    if chars and os.path.isdir(chars):
        for f in sorted(os.listdir(chars)):
            if f.rsplit(".", 1)[-1].lower() in ("vrm", "glb"):
                files.setdefault("character:" + f, os.path.join(chars, f))
    return files


# ---- Guard rails for the code Stella writes ------------------------------------------------------
BPY_IMPORTS = {"bpy", "bmesh", "mathutils", "math", "random", "itertools", "functools", "collections", "colorsys",
               "bpy_extras", "statistics", "numpy"}
BPY_BAD_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "input", "breakpoint",
                 "exit", "quit", "memoryview", "delattr"}
BPY_BAD_ATTRS = {"texts", "handlers", "timers", "driver_namespace", "utils", "preferences", "as_module", "execfile",
                 "unpack", "write", "tofile", "savetxt", "savez", "fromfile", "addon_utils", "system"}
BPY_BAD_OPS = {"wm", "script", "preferences", "extensions", "file", "export_scene", "export_mesh", "export_anim",
               "import_scene", "import_mesh", "import_anim", "import_curve", "image", "sound", "text", "render",
               "screen", "ed", "workspace", "cachefile", "sequencer", "clip", "buttons", "console", "info"}


def check_bpy_code(code, allowed=None, extra_bad=(), bad_prefixes=()):
    """Problems that stop a step from running: no files, network, shell or program settings, only design edits.
    A guard rail against mistakes, not a sandbox. Defaults are for Blender; the CAD and PCB designers pass their
    own allowed imports and blocked names."""
    allowed = allowed or BPY_IMPORTS
    if len(code) > 40000:
        return ["the script is too long (40,000 characters max); split the change into smaller steps"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error on line {e.lineno}: {e.msg}"]
    probs = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            probs += [f"import {a.name} isn't allowed" for a in n.names if a.name.split(".")[0] not in allowed]
        elif isinstance(n, ast.ImportFrom):
            if n.level or (n.module or "").split(".")[0] not in allowed:
                probs.append(f"from {n.module} import isn't allowed")
        elif isinstance(n, ast.Name) and (n.id in BPY_BAD_NAMES or n.id in extra_bad or
                                          any(n.id.startswith(b) for b in bad_prefixes)):
            probs.append(f"{n.id}() isn't allowed")
        elif isinstance(n, ast.Attribute):
            base_is_ops = (isinstance(n.value, ast.Attribute) and n.value.attr == "ops") or \
                          (isinstance(n.value, ast.Name) and n.value.id == "ops")
            if n.attr.startswith("_"):
                probs.append(f"'.{n.attr}' isn't allowed")
            elif n.attr in BPY_BAD_ATTRS or n.attr in extra_bad or n.attr.lower().startswith("save") or \
                    any(n.attr.startswith(b) for b in bad_prefixes):
                probs.append(f"'.{n.attr}' isn't allowed (no files, scripts or settings)")
            elif base_is_ops and n.attr in BPY_BAD_OPS:
                probs.append(f"bpy.ops.{n.attr} isn't allowed (files, rendering and windows are handled by StellAI)")
            elif isinstance(n.ctx, ast.Store) and n.attr in ("filepath", "filepath_raw", "directory"):
                probs.append(f"setting .{n.attr} isn't allowed")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "setattr", "hasattr"):
            if len(n.args) < 2 or not (isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)
                                       and not n.args[1].value.startswith("_")):
                probs.append(f"{n.func.id}() needs a plain attribute name in quotes")
    return sorted(set(probs))[:8]


# ---- Measuring, comparing and looking --------------------------------------------------------------
def _scene_index(rep):
    return {o["name"]: o for o in rep.get("objects", [])}


def _changes(before, after):
    b, a = _scene_index(before), _scene_index(after)
    added = [_short(a[n]) for n in a if n not in b]
    removed = [n for n in b if n not in a]
    changed = []
    for n in a:
        if n not in b:
            continue
        diff = {}
        for k in ("loc", "rot_deg", "scale", "size", "verts", "mats", "parent", "modifiers", "hidden"):
            if b[n].get(k) != a[n].get(k):
                diff[k] = {"from": b[n].get(k), "to": a[n].get(k)}
        if diff:
            changed.append({"name": n, **diff})
    return {"added": added[:20], "removed": removed[:20], "changed": changed[:20],
            "more": max(0, len(added) - 20) + max(0, len(changed) - 20)}


def _short(o):
    return {k: o[k] for k in ("name", "type", "loc", "rot_deg", "size", "min", "max", "verts", "tris", "mats",
                              "parent", "bones") if k in o}


def _scene_summary(rep, limit=40):
    objs = sorted(rep.get("objects", []), key=lambda o: -(o.get("tris") or 0))
    return {"objects": [_short(o) for o in objs[:limit]], "more_objects": max(0, len(objs) - limit),
            "triangles": rep.get("tris"), "scene_size_m": rep.get("scene_size"), "scene_min": rep.get("scene_min"),
            "scene_max": rep.get("scene_max"), "units": rep.get("units"), "issues": rep.get("issues") or {}}


def _blender_render(views=None, focus="", look="solid", reopen=""):
    """Renders the loaded scene to one picture in the diagram folder. Returns (image id, render info).
    Quick renders need the graphics card; if Blender can't use it (or crashes trying), this falls back to slower
    CPU rendering (Cycles) and remembers that until the server restarts."""
    views = [v for v in (views or ["front", "right", "back", "three_quarter"]) if v in BLENDER_VIEWS][:6] or ["front"]
    image_id = uuid.uuid4().hex[:16]
    out = os.path.join(DIAGRAM_DIR, image_id + ".png")
    if BLENDER.get("cpu_only"):
        look = "cycles"
    kw = dict(out=out, views=views, focus=focus or "", res=512)
    try:
        info = blender_call("render", timeout=420, look=look, **kw)
    except RuntimeError as e:
        if look == "cycles" or "stopped unexpectedly" not in str(e) or not reopen:
            raise
        BLENDER["cpu_only"] = True
        log("Blender crashed rendering with the graphics card; using CPU renders (Cycles) from now on")
        blender_call("open", timeout=180, path=reopen)
        info = blender_call("render", timeout=600, look="cycles", **kw)
    if info.get("engine") == "CYCLES" and look != "cycles":
        BLENDER["cpu_only"] = True
    if not os.path.exists(out):
        raise RuntimeError("Blender rendered no picture")
    return image_id, info


def _vision_check(image_id, goal, facts, layout, workspace="", refs=None):
    """Asks the vision add-on whether the renders match the step's goal (and the workspace's reference pictures).
    Text, or why it couldn't look."""
    import base64
    try:
        with open(os.path.join(DIAGRAM_DIR, image_id + ".png"), "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        if refs is None:
            refs = _ref_images(workspace) if workspace else []
        content = []
        for label, path in refs:
            with open(path, "rb") as f:
                content.append({"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()}})
        ref_text = (f"The first {len(refs)} picture(s) are the user's reference photos ("
                    + ", ".join(l for l, _ in refs) + ") showing what the model should end up looking like; the LAST "
                    "picture is the model now. Compare shape, proportions, colours and details with the references, "
                    "and say what differs most. ") if refs else ""
        prompt = ("You are checking one step of a 3D modeling job in Blender. " + ref_text +
                  f"The model picture shows it from several angles ({layout}); each panel is labelled in its corner. "
                  "FRONT looks at the side facing -Y (a character's face); Z is up. Grey is background.\n"
                  f"The step was meant to: {goal}\n"
                  f"Measured by Blender (metres): {json.dumps(facts)[:2500]}\n"
                  "Judge only what you can see, and trust the measurements over your eyes for sizes. Reply in this form:\n"
                  "VERDICT: looks right | has problems | can't tell\n"
                  "- up to 5 short, concrete observations (which part, where, how it differs from the goal"
                  + (" or the references" if refs else "") + ")")
        content = [{"type": "text", "text": prompt}] + content + [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]
        body = {"model": "local", "temperature": 0.1, "max_tokens": 500,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": content}]}
        out = _llama("/v1/chat/completions", body, timeout=300)["choices"][0]["message"].get("content") or ""
        return re.sub(r"<think>[\s\S]*?</think>", "", out).strip() or "can't tell (empty answer)"
    except urllib.error.HTTPError as e:
        return f"not checked: the model server refused the picture (HTTP {e.code}; is the vision add-on installed?)"
    except (urllib.error.URLError, OSError, KeyError, ValueError, IndexError) as e:
        return f"not checked: the vision add-on couldn't look ({e})"


# ---- The tools ---------------------------------------------------------------------------------------
def tool_blender_project(args):
    action = str(args.get("action", "status")).strip().lower()
    if action == "status":
        exe = find_blender()
        out = {"blender": exe or "not installed", "files_you_can_import": list(_model_files())[:40],
               "inbox_folder": _inbox(), "workspaces": []}
        if exe and BLENDER["info"]:
            out.update(version=BLENDER["info"].get("version"), vrm_addon=BLENDER["info"].get("vrm"))
        wroot = os.path.join(BLENDER["root"], "workspaces")
        for n in sorted(os.listdir(wroot)) if os.path.isdir(wroot) else []:
            if WS_NAME.fullmatch(n) and _ws_versions(n):
                out["workspaces"].append({"name": n, "latest_version": _ws_versions(n)[-1]})
        try:
            out["active"] = _ws("")
            out["reference_pictures"] = len(_refs(out["active"]))
        except ValueError:
            out["active"] = None
        out["photo_to_3d"] = "installed" if _shape_config()["python"] else "not installed"
        out["graphics_cards"] = [{"index": g[0], "name": g[3], "free_gb": round(g[1] / 1024, 1),
                                  "total_gb": round(g[2] / 1024, 1)} for g in _gpus()]
        out["can_pause_stella_for_shape_jobs"] = _can_pause_model()
        return out, "Checked 3D workspaces"
    if action == "new":
        name = str(args.get("name", "")).strip().lower()
        if not WS_NAME.fullmatch(name):
            raise ValueError("give a name: lowercase letters, digits and dashes, e.g. vela-halo")
        if _ws_versions(name):
            raise ValueError(f"workspace {name} already exists; use action open, or pick another name")
        os.makedirs(_ws_dir(name, "versions"), exist_ok=True)
        os.makedirs(_ws_dir(name, "exports"), exist_ok=True)
        src = str(args.get("file", "")).strip()
        with BLENDER["lock"]:
            note = ""
            if src:
                files = _model_files()
                if src not in files:
                    raise ValueError(f"no file {src!r}; files you can import: {list(files)[:30]} (the user can drop "
                                     f"files into {_inbox()})")
                if src.lower().endswith(".blend"):
                    blender_call("open", timeout=180, path=files[src])
                else:
                    note = blender_call("import", timeout=300, path=files[src]).get("note", "")
            else:
                blender_call("new", timeout=60)
            v = _ws_save_new(name, f"start: {src or 'empty scene'}")
            _ws_activate(name)
            rep = blender_call("report", timeout=180, full=True)
            img, info = (None, {})
            if rep.get("scene_size"):
                img, info = _blender_render(reopen=_ws_file(name, v))
        res = {"workspace": name, "version": v, "scene": _scene_summary(rep), "note": note}
        if img:
            res.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
        return res, f"Started 3D workspace {name} (v{v})"
    if action == "open":
        name = _ws(args.get("name"))
        v = _ws_latest(name)
        _ws_activate(name)
        return {"workspace": name, "latest_version": v, "recent": _ws_history(name)[-8:]}, f"Opened {name} (v{v})"
    if action == "history":
        name = _ws(args.get("name"))
        return {"workspace": name, "versions_on_disk": _ws_versions(name), "history": _ws_history(name)[-25:]}, \
            f"History of {name}"
    if action == "revert":
        name = _ws(args.get("name"))
        try:
            target = int(str(args.get("version", "")).lstrip("vV"))
        except ValueError:
            raise ValueError("give the version number to go back to, e.g. 7")
        if target not in _ws_versions(name):
            raise ValueError(f"version {target} isn't on disk; available: {_ws_versions(name)[-15:]}")
        with BLENDER["lock"]:
            blender_call("open", timeout=180, path=_ws_file(name, target))
            v = _ws_save_new(name, f"back to v{target}")
            img, info = _blender_render(reopen=_ws_file(name, v))
        return {"workspace": name, "version": v, "same_as": target, "image": img, "title": f"{name} v{v}",
                "views": info.get("layout")}, f"Went back to v{target} (saved as v{v})"
    if action in ("add_reference", "references", "clear_references"):
        return _refs_action(action, args)
    if action == "export":
        name = _ws(args.get("name"))
        fmt = str(args.get("format", "glb")).lower().strip(". ")
        v = _ws_latest(name)
        os.makedirs(_ws_dir(name, "exports"), exist_ok=True)
        path = _ws_dir(name, "exports", f"{name}-v{v:04d}.{fmt}")
        if fmt == "blend":
            shutil.copyfile(_ws_file(name, v), path)
        else:
            with BLENDER["lock"]:
                blender_call("open", timeout=180, path=_ws_file(name, v))
                blender_call("export", timeout=420, path=path, format=fmt)
        return {"workspace": name, "version": v, "file": path, "bytes": os.path.getsize(path),
                "note": "The file is in this folder on the server computer."}, f"Exported {name} v{v} as {fmt}"
    raise ValueError("action must be status, new, open, history, revert, export, add_reference, references or "
                     "clear_references")


def tool_blender_step(args):
    code = str(args.get("code", ""))
    goal = str(args.get("goal", "")).strip()
    if not goal:
        raise ValueError("say what this step should do in goal (with numbers), so the result can be checked")
    if not code.strip():
        raise ValueError("no code")
    probs = check_bpy_code(code)
    if probs:
        return {"ok": False, "error": "the code was not run: " + "; ".join(probs)}, "Step blocked"
    name = _ws(args.get("workspace"))
    views = args.get("views") if isinstance(args.get("views"), list) else None
    timeout = max(30, min(600, int(args.get("timeout") or 180)))
    with BLENDER["lock"]:
        base = _ws_latest(name)
        blender_call("open", timeout=180, path=_ws_file(name, base))
        before = blender_call("report", timeout=180, full=False)
        try:
            ran = blender_call("run", timeout=timeout, code=code)
        except BlenderStepError as e:
            d = e.detail
            return {"ok": False, "error": d.get("error"), "where": d.get("where"), "output": d.get("output", ""),
                    "version": base, "note": f"Nothing was saved; the model is still v{base}."}, "Step failed"
        after = blender_call("report", timeout=180, full=True)
        diff = _changes(before, after)
        if not (diff["added"] or diff["removed"] or diff["changed"]):
            return {"ok": False, "error": "the code ran but nothing in the scene changed (no object was added, "
                                          "removed, moved, resized or re-materialed)", "output": ran.get("output", ""),
                    "version": base, "note": f"Nothing was saved; the model is still v{base}."}, "Step changed nothing"
        v = _ws_save_new(name, goal)
        try:
            img, info = _blender_render(views, str(args.get("focus", "")), str(args.get("look", "solid")),
                                        reopen=_ws_file(name, v))
        except (RuntimeError, BlenderStepError) as e:
            img, info = None, {"error": str(e)}
    touched = {o["name"] for o in diff["added"]} | {c["name"] for c in diff["changed"]}
    issues = {k: val for k, val in (after.get("issues") or {}).items() if k in touched}
    res = {"version": v, "previous": base, "changes": diff, "new_problems_in_changed_objects": issues,
           "triangles": after.get("tris"), "output": ran.get("output", "")[-1500:]}
    if img:
        res.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
        if str(args.get("check", True)).lower() not in ("false", "0", "no"):
            res["visual_check"] = _vision_check(img, goal, diff, info.get("layout", ""), name)
    else:
        res["render_error"] = info.get("error")
    return res, f"Saved {name} v{v}"


def tool_blender_inspect(args):
    name = _ws(args.get("workspace"))
    focus = str(args.get("focus", "")).strip()
    with BLENDER["lock"]:
        v = _ws_latest(name)
        blender_call("open", timeout=180, path=_ws_file(name, v))
        rep = blender_call("report", timeout=240, full=True, names=[focus] if focus else [])
        img, info = None, {}
        if args.get("render"):
            views = args.get("views") if isinstance(args.get("views"), list) else None
            img, info = _blender_render(views, focus, str(args.get("look", "solid")), reopen=_ws_file(name, v))
    res = {"workspace": name, "version": v, "scene": _scene_summary(rep)}
    if focus:
        res["focus"] = _scene_index(rep).get(focus) or f"no object named {focus!r}"
    if img:
        res.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
        if _ref_images(name) and str(args.get("compare", True)).lower() not in ("false", "0", "no"):
            res["comparison"] = _vision_check(img, "compare the model as it is now with the reference pictures",
                                              {"scene_size_m": rep.get("scene_size")}, info.get("layout", ""), name)
    return res, f"Inspected {name} v{v}"


# ---- Reference pictures: what the model should look like -------------------------------------------
PHOTO_ID = re.compile(r"[0-9a-f]{16}")
REF_LABELS = ("front", "left", "back", "right", "top", "detail")


def _refs(name):
    try:
        with open(_ws_dir(name, "refs", "refs.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_refs(name, refs):
    os.makedirs(_ws_dir(name, "refs"), exist_ok=True)
    with open(_ws_dir(name, "refs", "refs.json"), "w", encoding="utf-8") as f:
        json.dump(refs, f, indent=1)


def _photo_files(photos):
    """Chat photo ids (as shown to Stella, e.g. [photo 0123abcd...]) or "latest" -> file paths."""
    if isinstance(photos, str):
        photos = [p for p in re.split(r"[,\s]+", photos) if p]
    out = []
    for p in photos or []:
        p = str(p).strip().lower().replace("image:", "")
        if p in ("latest", "last", "newest"):
            files = sorted((os.path.join(IMAGE_DIR, f) for f in os.listdir(IMAGE_DIR) if f.endswith(".jpg")),
                           key=os.path.getmtime)
            files = [f for f in files if time.time() - os.path.getmtime(f) < 6 * 3600]
            if not files:
                raise ValueError("no photo was sent in the last few hours; ask the user to send one in the chat")
            out.append(files[-1])
            continue
        m = PHOTO_ID.search(p)
        if not m or not os.path.isfile(os.path.join(IMAGE_DIR, m.group(0) + ".jpg")):
            raise ValueError(f"no chat photo {p!r}; use the id from [photo ...] in the chat, or 'latest'")
        out.append(os.path.join(IMAGE_DIR, m.group(0) + ".jpg"))
    return out


def _ref_images(name, limit=3):
    """Up to limit reference pictures, labelled views first, as (label, path)."""
    refs = [r for r in _refs(name) if os.path.isfile(_ws_dir(name, "refs", r["file"]))]
    order = {l: i for i, l in enumerate(REF_LABELS)}
    refs.sort(key=lambda r: order.get(r.get("label") or "", 99))
    return [(r.get("label") or "reference", _ws_dir(name, "refs", r["file"])) for r in refs[:limit]]


def _refs_action(action, args):
    name = _ws(args.get("name"))
    refs = _refs(name)
    if action == "references":
        return {"workspace": name, "references": refs}, f"{len(refs)} reference picture(s)"
    if action == "clear_references":
        for r in refs:
            try:
                os.remove(_ws_dir(name, "refs", r["file"]))
            except OSError:
                pass
        _save_refs(name, [])
        return {"workspace": name, "references": []}, "Cleared reference pictures"
    files = _photo_files(args.get("photos") or "latest")
    labels = args.get("labels") or []
    if isinstance(labels, str):
        labels = [l for l in re.split(r"[,\s]+", labels) if l]
    added = []
    for i, src in enumerate(files):
        label = str(labels[i]).lower() if i < len(labels) else ""
        if label and label not in REF_LABELS:
            raise ValueError(f"labels are {', '.join(REF_LABELS)}")
        if label:   # one picture per view: a new front replaces the old front
            for r in [r for r in refs if r.get("label") == label]:
                refs.remove(r)
        fname = f"ref-{uuid.uuid4().hex[:8]}.jpg"
        os.makedirs(_ws_dir(name, "refs"), exist_ok=True)
        shutil.copyfile(src, _ws_dir(name, "refs", fname))
        entry = {"file": fname, "label": label, "photo": os.path.basename(src)[:-4], "at": int(time.time())}
        refs.append(entry)
        added.append(entry)
    _save_refs(name, refs[-8:])
    return {"workspace": name, "added": added, "references": refs[-8:],
            "note": "Every visual check now compares the renders with these pictures."}, \
        f"Added {len(added)} reference picture(s)"


# ---- Photo-to-3D shape generator (Hunyuan3D-2, on the graphics card) --------------------------------
SHAPE_MARK = "@@SHAPE@@"
SHAPE_NEED_MB = {"single": 7000, "multi": 9000}   # rough graphics memory a shape job needs
SHAPE_PY = r'''# StellAI photo-to-3D: turns one photo (or front/left/back photos) into a rough 3D mesh with Tencent's open
# Hunyuan3D-2 models, on this computer's graphics card. Written by stellai_hub.py: edits here are overwritten.
#   python stellai_shape.py --out shape.glb --image photo.jpg
#   python stellai_shape.py --out shape.glb --front f.jpg --left l.jpg --back b.jpg --right r.jpg
#   python stellai_shape.py --selftest            (downloads the model the first time and makes a test shape)
import argparse
import json
import os
import sys
import time


def say(**kw):
    print("@@SHAPE@@" + json.dumps(kw), flush=True)


def load(path, remove_bg):
    from PIL import Image
    img = Image.open(path)
    if img.mode == "RGBA" and img.getextrema()[3][0] < 250:
        return img            # already cut out
    img = img.convert("RGB")
    if remove_bg:
        from hy3dgen.rembg import BackgroundRemover
        img = BackgroundRemover()(img)
    return img


def tidy(mesh, faces):
    """Removes floating bits and broken faces, then cuts the face count. Each step is optional."""
    try:
        from hy3dgen.shapegen import FloaterRemover, DegenerateFaceRemover, FaceReducer
    except ImportError:
        return mesh, ["mesh clean-up tools not available in this Hunyuan3D version"]
    notes = []
    for name, fn in (("floaters", lambda m: FloaterRemover()(m)), ("degenerate faces", lambda m: DegenerateFaceRemover()(m)),
                     ("face reduction", lambda m: FaceReducer()(m, max_facenum=faces))):
        try:
            mesh = fn(mesh)
        except Exception as e:      # keep the unreduced mesh rather than fail the whole job
            notes.append(f"skipped {name}: {e}")
    return mesh, notes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="")
    p.add_argument("--image", default="")
    p.add_argument("--front", default="")
    p.add_argument("--left", default="")
    p.add_argument("--back", default="")
    p.add_argument("--right", default="")
    p.add_argument("--steps", type=int, default=30)
    p.add_argument("--octree", type=int, default=256)
    p.add_argument("--faces", type=int, default=40000)
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--keep-background", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    started = time.time()
    import torch
    if not torch.cuda.is_available():
        say(ok=False, error="PyTorch can't see an NVIDIA graphics card")
        sys.exit(2)
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
    if a.selftest:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((96, 96, 416, 416), fill=(200, 120, 60, 255))
        a.out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest.glb")
        a.steps, a.octree = 5, 128
        views = None
        image = img
    else:
        views = {k: getattr(a, k) for k in ("front", "left", "back", "right") if getattr(a, k)}
        if not views and not a.image:
            say(ok=False, error="no picture given")
            sys.exit(2)
        if views and "front" not in views:
            say(ok=False, error="multi-view needs at least a front picture")
            sys.exit(2)
        if not views:
            image = load(a.image, not a.keep_background)
            views = None
        else:
            image = {k: load(v, not a.keep_background) for k, v in views.items()}
    multi = isinstance(image, dict)
    repo, sub = ("tencent/Hunyuan3D-2mv", "hunyuan3d-dit-v2-mv") if multi else \
                ("tencent/Hunyuan3D-2mini", "hunyuan3d-dit-v2-mini")
    say(stage="loading", model=repo)
    pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(repo, subfolder=sub, use_safetensors=True,
                                                            device="cuda")
    say(stage="shaping")
    gen = torch.manual_seed(a.seed)
    mesh = pipe(image=image, num_inference_steps=a.steps, octree_resolution=a.octree, num_chunks=20000,
                generator=gen, output_type="trimesh")[0]
    mesh, notes = tidy(mesh, a.faces)
    mesh.export(a.out)
    say(ok=True, out=a.out, faces=int(len(mesh.faces)), verts=int(len(mesh.vertices)), model=repo,
        seconds=round(time.time() - started), notes=notes,
        vram_peak_mb=round(torch.cuda.max_memory_allocated() / 2**20))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        say(ok=False, error=f"{type(e).__name__}: {e}"[:1500])
        sys.exit(1)
'''


def _shape_config():
    try:
        with open(os.path.join(BLENDER["root"], "generator.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    py = os.environ.get("STELLAI_SHAPE_PYTHON") or cfg.get("python") or ""
    if not py:
        guess = os.path.expanduser(os.path.join("~", "llm", "shapegen", "venv", "bin", "python"))
        py = guess if os.path.isfile(guess) else ""
    return {"python": py if py and os.path.isfile(py) else "", "gpu": cfg.get("gpu", "auto")}


def _shape_script():
    d = os.path.join(BLENDER["root"], ".worker")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "stellai_shape.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(SHAPE_PY)
    return path


def _gpus():
    """[(index, free MB, total MB, name)] from nvidia-smi, or []."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    try:
        out = subprocess.run([smi, "--query-gpu=index,memory.free,memory.total,name", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=0x08000000 if os.name == "nt" else 0).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        try:
            gpus.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3] if len(parts) > 3 else "GPU"))
        except (ValueError, IndexError):
            pass
    return gpus


def _llm_dir():
    return os.path.dirname(SERVER_CONTROL.conf) if SERVER_CONTROL else ""


def _can_pause_model():
    d = _llm_dir()
    return bool(d) and os.path.exists(os.path.join(d, ".launcher-can-pause"))


def _pause_model():
    """Stops Stella's model so its graphics memory is free; the Linux launcher waits until .pause-model goes."""
    d = _llm_dir()
    open(os.path.join(d, ".pause-model"), "w").close()
    try:
        with open(SERVER_CONTROL.pid_file) as f:
            os.kill(int(f.read().strip()), 15)
    except (OSError, ValueError):
        pass
    log("3D: Stella's model paused to free the graphics card for a shape job")


def _resume_model():
    try:
        os.remove(os.path.join(_llm_dir(), ".pause-model"))
        log("3D: shape job done, Stella's model is loading again")
    except OSError:
        pass


def _wait_model(timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        try:
            _llama("/health", timeout=5)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(3)
    return False


def _pick_gpu(need_mb):
    """(gpu index, paused?) for a shape job, pausing Stella's model when that's the only way to fit."""
    gpus = _gpus()
    if not gpus:
        raise RuntimeError("the shape generator needs an NVIDIA graphics card, and nvidia-smi doesn't see one")
    want = _shape_config()["gpu"]
    pool = [g for g in gpus if str(g[0]) == str(want)] if str(want) != "auto" else gpus
    if not pool:
        raise RuntimeError(f"graphics card {want} (from generator.json) isn't there; cards: {[g[0] for g in gpus]}")
    best = max(pool, key=lambda g: g[1])
    if best[1] >= need_mb:
        return best[0], False
    if max(g[2] for g in pool) < need_mb:
        raise RuntimeError(f"the shape generator needs about {need_mb // 1000} GB of graphics memory; the biggest card "
                           f"here has {max(g[2] for g in pool) // 1000} GB")
    if not _can_pause_model():
        raise RuntimeError(f"not enough free graphics memory (about {need_mb // 1000} GB needed, {best[1] // 1000} GB "
                           "free) and StellAI can't pause Stella's model on this setup. Wait until her model sleeps, "
                           "add a second graphics card, or (Linux) re-run stellai-setup.sh to update the launcher.")
    _pause_model()
    end = time.time() + 90
    while time.time() < end:
        time.sleep(3)
        free = {g[0]: g[1] for g in _gpus()}
        cand = max((i for i in free if i in {g[0] for g in pool}), key=lambda i: free[i])
        if free[cand] >= need_mb:
            return cand, True
    _resume_model()
    raise RuntimeError("paused Stella's model, but the graphics memory didn't free up; something else is using it")


def _run_shape(files, views, faces, detail):
    """Runs the generator; returns (glb path, info). views: {"front": path, ...} or None for one picture."""
    cfg = _shape_config()
    if not cfg["python"]:
        raise RuntimeError("the photo-to-3D generator isn't installed. Linux: re-run stellai-setup.sh and say yes to "
                           "'photo-to-3D'. It needs an NVIDIA card with 8 GB or more.")
    out = os.path.join(BLENDER["root"], ".worker", f"shape-{uuid.uuid4().hex[:8]}.glb")
    cmd = [cfg["python"], _shape_script(), "--out", out, "--faces", str(faces),
           "--octree", "384" if detail == "high" else "256", "--steps", "50" if detail == "high" else "30"]
    if views:
        for k, v in views.items():
            cmd += [f"--{k}", v]
    else:
        cmd += ["--image", files[0]]
    gpu, paused = _pick_gpu(SHAPE_NEED_MB["multi" if views else "single"])
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu), PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
    lines = []
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=25 * 60, env=env,
                           cwd=os.path.join(BLENDER["root"], ".worker"), creationflags=0x08000000 if os.name == "nt" else 0)
        lines = [l for l in (p.stdout + "\n" + p.stderr).splitlines()]
    except subprocess.TimeoutExpired:
        raise RuntimeError("the shape generator took longer than 25 minutes, so StellAI stopped it")
    finally:
        if paused:
            _resume_model()
    msgs = []
    for l in lines:
        if l.startswith(SHAPE_MARK):
            try:
                msgs.append(json.loads(l[len(SHAPE_MARK):]))
            except ValueError:
                pass
    final = next((m for m in reversed(msgs) if "ok" in m), None)
    if not final or not final.get("ok") or not os.path.isfile(out):
        tail = " / ".join(l.strip() for l in lines[-4:] if l.strip())[:600]
        raise RuntimeError("the shape generator failed: " + ((final or {}).get("error") or tail or "no output"))
    return out, dict(final, gpu=gpu, paused_stella=paused)


def tool_blender_generate(args):
    name = _ws(args.get("workspace"))
    faces = max(2000, min(200000, int(args.get("faces") or 40000)))
    detail = "high" if str(args.get("detail", "")).lower() == "high" else "normal"
    views = None
    if args.get("photos"):
        files = _photo_files(args.get("photos"))
        labels = args.get("labels") or []
        if isinstance(labels, str):
            labels = [l for l in re.split(r"[,\s]+", labels) if l]
        labelled = {str(l).lower(): f for l, f in zip(labels, files) if str(l).lower() in ("front", "left", "back", "right")}
    else:
        refs = _ref_images(name, limit=8)
        if not refs:
            raise ValueError("no pictures: pass photos (ids from the chat, or 'latest'), or add reference pictures "
                             "first with blender_project add_reference")
        files = [p for _, p in refs]
        labelled = {l: p for l, p in refs if l in ("front", "left", "back", "right")}
    if "front" in labelled and len(labelled) > 1:
        views = labelled
    started = time.time()
    glb, info = _run_shape(files, views, faces, detail)
    with BLENDER["lock"]:
        base = _ws_latest(name)
        blender_call("open", timeout=180, path=_ws_file(name, base))
        imp = blender_call("import", timeout=300, path=glb, keep=True, rename=str(args.get("object") or "Generated"))
        v = _ws_save_new(name, f"generated a shape from {'%d views' % len(views) if views else 'one picture'}")
        rep = blender_call("report", timeout=180, full=True, names=imp.get("added") or [])
        try:
            img, rinfo = _blender_render(focus=(imp.get("added") or [""])[0], reopen=_ws_file(name, v))
        except RuntimeError as e:
            img, rinfo = None, {"error": str(e)}
    try:
        os.remove(glb)
    except OSError:
        pass
    added = [o for o in rep.get("objects", []) if o["name"] in (imp.get("added") or [])]
    res = {"version": v, "previous": base, "new_objects": [_short(o) for o in added],
           "new_problems": {k: val for k, val in (rep.get("issues") or {}).items() if k in (imp.get("added") or [])},
           "generator": {k: info.get(k) for k in ("model", "faces", "seconds", "gpu", "paused_stella", "notes")},
           "minutes": round((time.time() - started) / 60, 1),
           "next": "The shape is rough and unscaled, sits at the origin and may face the wrong way. Next: rotate it so "
                   "its front faces -Y, scale it from a real measurement (ask the user), then clean up and add "
                   "materials step by step."}
    if img:
        res.update(image=img, title=f"{name} v{v}", views=rinfo.get("layout"))
        if info.get("paused_stella"):
            _wait_model()
        res["visual_check"] = _vision_check(img, "a new shape generated from the reference pictures; judge how well "
                                                 "its overall form matches them (scale and colour don't matter yet)",
                                            res["new_objects"], rinfo.get("layout", ""), name)
    else:
        res["render_error"] = rinfo.get("error")
    return res, f"Generated a shape in {name} (v{v})"


BLENDER_STEP_DESC = (
    "3D Modeler: run ONE small change on the current model as a Blender Python (bpy) script. The server opens the "
    "newest version, runs the code, saves it as a new numbered version, renders it, measures what changed and "
    "has the vision add-on check the pictures against goal. If the code fails or changes nothing, nothing is saved. "
    "Metres, Z up, front faces -Y. Use bpy.data and bmesh (bmesh.ops.create_cube/create_uvsphere/create_cone, "
    "bmesh.ops.create_circle...), not edit-mode bpy.ops; no files, imports other than bpy/bmesh/mathutils/math/"
    "random/numpy, or bpy.ops.wm/render/export. Ready to use: obj(name), bbox(o) -> (min, max) world, "
    "new_object(name, bm), material(name, color, emission=None, strength=1), assign(o, mat), "
    "bone_world(armature, bone) -> (head, tail), parent_to_bone(o, armature, bone), apply_transform(o), "
    "Vector, Matrix, Euler, radians, scene. print() output comes back to you.")

SERVER_TOOLS["blender_project"] = (tool_blender_project, _fn(
    "blender_project",
    "3D Modeler workspaces (a model with numbered versions). status: Blender, workspaces, importable files. "
    "new: start a workspace from an empty scene or an importable file. open: switch workspace. history: versions "
    "and what each changed. revert: go back to a version (saved as a new one). export: write glb/fbx/obj/stl/"
    "vrm/blend of the newest version to the workspace's exports folder. add_reference: keep chat photos as "
    "reference pictures (labels front/left/back/right/top/detail); every visual check then compares with them. "
    "references / clear_references: list or remove them.",
    {"action": _p("status, new, open, history, revert or export"),
     "name": _p("Workspace name: lowercase, digits, dashes (default: the active one)"),
     "file": _p("For new: a file from status's files_you_can_import"),
     "version": _p("For revert: the version number", "integer"),
     "format": _p("For export: glb (default), fbx, obj, stl, vrm or blend"),
     "photos": {"type": "array", "items": {"type": "string"},
                "description": "For add_reference: photo ids from [photo ...] in the chat, or 'latest'"},
     "labels": {"type": "array", "items": {"type": "string"},
                "description": "For add_reference: the view of each photo: front, left, back, right, top or detail"}},
    ["action"]))
SERVER_TOOLS["blender_step"] = (tool_blender_step, _fn(
    "blender_step", BLENDER_STEP_DESC,
    {"goal": _p("What this step should achieve, with numbers, e.g. 'add a torus halo, 0.32 m wide, 0.06 m above "
                "the head bone, tilted 8° on X, lime emissive material'"),
     "code": _p("The bpy script"),
     "views": {"type": "array", "items": {"type": "string"},
               "description": "Render angles: front, right, back, left, top, three_quarter (default 4 of them)"},
     "focus": _p("Optional object name to frame the renders on"),
     "look": _p("solid (default, fast and clear) or material (EEVEE, slower)"),
     "check": _p("false skips the visual check (faster)", "boolean"),
     "workspace": _p("Default: the active workspace")}, ["goal", "code"]))
SERVER_TOOLS["blender_generate"] = (tool_blender_generate, _fn(
    "blender_generate",
    "3D Modeler: make a new rough 3D shape from pictures with the local photo-to-3D generator (Hunyuan3D-2) and add "
    "it to the current model as a new version. Uses the workspace's reference pictures unless photos are given; a "
    "front picture plus left/back/right views gives a better shape than one picture. Slow (several minutes; on a "
    "single graphics card Stella's model is paused meanwhile). The result is one unrigged, untextured, unscaled "
    "mesh: scale, orient and clean it up afterwards with blender_step. Best for solid objects and props; rough "
    "for characters; poor for whole rooms.",
    {"photos": {"type": "array", "items": {"type": "string"},
                "description": "Optional photo ids from [photo ...] in the chat, or 'latest'"},
     "labels": {"type": "array", "items": {"type": "string"},
                "description": "Optional view of each photo: front, left, back or right"},
     "faces": _p("Face count to reduce to (default 40000)", "integer"),
     "detail": _p("normal (default) or high (slower)"),
     "object": _p("Name for the new object (default Generated)"),
     "workspace": _p("Default: the active workspace")}, []))
SERVER_TOOLS["blender_inspect"] = (tool_blender_inspect, _fn(
    "blender_inspect",
    "3D Modeler: read the current model without changing it: objects with positions, sizes, rotations, parents, "
    "vertex/triangle counts, materials, bones and mesh problems. render=true also shows pictures. Do this before "
    "the first change and whenever you're unsure of names or sizes.",
    {"focus": _p("Optional object name for details and framing"),
     "render": _p("true to render pictures too", "boolean"),
     "views": {"type": "array", "items": {"type": "string"},
               "description": "front, right, back, left, top, three_quarter"},
     "look": _p("solid (default) or material"),
     "compare": _p("With render and reference pictures: false skips the comparison", "boolean"),
     "workspace": _p("Default: the active workspace")}, []))


# ---- Versioned workspaces for the CAD and PCB designers ----------------------------------------------
class Workspaces:
    """Numbered versions of one design: a file per version (CAD) or a folder per version (a KiCad project).
    Lives next to the 3D Modeler's folders, under the same root."""

    def __init__(self, kind, ext, label):
        self.kind, self.ext, self.label = kind, ext, label      # ext "" = one folder per version

    def root(self, *parts):
        return os.path.join(BLENDER["root"], self.kind, *parts)

    def name(self, name):
        name = str(name or "").strip().lower()
        if not name:
            try:
                with open(self.root("active.txt"), encoding="utf-8") as f:
                    name = f.read().strip()
            except OSError:
                name = ""
            if not name:
                raise ValueError(f"no {self.label} workspace yet: start one with action new")
        if not WS_NAME.fullmatch(name):
            raise ValueError("workspace names use lowercase letters, digits and dashes, e.g. fan-duct")
        return name

    def dir(self, name, *parts):
        return self.root("workspaces", name, *parts)

    def versions(self, name):
        d = self.dir(name, "versions")
        pat = re.compile(r"v(\d{4,})" + (re.escape(self.ext) if self.ext else ""))
        out = []
        for f in os.listdir(d) if os.path.isdir(d) else []:
            m = pat.fullmatch(f)
            if m and (self.ext or os.path.isdir(os.path.join(d, f))):
                out.append(int(m.group(1)))
        return sorted(out)

    def path(self, name, v):
        return self.dir(name, "versions", f"v{v:04d}{self.ext}")

    def latest(self, name):
        vs = self.versions(name)
        if not vs:
            raise ValueError(f"workspace {name} has no saved versions")
        return vs[-1]

    def next(self, name):
        v = (self.versions(name) or [0])[-1] + 1
        return v, self.path(name, v)

    def activate(self, name):
        os.makedirs(self.root(), exist_ok=True)
        with open(self.root("active.txt"), "w", encoding="utf-8") as f:
            f.write(name)

    def history(self, name):
        try:
            with open(self.dir(name, "history.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def record(self, name, v, what):
        hist = self.history(name) + [{"version": v, "what": str(what)[:300], "at": int(time.time())}]
        with open(self.dir(name, "history.json"), "w", encoding="utf-8") as f:
            json.dump(hist[-500:], f, indent=1)
        keep = set(self.versions(name)[-BLENDER_KEEP:]) | {1}
        for old in self.versions(name):
            if old not in keep:
                p = self.path(name, old)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else _quiet_remove(p)

    def list(self):
        d = self.root("workspaces")
        names = sorted(os.listdir(d)) if os.path.isdir(d) else []
        return [{"name": n, "latest_version": self.versions(n)[-1]}
                for n in names if WS_NAME.fullmatch(n) and self.versions(n)]

    def start(self, name):
        name = str(name or "").strip().lower()
        if not WS_NAME.fullmatch(name):
            raise ValueError("give a name: lowercase letters, digits and dashes, e.g. fan-duct")
        if self.versions(name):
            raise ValueError(f"workspace {name} already exists; use action open, or pick another name")
        for sub in ("versions", "exports", "refs"):
            os.makedirs(self.dir(name, sub), exist_ok=True)
        return name

    # reference pictures, the same as the 3D Modeler's
    def refs(self, name):
        try:
            with open(self.dir(name, "refs", "refs.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def ref_images(self, name, limit=3):
        refs = [r for r in self.refs(name) if os.path.isfile(self.dir(name, "refs", r["file"]))]
        order = {l: i for i, l in enumerate(REF_LABELS)}
        refs.sort(key=lambda r: order.get(r.get("label") or "", 99))
        return [(r.get("label") or "reference", self.dir(name, "refs", r["file"])) for r in refs[:limit]]

    def refs_action(self, action, args):
        name = self.name(args.get("name"))
        refs = self.refs(name)
        os.makedirs(self.dir(name, "refs"), exist_ok=True)
        if action == "references":
            return {"workspace": name, "references": refs}, f"{len(refs)} reference picture(s)"
        if action == "clear_references":
            for r in refs:
                _quiet_remove(self.dir(name, "refs", r["file"]))
            refs = []
        else:
            labels = args.get("labels") or []
            if isinstance(labels, str):
                labels = [l for l in re.split(r"[,\s]+", labels) if l]
            for i, src in enumerate(_photo_files(args.get("photos") or "latest")):
                label = str(labels[i]).lower() if i < len(labels) else ""
                if label and label not in REF_LABELS:
                    raise ValueError(f"labels are {', '.join(REF_LABELS)}")
                if label:
                    refs = [r for r in refs if r.get("label") != label]
                fname = f"ref-{uuid.uuid4().hex[:8]}.jpg"
                shutil.copyfile(src, self.dir(name, "refs", fname))
                refs.append({"file": fname, "label": label, "photo": os.path.basename(src)[:-4], "at": int(time.time())})
        with open(self.dir(name, "refs", "refs.json"), "w", encoding="utf-8") as f:
            json.dump(refs[-8:], f, indent=1)
        return {"workspace": name, "references": refs[-8:]}, f"{len(refs[-8:])} reference picture(s)"


def _quiet_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _pick_inbox_file(src, exts):
    """A file (or folder) the user dropped into the 3D inbox, by name."""
    src = os.path.basename(str(src or "").strip())
    path = os.path.join(_inbox(), src)
    if not src or not os.path.exists(path):
        have = [f for f in sorted(os.listdir(_inbox())) if f.rsplit(".", 1)[-1].lower() in exts
                or os.path.isdir(os.path.join(_inbox(), f))]
        raise ValueError(f"no {src!r} in the 3D inbox ({_inbox()}); there: {have[:30]}")
    return path


def _run_marked(cmd, mark, job, timeout, env_extra=None, cwd=None):
    """Runs a runner script with a JSON job; returns its JSON answer. Raises RuntimeError if it never answers."""
    wdir = os.path.join(BLENDER["root"], ".worker")
    os.makedirs(wdir, exist_ok=True)
    jpath = os.path.join(wdir, f"job-{uuid.uuid4().hex[:8]}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(job, f)
    env = dict(os.environ, STELLAI_JOB=jpath, **(env_extra or {}))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=env, cwd=cwd or wdir, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the step took longer than {timeout} s, so StellAI stopped it. Nothing was saved.")
    finally:
        _quiet_remove(jpath)
    lines = (p.stdout or "").splitlines() + (p.stderr or "").splitlines()
    for l in reversed(lines):
        if l.startswith(mark):
            try:
                return json.loads(l[len(mark):])
            except ValueError:
                break
    tail = " / ".join(l.strip() for l in lines[-5:] if l.strip())[:700]
    raise RuntimeError("the program stopped without an answer" + (f": {tail}" if tail else ""))


def _diff_by(before, after, key, fields):
    b = {o[key]: o for o in before}
    a = {o[key]: o for o in after}
    changed = []
    for k in a:
        if k in b:
            d = {f: {"from": b[k].get(f), "to": a[k].get(f)} for f in fields if b[k].get(f) != a[k].get(f)}
            if d:
                changed.append({key: k, **d})
    return {"added": [a[k] for k in a if k not in b][:20], "removed": [k for k in b if k not in a][:20],
            "changed": changed[:25]}


def _check_and_render_stl(stl, goal, facts, refs, views=None, look="solid"):
    """Renders an exported STL with the server's Blender and runs the visual check. (image, info, check text)."""
    if not find_blender():
        return None, {"note": "no pictures: Blender isn't installed on the server (measurements only)"}, None
    with BLENDER["lock"]:
        blender_call("import", timeout=300, path=stl)
        img, info = _blender_render(views, "", look)
    check = _vision_check(img, goal, facts, info.get("layout", ""), refs=refs) if goal else None
    return img, info, check


# ---- CAD Designer: FreeCAD ----------------------------------------------------------------------------
CAD = Workspaces("cad", ".FCStd", "CAD")
CAD_MARK = "@@CAD@@"
CAD_RUNNER_PY = r'''# StellAI CAD runner: one step inside headless FreeCAD (freecadcmd this_file). The job comes as JSON in the
# file named by STELLAI_JOB. Written by stellai_hub.py: edits here are overwritten.
import contextlib
import io
import json
import math
import os
import sys
import traceback

import FreeCAD as App
import Part

MARK = "@@CAD@@"
OUT = sys.stdout


def rnd(v, n=3):
    return [round(float(x), n) for x in v]


def shaped(doc):
    return [o for o in doc.Objects if hasattr(o, "Shape") and not o.Shape.isNull()]


def report(doc, full=True):
    objs, issues = [], {}
    lo, hi = None, None
    for o in shaped(doc):
        s = o.Shape
        bb = s.BoundBox
        used = [p.Name for p in o.InList]
        e = {"name": o.Name, "label": o.Label, "type": o.TypeId, "size_mm": rnd((bb.XLength, bb.YLength, bb.ZLength)),
             "min": rnd((bb.XMin, bb.YMin, bb.ZMin)), "max": rnd((bb.XMax, bb.YMax, bb.ZMax)),
             "solids": len(s.Solids), "faces": len(s.Faces)}
        if used:
            e["used_by"] = used[:5]     # an input to another feature, not a final part
        try:
            e["volume_mm3"] = round(s.Volume, 2)
        except Exception:
            pass
        if not getattr(o, "Visibility", True):
            e["hidden"] = True
        if full and not used:
            found = []
            try:
                if not s.isValid():
                    found.append("shape is not valid (bad geometry)")
            except Exception:
                pass
            if not s.Solids:
                found.append("no solid (only faces or edges): not printable or exportable as a part")
            elif any(not sol.isClosed() for sol in s.Solids):
                found.append("a solid isn't closed")
            if len(s.Solids) > 1:
                found.append(f"{len(s.Solids)} separate solids (fuse them if they're meant to be one part)")
            if found:
                issues[o.Name] = found
        if not used and getattr(o, "Visibility", True):
            lo = rnd((bb.XMin, bb.YMin, bb.ZMin)) if lo is None else [min(a, b) for a, b in zip(lo, (bb.XMin, bb.YMin, bb.ZMin))]
            hi = rnd((bb.XMax, bb.YMax, bb.ZMax)) if hi is None else [max(a, b) for a, b in zip(hi, (bb.XMax, bb.YMax, bb.ZMax))]
        objs.append(e)
    return {"objects": objs, "issues": issues,
            "size_mm": rnd([b - a for a, b in zip(lo, hi)]) if lo else None, "min": lo, "max": hi}


def finals(doc):
    """The parts: shaped, visible, and not an input to another feature."""
    return [o for o in shaped(doc) if not o.InList and getattr(o, "Visibility", True)]


# ---- helpers for step code (millimetres) ----------------------------------------------------------
def namespace(doc):
    V = App.Vector

    def get(name):
        o = doc.getObject(name) or next((x for x in doc.Objects if x.Label == name), None)
        if o is None:
            raise KeyError(f"no object {name!r}; objects: {[x.Name for x in doc.Objects][:30]}")
        return o

    def add(name, shape):
        """Puts a shape in the document as a part called name (replacing one with that name)."""
        old = doc.getObject(name)
        if old is not None:
            doc.removeObject(old.Name)
        o = doc.addObject("Part::Feature", name)
        o.Shape = shape
        return o

    def remove(name):
        doc.removeObject(get(name).Name)

    def box(x, y, z, at=(0, 0, 0)):
        """A box x by y by z mm with its corner at `at`."""
        return Part.makeBox(x, y, z, V(*at))

    def cylinder(r, h, at=(0, 0, 0), axis=(0, 0, 1)):
        return Part.makeCylinder(r, h, V(*at), V(*axis))

    def hole(d, depth, at, axis=(0, 0, -1)):
        """A cylinder to cut a hole of diameter d from point `at` along axis."""
        return Part.makeCylinder(d / 2, depth, V(*at), V(*axis))

    def fillet(shape, r, edges=None):
        return shape.makeFillet(r, edges or shape.Edges)

    def chamfer(shape, d, edges=None):
        return shape.makeChamfer(d, edges or shape.Edges)

    def hide(name):
        get(name).Visibility = False

    return {"App": App, "FreeCAD": App, "Part": Part, "Vector": V, "Placement": App.Placement,
            "Rotation": App.Rotation, "doc": doc, "math": math, "get": get, "add": add, "remove": remove,
            "box": box, "cylinder": cylinder, "hole": hole, "fillet": fillet, "chamfer": chamfer, "hide": hide,
            "__name__": "stellai_step"}


def export(doc, path, fmt):
    parts = finals(doc)
    if not parts:
        raise ValueError("nothing to export: no visible solid parts")
    if fmt == "stl":
        import Mesh
        import MeshPart
        meshes = [MeshPart.meshFromShape(Shape=o.Shape, LinearDeflection=0.05, AngularDeflection=0.3) for o in parts]
        m = meshes[0]
        for extra in meshes[1:]:
            m.addMesh(extra)
        m.write(path)
    elif fmt in ("step", "stp"):
        import Import
        Import.export(parts, path)
    elif fmt == "brep":
        Part.makeCompound([o.Shape for o in parts]).exportBrep(path)
    elif fmt in ("iges", "igs"):
        Part.makeCompound([o.Shape for o in parts]).exportIges(path)
    else:
        raise ValueError("format must be stl, step, brep or iges")


def main():
    with open(os.environ["STELLAI_JOB"], encoding="utf-8") as f:
        job = json.load(f)
    out = {"ok": False}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            doc = App.openDocument(job["open"]) if job.get("open") else App.newDocument("Model")
            if job.get("import"):
                src = job["import"]
                ext = src.rsplit(".", 1)[-1].lower()
                if ext in ("step", "stp", "iges", "igs"):
                    import Import
                    Import.insert(src, doc.Name)
                elif ext == "brep":
                    o = doc.addObject("Part::Feature", "Imported")
                    o.Shape = Part.read(src)
                elif ext == "stl":     # a mesh: turned into a solid so it can be cut, measured and exported
                    import Mesh
                    mesh = Mesh.Mesh(src)
                    shell = Part.Shape()
                    shell.makeShapeFromMesh(mesh.Topology, 0.05)
                    solid = Part.makeSolid(shell)
                    o = doc.addObject("Part::Feature", "Imported")
                    o.Shape = solid.removeSplitter() if solid.isValid() else shell
                else:
                    raise ValueError(f"can't import .{ext} files")
                doc.recompute()
            if job.get("code"):
                out["before"] = report(doc, full=False)
                code = compile(job["code"], "<step>", "exec")
                exec(code, namespace(doc))
            doc.recompute()
            bad = [o.Name for o in doc.Objects if "Invalid" in o.State or "Error" in o.State]
            out["after"] = report(doc, full=True)
            if bad:
                out["after"]["recompute_errors"] = bad
            if job.get("save"):
                doc.saveAs(job["save"])
            for path, fmt in job.get("exports", []):
                export(doc, path, fmt)
        out["ok"] = True
    except Exception as e:
        tb = traceback.extract_tb(sys.exc_info()[2])
        out.update(error=f"{type(e).__name__}: {e}"[:1500],
                   where=[f"line {f.lineno}: {(f.line or '').strip()}" for f in tb if f.filename == "<step>"][-3:])
    out["output"] = buf.getvalue()[-3000:]
    OUT.write(MARK + json.dumps(out) + "\n")
    OUT.flush()


main()
os._exit(0)
'''
CAD_IMPORTS = {"FreeCAD", "Part", "Sketcher", "PartDesign", "Draft", "Mesh", "MeshPart", "math", "random",
               "itertools", "functools", "collections", "statistics", "numpy"}
CAD_BAD_ATTRS = {"openDocument", "newDocument", "closeDocument", "loadFile", "exportStep", "exportStl", "exportBrep",
                 "exportIges", "read", "export", "insert", "addModule", "runCommand", "open", "importStep", "setActiveDocument"}
CAD_FILE_EXTS = ("fcstd", "step", "stp", "stl", "iges", "igs", "brep")


def find_freecad():
    """The command that runs headless FreeCAD, as a list, or []."""
    env = os.environ.get("STELLAI_FREECAD", "")
    if env and os.path.isfile(env):
        return [env]
    appimage = os.path.expanduser(os.path.join("~", "llm", "freecad", "FreeCAD.AppImage"))
    if os.path.isfile(appimage) and os.access(appimage, os.X_OK):
        return [appimage, "freecadcmd"]
    for n in ("freecadcmd", "FreeCADCmd", "freecad.cmd"):
        w = shutil.which(n)
        if w:
            return [w]
    if os.name == "nt":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            for exe in ("FreeCADCmd.exe", "freecadcmd.exe"):
                found = sorted(glob.glob(os.path.join(root, "FreeCAD*", "bin", exe)), reverse=True)
                if found:
                    return [found[0]]
    for c in ("/usr/bin/freecadcmd", "/usr/bin/FreeCADCmd", "/snap/bin/freecad.cmd"):
        if os.path.isfile(c):
            return [c]
    return []


def _cad(job, timeout=300):
    cmd = find_freecad()
    if not cmd:
        raise RuntimeError("FreeCAD isn't installed on the server. Linux: re-run stellai-setup.sh and say yes to "
                           "FreeCAD. Windows: install it from freecad.org. Then try again.")
    script = os.path.join(BLENDER["root"], ".worker", "stellai_cad.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write(CAD_RUNNER_PY)
    return _run_marked(cmd + [script], CAD_MARK, job, timeout)


def _cad_summary(rep):
    return {"parts": [o for o in rep.get("objects", []) if not o.get("used_by")][:40],
            "inputs_used_by_other_features": len([o for o in rep.get("objects", []) if o.get("used_by")]),
            "overall_size_mm": rep.get("size_mm"), "min": rep.get("min"), "max": rep.get("max"),
            "problems": rep.get("issues") or {}, "recompute_errors": rep.get("recompute_errors")}


def tool_cad_project(args):
    action = str(args.get("action", "status")).strip().lower()
    if action == "status":
        return {"freecad": " ".join(find_freecad()) or "not installed", "blender_for_pictures": bool(find_blender()),
                "inbox_folder": _inbox(),
                "files_you_can_import": [f for f in sorted(os.listdir(_inbox()))
                                         if f.rsplit(".", 1)[-1].lower() in CAD_FILE_EXTS][:40],
                "workspaces": CAD.list(), "active": _safe(lambda: CAD.name(""))}, "Checked CAD workspaces"
    if action == "new":
        name = CAD.start(args.get("name"))
        v, path = CAD.next(name)
        job = {"save": path}
        if args.get("file"):
            src = _pick_inbox_file(args.get("file"), CAD_FILE_EXTS)
            if src.lower().endswith(".fcstd"):
                job["open"] = src
            else:
                job["import"] = src
        res = _cad(job)
        if not res.get("ok"):
            shutil.rmtree(CAD.dir(name), ignore_errors=True)
            raise RuntimeError(res.get("error") or "FreeCAD couldn't start the workspace")
        CAD.record(name, v, f"start: {args.get('file') or 'empty'}")
        CAD.activate(name)
        return {"workspace": name, "version": v, "design": _cad_summary(res.get("after", {}))}, \
            f"Started CAD workspace {name} (v{v})"
    if action in ("add_reference", "references", "clear_references"):
        return CAD.refs_action(action, args)
    name = CAD.name(args.get("name"))
    if action == "open":
        CAD.activate(name)
        return {"workspace": name, "latest_version": CAD.latest(name), "recent": CAD.history(name)[-8:]}, f"Opened {name}"
    if action == "history":
        return {"workspace": name, "versions_on_disk": CAD.versions(name), "history": CAD.history(name)[-25:]}, \
            f"History of {name}"
    if action == "revert":
        target = _int_version(args.get("version"))
        if target not in CAD.versions(name):
            raise ValueError(f"version {target} isn't on disk; available: {CAD.versions(name)[-15:]}")
        v, path = CAD.next(name)
        shutil.copyfile(CAD.path(name, target), path)
        CAD.record(name, v, f"back to v{target}")
        return {"workspace": name, "version": v, "same_as": target}, f"Went back to v{target} (saved as v{v})"
    if action == "export":
        fmt = str(args.get("format", "step")).lower().strip(". ")
        v = CAD.latest(name)
        if fmt == "blender":     # hand the part to the 3D Modeler
            out = os.path.join(_inbox(), f"{name}-v{v:04d}.stl")
            fmt_real = "stl"
        else:
            if fmt not in ("step", "stl", "fcstd", "brep", "iges"):
                raise ValueError("format must be step, stl, fcstd, brep, iges or blender")
            os.makedirs(CAD.dir(name, "exports"), exist_ok=True)
            out = CAD.dir(name, "exports", f"{name}-v{v:04d}.{fmt}")
            fmt_real = fmt
        if fmt_real == "fcstd":
            shutil.copyfile(CAD.path(name, v), out)
        else:
            res = _cad({"open": CAD.path(name, v), "exports": [[out, fmt_real]]})
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "export failed")
        note = ("It's in the 3D inbox now: in the 3D Modeler, start a workspace with this file."
                if fmt == "blender" else "The file is in this folder on the server computer.")
        return {"workspace": name, "version": v, "file": out, "bytes": os.path.getsize(out), "note": note}, \
            f"Exported {name} v{v} as {fmt}"
    raise ValueError("action must be status, new, open, history, revert, export, add_reference, references or "
                     "clear_references")


def _safe(fn):
    try:
        return fn()
    except (ValueError, OSError):
        return None


def _int_version(v):
    try:
        return int(str(v).lstrip("vV"))
    except ValueError:
        raise ValueError("give the version number, e.g. 7")


def tool_cad_step(args):
    code, goal = str(args.get("code", "")), str(args.get("goal", "")).strip()
    if not goal or not code.strip():
        raise ValueError("give goal (with millimetres) and code")
    probs = check_bpy_code(code, allowed=CAD_IMPORTS, extra_bad=CAD_BAD_ATTRS)
    if probs:
        return {"ok": False, "error": "the code was not run: " + "; ".join(probs)}, "Step blocked"
    name = CAD.name(args.get("workspace"))
    base = CAD.latest(name)
    v, path = CAD.next(name)
    stl = os.path.join(BLENDER["root"], ".worker", f"cad-{uuid.uuid4().hex[:8]}.stl")
    res = _cad({"open": CAD.path(name, base), "code": code, "save": path, "exports": [[stl, "stl"]]},
               timeout=max(30, min(600, int(args.get("timeout") or 300))))
    if not res.get("ok"):
        _quiet_remove(path)
        _quiet_remove(stl)
        return {"ok": False, "error": res.get("error"), "where": res.get("where"), "output": res.get("output", ""),
                "note": f"Nothing was saved; the design is still v{base}."}, "Step failed"
    fields = ("label", "size_mm", "min", "volume_mm3", "solids", "faces", "hidden", "used_by")
    diff = _diff_by(res["before"]["objects"], res["after"]["objects"], "name", fields)
    if not (diff["added"] or diff["removed"] or diff["changed"]):
        _quiet_remove(path)
        _quiet_remove(stl)
        return {"ok": False, "error": "the code ran but nothing in the design changed", "output": res.get("output", ""),
                "note": f"Nothing was saved; the design is still v{base}."}, "Step changed nothing"
    CAD.record(name, v, goal)
    out = {"version": v, "previous": base, "changes": diff, "design": _cad_summary(res["after"]),
           "output": res.get("output", "")[-1500:]}
    if os.path.exists(stl):
        try:
            check = str(args.get("check", True)).lower() not in ("false", "0", "no")
            img, info, vc = _check_and_render_stl(stl, goal if check else "", diff, CAD.ref_images(name),
                                                  args.get("views") if isinstance(args.get("views"), list) else None)
            if img:
                out.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
            if vc:
                out["visual_check"] = vc
            if info.get("note"):
                out["note"] = info["note"]
        except RuntimeError as e:
            out["render_error"] = str(e)
        _quiet_remove(stl)
    return out, f"Saved {name} v{v}"


def tool_cad_inspect(args):
    name = CAD.name(args.get("workspace"))
    v = CAD.latest(name)
    want = bool(args.get("render"))
    stl = os.path.join(BLENDER["root"], ".worker", f"cad-{uuid.uuid4().hex[:8]}.stl")
    res = _cad({"open": CAD.path(name, v), "exports": [[stl, "stl"]] if want else []})
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "FreeCAD couldn't read the design")
    out = {"workspace": name, "version": v, "design": _cad_summary(res["after"])}
    if want and os.path.exists(stl):
        refs = CAD.ref_images(name)
        goal = "compare the design as it is now with the reference pictures" if refs else ""
        try:
            img, info, vc = _check_and_render_stl(stl, goal, {"size_mm": res["after"].get("size_mm")}, refs,
                                                  args.get("views") if isinstance(args.get("views"), list) else None)
            if img:
                out.update(image=img, title=f"{name} v{v}", views=info.get("layout"))
            if vc:
                out["comparison"] = vc
            if info.get("note"):
                out["note"] = info["note"]
        except RuntimeError as e:
            out["render_error"] = str(e)
        _quiet_remove(stl)
    return out, f"Inspected {name} v{v}"


# ---- PCB Designer: KiCad -------------------------------------------------------------------------------
PCB = Workspaces("pcb", "", "PCB")
PCB_MARK = "@@PCB@@"
PCB_RUNNER_PY = r'''# StellAI PCB runner: one step on a KiCad board with KiCad's Python (pcbnew). The job comes as JSON in the
# file named by STELLAI_JOB. Written by stellai_hub.py: edits here are overwritten.
import contextlib
import io
import json
import math
import os
import sys
import traceback

import pcbnew

MARK = "@@PCB@@"
OUT = sys.stdout


def mm(v):
    return round(pcbnew.ToMM(v), 3)


def report(board):
    fps = []
    for fp in board.GetFootprints():
        p = fp.GetPosition()
        fps.append({"ref": fp.GetReference(), "value": fp.GetValue(), "footprint": fp.GetFPID().GetUniStringLibId(),
                    "x_mm": mm(p.x), "y_mm": mm(p.y), "rot_deg": round(fp.GetOrientationDegrees(), 2),
                    "side": "bottom" if fp.IsFlipped() else "top"})
    tracks = vias = 0
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            vias += 1
        else:
            tracks += 1
    out = {"footprints": fps, "tracks": tracks, "vias": vias, "zones": len(list(board.Zones())),
           "nets": board.GetNetCount(), "copper_layers": board.GetCopperLayerCount()}
    try:
        bb = board.GetBoardEdgesBoundingBox()
        if bb.GetWidth() > 0:
            out["board_mm"] = [mm(bb.GetWidth()), mm(bb.GetHeight())]
            out["board_origin_mm"] = [mm(bb.GetX()), mm(bb.GetY())]
    except Exception:
        pass
    return out


def namespace(board):
    V = pcbnew.VECTOR2I
    F = pcbnew.FromMM

    def fp(ref):
        f = board.FindFootprintByReference(ref)
        if f is None:
            raise KeyError(f"no footprint {ref!r}; footprints: {[x.GetReference() for x in board.GetFootprints()][:60]}")
        return f

    def move(ref, x, y, rot=None):
        """Puts footprint ref at x, y mm (board coordinates, Y grows downwards), optionally turned to rot degrees."""
        f = fp(ref)
        f.SetPosition(V(F(x), F(y)))
        if rot is not None:
            f.SetOrientationDegrees(float(rot))

    def layer(name):
        lid = board.GetLayerID(name)
        if lid < 0:
            raise KeyError(f"no layer {name!r}")
        return lid

    def line(x1, y1, x2, y2, layer_name="Edge.Cuts", width=0.1):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(V(F(x1), F(y1)))
        s.SetEnd(V(F(x2), F(y2)))
        s.SetLayer(layer(layer_name))
        s.SetWidth(F(width))
        board.Add(s)
        return s

    def outline_rect(w, h, x=0.0, y=0.0, replace=True):
        """A w by h mm board outline with its top-left corner at x, y (replaces the old outline)."""
        if replace:
            for d in list(board.GetDrawings()):
                if d.GetLayer() == pcbnew.Edge_Cuts:
                    board.Remove(d)
        line(x, y, x + w, y)
        line(x + w, y, x + w, y + h)
        line(x + w, y + h, x, y + h)
        line(x, y + h, x, y)

    def track(x1, y1, x2, y2, width=0.25, layer_name="F.Cu", net=None):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(V(F(x1), F(y1)))
        t.SetEnd(V(F(x2), F(y2)))
        t.SetWidth(F(width))
        t.SetLayer(layer(layer_name))
        if net:
            n = board.FindNet(net)
            if n is None:
                raise KeyError(f"no net {net!r}")
            t.SetNet(n)
        board.Add(t)
        return t

    def text(s, x, y, size=1.0, layer_name="F.SilkS"):
        t = pcbnew.PCB_TEXT(board)
        t.SetText(s)
        t.SetPosition(V(F(x), F(y)))
        t.SetLayer(layer(layer_name))
        t.SetTextSize(V(F(size), F(size)))
        board.Add(t)
        return t

    return {"pcbnew": pcbnew, "board": board, "math": math, "mm": F, "to_mm": pcbnew.ToMM, "VECTOR2I": V,
            "fp": fp, "move": move, "line": line, "outline_rect": outline_rect, "track": track, "text": text,
            "__name__": "stellai_step"}


# ---- a board from a netlist (circuit as code) ------------------------------------------------------
def _sexp(text):
    """A tiny S-expression reader for KiCad netlists."""
    tokens, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "()":
            tokens.append(c)
            i += 1
        elif c.isspace():
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            tokens.append(("s", "".join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            tokens.append(("s", text[i:j]))
            i = j
    stack, cur = [], []
    for t in tokens:
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            done, cur = cur, stack.pop()
            cur.append(done)
        else:
            cur.append(t[1])
    return cur[0] if cur else []


def _find(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def _val(node, key, default=""):
    f = _find(node, key)
    return f[0][1] if f and len(f[0]) > 1 and isinstance(f[0][1], str) else default


def read_netlist(path):
    with open(path, encoding="utf-8") as f:
        tree = _sexp(f.read())
    comps = []
    for blk in _find(tree, "components"):
        for c in _find(blk, "comp"):
            comps.append({"ref": _val(c, "ref"), "value": _val(c, "value"), "footprint": _val(c, "footprint")})
    nets = []
    for blk in _find(tree, "nets"):
        for nt in _find(blk, "net"):
            nodes = [(_val(nd, "ref"), _val(nd, "pin")) for nd in _find(nt, "node")]
            nets.append({"name": _val(nt, "name"), "nodes": nodes})
    return comps, nets


def _fp_dirs(job):
    dirs = [d for d in (job.get("footprint_dirs") or []) if d and os.path.isdir(d)]
    for k in ("KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR"):
        if os.environ.get(k) and os.path.isdir(os.environ[k]):
            dirs.append(os.environ[k])
    return dirs


def _load_fp(dirs, lib_id):
    lib, _, name = lib_id.partition(":")
    for d in dirs:
        p = os.path.join(d, lib + ".pretty")
        if os.path.isdir(p):
            fp = pcbnew.FootprintLoad(p, name)
            if fp is not None:
                return fp
    raise KeyError(f"footprint {lib_id} not found in KiCad's libraries")


def _bbox(fp):
    """A footprint's outline box without its text (the call changed between KiCad versions)."""
    try:
        return fp.GetBoundingBox(False, False)
    except TypeError:
        return fp.GetBoundingBox()


def build_board(board, job):
    """Footprints from the netlist, connected, on a wide grid; then a board outline around them."""
    comps, nets = read_netlist(job["netlist"])
    dirs = _fp_dirs(job)
    V, F = pcbnew.VECTOR2I, pcbnew.FromMM
    for fp in list(board.GetFootprints()):
        board.Remove(fp)
    for t in list(board.GetTracks()):
        board.Remove(t)
    netinfo = {}
    for n in nets:
        ni = pcbnew.NETINFO_ITEM(board, n["name"])
        board.Add(ni)
        netinfo[n["name"]] = ni
    pad_net = {(r, p): n["name"] for n in nets for r, p in n["nodes"]}
    fps = []
    for c in comps:
        fp = _load_fp(dirs, c["footprint"])
        fp.SetReference(c["ref"])
        fp.SetValue(c["value"])
        board.Add(fp)
        for pad in fp.Pads():
            name = pad_net.get((c["ref"], pad.GetNumber()))
            if name:
                pad.SetNet(netinfo[name])
        fps.append(fp)
    # Wide, simple placement: connectors down the left edge, everything else on a grid in reference order,
    # with room between parts for thick tracks and hand soldering.
    gap = float(job.get("spacing_mm") or 8)

    def size(fp):
        bb = _bbox(fp)
        return pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())

    def key(fp):
        ref = fp.GetReference()
        letters = "".join(ch for ch in ref if ch.isalpha())
        num = "".join(ch for ch in ref if ch.isdigit())
        order = {"U": 0, "Q": 1, "D": 2, "SW": 3, "R": 4, "C": 5, "L": 6}
        return (order.get(letters, 7), letters, int(num or 0))

    conns = sorted([f for f in fps if f.GetReference().startswith(("J", "P"))], key=key)
    rest = sorted([f for f in fps if f not in conns], key=key)
    cell_w = max([size(f)[0] for f in rest] + [5]) + gap
    cell_h = max([size(f)[1] for f in rest] + [5]) + gap
    cols = max(1, int(round(len(rest) ** 0.5 + 0.49)))
    x0, y0 = 100.0, 100.0
    left = max([size(f)[0] for f in conns] + [0])
    y = y0
    for f in conns:
        w, h = size(f)
        f.SetPosition(V(F(x0 + w / 2), F(y + h / 2)))
        y += h + gap
    gx = x0 + (left + gap if conns else 0)
    for i, f in enumerate(rest):
        r, c = divmod(i, cols)
        f.SetPosition(V(F(gx + c * cell_w + cell_w / 2), F(y0 + r * cell_h + cell_h / 2)))
    # outline: around the parts with a margin, or the size asked for
    bb = None
    for f in fps:
        b = _bbox(f)
        if bb is None:
            bb = b
        else:
            bb.Merge(b)
    margin = float(job.get("margin_mm") or 5)
    ns = namespace(board)
    if bb is not None:
        bx, by = pcbnew.ToMM(bb.GetX()) - margin, pcbnew.ToMM(bb.GetY()) - margin
        bw, bh = pcbnew.ToMM(bb.GetWidth()) + 2 * margin, pcbnew.ToMM(bb.GetHeight()) + 2 * margin
        bw, bh = max(bw, float(job.get("width_mm") or 0)), max(bh, float(job.get("height_mm") or 0))
        ns["outline_rect"](round(bw, 1), round(bh, 1), round(bx, 1), round(by, 1))
    # wide tracks by default
    width = F(float(job.get("track_mm") or 0.5))
    try:
        ds = board.GetDesignSettings()
        try:
            nc = ds.m_NetSettings.GetDefaultNetclass()
        except Exception:
            nc = ds.m_NetSettings.m_DefaultNetClass
        nc.SetTrackWidth(width)
        nc.SetClearance(F(float(job.get("clearance_mm") or 0.3)))
    except Exception:
        pass
    return {"placed": len(fps), "nets": len(nets)}


def autoroute(board, job):
    dsn, ses = job["dsn"], job["ses"]
    pcbnew.ExportSpecctraDSN(board, dsn)
    return os.path.exists(dsn)


def import_routes(board, job):
    try:
        ok = pcbnew.ImportSpecctraSES(board, job["ses"])
    except TypeError:
        ok = pcbnew.ImportSpecctraSES(job["ses"])
    return bool(ok)


def main():
    with open(os.environ["STELLAI_JOB"], encoding="utf-8") as f:
        job = json.load(f)
    for k, v in (job.get("env") or {}).items():
        os.environ[k] = v
    out = {"ok": False}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if job.get("new"):
                board = pcbnew.NewBoard(job["board"]) if hasattr(pcbnew, "NewBoard") else pcbnew.BOARD()
            else:
                board = pcbnew.LoadBoard(job["board"])
            if job.get("netlist"):
                out["before"] = report(board)
                out["built"] = build_board(board, job)
            if job.get("dsn"):
                out["dsn"] = autoroute(board, job)
            if job.get("import_ses"):
                out["routed"] = import_routes(board, job)
            if job.get("code"):
                out["before"] = report(board)
                exec(compile(job["code"], "<step>", "exec"), namespace(board))
            out["after"] = report(board)
            if job.get("save"):
                pcbnew.SaveBoard(job["board"], board)
        out["ok"] = True
    except Exception as e:
        tb = traceback.extract_tb(sys.exc_info()[2])
        out.update(error=f"{type(e).__name__}: {e}"[:1500],
                   where=[f"line {f.lineno}: {(f.line or '').strip()}" for f in tb if f.filename == "<step>"][-3:])
    out["output"] = buf.getvalue()[-3000:]
    OUT.write(MARK + json.dumps(out) + "\n")
    OUT.flush()


main()
'''
PCB_IMPORTS = {"pcbnew", "math", "random", "itertools", "functools", "collections", "statistics"}
PCB_BAD_ATTRS = {"LoadBoard", "NewBoard", "CreateEmptyBoard", "WriteDRCReport", "PLOT_CONTROLLER", "Load", "Plot"}
KICAD = {"python": None}


def find_kicad_cli():
    env = os.environ.get("STELLAI_KICAD_CLI", "")
    if env and os.path.isfile(env):
        return env
    w = shutil.which("kicad-cli")
    if w:
        return w
    if os.name == "nt":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            found = sorted(glob.glob(os.path.join(root, "KiCad", "*", "bin", "kicad-cli.exe")), reverse=True)
            if found:
                return found[0]
    for c in ("/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"):
        if os.path.isfile(c):
            return c
    return ""


def find_kicad_python():
    """A Python that can import pcbnew (KiCad's own on Windows), or "" (checked once per run)."""
    if KICAD["python"] is not None:
        return KICAD["python"]
    cands = [os.environ.get("STELLAI_KICAD_PYTHON", "")]
    cli = find_kicad_cli()
    if cli and os.name == "nt":
        cands.append(os.path.join(os.path.dirname(cli), "python.exe"))
    cands += ["/usr/bin/python3", shutil.which("python3") or ""]
    KICAD["python"] = ""
    for c in cands:
        if c and os.path.isfile(c):
            try:
                if subprocess.run([c, "-c", "import pcbnew"], capture_output=True, timeout=60,
                                  creationflags=0x08000000 if os.name == "nt" else 0).returncode == 0:
                    KICAD["python"] = c
                    break
            except (OSError, subprocess.TimeoutExpired):
                pass
    return KICAD["python"]


def _kicad_cli(args, timeout=300):
    cli = find_kicad_cli()
    if not cli:
        raise RuntimeError("KiCad isn't installed on the server. Linux: re-run stellai-setup.sh and say yes to KiCad. "
                           "Windows: install it from kicad.org. Then try again.")
    p = subprocess.run([cli] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, stdin=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0)
    return p.returncode, ((p.stdout or "") + "\n" + (p.stderr or "")).strip()


def _project_files(d):
    """(board .kicad_pcb, root schematic .kicad_sch or "") inside a version folder."""
    pcbs = sorted(glob.glob(os.path.join(d, "*.kicad_pcb")))
    pros = sorted(glob.glob(os.path.join(d, "*.kicad_pro")))
    stem = os.path.splitext(os.path.basename(pros[0]))[0] if pros else (
        os.path.splitext(os.path.basename(pcbs[0]))[0] if pcbs else "")
    board = os.path.join(d, stem + ".kicad_pcb") if stem and os.path.isfile(os.path.join(d, stem + ".kicad_pcb")) \
        else (pcbs[0] if pcbs else "")
    sch = os.path.join(d, stem + ".kicad_sch") if stem and os.path.isfile(os.path.join(d, stem + ".kicad_sch")) else ""
    return board, sch


def _pcb_run(board, code="", save=False, timeout=180):
    py = find_kicad_python()
    if not py:
        raise RuntimeError("KiCad's Python scripting (pcbnew) isn't available on the server, so board edits can't "
                           "run. Checks, pictures and exports still work. Linux: install KiCad's Python module "
                           "(it comes with the kicad package); Windows: it ships with KiCad.")
    script = os.path.join(BLENDER["root"], ".worker", "stellai_pcb.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write(PCB_RUNNER_PY)
    return _run_marked([py, script], PCB_MARK, {"board": board, "code": code, "save": save}, timeout)


def _summarize_violations(items, limit=12):
    lines, counts = [], {}
    for v in items:
        sev = v.get("severity", "?")
        counts[sev] = counts.get(sev, 0) + 1
        if len(lines) < limit:
            where = "; ".join(i.get("description", "")[:80] for i in (v.get("items") or [])[:2])
            lines.append(f"[{sev}] {v.get('type', '')}: {v.get('description', '')}" + (f" ({where})" if where else ""))
    return counts, lines


def _pcb_checks(d):
    board, sch = _project_files(d)
    out = {}
    tmp = os.path.join(BLENDER["root"], ".worker")
    if board:
        rpt = os.path.join(tmp, f"drc-{uuid.uuid4().hex[:8]}.json")
        args = ["pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "-o", rpt]
        if sch:
            args.append("--schematic-parity")
        _rc, text = _kicad_cli(args + [board])
        try:
            with open(rpt, encoding="utf-8") as f:
                data = json.load(f)
            counts, lines = _summarize_violations(data.get("violations", []))
            out["drc"] = {"counts": counts, "unconnected": len(data.get("unconnected_items", [])),
                          "schematic_mismatches": len(data.get("schematic_parity", [])), "top": lines}
        except (OSError, ValueError):
            out["drc"] = {"error": text[-600:] or "no report"}
        _quiet_remove(rpt)
    if sch:
        rpt = os.path.join(tmp, f"erc-{uuid.uuid4().hex[:8]}.json")
        _rc, text = _kicad_cli(["sch", "erc", "--format", "json", "--severity-all", "-o", rpt, sch])
        try:
            with open(rpt, encoding="utf-8") as f:
                data = json.load(f)
            items = [v for s in data.get("sheets", []) for v in s.get("violations", [])]
            counts, lines = _summarize_violations(items)
            out["erc"] = {"counts": counts, "top": lines}
        except (OSError, ValueError):
            out["erc"] = {"error": text[-600:] or "no report"}
        _quiet_remove(rpt)
    return out


def _pcb_render(d, side="top"):
    board, _ = _project_files(d)
    if not board:
        return None, "no board file to show"
    side = side if side in ("top", "bottom", "front", "back", "left", "right") else "top"
    image_id = uuid.uuid4().hex[:16]
    png = os.path.join(DIAGRAM_DIR, image_id + ".png")
    rc, text = _kicad_cli(["pcb", "render", "--side", side, "--width", "1400", "--height", "1000", "--quality",
                           "basic", "-o", png, board], timeout=240)
    if not os.path.exists(png):       # older kicad-cli: fewer options
        rc, text = _kicad_cli(["pcb", "render", "-o", png, board], timeout=240)
    if not os.path.exists(png):
        return None, "KiCad couldn't draw the board (pictures need KiCad 9 or newer): " + text[-300:]
    return image_id, ""


def tool_pcb_project(args):
    action = str(args.get("action", "status")).strip().lower()
    if action == "status":
        ver = ""
        if find_kicad_cli():
            try:
                ver = _kicad_cli(["version"], timeout=30)[1].splitlines()[0]
            except (OSError, IndexError, subprocess.TimeoutExpired):
                ver = "?"
        return {"kicad_cli": find_kicad_cli() or "not installed", "kicad_version": ver,
                "board_scripting": bool(find_kicad_python()), "inbox_folder": _inbox(),
                "projects_you_can_import": [f for f in sorted(os.listdir(_inbox())) if f.lower().endswith(
                    (".zip", ".kicad_pcb")) or os.path.isdir(os.path.join(_inbox(), f))][:40],
                "workspaces": PCB.list(), "active": _safe(lambda: PCB.name(""))}, "Checked PCB workspaces"
    if action == "new":
        name = PCB.start(args.get("name"))
        v, path = PCB.next(name)
        try:
            if args.get("file"):
                src = _pick_inbox_file(args.get("file"), ("zip", "kicad_pcb"))
                if os.path.isdir(src):
                    shutil.copytree(src, path)
                elif src.lower().endswith(".zip"):
                    import zipfile
                    with zipfile.ZipFile(src) as z:
                        for m in z.namelist():
                            if m.startswith("/") or ".." in m.replace("\\", "/").split("/"):
                                raise ValueError("that zip has unsafe paths in it")
                        z.extractall(path)
                    inner = [e for e in os.listdir(path) if not e.startswith(".")]
                    if len(inner) == 1 and os.path.isdir(os.path.join(path, inner[0])):   # zip of a folder
                        sub = os.path.join(path, inner[0])
                        for e in os.listdir(sub):
                            shutil.move(os.path.join(sub, e), path)
                        os.rmdir(sub)
                else:
                    os.makedirs(path)
                    shutil.copyfile(src, os.path.join(path, os.path.basename(src)))
                    sch = src[:-len(".kicad_pcb")] + ".kicad_sch"
                    if os.path.isfile(sch):
                        shutil.copyfile(sch, os.path.join(path, os.path.basename(sch)))
                if not _project_files(path)[0] and not _project_files(path)[1]:
                    raise ValueError("no .kicad_pcb or .kicad_sch file in it")
            else:
                os.makedirs(path)
                board = os.path.join(path, name + ".kicad_pcb")
                w, h = float(args.get("width_mm") or 50), float(args.get("height_mm") or 50)
                py = find_kicad_python()
                if not py:
                    raise RuntimeError("starting an empty board needs KiCad's Python (pcbnew); import a project "
                                       "from the 3D inbox instead")
                script = os.path.join(BLENDER["root"], ".worker", "stellai_pcb.py")
                with open(script, "w", encoding="utf-8") as f:
                    f.write(PCB_RUNNER_PY)
                res = _run_marked([py, script], PCB_MARK, {"board": board, "new": True, "save": True,
                                                           "code": f"outline_rect({w}, {h}, 100, 100)"}, 180)
                if not res.get("ok"):
                    raise RuntimeError(res.get("error") or "KiCad couldn't make the board")
        except Exception:
            shutil.rmtree(PCB.dir(name), ignore_errors=True)
            raise
        PCB.record(name, v, f"start: {args.get('file') or 'empty board'}")
        PCB.activate(name)
        board, sch = _project_files(path)
        res = {"workspace": name, "version": v, "board": os.path.basename(board) or None,
               "schematic": os.path.basename(sch) or None, "checks": _pcb_checks(path)}
        img, why = _pcb_render(path)
        if img:
            res.update(image=img, title=f"{name} v{v}")
        elif why:
            res["picture"] = why
        return res, f"Started PCB workspace {name} (v{v})"
    if action in ("add_reference", "references", "clear_references"):
        return PCB.refs_action(action, args)
    name = PCB.name(args.get("name"))
    if action == "open":
        PCB.activate(name)
        return {"workspace": name, "latest_version": PCB.latest(name), "recent": PCB.history(name)[-8:]}, f"Opened {name}"
    if action == "history":
        return {"workspace": name, "versions_on_disk": PCB.versions(name), "history": PCB.history(name)[-25:]}, \
            f"History of {name}"
    if action == "revert":
        target = _int_version(args.get("version"))
        if target not in PCB.versions(name):
            raise ValueError(f"version {target} isn't on disk; available: {PCB.versions(name)[-15:]}")
        v, path = PCB.next(name)
        shutil.copytree(PCB.path(name, target), path)
        PCB.record(name, v, f"back to v{target}")
        return {"workspace": name, "version": v, "same_as": target}, f"Went back to v{target} (saved as v{v})"
    if action == "export":
        return _pcb_export(name, str(args.get("format", "fab")).lower().strip(". "))
    raise ValueError("action must be status, new, open, history, revert, export, add_reference, references or "
                     "clear_references")


def _pcb_export(name, fmt):
    import zipfile
    v = PCB.latest(name)
    d = PCB.path(name, v)
    board, sch = _project_files(d)
    outdir = PCB.dir(name, "exports")
    os.makedirs(outdir, exist_ok=True)
    stem = f"{name}-v{v:04d}"
    made, problems = [], []

    def run(args, path):
        rc, text = _kicad_cli(args)
        if os.path.exists(path):
            made.append(path)
        else:
            problems.append(text[-300:] or f"{os.path.basename(path)} wasn't made")

    if fmt in ("fab", "gerbers", "all"):
        if not board:
            raise ValueError("no board file in this project")
        gdir = os.path.join(BLENDER["root"], ".worker", f"gerb-{uuid.uuid4().hex[:8]}")
        os.makedirs(gdir)
        _kicad_cli(["pcb", "export", "gerbers", "-o", gdir + os.sep, board])
        _kicad_cli(["pcb", "export", "drill", "-o", gdir + os.sep, board])
        zpath = os.path.join(outdir, stem + "-fab.zip")
        files = sorted(os.listdir(gdir))
        if files:
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(os.path.join(gdir, f), f)
            made.append(zpath)
        else:
            problems.append("no Gerber or drill files came out")
        shutil.rmtree(gdir, ignore_errors=True)
    if fmt in ("bom", "all") and sch:
        run(["sch", "export", "bom", "-o", os.path.join(outdir, stem + "-bom.csv"), sch], os.path.join(outdir, stem + "-bom.csv"))
    if fmt in ("pos", "placement", "all") and board:
        p = os.path.join(outdir, stem + "-positions.csv")
        run(["pcb", "export", "pos", "--format", "csv", "--units", "mm", "-o", p, board], p)
    if fmt in ("step", "3d", "all") and board:
        p = os.path.join(outdir, stem + ".step")
        run(["pcb", "export", "step", "--subst-models", "-o", p, board], p)
        if os.path.exists(p):   # so the CAD Designer can build an enclosure around it
            shutil.copyfile(p, os.path.join(_inbox(), stem + ".step"))
    if fmt in ("pdf", "schematic", "all") and sch:
        p = os.path.join(outdir, stem + "-schematic.pdf")
        run(["sch", "export", "pdf", "-o", p, sch], p)
    if fmt in ("project", "kicad", "all"):
        zpath = os.path.join(outdir, stem + "-kicad-project.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _dirs, files in os.walk(d):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, d))
        made.append(zpath)
    if not made and not problems:
        raise ValueError("format must be fab (Gerbers + drill), bom, pos, step, pdf, project or all")
    return {"workspace": name, "version": v, "files": made, "problems": problems,
            "note": "The files are in this folder on the server computer. fab is the zip a board maker asks for."}, \
        f"Exported {name} v{v}"


def tool_pcb_check(args):
    name = PCB.name(args.get("workspace"))
    v = PCB.latest(name)
    d = PCB.path(name, v)
    out = {"workspace": name, "version": v, "checks": _pcb_checks(d)}
    board, _ = _project_files(d)
    if board and find_kicad_python():
        try:
            r = _pcb_run(board)
            if r.get("ok"):
                a = r["after"]
                out["board"] = {k: a.get(k) for k in ("board_mm", "copper_layers", "nets", "tracks", "vias", "zones")}
                out["footprints"] = a.get("footprints", [])[:80]
        except RuntimeError as e:
            out["board_error"] = str(e)
    if str(args.get("render", True)).lower() not in ("false", "0", "no"):
        img, why = _pcb_render(d, str(args.get("side", "top")).lower())
        if img:
            out.update(image=img, title=f"{name} v{v} ({args.get('side', 'top')})")
        elif why:
            out["picture"] = why
    return out, f"Checked {name} v{v}"


def tool_pcb_step(args):
    code, goal = str(args.get("code", "")), str(args.get("goal", "")).strip()
    if not goal or not code.strip():
        raise ValueError("give goal (with millimetres and references like R3) and code")
    probs = check_bpy_code(code, allowed=PCB_IMPORTS, extra_bad=PCB_BAD_ATTRS, bad_prefixes=("Export", "Import", "Plot"))
    if probs:
        return {"ok": False, "error": "the code was not run: " + "; ".join(probs)}, "Step blocked"
    name = PCB.name(args.get("workspace"))
    base = PCB.latest(name)
    before_checks = _pcb_checks(PCB.path(name, base))
    v, path = PCB.next(name)
    shutil.copytree(PCB.path(name, base), path)
    board, _ = _project_files(path)
    if not board:
        shutil.rmtree(path, ignore_errors=True)
        raise ValueError("this project has no board file to edit")
    try:
        res = _pcb_run(board, code, save=True, timeout=max(30, min(600, int(args.get("timeout") or 180))))
    except RuntimeError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    if not res.get("ok"):
        shutil.rmtree(path, ignore_errors=True)
        return {"ok": False, "error": res.get("error"), "where": res.get("where"), "output": res.get("output", ""),
                "note": f"Nothing was saved; the board is still v{base}."}, "Step failed"
    b, a = res["before"], res["after"]
    diff = _diff_by(b["footprints"], a["footprints"], "ref", ("x_mm", "y_mm", "rot_deg", "side", "value", "footprint"))
    for k in ("board_mm", "tracks", "vias", "zones", "nets", "copper_layers"):
        if b.get(k) != a.get(k):
            diff.setdefault("board", {})[k] = {"from": b.get(k), "to": a.get(k)}
    if not (diff["added"] or diff["removed"] or diff["changed"] or diff.get("board")):
        shutil.rmtree(path, ignore_errors=True)
        return {"ok": False, "error": "the code ran but nothing on the board changed", "output": res.get("output", ""),
                "note": f"Nothing was saved; the board is still v{base}."}, "Step changed nothing"
    PCB.record(name, v, goal)
    after_checks = _pcb_checks(path)
    out = {"version": v, "previous": base, "changes": diff, "checks_before": before_checks.get("drc", {}).get("counts"),
           "checks_now": after_checks, "output": res.get("output", "")[-1500:]}
    img, why = _pcb_render(path, str(args.get("side", "top")).lower())
    if img:
        out.update(image=img, title=f"{name} v{v}")
        if str(args.get("check", True)).lower() not in ("false", "0", "no"):
            out["visual_check"] = _vision_check(img, goal, diff, "one picture of the board from the "
                                                + str(args.get("side", "top")), refs=PCB.ref_images(name))
    elif why:
        out["picture"] = why
    return out, f"Saved {name} v{v}"


_REF_ARGS = {"photos": {"type": "array", "items": {"type": "string"},
                        "description": "For add_reference: photo ids from [photo ...] in the chat, or 'latest'"},
             "labels": {"type": "array", "items": {"type": "string"},
                        "description": "For add_reference: front, left, back, right, top or detail"}}

SERVER_TOOLS["cad_project"] = (tool_cad_project, _fn(
    "cad_project",
    "CAD Designer workspaces (FreeCAD, millimetres, numbered versions). status: FreeCAD, workspaces, importable files. "
    "new: start from an empty design or a .FCStd/.step/.stl/.iges in the 3D inbox. open, history, revert. export: "
    "step (for other CAD), stl (for 3D printing), fcstd, brep, iges, or blender (sends an STL to the 3D Modeler's "
    "inbox). add_reference / references / clear_references: photos or sketches to compare against.",
    {"action": _p("status, new, open, history, revert, export, add_reference, references or clear_references"),
     "name": _p("Workspace name (default: the active one)"), "file": _p("For new: a file in the 3D inbox"),
     "version": _p("For revert", "integer"), "format": _p("For export: step (default), stl, fcstd, brep, iges, blender"),
     **_REF_ARGS}, ["action"]))
SERVER_TOOLS["cad_step"] = (tool_cad_step, _fn(
    "cad_step",
    "CAD Designer: run ONE small change on the current design as a FreeCAD Python script (headless, millimetres, Z up, "
    "front faces -Y). Saved as a new version only if it works and changes something; then measured (sizes, volume, "
    "solids, broken geometry), rendered and checked against goal and reference pictures. Use Part shapes: "
    "Part.makeBox/makeCylinder/makeCone/makeSphere/makeTorus, shape.cut/fuse/common, makeFillet/makeChamfer, "
    "Part.makePolygon/Face/extrude for profiles. Ready to use: doc, add(name, shape) (makes or replaces a part), "
    "get(name), remove(name), hide(name), box(x, y, z, at), cylinder(r, h, at, axis), hole(d, depth, at, axis), "
    "fillet(shape, r, edges), chamfer(shape, d, edges), Vector, Placement, Rotation. Keep one final part per real "
    "object: build with shapes, then add() the result. No files or other imports.",
    {"goal": _p("What this step should achieve with millimetres, e.g. 'add two M3 holes (3.2 mm) through the base, "
                "30 mm apart, centred'"),
     "code": _p("The FreeCAD script"),
     "views": {"type": "array", "items": {"type": "string"},
               "description": "front, right, back, left, top, three_quarter (default 4)"},
     "check": _p("false skips the visual check", "boolean"), "workspace": _p("Default: the active workspace")},
    ["goal", "code"]))
SERVER_TOOLS["cad_inspect"] = (tool_cad_inspect, _fn(
    "cad_inspect",
    "CAD Designer: read the current design without changing it: parts with sizes (mm), positions, volumes, solids and "
    "geometry problems. render=true adds pictures (and a comparison with reference pictures).",
    {"render": _p("true for pictures", "boolean"),
     "views": {"type": "array", "items": {"type": "string"}, "description": "Render angles"},
     "workspace": _p("Default: the active workspace")}, []))
SERVER_TOOLS["pcb_project"] = (tool_pcb_project, _fn(
    "pcb_project",
    "PCB Designer workspaces (KiCad projects with numbered versions). status: KiCad, workspaces, importable projects. "
    "new: import a KiCad project (.zip, folder or .kicad_pcb in the 3D inbox) or start an empty board of width_mm x "
    "height_mm. open, history, revert. export: fab (Gerbers + drill zip for a board maker), bom, pos (pick and "
    "place), step (3D), pdf (schematic), project (zip of the KiCad files) or all. add_reference etc. as in CAD.",
    {"action": _p("status, new, open, history, revert, export, add_reference, references or clear_references"),
     "name": _p("Workspace name (default: the active one)"), "file": _p("For new: a project in the 3D inbox"),
     "width_mm": _p("For an empty board", "number"), "height_mm": _p("For an empty board", "number"),
     "version": _p("For revert", "integer"), "format": _p("For export: fab (default), bom, pos, step, pdf, project, all"),
     **_REF_ARGS}, ["action"]))
SERVER_TOOLS["pcb_check"] = (tool_pcb_check, _fn(
    "pcb_check",
    "PCB Designer: check the current board without changing it: KiCad's electrical rules check (schematic) and design "
    "rules check (board: clearances, unrouted connections, mismatches with the schematic), board size, layers, nets, "
    "footprint positions, and a picture.",
    {"side": _p("Picture side: top (default) or bottom"), "render": _p("false skips the picture", "boolean"),
     "workspace": _p("Default: the active workspace")}, []))
SERVER_TOOLS["pcb_step"] = (tool_pcb_step, _fn(
    "pcb_step",
    "PCB Designer: run ONE small board change as a KiCad pcbnew script (millimetres; board coordinates, Y grows "
    "downwards). Saved as a new version only if it works and changes something; then the design rules are checked "
    "again and the board is pictured. Ready to use: board, pcbnew, mm(x) (to KiCad units), to_mm, fp(ref), "
    "move(ref, x, y, rot), outline_rect(w, h, x, y), line(x1, y1, x2, y2, layer, width), track(x1, y1, x2, y2, width, "
    "layer, net), text(s, x, y, size, layer). Schematic changes aren't scripted: guide the user in KiCad for those.",
    {"goal": _p("What this step should achieve, e.g. 'move C3 within 2 mm of U1 pin 8 and rotate it 90°'"),
     "code": _p("The pcbnew script"), "side": _p("Picture side: top (default) or bottom"),
     "check": _p("false skips the visual check", "boolean"), "workspace": _p("Default: the active workspace")},
    ["goal", "code"]))


# ---- Circuit as code: a SKiDL script -> netlist + schematic -> wide placement -> Freerouting -> checks -----------
# Stella writes the circuit (parts and connections) as Python; KiCad-compatible files come out of it. Boards are
# laid out wide and simple (through-hole friendly, thick tracks) so early designs are easy to check and solder.
# A small library on the server keeps sub-circuits that worked (recipes), what went wrong (notes) and pinouts the
# user confirmed, so she gets better over time instead of guessing the same things again.
SKIDL_MARK = "@@SKIDL@@"
SKIDL_RUNNER_PY = r'''# StellAI circuit runner: runs a circuit written with SKiDL, checks it and writes a KiCad netlist (and a
# schematic when SKiDL can draw one). The job comes as JSON in the file named by STELLAI_JOB. Written by
# stellai_hub.py: edits here are overwritten.
import contextlib
import io
import json
import os
import sys
import traceback

MARK = "@@SKIDL@@"
OUT = sys.stdout


def main():
    with open(os.environ["STELLAI_JOB"], encoding="utf-8") as f:
        job = json.load(f)
    for k, v in (job.get("env") or {}).items():      # where KiCad's symbol and footprint libraries are
        os.environ[k] = v
    out = {"ok": False}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            import skidl
            ns = {"__name__": "stellai_circuit"}
            exec("from skidl import *", ns)
            names = [job.get("tool") or "KICAD9", "KICAD9", "KICAD8", "KICAD10", "KICAD7"]
            for tname in names:
                if hasattr(skidl, tname):
                    try:
                        skidl.set_default_tool(getattr(skidl, tname))
                    except Exception:
                        pass
                    break
            exec(compile(job["code"], "<circuit>", "exec"), ns)
            circuit = skidl.default_circuit
            erc_buf = io.StringIO()
            with contextlib.redirect_stdout(erc_buf), contextlib.redirect_stderr(erc_buf):
                try:
                    skidl.ERC()
                except Exception as e:
                    print(f"ERC couldn't run: {e}")
            parts = []
            for p in circuit.parts:
                parts.append({"ref": p.ref, "name": getattr(p, "name", ""), "value": str(getattr(p, "value", "") or ""),
                              "footprint": str(getattr(p, "footprint", "") or ""),
                              "pins": [f"{pin.num}:{pin.name}" for pin in p.pins][:40],
                              "unconnected": [f"{pin.num}:{pin.name}" for pin in p.pins if not pin.is_connected()][:20]})
            nets = []
            for n in circuit.get_nets():
                nets.append({"name": n.name, "pins": [f"{pin.part.ref}.{pin.num}({pin.name})" for pin in n.pins][:30]})
            missing_fp = [p["ref"] for p in parts if not p["footprint"]]
            out.update(parts=parts, nets=nets, erc=erc_buf.getvalue()[-3000:], missing_footprints=missing_fp)
            if missing_fp:
                raise ValueError("every part needs a footprint (footprint='Library:Name'); missing on: "
                                 + ", ".join(missing_fp))
            skidl.generate_netlist(file_=job["netlist"])
            if job.get("schematic_dir"):
                try:
                    skidl.generate_schematic(filepath=job["schematic_dir"], top_name=job.get("name", "circuit"))
                    out["schematic"] = True
                except Exception as e:
                    out["schematic"] = False
                    out["schematic_note"] = f"no schematic drawing ({type(e).__name__}: {str(e)[:200]})"
        out["ok"] = True
    except Exception as e:
        tb = traceback.extract_tb(sys.exc_info()[2])
        out.update(error=f"{type(e).__name__}: {e}"[:1500],
                   where=[f"line {f.lineno}: {(f.line or '').strip()}" for f in tb if f.filename == "<circuit>"][-3:])
    out["output"] = buf.getvalue()[-3000:]
    OUT.write(MARK + json.dumps(out) + "\n")
    OUT.flush()


main()
'''
SKIDL_IMPORTS = {"skidl", "math", "itertools", "functools", "collections"}
SKIDL_BAD = {"lib_search_paths", "footprint_search_paths", "set_default_tool", "backup_parts", "load_backup_lib",
             "set_query_backend", "ERC", "reset"}
CIRCUIT = {"python": None, "lock": threading.Lock()}


def _kicad_libs():
    """KiCad's symbol and footprint folders, and environment variables that point SKiDL at them."""
    roots = ["/usr/share/kicad", "/usr/local/share/kicad"]
    cli = find_kicad_cli()
    if cli:
        roots.insert(0, os.path.normpath(os.path.join(os.path.dirname(cli), "..", "share", "kicad")))
    sym = fpd = ""
    for r in roots:
        if not sym and os.path.isdir(os.path.join(r, "symbols")):
            sym = os.path.join(r, "symbols")
        if not fpd and os.path.isdir(os.path.join(r, "footprints")):
            fpd = os.path.join(r, "footprints")
    sym = os.environ.get("STELLAI_KICAD_SYMBOLS", sym)
    fpd = os.environ.get("STELLAI_KICAD_FOOTPRINTS", fpd)
    env = {}
    for v in ("", "7", "8", "9", "10"):
        if sym:
            env[f"KICAD{v}_SYMBOL_DIR"] = sym
        if fpd:
            env[f"KICAD{v}_FOOTPRINT_DIR"] = fpd
    return sym, fpd, env


def find_skidl_python():
    """A Python that can import skidl (its own folder from the setup script first), or "" (checked once)."""
    if CIRCUIT["python"] is not None:
        return CIRCUIT["python"]
    base = os.path.dirname(CHATS_PATH) if CHATS_PATH else ""
    cands = [os.environ.get("STELLAI_SKIDL_PYTHON", "")]
    if base:
        cands += [os.path.join(base, "skidl-venv", "bin", "python3"),
                  os.path.join(base, "skidl-venv", "Scripts", "python.exe")]
    cands += [find_kicad_python(), sys.executable, shutil.which("python3") or ""]
    CIRCUIT["python"] = ""
    for c in cands:
        if c and os.path.isfile(c):
            try:
                if subprocess.run([c, "-c", "import skidl"], capture_output=True, timeout=90,
                                  creationflags=0x08000000 if os.name == "nt" else 0).returncode == 0:
                    CIRCUIT["python"] = c
                    break
            except (OSError, subprocess.TimeoutExpired):
                pass
    return CIRCUIT["python"]


def find_freerouting():
    """(java, freerouting jar) or ("", "")."""
    java = os.environ.get("STELLAI_JAVA", "") or shutil.which("java") or ""
    jar = os.environ.get("STELLAI_FREEROUTING", "")
    if not (jar and os.path.isfile(jar)):
        dirs = [os.path.join(os.path.dirname(CHATS_PATH), "freerouting")] if CHATS_PATH else []
        dirs.append(os.path.join(BLENDER["root"], "freerouting"))
        jars = [j for d in dirs for j in sorted(glob.glob(os.path.join(d, "freerouting*.jar")), reverse=True)]
        jar = jars[0] if jars else ""
    return (java, jar) if java and jar else ("", "")


def _kicad_major():
    try:
        m = re.search(r"(\d+)\.", _kicad_cli(["version"], timeout=30)[1])
        return int(m.group(1)) if m else 9
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return 9


# the library: recipes (sub-circuits that worked), notes (lessons) and confirmed pinouts
def _lib_path():
    return os.path.join(BLENDER["root"], "pcb", "library.json")


def _lib():
    try:
        with open(_lib_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("recipes", {})
    d.setdefault("pinouts", {})
    d.setdefault("notes", [])
    return d


def _lib_save(d):
    os.makedirs(os.path.dirname(_lib_path()), exist_ok=True)
    tmp = _lib_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, _lib_path())


def _part_key(name, footprint):
    return f"{str(name).strip()}|{str(footprint).strip()}".lower()


def _skidl_check(code):
    return check_bpy_code(code, allowed=SKIDL_IMPORTS, extra_bad=SKIDL_BAD, bad_prefixes=("generate_",))


def _pcb_runner_script():
    script = os.path.join(BLENDER["root"], ".worker", "stellai_pcb.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write(PCB_RUNNER_PY)
    return script


def _autoroute(board, passes, timeout, env):
    """Board -> Specctra DSN -> Freerouting -> SES -> back onto the board. Returns a short report."""
    java, jar = find_freerouting()
    if not java:
        return {"routed": False, "note": "the autorouter (Freerouting + Java) isn't installed on the server; parts "
                                         "are placed and connected by ratsnest lines. Re-run the setup and say yes "
                                         "to the autorouter, or route by hand in KiCad."}
    py = find_kicad_python()
    work = os.path.join(BLENDER["root"], ".worker", f"route-{uuid.uuid4().hex[:8]}")
    os.makedirs(work)
    dsn, ses = os.path.join(work, "board.dsn"), os.path.join(work, "board.ses")
    try:
        r = _run_marked([py, _pcb_runner_script()], PCB_MARK, {"board": board, "dsn": dsn, "ses": ses, "env": env}, 180)
        if not r.get("ok") or not os.path.isfile(dsn):
            return {"routed": False, "note": "KiCad couldn't export the board for the autorouter: "
                                             + str(r.get("error") or "no file")[:300]}
        base_cmd = [java, "-Djava.awt.headless=true", "-jar", jar, "-de", dsn, "-do", ses, "-mp", str(passes)]
        tail = ""
        for extra in (["--gui.enabled=false"], []):      # Freerouting 2.x needs the first, 1.x refuses it
            try:
                p = subprocess.run(base_cmd + extra, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout, cwd=work, stdin=subprocess.DEVNULL,
                                   creationflags=0x08000000 if os.name == "nt" else 0)
                tail = ((p.stdout or "") + (p.stderr or ""))[-400:]
            except subprocess.TimeoutExpired:
                return {"routed": False, "note": f"the autorouter ran longer than {timeout} s and was stopped; the "
                                                 "board is saved unrouted. Try more spacing or fewer parts."}
            if os.path.isfile(ses):
                break
        if not os.path.isfile(ses):
            return {"routed": False, "note": "the autorouter didn't produce a result: " + tail.strip()[-300:]}
        r = _run_marked([py, _pcb_runner_script()], PCB_MARK,
                        {"board": board, "ses": ses, "import_ses": True, "save": True, "env": env}, 180)
        if not r.get("ok") or not r.get("routed"):
            return {"routed": False, "note": "KiCad couldn't bring the routes back: " + str(r.get("error") or "")[:300]}
        a = r.get("after", {})
        return {"routed": True, "tracks": a.get("tracks"), "vias": a.get("vias")}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def tool_pcb_circuit(args):
    code, goal = str(args.get("code", "")), str(args.get("goal", "")).strip()
    if not goal or not code.strip():
        raise ValueError("give goal (what the circuit does) and code (the SKiDL script)")
    lib = _lib()
    used = []
    pre = []
    for rname in args.get("recipes") or []:
        rec = lib["recipes"].get(str(rname).strip().lower())
        if not rec:
            raise ValueError(f"no recipe called {rname}; pcb_library list shows the saved ones")
        pre.append(f"# ---- recipe: {rname}\n" + rec["code"])
        used.append(str(rname).strip().lower())
    full = "\n\n".join(pre + [code])
    probs = _skidl_check(full)
    if probs:
        return {"ok": False, "error": "the script was not run: " + "; ".join(probs)}, "Circuit blocked"
    sp = find_skidl_python()
    if not sp:
        raise RuntimeError("SKiDL (circuit as code) isn't installed on the server. Linux: re-run stellai-setup.sh "
                           "and say yes to the PCB Designer; Windows: pip install skidl into KiCad's Python.")
    if not find_kicad_python():
        raise RuntimeError("building a board needs KiCad's Python (pcbnew) on the server")
    # workspace: a new name starts one, otherwise the active (or named) one gets a new version
    wanted = str(args.get("workspace") or "").strip().lower()
    if wanted and not PCB.versions(wanted):
        name = PCB.start(wanted)
    else:
        name = PCB.name(wanted)
    prev = PCB.versions(name)
    v, path = PCB.next(name)
    os.makedirs(path)
    stem = name
    sym, fpd, env = _kicad_libs()
    major = _kicad_major()
    try:
        net = os.path.join(path, stem + ".net")
        res = _run_marked([sp, _skidl_script()], SKIDL_MARK,
                          {"code": full, "netlist": net, "name": stem, "tool": f"KICAD{major}", "env": env,
                           "schematic_dir": path if str(args.get("schematic", True)).lower() not in ("false", "0", "no")
                           else ""}, max(30, min(600, int(args.get("timeout") or 240))))
        if not res.get("ok"):
            shutil.rmtree(path, ignore_errors=True)
            return {"ok": False, "error": res.get("error"), "where": res.get("where"), "erc": res.get("erc", ""),
                    "output": res.get("output", "")[-1500:],
                    "note": "Nothing was saved." + (f" The board is still v{prev[-1]}." if prev else "")}, \
                "Circuit failed"
        board = os.path.join(path, stem + ".kicad_pcb")
        job = {"board": board, "new": True, "netlist": net, "save": True, "env": env, "footprint_dirs": [fpd],
               "spacing_mm": args.get("spacing_mm") or 8, "track_mm": args.get("track_mm") or 0.5,
               "clearance_mm": args.get("clearance_mm") or 0.3, "margin_mm": args.get("margin_mm") or 5,
               "width_mm": args.get("width_mm") or 0, "height_mm": args.get("height_mm") or 0}
        if prev and str(args.get("keep_placement", True)).lower() not in ("false", "0", "no"):
            old_board, _ = _project_files(PCB.path(name, prev[-1]))
            if old_board:
                job["keep_from"] = old_board
        b = _run_marked([find_kicad_python(), _pcb_runner_script()], PCB_MARK, job, 300)
        if not b.get("ok"):
            shutil.rmtree(path, ignore_errors=True)
            return {"ok": False, "error": "the circuit is fine but the board couldn't be built: " + str(b.get("error")),
                    "parts": res.get("parts"), "note": "Nothing was saved. A wrong footprint name is the usual cause "
                                                       "(Library:Footprint, as in KiCad)."}, "Board failed"
        out = {"workspace": name, "version": v, "parts": len(res.get("parts", [])), "nets": len(res.get("nets", [])),
               "placed": b.get("built"), "erc": (res.get("erc") or "").strip()[-1500:] or "no problems reported",
               "schematic": res.get("schematic", False)}
        if res.get("schematic_note"):
            out["schematic_note"] = res["schematic_note"]
        if str(args.get("route", True)).lower() not in ("false", "0", "no"):
            out["routing"] = _autoroute(board, max(5, min(200, int(args.get("passes") or 40))),
                                        max(60, min(1800, int(args.get("route_timeout") or 600))), env)
        else:
            out["routing"] = {"routed": False, "note": "not routed (route=false)"}
    except Exception:
        shutil.rmtree(path, ignore_errors=True)
        raise
    with open(os.path.join(path, "circuit.py"), "w", encoding="utf-8") as f:   # the source, for the next change
        f.write(full)
    PCB.record(name, v, "circuit: " + goal)
    PCB.activate(name)
    # parts whose pinout nobody has confirmed yet: the user should check them against the datasheet
    with CIRCUIT["lock"]:
        lib = _lib()
        for u in used:
            lib["recipes"][u]["uses"] = lib["recipes"][u].get("uses", 0) + 1
        if used:
            _lib_save(lib)
    simple = ("device:r", "device:c", "device:c_polarized", "device:led", "device:d", "device:l")
    unknown = []
    for p in res.get("parts", []):
        if _part_key(p.get("name"), p.get("footprint")) in lib["pinouts"]:
            continue
        if any(str(p.get("name", "")).lower() == s.split(":")[1] for s in simple):
            continue
        unknown.append({"ref": p["ref"], "part": p.get("name"), "footprint": p.get("footprint"),
                        "pins": p.get("pins", [])[:24]})
    if unknown:
        out["verify_with_user"] = unknown[:12]
        out["verify_note"] = ("These parts' pin numbers aren't confirmed yet. Show the user the pins and ask them to "
                              "check against the datasheet or the real part; when they say it's right, save it with "
                              "pcb_library confirm_pinout so it's trusted next time.")
    floating = [f"{p['ref']}: {', '.join(p['unconnected'][:6])}" for p in res.get("parts", []) if p.get("unconnected")]
    if floating:
        out["unconnected_pins"] = floating[:15]
    out["checks"] = _pcb_checks(path)
    img, why = _pcb_render(path, str(args.get("side", "top")).lower())
    if img:
        out.update(image=img, title=f"{name} v{v}")
        if str(args.get("check", True)).lower() not in ("false", "0", "no"):
            out["visual_check"] = _vision_check(img, goal, {"parts": out["parts"], "nets": out["nets"]},
                                                "one picture of the board from the top", refs=PCB.ref_images(name))
    elif why:
        out["picture"] = why
    out["library_tip"] = ("If the user confirms this works (tested or looks right), offer to save the useful part as a "
                          "recipe with pcb_library save_recipe; if something was wrong, save what you learned with "
                          "pcb_library note.")
    return out, f"Built {name} v{v} from the circuit"


def _skidl_script():
    script = os.path.join(BLENDER["root"], ".worker", "stellai_skidl.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write(SKIDL_RUNNER_PY)
    return script


def tool_pcb_library(args):
    action = str(args.get("action", "list")).strip().lower()
    with CIRCUIT["lock"]:
        lib = _lib()
        name = str(args.get("name", "")).strip().lower()
        if action == "list":
            q = str(args.get("query", "")).lower()
            recs = [{"name": n, "description": r.get("description", ""), "confirmed": r.get("confirmed", False),
                     "uses": r.get("uses", 0)} for n, r in sorted(lib["recipes"].items())
                    if not q or q in n or q in r.get("description", "").lower()]
            pins = [{"part": p.get("part"), "footprint": p.get("footprint"), "source": p.get("source", "")}
                    for p in lib["pinouts"].values() if not q or q in json.dumps(p).lower()]
            notes = [n for n in lib["notes"] if not q or q in n.get("text", "").lower()][-15:]
            return {"recipes": recs[:60], "confirmed_pinouts": pins[:80], "notes": notes,
                    "tools": {"skidl": bool(find_skidl_python()), "kicad_python": bool(find_kicad_python()),
                              "autorouter": bool(find_freerouting()[0])}}, "Read the PCB library"
        if action == "get":
            rec = lib["recipes"].get(name)
            if not rec:
                raise ValueError(f"no recipe called {name}")
            return {"name": name, **rec}, f"Read recipe {name}"
        if action == "save_recipe":
            code = str(args.get("code", ""))
            if not WS_NAME.fullmatch(name or "-") or not code.strip():
                raise ValueError("give name (lowercase-with-dashes) and code (SKiDL, ideally a @subcircuit function)")
            probs = _skidl_check(code)
            if probs:
                return {"ok": False, "error": "not saved: " + "; ".join(probs)}, "Recipe blocked"
            old = lib["recipes"].get(name, {})
            lib["recipes"][name] = {"description": str(args.get("description", old.get("description", "")))[:400],
                                    "code": code, "confirmed": False, "uses": old.get("uses", 0),
                                    "notes": old.get("notes", []), "saved_at": int(time.time())}
            _lib_save(lib)
            return {"saved": name, "confirmed": False,
                    "note": "Saved as a draft; mark it confirmed once the user says it works."}, f"Saved recipe {name}"
        if action == "confirm_recipe":
            if name not in lib["recipes"]:
                raise ValueError(f"no recipe called {name}")
            lib["recipes"][name]["confirmed"] = True
            _lib_save(lib)
            return {"confirmed": name}, f"Confirmed recipe {name}"
        if action == "note":
            text = str(args.get("text", "")).strip()
            if not text:
                raise ValueError("give text: what went wrong and what fixed it")
            entry = {"text": text[:600], "at": int(time.time())}
            if name and name in lib["recipes"]:
                lib["recipes"][name].setdefault("notes", []).append(entry)
            else:
                lib["notes"] = (lib["notes"] + [entry])[-200:]
            _lib_save(lib)
            return {"noted": text[:120]}, "Saved a PCB note"
        if action == "confirm_pinout":
            part, fp = str(args.get("part", "")).strip(), str(args.get("footprint", "")).strip()
            if not part or not fp:
                raise ValueError("give part (the symbol, e.g. Regulator_Linear:AMS1117-3.3 or its name) and footprint")
            lib["pinouts"][_part_key(part.split(":")[-1], fp)] = {
                "part": part, "footprint": fp, "pins": str(args.get("pins", ""))[:1500],
                "source": str(args.get("source", "the user confirmed it"))[:200], "at": int(time.time())}
            _lib_save(lib)
            return {"confirmed": f"{part} on {fp}"}, f"Confirmed pinout of {part}"
        if action == "forget":
            gone = lib["recipes"].pop(name, None) is not None
            _lib_save(lib)
            return {"forgot": name if gone else None}, f"Forgot recipe {name}" if gone else "Nothing to forget"
    raise ValueError("action must be list, get, save_recipe, confirm_recipe, note, confirm_pinout or forget")


SERVER_TOOLS["pcb_circuit"] = (tool_pcb_circuit, _fn(
    "pcb_circuit",
    "PCB Designer, circuit as code: write the whole circuit as a SKiDL Python script (parts + connections) and get a "
    "KiCad netlist, a schematic, and a board with every part placed on a wide grid (connectors on the left), thick "
    "tracks and an outline, autorouted with Freerouting, then checked (ERC, DRC) and pictured. Each run is a new "
    "version; parts keep the places they had. Script basics (from skidl import * is done): "
    "r = Part('Device', 'R', value='1k', footprint='Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal'); "
    "vcc, gnd = Net('VCC'), Net('GND'); vcc += r[1]; r[2] & led['A']; led['K'] += gnd. EVERY part needs a real KiCad "
    "footprint (prefer through-hole: _THT, PinHeader_1x02_P2.54mm_Vertical for connectors). Name nets; give power "
    "nets a power flag or a connector. Start simple; reuse recipes from pcb_library.",
    {"goal": _p("What the circuit does, e.g. '5 V USB in, 3.3 V regulator, power LED, 2-pin header out'"),
     "code": _p("The SKiDL script"),
     "workspace": _p("Workspace name: a new name starts one; default: the active workspace"),
     "recipes": {"type": "array", "items": {"type": "string"},
                 "description": "Saved recipe names to include before the script (their functions become usable)"},
     "route": _p("false skips autorouting", "boolean"), "spacing_mm": _p("Gap between parts (default 8)", "number"),
     "track_mm": _p("Track width (default 0.5)", "number"), "width_mm": _p("Minimum board width", "number"),
     "height_mm": _p("Minimum board height", "number"),
     "keep_placement": _p("false re-places every part on the grid", "boolean"),
     "check": _p("false skips the visual check", "boolean")},
    ["goal", "code"]))
SERVER_TOOLS["pcb_library"] = (tool_pcb_library, _fn(
    "pcb_library",
    "PCB Designer's memory of what works. list (query optional): saved recipes (sub-circuits as SKiDL functions), "
    "confirmed pinouts, lessons. get: a recipe's code. save_recipe: keep a working sub-circuit (only after it built "
    "cleanly; marked draft). confirm_recipe: the user said it works. note: what went wrong and the fix (name ties it "
    "to a recipe). confirm_pinout: the user checked a part's pins (part, footprint, pins, source). forget: a recipe.",
    {"action": _p("list, get, save_recipe, confirm_recipe, note, confirm_pinout or forget"),
     "name": _p("Recipe name (lowercase-with-dashes)"), "description": _p("For save_recipe: what it does, values"),
     "code": _p("For save_recipe: SKiDL code, ideally @subcircuit def name(...)"),
     "text": _p("For note"), "query": _p("For list: filter words"),
     "part": _p("For confirm_pinout: the symbol"), "footprint": _p("For confirm_pinout"),
     "pins": _p("For confirm_pinout: e.g. 1=GND 2=VOUT 3=VIN"), "source": _p("For confirm_pinout: datasheet etc.")},
    ["action"]))


import hashlib

# ---- Auto mode: Stella picks the right mode for each message ----------------------------------------
# A General chat routes every message: a quick keyword score (instant), sticky by default, switching only when
# it's sure, never mid-job without a clear topic change, and learning from the user's corrections. Each switch
# starts a "lane"; lanes in other modes are folded into short notes so one long chat stays focused.
AUTO_MODES = {
    "general": ("General", None),
    "programmer": ("Programmer", re.compile(
        r"\b(code|coding|kotlin|python|javascript|typescript|java|rust|c\+\+|function|class|bug|debug|compile|"
        r"stack ?trace|exception|api|endpoint|sql|regex|refactor|unit tests?|apk|gradle|android app|web ?app|"
        r"repo|git|github|commit|pull request|script|app (idea|design|feature)|build an app|making an app)\b", re.I)),
    "electronics": ("Electronics", re.compile(
        r"\b(resistor|capacitor|transistor|mosfet|voltage|current|ohm|amps?|led|arduino|esp32|esp8266|raspberry pi|"
        r"pico|firmware|microcontroller|sensor|wiring|solder|multimeter|i2c|spi|uart|gpio|op-?amp|power supply)\b", re.I)),
    "modeler": ("3D Modeler", re.compile(
        r"\b(blender|3d model|vrm|vroid|avatar model|mesh|sculpt|uv|rig(ged|ging)?|armature|texture|glb|fbx|"
        r"character model|low ?poly|retopo)\b", re.I)),
    "cad": ("CAD Designer", re.compile(
        r"\b(freecad|cad|bracket|enclosure|mount(ing)? (plate|bracket)|stl|step file|3d print(ed|ing)?|fillet|"
        r"chamfer|tolerance|m[2-6] (screw|bolt|hole)s?|fan duct|adapter plate|standoff|case for)\b", re.I)),
    "pcb": ("PCB Designer", re.compile(
        r"\b(pcb|kicad|circuit board|schematic|gerbers?|footprints?|traces?|copper|drc|erc|silkscreen|"
        r"via|netlist|board layout|jlcpcb|pcbway)\b", re.I)),
    "writer": ("Writer", re.compile(
        r"\b(novel|chapter|short story|my story|character arc|plot|poem|fiction|manuscript|dialogue scene)\b", re.I)),
    "tutor": ("Tutor", re.compile(r"\b(teach me|explain like|help me (learn|understand)|quiz me|homework|study)\b",
                                  re.I)),
    "brainstorm": ("Brainstorm", re.compile(r"\b(brainstorm|ideas for|give me ideas|name ideas)\b", re.I)),
}
AUTO_WORK_MODES = {"modeler", "cad", "pcb"}        # modes with an open job: harder to leave by accident
AUTO_TOPIC_CUE = re.compile(
    r"\b(different (project|topic|thing)|something (else|different)|new (topic|project)|unrelated|switch(ing)? gears|"
    r"another (project|thing)|change of topic|moving on|on another note|separate project|next,? (can|could|let's))\b",
    re.I)
AUTO_SOFT_CUE = re.compile(r"\b(now|next|then|after that|also)\b.{0,30}\b(design|make|build|do|help|create|need|want|"
                           r"work on|start)\b|\bfor (my|the|a) (other|new|second)\b", re.I)
AUTO_BACK = re.compile(r"\b(?:back to|return to|returning to|go back to)\s+(?:the |my |our )?([a-z0-9 '-]{2,40})",
                       re.I)
AUTO_COMMAND = re.compile(
    r"\b(?:switch|change|go|move|put me)\s+(?:back\s+)?(?:to|into)\s+(?:the\s+)?([a-z0-9 ]{2,24}?)(?:\s+mode)?\b|"
    r"\buse\s+(?:the\s+)?([a-z0-9 ]{2,24}?)\s+mode\b", re.I)
AUTO_STATE = {"lock": threading.Lock(), "path": ""}


def _auto_db():
    try:
        with open(AUTO_STATE["path"], encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"chats": {}, "learned": []}


def _auto_save(db):
    os.makedirs(os.path.dirname(AUTO_STATE["path"]), exist_ok=True)
    tmp = AUTO_STATE["path"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f)
    os.replace(tmp, AUTO_STATE["path"])


def _fingerprint(text):
    t = re.sub(r"\(image:[0-9a-f]{16}\)|!\[[^\]]*\]|\[photo [0-9a-f]{16}\]", "", str(text or "")).lower()
    return hashlib.sha1(re.sub(r"\s+", " ", t).strip()[:300].encode()).hexdigest()[:16]


def _words(text):
    return set(re.findall(r"[a-z0-9]{3,}", str(text).lower()))


def _mode_by_name(name):
    n = re.sub(r"\s+", " ", str(name or "").lower()).strip()
    n = re.sub(r"\b(mode|designer|the)\b", "", n).strip()
    aliases = {"general": "general", "normal": "general", "default": "general", "chat": "general",
               "code": "programmer", "coding": "programmer", "programmer": "programmer", "programming": "programmer",
               "electronics": "electronics", "3d": "modeler", "3d modeler": "modeler", "modeler": "modeler",
               "blender": "modeler", "modeling": "modeler", "cad": "cad", "freecad": "cad", "pcb": "pcb",
               "kicad": "pcb", "circuit board": "pcb", "writer": "writer", "writing": "writer", "tutor": "tutor",
               "brainstorm": "brainstorm"}
    return aliases.get(n) or (n if n in AUTO_MODES else "")


def route_mode(data):
    """Which mode the next reply should use. In: chat_id, text, level (auto|ask|off), forced (a mode the user
    chose for this message), current (the mode the chat was in). Out: mode, switched, suggest, reason."""
    chat = str(data.get("chat_id") or "")[:80]
    text = str(data.get("text") or "")[:4000]
    level = str(data.get("level") or "auto").lower()
    forced = str(data.get("forced") or "")
    with AUTO_STATE["lock"]:
        db = _auto_db()
        lanes = db["chats"].setdefault(chat, {"lanes": []})["lanes"] if chat else []
        current = str(data.get("current") or (lanes[-1]["mode"] if lanes else "general"))
        if current not in AUTO_MODES:
            current = "general"
        mode, reason, suggest = current, "same topic", None
        cmd = AUTO_COMMAND.search(text)
        cmd_mode = _mode_by_name(next((g for g in cmd.groups() if g), "")) if cmd else ""
        if forced in AUTO_MODES:
            mode, reason = forced, "you chose it"
            if text.strip():    # learn: this kind of message belongs in this mode
                db["learned"] = ([l for l in db.get("learned", []) if _fingerprint(l["text"]) != _fingerprint(text)]
                                 + [{"text": text[:300], "mode": forced}])[-300:]
        elif cmd_mode:
            mode, reason = cmd_mode, "you asked to switch"
        elif AUTO_BACK.search(text) and len(lanes) > 1:
            # "back to the app": the earlier topic whose first message shares the most words
            want = _words(AUTO_BACK.search(text).group(1))
            best_lane = max(lanes[:-1], key=lambda l: len(want & _words(l["title"])), default=None)
            if best_lane and want & _words(best_lane["title"]):
                mode, reason = best_lane["mode"], "back to an earlier topic"
        elif level != "off":
            weight = {"modeler": 3, "cad": 3, "pcb": 3}
            scores = {m: len(rx.findall(text)) * weight.get(m, 2) for m, (_n, rx) in AUTO_MODES.items() if rx}
            words = _words(text)
            for l in db.get("learned", []):      # corrections the user made before
                lw = _words(l["text"])
                if lw and len(words & lw) / max(1, len(words | lw)) >= 0.5 and l["mode"] in scores:
                    scores[l["mode"]] += 4
            best = max(scores, key=lambda m: scores[m]) if scores else "general"
            top, here = scores.get(best, 0), scores.get(current, 0)
            cue = bool(AUTO_TOPIC_CUE.search(text))
            soft = cue or bool(AUTO_SOFT_CUE.search(text))
            if top == 0:
                if cue and current != "general":
                    mode, reason = "general", "new topic, nothing specific"
                else:
                    reason = "follow-up" if len(text.split()) <= 12 else "same topic"
            elif best == current or top <= here:
                pass
            elif current in AUTO_WORK_MODES and not soft and top < 6:
                suggest, reason = best, "in the middle of a job"
            elif top >= here + 2:
                mode, reason = best, "new topic" if soft else "the message is about this"
            else:
                suggest, reason = best, "not sure"
                pick = _helper_mode(text, current, best)
                if pick == best:
                    mode, suggest, reason = best, None, "the message is about this"
                elif pick == current:
                    suggest, reason = None, "same topic"
            if level == "ask" and mode != current:
                suggest, mode, reason = mode, current, "asking first"
        switched = mode != current
        if chat:
            if not lanes or switched:
                lanes.append({"mode": mode, "start": _fingerprint(text), "title": text.strip()[:80], "at": int(time.time())})
                del lanes[:-40]
            db["chats"][chat]["lanes"] = lanes
            if len(db["chats"]) > 2000:
                for k in sorted(db["chats"], key=lambda k: (db["chats"][k]["lanes"] or [{"at": 0}])[-1]["at"])[:200]:
                    db["chats"].pop(k, None)
            _auto_save(db)
    return {"mode": mode, "name": AUTO_MODES[mode][0], "previous": current, "switched": switched,
            "suggest": suggest if suggest and suggest != mode else None,
            "suggest_name": AUTO_MODES[suggest][0] if suggest and suggest != mode else None, "reason": reason}


def _helper_mode(text, current, best):
    """Asks the helper model which of two modes fits when the keywords can't tell (only when a helper runs)."""
    if not helper_up() or not text.strip():
        return ""
    names = {current: AUTO_MODES[current][0], best: AUTO_MODES[best][0]}
    body = {"model": "local", "temperature": 0, "max_tokens": 20, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": "You sort a user's chat message into the assistant mode that "
                          "should answer it. Answer with the mode id only."},
                         {"role": "user", "content": f"Modes: {current} = {names[current]} (the chat's current "
                          f"mode), {best} = {names[best]}.\nMessage: {text[:600]}\nWhich mode id?"}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "mode", "schema": {
                "type": "object", "properties": {"mode": {"type": "string", "enum": [current, best]}},
                "required": ["mode"]}}}}
    try:
        out = _quick_model(body, timeout=8, helper_timeout=6)["choices"][0]["message"].get("content") or ""
        return str(json.loads(re.search(r"\{[\s\S]*\}", out).group(0)).get("mode", ""))
    except (urllib.error.URLError, OSError, ValueError, AttributeError, KeyError, IndexError):
        return ""


FACT_LINE = re.compile(r"(\d+(\.\d+)?\s?(mm|cm|m\b|v\b|volts?|ma\b|a\b|amps?|ohms?|k\b|uf|nf|pf|mhz|ghz|°|deg|"
                       r"%|gb|mb)|\bv\d{1,4}\b|version \d+|\b(decided|we'll|let's go with|use the|chose|picked|"
                       r"agreed|must|needs? to|part number|model number)\b)", re.I)


def _lane_facts(msgs):
    """Short, verbatim facts from a folded lane: lines with measurements, versions and decisions."""
    facts, seen = [], set()
    for m in msgs:
        for line in re.split(r"(?<=[.!?])\s+|\n", str(m.get("content") or "")):
            line = line.strip(" -*•#")
            if 12 <= len(line) <= 220 and FACT_LINE.search(line) and line.lower() not in seen:
                seen.add(line.lower())
                facts.append(line)
    return facts[-10:]


def fold_lanes(chat_id, msgs):
    """(indices of messages to leave out, note about them) for a General chat that switched topics."""
    with AUTO_STATE["lock"]:
        lanes = _auto_db()["chats"].get(str(chat_id or ""), {}).get("lanes", [])
    if len(lanes) < 2:
        return set(), ""
    starts = {}
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            fp = _fingerprint(m.get("content"))
            for li, lane in enumerate(lanes):
                if lane["start"] == fp and li not in starts:
                    starts[li] = i
    if not starts:
        return set(), ""
    current = lanes[-1]["mode"]
    order = sorted(starts.items(), key=lambda x: x[1])
    drop, notes = set(), []
    for k, (li, begin) in enumerate(order):
        end = order[k + 1][1] if k + 1 < len(order) else len(msgs)
        lane = lanes[li]
        if lane["mode"] == current or li == len(lanes) - 1:
            continue
        drop.update(range(begin, end))
        facts = _lane_facts(msgs[begin:end])
        notes.append(f"- [{AUTO_MODES.get(lane['mode'], ('General',))[0]}] \"{lane['title']}\"" +
                     ("; facts: " + " | ".join(facts) if facts else ""))
    if not drop:
        return set(), ""
    note = ("Earlier topics in this chat, folded to keep you focused (the full messages are saved; search_chats "
            "finds them if exact details are needed):\n" + "\n".join(notes[-8:]))
    return drop, note


def tool_switch_mode(args):
    mode = _mode_by_name(args.get("mode")) or str(args.get("mode") or "")
    if mode not in AUTO_MODES:
        raise ValueError(f"mode must be one of: {', '.join(AUTO_MODES)}")
    chat = str(args.get("conversation_id") or "")
    if chat:
        with AUTO_STATE["lock"]:
            db = _auto_db()
            lanes = db["chats"].setdefault(chat, {"lanes": []})["lanes"]
            if not lanes or lanes[-1]["mode"] != mode:
                lanes.append({"mode": mode, "start": _fingerprint(args.get("message") or ""),
                              "title": str(args.get("reason") or "")[:80], "at": int(time.time())})
            _auto_save(db)
    return {"switched_to": mode, "name": AUTO_MODES[mode][0],
            "note": "Your instructions for that mode apply from your next step. Carry over any facts the user "
                    "will need (sizes, parts, decisions) and say you switched."}, f"Switched to {AUTO_MODES[mode][0]}"


SERVER_TOOLS["switch_mode"] = (tool_switch_mode, _fn(
    "switch_mode",
    "Auto mode: switch this chat to the mode that fits what the user is asking now, when the automatic choice "
    "missed it (e.g. the request is clearly CAD work but you're in General). Don't switch for small follow-ups.",
    {"mode": _p("general, programmer, electronics, modeler (Blender), cad (FreeCAD), pcb (KiCad), writer, tutor or "
                "brainstorm"),
     "reason": _p("A few words on why")}, ["mode"]))


def run_server_tool(name, args):
    entry = SERVER_TOOLS.get(name)
    if not entry:
        return {"ok": False, "error": f"unknown tool {name}"}, f"Unknown tool {name}"
    args = dict(args) if isinstance(args, dict) else {}
    max_chars = args.pop("_max_chars", None)
    try:
        result, summary = entry[0](args)
        result = {"ok": True, **result}
        if max_chars:
            result = shrink_result(result, int(max_chars))
        return result, summary
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out"}, "Timed out"
    except (ValueError, KeyError, TypeError, ZeroDivisionError, OverflowError, SyntaxError, RuntimeError,
            json.JSONDecodeError, OSError) as e:
        msg = str(e) or type(e).__name__
        if isinstance(e, KeyError):
            msg = f"missing value: {e.args[0]}"
        if isinstance(e, ZeroDivisionError):
            msg = "division by zero"
        return {"ok": False, "error": msg}, msg


# ----------------------------------------------------------------------------- activity + updates
ACTIVITY = {"active": 0, "last": time.time(), "lock": threading.Lock()}
WEB = None            # the running HTTP server, set in start()
RESTART_CODE = 75     # exit code that tells the launcher "start me again" (after an update)
EXIT_CODE = 0


def _tracked(fn):
    """Counts requests in progress, so updates wait until nobody is using Stella."""
    def wrapper(self):
        p = urlparse(self.path).path
        if p == "/health" or p == "/avatar/events" or p.startswith("/avatar/lib/"):
            return fn(self)     # health checks and avatar screens polling don't count as using Stella
        with ACTIVITY["lock"]:
            ACTIVITY["active"] += 1
        try:
            return fn(self)
        finally:
            with ACTIVITY["lock"]:
                ACTIVITY["active"] -= 1
                ACTIVITY["last"] = time.time()
    return wrapper


def llama_busy():
    """True while llama-server is generating (phones talk to it directly, not through the hub)."""
    try:
        slots = _llama("/slots", timeout=5)
        return any(s.get("is_processing") for s in slots) if isinstance(slots, list) else False
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False


def server_idle(quiet=120):
    with ACTIVITY["lock"]:
        busy = ACTIVITY["active"] > 0 or time.time() - ACTIVITY["last"] < quiet
    return not busy and not llama_busy()


def wait_until_idle(max_wait=6 * 3600, quiet=120):
    waited, step = 0, min(30, max(1, quiet))
    while not server_idle(quiet):
        time.sleep(step)
        waited += step
        if waited >= max_wait:
            return False
    return True


def _fetch(url, token="", timeout=60):
    headers = {"User-Agent": "StellAI-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def self_test():
    """Quick check that this file works, run on a downloaded update before it replaces the old one."""
    with tempfile.TemporaryDirectory() as tmp:
        db = ChatDB(os.path.join(tmp, "t.db"))
        db.sync(0, [{"id": "c", "title": "t", "created_at": 1, "updated_at": 1}], [])
        p = db.save_project({"name": "Test"})
        db.add_project_note(p["id"], "note")
        assert db.save_mode({"name": "Test mode", "temperature": 0.7})["id"].startswith("c-")
        r = db.add_reminder("test", int(time.time() * 1000) + 60_000, "daily")
        db.update_reminder(r["id"], done=True)
        db.add_items("Shopping list", ["flux"])
        assert db.lists()["shopping"][0]["text"] == "flux"
        assert "web_search" in SERVER_TOOLS and "github_commit" in APPROVAL_TOOLS
        assert ChatSearch._pieces([("user", "hi", 1), ("assistant", "hello", 2)])
        assert model_file_for("35b", 20, 6)[0].endswith("IQ3_S.gguf")
        assert db.project(p["id"])["notes"]
        assert chunk_text("a\n" * 3000)
        assert "render_diagram" in SERVER_TOOLS
        assert {"blender_project", "blender_step", "blender_inspect"} <= set(SERVER_TOOLS)
        assert not check_bpy_code("import bmesh\nbm = bmesh.new()\nbmesh.ops.create_cube(bm, size=0.1)\n"
                                  "o = new_object('Box', bm)\no.location.z = getattr(o, 'location').z + 1")
        assert check_bpy_code("import os") and check_bpy_code("open('x')") and check_bpy_code("bpy.ops.wm.quit_blender()")
        assert check_bpy_code("o.__class__") and check_bpy_code("bpy.data.texts.new('a')")
        compile(BLENDER_WORKER_PY, "worker", "exec")
        compile(SHAPE_PY, "shape", "exec")
        assert "blender_generate" in SERVER_TOOLS
        assert {"cad_project", "cad_step", "cad_inspect", "pcb_project", "pcb_check", "pcb_step", "pcb_circuit",
                "pcb_library"} <= set(SERVER_TOOLS)
        assert _skidl_check("generate_netlist(file_='/etc/x')") and _skidl_check("import os")
        assert not _skidl_check("r = Part('Device', 'R', value='1k', footprint='Resistor_THT:R')\nNet('VCC') += r[1]"
                                .replace("Net('VCC') += r[1]", "v = Net('VCC')\nv += r[1]"))
        compile(CAD_RUNNER_PY, "cad", "exec")
        assert "switch_mode" in SERVER_TOOLS and _mode_by_name("PCB designer") == "pcb"
        compile(PCB_RUNNER_PY, "pcb", "exec")
        compile(SKIDL_RUNNER_PY, "skidl", "exec")
        if TUNE:
            hw = {"cores": 8, "ram_gb": 32, "avx2": True,
                  "gpus": [{"index": 0, "name": "RTX 3060", "vram_gb": 12, "vendor": "nvidia"}]}
            assert TUNE.rank(hw, 26)["picks"] and TUNE.library(hw, 26, tmp)
        assert not helper_up() and _helper_mode("x", "general", "pcb") == ""
        assert "move_stella" in SERVER_TOOLS and resolve_move_target("http://100.64.0.5:8080/x") == ("100.64.0.5",
                                                                                                  "100.64.0.5")
        assert TOOL_TRIGGERS["move"].search("Stella, move yourself to the server")
        assert check_python_code("import subprocess") and check_python_code("open('/etc/passwd')")
        did = db.create_doc("t1", "Ch 1", "# One\nAlpha\n# Two\nBeta")
        assert db.save_doc(did, "# One\nAlpha!\n# Two\nBeta") == 2 and _find_section(db.doc(did)["text"], "text", "two") == (2, 4) and _find_section("# A\nx\n\n# B\ny", "text", "a") == (0, 2)
        assert not check_python_code("import numpy as np\nprint(np.sqrt(2))\nopen('out.csv', 'w')")
        db.sync(0, [{"id": "t1", "title": "t", "created_at": 1, "updated_at": 1}], [])
        n = db.add_chat_note("t1", "The bracket is 70 mm")
        assert db.chat_notes("t1")[0]["text"] == "The bracket is 70 mm" and db.delete_chat_note("t1", n["id"])
        assert not check_bpy_code("import Part\nadd('Base', box(40, 20, 5).cut(hole(3.2, 10, (5, 10, 6))))",
                                  allowed=CAD_IMPORTS, extra_bad=CAD_BAD_ATTRS)
        assert check_bpy_code("Part.read('/etc/x')", allowed=CAD_IMPORTS, extra_bad=CAD_BAD_ATTRS)
        assert check_bpy_code("import os", allowed=CAD_IMPORTS)
        assert check_bpy_code("pcbnew.SaveBoard('x', board)", allowed=PCB_IMPORTS, extra_bad=PCB_BAD_ATTRS)
        assert check_bpy_code("board.ExportSpecctraDSN('x')", allowed=PCB_IMPORTS, bad_prefixes=("Export",))
        assert not check_bpy_code("move('R1', 10, 5, 90)", allowed=PCB_IMPORTS, extra_bad=PCB_BAD_ATTRS)
        db.db.close()
    print("self-test ok")


RAW_BASE = os.environ.get("STELLAI_UPDATE_BASE", "https://raw.githubusercontent.com")


def check_for_update(repo, token="", path="server/stellai_hub.py"):
    """Downloads the hub from GitHub; if it differs from this file and passes its self-test, installs it.

    Returns True when a new version was installed (the caller restarts)."""
    import hashlib
    me = os.path.abspath(sys.argv[0] if sys.argv[0].endswith(".py") else __file__)
    new = _fetch(f"{RAW_BASE}/{repo}/HEAD/{path}", token)
    with open(me, "rb") as f:
        cur = f.read()
    digest = hashlib.sha256(new).hexdigest()
    if digest == hashlib.sha256(cur).hexdigest():
        return False
    try:
        with open(os.path.join(os.path.dirname(me), "stellai_hub.bad")) as f:
            if digest in f.read().split():
                return False   # this version already failed to start here; wait for a newer one
    except OSError:
        pass
    compile(new, "stellai_hub.py", "exec")                       # must at least be valid Python
    staged = me + ".new"
    with open(staged, "wb") as f:
        f.write(new)
    p = subprocess.run([sys.executable, staged, "--self-test"], capture_output=True, text=True, timeout=120)
    if p.returncode != 0 or "self-test ok" not in p.stdout:
        os.remove(staged)
        detail = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"the new version failed its self-test ({detail[0][:200]}); keeping this one")
    log("update downloaded and checked; installing when Stella is idle")
    if not wait_until_idle(quiet=IDLE_QUIET):
        os.remove(staged)
        return False
    import shutil
    shutil.copy2(me, me + ".bak")                               # the launcher restores this if the update fails
    os.replace(staged, me)
    return True


def update_companions(repo, token=""):
    """Files that live next to the hub and update with it: the model catalog (stellai_tune.py). Best effort."""
    if getattr(sys, "frozen", False):
        return
    here = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv[0].endswith(".py") else __file__))
    for name in ("stellai_tune.py",):
        try:
            new = _fetch(f"{RAW_BASE}/{repo}/HEAD/server/{name}", token)
            compile(new, name, "exec")
            dest = os.path.join(here, name)
            try:
                with open(dest, "rb") as f:
                    if f.read() == new:
                        continue
            except OSError:
                pass
            with open(dest + ".tmp", "wb") as f:
                f.write(new)
            os.replace(dest + ".tmp", dest)
            log(f"updated {name}")
        except Exception as e:
            log(f"couldn't update {name}: {e}")


UPDATE_INTERVAL = int(os.environ.get("STELLAI_UPDATE_INTERVAL", "1800"))   # 30 minutes
IDLE_QUIET = int(os.environ.get("STELLAI_IDLE_QUIET", "120"))             # no requests for 2 minutes


def updater_loop(repo, token="", interval=None):
    interval = interval or UPDATE_INTERVAL
    time.sleep(min(90, interval))
    while True:
        try:
            update_companions(repo, token)
            if check_for_update(repo, token):
                log("installed an update from GitHub; restarting the hub (chats and settings are kept)")
                global EXIT_CODE
                EXIT_CODE = RESTART_CODE
                if WEB:
                    WEB.shutdown()
                return
        except Exception as e:  # never let a failed check stop the hub
            log(f"update check failed: {e}")
        time.sleep(interval)


OPTIONAL_PACKAGES = {"pypdf": "pypdf"}   # import name -> pip package; installed into the hub's venv if missing


def ensure_packages():
    import importlib.util
    if getattr(sys, "frozen", False) or sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        return   # only inside our own virtualenv
    for mod, pkg in OPTIONAL_PACKAGES.items():
        if importlib.util.find_spec(mod) is None:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", pkg], capture_output=True, timeout=600)
            log(f"installed {pkg}" if r.returncode == 0 else f"couldn't install {pkg}")


# ----------------------------------------------------------------------------- desktop web chat
# The page is kept in server/webchat.html and copied here by server/embed_webchat.py.
WEBCHAT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StellAI</title>
<script type="importmap">{"imports":{"three":"/avatar/lib/build/three.module.js","three/addons/":"/avatar/lib/examples/jsm/","@pixiv/three-vrm":"/avatar/lib/three-vrm.module.min.js","@pixiv/three-vrm-core":"/avatar/lib/three-vrm.module.min.js","@pixiv/three-vrm-animation":"/avatar/lib/three-vrm-animation.module.min.js"}}</script>
<style>
:root{--void:#04060d;--top:#070b1a;--panel:#0c1326;--panel2:#141d38;--line:#26335c;--text:#e6ecff;--muted:#8a96bf;
--accent:#5ce1e6;--glow:#9b7bff;--gold:#ffd166;--red:#ff6b7a;--mono:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--void);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{-webkit-tap-highlight-color:transparent}
body{background:radial-gradient(ellipse at 20% 0%,#0b1430 0%,var(--top) 45%,#0b0620 100%) fixed}
button{font:inherit;color:inherit;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--accent)}button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);color:#0a0a14;border-color:var(--accent);font-weight:600}
button.ghost{background:none;border-color:transparent}button.ghost:hover{border-color:var(--line)}
input,textarea,select{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 10px;width:100%}
textarea{resize:vertical}input:focus,textarea:focus{outline:none;border-color:var(--accent)}
a{color:var(--accent)}
.hud{font-family:var(--mono);letter-spacing:2px;text-transform:uppercase;font-size:11px;color:var(--muted)}
#app{display:flex;height:100vh}
#side{width:290px;flex:none;background:rgba(12,19,38,.92);border-right:1px solid var(--line);display:flex;flex-direction:column}
#side header{padding:18px 18px 10px;color:var(--accent);font-family:var(--mono);letter-spacing:3px;font-weight:700}
#side .scroll{flex:1;overflow:auto;padding:0 10px 10px}
.item{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:10px;cursor:pointer;color:var(--text)}
.item:hover{background:rgba(255,255,255,.04)}.item.sel{background:rgba(92,225,230,.12);color:var(--accent)}
.item .t{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .x{opacity:0;border:none;background:none;padding:0 4px;color:var(--muted)}.item:hover .x{opacity:1}
.sec{display:flex;align-items:center;justify-content:space-between;padding:12px 10px 4px}
.sub{font-size:11px;color:var(--muted)}
#side footer{border-top:1px solid var(--line);padding:8px 12px;display:flex;gap:6px;flex-wrap:wrap}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#docs{width:min(40vw,640px);flex:none;display:none;flex-direction:column;background:rgba(12,19,38,.95);border-left:1px solid var(--line);min-width:0}
body.docs #docs{display:flex}
#docs .dh{display:flex;align-items:center;gap:6px;padding:10px 12px;border-bottom:1px solid var(--line)}
#docs .dh select{flex:1;min-width:0}
#docs .dbody{flex:1;overflow:auto;padding:12px 16px}
#docs textarea{width:100%;height:100%;min-height:60vh;font-family:var(--mono);font-size:13px;resize:none}
#docs pre.code{white-space:pre;overflow:auto;font-family:var(--mono);font-size:12.5px;margin:0}
#docs .dfoot{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:8px 12px;border-top:1px solid var(--line)}#docs .dfoot select{width:auto;max-width:190px;flex:none}
#docs .outline{display:flex;gap:4px;flex-wrap:wrap;padding:6px 12px 0}
#docs .outline button{font-size:11px;padding:2px 8px}
.docref{border:1px solid rgba(92,225,230,.45);border-radius:8px;padding:1px 8px;color:var(--accent);cursor:pointer;background:none;font:inherit}
@media (max-width:1000px){#docs{position:fixed;right:0;top:0;bottom:0;z-index:6;width:100%}}
#bar{display:flex;align-items:center;gap:10px;padding:10px 16px;background:rgba(4,6,13,.55);border-bottom:1px solid rgba(92,225,230,.15)}
#bar .title{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip{border:1px solid rgba(92,225,230,.45);border-radius:999px;padding:3px 10px;font-family:var(--mono);font-size:11px;letter-spacing:1px;color:var(--accent);cursor:pointer;background:none;white-space:nowrap}
#meter{display:flex;align-items:center;gap:6px;cursor:pointer}
#meter .track{width:70px;height:4px;background:var(--line);border-radius:2px;overflow:hidden}#meter .fill{height:100%;background:var(--accent)}
#msgs{flex:1;overflow:auto;padding:18px 16px 30px}
.wrap{max-width:860px;margin:0 auto;display:flex;flex-direction:column;gap:12px}
.msg{position:relative;max-width:85%;padding:10px 14px;border-radius:18px}
.msg.user{align-self:flex-end;background:linear-gradient(135deg,rgba(92,225,230,.28),rgba(155,123,255,.28));border:1px solid rgba(92,225,230,.45);border-bottom-right-radius:4px;white-space:pre-wrap}
.msg.assistant{align-self:flex-start;background:rgba(12,19,38,.85);border:1px solid rgba(92,225,230,.22);border-bottom-left-radius:4px}
.msg.assistant.wide{max-width:100%;width:100%}
.msg .who{font-family:var(--mono);font-size:10px;letter-spacing:2px;color:var(--accent);opacity:.8;margin-bottom:4px}
.msg .acts{position:absolute;top:-12px;right:8px;display:none;gap:4px}.msg:hover .acts{display:flex}
.msg .acts button{padding:1px 8px;font-size:12px;border-radius:8px}
.tool{font-family:var(--mono);font-size:13px;color:var(--gold);padding:0 10px}
.err{background:rgba(255,107,122,.12);border:1px solid rgba(255,107,122,.5);border-radius:12px;padding:10px 12px;color:#ffc7cd}
.summary{border:1px solid rgba(155,123,255,.4);background:rgba(155,123,255,.08);border-radius:12px;padding:10px 14px;cursor:pointer}
.summary .hud{color:var(--glow)}
.think{font-family:var(--mono);font-size:12px;color:var(--glow);font-style:italic;white-space:pre-wrap;cursor:pointer;margin-bottom:6px}
.md p{margin:.35em 0}.md h1,.md h2,.md h3{margin:.6em 0 .3em;font-size:1.05em}.md ul,.md ol{margin:.3em 0;padding-left:1.4em}
.md code{font-family:var(--mono);background:rgba(255,255,255,.07);padding:1px 5px;border-radius:5px;font-size:.92em}
.md pre{background:#060a16;border:1px solid var(--line);border-radius:10px;padding:10px 12px;overflow:auto;position:relative}
.md pre code{background:none;padding:0;font-size:13px;line-height:1.45}
.md pre .cp{position:absolute;top:6px;right:6px;font-size:11px;padding:1px 8px}
.md img{max-width:100%;background:#fff;border-radius:8px;margin:6px 0;cursor:zoom-in}
.md blockquote{border-left:3px solid var(--line);margin:.4em 0;padding-left:10px;color:var(--muted)}
.md table{border-collapse:collapse}.md td,.md th{border:1px solid var(--line);padding:3px 8px}
#composer{border-top:1px solid rgba(92,225,230,.18);background:rgba(4,6,13,.7);padding:10px 16px}
#composer .row{max-width:860px;margin:0 auto;display:flex;gap:8px;align-items:flex-end}
#input{min-height:46px;max-height:240px;border-radius:22px;padding:11px 16px}
#atts{max-width:860px;margin:0 auto 6px;display:flex;gap:8px;flex-wrap:wrap}
#atts .a{position:relative}#atts img{height:60px;border-radius:8px;border:1px solid var(--line)}
#atts .a button{position:absolute;top:-6px;right:-6px;padding:0 6px;border-radius:50%;font-size:11px}
#notice{max-width:860px;margin:0 auto 6px;font-size:13px;color:var(--gold);display:none;cursor:pointer}
.empty{margin:auto;text-align:center;color:var(--muted);padding:60px 20px}.empty .star{font-size:44px;color:var(--accent)}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:10;padding:16px}
.dialog{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px 20px;width:min(640px,100%);max-height:88vh;overflow:auto}
.dialog h2{margin:0 0 10px;font-size:18px}.dialog .btns{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}
.list .row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(38,51,92,.6)}
.list .row .t{flex:1;min-width:0}
.pick{display:flex;gap:10px;align-items:center;padding:9px 8px;border-radius:10px;cursor:pointer}.pick:hover{background:rgba(255,255,255,.04)}
.li{width:1.2em;height:1.2em;vertical-align:-.22em;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex:none;margin-right:.4em}
.li.solo{margin-right:0}.li.ok{color:var(--accent)}.li.bad{color:var(--red)}.pick .ic .li,.check .ic .li{margin:0}
#search{padding-left:34px;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%238a96bf' stroke-width='1.6' stroke-linecap='round'%3E%3Cpath d='M10.5 4a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13Z M15.3 15.3L20 20'/%3E%3C/svg%3E") no-repeat 10px 50%/17px}
.pick .ic{font-size:20px;width:30px;text-align:center}.pick.sel{color:var(--accent)}
.status{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted)}.status.on{background:var(--accent);box-shadow:0 0 8px var(--accent)}
.hit .ex{font-size:12px;color:var(--muted);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.check{display:flex;gap:10px;padding:5px 0}.check .ic{width:18px;flex:none;font-weight:700}
.bar2{height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin-top:6px}.bar2 div{height:100%;background:var(--accent)}
.zoom{position:fixed;inset:0;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:center;z-index:20;cursor:zoom-out}
.zoom img{max-width:95vw;max-height:95vh;background:#fff;border-radius:8px}
#stage{display:none;position:relative;flex:1;min-height:0;overflow:hidden}
#avatarHost{position:absolute;inset:0}
/* subtitles: a small window below or beside her (per screen), or the classic film-style line over her */
#caption{position:absolute;z-index:2;box-sizing:border-box;padding:10px 14px;border:1px solid var(--line);border-radius:12px;background:rgba(8,12,24,.82);
  font-size:15px;line-height:1.45;color:var(--text);text-align:left;overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin;transition:opacity .5s;opacity:0}
#caption .who{display:block;font-family:var(--mono);font-size:10px;letter-spacing:3px;color:var(--accent);margin-bottom:3px}
body:not([data-cap=over]) #caption{opacity:.45}
#caption.on,body[data-cap] #caption.on{opacity:1}
#caption:empty::before{content:"Subtitles appear here when she talks";color:var(--muted);font-size:13px}
body[data-cap=below] #caption{left:50%;transform:translateX(-50%);bottom:14px;width:min(620px,calc(100% - 28px));height:clamp(78px,17vh,132px)}
body[data-cap=below] #avatarHost{bottom:calc(clamp(78px,17vh,132px) + 24px)}
body[data-cap=side] #caption{right:14px;top:50%;transform:translateY(-50%);width:min(340px,34%);max-height:62%;min-height:90px}
body[data-cap=side] #avatarHost{right:calc(min(340px,34%) + 24px)}
@media (orientation:portrait) and (max-width:760px){
  body[data-cap=side] #caption{right:auto;top:auto;left:50%;transform:translateX(-50%);bottom:14px;width:calc(100% - 28px);max-height:none;min-height:0;height:clamp(78px,17vh,132px)}
  body[data-cap=side] #avatarHost{right:0;bottom:calc(clamp(78px,17vh,132px) + 24px)}}
body[data-cap=over] #caption{left:50%;bottom:22px;transform:translateX(-50%);width:min(900px,92%);text-align:center;font-size:clamp(17px,2.4vw,27px);line-height:1.35;
  background:none;border:none;padding:0;overflow:hidden;text-shadow:0 2px 14px #000,0 0 3px #000;pointer-events:none}
body[data-cap=over] #caption:empty::before{content:none}
body:not([data-cap=over]) #caption .who{position:sticky;top:-10px;margin:-10px -14px 3px;padding:8px 14px 3px;background:rgb(10,14,27)}
body[data-cap=off] #caption{display:none}
#avName{position:absolute;top:14px;left:18px;font-family:var(--mono);letter-spacing:6px;color:var(--accent);font-weight:700}
#avState{position:absolute;top:36px;left:18px;font-family:var(--mono);font-size:12px;letter-spacing:2px;color:var(--muted)}
#avStatus{position:absolute;top:58px;left:18px;right:18px;font-size:12px;color:var(--gold);display:none}
#stageBar{position:absolute;top:10px;right:12px;display:flex;gap:6px;opacity:.4;transition:opacity .2s;z-index:2}#stageBar:hover{opacity:1}
#tapStart{position:absolute;inset:0;display:none;align-items:center;justify-content:center;flex-direction:column;gap:8px;background:rgba(4,6,13,.6);font-size:22px;cursor:pointer;z-index:3}
#mic.on{background:var(--red);border-color:var(--red);color:#fff}
body.avatar #side,body.avatar #bar,body.avatar #docs{display:none}
body.avatar{height:100dvh;overflow:hidden;overscroll-behavior:none;touch-action:manipulation}
body.avatar #stage{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
body.avatar #stage{display:block}
body.avatar #msgs{display:none}
body.avatar.showchat #msgs{display:block;flex:0 0 40%;border-top:1px solid var(--line);background:rgba(4,6,13,.6)}
body.avatar #composer{background:rgba(4,6,13,.35);border-top:none}
body.inapp #composer,body.inapp #avChat,body.inapp #avFull{display:none}
body.inapp #avName{top:56px;left:60px}body.inapp #avState{top:78px;left:60px}body.inapp #avStatus{top:100px}body.inapp #stageBar{top:48px}
body.inapp #caption{bottom:110px!important}
body.inapp[data-cap=below] #avatarHost,body.inapp[data-cap=side] #avatarHost{bottom:calc(clamp(78px,17vh,132px) + 120px)}
@media (orientation:landscape){body.inapp[data-cap=side] #caption{bottom:auto!important}body.inapp[data-cap=side] #avatarHost{bottom:0}}
@media (max-width:760px){#side{position:fixed;z-index:5;height:100%;transform:translateX(-100%);transition:transform .2s}#side.open{transform:none}}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <header><span data-ic="sparkle"></span>STELLAI</header>
    <div style="padding:0 12px 6px"><button class="primary" style="width:100%" id="newChat">+ New chat</button></div>
    <div style="padding:0 12px 6px"><input id="search" type="search" placeholder="Search past chats" autocomplete="off"></div>
    <div class="scroll" id="searchView" style="display:none"><div id="searchResults"></div></div>
    <div class="scroll" id="listView">
      <div class="sec"><span class="hud">Projects</span><button class="ghost" id="newProject" title="New project">+</button></div>
      <div id="projects"></div>
      <div class="sec"><span class="hud">Chats</span></div>
      <div id="chats"></div>
    </div>
    <footer>
      <button class="ghost" id="openLibrary"><span data-ic="library"></span>Library</button>
      <button class="ghost" id="openTasks"><span data-ic="alarm"></span>Reminders</button>
      <button class="ghost" id="openMemory"><span data-ic="memory"></span>Memory</button>
      <button class="ghost" id="openServer"><span data-ic="server"></span>Server</button>
      <button class="ghost" id="openSettings"><span data-ic="settings"></span>Settings</button>
      <div class="sub" id="syncState" style="width:100%"></div>
    </footer>
  </aside>
  <main id="main">
    <div id="bar">
      <button class="ghost" id="menuBtn" style="display:none"><span data-ic="menu" data-solo></span></button>
      <span class="status" id="dot"></span>
      <div class="title"><div id="title" class="hud" style="font-size:14px;color:var(--text);letter-spacing:4px">STELLA</div><div id="subtitle" class="sub"></div></div>
      <button class="chip" id="modeChip"></button>
      <button class="chip" id="projectChip" style="display:none"></button>
      <button class="chip" id="notesChip" title="Pinned notes: facts kept in mind for this whole chat"><span data-ic="pin" data-solo></span></button>
      <button class="chip" id="docsChip" title="Documents beside this chat: long code, chapters and write-ups"><span data-ic="doc" data-solo></span></button>
      <div id="meter" title="How full Stella's memory is for this chat"><span class="hud">Context</span><div class="track"><div class="fill" id="meterFill" style="width:0"></div></div><span class="hud" id="meterPct"></span></div>
    </div>
    <div id="stage">
      <div id="avatarHost"></div>
      <div id="avName">STELLA</div><div id="avState"></div><div id="avStatus"></div>
      <div id="stageBar"><button class="ghost" id="avNew" title="New chat">＋</button><button class="ghost" id="avChat" title="Show the chat"><span data-ic="chat" data-solo></span></button>
        <button class="ghost" id="avSet" title="Avatar settings"><span data-ic="settings" data-solo></span></button><button class="ghost" id="avFull" title="Full screen"><span data-ic="fullscreen" data-solo></span></button></div>
      <div id="caption"></div>
      <div id="tapStart"><div><span data-ic="sparkle"></span>Tap to wake Stella</div><div class="sub">Browsers need one tap before a page can talk</div></div>
    </div>
    <div id="msgs"><div class="wrap" id="wrap"></div></div>
    <div id="composer">
      <div id="notice"></div>
      <div id="atts"></div>
      <div class="row">
        <button class="ghost" id="attach" title="Add a photo"><span data-ic="camera" data-solo></span></button>
        <input type="file" id="file" accept="image/*" multiple hidden>
        <textarea id="input" rows="1" placeholder="Message Stella… (Enter to send, Shift+Enter for a new line)"></textarea>
        <button class="ghost" id="mic" title="Talk (Space on the avatar screen)"><span data-ic="mic" data-solo></span></button>
        <button class="primary" id="send">Send</button>
      </div>
    </div>
  </main>
  <aside id="docs">
    <div class="dh"><select id="docSel"></select><button class="ghost" id="docNew" title="New document">＋</button><button class="ghost" id="docClose" title="Close"><span data-ic="close" data-solo></span></button></div>
    <div class="outline" id="docOutline"></div>
    <div class="dbody" id="docBody"><p class="sub">No documents in this chat yet.</p></div>
    <div class="dfoot"><span class="sub" id="docInfo" style="flex:1"></span><button class="ghost" id="docCopy">Copy</button><button class="ghost" id="docDl">Download</button>
      <select id="docVer" title="Earlier versions"></select><button id="docEdit">Edit</button></div>
  </aside>
</div>
<script>
"use strict";
// ---------------------------------------------------------------- basics
const $ = s => document.querySelector(s);
// ---------------------------------------------------------------- line icons (instead of emoji; same style as the phone app)
// 24×24 outlines drawn with the current text colour. icon(name) gives the SVG; elements with data-ic get theirs at start.
const ICON_PATHS = {
  sparkle: "M12 2.5C12.9 8.2 15.8 11.1 21.5 12C15.8 12.9 12.9 15.8 12 21.5C11.1 15.8 8.2 12.9 2.5 12C8.2 11.1 11.1 8.2 12 2.5Z M19 3v3 M17.5 4.5h3",
  search: "M10.5 4a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13Z M15.3 15.3L20 20",
  library: "M4 5h4v14H4Z M8 7h4v12H8Z M13.2 6.6l3.8-1 3.4 12.6-3.8 1Z",
  alarm: "M12 6.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13Z M12 9.5V13l2.2 1.6 M4 6.2L7 3.6 M20 6.2L17 3.6 M7.5 18.5L6 20.5 M16.5 18.5L18 20.5",
  memory: "M9.5 4.5C7 4.5 5 6.4 5 8.8c-1.2.6-2 1.8-2 3.2 0 1.5.9 2.8 2.2 3.3.2 2.2 2 3.7 4.3 3.7h.5V4.5Z M14.5 4.5C17 4.5 19 6.4 19 8.8c1.2.6 2 1.8 2 3.2 0 1.5-.9 2.8-2.2 3.3-.2 2.2-2 3.7-4.3 3.7H14V4.5Z M10 4.5h4 M10 19h4 M7.5 10.5c1 0 2 .6 2.5 1.5 M16.5 10.5c-1 0-2 .6-2.5 1.5",
  server: "M4 4.5h16v6H4Z M4 13.5h16v6H4Z M7.5 7.5h.01 M7.5 16.5h.01 M11 7.5h5 M11 16.5h5",
  settings: "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6Z M12 2.8l1.4 2.4 2.7-.6.6 2.7 2.4 1.4-1.2 2.5 1.2 2.5-2.4 1.4-.6 2.7-2.7-.6L12 21.2l-1.4-2.4-2.7.6-.6-2.7-2.4-1.4 1.2-2.5-1.2-2.5 2.4-1.4.6-2.7 2.7.6Z",
  menu: "M4 7h16 M4 12h16 M4 17h16",
  pin: "M9 3.5h6 M10 3.5v6l-3.5 4h11L14 9.5v-6 M12 13.5V21",
  doc: "M6 3h8l4 4v14H6Z M14 3v4h4 M9 12h6 M9 15.5h6 M9 9h2.5",
  chat: "M4 5h16v11H9l-5 4Z M8 9.5h8 M8 12.5h5",
  fullscreen: "M4 9V4h5 M15 4h5v5 M20 15v5h-5 M9 20H4v-5",
  camera: "M3.5 8h4l1.5-2.5h6L16.5 8h4v11h-17Z M12 10.5a3.5 3.5 0 1 0 0 7a3.5 3.5 0 1 0 0-7Z",
  mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z M6 11.5a6 6 0 0 0 12 0 M12 17.5V21 M9 21h6",
  close: "M6 6l12 12 M18 6L6 18",
  trash: "M4.5 6.5h15 M9.5 6.5V4h5v2.5 M6.5 6.5l1 13.5h9l1-13.5 M10.2 10v6.5 M13.8 10v6.5",
  keyboard: "M3 6.5h18v11H3Z M6.5 10h.01 M9.5 10h.01 M12.5 10h.01 M15.5 10h.01 M18 10h-.5 M6.5 13h.01 M17.5 13h.01 M9 14.5h6",
  pen: "M15.5 4.5l4 4L9 19l-5 1 1-5Z M13.5 6.5l4 4",
  bolt: "M13.5 2.5L5 13.5h6l-1 8 8.5-11H12.5Z",
  cube: "M12 3l8 4.5v9L12 21l-8-4.5v-9Z M4 7.5l8 4.5 8-4.5 M12 12v9",
  ruler: "M4 20V4l16 16Z M8 16v-3.5l3.5 3.5Z M4 8h2 M4 12h2",
  plug: "M9 3v5 M15 3v5 M6.5 8h11v3a5.5 5.5 0 0 1-11 0Z M12 16.5V21",
  school: "M2.5 9.5L12 5l9.5 4.5L12 14Z M6.5 11.5v4.5c1.5 1.5 3.5 2.2 5.5 2.2s4-.7 5.5-2.2v-4.5 M21.5 9.5v5",
  bulb: "M9 17.5h6 M10 20.5h4 M12 3a6 6 0 0 0-3.6 10.8c.4.3.6.8.6 1.3v.4h6v-.4c0-.5.2-1 .6-1.3A6 6 0 0 0 12 3Z",
  scroll: "M7 4h11a2 2 0 0 1 0 4H17v10a2 2 0 0 1-2 2H5a2 2 0 0 1 0-4h1V6a2 2 0 0 1 2-2Z M5 16h9a2 2 0 0 0 2 2 M9.5 9h4 M9.5 12h4",
  game: "M7 8h10a4 4 0 0 1 3.9 4.8l-.8 3.6a2 2 0 0 1-3.4.9L14.5 15h-5l-2.2 2.3a2 2 0 0 1-3.4-.9l-.8-3.6A4 4 0 0 1 7 8Z M8 10.5v3 M6.5 12h3 M15.5 11h.01 M17 13h.01",
  food: "M6 3v7a2 2 0 0 0 4 0V3 M8 3v18 M16.5 3c-1.7 1-2.5 3.2-2.5 6v4h2.5v8",
  flask: "M9.5 3h5 M10 3v6.5L4.8 18.3A1.8 1.8 0 0 0 6.4 21h11.2a1.8 1.8 0 0 0 1.6-2.7L14 9.5V3 M7.5 14.5h9",
  music: "M9 18V5.5l11-2V16 M6.5 15.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 1 0 0-5Z M17.5 13.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 1 0 0-5Z",
  chart: "M4 4v16h16 M7 15l4-4 3 3 5-6",
  map: "M9 4L3.5 6v14L9 18l6 2 5.5-2V4L15 6Z M9 4v14 M15 6v14",
  wrench: "M14.7 6.3a4 4 0 0 0 5 5L21 10a5.5 5.5 0 0 1-7-7l-1.3 1.3Z M13.2 8.8L4 18a1.4 1.4 0 0 0 2 2l9.2-9.2",
  heart: "M12 20s-7.5-4.5-7.5-10A4.3 4.3 0 0 1 12 7.3 4.3 4.3 0 0 1 19.5 10c0 5.5-7.5 10-7.5 10Z",
  moon: "M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10Z",
  star: "M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9Z",
  code: "M8.5 7.5L4 12l4.5 4.5 M15.5 7.5L20 12l-4.5 4.5 M13.5 5l-3 14",
  image: "M3.5 5h17v14h-17Z M3.5 16l5-5 4 4 2.5-2.5 5.5 5.5 M15.5 8.5h.01",
  note: "M5 4h14v16H5Z M8.5 8.5h7 M8.5 12h7 M8.5 15.5h4",
  folder: "M3.5 6.5h6l2 2h9v10h-17Z",
  play: "M8 5.5v13l10.5-6.5Z",
  check: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18Z M8 12.3l2.7 2.7L16 9.7",
  cross: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18Z M9 9l6 6 M15 9l-6 6",
  warn: "M12 4L2.8 19.5h18.4Z M12 10v4.5 M12 17.2h.01",
  swap: "M4 8.5h14 M14.5 5L18 8.5 14.5 12 M20 15.5H6 M9.5 12L6 15.5 9.5 19",
  redo: "M5 5v6a4 4 0 0 0 4 4h10 M15.5 11.5L19 15l-3.5 3.5",
  diamond: "M12 3.5L20.5 12 12 20.5 3.5 12Z",
  thought: "M12 4.5a7.5 7.5 0 1 0 0 15a7.5 7.5 0 1 0 0-15Z M9 12h.01 M12 12h.01 M15 12h.01",
  dot: "M12 7a5 5 0 1 0 0 10a5 5 0 1 0 0-10Z",
  info: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18Z M12 11v5.5 M12 7.8h.01",
};
function icon(name, cls = "") {
  return `<svg class="li${cls ? " " + cls : ""}" viewBox="0 0 24 24" aria-hidden="true"><path d="${ICON_PATHS[name] || ICON_PATHS.sparkle}"/></svg>`;
}
// A mode's icon: modes store a symbol (shared with the phone app), drawn here as a line icon
const MODE_ICON = { "⌨": "keyboard", "✒": "pen", "✍": "pen", "✏": "pen", "⚡": "bolt", "🎓": "school", "💡": "bulb", "📜": "scroll",
  "🎮": "game", "🍳": "food", "🧪": "flask", "🎵": "music", "🎶": "music", "📈": "chart", "🗺": "map", "🛠": "wrench", "🔧": "wrench",
  "❤": "heart", "♥": "heart", "🌙": "moon", "★": "star", "⭐": "star", "💻": "code", "◆": "cube", "🧊": "cube", "📐": "ruler", "🔌": "plug" };
const modeIc = sym => icon(MODE_ICON[String(sym || "").trim()] || "sparkle");
// Stored marks at the start of a line ("✓ Saved", "⇄ Auto: CAD") drawn as icons
const MARKS = { "✓": ["check", "ok"], "✗": ["cross", "bad"], "⚠": ["warn", "bad"], "⇄": ["swap", ""], "↪": ["redo", ""] };
function markedLine(text) {
  const t = String(text || "").trimStart(); const m = MARKS[t[0]];
  return m ? icon(m[0], m[1]) + esc(t.slice(1).trim()) : esc(t);
}
const fileIc = kind => icon(kind === "pdf" ? "doc" : kind === "image" ? "image" : kind === "code" ? "code" : "note");
document.querySelectorAll("[data-ic]").forEach(el => { el.outerHTML = icon(el.dataset.ic, el.hasAttribute("data-solo") ? "solo" : ""); });
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
  const r = Math.random() * 16 | 0; return (c === "x" ? r : (r & 3 | 8)).toString(16); }));
const hex16 = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, "0")).join("");
let lastTs = 0;
const nextTs = () => { const n = Date.now(); lastTs = n > lastTs ? n : lastTs + 1; return lastTs; };
const store = { get(k, d) { try { const v = localStorage.getItem("stellai." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
                set(k, v) { try { localStorage.setItem("stellai." + k, JSON.stringify(v)); } catch {} } };

// Key from the link the server app opens (#key=...), or saved earlier
(function () {
  const m = location.hash.match(/key=([^&]+)/);
  if (m) { store.set("key", decodeURIComponent(m[1])); history.replaceState(null, "", location.pathname + location.search); }
})();
const AVATAR_MODE = location.pathname === "/avatar";
if (AVATAR_MODE) { document.body.classList.add("avatar"); if (new URLSearchParams(location.search).get("app") === "1") document.body.classList.add("inapp"); }
// In the phone app's avatar screen, tell the app about errors that stop the page (it shows them)
if (window.StellaiApp) {
  window.addEventListener("error", e => { try { StellaiApp.error(String(e.message || e)); } catch {} });
  window.addEventListener("unhandledrejection", e => { try { StellaiApp.error(String((e.reason && e.reason.message) || e.reason)); } catch {} });
}
let KEY = store.get("key", "");
const NAME = () => store.get("name", "Stella");

async function api(path, opts = {}) {
  const headers = Object.assign({ Authorization: "Bearer " + KEY }, opts.headers || {});
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  const text = await r.text();
  let data = null; try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (r.status === 410 && data && data.moved_to) followMove(data.moved_to, data.moved_from);
  if (!r.ok) { const e = new Error((data && data.error) || ("HTTP " + r.status)); e.status = r.status; throw e; }
  return data;
}
// Stella moved to another server: this page follows her there (the key travels in the #fragment, as always)
let MOVING_TO = null;
function followMove(to, from) {
  if (MOVING_TO || !to || !to.hub_url) return;
  MOVING_TO = to.hub_url;
  const q = new URLSearchParams(location.search); if (from) q.set("moved_from", from);
  setTimeout(() => location.replace(to.hub_url + location.pathname + "?" + q.toString() + "#key=" + encodeURIComponent(KEY)), 1500);
}
const MOVED_FROM = new URLSearchParams(location.search).get("moved_from");
if (MOVED_FROM !== null) { const q = new URLSearchParams(location.search); q.delete("moved_from");
  history.replaceState(null, "", location.pathname + (q.toString() ? "?" + q.toString() : "")); }
function movedHello() {     // the first thing on the new server: say so, on this screen only
  if (MOVED_FROM === null) return;
  const text = `I'm here now${MOVED_FROM ? `: I moved over from ${MOVED_FROM}` : ""}, and everything came with me.`;
  if (AVATAR_MODE) { avatarCaption(text, "stella"); if (AV.cfg && AV.cfg.speak) { try { SPEAK.say(text); } catch {} } }
  else if (typeof notice === "function") notice(text);
}
const media = ref => { const [k, id] = ref.split(":"); return (k === "image" ? `/images/${id}.jpg` : `/diagrams/${id}.png`) + "?key=" + encodeURIComponent(KEY); };

// ---------------------------------------------------------------- modes (same as the phone app)
const MODES = [
  { id: "general", name: "General", icon: "✦", desc: "Everyday help and quick answers", prompt: "" },
  { id: "programmer", name: "Programmer", icon: "⌨", desc: "Pair programming: complete code, debugging, reviews", temperature: 0.3, thinking: "on",
    prompt: "Mode: Programmer. Act as a senior software engineer pair-programming with the user.\n- Give complete, working code in fenced blocks with a language tag. For changes to existing code, show the whole changed function or file section, not fragments.\n- Explain briefly why, not just what. Point out bugs, edge cases, performance and security issues you notice.\n- When debugging, reason from the actual error message and code; if they're missing, ask for them instead of guessing.\n- Follow the user's language, framework and code style. Prefer simple, maintainable solutions.\n- Replies can be as long as the code needs." },
  { id: "writer", name: "Writer", icon: "✒", desc: "Creative writing partner for novels and stories", temperature: 0.9, thinking: "off", tools: "minimal", style: "story",
    prompt: "Mode: Writer. You are the user's creative writing partner for fiction: novels, short stories, scenes and worldbuilding.\n- Write vivid, original prose with a strong voice, sensory detail, natural dialogue and good pacing. Show, don't tell.\n- Keep continuity: characters, names, timeline, world rules and established facts. Match the user's genre, tense, point of view and style.\n- When asked to write, write the full passage, not a summary or outline (unless asked). Long replies are welcome.\n- Write story text as plain prose: no headings, bullet points or bold in the story itself.\n- Avoid clichés and purple prose. Give characters distinct voices.\n- Suggest directions or ask questions only at natural decision points or when asked.\n- You don't need to be brief in this mode." },
  { id: "electronics", name: "Electronics", icon: "⚡", desc: "Circuits, PCBs, firmware and calculations", temperature: 0.4,
    prompt: "Mode: Electronics. Act as an experienced electronics engineer.\n- Always use the calculate/electronics tools for maths, and pick real standard part values (E12/E24) and common parts.\n- Draw schematics or timing diagrams with render_diagram when a picture helps.\n- Mention ratings that matter (voltage, current, power dissipation, tolerances) and give pin numbers when relevant.\n- Flag safety issues clearly (mains voltage, Li-ion batteries, high current, heat)." },
  { id: "modeler", name: "3D Modeler", icon: "◆", desc: "Blender modeling on your server, checked step by step", temperature: 0.3, thinking: "on",
    prompt: "Mode: 3D Modeler. You build and fix 3D models in Blender on the user's server, one careful step at a time. Accuracy matters more than speed.\n- Start with blender_project (status, then new or open) and blender_inspect before changing anything. Never guess object names, bone names or sizes: read them.\n- Each blender_step makes ONE small, checkable change (add one part, move one thing, fix one problem). Write the goal with numbers: metres, degrees, which object or bone.\n- Write bpy with bpy.data, bmesh and the ready-made helpers; avoid edit-mode bpy.ops. Z is up and the front faces -Y (a character's face is in the FRONT view).\n- After each step, read the measured changes, any new mesh problems and the visual check. If something is off, fix it with another step or go back with revert. Never say a step worked when the check or the numbers say otherwise.\n- Tell the user the version number after each change so they can ask to go back. Before big changes (deleting or joining parts, remeshing, re-rigging) explain the plan and wait for a yes.\n- Photos: when the user sends pictures of what they want, save them with blender_project add_reference (label front/left/back/right when you can tell), so every check compares against them. Ask for 2-3 photos from different sides when the shape matters.\n- New object from photos: blender_generate makes a rough shape (several minutes). Then, one step each: turn it so its front faces -Y, scale it from a real measurement (ask the user for one; photos can't show size), clean up the mesh, add materials, and compare with blender_inspect render=true.\n- Editing something to look like a photo: change it step by step and let the reference comparison tell you what still differs.\n- You're strongest at hard-surface parts, props and accessories, materials (including glowing emission), cleanup, attaching parts to bones and exports. Generated shapes are rough for characters and poor for whole rooms. For organic sculpting (faces, bodies, cloth folds) guide the user step by step in Blender instead and check their result.\n- Files: the user drops .blend, .vrm, .glb, .fbx, .obj or .stl files into the 3D inbox folder on the server (blender_project status shows where); exports go to the workspace's exports folder." },
  { id: "cad", name: "CAD Designer", icon: "📐", desc: "Precise parts in FreeCAD: measured, checked, ready to print", temperature: 0.3, thinking: "on",
    prompt: "Mode: CAD Designer. You design precise parts in FreeCAD on the user's server, one checked step at a time: brackets, enclosures, mounts, adapters, fan ducts, replacement parts and anything to 3D print or machine. Accuracy matters more than speed.\n- Start with cad_project (status, then new or open) and cad_inspect. Ask for the measurements that matter (sizes, hole sizes and spacing, wall thickness, what it attaches to, the printer or material) before designing; never guess a size that has to fit something real.\n- Each cad_step makes ONE change with exact millimetres in the goal. Build with Part shapes, then add() one final part per real object. Z is up; put the part on Z=0 and the front towards -Y.\n- After each step read the measured sizes, volumes and problems (not a solid, separate solids, invalid geometry) and the visual check. Fix problems before going on; never say it worked when the numbers say otherwise. Say the version number so the user can go back.\n- For 3D printing: walls at least 1.2 mm (2 mm or more for strength), holes 0.2-0.4 mm bigger than the bolt or peg, avoid overhangs steeper than 45 degrees and floating parts, add fillets where it takes load, and suggest a print orientation.\n- Photos or sketches the user sends can be saved with cad_project add_reference so every check compares against them. A board exported from the PCB Designer as step appears in the 3D inbox, ready to build an enclosure around. Exports: step for other CAD, stl for printing, blender to hand the part to the 3D Modeler." },
  { id: "pcb", name: "PCB Designer", icon: "🔌", desc: "Circuit boards in KiCad: circuits as code, autorouting, checks, fab files", temperature: 0.3, thinking: "on",
    prompt: "Mode: PCB Designer. You help with circuit boards in KiCad on the user's server: designing circuits as code, checking, explaining, placing parts, board outlines, tracks, and making the files a board maker needs. Accuracy matters more than speed.\n- Start with pcb_project (status, then new or open) and pcb_check. Explain each rules-check problem in plain words: what it means, where it is, and how to fix it.\n- Each pcb_step makes ONE board change (move or rotate a part, set the outline, add a track or text) with millimetres and references (R3, U1) in the goal. Board Y grows downwards. After each step compare the new rules check with the old one, say the version number, and undo anything you made worse.\n- New circuits: write them with pcb_circuit as a SKiDL script (parts with real KiCad footprints, named nets). It makes the schematic and netlist, lays the parts out wide on a grid, autoroutes and checks. Start simple, prefer through-hole parts, keep boards roomy. To change the circuit, send the whole script again (it is saved as circuit.py in each version; parts keep their places).\n- Learn over time: run pcb_library list before designing and reuse recipes that worked. After a clean build the user is happy with, offer save_recipe; when something was wrong, save a note with the fix. Pins listed under verify_with_user aren't confirmed: show them and ask the user to check the datasheet or the part, then confirm_pinout. Don't invent part values or pinouts: ask, or point to the datasheet.\n- Imported boards: their schematics aren't scripted; walk the user through changes in KiCad's schematic editor, then confirm with pcb_check (including the schematic mismatch count).\n- Safety: for mains voltage, batteries or high current, point out clearance and creepage, trace width for the current, fusing and isolation, and recommend a qualified review before powering it.\n- Exports: fab (Gerbers and drill files zipped for JLCPCB, PCBWay and others), bom, pos (pick and place), step (3D, also dropped into the 3D inbox for the CAD Designer), pdf (schematic), project (the KiCad files)." },
  { id: "tutor", name: "Tutor", icon: "🎓", desc: "Learn step by step instead of just getting answers", temperature: 0.5,
    prompt: "Mode: Tutor. Help the user learn and understand, not just get answers.\n- Explain step by step with simple examples and analogies, building on what they already know.\n- For exercises, guide with hints and questions before giving the full solution, unless they ask for it.\n- Check understanding with a short question now and then. Correct misconceptions kindly and clearly." },
  { id: "brainstorm", name: "Brainstorm", icon: "💡", desc: "Lots of ideas, from practical to wild", temperature: 1.0, thinking: "off",
    prompt: "Mode: Brainstorm. Generate many diverse ideas quickly: practical ones and bold, unusual ones.\n- Use short bullet points, one idea each, grouped if that helps. Build on and remix the user's ideas.\n- Don't judge ideas while generating; offer to evaluate or narrow them down afterwards." },
];
const COLORS = { cyan: "#5CE1E6", blue: "#5AA9FF", red: "#FF5C6C", green: "#4DE8A0", purple: "#A77BFF", pink: "#FF7AC6", orange: "#FFA552", yellow: "#FFD84D" };

// ---------------------------------------------------------------- state
const S = {
  convs: new Map(), msgs: new Map(), dirtyC: new Set(), dirtyM: new Map(),
  cursor: 0, serverId: "", current: null, pendingProject: "", pendingMode: "",
  projects: [], customModes: [], memories: [], tools: [], approval: new Set(),
  busy: false, abort: null, attachments: [], context: null, lastSync: 0, syncError: "", sticky: new Map(), auto: new Map(), forced: new Map(),
};
const allModes = () => MODES.concat(S.customModes.map(m => ({ ...m, desc: "Your mode", thinking: m.thinking || "default", tools: m.tools || "all" })));
const modeById = id => allModes().find(m => m.id === id) || MODES[0];
const curConv = () => S.current ? S.convs.get(S.current) : null;
const curMode = () => modeById(curConv() ? curConv().mode : S.pendingMode);
const curProjectId = () => curConv() ? (curConv().project_id || "") : S.pendingProject;
const project = id => S.projects.find(p => p.id === id);
const msgsOf = id => (S.msgs.get(id) || []).filter(m => !m.deleted).sort((a, b) => a.created_at - b.created_at || (a.id < b.id ? -1 : 1));

function saveConv(c) { S.convs.set(c.id, c); S.dirtyC.add(c.id); }
function saveMsg(m) {
  if (!S.msgs.has(m.conv_id)) S.msgs.set(m.conv_id, []);
  const list = S.msgs.get(m.conv_id); const i = list.findIndex(x => x.id === m.id);
  if (i >= 0) list[i] = m; else list.push(m);
  S.dirtyM.set(m.id, m);
}

// ---------------------------------------------------------------- sync (same protocol as the phone)
let syncing = null;
async function sync() {
  if (syncing) return syncing;
  syncing = (async () => {
    try {
      for (let page = 0; page < 200; page++) {
        const convs = [...S.dirtyC].map(id => S.convs.get(id)).filter(Boolean);
        const msgs = [...S.dirtyM.values()].slice(0, 1500);
        const resp = await api("/sync", { method: "POST", json: { since: S.cursor, conversations: convs, messages: msgs } });
        if (S.serverId && resp.server_id !== S.serverId) { S.cursor = 0; }
        S.serverId = resp.server_id;
        convs.forEach(c => { if (S.convs.get(c.id) === c) S.dirtyC.delete(c.id); });
        msgs.forEach(m => { if (S.dirtyM.get(m.id) === m) S.dirtyM.delete(m.id); });
        let changed = false;
        for (const c of resp.conversations) {
          const local = S.convs.get(c.id);
          if (local && local.updated_at >= c.updated_at) continue;
          S.convs.set(c.id, c); changed = true;
          if (c.deleted) S.msgs.delete(c.id);
        }
        for (const m of resp.messages) {
          const list = S.msgs.get(m.conv_id) || []; const local = list.find(x => x.id === m.id);
          if (local && local.updated_at >= m.updated_at) continue;
          if (local) Object.assign(local, m); else list.push(m);
          S.msgs.set(m.conv_id, list); changed = true;
        }
        S.cursor = resp.cursor;
        if (changed && !S.busy) { renderSide(); renderMsgs(); }
        if (!resp.more && S.dirtyM.size === 0 && S.dirtyC.size === 0) break;
      }
      S.lastSync = Date.now(); S.syncError = "";
    } catch (e) {
      S.syncError = e.status === 401 ? "Wrong API key" : "Sync failed: " + e.message;
      if (e.status === 401) askKey();
    } finally { syncing = null; renderSyncState(); }
  })();
  return syncing;
}
function renderSyncState() {
  $("#syncState").textContent = S.syncError || (S.lastSync ? "Synced " + new Date(S.lastSync).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "Syncing…");
  $("#dot").classList.toggle("on", S.busy);
}

async function loadMeta() {
  const [t, p, md, mem] = await Promise.allSettled([api("/tools"), api("/projects"), api("/modes"), api("/memories")]);
  if (t.status === "fulfilled") { S.tools = t.value.tools || []; S.approval = new Set(t.value.approval || []); }
  if (p.status === "fulfilled") S.projects = p.value.projects || [];
  if (md.status === "fulfilled") S.customModes = md.value.modes || [];
  if (mem.status === "fulfilled") S.memories = mem.value.memories || [];
  renderSide(); renderBar();
}

// ---------------------------------------------------------------- markdown
function inline(s) {
  s = esc(s);
  s = s.replace(/!\[([^\]]*)\]\((diagram|image):([0-9a-f]{16})\)/g, (_, t, k, id) => `<img alt="${t}" src="${media(k + ":" + id)}">`);
  s = s.replace(/\[doc:([0-9a-f]{10})\]/g, '<button class="docref" data-doc="$1">' + icon("doc") + 'open</button>');
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  return s;
}
function md(src) {
  const out = []; const lines = String(src || "").replace(/\r/g, "").split("\n"); let i = 0, list = null, para = [];
  const flush = () => { if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; } if (list) { out.push(`</${list}>`); list = null; } };
  while (i < lines.length) {
    const l = lines[i];
    const fence = l.match(/^\s*```\s*([\w+#.-]*)/);
    if (fence) {
      flush(); const code = []; i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i++; out.push(`<pre><button class="cp">Copy</button><code data-lang="${esc(fence[1])}">${esc(code.join("\n"))}</code></pre>`); continue;
    }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { flush(); out.push(`<h${Math.min(3, m[1].length)}>${inline(m[2])}</h${Math.min(3, m[1].length)}>`); }
    else if ((m = l.match(/^\s*([-*•]|\d+[.)])\s+(.*)/))) {
      const kind = /\d/.test(m[1]) ? "ol" : "ul"; if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; }
      if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[2])}</li>`);
    } else if ((m = l.match(/^>\s?(.*)/))) { flush(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
    else if (/^\s*\|.*\|\s*$/.test(l)) {
      flush(); const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { if (!/^\s*\|[\s:|-]+\|\s*$/.test(lines[i])) rows.push(lines[i]); i++; }
      out.push("<table>" + rows.map((r, n) => "<tr>" + r.trim().slice(1, -1).split("|").map(c => n ? `<td>${inline(c.trim())}</td>` : `<th>${inline(c.trim())}</th>`).join("") + "</tr>").join("") + "</table>");
      continue;
    } else if (!l.trim()) flush();
    else { if (list) { out.push(`</${list}>`); list = null; } para.push(l); }
    i++;
  }
  flush(); return out.join("");
}

// ---------------------------------------------------------------- rendering
function renderSide() {
  const pid = curProjectId();
  $("#projects").innerHTML = S.projects.length ? S.projects.map(p => `<div class="item${p.id === pid && !S.current ? " sel" : ""}" data-p="${p.id}">
      <span style="color:${COLORS[p.color] || COLORS.cyan}">${icon("diamond")}</span><span class="t">${esc(p.name)}</span><span class="sub">${[...S.convs.values()].filter(c => !c.deleted && c.project_id === p.id).length || ""}</span></div>`).join("")
    : `<div class="sub" style="padding:2px 10px 6px">Group chats with shared instructions, notes and files.</div>`;
  const convs = [...S.convs.values()].filter(c => !c.deleted).sort((a, b) => b.updated_at - a.updated_at);
  $("#chats").innerHTML = convs.map(c => {
    const p = project(c.project_id); const m = c.mode ? modeIc(modeById(c.mode).icon) : "";
    return `<div class="item${c.id === S.current ? " sel" : ""}" data-c="${c.id}"><span class="t">${m}${esc(c.title || "Untitled")}${p ? `<div class="sub" style="color:${COLORS[p.color] || COLORS.cyan}">${icon("diamond")}${esc(p.name)}</div>` : ""}</span><button class="x" data-del="${c.id}" title="Delete chat">${icon("trash", "solo")}</button></div>`;
  }).join("") || `<div class="sub" style="padding:2px 10px">No chats yet.</div>`;
}
function renderBar() {
  const c = curConv(), m = curMode(), p = project(curProjectId());
  $("#title").textContent = NAME().toUpperCase();
  $("#subtitle").textContent = S.status || (c ? c.title : "New chat");
  $("#modeChip").innerHTML = `${modeIc(m.icon)}${esc(m.name.toUpperCase())} ▾`;
  $("#projectChip").style.display = p ? "" : "none";
  if (p) { $("#projectChip").innerHTML = icon("diamond") + esc(p.name); $("#projectChip").style.color = COLORS[p.color] || COLORS.cyan; }
  const info = S.context;
  $("#meter").style.display = info && c ? "" : "none";
  if (info) {
    const f = Math.min(1, info.used / info.limit);
    $("#meterFill").style.width = (f * 100) + "%";
    $("#meterFill").style.background = f > .85 ? "var(--red)" : f > .6 ? "var(--gold)" : "var(--accent)";
    $("#meterPct").textContent = Math.round(f * 100) + "%";
  }
  $("#send").textContent = S.busy ? "Stop" : "Send";
  renderSyncState();
}
function renderMsgs(scroll = true) {
  const wrap = $("#wrap"); const list = S.current ? msgsOf(S.current) : [];
  if (!list.length && !S.streaming) {
    const m = curMode(), p = project(curProjectId());
    wrap.innerHTML = `<div class="empty">${p ? `<div class="hud" style="color:${COLORS[p.color]}">${icon("diamond")}${esc(p.name)}</div><div class="sub">New chat in this project. ${esc(NAME())} knows its instructions, notes and files.</div><br>` : ""}
      ${m.id !== "general" ? `<div class="hud" style="color:var(--glow)">${modeIc(m.icon)}${esc(m.name)} mode</div><div class="sub">${esc(m.desc)}</div><br>` : ""}
      <div class="star">${icon("sparkle", "solo")}</div><div class="hud" style="font-size:16px;color:var(--text);letter-spacing:5px">${esc(NAME().toUpperCase())} ONLINE</div>
      <p>Same chats as your phone. Try “Remind me to check the 3D print at 9”, or open a project and drop in a datasheet.</p></div>`;
    return;
  }
  const lastReply = [...list].reverse().find(m => m.role === "assistant");
  wrap.innerHTML = list.map(m => msgHtml(m, lastReply && m.id === lastReply.id)).join("") + (S.streaming ? msgHtml(S.streaming, false, true) : "") +
    (S.error ? `<div class="err">${icon("warn")}${esc(S.error)}</div>` : "");
  if (scroll) $("#msgs").scrollTop = $("#msgs").scrollHeight;
}
function msgHtml(m, canRetry, live) {
  const name = NAME().toUpperCase();
  if (m.role === "tool" && m.text.startsWith("⇄")) {   // Auto mode's chip: change the mode for the last reply
    const now = S.auto.get(S.current);
    const opts = AUTO_CHOICES.filter(id => id !== now).map(id => `<option value="${id}">Redo last reply in ${esc(modeById(id).name)}</option>`).join("");
    return `<div class="tool">${markedLine(m.text)}${S.busy ? "" : ` <select data-redo="1" style="margin-left:6px;max-width:240px"><option value="">Change…</option>${now ? `<option value="pin:${now}">Keep this chat in ${esc(modeById(now).name)}</option>` : ""}${opts}</select>`}</div>`;
  }
  if (m.role === "tool") {
    const lines = m.text.split("\n"); const img = lines.find(l => /^!\[[^\]]*\]\(diagram:/.test(l.trim()));
    const txt = markedLine(lines.filter(l => l !== img).join(" ")).replace(/\[doc:([0-9a-f]{10})\]/g, '<button class="docref" data-doc="$1">open</button>');
    return `<div class="tool">${txt}${img ? `<div class="md">${inline(img.trim())}</div>` : ""}</div>`;
  }
  if (m.role === "summary") return `<div class="summary" data-sum="${m.id}"><div class="hud">${icon("sparkle")}Context refreshed</div><div class="sumbody sub">${esc(NAME())} continues from a summary of the messages above. Click to read it.</div></div>`;
  if (m.role === "user") {
    const photos = [...m.text.matchAll(/!\[[^\]]*\]\(image:([0-9a-f]{16})\)/g)].map(x => x[1]);
    const text = m.text.replace(/!\[[^\]]*\]\(image:[0-9a-f]{16}\)/g, "").trim();
    return `<div class="msg user">${live ? "" : `<div class="acts"><button data-copy="${m.id}">Copy</button>${S.busy ? "" : `<button data-edit="${m.id}">Edit</button>`}</div>`}${photos.map(id => `<div class="md"><img src="${media("image:" + id)}"></div>`).join("")}${esc(text)}</div>`;
  }
  const wide = /```|\n\|/.test(m.text);
  return `<div class="msg assistant${wide ? " wide" : ""}">${live ? "" : `<div class="acts"><button data-copy="${m.id}">Copy</button>${canRetry && !S.busy ? `<button data-retry="1">Retry</button>` : ""}</div>`}
    <div class="who">${icon("sparkle")}${esc(name)}</div>${m.reasoning ? `<div class="think" data-think>${icon("thought")}Thought process (click to show)</div><div class="thinkbody" style="display:none" class="think">${esc(m.reasoning)}</div>` : ""}
    <div class="md">${m.text ? md(m.text) : '<span class="sub">…</span>'}</div></div>`;
}

// ---------------------------------------------------------------- prompt building (mirrors the phone app)
function nowLine() {
  const now = new Date(); const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const off = -now.getTimezoneOffset(); const sign = off >= 0 ? "+" : "-";
  const offset = sign + String(Math.floor(Math.abs(off) / 60)).padStart(2, "0") + ":" + String(Math.abs(off) % 60).padStart(2, "0");
  const date = now.toLocaleString("en-CA", { weekday: "long", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  return `Current time: ${date} UTC${offset} (${tz})`;
}
function oldMemorySection() {
  if (!S.memories.length) return "";
  let chars = 0; const lines = [];
  for (const m of S.memories) { const l = `- [${m.id}] ${m.text}`; if (chars + l.length > 3500) break; lines.push(l); chars += l.length; }
  return "What you remember about the user (id in brackets, for forget). Use naturally; don't recite.\n" + lines.join("\n");
}
// Time and per-message context go in front of the newest message only, so the start of the prompt stays the
// same between messages and the model server can reuse its cache (much faster on a CPU).
function withTurnContext(history, extra) {
  const i = history.map(h => h.role).lastIndexOf("user"); if (i < 0) return history;
  const block = `[Automatic context, not written by the user]\n${nowLine()}${extra ? "\n" + extra.trim() : ""}\n[End of context]\n\n`;
  const out = history.slice(); out[i] = { ...out[i], content: block + out[i].content }; return out;
}
function systemPrompt(projectText, summary, mode, memory) {
  const name = NAME();
  let p = `You are ${name}, the user's private AI assistant (StellAI), running on their own computer. Warm and capable with a light space-explorer touch; concise and practical.
The user is in the StellAI desktop web chat. Phone actions (alarms, apps, texts) aren't available here, but reminders still ring on their phones.
Markdown is fine; always put code in fenced blocks with a language; avoid tables.
For long code (over ~60 lines), chapters or write-ups, use the document tool: they show beside the chat; refer to them in a line instead of pasting.
Use the calculate/electronics tools for any maths. Use render_diagram when a picture helps (it shows automatically).
Save lasting facts with remember. Reminders: set_reminder. Lists: add_to_list. Current info: web_search then fetch_page, citing links.
A user message may start with an automatic context block (time, memories, file excerpts); the user didn't write it.
Working style: be direct and get to the point. If a request is ambiguous in a way that changes the answer, ask one short clarifying question first; otherwise make a sensible assumption and say it. Say plainly when you're not sure or don't know, rather than guessing. Check your work: re-read numbers, units and code before answering, and use run_python or the calculators when it's available instead of doing long arithmetic in your head. If you get something wrong, say so and fix it.
Pin decisions, names and facts that must stay true in this chat with pin_note when it's available.`;
  if (mode.prompt) p += `\n\n## Current mode: ${mode.name}\n${mode.prompt}\nThese mode instructions take priority over the general style guidance above (such as keeping replies short).`;
  if (memory) p += "\n\n" + memory;
  if (projectText) p += "\n\n" + projectText;
  if (summary) p += "\n\n## Earlier in this chat\nThe start of this conversation was summarized to free up memory. Continue naturally from it; the user can still see the full chat. When you need its exact words (code, numbers, names), call recall_chat.\n" + summary;
  return p;
}
const PHOTO = /!\[[^\]]*\]\(image:([0-9a-f]{16})\)/g, DIAGRAM = /!\[[^\]]*\]\(diagram:[0-9a-f]+\)/g;
function buildHistory(list) {
  const out = []; let pending = "";
  const flush = () => { if (pending.trim()) out.push({ role: "assistant", content: pending.trim() }); pending = ""; };
  for (const m of list) {
    if (m.role === "user") { flush(); out.push({ role: "user", content: m.text.replace(PHOTO, "[photo $1]").trim(), _id: m.id, _text: m.text }); }
    else if (m.role === "assistant" && m.text.trim()) pending += m.text + "\n";
    else if (m.role === "tool" && !m.text.startsWith("⇄")) pending += "(action result: " + m.text.replace(DIAGRAM, "[diagram shown]").trim() + ")\n";
  }
  flush(); return out;
}
async function prepare(convId, images, voice = false, override = null, auto = false) {
  const c = S.convs.get(convId); const mode = override || modeById(c.mode); const list = msgsOf(convId);
  let last = -1; list.forEach((m, i) => { if (m.role === "summary") last = i; });
  const summary = last >= 0 ? list[last].text : "";
  let history = buildHistory(list.slice(last + 1));
  const base = systemPrompt("", summary, mode, "");
  const sticky = [...(S.sticky.get(convId) || [])];
  const query = list.filter(m => m.role === "user").slice(-2).map(m => m.text.replace(PHOTO, "")).join("\n");
  let plan = null;
  try {
    plan = await api("/context", { method: "POST", json: { project_id: c.project_id || "", query, fixed: base, images, mode: mode.id,
      mode_tools: mode.tools || "all", split: true, sticky, thinking: voice ? "auto" : thinkPref(mode),
      last: (list.filter(m => m.role === "user").slice(-1)[0] || { text: "" }).text.replace(PHOTO, ""), chat_id: convId, auto, messages: history.map(h => ({ role: h.role, content: h.content })) } });
  } catch { plan = null; }
  let start = 0, info = null, needsRefresh = false, names = null, projectText = "", memory = oldMemorySection(), extra = "";
  let think = thinkPref(mode) === "on";
  if (plan) {
    const limit = plan.n_ctx - plan.reply_reserve, used = plan.fixed_tokens + plan.project_tokens + plan.history_tokens;
    info = { used, limit }; names = plan.tools; projectText = plan.project_context || "";
    if ("memory_context" in plan) memory = plan.memory_context || "";
    extra = plan.turn_context || "";
    if ("think" in plan) think = !!plan.think;
    // Auto mode: earlier topics in other modes are left out (the server sends a short note about them)
    if (plan.drop && plan.drop.length) { const drop = new Set(plan.drop); history = history.filter((_, i) => !drop.has(i)); }
    start = Math.min(plan.keep_from || 0, history.length);
    while (start < history.length && history[start].role !== "user") start++;
    if (start >= history.length) start = Math.max(0, history.map(h => h.role).lastIndexOf("user"));
    needsRefresh = history.filter(h => h.role === "user").length >= 2 && (start > 0 || used > limit * 0.85);
  } else start = Math.max(0, history.length - 24);
  const tools = S.tools.filter(t => !names || names.includes(t.function.name));
  if (voice) { extra = (extra ? extra + "\n" : "") + VOICE_STYLE; if (!plan) think = false; }
  const hist = withTurnContext(history.slice(start), extra);
  return { system: systemPrompt(projectText, summary, mode, memory), history: hist, info, needsRefresh, tools, mode, think };
}
// "on"/"off"/"auto": the chat's mode first, then this browser's setting (auto = think on hard questions only)
function thinkPref(mode) {
  if (mode.thinking === "on" || mode.thinking === "off") return mode.thinking;
  const v = store.get("thinkingMode", null);
  return v || (store.get("thinking", false) ? "on" : "auto");
}
async function compact(convId, keepUserTurns) {
  const c = S.convs.get(convId); const list = msgsOf(convId);
  let last = -1; list.forEach((m, i) => { if (m.role === "summary") last = i; });
  const after = list.slice(last + 1); const users = after.map((m, i) => m.role === "user" ? i : -1).filter(i => i >= 0);
  let k = Math.min(keepUserTurns, users.length); while (k > 1 && users[users.length - k] === 0) k--;
  const cut = k === 0 ? after.length : users[users.length - k]; const part = after.slice(0, cut);
  if (!part.some(m => m.role === "user" || m.role === "assistant")) return false;
  const res = await api("/summarize", { method: "POST", json: { previous: last >= 0 ? list[last].text : "", style: modeById(c.mode).style || "work",
    messages: part.map(m => ({ role: m.role, content: m.text.replace(PHOTO, "[photo]").replace(DIAGRAM, "[diagram]") })) } });
  if (!res.summary) return false;
  let created;
  if (cut < after.length) { const next = after[cut].created_at, prev = part[part.length - 1].created_at; created = next - prev >= 2 ? Math.floor((prev + next) / 2) : next - 1; }
  else created = nextTs();
  saveMsg({ id: uuid(), conv_id: convId, role: "summary", text: res.summary.trim(), reasoning: "", created_at: created, updated_at: nextTs(), deleted: false });
  return true;
}
async function updateMeter() {
  if (!S.current || S.busy) return;
  try { const p = await prepare(S.current, 0); if (p.info) { S.context = p.info; renderBar(); } } catch {}
}

// ---------------------------------------------------------------- chatting
function addMsg(convId, role, text, reasoning = "") {
  const ts = nextTs(); const m = { id: uuid(), conv_id: convId, role, text, reasoning, created_at: ts, updated_at: ts, deleted: false };
  saveMsg(m); return m;
}
function touch(convId) { const c = S.convs.get(convId); if (c) saveConv({ ...c, updated_at: nextTs() }); }

async function send() {
  if (S.busy) { stop(); return; }
  const text = $("#input").value.trim(); const photos = S.attachments.slice();
  if (!text && !photos.length) return;
  if (S.editing) { truncateFrom(S.editing); S.editing = null; }
  $("#input").value = ""; autosize(); S.attachments = []; renderAtts();
  let convId = S.current;
  if (!convId) {
    convId = uuid(); const now = nextTs(); const t = (text || "Photo").replace(/\s+/g, " ");
    saveConv({ id: convId, title: t.length <= 48 ? t : t.slice(0, 47) + "…", created_at: now, updated_at: now, deleted: false,
      project_id: S.pendingProject, mode: S.pendingMode });
    S.current = convId; S.pendingProject = ""; S.pendingMode = ""; store.set("current", convId);
  }
  for (const p of photos) { try { await api(`/images/${p.id}.jpg`, { method: "PUT", body: p.blob, headers: { "Content-Type": "image/jpeg" } }); } catch (e) { notice("Couldn't upload a photo: " + e.message); } }
  addMsg(convId, "user", (text + photos.map(p => `\n![photo](image:${p.id})`).join("")).trim());
  renderSide(); renderMsgs();
  const voice = !!S.voiceNext; S.voiceNext = false;
  await run(convId, photos, voice, true);
}
function stop() { if (S.abort) S.abort.abort(); S.stopRequested = true; SPEAK.cancel(); }
async function retry() {
  if (S.busy || !S.current) return; const list = msgsOf(S.current);
  const lastUser = list.map(m => m.role).lastIndexOf("user"); if (lastUser < 0) return;
  if (lastUser + 1 < list.length) truncateFrom(list[lastUser + 1].id);
  await run(S.current, []);
}
function truncateFrom(id) {
  const list = msgsOf(S.current); const i = list.findIndex(m => m.id === id); if (i < 0) return;
  const now = nextTs(); list.slice(i).forEach(m => saveMsg({ ...m, deleted: true, text: "", reasoning: "", updated_at: now }));
}

// Simple requests (the time, sums, lists, reminders) are answered by the server at once, without the model.
// Only for a message just sent (not Retry: a reminder mustn't be set twice).
async function quickAnswer(convId) {
  const last = msgsOf(convId).filter(m => m.role === "user").pop();
  const text = last ? last.text.trim() : "";
  if (!text || text.length > 140 || text.includes("\n")) return null;
  const now = new Date(), off = -now.getTimezoneOffset(), pad = n => String(Math.abs(n)).padStart(2, "0");
  const iso = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}` +
    `${off >= 0 ? "+" : "-"}${pad(Math.floor(Math.abs(off) / 60))}:${pad(Math.abs(off) % 60)}`;
  try { const r = await api("/quick", { method: "POST", json: { text, now: iso } }); return r && r.reply ? r.reply : null; } catch { return null; }
}

const AUTO_CHOICES = ["general", "programmer", "electronics", "cad", "pcb", "modeler", "writer", "tutor", "brainstorm"];
async function autoMode(convId, fresh) {
  const forced = S.forced.get(convId) || ""; S.forced.delete(convId);
  if (!fresh && !forced) return modeById(S.auto.get(convId) || "general");
  const last = msgsOf(convId).filter(m => m.role === "user").pop();
  const text = last ? last.text.replace(PHOTO, "").trim() : "";
  let r;
  try { r = await api("/route", { method: "POST", json: { chat_id: convId, text, level: store.get("autoModes", "auto"), forced, current: S.auto.get(convId) || null } }); }
  catch { return modeById(S.auto.get(convId) || "general"); }
  S.auto.set(convId, r.mode);
  if (r.switched || forced) addMsg(convId, "tool", "⇄ Auto: " + modeById(r.mode).name + (r.reason ? " (" + r.reason + ")" : ""));
  else if (r.suggest) addMsg(convId, "tool", "⇄ Suggest: " + r.suggest_name + " (pick it below to redo)");
  return modeById(r.mode);
}
async function run(convId, photos, voice = false, fresh = false) {
  S.continued = 0;
  S.busy = true; S.error = null; S.stopRequested = false; S.status = "Contacting server…"; renderBar(); renderMsgs();
  SPEAK.cancel(); SPEAK.newReply(); postAvatar({ state: "thinking", voice });
  const turn = []; const projectId = (S.convs.get(convId).project_id || "");
  try {
    // (not in Writer, Tutor, Brainstorm or custom modes, where "what time is it?" may be part of the story)
    const quick = fresh && !photos.length && ["general", "programmer", "electronics"].includes(curMode().id) ? await quickAnswer(convId) : null;
    if (quick) {
      if (AVATAR_MODE) { SPEAK.begin(); SPEAK.feed(quick, true); avatarCaption(quick); if (AV.avatar) AV.avatar.react(quick); }
      postAvatar({ state: "reply", text: quick });
      addMsg(convId, "assistant", quick);
      return;
    }
    // Auto mode: a General chat picks the mode that fits this message (instant keyword check on the server)
    const auto = (S.convs.get(convId).mode || "general") === "general" && store.get("autoModes", "auto") !== "off";
    let amode = auto ? await autoMode(convId, fresh) : null;
    let p = await prepare(convId, photos.length, voice, amode, auto);
    if (p.needsRefresh && !S.stopRequested) {
      S.status = "Refreshing context…"; renderBar();
      try { if (await compact(convId, 2)) { p = await prepare(convId, photos.length, voice, amode, auto); renderMsgs(); } } catch (e) { notice("Couldn't refresh the context: " + e.message); }
    }
    if (p.info) S.context = p.info;
    const st = S.sticky.get(convId) || new Set(); p.tools.forEach(t => st.add(t.function.name)); S.sticky.set(convId, st);
    // newest message with photos goes to the model as images
    let hist = p.history.map(h => ({ role: h.role, content: h.content }));
    const lastUser = p.history.map(h => h.role).lastIndexOf("user");
    if (lastUser >= 0 && photos.length) {
      const block = (hist[lastUser].content.match(/^\[Automatic context[\s\S]*?\[End of context\]\n\n/) || [""])[0];
      const parts = [{ type: "text", text: block + ((p.history[lastUser]._text.replace(PHOTO, "").trim() || "(The user sent a photo.)") + " " + (p.history[lastUser]._text.match(PHOTO) || []).map(t => "[photo " + t.match(/image:([0-9a-f]{16})/)[1] + "]").join(" ")).trim() }];
      for (const ph of photos) parts.push({ type: "image_url", image_url: { url: ph.dataUrl } });
      hist[lastUser] = { role: "user", content: parts };
    }
    const thinking = p.think;
    // Qwen's recommended sampling for Qwen3.5 (model card); a mode's creativity overrides temperature
    const code = ["programmer", "electronics", "modeler", "cad", "pcb"].includes(p.mode.id);
    for (let round = 0; round < 6; round++) {
      S.status = "Contacting server…"; renderBar();
      const body = { model: "local", stream: true, messages: [{ role: "system", content: p.system }, ...hist, ...turn],
        tools: p.tools, chat_template_kwargs: { enable_thinking: thinking },
        temperature: p.mode.temperature != null ? p.mode.temperature : (thinking ? (code ? 0.6 : 1.0) : 0.7),
        top_p: thinking ? 0.95 : 0.8, top_k: 20, min_p: 0, presence_penalty: code ? 0 : 1.5 };
      if (voice && !thinking) body.max_tokens = 700;
      const result = await stream(body);
      const finalText = result.content.replace(/<think>[\s\S]*?(<\/think>|$)/g, "").trim();
      S.streaming = null;
      if (AVATAR_MODE) { SPEAK.feed(finalText, true); avatarCaption(finalText); if (finalText && AV.avatar) AV.avatar.react(finalText); }
      if (finalText && !result.toolCalls.length) postAvatar({ state: "reply", text: finalText });
      if (finalText || !result.toolCalls.length) addMsg(convId, "assistant", finalText || "(no reply)", result.reasoning);
      // checking something takes a moment: say so instead of going quiet
      if (voice && AVATAR_MODE && SPEAK.enabled && result.toolCalls.length && !SPEAK.saidAny) SPEAK.say(ACKS[Math.floor(Math.random() * ACKS.length)]);
      const assistant = { role: "assistant", content: finalText };
      if (result.toolCalls.length) assistant.tool_calls = result.toolCalls.map(tc => ({ id: tc.id, type: "function", function: { name: tc.name, arguments: tc.arguments } }));
      turn.push(assistant); renderMsgs();
      if (!result.toolCalls.length) {
        // the reply ran out of room (her memory filled up mid-answer): make room, then carry on where it stopped
        if (result.finish === "length" && finalText && (S.continued = (S.continued || 0)) < 2 && !S.stopRequested) {
          S.continued++; addMsg(convId, "tool", "↪ Long reply: making room and continuing");
          S.status = "Continuing…"; renderBar();
          try { await compact(convId, 1); } catch {}
          p = await prepare(convId, 0, voice, amode, auto);
          hist = p.history.map(h => ({ role: h.role, content: h.content }));
          turn.length = 0; turn.push({ role: "user", content: "(Automatic: your previous reply was cut off because your memory filled up. Continue it exactly where it stopped, without repeating anything or adding a preamble.)" });
          continue;
        }
        break;
      }
      for (const tc of result.toolCalls) {
        let args = {}; try { args = JSON.parse(tc.arguments || "{}"); } catch {}
        delete args.approved;
        if (["project_note", "project_search"].includes(tc.name) && projectId) args.project_id = projectId;
        let outcome;
        if (!S.tools.some(t => t.function.name === tc.name)) {
          outcome = { line: "✗ " + tc.name + " isn't available on the desktop", result: JSON.stringify({ ok: false, error: "That's a phone action; it isn't available in the desktop chat." }) };
        } else if (S.approval.has(tc.name) && !(await approve(tc.name, args))) {
          outcome = { line: "✗ You declined: " + tc.name.replace("github_", "GitHub "), result: JSON.stringify({ ok: false, error: "The user declined this action." }) };
        } else {
          if (S.approval.has(tc.name)) args.approved = true;
          if (tc.name === "search_chats") args.current_conversation = convId;
          if (!("conversation_id" in args)) args.conversation_id = convId;   // chat tools: notes, recall, Auto mode
          // keep big results (pages, files) within the space left in Stella's context
          const left = p.info ? p.info.limit - p.info.used - Math.floor(JSON.stringify(turn).length / 3) : null;
          args._max_chars = left == null ? 12000 : Math.max(1500, Math.floor(left * 3 * 0.6));
          S.status = toolLabel(tc.name) + "…"; renderBar();
          try {
            const r = await api("/tools/" + encodeURIComponent(tc.name), { method: "POST", json: args });
            const res = r.result || {}; let line = (res.ok ? "✓ " : "✗ ") + (r.summary || tc.name);
            if (res.ok && res.image) line += `\n![${(res.title || "Diagram").replace(/]/g, ")")}](diagram:${res.image})`;
            outcome = { line, result: JSON.stringify(res) };
          } catch (e) { outcome = { line: "✗ " + e.message, result: JSON.stringify({ ok: false, error: e.message }) }; }
        }
        let switched = null;
        try { if (tc.name === "switch_mode" && auto) switched = JSON.parse(outcome.result).switched_to || null; } catch {}
        if (switched) {   // Stella switched herself: her next step uses that mode's instructions and tools
          S.auto.set(convId, switched); amode = modeById(switched);
          addMsg(convId, "tool", "⇄ Auto: " + amode.name + " (Stella switched)");
          p = await prepare(convId, 0, voice, amode, true);
          hist = p.history.map(h => ({ role: h.role, content: h.content }));
        } else addMsg(convId, "tool", outcome.line);
        if (tc.name === "document" && convId === S.current) { const m = outcome.line.match(/\[doc:([0-9a-f]{10})\]/); refreshDocs(m ? m[1] : null, !!m); }
        turn.push({ role: "tool", tool_call_id: tc.id, name: tc.name, content: outcome.result });
        renderMsgs();
      }
    }
  } catch (e) {
    S.streaming = null;
    if (!S.stopRequested) S.error = e.status === 401 ? "The API key was rejected." : (e.message || String(e));
  } finally {
    if (S.stopRequested) S.error = "Stopped.";
    S.busy = false; S.status = ""; S.abort = null; S.streaming = null;
    postAvatar({ state: "idle" });
    if (voice && !S.stopRequested && !S.error) startFollowUp();
    touch(convId); renderSide(); renderMsgs(); renderBar();
    sync().then(updateMeter);
    if (turn.some(t => t.role === "tool" && t.name === "project_note")) loadMeta();
  }
}
const toolLabel = n => ({ cad_step: "Working in FreeCAD", cad_inspect: "Measuring the design", cad_project: "Opening the CAD workspace", pcb_step: "Editing the board", pin_note: "Pinning a note", document: "Writing in the document", run_python: "Running Python", recall_chat: "Looking back in this chat", move_stella: "Moving to another server", pcb_circuit: "Building the circuit and board", pcb_library: "Checking the PCB library", pcb_check: "Checking the board", pcb_project: "Opening the PCB workspace", blender_generate: "Making a 3D shape from the pictures", blender_step: "Working in Blender", blender_inspect: "Looking at the model", blender_project: "Opening the 3D workspace", render_diagram: "Drawing diagram", web_search: "Searching the web", fetch_page: "Reading the page", set_reminder: "Setting reminder",
  project_search: "Searching project files", library_search: "Searching the library", remember: "Remembering" }[n] || (n.startsWith("github") ? "Working with GitHub" : "Calculating"));

async function stream(body) {
  S.abort = new AbortController();
  const r = await fetch("/llm/v1/chat/completions", { method: "POST", signal: S.abort.signal,
    headers: { Authorization: "Bearer " + KEY, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) { let msg = "HTTP " + r.status; try { const j = await r.json(); msg = (j.error && (j.error.message || j.error)) || msg; } catch {} const e = new Error(msg); e.status = r.status; throw e; }
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
  const res = { content: "", reasoning: "", toolCalls: [] }; const calls = [];
  S.streaming = { id: "live", role: "assistant", text: "", reasoning: "" };
  let last = 0; SPEAK.begin();
  for (;;) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 1);
      if (!line.startsWith("data:")) continue; const data = line.slice(5).trim(); if (data === "[DONE]") continue;
      let j; try { j = JSON.parse(data); } catch { continue; }
      if (j.error) throw new Error(j.error.message || "server error");
      if (j.choices && j.choices[0] && j.choices[0].finish_reason) res.finish = j.choices[0].finish_reason;
      const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
      if (d.content) res.content += d.content;
      if (d.reasoning_content) res.reasoning += d.reasoning_content;
      for (const tc of d.tool_calls || []) {
        const c = calls[tc.index || 0] || (calls[tc.index || 0] = { id: "", name: "", arguments: "" });
        if (tc.id) c.id = tc.id; if (tc.function && tc.function.name) c.name += tc.function.name; if (tc.function && tc.function.arguments) c.arguments += tc.function.arguments;
      }
    }
    S.streaming.text = res.content.replace(/<think>[\s\S]*?(<\/think>|$)/g, "").trimStart(); S.streaming.reasoning = res.reasoning;
    S.status = !S.streaming.text && res.reasoning ? "Thinking…" : "Writing…";
    if (AVATAR_MODE) SPEAK.feed(S.streaming.text);
    if (Date.now() - last > 80) { last = Date.now(); renderMsgs(); renderBar(); if (AVATAR_MODE && S.streaming.text) avatarCaption(S.streaming.text); }
  }
  res.toolCalls = calls.filter(Boolean).map((c, i) => ({ ...c, id: c.id || "call_" + i }));
  return res;
}

// ---------------------------------------------------------------- approvals (GitHub commits and pull requests)
function approve(name, args) {
  return new Promise(resolve => {
    let body;
    if (name === "github_commit") {
      const files = (args.files || []).map((f, i) => `<details><summary>${esc(f.path)} <span class="sub">(${(f.content || "").split("\n").length} lines)</span></summary><pre style="max-height:300px;overflow:auto"><code>${esc(f.content || "")}</code></pre></details>`).join("");
      body = `<p>${esc(NAME())} wants to <b>commit</b> to <code>${esc(args.repo || "your default repo")}</code>, branch <code>${esc(args.branch)}</code>:</p>
        <p><i>${esc(args.message || "")}</i></p>${files}${(args.delete || []).length ? `<p>Delete: ${(args.delete || []).map(esc).join(", ")}</p>` : ""}
        <p class="sub">Nothing goes to your main branch; you can review it on GitHub and merge with a pull request.</p>`;
    } else {
      body = `<p>${esc(NAME())} wants to <b>open a pull request</b> in <code>${esc(args.repo || "your default repo")}</code> from <code>${esc(args.branch)}</code>:</p>
        <p><b>${esc(args.title || "")}</b></p><div class="md">${md(args.body || "")}</div>`;
    }
    const d = dialog(`<h2>Approve this GitHub change?</h2>${body}<div class="btns"><button id="no">Decline</button><button class="primary" id="yes">Approve</button></div>`);
    d.querySelector("#yes").onclick = () => { d.remove(); resolve(true); };
    d.querySelector("#no").onclick = () => { d.remove(); resolve(false); };
  });
}

// ---------------------------------------------------------------- dialogs
function dialog(html, onClose) {
  const o = document.createElement("div"); o.className = "overlay";
  o.innerHTML = `<div class="dialog">${html}</div>`;
  o.addEventListener("mousedown", e => { if (e.target === o) { o.remove(); onClose && onClose(); } });
  document.body.appendChild(o); return o;
}
function notice(text) { const n = $("#notice"); n.innerHTML = icon("sparkle") + esc(text + "  (click to dismiss)"); n.style.display = "block"; }
function askKey() {
  if (document.querySelector("#keyDlg")) return;
  const d = dialog(`<h2 id="keyDlg">Connect to Stella</h2><p class="sub">Enter the API key shown by StellAI Server (Windows: the server window; Linux: the StellAI Server terminal).</p>
    <input id="k" type="password" placeholder="API key" autocomplete="off"><div class="btns"><button class="primary" id="ok">Connect</button></div>`);
  const go = () => { KEY = d.querySelector("#k").value.trim(); store.set("key", KEY); d.remove(); start(); };
  d.querySelector("#ok").onclick = go; d.querySelector("#k").onkeydown = e => { if (e.key === "Enter") go(); };
  setTimeout(() => d.querySelector("#k").focus(), 50);
}
function pickMode() {
  const cur = curMode();
  const d = dialog(`<h2>${S.current ? "Switch mode" : "Mode for this chat"}</h2><p class="sub">Changes how ${esc(NAME())} thinks and writes in this chat, from her next reply on.</p>` +
    allModes().map(m => `<div class="pick${m.id === cur.id ? " sel" : ""}" data-m="${esc(m.id)}"><span class="ic">${modeIc(m.icon)}</span><div style="flex:1"><div>${esc(m.name)}</div><div class="sub">${esc(m.desc)}</div></div>${m.id === cur.id ? icon("check", "solo") : ""}</div>`).join("") +
    `<div class="btns"><span class="sub" style="flex:1">Create your own modes in the phone app (Manage modes).</span><button id="close">Close</button></div>`);
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelectorAll("[data-m]").forEach(el => el.onclick = () => {
    const id = el.dataset.m === "general" ? "" : el.dataset.m;
    if (S.current) { saveConv({ ...curConv(), mode: id, updated_at: nextTs() }); sync().then(updateMeter); } else S.pendingMode = id;
    d.remove(); renderBar(); renderSide(); renderMsgs();
  });
}
function pickProject() {
  const pid = curProjectId();
  const d = dialog(`<h2>${S.current ? "Move chat to…" : "Start this chat in…"}</h2>` +
    [{ id: "", name: "No project", color: "" }].concat(S.projects).map(p => `<div class="pick${p.id === pid ? " sel" : ""}" data-p="${p.id}"><span class="ic" style="color:${COLORS[p.color] || "var(--muted)"}">${icon("diamond")}</span><div style="flex:1">${esc(p.name)}</div>${p.id === pid ? icon("check", "solo") : ""}</div>`).join("") +
    `<div class="btns"><button id="close">Close</button></div>`);
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelectorAll("[data-p]").forEach(el => el.onclick = () => {
    if (S.current) { saveConv({ ...curConv(), project_id: el.dataset.p, updated_at: nextTs() }); sync().then(updateMeter); } else S.pendingProject = el.dataset.p;
    d.remove(); renderBar(); renderSide(); renderMsgs();
  });
}
async function openProject(id) {
  const isNew = id === "new", isLib = id === "library";
  let p = isNew ? { id: "", name: "", color: "cyan", instructions: "", notes: [], files: [] } : isLib ? await api("/library") : await api("/projects").then(r => r.projects.find(x => x.id === id));
  if (!p) return;
  const fileRows = files => files.map(f => `<div class="row"><span>${fileIc(f.kind)}</span><div class="t">${esc(f.name)}<div class="sub">${f.size > 1e6 ? (f.size / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(f.size / 1000)) + " KB"} · ~${f.tokens} tokens${f.note ? " · " + esc(f.note) : ""}</div></div><button class="ghost" data-view="${f.id}">View</button><button class="ghost" data-delf="${f.id}">${icon("trash", "solo")}</button></div>`).join("") || `<div class="sub">No files yet.</div>`;
  const d = dialog(`<h2>${isLib ? icon("library") + "Library" : isNew ? "New project" : icon("diamond") + esc(p.name)}</h2>
    ${isLib ? `<p class="sub">Documents ${esc(NAME())} can search from any chat: datasheets, manuals, reference notes.</p>` : `
    <label class="sub">Name</label><input id="pn" value="${esc(p.name)}" maxlength="60">
    <div style="display:flex;gap:8px;margin:10px 0">${Object.entries(COLORS).map(([k, v]) => `<span data-col="${k}" style="width:24px;height:24px;border-radius:50%;background:${v};cursor:pointer;outline:${k === p.color ? "3px solid #fff" : "none"}"></span>`).join("")}</div>
    <label class="sub">Instructions for ${esc(NAME())}</label><textarea id="pi" rows="4">${esc(p.instructions)}</textarea>
    <div class="btns" style="justify-content:flex-start"><button class="primary" id="psave">${isNew ? "Create project" : "Save changes"}</button>${isNew ? "" : `<button id="pchat">+ New chat in this project</button>`}</div>`}
    ${isNew ? "" : `${isLib ? "" : `<h3 class="hud" style="margin-top:16px">Notes</h3><div class="list" id="notes">${p.notes.map(n => `<div class="row"><div class="t">${esc(n.text)}</div><button class="ghost" data-deln="${n.id}">${icon("trash", "solo")}</button></div>`).join("") || `<div class="sub">No notes yet.</div>`}</div>
      <div style="display:flex;gap:8px;margin-top:6px"><input id="nn" placeholder="e.g. Input is 12–24 V, output 5 V 3 A"><button id="nadd">Add</button></div>`}
    <h3 class="hud" style="margin-top:16px">Files</h3><div class="list" id="files">${fileRows(p.files)}</div>
    <div style="margin-top:8px"><button id="fadd">+ Add files</button> <span class="sub" id="fstat">PDF, Word, code, text or images, up to 25 MB each. You can also drop files here.</span><input type="file" id="fin" multiple hidden></div>
    ${isLib ? "" : `<div class="btns"><button class="ghost" id="pdel" style="color:var(--red)">Delete project</button></div>`}`}
    <div class="btns"><button id="close">Close</button></div>`, loadMeta);
  const q = s => d.querySelector(s);
  q("#close").onclick = () => { d.remove(); loadMeta(); };
  let color = p.color;
  d.querySelectorAll("[data-col]").forEach(el => el.onclick = () => { color = el.dataset.col; d.querySelectorAll("[data-col]").forEach(x => x.style.outline = x === el ? "3px solid #fff" : "none"); });
  if (q("#psave")) q("#psave").onclick = async () => {
    try { const saved = await api("/projects", { method: "POST", json: { id: p.id || undefined, name: q("#pn").value, color, instructions: q("#pi").value } });
      d.remove(); await loadMeta(); if (isNew) openProject(saved.id); } catch (e) { alertBox(e.message); } };
  if (q("#pchat")) q("#pchat").onclick = () => { d.remove(); newChat(p.id); };
  if (q("#nadd")) q("#nadd").onclick = async () => { const t = q("#nn").value.trim(); if (!t) return; await api(`/projects/${p.id}/notes`, { method: "POST", json: { text: t } }); d.remove(); openProject(id); };
  d.querySelectorAll("[data-deln]").forEach(el => el.onclick = async () => { await api(`/projects/${p.id}/notes/${el.dataset.deln}`, { method: "DELETE" }); d.remove(); openProject(id); });
  d.querySelectorAll("[data-delf]").forEach(el => el.onclick = async () => { await api(`/projects/${p.id}/files/${el.dataset.delf}`, { method: "DELETE" }); d.remove(); openProject(id); });
  d.querySelectorAll("[data-view]").forEach(el => el.onclick = async () => {
    const r = await fetch(`/projects/${p.id}/files/${el.dataset.view}.txt?key=${encodeURIComponent(KEY)}`); const t = await r.text();
    dialog(`<h2>What ${esc(NAME())} reads from this file</h2><pre style="white-space:pre-wrap;font-size:12px">${esc(t.slice(0, 30000))}${t.length > 30000 ? "\n[…]" : ""}</pre>`); });
  const upload = async files => {
    for (const f of files) {
      if (f.size > 25e6) { q("#fstat").textContent = f.name + " is over 25 MB"; continue; }
      q("#fstat").textContent = "Reading " + f.name + "…";
      try { await api(`/projects/${p.id}/files?name=${encodeURIComponent(f.name)}`, { method: "PUT", body: f, headers: { "Content-Type": "application/octet-stream" } }); }
      catch (e) { alertBox("Couldn't add " + f.name + ": " + e.message); }
    }
    d.remove(); openProject(id);
  };
  if (q("#fadd")) { q("#fadd").onclick = () => q("#fin").click(); q("#fin").onchange = () => upload([...q("#fin").files]);
    q(".dialog").addEventListener("dragover", e => e.preventDefault()); q(".dialog").addEventListener("drop", e => { e.preventDefault(); upload([...e.dataTransfer.files]); }); }
  if (q("#pdel")) q("#pdel").onclick = async () => { if (!confirm(`Delete “${p.name}”? Its notes and files are deleted; its chats are kept.`)) return;
    await api(`/projects/${p.id}`, { method: "DELETE" }); d.remove(); await loadMeta(); sync(); };
}
function alertBox(text) { const d = dialog(`<p>${esc(text)}</p><div class="btns"><button id="ok">OK</button></div>`); d.querySelector("#ok").onclick = () => d.remove(); }
async function openTasks() {
  const [r, l] = await Promise.all([api("/reminders"), api("/lists")]);
  const fmt = ms => new Date(ms).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const up = r.reminders.filter(x => !x.done);
  const d = dialog(`<h2>${icon("alarm")}Reminders & lists</h2>
    <div class="list">${up.map(x => `<div class="row"><div class="t">${esc(x.text)}<div class="sub">${fmt(x.due_at)}${x.repeat !== "none" ? " · " + x.repeat : ""}</div></div><button class="ghost" data-cancel="${x.id}">Cancel</button></div>`).join("") || `<div class="sub">No upcoming reminders. Ask ${esc(NAME())}: “remind me to…”.</div>`}</div>
    <div style="display:flex;gap:8px;margin-top:8px"><input id="rt" placeholder="New reminder"><input id="rw" type="datetime-local" style="max-width:220px"><button id="radd">Add</button></div>
    ${Object.entries(l.lists).map(([name, items]) => `<h3 class="hud" style="margin-top:16px">${esc(name)} <button class="ghost" data-clear="${esc(name)}">Clear ticked</button></h3>
      <div class="list">${items.map(i => `<div class="row"><input type="checkbox" style="width:auto" data-tick="${i.id}" ${i.done ? "checked" : ""}><div class="t" style="${i.done ? "text-decoration:line-through;color:var(--muted)" : ""}">${esc(i.text)}</div><button class="ghost" data-deli="${i.id}">${icon("trash", "solo")}</button></div>`).join("")}</div>
      <div style="display:flex;gap:8px;margin-top:4px"><input data-newin="${esc(name)}" placeholder="Add to ${esc(name)}"></div>`).join("")}
    <div style="display:flex;gap:8px;margin-top:16px"><input id="ln" placeholder="New list, e.g. parts" style="max-width:200px"><input id="li" placeholder="First item"><button id="ladd">Add</button></div>
    <div class="btns"><button id="close">Close</button></div>`);
  const again = () => { d.remove(); openTasks(); };
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelectorAll("[data-cancel]").forEach(el => el.onclick = async () => { await api(`/reminders/${el.dataset.cancel}`, { method: "POST", json: { cancel: true } }); again(); });
  d.querySelector("#radd").onclick = async () => { const t = d.querySelector("#rt").value.trim(), w = d.querySelector("#rw").value; if (!t || !w) return;
    try { await api("/reminders", { method: "POST", json: { text: t, due_at: new Date(w).getTime() } }); again(); } catch (e) { alertBox(e.message); } };
  d.querySelectorAll("[data-tick]").forEach(el => el.onchange = async () => { await api(`/lists/items/${el.dataset.tick}`, { method: "POST", json: { done: el.checked } }); again(); });
  d.querySelectorAll("[data-deli]").forEach(el => el.onclick = async () => { await api(`/lists/items/${el.dataset.deli}`, { method: "DELETE" }); again(); });
  d.querySelectorAll("[data-clear]").forEach(el => el.onclick = async () => { await api(`/lists/${encodeURIComponent(el.dataset.clear)}/clear`, { method: "POST", json: {} }); again(); });
  d.querySelectorAll("[data-newin]").forEach(el => el.onkeydown = async e => { if (e.key === "Enter" && el.value.trim()) { await api(`/lists/${encodeURIComponent(el.dataset.newin)}`, { method: "POST", json: { items: [el.value.trim()] } }); again(); } });
  d.querySelector("#ladd").onclick = async () => { const n = d.querySelector("#ln").value.trim(), i = d.querySelector("#li").value.trim(); if (!n || !i) return;
    await api(`/lists/${encodeURIComponent(n)}`, { method: "POST", json: { items: [i] } }); again(); };
}
async function openSettings() {
  let gh = {}; try { gh = await api("/github"); } catch {}
  const d = dialog(`<h2>${icon("settings")}Settings</h2>
    <label class="sub">Assistant name (this browser)</label><input id="nm" value="${esc(NAME())}">
    <label class="sub" style="display:block;margin-top:10px">Thinking before answering</label>
    <select id="th">${[["auto", "Auto: only for hard questions"], ["on", "Always"], ["off", "Never"]].map(([v, l]) => `<option value="${v}" ${thinkPref({}) === v ? "selected" : ""}>${l}</option>`).join("")}</select>
    <p class="sub" style="margin-top:12px">Switching modes by herself: in General chats ${esc(NAME())} picks the mode that fits each message and shows a chip you can change.</p>
    <select id="am">${[["auto", "Auto: switch when sure"], ["ask", "Ask first: only suggest"], ["off", "Off: only the mode I pick"]].map(([v, l]) => `<option value="${v}" ${store.get("autoModes", "auto") === v ? "selected" : ""}>${l}</option>`).join("")}</select>
    <h3 class="hud" style="margin-top:18px">GitHub</h3>
    <p class="sub">${gh.token_set ? (gh.user ? "Connected as <b>" + esc(gh.user) + "</b>." : "Token saved" + (gh.error ? ": " + esc(gh.error) : ".")) : "Not connected: public repositories only, read-only."}
    For private repos and commits, create a fine-grained token on GitHub (Settings → Developer settings → Fine-grained tokens) with access to your repos and permissions: Contents read/write, Pull requests read/write, Actions read, Issues read.</p>
    <input id="tok" type="password" placeholder="${gh.token_set ? "•••••••• (leave empty to keep)" : "GitHub token"}" autocomplete="off">
    <input id="repo" style="margin-top:8px" placeholder="Default repository, e.g. courier-iot/StellAI" value="${esc(gh.default_repo || "")}">
    <div class="btns" style="justify-content:flex-start">${gh.token_set ? `<button class="ghost" id="untok">Remove token</button>` : ""}</div>
    <h3 class="hud" style="margin-top:18px">This browser</h3>
    <button id="logout">Forget the API key here</button>
    <div class="btns"><button id="close">Cancel</button><button class="primary" id="save">Save</button></div>`);
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelector("#logout").onclick = () => { store.set("key", ""); location.reload(); };
  if (d.querySelector("#untok")) d.querySelector("#untok").onclick = async () => { await api("/github", { method: "POST", json: { token: "" } }); d.remove(); openSettings(); };
  d.querySelector("#save").onclick = async () => {
    store.set("name", d.querySelector("#nm").value.trim() || "Stella"); store.set("thinkingMode", d.querySelector("#th").value); store.set("autoModes", d.querySelector("#am").value);
    const body = { default_repo: d.querySelector("#repo").value.trim() }; const t = d.querySelector("#tok").value.trim(); if (t) body.token = t;
    try { const st = await api("/github", { method: "POST", json: body }); d.remove(); if (st.error) alertBox("GitHub: " + st.error); renderBar(); renderMsgs(); }
    catch (e) { alertBox(e.message); }
  };
}

// ---------------------------------------------------------------- search past chats
let searchTimer = 0, searchSeq = 0;
function renderSearch(q, hits, note) {
  const ql = q.toLowerCase(); const seen = new Set(hits.map(h => h.conversation_id));
  const titles = [...S.convs.values()].filter(c => !c.deleted && !seen.has(c.id) && (c.title || "").toLowerCase().includes(ql))
    .sort((a, b) => b.updated_at - a.updated_at);
  $("#searchResults").innerHTML = (note ? `<div class="sub" style="padding:4px 10px">${esc(note)}</div>` : "")
    + hits.map(h => `<div class="item hit${h.conversation_id === S.current ? " sel" : ""}" data-c="${esc(h.conversation_id)}"><span class="t" style="white-space:normal">${esc(h.title || "Untitled")}
        <div class="sub">${esc(h.date || "")}</div><div class="ex">${esc(h.excerpt || "")}</div></span></div>`).join("")
    + titles.map(c => `<div class="item${c.id === S.current ? " sel" : ""}" data-c="${c.id}"><span class="t">${esc(c.title || "Untitled")}</span></div>`).join("")
    || `<div class="sub" style="padding:4px 10px">${q.length < 3 ? "Keep typing…" : "No chats found."}</div>`;
}
function onSearch() {
  const q = $("#search").value.trim(); clearTimeout(searchTimer);
  $("#searchView").style.display = q ? "" : "none"; $("#listView").style.display = q ? "none" : "";
  if (!q) return;
  renderSearch(q, [], q.length >= 3 ? "Searching…" : "");
  if (q.length < 3) return;
  const seq = ++searchSeq;
  searchTimer = setTimeout(async () => {
    try { const r = await api("/search?q=" + encodeURIComponent(q)); if (seq === searchSeq) renderSearch(q, r.results || []); }
    catch { if (seq === searchSeq) renderSearch(q, [], "Server search unavailable; title matches only."); }
  }, 400);
}

// ---------------------------------------------------------------- server: status, models, health check, backups
async function openServer() {
  const d = dialog(`<h2>${icon("server")}Server</h2><div id="sv">Loading…</div>
    <h3 class="hud" style="margin-top:18px">Health check</h3><button id="doc">Check everything</button> <button id="spd">Speed test</button><div id="docOut"></div>
    <h3 class="hud" style="margin-top:18px">Backups</h3><div id="bk">Loading…</div>
    <div class="btns"><button id="close">Close</button></div>`);
  let timer = 0, closed = false;
  d.querySelector("#close").onclick = () => { closed = true; clearTimeout(timer); d.remove(); };
  const msg = (t, bad) => { const n = document.createElement("p"); n.className = "sub"; n.style.color = bad ? "var(--red)" : "var(--gold)"; n.textContent = t; d.querySelector("#sv").prepend(n); };
  const fmtUp = s => s < 3600 ? Math.floor(s / 60) + " min" : s < 86400 ? Math.floor(s / 3600) + " h " + Math.floor(s % 3600 / 60) + " min" : Math.floor(s / 86400) + " days";
  async function loadServer() {
    if (closed || !d.isConnected) return;
    let st; try { st = await api("/server"); } catch (e) { d.querySelector("#sv").innerHTML = `<p class="err">Couldn't reach the server: ${esc(e.message)}</p>`; return; }
    const rows = [["Model", st.model || st.model_path || "unknown"], ["Context", st.context ? st.context + " tokens" : "–"],
      ["Runs on", (st.backend || "").toUpperCase() || "–"], ["Memory", st.ram_gb + " GB RAM" + (st.vram_gb ? " · " + st.vram_gb + " GB GPU" : "")], ["Up for", fmtUp(st.uptime_s || 0)]];
    const models = st.models || [];
    d.querySelector("#sv").innerHTML = `<p><span class="status${st.loaded ? " on" : ""}"></span> ${st.loaded ? "Running" : "Loading the model…"}</p>
      <div class="list">${rows.map(([k, v]) => `<div class="row"><span class="sub" style="width:90px">${k}</span><span class="t">${esc(v)}</span></div>`).join("")}</div>
      ${models.length ? `<h3 class="hud" style="margin-top:18px">Model</h3><p class="sub">Switching takes about a minute while the new model loads.</p>
      <div class="list">${models.map(m => { const dl = m.download || null; const pct = dl && dl.total ? Math.floor(dl.done * 100 / dl.total) : 0;
        return `<div class="row" style="flex-wrap:wrap"><div class="t"><b style="${m.current ? "color:var(--accent)" : ""}">${esc(m.label)}${m.current ? " · in use" : ""}</b>
          <div class="sub">${esc(m.about || "")} ${m.size_gb} GB, ${Math.round((m.context || 0) / 1024)}K context.</div>
          ${dl && dl.state === "downloading" ? `<div class="bar2"><div style="width:${pct}%"></div></div><div class="sub">Downloading… ${pct}%</div>` : ""}
          ${dl && dl.state === "failed" ? `<div class="sub" style="color:var(--red)">Download failed: ${esc(dl.error)}</div>` : ""}</div>
          ${m.current ? "" : m.installed ? `<button data-sw="${esc(m.key)}">Switch</button>` : dl && dl.state === "downloading" ? "" : `<button data-dl="${esc(m.key)}">Download</button>`}</div>`; }).join("")}</div>`
      : `<p class="sub">This server can't switch models from here yet (re-run its setup to enable it).</p>`}`;
    d.querySelectorAll("[data-sw]").forEach(b => b.onclick = async () => {
      const m = models.find(x => x.key === b.dataset.sw); if (!confirm(`Switch to ${m.label}? ${NAME()} is unavailable for about a minute while it loads.`)) return;
      try { const r = await api("/server/model", { method: "POST", json: { key: m.key } }); await loadServer(); msg(r.message || "Switching…"); } catch (e) { msg(e.message, true); } });
    d.querySelectorAll("[data-dl]").forEach(b => b.onclick = async () => {
      try { const r = await api("/server/download", { method: "POST", json: { key: b.dataset.dl } }); await loadServer(); msg(r.message || "Downloading…"); } catch (e) { msg(e.message, true); } });
    const busy = !st.loaded || models.some(m => m.download && m.download.state === "downloading");
    clearTimeout(timer); if (busy) timer = setTimeout(loadServer, 2500);
  }
  async function loadBackups() {
    let b; try { b = await api("/backups"); } catch (e) { d.querySelector("#bk").innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
    d.querySelector("#bk").innerHTML = `<p class="sub">Every night the server saves your chats, memories, projects, photos and wake words. It keeps the last ${b.keep}.</p>
      <p>${b.last ? "Last backup: " + new Date(b.last).toLocaleString() : "No backup yet"}</p>${b.error ? `<p class="err">${esc(b.error)}</p>` : ""}
      <button class="primary" id="bnow">Back up now</button>
      <label class="sub" style="display:block;margin-top:10px">Also copy to (folder on the server, e.g. a USB drive)</label>
      <div style="display:flex;gap:8px"><input id="bcopy" value="${esc(b.copy_to || "")}" placeholder="/media/you/USB/stellai"><input id="bkeep" type="number" min="2" max="60" value="${b.keep}" style="width:80px" title="How many to keep"><button id="bsave">Save</button></div>
      <p class="sub">To restore: Linux: run stellai-restore.sh on the server. Windows: StellAI app → Restore a backup.</p>
      <div class="list">${(b.backups || []).slice(0, 14).map(f => `<div class="row"><span class="t sub">${esc(f.name)}</span><span class="sub">${Math.round(f.size / 1e6)} MB</span></div>`).join("")}</div>`;
    d.querySelector("#bnow").onclick = async e => { e.target.disabled = true; e.target.textContent = "Backing up…";
      try { await api("/backups", { method: "POST", json: {} }); } catch (err) { alertBox(err.message); } loadBackups(); };
    d.querySelector("#bsave").onclick = async () => {
      try { await api("/backups/config", { method: "POST", json: { copy_to: d.querySelector("#bcopy").value.trim(), keep: +d.querySelector("#bkeep").value || 14 } }); loadBackups(); }
      catch (err) { alertBox(err.message); } };
  }
  d.querySelector("#doc").onclick = async e => {
    e.target.disabled = true; d.querySelector("#docOut").innerHTML = `<p class="sub">Checking… (takes a few seconds)</p>`;
    try { const r = await api("/doctor"); const ic = { ok: [icon("check"), "var(--accent)"], warn: [icon("warn"), "var(--gold)"], fail: [icon("cross"), "var(--red)"] };
      d.querySelector("#docOut").innerHTML = (r.checks || []).map(c => { const [i, col] = ic[c.state] || ic.fail;
        return `<div class="check"><span class="ic" style="color:${col}">${i}</span><div><b>${esc(c.name)}</b>: ${esc(c.detail)}${c.fix && c.state !== "ok" ? `<div class="sub">→ ${esc(c.fix)}</div>` : ""}</div></div>`; }).join(""); }
    catch (err) { d.querySelector("#docOut").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
    e.target.disabled = false;
  };
  // Real numbers for this server: reading, writing, waking, hearing and speaking, compared with the last run
  d.querySelector("#spd").onclick = async e => {
    e.target.disabled = true; d.querySelector("#docOut").innerHTML = `<p class="sub">Measuring… (up to a minute; ${esc(NAME())} can't chat meanwhile)</p>`;
    try { const r = await api("/speedtest", { method: "POST", json: {} });
      d.querySelector("#docOut").innerHTML = (r.results || []).map(x => {
        if (x.error) return `<div class="check"><span class="ic" style="color:var(--red)">${icon("cross")}</span><div><b>${esc(x.name)}</b>: ${esc(x.error)}</div></div>`;
        const up = x.unit.endsWith("/s"), ch = x.change_pct;
        const diff = ch ? ` <span style="color:${(ch > 0) === up ? "var(--accent)" : "var(--gold)"}">(${ch > 0 ? "+" : ""}${ch}% vs last time)</span>` : "";
        return `<div class="check"><span class="ic" style="color:var(--accent)">${icon("diamond")}</span><div><b>${esc(x.name)}</b>: ${x.value} ${esc(x.unit)}${diff}<div class="sub">${esc(x.detail || "")}</div></div></div>`; }).join("")
        + `<p class="sub">${esc(r.model || "")} · took ${r.took_s} s</p>`; }
    catch (err) { d.querySelector("#docOut").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
    e.target.disabled = false;
  };
  loadServer(); loadBackups();
}

// ---------------------------------------------------------------- photos
async function addPhotos(files) {
  for (const f of files) {
    if (!f.type.startsWith("image/")) continue;
    const img = await createImageBitmap(f); const scale = Math.min(1, 1280 / Math.max(img.width, img.height));
    const c = document.createElement("canvas"); c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    const blob = await new Promise(r => c.toBlob(r, "image/jpeg", 0.85));
    S.attachments.push({ id: hex16(), blob, dataUrl: c.toDataURL("image/jpeg", 0.85) });
  }
  renderAtts();
}
function renderAtts() {
  $("#atts").innerHTML = S.attachments.map((a, i) => `<div class="a"><img src="${a.dataUrl}"><button data-ra="${i}">${icon("close", "solo")}</button></div>`).join("");
  $("#atts").querySelectorAll("[data-ra]").forEach(b => b.onclick = () => { S.attachments.splice(+b.dataset.ra, 1); renderAtts(); });
}

// ---------------------------------------------------------------- wiring
function newChat(projectId = "") { if (S.busy) stop(); S.current = null; S.pendingProject = projectId; S.pendingMode = ""; S.context = null; S.error = null; S.editing = null; store.set("current", ""); renderSide(); renderBar(); renderMsgs(); $("#input").focus(); }
function openChat(id) { if (S.busy) stop(); S.current = id; S.context = null; S.error = null; S.editing = null; store.set("current", id); renderSide(); renderBar(); renderMsgs(); updateMeter(); $("#side").classList.remove("open"); refreshDocs(); }
function autosize() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(240, t.scrollHeight) + "px"; }
$("#newChat").onclick = () => newChat();
$("#newProject").onclick = () => openProject("new");
$("#openLibrary").onclick = () => openProject("library");
$("#openTasks").onclick = openTasks;
$("#openSettings").onclick = openSettings;
$("#openServer").onclick = openServer;
$("#search").addEventListener("input", onSearch);
$("#search").addEventListener("keydown", e => { if (e.key === "Escape") { $("#search").value = ""; onSearch(); } });
$("#searchResults").onclick = e => { const el = e.target.closest("[data-c]"); if (el) { $("#search").value = ""; onSearch(); openChat(el.dataset.c); } };
$("#modeChip").onclick = pickMode;
$("#projectChip").onclick = () => openProject(curProjectId());
$("#openMemory").onclick = () => openMemory();
// Memory: what Stella remembers about you, and facts she suggests from quiet chats (keep or drop them)
async function openMemory() {
  const d = dialog(`<h2>${icon("memory")}Memory</h2><p class="sub">${esc(NAME())} uses these facts in every conversation, on all your devices. She also reads chats that have gone quiet and suggests facts to keep.</p>
    <label class="sub">Learning from quiet chats</label>
    <select id="mm"><option value="review">Suggest, I review</option><option value="auto">Save by itself</option><option value="off">Off</option></select>
    <div id="ms"></div><h3 class="hud" style="margin-top:12px">Remembered</h3><div class="list" id="ml"><p class="sub">Loading…</p></div>
    <div style="display:flex;gap:8px;margin-top:8px"><input id="mn" placeholder="e.g. I use KiCad for PCB design"><button id="madd">Add</button></div>
    <div class="btns"><button id="close">Close</button></div>`);
  const draw = async () => {
    try {
      const [sg, mem] = await Promise.all([api("/memories/suggestions"), api("/memories")]);
      S.memories = mem.memories || [];
      d.querySelector("#mm").value = sg.mode || "review";
      const sug = sg.suggestions || [];
      d.querySelector("#ms").innerHTML = sug.length ? `<h3 class="hud" style="margin-top:12px;color:var(--accent)">Suggested from your chats (${sug.length})</h3><div class="list">` +
        sug.map(x => `<div class="row"><span class="t">${esc(x.text)}${x.replaces_text ? `<br><span class="sub">Replaces: ${esc(x.replaces_text)}</span>` : ""}</span><button data-keep="${x.id}">Keep</button><button class="ghost" data-drop="${x.id}">Drop</button></div>`).join("") + "</div>" : "";
      d.querySelector("#ml").innerHTML = S.memories.length ? S.memories.map(m => `<div class="row"><span class="t">${esc(m.text)}</span><button class="ghost" data-forget="${m.id}" title="Forget">${icon("close", "solo")}</button></div>`).join("") : `<p class="sub">Nothing yet.</p>`;
    } catch (e) { d.querySelector("#ml").innerHTML = `<p class="sub" style="color:var(--red)">${esc(e.message)}</p>`; }
  };
  draw();
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelector("#mm").onchange = async e => { try { await api("/memories/settings", { method: "POST", json: { mode: e.target.value } }); } catch (er) { notice(er.message); } };
  d.querySelector("#madd").onclick = async () => { const t = d.querySelector("#mn").value.trim(); if (!t) return; d.querySelector("#mn").value = "";
    try { await api("/memories", { method: "POST", json: { text: t } }); } catch (e) { notice(e.message); } draw(); };
  d.onclick = async e => {
    const t = e.target.dataset || {};
    try {
      if (t.keep || t.drop) await api("/memories/suggestions/" + (t.keep || t.drop), { method: "POST", json: { accept: !!t.keep } });
      else if (t.forget) await api("/memories/" + t.forget, { method: "DELETE" });
      else return;
    } catch (er) { notice(er.message); }
    draw();
  };
}
$("#notesChip").onclick = () => S.current ? openNotes(S.current) : notice("Start a chat first.");
// ---------------------------------------------------------------- documents beside the chat
// Long code and chapters live here, not in the chat: Stella edits them a section at a time and the chat keeps a
// one-line reference. Every change is a version; you can edit too (the server refuses if Stella changed it meanwhile).
const D = { list: [], cur: null, doc: null, editing: false, conv: null };
async function refreshDocs(focus, show) {
  const conv = S.current; if (!conv) { D.list = []; D.doc = null; drawDocs(); return; }
  try { D.list = (await api(`/chats/${encodeURIComponent(conv)}/docs`)).docs || []; } catch { D.list = []; }
  if (conv !== D.conv) { D.conv = conv; D.cur = null; D.editing = false; if (!focus) document.body.classList.remove("docs"); }
  if (focus) D.cur = focus; if (!D.list.some(d => d.id === D.cur)) D.cur = D.list.length ? D.list[D.list.length - 1].id : null;
  if (show && D.cur) document.body.classList.add("docs");
  if (D.cur && !D.editing) { try { D.doc = await api(`/docs/${D.cur}`); } catch { D.doc = null; } } else if (!D.cur) D.doc = null;
  drawDocs();
}
function openDocs(id) { document.body.classList.add("docs"); D.editing = false; refreshDocs(id); }
function drawDocs() {
  $("#docsChip").innerHTML = icon("doc", D.list.length ? "" : "solo") + (D.list.length || "");
  $("#docSel").innerHTML = D.list.map(d => `<option value="${d.id}" ${d.id === D.cur ? "selected" : ""}>${esc(d.title)} · v${d.version}</option>`).join("") || `<option>No documents</option>`;
  const d = D.doc;
  if (!d) { $("#docBody").innerHTML = `<p class="sub">No documents in this chat yet. Ask ${esc(NAME())} to write something long (“draft chapter 1 in a document”) or press ＋.</p>`;
    $("#docOutline").innerHTML = ""; $("#docInfo").textContent = ""; $("#docVer").innerHTML = ""; return; }
  $("#docInfo").textContent = `v${d.version} · ${d.text.split("\n").length} lines${d.language ? " · " + d.language : ""}`;
  $("#docVer").innerHTML = `<option value="">Versions…</option>` + d.versions.map(v => `<option value="${v.version}">v${v.version} · ${v.by === "user" ? "you" : esc(NAME())}${v.note ? " · " + esc(v.note) : ""}</option>`).join("");
  $("#docEdit").textContent = D.editing ? "Save" : "Edit";
  if (D.editing) { $("#docOutline").innerHTML = ""; $("#docBody").innerHTML = `<textarea id="docText"></textarea>`; $("#docText").value = d.text; $("#docText").focus(); return; }
  const heads = d.kind === "code" ? [] : d.text.split("\n").map((l, i) => [l.match(/^(#{1,3})\s+(.+)/), i]).filter(x => x[0]);
  $("#docOutline").innerHTML = heads.slice(0, 40).map(([m, i]) => `<button class="ghost" data-h="${i}">${esc(m[2])}</button>`).join("");
  $("#docBody").innerHTML = d.kind === "code" ? `<pre class="code"><code>${esc(d.text)}</code></pre>` : `<div class="md">${md(d.text)}</div>`;
}
$("#docsChip").onclick = () => { if (!S.current) return notice("Start a chat first."); document.body.classList.toggle("docs"); refreshDocs(); };
$("#docClose").onclick = () => { document.body.classList.remove("docs"); D.editing = false; };
$("#docSel").onchange = e => { D.editing = false; refreshDocs(e.target.value); };
$("#docOutline").onclick = e => { const h = e.target.dataset.h; if (h == null || !D.doc) return;
  const title = D.doc.text.split("\n")[+h].replace(/^#+\s+/, "").trim();
  const el = [...$("#docBody").querySelectorAll("h1,h2,h3,h4")].find(x => x.textContent.trim() === title); if (el) el.scrollIntoView({ block: "start" }); };
$("#docCopy").onclick = () => D.doc && navigator.clipboard.writeText(D.editing ? $("#docText").value : D.doc.text);
$("#docDl").onclick = () => { if (!D.doc) return; const ext = { python: "py", kotlin: "kt", javascript: "js", typescript: "ts", cpp: "cpp", c: "c", java: "java", rust: "rs", go: "go", bash: "sh", html: "html", css: "css", json: "json", yaml: "yaml" }[D.doc.language] || (D.doc.kind === "code" ? "txt" : "md");
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([D.doc.text], { type: "text/plain" }));
  a.download = /\.[a-z0-9]{1,5}$/i.test(D.doc.title) ? D.doc.title : D.doc.title.replace(/[^\w .-]+/g, "_") + "." + ext; a.click(); URL.revokeObjectURL(a.href); };
$("#docEdit").onclick = async () => {
  if (!D.doc) return;
  if (!D.editing) { D.editing = true; drawDocs(); return; }
  try { D.doc = await api(`/docs/${D.doc.id}`, { method: "POST", json: { text: $("#docText").value, base_version: D.doc.version } }); D.editing = false; refreshDocs(D.doc.id); }
  catch (e) { notice(/changed meanwhile/.test(e.message) ? `${NAME()} changed this document while you were editing. Copy your text, reopen it and add your change again.` : "Couldn't save: " + e.message); }
};
$("#docVer").onchange = async e => { const v = e.target.value; if (!v || !D.doc) return;
  if (!confirm(`Go back to version ${v}? The current text stays in the version list.`)) { e.target.value = ""; return; }
  try { D.doc = await api(`/docs/${D.doc.id}`, { method: "POST", json: { revert: +v } }); D.editing = false; refreshDocs(D.doc.id); } catch (err) { notice("Couldn't go back: " + err.message); } };
$("#docNew").onclick = async () => { if (!S.current) return notice("Start a chat first."); const t = prompt("Document title (end it in .py, .kt, … for code):"); if (!t) return;
  const ext = (t.match(/\.([a-z0-9]{1,5})$/i) || [])[1] || ""; const lang = { py: "python", kt: "kotlin", js: "javascript", ts: "typescript", cpp: "cpp", c: "c", h: "c", java: "java", rs: "rust", go: "go", sh: "bash", ino: "cpp" }[ext.toLowerCase()] || "";
  try { const d = await api(`/chats/${encodeURIComponent(S.current)}/docs`, { method: "POST", json: { title: t, text: "", language: lang } }); D.editing = true; await refreshDocs(d.id, true); D.doc = d; drawDocs(); } catch (e) { notice("Couldn't create it: " + e.message); } };
// Pinned notes: facts for one chat (goals, decisions, names, story facts), always included, never summarized away
async function openNotes(convId) {
  const d = dialog(`<h2>${icon("pin")}Pinned notes</h2><p class="sub">${esc(NAME())} always keeps these in mind in this chat, however long it gets. She pins decisions, names and facts herself; add your own or remove ones that changed.</p>
    <div class="list" id="nl"><p class="sub">Loading…</p></div>
    <textarea id="nt" rows="2" placeholder="Pin a fact for this chat"></textarea>
    <div class="btns"><button id="close">Close</button><button class="primary" id="pin">Pin</button></div>`);
  const draw = notes => { d.querySelector("#nl").innerHTML = notes.length ? notes.map(n => `<div class="row"><span class="t">${n.source === "stella" ? icon("sparkle") : "• "}${esc(n.text)}</span><button class="ghost" data-un="${esc(n.id)}" title="Unpin">${icon("close", "solo")}</button></div>`).join("") : `<p class="sub">Nothing pinned yet.</p>`; };
  const run = async p => { try { draw((await p).notes || []); } catch (e) { d.querySelector("#nl").innerHTML = `<p class="sub" style="color:var(--red)">${esc(e.message)}</p>`; } };
  run(api(`/chats/${encodeURIComponent(convId)}/notes`));
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelector("#pin").onclick = () => { const t = d.querySelector("#nt").value.trim(); if (!t) return; d.querySelector("#nt").value = "";
    run(api(`/chats/${encodeURIComponent(convId)}/notes`, { method: "POST", json: { text: t } })); };
  d.querySelector("#nl").onclick = e => { const id = e.target.dataset && e.target.dataset.un;
    if (id) run(api(`/chats/${encodeURIComponent(convId)}/notes/${encodeURIComponent(id)}`, { method: "DELETE" })); };
}
$("#meter").onclick = () => {
  const i = S.context; if (!i) return;
  const d = dialog(`<h2>${esc(NAME())}'s memory for this chat</h2><p>${i.used} of ${i.limit} tokens used (${Math.round(Math.min(1, i.used / i.limit) * 100)}%).</p>
    <p class="sub">Long chats don't fill up: while ${esc(NAME())} is idle, the server folds the oldest part of the chat into a summary (from about 60% full), and the newest part stays word for word. She can look up the exact earlier words when she needs them, and pinned notes are never summarized. Every message stays here and on your other devices.</p>
    <div class="btns"><button id="close">Close</button><button id="cont" ${S.busy ? "disabled" : ""}>Continue in a new chat</button><button class="primary" id="ref" ${S.busy ? "disabled" : ""}>Refresh now</button></div>`);
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelector("#cont").onclick = async () => { d.remove(); S.busy = true; S.status = "Carrying this chat over…"; renderBar();
    try { const r = await api(`/chats/${encodeURIComponent(S.current)}/continue`, { method: "POST", json: {} });
      await sync(); S.current = r.conversation_id; store.set("current", S.current); renderSide(); renderMsgs(); updateMeter();
    } catch (e) { notice("Couldn't continue in a new chat: " + e.message); }
    S.busy = false; S.status = ""; renderBar(); };
  d.querySelector("#ref").onclick = async () => { d.remove(); S.busy = true; S.status = "Refreshing context…"; renderBar();
    try { if (!(await compact(S.current, 0))) notice("Nothing new to summarize yet."); } catch (e) { notice("Couldn't refresh: " + e.message); }
    S.busy = false; S.status = ""; renderMsgs(); renderBar(); sync().then(updateMeter); };
};
$("#send").onclick = send;
$("#attach").onclick = () => $("#file").click();
$("#file").onchange = () => { addPhotos([...$("#file").files]); $("#file").value = ""; };
$("#notice").onclick = () => { $("#notice").style.display = "none"; };
$("#input").addEventListener("input", autosize);
$("#input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("#input").addEventListener("paste", e => { const files = [...(e.clipboardData || {}).files || []].filter(f => f.type.startsWith("image/")); if (files.length) { e.preventDefault(); addPhotos(files); } });
$("#menuBtn").onclick = () => $("#side").classList.toggle("open");
if (matchMedia("(max-width:760px)").matches) $("#menuBtn").style.display = "";
$("#projects").onclick = e => { const el = e.target.closest("[data-p]"); if (el) openProject(el.dataset.p); };
$("#chats").onclick = e => {
  const del = e.target.closest("[data-del]");
  if (del) { const c = S.convs.get(del.dataset.del); if (c && confirm(`Delete “${c.title}” on all your devices?`)) {
    saveConv({ ...c, deleted: true, updated_at: nextTs() }); S.msgs.delete(c.id); if (S.current === c.id) newChat(); renderSide(); sync(); } return; }
  const el = e.target.closest("[data-c]"); if (el) openChat(el.dataset.c);
};
$("#wrap").onchange = e => {
  const t = e.target; if (!t.dataset || !t.dataset.redo || !t.value || !S.current) return;
  if (t.value.startsWith("pin:")) { const id = t.value.slice(4); saveConv({ ...curConv(), mode: id === "general" ? "" : id, updated_at: nextTs() }); sync().then(updateMeter); renderBar(); renderMsgs(); return; }
  S.forced.set(S.current, t.value); retry();
};
$("#wrap").onclick = e => {
  const t = e.target;
  if (t.dataset.doc) { openDocs(t.dataset.doc); return; }
  if (t.classList.contains("cp")) { navigator.clipboard.writeText(t.nextElementSibling.textContent); t.textContent = "Copied"; setTimeout(() => t.textContent = "Copy", 1200); return; }
  if (t.dataset.copy) { const m = msgsOf(S.current).find(x => x.id === t.dataset.copy); if (m) navigator.clipboard.writeText(m.text.replace(/!\[[^\]]*\]\((image|diagram):[0-9a-f]+\)/g, "").trim()); return; }
  if (t.dataset.retry) { retry(); return; }
  if (t.dataset.edit) { const m = msgsOf(S.current).find(x => x.id === t.dataset.edit); if (!m) return; S.editing = m.id;
    $("#input").value = m.text.replace(PHOTO, "").trim(); autosize(); $("#input").focus(); notice("Editing a message: sending replaces it and the replies after it."); return; }
  if (t.hasAttribute("data-think")) { const b = t.nextElementSibling; b.style.display = b.style.display === "none" ? "block" : "none"; b.className = "think"; return; }
  const sum = t.closest("[data-sum]"); if (sum) { const m = msgsOf(S.current).find(x => x.id === sum.dataset.sum); const b = sum.querySelector(".sumbody");
    if (b.classList.contains("md")) { b.className = "sumbody sub"; b.textContent = NAME() + " continues from a summary of the messages above. Click to read it."; } else { b.className = "sumbody md"; b.innerHTML = md(m.text); } return; }
  if (t.tagName === "IMG") { const z = document.createElement("div"); z.className = "zoom"; z.innerHTML = `<img src="${t.src}">`; z.onclick = () => z.remove(); document.body.appendChild(z); }
};

// ---------------------------------------------------------------- voice input (microphone → Whisper on the server)
// A speech detector (Silero VAD, or loudness if it isn't downloaded) decides when you've finished: it waits longer
// when you trail off or say a short "um", and answers quickly after a clear sentence. While you're still talking,
// finished phrases are already being transcribed, so the text is ready almost as soon as you stop.
const MIC = { rec: null };
const VOICE_STYLE = "Voice conversation: the user said this out loud and will hear your reply spoken. Answer the way a person talks: " +
  "usually one to three short sentences, warm and natural, with contractions. No markdown, lists, headings, tables, code or links. " +
  "If the answer needs detail, give the key point first and offer to go into more detail or put it on screen.";
const END_PHRASE = /^(ok(ay)?[, ]+)?(thanks|thank you( very much| so much)?|thanks a lot|cheers|that'?s (all|it)|nothing|no( thanks| thank you)?|never ?mind|stop|good ?bye|bye( bye)?|i'?m good|all good)[.!, ]*(stella)?[.!]*$/i;
const ACKS = ["One sec, let me check.", "Let me look that up.", "On it, one moment.", "Just a moment.", "Let me check."];
function micHelp() {
  alertBox("The microphone only works on a secure page. Options: open the chat on the server computer itself " +
    "(http://localhost:8092), use Tailscale HTTPS (run 'sudo tailscale serve --bg 8092' on the server and open the https://… address it shows), " +
    "or in Chrome open chrome://flags/#unsafely-treat-insecure-origin-as-secure, add this address (" + location.origin + ") and restart Chrome.");
}
async function getVad() {
  if (MIC.vad !== undefined) return MIC.vad;
  try { const mod = await import("/avatar/avatar.js"); MIC.vad = await mod.createVad(); } catch { MIC.vad = null; }
  return MIC.vad;
}
async function transcribePcm(int16) {
  const r = await fetch("/transcribe", { method: "POST", headers: { Authorization: "Bearer " + KEY, "X-Sample-Rate": "16000",
    "Content-Type": "application/octet-stream" }, body: int16.buffer });
  const j = await r.json(); if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return (j.text || "").trim();
}
async function micToggle(opts = {}) {
  if (MIC.rec) { MIC.rec.stop(); return; }
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { if (!opts.followup) micHelp(); return; }
  const followup = !!opts.followup;
  if (S.busy) stop();
  SPEAK.cancel();
  let streamIn;
  try { streamIn = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } }); }
  catch (e) { if (!followup) alertBox("Couldn't use the microphone: " + e.message); return; }
  const mod = await import("/avatar/avatar.js");
  const vadModel = await getVad(), vad = vadModel ? vadModel.stream() : null;
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaStreamSource(streamIn), proc = ctx.createScriptProcessor(4096, 1, 1);
  const resample = mod.makeResampler(ctx.sampleRate);
  const SR = 16000, FRAME = 512;
  let audio = new Float32Array(SR * 32), n = 0;              // everything heard, at 16 kHz
  let frameAt = 0, noise = 0.004, heard = false, speech = 0, lastSpeech = 0, burstStart = -1, lastBurst = 0, speechRun = 0, done = false;
  let segStart = 0, segCutFor = -1; const segments = [];     // phrases sent for transcription while you keep talking
  const startedAt = performance.now();
  const toInt16 = (a, b) => { const out = new Int16Array(Math.max(0, b - a)); for (let i = a; i < b; i++) out[i - a] = Math.max(-1, Math.min(1, audio[i])) * 32767; return out; };
  let working = false;
  async function analyse() {
    if (working) return; working = true;
    try {
      while (!done && n - frameAt >= FRAME) {
        const f = audio.subarray(frameAt, frameAt + FRAME);
        let rms = 0; for (let i = 0; i < FRAME; i += 2) rms += f[i] * f[i]; rms = Math.sqrt(rms / (FRAME / 2));
        AV.micLevel = Math.min(1, rms * 8);
        let p;
        if (vad) { try { p = await vad.prob(Float32Array.from(f)); } catch { p = rms > Math.max(0.02, noise * 3) ? 0.9 : 0.1; } }
        else p = rms > Math.max(0.02, noise * 3) ? 0.9 : 0.1;
        if (p < 0.3) noise = noise * 0.97 + rms * 0.03;
        const t = (frameAt + FRAME) / SR;
        if (p > 0.5) { speechRun++; if (speechRun >= 2) { if (!heard) { heard = true; } speech += FRAME / SR; lastSpeech = t; if (burstStart < 0) burstStart = t; } }
        else { speechRun = 0; if (burstStart >= 0 && t - lastSpeech > 0.15) { lastBurst = lastSpeech - burstStart; burstStart = -1; } }
        frameAt += FRAME;
        if (heard) {
          const quiet = t - lastSpeech;
          // a phrase just ended mid-sentence: start transcribing it now
          if (quiet >= 0.4 && segCutFor !== lastSpeech && (lastSpeech * SR) - segStart > SR * 1.2) {
            segCutFor = lastSpeech;
            const cut = Math.floor((lastSpeech + 0.2) * SR); segments.push(transcribePcm(toInt16(segStart, cut)).catch(() => "")); segStart = cut;
          }
          const need = speech < 0.8 || (burstStart < 0 && lastBurst < 0.35) ? 1.25 : 0.75;    // "um…" or a short start: wait longer
          if (quiet >= need) { rec.stop(); break; }
        } else if (t > (followup ? 5 : 8)) { rec.stop(); break; }
        if (t > 30) { rec.stop(); break; }
      }
    } finally { working = false; }
  }
  proc.onaudioprocess = ev => {
    if (done) return;
    const out = resample(ev.inputBuffer.getChannelData(0));
    if (n + out.length > audio.length) { const bigger = new Float32Array(audio.length * 2); bigger.set(audio.subarray(0, n)); audio = bigger; }
    audio.set(out, n); n += out.length;
    analyse();
  };
  src.connect(proc); proc.connect(ctx.destination);
  const rec = MIC.rec = {
    async stop() {
      if (done) return; done = true; MIC.rec = null; AV.micLevel = 0;
      try { proc.disconnect(); src.disconnect(); } catch {}
      streamIn.getTracks().forEach(t => t.stop()); ctx.close();
      $("#mic").classList.remove("on"); updateAvatarState();
      if (!heard) { if (!followup) avatarCaption(""); postAvatar({ state: "idle" }); return; }
      const end = Math.min(n, Math.floor((lastSpeech + 0.3) * SR));
      if (end - segStart > SR * 0.2) segments.push(transcribePcm(toInt16(segStart, end)).catch(e => { notice("Couldn't transcribe: " + e.message); return ""; }));
      S.status = "Listening… transcribing"; renderBar(); avatarCaption("…", "you");
      const text = (await Promise.all(segments)).filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
      S.status = ""; renderBar();
      if (!text) { avatarCaption("(didn't catch that)", "you"); postAvatar({ state: "idle" }); return; }
      avatarCaption(text, "you");
      if (followup && END_PHRASE.test(text)) {      // "thanks", "that's all": the conversation is over
        postAvatar({ state: "idle" });
        if (/thank|cheers/i.test(text) && AV.cfg.speak) { SPEAK.enabled = true; SPEAK.say(["Anytime!", "You're welcome!", "Happy to help."][Math.floor(Math.random() * 3)]); }
        return;
      }
      $("#input").value = ($("#input").value ? $("#input").value + " " : "") + text; autosize();
      S.voiceNext = AVATAR_MODE && !!AV.cfg.speak; send();    // spoken replies get the spoken style
    },
  };
  $("#mic").classList.add("on"); updateAvatarState();
  postAvatar({ state: "listening" });
  if (followup) avatarStatus("");
}
// After a spoken answer, keep listening for a few seconds so you can reply without the wake word
function startFollowUp() {
  if (!AVATAR_MODE || !AV.cfg.followup || !window.isSecureContext) return;
  const go = () => { if (!MIC.rec && !S.busy) micToggle({ followup: true }); };
  const wait = () => SPEAK.speaking ? setTimeout(wait, 150) : setTimeout(go, 250);
  wait();
}

// ---------------------------------------------------------------- Stella's voice (Piper or Kokoro on the server)
const SPEAK = {
  ctx: null, analyser: null, data: null, freq: null, queue: [], playing: null, fed: 0, gen: 0, busyFetch: 0, enabled: false, onIdle: null,
  firstDone: false, saidAny: false,
  audio() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      this.analyser = this.ctx.createAnalyser(); this.analyser.fftSize = 512; this.analyser.connect(this.ctx.destination);
      this.data = new Uint8Array(this.analyser.fftSize); this.freq = new Uint8Array(this.analyser.frequencyBinCount);
    }
    return this.ctx;
  },
  level() {
    if (!this.playing || !this.analyser) return 0;
    this.analyser.getByteTimeDomainData(this.data); let s = 0;
    for (let i = 0; i < this.data.length; i++) { const v = (this.data[i] - 128) / 128; s += v * v; }
    return Math.min(1, Math.sqrt(s / this.data.length) * 4);
  },
  // Rough mouth shapes from the sound: where the energy sits (low/mid/high) says open "aa", round "oh/ou" or wide "ee/ih"
  visemes() {
    if (!this.playing || !this.analyser) return null;
    this.analyser.getByteFrequencyData(this.freq);
    const hz = this.ctx.sampleRate / this.analyser.fftSize, band = (a, b) => { let s = 0; for (let i = Math.floor(a / hz); i < Math.min(this.freq.length, b / hz); i++) s += this.freq[i]; return s; };
    const L = band(200, 800), M = band(800, 1700), H = band(1700, 4000), e = L + M + H;
    if (e < 400) return null;
    const l = L / e, m = M / e, h = H / e, c = (x) => Math.max(0, Math.min(1, x));
    const v = { aa: c((m - 0.22) * 3.5), oh: c((l - 0.42) * 3) * c(1 - h * 2.5), ou: c((l - 0.6) * 4), ee: c((h - 0.28) * 3.5), ih: c((h - 0.18) * 3) * 0.6 };
    const sum = Object.values(v).reduce((a, b) => a + b, 0) || 1;
    for (const k in v) v[k] /= sum;
    return v;
  },
  get speaking() { return !!this.playing || this.queue.length > 0 || this.busyFetch > 0; },
  begin() { this.fed = 0; },
  newReply() { this.firstDone = false; this.saidAny = false; },
  // Speak a reply while it's still streaming in. The first bit goes as soon as there's a natural phrase
  // (up to a comma), so she starts talking sooner; after that, whole sentences.
  feed(text, finish = false) {
    if (!this.enabled) return;
    let rest = text.slice(this.fed);
    const fences = (text.slice(0, this.fed).match(/```/g) || []).length;
    if (!finish && fences % 2 === 1) return;    // inside a code block: wait until it closes
    let cut = -1;
    if (finish) cut = rest.length;
    else {
      const rx = this.firstDone ? /[.!?…:;]["')\]]?\s+|\n{2,}/g : /[.!?…:;,—]["')\]]?\s+|\n{2,}/g;
      const min = this.firstDone ? 40 : 24; let m;
      while ((m = rx.exec(rest))) { if (m.index + m[0].length >= min || /\n/.test(m[0])) { cut = m.index + m[0].length; if (!this.firstDone) break; } }
      const fence = rest.indexOf("```"); if (fence >= 0 && (cut < 0 || fence < cut)) {
        const close = rest.indexOf("```", fence + 3);
        if (close < 0) cut = fence > 0 ? fence : -1; else cut = close + 3;
      }
    }
    if (cut <= 0) return;
    const piece = rest.slice(0, cut); this.fed += cut;
    if (piece.trim()) { this.firstDone = true; this.say(piece); }
  },
  say(text) {
    const gen = this.gen; this.busyFetch++; this.saidAny = true;
    const p = fetch("/tts", { method: "POST", headers: { Authorization: "Bearer " + KEY, "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: AV.cfg.voice, speed: AV.cfg.speed, engine: AV.cfg.engine, kokoro_voice: AV.cfg.kokoro_voice }) })
      .then(async r => { if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || ("HTTP " + r.status)); } return r.arrayBuffer(); })
      .then(buf => buf.byteLength > 44 ? this.audio().decodeAudioData(buf) : null)
      .catch(e => { if (gen === this.gen) avatarStatus("Voice: " + e.message); return null; })
      .finally(() => { this.busyFetch--; });
    this.queue.push({ p, gen }); this.pump();
  },
  async pump() {
    if (this.playing || !this.queue.length) return;
    const item = this.queue.shift(); this.playing = item;
    const buf = await item.p;
    if (buf && item.gen === this.gen) {
      const ctx = this.audio(); if (ctx.state === "suspended") { try { await ctx.resume(); } catch {} }
      await new Promise(res => { const s = ctx.createBufferSource(); s.buffer = buf; s.connect(this.analyser); s.onended = res; item.src = s; s.start(); });
    }
    this.playing = null;
    if (this.queue.length) this.pump(); else { updateAvatarState(); if (this.onIdle) this.onIdle(); }
  },
  cancel() {
    this.gen++; this.queue = [];
    if (this.playing && this.playing.src) { try { this.playing.src.stop(); } catch {} }
    this.playing = null; this.fed = 0;
  },
};

// ---------------------------------------------------------------- what Stella is doing, shared with avatar screens
const CLIENT_ID = uuid();
const DEVICE = AVATAR_MODE ? "avatar screen" : "desktop chat";
function postAvatar(ev) {
  if (!KEY) return;
  fetch("/avatar/event", { method: "POST", headers: { Authorization: "Bearer " + KEY, "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({ source: CLIENT_ID, device: DEVICE }, ev)) }).catch(() => {});
}

// ---------------------------------------------------------------- the avatar screen (/avatar)
const AV = { avatar: null, cfg: { style: "orb", voice: "", speed: 1, speak: true, speak_others: true, camera: "upper", voices: [] },
  remote: null, remoteUntil: 0, since: 0, micLevel: 0, captionTimer: 0 };
function avatarStatus(text) { const el = $("#avStatus"); if (el) { el.textContent = text || ""; el.style.display = text ? "" : "none"; } }
function avatarCaption(text, who = "stella") {
  const el = $("#caption"); if (!el) return;
  clearTimeout(AV.captionTimer);
  const t = String(text || "").replace(/```[\s\S]*?(```|$)/g, " [code] ").replace(/!\[[^\]]*\]\([^)]*\)/g, "").replace(/[*#`>]|(^|\s)_+|_+(\s|$)/g, "$1$2").replace(/\s+/g, " ").trim();
  const over = captionAt() === "over", max = over ? 260 : 3000;     // the window scrolls, so it can hold the whole answer
  el.innerHTML = t ? `<span class="who">${who === "you" ? "YOU" : esc(NAME().toUpperCase())}</span>${esc(t.length > max ? "…" + t.slice(-max) : t)}` : "";
  el.classList.toggle("on", !!t);
  if (t) el.scrollTop = el.scrollHeight;
  if (t && who === "stella" && !S.busy) AV.captionTimer = setTimeout(() => { if (!SPEAK.speaking && !S.busy) el.classList.remove("on"); }, 12000);
}
function updateAvatarState() {
  if (!AV.avatar) return;
  let st = "idle";
  if (MIC.rec) st = "listening";
  else if (SPEAK.speaking && (SPEAK.playing || SPEAK.queue.length)) st = "talking";
  else if (S.busy) st = S.streaming && S.streaming.text ? "talking" : "thinking";
  else if (AV.remote && Date.now() < AV.remoteUntil) st = AV.remote;
  AV.avatar.setState(st);
  const dotTxt = { idle: WAKE.det ? `Say “${wakeLabel()}”` : "", listening: "Listening…", thinking: "Thinking…", talking: "" }[st];
  const s = $("#avState"); if (s) s.textContent = dotTxt || "";
}
function animUrl(a) { return "/avatar/bin/animations/" + encodeURIComponent(a.file) + "?key=" + encodeURIComponent(KEY); }
const neonOf = c => ({ on: !!c.neon, glow: c.neon_glow || "#3dff9e", haze: c.neon_haze || "#ff3fb4", crowd: c.neon_crowd !== false });
function lightOn() { return AV.cfg.light === "on" || (AV.cfg.light === "auto" && (AV.slow || AV.autoLight)); }
// Phones, tablets and light-mode screens load a copy of the character with smaller textures (the server makes it once)
function liteChar() { return lightOn() || matchMedia("(pointer: coarse)").matches || new URLSearchParams(location.search).get("app") === "1"; }
async function loadAvatarConfig() {
  try { AV.cfg = await api("/avatar/config"); } catch (e) { avatarStatus("Couldn't load avatar settings: " + e.message); return; }
  AV.rev = AV.cfg.rev;
  SPEAK.enabled = AV.cfg.speak;
  const st = AV.cfg.status || {}, vs = st.voice || "", libs = st.libs || "";
  avatarStatus(vs && vs !== "ready" ? "Voice: " + vs + (vs.startsWith("failed") ? "" : "…")
    : libs.startsWith("failed") ? "Avatar files: " + libs : libs === "downloading" ? "Downloading avatar files…" : "");
  if ((vs && vs !== "ready" && !vs.startsWith("failed")) || libs === "downloading") setTimeout(loadAvatarConfig, 4000);
  if (!AV.avatar) return;
  AV.avatar.setCamera(AV.cfg.camera); AV.avatar.setHeight(AV.cfg.height_cm); AV.avatar.setLight(lightOn());
  AV.avatar.setGestures(AV.cfg.gestures);
  AV.avatar.setNeon(neonOf(AV.cfg));
  AV.avatar.setAnimations((AV.cfg.animations || []).map(a => ({ ...a, url: animUrl(a) })));
  if (AV.lite === undefined) AV.lite = liteChar();   // decided once per page, so she isn't reloaded mid-session
  const lite = AV.lite;
  await AV.avatar.setStyle(AV.cfg.style, "/avatar/bin/characters/" + encodeURIComponent(AV.cfg.character || "") + "?key=" + encodeURIComponent(KEY) + (lite ? "&lite=1" : ""),
    (AV.cfg.character || "") + "@" + (AV.cfg.model_v || "") + (lite ? "@lite" : ""));
  if (AV.loadingMsg && $("#avStatus").textContent === AV.loadingMsg) avatarStatus("");
  AV.loadingMsg = "";
  AV.avatar.setAnimations((AV.cfg.animations || []).map(a => ({ ...a, url: animUrl(a) })));
  if (libs !== "downloading") startWake();
}

// ---- wake word on this screen (the phone app's avatar screen uses the app's own)
const WAKE = { det: null, sig: "", error: "" };
function chime() {
  try { const ctx = SPEAK.audio(), now = ctx.currentTime;
    for (const [f, d] of [[880, 0], [1320, 0.09]]) { const o = ctx.createOscillator(), g = ctx.createGain(); o.frequency.value = f;
      g.gain.setValueAtTime(0, now + d); g.gain.linearRampToValueAtTime(0.12, now + d + 0.02); g.gain.exponentialRampToValueAtTime(0.001, now + d + 0.25);
      o.connect(g); g.connect(ctx.destination); o.start(now + d); o.stop(now + d + 0.3); } } catch {}
}
function wakeLabel() { const w = (AV.cfg.wake_words || []).find(x => x.id === AV.cfg.wake_word); return w ? w.label : "Hey Jarvis"; }
async function startWake() {
  const want = AV.cfg.wake && !document.body.classList.contains("inapp") && window.isSecureContext && !!navigator.mediaDevices;
  const sig = want ? AV.cfg.wake_word + "|" + AV.cfg.wake_sensitivity : "";
  if (sig === WAKE.sig) return;
  if (WAKE.det) { WAKE.det.stop(); WAKE.det = null; }
  WAKE.sig = sig; WAKE.error = "";
  if (!want) return;
  const id = AV.cfg.wake_word || "hey_jarvis";
  const custom = id.startsWith("custom:");
  try {
    const mod = await import("/avatar/avatar.js");
    WAKE.det = await mod.createWakeWord({
      modelUrl: custom ? "/wakewords/" + encodeURIComponent(id.slice(7)) + ".onnx" : "/avatar/lib/oww/" + id + "_v0.1.onnx",
      headers: custom ? { Authorization: "Bearer " + KEY } : {},
      threshold: 1 - AV.cfg.wake_sensitivity,
      isBusy: () => !!MIC.rec, isSpeaking: () => SPEAK.speaking,
      onWake: () => { chime(); if (SPEAK.speaking) SPEAK.cancel(); if (!MIC.rec) micToggle(); },
      bargeIn: () => !!AV.cfg.barge_in, onBargeIn: () => { SPEAK.cancel(); if (S.busy) stop(); if (!MIC.rec) micToggle({ followup: true }); },
      onError: e => { WAKE.error = e.message; },
    });
  } catch (e) { WAKE.error = e.message; WAKE.sig = ""; avatarStatus("Wake word: " + e.message + " (use the microphone button or Space)"); }
}
// Which replies this screen says out loud (saved on this screen only): by default only conversations held
// on this screen, so a reply isn't spoken twice by the phone and the computer at the same time.
const SPEAK_FROM = [["this", "Only conversations on this screen"], ["phone", "This screen and the StellAI phone app"], ["all", "Every device (phone app, desktop chat, other avatar screens)"]];
const OWN_SOURCE = new URLSearchParams(location.search).get("src") || "";   // the phone app that shows this page
const CAPTION_AT = [["below", "In a small window below her"], ["side", "In a small window beside her"], ["over", "Over her, like film subtitles"], ["off", "Off"]];
function captionAt() { const v = store.get("avatarCaption", "below"); return CAPTION_AT.some(c => c[0] === v) ? v : "below"; }
function applyCaptionAt() { document.body.dataset.cap = captionAt(); }
function speakFrom() { return store.get("avatarSpeakFrom", OWN_SOURCE ? "this" : "this"); }
function speaksFor(ev) {
  if (OWN_SOURCE && ev.source === OWN_SOURCE) return true;          // the phone this screen runs in
  const f = speakFrom();
  return f === "all" || (f === "phone" && ev.device === "phone");
}
async function pollAvatarEvents() {
  try {
    const r = await api("/avatar/events?since=" + AV.since);
    const first = !AV.polled; AV.polled = true; AV.since = r.seq;
    // something changed in the shared bin or the settings (on any screen, or files dropped on the server): reload
    if (r.avatar_rev && AV.rev && r.avatar_rev !== AV.rev && AV.avatar && !document.querySelector(".overlay")) { AV.rev = r.avatar_rev; loadAvatarConfig(); }
    if (first) return;      // don't replay what happened before this screen opened
    for (const ev of r.events) {
      if (ev.source === CLIENT_ID) continue;
      if (ev.state === "reply") {
        AV.remote = null; avatarCaption(ev.text, "stella"); AV.avatar && AV.avatar.react(ev.text);
        // a screen that was asleep (phone app in the background) catches up quietly instead of reading out old replies
        const fresh = !document.hidden && (!r.now || !ev.ts || r.now - ev.ts < 25);
        if (AV.cfg.speak && ev.text && fresh && speaksFor(ev)) { SPEAK.cancel(); SPEAK.say(ev.text); }
      } else if (ev.state === "idle") { AV.remote = null; }
      else { AV.remote = ev.state; AV.remoteUntil = Date.now() + (ev.state === "thinking" ? 120000 : 15000);
        if (ev.state === "listening") avatarCaption("…", "you"); }
    }
  } catch {}
}
const cmToFtIn = cm => { const inches = Math.round(cm / 2.54); return `${Math.floor(inches / 12)}′${inches % 12}″`; };
const WHEN = [["idle", "While idle"], ["greeting", "When the screen opens"], ["quiet", "After a long quiet"], ["happy", "When she's happy"], ["kiss", "Love / goodnight"], ["dance", "When asked to dance"], ["off", "Off"]];
function openAvatarSettings() {
  const c = AV.cfg;
  const anims = c.animations || [];
  const d = dialog(`<h2>${icon("sparkle")}Avatar</h2>
    <label class="sub">Look</label>
    <div style="display:flex;gap:8px;margin:4px 0 10px"><button id="stOrb" class="${c.style === "orb" ? "primary" : ""}">Space orb</button>
      <button id="stVrm" class="${c.style === "vrm" ? "primary" : ""}">3D character</button></div>
    ${(c.characters || []).length ? `<label class="sub">Character</label><div style="display:flex;gap:8px;margin:4px 0 6px"><select id="chr">${c.characters.map(ch =>
      `<option value="${esc(ch.file)}" ${c.character === ch.file ? "selected" : ""}>${esc(ch.name)} (${Math.round(ch.size / 1e6)} MB)</option>`).join("")}</select></div>` : ""}
    <div class="sub">${c.has_model ? "" : "No character yet."} Use a female <b>.vrm</b> character: make one free in
      <a href="https://vroid.com/en/studio" target="_blank" rel="noopener">VRoid Studio</a>, or download one whose license allows it from
      <a href="https://hub.vroid.com/en" target="_blank" rel="noopener">VRoid Hub</a>.
      <div style="display:flex;gap:8px;margin-top:6px"><button id="upl">${c.has_model ? "Add another character…" : "Upload character…"}</button>${c.has_model ? `<button class="ghost" id="rm">Remove this one</button>` : ""}</div>
      <input type="file" id="vrmFile" accept=".vrm" hidden><div id="uplState"></div>
      <p class="sub" style="margin-top:8px">${icon("folder")}<b>Shared folder</b> on the server: <code>${esc(c.bin || "")}</code><br>Drop <code>.vrm</code> files in <code>characters</code> and
      <code>.vrma</code>/<code>.fbx</code> in <code>animations</code> (add <code>@greeting</code>, <code>@quiet</code> or <code>@happy</code> to a file name to choose when it plays). Every screen picks up new, changed and deleted files within seconds.</p></div>
    <label class="sub" style="display:block;margin-top:10px">Height: <b id="hv">${cmToFtIn(c.height_cm)}</b> <span id="hcm">(${(+c.height_cm).toFixed(0)} cm)</span></label>
    <input id="ht" type="range" min="140" max="200" step="0.5" value="${c.height_cm}">
    <label class="sub" style="display:block;margin-top:10px">Camera</label>
    <select id="cam">${[["face", "Face close-up"], ["upper", "Head and shoulders"], ["full", "Whole body"]].map(([v, l]) => `<option value="${v}" ${c.camera === v ? "selected" : ""}>${l}</option>`).join("")}</select>
    <h3 class="hud" style="margin-top:16px">Neon glow</h3>
    <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="neon" style="width:auto" ${c.neon ? "checked" : ""}> Neon look: glowing edges, coloured back lights and a dark stage</label>
    <div style="display:flex;gap:16px;align-items:center;margin:6px 0">
      <label class="sub" style="display:flex;gap:6px;align-items:center">Glow <input type="color" id="ng" value="${esc(neonOf(c).glow)}" style="width:52px;height:32px;padding:2px"></label>
      <label class="sub" style="display:flex;gap:6px;align-items:center">Haze <input type="color" id="nh" value="${esc(neonOf(c).haze)}" style="width:52px;height:32px;padding:2px"></label></div>
    <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="ncr" style="width:auto" ${neonOf(c).crowd ? "checked" : ""}> Dancing silhouettes behind her</label>
    <p class="sub">Glowing edges and the outline work best on toon-shaded characters (VRoid ones are). Changes show straight away; Save keeps them.</p>
    <h3 class="hud" style="margin-top:16px">Animations</h3>
    <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="ges" style="width:auto" ${c.gestures ? "checked" : ""}> Built-in gestures (wave, look around, head tilt, stretch…)</label>
    <div class="sub" style="margin:4px 0">Try: ${["Wave", "Look around", "Head tilt", "Weight shift", "Shoulder roll", "Stretch", "Bounce", "Blow a kiss", "Hip sway"].map(g => `<button class="ghost" data-try="${g}" style="padding:3px 8px;margin:2px">${g}</button>`).join("")}</div>
    <div class="list" id="animList">${anims.map(a => `<div class="row" data-id="${a.id}"><span class="t">${esc(a.name)} <span class="sub">.${a.type}</span></span>
      <select data-when="${a.id}" style="width:auto">${WHEN.map(([v, l]) => `<option value="${v}" ${a.when === v ? "selected" : ""}>${l}</option>`).join("")}</select>
      <button class="ghost" data-play="${a.id}" title="Play">${icon("play", "solo")}</button><button class="ghost" data-adel="${a.id}" title="Delete">${icon("close", "solo")}</button></div>`).join("")
      || `<p class="sub">No uploaded animations yet. Add <b>.vrma</b> files (VRoid Hub, BOOTH) or Mixamo <b>.fbx</b> files (download "Without Skin").</p>`}</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:6px"><button id="anUp">Add animation…</button>
      <select id="anWhen" style="width:auto">${WHEN.slice(0, 6).map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
      <input type="file" id="anFile" accept=".vrma,.fbx" multiple hidden></div><div id="anState" class="sub"></div>
    <h3 class="hud" style="margin-top:16px">Voice</h3>
    <select id="eng" style="margin-bottom:6px"><option value="piper" ${c.engine !== "kokoro" ? "selected" : ""}>Piper: fast, works everywhere</option>
      ${c.kokoro_available ? `<option value="kokoro" ${c.engine === "kokoro" ? "selected" : ""}>Kokoro: more natural (one-time download ~175 MB)</option>` : ""}</select>
    ${c.engine === "kokoro" && (c.status || {}).kokoro && c.status.kokoro !== "ready" ? `<p class="sub">Kokoro: ${esc(c.status.kokoro)}… (Piper speaks until it's ready)</p>` : ""}
    <div id="kokBox" style="display:${c.engine === "kokoro" ? "flex" : "none"};gap:8px;margin-bottom:6px"><select id="kv">${(c.kokoro_voices || []).map(v => `<option value="${v.id}" ${c.kokoro_voice === v.id ? "selected" : ""}>${esc(v.label)}</option>`).join("")}</select></div>
    <div id="piperBox" style="display:${c.engine === "kokoro" ? "none" : "flex"};gap:8px"><select id="vc">${(c.voices || []).map(v => `<option value="${v.id}" ${c.voice === v.id ? "selected" : ""}>${esc(v.label)}</option>`).join("")}</select></div>
    <button id="hear" style="margin-top:6px">${icon("play")}Hear this voice</button>
    <label class="sub" style="display:block;margin-top:10px">Speaking speed: <span id="spv">${(+c.speed).toFixed(2)}×</span></label>
    <input id="sp" type="range" min="0.7" max="1.4" step="0.05" value="${c.speed}">
    <label style="display:flex;gap:8px;align-items:center;margin-top:10px"><input type="checkbox" id="spk" style="width:auto" ${c.speak ? "checked" : ""}> Speak replies out loud</label>
    <label class="sub" style="display:block;margin-top:8px">Which replies this screen speaks (this screen only)</label>
    <select id="oth">${SPEAK_FROM.map(([v, l]) => `<option value="${v}" ${speakFrom() === v ? "selected" : ""}>${l}</option>`).join("")}</select>
    <label class="sub" style="display:block;margin-top:8px">Subtitles (this screen only)</label>
    <select id="capAt">${CAPTION_AT.map(([v, l]) => `<option value="${v}" ${captionAt() === v ? "selected" : ""}>${l}</option>`).join("")}</select>
    <h3 class="hud" style="margin-top:16px">Conversation</h3>
    <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="fu" style="width:auto" ${c.followup ? "checked" : ""}> Keep listening after she answers, so you can reply without the wake word ("thanks" or "that's all" ends it)</label>
    <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="bi" style="width:auto" ${c.barge_in ? "checked" : ""}> Interrupt her by talking (best with a speakerphone that cancels echo)</label>
    <h3 class="hud" style="margin-top:16px">Wake word</h3>
    <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="wk" style="width:auto" ${c.wake ? "checked" : ""}> Listen for a wake word on this screen</label>
    <div style="display:flex;gap:8px;margin-top:6px"><select id="ww">${(c.wake_words || []).map(w => `<option value="${esc(w.id)}" ${c.wake_word === w.id ? "selected" : ""}>${esc(w.label)}</option>`).join("")}</select></div>
    <label class="sub" style="display:block;margin-top:8px">Sensitivity: <span id="wsv">${Math.round(c.wake_sensitivity * 100)}%</span> (higher hears you more easily, but triggers by mistake more often)</label>
    <input id="ws" type="range" min="0.2" max="0.9" step="0.05" value="${c.wake_sensitivity}">
    <p class="sub">${WAKE.det ? "Listening for the wake word now." : WAKE.error ? "Wake word unavailable: " + esc(WAKE.error) : !window.isSecureContext ? "Needs a secure page for the microphone (see the microphone button)." : ""}</p>
    <h3 class="hud" style="margin-top:16px">Performance</h3>
    <select id="lt"><option value="auto" ${c.light === "auto" ? "selected" : ""}>Light mode: automatic${AV.slow || AV.autoLight ? " (on for this device)" : ""}</option>
      <option value="on" ${c.light === "on" ? "selected" : ""}>Light mode: on (Raspberry Pi, old phones)</option><option value="off" ${c.light === "off" ? "selected" : ""}>Light mode: off (best quality)</option></select>
    <p class="sub">Running at ${Math.round(AV.avatar ? AV.avatar.fps() : 0)} frames a second. Settings are saved on the server, so every avatar screen uses them.</p>
    <div class="btns"><button id="close">Close</button><button class="primary" id="save">Save</button></div>`);
  let style = c.style;
  const $d = q => d.querySelector(q);
  const neonNow = () => ({ on: $d("#neon").checked, glow: $d("#ng").value, haze: $d("#nh").value, crowd: $d("#ncr").checked });
  for (const id of ["#neon", "#ng", "#nh", "#ncr"]) $d(id).addEventListener("input", () => AV.avatar && AV.avatar.setNeon(neonNow()));
  const mark = () => { $d("#stOrb").className = style === "orb" ? "primary" : ""; $d("#stVrm").className = style === "vrm" ? "primary" : ""; };
  $d("#stOrb").onclick = () => { style = "orb"; mark(); };
  $d("#stVrm").onclick = () => { style = "vrm"; mark(); if (!c.has_model) $d("#vrmFile").click(); };
  $d("#sp").oninput = e => { $d("#spv").textContent = (+e.target.value).toFixed(2) + "×"; };
  $d("#ws").oninput = e => { $d("#wsv").textContent = Math.round(e.target.value * 100) + "%"; };
  $d("#ht").oninput = e => { $d("#hv").textContent = cmToFtIn(+e.target.value); $d("#hcm").textContent = `(${(+e.target.value).toFixed(0)} cm)`;
    if (AV.avatar) AV.avatar.setHeight(+e.target.value); };
  $d("#upl").onclick = () => $d("#vrmFile").click();
  $d("#vrmFile").onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    $d("#uplState").textContent = `Uploading ${Math.round(f.size / 1e6)} MB…`;
    try { AV.cfg = await api("/avatar/model?name=" + encodeURIComponent(f.name), { method: "PUT", body: f, headers: { "Content-Type": "model/gltf-binary" } });
      d.remove(); if (AV.avatar) await AV.avatar.setStyle("orb"); loadAvatarConfig(); }
    catch (err) { $d("#uplState").textContent = "Couldn't upload: " + err.message; }
  };
  if ($d("#rm")) $d("#rm").onclick = async () => { const ch = $d("#chr") ? $d("#chr").value : true;
    AV.cfg = await api("/avatar/config", { method: "POST", json: { remove_model: ch } }); d.remove(); loadAvatarConfig(); };
  d.querySelectorAll("[data-try]").forEach(b => b.onclick = () => { if (!AV.avatar || AV.avatar.style !== "vrm") return alertBox("Gestures are for the 3D character."); AV.avatar.play(b.dataset.try); });
  d.querySelectorAll("[data-play]").forEach(b => b.onclick = () => { if (AV.avatar && !AV.avatar.play(b.dataset.play)) alertBox("That animation couldn't be loaded on this character (or the 3D character isn't showing)."); });
  d.querySelectorAll("[data-when]").forEach(sel => sel.onchange = async () => {
    try { await api("/avatar/anims/" + sel.dataset.when, { method: "POST", json: { when: sel.value } }); } catch (e) { alertBox(e.message); } });
  d.querySelectorAll("[data-adel]").forEach(b => b.onclick = async () => {
    try { await api("/avatar/anims/" + b.dataset.adel, { method: "POST", json: { delete: true } }); b.closest(".row").remove(); } catch (e) { alertBox(e.message); } });
  $d("#anUp").onclick = () => $d("#anFile").click();
  $d("#anFile").onchange = async e => {
    for (const f of e.target.files) {
      const ext = (f.name.split(".").pop() || "").toLowerCase(), name = f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ");
      $d("#anState").textContent = `Uploading ${f.name}…`;
      try { await api(`/avatar/anims?type=${ext}&when=${$d("#anWhen").value}&name=${encodeURIComponent(name)}`, { method: "PUT", body: f, headers: { "Content-Type": "application/octet-stream" } }); }
      catch (err) { $d("#anState").textContent = `${f.name}: ${err.message}`; return; }
    }
    d.remove(); await loadAvatarConfig(); openAvatarSettings();
  };
  $d("#eng").onchange = e => { const k = e.target.value === "kokoro"; $d("#kokBox").style.display = k ? "flex" : "none"; $d("#piperBox").style.display = k ? "none" : "flex"; };
  $d("#hear").onclick = () => {
    const was = SPEAK.enabled; SPEAK.enabled = true; SPEAK.cancel();
    const prev = { voice: AV.cfg.voice, speed: AV.cfg.speed, engine: AV.cfg.engine, kokoro_voice: AV.cfg.kokoro_voice };
    Object.assign(AV.cfg, { voice: $d("#vc").value, speed: +$d("#sp").value, engine: $d("#eng").value, kokoro_voice: $d("#kv") ? $d("#kv").value : prev.kokoro_voice });
    SPEAK.say(`Hi, I'm ${NAME()}. It's 3:30 and the board is running at 5V. This is how I sound.`);
    Object.assign(AV.cfg, prev); SPEAK.enabled = was;
  };
  $d("#close").onclick = () => { d.remove(); if (AV.avatar) { AV.avatar.setHeight(AV.cfg.height_cm); AV.avatar.setNeon(neonOf(AV.cfg)); } };
  $d("#save").onclick = async () => {
    try {
      AV.cfg = await api("/avatar/config", { method: "POST", json: { style, character: $d("#chr") ? $d("#chr").value : undefined, camera: $d("#cam").value, voice: $d("#vc").value,
        speed: +$d("#sp").value, speak: $d("#spk").checked, height_cm: +$d("#ht").value,
        gestures: $d("#ges").checked, wake: $d("#wk").checked, engine: $d("#eng").value, kokoro_voice: $d("#kv") ? $d("#kv").value : undefined,
        followup: $d("#fu").checked, barge_in: $d("#bi").checked, wake_word: $d("#ww").value, wake_sensitivity: +$d("#ws").value, light: $d("#lt").value,
        neon: $d("#neon").checked, neon_glow: $d("#ng").value, neon_haze: $d("#nh").value, neon_crowd: $d("#ncr").checked } });
      store.set("avatarSpeakFrom", $d("#oth").value);
      store.set("avatarCaption", $d("#capAt").value); applyCaptionAt();
      d.remove(); loadAvatarConfig();
    } catch (e) { alertBox(e.message); }
  };
}
async function startAvatarScreen() {
  document.body.classList.add("avatar");
  $("#input").placeholder = "Message " + NAME() + "…  (or press Space to talk)";
  if (new URLSearchParams(location.search).get("app") === "1") document.body.classList.add("inapp");
  applyCaptionAt();
  await pollAvatarEvents();      // remember where the event list is now, so nothing that happens while loading is missed
  const mod = await import("/avatar/avatar.js");
  AV.slow = mod.looksLikeSlowDevice();
  try { AV.cfg = await api("/avatar/config"); } catch {}
  AV.avatar = mod.createAvatar($("#avatarHost"), { camera: AV.cfg.camera, height: AV.cfg.height_cm, light: lightOn(), gestures: AV.cfg.gestures,
    neon: neonOf(AV.cfg), stageHost: $("#stage"),
    onError: e => avatarStatus("3D character: " + e.message + " (showing the orb)"), onInfo: m => avatarStatus(m),
    onProgress: (got, total) => { AV.loadingMsg = total ? `Loading ${NAME()}'s 3D character… ${Math.round(got / 1e6)} of ${Math.round(total / 1e6)} MB`
      : `Loading ${NAME()}'s 3D character… ${Math.round(got / 1e6)} MB`; avatarStatus(AV.loadingMsg); } });
  await AV.avatar.setStyle("orb");
  // The page is running: tell the phone app now. The 3D character can take a while longer on mobile data;
  // the orb shows (with download progress) until she's ready.
  try { window.StellaiApp && window.StellaiApp.ready(); } catch {}
  loadAvatarConfig().catch(e => avatarStatus("Avatar: " + e.message));
  // Automatic light mode: switch on if this device draws too slowly
  setTimeout(() => { if (AV.avatar && AV.cfg.light === "auto" && !AV.slow && AV.avatar.fps() < 32) { AV.autoLight = true; AV.avatar.setLight(true); } }, 8000);
  // Browsers only allow sound after a tap: ask once if needed
  const ctx = SPEAK.audio();
  if (ctx.state === "suspended") {
    $("#tapStart").style.display = "flex";
    const go = async () => { try { await ctx.resume(); } catch {} $("#tapStart").style.display = "none"; };
    $("#tapStart").onclick = go; document.addEventListener("keydown", go, { once: true });
  }
  setInterval(pollAvatarEvents, 1000); pollAvatarEvents();
  (function loop() {
    if (AV.avatar) { AV.avatar.setLevel(MIC.rec ? AV.micLevel * 0.6 : SPEAK.level()); if (!MIC.rec) { const v = SPEAK.visemes(); if (v) AV.avatar.setVisemes(v); } updateAvatarState(); }
    requestAnimationFrame(loop);
  })();
  $("#avChat").onclick = () => document.body.classList.toggle("showchat");
  $("#avNew").onclick = () => { newChat(); avatarCaption(""); };
  $("#avSet").onclick = openAvatarSettings;
  $("#avFull").onclick = () => { document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => {}); };
  // Space bar: push to talk (when not typing)
  document.addEventListener("keydown", e => { if (e.code === "Space" && document.activeElement !== $("#input") && !e.repeat && !document.querySelector(".overlay")) { e.preventDefault(); micToggle(); } });
}

$("#mic").onclick = micToggle;

async function start() {
  if (!KEY) { askKey(); return; }
  S.current = store.get("current", "") || null;
  renderBar(); renderMsgs();
  // The avatar starts straight away: the chat history below can take a while on a new device or a slow connection
  if (AVATAR_MODE && !AV.avatar) {
    $("#avName").textContent = NAME().toUpperCase();
    startAvatarScreen().then(() => { try { window.StellaiApp && window.StellaiApp.ready(); } catch {} movedHello(); })
      .catch(e => { avatarStatus("Avatar: " + e.message); try { window.StellaiApp && window.StellaiApp.error(String(e.message || e)); } catch {} });
  }
  if (!AVATAR_MODE) movedHello();
  try { await sync(); } catch (e) { console.warn("sync", e); }
  if (S.current && !S.convs.has(S.current)) S.current = null;
  try { await loadMeta(); } catch (e) { console.warn("meta", e); }
  renderSide(); renderMsgs(); updateMeter(); if (!AVATAR_MODE) refreshDocs();
  if (AVATAR_MODE) $("#avName").textContent = NAME().toUpperCase();
}
setInterval(() => { if (KEY && !S.busy) sync(); if (KEY && !S.busy && document.body.classList.contains("docs") && !D.editing) refreshDocs(); }, 20000);
setInterval(() => { if (KEY) loadMeta(); }, 300000);
start();
</script>
</body>
</html>
"""  # end of web chat


# ----------------------------------------------------------------------------- avatar screen and voice
# A full-screen page (/avatar) with a 3D VRM character or an animated orb that listens, thinks and talks.
# Stella's voice is Piper (local neural text-to-speech). Everything is downloaded on first use.
import zipfile

AVATAR = {"dir": "", "lock": threading.Lock(), "status": {}}
THREE_VERSION = "0.180.0"
AVATAR_LIBS = {   # path under the lib folder -> URLs to try
    "build/three.module.js": [f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/build/three.module.js",
                              f"https://unpkg.com/three@{THREE_VERSION}/build/three.module.js"],
    "build/three.core.js": [f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/build/three.core.js",
                            f"https://unpkg.com/three@{THREE_VERSION}/build/three.core.js"],
    "examples/jsm/loaders/GLTFLoader.js": [
        f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/examples/jsm/loaders/GLTFLoader.js",
        f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/loaders/GLTFLoader.js"],
    "examples/jsm/utils/BufferGeometryUtils.js": [
        f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/examples/jsm/utils/BufferGeometryUtils.js",
        f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/utils/BufferGeometryUtils.js"],
    "three-vrm.module.min.js": ["https://cdn.jsdelivr.net/npm/@pixiv/three-vrm@3/lib/three-vrm.module.min.js",
                                "https://unpkg.com/@pixiv/three-vrm@3/lib/three-vrm.module.min.js"],
    # animations: .vrma (VRM Animation) and .fbx (Mixamo)
    "three-vrm-animation.module.min.js": [
        "https://cdn.jsdelivr.net/npm/@pixiv/three-vrm-animation@3/lib/three-vrm-animation.module.min.js",
        "https://unpkg.com/@pixiv/three-vrm-animation@3/lib/three-vrm-animation.module.min.js"],
}
for _rel in ("loaders/FBXLoader.js", "libs/fflate.module.js", "curves/NURBSCurve.js", "curves/NURBSUtils.js"):
    AVATAR_LIBS["examples/jsm/" + _rel] = [f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/examples/jsm/{_rel}",
                                           f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/{_rel}"]
# Wake word in the browser: ONNX Runtime Web + the same openWakeWord models as the phone app
ORT_VERSION = "1.20.1"
for _f in ("ort.wasm.min.js", "ort-wasm-simd-threaded.wasm", "ort-wasm-simd-threaded.mjs"):
    AVATAR_LIBS["ort/" + _f] = [f"https://cdn.jsdelivr.net/npm/onnxruntime-web@{ORT_VERSION}/dist/{_f}",
                                f"https://unpkg.com/onnxruntime-web@{ORT_VERSION}/dist/{_f}"]
OWW_MODELS = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx", "alexa_v0.1.onnx",
              "hey_mycroft_v0.1.onnx")
for _f in OWW_MODELS:
    AVATAR_LIBS["oww/" + _f] = [f"https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/{_f}"]
# Silero VAD: tells speech from silence/noise, so Stella knows when you've finished talking
AVATAR_LIBS["vad/silero_vad.onnx"] = [
    "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx",
    "https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx"]
LIB_TYPES = {".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
             ".wasm": "application/wasm", ".onnx": "application/octet-stream"}
PIPER_RELEASE = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
VOICES = {   # Piper voices (huggingface.co/rhasspy/piper-voices), ~60 MB each
    "en_US-lessac-medium": "Lessac: clear, warm (US)",
    "en_US-amy-medium": "Amy: bright, friendly (US)",
    "en_US-hfc_female-medium": "HFC: soft, calm (US)",
    "en_US-kristin-medium": "Kristin: expressive (US)",
    "en_GB-alba-medium": "Alba: gentle Scottish accent (UK)",
    "en_GB-jenny_dioco-medium": "Jenny: natural British (UK)",
}
DEFAULT_VOICE = "en_US-lessac-medium"


def _voice_url(name, ext):
    lang, rest = name.split("-", 1)
    speaker, quality = rest.rsplit("-", 1)
    return (f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{lang.split('_')[0]}/{lang}/{speaker}/"
            f"{quality}/{name}.onnx{ext}")


def _avatar_path(*parts):
    return os.path.join(AVATAR["dir"], *parts)


def avatar_config():
    cfg = {"style": "orb", "voice": DEFAULT_VOICE, "speak": True, "speak_others": True, "speed": 1.0,
           "name_tag": True, "camera": "upper", "height_cm": 172.7, "light": "auto", "wake": True,
           "wake_word": "hey_jarvis", "wake_sensitivity": 0.5, "gestures": True, "character": "",
           "engine": "piper", "kokoro_voice": "af_heart", "followup": True, "barge_in": False,
           "neon": False, "neon_glow": "#3dff9e", "neon_haze": "#ff3fb4", "neon_crowd": True}
    try:
        with open(_avatar_path("config.json")) as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in cfg})
    except (OSError, ValueError):
        pass
    if cfg["voice"] not in VOICES:
        cfg["voice"] = DEFAULT_VOICE
    chars = list_characters()
    if cfg.get("character") not in [c["file"] for c in chars]:
        cfg["character"] = chars[0]["file"] if chars else ""     # newest one
    cfg["characters"] = chars
    cfg["has_model"] = bool(cfg["character"])
    cfg["model_v"] = next((c["v"] for c in chars if c["file"] == cfg["character"]), 0)
    cfg["rev"] = avatar_rev()
    cfg["bin"] = _avatar_path("bin")
    if cfg["style"] == "vrm" and not cfg["has_model"]:
        cfg["style"] = "orb"
    cfg["voices"] = [{"id": k, "label": v} for k, v in VOICES.items()]
    cfg["animations"] = list_animations()
    cfg["kokoro_voices"] = [{"id": k, "label": v} for k, v in KOKORO_VOICES.items()]
    cfg["kokoro_available"] = KOKORO.available()
    cfg["wake_words"] = [{"id": "hey_jarvis", "label": "Hey Jarvis"}, {"id": "alexa", "label": "Alexa"},
                         {"id": "hey_mycroft", "label": "Hey Mycroft"}] + [
        {"id": "custom:" + n, "label": n.replace("_", " ").title()} for n in custom_wakewords()]
    cfg["status"] = dict(AVATAR["status"])
    return cfg


def save_avatar_config(new):
    cur = {k: v for k, v in avatar_config().items()
           if k not in ("has_model", "model_v", "voices", "status", "animations", "wake_words", "characters", "rev", "bin",
                        "kokoro_voices", "kokoro_available")}
    if new.get("style") in ("orb", "vrm"):
        cur["style"] = new["style"]
    if new.get("voice") in VOICES:
        cur["voice"] = new["voice"]
    if "height_cm" in new:
        cur["height_cm"] = round(max(140.0, min(200.0, float(new["height_cm"]))), 1)
    if new.get("light") in ("auto", "on", "off"):
        cur["light"] = new["light"]
    if "wake_sensitivity" in new:
        cur["wake_sensitivity"] = round(max(0.2, min(0.9, float(new["wake_sensitivity"]))), 2)
    if isinstance(new.get("wake_word"), str) and re.fullmatch(r"(custom:)?[A-Za-z0-9_.-]{1,64}", new["wake_word"]):
        cur["wake_word"] = new["wake_word"]
    if new.get("engine") in ("piper", "kokoro"):
        cur["engine"] = new["engine"]
    if new.get("kokoro_voice") in KOKORO_VOICES:
        cur["kokoro_voice"] = new["kokoro_voice"]
    for k in ("neon_glow", "neon_haze"):
        if isinstance(new.get(k), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", new[k]):
            cur[k] = new[k].lower()
    for k in ("speak", "speak_others", "name_tag", "wake", "gestures", "followup", "barge_in", "neon", "neon_crowd"):
        if k in new:
            cur[k] = bool(new[k])
    if "speed" in new:
        cur["speed"] = max(0.6, min(1.6, float(new["speed"])))
    if new.get("camera") in ("face", "upper", "full"):
        cur["camera"] = new["camera"]
    if isinstance(new.get("character"), str) and new["character"] in [c["file"] for c in list_characters()]:
        cur["character"] = new["character"]
    if new.get("remove_model"):
        name = new.get("remove_model") if isinstance(new.get("remove_model"), str) else cur.get("character")
        if name and _safe_bin_name(name):
            try:
                os.remove(_avatar_path("bin", "characters", name))
            except OSError:
                pass
        if not list_characters():
            cur["style"] = "orb"
    os.makedirs(AVATAR["dir"], exist_ok=True)
    with open(_avatar_path("config.json.part"), "w") as f:
        json.dump(cur, f, indent=1)
    os.replace(_avatar_path("config.json.part"), _avatar_path("config.json"))
    if cur["voice"] != TTS.voice:
        threading.Thread(target=ensure_voice, args=(cur["voice"],), daemon=True).start()
    if cur.get("engine") == "kokoro":
        KOKORO.prepare()
    return avatar_config()


def _fetch_first(urls, dest):
    err = None
    for u in urls:
        try:
            _download_file(u, dest, {})
            return
        except (urllib.error.URLError, OSError) as e:
            err = e
            try:
                os.remove(dest + ".part")
            except OSError:
                pass
    raise OSError(f"couldn't download {os.path.basename(dest)}: {err}")


def ensure_avatar_libs():
    """three.js and three-vrm for the 3D character (~1.6 MB, once)."""
    missing = [p for p in AVATAR_LIBS if not os.path.isfile(_avatar_path("lib", p))]
    if not missing:
        AVATAR["status"]["libs"] = "ready"
        return True
    AVATAR["status"]["libs"] = "downloading"
    try:
        for p in missing:
            dest = _avatar_path("lib", p)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _fetch_first(AVATAR_LIBS[p], dest)
        AVATAR["status"]["libs"] = "ready"
        return True
    except OSError as e:
        AVATAR["status"]["libs"] = f"failed: {e}"
        log(f"avatar: {e}")
        return False


def piper_exe():
    d = _avatar_path("piper")
    for name in ("piper.exe", "piper"):
        if os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    return ""


def ensure_piper():
    """The Piper speech engine (~25 MB, once)."""
    if piper_exe():
        return True
    import platform
    import tarfile
    machine = platform.machine().lower()
    if os.name == "nt":
        asset = "piper_windows_amd64.zip"
    elif machine in ("x86_64", "amd64"):
        asset = "piper_linux_x86_64.tar.gz"
    elif machine in ("aarch64", "arm64"):
        asset = "piper_linux_aarch64.tar.gz"
    elif machine.startswith("armv7"):
        asset = "piper_linux_armv7l.tar.gz"
    else:
        AVATAR["status"]["voice"] = f"failed: no Piper build for {machine}"
        return False
    AVATAR["status"]["voice"] = "downloading the speech engine"
    tmp = _avatar_path(asset)
    try:
        os.makedirs(AVATAR["dir"], exist_ok=True)
        _download_file(PIPER_RELEASE + asset, tmp, {})
        target = os.path.realpath(AVATAR["dir"])
        if asset.endswith(".zip"):
            with zipfile.ZipFile(tmp) as z:
                for n in z.namelist():
                    if not os.path.realpath(os.path.join(target, n)).startswith(target + os.sep):
                        raise OSError("unsafe file in the Piper download")
                z.extractall(target)
        else:
            with tarfile.open(tmp) as t:
                for m in t.getmembers():
                    if not os.path.realpath(os.path.join(target, m.name)).startswith(target + os.sep) \
                            or m.issym() or m.islnk() and not os.path.realpath(
                                os.path.join(target, m.linkname)).startswith(target + os.sep):
                        raise OSError("unsafe file in the Piper download")
                t.extractall(target)
        exe = piper_exe()
        if exe and os.name != "nt":
            os.chmod(exe, 0o755)
        return bool(exe)
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
        AVATAR["status"]["voice"] = f"failed: {e}"
        log(f"avatar voice: {e}")
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def ensure_voice(name):
    if not ensure_piper():
        return False
    model = _avatar_path("voices", name + ".onnx")
    if os.path.isfile(model) and os.path.isfile(model + ".json"):
        AVATAR["status"]["voice"] = "ready"
        return True
    AVATAR["status"]["voice"] = f"downloading the voice {VOICES.get(name, name).split(':')[0]}"
    try:
        os.makedirs(os.path.dirname(model), exist_ok=True)
        _download_file(_voice_url(name, ".json"), model + ".json", {})
        _download_file(_voice_url(name, ""), model, {})
        AVATAR["status"]["voice"] = "ready"
        return True
    except (urllib.error.URLError, OSError) as e:
        AVATAR["status"]["voice"] = f"failed: {e}"
        log(f"avatar voice: {e}")
        return False


def prepare_avatar():
    """Downloads what the avatar screen needs, once, in the background (called when the page first opens)."""
    if AVATAR.get("preparing"):
        return
    AVATAR["preparing"] = True

    def work():
        try:
            ensure_avatar_libs()
            cfg = avatar_config()
            if ensure_voice(cfg["voice"]):
                try:
                    TTS.say("Hello.", cfg["voice"], cfg["speed"])     # keep the voice loaded: the first reply starts sooner
                except (RuntimeError, OSError):
                    pass
            if cfg.get("engine") == "kokoro":
                KOKORO.prepare()
        finally:
            AVATAR["preparing"] = False
    threading.Thread(target=work, daemon=True).start()


SPEAKABLE_DROP = [
    (re.compile(r"```[\s\S]*?(```|$)"), " (the code is on screen) "),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"https?://[^\s,)]+"), " a link "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"[*_#>|~]+"), " "),
    (re.compile(r"^\s*[-+]\s+", re.M), ""),
    (re.compile(r"\s+"), " "),
]


# ---- Speech-friendly text: say "five volts", not "five V"
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
        "seventeen eighteen nineteen".split()
_TENS = "  twenty thirty forty fifty sixty seventy eighty ninety".split(" ")


def number_words(n):
    """Whole numbers to words (up to the trillions), e.g. 1250 -> 'one thousand two hundred fifty'."""
    n = int(n)
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + number_words(n % 100))
    for value, name in ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")):
        if n >= value:
            return number_words(n // value) + " " + name + ("" if n % value == 0 else " " + number_words(n % value))
    return str(n)


def say_number(s):
    """'3.3' -> 'three point three', '1,200' -> 'one thousand two hundred', '0.5' -> 'zero point five'."""
    s = s.replace(",", "")
    neg = s.startswith("-")
    s = s.lstrip("-+")
    if "." in s:
        whole, frac = s.split(".", 1)
        out = number_words(int(whole or 0)) + " point " + " ".join(_ONES[int(d)] for d in frac if d.isdigit())
    else:
        out = number_words(int(s)) if len(s) <= 15 else s
    return ("minus " if neg else "") + out


# unit: (singular, plural); matched right after a number, e.g. 5V, 3.3 V, 220µF, 12 mAh, 25°C
_UNITS = {
    "V": ("volt", "volts"), "mV": ("millivolt", "millivolts"), "kV": ("kilovolt", "kilovolts"),
    "A": ("amp", "amps"), "mA": ("milliamp", "milliamps"), "µA": ("microamp", "microamps"), "uA": ("microamp", "microamps"),
    "W": ("watt", "watts"), "mW": ("milliwatt", "milliwatts"), "kW": ("kilowatt", "kilowatts"),
    "kWh": ("kilowatt hour", "kilowatt hours"), "Wh": ("watt hour", "watt hours"), "mAh": ("milliamp hour", "milliamp hours"),
    "Ah": ("amp hour", "amp hours"),
    "Ω": ("ohm", "ohms"), "ohm": ("ohm", "ohms"), "kΩ": ("kilohm", "kilohms"), "MΩ": ("megohm", "megohms"),
    "k": ("k", "k"),
    "F": ("farad", "farads"), "µF": ("microfarad", "microfarads"), "uF": ("microfarad", "microfarads"),
    "nF": ("nanofarad", "nanofarads"), "pF": ("picofarad", "picofarads"),
    "H": ("henry", "henries"), "mH": ("millihenry", "millihenries"), "µH": ("microhenry", "microhenries"),
    "uH": ("microhenry", "microhenries"),
    "Hz": ("hertz", "hertz"), "kHz": ("kilohertz", "kilohertz"), "MHz": ("megahertz", "megahertz"),
    "GHz": ("gigahertz", "gigahertz"),
    "°C": ("degree Celsius", "degrees Celsius"), "°F": ("degree Fahrenheit", "degrees Fahrenheit"),
    "°": ("degree", "degrees"), "%": ("percent", "percent"),
    "mm": ("millimetre", "millimetres"), "cm": ("centimetre", "centimetres"), "m": ("metre", "metres"),
    "km": ("kilometre", "kilometres"), "km/h": ("kilometre per hour", "kilometres per hour"),
    "mph": ("mile per hour", "miles per hour"), "kg": ("kilogram", "kilograms"), "g": ("gram", "grams"),
    "mg": ("milligram", "milligrams"), "lb": ("pound", "pounds"), "lbs": ("pounds", "pounds"),
    "oz": ("ounce", "ounces"), "ft": ("foot", "feet"), "in": ("inch", "inches"), "mi": ("mile", "miles"),
    "ml": ("millilitre", "millilitres"), "mL": ("millilitre", "millilitres"), "L": ("litre", "litres"),
    "ms": ("millisecond", "milliseconds"), "s": ("second", "seconds"), "min": ("minute", "minutes"),
    "h": ("hour", "hours"), "hr": ("hour", "hours"), "hrs": ("hours", "hours"),
    "GB": ("gigabyte", "gigabytes"), "MB": ("megabyte", "megabytes"), "KB": ("kilobyte", "kilobytes"),
    "kB": ("kilobyte", "kilobytes"), "TB": ("terabyte", "terabytes"), "Mbps": ("megabit per second", "megabits per second"),
    "fps": ("frame per second", "frames per second"), "rpm": ("RPM", "RPM"), "AWG": ("gauge", "gauge"),
}
# units that are also ordinary words or letters only count when they're stuck to the number ("5m", "10s", "3in")
_ATTACHED_ONLY = {"m", "s", "in", "h", "g", "A", "F", "H", "L", "k", "mi", "min"}
_UNIT_RX = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)(\s?)(" + "|".join(
    sorted((re.escape(u) for u in _UNITS), key=len, reverse=True)) + r")(?![\w°])")
_MONEY_RX = re.compile(r"([$€£])\s?(\d[\d,]*)(?:\.(\d\d))?(\s?(?:k|K|million|billion)\b)?(?!\d|\.\d)")
_TIME_RX = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?:\s?([AaPp])\.?\s?[Mm]\.?)?(?!\d)")
_RANGE_RX = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?[-–]\s?(\d+(?:\.\d+)?)(?![\w.])")
_ABBREV = [(re.compile(p, re.I), r) for p, r in [
    (r"\be\.g\.", "for example"), (r"\bi\.e\.", "that is"), (r"\betc\.", "et cetera"), (r"\bvs\.?(?=\s)", "versus"),
    (r"\bapprox\.", "approximately"), (r"\bw/o\b", "without"), (r"\bw/(?=\s)", "with"),
]]
_SYMBOLS = [(re.compile(p), r) for p, r in [
    (r"(?<=\d)\s?[x×]\s?(?=\d)", " by "), (r"\s&\s", " and "), (r"(?<=\s)\+(?=\s)", " plus "),
    (r"(?<=\s)=(?=\s)", " equals "), (r"~(?=\s?\d)", "about "), (r"≈", " about "), (r"±", " plus or minus "),
    (r"(?<=\s)/(?=\s)", " or "), (r"->|→", " to "), (r"\s[-–—]\s", ", "), (r"…", "..."),
]]
_EMOJI_RX = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]+")


def _unit_say(m):
    num, gap, unit = m.group(1), m.group(2), m.group(3)
    if gap and unit in _ATTACHED_ONLY:
        return m.group(0)
    one, many = _UNITS[unit]
    if unit == "k":                       # 10k (resistor) -> ten k
        return say_number(num) + " k"
    plural = num.replace(",", "") not in ("1", "1.0", "-1")
    return say_number(num) + " " + (many if plural else one)


def _money_say(m):
    sym, whole, cents, scale = m.groups()
    name = {"$": "dollars", "€": "euros", "£": "pounds"}[sym]
    out = say_number(whole)
    if scale:
        sc = scale.strip().lower()
        out += " thousand" if sc == "k" else " " + sc
    elif whole.replace(",", "") == "1":
        name = name[:-1]
    out += " " + name
    if cents and cents != "00":
        out += " and " + number_words(int(cents)) + " cents"
    return out


def _time_say(m):
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    words = number_words(h) + ("" if mi == 0 and ap else " o'clock" if mi == 0 else
                               " oh " + number_words(mi) if mi < 10 else " " + number_words(mi))
    return words + (" " + ("AM" if ap.lower() == "a" else "PM") if ap else "")


def speech_text(text):
    """Turn written text into what a person would say: units, money, times, ranges, abbreviations, symbols."""
    for rx, rep in _ABBREV:
        text = rx.sub(rep, text)
    text = _MONEY_RX.sub(_money_say, text)
    text = _TIME_RX.sub(_time_say, text)
    text = _RANGE_RX.sub(lambda m: f"{m.group(1)} to {m.group(2)}", text)
    for rx, rep in _SYMBOLS:
        text = rx.sub(rep, text)
    text = _UNIT_RX.sub(_unit_say, text)
    text = _EMOJI_RX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def speakable(text):
    """What to say out loud for a reply: no code blocks, markdown symbols or long links; units and
    symbols as words."""
    for rx, rep in SPEAKABLE_DROP:
        text = rx.sub(rep, text)
    return speech_text(text)[:1200]


class PiperTTS:
    """A Piper process kept running (the voice stays loaded): one JSON line in, one WAV file out."""

    def __init__(self):
        self.proc, self.voice, self.speed, self.lock, self.lines = None, "", 1.0, threading.Lock(), None

    def _start(self, voice, speed):
        import queue
        self.stop()
        exe = piper_exe()
        model = _avatar_path("voices", voice + ".onnx")
        if not exe or not os.path.isfile(model):
            raise RuntimeError("the voice isn't downloaded yet")
        env = dict(os.environ)
        if os.name != "nt":
            env["LD_LIBRARY_PATH"] = os.path.dirname(exe) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        self.proc = subprocess.Popen(
            [exe, "--model", model, "--json-input", "--output_dir", _avatar_path("tts"),
             "--length_scale", str(round(1.0 / max(0.5, speed), 3)), "--sentence_silence", "0.15", "--quiet"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=os.path.dirname(exe),
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.voice, self.speed = voice, speed
        lines = self.lines = queue.Queue()
        out = self.proc.stdout

        def reader():   # Piper prints each finished file's name; read it without blocking the caller forever
            for raw in iter(out.readline, b""):
                lines.put(raw.decode("utf-8", "replace").strip())
            lines.put(None)
        threading.Thread(target=reader, daemon=True).start()

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def say(self, text, voice, speed=1.0):
        import queue
        text = speakable(text)
        if not text:
            return b""
        out_dir = _avatar_path("tts")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, uuid.uuid4().hex + ".wav")
        with self.lock:
            for attempt in range(2):
                if not self.proc or self.proc.poll() is not None or self.voice != voice or self.speed != speed:
                    self._start(voice, speed)
                try:
                    self.proc.stdin.write((json.dumps({"text": text, "output_file": out}) + "\n").encode("utf-8"))
                    self.proc.stdin.flush()
                    deadline, got = time.time() + 60, False
                    while time.time() < deadline:
                        line = self.lines.get(timeout=max(0.1, deadline - time.time()))
                        if line is None:
                            break
                        if os.path.basename(out) in line:
                            got = True
                            break
                    if got and os.path.isfile(out):
                        break
                except (OSError, ValueError, queue.Empty):
                    pass
                self.stop()
            else:
                raise RuntimeError("the speech engine stopped")
        try:
            with open(out, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(out)
            except OSError:
                pass


TTS = PiperTTS()

# ---- Kokoro: a more natural-sounding voice (82M-parameter model, runs on the CPU; optional)
KOKORO_VOICES = {
    "af_heart": "Heart: warm, natural (US)", "af_bella": "Bella: bright, expressive (US)",
    "af_nicole": "Nicole: soft, close (US)", "af_sarah": "Sarah: clear, friendly (US)", "af_sky": "Sky: light, young (US)",
    "bf_emma": "Emma: calm (UK)", "bf_isabella": "Isabella: polished (UK)",
}
KOKORO_FILES = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


KOKORO_WORKER = r'''# StellAI Kokoro worker: keeps the voice model loaded. One JSON line in, one WAV file out (its path printed).
import json, sys, wave
import numpy as np
from kokoro_onnx import Kokoro

model = Kokoro(sys.argv[1], sys.argv[2])
print("ready", flush=True)
for line in sys.stdin:
    try:
        req = json.loads(line)
        samples, rate = model.create(req["text"], voice=req["voice"], speed=float(req.get("speed", 1.0)),
                                     lang=req.get("lang", "en-us"))
        with wave.open(req["out"], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(rate))
            w.writeframes((np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes())
        print(req["out"], flush=True)
    except Exception as e:  # noqa: BLE001 - report and keep serving
        print("error " + str(e).replace("\n", " "), flush=True)
'''
# Windows: the StellAI app is a packaged program without its own Python, so Kokoro gets a small private copy of
# the official Python (the "embeddable" build from python.org) next to the voice files.
WIN_PYTHON_URL = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


class KokoroTTS:
    """Kokoro runs in its own process (a worker), with the Python that has kokoro-onnx installed:
    the hub's own on Linux, a private embedded Python on Windows."""

    def __init__(self):
        self.proc, self.lines, self.lock, self.preparing = None, None, threading.Lock(), False

    def _frozen(self):
        return bool(getattr(sys, "frozen", False))

    def available(self):
        import platform
        if self._frozen():
            return os.name == "nt" and platform.machine().lower() in ("amd64", "x86_64")
        return True

    def python(self):
        if self._frozen():
            return _avatar_path("kokoro", "python", "python.exe")
        return sys.executable

    def _importable(self):
        py = self.python()
        if not os.path.isfile(py):
            return False
        try:
            return subprocess.run([py, "-c", "import kokoro_onnx, numpy"], capture_output=True, timeout=120,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def ready(self):
        if self.proc and self.proc.poll() is None:
            return True
        return AVATAR["status"].get("kokoro") == "ready" or (
            all(os.path.isfile(_avatar_path("kokoro", f)) for f in KOKORO_FILES) and os.path.isfile(self.python())
            and os.path.isfile(_avatar_path("kokoro", ".installed")))

    def _setup_windows_python(self):
        d = _avatar_path("kokoro", "python")
        if os.path.isfile(os.path.join(d, "python.exe")):
            return
        AVATAR["status"]["kokoro"] = "downloading Python for the voice engine"
        os.makedirs(d, exist_ok=True)
        z = _avatar_path("kokoro", "python-embed.zip")
        _download_file(WIN_PYTHON_URL, z, {})
        target = os.path.realpath(d)
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if not os.path.realpath(os.path.join(target, n)).startswith(target + os.sep):
                    raise OSError("unsafe file in the Python download")
            zf.extractall(target)
        os.remove(z)
        # The embeddable Python ignores installed packages until "import site" is switched on
        for f in os.listdir(d):
            if f.endswith("._pth"):
                p = os.path.join(d, f)
                with open(p) as fh:
                    txt = fh.read()
                txt = txt.replace("#import site", "import site")
                if "Lib\\site-packages" not in txt:
                    txt += "\nLib\\site-packages\n"
                with open(p, "w") as fh:
                    fh.write(txt)
        AVATAR["status"]["kokoro"] = "installing pip"
        gp = os.path.join(d, "get-pip.py")
        _download_file(GET_PIP_URL, gp, {})
        r = subprocess.run([os.path.join(d, "python.exe"), gp, "--no-warn-script-location", "--quiet"],
                           capture_output=True, text=True, timeout=900, cwd=d,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise OSError("installing pip failed: " + (r.stderr or r.stdout).strip()[-300:])

    def prepare(self):
        """Sets up Kokoro once, in the background: (Windows: a private Python), the kokoro-onnx package
        (~60 MB with onnxruntime) and the model (~115 MB)."""
        if self.preparing or not self.available() or (self.ready() and self._importable()):
            if self.ready() and not self.preparing:
                AVATAR["status"]["kokoro"] = "ready"
            return
        self.preparing = True

        def work():
            try:
                os.makedirs(_avatar_path("kokoro"), exist_ok=True)
                if self._frozen():
                    self._setup_windows_python()
                if not self._importable():
                    AVATAR["status"]["kokoro"] = "installing the voice engine"
                    r = subprocess.run([self.python(), "-m", "pip", "install", "--quiet", "--no-warn-script-location",
                                        "kokoro-onnx>=0.4"], capture_output=True, text=True, timeout=1800,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if r.returncode != 0 or not self._importable():
                        raise OSError("installing kokoro-onnx failed: " + (r.stderr or r.stdout).strip()[-300:])
                for name, url in KOKORO_FILES.items():
                    if not os.path.isfile(_avatar_path("kokoro", name)):
                        AVATAR["status"]["kokoro"] = f"downloading {name}"
                        _download_file(url, _avatar_path("kokoro", name), {})
                with open(_avatar_path("kokoro", "worker.py"), "w") as f:
                    f.write(KOKORO_WORKER)
                open(_avatar_path("kokoro", ".installed"), "w").close()
                AVATAR["status"]["kokoro"] = "ready"
                try:
                    self.say("Hello.", "af_heart")          # load it now so the first reply is quick
                except Exception:   # noqa: BLE001 - warming up is optional
                    pass
            except (OSError, subprocess.SubprocessError, urllib.error.URLError, zipfile.BadZipFile) as e:
                AVATAR["status"]["kokoro"] = f"failed: {e}"
                log(f"kokoro: {e}")
            finally:
                self.preparing = False
        threading.Thread(target=work, daemon=True).start()

    def _start(self):
        import queue
        self.stop()
        with open(_avatar_path("kokoro", "worker.py"), "w") as f:     # always the version that matches this hub
            f.write(KOKORO_WORKER)
        self.proc = subprocess.Popen(
            [self.python(), _avatar_path("kokoro", "worker.py"), _avatar_path("kokoro", "kokoro-v1.0.int8.onnx"),
             _avatar_path("kokoro", "voices-v1.0.bin")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=_avatar_path("kokoro"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = self.lines = queue.Queue()
        out = self.proc.stdout

        def reader():
            for raw in iter(out.readline, b""):
                lines.put(raw.decode("utf-8", "replace").strip())
            lines.put(None)
        threading.Thread(target=reader, daemon=True).start()
        first = lines.get(timeout=180)          # loading the model takes a few seconds
        if first != "ready":
            self.stop()
            raise RuntimeError("the Kokoro voice didn't start")

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def say(self, text, voice, speed=1.0):
        import queue
        text = speakable(text)
        if not text:
            return b""
        out = _avatar_path("tts", uuid.uuid4().hex + ".wav")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                self._start()
            req = {"text": text, "voice": voice if voice in KOKORO_VOICES else "af_heart",
                   "speed": max(0.6, min(1.6, speed)), "lang": "en-gb" if voice.startswith("b") else "en-us", "out": out}
            self.proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            try:
                line = self.lines.get(timeout=60)
            except queue.Empty:
                self.stop()
                raise RuntimeError("the Kokoro voice took too long")
            if line is None or line.startswith("error"):
                if line is None:
                    self.stop()
                raise RuntimeError("Kokoro: " + (line or "stopped"))
        try:
            with open(out, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(out)
            except OSError:
                pass


KOKORO = KokoroTTS()



def _tts_speakable_ok():
    return bool(piper_exe()) and os.path.isfile(_avatar_path("voices", avatar_config()["voice"] + ".onnx"))


class PhraseCache:
    """Short spoken phrases ("Okay.", "Reminder set.") are kept as audio, so repeats don't run the voice engine."""
    MAX_CHARS, MAX_ITEMS, MAX_BYTES = 90, 150, 24_000_000

    def __init__(self):
        import collections
        self.items, self.size, self.lock = collections.OrderedDict(), 0, threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self.lock:
            wav = self.items.get(key)
            if wav is None:
                self.misses += 1
                return None
            self.items.move_to_end(key)
            self.hits += 1
            return wav

    def put(self, key, wav):
        if not wav or len(wav) > 2_000_000:
            return
        with self.lock:
            if key in self.items:
                self.size -= len(self.items.pop(key))
            self.items[key] = wav
            self.size += len(wav)
            while len(self.items) > self.MAX_ITEMS or self.size > self.MAX_BYTES:
                self.size -= len(self.items.popitem(last=False)[1])


PHRASES = PhraseCache()


class AvatarBus:
    """What Stella is doing on any device (listening, thinking, replying), for avatar screens to show.
    Screens poll /avatar/events?since=N about once a second."""

    def __init__(self):
        self.events, self.seq, self.lock = [], 0, threading.Lock()

    def post(self, ev):
        state = str(ev.get("state", ""))
        if state not in ("idle", "listening", "thinking", "talking", "reply"):
            raise ValueError("unknown state")
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "ts": time.time(), "state": state,
                                "source": str(ev.get("source", ""))[:64],
                                "device": str(ev.get("device", ""))[:40],
                                "text": str(ev.get("text", ""))[:4000],
                                "voice": bool(ev.get("voice"))})
            self.events = self.events[-50:]
            return self.seq

    def since(self, seq):
        with self.lock:
            return {"seq": self.seq, "now": time.time(), "events": [e for e in self.events if e["seq"] > seq and time.time() - e["ts"] < 120]}


AVATAR_BUS = AvatarBus()

WAKE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def custom_wakewords():
    if not WAKEWORD_DIR:
        return []
    try:
        return sorted(f[:-5] for f in os.listdir(WAKEWORD_DIR) if f.endswith(".onnx") and WAKE_NAME.fullmatch(f[:-5])
                      and ".." not in f)
    except (OSError, NameError):
        return []


# ---- The avatar "bin": one shared folder on the server with every character and animation.
#   bin/characters/*.vrm               characters (the newest is used unless you pick one)
#   bin/animations/*.vrma | *.fbx      animations; "name@greeting.fbx" sets when it plays (idle, greeting, quiet, happy)
# Drop files in (file manager, network share, scp) or upload them on any avatar screen. Every screen checks the
# bin's revision about once a second and loads what's new; deleted files disappear from every screen.
ANIM_WHEN = ("idle", "greeting", "quiet", "happy", "kiss", "dance", "off")
BIN_NAME = re.compile(r"[^/\\\x00]{1,120}")
_REV_CACHE = {"t": 0.0, "rev": ""}


def _safe_bin_name(name):
    return bool(BIN_NAME.fullmatch(name)) and name not in (".", "..") and not name.startswith(".")


def _bin_files(kind, exts):
    d = _avatar_path("bin", kind)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        full = os.path.join(d, n)
        if _safe_bin_name(n) and n.lower().endswith(exts) and os.path.isfile(full):
            st = os.stat(full)
            out.append((n, int(st.st_mtime), st.st_size))
    return sorted(out, key=lambda x: -x[1])


def list_characters():
    return [{"file": n, "name": os.path.splitext(n)[0].replace("_", " "), "v": v, "size": size}
            for n, v, size in _bin_files("characters", (".vrm",))]


def _anim_meta():
    try:
        with open(_avatar_path("animations.json")) as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_anim_meta(meta):
    os.makedirs(AVATAR["dir"], exist_ok=True)
    with open(_avatar_path("animations.json.part"), "w") as f:
        json.dump(meta, f, indent=1)
    os.replace(_avatar_path("animations.json.part"), _avatar_path("animations.json"))


def _anim_id(name, v, size):
    import hashlib
    return hashlib.sha1(f"{name}|{v}|{size}".encode()).hexdigest()[:12]


def list_animations():
    meta, out = _anim_meta(), []
    for n, v, size in _bin_files("animations", (".vrma", ".fbx")):
        base, ext = os.path.splitext(n)
        m = re.fullmatch(r"(.*?)@(idle|greeting|quiet|happy|kiss|dance|off)", base, re.I)
        when = (meta.get(n) or {}).get("when") or (m.group(2).lower() if m else "idle")
        out.append({"id": _anim_id(n, v, size), "file": n, "type": ext[1:].lower(), "v": v,
                    "name": (m.group(1) if m else base).replace("_", " ").strip() or base, "when": when})
    return sorted(out, key=lambda a: a["name"].lower())


def avatar_rev():
    """Changes whenever anything in the bin (or the avatar settings) changes."""
    now = time.time()
    if now - _REV_CACHE["t"] < 1.0 and _REV_CACHE["rev"]:
        return _REV_CACHE["rev"]
    import hashlib
    h = hashlib.sha1()
    for kind, exts in (("characters", (".vrm",)), ("animations", (".vrma", ".fbx"))):
        for item in _bin_files(kind, exts):
            h.update(repr((kind,) + item).encode())
    for f in ("config.json", "animations.json"):
        try:
            h.update(str(os.path.getmtime(_avatar_path(f))).encode())
        except OSError:
            pass
    _REV_CACHE.update(t=now, rev=h.hexdigest()[:12])
    return _REV_CACHE["rev"]


# ---- Lighter characters for phones and small screens ------------------------------------------
# VRoid characters carry 2048-4096 px textures: most of the download, and hundreds of MB of graphics memory once
# loaded. Phones and light-mode screens get a copy with textures capped at 1024 px (made once, kept in
# avatar/lite). The original file is never changed, and big screens still get it.
LITE_TEXTURE = 1024
_LITE = {"lock": threading.Lock(), "busy": set(), "failed": set()}


def slim_vrm(data, max_px=LITE_TEXTURE):
    """A copy of a .vrm/.glb with oversized textures scaled down, or None when there's nothing to gain."""
    import struct
    from PIL import Image
    if len(data) < 28 or data[:4] != b"glTF":
        return None
    version, total = struct.unpack_from("<II", data, 4)
    jlen, jtype = struct.unpack_from("<II", data, 12)
    if jtype != 0x4E4F534A:
        return None
    doc = json.loads(data[20:20 + jlen].decode("utf-8"))
    off = 20 + jlen
    if off + 8 > len(data):
        return None
    blen, btype = struct.unpack_from("<II", data, off)
    if btype != 0x004E4942:
        return None
    blob = memoryview(data)[off + 8:off + 8 + blen]
    views = doc.get("bufferViews") or []
    if len(doc.get("buffers") or []) != 1 or any(v.get("buffer", 0) != 0 for v in views):
        return None
    replaced = {}
    for img in doc.get("images") or []:
        vi = img.get("bufferView")
        if vi is None or vi in replaced or not 0 <= vi < len(views):
            continue
        v = views[vi]
        raw = bytes(blob[v.get("byteOffset", 0):v.get("byteOffset", 0) + v["byteLength"]])
        try:
            im = Image.open(io.BytesIO(raw))
            fmt = im.format
            if fmt not in ("PNG", "JPEG") or max(im.size) <= max_px:
                continue
            scale = max_px / max(im.size)
            size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            if im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGBA" if "A" in im.mode or "transparency" in im.info else "RGB")
            small = im.resize(size, Image.LANCZOS)
            out = io.BytesIO()
            if fmt == "JPEG":
                small.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
            else:
                small.save(out, "PNG", optimize=True)
        except Exception:   # noqa: BLE001 - an image Pillow can't read stays as it is
            continue
        if out.tell() < len(raw):
            replaced[vi] = out.getvalue()
    if not replaced:
        return None
    # Rebuild the binary part: every view in its original order, 4-byte aligned, with the new image bytes
    parts, pos = [], 0
    for i in sorted(range(len(views)), key=lambda k: views[k].get("byteOffset", 0)):
        v = views[i]
        chunk = replaced.get(i)
        if chunk is None:
            chunk = bytes(blob[v.get("byteOffset", 0):v.get("byteOffset", 0) + v["byteLength"]])
        pad = (-pos) % 4
        parts.append(b"\0" * pad)
        pos += pad
        v["byteOffset"], v["byteLength"] = pos, len(chunk)
        parts.append(chunk)
        pos += len(chunk)
    parts.append(b"\0" * ((-pos) % 4))
    binary = b"".join(parts)
    doc["buffers"][0]["byteLength"] = len(binary)
    js = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    js += b" " * ((-len(js)) % 4)
    return (b"glTF" + struct.pack("<II", version, 12 + 8 + len(js) + 8 + len(binary)) +
            struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(binary), 0x004E4942) + binary)


def lite_character(name):
    """Path of the lighter copy of a character, or None while it's being made (or when it can't be). The first
    request starts the work in the background and gets the original, so nobody waits."""
    src = _avatar_path("bin", "characters", name)
    try:
        st = os.stat(src)
    except OSError:
        return None
    tag = f"{int(st.st_mtime)}-{st.st_size}"
    folder = _avatar_path("lite")
    dest = os.path.join(folder, f"{name}.{tag}.vrm")
    if os.path.isfile(dest):
        return dest if os.path.getsize(dest) > 0 else None    # empty marker: nothing to gain for this file
    key = (name, tag)
    with _LITE["lock"]:
        if key in _LITE["busy"] or key in _LITE["failed"]:
            return None
        _LITE["busy"].add(key)

    def work():
        try:
            started = time.time()
            with open(src, "rb") as f:
                data = f.read()
            small = slim_vrm(data)
            os.makedirs(folder, exist_ok=True)
            for old in os.listdir(folder):               # copies of earlier versions of this character
                if old.startswith(name + ".") and old != os.path.basename(dest):
                    try:
                        os.remove(os.path.join(folder, old))
                    except OSError:
                        pass
            with open(dest + ".part", "wb") as f:
                f.write(small or b"")
            os.replace(dest + ".part", dest)
            if small:
                log(f"avatar: lighter copy of {name} for phones: {len(data) / 1e6:.1f} MB -> {len(small) / 1e6:.1f} MB "
                    f"({time.time() - started:.1f}s)")
        except ImportError:
            _LITE["failed"].add(key)
        except Exception as e:   # noqa: BLE001 - screens simply keep using the original
            _LITE["failed"].add(key)
            log(f"avatar: couldn't make a lighter copy of {name}: {e}")
        finally:
            _LITE["busy"].discard(key)

    threading.Thread(target=work, daemon=True).start()
    return None


def _free_name(folder, name):
    base, ext = os.path.splitext(name)
    cand, i = name, 2
    while os.path.exists(os.path.join(folder, cand)):
        cand, i = f"{base} ({i}){ext}", i + 1
    return cand


def _clean_name(name, ext):
    name = re.sub(r"[^\w .()@-]+", "_", os.path.basename(name or "")).strip(" .") or "file"
    if not name.lower().endswith("." + ext):
        name += "." + ext
    return name[-120:]


def add_character(data, name):
    if not data or data[:4] != b"glTF":
        raise ValueError("send a .vrm file (VRoid Studio exports these), under 120 MB")
    folder = _avatar_path("bin", "characters")
    os.makedirs(folder, exist_ok=True)
    fname = _free_name(folder, _clean_name(name or "character", "vrm"))
    with open(os.path.join(folder, fname + ".part"), "wb") as f:
        f.write(data)
    os.replace(os.path.join(folder, fname + ".part"), os.path.join(folder, fname))
    _REV_CACHE["t"] = 0
    return fname


def add_animation(data, name, when, ext):
    if ext not in ("vrma", "fbx"):
        raise ValueError("animations must be .vrma (VRM Animation) or .fbx (Mixamo)")
    if ext == "vrma" and data[:4] != b"glTF":
        raise ValueError("that isn't a .vrma file")
    if ext == "fbx" and not (data[:18] == b"Kaydara FBX Binary" or b"FBXHeaderExtension" in data[:2000]):
        raise ValueError("that isn't an .fbx file")
    folder = _avatar_path("bin", "animations")
    os.makedirs(folder, exist_ok=True)
    fname = _free_name(folder, _clean_name(name or "animation", ext))
    with open(os.path.join(folder, fname), "wb") as f:
        f.write(data)
    meta = _anim_meta()
    meta[fname] = {"when": when if when in ANIM_WHEN else "idle"}
    _save_anim_meta(meta)
    _REV_CACHE["t"] = 0
    return list_animations()


def update_animation(aid, change):
    for a in list_animations():
        if a["id"] != aid:
            continue
        meta = _anim_meta()
        if change.get("delete"):
            try:
                os.remove(_avatar_path("bin", "animations", a["file"]))
            except OSError:
                pass
            meta.pop(a["file"], None)
        elif change.get("when") in ANIM_WHEN:
            meta.setdefault(a["file"], {})["when"] = change["when"]
        _save_anim_meta(meta)
        _REV_CACHE["t"] = 0
        return list_animations()
    raise KeyError("no such animation")


def migrate_avatar_files():
    """Earlier versions kept model.vrm and anims/ next to the settings; move them into the bin."""
    try:
        old = _avatar_path("model.vrm")
        if os.path.isfile(old):
            os.makedirs(_avatar_path("bin", "characters"), exist_ok=True)
            os.replace(old, os.path.join(_avatar_path("bin", "characters"), _free_name(_avatar_path("bin", "characters"), "character.vrm")))
        try:
            with open(_avatar_path("animations.json")) as f:
                old_list = json.load(f)
        except (OSError, ValueError):
            old_list = None
        if isinstance(old_list, list):
            new_meta = {}
            os.makedirs(_avatar_path("bin", "animations"), exist_ok=True)
            for a in old_list:
                src = _avatar_path("anims", a.get("file", ""))
                if os.path.isfile(src):
                    fname = _free_name(_avatar_path("bin", "animations"), _clean_name(a.get("name"), a.get("type", "fbx")))
                    os.replace(src, os.path.join(_avatar_path("bin", "animations"), fname))
                    new_meta[fname] = {"when": a.get("when", "idle")}
            _save_anim_meta(new_meta)
    except OSError as e:
        log(f"avatar: couldn't move old files into the bin: {e}")


AVATAR_JS = r"""// StellAI avatar: an animated space orb (no libraries) or a 3D VRM character (three.js + three-vrm).
// Used by the web chat's avatar screen (/avatar). States: idle, listening, thinking, talking.
// setLevel(0..1) is the loudness of Stella's voice right now (moves the mouth / pulses the orb).
// Also: createWakeWord() — openWakeWord in the browser (ONNX Runtime Web), same models as the phone app.

const COLORS = { idle: [92, 225, 230], listening: [77, 232, 160], thinking: [155, 123, 255], talking: [120, 235, 255] };
const lerp = (a, b, t) => a + (b - a) * t;
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const smoothstep = x => { x = clamp(x); return x * x * (3 - 2 * x); };

/** Slow devices (Raspberry Pi and similar): fewer pixels and frames. */
export function looksLikeSlowDevice() {
  const ua = navigator.userAgent || "", cores = navigator.hardwareConcurrency || 4;
  return /aarch64|armv7|armv8|Raspbian|CrOS armv/i.test(ua) && !/Android|iPhone|iPad/i.test(ua) || cores <= 2;
}

export function createAvatar(host, opts = {}) {
  let impl = null, style = "", state = "idle", level = 0, destroyed = false;
  const o = { camera: "upper", height: 172.7, light: false, gestures: true, animations: [], key: "", ...opts };
  const onError = o.onError || (() => {});
  const stage = createStage(o.stageHost || host, o);      // the neon stage sits behind either look
  const keep = ":scope > :not([data-loading]):not([data-stage])";
  const sync = () => { impl.setState(state); impl.setLevel(level); impl.setNeon && impl.setNeon(o.neon); };

  let ver = "", seq = 0, loading = null;     // loading = {key, promise} while a 3D character downloads
  function showOrb() {
    if (impl && style === "orb") return;
    if (impl) impl.destroy();
    host.querySelectorAll(keep).forEach(el => el.remove());
    impl = createOrb(host, o); style = "orb";
  }
  // The current look (usually the orb) stays on screen while a 3D character downloads and is set up
  // in a hidden layer; it's swapped in only when it's ready. A big character over mobile data can take a while.
  async function setStyle(next, modelUrl, version = "") {
    const key = next + "|" + (next === "vrm" ? modelUrl + "|" + version : "");
    if (next === style && impl && (next !== "vrm" || version === ver)) { seq++; loading = null; return; }
    if (loading && loading.key === key) return loading.promise;
    const my = ++seq;
    if (next !== "vrm") { loading = null; showOrb(); ver = version; sync(); return; }
    const layer = document.createElement("div");
    layer.dataset.loading = "1"; layer.style.cssText = "position:absolute;inset:0;visibility:hidden";
    host.appendChild(layer);
    if (!impl) showOrb();
    const promise = (async () => {
      try {
        const v = await createVRM(layer, modelUrl, o);
        if (destroyed || my !== seq) { v.destroy(); layer.remove(); return; }
        if (impl) impl.destroy();
        host.querySelectorAll(keep).forEach(el => el.remove());
        delete layer.dataset.loading; layer.style.visibility = "";
        impl = v; style = "vrm"; ver = version; sync();
      } catch (e) {
        layer.remove();
        if (destroyed || my !== seq) return;
        onError(e); showOrb(); sync();
      } finally { if (my === seq) loading = null; }
    })();
    loading = { key, promise };
    return promise;
  }
  const call = (fn, ...a) => impl && impl[fn] && impl[fn](...a);
  return {
    setStyle,
    get style() { return style; },
    setState(s) { state = s; stage.setState(s); call("setState", s); },
    setLevel(x) { level = x; stage.setLevel(x); call("setLevel", x); },
    /** Neon look: {on, glow: "#rrggbb", haze: "#rrggbb", crowd: true/false} */
    setNeon(n) { o.neon = n; stage.setNeon(n); call("setNeon", n); },
    setVisemes(v) { call("setVisemes", v); },
    setCamera(c) { o.camera = c; call("setCamera", c); },
    setHeight(cm) { o.height = cm; call("setHeight", cm); },
    setLight(on) { o.light = !!on; stage.setLight(o.light); call("setLight", o.light); },
    setGestures(on) { o.gestures = !!on; },
    setAnimations(list) { o.animations = list || []; call("setAnimations", o.animations); },
    play(what) { return call("play", what); },
    trigger(tag) { call("trigger", tag); },
    react(text) { call("react", text); },
    fps() { return impl && impl.fps ? impl.fps() : 0; },
    destroy() { destroyed = true; if (impl) impl.destroy(); impl = null; stage.destroy(); host.innerHTML = ""; },
  };
}

// ------------------------------------------------------------------ the orb
function createOrb(host, o) {
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "position:relative;width:100%;height:100%;display:block";
  host.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  let state = "idle", level = 0, smooth = 0, raf = 0, w = 0, h = 0, dpr = 1, last = performance.now(), t = 0, fps = 60, acc = 0;
  let color = COLORS.idle.slice(), spin = 0, ripple = 0;
  const stars = Array.from({ length: 140 }, () => ({ x: Math.random(), y: Math.random(), r: Math.random() * 1.3 + 0.2, s: Math.random() * 0.6 + 0.2, p: Math.random() * 6 }));
  const sparks = Array.from({ length: 36 }, (_, i) => ({ a: (i / 36) * Math.PI * 2, ring: i % 3 }));
  const ro = new ResizeObserver(resize); ro.observe(host);
  // Resizing a canvas wipes it, so only resize when the size really changed, and redraw straight away
  // (phones resize the page all the time as the address bar slides and the keyboard opens: that was the flicker)
  function resize() {
    const nd = o.light ? 1 : Math.min(2, window.devicePixelRatio || 1), nw = host.clientWidth, nh = host.clientHeight;
    if (nd === dpr && nw === w && nh === h) return;
    dpr = nd; w = nw; h = nh;
    canvas.width = Math.max(1, w * dpr); canvas.height = Math.max(1, h * dpr);
    if (raf) frame(performance.now(), true);
  }
  resize();
  function frame(now, now_) {
    if (!now_) raf = requestAnimationFrame(frame);
    const step = Math.max((now - last) / 1000, 1e-3);
    if (o.light && step < 1 / 31 && !now_) return;          // light mode: 30 frames a second
    const dt = Math.min(0.05, step); last = now; t += dt;
    fps = lerp(fps, 1 / Math.max(step, 1e-3), 0.05);
    smooth = lerp(smooth, level, level > smooth ? 0.5 : 0.15);
    const target = COLORS[state] || COLORS.idle;
    for (let i = 0; i < 3; i++) color[i] = lerp(color[i], target[i], 0.06);
    const speed = state === "thinking" ? 2.4 : state === "listening" ? 0.9 : state === "talking" ? 0.7 : 0.35;
    spin += dt * speed;
    ripple = state === "listening" ? (ripple + dt * 0.55) % 1 : 0;
    const c = `${color[0] | 0},${color[1] | 0},${color[2] | 0}`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const nStars = o.light ? 60 : stars.length;
    for (let i = 0; i < nStars; i++) {
      const s = stars[i], tw = 0.5 + 0.5 * Math.sin(t * s.s * 2 + s.p);
      ctx.fillStyle = `rgba(200,215,255,${0.15 + tw * 0.5})`;
      ctx.beginPath(); ctx.arc(((s.x + t * 0.004 * s.s) % 1) * w, s.y * h, s.r, 0, 7); ctx.fill();
    }
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.17;
    const r = R * (1 + 0.035 * Math.sin(t * 1.6)) * (1 + smooth * 0.22);
    let g = ctx.createRadialGradient(cx, cy, r * 0.2, cx, cy, r * 3.2);
    g.addColorStop(0, `rgba(${c},0.35)`); g.addColorStop(0.4, `rgba(${c},0.10)`); g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r * 3.2, 0, 7); ctx.fill();
    if (state === "listening") {
      for (let k = 0; k < 3; k++) {
        const p = (ripple + k / 3) % 1;
        ctx.strokeStyle = `rgba(${c},${(1 - p) * 0.55})`; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(cx, cy, r * (1.1 + p * 1.6), 0, 7); ctx.stroke();
      }
    }
    for (let k = 0; k < 3; k++) {
      const tilt = [0.32, -0.55, 0.9][k], rx = r * (1.55 + k * 0.32), ry = rx * (0.28 + k * 0.08);
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(tilt + spin * (k % 2 ? -0.15 : 0.12));
      ctx.strokeStyle = `rgba(${c},${0.18 + (state === "thinking" ? 0.15 : 0)})`; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.ellipse(0, 0, rx, ry, 0, 0, 7); ctx.stroke();
      for (const s of sparks) {
        if (s.ring !== k) continue;
        const a = s.a + spin * (1 + k * 0.35) * (k % 2 ? -1 : 1), front = Math.sin(a) > 0;
        ctx.fillStyle = `rgba(${c},${front ? 0.95 : 0.35})`;
        ctx.beginPath(); ctx.arc(Math.cos(a) * rx, Math.sin(a) * ry, (front ? 2.4 : 1.4) * (state === "thinking" ? 1.3 : 1), 0, 7); ctx.fill();
      }
      ctx.restore();
    }
    if (smooth > 0.02 || state === "talking") {
      const n = o.light ? 48 : 72;
      ctx.strokeStyle = `rgba(${c},0.85)`; ctx.lineWidth = 2; ctx.beginPath();
      for (let i = 0; i <= n; i++) {
        const a = (i / n) * Math.PI * 2, wob = Math.sin(a * 6 + t * 9) * 0.5 + Math.sin(a * 11 - t * 13) * 0.5;
        const rr = r * (1.18 + smooth * 0.35 * (0.6 + 0.4 * wob));
        const x = cx + Math.cos(a) * rr, y = cy + Math.sin(a) * rr;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.stroke();
    }
    g = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 0.05, cx, cy, r);
    g.addColorStop(0, "rgba(255,255,255,0.95)"); g.addColorStop(0.35, `rgba(${c},0.95)`); g.addColorStop(1, `rgba(${c},0.25)`);
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.fill();
    if (state === "thinking") {
      for (let i = 0; i < 14; i++) {
        const a = spin * 2.2 - i * 0.09;
        ctx.fillStyle = `rgba(255,255,255,${0.8 * (1 - i / 14)})`;
        ctx.beginPath(); ctx.arc(cx + Math.cos(a) * r * 1.25, cy + Math.sin(a) * r * 1.25, 2.6 * (1 - i / 16), 0, 7); ctx.fill();
      }
    }
  }
  raf = requestAnimationFrame(frame);
  return {
    setState(s) { state = s; },
    setLevel(x) { level = clamp(x); },
    setLight() { resize(); },
    fps: () => fps,
    destroy() { cancelAnimationFrame(raf); ro.disconnect(); canvas.remove(); },
  };
}

// ------------------------------------------------------------------ the neon stage
// A dark stage behind Stella: coloured haze that breathes with her voice, a glowing floor, and (optional)
// soft, blurred dancer silhouettes swaying on both sides with the middle left clear. Drawn only while the
// neon look is on, at 30 frames a second (20 in light mode); the silhouettes are drawn once and reused.
const hexRgb = (h, d) => { const m = /^#?([0-9a-f]{6})$/i.exec(h || ""); const n = parseInt(m ? m[1] : d, 16); return [n >> 16, (n >> 8) & 255, n & 255]; };
function stageFigure(color, pose) {
  const c = document.createElement("canvas"); c.width = 220; c.height = 440;
  const g = c.getContext("2d");
  g.filter = "blur(10px)"; g.fillStyle = g.strokeStyle = color; g.lineCap = g.lineJoin = "round";
  g.beginPath(); g.arc(110, 62, 28, 0, 7); g.fill();                                   // head
  g.beginPath(); g.moveTo(100, 84); g.lineTo(120, 84); g.lineTo(152, 108); g.lineTo(136, 186);
  g.lineTo(160, 248); g.lineTo(60, 248); g.lineTo(84, 186); g.lineTo(68, 108); g.closePath(); g.fill();   // body
  g.lineWidth = 19; g.beginPath();
  g.moveTo(148, 110); if (pose) { g.lineTo(176, 60); g.lineTo(168, 14); } else { g.lineTo(184, 172); g.lineTo(148, 212); }   // her left arm: raised or on the hip
  g.moveTo(72, 110); g.lineTo(36, 172); g.lineTo(72, 212); g.stroke();                 // other hand on the hip
  g.lineWidth = 30; g.beginPath(); g.moveTo(128, 244); g.lineTo(140, 422); g.moveTo(92, 244); g.lineTo(84, 422); g.stroke();   // legs
  return c;
}
function createStage(host, o) {
  const canvas = document.createElement("canvas");
  canvas.dataset.stage = "1";
  canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:none;pointer-events:none";
  host.insertBefore(canvas, host.firstChild);
  const ctx = canvas.getContext("2d");
  let neon = null, level = 0, smooth = 0, state = "idle", raf = 0, last = 0, t = 0, w = 0, h = 0, figs = null, figKey = "";
  // [x across the screen (0..0.5, mirrored on the right), size (1 = nearest), pose]: far ones first
  const spots = [[0.06, 0.62, 1], [0.17, 0.66, 0], [0.28, 0.7, 1], [0.11, 0.8, 0], [0.23, 0.86, 1], [0.33, 0.95, 0]];
  const crowd = [];
  for (const side of [-1, 1]) for (const [x, k, pose] of spots) crowd.push({ x: side < 0 ? x : 1 - x, k, pose, flip: side, ph: Math.random() * 6.3, sp: 0.85 + Math.random() * 0.3 });
  crowd.sort((a, b) => a.k - b.k);
  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (!neon || !neon.on) return;
    const step = (now - last) / 1000;
    if (step < (o.light ? 1 / 20 : 1 / 31)) return;
    const dt = Math.min(0.1, step); last = now; t += dt;
    const nw = host.clientWidth, nh = host.clientHeight;
    if (nw !== w || nh !== h) { w = nw; h = nh; canvas.width = Math.max(1, w); canvas.height = Math.max(1, h); }   // soft by design: 1 pixel per CSS pixel is plenty
    smooth = lerp(smooth, level, 1 - Math.exp(-dt * 10));
    const hz = hexRgb(neon.haze, "ff3fb4"), gl = hexRgb(neon.glow, "3dff9e");
    const H = `${hz[0]},${hz[1]},${hz[2]}`, G = `${gl[0]},${gl[1]},${gl[2]}`;
    let g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, "#05030a"); g.addColorStop(0.6, `rgba(${H},0.08)`); g.addColorStop(1, `rgba(${H},0.28)`);
    ctx.globalAlpha = 1; ctx.fillStyle = "#05030a"; ctx.fillRect(0, 0, w, h); ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    g = ctx.createRadialGradient(w / 2, h * 0.55, 0, w / 2, h * 0.55, Math.max(w, h) * 0.6);
    g.addColorStop(0, `rgba(${H},${0.16 + 0.22 * smooth})`); g.addColorStop(1, `rgba(${H},0)`);
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    if (neon.crowd !== false) {
      const key = neon.haze;
      if (key !== figKey) { figKey = key; figs = [stageFigure(`rgb(${H})`, 0), stageFigure(`rgb(${H})`, 1)]; }
      const fast = state === "talking" ? 1.8 : 1;
      // sized by the narrower side, and squeezed outwards on tall (portrait) screens so the middle stays hers
      const spread = w >= h ? 1 : 0.62;
      for (const f of crowd) {
        const fh = Math.min(h * 0.72, w * 0.62) * f.k, fw = fh / 2, feet = h * (0.99 - (1 - f.k) * 0.35);
        const x = f.x < 0.5 ? f.x * spread : 1 - (1 - f.x) * spread;
        const a = t * 1.5 * f.sp * fast + f.ph;
        ctx.save();
        ctx.globalAlpha = 0.16 + 0.3 * f.k;
        ctx.translate(x * w + Math.sin(a) * fw * 0.08, feet + Math.abs(Math.cos(a)) * fh * 0.012);
        ctx.rotate(Math.sin(a) * 0.07); ctx.scale(f.flip, 1);
        ctx.drawImage(figs[f.pose], -fw / 2, -fh, fw, fh);
        ctx.restore();
      }
      ctx.globalAlpha = 1;
    }
    ctx.save(); ctx.translate(w / 2, h * 0.97); ctx.scale(1, 0.22);          // a flat pool of light on the floor
    g = ctx.createRadialGradient(0, 0, 0, 0, 0, w * 0.45);
    g.addColorStop(0, `rgba(${G},${0.2 + 0.25 * smooth})`); g.addColorStop(1, `rgba(${G},0)`);
    ctx.fillStyle = g; ctx.fillRect(-w / 2, -h * 4.6, w, h * 4.6 + w);
    ctx.restore();
  }
  raf = requestAnimationFrame(frame);
  return {
    setNeon(n) { neon = n || null; canvas.style.display = neon && neon.on ? "block" : "none"; last = 0; },
    setLevel(x) { level = clamp(x); },
    setState(s) { state = s; },
    setLight() {},
    destroy() { cancelAnimationFrame(raf); canvas.remove(); },
  };
}

// ------------------------------------------------------------------ the 3D character
// Mixamo bone names -> VRM humanoid bones (for .fbx animations from mixamo.com)
const MIXAMO = { Hips: "hips", Spine: "spine", Spine1: "chest", Spine2: "upperChest", Neck: "neck", Head: "head",
  LeftShoulder: "leftShoulder", LeftArm: "leftUpperArm", LeftForeArm: "leftLowerArm", LeftHand: "leftHand",
  RightShoulder: "rightShoulder", RightArm: "rightUpperArm", RightForeArm: "rightLowerArm", RightHand: "rightHand",
  LeftUpLeg: "leftUpperLeg", LeftLeg: "leftLowerLeg", LeftFoot: "leftFoot", LeftToeBase: "leftToes",
  RightUpLeg: "rightUpperLeg", RightLeg: "rightLowerLeg", RightFoot: "rightFoot", RightToeBase: "rightToes" };
for (const side of ["Left", "Right"]) {
  const s = side.toLowerCase();
  MIXAMO[side + "HandThumb1"] = s + "ThumbMetacarpal"; MIXAMO[side + "HandThumb2"] = s + "ThumbProximal"; MIXAMO[side + "HandThumb3"] = s + "ThumbDistal";
  for (const f of ["Index", "Middle", "Ring", "Pinky"]) {
    const v = f === "Pinky" ? "Little" : f;
    MIXAMO[side + "Hand" + f + "1"] = s + v + "Proximal"; MIXAMO[side + "Hand" + f + "2"] = s + v + "Intermediate"; MIXAMO[side + "Hand" + f + "3"] = s + v + "Distal";
  }
}
const BODY = ["hips", "spine", "chest", "upperChest", "neck", "head", "leftShoulder", "rightShoulder", "leftUpperArm", "rightUpperArm",
  "leftLowerArm", "rightLowerArm", "leftHand", "rightHand", "leftUpperLeg", "rightUpperLeg", "leftLowerLeg", "rightLowerLeg",
  "leftFoot", "rightFoot", "leftToes", "rightToes"];
const FINGERS = []; for (const s of ["left", "right"]) for (const f of ["Thumb", "Index", "Middle", "Ring", "Little"])
  for (const j of f === "Thumb" ? ["Metacarpal", "Proximal", "Distal"] : ["Proximal", "Intermediate", "Distal"]) FINGERS.push(s + f + j);

// Built-in gestures: offsets (radians) added to the relaxed pose, shaped by an envelope over `len` seconds.
// p = 0..1 progress, e = envelope 0..1, A = arm sign for this character. Tags say when each can play.
const GESTURES = {
  "Look around": { len: 4.5, tags: ["idle"], fn: (p, e) => ({ neck: [0, Math.sin(p * Math.PI * 2) * 0.35 * e, 0], head: [0, Math.sin(p * Math.PI * 2) * 0.25 * e, 0], spine: [0, Math.sin(p * Math.PI * 2) * 0.08 * e, 0], look: Math.sin(p * Math.PI * 2) * 1.6 * e }) },
  "Head tilt": { len: 3, tags: ["idle", "happy"], fn: (p, e) => ({ neck: [0, 0, 0.16 * e], head: [-0.05 * e, 0.05 * e, 0.1 * e], happy: 0.45 * e }) },
  "Weight shift": { len: 4, tags: ["idle"], fn: (p, e) => ({ hips: [0, 0.06 * e, Math.sin(p * Math.PI) * 0.05 * e], spine: [0, -0.04 * e, -Math.sin(p * Math.PI) * 0.05 * e], neck: [0, 0, -Math.sin(p * Math.PI) * 0.03 * e] }) },
  "Shoulder roll": { len: 2.6, tags: ["idle"], fn: (p, e, A) => { const s = Math.sin(p * Math.PI * 2) * e;
    return { leftShoulder: [0, 0, -0.12 * s * A], rightShoulder: [0, 0, 0.12 * s * A], chest: [-0.05 * e, 0, 0], neck: [0.06 * e, 0, 0] }; } },
  "Stretch": { len: 5, tags: ["quiet"], fn: (p, e, A) => { const up = smoothstep(clamp(p * 2.5)) * smoothstep(clamp((1 - p) * 2.5));
    return { leftUpperArm: [0, 0, 2.6 * up * A], rightUpperArm: [0, 0, -2.6 * up * A], leftLowerArm: [0, 0, 0.3 * up * A], rightLowerArm: [0, 0, -0.3 * up * A],
      spine: [-0.12 * up, 0, 0], chest: [-0.08 * up, 0, 0], head: [-0.15 * up, 0, 0], blink: up * 0.85, relaxed: up * 0.6 }; } },
  "Wave": { len: 3.2, tags: ["greeting"], fn: (p, e, A) => { const wv = Math.sin(p * Math.PI * 7) * 0.35 * e;
    return { rightUpperArm: [0, -0.3 * e * A, -1.5 * e * A], rightLowerArm: [0, 0, (-1.35 + wv) * e * A], rightHand: [0, 0, 0.1 * e],
      head: [0, -0.06 * e, -0.06 * e], happy: 0.7 * e }; } },
  // Blowing a kiss: right hand comes up to her lips (pucker, eyes closed), then sweeps out toward you
  // with an open hand and a wink and a smile. Uses arm IK so the hand really reaches her mouth on any character.
  "Blow a kiss": { len: 3.4, tags: ["kiss"], fn: (p, e) => {
    const ramp = (a, b) => smoothstep((p - a) / (b - a));
    const toLips = ramp(0.0, 0.28), out = ramp(0.48, 0.66), back = ramp(0.8, 1.0);
    const reach = toLips * (1 - back) * e;
    const pucker = toLips * (1 - out);
    return {
      ik: [{ side: "right", w: reach, target: out < 1e-3 ? { at: "lips" } : { at: "lips", to: "front", mix: out },
             pole: [0.3, -0.45, 0.05] }],
      straighten: { right: reach },
      neck: [0.04 * pucker - 0.03 * out, -0.06 * pucker, -0.06 * pucker + 0.05 * out],
      head: [0.05 * pucker - 0.06 * out, 0, -0.04 * pucker],
      spine: [0.02 * pucker - 0.02 * out, -0.04 * reach, 0],
      ou: 0.85 * pucker * e, blink: 0.75 * pucker * e,
      blinkLeft: out * (1 - ramp(0.72, 0.84)) * e,             // a quick wink as the kiss flies
      happy: (0.25 * pucker + 0.75 * out * (1 - back)) * e,
    }; } },
  // Hip-sway dance: both hands behind her head (elbows up and out), hips swaying right-to-left with a little
  // bounce on each step, upper body leaning the other way (an S-curve), head tilting with the beat, open smile.
  // Hair swings on its own (spring bones). Feet stay roughly planted.
  "Hip sway": { len: 9.6, tags: ["dance", "idle"], fn: (p, e) => {
    const ramp = (a, b) => smoothstep((p - a) / (b - a));
    const arms = ramp(0, 0.06) * (1 - ramp(0.94, 1)) * e;      // hands go up first, come down last
    const sw = Math.sin(p * Math.PI * 12) * ramp(0.04, 0.1) * (1 - ramp(0.9, 0.97)) * e;   // 6 sways, starting to her right
    const bounce = Math.abs(Math.sin(p * Math.PI * 12));      // up on each step
    const shift = -sw;                                        // + = toward her left
    const tilt = 0.14 * shift, twist = 0.12 * shift;
    const legL = 0.8, slide = Math.asin(clamp(0.05 * shift / legL, -0.2, 0.2));
    const bentL = Math.max(0, -shift), bentR = Math.max(0, shift);        // the leg without the weight bends
    return {
      ik: [{ side: "left", w: arms, target: { at: "behindHead" }, pole: [0.42, 0.3, 0.02] },
           { side: "right", w: arms, target: { at: "behindHead" }, pole: [0.42, 0.3, 0.02] }],
      hips: [0, twist, tilt], hipsX: 0.05 * shift, hipsY: 0.012 * bounce * e - 0.015 * e,
      leftUpperLeg: [-0.08 * bentL - 0.04 * e, 0, -tilt - slide], rightUpperLeg: [-0.08 * bentR - 0.04 * e, 0, -tilt - slide],
      leftLowerLeg: [0.22 * bentL + 0.08 * e, 0, 0], rightLowerLeg: [0.22 * bentR + 0.08 * e, 0, 0],
      leftFoot: [-0.12 * bentL, 0, 0], rightFoot: [-0.12 * bentR, 0, 0],
      spine: [-0.04 * e, -0.6 * twist, -0.75 * tilt], chest: [-0.03 * e, -0.3 * twist, -0.35 * tilt],
      neck: [0, 0.25 * twist, -0.2 * tilt], head: [0.03 * bounce * e, 0, 0.5 * tilt],
      happy: 0.3 * e, relaxed: 0.25 * e, aa: 0.35 * e,
    }; } },
  // Happy bounce: eyes closed in a big smile, hands tucked behind her back, shoulders up, bouncing on the beat
  // (knees dip, then she springs up with her head tipping back), head rocking side to side with each bounce.
  "Bounce": { len: 3.6, tags: ["happy", "greeting", "idle"], fn: (p, e, A) => {
    const beats = 7, ph = p * beats;                         // ~2 bounces a second
    const up = Math.pow(Math.abs(Math.sin(Math.PI * ph)), 0.7) * e, dip = (1 - Math.pow(Math.abs(Math.sin(Math.PI * ph)), 0.7)) * e;
    const side = Math.sin(Math.PI * ph) * e;                 // changes sign every bounce: rock left, then right
    return {
      leftUpperArm: [0, 0.45 * e * A, 0.1 * e * A], rightUpperArm: [0, -0.45 * e * A, -0.1 * e * A],     // arms swung back
      leftLowerArm: [0, 0.35 * e * A, 0], rightLowerArm: [0, -0.35 * e * A, 0],                          // hands meet behind
      leftShoulder: [0, 0, 0.1 * e * A], rightShoulder: [0, 0, -0.1 * e * A],                            // shoulders up
      leftUpperLeg: [-0.22 * dip, 0, 0], rightUpperLeg: [-0.22 * dip, 0, 0],                             // knees dip
      leftLowerLeg: [0.42 * dip, 0, 0], rightLowerLeg: [0.42 * dip, 0, 0],
      leftFoot: [-0.2 * dip, 0, 0], rightFoot: [-0.2 * dip, 0, 0],
      hipsY: (0.012 * up - 0.03 * dip),
      spine: [-0.05 * up, 0, 0.02 * side], chest: [-0.04 * up, 0, 0],
      neck: [0, 0.04 * side, 0.09 * side], head: [-0.1 * up, 0, 0.05 * side],
      happy: 0.95 * e, aa: 0.3 * up,
    }; } },
};

async function createVRM(host, url, o) {
  const THREE = await import("three");
  const { GLTFLoader } = await import("three/addons/loaders/GLTFLoader.js");
  const { VRMLoaderPlugin, VRMUtils } = await import("@pixiv/three-vrm");

  let light = !!o.light;
  const renderer = new THREE.WebGLRenderer({ antialias: !light, alpha: true, powerPreference: light ? "low-power" : "default" });
  const pixelRatio = () => light ? Math.min(1, window.devicePixelRatio || 1) * 0.85 : Math.min(2, window.devicePixelRatio || 1);
  renderer.setPixelRatio(pixelRatio());
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.style.cssText = "width:100%;height:100%;display:block";
  host.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(28, 1, 0.05, 60);
  const hemi = new THREE.HemisphereLight(0xbfd8ff, 0x1a1030, 1.6); scene.add(hemi);
  const key = new THREE.DirectionalLight(0xffffff, 2.2); key.position.set(1, 2, 3); scene.add(key);
  const rim = new THREE.DirectionalLight(0x5ce1e6, 1.4); rim.position.set(-2, 1.5, -2); scene.add(rim);
  const rim2 = new THREE.DirectionalLight(0xff3fb4, 0); rim2.position.set(2, 1.2, -1.5); scene.add(rim2);   // neon: second back light

  const loader = new GLTFLoader();
  loader.register(parser => new VRMLoaderPlugin(parser));
  const gltf = await new Promise((res, rej) => loader.load(url, res, x => o.onProgress && o.onProgress(x.loaded || 0, x.total || 0), e => rej(new Error("couldn't load the character: " + (e && e.message || e)))));
  const vrm = gltf.userData.vrm;
  if (!vrm) throw new Error("that file isn't a VRM character");
  VRMUtils.removeUnnecessaryVertices && VRMUtils.removeUnnecessaryVertices(gltf.scene);
  (VRMUtils.combineSkeletons || VRMUtils.removeUnnecessaryJoints || (() => {}))(gltf.scene);
  VRMUtils.rotateVRM0 && VRMUtils.rotateVRM0(vrm);
  vrm.scene.traverse(obj => { obj.frustumCulled = false; });
  scene.add(vrm.scene);

  const bone = n => vrm.humanoid && vrm.humanoid.getNormalizedBoneNode(n);
  const raw = n => vrm.humanoid && (vrm.humanoid.getRawBoneNode ? vrm.humanoid.getRawBoneNode(n) : bone(n));
  const ex = vrm.expressionManager;
  const setEx = (n, v) => { try { ex && ex.setValue(n, clamp(v)); } catch {} };
  const lookTarget = new THREE.Object3D(); scene.add(lookTarget);
  if (vrm.lookAt) { vrm.lookAt.target = lookTarget; if ("autoUpdate" in vrm.lookAt) vrm.lookAt.autoUpdate = true; }
  const bones = {}; for (const n of BODY.concat(FINGERS)) { const b = bone(n); if (b) bones[n] = b; }
  const hipsRest = bones.hips ? bones.hips.position.clone() : new THREE.Vector3();

  // ---- size: scale the character to the chosen height (default 5'8" = 172.7 cm)
  const box = new THREE.Box3().setFromObject(vrm.scene);
  const nativeHeight = Math.max(0.3, box.max.y - box.min.y);
  const headPos = new THREE.Vector3();
  function measureHead() { vrm.scene.updateMatrixWorld(true); (raw("head") || vrm.scene).getWorldPosition(headPos); }
  function setHeight(cm) {
    const s = clamp((+cm || 172.7) / 100, 1.2, 2.2) / nativeHeight;
    vrm.scene.scale.setScalar(s);
    measureHead(); setCamera(cameraMode, true);
  }

  // ---- relaxed pose: arms down instead of the T-pose. The sign is checked (models differ), then fixed.
  let A = 1;
  const P = (n, x, y, z) => { const b = bones[n]; if (b) { b.rotation.x += x; b.rotation.y += y; b.rotation.z += z; } };
  function resetPose() {
    for (const n in bones) bones[n].quaternion.identity();
    if (bones.hips) bones.hips.position.copy(hipsRest);
  }
  function relaxed() {
    P("leftUpperArm", 0, 0, -1.22 * A); P("rightUpperArm", 0, 0, 1.22 * A);
    P("leftLowerArm", 0, -0.15, -0.12 * A); P("rightLowerArm", 0, 0.15, 0.12 * A);
    for (const s of ["left", "right"]) for (const f of ["Index", "Middle", "Ring", "Little"]) {   // soft, slightly curled fingers
      const sg = s === "left" ? -1 : 1; P(s + f + "Proximal", 0, 0, 0.18 * sg * A); P(s + f + "Intermediate", 0, 0, 0.22 * sg * A);
    }
  }
  // Arm IK (for gestures where a hand has to reach a spot, like her lips): rest directions of each arm bone
  const restDir = {}, V3 = THREE.Vector3, Q = THREE.Quaternion;
  resetPose(); vrm.update(0); vrm.scene.updateMatrixWorld(true);
  for (const sd of ["left", "right"]) {
    const U = bones[sd + "UpperArm"], L = bones[sd + "LowerArm"], H = bones[sd + "Hand"];
    if (!U || !L || !H) continue;
    const pU = U.getWorldPosition(new V3()), pL = L.getWorldPosition(new V3()), pH = H.getWorldPosition(new V3());
    restDir[sd + "UpperArm"] = pL.clone().sub(pU).normalize().applyQuaternion(U.parent.getWorldQuaternion(new Q()).invert());
    restDir[sd + "LowerArm"] = pH.clone().sub(pL).normalize().applyQuaternion(U.getWorldQuaternion(new Q()).invert());
  }
  const hipsW = bones.hips ? bones.hips.getWorldPosition(new V3()) : new V3();
  const rightSign = bones.rightUpperArm ? Math.sign(bones.rightUpperArm.getWorldPosition(new V3()).x - hipsW.x) || -1 : -1;
  function reach(sd, T, pole, w) {     // two-bone IK: shoulder -> elbow -> wrist to target T (world), elbow toward pole
    const U = bones[sd + "UpperArm"], L = bones[sd + "LowerArm"], H = bones[sd + "Hand"];
    if (!U || !L || !H || !restDir[sd + "UpperArm"] || w <= 0) return;
    vrm.scene.updateMatrixWorld(true);
    const S = U.getWorldPosition(new V3()), E0 = L.getWorldPosition(new V3()), H0 = H.getWorldPosition(new V3());
    const L1 = S.distanceTo(E0), L2 = E0.distanceTo(H0);
    const dir = T.clone().sub(S); const d = clamp(dir.length(), Math.abs(L1 - L2) + 1e-3, L1 + L2 - 1e-3); dir.normalize();
    const a = (L1 * L1 - L2 * L2 + d * d) / (2 * d), h = Math.sqrt(Math.max(0, L1 * L1 - a * a));
    const pp = pole.clone().sub(S); const perp = pp.sub(dir.clone().multiplyScalar(pp.dot(dir))).normalize();
    const E = S.clone().add(dir.clone().multiplyScalar(a)).add(perp.multiplyScalar(h));
    const Hn = S.clone().add(dir.clone().multiplyScalar(d));
    const u = E.clone().sub(S).normalize().applyQuaternion(U.parent.getWorldQuaternion(new Q()).invert());
    U.quaternion.slerp(new Q().setFromUnitVectors(restDir[sd + "UpperArm"], u), w);
    U.updateMatrixWorld(true);
    const f = Hn.clone().sub(E).normalize().applyQuaternion(U.getWorldQuaternion(new Q()).invert());
    L.quaternion.slerp(new Q().setFromUnitVectors(restDir[sd + "LowerArm"], f), w);
    L.updateMatrixWorld(true);
  }
  function ikTargets(spec, sd) {       // named spots, in metres for a 170 cm person, scaled to her height
    const k = (o.height || 172.7) / 170, sx = sd === "right" ? rightSign : -rightSign;
    const head = (bones.head || vrm.scene).getWorldPosition(new V3());
    const sh = bones[sd + "UpperArm"].getWorldPosition(new V3());
    const spot = n => n === "lips" ? head.clone().add(new V3(sx * 0.03 * k, -0.06 * k, 0.13 * k))
      : n === "front" ? sh.clone().add(new V3(sx * 0.1 * k, 0.12 * k, 0.48 * k))
      : n === "behindHead" ? head.clone().add(new V3(sx * 0.07 * k, 0.11 * k, -0.07 * k)) : head.clone();
    const T = spot(spec.at);
    if (spec.to) T.lerp(spot(spec.to), spec.mix || 0);
    return T;
  }

  resetPose(); relaxed(); vrm.update(0); vrm.scene.updateMatrixWorld(true);
  const pa = new THREE.Vector3(), pb = new THREE.Vector3();
  if (raw("leftHand") && raw("leftUpperArm")) {
    raw("leftHand").getWorldPosition(pa); raw("leftUpperArm").getWorldPosition(pb);
    if (pa.y > pb.y - 0.05) A = -1;
  }

  // ---- camera framing
  let cameraMode = o.camera || "upper";
  const camGoal = new THREE.Vector3(), camAim = new THREE.Vector3(), aimNow = new THREE.Vector3();
  function setCamera(mode, jump) {
    cameraMode = mode;
    const y = headPos.y, hgt = y / 0.93;
    if (mode === "face") { camAim.set(0, y + 0.02, 0); camGoal.set(0, y + 0.04, 0.75); }
    else if (mode === "full") { camAim.set(0, hgt * 0.52, 0); camGoal.set(0, hgt * 0.58, hgt * 2.5); }
    else { camAim.set(0, y - 0.18, 0); camGoal.set(0, y - 0.08, 1.75); }
    if (jump) { cam.position.copy(camGoal); aimNow.copy(camAim); }
  }
  setHeight(o.height);

  // ---- neon look: glowing edges (the toon shader's rim light, and its outline when the model has one, in the
  // glow colour, unaffected by lighting), dimmer front light with two coloured back lights, and a soft outer
  // glow around her silhouette (a CSS drop-shadow on the see-through canvas; off in light mode). Talking
  // makes the edges glow brighter.
  const neon = { on: false, glow: new THREE.Color("#3dff9e"), haze: new THREE.Color("#ff3fb4"), pulse: 0 };
  const toon = [], tmpC = new THREE.Color();
  vrm.scene.traverse(m => {
    if (!m.isMesh) return;
    for (const mt of [].concat(m.material)) {
      if (!mt || !("parametricRimColorFactor" in mt) || !mt.parametricRimColorFactor || toon.some(x => x.m === mt)) continue;
      toon.push({ m: mt, rim: mt.parametricRimColorFactor.clone(), pow: mt.parametricRimFresnelPowerFactor, lift: mt.parametricRimLiftFactor,
        mix: mt.rimLightingMixFactor, oc: mt.outlineColorFactor ? mt.outlineColorFactor.clone() : null, omix: mt.outlineLightingMixFactor });
    }
  });
  function cssGlow() {
    const c = "#" + neon.glow.getHexString();
    renderer.domElement.style.filter = neon.on && !light ? `drop-shadow(0 0 3px ${c}) drop-shadow(0 0 14px ${c}99)` : "";
  }
  function setNeon(n) {
    neon.on = !!(n && n.on);
    try { neon.glow.set((n && n.glow) || "#3dff9e"); neon.haze.set((n && n.haze) || "#ff3fb4"); } catch {}
    hemi.intensity = neon.on ? 0.55 : 1.6; key.intensity = neon.on ? 1.0 : 2.2;
    if (neon.on) rim.color.copy(neon.glow); else rim.color.set(0x5ce1e6);
    rim.intensity = neon.on ? 3 : 1.4;
    rim2.color.copy(neon.haze); rim2.intensity = neon.on ? 2.2 : 0;
    for (const x of toon) {
      const mt = x.m;
      try {
        if (neon.on) {
          mt.parametricRimFresnelPowerFactor = 2.6; mt.parametricRimLiftFactor = 0.05;
          if (x.mix !== undefined) mt.rimLightingMixFactor = 0;
          if (x.oc) { mt.outlineColorFactor.copy(neon.glow); if (x.omix !== undefined) mt.outlineLightingMixFactor = 0; }
        } else {
          mt.parametricRimColorFactor.copy(x.rim); mt.parametricRimFresnelPowerFactor = x.pow; mt.parametricRimLiftFactor = x.lift;
          if (x.mix !== undefined) mt.rimLightingMixFactor = x.mix;
          if (x.oc) { mt.outlineColorFactor.copy(x.oc); if (x.omix !== undefined) mt.outlineLightingMixFactor = x.omix; }
        }
      } catch {}
    }
    cssGlow();
  }

  let lastW = 0, lastH = 0;
  function resize() {
    const w = host.clientWidth || 1, h = host.clientHeight || 1;
    if (w === lastW && h === lastH) return;
    lastW = w; lastH = h;
    renderer.setSize(w, h, false); cam.aspect = w / h; cam.updateProjectionMatrix();
    try { renderer.render(scene, cam); } catch {}        // redraw now so the screen doesn't blink
  }
  const ro = new ResizeObserver(resize); ro.observe(host); resize();

  // ---- animation clips (uploaded .vrma / Mixamo .fbx)
  const mixer = new THREE.AnimationMixer(vrm.scene);
  const clips = new Map();      // id -> {name, when, clip}
  let anim = null;              // {kind:"clip"|"gesture", ...}
  async function loadClip(a) {
    const u = a.url;
    if (a.type === "vrma") {
      const { VRMAnimationLoaderPlugin, createVRMAnimationClip } = await import("@pixiv/three-vrm-animation");
      const l = new GLTFLoader(); l.register(parser => new VRMAnimationLoaderPlugin(parser));
      const g = await l.loadAsync(u);
      const va = g.userData.vrmAnimations && g.userData.vrmAnimations[0];
      if (!va) throw new Error("no animation in " + a.name);
      return createVRMAnimationClip(va, vrm);
    }
    const { FBXLoader } = await import("three/addons/loaders/FBXLoader.js");
    const asset = await new FBXLoader().loadAsync(u);
    const src = THREE.AnimationClip.findByName(asset.animations, "mixamo.com") || asset.animations[0];
    if (!src) throw new Error("no animation in " + a.name);
    const tracks = [], restInv = new THREE.Quaternion(), parentRest = new THREE.Quaternion(), q = new THREE.Quaternion(), v = new THREE.Vector3();
    const mHips = asset.getObjectByName("mixamorigHips");
    const hipsH = mHips ? mHips.position.y : 1;
    const vrmHipsH = bones.hips ? Math.abs(bones.hips.getWorldPosition(v).y - vrm.scene.getWorldPosition(new THREE.Vector3()).y) / vrm.scene.scale.y : 1;
    const scaleHips = vrmHipsH / (hipsH || 1);
    const v0 = vrm.meta && vrm.meta.metaVersion === "0";
    for (const track of src.tracks) {
      const [rigName, prop] = track.name.split(".");
      const vrmBone = MIXAMO[rigName.replace(/^mixamorig:?/, "")];
      const node = vrmBone && bone(vrmBone), rigNode = asset.getObjectByName(rigName);
      if (!node || !rigNode) continue;
      rigNode.getWorldQuaternion(restInv).invert();
      rigNode.parent.getWorldQuaternion(parentRest);
      if (track instanceof THREE.QuaternionKeyframeTrack) {
        const vals = track.values.slice();
        for (let i = 0; i < vals.length; i += 4) {
          q.fromArray(vals, i).premultiply(parentRest).multiply(restInv).toArray(vals, i);
        }
        tracks.push(new THREE.QuaternionKeyframeTrack(`${node.name}.${prop}`, track.times, vals.map((x, i) => v0 && i % 2 === 0 ? -x : x)));
      } else if (track instanceof THREE.VectorKeyframeTrack && vrmBone === "hips") {
        // keep the bob, drop walking away: horizontal movement is made relative to the first frame
        const vals = track.values.map((x, i) => (v0 && i % 3 !== 1 ? -x : x) * scaleHips);
        const x0 = vals[0] - hipsRest.x, z0 = vals[2] - hipsRest.z;
        for (let i = 0; i < vals.length; i += 3) { vals[i] -= x0; vals[i + 2] -= z0; }
        tracks.push(new THREE.VectorKeyframeTrack(`${node.name}.${prop}`, track.times, vals));
      }
    }
    return new THREE.AnimationClip(a.name, src.duration, tracks);
  }
  async function setAnimations(list) {
    for (const a of list || []) {
      if (clips.has(a.id)) { clips.get(a.id).when = a.when; continue; }
      try { clips.set(a.id, { name: a.name, when: a.when, clip: await loadClip(a) }); }
      catch (e) { (o.onInfo || console.warn)("Animation " + a.name + ": " + e.message); }
    }
    const keep = new Set((list || []).map(a => a.id));
    for (const id of [...clips.keys()]) if (!keep.has(id)) clips.delete(id);
  }

  // ---- what plays when
  let state = "idle", level = 0, mouth = 0, t = 0, raf = 0, last = performance.now(), fps = 60;
  let visemes = null, visemesAt = 0; const vshape = {};
  let blinkAt = 1.5, blinkT = -1, emotion = null, emotionUntil = 0, vowel = "aa", vowelAt = 0, glance = 0, glanceAt = 2;
  let lastActive = performance.now() / 1000, nextIdleAt = 15 + Math.random() * 20, quietDone = false;
  const sm = { lean: 0, tilt: 0, nod: 0 };
  const emo = { happy: 0, sad: 0, surprised: 0, relaxed: 0 };
  function candidates(tag) {
    const out = [];
    for (const [id, c] of clips) if (c.when === tag) out.push({ kind: "clip", id, name: c.name, clip: c.clip });
    if (o.gestures !== false) for (const [name, g] of Object.entries(GESTURES)) if (g.tags.includes(tag)) out.push({ kind: "gesture", name, g });
    // uploaded animations are preferred when there are any for this moment
    const up = out.filter(x => x.kind === "clip");
    return up.length && Math.random() < 0.8 ? up : out;
  }
  function start(c) {
    if (!c) return false;
    stopAnim(true);
    if (c.kind === "clip") {
      const action = mixer.clipAction(c.clip); action.reset(); action.setLoop(THREE.LoopOnce, 1); action.clampWhenFinished = true; action.play();
      anim = { ...c, action, t0: t, len: c.clip.duration, w: 0, out: false };
    } else anim = { ...c, t0: t, len: c.g.len, w: 1, out: false };
    return true;
  }
  function stopAnim(now) {
    if (!anim) return;
    if (now) { if (anim.action) anim.action.stop(); anim = null; } else anim.out = true;
  }
  let pending = null;   // a gesture to play as soon as she's finished talking
  function trigger(tag) {
    if (state !== "idle" && tag !== "happy") { if (tag === "kiss" || tag === "dance") pending = tag; return; }
    const list = candidates(tag);
    if (list.length) start(list[Math.floor(Math.random() * list.length)]);
  }
  function play(what) {
    for (const [id, c] of clips) if (id === what || c.name === what) return start({ kind: "clip", id, name: c.name, clip: c.clip });
    if (GESTURES[what]) return start({ kind: "gesture", name: what, g: GESTURES[what] });
    return false;
  }
  function react(text) {
    const s = String(text || "").toLowerCase();
    let e = null;
    if (/\b(sorry|unfortunately|sad|can't|cannot|failed|error)\b/.test(s)) e = "sad";
    else if (/(!|\b(great|awesome|glad|love|nice|yay|congrat|happy|perfect|fun)\b)/.test(s)) e = "happy";
    else if (/\b(wow|whoa|really\?|interesting)\b/.test(s)) e = "surprised";
    if (e) { emotion = e; emotionUntil = t + 3.5; }
    if (/\b(love you|good ?night|sweet dreams|kisses?|xoxo|mwah)\b|💋|😘|😚/.test(s)) pending = "kiss";
    else if (/\b(danc(e|es|ing)|let'?s groove|💃)/.test(s) || /💃/.test(s)) pending = "dance";
    else if (e === "happy" && Math.random() < 0.35) setTimeout(() => trigger("happy"), 400);
  }

  const qProc = {}; for (const n in bones) qProc[n] = new THREE.Quaternion();
  const hipsProc = new THREE.Vector3();
  function frame(now) {
    raf = requestAnimationFrame(frame);
    const step = (now - last) / 1000;
    if (light && step < (state === "idle" && !anim ? 1 / 25 : 1 / 31)) return;   // light mode: 30 fps (25 when idle)
    const dt = Math.min(0.05, step); last = now; t += dt;
    fps = lerp(fps, 1 / Math.max(step, 1e-3), 0.05);
    if (state !== "idle") { lastActive = t; quietDone = false; if (anim && anim.kind === "clip" ? true : anim && !(anim.name === "Bounce" || anim.name === "Head tilt")) stopAnim(false); }

    // scheduler: an idle animation every 20–60 s; a stretch after 10 quiet minutes
    if (state === "idle" && !anim && pending) { const tg = pending; pending = null; trigger(tg); lastActive = t; }
    if (state === "idle" && !anim) {
      if (!quietDone && t - lastActive > 600) { quietDone = true; trigger("quiet"); }
      else if (t - lastActive > nextIdleAt) { trigger("idle"); lastActive = t; nextIdleAt = 20 + Math.random() * 40; }
    }

    // 1. the living base pose: relaxed arms, breathing, sway, listening lean, thinking tilt
    resetPose(); relaxed();
    sm.lean = lerp(sm.lean, state === "listening" ? 0.06 : 0, 0.08);
    sm.tilt = lerp(sm.tilt, state === "thinking" ? 0.1 : 0, 0.05);
    sm.nod = lerp(sm.nod, state === "thinking" ? -0.08 : (state === "talking" ? Math.sin(t * 3.1) * 0.03 : 0), 0.1);
    P("spine", sm.lean + Math.sin(t * 1.4) * 0.012, Math.sin(t * 0.37) * 0.025, 0);
    P(bones.chest ? "chest" : "upperChest", Math.sin(t * 1.4 + 0.6) * 0.015, 0, 0);
    P("neck", 0, Math.sin(t * 0.5) * 0.05, sm.tilt + Math.sin(t * 0.31) * 0.02);
    P("head", sm.nod, 0, 0);
    const sway = Math.sin(t * 0.9) * 0.03;
    P("leftUpperArm", 0, 0, sway * A); P("rightUpperArm", 0, 0, -sway * A);

    // 2. a built-in gesture adds on top; an uploaded clip blends in and out
    let extra = {}, look = 0;
    if (anim) {
      const p = clamp((t - anim.t0) / anim.len);
      if (anim.kind === "gesture") {
        let e = smoothstep(Math.min(p / 0.2, (1 - p) / 0.25));
        if (anim.out) { anim.w = Math.max(0, anim.w - dt * 4); e *= anim.w; }
        extra = anim.g.fn(p, e, A) || {};
        for (const [n, r] of Object.entries(extra)) if (Array.isArray(r)) P(n, r[0], r[1], r[2]);
        if (extra.hipsY && bones.hips) bones.hips.position.y += extra.hipsY / vrm.scene.scale.y;
        if (extra.hipsX && bones.hips) bones.hips.position.x += -rightSign * extra.hipsX / vrm.scene.scale.x;   // + = her left
        for (const sd in extra.straighten || {}) {           // open the hand (undo the relaxed finger curl)
          const w = extra.straighten[sd];
          for (const n of FINGERS) if (n.startsWith(sd) && bones[n]) bones[n].quaternion.slerp(new Q(), w);
        }
        for (const ik of extra.ik || []) {
          if (ik.w <= 0) continue;
          const S = bones[ik.side + "UpperArm"]; if (!S) continue;
          const k = (o.height || 172.7) / 170, sx = ik.side === "right" ? rightSign : -rightSign;
          const pole = S.getWorldPosition(new V3()).add(new V3(sx * ik.pole[0] * k, ik.pole[1] * k, ik.pole[2] * k));
          reach(ik.side, ikTargets(ik.target, ik.side), pole, ik.w);
        }
        look = extra.look || 0;
        if (p >= 1 || (anim.out && anim.w <= 0)) anim = null;
      } else {
        for (const n in bones) qProc[n].copy(bones[n].quaternion);
        if (bones.hips) hipsProc.copy(bones.hips.position);
        mixer.update(dt);
        anim.w = anim.out ? Math.max(0, anim.w - dt * 4) : Math.min(1, anim.w + dt * 2.5);
        const fadeEnd = smoothstep((anim.len - (t - anim.t0)) / 0.5);
        const w = anim.w * fadeEnd;
        for (const n in bones) { const b = bones[n]; const aq = b.quaternion.clone(); b.quaternion.copy(qProc[n]).slerp(aq, w); }
        if (bones.hips) bones.hips.position.lerpVectors(hipsProc, bones.hips.position.clone(), w);
        if (p >= 1 || (anim.out && anim.w <= 0)) { anim.action.stop(); anim = null; }
      }
    }

    // eyes: look at the camera, glance away now and then (up when thinking)
    if (t > glanceAt) { glance = Math.random() < 0.5 ? 0 : (Math.random() - 0.5) * 0.5; glanceAt = t + 1.5 + Math.random() * 3; }
    const sc = vrm.scene.scale.y;
    lookTarget.position.lerp(new THREE.Vector3(cam.position.x + (state === "thinking" ? 0.4 : glance) * sc + look,
      cam.position.y + (state === "thinking" ? 0.35 * sc : 0), cam.position.z), 0.08);

    // blinking
    if (t > blinkAt && blinkT < 0) blinkT = 0;
    let blink = 0;
    if (blinkT >= 0) { blinkT += dt; blink = blinkT < 0.07 ? blinkT / 0.07 : blinkT < 0.16 ? 1 - (blinkT - 0.07) / 0.09 : 0;
      if (blinkT >= 0.16) { blinkT = -1; blinkAt = t + 2 + Math.random() * 4; } }
    setEx("blink", Math.max(blink, extra.blink || 0));
    setEx("blinkLeft", extra.blinkLeft || 0); setEx("blinkRight", extra.blinkRight || 0);

    // mouth follows the voice; the vowel shape changes a few times a second
    mouth = lerp(mouth, clamp(level * 1.6), level > mouth ? 0.55 : 0.25);
    const vis = visemes && performance.now() - visemesAt < 250 ? visemes : null;    // mouth shapes from the actual sound
    if (!vis && t > vowelAt) { vowel = ["aa", "aa", "oh", "ih", "ou", "ee"][Math.floor(Math.random() * 6)]; vowelAt = t + 0.09 + Math.random() * 0.12; }
    for (const v of ["aa", "ih", "ou", "ee", "oh"]) {
      const shape = vis ? clamp(mouth * (vis[v] || 0) * 1.4) : (v === vowel ? mouth : (v === "aa" ? mouth * 0.3 : 0));
      vshape[v] = lerp(vshape[v] || 0, shape, 0.45);
      setEx(v, Math.max(vshape[v], extra[v] || 0));
    }

    // feelings fade in and out
    if (t > emotionUntil) emotion = null;
    const goal = { happy: Math.max(emotion === "happy" ? 0.65 : (state === "idle" ? 0.12 : 0), extra.happy || 0),
      sad: emotion === "sad" ? 0.45 : 0, surprised: emotion === "surprised" ? 0.5 : 0,
      relaxed: Math.max(state === "listening" ? 0.25 : 0, extra.relaxed || 0) };
    for (const k in emo) { emo[k] = lerp(emo[k], goal[k], 0.08); setEx(k, emo[k] * (1 - mouth * 0.5)); }

    if (neon.on) {             // edges glow brighter with her voice
      neon.pulse = lerp(neon.pulse, level, 1 - Math.exp(-dt * 12));
      tmpC.copy(neon.glow).multiplyScalar(0.9 + neon.pulse * 0.9);
      for (const x of toon) x.m.parametricRimColorFactor.copy(tmpC);
    }
    cam.position.lerp(camGoal, 0.05); aimNow.lerp(camAim, 0.05); cam.lookAt(aimNow);
    vrm.update(dt);
    renderer.render(scene, cam);
  }
  raf = requestAnimationFrame(frame);
  setTimeout(() => trigger("greeting"), 1200);    // a little wave when the screen opens
  setAnimations(o.animations);
  setNeon(o.neon);
  return {
    setState(s) { state = s; },
    setLevel(x) { level = clamp(x); },
    setVisemes(v) { visemes = v; visemesAt = performance.now(); },
    setCamera: m => setCamera(m),
    setHeight,
    setLight(on) { light = on; renderer.setPixelRatio(pixelRatio()); resize(); cssGlow(); },
    setNeon, setAnimations, play, trigger, react,
    fps: () => fps,
    destroy() {
      cancelAnimationFrame(raf); ro.disconnect(); mixer.stopAllAction();
      try { VRMUtils.deepDispose && VRMUtils.deepDispose(vrm.scene); } catch {}
      renderer.dispose(); renderer.domElement.remove();
    },
  };
}

// ------------------------------------------------------------------ wake word (openWakeWord in the browser)
// Same pipeline as the phone app: 80 ms chunks of 16 kHz audio -> mel spectrogram -> embeddings -> wake word score.
let ortLoading = null;
export function loadOrt() {     // ONNX Runtime Web, served by the StellAI server
  if (window.ort) return Promise.resolve(window.ort);
  if (!ortLoading) ortLoading = new Promise((res, rej) => {
    const s = document.createElement("script"); s.src = "/avatar/lib/ort/ort.wasm.min.js";
    s.onload = () => { window.ort.env.wasm.wasmPaths = "/avatar/lib/ort/"; window.ort.env.wasm.numThreads = 1; res(window.ort); };
    s.onerror = () => { ortLoading = null; rej(new Error("the speech engine files aren't downloaded yet")); }; document.head.appendChild(s);
  });
  return ortLoading;
}

// ------------------------------------------------------------------ voice activity detection (Silero VAD)
// prob(frame) -> 0..1 chance that a 512-sample (32 ms, 16 kHz) frame is speech. One model, many streams.
let vadSession = null;
export async function createVad() {
  const ort = await loadOrt();
  if (!vadSession) {
    const r = await fetch("/avatar/lib/vad/silero_vad.onnx");
    if (!r.ok) throw new Error("the speech detector isn't downloaded yet");
    vadSession = await ort.InferenceSession.create(new Uint8Array(await r.arrayBuffer()), { executionProviders: ["wasm"] });
  }
  const sess = vadSession;
  return {
    stream() {
      let state = new Float32Array(2 * 128), ctx = new Float32Array(64), busy = Promise.resolve();
      const sr = new ort.Tensor("int64", BigInt64Array.from([16000n]), []);
      return {
        reset() { state = new Float32Array(2 * 128); ctx = new Float32Array(64); },
        prob(frame) {          // frame: Float32Array(512), -1..1
          const run = async () => {
            const x = new Float32Array(576); x.set(ctx, 0); x.set(frame, 64); ctx = frame.slice(448);
            const out = await sess.run({ input: new ort.Tensor("float32", x, [1, 576]), state: new ort.Tensor("float32", state, [2, 1, 128]), sr });
            state = Float32Array.from(out.stateN.data);
            return out.output.data[0];
          };
          const p = busy.then(run, run); busy = p.catch(() => 0); return p;
        },
      };
    },
  };
}

/** Turns microphone audio at any sample rate into 16 kHz samples. */
export function makeResampler(rate) {
  const ratio = rate / 16000; let pos = 0, carry = new Float32Array(0);
  return input => {
    const all = new Float32Array(carry.length + input.length); all.set(carry, 0); all.set(input, carry.length);
    const out = [];
    while (pos + ratio <= all.length) { let s = 0; const a = Math.floor(pos), b = Math.floor(pos + ratio);
      for (let j = a; j < b; j++) s += all[j]; out.push(s / Math.max(1, b - a)); pos += ratio; }
    const used = Math.floor(pos); carry = all.slice(used); pos -= used;
    return Float32Array.from(out);
  };
}

export async function createWakeWord({ modelUrl, headers = {}, onWake, onError, threshold = 0.5, isBusy = () => false, isSpeaking = () => false,
                                        bargeIn = () => false, onBargeIn = null }) {
  const ort = await loadOrt();
  const fetchModel = async (u, h) => { const r = await fetch(u, { headers: h }); if (!r.ok) throw new Error("couldn't load " + u.split("/").pop()); return new Uint8Array(await r.arrayBuffer()); };
  const opts = { executionProviders: ["wasm"] };
  const mel = await ort.InferenceSession.create(await fetchModel("/avatar/lib/oww/melspectrogram.onnx"), opts);
  const emb = await ort.InferenceSession.create(await fetchModel("/avatar/lib/oww/embedding_model.onnx"), opts);
  const wake = await ort.InferenceSession.create(await fetchModel(modelUrl, headers), opts);
  const FRAMES = 16, CHUNK = 1280, CTX = 480, BINS = 32, WIN = 76;

  let tail = new Float32Array(CTX), melFrames = [], feats = [], preds = 0, cooldownUntil = 0;
  function reset() { melFrames = Array.from({ length: WIN }, () => new Float32Array(BINS).fill(1)); feats = []; preds = 0; }
  reset();
  async function run(sess, data, dims) {
    const out = await sess.run({ [sess.inputNames[0]]: new ort.Tensor("float32", data, dims) });
    return out[sess.outputNames[0]].data;
  }
  async function processChunk(chunk) {     // chunk: 1280 samples as int16-range floats
    const audio = new Float32Array(CTX + CHUNK); audio.set(tail, 0); audio.set(chunk, CTX); tail = audio.slice(audio.length - CTX);
    const m = await run(mel, audio, [1, audio.length]);
    for (let f = 0; f < m.length / BINS; f++) { const fr = new Float32Array(BINS); for (let b = 0; b < BINS; b++) fr[b] = m[f * BINS + b] / 10 + 2; melFrames.push(fr); }
    if (melFrames.length > 200) melFrames.splice(0, melFrames.length - 200);
    const win = new Float32Array(WIN * BINS), st = melFrames.length - WIN;
    for (let i = 0; i < WIN; i++) win.set(melFrames[st + i], i * BINS);
    feats.push(Float32Array.from(await run(emb, win, [1, WIN, BINS, 1])));
    if (feats.length > FRAMES * 2) feats.splice(0, feats.length - FRAMES * 2);
    if (feats.length < FRAMES) return 0;
    const dim = feats[0].length, inp = new Float32Array(FRAMES * dim), s0 = feats.length - FRAMES;
    for (let i = 0; i < FRAMES; i++) inp.set(feats[s0 + i], i * dim);
    const score = (await run(wake, inp, [1, FRAMES, dim]))[0];
    return ++preds <= 5 ? 0 : score;
  }

  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaStreamSource(stream), proc = ctx.createScriptProcessor(4096, 1, 1);
  const resample = makeResampler(ctx.sampleRate); let pending = [], working = false, stopped = false;
  // Interrupting by talking: while Stella speaks, a speech detector listens for you (needs echo cancellation)
  let vad = null, vbuf = [], speechRun = 0, vadBusy = false;
  if (onBargeIn) createVad().then(v => { vad = v.stream(); }).catch(() => {});
  async function bargeCheck(samples) {
    if (!vad || !isSpeaking() || !bargeIn() || isBusy()) { vbuf = []; speechRun = 0; return; }
    for (const x of samples) vbuf.push(x);
    if (vadBusy) return; vadBusy = true;
    try {
      while (vbuf.length >= 512) {
        const p = await vad.prob(Float32Array.from(vbuf.splice(0, 512)));
        speechRun = p > 0.8 ? speechRun + 1 : Math.max(0, speechRun - 2);
        if (speechRun >= 12) { speechRun = 0; vbuf = []; vad.reset(); onBargeIn(); break; }   // ~0.4 s of clear speech
      }
    } catch {} finally { vadBusy = false; }
  }
  proc.onaudioprocess = ev => {
    if (stopped) return;
    const out = resample(ev.inputBuffer.getChannelData(0));
    bargeCheck(out);
    for (const x of out) pending.push(x * 32767);
    if (pending.length > CHUNK * 6) pending.splice(0, pending.length - CHUNK * 6);   // falling behind: drop old audio
    pump();
  };
  async function pump() {
    if (working) return; working = true;
    try {
      while (pending.length >= CHUNK && !stopped) {
        const chunk = Float32Array.from(pending.splice(0, CHUNK));
        if (isBusy()) { reset(); continue; }                 // already recording a question
        const score = await processChunk(chunk);
        const need = isSpeaking() ? Math.min(0.92, threshold + 0.3) : threshold;  // harder while Stella talks
        if (score >= need && performance.now() > cooldownUntil) { cooldownUntil = performance.now() + 2000; reset(); onWake && onWake(score); }
      }
    } catch (e) { onError && onError(e); }
    working = false;
  }
  src.connect(proc); proc.connect(ctx.destination);
  return {
    setThreshold(x) { threshold = x; },
    stop() { stopped = true; try { proc.disconnect(); src.disconnect(); } catch {} stream.getTracks().forEach(tr => tr.stop()); ctx.close(); },
  };
}
"""  # end of avatar script


# ---- Moving Stella to another server ---------------------------------------------------------------------------
# "Stella, move yourself to the server": this server packs everything (a normal backup plus the API key and Auto
# mode's memory), sends it over Tailscale to the other StellAI server, which restores it and restarts as Stella.
# This one becomes a client: its model and voice stop, and it stays up as a signpost that points phones, browsers
# and avatar screens to the new address, so they follow by themselves. Models don't travel: each computer keeps
# the ones that suit its own hardware. The receiving server only accepts a move from a device on the same
# Tailscale account (or with its API key).
MOVE = {"state": "", "target": "", "error": "", "tokens": {}, "signpost": None, "arrived": None, "base": "",
        "lock": threading.Lock(), "restart_hook": None, "client_hook": None}
MOVE_TTL = 300            # a confirmation token is valid for 5 minutes
MOVE_RESTART_CODE = 76   # "start me again" after a move arrived (not 75: that one means "after an update")
MOVE_OPEN = ("/health", "/move/hello", "/move/receive", "/move/status")


def _move_path(*parts):
    return os.path.join(MOVE["base"] or os.path.dirname(os.path.abspath(CHATS_PATH or ".")), *parts)


def move_id():
    """This installation's id (so the old server can recognise its own arrival on the new one)."""
    p = _move_path("move-id.txt")
    try:
        with open(p) as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    v = uuid.uuid4().hex
    try:
        with open(p, "w") as f:
            f.write(v)
    except OSError:
        pass
    return v


def _tailscale_exe():
    import shutil
    w = shutil.which("tailscale")
    if w:
        return w
    for c in (r"C:\Program Files\Tailscale\tailscale.exe", "/usr/bin/tailscale", "/usr/sbin/tailscale"):
        if os.path.isfile(c):
            return c
    return ""


def _tailscale_json(*args, timeout=15):
    exe = _tailscale_exe()
    if not exe:
        return None
    try:
        p = subprocess.run([exe, *args, "--json"], capture_output=True, text=True, timeout=timeout,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def tailscale_devices():
    """(your devices on Tailscale, this device's user id). Each: name, dns, ip, online, os, user."""
    st = _tailscale_json("status")
    if not st:
        return [], None
    me = (st.get("Self") or {}).get("UserID")
    out = []
    for p in (st.get("Peer") or {}).values():
        ips = p.get("TailscaleIPs") or []
        out.append({"name": p.get("HostName", ""), "dns": (p.get("DNSName") or "").split(".")[0],
                    "ip": next((i for i in ips if "." in i), ips[0] if ips else ""), "online": bool(p.get("Online")),
                    "os": p.get("OS", ""), "user": p.get("UserID"), "mine": p.get("UserID") == me})
    return out, me


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def resolve_move_target(target):
    """(address, display name) for a Tailscale device name ("the server", "dl380p") or an address."""
    t = str(target or "").strip()
    t = re.sub(r"^(https?://)", "", t).split("/")[0]
    t = re.sub(r":\d+$", "", t)
    if not t:
        raise ValueError("say which computer to move to: its Tailscale name or its address")
    if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", t) or "." in t:
        return t, t
    devices, _ = tailscale_devices()
    mine = [d for d in devices if d["mine"]]
    want = _norm_name(re.sub(r"^(the|my)\s+", "", t, flags=re.I))
    exact = [d for d in mine if want in (_norm_name(d["name"]), _norm_name(d["dns"]))]
    near = exact or [d for d in mine if want and (want in _norm_name(d["name"]) or want in _norm_name(d["dns"]))]
    if len(near) == 1:
        return near[0]["ip"], near[0]["name"] or near[0]["dns"]
    names = ", ".join(sorted(d["name"] or d["dns"] for d in mine)) or "none found (is Tailscale installed here?)"
    if len(near) > 1:
        raise ValueError(f"'{t}' matches several of your devices ({', '.join(d['name'] for d in near)}); say which one")
    raise ValueError(f"no Tailscale device of yours is called '{t}'. Your devices: {names}")


def _hello(addr, timeout=6):
    host = f"[{addr}]" if ":" in addr and not addr.startswith("[") else addr
    with urllib.request.urlopen(f"http://{host}:8092/move/hello", timeout=timeout) as r:
        return json.loads(r.read())


def move_hello():
    import socket
    return {"stellai": True, "name": socket.gethostname(), "id": move_id(), "state": MOVE["state"] or "ready",
            "signpost": bool(MOVE["signpost"]), "arrived": MOVE["arrived"], "accepts": not MOVE["state"]}


def tool_move_stella(args):
    target, token = str(args.get("target", "")).strip(), str(args.get("confirm_token", "")).strip()
    if MOVE["state"] == "moving":
        return {"moving": True, "target": MOVE["target"]}, "Already moving"
    if MOVE["signpost"]:
        raise ValueError(f"Stella already moved from here to {MOVE['signpost'].get('name')}; this computer is a client")
    if token:
        with MOVE["lock"]:
            t = MOVE["tokens"].pop(token, None)
        if not t or time.time() - t["at"] > MOVE_TTL:
            raise ValueError("that confirmation expired: ask again where to move, and confirm")
        reply = str(args.get("user_reply", "")).lower()
        if not re.search(r"\b(yes|yeah|yep|sure|ok|okay|go|do it|confirm|move)\b", reply) or \
                re.search(r"\b(no|don't|do not|stop|cancel|wait)\b", reply):
            raise ValueError("the user hasn't confirmed: only move after they say yes")
        threading.Thread(target=_run_move, args=(t["addr"], t["name"], str(args.get("target_key", "") or "")),
                         daemon=True, name="move").start()
        return {"started": True, "target": t["name"],
                "say": f"Moving to {t['name']} now. It takes a few minutes; I'll say when I'm there."}, \
            f"Moving to {t['name']}"
    addr, name = resolve_move_target(target)
    try:
        h = _hello(addr)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ValueError(f"{name} doesn't answer as a StellAI server ({e}). Install StellAI there first (any model) "
                         "and make sure it's running.")
    if h.get("id") == move_id():
        raise ValueError("that's this computer")
    if not h.get("accepts"):
        raise ValueError(f"{h.get('name', name)} is busy ({h.get('state')}); try again in a few minutes")
    tok = uuid.uuid4().hex[:12]
    with MOVE["lock"]:
        MOVE["tokens"] = {k: v for k, v in MOVE["tokens"].items() if time.time() - v["at"] < MOVE_TTL}
        MOVE["tokens"][tok] = {"addr": addr, "name": h.get("name") or name, "at": time.time()}
    return {"needs_confirmation": True, "confirm_token": tok, "target": h.get("name") or name, "address": addr,
            "moves": "chats, memories, projects, reminders and lists, photos, diagrams, wake words, the avatar and "
                     "its settings, modes, the API key",
            "stays": "the models (the other computer uses its own); this computer becomes a client of it",
            "ask": f"Ask the user to confirm moving to {h.get('name') or name}. Only after they say yes, call "
                   "move_stella again with confirm_token and their exact words as user_reply."}, \
        f"Ready to move to {h.get('name') or name} (needs a yes)"


def _move_package():
    """A normal backup plus what a move also needs: the API key and Auto mode's topics."""
    import shutil
    import zipfile
    src = backup_now()
    pkg = _move_path(f"move-{uuid.uuid4().hex[:8]}.zip")
    shutil.copyfile(src, pkg)
    with zipfile.ZipFile(pkg, "a", zipfile.ZIP_DEFLATED) as z:
        import socket
        z.writestr("move.json", json.dumps({"api_key": API_KEY, "from": socket.gethostname(), "from_id": move_id(),
                                            "at": int(time.time())}))
        if AUTO_STATE.get("path") and os.path.isfile(AUTO_STATE["path"]):
            z.write(AUTO_STATE["path"], "files/auto-modes.json")
    return pkg


def _announce(text):
    try:
        AVATAR_BUS.post({"state": "reply", "text": text, "source": "server-move", "device": "server"})
    except Exception:
        pass


def _run_move(addr, name, target_key=""):
    MOVE.update(state="moving", target=name, error="")
    _announce(f"Moving to {name}. Give me a few minutes; this screen follows me there by itself.")
    log(f"move: packing up for {name} ({addr})")
    pkg = ""
    try:
        time.sleep(3)                    # anything already on its way lands before the snapshot
        pkg = _move_package()
        size = os.path.getsize(pkg)
        host = f"[{addr}]" if ":" in addr else addr
        log(f"move: sending {size // 1048576} MB to {name}")
        with open(pkg, "rb") as f:
            req = urllib.request.Request(f"http://{host}:8092/move/receive", data=f, method="POST",
                                         headers={"Content-Type": "application/zip", "Content-Length": str(size),
                                                  **({"Authorization": f"Bearer {target_key}"} if target_key else {})})
            with urllib.request.urlopen(req, timeout=max(300, size // 200_000)) as r:
                json.loads(r.read() or b"{}")
        log(f"move: {name} accepted it; waiting for it to restart as Stella")
        me, deadline = move_id(), time.time() + 900
        while time.time() < deadline:
            time.sleep(5)
            try:
                h = _hello(addr, timeout=5)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if (h.get("arrived") or {}).get("from_id") == me:
                break
        else:
            raise RuntimeError(f"{name} took the data but didn't come back within 15 minutes; check it there")
        info = {"name": name, "address": addr, "llama_url": f"http://{host}:8080", "hub_url": f"http://{host}:8092",
                "at": int(time.time())}
        with open(_move_path("moved.json"), "w") as f:
            json.dump(info, f)
        _announce(f"Done: I'm on {name} now. This computer is a client; this screen switches over in a moment.")
        log(f"move: Stella is on {name} now; this server becomes a client")
        time.sleep(15)                   # let screens show and say it before they're redirected
        MOVE.update(signpost=info, state="")
        _become_client(info)
    except Exception as e:
        MOVE.update(state="", error=str(e))
        log(f"move failed: {e}")
        _announce(f"The move to {name} didn't work, so I'm staying here. {str(e)[:200]}")
    finally:
        if pkg:
            _quiet_remove(pkg)


def _become_client(info):
    """Frees this computer: the model and voice stop; the hub stays as a signpost to the new address."""
    if MOVE["client_hook"]:              # the Windows app switches itself to client mode
        try:
            MOVE["client_hook"](info)
        except Exception as e:
            log(f"move: couldn't switch the app to client mode: {e}")
        return
    pid_file = getattr(SERVER_CONTROL, "pid_file", "") if SERVER_CONTROL else ""
    if pid_file:                         # the Linux launcher sees moved.json and doesn't start the model again
        try:
            with open(pid_file) as f:
                os.kill(int(f.read().strip()), 15)
        except (OSError, ValueError):
            pass


def _peer_is_mine(ip):
    """Is the computer at this address one of the same person's Tailscale devices?"""
    who = _tailscale_json("whois", ip)
    st = _tailscale_json("status")
    if not who or not st:
        return False
    me = (st.get("Self") or {}).get("UserID")
    peer = (who.get("UserProfile") or {}).get("ID")
    return bool(me) and me == peer


def receive_move(handler, cfg):
    """POST /move/receive: saves the package, checks it, and restarts this server as Stella."""
    import zipfile
    ip = handler.client_address[0]
    if not (handler.authorized() or _peer_is_mine(ip)):
        return handler.send_json(403, {"error": "moves are only accepted from your own Tailscale devices, or with "
                                                "this server's API key"})
    if MOVE["state"]:
        return handler.send_json(409, {"error": f"busy ({MOVE['state']})"})
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0 or n > 40 * 2**30:
        return handler.send_json(400, {"error": "send the move package (up to 40 GB)"})
    MOVE["state"] = "receiving"
    dest = _move_path("incoming-move.zip")
    try:
        left = n
        with open(dest + ".part", "wb") as f:
            while left > 0:
                buf = handler.rfile.read(min(left, 1 << 20))
                if not buf:
                    raise OSError("the transfer stopped early")
                f.write(buf)
                left -= len(buf)
        with zipfile.ZipFile(dest + ".part") as z:
            names = z.namelist()
            if "chats.db" not in names or "move.json" not in names:
                raise ValueError("that isn't a StellAI move package")
            info = json.loads(z.read("move.json"))
            if z.testzip() is not None:
                raise ValueError("the package arrived damaged")
        os.replace(dest + ".part", dest)
        with open(_move_path("pending-move.json"), "w") as f:
            json.dump({"zip": dest, "from": info.get("from", "?"), "from_id": info.get("from_id", ""),
                       "at": int(time.time())}, f)
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        MOVE["state"] = ""
        _quiet_remove(dest + ".part")
        return handler.send_json(400, {"error": f"move not accepted: {e}"})
    log(f"move: received Stella from {info.get('from')} ({n // 1048576} MB); restarting to take over")
    _announce(f"Stella is moving here from {info.get('from')}. One moment…")
    handler.send_json(200, {"accepted": True})
    threading.Thread(target=_restart_for_move, daemon=True).start()


def _restart_for_move():
    time.sleep(2)
    if MOVE["restart_hook"]:             # the Windows app restarts its servers
        MOVE["restart_hook"]()
        return
    global EXIT_CODE
    if MOVE["signpost"]:                 # this was a client: the launcher starts everything again
        _quiet_remove(_move_path("moved.json"))
        EXIT_CODE = 0
    else:
        EXIT_CODE = MOVE_RESTART_CODE     # the launcher starts the hub again in a few seconds
    if WEB:
        WEB.shutdown()


def apply_pending_move(cfg):
    """At start-up, before anything else: puts a received move in place (data, API key, Auto mode topics)."""
    import zipfile
    base = os.path.dirname(os.path.abspath(cfg.db))
    pend = os.path.join(base, "pending-move.json")
    if not os.path.isfile(pend):
        return None
    try:
        with open(pend) as f:
            info = json.load(f)
        zpath = info["zip"]
        log(f"move: restoring what came from {info.get('from')}")
        restore_backup(zpath, cfg)
        with zipfile.ZipFile(zpath) as z:
            meta = json.loads(z.read("move.json"))
            if "files/auto-modes.json" in z.namelist():
                with open(os.path.join(base, "auto-modes.json"), "wb") as out:
                    out.write(z.read("files/auto-modes.json"))
        key = str(meta.get("api_key") or "").strip()
        if key:
            with open(cfg.key_file, "w") as f:
                f.write(key)
        _quiet_remove(os.path.join(base, "moved.json"))   # whatever this computer was, it's Stella now
        info["arrived_at"] = int(time.time())
        with open(os.path.join(base, "arrived.json"), "w") as f:
            json.dump(info, f)
        # the model server must use the API key that came along (Linux launcher: it re-reads the key file)
        conf, pid_file = getattr(cfg, "model_conf", ""), getattr(cfg, "llama_pid", "")
        if conf and pid_file and os.path.isfile(pid_file):
            open(os.path.join(os.path.dirname(conf), ".switch-model"), "w").close()
            try:
                with open(pid_file) as f:
                    os.kill(int(f.read().strip()), 15)
            except (OSError, ValueError):
                pass
        log(f"move: Stella moved here from {info.get('from')}")
        return info
    except Exception as e:
        log(f"move: couldn't put the received data in place ({e}); keeping what was here")
        return None
    finally:
        _quiet_remove(pend)
        try:
            _quiet_remove(info["zip"])
        except (NameError, KeyError, TypeError):
            pass


SERVER_TOOLS["move_stella"] = (tool_move_stella, _fn(
    "move_stella",
    "Move Stella (all chats, memories, projects, settings, avatar, API key) from this server to another StellAI "
    "server on the user's Tailscale network, e.g. from a desktop to the home server. This computer then becomes a "
    "client and the phones, browsers and avatar screens follow her by themselves. Models stay: the other computer "
    "uses its own. Two calls: first with target only (it checks the other server and returns confirm_token), "
    "then ask the user to confirm and call again with confirm_token and their exact reply as user_reply. Never "
    "skip asking.",
    {"target": _p("The other computer: its Tailscale name ('server', 'dl380p') or address"),
     "confirm_token": _p("From the first call, after the user said yes"),
     "user_reply": _p("The user's exact words confirming the move"),
     "target_key": _p("Only if the other server isn't on the user's Tailscale account: its API key")},
    ["target"]))


# ----------------------------------------------------------------------------- HTTP API
def make_http_handler(cfg, chats):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def read_body(self, limit):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > limit:
                return None
            return self.rfile.read(n)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/move/receive":
                return receive_move(self, cfg)
            if self.move_gate(path, "POST"):
                return
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})

            if path == "/transcribe":
                rate = int(self.headers.get("X-Sample-Rate") or 16000)
                pcm = self.read_body(MAX_SECONDS * rate * 2)
                if pcm is None:
                    return self.send_json(400, {"error": "send 16-bit mono PCM, up to 60 s"})
                seconds = len(pcm) / (rate * 2)
                started = time.time()
                if not has_speech(pcm, rate):
                    log(f"phone: {seconds:.1f}s of silence (Whisper skipped)")
                    return self.send_json(200, {"text": ""})
                try:
                    text = clean_transcript(transcribe(cfg.whisper, pcm_to_wav(pcm, rate), cfg.language))
                except (urllib.error.URLError, OSError, RuntimeError) as e:
                    log(f"whisper error: {e}")
                    return self.send_json(503, {"error": "speech recognition unavailable"})
                log(f'phone: {seconds:.1f}s -> "{text}" ({time.time() - started:.1f}s)')
                return self.send_json(200, {"text": text})

            if path == "/tts":
                try:
                    req = json.loads(self.read_body(100_000) or b"{}")
                    ac = avatar_config()
                    voice = req.get("voice") if req.get("voice") in VOICES else ac["voice"]
                    if not os.path.isfile(_avatar_path("voices", voice + ".onnx")):
                        prepare_avatar() if voice == ac["voice"] else threading.Thread(
                            target=ensure_voice, args=(voice,), daemon=True).start()
                        return self.send_json(503, {"error": "The voice is still downloading", "status": AVATAR["status"]})
                    speed = float(req.get("speed") or ac["speed"])
                    wav = None
                    said = str(req.get("text", ""))
                    kvoice = req.get("kokoro_voice") or ac["kokoro_voice"]
                    short = len(said) <= PhraseCache.MAX_CHARS
                    key = None
                    if (req.get("engine") or ac["engine"]) == "kokoro":
                        if KOKORO.ready():
                            key = ("kokoro", str(kvoice), round(speed, 2), speakable(said))
                            wav = PHRASES.get(key) if short else None
                            try:
                                wav = wav or KOKORO.say(said, kvoice, speed)
                            except Exception as e:   # noqa: BLE001 - fall back to Piper rather than going silent
                                log(f"kokoro failed, using Piper: {e}")
                                key = None
                        else:
                            KOKORO.prepare()
                    if wav is None:
                        key = ("piper", voice, round(speed, 2), speakable(said))
                        wav = (PHRASES.get(key) if short else None) or TTS.say(said, voice, speed)
                    if short and key:
                        PHRASES.put(key, wav)
                except (ValueError, TypeError) as e:
                    return self.send_json(400, {"error": str(e)})
                except (RuntimeError, OSError) as e:
                    return self.send_json(503, {"error": f"speech failed: {e}"})
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav)))
                self.end_headers()
                self.wfile.write(wav)
                return
            if path == "/avatar/event":
                try:
                    seq = AVATAR_BUS.post(json.loads(self.read_body(100_000) or b"{}"))
                except ValueError as e:
                    return self.send_json(400, {"error": str(e)})
                return self.send_json(200, {"seq": seq})
            m = re.fullmatch(r"/avatar/anims/([0-9a-f]{12})", path)
            if m:
                try:
                    return self.send_json(200, {"animations": update_animation(
                        m.group(1), json.loads(self.read_body(10_000) or b"{}"))})
                except (KeyError, ValueError) as e:
                    return self.send_json(404, {"error": str(e)})
            if path == "/avatar/config":
                try:
                    return self.send_json(200, save_avatar_config(json.loads(self.read_body(100_000) or b"{}")))
                except (ValueError, TypeError, OSError) as e:
                    return self.send_json(400, {"error": str(e)})

            if path == "/sync":
                body = self.read_body(20_000_000)
                try:
                    req = json.loads(body or b"{}")
                    result = chats.sync(int(req.get("since", 0)),
                                        req.get("conversations") or [], req.get("messages") or [])
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": f"bad sync request: {e}"})
                return self.send_json(200, result)

            m = re.fullmatch(r"/chats/([^/]{1,64})/notes", path)
            if m:      # pin a note to a chat, or edit one ({"id", "text"})
                try:
                    data = json.loads(self.read_body(10_000) or b"{}")
                    note = (chats.update_chat_note(m.group(1), data["id"], data.get("text", "")) if data.get("id")
                            else chats.add_chat_note(m.group(1), data.get("text", ""), "user"))
                except (ValueError, KeyError, TypeError) as e:
                    return self.send_json(400, {"error": str(e)})
                return self.send_json(200, {"note": note, "notes": chats.chat_notes(m.group(1))})

            if path == "/memories":
                try:
                    data = json.loads(self.read_body(10_000) or b"{}")
                    mid, new = chats.add_memory(data.get("text", ""), "user")
                except ValueError as e:
                    return self.send_json(400, {"error": str(e)})
                return self.send_json(200, {"id": mid, "new": new})

            m = re.fullmatch(r"/memories/suggestions/([0-9a-f]{1,12})", path)
            if m:      # keep ({"accept": true, "text": optional edit}) or drop a suggested memory
                try:
                    data = json.loads(self.read_body(10_000) or b"{}")
                    chats.resolve_suggestion(m.group(1), bool(data.get("accept")), data.get("text"))
                except (ValueError, TypeError) as e:
                    return self.send_json(400, {"error": str(e)})
                return self.send_json(200, {"suggestions": chats.memory_suggestions(), "memories": chats.memories()})
            if path == "/memories/settings":
                try:
                    return self.send_json(200, save_memory_settings(json.loads(self.read_body(1000) or b"{}")))
                except (ValueError, TypeError) as e:
                    return self.send_json(400, {"error": str(e)})
            m = re.fullmatch(r"/chats/([^/]{1,64})/docs", path)
            if m:      # the user starts a document beside a chat
                try:
                    data = json.loads(self.read_body(2_000_000) or b"{}")
                    lang = str(data.get("language", "")).lower()
                    did = chats.create_doc(m.group(1), data.get("title", ""), data.get("text", ""),
                                           "code" if lang and lang not in ("markdown", "md", "text") else "text", lang, "user")
                except (ValueError, TypeError) as e:
                    return self.send_json(400, {"error": str(e)})
                return self.send_json(200, chats.doc(did))
            m = re.fullmatch(r"/docs/([0-9a-f]{10})", path)
            if m:      # the user's own edit from the side panel (base_version guards against overwriting Stella's)
                try:
                    data = json.loads(self.read_body(2_000_000) or b"{}")
                    if "revert" in data:
                        old = chats.doc_version_text(m.group(1), int(data["revert"]))
                        if old is None:
                            raise ValueError("that version isn't kept")
                        chats.save_doc(m.group(1), old, "user", f"back to v{int(data['revert'])}")
                    else:
                        chats.save_doc(m.group(1), data.get("text", ""), "user", "edited by you",
                                       title=data.get("title") or None, base_version=data.get("base_version"))
                except (ValueError, TypeError) as e:
                    return self.send_json(409 if "changed meanwhile" in str(e) else 400, {"error": str(e)})
                return self.send_json(200, chats.doc(m.group(1)))
            m = re.fullmatch(r"/chats/([^/]{1,64})/continue", path)
            if m:      # a new chat that starts from this one's summary and pinned notes
                try:
                    return self.send_json(200, continue_in_new_chat(m.group(1)))
                except ValueError as e:
                    return self.send_json(400, {"error": str(e)})
                except (urllib.error.URLError, OSError, RuntimeError) as e:
                    return self.send_json(503, {"error": f"couldn't summarize the chat: {e}"})
            if path == "/memories/learn":
                try:
                    data = json.loads(self.read_body(2_000_000) or b"{}")
                    started = time.time()
                    result = learn_memories(data.get("messages") or [])
                except (ValueError, KeyError, urllib.error.URLError, OSError) as e:
                    log(f"memory learning failed: {e}")
                    return self.send_json(503, {"error": f"couldn't learn from this chat: {e}"})
                if result["added"] or result["removed"]:
                    log(f"learned {result['added']} forgot {result['removed']} ({time.time() - started:.1f}s)")
                return self.send_json(200, result)

            if path == "/llm/v1/chat/completions":
                return self.proxy_llm()

            if path == "/quick":
                # Instant answers for simple requests; {"reply": null} means "ask the model"
                try:
                    data = json.loads(self.read_body(10_000) or b"{}")
                    hit = quick_reply(data.get("text", ""), data.get("now", ""), bool(data.get("phone")))
                except (ValueError, TypeError, AttributeError, OverflowError, KeyError) as e:
                    log(f"quick answer failed: {e}")
                    hit = None
                if hit:
                    log(f"instant answer ({hit['kind']}): no model needed")
                return self.send_json(200, hit or {"reply": None})

            if path == "/speedtest":
                report = run_speed_test(cfg.whisper if DOCTOR["whisper"] else "", cfg.language)
                if parse_qs(urlparse(self.path).query).get("format", [""])[0] == "text":
                    body = speed_text(report).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self.send_json(409 if report.get("error") else 200, report)

            if path == "/route":
                try:
                    data = json.loads(self.read_body(200_000) or b"{}")
                    return self.send_json(200, route_mode(data))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": f"bad request: {e}"})

            if path == "/context":
                try:
                    data = json.loads(self.read_body(5_000_000) or b"{}")
                    return self.send_json(200, plan_context(data))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": f"bad request: {e}"})

            if path == "/summarize":
                try:
                    data = json.loads(self.read_body(10_000_000) or b"{}")
                    started = time.time()
                    summary = summarize_chat(str(data.get("previous", "")), data.get("messages") or [],
                                             "story" if data.get("style") == "story" else "work")
                except (ValueError, TypeError, KeyError, IndexError, RuntimeError, urllib.error.URLError, OSError) as e:
                    log(f"summary failed: {e}")
                    return self.send_json(503, {"error": f"couldn't summarize: {e}"})
                log(f"refreshed a chat's context ({time.time() - started:.1f}s)")
                return self.send_json(200, {"summary": summary, "tokens": count_tokens(summary)})

            if path in ("/backups", "/backups/config", "/server/model", "/server/download"):
                try:
                    data = json.loads(self.read_body(20_000) or b"{}")
                    if path == "/backups":
                        return self.send_json(200, {"file": os.path.basename(backup_now()), **backups_status()})
                    if path == "/backups/config":
                        return self.send_json(200, save_backup_config(data))
                    if not SERVER_CONTROL:
                        return self.send_json(400, {"error": "this server can't switch models from the app"})
                    if path == "/server/model":
                        return self.send_json(200, {"message": SERVER_CONTROL.switch(str(data.get("key")))})
                    return self.send_json(200, {"message": SERVER_CONTROL.start_download(str(data.get("key")))})
                except (ValueError, RuntimeError, OSError, TypeError) as e:
                    return self.send_json(400, {"error": str(e)})

            if path == "/reminders" or path.startswith("/reminders/") or path.startswith("/lists/") \
                    or path == "/github":
                try:
                    data = json.loads(self.read_body(200_000) or b"{}")
                    if path == "/reminders":
                        due = int(data["due_at"]) if data.get("due_at") else _parse_when(data.get("when", ""))
                        return self.send_json(200, chats.add_reminder(data.get("text", ""), due, data.get("repeat", "none")))
                    if path == "/github":
                        return self.send_json(200, save_github_config(data))
                    m = re.fullmatch(r"/reminders/([0-9a-f]{10})", path)
                    if m:
                        r = chats.update_reminder(m.group(1), done=data.get("done"),
                                                  snooze_minutes=data.get("snooze_minutes"), cancel=bool(data.get("cancel")))
                        return self.send_json(200 if r else 404, r or {"error": "no such reminder"})
                    m = re.fullmatch(r"/lists/items/([0-9a-f]{10})", path)
                    if m:
                        return self.send_json(200, {"ok": chats.set_item(m.group(1), done=bool(data.get("done")))})
                    m = re.fullmatch(r"/lists/([^/]{1,60})/clear", path)
                    if m:
                        return self.send_json(200, {"removed": chats.clear_done(urllib.parse.unquote(m.group(1)))})
                    m = re.fullmatch(r"/lists/([^/]{1,60})", path)
                    if m:
                        items = data.get("items") or [data.get("text", "")]
                        name, added = chats.add_items(urllib.parse.unquote(m.group(1)), items)
                        return self.send_json(200, {"list": name, "added": added})
                    return self.send_json(404, {"error": "not found"})
                except (ValueError, TypeError, KeyError, AttributeError) as e:
                    return self.send_json(400, {"error": str(e)})

            if path == "/modes":
                try:
                    data = json.loads(self.read_body(100_000) or b"{}")
                    return self.send_json(200, chats.save_mode(data))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": str(e)})

            if path == "/projects":
                try:
                    data = json.loads(self.read_body(100_000) or b"{}")
                    return self.send_json(200, chats.save_project(data))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": str(e)})

            m = re.fullmatch(r"/projects/([0-9a-f]{12})/notes", path)
            if m:
                try:
                    data = json.loads(self.read_body(20_000) or b"{}")
                    return self.send_json(200, chats.add_project_note(m.group(1), data.get("text", ""), "user"))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": str(e)})

            if path.startswith("/tools/"):
                name = path[len("/tools/"):]
                body = self.read_body(1_000_000)
                try:
                    args = json.loads(body or b"{}")
                except ValueError:
                    return self.send_json(400, {"error": "arguments must be JSON"})
                started = time.time()
                result, summary = run_server_tool(name, args)
                log(f"tool {name}: {summary} ({time.time() - started:.1f}s)")
                return self.send_json(200, {"result": result, "summary": summary})

            return self.send_json(404, {"error": "not found"})

        def proxy_llm(self):
            """Streams a chat request through to llama-server, so the browser only talks to this one address."""
            body = self.read_body(30_000_000)
            if not body:
                return self.send_json(400, {"error": "empty request"})
            req = urllib.request.Request(LLAMA_URL.rstrip("/") + "/v1/chat/completions", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
            try:
                resp = urllib.request.urlopen(req, timeout=600)
            except urllib.error.HTTPError as e:
                data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except (urllib.error.URLError, OSError) as e:
                return self.send_json(502, {"error": f"the AI model isn't running ({e})"})
            with resp:
                self.send_response(200)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "text/event-stream"))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the browser stopped the reply

        def log_message(self, *args):
            pass

        def move_gate(self, path, method):
            """Moving or moved: answers instead of the normal handlers (True when it did)."""
            sp = MOVE["signpost"]
            if path in MOVE_OPEN:
                return False
            if sp:
                if method == "GET" and path in ("/", "/index.html", "/chat", "/avatar"):
                    q = urlparse(self.path).query
                    sep = "&" if q else ""
                    from urllib.parse import quote
                    loc = sp["hub_url"] + path + "?" + q + sep + "moved_from=" + quote(MOVE.get("from_name") or "")
                    self.send_response(302)
                    self.send_header("Location", loc)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return True
                self.send_json(410, {"error": f"Stella moved to {sp['name']} ({sp['hub_url']})", "moved_to": sp,
                                     "moved_from": MOVE.get("from_name", "")})
                return True
            if MOVE["state"] in ("moving", "receiving") and method != "GET" and path != "/avatar/event":
                self.send_json(503, {"error": f"Stella is moving{' to ' + MOVE['target'] if MOVE['target'] else ''}; "
                                              "try again in a few minutes", "moving": True})
                return True
            return False

        def authorized(self):
            auth = self.headers.get("Authorization", "")
            given = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if given and key_ok(given, cfg.key):
                return True
            qs = parse_qs(urlparse(self.path).query)
            given = given or qs.get("key", [""])[0]
            if key_ok(given, cfg.key):
                return True
            if given:
                note_wrong_key(self.client_address[0])
            return False

        def send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, file, content_type, revalidate=False):
            if not os.path.isfile(file):
                return self.send_json(404, {"error": "not found"})
            etag = None
            if revalidate:
                st = os.stat(file)
                etag = f'"{int(st.st_mtime)}-{st.st_size}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
            with open(file, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if etag:
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "private, no-cache")
            else:
                self.send_header("Cache-Control", "private, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):
            path = urlparse(self.path).path
            if self.move_gate(path, "PUT"):
                return
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})
            m = re.fullmatch(r"/projects/(library|[0-9a-f]{12})/files", path)
            if m:
                name = parse_qs(urlparse(self.path).query).get("name", ["file"])[0]
                data = self.read_body(MAX_PROJECT_FILE)
                if not data:
                    return self.send_json(400, {"error": "send a file under 25 MB"})
                started = time.time()
                try:
                    fid = add_project_file(m.group(1), name, data)
                except (ValueError, OSError, subprocess.SubprocessError) as e:
                    return self.send_json(400, {"error": str(e)})
                log(f"project file {name}: {len(data) // 1024} KB ({time.time() - started:.1f}s)")
                return self.send_json(200, {"id": fid, "project": chats.project(m.group(1))})
            if path == "/avatar/anims":
                q = parse_qs(urlparse(self.path).query)
                try:
                    items = add_animation(self.read_body(60_000_000) or b"", q.get("name", [""])[0],
                                          q.get("when", ["idle"])[0], q.get("type", [""])[0].lower())
                except ValueError as e:
                    return self.send_json(400, {"error": str(e)})
                return self.send_json(200, {"animations": items})
            if path == "/avatar/model":
                try:
                    fname = add_character(self.read_body(120_000_000),
                                          parse_qs(urlparse(self.path).query).get("name", ["character"])[0])
                except ValueError as e:
                    return self.send_json(400, {"error": str(e)})
                cfg_now = save_avatar_config({"style": "vrm", "character": fname})
                log(f"avatar: new character {fname}")
                return self.send_json(200, cfg_now)
            m = re.fullmatch(r"/images/([0-9a-f]{16})\.jpg", path)
            if not m:
                return self.send_json(404, {"error": "not found"})
            data = self.read_body(8_000_000)
            if not data or not data.startswith(b"\xff\xd8"):
                return self.send_json(400, {"error": "send a JPEG under 8 MB"})
            file = os.path.join(IMAGE_DIR, m.group(1) + ".jpg")
            if not os.path.exists(file):
                with open(file + ".part", "wb") as f:
                    f.write(data)
                os.replace(file + ".part", file)
            return self.send_json(200, {"ok": True})

        def do_DELETE(self):
            path = urlparse(self.path).path
            if self.move_gate(path, "DELETE"):
                return
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})
            m = re.fullmatch(r"/modes/(c-[0-9a-f]{10})", path)
            if m:
                return self.send_json(200, {"deleted": chats.delete_mode(m.group(1))})
            m = re.fullmatch(r"/docs/([0-9a-f]{10})", path)
            if m:
                return self.send_json(200, {"deleted": chats.delete_doc(m.group(1))})
            m = re.fullmatch(r"/chats/([^/]{1,64})/notes/([0-9a-f]{1,12})", path)
            if m:
                return self.send_json(200, {"deleted": chats.delete_chat_note(m.group(1), m.group(2)),
                                            "notes": chats.chat_notes(m.group(1))})
            m = re.fullmatch(r"/lists/items/([0-9a-f]{10})", path)
            if m:
                return self.send_json(200, {"deleted": chats.set_item(m.group(1), delete=True)})
            m = re.fullmatch(r"/projects/(library)/(files)/([0-9a-f]{1,32})", path) or \
                re.fullmatch(r"/projects/([0-9a-f]{12})(?:/(notes|files)/([0-9a-f]{1,32}))?", path)
            if m:
                pid, what, item = m.groups()
                if what == "notes":
                    return self.send_json(200, {"deleted": chats.delete_project_note(pid, item)})
                if what == "files":
                    return self.send_json(200, {"deleted": chats.delete_project_file(pid, item)})
                return self.send_json(200, {"deleted": chats.delete_project(pid)})
            m = re.fullmatch(r"/memories/([0-9a-f]{1,32})", path)
            if not m:
                return self.send_json(404, {"error": "not found"})
            return self.send_json(200, {"deleted": chats.delete_memory(m.group(1))})

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                return self.send_json(200, {"ok": True})
            if path == "/move/hello":
                return self.send_json(200, move_hello())
            if self.move_gate(path, "GET"):
                return
            if path == "/move/status" and self.authorized():
                return self.send_json(200, {**move_hello(), "target": MOVE["target"], "error": MOVE["error"],
                                            "moved_to": MOVE["signpost"]})
            if path == "/avatar/avatar.js":
                body = AVATAR_JS.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/avatar/lib/"):
                rel = path[len("/avatar/lib/"):]
                if rel not in AVATAR_LIBS:          # only the files listed above, never anything else
                    return self.send_json(404, {"error": "not found"})
                return self.send_file(_avatar_path("lib", *rel.split("/")),
                                      LIB_TYPES.get(os.path.splitext(rel)[1], "application/octet-stream"))
            if path in ("/", "/index.html", "/chat", "/avatar"):
                if path == "/avatar":
                    prepare_avatar()
                body = WEBCHAT_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; "
                                 "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
                                 "connect-src 'self' blob:; media-src 'self' blob:; worker-src 'self' blob:; "
                                 "frame-ancestors 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/favicon.ico":
                return self.send_json(404, {"error": "none"})
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})
            if path == "/avatar/config":
                return self.send_json(200, avatar_config())
            if path == "/avatar/events":
                try:
                    since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0])
                except ValueError:
                    since = 0
                return self.send_json(200, dict(AVATAR_BUS.since(since), avatar_rev=avatar_rev()))
            if path.startswith("/avatar/bin/"):
                kind, _, name = urllib.parse.unquote(path[len("/avatar/bin/"):]).partition("/")
                if kind not in ("characters", "animations") or not _safe_bin_name(name):
                    return self.send_json(404, {"error": "not found"})
                # revalidated each time (not cached forever), so a replaced file replaces the screen's copy
                if kind == "characters" and parse_qs(urlparse(self.path).query).get("lite", [""])[0] == "1":
                    lite = lite_character(name)   # phones and light-mode screens: smaller textures
                    if lite:
                        return self.send_file(lite, "application/octet-stream", revalidate=True)
                return self.send_file(_avatar_path("bin", kind, name), "application/octet-stream", revalidate=True)
            if path == "/avatar/model.vrm":
                ch = avatar_config()["character"]
                return self.send_file(_avatar_path("bin", "characters", ch), "model/gltf-binary", revalidate=True) \
                    if ch else self.send_json(404, {"error": "no character"})
            if path == "/tools":
                return self.send_json(200, {"tools": [t[1] for t in SERVER_TOOLS.values()],
                                            "approval": sorted(APPROVAL_TOOLS)})
            if path == "/reminders":
                return self.send_json(200, {"reminders": chats.reminders(), "now": int(time.time() * 1000)})
            if path == "/lists":
                return self.send_json(200, {"lists": chats.lists()})
            if path == "/github":
                return self.send_json(200, github_status())
            if path == "/search":
                q = parse_qs(urlparse(self.path).query)
                return self.send_json(200, {"results": CHAT_SEARCH.search(q.get("q", [""])[0], k=20,
                                                                           exclude=q.get("exclude", [""])[0])})
            if path == "/speedtest":      # past results (POST runs a new one)
                return self.send_json(200, {"history": _speed_history()[-10:]})
            if path == "/doctor":
                report = run_doctor()
                if parse_qs(urlparse(self.path).query).get("format", [""])[0] == "text":
                    body = doctor_text(report).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self.send_json(200, report)
            if path == "/backups":
                return self.send_json(200, backups_status())
            if path == "/server":
                return self.send_json(200, server_status())
            if path == "/memories":
                return self.send_json(200, {"memories": chats.memories()})
            m = re.fullmatch(r"/chats/([^/]{1,64})/notes", path)
            if m:
                return self.send_json(200, {"notes": chats.chat_notes(m.group(1))})
            if path == "/memories/suggestions":
                return self.send_json(200, {**memory_settings(), "suggestions": chats.memory_suggestions()})
            m = re.fullmatch(r"/chats/([^/]{1,64})/docs", path)
            if m:
                return self.send_json(200, {"docs": chats.docs(m.group(1))})
            m = re.fullmatch(r"/docs/([0-9a-f]{10})", path)
            if m:
                d = chats.doc(m.group(1))
                return self.send_json(200, d) if d else self.send_json(404, {"error": "no such document"})
            m = re.fullmatch(r"/docs/([0-9a-f]{10})/versions/(\d+)", path)
            if m:
                t = chats.doc_version_text(m.group(1), int(m.group(2)))
                return self.send_json(200, {"text": t}) if t is not None else self.send_json(404, {"error": "not kept"})
            if path == "/projects":
                return self.send_json(200, {"projects": chats.projects()})
            if path == "/modes":
                return self.send_json(200, {"modes": chats.modes()})
            if path == "/library":
                return self.send_json(200, chats.library())
            m = re.fullmatch(r"/projects/(library|[0-9a-f]{12})/files/([0-9a-f]{16})\.txt", path)
            if m:
                return self.send_file(os.path.join(PROJECT_DIR, m.group(2) + ".txt"), "text/plain; charset=utf-8")
            if path == "/wakewords":
                names = sorted(f[:-5] for f in os.listdir(WAKEWORD_DIR) if f.endswith(".onnx")
                               and WAKEWORD_NAME.fullmatch(f[:-5]) and ".." not in f)
                return self.send_json(200, {"wakewords": [
                    {"id": n, "label": " ".join(w.capitalize() for w in re.split(r"[_-]+", re.sub(r"_v\d.*$", "", n)) if w)}
                    for n in names]})
            m = re.fullmatch(r"/wakewords/(.+)\.onnx", path)
            if m and WAKEWORD_NAME.fullmatch(m.group(1)) and ".." not in m.group(1):
                return self.send_file(os.path.join(WAKEWORD_DIR, m.group(1) + ".onnx"), "application/octet-stream")
            m = re.fullmatch(r"/images/([0-9a-f]{16})\.jpg", path)
            if m:
                return self.send_file(os.path.join(IMAGE_DIR, m.group(1) + ".jpg"), "image/jpeg")
            m = re.fullmatch(r"/diagrams/([0-9a-f]{16})\.png", path)
            if m:
                file = os.path.join(DIAGRAM_DIR, m.group(1) + ".png")
                if not os.path.exists(file):
                    return self.send_json(404, {"error": "no such diagram"})
                with open(file, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(data)
                return
            return self.send_json(404, {"error": "not found"})

    for name in ("do_GET", "do_POST", "do_PUT", "do_DELETE"):
        setattr(Handler, name, _tracked(getattr(Handler, name)))
    return Handler


# ----------------------------------------------------------------------------- main
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="StellAI hub: speech-to-text and chat sync")
    p.add_argument("--key-file", required=True, help="file containing the API key")
    p.add_argument("--app-port", type=int, default=8092)
    p.add_argument("--whisper", default="http://127.0.0.1:8093", help="whisper-server URL")
    p.add_argument("--language", default="en", help="spoken language code, or empty for auto")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--db", default="chats.db", help="SQLite file for synced chats")
    p.add_argument("--diagram-dir", default="diagrams", help="where rendered diagrams are kept")
    p.add_argument("--diagram-python", default=sys.executable, help="Python with schemdraw + matplotlib")
    p.add_argument("--mmdc", default="mmdc", help="Mermaid CLI command")
    p.add_argument("--llama", default="http://127.0.0.1:8080", help="llama-server URL (for memory learning)")
    p.add_argument("--image-dir", default="images", help="where photos from chats are kept")
    p.add_argument("--wakeword-dir", default="wakewords", help="custom wake word models (*.onnx) for the app")
    p.add_argument("--project-dir", default="", help="project files (default: next to the chat database)")
    p.add_argument("--backup-dir", default="", help="where nightly backups go (default: backups next to the database)")
    p.add_argument("--model-conf", default="", help="Linux launcher's model.conf (enables switching models from the app)")
    p.add_argument("--models-dir", default="", help="folder with the .gguf models")
    p.add_argument("--llama-pid", default="", help="file with llama-server's process id")
    p.add_argument("--avatar-dir", default="", help="avatar screen files: voice, 3D libraries, your character "
                                                    "(default: avatar next to the database)")
    p.add_argument("--restore", default="", help="restore this backup zip (with the server stopped) and exit")
    p.add_argument("--searxng", default="", help="SearXNG URL for web search, e.g. http://127.0.0.1:8888 "
                                                 "(optional; DuckDuckGo is used otherwise)")
    p.add_argument("--github-file", default="", help="where the GitHub token/settings are kept "
                                                     "(default: github.json next to the chat database)")
    p.add_argument("--helper", default="", help="small helper llama-server URL for quick background jobs, e.g. "
                   "http://127.0.0.1:8095")
    p.add_argument("--embed", default="", help="embedding llama-server URL for meaning-based search, e.g. "
                                               "http://127.0.0.1:8094 (optional)")
    p.add_argument("--blender", default="", help="Blender executable for the 3D Modeler mode (default: found "
                                                 "automatically)")
    p.add_argument("--modeling-dir", default="", help="3D Modeler workspaces and inbox (default: 3d next to the "
                                                      "chat database)")
    p.add_argument("--update-repo", default="", help="owner/repo to self-update from every 30 minutes (Linux)")
    p.add_argument("--update-token-file", default="", help="GitHub token for a private update repo")
    return p.parse_args(argv)


def start(cfg):
    """Set up storage and return a ready (not yet serving) HTTP server. Used by main() and the Windows app."""
    MOVE["base"] = os.path.dirname(os.path.abspath(cfg.db))
    MOVE.update(state="", error="", signpost=None)
    arrived = apply_pending_move(cfg)
    with open(cfg.key_file) as f:
        cfg.key = f.read().strip()
    if not cfg.key:
        raise SystemExit("API key file is empty")

    chats = ChatDB(cfg.db)
    global DIAGRAM_DIR, PY_FOR_DIAGRAMS, MMDC, CHATS, LLAMA_URL, API_KEY, IMAGE_DIR, WAKEWORD_DIR, PROJECT_DIR
    CHATS, LLAMA_URL, API_KEY = chats, cfg.llama, cfg.key
    PROJECT_DIR = os.path.abspath(cfg.project_dir or os.path.join(os.path.dirname(os.path.abspath(cfg.db)),
                                                                  "projects"))
    os.makedirs(PROJECT_DIR, exist_ok=True)
    IMAGE_DIR = os.path.abspath(cfg.image_dir)
    WAKEWORD_DIR = os.path.abspath(cfg.wakeword_dir)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(WAKEWORD_DIR, exist_ok=True)
    DIAGRAM_DIR = os.path.abspath(cfg.diagram_dir)
    os.makedirs(DIAGRAM_DIR, exist_ok=True)
    PY_FOR_DIAGRAMS, MMDC = cfg.diagram_python, cfg.mmdc
    refresh_diagram_tool()
    EMBEDDER.start(getattr(cfg, "embed", ""))
    HELPER["url"] = getattr(cfg, "helper", "") or ""
    global SEARXNG_URL, GITHUB_FILE, CHATS_PATH, SERVER_CONTROL
    CHATS_PATH = os.path.abspath(cfg.db)
    base = os.path.dirname(CHATS_PATH)
    BACKUP["dir"] = os.path.abspath(getattr(cfg, "backup_dir", "") or os.path.join(base, "backups"))
    BACKUP["config"] = os.path.join(base, "backup.json")
    global SPEED_FILE
    SPEED_FILE = os.path.join(base, "speed-tests.json")
    BACKUP["paths"] = {"projects": PROJECT_DIR, "images": IMAGE_DIR, "diagrams": DIAGRAM_DIR, "wakewords": WAKEWORD_DIR}
    DOCTOR["whisper"] = cfg.whisper
    AVATAR["dir"] = os.path.abspath(getattr(cfg, "avatar_dir", "") or os.path.join(base, "avatar"))
    AUTO_STATE["path"] = os.path.join(base, "auto-modes.json")
    try:      # moved away earlier: this hub is only a signpost to Stella's new address
        with open(os.path.join(base, "moved.json")) as f:
            MOVE["signpost"] = json.load(f)
    except (OSError, ValueError):
        MOVE["signpost"] = None
    try:
        with open(os.path.join(base, "arrived.json")) as f:
            MOVE["arrived"] = json.load(f)
        if arrived:
            frm = MOVE["arrived"].get("from", "the other computer")
            threading.Timer(20, _announce, args=(f"I'm here now: I moved over from {frm}, and everything came "
                                                 "with me.",)).start()
    except (OSError, ValueError):
        MOVE["arrived"] = None
    import socket as _socket
    MOVE["from_name"] = _socket.gethostname()
    BLENDER["root"] = os.path.abspath(getattr(cfg, "modeling_dir", "") or os.path.join(base, "3d"))
    BLENDER["arg"] = getattr(cfg, "blender", "") or ""
    os.makedirs(os.path.join(BLENDER["root"], "inbox"), exist_ok=True)
    migrate_avatar_files()
    for kind in ("characters", "animations"):
        os.makedirs(_avatar_path("bin", kind), exist_ok=True)
    if getattr(cfg, "model_conf", "") and getattr(cfg, "models_dir", ""):
        SERVER_CONTROL = LinuxServerControl(cfg.model_conf, cfg.models_dir, cfg.llama_pid)
    SEARXNG_URL = getattr(cfg, "searxng", "") or ""
    GITHUB_FILE = os.path.abspath(getattr(cfg, "github_file", "") or
                                  os.path.join(os.path.dirname(os.path.abspath(cfg.db)), "github.json"))
    global WEB
    web = ThreadingHTTPServer((cfg.host, cfg.app_port), make_http_handler(cfg, chats))
    web.daemon_threads = True
    WEB = web
    CHAT_SEARCH.start()
    if not BACKUP.get("loop"):
        BACKUP["loop"] = True
        threading.Thread(target=backup_loop, daemon=True, name="backups").start()
    if not SUMMARIZER.get("loop") and not MOVE["signpost"]:
        SUMMARIZER["loop"] = True
        threading.Thread(target=summarizer_loop, daemon=True, name="summaries").start()
        threading.Thread(target=learner_loop, daemon=True, name="learner").start()
    log(f"hub ready on :{cfg.app_port}, chats in {cfg.db}"
        + ("" if mermaid_command() else " (Mermaid not installed: flowcharts use schemdraw)"))
    return web


def main():
    if "--self-test" in sys.argv:
        self_test()
        return
    if "--write-shape-script" in sys.argv:   # used by stellai-setup.sh to test the photo-to-3D install
        out = sys.argv[sys.argv.index("--write-shape-script") + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write(SHAPE_PY)
        print(out)
        return
    cfg = parse_args()
    if cfg.restore:
        with open(cfg.key_file) as f:
            pass
        print(restore_backup(cfg.restore, cfg))
        return
    web = start(cfg)
    threading.Thread(target=ensure_packages, daemon=True).start()
    if cfg.update_repo:
        token = ""
        if cfg.update_token_file:
            try:
                with open(cfg.update_token_file) as f:
                    token = f.read().strip()
            except OSError:
                pass
        threading.Thread(target=updater_loop, args=(cfg.update_repo, token), daemon=True).start()
        log(f"auto-update: checking github.com/{cfg.update_repo} every 30 minutes")
        DOCTOR["update"] = f"automatic, from github.com/{cfg.update_repo}"
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        web.server_close()
        CHATS.db.close()
    sys.exit(EXIT_CODE)


if __name__ == "__main__":
    main()
