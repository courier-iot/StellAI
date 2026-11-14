"""The web API: the key is required where it should be, unknown addresses are 404, and the instructions are served."""
from helpers import call, check, done, start_hub

h, web, tmp, port = start_hub()
check(call(port, "GET", "/health", key=None) == (200, {"ok": True}), "health needs no key")
st, page = call(port, "GET", "/chat", key=None)
check(st == 200 and b"<html" in page[:200], "the web chat page needs no key")
check(call(port, "GET", "/memories", key=None)[0] == 401, "data needs the key")
check(call(port, "GET", "/memories", key="wrong")[0] == 401, "a wrong key is refused")
check(call(port, "GET", "/nothing-here")[0] == 404 and call(port, "DELETE", "/nothing")[0] == 404, "unknown addresses are 404")
check(call(port, "GET", "/avatar/lib/../../etc/passwd", key=None)[0] == 404, "the avatar's library serves only its files")
check(call(port, "GET", "/move/status", key=None)[0] == 401, "move status needs the key")
st, r = call(port, "POST", "/tools/calculate", {"expression": "2+2"})
check(st == 200 and r["result"]["ok"] and "4" in r["summary"], "server tools run over the API", r)

st, p = call(port, "GET", "/prompts")
check(st == 200 and len(p["version"]) == 12, "the instructions are served", p)
check("{name}" in p["instructions"]["phone"] and "Android phone" in p["instructions"]["phone"]
      and "desktop web chat" in p["instructions"]["web"] and "Android phone" not in p["instructions"]["web"],
      "each app gets its own lines")
ids = [m["id"] for m in p["modes"]]
check(ids[0] == "general" and {"programmer", "writer", "cad", "pcb"} <= set(ids), "the built-in modes", ids)
writer = next(m for m in p["modes"] if m["id"] == "writer")
check(writer["tools"] == "minimal" and writer["style"] == "story" and writer["prompt"].startswith("Mode: Writer."), "mode settings", writer)
check(p["voice_style"].startswith("Voice conversation") and "recall_chat" in p["summary_intro"], "voice and summary text")
st, u = call(port, "POST", "/server/update", {})
check(st == 200 and u["started"] is False and "update" in u["message"].lower(), "Check for updates explains when updates are off", u)
check(h.BUTTON_MARK.sub("", "✓ Timer started: 5m\n[[button:Cancel timer|cancel_timer|{}]]") == "✓ Timer started: 5m",
      "summaries leave out the phone's Cancel/Undo buttons")
routes = [r[1] for r in web.RequestHandlerClass.routes.describe()]
check(len(routes) == len(set((r[0], r[1]) for r in web.RequestHandlerClass.routes.describe())), "no address is listed twice")
web.server_close()
done()
