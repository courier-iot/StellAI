# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Saved chat memory: going back to a long chat answers straight away -------------------------------------------
# Before answering, the model reads the whole chat (thousands of tokens: on a CPU or a small card that's the slow part).
# llama-server keeps what it read for the chat it last answered, but switching chats throws that away. With
# --slot-save-path it can save that work to disk and load it back: so when you switch chats, the hub saves the chat
# being left and restores the one you're going back to. A model started without the option just skips this.
SLOTS = {"current": "", "off_until": 0.0, "lock": threading.Lock(), "dir": ""}
SLOT_KEEP = 12                  # chats kept on disk (newest)
SLOT_MIN_TOKENS = 1500          # shorter chats re-read in a moment anyway


def _slot_call(action, filename):
    return _llama(f"/slots/0?action={action}", {"filename": filename}, timeout=120)


def _slot_name(conv_id):
    return "stellai-" + re.sub(r"[^A-Za-z0-9_-]", "", conv_id)[:60] + ".bin"


def slot_switch(conv_id, used_tokens=0):
    """Call before a reply on the everyday model: saves the chat that was loaded, restores this one if saved.
    True when the request should go to slot 0 (the one being saved and restored)."""
    if time.time() < SLOTS["off_until"]:
        return False
    with SLOTS["lock"]:
        prev = SLOTS["current"]
        if prev == conv_id:
            return True
        try:
            if prev and SLOTS.get("prev_tokens", 0) >= SLOT_MIN_TOKENS:
                _slot_call("save", _slot_name(prev))
                SLOTS.setdefault("saved", {})[prev] = time.time()
            if conv_id in SLOTS.get("saved", {}) or (SLOTS["dir"] and os.path.isfile(os.path.join(SLOTS["dir"], _slot_name(conv_id)))):
                _slot_call("restore", _slot_name(conv_id))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 501):   # this model wasn't started with --slot-save-path: try again in an hour
                SLOTS["off_until"] = time.time() + 3600
                SLOTS["current"] = ""
                return False
            log(f"chat memory: {e}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"chat memory: {e}")
        SLOTS["current"] = conv_id
        SLOTS["prev_tokens"] = used_tokens
        _slot_tidy()
        return True


def _slot_tidy():
    """Keeps the newest SLOT_KEEP saved chats on disk."""
    saved = SLOTS.setdefault("saved", {})
    for old in sorted(saved, key=saved.get)[:-SLOT_KEEP]:
        saved.pop(old, None)
    d = SLOTS["dir"]
    if not d or not os.path.isdir(d):
        return
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.startswith("stellai-") and f.endswith(".bin")),
                       key=os.path.getmtime)
        for f in files[:-SLOT_KEEP]:
            os.remove(f)
    except OSError:
        pass


def slot_done(conv_id, used_tokens):
    """After a reply: how much of this chat the model holds (decides whether it's worth saving later)."""
    if SLOTS["current"] == conv_id:
        SLOTS["prev_tokens"] = used_tokens
