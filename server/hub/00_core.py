# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
#!/usr/bin/env python3
"""
StellAI hub: speech-to-text and chat sync for the StellAI phone app.

  Phone app --(HTTP, port 8092)--> hub
     POST /transcribe  phone audio (after the wake word) -> Whisper (127.0.0.1:8093) -> text
     POST /sync        chat history sync between devices (SQLite)
     GET  /tools       server-side tools Stella can use (calculator, electronics, diagrams, memory)
     POST /tools/NAME  run one of them
     GET  /diagrams/ID.png  a rendered diagram
     PUT/GET /images/ID.jpg  photos sent in chats
     GET  /memories, POST /memories, DELETE /memories/ID, POST /memories/learn   long-term memory
     GET  /wakewords, GET /wakewords/NAME.onnx   custom wake word models
     GET  /health

Standard library only. Usage:
  python3 stellai_hub.py --key-file ~/llm/api-key.txt --db ~/llm/chats.db
"""

import argparse
import hmac
import io
import json
import re
import socket
import base64
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def trust_more_certificates():
    """HTTPS downloads trust the system's certificates plus Mozilla's list (certifi, when installed).

    Windows only fetches some root certificates when its own programs need them, so Python on a fresh
    Windows PC can fail with CERTIFICATE_VERIFY_FAILED for sites like python.org. Certificates are still
    checked: this only adds trusted roots."""
    import ssl
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:   # noqa: BLE001 - certifi is optional; the system's certificates still apply
            pass
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)))
    except Exception:       # noqa: BLE001 - keep Python's default if anything goes wrong
        pass


trust_more_certificates()


def _is_cert_error(e):
    return "CERTIFICATE_VERIFY_FAILED" in str(e) or "certificate verify failed" in str(e)


def _windows_download(url, dest):
    """Downloads with Windows' own HTTPS stack, which fetches missing root certificates by itself."""
    env = {**os.environ, "STELLAI_URL": url, "STELLAI_OUT": dest}
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "$ProgressPreference='SilentlyContinue'; "
                        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
                        "Invoke-WebRequest -UseBasicParsing -Uri $env:STELLAI_URL -OutFile $env:STELLAI_OUT"],
                       capture_output=True, text=True, timeout=3600, env=env,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not os.path.isfile(dest):
        raise OSError("download failed: " + (r.stderr or r.stdout).strip()[-300:])

MAX_SECONDS = 60

# Things Whisper tends to "hear" in silence or noise.
NOISE_PATTERNS = re.compile(
    r"^\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*)\s*$|^\s*(thank you\.?|thanks for watching!?|you|\.)\s*$",
    re.IGNORECASE,
)


LOG_HOOK = None     # optional callable(str), used by the Windows app's log window


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except (AttributeError, OSError, ValueError):
        pass  # no console (Windows app without a terminal)
    if LOG_HOOK:
        try:
            LOG_HOOK(line)
        except Exception:
            pass


