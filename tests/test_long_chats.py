"""Long chats: background summaries, pinned notes, recall from the same chat (fake model server)."""
import time

from helpers import check, done, fake_model, start_hub

fl = fake_model(18999, 2048)
h, web, tmp, port = start_hub(llama="http://127.0.0.1:18999")
now = int(time.time() * 1000)
msgs = []
for i in range(14):
    msgs.append({"id": f"u{i}", "conv_id": "c1", "role": "user", "text": f"Question {i}: the bracket arm {i} should be {10 + i} mm long, and the hole is {3 + i} mm. " * 6, "created_at": now + i * 100, "updated_at": now + i * 100})
    msgs.append({"id": f"a{i}", "conv_id": "c1", "role": "assistant", "text": f"Answer {i}: noted, bracket part {i} at {10 + i} mm. " * 6, "created_at": now + i * 100 + 50, "updated_at": now + i * 100 + 50})
h.CHATS.sync(0, [{"id": "c1", "title": "Bracket", "created_at": now, "updated_at": now, "mode": "cad"}], msgs)
check(h.summarize_in_background("c1"), "a long chat gets summarized")
rows = h.CHATS.db.execute("SELECT role FROM messages WHERE conv_id='c1' AND deleted=0 ORDER BY created_at").fetchall()
idx = [i for i, r in enumerate(rows) if r[0] == "summary"]
check(len(idx) == 1 and 0 < idx[0] < len(rows) - 2, "the summary sits before the newest messages", idx)
check(not h.summarize_in_background("c1"), "nothing new: no second summary")
r, s = h.run_server_tool("pin_note", {"conversation_id": "c1", "text": "Bracket arms are PETG, 3 mm walls"})
check(s.startswith("Pinned:"), "pin a note", s)
r, s = h.run_server_tool("pin_note", {"conversation_id": "c1", "action": "list"})
nid = r["notes"][0]["id"]
h.CHAT_SEARCH.index_dirty()
r, s = h.run_server_tool("recall_chat", {"conversation_id": "c1", "query": "bracket arm 3 hole"})
check(r["passages"] and "bracket arm 3" in r["passages"][0]["text"], "recall finds the exact earlier words", r)
plan = h.plan_context({"chat_id": "c1", "split": True, "mode_tools": "all", "query": "what length was arm 2?",
                       "last": "what length was arm 2?", "messages": [{"role": "user", "content": "hi"}]})
check({"pin_note", "recall_chat"} <= set(plan["tools"]) and "Pinned notes" in plan["memory_context"], "long-chat tools and notes", plan)
r, s = h.run_server_tool("pin_note", {"conversation_id": "c1", "action": "remove", "note_id": nid})
check(s == "Unpinned a note", "unpin", s)
check(any(m["role"] == "summary" for m in h.CHATS.sync(0, [], [])["messages"]), "summaries sync to the apps")
fl.terminate()
web.server_close()
done()
