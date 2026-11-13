"""Builds server/stellai_hub.py from its parts.

The hub is edited as sections in server/hub/*.py (00_core.py, 01_chat_store.py, … 70_main.py) plus
server/webchat.html and server/avatar.js. Installed servers update by downloading the one built file, so it
stays a single file: this script joins the parts in name order (they share one namespace, as if they were one
file) and puts the web chat page and avatar script where a part says EMBED("file").

    python3 server/build_hub.py           # rebuild stellai_hub.py
    python3 server/build_hub.py --check   # fail if stellai_hub.py isn't up to date (GitHub runs this)
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PART_HEAD = "# Part of stellai_hub.py:"
NOTICE = ("# Built by server/build_hub.py from server/hub/*.py, webchat.html and avatar.js: edit those, then run\n"
          "# `python3 server/build_hub.py`. (Kept as one file because installed servers update by downloading it.)\n")
EMBED = re.compile(r'^(\w+) = EMBED\("([\w.-]+)"\)  (# end of [^(]*?)  \(build_hub\.py puts [\w.-]+ here\)$', re.M)


def build():
    parts = sorted(glob.glob(os.path.join(HERE, "hub", "[0-9][0-9]_*.py")))
    if not parts:
        raise SystemExit("no parts in server/hub/")
    out = []
    for p in parts:
        text = open(p, encoding="utf-8").read()
        if text.startswith(PART_HEAD):
            text = text.split("\n", 1)[1]
        out.append(text)
    src = "".join(out)

    def embed(m):
        name, file, marker = m.groups()
        content = open(os.path.join(HERE, file), encoding="utf-8").read()
        if '"""' in content or "\\N" in content:
            raise SystemExit(f'{file} can\'t contain """ or \\N (it goes inside a Python r-string)')
        return f'{name} = r"""' + content + '"""  ' + marker
    src, n = EMBED.subn(embed, src)
    if n != 2:
        raise SystemExit(f"expected 2 EMBED lines (web chat, avatar), found {n}")
    first, rest = src.split("\n", 1)
    if not first.startswith("#!"):
        raise SystemExit("00_core.py must start with the #! line")
    return first + "\n" + NOTICE + rest


def main():
    target = os.path.join(HERE, "stellai_hub.py")
    new = build()
    compile(new, "stellai_hub.py", "exec")
    try:
        cur = open(target, encoding="utf-8").read()
    except OSError:
        cur = ""
    if "--check" in sys.argv:
        if cur != new:
            raise SystemExit("server/stellai_hub.py is out of date: run  python3 server/build_hub.py  and upload it too")
        print("stellai_hub.py is up to date")
        return
    if cur != new:
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
    parts = len(glob.glob(os.path.join(HERE, "hub", "[0-9][0-9]_*.py")))
    print(f"built stellai_hub.py from {parts} parts ({new.count(chr(10)) + 1} lines)")


if __name__ == "__main__":
    main()
