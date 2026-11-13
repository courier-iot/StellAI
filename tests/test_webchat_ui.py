"""The desktop web chat in a real browser: it loads without script errors, gets its modes from the server, draws
line icons, and the documents panel opens, edits and saves. Needs Playwright with Chromium (skipped otherwise)."""
import os
import shutil
import time

from helpers import check, done, start_hub

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIPPED: Playwright isn't installed")
    raise SystemExit(0)

h, web, tmp, port = start_hub()
now = int(time.time() * 1000)
h.CHATS.sync(0, [{"id": "cU", "title": "Novel", "created_at": now, "updated_at": now, "mode": "writer"}],
             [{"id": "u1", "conv_id": "cU", "role": "user", "text": "Draft chapter 1 in a document", "created_at": now, "updated_at": now}])
r, s = h.run_server_tool("document", {"conversation_id": "cU", "action": "create", "title": "The Long Orbit",
                                      "content": "# Chapter 1: Launch\nThe rocket rose.\n\n# Chapter 2: Drift\nSilence."})
h.CHATS.sync(0, [], [{"id": "t1", "conv_id": "cU", "role": "tool", "text": "✓ " + s, "created_at": now + 1, "updated_at": now + 1},
                     {"id": "a1", "conv_id": "cU", "role": "assistant", "text": f"Chapter 1 is beside the chat [doc:{r['doc']}].",
                      "created_at": now + 2, "updated_at": now + 2}])
chrome = os.environ.get("STELLAI_TEST_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or None
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"]) if chrome else p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 820})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script('localStorage.setItem("stellai.key", JSON.stringify("s")); localStorage.setItem("stellai.current", JSON.stringify("cU"));')
    pg.goto(f"http://127.0.0.1:{port}/chat")
    pg.wait_for_timeout(2500)
    check(not errors, "no script errors", errors)
    check(pg.locator("svg.li").count() > 10, "line icons are drawn")
    pg.click("#modeChip")
    pg.wait_for_timeout(300)
    names = pg.locator(".pick").all_inner_texts()
    check(any("Programmer" in n for n in names) and any("PCB Designer" in n for n in names), "modes come from the server", names)
    pg.keyboard.press("Escape")
    pg.goto(f"http://127.0.0.1:{port}/chat")
    pg.wait_for_timeout(2000)
    pg.click(".docref >> nth=0")
    pg.wait_for_timeout(800)
    check(pg.is_visible("#docs") and "Chapter 1: Launch" in pg.inner_text("#docBody"), "the document opens beside the chat")
    pg.click("#docEdit")
    pg.fill("#docText", pg.input_value("#docText") + "\n\nThe end, for now.")
    pg.click("#docEdit")
    pg.wait_for_timeout(800)
    check(pg.inner_text("#docInfo").startswith("v2"), "saving makes v2", pg.inner_text("#docInfo"))
    check(not errors, "still no script errors", errors)
    b.close()
check(h.CHATS.doc(r["doc"])["text"].endswith("The end, for now.") and h.CHATS.doc(r["doc"])["versions"][0]["by"] == "user",
      "the edit reached the server")
web.server_close()
done()
