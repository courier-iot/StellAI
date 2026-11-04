#!/usr/bin/env bash
# StellAI server setup for Linux Mint (ThinkPad T470, desktops, anything x86-64)
# Detects the CPU, RAM and NVIDIA GPU, picks a Qwen3.5 model that fits,
# builds llama.cpp (with CUDA when there's an NVIDIA card) and whisper.cpp,
# enables zram, and adds launchers: local chat, and a network server for the
# phone app (AI, voice commands, chat sync).
#
# Usage:  chmod +x stellai-setup.sh && ./stellai-setup.sh
#         MODEL_CHOICE=2 ./stellai-setup.sh   (skip the question: 1=4B, 2=9B, 3=35B-A3B)

set -euo pipefail

# ---- Settings you can change ------------------------------------------------
LLM_DIR="$HOME/llm"
STELLAI_REPO="${STELLAI_REPO:-courier-iot/StellAI}"   # where updates come from
PORT=8080
# The model is chosen automatically below (you can override it when asked).
# -----------------------------------------------------------------------------

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m!!  %s\033[0m\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user (it will ask for sudo when needed)."; exit 1
fi

mkdir -p "$LLM_DIR/models"

# 0. Detect hardware and choose a model ----------------------------------------
RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1048576 + 0.5}' /proc/meminfo)
PHYS_CORES=$(lscpu -p=core 2>/dev/null | grep -v '^#' | sort -u | wc -l)
[[ "$PHYS_CORES" -lt 1 ]] && PHYS_CORES=2
CPU_NAME=$(lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1)
GPU_NAME=""
VRAM_GB=0
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
  VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -dc '0-9')
  VRAM_GB=$(( (${VRAM_MB:-0} + 512) / 1024 ))
elif lspci 2>/dev/null | grep -Ei 'vga|3d' | grep -qi nvidia; then
  warn "This computer has an NVIDIA graphics card, but its driver isn't installed."
  echo "    To use it: Menu > Driver Manager > choose the recommended NVIDIA driver > Apply Changes,"
  echo "    reboot, then run this script again."
  read -r -p "Continue without the graphics card for now? [y/N] " ans
  [[ "${ans,,}" == "y" ]] || exit 1
fi

LOGICAL_CPUS=$(nproc)

# 0b. Slow computer? Use a stronger StellAI server instead ----------------------
# A 2-core laptop without a usable graphics card answers far slower than a desktop with a GPU, and every device
# shares the same chats anyway. So weak machines are offered "client mode": just the desktop chat, pointed at the
# stronger server. (Force it with CLIENT_ONLY=1, skip it with CLIENT_ONLY=0.)
WEAK=0
if [[ $VRAM_GB -eq 0 && $PHYS_CORES -le 2 ]] || [[ $RAM_GB -lt 8 ]]; then WEAK=1; fi
if [[ "${CLIENT_ONLY:-}" == "1" ]] || [[ $WEAK -eq 1 && "${CLIENT_ONLY:-}" != "0" ]]; then
  say "This computer is slow for running Stella itself"
  echo "    ${CPU_NAME:-CPU} with ${PHYS_CORES} cores, ${RAM_GB} GB RAM and no usable graphics card."
  echo "    If you have a stronger StellAI server (e.g. a desktop with a GPU), this computer can simply use it:"
  echo "    the same chats, much faster answers, and nothing big to install here."
  read -r -p "Use another StellAI server instead of running Stella on this computer? [Y/n] " ans
  if [[ "${ans,,}" != "n" ]]; then
    read -r -p "Server address (its Tailscale address works at home and away, e.g. 100.101.102.103): " SRV
    SRV=${SRV#http://}; SRV=${SRV#https://}; SRV=${SRV%%/*}; SRV=${SRV%%:*}
    read -r -s -p "API key (shown by StellAI Server on that computer): " SKEY; echo
    umask 077
    printf '%s' "$SKEY" > "$LLM_DIR/client-key.txt"
    printf 'SERVER="%s"\n' "$SRV" > "$LLM_DIR/client.conf"
    umask 022
    mkdir -p "$HOME/.local/share/applications"
    cat > "$LLM_DIR/open-web-chat.sh" <<EOF
#!/usr/bin/env bash
# Opens StellAI on the server $SRV (client mode: this computer doesn't run the model)
xdg-open "http://$SRV:8092/#key=\$(cat "$LLM_DIR/client-key.txt")"
EOF
    chmod +x "$LLM_DIR/open-web-chat.sh"
    cat > "$HOME/.local/share/applications/stellai-web-chat.desktop" <<EOF
[Desktop Entry]
Name=StellAI
Comment=Chat with Stella (runs on $SRV)
Exec=$LLM_DIR/open-web-chat.sh
Icon=internet-chat
Terminal=false
Type=Application
Categories=Utility;
EOF
    if ! command -v tailscale >/dev/null; then
      read -r -p "Install Tailscale so this computer can reach the server away from home? [Y/n] " ts
      if [[ "${ts,,}" != "n" ]]; then
        curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up || warn "Tailscale didn't finish; run 'sudo tailscale up' later"
      fi
    fi
    if curl -fsS -m 5 "http://$SRV:8092/health" >/dev/null 2>&1; then
      say "Done! Open \"StellAI\" from the menu (server $SRV answers)"
    else
      warn "Couldn't reach http://$SRV:8092 right now. Make sure StellAI Server is running there; then open \"StellAI\" from the menu."
    fi
    echo "    To run Stella on this computer after all: CLIENT_ONLY=0 $0"
    exit 0
  fi
fi

# Qwen3.5 models (Unsloth GGUF builds). Sizes are download sizes.
#   1: 4B          ~2.7 GB      any machine with 8 GB RAM
#   2: 9B          ~4.7-5.7 GB  16 GB RAM, or fully on a 6 GB+ GPU
#   3: 35B-A3B MoE ~13.6 GB     needs ~18 GB RAM. With an NVIDIA GPU the always-used
#                               parts run on the GPU and the "experts" stay in RAM.
#                               Smartest, and fast: only ~3B parameters per word.
if [[ $VRAM_GB -ge 5 && $RAM_GB -ge 18 ]]; then RECOMMENDED=3
elif [[ $VRAM_GB -ge 5 || $RAM_GB -ge 14 ]]; then RECOMMENDED=2
else RECOMMENDED=1
fi

say "Detected hardware"
echo "    CPU: ${CPU_NAME:-unknown} (${PHYS_CORES} cores)"
echo "    RAM: ${RAM_GB} GB"
if [[ -n "$GPU_NAME" ]]; then echo "    GPU: $GPU_NAME (${VRAM_GB} GB)"; else echo "    GPU: none usable (CPU only)"; fi
echo
echo "    1) Qwen3.5 4B        - quick and light (laptops)"
echo "    2) Qwen3.5 9B        - smarter; needs 16 GB RAM or a 6 GB GPU"
echo "    3) Qwen3.5 35B-A3B   - smartest; needs ~18 GB RAM (best with an NVIDIA GPU)"
CHOICE="${MODEL_CHOICE:-}"
if [[ -z "$CHOICE" ]]; then
  read -r -p "Model to install [recommended: $RECOMMENDED]: " CHOICE
fi
[[ "$CHOICE" =~ ^[123]$ ]] || CHOICE=$RECOMMENDED

GPU_ARGS=""
case "$CHOICE" in
  1)
    MODEL_LABEL="Qwen3.5 4B"
    MODEL_REPO="unsloth/Qwen3.5-4B-GGUF"; MODEL_NAME="Qwen3.5-4B-Q4_K_M.gguf"
    CONTEXT=4096; NET_CONTEXT=8192
    if [[ $VRAM_GB -ge 4 ]]; then GPU_ARGS="-ngl 99"; fi
    ;;
  2)
    MODEL_LABEL="Qwen3.5 9B"
    MODEL_REPO="unsloth/Qwen3.5-9B-GGUF"
    if [[ $VRAM_GB -ge 5 && $VRAM_GB -lt 8 ]]; then
      MODEL_NAME="Qwen3.5-9B-Q3_K_M.gguf"   # 3-bit so it fits entirely on a 6 GB card
    else
      MODEL_NAME="Qwen3.5-9B-Q4_K_M.gguf"
    fi
    CONTEXT=8192; NET_CONTEXT=8192
    if [[ $VRAM_GB -ge 5 ]]; then GPU_ARGS="-ngl 99"; fi
    ;;
  3)
    MODEL_LABEL="Qwen3.5 35B-A3B"
    MODEL_REPO="unsloth/Qwen3.5-35B-A3B-GGUF"
    # Best quality that fits RAM + GPU memory (more bits = more accurate, especially for code)
    if (( RAM_GB + VRAM_GB >= 40 )); then MODEL_NAME="Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf"     # 22.2 GB
    elif (( RAM_GB + VRAM_GB >= 30 )); then MODEL_NAME="Qwen3.5-35B-A3B-UD-IQ4_XS.gguf"   # 17.5 GB
    else MODEL_NAME="Qwen3.5-35B-A3B-UD-IQ3_S.gguf"                                        # 13.6 GB
    fi
    CONTEXT=16384; NET_CONTEXT=16384
    # Experts stay in RAM; everything else goes on the GPU (if there is one).
    if [[ $VRAM_GB -ge 4 ]]; then GPU_ARGS="-ngl 99 --cpu-moe"; fi
    # The experts live in RAM, which leaves GPU room for a long context (32K tokens: longer chats before a refresh)
    if [[ $VRAM_GB -ge 5 ]]; then NET_CONTEXT=32768; fi
    if [[ $RAM_GB -lt 18 ]]; then
      warn "Only ${RAM_GB} GB RAM: the 35B model will partly run from disk and be slow."
    fi
    ;;
esac
MODEL_URL="https://huggingface.co/$MODEL_REPO/resolve/main/$MODEL_NAME"
MODEL_FILE="$LLM_DIR/models/$MODEL_NAME"

# Speech recognition: the more accurate "small" model on stronger machines.
if [[ -n "${WHISPER_NAME:-}" ]]; then
  WHISPER_SIZE="your choice"            # e.g. WHISPER_NAME=ggml-small.en.bin ./stellai-setup.sh
elif [[ $RAM_GB -ge 14 && $PHYS_CORES -ge 6 ]]; then
  WHISPER_NAME="ggml-small.en.bin"; WHISPER_SIZE="~470 MB"
else
  WHISPER_NAME="ggml-base.en.bin"; WHISPER_SIZE="~150 MB"
fi
WHISPER_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER_NAME"
WHISPER_MODEL="$LLM_DIR/models/$WHISPER_NAME"
echo "    Installing: $MODEL_LABEL ($MODEL_NAME); speech recognition: $WHISPER_NAME"

# 1. Packages ------------------------------------------------------------------
say "Installing build tools"
sudo apt update
sudo apt install -y build-essential cmake git curl libcurl4-openssl-dev zram-tools

# 2. zram (compressed RAM swap) ------------------------------------------------
say "Configuring zram (compressed swap in RAM)"
sudo tee /etc/default/zramswap >/dev/null <<'EOF'
ALGO=zstd
PERCENT=60
PRIORITY=100
EOF
sudo systemctl enable --now zramswap.service || warn "zram service did not start; check 'systemctl status zramswap'"
# Prefer zram over disk swap, and keep the model in RAM when possible
echo 'vm.swappiness=100' | sudo tee /etc/sysctl.d/99-llm-zram.conf >/dev/null
echo 'vm.page-cluster=0' | sudo tee -a /etc/sysctl.d/99-llm-zram.conf >/dev/null
sudo sysctl --system >/dev/null

# 3. Build llama.cpp (CPU optimizations, plus CUDA for NVIDIA GPUs) ------------
if [[ -d "$LLM_DIR/llama.cpp/.git" ]]; then
  git -C "$LLM_DIR/llama.cpp" pull --ff-only
else
  git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLM_DIR/llama.cpp"
fi
cd "$LLM_DIR/llama.cpp"

build_llama() {
  rm -rf build
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DLLAMA_CURL=ON "$@" &&
    cmake --build build --config Release -j"$(nproc)" --target llama-server llama-cli llama-bench
}

BACKEND=cpu
if [[ $VRAM_GB -gt 0 ]]; then
  say "Installing the CUDA toolkit for the NVIDIA GPU (large download, ~3 GB)"
  if sudo apt install -y nvidia-cuda-toolkit gcc-12 g++-12; then
    HOST_CXX=()
    if command -v g++-12 >/dev/null; then HOST_CXX=(-DCMAKE_CUDA_HOST_COMPILER=g++-12); fi
    say "Building llama.cpp with CUDA (~15-30 min)"
    if build_llama -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=native "${HOST_CXX[@]}"; then
      BACKEND=cuda
    fi
  fi
  if [[ $BACKEND == cpu ]]; then
    warn "The CUDA build didn't work; trying the Vulkan GPU backend instead"
    if sudo apt install -y libvulkan-dev glslc && build_llama -DGGML_VULKAN=ON; then
      BACKEND=vulkan
    fi
  fi
fi
if [[ $BACKEND == cpu ]]; then
  if [[ $VRAM_GB -gt 0 ]]; then warn "GPU builds failed; using the CPU only"; fi
  say "Building llama.cpp for this CPU (~10-20 min on a laptop)"
  build_llama
  GPU_ARGS="${GPU_ARGS/-ngl 99/}"   # no GPU layers without a GPU build
fi
if [[ $BACKEND != cpu ]]; then
  # llama-server's --fit (on by default) places as many layers and MoE experts on the GPU as fit, keeping a
  # safety margin; so no fixed -ngl / --cpu-moe. A bigger micro-batch speeds up reading long prompts on a GPU.
  GPU_ARGS="-ub 1024"
fi
say "llama.cpp built with: $BACKEND"

# 3b. Build whisper.cpp (speech-to-text for the phone's voice commands) -------
say "Building whisper.cpp for voice dictation (~5-10 min)"
if [[ -d "$LLM_DIR/whisper.cpp/.git" ]]; then
  git -C "$LLM_DIR/whisper.cpp" pull --ff-only
else
  git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$LLM_DIR/whisper.cpp"
fi
cd "$LLM_DIR/whisper.cpp"
build_whisper() {
  rm -rf build
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON "$@" &&
    cmake --build build --config Release -j"$(nproc)" --target whisper-server
}
WHISPER_GPU=0
if [[ $BACKEND == cuda ]] && build_whisper -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=native "${HOST_CXX[@]}"; then
  WHISPER_GPU=1
elif [[ $BACKEND == vulkan ]] && build_whisper -DGGML_VULKAN=ON; then
  WHISPER_GPU=1
else
  build_whisper
fi
[[ $WHISPER_GPU -eq 1 ]] && say "whisper.cpp uses the GPU (voice commands transcribe much faster)"

if [[ ! -s "$WHISPER_MODEL" ]]; then
  say "Downloading Whisper speech model $WHISPER_NAME ($WHISPER_SIZE)"
  curl -L --fail --continue-at - -o "$WHISPER_MODEL" "$WHISPER_URL"
fi

# 3b2. Search model: meaning-based search in project files and the library (~140 MB, runs on the CPU)
EMBED_NAME="nomic-embed-text-v1.5.Q8_0.gguf"
EMBED_MODEL="$LLM_DIR/models/$EMBED_NAME"
if [[ ! -s "$EMBED_MODEL" ]]; then
  say "Downloading the search model for project files ($EMBED_NAME, ~140 MB)"
  if curl -L --fail --continue-at - -o "$EMBED_MODEL.part" \
      "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/$EMBED_NAME"; then
    mv "$EMBED_MODEL.part" "$EMBED_MODEL"
  else
    warn "Couldn't download the search model; file search will use keywords only"
  fi
fi

# 3c. Diagram tools: schemdraw (schematics), matplotlib (timing diagrams), Mermaid (block diagrams)
say "Installing diagram tools (schematics, timing and block diagrams)"
sudo apt install -y python3-venv python3-pip nodejs npm
if [[ ! -x "$LLM_DIR/venv/bin/python" ]]; then
  python3 -m venv "$LLM_DIR/venv"
fi
"$LLM_DIR/venv/bin/pip" install --upgrade --quiet schemdraw matplotlib ||
  warn "Couldn't install schemdraw/matplotlib; schematics and timing diagrams won't work"
# Project files: PDF datasheets
"$LLM_DIR/venv/bin/pip" install --upgrade --quiet pypdf || sudo apt install -y poppler-utils ||
  warn "Couldn't install a PDF reader; PDF files in projects won't work"
mkdir -p "$LLM_DIR/mermaid" "$LLM_DIR/diagrams" "$LLM_DIR/images" "$LLM_DIR/wakewords" "$LLM_DIR/projects"
if ! npm install --prefix "$LLM_DIR/mermaid" --no-fund --no-audit @mermaid-js/mermaid-cli; then
  warn "Couldn't install Mermaid (needs Node.js 18.19 or newer); block diagrams won't work"
fi

# Hub: phone speech-to-text, chat sync between devices, and Stella's server tools
cat > "$LLM_DIR/stellai_hub.py" <<'PYEOF'
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


# ---- Reminders and lists ---------------------------------------------------------
REPEATS = ("none", "daily", "weekdays", "weekly", "monthly")


def _parse_when(text):
    """ISO 8601 date-time (ideally with a UTC offset, as the phone's clock gives) -> epoch ms."""
    from datetime import datetime
    t = str(text or "").strip().replace(" ", "T", 1)
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise ValueError("give the time as ISO 8601, e.g. 2026-11-04T17:00:00-06:00")
    if dt.tzinfo is None:
        dt = dt.astimezone()   # no offset given: the server's local time
    return int(dt.timestamp() * 1000)


def _next_due(due, repeat, now=None):
    """Next occurrence after `now` for a repeating reminder."""
    from datetime import datetime, timedelta
    now = now or int(time.time() * 1000)
    dt = datetime.fromtimestamp(due / 1000).astimezone()
    while int(dt.timestamp() * 1000) <= now:
        if repeat == "daily":
            dt += timedelta(days=1)
        elif repeat == "weekly":
            dt += timedelta(weeks=1)
        elif repeat == "weekdays":
            dt += timedelta(days=1)
            while dt.weekday() >= 5:
                dt += timedelta(days=1)
        elif repeat == "monthly":
            month = dt.month % 12 + 1
            year = dt.year + (1 if dt.month == 12 else 0)
            day = min(dt.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31,
                               30, 31, 31, 30, 31, 30, 31][month - 1])
            dt = dt.replace(year=year, month=month, day=day)
        else:
            break
    return int(dt.timestamp() * 1000)


def _fmt_time(ms):
    from datetime import datetime
    return datetime.fromtimestamp(ms / 1000).astimezone().strftime("%a %b %d, %H:%M")


