# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Working habits for server-run replies --------------------------------------------------------------------------
# Small local models do much better with a few firm rules around them than with longer instructions:
#  · tools on demand: only the tools this message needs go with it, plus load_tools to fetch a group by name, so a
#    chat that touched CAD, Blender and KiCad doesn't carry all three (they cost thousands of tokens of context)
#  · checked tool calls: arguments are parsed, repaired and checked against the tool's own schema before it runs, and
#    a call that can't be fixed goes back to her with the exact problem instead of running half-filled
#  · ask, then wait: a reply that asks the user something ends the turn, even if she also tried to call tools
#  · plan, act, check: multi-step work gets a short plan in the chat that ticks off as she goes, failed steps come
#    back with a "fix the cause, retry once" note, and the same failure twice tells her to stop and explain
TOOL_GROUP_LABELS = {   # what load_tools offers, in the words the model sees
    "electronics": "electronics calculators and diagrams",
    "lists": "managing reminders, lists and memories (list, cancel, tick off, forget)",
    "github": "the user's GitHub repositories (read code, builds, commits)",
    "code": "run_python (run code, plots, data)",
    "blender": "3D Modeler in Blender",
    "cad": "CAD Designer in FreeCAD (parts for 3D printing)",
    "pcb": "PCB Designer in KiCad",
    "history": "searching earlier chats",
    "skills": "saving a new skill",
}


def load_tools_def(loaded_names):
    """load_tools, listing only the groups that aren't fully offered yet (None when every group is)."""
    missing = [g for g in TOOL_GROUP_LABELS if g in TOOL_GROUPS
               and any(n in SERVER_TOOLS and n not in loaded_names for n in TOOL_GROUPS[g])]
    if not missing:
        return None
    return _fn("load_tools",
               "Load more tools when the task needs ones you don't have yet: " +
               "; ".join(f"{g} ({TOOL_GROUP_LABELS[g]})" for g in missing) + ". They're usable from your next step.",
               {"groups": {"type": "array", "items": {"type": "string", "enum": [g for g in TOOL_GROUP_LABELS if g in TOOL_GROUPS]},
                           "description": "Groups to load"}},
               ["groups"])


def load_tool_groups(args):
    groups = args.get("groups")
    if isinstance(groups, str):
        groups = [g for g in re.split(r"[,\s]+", groups) if g]
    names, unknown = [], []
    for g in groups or []:
        g = str(g).strip().lower()
        if g in TOOL_GROUPS:
            names += [n for n in TOOL_GROUPS[g] if n in SERVER_TOOLS and n not in names]
        else:
            unknown.append(g)
    return names, unknown


# ---- Checking tool arguments -------------------------------------------------------------------------------------
def parse_tool_args(raw):
    """The model's argument text as a dict, repairing the usual slips. (args, error or None)."""
    raw = (raw or "").strip()
    if not raw:
        return {}, None
    tries = [raw]
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    tries.append(fenced)
    tries.append(re.sub(r",\s*([}\]])", r"\1", fenced))           # trailing commas
    for t in tries:
        try:
            v = json.loads(t)
            return (v, None) if isinstance(v, dict) else ({}, "the arguments must be a JSON object")
        except ValueError:
            pass
    try:   # Python-style dicts: single quotes, True/False/None
        import ast
        v = ast.literal_eval(fenced)
        if isinstance(v, dict):
            return v, None
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    return {}, "the arguments weren't valid JSON"


