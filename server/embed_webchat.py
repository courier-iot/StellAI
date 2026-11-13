"""Replaced by build_hub.py, which builds stellai_hub.py from server/hub/*.py, webchat.html and avatar.js.
Kept so old instructions still work:  python3 server/embed_webchat.py  runs the build."""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_hub.py"), run_name="__main__")
