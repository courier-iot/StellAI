# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ----------------------------------------------------------------------------- HTTP API
class Prefix(str):
    """A route pattern: any path that starts with this."""


def R(pattern):
    """A route pattern: a regular expression the whole path must match (its groups reach the handler as m)."""
    return re.compile(pattern)


class Routes:
    """The hub's web addresses, in order. find() gives the first handler whose pattern matches."""

    def __init__(self):
        self.table = []    # (method, pattern, access, handler)

    def add(self, method, *patterns, access="key"):
        def register(fn):
            for p in patterns:
                self.table.append((method, p, access, fn))
            return fn
        return register

    def find(self, method, path, access):
        for meth, pat, acc, fn in self.table:
            if meth != method or acc != access:
                continue
            if isinstance(pat, Prefix):
                if path.startswith(pat):
                    return fn, None
            elif isinstance(pat, str):
                if path == pat:
                    return fn, None
            else:
                m = pat.fullmatch(path)
                if m:
                    return fn, m
        return None, None

    def describe(self):
        """[(method, path pattern, access)] for the API list (python3 stellai_hub.py --list-routes)."""
        out = []
        for meth, pat, acc, fn in self.table:
            shown = pat + "…" if isinstance(pat, Prefix) else pat if isinstance(pat, str) else pat.pattern
            out.append((meth, shown, {"first": "always", "open": "no key", "key": "API key"}[acc]))
        return out


def routes_markdown():
    """The route table as a Markdown list, for docs/api.md."""
    handler = make_http_handler(argparse.Namespace(), None)
    rows = ["| Method | Path | Access |", "|---|---|---|"]
    rows += [f"| {m} | `{p}` | {a} |" for m, p, a in handler.routes.describe()]
    return "\n".join(rows)


