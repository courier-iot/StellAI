# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Backups ----------------------------------------------------------------------------
BACKUP = {"dir": None, "config": None, "paths": {}, "last": 0, "error": "", "running": False}


def backup_config():
    try:
        with open(BACKUP["config"]) as f:
            c = json.load(f)
    except (OSError, ValueError, TypeError):
        c = {}
    return {"copy_to": str(c.get("copy_to") or ""), "keep": max(2, min(60, int(c.get("keep") or 14)))}


def save_backup_config(data):
    c = backup_config()
    if "copy_to" in data:
        path = os.path.expanduser(str(data.get("copy_to") or "").strip())
        if path and not os.path.isdir(path):
            raise ValueError(f"folder not found: {path}")
        c["copy_to"] = path
    if "keep" in data:
        c["keep"] = max(2, min(60, int(data["keep"])))
    with open(BACKUP["config"], "w") as f:
        json.dump(c, f)
    return backups_status()


def _backup_files(folder):
    try:
        return sorted(f for f in os.listdir(folder) if f.startswith("stellai-backup-") and f.endswith(".zip"))
    except OSError:
        return []


def backups_status():
    folder = BACKUP["dir"]
    items = []
    for f in reversed(_backup_files(folder)):
        st = os.stat(os.path.join(folder, f))
        items.append({"name": f, "size": st.st_size, "time": int(st.st_mtime * 1000)})
    cfg = backup_config()
    return {"folder": folder, "copy_to": cfg["copy_to"], "keep": cfg["keep"], "backups": items,
            "last": items[0]["time"] if items else 0, "error": BACKUP["error"], "running": BACKUP["running"]}


def backup_now():
    """Zips chats (consistent SQLite snapshot), memories, projects, photos, diagrams and wake words."""
    import zipfile
    if BACKUP["running"]:
        raise RuntimeError("a backup is already running")
    BACKUP["running"] = True
    try:
        folder = BACKUP["dir"]
        os.makedirs(folder, exist_ok=True)
        name = time.strftime("stellai-backup-%Y-%m-%d_%H%M.zip")
        dest = os.path.join(folder, name)
        with tempfile.TemporaryDirectory() as tmp:
            snap = os.path.join(tmp, "chats.db")
            dst = sqlite3.connect(snap)
            with CHATS.lock:
                CHATS.db.backup(dst)
            dst.close()
            with zipfile.ZipFile(dest + ".part", "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, "chats.db")
                for label, path in BACKUP["paths"].items():
                    if not path or not os.path.isdir(path):
                        continue
                    for root, _, files in os.walk(path):
                        for f in files:
                            full = os.path.join(root, f)
                            z.write(full, os.path.join(label, os.path.relpath(full, path)))
                # the avatar's settings, character and animations (not its downloads)
                for f in ["config.json", "animations.json"] + (
                        ["bin/characters/" + c["file"] for c in list_characters()] +
                        ["bin/animations/" + a["file"] for a in list_animations()] if AVATAR["dir"] else []):
                    if AVATAR["dir"] and os.path.isfile(_avatar_path(*f.split("/"))):
                        z.write(_avatar_path(*f.split("/")), "avatar/" + f)
                gh = github_config()
                gh.pop("token", None)   # never put the GitHub token in a backup
                z.writestr("github.json", json.dumps(gh))
                z.writestr("backup-info.json", json.dumps({
                    "made": time.strftime("%Y-%m-%d %H:%M"), "version": 1,
                    "chats": CHATS.db.execute("SELECT COUNT(*) FROM conversations WHERE deleted=0").fetchone()[0]}))
        os.replace(dest + ".part", dest)
        cfg = backup_config()
        for f in _backup_files(folder)[:-cfg["keep"]]:
            os.remove(os.path.join(folder, f))
        if cfg["copy_to"]:
            try:
                import shutil
                shutil.copy2(dest, os.path.join(cfg["copy_to"], name))
                for f in _backup_files(cfg["copy_to"])[:-cfg["keep"]]:
                    os.remove(os.path.join(cfg["copy_to"], f))
            except OSError as e:
                raise RuntimeError(f"backup saved here, but copying to {cfg['copy_to']} failed: {e}")
        BACKUP["error"] = ""
        log(f"backup saved: {name} ({os.path.getsize(dest) // 1024} KB)")
        return dest
    except Exception as e:
        BACKUP["error"] = str(e)
        raise
    finally:
        BACKUP["running"] = False


def backup_loop():
    """Nightly backup (between 2 and 5 am when Stella is idle; at the latest after 36 hours)."""
    time.sleep(120)
    while True:
        try:
            last = backups_status()["last"]
            age_h = (time.time() * 1000 - last) / 3600_000 if last else 999
            hour = time.localtime().tm_hour
            if age_h > 36 or (age_h > 20 and 2 <= hour < 5 and server_idle(300)):
                backup_now()
        except Exception as e:
            log(f"backup failed: {e}")
        time.sleep(1800)


def restore_backup(zip_path, cfg):
    """Puts a backup back (run while the server is stopped). The current data is kept as *.before-restore."""
    import shutil
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "chats.db" not in names:
            raise ValueError("that isn't a StellAI backup")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        had_db = os.path.exists(cfg.db)
        targets = {"projects": cfg.project_dir or os.path.join(os.path.dirname(os.path.abspath(cfg.db)), "projects"),
                   "images": cfg.image_dir, "diagrams": cfg.diagram_dir, "wakewords": cfg.wakeword_dir,
                   "avatar": getattr(cfg, "avatar_dir", "") or os.path.join(os.path.dirname(os.path.abspath(cfg.db)), "avatar")}
        if os.path.exists(cfg.db):
            os.replace(cfg.db, f"{cfg.db}.before-restore-{stamp}")
            for ext in ("-wal", "-shm"):
                if os.path.exists(cfg.db + ext):
                    os.remove(cfg.db + ext)
        with z.open("chats.db") as src, open(cfg.db, "wb") as out:
            shutil.copyfileobj(src, out)
        for label, target in targets.items():
            target = os.path.abspath(target)
            for n in names:
                if not n.startswith(label + "/") or n.endswith("/"):
                    continue
                rel = n[len(label) + 1:]
                dest = os.path.realpath(os.path.join(target, rel))
                if not dest.startswith(os.path.realpath(target) + os.sep):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    return f"restored {os.path.basename(zip_path)}" + (
        f"; the previous chat database was kept as {cfg.db}.before-restore-{stamp}" if had_db else "")


