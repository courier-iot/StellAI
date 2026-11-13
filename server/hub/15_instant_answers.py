# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
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


