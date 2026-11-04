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
    MODEL_REPO="unsloth/Qwen3.5-35B-A3B-GGUF"; MODEL_NAME="Qwen3.5-35B-A3B-UD-IQ3_S.gguf"
    CONTEXT=16384; NET_CONTEXT=16384
    # Experts stay in RAM; everything else goes on the GPU (if there is one).
    if [[ $VRAM_GB -ge 4 ]]; then GPU_ARGS="-ngl 99 --cpu-moe"; fi
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
say "llama.cpp built with: $BACKEND"

# 3b. Build whisper.cpp (speech-to-text for the phone's voice commands) -------
say "Building whisper.cpp for voice dictation (~5-10 min)"
if [[ -d "$LLM_DIR/whisper.cpp/.git" ]]; then
  git -C "$LLM_DIR/whisper.cpp" pull --ff-only
else
  git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$LLM_DIR/whisper.cpp"
fi
cd "$LLM_DIR/whisper.cpp"
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON
cmake --build build --config Release -j"$(nproc)" --target whisper-server

if [[ ! -s "$WHISPER_MODEL" ]]; then
  say "Downloading Whisper speech model $WHISPER_NAME ($WHISPER_SIZE)"
  curl -L --fail --continue-at - -o "$WHISPER_MODEL" "$WHISPER_URL"
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
import sqlite3
import sys
import threading
import time
import urllib.error
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
            if not self.db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
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
            self.db.commit()
            self._bump(pid)
        if cur.rowcount:
            _remove_project_file(fid)
        return cur.rowcount > 0

    def project_chunks(self, pid):
        with self.lock:
            return self.db.execute(
                """SELECT c.file_id, f.name, c.idx, f.chunks, c.text FROM project_chunks c
                   JOIN project_files f ON f.id = c.file_id WHERE f.project_id=? ORDER BY f.created_at, c.idx""",
                (pid,)).fetchall()


class ChatDB(ProjectStoreMixin):
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
            CREATE INDEX IF NOT EXISTS note_proj ON project_notes(project_id);
            CREATE INDEX IF NOT EXISTS file_proj ON project_files(project_id);
            """
        )
        ccols = {r[1] for r in self.db.execute("PRAGMA table_info(conversations)")}
        if "project_id" not in ccols:
            self.db.execute("ALTER TABLE conversations ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
        self.project_versions = {}
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
                row = self.db.execute("SELECT updated_at, project_id FROM conversations WHERE id=?", (cid,)).fetchone()
                if row and row[0] >= upd:
                    continue
                deleted = 1 if c.get("deleted") else 0
                # Older apps don't send project_id: keep what the server has
                project = str(c.get("project_id") or "")[:64] if "project_id" in c else (row[1] if row else "")
                self.db.execute(
                    """INSERT OR REPLACE INTO conversations
                       (id, title, created_at, updated_at, deleted, seq, project_id) VALUES(?,?,?,?,?,?,?)""",
                    (cid, str(c.get("title", ""))[:200], self._ts(c.get("created_at", upd)), upd, deleted,
                     self._next(), project),
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
            self.db.commit()

            rows = self.db.execute(
                """SELECT 'c', id, title, created_at, updated_at, deleted, project_id, NULL, NULL, NULL, seq
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
                              "updated_at": updated, "deleted": bool(deleted), "project_id": conv_id or ""})
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


MERMAID_HINT = ("kind='mermaid' for block diagrams, flowcharts, state machines and sequence diagrams "
                "(Mermaid syntax). ")
NO_MERMAID_HINT = ("For block diagrams and flowcharts use kind='schemdraw' with schemdraw.flow "
                   "('import schemdraw.flow as flow'; flow.Start, flow.Box, flow.Decision, flow.Arrow). ")