class TaskStoreMixin:
    """Reminders (ring on every phone) and simple named lists (shopping, parts, to-do)."""

    def _tasks_schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reminders(
                id TEXT PRIMARY KEY, text TEXT NOT NULL, due_at INTEGER NOT NULL, repeat TEXT NOT NULL DEFAULT 'none',
                done INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS list_items(
                id TEXT PRIMARY KEY, list TEXT NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS list_name ON list_items(list);
        """)

    def reminders(self, include_done_days=3):
        now = int(time.time() * 1000)
        with self.lock:
            # Repeating reminders that went by without being marked done move on to their next time
            for rid, due, rep in self.db.execute(
                    "SELECT id, due_at, repeat FROM reminders WHERE done=0 AND repeat!='none' AND due_at < ?",
                    (now - 6 * 3600_000,)).fetchall():
                self.db.execute("UPDATE reminders SET due_at=?, updated_at=? WHERE id=?",
                                (_next_due(due, rep, now), now, rid))
            self.db.execute("DELETE FROM reminders WHERE done=1 AND updated_at < ?", (now - 30 * 86400_000,))
            self.db.commit()
            rows = self.db.execute(
                "SELECT id, text, due_at, repeat, done, updated_at FROM reminders WHERE done=0 OR updated_at > ? "
                "ORDER BY due_at", (now - include_done_days * 86400_000,)).fetchall()
        return [{"id": r[0], "text": r[1], "due_at": r[2], "repeat": r[3], "done": bool(r[4]), "updated_at": r[5]}
                for r in rows]

    def add_reminder(self, text, due_at, repeat="none"):
        text = " ".join(str(text).split())[:300]
        if not text:
            raise ValueError("what should the reminder say?")
        repeat = repeat if repeat in REPEATS else "none"
        now = int(time.time() * 1000)
        if due_at < now - 60_000:
            if repeat == "none":
                raise ValueError(f"that time ({_fmt_time(due_at)}) has already passed")
            due_at = _next_due(due_at, repeat, now)
        rid = uuid.uuid4().hex[:10]
        with self.lock:
            self.db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,?)", (rid, text, int(due_at), repeat, 0, now, now))
            self.db.commit()
        return {"id": rid, "text": text, "due_at": int(due_at), "repeat": repeat, "done": False, "updated_at": now}

    def update_reminder(self, rid, done=None, snooze_minutes=None, cancel=False):
        now = int(time.time() * 1000)
        with self.lock:
            r = self.db.execute("SELECT due_at, repeat FROM reminders WHERE id=?", (rid,)).fetchone()
            if not r:
                return None
            due, rep = r
            if cancel:
                self.db.execute("UPDATE reminders SET done=1, repeat='none', updated_at=? WHERE id=?", (now, rid))
            elif snooze_minutes:
                self.db.execute("UPDATE reminders SET due_at=?, done=0, updated_at=? WHERE id=?",
                                (now + int(snooze_minutes) * 60_000, now, rid))
            elif done:
                if rep != "none":
                    self.db.execute("UPDATE reminders SET due_at=?, updated_at=? WHERE id=?",
                                    (_next_due(max(due, now), rep, now), now, rid))
                else:
                    self.db.execute("UPDATE reminders SET done=1, updated_at=? WHERE id=?", (now, rid))
            self.db.commit()
        return next((x for x in self.reminders() if x["id"] == rid), None)

    def find_reminders(self, words):
        w = str(words).strip().lower()
        return [r for r in self.reminders(0) if not r["done"] and (r["id"] == w or (w and w in r["text"].lower()))]

    # lists
    def lists(self, include_done=True):
        with self.lock:
            rows = self.db.execute("SELECT id, list, text, done FROM list_items ORDER BY list, done, created_at").fetchall()
        out = {}
        for rid, name, text, done in rows:
            if done and not include_done:
                continue
            out.setdefault(name, []).append({"id": rid, "text": text, "done": bool(done)})
        return out

    @staticmethod
    def _list_name(name):
        n = " ".join(str(name or "").lower().split())[:40]
        n = re.sub(r"\s+list$", "", n) or "to-do"
        return n

    def add_items(self, name, items):
        name = self._list_name(name)
        now = int(time.time() * 1000)
        added = []
        with self.lock:
            existing = {r[0].lower(): r[1] for r in self.db.execute(
                "SELECT text, id FROM list_items WHERE list=?", (name,))}
            for it in items:
                t = " ".join(str(it).split())[:200]
                if not t:
                    continue
                if t.lower() in existing:   # already there: un-tick it
                    self.db.execute("UPDATE list_items SET done=0, updated_at=? WHERE id=?", (now, existing[t.lower()]))
                else:
                    self.db.execute("INSERT INTO list_items VALUES(?,?,?,?,?,?)",
                                    (uuid.uuid4().hex[:10], name, t, 0, now, now))
                added.append(t)
            self.db.commit()
        return name, added

    def set_item(self, iid, done=None, delete=False):
        now = int(time.time() * 1000)
        with self.lock:
            if delete:
                cur = self.db.execute("DELETE FROM list_items WHERE id=?", (iid,))
            else:
                cur = self.db.execute("UPDATE list_items SET done=?, updated_at=? WHERE id=?", (1 if done else 0, now, iid))
            self.db.commit()
        return cur.rowcount > 0

    def check_items(self, name, words, delete=False):
        name = self._list_name(name)
        changed = []
        for item in self.lists().get(name, []):
            for w in words:
                w = str(w).strip().lower()
                if w and (w in item["text"].lower() or item["text"].lower() in w):
                    self.set_item(item["id"], done=True, delete=delete)
                    changed.append(item["text"])
                    break
        return name, changed

    def clear_done(self, name):
        with self.lock:
            cur = self.db.execute("DELETE FROM list_items WHERE list=? AND done=1", (self._list_name(name),))
            self.db.commit()
        return cur.rowcount


CHATS_PATH = "chats.db"
LIBRARY_ID = "library"   # the shared document library is stored as a pseudo-project


class ProjectStoreMixin:
    """Projects live in the same database as chats. Mixed into ChatDB."""

    MAX_NOTES = 200

    def _bump(self, pid):
        self.project_versions[pid] = self.project_versions.get(pid, 0) + 1

    def project_version(self, pid):
        return self.project_versions.get(pid, 0)

    def projects(self):
        with self.lock:
            rows = self.db.execute("SELECT id FROM projects ORDER BY updated_at DESC").fetchall()
        return [self.project(r[0]) for r in rows]

    def project(self, pid):
        if pid == LIBRARY_ID:
            return self.library()
        with self.lock:
            r = self.db.execute("SELECT id, name, color, instructions, created_at, updated_at FROM projects WHERE id=?",
                                (str(pid),)).fetchone()
            if not r:
                return None
            notes = self.db.execute("SELECT id, text, source, created_at FROM project_notes WHERE project_id=? "
                                    "ORDER BY created_at", (r[0],)).fetchall()
            files = self.db.execute("SELECT id, name, kind, size, tokens, chunks, note, created_at FROM project_files "
                                    "WHERE project_id=? ORDER BY created_at", (r[0],)).fetchall()
            chats = self.db.execute("SELECT COUNT(*) FROM conversations WHERE project_id=? AND deleted=0",
                                    (r[0],)).fetchone()[0]
        return {"id": r[0], "name": r[1], "color": r[2], "instructions": r[3], "created_at": r[4], "updated_at": r[5],
                "chats": chats,
                "notes": [{"id": n[0], "text": n[1], "source": n[2], "created_at": n[3]} for n in notes],
                "files": [{"id": f[0], "name": f[1], "kind": f[2], "size": f[3], "tokens": f[4], "chunks": f[5],
                           "note": f[6], "created_at": f[7]} for f in files]}

    def library(self):
        """The shared document library: files any chat can search (stored like a project's files)."""
        with self.lock:
            files = self.db.execute("SELECT id, name, kind, size, tokens, chunks, note, created_at FROM project_files "
                                    "WHERE project_id=? ORDER BY created_at", (LIBRARY_ID,)).fetchall()
        return {"id": LIBRARY_ID, "name": "Library", "color": "cyan", "instructions": "", "created_at": 0,
                "updated_at": 0, "chats": 0, "notes": [],
                "files": [{"id": f[0], "name": f[1], "kind": f[2], "size": f[3], "tokens": f[4], "chunks": f[5],
                           "note": f[6], "created_at": f[7]} for f in files]}

    def chunk_texts(self, fid):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT text FROM project_chunks WHERE file_id=? ORDER BY idx", (fid,))]

    def file_project(self, fid):
        with self.lock:
            r = self.db.execute("SELECT project_id FROM project_files WHERE id=?", (fid,)).fetchone()
        return r[0] if r else None

    def save_vectors(self, fid, vecs):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO chunk_vecs VALUES(?,?,?)",
                                [(fid, i, v) for i, v in enumerate(vecs)])
            self.db.commit()
            pid = self.db.execute("SELECT project_id FROM project_files WHERE id=?", (fid,)).fetchone()
            if pid:
                self._bump(pid[0])

    def vectors(self, pid):
        """{(file_id, idx): blob} for a project's chunks."""
        with self.lock:
            rows = self.db.execute("""SELECT v.file_id, v.idx, v.vec FROM chunk_vecs v JOIN project_files f
                                      ON f.id = v.file_id WHERE f.project_id=?""", (pid,)).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def files_without_vectors(self):
        with self.lock:
            return [r[0] for r in self.db.execute(
                """SELECT f.id FROM project_files f WHERE f.chunks > 0 AND
                   (SELECT COUNT(*) FROM chunk_vecs v WHERE v.file_id = f.id) < f.chunks ORDER BY f.created_at""")]

    def save_project(self, data):
        name = " ".join(str(data.get("name", "")).split())[:60]
        if not name:
            raise ValueError("the project needs a name")
        color = str(data.get("color") or "cyan")
        color = color if color in COLORS else "cyan"
        instructions = str(data.get("instructions", ""))[:6000]
        pid = str(data.get("id") or "")
        if pid and not re.fullmatch(r"[0-9a-f]{12}", pid):
            raise ValueError("bad project id")
        now = int(time.time() * 1000)
        with self.lock:
            exists = pid and self.db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone()
            if exists:
                self.db.execute("UPDATE projects SET name=?, color=?, instructions=?, updated_at=? WHERE id=?",
                                (name, color, instructions, now, pid))
            else:
                pid = pid or uuid.uuid4().hex[:12]
                self.db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)", (pid, name, color, instructions, now, now))
            self.db.commit()
            self._bump(pid)
        return self.project(pid)

    def delete_project(self, pid):
        now = int(time.time() * 1000)
        with self.lock:
            files = [r[0] for r in self.db.execute("SELECT id FROM project_files WHERE project_id=?", (pid,))]
            cur = self.db.execute("DELETE FROM projects WHERE id=?", (pid,))
            self.db.execute("DELETE FROM project_notes WHERE project_id=?", (pid,))
            for fid in files:
                self.db.execute("DELETE FROM project_chunks WHERE file_id=?", (fid,))
                self.db.execute("DELETE FROM chunk_vecs WHERE file_id=?", (fid,))
            self.db.execute("DELETE FROM project_files WHERE project_id=?", (pid,))
            # Its chats stay, outside any project (synced to every device)
            for (cid,) in self.db.execute("SELECT id FROM conversations WHERE project_id=?", (pid,)).fetchall():
                self.db.execute("UPDATE conversations SET project_id='', updated_at=?, seq=? WHERE id=?",
                                (now, self._next(), cid))
            self.db.commit()
            self._bump(pid)
        for fid in files:
            _remove_project_file(fid)
        return cur.rowcount > 0

    def add_project_note(self, pid, text, source="user"):
        text = " ".join(str(text).split())[:1000]
        if len(text) < 2:
            raise ValueError("the note is empty")
        now = int(time.time() * 1000)
        with self.lock:
            if not self.db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                raise ValueError("no such project")
            dup = self.db.execute("SELECT id FROM project_notes WHERE project_id=? AND lower(text)=lower(?)",
                                  (pid, text)).fetchone()
            nid = dup[0] if dup else uuid.uuid4().hex[:8]
            if not dup:
                self.db.execute("INSERT INTO project_notes VALUES(?,?,?,?,?)", (nid, pid, text, source, now))
                self.db.execute("""DELETE FROM project_notes WHERE project_id=? AND id NOT IN
                                   (SELECT id FROM project_notes WHERE project_id=? ORDER BY created_at DESC LIMIT ?)""",
                                (pid, pid, self.MAX_NOTES))
            self.db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, pid))
            self.db.commit()
            self._bump(pid)
        return {"id": nid, "text": text, "source": source, "created_at": now}

    def delete_project_note(self, pid, nid):
        with self.lock:
            cur = self.db.execute("DELETE FROM project_notes WHERE id=? AND project_id=?", (nid, pid))
            self.db.commit()
            self._bump(pid)
        return cur.rowcount > 0

    def add_project_file(self, pid, name, kind, size, text, tokens, chunks, note):
        fid = uuid.uuid4().hex[:16]
        now = int(time.time() * 1000)
        with self.lock:
            if pid != LIBRARY_ID and not self.db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                raise ValueError("no such project")
            self.db.execute("INSERT INTO project_files VALUES(?,?,?,?,?,?,?,?,?)",
                            (fid, pid, name, kind, size, tokens, len(chunks), note, now))
            self.db.executemany("INSERT INTO project_chunks VALUES(?,?,?)", [(fid, i, c) for i, c in enumerate(chunks)])
            self.db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, pid))
            self.db.commit()
            self._bump(pid)
        return fid

    def delete_project_file(self, pid, fid):
        with self.lock:
            cur = self.db.execute("DELETE FROM project_files WHERE id=? AND project_id=?", (fid, pid))
            if cur.rowcount:
                self.db.execute("DELETE FROM project_chunks WHERE file_id=?", (fid,))
                self.db.execute("DELETE FROM chunk_vecs WHERE file_id=?", (fid,))
            self.db.commit()
            self._bump(pid)
        if cur.rowcount:
            _remove_project_file(fid)
        return cur.rowcount > 0

    # ---- Custom modes ("mindsets"): built-in ones live in the app ----
    def modes(self):
        with self.lock:
            rows = self.db.execute("SELECT id, name, icon, prompt, temperature, thinking, tools, style, updated_at "
                                   "FROM modes ORDER BY created_at").fetchall()
        return [{"id": r[0], "name": r[1], "icon": r[2], "prompt": r[3], "temperature": r[4], "thinking": r[5],
                 "tools": r[6], "style": r[7], "updated_at": r[8]} for r in rows]

    def save_mode(self, data):
        name = " ".join(str(data.get("name", "")).split())[:40]
        if not name:
            raise ValueError("the mode needs a name")
        icon = str(data.get("icon") or "✦").strip()[:4] or "✦"
        prompt = str(data.get("prompt", ""))[:6000]
        temp = data.get("temperature")
        temp = None if temp in (None, "") else max(0.0, min(1.5, float(temp)))
        thinking = data.get("thinking") if data.get("thinking") in ("on", "off", "default") else "default"
        tools = data.get("tools") if data.get("tools") in ("all", "minimal") else "all"
        style = data.get("style") if data.get("style") in ("work", "story") else "work"
        mid = str(data.get("id") or "")
        if mid and not re.fullmatch(r"c-[0-9a-f]{10}", mid):
            raise ValueError("bad mode id")
        now = int(time.time() * 1000)
        with self.lock:
            if mid and self.db.execute("SELECT 1 FROM modes WHERE id=?", (mid,)).fetchone():
                self.db.execute("UPDATE modes SET name=?, icon=?, prompt=?, temperature=?, thinking=?, tools=?, style=?, "
                                "updated_at=? WHERE id=?", (name, icon, prompt, temp, thinking, tools, style, now, mid))
            else:
                mid = mid or "c-" + uuid.uuid4().hex[:10]
                self.db.execute("INSERT INTO modes VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (mid, name, icon, prompt, temp, thinking, tools, style, now, now))
            self.db.commit()
        return next(m for m in self.modes() if m["id"] == mid)

    def delete_mode(self, mid):
        with self.lock:
            cur = self.db.execute("DELETE FROM modes WHERE id=?", (str(mid),))
            self.db.commit()
        return cur.rowcount > 0

    def project_chunks(self, pid):
        with self.lock:
            return self.db.execute(
                """SELECT c.file_id, f.name, c.idx, f.chunks, c.text FROM project_chunks c
                   JOIN project_files f ON f.id = c.file_id WHERE f.project_id=? ORDER BY f.created_at, c.idx""",
                (pid,)).fetchall()


class ChatDB(ProjectStoreMixin, TaskStoreMixin):
    """Chat history shared by all devices.

    Every change gets a increasing sequence number; devices ask for "everything
    after the last number I saw". Conflicts resolve by newest updated_at wins.
    """

    ROLES = {"user", "assistant", "tool", "summary"}
    PAGE = 500

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS conversations(
                id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '', reasoning TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, seq INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS conv_seq ON conversations(seq);
            CREATE INDEX IF NOT EXISTS msg_seq ON messages(seq);
            CREATE INDEX IF NOT EXISTS msg_conv ON messages(conv_id);
            CREATE TABLE IF NOT EXISTS memories(
                id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'user',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS projects(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT 'cyan',
                instructions TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS project_notes(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'user', created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS project_files(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                size INTEGER NOT NULL, tokens INTEGER NOT NULL, chunks INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS project_chunks(
                file_id TEXT NOT NULL, idx INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(file_id, idx));
            CREATE TABLE IF NOT EXISTS modes(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '✦',
                prompt TEXT NOT NULL DEFAULT '', temperature REAL, thinking TEXT NOT NULL DEFAULT 'default',
                tools TEXT NOT NULL DEFAULT 'all', style TEXT NOT NULL DEFAULT 'work',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_chunks(
                id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, idx INTEGER NOT NULL, text TEXT NOT NULL,
                ts INTEGER NOT NULL, vec BLOB);
            CREATE INDEX IF NOT EXISTS chat_chunk_conv ON chat_chunks(conv_id);
            CREATE TABLE IF NOT EXISTS chat_dirty(conv_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS chunk_vecs(
                file_id TEXT NOT NULL, idx INTEGER NOT NULL, vec BLOB NOT NULL, PRIMARY KEY(file_id, idx));
            CREATE INDEX IF NOT EXISTS note_proj ON project_notes(project_id);
            CREATE INDEX IF NOT EXISTS file_proj ON project_files(project_id);
            """
        )
        ccols = {r[1] for r in self.db.execute("PRAGMA table_info(conversations)")}
        if "project_id" not in ccols:
            self.db.execute("ALTER TABLE conversations ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
        if "mode" not in ccols:
            self.db.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT ''")
        self.project_versions = {}
        self._tasks_schema()
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "deleted" not in cols:  # older databases
            self.db.execute("ALTER TABLE messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        row = self.db.execute("SELECT v FROM meta WHERE k='server_id'").fetchone()
        if row:
            self.server_id = row[0]
        else:
            self.server_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO meta VALUES('server_id', ?)", (self.server_id,))
        self.seq = max(
            self.db.execute("SELECT COALESCE(MAX(seq),0) FROM conversations").fetchone()[0],
            self.db.execute("SELECT COALESCE(MAX(seq),0) FROM messages").fetchone()[0],
        )
        self.db.commit()

    @staticmethod
    def _id(v):
        v = str(v or "")
        if not v or len(v) > 64:
            raise ValueError("bad id")
        return v

    @staticmethod
    def _ts(v):
        return int(v)

    def _next(self):
        self.seq += 1
        return self.seq

    def sync(self, since, convs, msgs):
        with self.lock:
            for c in convs:
                cid, upd = self._id(c.get("id")), self._ts(c.get("updated_at"))
                row = self.db.execute("SELECT updated_at, project_id, mode FROM conversations WHERE id=?",
                                      (cid,)).fetchone()
                if row and row[0] >= upd:
                    continue
                deleted = 1 if c.get("deleted") else 0
                # Older apps don't send project_id: keep what the server has
                project = str(c.get("project_id") or "")[:64] if "project_id" in c else (row[1] if row else "")
                mode = str(c.get("mode") or "")[:64] if "mode" in c else (row[2] if row else "")
                self.db.execute(
                    """INSERT OR REPLACE INTO conversations
                       (id, title, created_at, updated_at, deleted, seq, project_id, mode) VALUES(?,?,?,?,?,?,?,?)""",
                    (cid, str(c.get("title", ""))[:200], self._ts(c.get("created_at", upd)), upd, deleted,
                     self._next(), project, mode),
                )
                if deleted:
                    self.db.execute("DELETE FROM messages WHERE conv_id=?", (cid,))
            for m in msgs:
                mid, upd = self._id(m.get("id")), self._ts(m.get("updated_at"))
                cid = self._id(m.get("conv_id"))
                role = str(m.get("role", ""))
                if role not in self.ROLES:
                    continue
                gone = self.db.execute("SELECT 1 FROM conversations WHERE id=? AND deleted=1", (cid,)).fetchone()
                if gone:
                    continue
                row = self.db.execute("SELECT updated_at FROM messages WHERE id=?", (mid,)).fetchone()
                if row and row[0] >= upd:
                    continue
                gone_msg = 1 if m.get("deleted") else 0
                self.db.execute(
                    """INSERT OR REPLACE INTO messages
                       (id, conv_id, role, text, reasoning, created_at, updated_at, seq, deleted)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (mid, cid, role, "" if gone_msg else str(m.get("text", ""))[:100000],
                     "" if gone_msg else str(m.get("reasoning", ""))[:100000],
                     self._ts(m.get("created_at", upd)), upd, self._next(), gone_msg),
                )
            # Conversations whose messages changed get re-indexed for "search past chats"
            touched = {str(c.get("id")) for c in convs} | {str(m.get("conv_id")) for m in msgs}
            self.db.executemany("INSERT OR IGNORE INTO chat_dirty VALUES(?)", [(t,) for t in touched if t])
            self.db.commit()

            rows = self.db.execute(
                """SELECT 'c', id, title, created_at, updated_at, deleted, project_id, mode, NULL, NULL, seq
                     FROM conversations WHERE seq > ?
                   UNION ALL
                   SELECT 'm', id, NULL, created_at, updated_at, deleted, conv_id, role, text, reasoning, seq
                     FROM messages WHERE seq > ?
                   ORDER BY seq LIMIT ?""",
                (since, since, self.PAGE),
            ).fetchall()

        out_c, out_m = [], []
        for kind, rid, title, created, updated, deleted, conv_id, role, text, reasoning, seq in rows:
            if kind == "c":
                out_c.append({"id": rid, "title": title, "created_at": created,
                              "updated_at": updated, "deleted": bool(deleted), "project_id": conv_id or "",
                              "mode": role or ""})
            else:
                out_m.append({"id": rid, "conv_id": conv_id, "role": role, "text": text,
                              "reasoning": reasoning, "created_at": created, "updated_at": updated,
                              "deleted": bool(deleted)})
        more = len(rows) == self.PAGE
        cursor = rows[-1][-1] if more else max(self.seq, since)
        return {"server_id": self.server_id, "cursor": cursor, "more": more,
                "conversations": out_c, "messages": out_m}

    # ---- Long-term memory -----------------------------------------------------------
    MAX_MEMORIES = 200

    def memories(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, text, source, created_at, updated_at FROM memories ORDER BY updated_at DESC").fetchall()
        return [{"id": r[0], "text": r[1], "source": r[2], "created_at": r[3], "updated_at": r[4]} for r in rows]

    def add_memory(self, text, source="user"):
        text = " ".join(str(text).split())[:300]
        if len(text) < 3:
            raise ValueError("memory is empty")
        now = int(time.time() * 1000)
        with self.lock:
            dup = self.db.execute("SELECT id FROM memories WHERE lower(text)=lower(?)", (text,)).fetchone()
            if dup:
                self.db.execute("UPDATE memories SET updated_at=? WHERE id=?", (now, dup[0]))
                self.db.commit()
                return dup[0], False
            mid = uuid.uuid4().hex[:8]
            self.db.execute("INSERT INTO memories VALUES(?,?,?,?,?)", (mid, text, source, now, now))
            # Keep the newest MAX_MEMORIES
            self.db.execute("""DELETE FROM memories WHERE id NOT IN
                               (SELECT id FROM memories ORDER BY updated_at DESC LIMIT ?)""", (self.MAX_MEMORIES,))
            self.db.commit()
            return mid, True

    def delete_memory(self, mid):
        with self.lock:
            cur = self.db.execute("DELETE FROM memories WHERE id=?", (str(mid),))
            self.db.commit()
            return cur.rowcount > 0

    def find_memories(self, query):
        q = str(query).strip().lower()
        if not q:
            return []
        return [m for m in self.memories() if m["id"] == q or q in m["text"].lower()]


def _remove_project_file(fid):
    for ext in (".txt", ".orig"):
        try:
            os.remove(os.path.join(PROJECT_DIR, fid + ext))
        except OSError:
            pass


def add_project_file(pid, name, data):
    name = os.path.basename(str(name).replace("\\", "/")).strip()[:120] or "file"
    kind, text, note = extract_text(name, data)
    text = text.replace("\x00", "")[:MAX_FILE_CHARS]
    if not text.strip():
        raise ValueError(f"no text found in '{name}' (scanned PDFs need OCR first)")
    chunks = chunk_text(text)
    tokens = count_tokens(text) if len(text) < 400_000 else int(len(text) / 3.3)
    fid = CHATS.add_project_file(pid, name, kind, len(data), text, tokens, chunks, note)
    with open(os.path.join(PROJECT_DIR, fid + ".txt"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(PROJECT_DIR, fid + ".orig"), "wb") as f:
        f.write(data)
    EMBEDDER.queue(fid)
    return fid


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(whisper_url: str, wav: bytes, language: str) -> str:
    boundary = "----lai" + uuid.uuid4().hex
    fields = {"response_format": "json", "temperature": "0.0"}
    if language:
        fields["language"] = language
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode()
        + wav
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        whisper_url.rstrip("/") + "/inference",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()
    if "error" in data:
        raise RuntimeError(data["error"])
    return str(data.get("text", "")).strip()


def clean_transcript(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]", " ", text)  # [BLANK_AUDIO], [MUSIC] ...
    text = re.sub(r"\s+", " ", text).strip()
    if NOISE_PATTERNS.match(text):
        return ""
    return text


def key_ok(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


# ----------------------------------------------------------------------------- Server tools
# Tools Stella can call that run here on the server (the phone app forwards them).
# Calculator and electronics helpers are exact; diagrams are rendered to PNG.

import ast
import math
import operator
import os
import subprocess
import tempfile

SI_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0,
             "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1.0}
UNIT_WORDS = r"(?:Hz|ohms?|Ohms?|Ω|V|A|W|F|H|s)"
# 4.7k, 10uF, 4k7, 4R7, 100mA, 3.3V, 1kHz, 2.2µF, 47Ω
QUANTITY_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?:([pnuµμmkKMGR])(\d+)?)?" + UNIT_WORDS + r"?(?![\w(.])"
)


def _quantity_sub(m):
    whole, prefix, frac = m.group(1), m.group(2) or "", m.group(3)
    if frac and "." not in whole and "e" not in whole.lower():
        whole = f"{whole}.{frac}"  # 4k7 -> 4.7k
    return repr(float(whole) * SI_PREFIX[prefix])


def parse_quantity(v):
    """Number, or a string like '4.7k', '10uF', '4k7', '100 mA', '3.3V'."""
    if isinstance(v, bool):
        raise ValueError("expected a number")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if not s:
        raise ValueError("empty value")
    m = QUANTITY_RE.fullmatch(s)
    if m:
        return float(_quantity_sub(m))
    return float(s)


def eng(x, unit=""):
    """Engineering notation with SI prefix: 0.00047 -> '470 µ' + unit."""
    if x == 0 or not math.isfinite(x):
        return f"{x:g} {unit}".strip()
    exp3 = int(math.floor(math.log10(abs(x)) / 3) * 3)
    exp3 = max(-12, min(9, exp3))
    prefix = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[exp3]
    return f"{x / 10 ** exp3:.4g} {prefix}{unit}".strip()


_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _parallel(*xs):
    return 1.0 / sum(1.0 / x for x in xs)


_FUNCS = {
    "sqrt": math.sqrt, "ln": math.log, "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin,
    "acos": math.acos, "atan": math.atan, "atan2": math.atan2, "sinh": math.sinh, "cosh": math.cosh,
    "tanh": math.tanh, "abs": abs, "round": round, "min": min, "max": max, "pow": math.pow,
    "ceil": math.ceil, "floor": math.floor, "hypot": math.hypot, "degrees": math.degrees,
    "radians": math.radians, "parallel": _parallel, "db": lambda r: 20 * math.log10(r),
    "db10": lambda r: 10 * math.log10(r),
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def safe_eval(expr):
    """Evaluate a maths expression without running arbitrary code."""
    text = QUANTITY_RE.sub(_quantity_sub, expr.replace("^", "**").replace("×", "*").replace("÷", "/"))
    tree = ast.parse(text, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError(f"not allowed in a calculation: {ast.unparse(node) if hasattr(ast, 'unparse') else type(node).__name__}")

    result = ev(tree)
    if isinstance(result, complex):
        raise ValueError("result is a complex number")
    return float(result)


# E-series standard resistor values (one decade)
E_SERIES = {
    "E12": [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2],
    "E24": [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1,
            5.6, 6.2, 6.8, 7.5, 8.2, 9.1],
    "E96": [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33, 1.37, 1.40,
            1.43, 1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82, 1.87, 1.91, 1.96, 2.00,
            2.05, 2.10, 2.15, 2.21, 2.26, 2.32, 2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87,
            2.94, 3.01, 3.09, 3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12,
            4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49, 5.62, 5.76, 5.90,
            6.04, 6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45,
            8.66, 8.87, 9.09, 9.31, 9.53, 9.76],
}


def _series_values(series):
    base = E_SERIES.get(series.upper())
    if not base:
        raise ValueError("series must be E12, E24 or E96")
    return [round(b * 10 ** d, 10) for d in range(-1, 8) for b in base]


def nearest_standard(value, series="E24"):
    vals = _series_values(series)
    below = max((v for v in vals if v <= value * (1 + 1e-9)), default=vals[0])
    above = min((v for v in vals if v >= value * (1 - 1e-9)), default=vals[-1])
    nearest = min((below, above), key=lambda v: abs(v - value))
    return below, nearest, above


def _need(args, keys, count):
    got = {k: parse_quantity(args[k]) for k in keys if args.get(k) not in (None, "")}
    if len(got) < count:
        raise ValueError(f"give at least {count} of: {', '.join(keys)}")
    return got


def tool_calculate(args):
    expr = str(args.get("expression", "")).strip()
    if not expr:
        raise ValueError("no expression")
    value = safe_eval(expr)
    unit = str(args.get("unit", "")).strip()
    return {"expression": expr, "value": value, "formatted": eng(value, unit)}, f"{expr} = {eng(value, unit)}"


def tool_ohms_law(args):
    g = _need(args, ["voltage", "current", "resistance", "power"], 2)
    v, i, r, p = g.get("voltage"), g.get("current"), g.get("resistance"), g.get("power")
    if v is not None and i is not None:
        r, p = v / i, v * i
    elif v is not None and r is not None:
        i = v / r; p = v * i
    elif v is not None and p is not None:
        i = p / v; r = v / i
    elif i is not None and r is not None:
        v = i * r; p = v * i
    elif i is not None and p is not None:
        v = p / i; r = v / i
    else:  # r and p
        v = math.sqrt(p * r); i = v / r
    res = {"voltage": v, "current": i, "resistance": r, "power": p}
    summary = f"{eng(v, 'V')}, {eng(i, 'A')}, {eng(r, 'Ω')}, {eng(p, 'W')}"
    return res, summary


def tool_voltage_divider(args):
    vin = parse_quantity(args["vin"]) if args.get("vin") not in (None, "") else None
    vout = parse_quantity(args["vout"]) if args.get("vout") not in (None, "") else None
    r1 = parse_quantity(args["r1"]) if args.get("r1") not in (None, "") else None
    r2 = parse_quantity(args["r2"]) if args.get("r2") not in (None, "") else None
    series = str(args.get("series") or "E24")
    known = sum(x is not None for x in (vin, vout, r1, r2))
    if vin is not None and r1 is not None and r2 is not None:
        vout = vin * r2 / (r1 + r2)
    elif vout is not None and r1 is not None and r2 is not None:
        vin = vout * (r1 + r2) / r2
    elif vin is not None and vout is not None and r1 is not None:
        r2 = r1 * vout / (vin - vout)
    elif vin is not None and vout is not None and r2 is not None:
        r1 = r2 * (vin - vout) / vout
    elif vin is not None and vout is not None and known == 2:
        # Suggest a standard-value pair near 10k total
        if not 0 < vout < vin:
            raise ValueError("vout must be between 0 and vin")
        best = None
        for a in _series_values(series):
            if not 1e3 <= a <= 1e6:
                continue
            _, b, _ = nearest_standard(a * vout / (vin - vout), series)
            err = abs(vin * b / (a + b) - vout)
            score = (round(err, 6), abs(math.log10((a + b) / 2e4)))
            if best is None or score < best[0]:
                best = (score, a, b)
        r1, r2 = best[1], best[2]
        actual = vin * r2 / (r1 + r2)
        res = {"vin": vin, "vout_target": vout, "r1": r1, "r2": r2, "vout_actual": actual,
               "error_percent": (actual - vout) / vout * 100, "series": series}
        return res, f"R1 = {eng(r1, 'Ω')}, R2 = {eng(r2, 'Ω')} → {eng(actual, 'V')} ({res['error_percent']:+.2f}%)"
    else:
        raise ValueError("give three of vin, vout, r1, r2 (or just vin and vout to get a suggested pair)")
    current = vin / (r1 + r2)
    res = {"vin": vin, "vout": vout, "r1": r1, "r2": r2, "current": current}
    return res, f"Vin {eng(vin, 'V')}, Vout {eng(vout, 'V')}, R1 {eng(r1, 'Ω')}, R2 {eng(r2, 'Ω')}, I {eng(current, 'A')}"


def tool_led_resistor(args):
    vs = parse_quantity(args["supply_voltage"])
    vf = parse_quantity(args["led_voltage"])
    n = int(args.get("leds_in_series") or 1)
    i = parse_quantity(args["current_ma"]) / 1000.0
    drop = vs - vf * n
    if drop <= 0:
        raise ValueError("supply voltage is too low for that many LEDs")
    r = drop / i
    _, _, e12_up = nearest_standard(r, "E12")
    _, _, e24_up = nearest_standard(r, "E24")
    actual_i = drop / e24_up
    p = actual_i ** 2 * e24_up
    rating = next(w for w in (0.125, 0.25, 0.5, 1, 2, 5, 10, 1e9) if w >= p * 2)
    res = {"exact_resistance": r, "e24_resistor": e24_up, "e12_resistor": e12_up,
           "actual_current": actual_i, "resistor_power": p, "suggested_rating_w": rating}
    return res, (f"{eng(r, 'Ω')} exact → use {eng(e24_up, 'Ω')} (E24) or {eng(e12_up, 'Ω')} (E12); "
                 f"{eng(actual_i, 'A')}, {eng(p, 'W')} → {rating:g} W resistor")


def tool_rc_filter(args):
    g = _need(args, ["resistance", "capacitance", "frequency"], 2)
    r, c, f = g.get("resistance"), g.get("capacitance"), g.get("frequency")
    if r is not None and c is not None:
        f = 1 / (2 * math.pi * r * c)
    elif r is not None and f is not None:
        c = 1 / (2 * math.pi * r * f)
    else:
        r = 1 / (2 * math.pi * c * f)
    tau = r * c
    res = {"resistance": r, "capacitance": c, "cutoff_frequency": f, "time_constant": tau}
    return res, f"R {eng(r, 'Ω')}, C {eng(c, 'F')}, fc {eng(f, 'Hz')}, τ {eng(tau, 's')}"


def tool_combine_resistors(args):
    vals = [parse_quantity(v) for v in (args.get("values") or [])]
    if len(vals) < 2:
        raise ValueError("give at least two values")
    mode = str(args.get("mode", "parallel")).lower()
    total = sum(vals) if mode == "series" else _parallel(*vals)
    return {"mode": mode, "total": total}, f"{mode}: {eng(total, 'Ω')}"


def tool_standard_resistor(args):
    value = parse_quantity(args["value"])
    series = str(args.get("series") or "E24")
    below, nearest, above = nearest_standard(value, series)
    res = {"value": value, "series": series, "nearest": nearest, "below": below, "above": above,
           "error_percent": (nearest - value) / value * 100}
    return res, f"{series}: nearest {eng(nearest, 'Ω')} ({res['error_percent']:+.1f}%), range {eng(below, 'Ω')}–{eng(above, 'Ω')}"


# ---- Diagrams ------------------------------------------------------------------
DIAGRAM_DIR = None   # set in main()
PY_FOR_DIAGRAMS = sys.executable
MMDC = "mmdc"

SCHEMDRAW_ALLOWED_IMPORTS = {"schemdraw", "schemdraw.elements", "schemdraw.logic", "schemdraw.dsp",
                             "schemdraw.flow", "schemdraw.segments", "math"}
SCHEMDRAW_BLOCKED_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
                           "getattr", "setattr", "delattr", "input", "breakpoint", "help", "exit", "quit",
                           "memoryview", "os", "sys", "subprocess", "shutil", "socket", "pathlib", "importlib"}


def check_schemdraw_code(code):
    """Only schemdraw drawing code: no file, network or system access."""
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in SCHEMDRAW_ALLOWED_IMPORTS:
                    raise ValueError(f"import of {a.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") not in SCHEMDRAW_ALLOWED_IMPORTS or node.level:
                raise ValueError(f"import from {node.module} is not allowed")
        elif isinstance(node, ast.Name) and node.id in SCHEMDRAW_BLOCKED_NAMES:
            raise ValueError(f"'{node.id}' is not allowed in diagram code")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private attributes are not allowed in diagram code")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise ValueError("global statements are not allowed")


SCHEMDRAW_RUNNER = r'''
import sys
try:
    import resource  # Linux/macOS only; Windows relies on the wall-clock timeout instead
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
except ImportError:
    pass
import matplotlib
matplotlib.use("Agg")
import schemdraw
import schemdraw.elements as elm
code = open(sys.argv[1]).read()
out = sys.argv[2]
ns = {"schemdraw": schemdraw, "elm": elm}
_orig_draw = schemdraw.Drawing.draw
drawings = []
def _capture(self, *a, **k):
    drawings.append(self)
    k["show"] = False
    return _orig_draw(self, *a, **k)
schemdraw.Drawing.draw = _capture
_orig_exit = schemdraw.Drawing.__exit__
def _exit(self, *a):
    drawings.append(self)
    try:
        return _orig_exit(self, *a)
    except Exception:
        return None
schemdraw.Drawing.__exit__ = _exit
exec(compile(code, "diagram", "exec"), ns)
d = ns.get("d") or (drawings[-1] if drawings else None)
if d is None:
    for v in ns.values():
        if isinstance(v, schemdraw.Drawing):
            d = v
if d is None:
    sys.exit("no schemdraw.Drawing found (assign it to d)")
d.save(out, transparent=False, dpi=160)
'''


DIAGRAM_RUNNER_CMD = None   # e.g. ["StellAI.exe", "--schemdraw-runner"] in the packaged Windows app


def run_schemdraw_runner(src, out):
    """Entry point for packaged apps: run the schemdraw sandbox in this (fresh) process."""
    sys.argv = ["runner", src, out]
    exec(compile(SCHEMDRAW_RUNNER, "runner", "exec"), {"__name__": "__main__"})


def _child_env():
    keep = ["PATH", "HOME"]
    if os.name == "nt":
        keep += ["SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
                 "PATHEXT", "COMSPEC", "WINDIR", "PROGRAMFILES"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("HOME", tempfile.gettempdir())
    env["MPLBACKEND"] = "Agg"
    return env


def _run(cmd, timeout):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flashing up
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_child_env(),
                           creationflags=flags, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the diagram took too long to draw (over {timeout} s)")
    if p.returncode != 0:
        if p.returncode in (-9, -24, 137, 152):  # killed by the CPU-time limit
            raise RuntimeError("the diagram took too long to draw (over 20 s of CPU)")
        lines = [ln.strip() for ln in (p.stderr or p.stdout or "").splitlines()
                 if ln.strip() and not ln.strip().startswith("at ") and "(" not in ln[:1]]
        # Prefer the first "Error: ..." line plus the line after it (e.g. Mermaid's "Expecting ...")
        for i, ln in enumerate(lines):
            if ln.startswith("Error") or "Error:" in ln:
                detail = next((x for x in lines[i + 1:] if x.startswith("Expecting")), "")
                raise RuntimeError(f"{ln} {detail}".strip()[:300])
        raise RuntimeError((lines or [f"exit code {p.returncode}"])[-1][:300])


def render_schemdraw(code, out_png):
    check_schemdraw_code(code)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "diagram.py")
        runner = os.path.join(tmp, "runner.py")
        with open(src, "w") as f:
            f.write(code)
        with open(runner, "w") as f:
            f.write(SCHEMDRAW_RUNNER)
        if DIAGRAM_RUNNER_CMD:
            _run(list(DIAGRAM_RUNNER_CMD) + [src, out_png], timeout=40)
        else:
            _run([PY_FOR_DIAGRAMS, "-I", runner, src, out_png], timeout=40)


def mermaid_command():
    """Full path to the Mermaid CLI, or None when it isn't installed (Mermaid is optional)."""
    import shutil
    if MMDC and os.path.isfile(MMDC):
        return MMDC
    return shutil.which(MMDC) if MMDC else None


def render_mermaid(source, out_png):
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "diagram.mmd")
        with open(src, "w") as f:
            f.write(source)
        mmdc = mermaid_command()
        if not mmdc:
            raise ValueError("Mermaid isn't installed on this server; draw block diagrams with "
                             "kind='schemdraw' (schemdraw.flow) instead")
        _run([mmdc, "-i", src, "-o", out_png, "-b", "white", "-s", "2"], timeout=60)


def _wave_segments(wave):
    """Expand a WaveDrom wave string into per-cycle states."""
    states, last = [], "x"
    for ch in wave:
        if ch == "." or ch == "|":
            states.append(("cont", last))
        else:
            last = ch
            states.append(("new", ch))
    return states


def render_wavedrom(source, out_png):
    """Draws a WaveDrom (WaveJSON) timing diagram with matplotlib. Supports the common wave
    characters: 0 1 h l p n P N x z = 2-9 . | and data labels."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon

    spec = json.loads(source)
    signals = []

    def collect(items):
        for it in items:
            if isinstance(it, list):
                collect(it[1:] if it and isinstance(it[0], str) else it)
            elif isinstance(it, dict) and "wave" in it:
                signals.append(it)
            elif isinstance(it, dict):
                signals.append(None)  # spacer
    collect(spec.get("signal", []))
    if not signals:
        raise ValueError("no signals found")
    cycles = max(len(s["wave"]) for s in signals if s) or 1
    hscale = float(spec.get("config", {}).get("hscale", 1))
    cw = 0.6 * hscale
    rows = len(signals)
    fig_w = 1.6 + cycles * cw + 0.3
    fig_h = 0.55 * rows + 0.6
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=160)
    ax.set_xlim(-1.6, cycles * cw + 0.3)
    ax.set_ylim(-rows * 0.55 - 0.1, 0.4)
    ax.axis("off")
    for c in range(cycles + 1):
        ax.axvline(c * cw, color="#dddddd", lw=0.6, zorder=0)
    title = (spec.get("head") or {}).get("text") if isinstance(spec.get("head"), dict) else None
    if title:
        ax.text(cycles * cw / 2, 0.25, str(title), ha="center", va="center", fontsize=10, weight="bold")

    bus_colors = {"=": "#ffffff", "2": "#ffffff", "3": "#fff3b0", "4": "#ffd6a5", "5": "#caffbf",
                  "6": "#a0c4ff", "7": "#bdb2ff", "8": "#ffc6ff", "9": "#e0e0e0"}
    for row, sig in enumerate(signals):
        if sig is None:
            continue
        y0 = -row * 0.55 - 0.45
        hi, lo, mid = y0 + 0.36, y0, y0 + 0.18
        ax.text(-0.1, mid, str(sig.get("name", "")), ha="right", va="center", fontsize=9, family="monospace")
        data = sig.get("data", [])
        if isinstance(data, str):
            data = data.split()
        # Group the wave into runs: (start cycle, length, character)
        runs = []
        for c, (kind, ch) in enumerate(_wave_segments(str(sig["wave"]))):
            if kind == "cont" and runs:
                runs[-1][1] += 1
            else:
                runs.append([c, 1, ch])
        di = 0
        level = None  # last drawn line level, for vertical transitions
        for start, span, ch in runs:
            x0, x1 = start * cw, (start + span) * cw
            if ch in "pPnN":
                for k in range(span):
                    a, b = x0 + k * cw, x0 + (k + 1) * cw
                    first, second = (hi, lo) if ch in "pP" else (lo, hi)
                    xm = (a + b) / 2
                    ax.plot([a, a, xm, xm, b], [level if level is not None else first, first, first, second, second],
                            color="black", lw=1.2)
                    level = second
            elif ch in "1h0lz":
                y = hi if ch in "1h" else lo if ch in "0l" else mid
                ax.plot([x0, x0, x1], [level if level is not None else y, y, y],
                        color="#1f6feb" if ch == "z" else "black", lw=1.2)
                level = y
            else:
                # Bus value or undefined: one hexagon per run, label in the middle
                e = min(0.08, (x1 - x0) / 4)
                poly = [(x0 + e, hi), (x1 - e, hi), (x1, mid), (x1 - e, lo), (x0 + e, lo), (x0, mid)]
                if ch == "x":
                    ax.add_patch(Polygon(poly, closed=True, facecolor="#eeeeee", edgecolor="black", lw=1.0, hatch="////"))
                else:
                    ax.add_patch(Polygon(poly, closed=True, facecolor=bus_colors.get(ch, "#ffffff"),
                                         edgecolor="black", lw=1.0))
                    if di < len(data):
                        ax.text((x0 + x1) / 2, mid, str(data[di]), ha="center", va="center", fontsize=8)
                    di += 1
                level = None
    fig.savefig(out_png, bbox_inches="tight", facecolor="white", pad_inches=0.15)
    plt.close(fig)


def tool_render_diagram(args):
    kind = str(args.get("kind", "")).lower().strip()
    source = str(args.get("source", ""))
    title = str(args.get("title", "")).strip() or kind
    if not source.strip():
        raise ValueError("no diagram source")
    if len(source) > 20000:
        raise ValueError("diagram source is too long")
    image_id = uuid.uuid4().hex[:16]
    out = os.path.join(DIAGRAM_DIR, image_id + ".png")
    if kind == "mermaid":
        render_mermaid(source, out)
    elif kind == "schemdraw":
        render_schemdraw(source, out)
    elif kind == "wavedrom":
        render_wavedrom(source, out)
    else:
        raise ValueError("kind must be mermaid, schemdraw or wavedrom")
    if not os.path.exists(out):
        raise RuntimeError("the renderer produced no image")
    return {"image": image_id, "title": title, "note": "The diagram is now shown to the user."}, f"Drew {title}"


def _p(desc, typ="string"):
    return {"type": typ, "description": desc}


QTY = "Number, or a value with SI prefix/unit like '4.7k', '10uF', '3.3V', '20mA'"


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


MERMAID_HINT = "mermaid: block diagrams, flowcharts, state/sequence diagrams. "
NO_MERMAID_HINT = "Flowcharts: schemdraw.flow (flow.Start, flow.Box, flow.Decision, flow.Arrow). "


DIAGRAM_DESC = (
    "Draw and show a diagram. schemdraw: circuit schematics as Python ('import schemdraw', "
    "'import schemdraw.elements as elm', 'with schemdraw.Drawing() as d:', 'd += elm.Resistor().label(\"10kΩ\")'; "
    "no other imports). {MERMAID}wavedrom: timing diagrams as WaveJSON."
)


def refresh_diagram_tool():
    """Only offer Mermaid to the model when the Mermaid CLI is actually installed."""
    hint = MERMAID_HINT if mermaid_command() else NO_MERMAID_HINT
    fn = SERVER_TOOLS["render_diagram"][1]["function"]
    fn["description"] = DIAGRAM_DESC.replace("{MERMAID}", hint)
    fn["parameters"]["properties"]["kind"]["description"] = (
        "schemdraw, mermaid or wavedrom" if mermaid_command() else "schemdraw or wavedrom")


SERVER_TOOLS = {
    "calculate": (tool_calculate, _fn(
        "calculate",
        "Exact maths. ALWAYS use this instead of doing arithmetic yourself. Supports + - * / ^ ( ), "
        "SI values like 4.7k, 10u, 3.3V, 100mA, functions sqrt ln log10 exp sin cos tan abs round min max, "
        "parallel(a,b,...) for parallel resistors, db(ratio), and constants pi e.",
        {"expression": _p("The expression, e.g. '12 * 4.7k / (4.7k + 10k)' or 'parallel(10k, 4.7k)'"),
         "unit": _p("Optional unit for the answer, e.g. 'V', 'Ω', 'Hz', 'A'")}, ["expression"])),
    "ohms_law": (tool_ohms_law, _fn(
        "ohms_law", "Ohm's law and power: give any two of voltage, current, resistance, power.",
        {"voltage": _p(QTY), "current": _p(QTY), "resistance": _p(QTY), "power": _p(QTY)}, [])),
    "voltage_divider": (tool_voltage_divider, _fn(
        "voltage_divider",
        "Resistor voltage divider (Vout = Vin*R2/(R1+R2), R2 to ground). Give three of vin, vout, r1, r2 "
        "to solve the fourth, or just vin and vout to get the best standard-value resistor pair.",
        {"vin": _p(QTY), "vout": _p(QTY), "r1": _p(QTY), "r2": _p(QTY),
         "series": _p("Resistor series for suggestions: E12, E24 (default) or E96")}, [])),
    "led_resistor": (tool_led_resistor, _fn(
        "led_resistor", "Current-limiting resistor for LEDs, with standard values and power rating.",
        {"supply_voltage": _p(QTY), "led_voltage": _p("Forward voltage of one LED, e.g. 2.0 red, 3.1 white/blue"),
         "current_ma": _p("LED current in mA, e.g. 10"),
         "leds_in_series": _p("How many LEDs in series (default 1)", "integer")},
        ["supply_voltage", "led_voltage", "current_ma"])),
    "rc_filter": (tool_rc_filter, _fn(
        "rc_filter", "RC low/high-pass filter: cutoff fc = 1/(2πRC). Give any two of resistance, capacitance, frequency.",
        {"resistance": _p(QTY), "capacitance": _p(QTY), "frequency": _p(QTY)}, [])),
    "combine_resistors": (tool_combine_resistors, _fn(
        "combine_resistors", "Total resistance of resistors in series or parallel.",
        {"values": {"type": "array", "items": {"type": "string"}, "description": "Resistor values, e.g. ['10k','4.7k']"},
         "mode": _p("'series' or 'parallel'")}, ["values", "mode"])),
    "standard_resistor": (tool_standard_resistor, _fn(
        "standard_resistor", "Nearest standard resistor values (E12/E24/E96) to a calculated value.",
        {"value": _p(QTY), "series": _p("E12, E24 (default) or E96")}, ["value"])),
    "render_diagram": (tool_render_diagram, _fn(
        "render_diagram",
        DIAGRAM_DESC.replace("{MERMAID}", MERMAID_HINT),
        {"kind": _p("schemdraw, mermaid or wavedrom"), "source": _p("The diagram code"),
         "title": _p("Short title, e.g. 'LED driver'")}, ["kind", "source"])),
}


# ---- Long-term memory tools ------------------------------------------------------
CHATS = None        # ChatDB, set in main()
LLAMA_URL = "http://127.0.0.1:8080"
API_KEY = ""


def tool_remember(args):
    fact = str(args.get("fact", "")).strip()
    mid, new = CHATS.add_memory(fact, "stella")
    return {"id": mid, "saved": fact, "new": new}, f"Remembered: {fact}" if new else f"Already knew: {fact}"


def tool_forget(args):
    query = str(args.get("memory", "")).strip()
    found = CHATS.find_memories(query)
    if not found:
        raise ValueError(f"no memory matches '{query}'")
    if len(found) > 3:
        raise ValueError(f"{len(found)} memories match '{query}'; be more specific or give the id")
    for m in found:
        CHATS.delete_memory(m["id"])
    return {"forgotten": [m["text"] for m in found]}, "Forgot: " + "; ".join(m["text"] for m in found)


LEARN_SCHEMA = {
    "type": "object",
    "properties": {
        "add": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "remove": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["add", "remove"],
}

LEARN_PROMPT = """You maintain a short list of long-term facts about the user, so their assistant can help them better over time.
Read the conversation and decide what is worth remembering for weeks or months: stable preferences, projects they are working on,
tools/boards/parts/software they use, people and pets in their life, routines and schedules, goals, and explicit "remember" requests.
Do NOT save: one-off questions, small talk, things already in the list, temporary states ("I'm tired"), or anything the assistant said.
Write each fact as one short sentence in third person, e.g. "Uses an ESP32-S3 for most projects." or "Works 8-4 on weekdays."
If the conversation shows an existing fact is now wrong, put its id in "remove" (and the corrected fact in "add").
Reply with JSON only: {"add": [...], "remove": [...]}. Empty lists are fine and are the usual answer."""


def learn_memories(messages):
    """Ask the local model which facts from a conversation are worth keeping."""
    existing = CHATS.memories()
    known = "\n".join(f"[{m['id']}] {m['text']}" for m in existing[:120]) or "(none yet)"
    convo = "\n".join(f"{m.get('role', 'user').upper()}: {str(m.get('text', ''))[:1500]}"
                      for m in messages[-12:] if m.get("role") in ("user", "assistant"))
    if not convo.strip():
        return {"added": [], "removed": []}
    body = {
        "model": "local",
        "messages": [
            {"role": "system", "content": LEARN_PROMPT},
            {"role": "user", "content": f"Known facts:\n{known}\n\nConversation:\n{convo}"},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_schema", "json_schema": {"name": "memory_update", "schema": LEARN_SCHEMA}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        LLAMA_URL.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        content = json.loads(resp.read())["choices"][0]["message"].get("content") or "{}"
    m = re.search(r"\{[\s\S]*\}", content)
    update = json.loads(m.group(0)) if m else {}
    removed, added = [], []
    ids = {x["id"] for x in existing}
    for mid in update.get("remove", [])[:5]:
        mid = str(mid).strip("[] ")
        if mid in ids and CHATS.delete_memory(mid):
            removed.append(mid)
    for fact in update.get("add", [])[:5]:
        if isinstance(fact, str) and len(fact.strip()) >= 3:
            _, new = CHATS.add_memory(fact, "learned")
            if new:
                added.append(" ".join(fact.split()))
    return {"added": added, "removed": removed}


SERVER_TOOLS["remember"] = (tool_remember, _fn(
    "remember",
    "Save a long-term fact about the user (preferences, projects, equipment, people, routines) so you can use it in "
    "future conversations. Use when the user tells you something worth remembering or asks you to remember it.",
    {"fact": _p("One short sentence in third person, e.g. 'Prefers Kotlin for Android apps.'")}, ["fact"]))
SERVER_TOOLS["forget"] = (tool_forget, _fn(
    "forget", "Delete a saved fact about the user, when they ask you to forget it or it is wrong.",
    {"memory": _p("The memory id (e.g. 'a1b2c3d4') or words from it")}, ["memory"]))

# ---- Context window: token counting, summaries -------------------------------------
_TOKEN_CACHE = {}
_NCTX = {"value": 0, "at": 0.0}


def _llama(path, body=None, timeout=60):
    req = urllib.request.Request(
        LLAMA_URL.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def count_tokens(text):
    """Exact count from llama-server's tokenizer (cached); a character estimate if it's unreachable."""
    text = str(text or "")
    if not text:
        return 0
    key = hash(text)
    hit = _TOKEN_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        n = len(_llama("/tokenize", {"content": text}, timeout=20).get("tokens", []))
    except (urllib.error.URLError, OSError, ValueError):
        return int(len(text) / 3.3) + 1   # not cached: retry exact counting next time
    if len(_TOKEN_CACHE) > 20000:
        _TOKEN_CACHE.clear()
    _TOKEN_CACHE[key] = n
    return n


def context_size():
    """The model's context window (tokens), from llama-server, cached for a minute."""
    if time.time() - _NCTX["at"] < 60 and _NCTX["value"]:
        return _NCTX["value"]
    n = 0
    try:
        props = _llama("/props", timeout=10)
        n = int((props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx") or 0)
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        pass
    if n:
        _NCTX.update(value=n, at=time.time())
    return n or _NCTX["value"] or 8192


SUMMARY_PROMPT = (
    "You compress a chat between a user and their AI assistant so the assistant can continue it with a fresh, "
    "nearly empty memory. Write a summary that lets the conversation continue seamlessly:\n"
    "- the user's goal and what has been decided or done so far\n"
    "- key facts, numbers, part numbers, file names, commands and code identifiers (keep short code snippets "
    "that are still being worked on)\n"
    "- open questions and what the user asked for last\n"
    "Use short bullet points under these headings: Goal, Done & decided, Key details, Open / next. "
    "Write in English, at most about 350 words. Output only the summary."
)


STORY_SUMMARY_PROMPT = (
    "You compress a creative-writing collaboration between a user and their AI writing partner so the partner can "
    "keep writing with a fresh, nearly empty memory. Preserve everything needed for continuity:\n"
    "- Characters: names, roles, relationships, appearance, voice and what each wants\n"
    "- Setting and world rules\n"
    "- Plot so far, in order, including the most recent scene in some detail\n"
    "- Style: genre, tense, point of view, tone, and any style rules the user gave\n"
    "- Open threads, planned events and what the user asked for next\n"
    "Use short bullet points under those headings. At most about 450 words. Output only the summary."
)


def _summarize_once(previous, convo, max_tokens=800, style="work"):
    user = (f"Summary so far:\n{previous}\n\n" if previous else "") + f"Conversation to add:\n{convo}"
    prompt = STORY_SUMMARY_PROMPT if style == "story" else SUMMARY_PROMPT
    body = {"model": "local", "temperature": 0.2, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            "chat_template_kwargs": {"enable_thinking": False}}
    out = _llama("/v1/chat/completions", body, timeout=300)["choices"][0]["message"].get("content") or ""
    out = re.sub(r"<think>[\s\S]*?</think>", "", out).strip()
    if not out:
        raise RuntimeError("the model returned an empty summary")
    return out


def summarize_chat(previous, messages, style="work"):
    """Rolling summary: long chats are summarized in pieces that fit the model's context."""
    n_ctx = context_size()
    piece_budget = max(1000, int(n_ctx * 0.5) - count_tokens(SUMMARY_PROMPT) - count_tokens(previous) - 800)
    lines = []
    for m in messages:
        role = str(m.get("role", "user"))
        text = str(m.get("content", "")).strip()
        if not text or role not in ("user", "assistant", "tool", "summary"):
            continue
        label = {"user": "USER", "assistant": "ASSISTANT", "tool": "ACTION RESULT", "summary": "EARLIER SUMMARY"}[role]
        max_chars = piece_budget * 3
        if len(text) > max_chars:
            text = text[: max_chars // 2] + "\n[…]\n" + text[-max_chars // 2:]
        lines.append(f"{label}: {text}")
    if not lines:
        return previous or ""
    summary, batch, used = previous or "", [], 0
    for line in lines:
        t = count_tokens(line)
        if batch and used + t > piece_budget:
            summary = _summarize_once(summary, "\n\n".join(batch), style=style)
            batch, used = [], 0
        batch.append(line)
        used += t
    if batch:
        summary = _summarize_once(summary, "\n\n".join(batch), style=style)
    return summary


# ---- Projects: instructions, notes and files that give a group of chats shared context ----
PROJECT_DIR = None   # set in start(); originals and extracted text of project files
MAX_PROJECT_FILE = 25_000_000
MAX_FILE_CHARS = 3_000_000
TEXT_EXTS = {
    "txt", "md", "markdown", "rst", "csv", "tsv", "json", "yaml", "yml", "toml", "ini", "cfg", "conf", "xml", "html",
    "htm", "css", "js", "mjs", "ts", "tsx", "jsx", "py", "kt", "kts", "java", "c", "h", "cpp", "hpp", "cc", "ino",
    "rs", "go", "rb", "php", "sh", "bash", "ps1", "bat", "sql", "gradle", "properties", "v", "sv", "vhd", "vhdl",
    "lua", "swift", "cs", "m", "scad", "gcode", "log", "env", "dts", "ld", "s", "asm", "cmake", "mk", "makefile",
    "kicad_sch", "kicad_pcb", "kicad_pro", "net", "cir", "sp", "spice", "svg", "tex",
}
IMAGE_EXTS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
              "gif": "image/gif", "bmp": "image/bmp"}
COLORS = {"cyan", "blue", "red", "green", "purple", "pink", "orange", "yellow"}


def _pdf_text(data):
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages):
            pages.append(f"[page {i + 1}]\n" + (page.extract_text() or ""))
        return "\n\n".join(pages)
    except ImportError:
        pass
    import shutil
    exe = shutil.which("pdftotext")
    if not exe:
        raise ValueError("can't read PDFs on this server: install pypdf (pip install pypdf) or poppler-utils")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        with open(src, "wb") as f:
            f.write(data)
        p = subprocess.run([exe, "-layout", src, "-"], capture_output=True, timeout=120)
        if p.returncode != 0:
            raise ValueError("couldn't read this PDF")
        return p.stdout.decode("utf-8", "replace").replace("\f", "\n\n[next page]\n")


def _docx_text(data):
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(text)


def describe_image(data, mime):
    """Uses the vision add-on to turn an image into searchable text."""
    import base64
    b64 = base64.b64encode(data).decode()
    body = {"model": "local", "temperature": 0.2, "max_tokens": 700,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe this image in detail so it can be used later without seeing it: "
                                         "all readable text, labels, part numbers, component values, connections, "
                                         "measurements and layout. Plain text, no preamble."},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]}
    out = _llama("/v1/chat/completions", body, timeout=300)["choices"][0]["message"].get("content") or ""
    return re.sub(r"<think>[\s\S]*?</think>", "", out).strip()


def extract_text(name, data):
    """Returns (kind, text, note) for an uploaded project file."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else name.lower()
    if ext == "pdf" or data[:5] == b"%PDF-":
        return "pdf", _pdf_text(data), ""
    if ext == "docx":
        return "doc", _docx_text(data), ""
    if ext in IMAGE_EXTS:
        try:
            desc = describe_image(data, IMAGE_EXTS[ext])
            return "image", f"(Image file. Description made when it was added:)\n{desc}", ""
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            return "image", "(Image file. No description: the vision add-on isn't available on the server.)", \
                f"Stella can't see this image yet ({e})"
    if b"\x00" in data[:8000]:
        raise ValueError(f"'{name}' looks like a binary file; add text, code, PDF, Word or image files")
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return ("code" if ext not in ("txt", "md", "markdown", "rst", "csv", "tsv", "log") else "text"), text, ""


def chunk_text(text, size=1800, overlap=200):
    """Splits text into overlapping pieces on line boundaries."""
    text = text.replace("\r\n", "\n")
    if len(text) <= size:
        return [text] if text.strip() else []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut
        piece = text[start:end]
        if piece.strip():
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


_WORD = re.compile(r"[A-Za-z0-9]+")
_STOP = set("""a an and are as at be but by can do does for from has have how i if in into is it its me my no not of
on or our so that the their them then there these they this to was we what when where which who why will with you
your please tell show about""".split())


def _terms(text):
    return [w for w in (t.lower() for t in _WORD.findall(text)) if w not in _STOP and (len(w) > 1 or w.isdigit())]


class ProjectIndex:
    """BM25 keyword search over a project's file chunks (no extra model or packages needed)."""

    def __init__(self):
        self.cache = {}  # project id -> (version, docs, df, avgdl)
        self.lock = threading.Lock()

    def search(self, db, pid, query, k=8):
        import math as _m
        from collections import Counter
        version = db.project_version(pid)
        with self.lock:
            entry = self.cache.get(pid)
            if not entry or entry[0] != version:
                docs = []
                df = Counter()
                for fid, fname, idx, n, text in db.project_chunks(pid):
                    tf = Counter(_terms(fname + " " + text))
                    docs.append((fid, fname, idx, n, text, tf, sum(tf.values()) or 1))
                    df.update(tf.keys())
                avgdl = (sum(d[6] for d in docs) / len(docs)) if docs else 1
                entry = (version, docs, df, avgdl)
                self.cache[pid] = entry
        _, docs, df, avgdl = entry
        q = set(_terms(query))
        if not docs:
            return []
        if not q:
            return [d[:5] for d in docs[:k]]
        N = len(docs)
        scored = []
        for d in docs:
            tf, dl = d[5], d[6]
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    idf = _m.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl / avgdl))
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        keyword = [d[:5] for _, d in scored[:max(k * 3, 20)]]
        meaning = EMBEDDER.rank(db, pid, version, docs, query, max(k * 3, 20))
        if not meaning:
            return keyword[:k]
        # Reciprocal rank fusion: good matches by keywords *or* by meaning rise to the top
        fused = {}
        for ranked in (keyword, meaning):
            for rank, d in enumerate(ranked):
                key = (d[0], d[2])
                fused.setdefault(key, [0.0, d])[0] += 1.0 / (60 + rank)
        best = sorted(fused.values(), key=lambda x: -x[0])
        return [d for _, d in best[:k]]


PROJECT_INDEX = ProjectIndex()


class Embedder:
    """Meaning-based search: turns file chunks and questions into vectors with a small embedding model
    (nomic-embed-text, run by a second llama-server with --embedding). Optional: without it, search
    falls back to keywords only."""

    DOC, QUERY = "search_document: ", "search_query: "

    def __init__(self):
        self.url = ""
        self.pending = []
        self.cond = threading.Condition()
        self.cache = {}     # project id -> (version, keys, matrix or list)
        self.qcache = {}
        self.started = False
        self.healthy = False

    def start(self, url):
        self.url = (url or "").rstrip("/")
        if not self.url or self.started:
            return
        self.started = True
        threading.Thread(target=self._worker, daemon=True, name="embedder").start()

    def queue(self, fid):
        if not self.url:
            return
        with self.cond:
            if fid not in self.pending:
                self.pending.append(fid)
            self.cond.notify()

    def embed(self, texts, prefix):
        import array
        out = []
        for i in range(0, len(texts), 16):
            batch = [prefix + t[:6000] for t in texts[i:i + 16]]
            req = urllib.request.Request(self.url + "/v1/embeddings",
                                         data=json.dumps({"input": batch, "model": "embed"}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=300) as r:
                data = sorted(json.loads(r.read())["data"], key=lambda d: d["index"])
            for d in data:
                v = d["embedding"]
                norm = sum(x * x for x in v) ** 0.5 or 1.0
                out.append(array.array("f", (x / norm for x in v)).tobytes())
        return out

    def _worker(self):
        time.sleep(5)
        for fid in CHATS.files_without_vectors():   # catch up on files added before the model was installed
            self.queue(fid)
        while True:
            with self.cond:
                while not self.pending:
                    self.cond.wait()
                fid = self.pending.pop(0)
            try:
                chunks = CHATS.chunk_texts(fid)
                if chunks and CHATS.file_project(fid) is not None:
                    CHATS.save_vectors(fid, self.embed(chunks, self.DOC))
                    self.healthy = True
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                self.healthy = False
                log(f"meaning search: couldn't index a file yet ({e}); will retry")
                time.sleep(60)
                self.queue(fid)

    def rank(self, db, pid, version, docs, query, k):
        """Chunks ranked by similarity of meaning to the query (empty if the model isn't available)."""
        import array
        if not self.url or not query.strip() or not docs:
            return []
        entry = self.cache.get(pid)
        if not entry or entry[0] != version:
            blobs = db.vectors(pid)
            keys = [(d[0], d[2]) for d in docs if (d[0], d[2]) in blobs]
            vecs = [array.array("f", blobs[key]) for key in keys]
            try:
                import numpy as np
                mat = np.array(vecs, dtype=np.float32) if vecs else None
            except ImportError:
                mat = vecs
            entry = (version, keys, mat)
            self.cache[pid] = entry
        _, keys, mat = entry
        if not keys:
            return []
        qv = self.qcache.get(query)
        if qv is None:
            try:
                qv = array.array("f", self.embed([query], self.QUERY)[0])
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                return []
            if len(self.qcache) > 200:
                self.qcache.clear()
            self.qcache[query] = qv
        try:
            import numpy as np
            scores = (mat @ np.array(qv, dtype=np.float32)).tolist() if not isinstance(mat, list) else None
        except ImportError:
            scores = None
        if scores is None:
            scores = [sum(a * b for a, b in zip(v, qv)) for v in mat]
        by_key = {(d[0], d[2]): d for d in docs}
        order = sorted(range(len(keys)), key=lambda i: -scores[i])[:k]
        return [by_key[keys[i]][:5] for i in order if scores[i] > 0.3]


EMBEDDER = Embedder()


def _file_text(fid):
    try:
        with open(os.path.join(PROJECT_DIR, fid + ".txt"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def build_project_context(pid, query, budget, split=False):
    """The project section of Stella's instructions, fitted to `budget` tokens.
    With split=True returns (stable_text, excerpts_text, tokens): excerpts change with each message, so clients
    attach them to the newest message instead of the instructions (keeps the model server's cache valid)."""
    p = CHATS.project(pid)
    if not p:
        return ("", "", 0) if split else ("", 0)
    head = [f"## Current project: {p['name']}",
            "This chat belongs to the project above. Use its instructions, notes and files; they apply to the whole project."]
    if p["instructions"].strip():
        head.append("Project instructions (follow these):\n" + p["instructions"].strip())
    if p["notes"]:
        lines, used = [], 0
        for n in p["notes"]:
            line = f"- [{n['id']}] {n['text']}"
            used += len(line)
            if used > budget * 1.2:   # characters ~ tokens/3; keep notes to about 40% of the budget
                lines.append("- (more notes not shown)")
                break
            lines.append(line)
        head.append("Project notes (add new ones with project_note):\n" + "\n".join(lines))
    files = p["files"]
    if files:
        head.append("Project files: " + "; ".join(f"{f['name']} (~{f['tokens']} tokens)" for f in files))
    text = "\n\n".join(head)
    used = count_tokens(text)
    remaining = budget - used
    if not files or remaining < 200:
        if files:
            text += "\n\nThe file contents don't fit right now; call project_search to look things up in them."
        return (text, "", count_tokens(text)) if split else (text, count_tokens(text))
    total = sum(f["tokens"] for f in files)
    blocks = []
    if total <= remaining - 50:
        for f in files:
            blocks.append(f"### File: {f['name']}\n{_file_text(f['id'])}")
        text += "\n\nFull contents of the project files:\n\n" + "\n\n".join(blocks)
        return (text, "", count_tokens(text)) if split else (text, count_tokens(text))
    else:
        whole = set()
        for f in sorted(files, key=lambda f: f["tokens"]):   # small files (pinouts, notes) go in whole
            if f["tokens"] > min(500, remaining * 0.25):
                break
            block = f"### File: {f['name']}\n{_file_text(f['id'])}"
            blocks.append(block)
            whole.add(f["id"])
            remaining -= count_tokens(block)
        if whole:
            text += "\n\nSmall project files:\n\n" + "\n\n".join(blocks)
            blocks = []
        hits = [h for h in PROJECT_INDEX.search(CHATS, pid, query, k=40) if h[0] not in whole]
        for fid, fname, idx, n, chunk in hits:
            block = f"### {fname} (part {idx + 1} of {n})\n{chunk}"
            t = count_tokens(block)
            if t > remaining - 60:
                continue
            blocks.append(block)
            remaining -= t
            if remaining < 150:
                break
        text += ("\n\nThe bigger files don't fit whole: the parts related to the latest message are attached to it; "
                 "call project_search for anything else.")
        excerpts = ("From the project files:\n\n" + "\n\n".join(blocks)) if blocks else ""
        if split:
            return text, excerpts, count_tokens(text) + count_tokens(excerpts)
        return (text + "\n\n" + excerpts).strip(), count_tokens(text) + count_tokens(excerpts)


TOOL_GROUPS = {
    "core": ["calculate", "remember", "set_reminder", "add_to_list", "web_search", "fetch_page"],
    "electronics": ["ohms_law", "voltage_divider", "led_resistor", "rc_filter", "combine_resistors",
                    "standard_resistor", "render_diagram"],
    "lists": ["list_reminders", "cancel_reminder", "show_list", "check_off", "forget"],
    "github": ["github_repos", "github_read", "github_search", "github_history", "github_issues", "github_builds",
               "github_commit", "github_pull_request"],
    "project": ["project_note", "project_search"],
    "library": ["library_search"],
}
TOOL_TRIGGERS = {
    "electronics": re.compile(r"resist|volt|ohm|\bleds?\b|circuit|schemat|diagram|flow ?chart|capacit|filter|divider|"
                              r"current|\bamps?\b|watt|pcb|timing|waveform|state machine|e12|e24|e96|draw|sketch|"
                              r"inductor|transistor|mosfet|op.?amp|\bmah\b|frequency|\bhz\b", re.I),
    "lists": re.compile(r"remind|\blists?\b|shopping|check(ed)? off|tick|cancel|to.?do|groceries|forget|"
                        r"don'?t remember|wrong about", re.I),
    "github": re.compile(r"github|\brepo|commit|branch|pull request|\bpr\b|actions|\bbuild|workflow|\bissues?\b|"
                         r"source code|my code|codebase", re.I),
}


def select_tools(query, mode="", mode_tools="all", in_project=False, sticky=()):
    """Picks the server tools worth sending for this message. Every tool definition costs context tokens
    (all of them together are about 4K), so only likely-needed groups go with each request."""
    if mode_tools == "minimal":
        names = ["remember", "set_reminder", "add_to_list", "web_search", "fetch_page"]
        if TOOL_TRIGGERS["lists"].search(query or ""):
            names.append("forget")
    else:
        names = list(TOOL_GROUPS["core"])
        for group, rx in TOOL_TRIGGERS.items():
            if rx.search(query or ""):
                names += TOOL_GROUPS[group]
        if mode == "electronics":
            names += TOOL_GROUPS["electronics"]
        if mode == "programmer":
            names += TOOL_GROUPS["github"]
    if in_project:
        names += TOOL_GROUPS["project"]
    if CHATS.library()["files"]:
        names += TOOL_GROUPS["library"]
    # Tools already offered earlier in this chat stay, so the tool list (which sits at the start of the prompt)
    # only grows occasionally instead of changing every message; that lets the model server reuse its cache.
    names += [n for n in sticky if not (n in PROJECT_TOOLS and not in_project)]
    wanted = set(names)
    return [n for n in SERVER_TOOLS if n in wanted]


_MEMVEC = {}


def select_memories(query, limit=8, all_up_to=12):
    """(stable_section, per_message_lines). Few memories: all go in the stable instructions. Many: the newest few
    stay stable and the ones related to this message ride along with it, chosen by meaning (or keywords)."""
    mems = CHATS.memories()
    if not mems:
        return "", ""
    head = "What you remember about the user (id in brackets, for forget). Use naturally; don't recite.\n"
    if len(mems) <= all_up_to:
        return head + "\n".join(f"- [{m['id']}] {m['text']}" for m in mems), ""
    # Always kept: the newest few, plus standing preferences ("prefers short answers") that apply to everything
    pref = re.compile(r"\b(prefers?|likes?|wants?|always|never|don'?t|do not|call(s)? (him|her|them|me)|"
                      r"goes by|name is|pronouns?)\b", re.I)
    stable = mems[:4] + [m for m in mems[4:] if pref.search(m["text"])][:6]
    rest = [m for m in mems if m not in stable]
    scores = None
    if EMBEDDER.url and query.strip():
        try:
            import array
            todo = [m["text"] for m in rest if m["text"] not in _MEMVEC]
            if todo:
                for t, v in zip(todo, EMBEDDER.embed(todo, EMBEDDER.DOC)):
                    _MEMVEC[t] = array.array("f", v)
            q = array.array("f", EMBEDDER.embed([query[-2000:]], EMBEDDER.QUERY)[0])
            scores = [sum(a * b for a, b in zip(_MEMVEC[m["text"]], q)) for m in rest]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            scores = None
    if scores is None:
        qt = set(_terms(query))
        scores = [len(qt & set(_terms(m["text"]))) / (1 + len(qt)) for m in rest]
    ranked = [m for sc, m in sorted(zip(scores, rest), key=lambda x: -x[0]) if sc > (0.35 if EMBEDDER.url else 0)][:limit]
    turn = ("Possibly relevant memories:\n" + "\n".join(f"- [{m['id']}] {m['text']}" for m in ranked)) if ranked else ""
    return head + "\n".join(f"- [{m['id']}] {m['text']}" for m in stable), turn


HARD_QUESTION = re.compile(
    r"```|\bdebug|\bbug\b|\berror\b|exception|traceback|stack ?trace|doesn'?t (work|compile)|won'?t (work|compile)|"
    r"what'?s wrong|fix (this|my|it)|why (does|is|do|did|doesn'?t|isn'?t|won'?t)|\bprove\b|derive|\bsolve\b|"
    r"algorithm|optimi[sz]|refactor|architect|\bdesign (a|an|the|my)\b|trade-?offs?|compare|pros and cons|"
    r"step by step|work out|figure out|\bplan (a|an|my|the)\b|equation|integral|probability|review (my|this)",
    re.I)


def wants_thinking(query):
    """Auto thinking: reason first only when the newest message looks hard (debugging, maths, design, long
    questions), and answer straight away otherwise ("set a timer" shouldn't think)."""
    last = (query or "").strip().splitlines()
    text = query or ""
    return bool(HARD_QUESTION.search(text)) or len(text) > 700 or (len(last) > 12)


def plan_context(data):
    """Works out what fits in the model's context for the next request.

    Input: project_id, query, fixed (system prompt + tool definitions), messages [{role, content}], images.
    Output: the project section, token counts, and keep_from (index of the oldest message that still fits)."""
    n_ctx = context_size()
    reply_reserve = min(4096, max(1024, n_ctx // 5))
    # Thinking: "on"/"off" from the user or mode, or "auto" (decided from the message here)
    pref = str(data.get("thinking") or "")
    think = pref == "on" or (pref == "auto" and wants_thinking(str(data.get("last", "")) or str(data.get("query", ""))))
    if think:
        reply_reserve += 1024 if n_ctx <= 8192 else 2048   # room for the capped reasoning
    tools = None
    if "mode_tools" in data:   # newer clients let the server choose which server tools to send
        tools = select_tools(str(data.get("query", "")), str(data.get("mode", "")), str(data.get("mode_tools")),
                             bool(data.get("project_id")), [str(n) for n in (data.get("sticky") or [])])
    tools_json = json.dumps([SERVER_TOOLS[n][1] for n in tools]) if tools else ""
    split = bool(data.get("split"))   # newer clients: stable instructions + a per-message context block
    query = str(data.get("query", ""))
    mem_stable, mem_turn = select_memories(query) if split else ("", "")
    fixed_t = count_tokens(str(data.get("fixed", "")) + tools_json) + 900 * int(data.get("images") or 0)
    fixed_t += count_tokens(mem_stable) + count_tokens(mem_turn)
    avail = n_ctx - reply_reserve - fixed_t
    proj_text, proj_t, excerpts = "", 0, ""
    pid = str(data.get("project_id") or "")
    if pid:
        share = 0.2 if n_ctx <= 8192 else 0.3
        budget = max(0, min(int(n_ctx * share), avail - 600))
        if split:
            proj_text, excerpts, proj_t = build_project_context(pid, query, budget, split=True)
        else:
            proj_text, proj_t = build_project_context(pid, query, budget)
    lib = CHATS.library()["files"]
    if lib:
        names = "; ".join(f["name"] for f in lib[:40]) + ("; …" if len(lib) > 40 else "")
        lib_text = ("## Document library\nThe user's shared library (usable from any chat) contains: " + names +
                    ". Call library_search to look things up in it when it could help.")
        proj_text = (proj_text + "\n\n" + lib_text).strip()
        proj_t += count_tokens(lib_text)
    avail -= proj_t
    msgs = data.get("messages") or []
    counts = [count_tokens(str(m.get("content", ""))) + 6 for m in msgs]
    keep_from, kept = len(msgs), 0
    for i in range(len(msgs) - 1, -1, -1):
        if kept + counts[i] > avail and keep_from < len(msgs):
            break
        kept += counts[i]
        keep_from = i
    return {"n_ctx": n_ctx, "reply_reserve": reply_reserve, "fixed_tokens": fixed_t, "project_tokens": proj_t,
            "history_tokens": sum(counts), "kept_tokens": kept, "keep_from": keep_from,
            "project_context": proj_text, "tools": tools, "memory_context": mem_stable, "think": think,
            "turn_context": "\n\n".join(x for x in (mem_turn, excerpts) if x)}


def tool_project_note(args):
    pid = str(args.get("project_id", ""))
    if not pid or not CHATS.project(pid):
        raise ValueError("this chat isn't in a project")
    note = CHATS.add_project_note(pid, args.get("note", ""), "stella")
    return {"saved": note["text"], "id": note["id"]}, f"Saved to project notes: {note['text']}"


def tool_project_search(args):
    pid = str(args.get("project_id", ""))
    if not pid or not CHATS.project(pid):
        raise ValueError("this chat isn't in a project")
    query = str(args.get("query", ""))
    hits = PROJECT_INDEX.search(CHATS, pid, query, k=6)
    results = [{"file": f, "part": f"{i + 1}/{n}", "text": t} for _, f, i, n, t in hits]
    return {"results": results, "found": len(results)}, \
        f"Searched project files for “{query}”: {len(results)} match{'es' if len(results) != 1 else ''}"


SERVER_TOOLS["project_note"] = (tool_project_note, _fn(
    "project_note",
    "Save a note to the current project (decisions, specs, part choices, pin assignments, TODOs) so it is "
    "available in every chat of this project. Only available in project chats.",
    {"note": _p("The note, one or two sentences")}, ["note"]))
SERVER_TOOLS["project_search"] = (tool_project_search, _fn(
    "project_search",
    "Search the current project's files (datasheets, code, documents) for a topic, register, pin, function name "
    "etc. Use when the answer may be in the files but isn't shown in your instructions.",
    {"query": _p("Keywords to look for, e.g. 'ADC reference voltage'")}, ["query"]))
PROJECT_TOOLS = {"project_note", "project_search"}


def tool_library_search(args):
    query = str(args.get("query", ""))
    hits = PROJECT_INDEX.search(CHATS, LIBRARY_ID, query, k=6)
    results = [{"file": f, "part": f"{i + 1}/{n}", "text": t} for _, f, i, n, t in hits]
    return {"results": results, "found": len(results)}, \
        f"Searched the library for “{query}”: {len(results)} match{'es' if len(results) != 1 else ''}"


SERVER_TOOLS["library_search"] = (tool_library_search, _fn(
    "library_search",
    "Search the user's shared document library (datasheets, manuals, notes, code they've added) for a topic. "
    "Works in any chat.",
    {"query": _p("What to look for, in plain words, e.g. 'maximum charge current of the TP4056'")}, ["query"]))


def tool_set_reminder(args):
    due = _parse_when(args.get("when", ""))
    r = CHATS.add_reminder(args.get("text", ""), due, str(args.get("repeat") or "none"))
    rep = "" if r["repeat"] == "none" else f", repeats {r['repeat']}"
    return {"id": r["id"], "text": r["text"], "due": _fmt_time(r["due_at"]), "repeat": r["repeat"],
            "note": "It will ring on all the user's phones."}, f"Reminder set for {_fmt_time(r['due_at'])}{rep}: {r['text']}"


def tool_list_reminders(args):
    items = [r for r in CHATS.reminders(0) if not r["done"]]
    return {"reminders": [{"id": r["id"], "text": r["text"], "due": _fmt_time(r["due_at"]), "repeat": r["repeat"]}
                          for r in items]}, f"{len(items)} upcoming reminder{'s' if len(items) != 1 else ''}"


def tool_cancel_reminder(args):
    found = CHATS.find_reminders(args.get("reminder", ""))
    if not found:
        raise ValueError("no matching reminder; call list_reminders to see them")
    if len(found) > 1:
        return {"ambiguous": [{"id": r["id"], "text": r["text"], "due": _fmt_time(r["due_at"])} for r in found]}, \
            "Several reminders match"
    CHATS.update_reminder(found[0]["id"], cancel=True)
    return {"cancelled": found[0]["text"]}, f"Cancelled reminder: {found[0]['text']}"


def tool_add_to_list(args):
    items = args.get("items") or []
    if isinstance(items, str):
        items = [x for x in re.split(r"[,\n]", items)]
    name, added = CHATS.add_items(args.get("list", "to-do"), items)
    if not added:
        raise ValueError("nothing to add")
    return {"list": name, "added": added}, f"Added to {name} list: {', '.join(added)}"


def tool_show_list(args):
    name = str(args.get("list") or "").strip()
    lists = CHATS.lists()
    if not name:
        summary = {n: len([i for i in items if not i["done"]]) for n, items in lists.items()}
        return {"lists": summary}, f"{len(summary)} list{'s' if len(summary) != 1 else ''}"
    key = CHATS._list_name(name)
    items = lists.get(key, [])
    return {"list": key, "items": [("✓ " if i["done"] else "") + i["text"] for i in items]}, \
        f"{key.capitalize()} list: {len([i for i in items if not i['done']])} item(s)"


def tool_check_off(args):
    items = args.get("items") or []
    if isinstance(items, str):
        items = [items]
    name, changed = CHATS.check_items(args.get("list", "to-do"), items, delete=bool(args.get("remove")))
    if not changed:
        raise ValueError(f"didn't find those on the {name} list")
    return {"list": name, "checked": changed}, f"Checked off on {name} list: {', '.join(changed)}"


SERVER_TOOLS["set_reminder"] = (tool_set_reminder, _fn(
    "set_reminder",
    "Set a reminder that rings on all the user's phones at a given time, e.g. 'remind me to reflow the board at 5'. "
    "Use this for reminders; use set_alarm only for wake-up alarms in the phone's clock app.",
    {"text": _p("What to remind about, short, e.g. 'Reflow the board'"),
     "when": _p("Date and time in ISO 8601 with the user's UTC offset, e.g. 2026-11-04T17:00:00-06:00. Work it out "
                "from the current date and time in your instructions."),
     "repeat": _p("none (default), daily, weekdays, weekly or monthly")}, ["text", "when"]))
SERVER_TOOLS["list_reminders"] = (tool_list_reminders, _fn(
    "list_reminders", "List the user's upcoming reminders.", {}, []))
SERVER_TOOLS["cancel_reminder"] = (tool_cancel_reminder, _fn(
    "cancel_reminder", "Cancel a reminder.", {"reminder": _p("Its id or words from it")}, ["reminder"]))
SERVER_TOOLS["add_to_list"] = (tool_add_to_list, _fn(
    "add_to_list",
    "Add items to one of the user's lists (e.g. shopping, parts, to-do, movies). Lists are created as needed and "
    "synced to all devices.",
    {"list": _p("List name, e.g. 'shopping' or 'parts'"),
     "items": {"type": "array", "items": {"type": "string"}, "description": "Items to add"}}, ["list", "items"]))
SERVER_TOOLS["show_list"] = (tool_show_list, _fn(
    "show_list", "Show a list's items, or all list names when no list is given.",
    {"list": _p("List name, or empty for all lists")}, []))
SERVER_TOOLS["check_off"] = (tool_check_off, _fn(
    "check_off", "Tick off (or remove) items on a list.",
    {"list": _p("List name"), "items": {"type": "array", "items": {"type": "string"}, "description": "Items or words"},
     "remove": _p("true to delete them instead of ticking them off", "boolean")}, ["list", "items"]))


# ---- Web search and reading pages -------------------------------------------------
SEARXNG_URL = ""   # e.g. http://127.0.0.1:8888 (set in start())
WEB_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def _public_host(host):
    """True if every address the host resolves to is on the public internet (no LAN, localhost, metadata)."""
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ValueError(f"couldn't look up {host} (is the server online?)")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))):
            return False
    return True


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urlparse(newurl)
        if u.scheme not in ("http", "https") or not _public_host(u.hostname or ""):
            raise urllib.error.URLError("redirect to a private or unsupported address was blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_public(url, data=None, headers=None, timeout=20, max_bytes=3_000_000):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http and https addresses can be opened")
    if not _public_host(u.hostname):
        raise ValueError("that address is on a private network; Stella only reads public web pages")
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": WEB_UA, "Accept-Language": "en", **(headers or {})})
    with opener.open(req, timeout=timeout) as r:
        body = r.read(max_bytes + 1)
        return body[:max_bytes], r.headers.get("Content-Type", ""), r.geturl()


def _html_text(html):
    import html as _h
    title = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form|aside)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article|pre)>", "\n", html)
    html = re.sub(r"(?i)<li[^>]*>", "\n- ", html)
    text = _h.unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return (_h.unescape(title.group(1)).strip() if title else ""), text


def _searx(query, n):
    url = f"{SEARXNG_URL.rstrip('/')}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "StellAI"}), timeout=20) as r:
        data = json.loads(r.read())
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:300]}
            for x in data.get("results", [])[:n]]


def _duckduckgo(query, n):
    import html as _h
    body, _, _ = _open_public("https://html.duckduckgo.com/html/", data=urllib.parse.urlencode({"q": query}).encode(),
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    page = body.decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>([\s\S]*?)'
                         r'(?:class="result__snippet"[^>]*>([\s\S]*?)</a>|<div class="result results_links)', page):
        href = _h.unescape(m.group(1))
        q = parse_qs(urlparse(href).query)
        url = q.get("uddg", [href])[0]
        if url.startswith("//"):
            url = "https:" + url
        if "duckduckgo.com/y.js" in url:   # ads
            continue
        title = _h.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        snippet = _h.unescape(re.sub(r"<[^>]+>", "", m.group(4) or "")).strip()
        out.append({"title": title, "url": url, "snippet": snippet[:300]})
        if len(out) >= n:
            break
    return out


def tool_web_search(args):
    query = " ".join(str(args.get("query", "")).split())[:300]
    if not query:
        raise ValueError("what should I search for?")
    n = max(1, min(10, int(args.get("count") or 6)))
    source = "SearXNG"
    results = []
    if SEARXNG_URL:
        try:
            results = _searx(query, n)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"SearXNG unavailable ({e}); using DuckDuckGo")
    if not results:
        source = "DuckDuckGo"
        results = _duckduckgo(query, n)
    return {"query": query, "results": results, "source": source,
            "note": "Cite the URLs you use. Call fetch_page to read a result in full."}, \
        f"Searched the web for “{query}”: {len(results)} result{'s' if len(results) != 1 else ''}"


def tool_fetch_page(args):
    url = str(args.get("url", "")).strip()
    limit = max(1000, min(30000, int(args.get("max_chars") or 12000)))
    body, ctype, final = _open_public(url)
    if "pdf" in ctype or body[:5] == b"%PDF-":
        title, text = url.rsplit("/", 1)[-1], _pdf_text(body)
    elif "html" in ctype or b"<html" in body[:2000].lower():
        title, text = _html_text(body.decode("utf-8", "replace"))
    else:
        title, text = url, body.decode("utf-8", "replace")
    find = str(args.get("find") or "").strip().lower()
    if find and find in text.lower():
        i = text.lower().index(find)
        text = text[max(0, i - limit // 3):]
    truncated = len(text) > limit
    return {"url": final, "title": title, "text": text[:limit], "truncated": truncated}, \
        f"Read {title or final}"[:200]


SERVER_TOOLS["web_search"] = (tool_web_search, _fn(
    "web_search",
    "Search the web for current information: documentation, news, prices, part availability, error messages. "
    "Returns titles, URLs and snippets.",
    {"query": _p("Search query"), "count": _p("Number of results (1-10, default 6)", "integer")}, ["query"]))
SERVER_TOOLS["fetch_page"] = (tool_fetch_page, _fn(
    "fetch_page",
    "Read a public web page or PDF as text (e.g. a search result, documentation or a datasheet URL).",
    {"url": _p("The http(s) address"), "find": _p("Optional words to jump to in a long page"),
     "max_chars": _p("Maximum characters to return (default 12000)", "integer")}, ["url"]))


# ---- GitHub ------------------------------------------------------------------------
GITHUB_FILE = None      # JSON with token + default repo, set in start()
GITHUB_API = os.environ.get("STELLAI_GITHUB_API", "https://api.github.com")
APPROVAL_TOOLS = {"github_commit", "github_pull_request"}   # clients must ask the user first


def github_config():
    try:
        with open(GITHUB_FILE) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def save_github_config(data):
    cfg = github_config()
    if "token" in data:
        cfg["token"] = str(data.get("token") or "").strip()
    if "default_repo" in data:
        repo = str(data.get("default_repo") or "").strip().strip("/")
        if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("default repository must look like owner/name")
        cfg["default_repo"] = repo
    fd = os.open(GITHUB_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    os.replace(GITHUB_FILE + ".tmp", GITHUB_FILE)
    return github_status()


def _gh(method, path, body=None, accept="application/vnd.github+json", raw=False, timeout=30):
    token = github_config().get("token", "")
    url = path if path.startswith(("https://", "http://")) else GITHUB_API + path
    headers = {"Accept": accept, "User-Agent": "StellAI", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read()).get("message", "")
        except (ValueError, OSError):
            pass
        if e.code in (401, 403) and not token:
            msg += " (add a GitHub token in StellAI's settings to use private repositories)"
        elif e.code == 404:
            msg = "not found (check the repository name and path" + ("" if token else ", or add a GitHub token") + ")"
        raise RuntimeError(f"GitHub {e.code}: {msg}".strip())
    if raw:
        return data
    return json.loads(data) if data else {}


def github_status():
    cfg = github_config()
    out = {"token_set": bool(cfg.get("token")), "default_repo": cfg.get("default_repo", ""), "user": ""}
    if cfg.get("token"):
        try:
            out["user"] = _gh("GET", "/user").get("login", "")
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            out["error"] = str(e)
    return out


def _repo(args):
    repo = str(args.get("repo") or github_config().get("default_repo") or "").strip().strip("/")
    repo = re.sub(r"^https?://github\.com/", "", repo).removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("which repository? Give it as owner/name (or set a default repo in Settings → GitHub)")
    return repo


def _q(path):
    return urllib.parse.quote(str(path).strip("/"))


def tool_github_repos(args):
    if not github_config().get("token"):
        raise ValueError("add a GitHub token in Settings → GitHub to list your repositories")
    repos = _gh("GET", "/user/repos?per_page=50&sort=updated")
    return {"repos": [{"name": r["full_name"], "private": r["private"], "description": r.get("description") or "",
                       "default_branch": r["default_branch"]} for r in repos]}, f"Listed {len(repos)} repositories"


def tool_github_read(args):
    import base64
    repo = _repo(args)
    path = str(args.get("path") or "").strip("/")
    ref = str(args.get("ref") or "")
    data = _gh("GET", f"/repos/{repo}/contents/{_q(path)}" + (f"?ref={urllib.parse.quote(ref)}" if ref else ""))
    if isinstance(data, list):
        items = [("📁 " if x["type"] == "dir" else "") + x["path"] for x in data]
        return {"repo": repo, "path": path or "/", "entries": items}, f"Listed {repo}/{path}"
    if data.get("encoding") == "base64" and data.get("content") is not None:
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    elif data.get("download_url"):
        text = _gh("GET", data["download_url"], raw=True).decode("utf-8", "replace")
    else:
        raise ValueError("can't read that file")
    start = max(1, int(args.get("start_line") or 1))
    lines = text.splitlines()
    chunk = "\n".join(lines[start - 1:start - 1 + 1500])
    truncated = len(chunk) > 40000 or start - 1 + 1500 < len(lines)
    return {"repo": repo, "path": path, "sha": data.get("sha"), "lines": len(lines), "from_line": start,
            "text": chunk[:40000], "truncated": truncated,
            "note": "Use start_line to read further." if truncated else ""}, f"Read {repo}/{path}"


def tool_github_search(args):
    repo = _repo(args)
    query = str(args.get("query", "")).strip()
    if not github_config().get("token"):
        raise ValueError("GitHub code search needs a token (Settings → GitHub)")
    data = _gh("GET", "/search/code?per_page=10&q=" + urllib.parse.quote(f"{query} repo:{repo}"),
               accept="application/vnd.github.text-match+json")
    hits = [{"path": i["path"], "matches": [m.get("fragment", "")[:300] for m in i.get("text_matches", [])][:2]}
            for i in data.get("items", [])]
    return {"repo": repo, "results": hits}, f"Searched {repo} for “{query}”: {len(hits)} file(s)"


def tool_github_history(args):
    repo = _repo(args)
    path = str(args.get("path") or "")
    n = max(1, min(30, int(args.get("count") or 10)))
    commits = _gh("GET", f"/repos/{repo}/commits?per_page={n}" + (f"&path={urllib.parse.quote(path)}" if path else ""))
    return {"repo": repo, "commits": [{"sha": c["sha"][:7], "message": c["commit"]["message"][:300],
                                       "author": c["commit"]["author"]["name"], "date": c["commit"]["author"]["date"]}
                                      for c in commits]}, f"Read {len(commits)} commits of {repo}"


def tool_github_issues(args):
    repo = _repo(args)
    state = args.get("state") if args.get("state") in ("open", "closed", "all") else "open"
    items = _gh("GET", f"/repos/{repo}/issues?per_page=20&state={state}")
    return {"repo": repo, "issues": [{"number": i["number"], "title": i["title"], "pull_request": "pull_request" in i,
                                      "state": i["state"], "body": (i.get("body") or "")[:500]} for i in items]}, \
        f"Read {len(items)} {state} issues/PRs of {repo}"


def tool_github_builds(args):
    """Recent GitHub Actions runs, with the end of the log for failed jobs (for fixing build errors)."""
    repo = _repo(args)
    runs = _gh("GET", f"/repos/{repo}/actions/runs?per_page=6").get("workflow_runs", [])
    out = [{"id": r["id"], "workflow": r["name"], "status": r["status"], "result": r.get("conclusion"),
            "branch": r["head_branch"], "commit": r["head_sha"][:7], "title": r.get("display_title", ""),
            "started": r["run_started_at"]} for r in runs]
    failed = next((r for r in runs if r.get("conclusion") == "failure"), None)
    detail = None
    if failed:
        jobs = _gh("GET", f"/repos/{repo}/actions/runs/{failed['id']}/jobs").get("jobs", [])
        detail = []
        for j in jobs:
            if j.get("conclusion") != "failure":
                continue
            steps = [s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"]
            log_tail = ""
            try:
                text = _gh("GET", f"/repos/{repo}/actions/jobs/{j['id']}/logs", raw=True).decode("utf-8", "replace")
                lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", ln) for ln in text.splitlines()]
                errs = [i for i, ln in enumerate(lines) if re.search(r"\berror\b|^e: |FAILED|Traceback", ln, re.I)]
                start = max(0, (errs[0] - 5) if errs else len(lines) - 60)
                log_tail = "\n".join(lines[start:start + 80])
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                log_tail = f"(couldn't read the log: {e})"
            detail.append({"job": j["name"], "failed_steps": steps, "log": log_tail[:8000]})
    return {"repo": repo, "runs": out, "latest_failure": detail}, f"Checked GitHub Actions for {repo}"


def _default_branch(repo):
    return _gh("GET", f"/repos/{repo}")["default_branch"]


def tool_github_commit(args):
    """Commits files to a branch (never the default branch) using the Git Data API. Needs the user's approval."""
    import base64
    if args.get("approved") is not True:
        raise ValueError("this needs the user's approval in the app first")
    if not github_config().get("token"):
        raise ValueError("committing needs a GitHub token with Contents write access (Settings → GitHub)")
    repo = _repo(args)
    branch = str(args.get("branch") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch:
        raise ValueError("give a branch name like 'stella/fix-build'")
    default = _default_branch(repo)
    if branch == default:
        raise ValueError(f"I don't commit straight to {default}; use a new branch and open a pull request")
    message = str(args.get("message") or "").strip() or "Update from Stella"
    files = args.get("files") or []
    deletes = [str(p).strip("/") for p in (args.get("delete") or [])]
    if not files and not deletes:
        raise ValueError("no files to commit")
    try:
        head = _gh("GET", f"/repos/{repo}/git/ref/heads/{_q(branch)}")["object"]["sha"]
        created = False
    except RuntimeError:
        head = _gh("GET", f"/repos/{repo}/git/ref/heads/{_q(default)}")["object"]["sha"]
        created = True
    base_tree = _gh("GET", f"/repos/{repo}/git/commits/{head}")["tree"]["sha"]
    tree = []
    for f in files:
        path = str(f.get("path", "")).strip("/")
        if not path or ".." in path.split("/"):
            raise ValueError(f"bad file path: {path!r}")
        blob = _gh("POST", f"/repos/{repo}/git/blobs",
                   {"content": base64.b64encode(str(f.get("content", "")).encode()).decode(), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    for path in deletes:
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    new_tree = _gh("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]
    commit = _gh("POST", f"/repos/{repo}/git/commits", {"message": message, "tree": new_tree, "parents": [head]})["sha"]
    if created:
        _gh("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    else:
        _gh("PATCH", f"/repos/{repo}/git/refs/heads/{_q(branch)}", {"sha": commit})
    url = f"https://github.com/{repo}/commit/{commit}"
    return {"repo": repo, "branch": branch, "commit": commit[:7], "url": url, "new_branch": created,
            "next": "Offer to open a pull request with github_pull_request."}, \
        f"Committed {len(files) + len(deletes)} file(s) to {repo}:{branch} ({commit[:7]})"


def tool_github_pull_request(args):
    if args.get("approved") is not True:
        raise ValueError("this needs the user's approval in the app first")
    repo = _repo(args)
    branch = str(args.get("branch") or "").strip()
    base = str(args.get("base") or "") or _default_branch(repo)
    pr = _gh("POST", f"/repos/{repo}/pulls", {"title": str(args.get("title") or "Changes from Stella")[:200],
                                               "head": branch, "base": base, "body": str(args.get("body") or "")})
    return {"number": pr["number"], "url": pr["html_url"]}, f"Opened pull request #{pr['number']}: {pr['html_url']}"


SERVER_TOOLS["github_repos"] = (tool_github_repos, _fn(
    "github_repos", "List the user's GitHub repositories.", {}, []))
SERVER_TOOLS["github_read"] = (tool_github_read, _fn(
    "github_read", "Read a file, or list a folder, in a GitHub repository.",
    {"repo": _p("owner/name (default: the user's default repo)"), "path": _p("File or folder path; empty for the root"),
     "ref": _p("Branch, tag or commit (optional)"), "start_line": _p("Line to start from for long files", "integer")},
    ["path"]))
SERVER_TOOLS["github_search"] = (tool_github_search, _fn(
    "github_search", "Search code in a GitHub repository (function names, strings, settings).",
    {"repo": _p("owner/name (optional)"), "query": _p("What to search for")}, ["query"]))
SERVER_TOOLS["github_history"] = (tool_github_history, _fn(
    "github_history", "Recent commits of a repository, optionally for one file.",
    {"repo": _p("owner/name (optional)"), "path": _p("File path (optional)"), "count": _p("How many (max 30)", "integer")},
    []))
SERVER_TOOLS["github_issues"] = (tool_github_issues, _fn(
    "github_issues", "Issues and pull requests of a repository.",
    {"repo": _p("owner/name (optional)"), "state": _p("open (default), closed or all")}, []))
SERVER_TOOLS["github_builds"] = (tool_github_builds, _fn(
    "github_builds",
    "Recent GitHub Actions builds of a repository, including the error log of the latest failed build. Use this when "
    "the user's build fails.",
    {"repo": _p("owner/name (optional)")}, []))
SERVER_TOOLS["github_commit"] = (tool_github_commit, _fn(
    "github_commit",
    "Commit file changes to a branch of a repository (never the default branch; a new branch is created if needed). "
    "The user must approve. Give complete file contents, not diffs; read the file first.",
    {"repo": _p("owner/name (optional)"), "branch": _p("Branch name, e.g. 'stella/fix-diagram-regex'"),
     "message": _p("Commit message"),
     "files": {"type": "array", "description": "Files to create or replace",
               "items": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                         "required": ["path", "content"]}},
     "delete": {"type": "array", "items": {"type": "string"}, "description": "Paths to delete (optional)"}},
    ["branch", "message", "files"]))
SERVER_TOOLS["github_pull_request"] = (tool_github_pull_request, _fn(
    "github_pull_request", "Open a pull request from a branch into the default branch. The user must approve.",
    {"repo": _p("owner/name (optional)"), "branch": _p("Branch with the changes"), "title": _p("Title"),
     "body": _p("Description of the changes")}, ["branch", "title"]))


# ---- Compact tool descriptions -----------------------------------------------------
# Tool definitions are sent with every message and cost context tokens, so they're kept short here.
_Q = "e.g. 4.7k, 3.3V, 20mA"
COMPACT_TOOLS = {
    "calculate": ("Exact maths; always use instead of mental arithmetic. SI values (4.7k, 10u, 3.3V), sqrt ln log10 exp "
                  "sin cos tan abs round min max, parallel(a,b,…), db(ratio), pi, e.",
                  {"expression": "e.g. 12*4.7k/(4.7k+10k)", "unit": "optional unit of the answer"}),
    "ohms_law": ("Ohm's law and power: give any two of voltage, current, resistance, power.",
                 {"voltage": _Q, "current": _Q, "resistance": _Q, "power": _Q}),
    "voltage_divider": ("Voltage divider (R2 to ground). Give three of vin, vout, r1, r2, or vin+vout for the best "
                        "standard pair.", {"vin": _Q, "vout": _Q, "r1": _Q, "r2": _Q, "series": "E12, E24 or E96"}),
    "led_resistor": ("LED series resistor with standard value and power.",
                     {"supply_voltage": _Q, "led_voltage": "forward voltage, e.g. 2.0 red, 3.1 blue",
                      "current_ma": "mA", "leds_in_series": "default 1"}),
    "rc_filter": ("RC filter cutoff fc=1/(2πRC): give two of resistance, capacitance, frequency.",
                  {"resistance": _Q, "capacitance": _Q, "frequency": _Q}),
    "combine_resistors": ("Resistors in series or parallel.", {"values": "e.g. ['10k','4.7k']", "mode": "series or parallel"}),
    "standard_resistor": ("Nearest standard resistor values.", {"value": _Q, "series": "E12, E24 or E96"}),
    "remember": ("Save a lasting fact about the user for future chats.", {"fact": "one short third-person sentence"}),
    "forget": ("Delete a saved fact about the user.", {"memory": "its id or words from it"}),
    "project_note": ("Save a note (decision, spec, pinout, TODO) to this chat's project.", {"note": "one or two sentences"}),
    "project_search": ("Search this project's files.", {"query": "keywords, e.g. ADC reference voltage"}),
    "library_search": ("Search the user's document library (any chat).", {"query": "plain words"}),
    "set_reminder": ("Reminder that rings on all the user's phones. (Wake-up alarms: set_alarm.)",
                     {"text": "short, e.g. Reflow the board",
                      "when": "ISO 8601 with the user's UTC offset, e.g. 2026-11-04T17:00:00-06:00",
                      "repeat": "none, daily, weekdays, weekly or monthly"}),
    "list_reminders": ("List upcoming reminders.", {}),
    "cancel_reminder": ("Cancel a reminder.", {"reminder": "id or words"}),
    "add_to_list": ("Add items to a named list (shopping, parts, to-do…); created as needed.",
                    {"list": "list name", "items": "items"}),
    "show_list": ("Show a list, or all list names if empty.", {"list": "list name"}),
    "check_off": ("Tick off (or remove) list items.", {"list": "list name", "items": "items or words",
                                                       "remove": "delete instead of ticking"}),
    "web_search": ("Search the web for current info (docs, prices, news, parts, errors).",
                   {"query": "query", "count": "1-10, default 6"}),
    "fetch_page": ("Read a public web page or PDF as text.", {"url": "http(s) URL", "find": "optional words to jump to",
                                                               "max_chars": "default 12000"}),
    "github_repos": ("List the user's GitHub repositories.", {}),
    "github_read": ("Read a file or list a folder on GitHub.", {"repo": "owner/name (optional)", "path": "path; empty = root",
                                                                "ref": "branch/tag/commit (optional)",
                                                                "start_line": "for long files"}),
    "github_search": ("Search code in a GitHub repo.", {"repo": "owner/name (optional)", "query": "what to find"}),
    "github_history": ("Recent commits, optionally of one file.", {"repo": "owner/name (optional)", "path": "optional",
                                                                   "count": "max 30"}),
    "github_issues": ("Issues and pull requests.", {"repo": "owner/name (optional)", "state": "open, closed or all"}),
    "github_builds": ("Recent GitHub Actions builds with the latest failure's error log.", {"repo": "owner/name (optional)"}),
    "github_commit": ("Commit full file contents to a new or existing branch (never the default branch). Needs the user's "
                      "approval. Read files first.", {"repo": "owner/name (optional)", "branch": "e.g. stella/fix-build",
                                                     "message": "commit message", "files": "files to create or replace",
                                                     "delete": "paths to delete (optional)"}),
    "github_pull_request": ("Open a pull request from a branch. Needs the user's approval.",
                            {"repo": "owner/name (optional)", "branch": "branch", "title": "title", "body": "description"}),
}


def _compact_tools():
    for name, (desc, params) in COMPACT_TOOLS.items():
        if name not in SERVER_TOOLS:
            continue
        fn = SERVER_TOOLS[name][1]["function"]
        fn["description"] = desc
        for key, prop in fn["parameters"]["properties"].items():
            if key in params:
                if params[key]:
                    prop["description"] = params[key]
                else:
                    prop.pop("description", None)
            if prop.get("type") == "array" and isinstance(prop.get("items"), dict):
                prop["items"].pop("description", None)


_compact_tools()



# ---- Searching past conversations ---------------------------------------------------
class ChatSearch:
    """Finds earlier conversations by keywords and by meaning. Conversations are cut into small pieces
    (a few messages each) and indexed in the background after they change."""

    def __init__(self):
        self.version = 0
        self.cache = None   # (version, docs, df, avgdl, keys, matrix)
        self.lock = threading.Lock()
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        with CHATS.lock:
            if not CHATS.db.execute("SELECT 1 FROM chat_chunks LIMIT 1").fetchone():   # first run: index everything
                CHATS.db.execute("INSERT OR IGNORE INTO chat_dirty SELECT id FROM conversations WHERE deleted=0")
                CHATS.db.commit()
        threading.Thread(target=self._loop, daemon=True, name="chat-index").start()

    def _loop(self):
        time.sleep(10)
        while True:
            try:
                self.index_dirty()
            except Exception as e:   # never stop indexing for one bad chat
                log(f"chat search: indexing problem ({e})")
            time.sleep(45)

    @staticmethod
    def _pieces(rows):
        """Groups messages into pieces of up to ~1200 characters (a question with its answer, usually)."""
        pieces, cur, ts = [], [], 0
        for role, text, created in rows:
            text = re.sub(r"!\[[^\]]*\]\((?:image|diagram):[0-9a-f]+\)", "[picture]", text or "").strip()
            if not text:
                continue
            who = {"user": "User", "assistant": "Stella", "tool": "Action"}.get(role, role)
            line = f"{who}: {text[:1500]}"
            if cur and (sum(len(x) for x in cur) + len(line) > 1200 or role == "user" and len(cur) >= 4):
                pieces.append(("\n".join(cur), ts))
                cur = []
            if not cur:
                ts = created
            cur.append(line)
        if cur:
            pieces.append(("\n".join(cur), ts))
        return pieces

    def index_dirty(self, limit=50):
        with CHATS.lock:
            dirty = [r[0] for r in CHATS.db.execute("SELECT conv_id FROM chat_dirty LIMIT ?", (limit,))]
        if not dirty:
            return
        for cid in dirty:
            with CHATS.lock:
                alive = CHATS.db.execute("SELECT 1 FROM conversations WHERE id=? AND deleted=0", (cid,)).fetchone()
                rows = CHATS.db.execute(
                    "SELECT role, text, created_at FROM messages WHERE conv_id=? AND deleted=0 AND role IN "
                    "('user','assistant','tool') ORDER BY created_at, id", (cid,)).fetchall() if alive else []
            pieces = self._pieces(rows)
            vecs = [None] * len(pieces)
            if pieces and EMBEDDER.url:
                try:
                    vecs = EMBEDDER.embed([t for t, _ in pieces], EMBEDDER.DOC)
                except (urllib.error.URLError, OSError, ValueError, KeyError):
                    vecs = [None] * len(pieces)   # keywords now; meaning added on a later pass
            with CHATS.lock:
                CHATS.db.execute("DELETE FROM chat_chunks WHERE conv_id=?", (cid,))
                CHATS.db.executemany("INSERT INTO chat_chunks VALUES(?,?,?,?,?,?)",
                                     [(f"{cid}:{i}", cid, i, t, ts, v) for i, ((t, ts), v) in enumerate(zip(pieces, vecs))])
                if EMBEDDER.url and pieces and vecs[0] is None:
                    pass   # stay dirty so meaning-vectors get added when the search model is up
                else:
                    CHATS.db.execute("DELETE FROM chat_dirty WHERE conv_id=?", (cid,))
                CHATS.db.commit()
        self.version += 1

    def _load(self):
        import math as _m
        from collections import Counter
        with self.lock:
            if self.cache and self.cache[0] == self.version:
                return self.cache
            with CHATS.lock:
                rows = CHATS.db.execute(
                    "SELECT c.conv_id, c.idx, c.text, c.ts, c.vec, v.title FROM chat_chunks c "
                    "JOIN conversations v ON v.id = c.conv_id AND v.deleted = 0").fetchall()
            docs, df = [], Counter()
            for cid, idx, text, ts, vec, title in rows:
                tf = Counter(_terms(text))
                docs.append({"conv": cid, "idx": idx, "text": text, "ts": ts, "vec": vec, "title": title or "Untitled",
                             "tf": tf, "len": sum(tf.values()) or 1})
                df.update(tf.keys())
            avgdl = (sum(d["len"] for d in docs) / len(docs)) if docs else 1
            self.cache = (self.version, docs, df, avgdl)
            return self.cache

    def search(self, query, k=6, exclude=""):
        import array
        import math as _m
        _, docs, df, avgdl = self._load()
        docs = [d for d in docs if d["conv"] != exclude]
        if not docs or not query.strip():
            return []
        q = set(_terms(query))
        N = len(docs)
        kw = []
        for i, d in enumerate(docs):
            sc = 0.0
            for t in q:
                f = d["tf"].get(t)
                if f:
                    sc += _m.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * d["len"] / avgdl))
            if sc > 0:
                kw.append((sc, i))
        kw.sort(reverse=True)
        ranked = [[i for _, i in kw[:60]]]
        if EMBEDDER.url:
            try:
                qv = array.array("f", EMBEDDER.embed([query], EMBEDDER.QUERY)[0])
                scored = []
                for i, d in enumerate(docs):
                    if d["vec"]:
                        v = array.array("f", d["vec"])
                        scored.append((sum(a * b for a, b in zip(v, qv)), i))
                scored.sort(reverse=True)
                ranked.append([i for sc, i in scored[:60] if sc > 0.35])
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                pass
        fused = {}
        for lst in ranked:
            for r, i in enumerate(lst):
                fused[i] = fused.get(i, 0) + 1.0 / (60 + r)
        best, seen = [], set()
        for i in sorted(fused, key=lambda i: -fused[i]):
            d = docs[i]
            if d["conv"] in seen:
                continue
            seen.add(d["conv"])
            best.append({"conversation_id": d["conv"], "title": d["title"], "when": d["ts"],
                         "date": time.strftime("%Y-%m-%d", time.localtime(d["ts"] / 1000)), "excerpt": d["text"][:900]})
            if len(best) >= k:
                break
        return best


CHAT_SEARCH = ChatSearch()


def tool_search_chats(args):
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("what should I look for?")
    hits = CHAT_SEARCH.search(query, k=5, exclude=str(args.get("current_conversation") or ""))
    return {"results": [{k: h[k] for k in ("title", "date", "excerpt")} for h in hits],
            "note": "Excerpts from the user's earlier chats; mention which chat when you use them."}, \
        f"Searched past chats for “{query}”: {len(hits)} found"


SERVER_TOOLS["search_chats"] = (tool_search_chats, _fn(
    "search_chats", "Search the user's earlier conversations (what was said, decided or made before).",
    {"query": _p("what to look for")}, ["query"]))
TOOL_GROUPS["history"] = ["search_chats"]
TOOL_TRIGGERS["history"] = re.compile(
    r"\b(we|i|you) (talked|discussed|chatted|said|decided|did|made|wrote|figured)\b|last time|earlier chat|"
    r"other chat|previous (chat|conversation)|remember when|did (we|i) ever|what was (that|the)|before\b|"
    r"\bago\b|yesterday|last (week|month|night)", re.I)


# ---- Backups ----------------------------------------------------------------------------
BACKUP = {"dir": None, "config": None, "paths": {}, "last": 0, "error": "", "running": False}


def backup_config():
    try:
        with open(BACKUP["config"]) as f:
            c = json.load(f)
    except (OSError, ValueError, TypeError):
        c = {}
    return {"copy_to": str(c.get("copy_to") or ""), "keep": max(2, min(60, int(c.get("keep") or 14)))}


def save_backup_config(data):
    c = backup_config()
    if "copy_to" in data:
        path = os.path.expanduser(str(data.get("copy_to") or "").strip())
        if path and not os.path.isdir(path):
            raise ValueError(f"folder not found: {path}")
        c["copy_to"] = path
    if "keep" in data:
        c["keep"] = max(2, min(60, int(data["keep"])))
    with open(BACKUP["config"], "w") as f:
        json.dump(c, f)
    return backups_status()


def _backup_files(folder):
    try:
        return sorted(f for f in os.listdir(folder) if f.startswith("stellai-backup-") and f.endswith(".zip"))
    except OSError:
        return []


def backups_status():
    folder = BACKUP["dir"]
    items = []
    for f in reversed(_backup_files(folder)):
        st = os.stat(os.path.join(folder, f))
        items.append({"name": f, "size": st.st_size, "time": int(st.st_mtime * 1000)})
    cfg = backup_config()
    return {"folder": folder, "copy_to": cfg["copy_to"], "keep": cfg["keep"], "backups": items,
            "last": items[0]["time"] if items else 0, "error": BACKUP["error"], "running": BACKUP["running"]}


def backup_now():
    """Zips chats (consistent SQLite snapshot), memories, projects, photos, diagrams and wake words."""
    import zipfile
    if BACKUP["running"]:
        raise RuntimeError("a backup is already running")
    BACKUP["running"] = True
    try:
        folder = BACKUP["dir"]
        os.makedirs(folder, exist_ok=True)
        name = time.strftime("stellai-backup-%Y-%m-%d_%H%M.zip")
        dest = os.path.join(folder, name)
        with tempfile.TemporaryDirectory() as tmp:
            snap = os.path.join(tmp, "chats.db")
            dst = sqlite3.connect(snap)
            with CHATS.lock:
                CHATS.db.backup(dst)
            dst.close()
            with zipfile.ZipFile(dest + ".part", "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, "chats.db")
                for label, path in BACKUP["paths"].items():
                    if not path or not os.path.isdir(path):
                        continue
                    for root, _, files in os.walk(path):
                        for f in files:
                            full = os.path.join(root, f)
                            z.write(full, os.path.join(label, os.path.relpath(full, path)))
                gh = github_config()
                gh.pop("token", None)   # never put the GitHub token in a backup
                z.writestr("github.json", json.dumps(gh))
                z.writestr("backup-info.json", json.dumps({
                    "made": time.strftime("%Y-%m-%d %H:%M"), "version": 1,
                    "chats": CHATS.db.execute("SELECT COUNT(*) FROM conversations WHERE deleted=0").fetchone()[0]}))
        os.replace(dest + ".part", dest)
        cfg = backup_config()
        for f in _backup_files(folder)[:-cfg["keep"]]:
            os.remove(os.path.join(folder, f))
        if cfg["copy_to"]:
            try:
                import shutil
                shutil.copy2(dest, os.path.join(cfg["copy_to"], name))
                for f in _backup_files(cfg["copy_to"])[:-cfg["keep"]]:
                    os.remove(os.path.join(cfg["copy_to"], f))
            except OSError as e:
                raise RuntimeError(f"backup saved here, but copying to {cfg['copy_to']} failed: {e}")
        BACKUP["error"] = ""
        log(f"backup saved: {name} ({os.path.getsize(dest) // 1024} KB)")
        return dest
    except Exception as e:
        BACKUP["error"] = str(e)
        raise
    finally:
        BACKUP["running"] = False


def backup_loop():
    """Nightly backup (between 2 and 5 am when Stella is idle; at the latest after 36 hours)."""
    time.sleep(120)
    while True:
        try:
            last = backups_status()["last"]
            age_h = (time.time() * 1000 - last) / 3600_000 if last else 999
            hour = time.localtime().tm_hour
            if age_h > 36 or (age_h > 20 and 2 <= hour < 5 and server_idle(300)):
                backup_now()
        except Exception as e:
            log(f"backup failed: {e}")
        time.sleep(1800)


def restore_backup(zip_path, cfg):
    """Puts a backup back (run while the server is stopped). The current data is kept as *.before-restore."""
    import shutil
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "chats.db" not in names:
            raise ValueError("that isn't a StellAI backup")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        had_db = os.path.exists(cfg.db)
        targets = {"projects": cfg.project_dir or os.path.join(os.path.dirname(os.path.abspath(cfg.db)), "projects"),
                   "images": cfg.image_dir, "diagrams": cfg.diagram_dir, "wakewords": cfg.wakeword_dir}
        if os.path.exists(cfg.db):
            os.replace(cfg.db, f"{cfg.db}.before-restore-{stamp}")
            for ext in ("-wal", "-shm"):
                if os.path.exists(cfg.db + ext):
                    os.remove(cfg.db + ext)
        with z.open("chats.db") as src, open(cfg.db, "wb") as out:
            shutil.copyfileobj(src, out)
        for label, target in targets.items():
            target = os.path.abspath(target)
            for n in names:
                if not n.startswith(label + "/") or n.endswith("/"):
                    continue
                rel = n[len(label) + 1:]
                dest = os.path.realpath(os.path.join(target, rel))
                if not dest.startswith(os.path.realpath(target) + os.sep):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    return f"restored {os.path.basename(zip_path)}" + (
        f"; the previous chat database was kept as {cfg.db}.before-restore-{stamp}" if had_db else "")


# ---- Server status and switching models ---------------------------------------------
MODEL_CATALOG = [
    {"key": "4b", "label": "Qwen3.5 4B", "repo": "unsloth/Qwen3.5-4B-GGUF", "about": "Fast; fine for everyday help."},
    {"key": "9b", "label": "Qwen3.5 9B", "repo": "unsloth/Qwen3.5-9B-GGUF", "about": "Smarter at code and reasoning."},
    {"key": "35b", "label": "Qwen3.5 35B-A3B", "repo": "unsloth/Qwen3.5-35B-A3B-GGUF",
     "about": "The strongest; needs about 20 GB RAM plus a GPU."},
]


def detect_memory():
    """(ram_gb, vram_gb) of this computer."""
    ram = 0
    try:
        if os.name == "nt":
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("x", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in "abcdefg"]
            m = M()
            m.l = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            ram = round(m.a / 2**30)
        else:
            with open("/proc/meminfo") as f:
                ram = round(int(re.search(r"MemTotal:\s+(\d+)", f.read()).group(1)) / 2**20)
    except (OSError, AttributeError, ValueError):
        pass
    vram = 0
    try:
        import shutil
        smi = shutil.which("nvidia-smi")
        if smi:
            out = subprocess.run([smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"], capture_output=True,
                                 text=True, timeout=10, creationflags=0x08000000 if os.name == "nt" else 0).stdout
            vram = round(int(out.split()[0]) / 1024) if out.strip() else 0
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return ram, vram


def model_file_for(key, ram, vram):
    """Same choices as the setup: the best version that fits this computer."""
    if key == "4b":
        return "Qwen3.5-4B-Q4_K_M.gguf", 2.7, 8192
    if key == "9b":
        if 5 <= vram < 8:
            return "Qwen3.5-9B-Q3_K_M.gguf", 4.6, 8192
        return "Qwen3.5-9B-Q4_K_M.gguf", 5.7, 8192
    total = ram + vram
    ctx = 32768 if vram >= 5 else 16384
    if total >= 40:
        return "Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf", 22.2, ctx
    if total >= 30:
        return "Qwen3.5-35B-A3B-UD-IQ4_XS.gguf", 17.5, ctx
    return "Qwen3.5-35B-A3B-UD-IQ3_S.gguf", 13.6, ctx


class LinuxServerControl:
    """Model switching for the Linux launcher: it re-reads model.conf whenever llama-server stops and a
    .switch-model file is present."""

    def __init__(self, conf, models_dir, pid_file):
        self.conf, self.models_dir, self.pid_file = conf, models_dir, pid_file
        self.download = {}   # key -> {"done", "total", "error", "state"}
        self.started = time.time()

    def _read_conf(self):
        out = {}
        try:
            with open(self.conf) as f:
                for line in f:
                    m = re.match(r'\s*([A-Z_]+)="?(.*?)"?\s*$', line)
                    if m:
                        out[m.group(1)] = m.group(2)
        except OSError:
            pass
        return out

    def catalog(self):
        ram, vram = detect_memory()
        conf = self._read_conf()
        current = os.path.basename(conf.get("MODEL_FILE", ""))
        out = []
        for m in MODEL_CATALOG:
            fname, gb, ctx = model_file_for(m["key"], ram, vram)
            path = os.path.join(self.models_dir, fname)
            out.append({**{k: m[k] for k in ("key", "label", "about")}, "file": fname, "size_gb": gb, "context": ctx,
                        "installed": os.path.isfile(path) and os.path.getsize(path) > 100_000_000,
                        "current": fname == current, "download": self.download.get(m["key"])})
        return out

    def status(self):
        conf = self._read_conf()
        return {"model": conf.get("MODEL_LABEL", ""), "file": os.path.basename(conf.get("MODEL_FILE", "")),
                "backend": conf.get("BACKEND", ""), "can_switch": True}

    def switch(self, key):
        entry = next((m for m in self.catalog() if m["key"] == key), None)
        if not entry:
            raise ValueError("unknown model")
        if not entry["installed"]:
            raise ValueError("download it first")
        conf = self._read_conf()
        mmproj = ""
        if conf.get("MMPROJ_FILE") or conf.get("VISION", "") == "1":
            cand = os.path.join(self.models_dir, entry["file"].replace(".gguf", ".mmproj-F16.gguf"))
            mmproj = cand if os.path.isfile(cand) else ""
        conf.update(MODEL_LABEL=entry["label"], MODEL_FILE=os.path.join(self.models_dir, entry["file"]),
                    NET_CONTEXT=str(entry["context"]), MMPROJ_FILE=mmproj)
        with open(self.conf + ".tmp", "w") as f:
            for k, v in conf.items():
                f.write(f'{k}="{v}"\n')
        os.replace(self.conf + ".tmp", self.conf)
        open(os.path.join(os.path.dirname(self.conf), ".switch-model"), "w").close()
        try:
            with open(self.pid_file) as f:
                os.kill(int(f.read().strip()), 15)
        except (OSError, ValueError):
            raise RuntimeError("couldn't restart the model server; restart StellAI Server to use the new model")
        return f"switching to {entry['label']}; it takes a minute to load"

    def start_download(self, key):
        entry = next((m for m in self.catalog() if m["key"] == key), None)
        if not entry:
            raise ValueError("unknown model")
        repo = next(m["repo"] for m in MODEL_CATALOG if m["key"] == key)
        state = {"state": "downloading", "done": 0, "total": 0, "error": ""}
        self.download[key] = state

        def work():
            files = [(f"https://huggingface.co/{repo}/resolve/main/{entry['file']}", entry["file"])]
            if self._read_conf().get("MMPROJ_FILE"):
                files.append((f"https://huggingface.co/{repo}/resolve/main/mmproj-F16.gguf",
                              entry["file"].replace(".gguf", ".mmproj-F16.gguf")))
            try:
                for url, name in files:
                    _download_file(url, os.path.join(self.models_dir, name), state)
                state["state"] = "ready"
            except Exception as e:
                state.update(state="failed", error=str(e))
        threading.Thread(target=work, daemon=True).start()
        return "download started"


def _download_file(url, dest, state):
    """Resumable download with progress in `state` (used for model downloads from the app)."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "StellAI", **({"Range": f"bytes={have}-"} if have else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:
            have = 0
        state["total"] = have + int(r.headers.get("Content-Length") or 0)
        with open(part, "ab" if have else "wb") as f:
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                have += len(buf)
                state["done"] = have
    os.replace(part, dest)


SERVER_CONTROL = None   # LinuxServerControl, or the Windows app's own controller
STARTED_AT = time.time()


def server_status():
    st = {"uptime_s": int(time.time() - STARTED_AT), "model": "", "context": 0, "loaded": False, "can_switch": False,
          "models": []}
    try:
        props = _llama("/props", timeout=5)
        st["context"] = int((props.get("default_generation_settings") or {}).get("n_ctx") or 0)
        st["model_path"] = os.path.basename(str(props.get("model_path") or ""))
        st["loaded"] = True
    except (urllib.error.URLError, OSError, ValueError):
        pass
    if SERVER_CONTROL:
        try:
            st.update(SERVER_CONTROL.status())
            st["models"] = SERVER_CONTROL.catalog()
        except Exception as e:
            st["error"] = str(e)
    ram, vram = detect_memory()
    st.update(ram_gb=ram, vram_gb=vram)
    return st


# ---- Health check ("doctor") --------------------------------------------------------------
DOCTOR = {"whisper": "", "embed": "", "update": ""}


def _probe(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError) as e:
        return 0, str(getattr(e, "reason", e))


def run_doctor():
    """Checks every part of StellAI and says what to do about problems."""
    import shutil
    checks = []

    def add(name, state, detail, fix=""):
        checks.append({"name": name, "state": state, "detail": detail, "fix": fix})

    code, err = _probe(LLAMA_URL.rstrip("/") + "/health")
    if code == 200:
        try:
            props = _llama("/props", timeout=5)
            n = (props.get("default_generation_settings") or {}).get("n_ctx")
            add("AI model", "ok", f"{os.path.basename(str(props.get('model_path') or 'loaded'))}, {n} tokens of context")
        except (urllib.error.URLError, OSError, ValueError):
            add("AI model", "ok", "running")
    elif code == 503:
        add("AI model", "warn", "still loading", "Wait a minute; big models take a while to load.")
    else:
        add("AI model", "fail", f"not reachable ({err or code})", "Start StellAI Server; if it stops right away, check llama.log / the Activity box.")
    if DOCTOR["whisper"]:
        code, err = _probe(DOCTOR["whisper"].rstrip("/") + "/health")
        add("Voice recognition", "ok" if code in (200, 404) else "fail",
            "running" if code in (200, 404) else f"not reachable ({err or code})",
            "" if code in (200, 404) else "Voice commands won't work. Re-run setup to install Whisper.")
    if EMBEDDER.url:
        code, err = _probe(EMBEDDER.url + "/health")
        add("Meaning search", "ok" if code == 200 else "warn", "running" if code == 200 else "not running; keyword search only",
            "" if code == 200 else "Re-run setup to download the search model.")
    else:
        add("Meaning search", "warn", "not set up; keyword search only", "Re-run setup (it downloads a 140 MB search model).")
    if SEARXNG_URL:
        code, err = _probe(SEARXNG_URL.rstrip("/") + "/healthz")
        add("Web search", "ok" if code == 200 else "warn", "SearXNG running" if code == 200 else "SearXNG not answering; using DuckDuckGo",
            "" if code == 200 else "Start Docker (or re-run the SearXNG step of setup).")
    else:
        add("Web search", "ok", "DuckDuckGo (SearXNG not set up)")
    try:
        with CHATS.lock:
            ok = CHATS.db.execute("PRAGMA quick_check").fetchone()[0]
            n = CHATS.db.execute("SELECT COUNT(*) FROM conversations WHERE deleted=0").fetchone()[0]
            mem = CHATS.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            pend = CHATS.db.execute("SELECT COUNT(*) FROM chat_dirty").fetchone()[0]
        add("Chat database", "ok" if ok == "ok" else "fail", f"{n} chats, {mem} memories" + (f", {pend} chats waiting to be indexed" if pend else ""),
            "" if ok == "ok" else "Restore yesterday's backup (see Backups).")
    except sqlite3.Error as e:
        add("Chat database", "fail", str(e), "Restore a backup.")
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(CHATS_PATH))).free / 2**30
    add("Disk space", "ok" if free > 10 else "warn" if free > 3 else "fail", f"{free:.1f} GB free",
        "" if free > 10 else "Free up space; backups and downloads need room.")
    st = backups_status()
    age = (time.time() * 1000 - st["last"]) / 86400_000 if st["last"] else None
    if st["error"]:
        add("Backups", "fail", st["error"], "Check the backup folder in Server → Backups.")
    elif age is None:
        add("Backups", "warn", "none yet", "One is made automatically tonight; or tap Back up now.")
    else:
        add("Backups", "ok" if age < 2 else "warn", f"last {age * 24:.0f} hours ago, {len(st['backups'])} kept" +
            (f", copied to {st['copy_to']}" if st["copy_to"] else ""), "" if age < 2 else "Tap Back up now.")
    try:
        import pypdf  # noqa: F401
        add("PDF reading", "ok", "pypdf")
    except ImportError:
        add("PDF reading", "ok" if shutil.which("pdftotext") else "warn",
            "pdftotext" if shutil.which("pdftotext") else "no PDF reader", "" if shutil.which("pdftotext") else "pip install pypdf")
    add("Mermaid diagrams", "ok" if mermaid_command() else "warn", "installed" if mermaid_command() else "not installed (flowcharts use schemdraw)")
    gh = github_status()
    add("GitHub", "ok" if gh.get("user") else "warn" if gh.get("token_set") else "ok",
        f"connected as {gh['user']}" if gh.get("user") else gh.get("error") or "no token (public repos only)",
        "Check the token in Settings → GitHub." if gh.get("token_set") and not gh.get("user") else "")
    ts = shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe" if os.name == "nt" else "")
    ip = ""
    if ts and os.path.exists(ts):
        try:
            ip = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=5,
                                creationflags=0x08000000 if os.name == "nt" else 0).stdout.strip().splitlines()[0]
        except (OSError, IndexError, subprocess.SubprocessError):
            ip = ""
    add("Tailscale", "ok" if ip else "warn", ip or "not connected", "" if ip else "Install/sign in to use Stella away from home.")
    if DOCTOR["update"]:
        add("Updates", "ok", DOCTOR["update"])
    worst = "fail" if any(c["state"] == "fail" for c in checks) else "warn" if any(c["state"] == "warn" for c in checks) else "ok"
    return {"state": worst, "checks": checks, "time": int(time.time() * 1000)}


def doctor_text(report):
    icon = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = ["StellAI health check — " + {"ok": "all good", "warn": "working, with warnings", "fail": "problems found"}[report["state"]], ""]
    for c in report["checks"]:
        lines.append(f"  {icon[c['state']]} {c['name']}: {c['detail']}")
        if c["fix"] and c["state"] != "ok":
            lines.append(f"      → {c['fix']}")
    return "\n".join(lines) + "\n"


IMAGE_DIR = None
WAKEWORD_DIR = None
WAKEWORD_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,59}")


def shrink_result(result, max_chars):
    """Shortens the longest text fields of a tool result until it fits in max_chars (the space left in context)."""
    max_chars = max(1200, max_chars)
    for _ in range(300):
        size = len(json.dumps(result))
        if size <= max_chars:
            break
        strings = []

        def walk(o, path):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, path + [k])
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, path + [i])
            elif isinstance(o, str) and len(o) > 200:
                strings.append((len(o), path))
        walk(result, [])
        if not strings:
            # drop list items from the end (e.g. too many search results)
            lists = [v for v in result.values() if isinstance(v, list) and len(v) > 1]
            if not lists:
                break
            max(lists, key=len).pop()
            continue
        length, path = max(strings)
        target = max(200, length - (size - max_chars) - 40)
        o = result
        for k in path[:-1]:
            o = o[k]
        o[path[-1]] = o[path[-1]][:target] + " […shortened to fit Stella's memory]"
        result["truncated"] = True
    return result


def run_server_tool(name, args):
    entry = SERVER_TOOLS.get(name)
    if not entry:
        return {"ok": False, "error": f"unknown tool {name}"}, f"Unknown tool {name}"
    args = dict(args) if isinstance(args, dict) else {}
    max_chars = args.pop("_max_chars", None)
    try:
        result, summary = entry[0](args)
        result = {"ok": True, **result}
        if max_chars:
            result = shrink_result(result, int(max_chars))
        return result, summary
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out"}, "Timed out"
    except (ValueError, KeyError, TypeError, ZeroDivisionError, OverflowError, SyntaxError, RuntimeError,
            json.JSONDecodeError, OSError) as e:
        msg = str(e) or type(e).__name__
        if isinstance(e, KeyError):
            msg = f"missing value: {e.args[0]}"
        if isinstance(e, ZeroDivisionError):
            msg = "division by zero"
        return {"ok": False, "error": msg}, msg


# ----------------------------------------------------------------------------- activity + updates
ACTIVITY = {"active": 0, "last": time.time(), "lock": threading.Lock()}
WEB = None            # the running HTTP server, set in start()
RESTART_CODE = 75     # exit code that tells the launcher "start me again" (after an update)
EXIT_CODE = 0


def _tracked(fn):
    """Counts requests in progress, so updates wait until nobody is using Stella."""
    def wrapper(self):
        if urlparse(self.path).path == "/health":
            return fn(self)
        with ACTIVITY["lock"]:
            ACTIVITY["active"] += 1
        try:
            return fn(self)
        finally:
            with ACTIVITY["lock"]:
                ACTIVITY["active"] -= 1
                ACTIVITY["last"] = time.time()
    return wrapper


def llama_busy():
    """True while llama-server is generating (phones talk to it directly, not through the hub)."""
    try:
        slots = _llama("/slots", timeout=5)
        return any(s.get("is_processing") for s in slots) if isinstance(slots, list) else False
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False


def server_idle(quiet=120):
    with ACTIVITY["lock"]:
        busy = ACTIVITY["active"] > 0 or time.time() - ACTIVITY["last"] < quiet
    return not busy and not llama_busy()


def wait_until_idle(max_wait=6 * 3600, quiet=120):
    waited, step = 0, min(30, max(1, quiet))
    while not server_idle(quiet):
        time.sleep(step)
        waited += step
        if waited >= max_wait:
            return False
    return True


def _fetch(url, token="", timeout=60):
    headers = {"User-Agent": "StellAI-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def self_test():
    """Quick check that this file works, run on a downloaded update before it replaces the old one."""
    with tempfile.TemporaryDirectory() as tmp:
        db = ChatDB(os.path.join(tmp, "t.db"))
        db.sync(0, [{"id": "c", "title": "t", "created_at": 1, "updated_at": 1}], [])
        p = db.save_project({"name": "Test"})
        db.add_project_note(p["id"], "note")
        assert db.save_mode({"name": "Test mode", "temperature": 0.7})["id"].startswith("c-")
        r = db.add_reminder("test", int(time.time() * 1000) + 60_000, "daily")
        db.update_reminder(r["id"], done=True)
        db.add_items("Shopping list", ["flux"])
        assert db.lists()["shopping"][0]["text"] == "flux"
        assert "web_search" in SERVER_TOOLS and "github_commit" in APPROVAL_TOOLS
        assert ChatSearch._pieces([("user", "hi", 1), ("assistant", "hello", 2)])
        assert model_file_for("35b", 20, 6)[0].endswith("IQ3_S.gguf")
        assert db.project(p["id"])["notes"]
        assert chunk_text("a\n" * 3000)
        assert "render_diagram" in SERVER_TOOLS
        db.db.close()
    print("self-test ok")


RAW_BASE = os.environ.get("STELLAI_UPDATE_BASE", "https://raw.githubusercontent.com")


def check_for_update(repo, token="", path="server/stellai_hub.py"):
    """Downloads the hub from GitHub; if it differs from this file and passes its self-test, installs it.

    Returns True when a new version was installed (the caller restarts)."""
    import hashlib
    me = os.path.abspath(sys.argv[0] if sys.argv[0].endswith(".py") else __file__)
    new = _fetch(f"{RAW_BASE}/{repo}/HEAD/{path}", token)
    with open(me, "rb") as f:
        cur = f.read()
    digest = hashlib.sha256(new).hexdigest()
    if digest == hashlib.sha256(cur).hexdigest():
        return False
    try:
        with open(os.path.join(os.path.dirname(me), "stellai_hub.bad")) as f:
            if digest in f.read().split():
                return False   # this version already failed to start here; wait for a newer one
    except OSError:
        pass
    compile(new, "stellai_hub.py", "exec")                       # must at least be valid Python
    staged = me + ".new"
    with open(staged, "wb") as f:
        f.write(new)
    p = subprocess.run([sys.executable, staged, "--self-test"], capture_output=True, text=True, timeout=120)
    if p.returncode != 0 or "self-test ok" not in p.stdout:
        os.remove(staged)
        detail = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"the new version failed its self-test ({detail[0][:200]}); keeping this one")
    log("update downloaded and checked; installing when Stella is idle")
    if not wait_until_idle(quiet=IDLE_QUIET):
        os.remove(staged)
        return False
    import shutil
    shutil.copy2(me, me + ".bak")                               # the launcher restores this if the update fails
    os.replace(staged, me)
    return True


UPDATE_INTERVAL = int(os.environ.get("STELLAI_UPDATE_INTERVAL", "1800"))   # 30 minutes
IDLE_QUIET = int(os.environ.get("STELLAI_IDLE_QUIET", "120"))             # no requests for 2 minutes


def updater_loop(repo, token="", interval=None):
    interval = interval or UPDATE_INTERVAL
    time.sleep(min(90, interval))
    while True:
        try:
            if check_for_update(repo, token):
                log("installed an update from GitHub; restarting the hub (chats and settings are kept)")
                global EXIT_CODE
                EXIT_CODE = RESTART_CODE
                if WEB:
                    WEB.shutdown()
                return
        except Exception as e:  # never let a failed check stop the hub
            log(f"update check failed: {e}")
        time.sleep(interval)


OPTIONAL_PACKAGES = {"pypdf": "pypdf"}   # import name -> pip package; installed into the hub's venv if missing


def ensure_packages():
    import importlib.util
    if getattr(sys, "frozen", False) or sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        return   # only inside our own virtualenv
    for mod, pkg in OPTIONAL_PACKAGES.items():
        if importlib.util.find_spec(mod) is None:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", pkg], capture_output=True, timeout=600)
            log(f"installed {pkg}" if r.returncode == 0 else f"couldn't install {pkg}")


# ----------------------------------------------------------------------------- desktop web chat
# The page is kept in server/webchat.html and copied here by server/embed_webchat.py.
WEBCHAT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StellAI</title>
<style>
:root{--void:#04060d;--top:#070b1a;--panel:#0c1326;--panel2:#141d38;--line:#26335c;--text:#e6ecff;--muted:#8a96bf;
--accent:#5ce1e6;--glow:#9b7bff;--gold:#ffd166;--red:#ff6b7a;--mono:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--void);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
body{background:radial-gradient(ellipse at 20% 0%,#0b1430 0%,var(--top) 45%,#0b0620 100%) fixed}
button{font:inherit;color:inherit;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--accent)}button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);color:#0a0a14;border-color:var(--accent);font-weight:600}
button.ghost{background:none;border-color:transparent}button.ghost:hover{border-color:var(--line)}
input,textarea,select{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 10px;width:100%}
textarea{resize:vertical}input:focus,textarea:focus{outline:none;border-color:var(--accent)}
a{color:var(--accent)}
.hud{font-family:var(--mono);letter-spacing:2px;text-transform:uppercase;font-size:11px;color:var(--muted)}
#app{display:flex;height:100vh}
#side{width:290px;flex:none;background:rgba(12,19,38,.92);border-right:1px solid var(--line);display:flex;flex-direction:column}
#side header{padding:18px 18px 10px;color:var(--accent);font-family:var(--mono);letter-spacing:3px;font-weight:700}
#side .scroll{flex:1;overflow:auto;padding:0 10px 10px}
.item{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:10px;cursor:pointer;color:var(--text)}
.item:hover{background:rgba(255,255,255,.04)}.item.sel{background:rgba(92,225,230,.12);color:var(--accent)}
.item .t{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .x{opacity:0;border:none;background:none;padding:0 4px;color:var(--muted)}.item:hover .x{opacity:1}
.sec{display:flex;align-items:center;justify-content:space-between;padding:12px 10px 4px}
.sub{font-size:11px;color:var(--muted)}
#side footer{border-top:1px solid var(--line);padding:8px 12px;display:flex;gap:6px;flex-wrap:wrap}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#bar{display:flex;align-items:center;gap:10px;padding:10px 16px;background:rgba(4,6,13,.55);border-bottom:1px solid rgba(92,225,230,.15)}
#bar .title{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip{border:1px solid rgba(92,225,230,.45);border-radius:999px;padding:3px 10px;font-family:var(--mono);font-size:11px;letter-spacing:1px;color:var(--accent);cursor:pointer;background:none;white-space:nowrap}
#meter{display:flex;align-items:center;gap:6px;cursor:pointer}
#meter .track{width:70px;height:4px;background:var(--line);border-radius:2px;overflow:hidden}#meter .fill{height:100%;background:var(--accent)}
#msgs{flex:1;overflow:auto;padding:18px 16px 30px}
.wrap{max-width:860px;margin:0 auto;display:flex;flex-direction:column;gap:12px}
.msg{position:relative;max-width:85%;padding:10px 14px;border-radius:18px}
.msg.user{align-self:flex-end;background:linear-gradient(135deg,rgba(92,225,230,.28),rgba(155,123,255,.28));border:1px solid rgba(92,225,230,.45);border-bottom-right-radius:4px;white-space:pre-wrap}
.msg.assistant{align-self:flex-start;background:rgba(12,19,38,.85);border:1px solid rgba(92,225,230,.22);border-bottom-left-radius:4px}
.msg.assistant.wide{max-width:100%;width:100%}
.msg .who{font-family:var(--mono);font-size:10px;letter-spacing:2px;color:var(--accent);opacity:.8;margin-bottom:4px}
.msg .acts{position:absolute;top:-12px;right:8px;display:none;gap:4px}.msg:hover .acts{display:flex}
.msg .acts button{padding:1px 8px;font-size:12px;border-radius:8px}
.tool{font-family:var(--mono);font-size:13px;color:var(--gold);padding:0 10px}
.err{background:rgba(255,107,122,.12);border:1px solid rgba(255,107,122,.5);border-radius:12px;padding:10px 12px;color:#ffc7cd}
.summary{border:1px solid rgba(155,123,255,.4);background:rgba(155,123,255,.08);border-radius:12px;padding:10px 14px;cursor:pointer}
.summary .hud{color:var(--glow)}
.think{font-family:var(--mono);font-size:12px;color:var(--glow);font-style:italic;white-space:pre-wrap;cursor:pointer;margin-bottom:6px}
.md p{margin:.35em 0}.md h1,.md h2,.md h3{margin:.6em 0 .3em;font-size:1.05em}.md ul,.md ol{margin:.3em 0;padding-left:1.4em}
.md code{font-family:var(--mono);background:rgba(255,255,255,.07);padding:1px 5px;border-radius:5px;font-size:.92em}
.md pre{background:#060a16;border:1px solid var(--line);border-radius:10px;padding:10px 12px;overflow:auto;position:relative}
.md pre code{background:none;padding:0;font-size:13px;line-height:1.45}
.md pre .cp{position:absolute;top:6px;right:6px;font-size:11px;padding:1px 8px}
.md img{max-width:100%;background:#fff;border-radius:8px;margin:6px 0;cursor:zoom-in}
.md blockquote{border-left:3px solid var(--line);margin:.4em 0;padding-left:10px;color:var(--muted)}
.md table{border-collapse:collapse}.md td,.md th{border:1px solid var(--line);padding:3px 8px}
#composer{border-top:1px solid rgba(92,225,230,.18);background:rgba(4,6,13,.7);padding:10px 16px}
#composer .row{max-width:860px;margin:0 auto;display:flex;gap:8px;align-items:flex-end}
#input{min-height:46px;max-height:240px;border-radius:22px;padding:11px 16px}
#atts{max-width:860px;margin:0 auto 6px;display:flex;gap:8px;flex-wrap:wrap}
#atts .a{position:relative}#atts img{height:60px;border-radius:8px;border:1px solid var(--line)}
#atts .a button{position:absolute;top:-6px;right:-6px;padding:0 6px;border-radius:50%;font-size:11px}
#notice{max-width:860px;margin:0 auto 6px;font-size:13px;color:var(--gold);display:none;cursor:pointer}
.empty{margin:auto;text-align:center;color:var(--muted);padding:60px 20px}.empty .star{font-size:44px;color:var(--accent)}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:10;padding:16px}
.dialog{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px 20px;width:min(640px,100%);max-height:88vh;overflow:auto}
.dialog h2{margin:0 0 10px;font-size:18px}.dialog .btns{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}
.list .row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(38,51,92,.6)}
.list .row .t{flex:1;min-width:0}
.pick{display:flex;gap:10px;align-items:center;padding:9px 8px;border-radius:10px;cursor:pointer}.pick:hover{background:rgba(255,255,255,.04)}
.pick .ic{font-size:20px;width:30px;text-align:center}.pick.sel{color:var(--accent)}
.status{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted)}.status.on{background:var(--accent);box-shadow:0 0 8px var(--accent)}
.hit .ex{font-size:12px;color:var(--muted);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.check{display:flex;gap:10px;padding:5px 0}.check .ic{width:18px;flex:none;font-weight:700}
.bar2{height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin-top:6px}.bar2 div{height:100%;background:var(--accent)}
.zoom{position:fixed;inset:0;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:center;z-index:20;cursor:zoom-out}
.zoom img{max-width:95vw;max-height:95vh;background:#fff;border-radius:8px}
@media (max-width:760px){#side{position:fixed;z-index:5;height:100%;transform:translateX(-100%);transition:transform .2s}#side.open{transform:none}}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <header>✦ STELLAI</header>
    <div style="padding:0 12px 6px"><button class="primary" style="width:100%" id="newChat">+ New chat</button></div>
    <div style="padding:0 12px 6px"><input id="search" type="search" placeholder="🔍 Search past chats" autocomplete="off"></div>
    <div class="scroll" id="searchView" style="display:none"><div id="searchResults"></div></div>
    <div class="scroll" id="listView">
      <div class="sec"><span class="hud">Projects</span><button class="ghost" id="newProject" title="New project">+</button></div>
      <div id="projects"></div>
      <div class="sec"><span class="hud">Chats</span></div>
      <div id="chats"></div>
    </div>
    <footer>
      <button class="ghost" id="openLibrary">📚 Library</button>
      <button class="ghost" id="openTasks">⏰ Reminders</button>
      <button class="ghost" id="openServer">🖥 Server</button>
      <button class="ghost" id="openSettings">⚙ Settings</button>
      <div class="sub" id="syncState" style="width:100%"></div>
    </footer>
  </aside>
  <main id="main">
    <div id="bar">
      <button class="ghost" id="menuBtn" style="display:none">☰</button>
      <span class="status" id="dot"></span>
      <div class="title"><div id="title" class="hud" style="font-size:14px;color:var(--text);letter-spacing:4px">STELLA</div><div id="subtitle" class="sub"></div></div>
      <button class="chip" id="modeChip"></button>
      <button class="chip" id="projectChip" style="display:none"></button>
      <div id="meter" title="How full Stella's memory is for this chat"><span class="hud">Context</span><div class="track"><div class="fill" id="meterFill" style="width:0"></div></div><span class="hud" id="meterPct"></span></div>
    </div>
    <div id="msgs"><div class="wrap" id="wrap"></div></div>
    <div id="composer">
      <div id="notice"></div>
      <div id="atts"></div>
      <div class="row">
        <button class="ghost" id="attach" title="Add a photo">📷</button>
        <input type="file" id="file" accept="image/*" multiple hidden>
        <textarea id="input" rows="1" placeholder="Message Stella… (Enter to send, Shift+Enter for a new line)"></textarea>
        <button class="primary" id="send">Send</button>
      </div>
    </div>
  </main>
</div>
<script>
"use strict";
// ---------------------------------------------------------------- basics
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
  const r = Math.random() * 16 | 0; return (c === "x" ? r : (r & 3 | 8)).toString(16); }));
const hex16 = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, "0")).join("");
let lastTs = 0;
const nextTs = () => { const n = Date.now(); lastTs = n > lastTs ? n : lastTs + 1; return lastTs; };
const store = { get(k, d) { try { const v = localStorage.getItem("stellai." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
                set(k, v) { try { localStorage.setItem("stellai." + k, JSON.stringify(v)); } catch {} } };

// Key from the link the server app opens (#key=...), or saved earlier
(function () {
  const m = location.hash.match(/key=([^&]+)/);
  if (m) { store.set("key", decodeURIComponent(m[1])); history.replaceState(null, "", location.pathname); }
})();
let KEY = store.get("key", "");
const NAME = () => store.get("name", "Stella");

async function api(path, opts = {}) {
  const headers = Object.assign({ Authorization: "Bearer " + KEY }, opts.headers || {});
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  const text = await r.text();
  let data = null; try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!r.ok) { const e = new Error((data && data.error) || ("HTTP " + r.status)); e.status = r.status; throw e; }
  return data;
}
const media = ref => { const [k, id] = ref.split(":"); return (k === "image" ? `/images/${id}.jpg` : `/diagrams/${id}.png`) + "?key=" + encodeURIComponent(KEY); };

// ---------------------------------------------------------------- modes (same as the phone app)
const MODES = [
  { id: "general", name: "General", icon: "✦", desc: "Everyday help and quick answers", prompt: "" },
  { id: "programmer", name: "Programmer", icon: "⌨", desc: "Pair programming: complete code, debugging, reviews", temperature: 0.3, thinking: "on",
    prompt: "Mode: Programmer. Act as a senior software engineer pair-programming with the user.\n- Give complete, working code in fenced blocks with a language tag. For changes to existing code, show the whole changed function or file section, not fragments.\n- Explain briefly why, not just what. Point out bugs, edge cases, performance and security issues you notice.\n- When debugging, reason from the actual error message and code; if they're missing, ask for them instead of guessing.\n- Follow the user's language, framework and code style. Prefer simple, maintainable solutions.\n- Replies can be as long as the code needs." },
  { id: "writer", name: "Writer", icon: "✒", desc: "Creative writing partner for novels and stories", temperature: 0.9, thinking: "off", tools: "minimal", style: "story",
    prompt: "Mode: Writer. You are the user's creative writing partner for fiction: novels, short stories, scenes and worldbuilding.\n- Write vivid, original prose with a strong voice, sensory detail, natural dialogue and good pacing. Show, don't tell.\n- Keep continuity: characters, names, timeline, world rules and established facts. Match the user's genre, tense, point of view and style.\n- When asked to write, write the full passage, not a summary or outline (unless asked). Long replies are welcome.\n- Write story text as plain prose: no headings, bullet points or bold in the story itself.\n- Avoid clichés and purple prose. Give characters distinct voices.\n- Suggest directions or ask questions only at natural decision points or when asked.\n- You don't need to be brief in this mode." },
  { id: "electronics", name: "Electronics", icon: "⚡", desc: "Circuits, PCBs, firmware and calculations", temperature: 0.4,
    prompt: "Mode: Electronics. Act as an experienced electronics engineer.\n- Always use the calculate/electronics tools for maths, and pick real standard part values (E12/E24) and common parts.\n- Draw schematics or timing diagrams with render_diagram when a picture helps.\n- Mention ratings that matter (voltage, current, power dissipation, tolerances) and give pin numbers when relevant.\n- Flag safety issues clearly (mains voltage, Li-ion batteries, high current, heat)." },
  { id: "tutor", name: "Tutor", icon: "🎓", desc: "Learn step by step instead of just getting answers", temperature: 0.5,
    prompt: "Mode: Tutor. Help the user learn and understand, not just get answers.\n- Explain step by step with simple examples and analogies, building on what they already know.\n- For exercises, guide with hints and questions before giving the full solution, unless they ask for it.\n- Check understanding with a short question now and then. Correct misconceptions kindly and clearly." },
  { id: "brainstorm", name: "Brainstorm", icon: "💡", desc: "Lots of ideas, from practical to wild", temperature: 1.0, thinking: "off",
    prompt: "Mode: Brainstorm. Generate many diverse ideas quickly: practical ones and bold, unusual ones.\n- Use short bullet points, one idea each, grouped if that helps. Build on and remix the user's ideas.\n- Don't judge ideas while generating; offer to evaluate or narrow them down afterwards." },
];
const COLORS = { cyan: "#5CE1E6", blue: "#5AA9FF", red: "#FF5C6C", green: "#4DE8A0", purple: "#A77BFF", pink: "#FF7AC6", orange: "#FFA552", yellow: "#FFD84D" };

// ---------------------------------------------------------------- state
const S = {
  convs: new Map(), msgs: new Map(), dirtyC: new Set(), dirtyM: new Map(),
  cursor: 0, serverId: "", current: null, pendingProject: "", pendingMode: "",
  projects: [], customModes: [], memories: [], tools: [], approval: new Set(),
  busy: false, abort: null, attachments: [], context: null, lastSync: 0, syncError: "", sticky: new Map(),
};
const allModes = () => MODES.concat(S.customModes.map(m => ({ ...m, desc: "Your mode", thinking: m.thinking || "default", tools: m.tools || "all" })));
const modeById = id => allModes().find(m => m.id === id) || MODES[0];
const curConv = () => S.current ? S.convs.get(S.current) : null;
const curMode = () => modeById(curConv() ? curConv().mode : S.pendingMode);
const curProjectId = () => curConv() ? (curConv().project_id || "") : S.pendingProject;
const project = id => S.projects.find(p => p.id === id);
const msgsOf = id => (S.msgs.get(id) || []).filter(m => !m.deleted).sort((a, b) => a.created_at - b.created_at || (a.id < b.id ? -1 : 1));

function saveConv(c) { S.convs.set(c.id, c); S.dirtyC.add(c.id); }
function saveMsg(m) {
  if (!S.msgs.has(m.conv_id)) S.msgs.set(m.conv_id, []);
  const list = S.msgs.get(m.conv_id); const i = list.findIndex(x => x.id === m.id);
  if (i >= 0) list[i] = m; else list.push(m);
  S.dirtyM.set(m.id, m);
}

// ---------------------------------------------------------------- sync (same protocol as the phone)
let syncing = null;
async function sync() {
  if (syncing) return syncing;
  syncing = (async () => {
    try {
      for (let page = 0; page < 200; page++) {
        const convs = [...S.dirtyC].map(id => S.convs.get(id)).filter(Boolean);
        const msgs = [...S.dirtyM.values()].slice(0, 1500);
        const resp = await api("/sync", { method: "POST", json: { since: S.cursor, conversations: convs, messages: msgs } });
        if (S.serverId && resp.server_id !== S.serverId) { S.cursor = 0; }
        S.serverId = resp.server_id;
        convs.forEach(c => { if (S.convs.get(c.id) === c) S.dirtyC.delete(c.id); });
        msgs.forEach(m => { if (S.dirtyM.get(m.id) === m) S.dirtyM.delete(m.id); });
        let changed = false;
        for (const c of resp.conversations) {
          const local = S.convs.get(c.id);
          if (local && local.updated_at >= c.updated_at) continue;
          S.convs.set(c.id, c); changed = true;
          if (c.deleted) S.msgs.delete(c.id);
        }
        for (const m of resp.messages) {
          const list = S.msgs.get(m.conv_id) || []; const local = list.find(x => x.id === m.id);
          if (local && local.updated_at >= m.updated_at) continue;
          if (local) Object.assign(local, m); else list.push(m);
          S.msgs.set(m.conv_id, list); changed = true;
        }
        S.cursor = resp.cursor;
        if (changed && !S.busy) { renderSide(); renderMsgs(); }
        if (!resp.more && S.dirtyM.size === 0 && S.dirtyC.size === 0) break;
      }
      S.lastSync = Date.now(); S.syncError = "";
    } catch (e) {
      S.syncError = e.status === 401 ? "Wrong API key" : "Sync failed: " + e.message;
      if (e.status === 401) askKey();
    } finally { syncing = null; renderSyncState(); }
  })();
  return syncing;
}
function renderSyncState() {
  $("#syncState").textContent = S.syncError || (S.lastSync ? "Synced " + new Date(S.lastSync).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "Syncing…");
  $("#dot").classList.toggle("on", S.busy);
}

async function loadMeta() {
  const [t, p, md, mem] = await Promise.allSettled([api("/tools"), api("/projects"), api("/modes"), api("/memories")]);
  if (t.status === "fulfilled") { S.tools = t.value.tools || []; S.approval = new Set(t.value.approval || []); }
  if (p.status === "fulfilled") S.projects = p.value.projects || [];
  if (md.status === "fulfilled") S.customModes = md.value.modes || [];
  if (mem.status === "fulfilled") S.memories = mem.value.memories || [];
  renderSide(); renderBar();
}

// ---------------------------------------------------------------- markdown
function inline(s) {
  s = esc(s);
  s = s.replace(/!\[([^\]]*)\]\((diagram|image):([0-9a-f]{16})\)/g, (_, t, k, id) => `<img alt="${t}" src="${media(k + ":" + id)}">`);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  return s;
}
function md(src) {
  const out = []; const lines = String(src || "").replace(/\r/g, "").split("\n"); let i = 0, list = null, para = [];
  const flush = () => { if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; } if (list) { out.push(`</${list}>`); list = null; } };
  while (i < lines.length) {
    const l = lines[i];
    const fence = l.match(/^\s*```\s*([\w+#.-]*)/);
    if (fence) {
      flush(); const code = []; i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i++; out.push(`<pre><button class="cp">Copy</button><code data-lang="${esc(fence[1])}">${esc(code.join("\n"))}</code></pre>`); continue;
    }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { flush(); out.push(`<h${Math.min(3, m[1].length)}>${inline(m[2])}</h${Math.min(3, m[1].length)}>`); }
    else if ((m = l.match(/^\s*([-*•]|\d+[.)])\s+(.*)/))) {
      const kind = /\d/.test(m[1]) ? "ol" : "ul"; if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; }
      if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[2])}</li>`);
    } else if ((m = l.match(/^>\s?(.*)/))) { flush(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
    else if (/^\s*\|.*\|\s*$/.test(l)) {
      flush(); const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { if (!/^\s*\|[\s:|-]+\|\s*$/.test(lines[i])) rows.push(lines[i]); i++; }
      out.push("<table>" + rows.map((r, n) => "<tr>" + r.trim().slice(1, -1).split("|").map(c => n ? `<td>${inline(c.trim())}</td>` : `<th>${inline(c.trim())}</th>`).join("") + "</tr>").join("") + "</table>");
      continue;
    } else if (!l.trim()) flush();
    else { if (list) { out.push(`</${list}>`); list = null; } para.push(l); }
    i++;
  }
  flush(); return out.join("");
}

// ---------------------------------------------------------------- rendering
function renderSide() {
  const pid = curProjectId();
  $("#projects").innerHTML = S.projects.length ? S.projects.map(p => `<div class="item${p.id === pid && !S.current ? " sel" : ""}" data-p="${p.id}">
      <span style="color:${COLORS[p.color] || COLORS.cyan}">◆</span><span class="t">${esc(p.name)}</span><span class="sub">${[...S.convs.values()].filter(c => !c.deleted && c.project_id === p.id).length || ""}</span></div>`).join("")
    : `<div class="sub" style="padding:2px 10px 6px">Group chats with shared instructions, notes and files.</div>`;
  const convs = [...S.convs.values()].filter(c => !c.deleted).sort((a, b) => b.updated_at - a.updated_at);
  $("#chats").innerHTML = convs.map(c => {
    const p = project(c.project_id); const m = c.mode ? modeById(c.mode).icon + " " : "";
    return `<div class="item${c.id === S.current ? " sel" : ""}" data-c="${c.id}"><span class="t">${esc(m + (c.title || "Untitled"))}${p ? `<div class="sub" style="color:${COLORS[p.color] || COLORS.cyan}">◆ ${esc(p.name)}</div>` : ""}</span><button class="x" data-del="${c.id}" title="Delete chat">🗑</button></div>`;
  }).join("") || `<div class="sub" style="padding:2px 10px">No chats yet.</div>`;
}
function renderBar() {
  const c = curConv(), m = curMode(), p = project(curProjectId());
  $("#title").textContent = NAME().toUpperCase();
  $("#subtitle").textContent = S.status || (c ? c.title : "New chat");
  $("#modeChip").textContent = `${m.icon} ${m.name.toUpperCase()} ▾`;
  $("#projectChip").style.display = p ? "" : "none";
  if (p) { $("#projectChip").textContent = "◆ " + p.name; $("#projectChip").style.color = COLORS[p.color] || COLORS.cyan; }
  const info = S.context;
  $("#meter").style.display = info && c ? "" : "none";
  if (info) {
    const f = Math.min(1, info.used / info.limit);
    $("#meterFill").style.width = (f * 100) + "%";
    $("#meterFill").style.background = f > .85 ? "var(--red)" : f > .6 ? "var(--gold)" : "var(--accent)";
    $("#meterPct").textContent = Math.round(f * 100) + "%";
  }
  $("#send").textContent = S.busy ? "Stop" : "Send";
  renderSyncState();
}
function renderMsgs(scroll = true) {
  const wrap = $("#wrap"); const list = S.current ? msgsOf(S.current) : [];
  if (!list.length && !S.streaming) {
    const m = curMode(), p = project(curProjectId());
    wrap.innerHTML = `<div class="empty">${p ? `<div class="hud" style="color:${COLORS[p.color]}">◆ ${esc(p.name)}</div><div class="sub">New chat in this project. ${esc(NAME())} knows its instructions, notes and files.</div><br>` : ""}
      ${m.id !== "general" ? `<div class="hud" style="color:var(--glow)">${m.icon} ${esc(m.name)} mode</div><div class="sub">${esc(m.desc)}</div><br>` : ""}
      <div class="star">✦</div><div class="hud" style="font-size:16px;color:var(--text);letter-spacing:5px">${esc(NAME().toUpperCase())} ONLINE</div>
      <p>Same chats as your phone. Try “Remind me to check the 3D print at 9”, or open a project and drop in a datasheet.</p></div>`;
    return;
  }
  const lastReply = [...list].reverse().find(m => m.role === "assistant");
  wrap.innerHTML = list.map(m => msgHtml(m, lastReply && m.id === lastReply.id)).join("") + (S.streaming ? msgHtml(S.streaming, false, true) : "") +
    (S.error ? `<div class="err">⚠ ${esc(S.error)}</div>` : "");
  if (scroll) $("#msgs").scrollTop = $("#msgs").scrollHeight;
}
function msgHtml(m, canRetry, live) {
  const name = NAME().toUpperCase();
  if (m.role === "tool") {
    const lines = m.text.split("\n"); const img = lines.find(l => /^!\[[^\]]*\]\(diagram:/.test(l.trim()));
    return `<div class="tool">› ${esc(lines.filter(l => l !== img).join(" "))}${img ? `<div class="md">${inline(img.trim())}</div>` : ""}</div>`;
  }
  if (m.role === "summary") return `<div class="summary" data-sum="${m.id}"><div class="hud">✦ Context refreshed</div><div class="sumbody sub">${esc(NAME())} continues from a summary of the messages above. Click to read it.</div></div>`;
  if (m.role === "user") {
    const photos = [...m.text.matchAll(/!\[[^\]]*\]\(image:([0-9a-f]{16})\)/g)].map(x => x[1]);
    const text = m.text.replace(/!\[[^\]]*\]\(image:[0-9a-f]{16}\)/g, "").trim();
    return `<div class="msg user">${live ? "" : `<div class="acts"><button data-copy="${m.id}">Copy</button>${S.busy ? "" : `<button data-edit="${m.id}">Edit</button>`}</div>`}${photos.map(id => `<div class="md"><img src="${media("image:" + id)}"></div>`).join("")}${esc(text)}</div>`;
  }
  const wide = /```|\n\|/.test(m.text);
  return `<div class="msg assistant${wide ? " wide" : ""}">${live ? "" : `<div class="acts"><button data-copy="${m.id}">Copy</button>${canRetry && !S.busy ? `<button data-retry="1">Retry</button>` : ""}</div>`}
    <div class="who">✦ ${esc(name)}</div>${m.reasoning ? `<div class="think" data-think>◌ Thought process (click to show)</div><div class="thinkbody" style="display:none" class="think">${esc(m.reasoning)}</div>` : ""}
    <div class="md">${m.text ? md(m.text) : '<span class="sub">…</span>'}</div></div>`;
}

// ---------------------------------------------------------------- prompt building (mirrors the phone app)
function nowLine() {
  const now = new Date(); const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const off = -now.getTimezoneOffset(); const sign = off >= 0 ? "+" : "-";
  const offset = sign + String(Math.floor(Math.abs(off) / 60)).padStart(2, "0") + ":" + String(Math.abs(off) % 60).padStart(2, "0");
  const date = now.toLocaleString("en-CA", { weekday: "long", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  return `Current time: ${date} UTC${offset} (${tz})`;
}
function oldMemorySection() {
  if (!S.memories.length) return "";
  let chars = 0; const lines = [];
  for (const m of S.memories) { const l = `- [${m.id}] ${m.text}`; if (chars + l.length > 3500) break; lines.push(l); chars += l.length; }
  return "What you remember about the user (id in brackets, for forget). Use naturally; don't recite.\n" + lines.join("\n");
}
// Time and per-message context go in front of the newest message only, so the start of the prompt stays the
// same between messages and the model server can reuse its cache (much faster on a CPU).
function withTurnContext(history, extra) {
  const i = history.map(h => h.role).lastIndexOf("user"); if (i < 0) return history;
  const block = `[Automatic context, not written by the user]\n${nowLine()}${extra ? "\n" + extra.trim() : ""}\n[End of context]\n\n`;
  const out = history.slice(); out[i] = { ...out[i], content: block + out[i].content }; return out;
}
function systemPrompt(projectText, summary, mode, memory) {
  const name = NAME();
  let p = `You are ${name}, the user's private AI assistant (StellAI), running on their own computer. Warm and capable with a light space-explorer touch; concise and practical.
The user is in the StellAI desktop web chat. Phone actions (alarms, apps, texts) aren't available here, but reminders still ring on their phones.
Markdown is fine; always put code in fenced blocks with a language; avoid tables.
Use the calculate/electronics tools for any maths. Use render_diagram when a picture helps (it shows automatically).
Save lasting facts with remember. Reminders: set_reminder. Lists: add_to_list. Current info: web_search then fetch_page, citing links.
A user message may start with an automatic context block (time, memories, file excerpts); the user didn't write it.`;
  if (mode.prompt) p += `\n\n## Current mode: ${mode.name}\n${mode.prompt}\nThese mode instructions take priority over the general style guidance above (such as keeping replies short).`;
  if (memory) p += "\n\n" + memory;
  if (projectText) p += "\n\n" + projectText;
  if (summary) p += "\n\n## Earlier in this chat\nThe start of this conversation was summarized to free up memory. Continue naturally from it; the user can still see the full chat.\n" + summary;
  return p;
}
const PHOTO = /!\[[^\]]*\]\(image:([0-9a-f]{16})\)/g, DIAGRAM = /!\[[^\]]*\]\(diagram:[0-9a-f]+\)/g;
function buildHistory(list) {
  const out = []; let pending = "";
  const flush = () => { if (pending.trim()) out.push({ role: "assistant", content: pending.trim() }); pending = ""; };
  for (const m of list) {
    if (m.role === "user") { flush(); out.push({ role: "user", content: m.text.replace(PHOTO, "[photo]").trim(), _id: m.id, _text: m.text }); }
    else if (m.role === "assistant" && m.text.trim()) pending += m.text + "\n";
    else if (m.role === "tool") pending += "(action result: " + m.text.replace(DIAGRAM, "[diagram shown]").trim() + ")\n";
  }
  flush(); return out;
}
async function prepare(convId, images) {
  const c = S.convs.get(convId); const mode = modeById(c.mode); const list = msgsOf(convId);
  let last = -1; list.forEach((m, i) => { if (m.role === "summary") last = i; });
  const summary = last >= 0 ? list[last].text : "";
  const history = buildHistory(list.slice(last + 1));
  const base = systemPrompt("", summary, mode, "");
  const sticky = [...(S.sticky.get(convId) || [])];
  const query = list.filter(m => m.role === "user").slice(-2).map(m => m.text.replace(PHOTO, "")).join("\n");
  let plan = null;
  try {
    plan = await api("/context", { method: "POST", json: { project_id: c.project_id || "", query, fixed: base, images, mode: mode.id,
      mode_tools: mode.tools || "all", split: true, sticky, thinking: thinkPref(mode),
      last: (list.filter(m => m.role === "user").slice(-1)[0] || { text: "" }).text.replace(PHOTO, ""), messages: history.map(h => ({ role: h.role, content: h.content })) } });
  } catch { plan = null; }
  let start = 0, info = null, needsRefresh = false, names = null, projectText = "", memory = oldMemorySection(), extra = "";
  let think = thinkPref(mode) === "on";
  if (plan) {
    const limit = plan.n_ctx - plan.reply_reserve, used = plan.fixed_tokens + plan.project_tokens + plan.history_tokens;
    info = { used, limit }; names = plan.tools; projectText = plan.project_context || "";
    if ("memory_context" in plan) memory = plan.memory_context || "";
    extra = plan.turn_context || "";
    if ("think" in plan) think = !!plan.think;
    start = Math.min(plan.keep_from || 0, history.length);
    while (start < history.length && history[start].role !== "user") start++;
    if (start >= history.length) start = Math.max(0, history.map(h => h.role).lastIndexOf("user"));
    needsRefresh = history.filter(h => h.role === "user").length >= 2 && (start > 0 || used > limit * 0.85);
  } else start = Math.max(0, history.length - 24);
  const tools = S.tools.filter(t => !names || names.includes(t.function.name));
  const hist = withTurnContext(history.slice(start), extra);
  return { system: systemPrompt(projectText, summary, mode, memory), history: hist, info, needsRefresh, tools, mode, think };
}
// "on"/"off"/"auto": the chat's mode first, then this browser's setting (auto = think on hard questions only)
function thinkPref(mode) {
  if (mode.thinking === "on" || mode.thinking === "off") return mode.thinking;
  const v = store.get("thinkingMode", null);
  return v || (store.get("thinking", false) ? "on" : "auto");
}
async function compact(convId, keepUserTurns) {
  const c = S.convs.get(convId); const list = msgsOf(convId);
  let last = -1; list.forEach((m, i) => { if (m.role === "summary") last = i; });
  const after = list.slice(last + 1); const users = after.map((m, i) => m.role === "user" ? i : -1).filter(i => i >= 0);
  let k = Math.min(keepUserTurns, users.length); while (k > 1 && users[users.length - k] === 0) k--;
  const cut = k === 0 ? after.length : users[users.length - k]; const part = after.slice(0, cut);
  if (!part.some(m => m.role === "user" || m.role === "assistant")) return false;
  const res = await api("/summarize", { method: "POST", json: { previous: last >= 0 ? list[last].text : "", style: modeById(c.mode).style || "work",
    messages: part.map(m => ({ role: m.role, content: m.text.replace(PHOTO, "[photo]").replace(DIAGRAM, "[diagram]") })) } });
  if (!res.summary) return false;
  let created;
  if (cut < after.length) { const next = after[cut].created_at, prev = part[part.length - 1].created_at; created = next - prev >= 2 ? Math.floor((prev + next) / 2) : next - 1; }
  else created = nextTs();
  saveMsg({ id: uuid(), conv_id: convId, role: "summary", text: res.summary.trim(), reasoning: "", created_at: created, updated_at: nextTs(), deleted: false });
  return true;
}
async function updateMeter() {
  if (!S.current || S.busy) return;
  try { const p = await prepare(S.current, 0); if (p.info) { S.context = p.info; renderBar(); } } catch {}
}

// ---------------------------------------------------------------- chatting
function addMsg(convId, role, text, reasoning = "") {
  const ts = nextTs(); const m = { id: uuid(), conv_id: convId, role, text, reasoning, created_at: ts, updated_at: ts, deleted: false };
  saveMsg(m); return m;
}
function touch(convId) { const c = S.convs.get(convId); if (c) saveConv({ ...c, updated_at: nextTs() }); }

async function send() {
  if (S.busy) { stop(); return; }
  const text = $("#input").value.trim(); const photos = S.attachments.slice();
  if (!text && !photos.length) return;
  if (S.editing) { truncateFrom(S.editing); S.editing = null; }
  $("#input").value = ""; autosize(); S.attachments = []; renderAtts();
  let convId = S.current;
  if (!convId) {
    convId = uuid(); const now = nextTs(); const t = (text || "Photo").replace(/\s+/g, " ");
    saveConv({ id: convId, title: t.length <= 48 ? t : t.slice(0, 47) + "…", created_at: now, updated_at: now, deleted: false,
      project_id: S.pendingProject, mode: S.pendingMode });
    S.current = convId; S.pendingProject = ""; S.pendingMode = ""; store.set("current", convId);
  }
  for (const p of photos) { try { await api(`/images/${p.id}.jpg`, { method: "PUT", body: p.blob, headers: { "Content-Type": "image/jpeg" } }); } catch (e) { notice("Couldn't upload a photo: " + e.message); } }
  addMsg(convId, "user", (text + photos.map(p => `\n![photo](image:${p.id})`).join("")).trim());
  renderSide(); renderMsgs();
  await run(convId, photos);
}
function stop() { if (S.abort) S.abort.abort(); S.stopRequested = true; }
async function retry() {
  if (S.busy || !S.current) return; const list = msgsOf(S.current);
  const lastUser = list.map(m => m.role).lastIndexOf("user"); if (lastUser < 0) return;
  if (lastUser + 1 < list.length) truncateFrom(list[lastUser + 1].id);
  await run(S.current, []);
}
function truncateFrom(id) {
  const list = msgsOf(S.current); const i = list.findIndex(m => m.id === id); if (i < 0) return;
  const now = nextTs(); list.slice(i).forEach(m => saveMsg({ ...m, deleted: true, text: "", reasoning: "", updated_at: now }));
}

async function run(convId, photos) {
  S.busy = true; S.error = null; S.stopRequested = false; S.status = "Contacting server…"; renderBar(); renderMsgs();
  const turn = []; const projectId = (S.convs.get(convId).project_id || "");
  try {
    let p = await prepare(convId, photos.length);
    if (p.needsRefresh && !S.stopRequested) {
      S.status = "Refreshing context…"; renderBar();
      try { if (await compact(convId, 2)) { p = await prepare(convId, photos.length); renderMsgs(); } } catch (e) { notice("Couldn't refresh the context: " + e.message); }
    }
    if (p.info) S.context = p.info;
    const st = S.sticky.get(convId) || new Set(); p.tools.forEach(t => st.add(t.function.name)); S.sticky.set(convId, st);
    // newest message with photos goes to the model as images
    const hist = p.history.map(h => ({ role: h.role, content: h.content }));
    const lastUser = p.history.map(h => h.role).lastIndexOf("user");
    if (lastUser >= 0 && photos.length) {
      const block = (hist[lastUser].content.match(/^\[Automatic context[\s\S]*?\[End of context\]\n\n/) || [""])[0];
      const parts = [{ type: "text", text: block + (p.history[lastUser]._text.replace(PHOTO, "").trim() || "(The user sent a photo.)") }];
      for (const ph of photos) parts.push({ type: "image_url", image_url: { url: ph.dataUrl } });
      hist[lastUser] = { role: "user", content: parts };
    }
    const thinking = p.think;
    // Qwen's recommended sampling for Qwen3.5 (model card); a mode's creativity overrides temperature
    const code = p.mode.id === "programmer" || p.mode.id === "electronics";
    for (let round = 0; round < 6; round++) {
      S.status = "Contacting server…"; renderBar();
      const body = { model: "local", stream: true, messages: [{ role: "system", content: p.system }, ...hist, ...turn],
        tools: p.tools, chat_template_kwargs: { enable_thinking: thinking },
        temperature: p.mode.temperature != null ? p.mode.temperature : (thinking ? (code ? 0.6 : 1.0) : 0.7),
        top_p: thinking ? 0.95 : 0.8, top_k: 20, min_p: 0, presence_penalty: code ? 0 : 1.5 };
      const result = await stream(body);
      const finalText = result.content.replace(/<think>[\s\S]*?(<\/think>|$)/g, "").trim();
      S.streaming = null;
      if (finalText || !result.toolCalls.length) addMsg(convId, "assistant", finalText || "(no reply)", result.reasoning);
      const assistant = { role: "assistant", content: finalText };
      if (result.toolCalls.length) assistant.tool_calls = result.toolCalls.map(tc => ({ id: tc.id, type: "function", function: { name: tc.name, arguments: tc.arguments } }));
      turn.push(assistant); renderMsgs();
      if (!result.toolCalls.length) break;
      for (const tc of result.toolCalls) {
        let args = {}; try { args = JSON.parse(tc.arguments || "{}"); } catch {}
        delete args.approved;
        if (["project_note", "project_search"].includes(tc.name) && projectId) args.project_id = projectId;
        let outcome;
        if (!S.tools.some(t => t.function.name === tc.name)) {
          outcome = { line: "✗ " + tc.name + " isn't available on the desktop", result: JSON.stringify({ ok: false, error: "That's a phone action; it isn't available in the desktop chat." }) };
        } else if (S.approval.has(tc.name) && !(await approve(tc.name, args))) {
          outcome = { line: "✗ You declined: " + tc.name.replace("github_", "GitHub "), result: JSON.stringify({ ok: false, error: "The user declined this action." }) };
        } else {
          if (S.approval.has(tc.name)) args.approved = true;
          if (tc.name === "search_chats") args.current_conversation = convId;
          // keep big results (pages, files) within the space left in Stella's context
          const left = p.info ? p.info.limit - p.info.used - Math.floor(JSON.stringify(turn).length / 3) : null;
          args._max_chars = left == null ? 12000 : Math.max(1500, Math.floor(left * 3 * 0.6));
          S.status = toolLabel(tc.name) + "…"; renderBar();
          try {
            const r = await api("/tools/" + encodeURIComponent(tc.name), { method: "POST", json: args });
            const res = r.result || {}; let line = (res.ok ? "✓ " : "✗ ") + (r.summary || tc.name);
            if (res.ok && res.image) line += `\n![${(res.title || "Diagram").replace(/]/g, ")")}](diagram:${res.image})`;
            outcome = { line, result: JSON.stringify(res) };
          } catch (e) { outcome = { line: "✗ " + e.message, result: JSON.stringify({ ok: false, error: e.message }) }; }
        }
        addMsg(convId, "tool", outcome.line);
        turn.push({ role: "tool", tool_call_id: tc.id, name: tc.name, content: outcome.result });
        renderMsgs();
      }
    }
  } catch (e) {
    S.streaming = null;
    if (!S.stopRequested) S.error = e.status === 401 ? "The API key was rejected." : (e.message || String(e));
  } finally {
    if (S.stopRequested) S.error = "Stopped.";
    S.busy = false; S.status = ""; S.abort = null; S.streaming = null;
    touch(convId); renderSide(); renderMsgs(); renderBar();
    sync().then(updateMeter);
    if (turn.some(t => t.role === "tool" && t.name === "project_note")) loadMeta();
  }
}
const toolLabel = n => ({ render_diagram: "Drawing diagram", web_search: "Searching the web", fetch_page: "Reading the page", set_reminder: "Setting reminder",
  project_search: "Searching project files", library_search: "Searching the library", remember: "Remembering" }[n] || (n.startsWith("github") ? "Working with GitHub" : "Calculating"));

async function stream(body) {
  S.abort = new AbortController();
  const r = await fetch("/llm/v1/chat/completions", { method: "POST", signal: S.abort.signal,
    headers: { Authorization: "Bearer " + KEY, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) { let msg = "HTTP " + r.status; try { const j = await r.json(); msg = (j.error && (j.error.message || j.error)) || msg; } catch {} const e = new Error(msg); e.status = r.status; throw e; }
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
  const res = { content: "", reasoning: "", toolCalls: [] }; const calls = [];
  S.streaming = { id: "live", role: "assistant", text: "", reasoning: "" };
  let last = 0;
  for (;;) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 1);
      if (!line.startsWith("data:")) continue; const data = line.slice(5).trim(); if (data === "[DONE]") continue;
      let j; try { j = JSON.parse(data); } catch { continue; }
      if (j.error) throw new Error(j.error.message || "server error");
      const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
      if (d.content) res.content += d.content;
      if (d.reasoning_content) res.reasoning += d.reasoning_content;
      for (const tc of d.tool_calls || []) {
        const c = calls[tc.index || 0] || (calls[tc.index || 0] = { id: "", name: "", arguments: "" });
        if (tc.id) c.id = tc.id; if (tc.function && tc.function.name) c.name += tc.function.name; if (tc.function && tc.function.arguments) c.arguments += tc.function.arguments;
      }
    }
    S.streaming.text = res.content.replace(/<think>[\s\S]*?(<\/think>|$)/g, "").trimStart(); S.streaming.reasoning = res.reasoning;
    S.status = !S.streaming.text && res.reasoning ? "Thinking…" : "Writing…";
    if (Date.now() - last > 80) { last = Date.now(); renderMsgs(); renderBar(); }
  }
  res.toolCalls = calls.filter(Boolean).map((c, i) => ({ ...c, id: c.id || "call_" + i }));
  return res;
}

// ---------------------------------------------------------------- approvals (GitHub commits and pull requests)
function approve(name, args) {
  return new Promise(resolve => {
    let body;
    if (name === "github_commit") {
      const files = (args.files || []).map((f, i) => `<details><summary>${esc(f.path)} <span class="sub">(${(f.content || "").split("\n").length} lines)</span></summary><pre style="max-height:300px;overflow:auto"><code>${esc(f.content || "")}</code></pre></details>`).join("");
      body = `<p>${esc(NAME())} wants to <b>commit</b> to <code>${esc(args.repo || "your default repo")}</code>, branch <code>${esc(args.branch)}</code>:</p>
        <p><i>${esc(args.message || "")}</i></p>${files}${(args.delete || []).length ? `<p>Delete: ${(args.delete || []).map(esc).join(", ")}</p>` : ""}
        <p class="sub">Nothing goes to your main branch; you can review it on GitHub and merge with a pull request.</p>`;
    } else {
      body = `<p>${esc(NAME())} wants to <b>open a pull request</b> in <code>${esc(args.repo || "your default repo")}</code> from <code>${esc(args.branch)}</code>:</p>
        <p><b>${esc(args.title || "")}</b></p><div class="md">${md(args.body || "")}</div>`;
    }
    const d = dialog(`<h2>Approve this GitHub change?</h2>${body}<div class="btns"><button id="no">Decline</button><button class="primary" id="yes">Approve</button></div>`);
    d.querySelector("#yes").onclick = () => { d.remove(); resolve(true); };
    d.querySelector("#no").onclick = () => { d.remove(); resolve(false); };
  });
}

// ---------------------------------------------------------------- dialogs
function dialog(html, onClose) {
  const o = document.createElement("div"); o.className = "overlay";
  o.innerHTML = `<div class="dialog">${html}</div>`;
  o.addEventListener("mousedown", e => { if (e.target === o) { o.remove(); onClose && onClose(); } });
  document.body.appendChild(o); return o;
}
function notice(text) { const n = $("#notice"); n.textContent = "✦ " + text + "  (click to dismiss)"; n.style.display = "block"; }
function askKey() {
  if (document.querySelector("#keyDlg")) return;
  const d = dialog(`<h2 id="keyDlg">Connect to Stella</h2><p class="sub">Enter the API key shown by StellAI Server (Windows: the server window; Linux: the StellAI Server terminal).</p>
    <input id="k" type="password" placeholder="API key" autocomplete="off"><div class="btns"><button class="primary" id="ok">Connect</button></div>`);
  const go = () => { KEY = d.querySelector("#k").value.trim(); store.set("key", KEY); d.remove(); start(); };
  d.querySelector("#ok").onclick = go; d.querySelector("#k").onkeydown = e => { if (e.key === "Enter") go(); };
  setTimeout(() => d.querySelector("#k").focus(), 50);
}
function pickMode() {
  const cur = curMode();
  const d = dialog(`<h2>${S.current ? "Switch mode" : "Mode for this chat"}</h2><p class="sub">Changes how ${esc(NAME())} thinks and writes in this chat, from her next reply on.</p>` +
    allModes().map(m => `<div class="pick${m.id === cur.id ? " sel" : ""}" data-m="${esc(m.id)}"><span class="ic">${esc(m.icon)}</span><div style="flex:1"><div>${esc(m.name)}</div><div class="sub">${esc(m.desc)}</div></div>${m.id === cur.id ? "✓" : ""}</div>`).join("") +
    `<div class="btns"><span class="sub" style="flex:1">Create your own modes in the phone app (Manage modes).</span><button id="close">Close</button></div>`);
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelectorAll("[data-m]").forEach(el => el.onclick = () => {
    const id = el.dataset.m === "general" ? "" : el.dataset.m;
    if (S.current) { saveConv({ ...curConv(), mode: id, updated_at: nextTs() }); sync().then(updateMeter); } else S.pendingMode = id;
    d.remove(); renderBar(); renderSide(); renderMsgs();
  });
}
function pickProject() {
  const pid = curProjectId();
  const d = dialog(`<h2>${S.current ? "Move chat to…" : "Start this chat in…"}</h2>` +
    [{ id: "", name: "No project", color: "" }].concat(S.projects).map(p => `<div class="pick${p.id === pid ? " sel" : ""}" data-p="${p.id}"><span class="ic" style="color:${COLORS[p.color] || "var(--muted)"}">◆</span><div style="flex:1">${esc(p.name)}</div>${p.id === pid ? "✓" : ""}</div>`).join("") +
    `<div class="btns"><button id="close">Close</button></div>`);
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelectorAll("[data-p]").forEach(el => el.onclick = () => {
    if (S.current) { saveConv({ ...curConv(), project_id: el.dataset.p, updated_at: nextTs() }); sync().then(updateMeter); } else S.pendingProject = el.dataset.p;
    d.remove(); renderBar(); renderSide(); renderMsgs();
  });
}
async function openProject(id) {
  const isNew = id === "new", isLib = id === "library";
  let p = isNew ? { id: "", name: "", color: "cyan", instructions: "", notes: [], files: [] } : isLib ? await api("/library") : await api("/projects").then(r => r.projects.find(x => x.id === id));
  if (!p) return;
  const fileRows = files => files.map(f => `<div class="row"><span>${f.kind === "pdf" ? "📄" : f.kind === "image" ? "🖼" : f.kind === "code" ? "⌨" : "📝"}</span><div class="t">${esc(f.name)}<div class="sub">${f.size > 1e6 ? (f.size / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(f.size / 1000)) + " KB"} · ~${f.tokens} tokens${f.note ? " · " + esc(f.note) : ""}</div></div><button class="ghost" data-view="${f.id}">View</button><button class="ghost" data-delf="${f.id}">🗑</button></div>`).join("") || `<div class="sub">No files yet.</div>`;
  const d = dialog(`<h2>${isLib ? "📚 Library" : isNew ? "New project" : "◆ " + esc(p.name)}</h2>
    ${isLib ? `<p class="sub">Documents ${esc(NAME())} can search from any chat: datasheets, manuals, reference notes.</p>` : `
    <label class="sub">Name</label><input id="pn" value="${esc(p.name)}" maxlength="60">
    <div style="display:flex;gap:8px;margin:10px 0">${Object.entries(COLORS).map(([k, v]) => `<span data-col="${k}" style="width:24px;height:24px;border-radius:50%;background:${v};cursor:pointer;outline:${k === p.color ? "3px solid #fff" : "none"}"></span>`).join("")}</div>
    <label class="sub">Instructions for ${esc(NAME())}</label><textarea id="pi" rows="4">${esc(p.instructions)}</textarea>
    <div class="btns" style="justify-content:flex-start"><button class="primary" id="psave">${isNew ? "Create project" : "Save changes"}</button>${isNew ? "" : `<button id="pchat">+ New chat in this project</button>`}</div>`}
    ${isNew ? "" : `${isLib ? "" : `<h3 class="hud" style="margin-top:16px">Notes</h3><div class="list" id="notes">${p.notes.map(n => `<div class="row"><div class="t">${esc(n.text)}</div><button class="ghost" data-deln="${n.id}">🗑</button></div>`).join("") || `<div class="sub">No notes yet.</div>`}</div>
      <div style="display:flex;gap:8px;margin-top:6px"><input id="nn" placeholder="e.g. Input is 12–24 V, output 5 V 3 A"><button id="nadd">Add</button></div>`}
    <h3 class="hud" style="margin-top:16px">Files</h3><div class="list" id="files">${fileRows(p.files)}</div>
    <div style="margin-top:8px"><button id="fadd">+ Add files</button> <span class="sub" id="fstat">PDF, Word, code, text or images, up to 25 MB each. You can also drop files here.</span><input type="file" id="fin" multiple hidden></div>
    ${isLib ? "" : `<div class="btns"><button class="ghost" id="pdel" style="color:var(--red)">Delete project</button></div>`}`}
    <div class="btns"><button id="close">Close</button></div>`, loadMeta);
  const q = s => d.querySelector(s);
  q("#close").onclick = () => { d.remove(); loadMeta(); };
  let color = p.color;
  d.querySelectorAll("[data-col]").forEach(el => el.onclick = () => { color = el.dataset.col; d.querySelectorAll("[data-col]").forEach(x => x.style.outline = x === el ? "3px solid #fff" : "none"); });
  if (q("#psave")) q("#psave").onclick = async () => {
    try { const saved = await api("/projects", { method: "POST", json: { id: p.id || undefined, name: q("#pn").value, color, instructions: q("#pi").value } });
      d.remove(); await loadMeta(); if (isNew) openProject(saved.id); } catch (e) { alertBox(e.message); } };
  if (q("#pchat")) q("#pchat").onclick = () => { d.remove(); newChat(p.id); };
  if (q("#nadd")) q("#nadd").onclick = async () => { const t = q("#nn").value.trim(); if (!t) return; await api(`/projects/${p.id}/notes`, { method: "POST", json: { text: t } }); d.remove(); openProject(id); };
  d.querySelectorAll("[data-deln]").forEach(el => el.onclick = async () => { await api(`/projects/${p.id}/notes/${el.dataset.deln}`, { method: "DELETE" }); d.remove(); openProject(id); });
  d.querySelectorAll("[data-delf]").forEach(el => el.onclick = async () => { await api(`/projects/${p.id}/files/${el.dataset.delf}`, { method: "DELETE" }); d.remove(); openProject(id); });
  d.querySelectorAll("[data-view]").forEach(el => el.onclick = async () => {
    const r = await fetch(`/projects/${p.id}/files/${el.dataset.view}.txt?key=${encodeURIComponent(KEY)}`); const t = await r.text();
    dialog(`<h2>What ${esc(NAME())} reads from this file</h2><pre style="white-space:pre-wrap;font-size:12px">${esc(t.slice(0, 30000))}${t.length > 30000 ? "\n[…]" : ""}</pre>`); });
  const upload = async files => {
    for (const f of files) {
      if (f.size > 25e6) { q("#fstat").textContent = f.name + " is over 25 MB"; continue; }
      q("#fstat").textContent = "Reading " + f.name + "…";
      try { await api(`/projects/${p.id}/files?name=${encodeURIComponent(f.name)}`, { method: "PUT", body: f, headers: { "Content-Type": "application/octet-stream" } }); }
      catch (e) { alertBox("Couldn't add " + f.name + ": " + e.message); }
    }
    d.remove(); openProject(id);
  };
  if (q("#fadd")) { q("#fadd").onclick = () => q("#fin").click(); q("#fin").onchange = () => upload([...q("#fin").files]);
    q(".dialog").addEventListener("dragover", e => e.preventDefault()); q(".dialog").addEventListener("drop", e => { e.preventDefault(); upload([...e.dataTransfer.files]); }); }
  if (q("#pdel")) q("#pdel").onclick = async () => { if (!confirm(`Delete “${p.name}”? Its notes and files are deleted; its chats are kept.`)) return;
    await api(`/projects/${p.id}`, { method: "DELETE" }); d.remove(); await loadMeta(); sync(); };
}
function alertBox(text) { const d = dialog(`<p>${esc(text)}</p><div class="btns"><button id="ok">OK</button></div>`); d.querySelector("#ok").onclick = () => d.remove(); }
async function openTasks() {
  const [r, l] = await Promise.all([api("/reminders"), api("/lists")]);
  const fmt = ms => new Date(ms).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const up = r.reminders.filter(x => !x.done);
  const d = dialog(`<h2>⏰ Reminders & lists</h2>
    <div class="list">${up.map(x => `<div class="row"><div class="t">${esc(x.text)}<div class="sub">${fmt(x.due_at)}${x.repeat !== "none" ? " · " + x.repeat : ""}</div></div><button class="ghost" data-cancel="${x.id}">Cancel</button></div>`).join("") || `<div class="sub">No upcoming reminders. Ask ${esc(NAME())}: “remind me to…”.</div>`}</div>
    <div style="display:flex;gap:8px;margin-top:8px"><input id="rt" placeholder="New reminder"><input id="rw" type="datetime-local" style="max-width:220px"><button id="radd">Add</button></div>
    ${Object.entries(l.lists).map(([name, items]) => `<h3 class="hud" style="margin-top:16px">${esc(name)} <button class="ghost" data-clear="${esc(name)}">Clear ticked</button></h3>
      <div class="list">${items.map(i => `<div class="row"><input type="checkbox" style="width:auto" data-tick="${i.id}" ${i.done ? "checked" : ""}><div class="t" style="${i.done ? "text-decoration:line-through;color:var(--muted)" : ""}">${esc(i.text)}</div><button class="ghost" data-deli="${i.id}">🗑</button></div>`).join("")}</div>
      <div style="display:flex;gap:8px;margin-top:4px"><input data-newin="${esc(name)}" placeholder="Add to ${esc(name)}"></div>`).join("")}
    <div style="display:flex;gap:8px;margin-top:16px"><input id="ln" placeholder="New list, e.g. parts" style="max-width:200px"><input id="li" placeholder="First item"><button id="ladd">Add</button></div>
    <div class="btns"><button id="close">Close</button></div>`);
  const again = () => { d.remove(); openTasks(); };
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelectorAll("[data-cancel]").forEach(el => el.onclick = async () => { await api(`/reminders/${el.dataset.cancel}`, { method: "POST", json: { cancel: true } }); again(); });
  d.querySelector("#radd").onclick = async () => { const t = d.querySelector("#rt").value.trim(), w = d.querySelector("#rw").value; if (!t || !w) return;
    try { await api("/reminders", { method: "POST", json: { text: t, due_at: new Date(w).getTime() } }); again(); } catch (e) { alertBox(e.message); } };
  d.querySelectorAll("[data-tick]").forEach(el => el.onchange = async () => { await api(`/lists/items/${el.dataset.tick}`, { method: "POST", json: { done: el.checked } }); again(); });
  d.querySelectorAll("[data-deli]").forEach(el => el.onclick = async () => { await api(`/lists/items/${el.dataset.deli}`, { method: "DELETE" }); again(); });
  d.querySelectorAll("[data-clear]").forEach(el => el.onclick = async () => { await api(`/lists/${encodeURIComponent(el.dataset.clear)}/clear`, { method: "POST", json: {} }); again(); });
  d.querySelectorAll("[data-newin]").forEach(el => el.onkeydown = async e => { if (e.key === "Enter" && el.value.trim()) { await api(`/lists/${encodeURIComponent(el.dataset.newin)}`, { method: "POST", json: { items: [el.value.trim()] } }); again(); } });
  d.querySelector("#ladd").onclick = async () => { const n = d.querySelector("#ln").value.trim(), i = d.querySelector("#li").value.trim(); if (!n || !i) return;
    await api(`/lists/${encodeURIComponent(n)}`, { method: "POST", json: { items: [i] } }); again(); };
}
async function openSettings() {
  let gh = {}; try { gh = await api("/github"); } catch {}
  const d = dialog(`<h2>⚙ Settings</h2>
    <label class="sub">Assistant name (this browser)</label><input id="nm" value="${esc(NAME())}">
    <label class="sub" style="display:block;margin-top:10px">Thinking before answering</label>
    <select id="th">${[["auto", "Auto: only for hard questions"], ["on", "Always"], ["off", "Never"]].map(([v, l]) => `<option value="${v}" ${thinkPref({}) === v ? "selected" : ""}>${l}</option>`).join("")}</select>
    <h3 class="hud" style="margin-top:18px">GitHub</h3>
    <p class="sub">${gh.token_set ? (gh.user ? "Connected as <b>" + esc(gh.user) + "</b>." : "Token saved" + (gh.error ? ": " + esc(gh.error) : ".")) : "Not connected: public repositories only, read-only."}
    For private repos and commits, create a fine-grained token on GitHub (Settings → Developer settings → Fine-grained tokens) with access to your repos and permissions: Contents read/write, Pull requests read/write, Actions read, Issues read.</p>
    <input id="tok" type="password" placeholder="${gh.token_set ? "•••••••• (leave empty to keep)" : "GitHub token"}" autocomplete="off">
    <input id="repo" style="margin-top:8px" placeholder="Default repository, e.g. courier-iot/StellAI" value="${esc(gh.default_repo || "")}">
    <div class="btns" style="justify-content:flex-start">${gh.token_set ? `<button class="ghost" id="untok">Remove token</button>` : ""}</div>
    <h3 class="hud" style="margin-top:18px">This browser</h3>
    <button id="logout">Forget the API key here</button>
    <div class="btns"><button id="close">Cancel</button><button class="primary" id="save">Save</button></div>`);
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelector("#logout").onclick = () => { store.set("key", ""); location.reload(); };
  if (d.querySelector("#untok")) d.querySelector("#untok").onclick = async () => { await api("/github", { method: "POST", json: { token: "" } }); d.remove(); openSettings(); };
  d.querySelector("#save").onclick = async () => {
    store.set("name", d.querySelector("#nm").value.trim() || "Stella"); store.set("thinkingMode", d.querySelector("#th").value);
    const body = { default_repo: d.querySelector("#repo").value.trim() }; const t = d.querySelector("#tok").value.trim(); if (t) body.token = t;
    try { const st = await api("/github", { method: "POST", json: body }); d.remove(); if (st.error) alertBox("GitHub: " + st.error); renderBar(); renderMsgs(); }
    catch (e) { alertBox(e.message); }
  };
}

// ---------------------------------------------------------------- search past chats
let searchTimer = 0, searchSeq = 0;
function renderSearch(q, hits, note) {
  const ql = q.toLowerCase(); const seen = new Set(hits.map(h => h.conversation_id));
  const titles = [...S.convs.values()].filter(c => !c.deleted && !seen.has(c.id) && (c.title || "").toLowerCase().includes(ql))
    .sort((a, b) => b.updated_at - a.updated_at);
  $("#searchResults").innerHTML = (note ? `<div class="sub" style="padding:4px 10px">${esc(note)}</div>` : "")
    + hits.map(h => `<div class="item hit${h.conversation_id === S.current ? " sel" : ""}" data-c="${esc(h.conversation_id)}"><span class="t" style="white-space:normal">${esc(h.title || "Untitled")}
        <div class="sub">${esc(h.date || "")}</div><div class="ex">${esc(h.excerpt || "")}</div></span></div>`).join("")
    + titles.map(c => `<div class="item${c.id === S.current ? " sel" : ""}" data-c="${c.id}"><span class="t">${esc(c.title || "Untitled")}</span></div>`).join("")
    || `<div class="sub" style="padding:4px 10px">${q.length < 3 ? "Keep typing…" : "No chats found."}</div>`;
}
function onSearch() {
  const q = $("#search").value.trim(); clearTimeout(searchTimer);
  $("#searchView").style.display = q ? "" : "none"; $("#listView").style.display = q ? "none" : "";
  if (!q) return;
  renderSearch(q, [], q.length >= 3 ? "Searching…" : "");
  if (q.length < 3) return;
  const seq = ++searchSeq;
  searchTimer = setTimeout(async () => {
    try { const r = await api("/search?q=" + encodeURIComponent(q)); if (seq === searchSeq) renderSearch(q, r.results || []); }
    catch { if (seq === searchSeq) renderSearch(q, [], "Server search unavailable; title matches only."); }
  }, 400);
}

// ---------------------------------------------------------------- server: status, models, health check, backups
async function openServer() {
  const d = dialog(`<h2>🖥 Server</h2><div id="sv">Loading…</div>
    <h3 class="hud" style="margin-top:18px">Health check</h3><button id="doc">Check everything</button><div id="docOut"></div>
    <h3 class="hud" style="margin-top:18px">Backups</h3><div id="bk">Loading…</div>
    <div class="btns"><button id="close">Close</button></div>`);
  let timer = 0, closed = false;
  d.querySelector("#close").onclick = () => { closed = true; clearTimeout(timer); d.remove(); };
  const msg = (t, bad) => { const n = document.createElement("p"); n.className = "sub"; n.style.color = bad ? "var(--red)" : "var(--gold)"; n.textContent = t; d.querySelector("#sv").prepend(n); };
  const fmtUp = s => s < 3600 ? Math.floor(s / 60) + " min" : s < 86400 ? Math.floor(s / 3600) + " h " + Math.floor(s % 3600 / 60) + " min" : Math.floor(s / 86400) + " days";
  async function loadServer() {
    if (closed || !d.isConnected) return;
    let st; try { st = await api("/server"); } catch (e) { d.querySelector("#sv").innerHTML = `<p class="err">Couldn't reach the server: ${esc(e.message)}</p>`; return; }
    const rows = [["Model", st.model || st.model_path || "unknown"], ["Context", st.context ? st.context + " tokens" : "–"],
      ["Runs on", (st.backend || "").toUpperCase() || "–"], ["Memory", st.ram_gb + " GB RAM" + (st.vram_gb ? " · " + st.vram_gb + " GB GPU" : "")], ["Up for", fmtUp(st.uptime_s || 0)]];
    const models = st.models || [];
    d.querySelector("#sv").innerHTML = `<p><span class="status${st.loaded ? " on" : ""}"></span> ${st.loaded ? "Running" : "Loading the model…"}</p>
      <div class="list">${rows.map(([k, v]) => `<div class="row"><span class="sub" style="width:90px">${k}</span><span class="t">${esc(v)}</span></div>`).join("")}</div>
      ${models.length ? `<h3 class="hud" style="margin-top:18px">Model</h3><p class="sub">Switching takes about a minute while the new model loads.</p>
      <div class="list">${models.map(m => { const dl = m.download || null; const pct = dl && dl.total ? Math.floor(dl.done * 100 / dl.total) : 0;
        return `<div class="row" style="flex-wrap:wrap"><div class="t"><b style="${m.current ? "color:var(--accent)" : ""}">${esc(m.label)}${m.current ? " · in use" : ""}</b>
          <div class="sub">${esc(m.about || "")} ${m.size_gb} GB, ${Math.round((m.context || 0) / 1024)}K context.</div>
          ${dl && dl.state === "downloading" ? `<div class="bar2"><div style="width:${pct}%"></div></div><div class="sub">Downloading… ${pct}%</div>` : ""}
          ${dl && dl.state === "failed" ? `<div class="sub" style="color:var(--red)">Download failed: ${esc(dl.error)}</div>` : ""}</div>
          ${m.current ? "" : m.installed ? `<button data-sw="${esc(m.key)}">Switch</button>` : dl && dl.state === "downloading" ? "" : `<button data-dl="${esc(m.key)}">Download</button>`}</div>`; }).join("")}</div>`
      : `<p class="sub">This server can't switch models from here yet (re-run its setup to enable it).</p>`}`;
    d.querySelectorAll("[data-sw]").forEach(b => b.onclick = async () => {
      const m = models.find(x => x.key === b.dataset.sw); if (!confirm(`Switch to ${m.label}? ${NAME()} is unavailable for about a minute while it loads.`)) return;
      try { const r = await api("/server/model", { method: "POST", json: { key: m.key } }); await loadServer(); msg(r.message || "Switching…"); } catch (e) { msg(e.message, true); } });
    d.querySelectorAll("[data-dl]").forEach(b => b.onclick = async () => {
      try { const r = await api("/server/download", { method: "POST", json: { key: b.dataset.dl } }); await loadServer(); msg(r.message || "Downloading…"); } catch (e) { msg(e.message, true); } });
    const busy = !st.loaded || models.some(m => m.download && m.download.state === "downloading");
    clearTimeout(timer); if (busy) timer = setTimeout(loadServer, 2500);
  }
  async function loadBackups() {
    let b; try { b = await api("/backups"); } catch (e) { d.querySelector("#bk").innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
    d.querySelector("#bk").innerHTML = `<p class="sub">Every night the server saves your chats, memories, projects, photos and wake words. It keeps the last ${b.keep}.</p>
      <p>${b.last ? "Last backup: " + new Date(b.last).toLocaleString() : "No backup yet"}</p>${b.error ? `<p class="err">${esc(b.error)}</p>` : ""}
      <button class="primary" id="bnow">Back up now</button>
      <label class="sub" style="display:block;margin-top:10px">Also copy to (folder on the server, e.g. a USB drive)</label>
      <div style="display:flex;gap:8px"><input id="bcopy" value="${esc(b.copy_to || "")}" placeholder="/media/you/USB/stellai"><input id="bkeep" type="number" min="2" max="60" value="${b.keep}" style="width:80px" title="How many to keep"><button id="bsave">Save</button></div>
      <p class="sub">To restore: Linux: run stellai-restore.sh on the server. Windows: StellAI app → Restore a backup.</p>
      <div class="list">${(b.backups || []).slice(0, 14).map(f => `<div class="row"><span class="t sub">${esc(f.name)}</span><span class="sub">${Math.round(f.size / 1e6)} MB</span></div>`).join("")}</div>`;
    d.querySelector("#bnow").onclick = async e => { e.target.disabled = true; e.target.textContent = "Backing up…";
      try { await api("/backups", { method: "POST", json: {} }); } catch (err) { alertBox(err.message); } loadBackups(); };
    d.querySelector("#bsave").onclick = async () => {
      try { await api("/backups/config", { method: "POST", json: { copy_to: d.querySelector("#bcopy").value.trim(), keep: +d.querySelector("#bkeep").value || 14 } }); loadBackups(); }
      catch (err) { alertBox(err.message); } };
  }
  d.querySelector("#doc").onclick = async e => {
    e.target.disabled = true; d.querySelector("#docOut").innerHTML = `<p class="sub">Checking… (takes a few seconds)</p>`;
    try { const r = await api("/doctor"); const ic = { ok: ["✓", "var(--accent)"], warn: ["!", "var(--gold)"], fail: ["✗", "var(--red)"] };
      d.querySelector("#docOut").innerHTML = (r.checks || []).map(c => { const [i, col] = ic[c.state] || ic.fail;
        return `<div class="check"><span class="ic" style="color:${col}">${i}</span><div><b>${esc(c.name)}</b>: ${esc(c.detail)}${c.fix && c.state !== "ok" ? `<div class="sub">→ ${esc(c.fix)}</div>` : ""}</div></div>`; }).join(""); }
    catch (err) { d.querySelector("#docOut").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
    e.target.disabled = false;
  };
  loadServer(); loadBackups();
}

// ---------------------------------------------------------------- photos
async function addPhotos(files) {
  for (const f of files) {
    if (!f.type.startsWith("image/")) continue;
    const img = await createImageBitmap(f); const scale = Math.min(1, 1280 / Math.max(img.width, img.height));
    const c = document.createElement("canvas"); c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    const blob = await new Promise(r => c.toBlob(r, "image/jpeg", 0.85));
    S.attachments.push({ id: hex16(), blob, dataUrl: c.toDataURL("image/jpeg", 0.85) });
  }
  renderAtts();
}
function renderAtts() {
  $("#atts").innerHTML = S.attachments.map((a, i) => `<div class="a"><img src="${a.dataUrl}"><button data-ra="${i}">✕</button></div>`).join("");
  $("#atts").querySelectorAll("[data-ra]").forEach(b => b.onclick = () => { S.attachments.splice(+b.dataset.ra, 1); renderAtts(); });
}

// ---------------------------------------------------------------- wiring
function newChat(projectId = "") { if (S.busy) stop(); S.current = null; S.pendingProject = projectId; S.pendingMode = ""; S.context = null; S.error = null; S.editing = null; store.set("current", ""); renderSide(); renderBar(); renderMsgs(); $("#input").focus(); }
function openChat(id) { if (S.busy) stop(); S.current = id; S.context = null; S.error = null; S.editing = null; store.set("current", id); renderSide(); renderBar(); renderMsgs(); updateMeter(); $("#side").classList.remove("open"); }
function autosize() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(240, t.scrollHeight) + "px"; }
$("#newChat").onclick = () => newChat();
$("#newProject").onclick = () => openProject("new");
$("#openLibrary").onclick = () => openProject("library");
$("#openTasks").onclick = openTasks;
$("#openSettings").onclick = openSettings;
$("#openServer").onclick = openServer;
$("#search").addEventListener("input", onSearch);
$("#search").addEventListener("keydown", e => { if (e.key === "Escape") { $("#search").value = ""; onSearch(); } });
$("#searchResults").onclick = e => { const el = e.target.closest("[data-c]"); if (el) { $("#search").value = ""; onSearch(); openChat(el.dataset.c); } };
$("#modeChip").onclick = pickMode;
$("#projectChip").onclick = () => openProject(curProjectId());
$("#meter").onclick = () => {
  const i = S.context; if (!i) return;
  const d = dialog(`<h2>${esc(NAME())}'s memory for this chat</h2><p>${i.used} of ${i.limit} tokens used (${Math.round(Math.min(1, i.used / i.limit) * 100)}%).</p>
    <p class="sub">When it fills up, ${esc(NAME())} refreshes it automatically: she summarizes the older part of the chat and continues from the summary. Every message stays here and on your other devices.</p>
    <div class="btns"><button id="close">Close</button><button class="primary" id="ref" ${S.busy ? "disabled" : ""}>Refresh now</button></div>`);
  d.querySelector("#close").onclick = () => d.remove();
  d.querySelector("#ref").onclick = async () => { d.remove(); S.busy = true; S.status = "Refreshing context…"; renderBar();
    try { if (!(await compact(S.current, 0))) notice("Nothing new to summarize yet."); } catch (e) { notice("Couldn't refresh: " + e.message); }
    S.busy = false; S.status = ""; renderMsgs(); renderBar(); sync().then(updateMeter); };
};
$("#send").onclick = send;
$("#attach").onclick = () => $("#file").click();
$("#file").onchange = () => { addPhotos([...$("#file").files]); $("#file").value = ""; };
$("#notice").onclick = () => { $("#notice").style.display = "none"; };
$("#input").addEventListener("input", autosize);
$("#input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("#input").addEventListener("paste", e => { const files = [...(e.clipboardData || {}).files || []].filter(f => f.type.startsWith("image/")); if (files.length) { e.preventDefault(); addPhotos(files); } });
$("#menuBtn").onclick = () => $("#side").classList.toggle("open");
if (matchMedia("(max-width:760px)").matches) $("#menuBtn").style.display = "";
$("#projects").onclick = e => { const el = e.target.closest("[data-p]"); if (el) openProject(el.dataset.p); };
$("#chats").onclick = e => {
  const del = e.target.closest("[data-del]");
  if (del) { const c = S.convs.get(del.dataset.del); if (c && confirm(`Delete “${c.title}” on all your devices?`)) {
    saveConv({ ...c, deleted: true, updated_at: nextTs() }); S.msgs.delete(c.id); if (S.current === c.id) newChat(); renderSide(); sync(); } return; }
  const el = e.target.closest("[data-c]"); if (el) openChat(el.dataset.c);
};
$("#wrap").onclick = e => {
  const t = e.target;
  if (t.classList.contains("cp")) { navigator.clipboard.writeText(t.nextElementSibling.textContent); t.textContent = "Copied"; setTimeout(() => t.textContent = "Copy", 1200); return; }
  if (t.dataset.copy) { const m = msgsOf(S.current).find(x => x.id === t.dataset.copy); if (m) navigator.clipboard.writeText(m.text.replace(/!\[[^\]]*\]\((image|diagram):[0-9a-f]+\)/g, "").trim()); return; }
  if (t.dataset.retry) { retry(); return; }
  if (t.dataset.edit) { const m = msgsOf(S.current).find(x => x.id === t.dataset.edit); if (!m) return; S.editing = m.id;
    $("#input").value = m.text.replace(PHOTO, "").trim(); autosize(); $("#input").focus(); notice("Editing a message: sending replaces it and the replies after it."); return; }
  if (t.hasAttribute("data-think")) { const b = t.nextElementSibling; b.style.display = b.style.display === "none" ? "block" : "none"; b.className = "think"; return; }
  const sum = t.closest("[data-sum]"); if (sum) { const m = msgsOf(S.current).find(x => x.id === sum.dataset.sum); const b = sum.querySelector(".sumbody");
    if (b.classList.contains("md")) { b.className = "sumbody sub"; b.textContent = NAME() + " continues from a summary of the messages above. Click to read it."; } else { b.className = "sumbody md"; b.innerHTML = md(m.text); } return; }
  if (t.tagName === "IMG") { const z = document.createElement("div"); z.className = "zoom"; z.innerHTML = `<img src="${t.src}">`; z.onclick = () => z.remove(); document.body.appendChild(z); }
};

async function start() {
  if (!KEY) { askKey(); return; }
  S.current = store.get("current", "") || null;
  renderBar(); renderMsgs();
  await sync();
  if (S.current && !S.convs.has(S.current)) S.current = null;
  await loadMeta();
  renderSide(); renderMsgs(); updateMeter();
}
setInterval(() => { if (KEY && !S.busy) sync(); }, 20000);
setInterval(() => { if (KEY) loadMeta(); }, 300000);
start();
</script>
</body>
</html>
"""  # end of web chat


# ----------------------------------------------------------------------------- HTTP API
def make_http_handler(cfg, chats):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def read_body(self, limit):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > limit:
                return None
            return self.rfile.read(n)

        def do_POST(self):
            path = urlparse(self.path).path
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})

            if path == "/transcribe":
                rate = int(self.headers.get("X-Sample-Rate") or 16000)
                pcm = self.read_body(MAX_SECONDS * rate * 2)
                if pcm is None:
                    return self.send_json(400, {"error": "send 16-bit mono PCM, up to 60 s"})
                seconds = len(pcm) / (rate * 2)
                started = time.time()
                try:
                    text = clean_transcript(transcribe(cfg.whisper, pcm_to_wav(pcm, rate), cfg.language))
                except (urllib.error.URLError, OSError, RuntimeError) as e:
                    log(f"whisper error: {e}")
                    return self.send_json(503, {"error": "speech recognition unavailable"})
                log(f'phone: {seconds:.1f}s -> "{text}" ({time.time() - started:.1f}s)')
                return self.send_json(200, {"text": text})

            if path == "/sync":
                body = self.read_body(20_000_000)
                try:
                    req = json.loads(body or b"{}")
                    result = chats.sync(int(req.get("since", 0)),
                                        req.get("conversations") or [], req.get("messages") or [])
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": f"bad sync request: {e}"})
                return self.send_json(200, result)

            if path == "/memories":
                try:
                    data = json.loads(self.read_body(10_000) or b"{}")
                    mid, new = chats.add_memory(data.get("text", ""), "user")
                except ValueError as e:
                    return self.send_json(400, {"error": str(e)})
                return self.send_json(200, {"id": mid, "new": new})

            if path == "/memories/learn":
                try:
                    data = json.loads(self.read_body(2_000_000) or b"{}")
                    started = time.time()
                    result = learn_memories(data.get("messages") or [])
                except (ValueError, KeyError, urllib.error.URLError, OSError) as e:
                    log(f"memory learning failed: {e}")
                    return self.send_json(503, {"error": f"couldn't learn from this chat: {e}"})
                if result["added"] or result["removed"]:
                    log(f"learned {result['added']} forgot {result['removed']} ({time.time() - started:.1f}s)")
                return self.send_json(200, result)

            if path == "/llm/v1/chat/completions":
                return self.proxy_llm()

            if path == "/context":
                try:
                    data = json.loads(self.read_body(5_000_000) or b"{}")
                    return self.send_json(200, plan_context(data))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": f"bad request: {e}"})

            if path == "/summarize":
                try:
                    data = json.loads(self.read_body(10_000_000) or b"{}")
                    started = time.time()
                    summary = summarize_chat(str(data.get("previous", "")), data.get("messages") or [],
                                             "story" if data.get("style") == "story" else "work")
                except (ValueError, TypeError, KeyError, IndexError, RuntimeError, urllib.error.URLError, OSError) as e:
                    log(f"summary failed: {e}")
                    return self.send_json(503, {"error": f"couldn't summarize: {e}"})
                log(f"refreshed a chat's context ({time.time() - started:.1f}s)")
                return self.send_json(200, {"summary": summary, "tokens": count_tokens(summary)})

            if path in ("/backups", "/backups/config", "/server/model", "/server/download"):
                try:
                    data = json.loads(self.read_body(20_000) or b"{}")
                    if path == "/backups":
                        return self.send_json(200, {"file": os.path.basename(backup_now()), **backups_status()})
                    if path == "/backups/config":
                        return self.send_json(200, save_backup_config(data))
                    if not SERVER_CONTROL:
                        return self.send_json(400, {"error": "this server can't switch models from the app"})
                    if path == "/server/model":
                        return self.send_json(200, {"message": SERVER_CONTROL.switch(str(data.get("key")))})
                    return self.send_json(200, {"message": SERVER_CONTROL.start_download(str(data.get("key")))})
                except (ValueError, RuntimeError, OSError, TypeError) as e:
                    return self.send_json(400, {"error": str(e)})

            if path == "/reminders" or path.startswith("/reminders/") or path.startswith("/lists/") \
                    or path == "/github":
                try:
                    data = json.loads(self.read_body(200_000) or b"{}")
                    if path == "/reminders":
                        due = int(data["due_at"]) if data.get("due_at") else _parse_when(data.get("when", ""))
                        return self.send_json(200, chats.add_reminder(data.get("text", ""), due, data.get("repeat", "none")))
                    if path == "/github":
                        return self.send_json(200, save_github_config(data))
                    m = re.fullmatch(r"/reminders/([0-9a-f]{10})", path)
                    if m:
                        r = chats.update_reminder(m.group(1), done=data.get("done"),
                                                  snooze_minutes=data.get("snooze_minutes"), cancel=bool(data.get("cancel")))
                        return self.send_json(200 if r else 404, r or {"error": "no such reminder"})
                    m = re.fullmatch(r"/lists/items/([0-9a-f]{10})", path)
                    if m:
                        return self.send_json(200, {"ok": chats.set_item(m.group(1), done=bool(data.get("done")))})
                    m = re.fullmatch(r"/lists/([^/]{1,60})/clear", path)
                    if m:
                        return self.send_json(200, {"removed": chats.clear_done(urllib.parse.unquote(m.group(1)))})
                    m = re.fullmatch(r"/lists/([^/]{1,60})", path)
                    if m:
                        items = data.get("items") or [data.get("text", "")]
                        name, added = chats.add_items(urllib.parse.unquote(m.group(1)), items)
                        return self.send_json(200, {"list": name, "added": added})
                    return self.send_json(404, {"error": "not found"})
                except (ValueError, TypeError, KeyError, AttributeError) as e:
                    return self.send_json(400, {"error": str(e)})

            if path == "/modes":
                try:
                    data = json.loads(self.read_body(100_000) or b"{}")
                    return self.send_json(200, chats.save_mode(data))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": str(e)})

            if path == "/projects":
                try:
                    data = json.loads(self.read_body(100_000) or b"{}")
                    return self.send_json(200, chats.save_project(data))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": str(e)})

            m = re.fullmatch(r"/projects/([0-9a-f]{12})/notes", path)
            if m:
                try:
                    data = json.loads(self.read_body(20_000) or b"{}")
                    return self.send_json(200, chats.add_project_note(m.group(1), data.get("text", ""), "user"))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json(400, {"error": str(e)})

            if path.startswith("/tools/"):
                name = path[len("/tools/"):]
                body = self.read_body(1_000_000)
                try:
                    args = json.loads(body or b"{}")
                except ValueError:
                    return self.send_json(400, {"error": "arguments must be JSON"})
                started = time.time()
                result, summary = run_server_tool(name, args)
                log(f"tool {name}: {summary} ({time.time() - started:.1f}s)")
                return self.send_json(200, {"result": result, "summary": summary})

            return self.send_json(404, {"error": "not found"})

        def proxy_llm(self):
            """Streams a chat request through to llama-server, so the browser only talks to this one address."""
            body = self.read_body(30_000_000)
            if not body:
                return self.send_json(400, {"error": "empty request"})
            req = urllib.request.Request(LLAMA_URL.rstrip("/") + "/v1/chat/completions", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
            try:
                resp = urllib.request.urlopen(req, timeout=600)
            except urllib.error.HTTPError as e:
                data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except (urllib.error.URLError, OSError) as e:
                return self.send_json(502, {"error": f"the AI model isn't running ({e})"})
            with resp:
                self.send_response(200)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "text/event-stream"))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the browser stopped the reply

        def log_message(self, *args):
            pass

        def authorized(self):
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and key_ok(auth[7:].strip(), cfg.key):
                return True
            qs = parse_qs(urlparse(self.path).query)
            return key_ok(qs.get("key", [""])[0], cfg.key)

        def send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, file, content_type):
            if not os.path.isfile(file):
                return self.send_json(404, {"error": "not found"})
            with open(file, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):
            path = urlparse(self.path).path
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})
            m = re.fullmatch(r"/projects/(library|[0-9a-f]{12})/files", path)
            if m:
                name = parse_qs(urlparse(self.path).query).get("name", ["file"])[0]
                data = self.read_body(MAX_PROJECT_FILE)
                if not data:
                    return self.send_json(400, {"error": "send a file under 25 MB"})
                started = time.time()
                try:
                    fid = add_project_file(m.group(1), name, data)
                except (ValueError, OSError, subprocess.SubprocessError) as e:
                    return self.send_json(400, {"error": str(e)})
                log(f"project file {name}: {len(data) // 1024} KB ({time.time() - started:.1f}s)")
                return self.send_json(200, {"id": fid, "project": chats.project(m.group(1))})
            m = re.fullmatch(r"/images/([0-9a-f]{16})\.jpg", path)
            if not m:
                return self.send_json(404, {"error": "not found"})
            data = self.read_body(8_000_000)
            if not data or not data.startswith(b"\xff\xd8"):
                return self.send_json(400, {"error": "send a JPEG under 8 MB"})
            file = os.path.join(IMAGE_DIR, m.group(1) + ".jpg")
            if not os.path.exists(file):
                with open(file + ".part", "wb") as f:
                    f.write(data)
                os.replace(file + ".part", file)
            return self.send_json(200, {"ok": True})

        def do_DELETE(self):
            path = urlparse(self.path).path
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})
            m = re.fullmatch(r"/modes/(c-[0-9a-f]{10})", path)
            if m:
                return self.send_json(200, {"deleted": chats.delete_mode(m.group(1))})
            m = re.fullmatch(r"/lists/items/([0-9a-f]{10})", path)
            if m:
                return self.send_json(200, {"deleted": chats.set_item(m.group(1), delete=True)})
            m = re.fullmatch(r"/projects/(library)/(files)/([0-9a-f]{1,32})", path) or \
                re.fullmatch(r"/projects/([0-9a-f]{12})(?:/(notes|files)/([0-9a-f]{1,32}))?", path)
            if m:
                pid, what, item = m.groups()
                if what == "notes":
                    return self.send_json(200, {"deleted": chats.delete_project_note(pid, item)})
                if what == "files":
                    return self.send_json(200, {"deleted": chats.delete_project_file(pid, item)})
                return self.send_json(200, {"deleted": chats.delete_project(pid)})
            m = re.fullmatch(r"/memories/([0-9a-f]{1,32})", path)
            if not m:
                return self.send_json(404, {"error": "not found"})
            return self.send_json(200, {"deleted": chats.delete_memory(m.group(1))})

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                return self.send_json(200, {"ok": True})
            if path in ("/", "/index.html", "/chat"):
                body = WEBCHAT_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; "
                                 "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                 "connect-src 'self'; frame-ancestors 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/favicon.ico":
                return self.send_json(404, {"error": "none"})
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})
            if path == "/tools":
                return self.send_json(200, {"tools": [t[1] for t in SERVER_TOOLS.values()],
                                            "approval": sorted(APPROVAL_TOOLS)})
            if path == "/reminders":
                return self.send_json(200, {"reminders": chats.reminders(), "now": int(time.time() * 1000)})
            if path == "/lists":
                return self.send_json(200, {"lists": chats.lists()})
            if path == "/github":
                return self.send_json(200, github_status())
            if path == "/search":
                q = parse_qs(urlparse(self.path).query)
                return self.send_json(200, {"results": CHAT_SEARCH.search(q.get("q", [""])[0], k=20,
                                                                           exclude=q.get("exclude", [""])[0])})
            if path == "/doctor":
                report = run_doctor()
                if parse_qs(urlparse(self.path).query).get("format", [""])[0] == "text":
                    body = doctor_text(report).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self.send_json(200, report)
            if path == "/backups":
                return self.send_json(200, backups_status())
            if path == "/server":
                return self.send_json(200, server_status())
            if path == "/memories":
                return self.send_json(200, {"memories": chats.memories()})
            if path == "/projects":
                return self.send_json(200, {"projects": chats.projects()})
            if path == "/modes":
                return self.send_json(200, {"modes": chats.modes()})
            if path == "/library":
                return self.send_json(200, chats.library())
            m = re.fullmatch(r"/projects/(library|[0-9a-f]{12})/files/([0-9a-f]{16})\.txt", path)
            if m:
                return self.send_file(os.path.join(PROJECT_DIR, m.group(2) + ".txt"), "text/plain; charset=utf-8")
            if path == "/wakewords":
                names = sorted(f[:-5] for f in os.listdir(WAKEWORD_DIR) if f.endswith(".onnx")
                               and WAKEWORD_NAME.fullmatch(f[:-5]) and ".." not in f)
                return self.send_json(200, {"wakewords": [
                    {"id": n, "label": " ".join(w.capitalize() for w in re.split(r"[_-]+", re.sub(r"_v\d.*$", "", n)) if w)}
                    for n in names]})
            m = re.fullmatch(r"/wakewords/(.+)\.onnx", path)
            if m and WAKEWORD_NAME.fullmatch(m.group(1)) and ".." not in m.group(1):
                return self.send_file(os.path.join(WAKEWORD_DIR, m.group(1) + ".onnx"), "application/octet-stream")
            m = re.fullmatch(r"/images/([0-9a-f]{16})\.jpg", path)
            if m:
                return self.send_file(os.path.join(IMAGE_DIR, m.group(1) + ".jpg"), "image/jpeg")
            m = re.fullmatch(r"/diagrams/([0-9a-f]{16})\.png", path)
            if m:
                file = os.path.join(DIAGRAM_DIR, m.group(1) + ".png")
                if not os.path.exists(file):
                    return self.send_json(404, {"error": "no such diagram"})
                with open(file, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(data)
                return
            return self.send_json(404, {"error": "not found"})

    for name in ("do_GET", "do_POST", "do_PUT", "do_DELETE"):
        setattr(Handler, name, _tracked(getattr(Handler, name)))
    return Handler


# ----------------------------------------------------------------------------- main
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="StellAI hub: speech-to-text and chat sync")
    p.add_argument("--key-file", required=True, help="file containing the API key")
    p.add_argument("--app-port", type=int, default=8092)
    p.add_argument("--whisper", default="http://127.0.0.1:8093", help="whisper-server URL")
    p.add_argument("--language", default="en", help="spoken language code, or empty for auto")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--db", default="chats.db", help="SQLite file for synced chats")
    p.add_argument("--diagram-dir", default="diagrams", help="where rendered diagrams are kept")
    p.add_argument("--diagram-python", default=sys.executable, help="Python with schemdraw + matplotlib")
    p.add_argument("--mmdc", default="mmdc", help="Mermaid CLI command")
    p.add_argument("--llama", default="http://127.0.0.1:8080", help="llama-server URL (for memory learning)")
    p.add_argument("--image-dir", default="images", help="where photos from chats are kept")
    p.add_argument("--wakeword-dir", default="wakewords", help="custom wake word models (*.onnx) for the app")
    p.add_argument("--project-dir", default="", help="project files (default: next to the chat database)")
    p.add_argument("--backup-dir", default="", help="where nightly backups go (default: backups next to the database)")
    p.add_argument("--model-conf", default="", help="Linux launcher's model.conf (enables switching models from the app)")
    p.add_argument("--models-dir", default="", help="folder with the .gguf models")
    p.add_argument("--llama-pid", default="", help="file with llama-server's process id")
    p.add_argument("--restore", default="", help="restore this backup zip (with the server stopped) and exit")
    p.add_argument("--searxng", default="", help="SearXNG URL for web search, e.g. http://127.0.0.1:8888 "
                                                 "(optional; DuckDuckGo is used otherwise)")
    p.add_argument("--github-file", default="", help="where the GitHub token/settings are kept "
                                                     "(default: github.json next to the chat database)")
    p.add_argument("--embed", default="", help="embedding llama-server URL for meaning-based search, e.g. "
                                               "http://127.0.0.1:8094 (optional)")
    p.add_argument("--update-repo", default="", help="owner/repo to self-update from every 30 minutes (Linux)")
    p.add_argument("--update-token-file", default="", help="GitHub token for a private update repo")
    return p.parse_args(argv)


