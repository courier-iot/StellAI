"""Runs every StellAI test:  python3 tests/run_tests.py  [name ...]

Each tests/test_*.py runs in its own process with the stand-in programs from tests/fakes on PATH (they pretend to
be Blender, FreeCAD, KiCad, SKiDL, Java, nvidia-smi and the model server), so no real tools or graphics card are
needed. GitHub runs this on every upload (.github/workflows/tests.yml). Exit code 0 = all passed.
"""
import glob
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    wanted = sys.argv[1:]
    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if wanted:
        files = [f for f in files if any(w in os.path.basename(f) for w in wanted)]
    # GitHub's web uploader doesn't keep the "executable" mark, so the fakes are copied and marked here
    bin_dir = tempfile.mkdtemp(prefix="stellai-fakes-")
    for f in glob.glob(os.path.join(HERE, "fakes", "*")):
        dest = os.path.join(bin_dir, os.path.basename(f))
        shutil.copy(f, dest)
        os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = dict(os.environ, STELLAI_TEST_FAKES=bin_dir, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""),
               PYTHONUNBUFFERED="1", STELLAI_UPDATE_INTERVAL="99999")
    results = []
    for f in files:
        name = os.path.basename(f)[:-3]
        started = time.time()
        try:
            p = subprocess.run([sys.executable, f], env=env, capture_output=True, text=True, timeout=600)
            ok, out = p.returncode == 0, p.stdout + p.stderr
        except subprocess.TimeoutExpired as e:
            ok, out = False, f"timed out after 600 s\n{e.stdout or ''}{e.stderr or ''}"
        secs = time.time() - started
        skipped = ok and "SKIPPED" in out
        results.append((name, ok, skipped))
        print(f"{'SKIP' if skipped else 'PASS' if ok else 'FAIL'}  {name}  ({secs:.0f} s)", flush=True)
        if not ok or os.environ.get("STELLAI_TEST_VERBOSE"):
            print("    " + "\n    ".join(l for l in out.strip().splitlines()[-40:]), flush=True)
    shutil.rmtree(bin_dir, ignore_errors=True)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
