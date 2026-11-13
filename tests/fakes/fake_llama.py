import json, sys, http.server, re
port = int(sys.argv[1]); nctx = int(sys.argv[2]) if len(sys.argv) > 2 else 2048
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, obj):
        b = json.dumps(obj).encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/props"): return self.send({"default_generation_settings": {"n_ctx": nctx}})
        if self.path.startswith("/slots"): return self.send([{"is_processing": False}])
        return self.send({"status": "ok"})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        if self.path == "/tokenize": return self.send({"tokens": [0] * max(1, len(body.get("content", "")) // 4)})
        msgs = body.get("messages", [])
        last = msgs[-1]["content"] if msgs else ""
        if isinstance(last, list): last = " ".join(x.get("text", "") for x in last if isinstance(x, dict))
        if "response_format" in body:
            return self.send({"choices": [{"message": {"content": json.dumps({"add": ["Likes PETG for brackets"], "remove": []})}}]})
        words = re.findall(r"bracket \w+|\d+ ?mm", last)
        return self.send({"choices": [{"message": {"content": "Goal: design things. Key details: " + ", ".join(sorted(set(words))[:6])}}]})
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