def start(cfg):
    """Set up storage and return a ready (not yet serving) HTTP server. Used by main() and the Windows app."""
    with open(cfg.key_file) as f:
        cfg.key = f.read().strip()
    if not cfg.key:
        raise SystemExit("API key file is empty")

    chats = ChatDB(cfg.db)
    global DIAGRAM_DIR, PY_FOR_DIAGRAMS, MMDC, CHATS, LLAMA_URL, API_KEY, IMAGE_DIR, WAKEWORD_DIR, PROJECT_DIR
    CHATS, LLAMA_URL, API_KEY = chats, cfg.llama, cfg.key
    PROJECT_DIR = os.path.abspath(cfg.project_dir or os.path.join(os.path.dirname(os.path.abspath(cfg.db)),
                                                                  "projects"))
    os.makedirs(PROJECT_DIR, exist_ok=True)
    IMAGE_DIR = os.path.abspath(cfg.image_dir)
    WAKEWORD_DIR = os.path.abspath(cfg.wakeword_dir)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(WAKEWORD_DIR, exist_ok=True)
    DIAGRAM_DIR = os.path.abspath(cfg.diagram_dir)
    os.makedirs(DIAGRAM_DIR, exist_ok=True)
    PY_FOR_DIAGRAMS, MMDC = cfg.diagram_python, cfg.mmdc
    refresh_diagram_tool()
    EMBEDDER.start(getattr(cfg, "embed", ""))
    global SEARXNG_URL, GITHUB_FILE, CHATS_PATH, SERVER_CONTROL
    CHATS_PATH = os.path.abspath(cfg.db)
    base = os.path.dirname(CHATS_PATH)
    BACKUP["dir"] = os.path.abspath(getattr(cfg, "backup_dir", "") or os.path.join(base, "backups"))
    BACKUP["config"] = os.path.join(base, "backup.json")
    BACKUP["paths"] = {"projects": PROJECT_DIR, "images": IMAGE_DIR, "diagrams": DIAGRAM_DIR, "wakewords": WAKEWORD_DIR}
    DOCTOR["whisper"] = cfg.whisper
    if getattr(cfg, "model_conf", "") and getattr(cfg, "models_dir", ""):
        SERVER_CONTROL = LinuxServerControl(cfg.model_conf, cfg.models_dir, cfg.llama_pid)
    SEARXNG_URL = getattr(cfg, "searxng", "") or ""
    GITHUB_FILE = os.path.abspath(getattr(cfg, "github_file", "") or
                                  os.path.join(os.path.dirname(os.path.abspath(cfg.db)), "github.json"))
    global WEB
    web = ThreadingHTTPServer((cfg.host, cfg.app_port), make_http_handler(cfg, chats))
    web.daemon_threads = True
    WEB = web
    CHAT_SEARCH.start()
    if not BACKUP.get("loop"):
        BACKUP["loop"] = True
        threading.Thread(target=backup_loop, daemon=True, name="backups").start()
    log(f"hub ready on :{cfg.app_port}, chats in {cfg.db}"
        + ("" if mermaid_command() else " (Mermaid not installed: flowcharts use schemdraw)"))
    return web


