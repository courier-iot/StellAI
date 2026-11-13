"""The Windows app's logic tests (windows/test_winsetup.py) run here too; they don't need Windows."""
import os
import subprocess
import sys

from helpers import ROOT, check, done

p = subprocess.run([sys.executable, os.path.join(ROOT, "windows", "test_winsetup.py")], capture_output=True, text=True,
                   cwd=os.path.join(ROOT, "windows"), timeout=300)
check(p.returncode == 0 and "all tests passed" in p.stdout, "windows/test_winsetup.py", (p.stdout + p.stderr)[-800:])
done()
