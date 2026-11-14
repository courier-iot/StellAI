# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Replies run on the server ----------------------------------------------------------------------------------
# The phone and the desktop chat send the message (POST /chats/<id>/reply); the server does everything else and
# streams the reply back as lines of JSON ("events"): instant answers, Auto mode, making room in a long chat, the
# model, server tools, continuing a reply that ran out of room. When Stella wants something only the app can do (a
# phone action, or the user's OK for a GitHub commit), the server sends a question event and waits for the app's
# answer (POST /replies/<run>/answer). Every message she writes is saved here and sent to the app, so all devices
# behave the same and new features are built once.
#
# Events: run {run_id} · status {text} · tool {name} (a server tool started) · delta {text, reasoning} (the reply so
# far) · message {message} (saved: assistant, tool line or summary) · mode {mode} (Auto mode changed) · changed
# {what} (reminders/lists changed) · client_tool {call_id, name, arguments} · approval {call_id, name, args} ·
# context {used, limit} · done {error, stopped}
REPLY_RUNS = {}
REPLY_LOCK = threading.Lock()
REPLY_MAX_ROUNDS = 6
REPLY_MAX_CONTINUES = 2
REPLY_CONTINUE = ("(Automatic: your previous reply was cut off because your memory filled up. Continue it exactly "
                  "where it stopped, without repeating anything or adding a preamble.)")
REPLY_PHOTO = re.compile(r"!\[[^\]]*\]\(image:([0-9a-f]{16})\)")
REPLY_DIAGRAM = re.compile(r"!\[[^\]]*\]\(diagram:[0-9a-f]+\)")
REPLY_THINK = re.compile(r"<think>[\s\S]*?(</think>|$)")
REPLY_CODE_MODES = {"programmer", "electronics", "modeler", "cad", "pcb"}
REPLY_QUICK_MODES = {"general", "programmer", "electronics"}
REPLY_STICKY = {}          # chat -> tools used so far (kept offered, so the model's cache stays warm)
_REPLY_TS = {"last": 0}


def _reply_ts(conv_id=""):
    """Strictly increasing millisecond timestamps, always after the chat's newest message (the phone's clock may
    run a little ahead of the server's, and her reply must still come after the question)."""
    newest = 0
    if conv_id:
        row = CHATS.db.execute("SELECT MAX(created_at) FROM messages WHERE conv_id=?", (conv_id,)).fetchone()
        newest = (row[0] or 0) if row else 0
    with REPLY_LOCK:
        t = max(int(time.time() * 1000), _REPLY_TS["last"] + 1, newest + 1)
        _REPLY_TS["last"] = t
        return t


class ReplyRun:
    def __init__(self, conv_id, data, send):
        self.id = uuid.uuid4().hex[:12]
        self.conv = conv_id
        self.data = data
        self._send = send
        self.stop = threading.Event()
        self.waiting = {}

    def emit(self, event):
        """Sends one event to the app. If the app has gone (stopped, closed), the reply stops."""
        if self.stop.is_set() and event.get("type") not in ("done", "message"):
            return
        try:
            self._send(event)
        except (OSError, ValueError):
            self.stop.set()

    def ask(self, kind, payload, timeout=600):
        """Asks the app something (a phone action, the user's OK) and waits for its answer (None = no answer)."""
        call_id = uuid.uuid4().hex[:10]
        slot = {"event": threading.Event(), "answer": None}
        self.waiting[call_id] = slot
        self.emit({"type": kind, "call_id": call_id, **payload})
        deadline = time.time() + timeout
        while not slot["event"].wait(0.5):
            if self.stop.is_set() or time.time() > deadline:
                break
        self.waiting.pop(call_id, None)
        return slot["answer"]

    def answer(self, call_id, answer):
        slot = self.waiting.get(str(call_id))
        if not slot:
            return False
        slot["answer"] = answer if isinstance(answer, dict) else {}
        slot["event"].set()
        return True


def reply_answer(run_id, call_id, answer):
    run = REPLY_RUNS.get(str(run_id))
    return bool(run and run.answer(call_id, answer))


def reply_stop(run_id):
    run = REPLY_RUNS.get(str(run_id))
    if run:
        run.stop.set()
    return bool(run)