def main():
    if "--self-test" in sys.argv:
        self_test()
        return
    cfg = parse_args()
    if cfg.restore:
        with open(cfg.key_file) as f:
            pass
        print(restore_backup(cfg.restore, cfg))
        return
    web = start(cfg)
    threading.Thread(target=ensure_packages, daemon=True).start()
    if cfg.update_repo:
        token = ""
        if cfg.update_token_file:
            try:
                with open(cfg.update_token_file) as f:
                    token = f.read().strip()
            except OSError:
                pass
        threading.Thread(target=updater_loop, args=(cfg.update_repo, token), daemon=True).start()
        log(f"auto-update: checking github.com/{cfg.update_repo} every 30 minutes")
        DOCTOR["update"] = f"automatic, from github.com/{cfg.update_repo}"
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        web.server_close()
        CHATS.db.close()
    sys.exit(EXIT_CODE)


if __name__ == "__main__":
    main()
PYEOF
chmod +x "$LLM_DIR/stellai_hub.py"
rm -f "$LLM_DIR/voice_bridge.py" "$LLM_DIR/laptop_hub.py"   # older versions

# 4. Download the model --------------------------------------------------------
if [[ ! -s "$MODEL_FILE" ]]; then
  say "Downloading $MODEL_LABEL ($MODEL_NAME) - this can take a while"
  curl -L --fail --continue-at - -o "$MODEL_FILE.part" "$MODEL_URL"
  mv "$MODEL_FILE.part" "$MODEL_FILE"