def make_http_handler(cfg, chats):
    routes = Routes()

    # ---- Every web address the hub answers, in the order they're checked. --------------------------------------
    # access="first": answered even after Stella moved away; "open": no API key needed (pages, the avatar's
    # files); otherwise the API key is required. Each handler gets the request, the path and the regex match.

    @routes.add("POST", "/move/receive", access="first")
    def post_move_receive(self, path, m):
        return receive_move(self, cfg)

    @routes.add("POST", "/transcribe")
    def post_transcribe(self, path, m):
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

    @routes.add("POST", "/tts")
    def post_tts(self, path, m):
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

    @routes.add("POST", "/avatar/event")
    def post_avatar_event(self, path, m):
        try:
            seq = AVATAR_BUS.post(json.loads(self.read_body(100_000) or b"{}"))
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(200, {"seq": seq})

    @routes.add("POST", R(r"/avatar/anims/([0-9a-f]{12})"))
    def post_root(self, path, m):
        try:
            return self.send_json(200, {"animations": update_animation(
                m.group(1), json.loads(self.read_body(10_000) or b"{}"))})
        except (KeyError, ValueError) as e:
            return self.send_json(404, {"error": str(e)})

    @routes.add("POST", "/avatar/config")
    def post_avatar_config(self, path, m):
        try:
            return self.send_json(200, save_avatar_config(json.loads(self.read_body(100_000) or b"{}")))
        except (ValueError, TypeError, OSError) as e:
            return self.send_json(400, {"error": str(e)})

    @routes.add("POST", "/sync")
    def post_sync(self, path, m):
        body = self.read_body(20_000_000)
        try:
            req = json.loads(body or b"{}")
            result = chats.sync(int(req.get("since", 0)),
                                req.get("conversations") or [], req.get("messages") or [])
        except (ValueError, TypeError, AttributeError) as e:
            return self.send_json(400, {"error": f"bad sync request: {e}"})
        return self.send_json(200, result)

    @routes.add("POST", R(r"/chats/([^/]{1,64})/notes"))   # pin a note to a chat, or edit one ({"id", "text"})
    def post_notes(self, path, m):
        try:
            data = json.loads(self.read_body(10_000) or b"{}")
            note = (chats.update_chat_note(m.group(1), data["id"], data.get("text", "")) if data.get("id")
                    else chats.add_chat_note(m.group(1), data.get("text", ""), "user"))
        except (ValueError, KeyError, TypeError) as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(200, {"note": note, "notes": chats.chat_notes(m.group(1))})

    @routes.add("POST", "/memories")
    def post_memories(self, path, m):
        try:
            data = json.loads(self.read_body(10_000) or b"{}")
            mid, new = chats.add_memory(data.get("text", ""), "user")
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(200, {"id": mid, "new": new})

    @routes.add("POST", R(r"/memories/suggestions/([0-9a-f]{1,12})"))
    def post_root_2(self, path, m):
        try:
            data = json.loads(self.read_body(10_000) or b"{}")
            chats.resolve_suggestion(m.group(1), bool(data.get("accept")), data.get("text"))
        except (ValueError, TypeError) as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(200, {"suggestions": chats.memory_suggestions(), "memories": chats.memories()})

    @routes.add("POST", "/memories/settings")
    def post_memories_settings(self, path, m):
        try:
            return self.send_json(200, save_memory_settings(json.loads(self.read_body(1000) or b"{}")))
        except (ValueError, TypeError) as e:
            return self.send_json(400, {"error": str(e)})

    @routes.add("POST", R(r"/chats/([^/]{1,64})/docs"))   # the user starts a document beside a chat
    def post_docs(self, path, m):
        try:
            data = json.loads(self.read_body(2_000_000) or b"{}")
            lang = str(data.get("language", "")).lower()
            did = chats.create_doc(m.group(1), data.get("title", ""), data.get("text", ""),
                                   "code" if lang and lang not in ("markdown", "md", "text") else "text", lang, "user")
        except (ValueError, TypeError) as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(200, chats.doc(did))

    @routes.add("POST", R(r"/docs/([0-9a-f]{10})"))   # the user's own edit from the side panel (base_version guards against overwriting Stella's)
    def post_root_3(self, path, m):
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

    @routes.add("POST", R(r"/chats/([^/]{1,64})/continue"))   # a new chat that starts from this one's summary and pinned notes
    def post_continue(self, path, m):
        try:
            return self.send_json(200, continue_in_new_chat(m.group(1)))
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except (urllib.error.URLError, OSError, RuntimeError) as e:
            return self.send_json(503, {"error": f"couldn't summarize the chat: {e}"})

    @routes.add("POST", "/memories/learn")
    def post_memories_learn(self, path, m):
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

    @routes.add("POST", "/llm/v1/chat/completions")
    def post_llm_v1_chat_completions(self, path, m):
        return self.proxy_llm()

    @routes.add("POST", "/quick")
    def post_quick(self, path, m):
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

    @routes.add("POST", "/speedtest")
    def post_speedtest(self, path, m):
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

    @routes.add("POST", "/route")
    def post_route(self, path, m):
        try:
            data = json.loads(self.read_body(200_000) or b"{}")
            return self.send_json(200, route_mode(data))
        except (ValueError, TypeError, AttributeError) as e:
            return self.send_json(400, {"error": f"bad request: {e}"})

    @routes.add("POST", "/context")
    def post_context(self, path, m):
        try:
            data = json.loads(self.read_body(5_000_000) or b"{}")
            return self.send_json(200, plan_context(data))
        except (ValueError, TypeError, AttributeError) as e:
            return self.send_json(400, {"error": f"bad request: {e}"})

    @routes.add("POST", "/summarize")
    def post_summarize(self, path, m):
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

    @routes.add("POST", "/backups", "/backups/config", "/server/model", "/server/download")
    def post_backups(self, path, m):
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

    @routes.add("POST", "/reminders", Prefix("/reminders/"), Prefix("/lists/"), "/github")
    def post_reminders(self, path, m):
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

    @routes.add("POST", "/modes")
    def post_modes(self, path, m):
        try:
            data = json.loads(self.read_body(100_000) or b"{}")
            return self.send_json(200, chats.save_mode(data))
        except (ValueError, TypeError, AttributeError) as e:
            return self.send_json(400, {"error": str(e)})

    @routes.add("POST", "/projects")
    def post_projects(self, path, m):
        try:
            data = json.loads(self.read_body(100_000) or b"{}")
            return self.send_json(200, chats.save_project(data))
        except (ValueError, TypeError, AttributeError) as e:
            return self.send_json(400, {"error": str(e)})

    @routes.add("POST", R(r"/projects/([0-9a-f]{12})/notes"))
    def post_notes_2(self, path, m):
        try:
            data = json.loads(self.read_body(20_000) or b"{}")
            return self.send_json(200, chats.add_project_note(m.group(1), data.get("text", ""), "user"))
        except (ValueError, TypeError, AttributeError) as e:
            return self.send_json(400, {"error": str(e)})

    @routes.add("POST", Prefix("/tools/"))
    def post_root_4(self, path, m):
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

    @routes.add("PUT", R(r"/projects/(library|[0-9a-f]{12})/files"))
    def put_files(self, path, m):
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

    @routes.add("PUT", "/avatar/anims")
    def put_avatar_anims(self, path, m):
        q = parse_qs(urlparse(self.path).query)
        try:
            items = add_animation(self.read_body(60_000_000) or b"", q.get("name", [""])[0],
                                  q.get("when", ["idle"])[0], q.get("type", [""])[0].lower())
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(200, {"animations": items})

    @routes.add("PUT", "/avatar/model")
    def put_avatar_model(self, path, m):
        try:
            fname = add_character(self.read_body(120_000_000),
                                  parse_qs(urlparse(self.path).query).get("name", ["character"])[0])
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        cfg_now = save_avatar_config({"style": "vrm", "character": fname})
        log(f"avatar: new character {fname}")
        return self.send_json(200, cfg_now)

    @routes.add("PUT", R(r"/images/([0-9a-f]{16})\.jpg"))
    def put_jpg(self, path, m):
        data = self.read_body(8_000_000)
        if not data or not data.startswith(b"\xff\xd8"):
            return self.send_json(400, {"error": "send a JPEG under 8 MB"})
        file = os.path.join(IMAGE_DIR, m.group(1) + ".jpg")
        if not os.path.exists(file):
            with open(file + ".part", "wb") as f:
                f.write(data)
            os.replace(file + ".part", file)
        return self.send_json(200, {"ok": True})

    @routes.add("DELETE", R(r"/modes/(c-[0-9a-f]{10})"))
    def delete_root(self, path, m):
        return self.send_json(200, {"deleted": chats.delete_mode(m.group(1))})

    @routes.add("DELETE", R(r"/docs/([0-9a-f]{10})"))
    def delete_root_2(self, path, m):
        return self.send_json(200, {"deleted": chats.delete_doc(m.group(1))})

    @routes.add("DELETE", R(r"/chats/([^/]{1,64})/notes/([0-9a-f]{1,12})"))
    def delete_notes(self, path, m):
        return self.send_json(200, {"deleted": chats.delete_chat_note(m.group(1), m.group(2)),
                                    "notes": chats.chat_notes(m.group(1))})

    @routes.add("DELETE", R(r"/lists/items/([0-9a-f]{10})"))
    def delete_root_3(self, path, m):
        return self.send_json(200, {"deleted": chats.set_item(m.group(1), delete=True)})

    @routes.add("DELETE", R(r"/projects/(library)/(files)/([0-9a-f]{1,32})"), R(r"/projects/([0-9a-f]{12})(?:/(notes|files)/([0-9a-f]{1,32}))?"))
    def delete_root_4(self, path, m):
        pid, what, item = m.groups()
        if what == "notes":
            return self.send_json(200, {"deleted": chats.delete_project_note(pid, item)})
        if what == "files":
            return self.send_json(200, {"deleted": chats.delete_project_file(pid, item)})
        return self.send_json(200, {"deleted": chats.delete_project(pid)})

    @routes.add("DELETE", R(r"/memories/([0-9a-f]{1,32})"))
    def delete_root_5(self, path, m):
        return self.send_json(200, {"deleted": chats.delete_memory(m.group(1))})

    @routes.add("GET", "/health", access="first")
    def get_health(self, path, m):
        return self.send_json(200, {"ok": True})

    @routes.add("GET", "/move/hello", access="first")
    def get_move_hello(self, path, m):
        return self.send_json(200, move_hello())

    @routes.add("GET", "/move/status", access="open")
    def get_move_status(self, path, m):
        if not self.authorized():
            return self.send_json(401, {"error": "wrong API key"})
        return self.send_json(200, {**move_hello(), "target": MOVE["target"], "error": MOVE["error"],
                                    "moved_to": MOVE["signpost"]})

    @routes.add("GET", "/avatar/avatar.js", access="open")
    def get_avatar_avatar_js(self, path, m):
        body = AVATAR_JS.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        return

    @routes.add("GET", Prefix("/avatar/lib/"), access="open")
    def get_root(self, path, m):
        rel = path[len("/avatar/lib/"):]
        if rel not in AVATAR_LIBS:          # only the files listed above, never anything else
            return self.send_json(404, {"error": "not found"})
        return self.send_file(_avatar_path("lib", *rel.split("/")),
                              LIB_TYPES.get(os.path.splitext(rel)[1], "application/octet-stream"))

    @routes.add("GET", "/", "/index.html", "/chat", "/avatar", access="open")
    def get_root_2(self, path, m):
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

    @routes.add("GET", "/favicon.ico", access="open")
    def get_favicon_ico(self, path, m):
        return self.send_json(404, {"error": "none"})

    @routes.add("GET", "/avatar/config")
    def get_avatar_config(self, path, m):
        return self.send_json(200, avatar_config())

    @routes.add("GET", "/avatar/events")
    def get_avatar_events(self, path, m):
        try:
            since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0])
        except ValueError:
            since = 0
        return self.send_json(200, dict(AVATAR_BUS.since(since), avatar_rev=avatar_rev()))

    @routes.add("GET", Prefix("/avatar/bin/"))
    def get_root_3(self, path, m):
        kind, _, name = urllib.parse.unquote(path[len("/avatar/bin/"):]).partition("/")
        if kind not in ("characters", "animations") or not _safe_bin_name(name):
            return self.send_json(404, {"error": "not found"})
        # revalidated each time (not cached forever), so a replaced file replaces the screen's copy
        if kind == "characters" and parse_qs(urlparse(self.path).query).get("lite", [""])[0] == "1":
            lite = lite_character(name)   # phones and light-mode screens: smaller textures
            if lite:
                return self.send_file(lite, "application/octet-stream", revalidate=True)
        return self.send_file(_avatar_path("bin", kind, name), "application/octet-stream", revalidate=True)

    @routes.add("GET", "/avatar/model.vrm")
    def get_avatar_model_vrm(self, path, m):
        ch = avatar_config()["character"]
        return self.send_file(_avatar_path("bin", "characters", ch), "model/gltf-binary", revalidate=True) \
            if ch else self.send_json(404, {"error": "no character"})

    @routes.add("GET", "/tools")
    def get_tools(self, path, m):
        return self.send_json(200, {"tools": [t[1] for t in SERVER_TOOLS.values()],
                                    "approval": sorted(APPROVAL_TOOLS)})

    @routes.add("GET", "/reminders")
    def get_reminders(self, path, m):
        return self.send_json(200, {"reminders": chats.reminders(), "now": int(time.time() * 1000)})

    @routes.add("GET", "/lists")
    def get_lists(self, path, m):
        return self.send_json(200, {"lists": chats.lists()})

    @routes.add("GET", "/github")
    def get_github(self, path, m):
        return self.send_json(200, github_status())

    @routes.add("GET", "/search")
    def get_search(self, path, m):
        q = parse_qs(urlparse(self.path).query)
        return self.send_json(200, {"results": CHAT_SEARCH.search(q.get("q", [""])[0], k=20,
                                                                   exclude=q.get("exclude", [""])[0])})

    @routes.add("GET", "/speedtest")   # past results (POST runs a new one)
    def get_speedtest(self, path, m):
        return self.send_json(200, {"history": _speed_history()[-10:]})

    @routes.add("GET", "/doctor")
    def get_doctor(self, path, m):
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

    @routes.add("GET", "/backups")
    def get_backups(self, path, m):
        return self.send_json(200, backups_status())

    @routes.add("GET", "/server")
    def get_server(self, path, m):
        return self.send_json(200, server_status())

    @routes.add("GET", "/memories")
    def get_memories(self, path, m):
        return self.send_json(200, {"memories": chats.memories()})

    @routes.add("GET", R(r"/chats/([^/]{1,64})/notes"))
    def get_notes(self, path, m):
        return self.send_json(200, {"notes": chats.chat_notes(m.group(1))})

    @routes.add("GET", "/memories/suggestions")
    def get_memories_suggestions(self, path, m):
        return self.send_json(200, {**memory_settings(), "suggestions": chats.memory_suggestions()})

    @routes.add("GET", R(r"/chats/([^/]{1,64})/docs"))
    def get_docs(self, path, m):
        return self.send_json(200, {"docs": chats.docs(m.group(1))})

    @routes.add("GET", R(r"/docs/([0-9a-f]{10})"))
    def get_root_4(self, path, m):
        d = chats.doc(m.group(1))
        return self.send_json(200, d) if d else self.send_json(404, {"error": "no such document"})

    @routes.add("GET", R(r"/docs/([0-9a-f]{10})/versions/(\d+)"))
    def get_versions(self, path, m):
        t = chats.doc_version_text(m.group(1), int(m.group(2)))
        return self.send_json(200, {"text": t}) if t is not None else self.send_json(404, {"error": "not kept"})

    @routes.add("GET", "/projects")
    def get_projects(self, path, m):
        return self.send_json(200, {"projects": chats.projects()})

    @routes.add("POST", R(r"/chats/([^/]{1,64})/reply"))   # the server runs the whole reply, streamed as events
    def post_chat_reply(self, path, m):
        try:
            data = json.loads(self.read_body(2_000_000) or b"{}")
        except ValueError as e:
            return self.send_json(400, {"error": f"bad request: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        lock = threading.Lock()

        def send(event):
            with lock:
                self.wfile.write((json.dumps(event) + "\n").encode())
                self.wfile.flush()
        start_reply(m.group(1), data, send)

    @routes.add("POST", R(r"/replies/([0-9a-f]{12})/answer"))   # the app's answer: a phone action's result, an OK
    def post_reply_answer(self, path, m):
        try:
            data = json.loads(self.read_body(2_000_000) or b"{}")
        except ValueError as e:
            return self.send_json(400, {"error": f"bad request: {e}"})
        return self.send_json(200, {"ok": reply_answer(m.group(1), data.get("call_id"), data.get("answer"))})

    @routes.add("POST", R(r"/replies/([0-9a-f]{12})/stop"))
    def post_reply_stop(self, path, m):
        return self.send_json(200, {"ok": reply_stop(m.group(1))})

    @routes.add("POST", "/server/update")   # "Check for updates": wakes the update loop now
    def post_server_update(self, path, m):
        started, message = request_update_check()
        return self.send_json(200, {"started": started, "message": message, "last_check": UPDATER["last_check"],
                                    "last_result": UPDATER["last_result"]})

    @routes.add("GET", "/prompts")   # Stella's instructions and the built-in modes (the apps cache them)
    def get_prompts(self, path, m):
        return self.send_json(200, prompts_payload())

    @routes.add("GET", "/modes")
    def get_modes(self, path, m):
        return self.send_json(200, {"modes": chats.modes()})

    @routes.add("GET", "/library")
    def get_library(self, path, m):
        return self.send_json(200, chats.library())

    @routes.add("GET", R(r"/projects/(library|[0-9a-f]{12})/files/([0-9a-f]{16})\.txt"))
    def get_files_txt(self, path, m):
        return self.send_file(os.path.join(PROJECT_DIR, m.group(2) + ".txt"), "text/plain; charset=utf-8")

    @routes.add("GET", "/wakewords")
    def get_wakewords(self, path, m):
        names = sorted(f[:-5] for f in os.listdir(WAKEWORD_DIR) if f.endswith(".onnx")
                       and WAKEWORD_NAME.fullmatch(f[:-5]) and ".." not in f)
        return self.send_json(200, {"wakewords": [
            {"id": n, "label": " ".join(w.capitalize() for w in re.split(r"[_-]+", re.sub(r"_v\d.*$", "", n)) if w)}
            for n in names]})

    @routes.add("GET", R(r"/wakewords/(.+)\.onnx"))
    def get_onnx(self, path, m):
        if not (WAKEWORD_NAME.fullmatch(m.group(1)) and ".." not in m.group(1)):
            return self.send_json(404, {"error": "not found"})
        return self.send_file(os.path.join(WAKEWORD_DIR, m.group(1) + ".onnx"), "application/octet-stream")

    @routes.add("GET", R(r"/images/([0-9a-f]{16})\.jpg"))
    def get_jpg(self, path, m):
        return self.send_file(os.path.join(IMAGE_DIR, m.group(1) + ".jpg"), "image/jpeg")

    @routes.add("GET", R(r"/diagrams/([0-9a-f]{16})\.png"))
    def get_png(self, path, m):
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

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def read_body(self, limit):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > limit:
                return None
            return self.rfile.read(n)


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

        def dispatch(self, method):
            path = urlparse(self.path).path
            fn, m = routes.find(method, path, "first")
            if fn:
                return fn(self, path, m)
            if self.move_gate(path, method):
                return
            fn, m = routes.find(method, path, "open")
            if fn:
                return fn(self, path, m)
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})
            fn, m = routes.find(method, path, "key")
            if fn:
                return fn(self, path, m)
            return self.send_json(404, {"error": "not found"})

        def do_GET(self):
            return self.dispatch("GET")

        def do_POST(self):
            return self.dispatch("POST")

        def do_PUT(self):
            return self.dispatch("PUT")

        def do_DELETE(self):
            return self.dispatch("DELETE")

    for name in ("do_GET", "do_POST", "do_PUT", "do_DELETE"):
        setattr(Handler, name, _tracked(getattr(Handler, name)))
    Handler.routes = routes
    return Handler