def _reply_save(run, role, text, reasoning="", created=None):
    ts = _reply_ts(run.conv)
    m = {"id": str(uuid.uuid4()), "conv_id": run.conv, "role": role, "text": text, "reasoning": reasoning or "",
         "created_at": created or ts, "updated_at": ts, "deleted": False}
    CHATS.sync(0, [], [m])
    run.emit({"type": "message", "message": m})
    return m


def _reply_messages(conv_id):
    rows = CHATS.db.execute("SELECT id, role, text, created_at FROM messages WHERE conv_id=? AND deleted=0 "
                            "ORDER BY created_at, id", (conv_id,)).fetchall()
    return [{"id": r[0], "role": r[1], "text": r[2], "created_at": r[3]} for r in rows]


def _reply_history(msgs):
    """The chat as the model sees it: user turns, and Stella's replies and action results folded together."""
    out, pending = [], []

    def flush():
        if "".join(pending).strip():
            out.append({"role": "assistant", "content": "".join(pending).strip()})
        pending.clear()
    for m in msgs:
        if m["role"] == "user":
            flush()
            out.append({"role": "user", "content": REPLY_PHOTO.sub(r"[photo \1]", m["text"]).strip(), "_text": m["text"]})
        elif m["role"] == "assistant" and m["text"].strip():
            pending.append(m["text"] + "\n")
        elif m["role"] == "tool" and not m["text"].startswith("⇄"):
            pending.append("(action result: " + REPLY_DIAGRAM.sub("[diagram shown]", BUTTON_MARK.sub("", m["text"])).strip() + ")\n")
    flush()
    return out


def _reply_mode(mode_id):
    for m in BUILT_IN_MODES:
        if m["id"] == mode_id:
            return m
    for m in CHATS.modes():
        if m["id"] == mode_id:
            return {**m, "desc": "Your mode"}
    return BUILT_IN_MODES[0]


def _reply_conversation(conv_id):
    try:
        r = CHATS.db.execute("SELECT project_id, mode FROM conversations WHERE id=? AND deleted=0", (conv_id,)).fetchone()
    except sqlite3.Error:
        r = None
    return (r[0] or "", r[1] or "") if r else None


def _reply_system(client, name, project_text, summary, mode, memory):
    p = instructions_for(client).replace("{name}", name)
    if mode.get("prompt"):
        p += f"\n\n## Current mode: {mode['name']}\n{mode['prompt']}" + (f"\n{MODE_NOTE}" if MODE_NOTE else "")
    if memory:
        p += "\n\n" + memory
    if project_text:
        p += "\n\n" + project_text
    if summary:
        p += "\n\n## Earlier in this chat\n" + SUMMARY_INTRO + "\n" + summary
    return p


