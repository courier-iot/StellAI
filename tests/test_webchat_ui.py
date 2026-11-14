"""The desktop web chat in a real browser: it loads without script errors, gets its modes from the server, draws
line icons, and the documents panel opens, edits and saves. Needs Playwright with Chromium (skipped otherwise)."""
import os
import shutil
import time

from helpers import check, done, fake_model, start_hub

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIPPED: Playwright isn't installed")
    raise SystemExit(0)

fl = fake_model(18996, 8192)
h, web, tmp, port = start_hub(llama="http://127.0.0.1:18996")
now = int(time.time() * 1000)
h.CHATS.sync(0, [{"id": "cU", "title": "Novel", "created_at": now, "updated_at": now, "mode": "writer"}],
             [{"id": "u1", "conv_id": "cU", "role": "user", "text": "Draft chapter 1 in a document", "created_at": now, "updated_at": now}])
r, s = h.run_server_tool("document", {"conversation_id": "cU", "action": "create", "title": "The Long Orbit",
                                      "content": "# Chapter 1: Launch\nThe rocket rose.\n\n# Chapter 2: Drift\nSilence."})
h.CHATS.sync(0, [], [{"id": "t1", "conv_id": "cU", "role": "tool", "text": "✓ " + s, "created_at": now + 1, "updated_at": now + 1},
                     {"id": "t2", "conv_id": "cU", "role": "tool", "text": "✓ Timer started: 5m\n[[button:Cancel timer|cancel_timer|{}]]",
                      "created_at": now + 1, "updated_at": now + 1},
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
    body = pg.inner_text("#msgs") if pg.locator("#msgs").count() else pg.inner_text("body")
    check("Timer started" in body and "[[button" not in body, "the phone's Cancel/Undo marker isn't shown on the desktop")
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
    # a message goes to the server, which runs the reply (with a tool) and streams it back
    pg.click("#docClose")
    pg.fill("#input", "please calc six times seven")
    pg.keyboard.press("Enter")
    for _ in range(100):   # (the page's security rules don't allow Playwright's wait_for_function)
        body = pg.inner_text("#wrap")
        if "Done:" in body:
            break
        pg.wait_for_timeout(200)
    check("6*7 = 42" in body and "Done:" in body, "the server runs the reply and the page shows it", body[-300:])
    rows = h.CHATS.db.execute("SELECT role FROM messages WHERE conv_id='cU' AND deleted=0 ORDER BY created_at").fetchall()
    check([r[0] for r in rows][-3:] == ["user", "tool", "assistant"], "the message and reply are saved on the server", rows)
    check(not errors, "no script errors while replying", errors)
    b.close()
fl.terminate()
check(h.CHATS.doc(r["doc"])["text"].endswith("The end, for now.") and h.CHATS.doc(r["doc"])["versions"][0]["by"] == "user",
      "the edit reached the server")
web.server_close()
done()