def _coerce(value, prop):
    """(value, ok): fits one argument to its schema type where the meaning is clear ("3" → 3, "a, b" → [a, b])."""
    t = prop.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t is None:
        return value, True
    if t == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value), True
        if isinstance(value, str):
            enum = prop.get("enum")
            if enum and value not in enum:
                low = {str(e).lower(): e for e in enum}
                return (low[value.strip().lower()], True) if value.strip().lower() in low else (value, False)
            return value, True
        return value, False
    if t in ("integer", "number"):
        if isinstance(value, bool):
            return value, False
        if isinstance(value, (int, float)):
            return (int(value) if t == "integer" and float(value).is_integer() else value), \
                not (t == "integer" and not float(value).is_integer())
        if isinstance(value, str):
            try:
                f = float(value.strip())
            except ValueError:
                return value, False
            if t == "integer":
                return (int(f), True) if f.is_integer() else (value, False)
            return f, True
        return value, False
    if t == "boolean":
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
            return value.strip().lower() in ("true", "yes"), True
        if value in (0, 1):
            return bool(value), True
        return value, False
    if t == "array":
        if isinstance(value, list):
            items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
            out = []
            for x in value:
                cx, ok = _coerce(x, items)
                if not ok:
                    return value, False
                out.append(cx)
            return out, True
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("["):
                try:
                    v = json.loads(s)
                    if isinstance(v, list):
                        return _coerce(v, prop)
                except ValueError:
                    pass
            parts = [p.strip() for p in s.split(",")] if "," in s else [s]
            return _coerce([p for p in parts if p], prop)
        return _coerce([value], prop)
    if t == "object":
        if isinstance(value, dict):
            return value, True
        if isinstance(value, str):
            try:
                v = json.loads(value)
                return (v, True) if isinstance(v, dict) else (value, False)
            except ValueError:
                return value, False
        return value, False
    return value, True


def check_tool_args(tool_def, args):
    """(fixed args, problem or None) against a tool's own definition: required arguments, types, choices."""
    params = ((tool_def or {}).get("function") or {}).get("parameters") or {}
    props = params.get("properties") or {}
    problems, out = [], dict(args)
    for key in list(out):
        if key not in props and not key.startswith("_") and key not in ("approved", "conversation_id"):
            close = [p for p in props if p.lower() == key.lower() or p.replace("_", "") == key.replace("_", "").lower()]
            if close:
                out[close[0]] = out.pop(key)
    for key, prop in props.items():
        if key not in out or out[key] is None:
            continue
        v, ok = _coerce(out[key], prop)
        if ok:
            out[key] = v
        else:
            want = prop.get("type", "value")
            if prop.get("enum"):
                want = "one of " + ", ".join(map(str, prop["enum"]))
            problems.append(f"{key} should be {want}")
    for key in params.get("required") or []:
        if key not in out or out[key] is None or (isinstance(out[key], str) and not out[key].strip()):
            hint = (props.get(key) or {}).get("description")
            problems.append(f"{key} is missing" + (f" ({hint})" if hint else ""))
    return out, ("; ".join(problems) if problems else None)


# ---- Ask, then wait ----------------------------------------------------------------------------------------------
_ASKS = re.compile(r"\b(would you like|do you want|should i|shall i|want me to|which (one|option|would|do you)|"
                   r"what (size|dimensions|kind|type|would you)|can you (tell|confirm|send|share)|could you "
                   r"(tell|confirm|send|share)|let me know|are you (ok|okay|sure|happy)|is that (ok|okay|right)|"
                   r"or would you rather|please confirm|before i (start|go|continue))\b", re.I)


def asks_user(text):
    """True when a reply ends by asking the user something, so it has to wait for the answer."""
    text = REPLY_THINK.sub("", text or "").strip()
    text = re.sub(r"```[\s\S]*?```", "", text).strip()
    if not text:
        return False
    tail = [ln.strip() for ln in text.splitlines() if ln.strip()][-3:]
    last = tail[-1].rstrip("*_ )\"'")
    if last.endswith("?"):
        return True
    return bool(_ASKS.search(" ".join(tail[-2:])))


# ---- Plan, act, check -------------------------------------------------------------------------------------------
PLAN_MODES = {"cad", "modeler", "pcb", "programmer", "electronics"}
PLAN_HINT = ("This looks like several steps of work. Before your first tool call, call update_plan with 3-6 short "
             "steps; then work through them, marking each done with update_plan as you finish it. Check each "
             "step's result before the next. When everything is done, check the result against what the user asked.")
RETRY_NOTE = ("This step failed. Read the error, fix its cause and try once more. If it fails the same way again, "
              "stop and tell the user what went wrong and what you'd try next.")
ESCALATE_NOTE = ("This failed twice the same way, so a bigger model now takes the next step: look at the cause again "
                 "from the start, fix it and retry.")
