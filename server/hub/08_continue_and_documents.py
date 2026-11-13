# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
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