def _reply_prepare(run, mode, auto, n_images):
    d = run.data
    msgs = _reply_messages(run.conv)
    last = max((i for i, m in enumerate(msgs) if m["role"] == "summary"), default=-1)
    summary = msgs[last]["text"] if last >= 0 else ""
    history = _reply_history(msgs[last + 1:])
    users = [m["text"] for m in msgs if m["role"] == "user"]
    project_id = run.project_id
    client = run.client
    base = _reply_system(client, run.name, "", summary, mode, "")
    think_pref = mode.get("thinking") if mode.get("thinking") in ("on", "off") else (
        "auto" if d.get("voice") else str(d.get("thinking") or "auto"))
    plan = None
    try:
        plan = plan_context({
            "project_id": project_id, "query": "\n".join(REPLY_PHOTO.sub("", t) for t in users[-2:]), "fixed": base,
            "images": n_images, "mode": mode["id"], "mode_tools": mode.get("tools") or "all", "split": True,
            "sticky": sorted(REPLY_STICKY.get(run.conv, ())), "thinking": think_pref,
            "last": REPLY_PHOTO.sub("", users[-1]) if users else "", "chat_id": run.conv, "auto": auto,
            "messages": [{"role": h["role"], "content": h["content"]} for h in history]})
    except (ValueError, TypeError, AttributeError, OSError, urllib.error.URLError, KeyError) as e:
        log(f"reply: couldn't plan the context ({e})")
    start, info, refresh, names, project_text, memory, extra = 0, None, False, None, "", "", ""
    think = think_pref == "on"
    if plan:
        limit = plan["n_ctx"] - plan["reply_reserve"]
        used = plan["fixed_tokens"] + plan["project_tokens"] + plan["history_tokens"]
        info = {"used": used, "limit": limit}
        names = plan.get("tools")
        project_text = plan.get("project_context") or ""
        memory = plan.get("memory_context") or ""
        extra = plan.get("turn_context") or ""
        if "think" in plan:
            think = bool(plan["think"])
        if plan.get("drop"):
            drop = set(plan["drop"])
            history = [h for i, h in enumerate(history) if i not in drop]
        start = min(plan.get("keep_from") or 0, len(history))
        while start < len(history) and history[start]["role"] != "user":
            start += 1
        if start >= len(history):
            roles = [h["role"] for h in history]
            start = max(0, len(roles) - 1 - roles[::-1].index("user")) if "user" in roles else 0
        refresh = sum(1 for h in history if h["role"] == "user") >= 2 and (start > 0 or used > limit * 0.85)
    else:
        start = max(0, len(history) - 24)
    tools = [SERVER_TOOLS[n][1] for n in (names if names is not None else SERVER_TOOLS) if n in SERVER_TOOLS]
    tools += [t for t in run.client_tools if t.get("function", {}).get("name") not in SERVER_TOOLS]
    if d.get("voice"):
        extra = (extra + "\n" if extra else "") + VOICE_STYLE
        if not plan:
            think = False
    hist = history[start:]
    user_idx = [i for i, h in enumerate(hist) if h["role"] == "user"]
    if user_idx:
        i = user_idx[-1]
        block = (f"[Automatic context, not written by the user]\n{d.get('now_line') or ''}"
                 + (f"\n{extra.strip()}" if extra else "") + "\n[End of context]\n\n")
        hist[i] = {**hist[i], "content": block + hist[i]["content"]}
    return {"system": _reply_system(client, run.name, project_text, summary, mode, memory), "history": hist,
            "info": info, "refresh": refresh, "tools": tools, "mode": mode, "think": think}


def _reply_compact(run, keep_user_turns, style):
    """Folds the older part of the chat into a summary (the newest exchanges stay word for word)."""
    msgs = _reply_messages(run.conv)
    last = max((i for i, m in enumerate(msgs) if m["role"] == "summary"), default=-1)
    after = msgs[last + 1:]
    users = [i for i, m in enumerate(after) if m["role"] == "user"]
    k = min(keep_user_turns, len(users))
    while k > 1 and users[len(users) - k] == 0:
        k -= 1
    cut = len(after) if k == 0 else users[len(users) - k]
    part = after[:cut]
    if not any(m["role"] in ("user", "assistant") for m in part):
        return False
    summary = summarize_chat(msgs[last]["text"] if last >= 0 else "",
                             [{"role": m["role"], "content": REPLY_DIAGRAM.sub("[diagram]", REPLY_PHOTO.sub("[photo]", m["text"]))}
                              for m in part], "story" if style == "story" else "work")
    if not summary.strip():
        return False
    if cut < len(after):
        nxt, prev = after[cut]["created_at"], part[-1]["created_at"]
        created = (prev + nxt) // 2 if nxt - prev >= 2 else nxt - 1
    else:
        created = None
    _reply_save(run, "summary", summary.strip(), created=created)
    return True