STOP_NOTE = ("This failed again the same way. Don't try it again: tell the user what went wrong and ask how to go on.")
PLAN_LIMIT = 8


def wants_plan(mode_id, text):
    """Multi-step work: a design mode, or a message that asks for several things or a build."""
    t = text or ""
    return (mode_id in PLAN_MODES and len(t) > 40) or bool(
        re.search(r"\b(step by step|build (me )?(a|an|the)|design (me )?(a|an|the)|make (me )?(a|an) .{3,40}(that|with)|"
                  r"set up|then\b.*\bthen\b)", t, re.I)) or len(re.findall(r"^\s*(\d+[.)]|[-*])\s", t, re.M)) >= 3


def plan_text(steps):
    done = sum(1 for s in steps if s["done"])
    head = f"☰ Plan: {done} of {len(steps)} done" if done < len(steps) else f"☰ Plan: all {len(steps)} done"
    return head + "\n" + "\n".join(f"{i + 1}. {s['text']}" + (" (done)" if s["done"] else "") for i, s in enumerate(steps))


def update_plan(run, args):
    """update_plan: sets the steps (the first call) and ticks them off. One plan message per reply, edited in place."""
    plan = getattr(run, "plan", None) or {"steps": [], "msg": None}
    steps = args.get("steps")
    if isinstance(steps, str):
        steps = [s for s in re.split(r"\n|;\s*", steps) if s.strip()]
    if isinstance(steps, list) and steps:
        new = []
        for s in steps[:PLAN_LIMIT]:
            text = s.get("text") if isinstance(s, dict) else s
            text = re.sub(r"^\s*(\d+[.)]|[-*])\s*", "", " ".join(str(text or "").split()))[:120]
            if text:
                old = next((x for x in plan["steps"] if x["text"].lower() == text.lower()), None)
                new.append({"text": text, "done": bool(isinstance(s, dict) and s.get("done")) or bool(old and old["done"])})
        if new:
            plan["steps"] = new
    for k in args.get("done") or []:
        try:
            i = int(k) - 1
        except (TypeError, ValueError):
            i = next((j for j, x in enumerate(plan["steps"]) if str(k).lower() in x["text"].lower()), -1)
        if 0 <= i < len(plan["steps"]):
            plan["steps"][i]["done"] = True
    if not plan["steps"]:
        raise ValueError("give the steps, e.g. steps=['Base plate 120x80x4 mm', 'Four M3 holes', 'Fillet the corners']")
    text = plan_text(plan["steps"])
    ts = _reply_ts(run.conv)
    if plan["msg"]:
        m = {**plan["msg"], "text": text, "updated_at": ts}
    else:
        m = {"id": str(uuid.uuid4()), "conv_id": run.conv, "role": "tool", "text": text, "reasoning": "",
             "created_at": ts, "updated_at": ts, "deleted": False}
    CHATS.sync(0, [], [m])
    run.emit({"type": "message", "message": m})
    plan["msg"] = m
    run.plan = plan
    left = [f"{i + 1}. {s['text']}" for i, s in enumerate(plan["steps"]) if not s["done"]]
    return {"ok": True, "plan": [f"{i + 1}. {s['text']}" + (" (done)" if s["done"] else "") for i, s in
                                 enumerate(plan["steps"])],
            "next": left[0] if left else "All steps done: check the result against the request, then answer."}


UPDATE_PLAN_DEF = _fn(
    "update_plan",
    "Your plan for multi-step work, shown to the user as a checklist. First call: steps (3-6 short steps). Then "
    "done=[step numbers] as you finish them.",
    {"steps": {"type": "array", "items": {"type": "string"}, "description": "The steps (only to set or change the plan)"},
     "done": {"type": "array", "items": {"type": "integer"}, "description": "Step numbers you just finished"}}, [])


def failure_note(run, name, result):
    """A note added to a failed tool result: retry once with the cause fixed, then stop."""
    fails = getattr(run, "fails", None)
    if fails is None:
        fails = run.fails = {}
    key = name + ":" + re.sub(r"\d+", "#", str(result.get("error") or ""))[:80]
    fails[key] = fails.get(key, 0) + 1
    return STOP_NOTE if fails[key] >= 2 else RETRY_NOTE
