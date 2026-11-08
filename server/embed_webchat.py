"""Copies webchat.html and avatar.js into stellai_hub.py (the hub stays a single file, so auto-updates bring the web chat too).
Run after editing either:  python3 server/embed_webchat.py"""
import os
import re

here = os.path.dirname(os.path.abspath(__file__))
html = open(os.path.join(here, "webchat.html"), encoding="utf-8").read()
assert '"""' not in html and "\\N" not in html
hub_path = os.path.join(here, "stellai_hub.py")
hub = open(hub_path, encoding="utf-8").read()
new = 'WEBCHAT_HTML = r"""' + html + '"""  # end of web chat'
hub, n = re.subn(r'WEBCHAT_HTML = r""".*?"""  # end of web chat', lambda m: new, hub, flags=re.S)
assert n == 1, "marker not found"
js = open(os.path.join(here, "avatar.js"), encoding="utf-8").read()
assert '"""' not in js and "\\N" not in js
new_js = 'AVATAR_JS = r"""' + js + '"""  # end of avatar script'
hub, n = re.subn(r'AVATAR_JS = r""".*?"""  # end of avatar script', lambda m: new_js, hub, flags=re.S)
assert n == 1, "avatar marker not found"
open(hub_path, "w", encoding="utf-8").write(hub)
print("embedded", len(html), "+", len(js), "characters")
