# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ---- Web search and reading pages -------------------------------------------------
SEARXNG_URL = ""   # e.g. http://127.0.0.1:8888 (set in start())
WEB_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def _public_host(host):
    """True if every address the host resolves to is on the public internet (no LAN, localhost, metadata)."""
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ValueError(f"couldn't look up {host} (is the server online?)")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))):
            return False
    return True


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urlparse(newurl)
        if u.scheme not in ("http", "https") or not _public_host(u.hostname or ""):
            raise urllib.error.URLError("redirect to a private or unsupported address was blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_public(url, data=None, headers=None, timeout=20, max_bytes=3_000_000):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http and https addresses can be opened")
    if not _public_host(u.hostname):
        raise ValueError("that address is on a private network; Stella only reads public web pages")
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": WEB_UA, "Accept-Language": "en", **(headers or {})})
    with opener.open(req, timeout=timeout) as r:
        body = r.read(max_bytes + 1)
        return body[:max_bytes], r.headers.get("Content-Type", ""), r.geturl()


def _html_text(html):
    import html as _h
    title = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form|aside)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article|pre)>", "\n", html)
    html = re.sub(r"(?i)<li[^>]*>", "\n- ", html)
    text = _h.unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return (_h.unescape(title.group(1)).strip() if title else ""), text


def _searx(query, n):
    url = f"{SEARXNG_URL.rstrip('/')}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "StellAI"}), timeout=20) as r:
        data = json.loads(r.read())
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:300]}
            for x in data.get("results", [])[:n]]


def _duckduckgo(query, n):
    import html as _h
    body, _, _ = _open_public("https://html.duckduckgo.com/html/", data=urllib.parse.urlencode({"q": query}).encode(),
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    page = body.decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>([\s\S]*?)'
                         r'(?:class="result__snippet"[^>]*>([\s\S]*?)</a>|<div class="result results_links)', page):
        href = _h.unescape(m.group(1))
        q = parse_qs(urlparse(href).query)
        url = q.get("uddg", [href])[0]
        if url.startswith("//"):
            url = "https:" + url
        if "duckduckgo.com/y.js" in url:   # ads
            continue
        title = _h.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        snippet = _h.unescape(re.sub(r"<[^>]+>", "", m.group(4) or "")).strip()
        out.append({"title": title, "url": url, "snippet": snippet[:300]})
        if len(out) >= n:
            break
    return out


def tool_web_search(args):
    query = " ".join(str(args.get("query", "")).split())[:300]
    if not query:
        raise ValueError("what should I search for?")
    n = max(1, min(10, int(args.get("count") or 6)))
    source = "SearXNG"
    results = []
    if SEARXNG_URL:
        try:
            results = _searx(query, n)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"SearXNG unavailable ({e}); using DuckDuckGo")
    if not results:
        source = "DuckDuckGo"
        results = _duckduckgo(query, n)
    return {"query": query, "results": results, "source": source,
            "note": "Cite the URLs you use. Call fetch_page to read a result in full."}, \
        f"Searched the web for “{query}”: {len(results)} result{'s' if len(results) != 1 else ''}"


def tool_fetch_page(args):
    url = str(args.get("url", "")).strip()
    limit = max(1000, min(30000, int(args.get("max_chars") or 12000)))
    body, ctype, final = _open_public(url)
    if "pdf" in ctype or body[:5] == b"%PDF-":
        title, text = url.rsplit("/", 1)[-1], _pdf_text(body)
    elif "html" in ctype or b"<html" in body[:2000].lower():
        title, text = _html_text(body.decode("utf-8", "replace"))
    else:
        title, text = url, body.decode("utf-8", "replace")
    find = str(args.get("find") or "").strip().lower()
    if find and find in text.lower():
        i = text.lower().index(find)
        text = text[max(0, i - limit // 3):]
    truncated = len(text) > limit
    return {"url": final, "title": title, "text": text[:limit], "truncated": truncated}, \
        f"Read {title or final}"[:200]


SERVER_TOOLS["web_search"] = (tool_web_search, _fn(
    "web_search",
    "Search the web for current information: documentation, news, prices, part availability, error messages. "
    "Returns titles, URLs and snippets.",
    {"query": _p("Search query"), "count": _p("Number of results (1-10, default 6)", "integer")}, ["query"]))
SERVER_TOOLS["fetch_page"] = (tool_fetch_page, _fn(
    "fetch_page",
    "Read a public web page or PDF as text (e.g. a search result, documentation or a datasheet URL).",
    {"url": _p("The http(s) address"), "find": _p("Optional words to jump to in a long page"),
     "max_chars": _p("Maximum characters to return (default 12000)", "integer")}, ["url"]))


