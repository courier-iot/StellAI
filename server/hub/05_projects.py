# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
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