else
  say "Model already downloaded"
fi

# 4b. Vision (photo understanding): Qwen3.5's image encoder, used with --mmproj
MMPROJ_FILE="$LLM_DIR/models/${MODEL_NAME%.gguf}.mmproj-F16.gguf"
MMPROJ_URL="https://huggingface.co/$MODEL_REPO/resolve/main/mmproj-F16.gguf"
VISION_ARGS=""
if [[ ! -s "$MMPROJ_FILE" ]]; then
  echo
  read -r -p "Let Stella understand photos? Downloads the vision add-on (~0.6-0.9 GB) [Y/n] " vis
  if [[ "${vis,,}" != "n" ]]; then
    say "Downloading the vision add-on for $MODEL_LABEL"
    if curl -L --fail --continue-at - -o "$MMPROJ_FILE.part" "$MMPROJ_URL"; then
      mv "$MMPROJ_FILE.part" "$MMPROJ_FILE"
    else
      rm -f "$MMPROJ_FILE.part"
      warn "Couldn't download the vision add-on; photos won't work (everything else will)"
    fi
  fi
fi
if [[ -s "$MMPROJ_FILE" ]]; then VISION_ARGS="--mmproj $MMPROJ_FILE"; fi

# 5. Launcher ------------------------------------------------------------------
# Remember what's installed, for reference
# (the server launcher re-reads this on every start, and the phone app can switch models by rewriting it)
printf 'MODEL_LABEL="%s"\nMODEL_FILE="%s"\nGPU_ARGS="%s"\nBACKEND="%s"\nNET_CONTEXT="%s"\nMMPROJ_FILE="%s"\nWHISPER_GPU="%s"\n' \
  "$MODEL_LABEL" "$MODEL_FILE" "$GPU_ARGS" "$BACKEND" "$NET_CONTEXT" "$([[ -s "$MMPROJ_FILE" ]] && echo "$MMPROJ_FILE")" \
  "${WHISPER_GPU:-0}" > "$LLM_DIR/model.conf"

