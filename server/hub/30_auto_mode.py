# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
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