def _reply_stream(run, body):
    """One model call, streamed: {content, reasoning, tool_calls, finish}. Sends delta events as it goes."""
    req = urllib.request.Request(LLAMA_URL.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "Authorization": f"Bearer {API_KEY}"})
    res = {"content": "", "reasoning": "", "tool_calls": [], "finish": ""}
    calls = {}
    last_sent = 0.0
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            if run.stop.is_set():
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                j = json.loads(data)
            except ValueError:
                continue
            if j.get("error"):
                raise RuntimeError((j["error"] or {}).get("message") if isinstance(j["error"], dict) else str(j["error"]))
            ch = (j.get("choices") or [{}])[0]
            if ch.get("finish_reason"):
                res["finish"] = ch["finish_reason"]
            delta = ch.get("delta") or {}
            res["content"] += delta.get("content") or ""
            res["reasoning"] += delta.get("reasoning_content") or ""
            for tc in delta.get("tool_calls") or []:
                c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                c["id"] = tc.get("id") or c["id"]
                fn = tc.get("function") or {}
                c["name"] += fn.get("name") or ""
                c["arguments"] += fn.get("arguments") or ""
            if time.time() - last_sent > 0.08:
                last_sent = time.time()
                run.emit({"type": "delta", "text": REPLY_THINK.sub("", res["content"]).lstrip(), "reasoning": res["reasoning"]})
    res["tool_calls"] = [{**c, "id": c["id"] or f"call_{i}"} for i, c in sorted(calls.items())]
    return res


def _reply_photos(ids):
    out = []
    for pid in ids:
        if not re.fullmatch(r"[0-9a-f]{16}", str(pid)) or not IMAGE_DIR:
            continue
        path = os.path.join(IMAGE_DIR, f"{pid}.jpg")
        if os.path.exists(path):
            with open(path, "rb") as f:
                out.append("data:image/jpeg;base64," + base64.b64encode(f.read()).decode())
    return out


def _reply_tool_line(name, result, summary):
    line = ("✓ " if result.get("ok") else "✗ ") + (summary or name)
    if result.get("ok") and result.get("image"):
        line += f"\n![{str(result.get('title') or 'Diagram').replace(']', ')')}](diagram:{result['image']})"
    return line


def _reply_auto(run, fresh):
    """Auto mode: which mode this message goes to (None = Auto mode isn't used in this chat)."""
    d = run.data
    if run.chat_mode not in ("", "general") or str(d.get("auto_level") or "auto") == "off":
        return None
    forced = str(d.get("forced") or "")
    msgs = _reply_messages(run.conv)
    text = next((REPLY_PHOTO.sub("", m["text"]).strip() for m in reversed(msgs) if m["role"] == "user"), "")
    if not fresh and not forced:
        lanes = _auto_db().get("chats", {}).get(run.conv, {}).get("lanes", [])
        return _reply_mode(lanes[-1]["mode"] if lanes else "general")
    r = route_mode({"chat_id": run.conv, "text": text, "level": d.get("auto_level") or "auto", "forced": forced})
    run.emit({"type": "mode", "mode": r["mode"]})
    if r.get("switched") or forced:
        _reply_save(run, "tool", "⇄ Auto: " + _reply_mode(r["mode"])["name"] + (f" ({r['reason']})" if r.get("reason") else ""))
    elif r.get("suggest"):
        _reply_save(run, "tool", f"⇄ Suggest: {r.get('suggest_name')} (pick it below to redo)")
    return _reply_mode(r["mode"])


def _reply_quick(run):
    """Instant answers (the time, sums, reminders, timers) without the model. True if it answered."""
    msgs = _reply_messages(run.conv)
    text = next((m["text"].strip() for m in reversed(msgs) if m["role"] == "user"), "")
    if not text or len(text) > 140 or "\n" in text:
        return False
    try:
        hit = quick_reply(text, str(run.data.get("now") or ""), run.client == "phone")
    except (ValueError, TypeError, AttributeError, OverflowError, KeyError) as e:
        log(f"quick answer failed: {e}")
        return False
    if not hit or not hit.get("reply"):
        return False
    reply = hit["reply"]
    if hit.get("changed"):
        run.emit({"type": "changed", "what": hit["changed"]})
    tool = hit.get("phone_tool")
    if tool and run.client == "phone":
        ans = run.ask("client_tool", {"name": tool.get("name"), "arguments": json.dumps(tool.get("args") or {})}) or {}
        line = str(ans.get("summary") or "✗ The phone didn't answer")
        _reply_save(run, "tool", line)
        if line.startswith("✗"):
            reply = "Sorry, that didn't work: " + BUTTON_MARK.sub("", line[1:]).strip()
    _reply_save(run, "assistant", reply)
    return True