# GPU memory left free by --fit for things it doesn't know about: the vision add-on and GPU Whisper.
FIT_ARGS=""
if [[ $BACKEND != cpu ]]; then
  FIT_MARGIN=1024
  [[ -n "$VISION_ARGS" ]] && FIT_MARGIN=$((FIT_MARGIN + 1000))
  [[ ${WHISPER_GPU:-0} -eq 1 ]] && FIT_MARGIN=$((FIT_MARGIN + 700))
  FIT_ARGS="--fit-target $FIT_MARGIN"
fi

# Cap thinking so "thinking mode" answers can't spend the whole context on reasoning (--reasoning-budget).
# (--cache-reuse isn't used: Qwen3.5's hybrid layers can't shift their cache; llama-server's automatic
#  context checkpoints plus StellAI's unchanging prompt start do that job instead.)
if [[ ${NET_CONTEXT:-$CONTEXT} -le 8192 ]]; then THINK_BUDGET=1024; else THINK_BUDGET=2048; fi

say "Creating launcher"
cat > "$LLM_DIR/start-chat.sh" <<EOF
#!/usr/bin/env bash
# Starts the local chat server and opens it in the browser.
# THREADS: try 2 vs 4 and keep whichever is faster.
THREADS=\${THREADS:-$PHYS_CORES}

