"""Documents beside the chat: create, edit by passage and section, versions, the user's edits, conflicts."""
from helpers import call, check, done, start_hub

h, web, tmp, port = start_hub()
T = lambda **a: h.run_server_tool("document", {"conversation_id": "c10", **a})
r, s = T(action="create", title="Story", content="# Chapter 1\nIt was dark.\n\n# Chapter 2\nDawn came.\n\n# Chapter 3\nThe end.")
did = r["doc"]
check(s.startswith("Created “Story” (v1)") and f"[doc:{did}]" in s, "create (the chat gets a short reference)", s)
check(T(action="outline")[0]["outline"] == ["line 1: Chapter 1", "line 4: Chapter 2", "line 7: Chapter 3"], "outline")
r, s = T(action="replace", find="It was dark.", replace="It was a dark and quiet night.")
check("(v2)" in s, "replace a passage", s)
r, s = T(action="set_section", section="chapter 2", content="# Chapter 2\nDawn came slowly.\nBirds sang.")
check("(v3)" in s and "rewrote section" in s, "rewrite a section", s)
r, s = T(action="insert_after", section="Chapter 3", content="# Chapter 4\nEpilogue.")
r, s = T(action="append", content="(fin)")
check("(v5)" in s, "insert and append", s)
text = h.CHATS.doc(did)["text"]
check("Dawn came slowly.\nBirds sang.\n\n# Chapter 3" in text and text.endswith("Epilogue.\n\n(fin)"),
      "sections keep the blank lines between them", text)
check("isn't in the document" in str(T(action="replace", find="nope", replace="x")[0]), "replace needs exact text")
check("no section called" in str(T(action="set_section", section="Chapter 9", content="x")[0]), "unknown sections are explained")
r, s = T(action="revert", version=2)
check(h.CHATS.doc(did)["text"].startswith("# Chapter 1\nIt was a dark and quiet night.\n\n# Chapter 2\nDawn came.\n"), "revert")
r, s = T(action="create", title="blink.py", language="python", content="import time\n\ndef on():\n    print('on')\n\ndef off():\n    print('off')\n")
T(action="set_section", doc="blink.py", section="def off", content="def off():\n    print('OFF')")
check(h.CHATS.doc(r["doc"])["text"] == "import time\n\ndef on():\n    print('on')\n\ndef off():\n    print('OFF')\n", "edit a function in code")
check("“Story”" in h.docs_section("c10") and "sections: Chapter 1" in h.docs_section("c10"), "Stella knows the documents' sections")

st, d = call(port, "GET", f"/docs/{did}")
v = d["version"]
st, d2 = call(port, "POST", f"/docs/{did}", {"text": d["text"] + "\nuser line", "base_version": v})
check(st == 200 and d2["version"] == v + 1 and d2["versions"][0]["by"] == "user", "the user's edit", d2)
check(call(port, "POST", f"/docs/{did}", {"text": "x", "base_version": v})[0] == 409, "a stale edit is refused")
check(call(port, "POST", f"/docs/{did}", {"revert": 1})[1]["version"] == v + 2, "the user can go back")
check(call(port, "GET", f"/docs/{did}/versions/1")[1]["text"].startswith("# Chapter 1\nIt was dark."), "old versions are kept")
st, nd = call(port, "POST", "/chats/c10/docs", {"title": "Mine", "text": "hello"})
check(nd["title"] == "Mine" and call(port, "DELETE", f"/docs/{nd['id']}")[1] == {"deleted": True}, "the user's own document")
plan = h.plan_context({"chat_id": "zz", "mode": "writer", "mode_tools": "minimal", "last": "hi", "query": "hi"})
check("document" in (plan.get("tools") or []), "the Writer mode gets the document tool")
plan = h.plan_context({"chat_id": "zz2", "mode": "general", "mode_tools": "all", "last": "write chapter 4 of my story", "query": "write chapter 4 of my story"})
check("document" in (plan.get("tools") or []), "asking for something long brings the document tool")
web.server_close()
done()
