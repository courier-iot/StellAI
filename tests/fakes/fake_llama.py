"""A stand-in for llama-server: token counts, summaries, memory facts, and streamed chat replies with tool calls.

Streamed replies follow words in the newest user message, so tests can steer it:
  "calc"        -> calls the calculate tool, then answers with what it got back
  "alarm please"-> calls the phone's set_alarm (an app-side action)
  "commit"      -> calls github_commit (needs the user's OK)
  "long story"  -> stops for lack of room (finish "length"); after the automatic "continue" it finishes
  "slow"        -> streams slowly (for Stop)
  photos        -> says how many pictures it got
  "ask first"   -> asks a question AND calls a tool (the tool must not run)
  "bad args"    -> calls calculate without its expression
  "load cad"    -> calls load_tools for the CAD tools, then lists the tools it was given
  "plan this"   -> sets a two-step plan, ticks step 1, then answers
  "use skill X" -> calls use_skill with that name
  anything else -> "Hello from the model."
"""
import http.server
import json
import re
import sys
import time

port = int(sys.argv[1])
nctx = int(sys.argv[2]) if len(sys.argv) > 2 else 2048
NAME = sys.argv[3] if len(sys.argv) > 3 else ""      # put in front of streamed replies ("[deep] ...")
SEEN = {"slots": [], "id_slot": []}                    # what the hub asked for (GET /seen)


def text_of(content):
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict))
    return content or ""


def reply_for(msgs, tools=()):
    """(content chunks, tool call or None, finish reason)"""
    last = msgs[-1] if msgs else {}
    user = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
    text = text_of(user.get("content"))
    names = sorted(t.get("function", {}).get("name", "") for t in tools or [])
    if last.get("role") == "tool":
        if last.get("name") == "load_tools":
            return ["Tools: " + ",".join(names)], None, "stop"
        if last.get("name") == "update_plan" and '"next": "2.' in text_of(last.get("content")):
            return [], ("update_plan", {"done": [2]}), "tool_calls"
        if last.get("name") == "update_plan" and "Plan" not in text and sum(
                1 for m in msgs if m.get("role") == "tool") == 1:
            return [], ("update_plan", {"done": [1]}), "tool_calls"
        return ["Done: ", text_of(last.get("content"))[:300]], None, "stop"
    if "Automatic check of your reply" in text:
        return ["Fixed: both parts answered."], None, "stop"
    if "check-me" in text:
        return ["Part one answered. " * 100], None, "stop"
    if "ask first" in text:
        return ["FreeCAD isn't installed. Should I use Blender instead?"], ("calculate", {"expression": "1+1"}), "tool_calls"
    if "bad args" in text:
        return [], ("calculate", {}), "tool_calls"
    if "load cad" in text:
        return [], ("load_tools", {"groups": ["cad"]}), "tool_calls"
    if "plan this" in text:
        return [], ("update_plan", {"steps": ["Draw the base", "Add the holes"]}), "tool_calls"
    m = re.search(r"use skill ([\w-]+)", text)
    if m:
        return [], ("use_skill", {"name": m.group(1)}), "tool_calls"
    pictures = sum(1 for x in user.get("content") if isinstance(x, dict) and x.get("type") == "image_url") \
        if isinstance(user.get("content"), list) else 0
    if "Continue it exactly where it stopped" in text:
        return ["…and they all ", "lived happily. The end."], None, "stop"
    if "calc" in text:
        return [], ("calculate", {"expression": "6*7"}), "tool_calls"
    if "alarm please" in text:
        return [], ("set_alarm", {"hour": 6, "minute": 30}), "tool_calls"
    if "commit" in text:
        return [], ("github_commit", {"repo": "a/b", "branch": "fix", "message": "Fix", "files": []}), "tool_calls"
    if "long story" in text:
        return ["Once upon a time ", "there was a robot"], None, "length"
    if "slow" in text:
        return ["word "] * 60, None, "stop"
    if pictures:
        return [f"I see {pictures} picture" + ("s" if pictures != 1 else "") + "."], None, "stop"
    return ["Hello ", "from the ", "model."], None, "stop"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/seen"):
            return self.send(SEEN)
        if self.path.startswith("/props"):
            return self.send({"default_generation_settings": {"n_ctx": nctx}})
        if self.path.startswith("/slots"):
            return self.send([{"is_processing": False}])
        return self.send({"status": "ok"})

    def event(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        if self.path == "/tokenize":
            return self.send({"tokens": [0] * max(1, len(body.get("content", "")) // 4)})
        if self.path.startswith("/slots/"):
            SEEN["slots"].append([self.path.split("action=")[-1], body.get("filename")])
            return self.send({"ok": True})
        msgs = body.get("messages", [])
        if body.get("stream"):
            SEEN["id_slot"].append(body.get("id_slot"))
            chunks, call, finish = reply_for(msgs, body.get("tools"))
            if NAME and chunks:
                chunks = [f"[{NAME}] "] + chunks
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            slow = any("slow" in text_of(m.get("content")) for m in msgs if m.get("role") == "user")
            try:
                for c in chunks:
                    self.event({"choices": [{"delta": {"content": c}}]})
                    time.sleep(0.1 if slow else 0.005)
                if call:
                    self.event({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {
                        "name": call[0], "arguments": json.dumps(call[1])}}]}}]})
                self.event({"choices": [{"delta": {}, "finish_reason": finish}]})
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        last = text_of(msgs[-1]["content"]) if msgs else ""
        if "response_format" in body:
            name = ((body["response_format"].get("json_schema") or {}).get("name"))
            if name == "lesson":
                out = {"lesson": "Use 3.2 mm clearance holes for M3 screws.", "skill": "cad-part"}
            elif name == "answer" and "Score the new reply" in last:
                out = {"pass": "Hello" in last.split("New reply:")[-1], "score": 7, "why": "fine"}
            elif name == "answer":
                out = {"ok": False, "problems": ["The second part wasn't answered"]} if "check-me" in last else {"ok": True, "problems": []}
            else:
                out = {"add": ["Likes PETG for brackets"], "remove": []}
            return self.send({"choices": [{"message": {"content": json.dumps(out)}}]})
        if "tools" in body and not body.get("stream"):     # the test set replays requests without streaming
            return self.send({"choices": [{"message": {"content": "Hello from the model."}}]})
        words = re.findall(r"bracket \w+|\d+ ?mm", last)
        return self.send({"choices": [{"message": {"content": "Goal: design things. Key details: " + ", ".join(sorted(set(words))[:6])}}]})


http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