set_gov() { for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo "\$1" | sudo tee "\$g" >/dev/null 2>&1; done; }
echo "Setting CPU to performance mode (may ask for your password)..."
set_gov performance || true
trap 'echo; echo "Restoring power-saving mode..."; set_gov powersave || true' EXIT

( sleep 6; xdg-open "http://127.0.0.1:$PORT" >/dev/null 2>&1 ) &

"$LLM_DIR/llama.cpp/build/bin/llama-server" \\
  -m "$MODEL_FILE" \\
  -c $CONTEXT \\
  -t "\$THREADS" -tb $LOGICAL_CPUS $GPU_ARGS $FIT_ARGS $VISION_ARGS \\
  -fa on \\
  --reasoning-budget $THINK_BUDGET --reasoning-budget-message "I have thought enough; time to answer." \\
  --cache-type-k q8_0 --cache-type-v q8_0 \\
  --jinja \\
  --host 127.0.0.1 --port $PORT
EOF
chmod +x "$LLM_DIR/start-chat.sh"

mkdir -p "$HOME/.local/share/applications"
rm -f "$HOME/.local/share/applications/local-llm.desktop" "$HOME/.local/share/applications/local-llm-network.desktop"
cat > "$HOME/.local/share/applications/stellai-chat.desktop" <<EOF
[Desktop Entry]
Name=StellAI Chat
Comment=$MODEL_LABEL running on llama.cpp
Exec=$LLM_DIR/start-chat.sh
Icon=utilities-terminal
Terminal=true
Type=Application
Categories=Utility;
EOF

