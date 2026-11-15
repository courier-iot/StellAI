# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- A real browser for pages fetch_page can't read ---------------------------------------------------------------
# Many sites build their page with JavaScript, so a plain download is nearly empty. browse_page opens the page in a
# headless Edge or Chrome (Chromium on Linux) on the server, lets its scripts run, then reads the finished page, and
# can show a picture of it. Public pages only, a fresh profile each time: no logins, cookies or saved passwords.
BROWSER = {"exe": None}
BROWSER_CANDIDATES_WIN = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe"]
BROWSER_CANDIDATES_UNIX = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge",
                           "microsoft-edge-stable"]


def find_browser():
    if BROWSER["exe"] is None:
        import shutil
        found = os.environ.get("STELLAI_BROWSER", "")
        if not found:
            if os.name == "nt":
                found = next((p for p in (os.path.expandvars(c) for c in BROWSER_CANDIDATES_WIN) if os.path.isfile(p)), "")
            else:
                found = next((shutil.which(c) for c in BROWSER_CANDIDATES_UNIX if shutil.which(c)), "")
        BROWSER["exe"] = found or ""
    return BROWSER["exe"]


def _browser_run(url, extra, timeout=60, wait_s=6):
    import tempfile
    import shutil
    exe = find_browser()
    if not exe:
        raise RuntimeError("no Edge, Chrome or Chromium on the server (on Linux: sudo apt install chromium)")
    profile = tempfile.mkdtemp(prefix="stellai-browser-")
    cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
           "--disable-extensions", "--mute-audio", "--hide-scrollbars", f"--user-data-dir={profile}",
           f"--virtual-time-budget={int(max(1, min(20, wait_s)) * 1000)}", f"--user-agent={WEB_UA}"]
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.append("--no-sandbox")       # Chromium refuses to run as root otherwise (e.g. in a container)
    try:
        r = subprocess.run(cmd + extra + [url], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.stdout.decode("utf-8", "replace"), profile
    except subprocess.TimeoutExpired:
        raise RuntimeError("the page took too long to load")
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def tool_browse_page(args):
    url = str(args.get("url", "")).strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http and https addresses can be opened")
    if not _public_host(u.hostname):
        raise ValueError("that address is on a private network; Stella only opens public web pages")
    limit = max(1000, min(30000, int(args.get("max_chars") or 12000)))
    wait = float(args.get("wait_seconds") or 6)
    html, _ = _browser_run(url, ["--dump-dom"], wait_s=wait)
    if len(html.strip()) < 50:
        raise RuntimeError("the browser returned an empty page")
    title, text = _html_text(html)
    find = str(args.get("find") or "").strip().lower()
    if find and find in text.lower():
        i = text.lower().index(find)
        text = text[max(0, i - limit // 3):]
    out = {"url": url, "title": title, "text": text[:limit], "truncated": len(text) > limit}
    if args.get("screenshot") and DIAGRAM_DIR:
        image_id = uuid.uuid4().hex[:16]
        path = os.path.join(DIAGRAM_DIR, image_id + ".png")
        _browser_run(url, [f"--screenshot={path}", "--window-size=1280,1600"], wait_s=wait)
        if os.path.isfile(path) and os.path.getsize(path) > 1000:
            out.update(image=image_id, title=title or url)
            out["note"] = "A picture of the page is shown to the user."
    return out, f"Opened {title or url} in the browser"[:200]


SERVER_TOOLS["browse_page"] = (tool_browse_page, _fn(
    "browse_page",
    "Open a public web page in a real (headless) browser and read it after its scripts run: for pages fetch_page "
    "returns empty or broken (shops, dashboards, apps built with JavaScript). Slower than fetch_page, so try that "
    "first. screenshot=true also shows the user a picture of the page. No logins.",
    {"url": _p("The http(s) address"), "find": _p("Optional words to jump to in a long page"),
     "screenshot": _p("true to show a picture of the page", "boolean"),
     "wait_seconds": _p("How long to let the page's scripts run (default 6, max 20)", "number"),
     "max_chars": _p("Maximum characters to return (default 12000)", "integer")}, ["url"]))
TOOL_GROUPS["browser"] = ["browse_page"]
TOOL_TRIGGERS["browser"] = re.compile(r"\bbrowser\b|screenshot|javascript|\bopen (the|this|that) (page|site|website)|"
                                      r"what does (the|this|that) (page|site|website) look like", re.I)
TOOL_GROUP_LABELS["browser"] = "a real browser for pages fetch_page can't read, with screenshots"