DIAGRAM_DESC = (
    "Draw a diagram and show it to the user. kind='schemdraw' for circuit schematics (Python code using "
    "schemdraw: 'import schemdraw' and 'import schemdraw.elements as elm', build 'with schemdraw.Drawing() as d:' "
    "and add elements with 'd += elm.Resistor().label(\"10kΩ\")' etc.; no other imports or file access). "
    "{MERMAID}"
    "kind='wavedrom' for digital timing diagrams (WaveJSON, e.g. {\"signal\":[{\"name\":\"clk\",\"wave\":\"p......\"}]})."
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


def _summarize_once(previous, convo, max_tokens=700):
    user = (f"Summary so far:\n{previous}\n\n" if previous else "") + f"Conversation to add:\n{convo}"
    body = {"model": "local", "temperature": 0.2, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": user}],
            "chat_template_kwargs": {"enable_thinking": False}}
    out = _llama("/v1/chat/completions", body, timeout=300)["choices"][0]["message"].get("content") or ""
    out = re.sub(r"<think>[\s\S]*?</think>", "", out).strip()
    if not out:
        raise RuntimeError("the model returned an empty summary")
    return out


def summarize_chat(previous, messages):
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
            summary = _summarize_once(summary, "\n\n".join(batch))
            batch, used = [], 0
        batch.append(line)
        used += t
    if batch:
        summary = _summarize_once(summary, "\n\n".join(batch))
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
        return [d[:5] for _, d in scored[:k]]


PROJECT_INDEX = ProjectIndex()


def _file_text(fid):
    try:
        with open(os.path.join(PROJECT_DIR, fid + ".txt"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def build_project_context(pid, query, budget):
    """The project section of Stella's instructions, fitted to `budget` tokens."""
    p = CHATS.project(pid)
    if not p:
        return "", 0
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
        return text, count_tokens(text)
    total = sum(f["tokens"] for f in files)
    blocks = []
    if total <= remaining - 50:
        for f in files:
            blocks.append(f"### File: {f['name']}\n{_file_text(f['id'])}")
        text += "\n\nFull contents of the project files:\n\n" + "\n\n".join(blocks)
    else:
        whole = set()
        for f in sorted(files, key=lambda f: f["tokens"]):   # small files (pinouts, notes) go in whole
            if f["tokens"] > min(500, remaining * 0.25):
                break
            block = f"### File: {f['name']}\n{_file_text(f['id'])}"
            blocks.append(block)
            whole.add(f["id"])
            remaining -= count_tokens(block)
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
        note = ("\n\nThe files are too big to include whole. These are the parts most related to the latest messages; "
                "call project_search to look up anything else in them.")
        text += note + ("\n\n" + "\n\n".join(blocks) if blocks else "")
    return text, count_tokens(text)


def plan_context(data):
    """Works out what fits in the model's context for the next request.

    Input: project_id, query, fixed (system prompt + tool definitions), messages [{role, content}], images.
    Output: the project section, token counts, and keep_from (index of the oldest message that still fits)."""
    n_ctx = context_size()
    reply_reserve = min(4096, max(1024, n_ctx // 5))
    fixed_t = count_tokens(data.get("fixed", "")) + 900 * int(data.get("images") or 0)
    avail = n_ctx - reply_reserve - fixed_t
    proj_text, proj_t = "", 0
    pid = str(data.get("project_id") or "")
    if pid:
        budget = max(0, min(int(n_ctx * 0.3), avail - 600))
        proj_text, proj_t = build_project_context(pid, str(data.get("query", "")), budget)
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
            "project_context": proj_text}


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


IMAGE_DIR = None
WAKEWORD_DIR = None
WAKEWORD_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,59}")


def run_server_tool(name, args):
    entry = SERVER_TOOLS.get(name)
    if not entry:
        return {"ok": False, "error": f"unknown tool {name}"}, f"Unknown tool {name}"
    try:
        result, summary = entry[0](args if isinstance(args, dict) else {})
        return {"ok": True, **result}, summary
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
                    summary = summarize_chat(str(data.get("previous", "")), data.get("messages") or [])
                except (ValueError, TypeError, KeyError, IndexError, RuntimeError, urllib.error.URLError, OSError) as e:
                    log(f"summary failed: {e}")
                    return self.send_json(503, {"error": f"couldn't summarize: {e}"})
                log(f"refreshed a chat's context ({time.time() - started:.1f}s)")
                return self.send_json(200, {"summary": summary, "tokens": count_tokens(summary)})

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
            m = re.fullmatch(r"/projects/([0-9a-f]{12})/files", path)
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
            m = re.fullmatch(r"/projects/([0-9a-f]{12})(?:/(notes|files)/([0-9a-f]{1,32}))?", path)
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
            if not self.authorized():
                return self.send_json(401, {"error": "wrong API key"})
            if path == "/tools":
                return self.send_json(200, {"tools": [t[1] for t in SERVER_TOOLS.values()]})
            if path == "/memories":
                return self.send_json(200, {"memories": chats.memories()})
            if path == "/projects":
                return self.send_json(200, {"projects": chats.projects()})
            m = re.fullmatch(r"/projects/([0-9a-f]{12})/files/([0-9a-f]{16})\.txt", path)
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
    global WEB
    web = ThreadingHTTPServer((cfg.host, cfg.app_port), make_http_handler(cfg, chats))
    web.daemon_threads = True
    WEB = web
    log(f"hub ready on :{cfg.app_port}, chats in {cfg.db}"
        + ("" if mermaid_command() else " (Mermaid not installed: flowcharts use schemdraw)"))
    return web


def main():
    if "--self-test" in sys.argv:
        self_test()
        return
    cfg = parse_args()
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
printf 'MODEL_LABEL="%s"\nMODEL_FILE="%s"\nGPU_ARGS="%s"\nBACKEND="%s"\n' \
  "$MODEL_LABEL" "$MODEL_FILE" "$GPU_ARGS" "$BACKEND" > "$LLM_DIR/model.conf"

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
  -t "\$THREADS" $GPU_ARGS $VISION_ARGS \\
  -fa on \\
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
hub_loop() {
  local child code started after_update=0
  trap 'kill "\$child" 2>/dev/null; exit 0' TERM INT
  while true; do
    started=\$(date +%s)
    "\$HUB_PY" "$LLM_DIR/stellai_hub.py" --key-file "$KEY_FILE" --db "$LLM_DIR/chats.db" \\
      --diagram-dir "$LLM_DIR/diagrams" --mmdc "$LLM_DIR/mermaid/node_modules/.bin/mmdc" \\
      --image-dir "$LLM_DIR/images" --wakeword-dir "$LLM_DIR/wakewords" --llama "http://127.0.0.1:$PORT" \\
      "\${UPDATE_ARGS[@]}" &
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
HELPER_PIDS="\$!"
if [[ -x "$LLM_DIR/whisper.cpp/build/bin/whisper-server" && -s "$WHISPER_MODEL" ]]; then
  "$LLM_DIR/whisper.cpp/build/bin/whisper-server" -m "$WHISPER_MODEL" -t "\$THREADS" \\
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
if [[ -z "\$HAS_WHISPER" ]]; then
echo "  (voice commands disabled: Whisper not installed - re-run the setup script)"
fi
echo "================================================================"

"$LLM_DIR/llama.cpp/build/bin/llama-server" \\
  -m "$MODEL_FILE" \\
  -c "\$CONTEXT" \\
  -t "\$THREADS" $GPU_ARGS $VISION_ARGS \\
  -fa on \\
  --cache-type-k q8_0 --cache-type-v q8_0 \\
  --jinja \\
  --api-key "\$KEY" \\
  --host 0.0.0.0 --port $PORT
EOF
chmod +x "$LLM_DIR/start-server-network.sh"

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
  Phone app mode:  open "StellAI Server" from your menu,
                   or run  $LLM_DIR/start-server-network.sh
                   (also starts chat sync and the voice services)

  Custom wake words: put trained .onnx files (e.g. hey_stella.onnx) in
                   $LLM_DIR/wakewords - they appear in the app's wake word list.

  Tip: close your browser tabs/other apps before chatting to free RAM.
EOF
