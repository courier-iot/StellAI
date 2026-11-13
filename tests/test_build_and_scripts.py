"""The built hub is current, it passes its self-test, the docs list every web address, and the scripts parse."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

from helpers import ROOT, SERVER, check, done

py = sys.executable

# the one-file hub matches its parts (installed servers download that file)
p = subprocess.run([py, os.path.join(SERVER, "build_hub.py"), "--check"], capture_output=True, text=True)
check(p.returncode == 0, "stellai_hub.py is built from server/hub (run python3 server/build_hub.py)", p.stdout + p.stderr)

p = subprocess.run([py, os.path.join(SERVER, "stellai_hub.py"), "--self-test"], capture_output=True, text=True, timeout=300)
check("self-test ok" in p.stdout, "hub self-test", (p.stdout + p.stderr)[-600:])

# docs/api.md lists exactly the routes the hub answers
p = subprocess.run([py, os.path.join(SERVER, "stellai_hub.py"), "--list-routes"], capture_output=True, text=True)
table = p.stdout.strip()
check(table.startswith("| Method | Path |") and "`/prompts`" in table, "--list-routes prints the route table", table[:200])
api = open(os.path.join(ROOT, "docs", "api.md"), encoding="utf-8").read()
check(table in api, "docs/api.md is up to date (python3 server/stellai_hub.py --list-routes)")

# every Python file compiles
for f in ["server/stellai_tune.py", "server/build_hub.py", "windows/winsetup.py", "windows/stellai_app.py"]:
    try:
        compile(open(os.path.join(ROOT, f), encoding="utf-8").read(), f, "exec")
    except SyntaxError as e:
        check(False, f"{f} compiles", str(e))

# shell scripts parse, and the setup installs the hub and catalog files that sit next to it
bash = shutil.which("bash")
if bash:
    for f in ["server/stellai-setup.sh", "server/stellai-proxmox.sh", "server/stellai-display-setup.sh"]:
        if os.path.exists(os.path.join(ROOT, f)):
            p = subprocess.run([bash, "-n", os.path.join(ROOT, f)], capture_output=True, text=True)
            check(p.returncode == 0, f"{f} parses", p.stderr)
    setup = open(os.path.join(SERVER, "stellai-setup.sh"), encoding="utf-8").read()
    check("PYEOF" not in setup and "TUNEEOF" not in setup, "the setup script no longer carries copies of the hub")
    a = setup.index("say() {")
    b = setup.index("\n}\n", setup.index("install_companion() {")) + 3
    tmp = tempfile.mkdtemp()
    fn = os.path.join(tmp, "fn.sh")
    open(fn, "w").write("set -euo pipefail\n" + setup[a:b])
    dest = os.path.join(tmp, "llm", "stellai_hub.py")

    def install(script_dir):
        return subprocess.run([bash, "-c", f'source "{fn}"; SCRIPT_DIR="{script_dir}"; STELLAI_REPO=none/none; '
                                           f'install_companion stellai_hub.py "{dest}"'], capture_output=True, text=True)
    p = install(SERVER)
    check(p.returncode == 0 and open(dest).read() == open(os.path.join(SERVER, "stellai_hub.py")).read(),
          "setup installs the hub from next to it", p.stderr)
    with open(dest, "a") as f:
        f.write("# auto-updated\n")
    p = install(os.path.dirname(dest))
    check(p.returncode == 0 and open(dest).read().endswith("# auto-updated\n"),
          "re-running setup from the install folder keeps the installed hub", p.stderr)
    os.makedirs(os.path.join(tmp, "empty"))
    env_no_net = dict(os.environ, https_proxy="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9")
    p = subprocess.run([bash, "-c", f'source "{fn}"; SCRIPT_DIR="{tmp}/empty"; STELLAI_REPO=none/none; '
                                    f'install_companion stellai_hub.py "{tmp}/x.py"'], capture_output=True, text=True, env=env_no_net)
    check(p.returncode != 0 and "git clone" in p.stdout + p.stderr, "setup explains what to do when the hub is missing",
          p.stdout + p.stderr)

# the web chat's script parses (needs Node; skipped without it)
node = shutil.which("node")
if node:
    html = open(os.path.join(SERVER, "webchat.html"), encoding="utf-8").read()
    js = re.search(r"<script>([\s\S]*)</script>", html).group(1)
    f = os.path.join(tempfile.mkdtemp(), "wc.js")
    open(f, "w", encoding="utf-8").write(js)
    p = subprocess.run([node, "--check", f], capture_output=True, text=True)
    check(p.returncode == 0, "the web chat's script parses", p.stderr[-400:])
    f2 = os.path.join(SERVER, "avatar.js")
    p = subprocess.run([node, "--check", f2], capture_output=True, text=True)
    check(p.returncode == 0, "avatar.js parses", p.stderr[-400:])
done()