def run_reply(run):
    d = run.data
    conv = _reply_conversation(run.conv)
    if conv is None:
        raise ValueError("this chat isn't on the server yet; sync first")
    run.project_id, run.chat_mode = conv
    run.client = "phone" if d.get("client") == "phone" else "web"
    run.name = " ".join(str(d.get("name") or "Stella").split())[:24] or "Stella"
    run.client_tools = [t for t in (d.get("client_tools") or []) if isinstance(t, dict)][:60]
    client_names = {t.get("function", {}).get("name") for t in run.client_tools}
    voice, fresh = bool(d.get("voice")), bool(d.get("fresh"))
    photos = [p for p in (d.get("photos") or [])][:8]
    if fresh and not photos and (run.chat_mode or "general") in REPLY_QUICK_MODES and _reply_quick(run):
        return
    amode = _reply_auto(run, fresh)
    mode = amode or _reply_mode(run.chat_mode or "general")
    auto = amode is not None
    p = _reply_prepare(run, mode, auto, len(photos))
    if p["refresh"] and not run.stop.is_set():
        run.emit({"type": "status", "text": "Refreshing context…"})
        try:
            if _reply_compact(run, 2, mode.get("style") or "work"):
                p = _reply_prepare(run, mode, auto, len(photos))
        except (RuntimeError, ValueError, KeyError, IndexError, OSError, urllib.error.URLError) as e:
            log(f"reply: couldn't refresh the context ({e})")
    if p["info"]:
        run.emit({"type": "context", **p["info"]})
    REPLY_STICKY.setdefault(run.conv, set()).update(t["function"]["name"] for t in p["tools"])
    hist = [{"role": h["role"], "content": h["content"]} for h in p["history"]]
    images = _reply_photos(photos)
    users = [i for i, h in enumerate(p["history"]) if h["role"] == "user"]
    if images and users:   # the newest message with photos goes to the model as pictures
        i = users[-1]
        raw = p["history"][i]
        block = (re.match(r"^\[Automatic context[\s\S]*?\[End of context\]\n\n", raw["content"]) or [""])[0] if \
            isinstance(raw["content"], str) else ""
        text = (REPLY_PHOTO.sub("", raw["_text"]).strip() or "(The user sent a photo.)") + " " + \
            " ".join(f"[photo {x}]" for x in REPLY_PHOTO.findall(raw["_text"]))
        hist[i] = {"role": "user", "content": [{"type": "text", "text": block + text.strip()}] +
                   [{"type": "image_url", "image_url": {"url": u}} for u in images]}
    turn, continued = [], 0
    for _ in range(REPLY_MAX_ROUNDS):
        if run.stop.is_set():
            return
        mode, think = p["mode"], p["think"]
        code = mode["id"] in REPLY_CODE_MODES
        body = {"model": "local", "stream": True, "messages": [{"role": "system", "content": p["system"]}, *hist, *turn],
                "tools": p["tools"], "chat_template_kwargs": {"enable_thinking": think},
                "temperature": mode["temperature"] if mode.get("temperature") is not None else (
                    (0.6 if code else 1.0) if think else 0.7),
                "top_p": 0.95 if think else 0.8, "top_k": 20, "min_p": 0, "presence_penalty": 0 if code else 1.5}
        if voice and not think:
            body["max_tokens"] = 700
        if not p["tools"]:
            body.pop("tools")
        run.emit({"type": "status", "text": "Contacting the model…"})
        res = _reply_stream(run, body)
        text = REPLY_THINK.sub("", res["content"]).strip()
        if run.stop.is_set():
            if text:
                _reply_save(run, "assistant", text, res["reasoning"])
            return
        if text or not res["tool_calls"]:
            _reply_save(run, "assistant", text or "(no reply)", res["reasoning"])
        assistant = {"role": "assistant", "content": text}
        if res["tool_calls"]:
            assistant["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": c["arguments"]}}
                                       for c in res["tool_calls"]]
        turn.append(assistant)
        if not res["tool_calls"]:
            if res["finish"] == "length" and text and continued < REPLY_MAX_CONTINUES:
                continued += 1
                _reply_save(run, "tool", "↪ Long reply: making room and continuing")
                run.emit({"type": "status", "text": "Continuing…"})
                try:
                    _reply_compact(run, 1, mode.get("style") or "work")
                except (RuntimeError, ValueError, KeyError, IndexError, OSError, urllib.error.URLError):
                    pass
                p = _reply_prepare(run, mode, auto, 0)
                hist = [{"role": h["role"], "content": h["content"]} for h in p["history"]]
                turn = [{"role": "user", "content": REPLY_CONTINUE}]
                continue
            return
        for tc in res["tool_calls"]:
            if run.stop.is_set():
                return
            try:
                args = json.loads(tc["arguments"] or "{}")
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}
            args.pop("approved", None)
            name = tc["name"]
            result_text = None
            if name in SERVER_TOOLS:
                ok_to_run = True
                if name in APPROVAL_TOOLS:
                    ans = run.ask("approval", {"name": name, "args": args}) or {}
                    ok_to_run = bool(ans.get("approved"))
                    if ok_to_run:
                        args["approved"] = True
                if not ok_to_run:
                    line = "✗ You declined: " + name.replace("github_", "GitHub ")
                    result_text = json.dumps({"ok": False, "error": "The user declined this action."})
                else:
                    if name in ("project_note", "project_search") and run.project_id:
                        args["project_id"] = run.project_id
                    if name == "search_chats":
                        args["current_conversation"] = run.conv
                    args.setdefault("conversation_id", run.conv)
                    if p["info"]:
                        left = p["info"]["limit"] - p["info"]["used"] - len(json.dumps(turn)) // 3
                        args["_max_chars"] = max(1500, int(left * 3 * 0.6))
                    else:
                        args["_max_chars"] = 12000
                    run.emit({"type": "tool", "name": name})
                    result, summary = run_server_tool(name, args)
                    line = _reply_tool_line(name, result, summary)
                    result_text = json.dumps(result)
            elif name in client_names:
                run.emit({"type": "tool", "name": name})
                ans = run.ask("client_tool", {"name": name, "arguments": json.dumps(args)}) or {}
                line = str(ans.get("summary") or "✗ The app didn't answer")
                result_text = str(ans.get("result") or json.dumps({"ok": False, "error": "The app didn't answer."}))
            else:
                where = "on the desktop" if run.client == "web" else "here"
                line = f"✗ {name} isn't available {where}"
                result_text = json.dumps({"ok": False, "error": f"{name} isn't available in this app."})
            switched = None
            if name == "switch_mode" and auto:
                try:
                    switched = json.loads(result_text).get("switched_to")
                except (ValueError, AttributeError):
                    switched = None
            if switched:   # Stella switched herself: her next step uses that mode's instructions and tools
                amode = _reply_mode(switched)
                run.emit({"type": "mode", "mode": switched})
                _reply_save(run, "tool", f"⇄ Auto: {amode['name']} (Stella switched)")
                p = _reply_prepare(run, amode, True, 0)
                hist = [{"role": h["role"], "content": h["content"]} for h in p["history"]]
            else:
                _reply_save(run, "tool", line)
            REPLY_STICKY.setdefault(run.conv, set()).add(name)
            turn.append({"role": "tool", "tool_call_id": tc["id"], "name": name, "content": result_text})


def start_reply(conv_id, data, send):
    """Runs one reply, sending events through send(dict). Always ends with a done event."""
    run = ReplyRun(conv_id, data if isinstance(data, dict) else {}, send)
    REPLY_RUNS[run.id] = run
    run.emit({"type": "run", "run_id": run.id})
    error = None
    with ACTIVITY["lock"]:
        ACTIVITY["active"] += 1
    try:
        run_reply(run)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error")
            detail = detail.get("message") if isinstance(detail, dict) else detail
        except ValueError:
            detail = None
        error = str(detail or f"the model answered HTTP {e.code}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        error = f"the AI model isn't running ({getattr(e, 'reason', e)})"
    except (ValueError, RuntimeError, KeyError, TypeError, OSError, sqlite3.Error) as e:
        error = str(e) or type(e).__name__
        log(f"reply failed: {e}")
    finally:
        with ACTIVITY["lock"]:
            ACTIVITY["active"] -= 1
            ACTIVITY["last"] = time.time()
        REPLY_RUNS.pop(run.id, None)
    run.emit({"type": "done", "error": error, "stopped": run.stop.is_set()})
    return run