# 5b. Network launcher (lets the Android app connect over Wi-Fi) --------------
say "Creating phone/network launcher"
KEY_FILE="$LLM_DIR/api-key.txt"
if [[ ! -s "$KEY_FILE" ]]; then
  NEWKEY=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
  printf '%s' "${NEWKEY:0:32}" > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
fi

cat > "$LLM_DIR/start-server-network.sh" <<EOF
#!/usr/bin/env bash
# Serves the model on your local network so the Android app can use it.
# Protected by the API key in $KEY_FILE
THREADS=\${THREADS:-$PHYS_CORES}
CONTEXT=\${CONTEXT:-$NET_CONTEXT}
KEY=\$(cat "$KEY_FILE")
IP=\$(hostname -I | awk '{print \$1}')
TS_IP=\$(tailscale ip -4 2>/dev/null | head -1)

if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  sudo ufw allow $PORT/tcp >/dev/null
  sudo ufw allow 8092/tcp >/dev/null
fi

set_gov() { for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo "\$1" | sudo tee "\$g" >/dev/null 2>&1; done; }
set_gov performance || true

# Hub: chat sync + voice (always), Whisper speech recognition (if installed)
HAS_WHISPER=""
HUB_PY="$LLM_DIR/venv/bin/python"
[[ -x "\$HUB_PY" ]] || HUB_PY=python3
# Auto-update: the hub checks GitHub every 30 minutes and updates itself when Stella is idle.
# Turn it off with: touch $LLM_DIR/no-auto-update
UPDATE_ARGS=()
[[ -f "$LLM_DIR/no-auto-update" ]] || UPDATE_ARGS=(--update-repo "$STELLAI_REPO")
[[ -f "$LLM_DIR/github-token.txt" ]] && UPDATE_ARGS+=(--update-token-file "$LLM_DIR/github-token.txt")
# Meaning-based file search (embedding model on the CPU) and private web search (SearXNG in Docker)
EXTRA_ARGS=()
if [[ -s "$EMBED_MODEL" ]]; then
  "$LLM_DIR/llama.cpp/build/bin/llama-server" -m "$EMBED_MODEL" --embedding --pooling mean -ngl 0 \\
    -c 8192 -b 2048 -ub 2048 -np 1 -t 2 --host 127.0.0.1 --port 8094 > "$LLM_DIR/embed.log" 2>&1 &
  EMBED_PID=\$!
  EXTRA_ARGS+=(--embed http://127.0.0.1:8094)
fi
if [[ -f "$LLM_DIR/searxng/settings.yml" ]]; then
  sudo -n docker start stellai-searxng >/dev/null 2>&1 || docker start stellai-searxng >/dev/null 2>&1 || true
  EXTRA_ARGS+=(--searxng http://127.0.0.1:8888)
fi
hub_loop() {
  local child code started after_update=0
  trap 'kill "\$child" 2>/dev/null; exit 0' TERM INT
  while true; do
    started=\$(date +%s)
    "\$HUB_PY" "$LLM_DIR/stellai_hub.py" --key-file "$KEY_FILE" --db "$LLM_DIR/chats.db" \\
      --diagram-dir "$LLM_DIR/diagrams" --mmdc "$LLM_DIR/mermaid/node_modules/.bin/mmdc" \\
      --image-dir "$LLM_DIR/images" --wakeword-dir "$LLM_DIR/wakewords" --llama "http://127.0.0.1:$PORT" \\
      --model-conf "$LLM_DIR/model.conf" --models-dir "$LLM_DIR/models" --llama-pid "$LLM_DIR/llama.pid" \\
      "\${UPDATE_ARGS[@]}" "\${EXTRA_ARGS[@]}" &
    child=\$!
    wait "\$child"; code=\$?
    if [[ \$code -eq 75 ]]; then after_update=1; echo "StellAI hub updated, restarting it"; continue; fi
    [[ \$code -eq 0 ]] && break
    if [[ \$after_update -eq 1 && \$(( \$(date +%s) - started )) -lt 60 && -f "$LLM_DIR/stellai_hub.py.bak" ]]; then
      echo "The updated hub failed to start: going back to the previous version"
      sha256sum "$LLM_DIR/stellai_hub.py" | cut -d' ' -f1 >> "$LLM_DIR/stellai_hub.bad"
      mv -f "$LLM_DIR/stellai_hub.py.bak" "$LLM_DIR/stellai_hub.py"
    fi
    after_update=0
    sleep 3
  done
}
hub_loop &
HELPER_PIDS="\$! \${EMBED_PID:-}"
if [[ -x "$LLM_DIR/whisper.cpp/build/bin/whisper-server" && -s "$WHISPER_MODEL" ]]; then
  "$LLM_DIR/whisper.cpp/build/bin/whisper-server" -m "$WHISPER_MODEL" -t "\$THREADS" -bo 1 \\
    --host 127.0.0.1 --port 8093 > "$LLM_DIR/whisper.log" 2>&1 &
  HELPER_PIDS="\$HELPER_PIDS \$!"
  HAS_WHISPER=1
fi
trap 'kill \$HELPER_PIDS 2>/dev/null; set_gov powersave || true' EXIT

echo "================================================================"
echo "  StellAI Server - enter these in the phone app's settings:"
if [[ -n "\$TS_IP" ]]; then
echo "  Server:   http://\$TS_IP:$PORT   (Tailscale: works at home and away)"
echo "            http://\$IP:$PORT   (home Wi-Fi only)"
else
echo "  Server:   http://\$IP:$PORT   (same Wi-Fi only; install Tailscale for away)"
fi
echo "  API key:  \$KEY"
echo "  Desktop chat (any browser): http://\${TS_IP:-\$IP}:8092   (this PC: StellAI Web Chat in the menu)"
if [[ -z "\$HAS_WHISPER" ]]; then
echo "  (voice commands disabled: Whisper not installed - re-run the setup script)"
fi
echo "================================================================"

# Stella's model. model.conf is re-read on every start, so the phone app can switch models: it rewrites
# model.conf, creates .switch-model and stops llama-server, and this loop starts the new model.
rm -f "$LLM_DIR/.switch-model"
while true; do
  source "$LLM_DIR/model.conf"
  CTX=\${NET_CONTEXT:-\$CONTEXT}
  VIS=""; FIT=""
  if [[ -n "\${MMPROJ_FILE:-}" && -s "\${MMPROJ_FILE:-}" ]]; then VIS="--mmproj \$MMPROJ_FILE"; fi
  if [[ "\$BACKEND" != cpu ]]; then
    M=1024; [[ -n "\$VIS" ]] && M=\$((M + 1000)); [[ "\${WHISPER_GPU:-0}" == 1 ]] && M=\$((M + 700))
    FIT="--fit-target \$M"
  fi
  if [[ \$CTX -le 8192 ]]; then TB=1024; else TB=2048; fi
  "$LLM_DIR/llama.cpp/build/bin/llama-server" -m "\$MODEL_FILE" -c "\$CTX" \\
    -t "\$THREADS" -tb $LOGICAL_CPUS \$GPU_ARGS \$FIT \$VIS -fa on \\
    --reasoning-budget \$TB --reasoning-budget-message "I have thought enough; time to answer." \\
    --cache-type-k q8_0 --cache-type-v q8_0 --jinja --api-key "\$KEY" --host 0.0.0.0 --port $PORT &
  LLAMA_PID=\$!
  echo \$LLAMA_PID > "$LLM_DIR/llama.pid"
  wait \$LLAMA_PID
  if [[ -f "$LLM_DIR/.switch-model" ]]; then
    rm -f "$LLM_DIR/.switch-model"; echo "Switching to \$(sed -n 's/^MODEL_LABEL="\(.*\)"/\1/p' "$LLM_DIR/model.conf")..."
    sleep 2; continue
  fi
  break
done
EOF
chmod +x "$LLM_DIR/start-server-network.sh"

# Health check and restore helpers
HUB_RUN_PY="$LLM_DIR/venv/bin/python"; [[ -x "$HUB_RUN_PY" ]] || HUB_RUN_PY=python3
cat > "$LLM_DIR/stellai-doctor.sh" <<EOF
#!/usr/bin/env bash
# Checks every part of StellAI and says what to do about problems.
KEY=\$(cat "$KEY_FILE")
if ! curl -fsS -m 60 -H "Authorization: Bearer \$KEY" "http://127.0.0.1:8092/doctor?format=text"; then
  echo "StellAI Server isn't running (nothing answers on port 8092)."
  echo "  -> Start it from the menu (StellAI Server). If it closes right away, run $LLM_DIR/start-server-network.sh"
  echo "     in a terminal to see the error."
fi
echo
read -r -p "Press Enter to close. " _
EOF
chmod +x "$LLM_DIR/stellai-doctor.sh"
cat > "$HOME/.local/share/applications/stellai-doctor.desktop" <<EOF
[Desktop Entry]
Name=StellAI Health Check
Comment=Check every part of StellAI
Exec=$LLM_DIR/stellai-doctor.sh
Icon=utilities-system-monitor
Terminal=true
Type=Application
Categories=Utility;
EOF
cat > "$LLM_DIR/stellai-restore.sh" <<EOF
#!/usr/bin/env bash
# Puts a backup back: ./stellai-restore.sh [backup.zip]   (close StellAI Server first)
set -e
ZIP="\${1:-}"
if [[ -z "\$ZIP" ]]; then
  echo "Recent backups:"; ls -1t "$LLM_DIR/backups"/stellai-backup-*.zip 2>/dev/null | head -10
  read -r -p "Backup to restore (Enter = newest): " ZIP
  ZIP="\${ZIP:-\$(ls -1t "$LLM_DIR/backups"/stellai-backup-*.zip | head -1)}"
fi
if curl -fsS -m 3 http://127.0.0.1:8092/health >/dev/null 2>&1; then
  echo "Close StellAI Server first (Ctrl+C in its window), then run this again."; exit 1
fi
"$HUB_RUN_PY" "$LLM_DIR/stellai_hub.py" --restore "\$ZIP" --key-file "$KEY_FILE" --db "$LLM_DIR/chats.db" \\
  --diagram-dir "$LLM_DIR/diagrams" --image-dir "$LLM_DIR/images" --wakeword-dir "$LLM_DIR/wakewords"
echo "Done. Start StellAI Server again; your phones re-sync automatically."
EOF
chmod +x "$LLM_DIR/stellai-restore.sh"

cat > "$HOME/.local/share/applications/stellai-server.desktop" <<EOF
[Desktop Entry]
Name=StellAI Server
Comment=Serve $MODEL_LABEL to the phone app (AI, voice, chat sync)
Exec=$LLM_DIR/start-server-network.sh
Icon=network-server
Terminal=true
Type=Application
Categories=Utility;
EOF

# Desktop web chat: same chats as the phone, in any browser
cat > "$LLM_DIR/open-web-chat.sh" <<EOF
#!/usr/bin/env bash
# Opens the StellAI desktop chat (start "StellAI Server" first). The key stays in the link on this PC.
xdg-open "http://127.0.0.1:8092/#key=\$(cat "$KEY_FILE")"
EOF
chmod +x "$LLM_DIR/open-web-chat.sh"
cat > "$HOME/.local/share/applications/stellai-web-chat.desktop" <<EOF
[Desktop Entry]
Name=StellAI Web Chat
Comment=Chat with Stella in the browser, synced with your phone (needs StellAI Server running)
Exec=$LLM_DIR/open-web-chat.sh
Icon=internet-chat
Terminal=false
Type=Application
Categories=Utility;
EOF

# 5c. Optional: Tailscale (use the phone app away from home) ------------------
echo
if command -v tailscale >/dev/null; then
  say "Tailscale already installed: $(tailscale ip -4 2>/dev/null | head -1)"
else
  read -r -p "Install Tailscale so the phone app works away from home? [Y/n] " ts
  if [[ "${ts,,}" != "n" ]]; then
    say "Installing Tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
    say "Sign in to Tailscale (a browser link will appear)"
    sudo tailscale up || warn "Tailscale sign-in didn't finish; run 'sudo tailscale up' later"
  fi
fi

# 5d. Optional: private web search (SearXNG in Docker) ------------------------
echo
if [[ -f "$LLM_DIR/searxng/settings.yml" ]]; then
  say "Web search (SearXNG) already set up"
else
  read -r -p "Install private web search for Stella (SearXNG in Docker, ~300 MB)? Without it she uses DuckDuckGo. [Y/n] " sx
  if [[ "${sx,,}" != "n" ]]; then
    say "Installing SearXNG"
    if ! command -v docker >/dev/null; then sudo apt install -y docker.io || warn "Couldn't install Docker"; fi
    sudo systemctl enable --now docker >/dev/null 2>&1 || true
    mkdir -p "$LLM_DIR/searxng"
    cat > "$LLM_DIR/searxng/settings.yml" <<EOF
use_default_settings: true
server:
  secret_key: "$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
  limiter: false
  image_proxy: false
search:
  safe_search: 0
  formats:
    - html
    - json
EOF
    sudo docker rm -f stellai-searxng >/dev/null 2>&1 || true
    if sudo docker run -d --name stellai-searxng --restart unless-stopped -p 127.0.0.1:8888:8080 \
        -v "$LLM_DIR/searxng:/etc/searxng" searxng/searxng:latest; then
      say "SearXNG is running on this PC only (127.0.0.1:8888) and starts with the computer"
    else
      warn "SearXNG didn't start; Stella will use DuckDuckGo. Retry by re-running this script."
      rm -f "$LLM_DIR/searxng/settings.yml"
    fi
  fi
fi

# 6. Optional: Lenovo throttling fix ------------------------------------------
ans=n
if grep -qi thinkpad /sys/class/dmi/id/product_version 2>/dev/null; then
  echo
  read -r -p "Install 'throttled' (fixes early CPU throttling on ThinkPads)? [y/N] " ans
fi
if [[ "${ans,,}" == "y" ]]; then
  say "Installing throttled"
  sudo apt install -y python3-dev python3-venv python3-wheel libdbus-glib-1-dev \
    libgirepository1.0-dev libcairo2-dev python3-cairo-dev || \
    warn "Some throttled dependencies failed; install may not work"
  if [[ ! -d "$LLM_DIR/throttled" ]]; then
    git clone https://github.com/erpalma/throttled "$LLM_DIR/throttled"
  fi
  sudo "$LLM_DIR/throttled/install.sh" || warn "throttled install failed (see its GitHub README; Secure Boot can block it)"
fi

say "Done!"
cat <<EOF

  Start chatting:  open "StellAI Chat" from your menu,
                   or run  $LLM_DIR/start-chat.sh
  Chat UI:         http://127.0.0.1:$PORT
  Speed test:      $LLM_DIR/llama.cpp/build/bin/llama-bench -m "$MODEL_FILE" -t 2,4
  Try threads:     THREADS=2 $LLM_DIR/start-chat.sh
  Desktop chat:    open "StellAI Web Chat" (with StellAI Server running),
                   or browse to http://THIS-PC:8092 from any computer
  Health check:    "StellAI Health Check" in the menu, or $LLM_DIR/stellai-doctor.sh
  Backups:         nightly into $LLM_DIR/backups (14 kept); restore with $LLM_DIR/stellai-restore.sh
  Phone app mode:  open "StellAI Server" from your menu,
                   or run  $LLM_DIR/start-server-network.sh
                   (also starts chat sync and the voice services)

  Custom wake words: put trained .onnx files (e.g. hey_stella.onnx) in
                   $LLM_DIR/wakewords - they appear in the app's wake word list.

  Tip: close your browser tabs/other apps before chatting to free RAM.
EOF
