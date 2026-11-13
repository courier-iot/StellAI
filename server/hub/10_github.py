# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- GitHub ------------------------------------------------------------------------
GITHUB_FILE = None      # JSON with token + default repo, set in start()
GITHUB_API = os.environ.get("STELLAI_GITHUB_API", "https://api.github.com")
APPROVAL_TOOLS = {"github_commit", "github_pull_request"}   # clients must ask the user first


def github_config():
    try:
        with open(GITHUB_FILE) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def save_github_config(data):
    cfg = github_config()
    if "token" in data:
        cfg["token"] = str(data.get("token") or "").strip()
    if "default_repo" in data:
        repo = str(data.get("default_repo") or "").strip().strip("/")
        if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("default repository must look like owner/name")
        cfg["default_repo"] = repo
    fd = os.open(GITHUB_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    os.replace(GITHUB_FILE + ".tmp", GITHUB_FILE)
    return github_status()


def _gh(method, path, body=None, accept="application/vnd.github+json", raw=False, timeout=30):
    token = github_config().get("token", "")
    url = path if path.startswith(("https://", "http://")) else GITHUB_API + path
    headers = {"Accept": accept, "User-Agent": "StellAI", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read()).get("message", "")
        except (ValueError, OSError):
            pass
        if e.code in (401, 403) and not token:
            msg += " (add a GitHub token in StellAI's settings to use private repositories)"
        elif e.code == 404:
            msg = "not found (check the repository name and path" + ("" if token else ", or add a GitHub token") + ")"
        raise RuntimeError(f"GitHub {e.code}: {msg}".strip())
    if raw:
        return data
    return json.loads(data) if data else {}


def github_status():
    cfg = github_config()
    out = {"token_set": bool(cfg.get("token")), "default_repo": cfg.get("default_repo", ""), "user": ""}
    if cfg.get("token"):
        try:
            out["user"] = _gh("GET", "/user").get("login", "")
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            out["error"] = str(e)
    return out


def _repo(args):
    repo = str(args.get("repo") or github_config().get("default_repo") or "").strip().strip("/")
    repo = re.sub(r"^https?://github\.com/", "", repo).removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("which repository? Give it as owner/name (or set a default repo in Settings → GitHub)")
    return repo


def _q(path):
    return urllib.parse.quote(str(path).strip("/"))


def tool_github_repos(args):
    if not github_config().get("token"):
        raise ValueError("add a GitHub token in Settings → GitHub to list your repositories")
    repos = _gh("GET", "/user/repos?per_page=50&sort=updated")
    return {"repos": [{"name": r["full_name"], "private": r["private"], "description": r.get("description") or "",
                       "default_branch": r["default_branch"]} for r in repos]}, f"Listed {len(repos)} repositories"


def tool_github_read(args):
    import base64
    repo = _repo(args)
    path = str(args.get("path") or "").strip("/")
    ref = str(args.get("ref") or "")
    data = _gh("GET", f"/repos/{repo}/contents/{_q(path)}" + (f"?ref={urllib.parse.quote(ref)}" if ref else ""))
    if isinstance(data, list):
        items = [x["path"] + ("/" if x["type"] == "dir" else "") for x in data]
        return {"repo": repo, "path": path or "/", "entries": items}, f"Listed {repo}/{path}"
    if data.get("encoding") == "base64" and data.get("content") is not None:
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    elif data.get("download_url"):
        text = _gh("GET", data["download_url"], raw=True).decode("utf-8", "replace")
    else:
        raise ValueError("can't read that file")
    start = max(1, int(args.get("start_line") or 1))
    lines = text.splitlines()
    chunk = "\n".join(lines[start - 1:start - 1 + 1500])
    truncated = len(chunk) > 40000 or start - 1 + 1500 < len(lines)
    return {"repo": repo, "path": path, "sha": data.get("sha"), "lines": len(lines), "from_line": start,
            "text": chunk[:40000], "truncated": truncated,
            "note": "Use start_line to read further." if truncated else ""}, f"Read {repo}/{path}"


def tool_github_search(args):
    repo = _repo(args)
    query = str(args.get("query", "")).strip()
    if not github_config().get("token"):
        raise ValueError("GitHub code search needs a token (Settings → GitHub)")
    data = _gh("GET", "/search/code?per_page=10&q=" + urllib.parse.quote(f"{query} repo:{repo}"),
               accept="application/vnd.github.text-match+json")
    hits = [{"path": i["path"], "matches": [m.get("fragment", "")[:300] for m in i.get("text_matches", [])][:2]}
            for i in data.get("items", [])]
    return {"repo": repo, "results": hits}, f"Searched {repo} for “{query}”: {len(hits)} file(s)"


def tool_github_history(args):
    repo = _repo(args)
    path = str(args.get("path") or "")
    n = max(1, min(30, int(args.get("count") or 10)))
    commits = _gh("GET", f"/repos/{repo}/commits?per_page={n}" + (f"&path={urllib.parse.quote(path)}" if path else ""))
    return {"repo": repo, "commits": [{"sha": c["sha"][:7], "message": c["commit"]["message"][:300],
                                       "author": c["commit"]["author"]["name"], "date": c["commit"]["author"]["date"]}
                                      for c in commits]}, f"Read {len(commits)} commits of {repo}"


def tool_github_issues(args):
    repo = _repo(args)
    state = args.get("state") if args.get("state") in ("open", "closed", "all") else "open"
    items = _gh("GET", f"/repos/{repo}/issues?per_page=20&state={state}")
    return {"repo": repo, "issues": [{"number": i["number"], "title": i["title"], "pull_request": "pull_request" in i,
                                      "state": i["state"], "body": (i.get("body") or "")[:500]} for i in items]}, \
        f"Read {len(items)} {state} issues/PRs of {repo}"


def tool_github_builds(args):
    """Recent GitHub Actions runs, with the end of the log for failed jobs (for fixing build errors)."""
    repo = _repo(args)
    runs = _gh("GET", f"/repos/{repo}/actions/runs?per_page=6").get("workflow_runs", [])
    out = [{"id": r["id"], "workflow": r["name"], "status": r["status"], "result": r.get("conclusion"),
            "branch": r["head_branch"], "commit": r["head_sha"][:7], "title": r.get("display_title", ""),
            "started": r["run_started_at"]} for r in runs]
    failed = next((r for r in runs if r.get("conclusion") == "failure"), None)
    detail = None
    if failed:
        jobs = _gh("GET", f"/repos/{repo}/actions/runs/{failed['id']}/jobs").get("jobs", [])
        detail = []
        for j in jobs:
            if j.get("conclusion") != "failure":
                continue
            steps = [s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"]
            log_tail = ""
            try:
                text = _gh("GET", f"/repos/{repo}/actions/jobs/{j['id']}/logs", raw=True).decode("utf-8", "replace")
                lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", ln) for ln in text.splitlines()]
                errs = [i for i, ln in enumerate(lines) if re.search(r"\berror\b|^e: |FAILED|Traceback", ln, re.I)]
                start = max(0, (errs[0] - 5) if errs else len(lines) - 60)
                log_tail = "\n".join(lines[start:start + 80])
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                log_tail = f"(couldn't read the log: {e})"
            detail.append({"job": j["name"], "failed_steps": steps, "log": log_tail[:8000]})
    return {"repo": repo, "runs": out, "latest_failure": detail}, f"Checked GitHub Actions for {repo}"


def _default_branch(repo):
    return _gh("GET", f"/repos/{repo}")["default_branch"]


def tool_github_commit(args):
    """Commits files to a branch (never the default branch) using the Git Data API. Needs the user's approval."""
    import base64
    if args.get("approved") is not True:
        raise ValueError("this needs the user's approval in the app first")
    if not github_config().get("token"):
        raise ValueError("committing needs a GitHub token with Contents write access (Settings → GitHub)")
    repo = _repo(args)
    branch = str(args.get("branch") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch:
        raise ValueError("give a branch name like 'stella/fix-build'")
    default = _default_branch(repo)
    if branch == default:
        raise ValueError(f"I don't commit straight to {default}; use a new branch and open a pull request")
    message = str(args.get("message") or "").strip() or "Update from Stella"
    files = args.get("files") or []
    deletes = [str(p).strip("/") for p in (args.get("delete") or [])]
    if not files and not deletes:
        raise ValueError("no files to commit")
    try:
        head = _gh("GET", f"/repos/{repo}/git/ref/heads/{_q(branch)}")["object"]["sha"]
        created = False
    except RuntimeError:
        head = _gh("GET", f"/repos/{repo}/git/ref/heads/{_q(default)}")["object"]["sha"]
        created = True
    base_tree = _gh("GET", f"/repos/{repo}/git/commits/{head}")["tree"]["sha"]
    tree = []
    for f in files:
        path = str(f.get("path", "")).strip("/")
        if not path or ".." in path.split("/"):
            raise ValueError(f"bad file path: {path!r}")
        blob = _gh("POST", f"/repos/{repo}/git/blobs",
                   {"content": base64.b64encode(str(f.get("content", "")).encode()).decode(), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    for path in deletes:
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    new_tree = _gh("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]
    commit = _gh("POST", f"/repos/{repo}/git/commits", {"message": message, "tree": new_tree, "parents": [head]})["sha"]
    if created:
        _gh("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    else:
        _gh("PATCH", f"/repos/{repo}/git/refs/heads/{_q(branch)}", {"sha": commit})
    url = f"https://github.com/{repo}/commit/{commit}"
    return {"repo": repo, "branch": branch, "commit": commit[:7], "url": url, "new_branch": created,
            "next": "Offer to open a pull request with github_pull_request."}, \
        f"Committed {len(files) + len(deletes)} file(s) to {repo}:{branch} ({commit[:7]})"


def tool_github_pull_request(args):
    if args.get("approved") is not True:
        raise ValueError("this needs the user's approval in the app first")
    repo = _repo(args)
    branch = str(args.get("branch") or "").strip()
    base = str(args.get("base") or "") or _default_branch(repo)
    pr = _gh("POST", f"/repos/{repo}/pulls", {"title": str(args.get("title") or "Changes from Stella")[:200],
                                               "head": branch, "base": base, "body": str(args.get("body") or "")})
    return {"number": pr["number"], "url": pr["html_url"]}, f"Opened pull request #{pr['number']}: {pr['html_url']}"


SERVER_TOOLS["github_repos"] = (tool_github_repos, _fn(
    "github_repos", "List the user's GitHub repositories.", {}, []))
SERVER_TOOLS["github_read"] = (tool_github_read, _fn(
    "github_read", "Read a file, or list a folder, in a GitHub repository.",
    {"repo": _p("owner/name (default: the user's default repo)"), "path": _p("File or folder path; empty for the root"),
     "ref": _p("Branch, tag or commit (optional)"), "start_line": _p("Line to start from for long files", "integer")},
    ["path"]))
SERVER_TOOLS["github_search"] = (tool_github_search, _fn(
    "github_search", "Search code in a GitHub repository (function names, strings, settings).",
    {"repo": _p("owner/name (optional)"), "query": _p("What to search for")}, ["query"]))
SERVER_TOOLS["github_history"] = (tool_github_history, _fn(
    "github_history", "Recent commits of a repository, optionally for one file.",
    {"repo": _p("owner/name (optional)"), "path": _p("File path (optional)"), "count": _p("How many (max 30)", "integer")},
    []))
SERVER_TOOLS["github_issues"] = (tool_github_issues, _fn(
    "github_issues", "Issues and pull requests of a repository.",
    {"repo": _p("owner/name (optional)"), "state": _p("open (default), closed or all")}, []))
SERVER_TOOLS["github_builds"] = (tool_github_builds, _fn(
    "github_builds",
    "Recent GitHub Actions builds of a repository, including the error log of the latest failed build. Use this when "
    "the user's build fails.",
    {"repo": _p("owner/name (optional)")}, []))
SERVER_TOOLS["github_commit"] = (tool_github_commit, _fn(
    "github_commit",
    "Commit file changes to a branch of a repository (never the default branch; a new branch is created if needed). "
    "The user must approve. Give complete file contents, not diffs; read the file first.",
    {"repo": _p("owner/name (optional)"), "branch": _p("Branch name, e.g. 'stella/fix-diagram-regex'"),
     "message": _p("Commit message"),
     "files": {"type": "array", "description": "Files to create or replace",
               "items": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                         "required": ["path", "content"]}},
     "delete": {"type": "array", "items": {"type": "string"}, "description": "Paths to delete (optional)"}},
    ["branch", "message", "files"]))
SERVER_TOOLS["github_pull_request"] = (tool_github_pull_request, _fn(
    "github_pull_request", "Open a pull request from a branch into the default branch. The user must approve.",
    {"repo": _p("owner/name (optional)"), "branch": _p("Branch with the changes"), "title": _p("Title"),
     "body": _p("Description of the changes")}, ["branch", "title"]))


