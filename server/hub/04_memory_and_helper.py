# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
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


# The phone keeps a Cancel/Undo button at the end of an action line; it's not part of what happened
BUTTON_MARK = re.compile(r"\n?\[\[button:[^\]\n]*\]\]\s*$")


def summarize_chat(previous, messages, style="work"):
    """Rolling summary: long chats are summarized in pieces that fit the model's context."""
    n_ctx = context_size()
    piece_budget = max(1000, int(n_ctx * 0.5) - count_tokens(SUMMARY_PROMPT) - count_tokens(previous) - 800)
    lines = []
    for m in messages:
        role = str(m.get("role", "user"))
        text = BUTTON_MARK.sub("", str(m.get("content", ""))).strip()
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


