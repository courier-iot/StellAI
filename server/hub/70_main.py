# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
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
    if "--list-routes" in sys.argv:   # every web address the hub answers (kept in docs/api.md)
        print(routes_markdown())
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
