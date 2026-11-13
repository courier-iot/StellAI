#!/usr/bin/env bash
# StellAI server setup for Linux Mint (ThinkPad T470, desktops, anything x86-64)
# Detects the CPU, RAM, graphics card (NVIDIA or AMD), driver and Linux distribution, picks a Qwen3.5 model
# that fits and the packages that match (apt, dnf, pacman or zypper; a CUDA toolkit no newer than the NVIDIA
# driver supports), builds llama.cpp (CUDA for NVIDIA, Vulkan for AMD) and whisper.cpp,
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

# The hub (stellai_hub.py) and the model catalog (stellai_tune.py) live next to this script in the repo's server
# folder. From a copy of the repo they're installed as they are; otherwise they're downloaded from GitHub.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
install_companion() {
  local name="$1" dest="$2" src="$SCRIPT_DIR/$1" tmp
  if [[ -n "$SCRIPT_DIR" && -f "$src" && "$src" -ef "$dest" ]]; then
    return 0   # setup was started from the install folder: keep the installed (maybe auto-updated) copy
  fi
  tmp="$(mktemp)"
  if [[ -n "$SCRIPT_DIR" && -f "$src" ]]; then
    cp "$src" "$tmp"
  elif ! curl -fsSL -o "$tmp" "https://raw.githubusercontent.com/$STELLAI_REPO/HEAD/server/$name"; then
    rm -f "$tmp"
    warn "Couldn't get $name. Run this script from a copy of the repo (git clone https://github.com/$STELLAI_REPO),"
    warn "or put $name next to it, then run it again."
    exit 1
  fi
  if ! python3 -c "import sys; compile(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1], 'exec')" "$tmp"; then
    rm -f "$tmp"; warn "$name looks damaged; download it again."; exit 1
  fi
  mkdir -p "$(dirname "$dest")"
  mv -f "$tmp" "$dest"
  chmod +x "$dest"
}

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user (it will ask for sudo when needed)."; exit 1
fi

mkdir -p "$LLM_DIR/models"

# 0. Detect hardware and choose a model ----------------------------------------
RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1048576 + 0.5}' /proc/meminfo)
PHYS_CORES=$(lscpu -p=core 2>/dev/null | grep -v '^#' | sort -u | wc -l)
[[ "$PHYS_CORES" -lt 1 ]] && PHYS_CORES=2
CPU_NAME=$(lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1)

# Linux distribution and its package manager: package names are translated for each one
. /etc/os-release 2>/dev/null || true
DISTRO="${PRETTY_NAME:-Linux}"
if command -v apt-get >/dev/null; then PKG=apt
elif command -v dnf >/dev/null; then PKG=dnf
elif command -v pacman >/dev/null; then PKG=pacman
elif command -v zypper >/dev/null; then PKG=zypper
else
  warn "This Linux ($DISTRO) has no apt, dnf, pacman or zypper; install build tools, cmake, git and curl yourself."
  PKG=none
fi

# pkg_names GENERIC... -> this distribution's package names
pkg_names() {
  local n
  for n in "$@"; do
    case "$PKG:$n" in
      *:git|*:cmake|*:curl|*:qrencode) echo "$n" ;;
      apt:build) echo build-essential ;;
      pacman:build) echo base-devel ;;
      *:build) echo gcc gcc-c++ make ;;
      apt:curl-dev) echo libcurl4-openssl-dev ;;
      dnf:curl-dev|zypper:curl-dev) echo libcurl-devel ;;
      pacman:curl-dev) ;;                                   # part of curl
      apt:zram) echo zram-tools ;;
      *:zram) echo zram-generator ;;
      apt:python) echo python3-venv python3-pip ;;
      pacman:python) echo python python-pip ;;
      *:python) echo python3 python3-pip ;;
      *:node) echo nodejs npm ;;
      apt:vulkan) echo libvulkan-dev glslc mesa-vulkan-drivers ;;
      dnf:vulkan) echo vulkan-loader-devel vulkan-headers glslc mesa-vulkan-drivers ;;
      pacman:vulkan) echo vulkan-icd-loader vulkan-headers shaderc
                     [[ $GPU_VENDOR == amd ]] && echo vulkan-radeon ;;
      zypper:vulkan) echo vulkan-devel shaderc
                     [[ $GPU_VENDOR == amd ]] && echo libvulkan_radeon ;;
      apt:docker) echo docker.io ;;
      dnf:docker) echo moby-engine ;;
      *:docker) echo docker ;;
      apt:poppler|dnf:poppler) echo poppler-utils ;;
      zypper:poppler) echo poppler-tools ;;
      pacman:poppler) echo poppler ;;
      apt:gcc12) echo gcc-12 g++-12 ;;
      apt:whiptail) echo whiptail ;;
      pacman:whiptail) echo libnewt ;;
      *:whiptail) echo newt ;;
      *:gcc12) ;;
    esac
  done
  return 0
}

PKG_REFRESHED=0
pkg_install() {
  local names
  read -r -a names <<< "$(pkg_names "$@" | tr '\n' ' ')"
  [[ ${#names[@]} -eq 0 || $PKG == none ]] && return 0
  if [[ $PKG_REFRESHED -eq 0 ]]; then
    case $PKG in
      apt) sudo apt-get update ;;
      dnf) sudo dnf makecache -q || true ;;
      pacman) sudo pacman -Syu --noconfirm ;;   # Arch: refresh only together with an upgrade
      zypper) sudo zypper --non-interactive refresh ;;
    esac
    PKG_REFRESHED=1
  fi
  case $PKG in
    apt) sudo apt-get install -y "${names[@]}" ;;
    dnf) sudo dnf install -y "${names[@]}" ;;
    pacman) sudo pacman -S --needed --noconfirm "${names[@]}" ;;
    zypper) sudo zypper --non-interactive install "${names[@]}" ;;
  esac
}

# ---- Setup screens -----------------------------------------------------------------------------------------
# Menu screens (whiptail, arrow keys and space bar) when the terminal allows, plain questions otherwise
# (or with PLAIN_UI=1). Any question can be answered ahead for an unattended run: ANSWER_<key>=y or n,
# e.g. ANSWER_blender=y ANSWER_freecad=n MODEL_CHOICE=qwen3.6-27b ./stellai-setup.sh
UI=plain
declare -A ANS=()
ui_init() {
  [[ -t 0 && -t 1 && -z "${PLAIN_UI:-}" ]] || return 0
  if ! command -v whiptail >/dev/null; then pkg_install whiptail >/dev/null 2>&1 || true; fi
  command -v whiptail >/dev/null && UI=whiptail
  return 0
}
ui_size() {
  local r c
  r=$(tput lines 2>/dev/null || echo 24); c=$(tput cols 2>/dev/null || echo 80)
  UI_H=$(( r > 32 ? 28 : r - 4 )); UI_W=$(( c > 104 ? 100 : c - 4 ))
  (( UI_H < 12 )) && UI_H=12
  (( UI_W < 60 )) && UI_W=60
  return 0
}
# ui_msg TITLE TEXT
ui_msg() {
  if [[ $UI == whiptail ]]; then
    ui_size; whiptail --title "$1" --scrolltext --msgbox "$2" "$UI_H" "$UI_W" || true
  else
    say "$1"; printf '%s\n' "$2" | sed 's/^/    /'
  fi
}
# ask_yn KEY QUESTION [default y|n] -> success for yes. Remembers the answer; ANSWER_KEY=y|n answers ahead.
ask_yn() {
  local key=$1 q=$2 def=${3:-n} env="ANSWER_$1" a=""
  a=${ANS[$key]:-${!env:-}}
  if [[ -z $a ]]; then
    if [[ $UI == whiptail ]]; then
      ui_size
      if [[ $def == y ]]; then whiptail --title "StellAI setup" --yesno "$q" 14 "$UI_W" && a=y || a=n
      else whiptail --title "StellAI setup" --defaultno --yesno "$q" 14 "$UI_W" && a=y || a=n; fi
    else
      local hint="[y/N]"; [[ $def == y ]] && hint="[Y/n]"
      read -r -p "$q $hint " a || a=""
      a=${a,,}; [[ -z $a ]] && a=$def
      [[ $a == y* ]] && a=y || a=n
    fi
  fi
  a=${a,,}; [[ $a == y* ]] && a=y || a=n
  ANS[$key]=$a
  [[ $a == y ]]
}
# ui_menu VAR TITLE TEXT DEFAULT TAG DESC [TAG DESC ...]
ui_menu() {
  local __var=$1 title=$2 text=$3 def=$4 out="" n=""
  shift 4
  if [[ $UI == whiptail ]]; then
    ui_size
    out=$(whiptail --title "$title" --default-item "$def" --menu "$text" "$UI_H" "$UI_W" $(( UI_H - 9 )) "$@" \
          3>&1 1>&2 2>&3) || out=$def
  else
    local tags=() i=1
    echo; echo "  $title"; printf '%s\n' "$text" | sed 's/^/    /'
    while (( $# >= 2 )); do
      tags+=("$1"); printf '    %2d) %s%s\n' "$i" "$2" "$([[ $1 == "$def" ]] && echo '   <- default')"
      i=$((i + 1)); shift 2
    done
    read -r -p "    Number [Enter = default]: " n || n=""
    if [[ $n =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#tags[@]} )); then out=${tags[$((n - 1))]}; else out=$def; fi
  fi
  printf -v "$__var" '%s' "$out"
}
# ui_check VAR TITLE TEXT TAG DESC on|off [...] -> VAR = selected tags, space-separated
ui_check() {
  local __var=$1 title=$2 text=$3 out="" n=""
  shift 3
  if [[ $UI == whiptail ]]; then
    ui_size
    out=$(whiptail --title "$title" --separate-output --checklist "$text" "$UI_H" "$UI_W" $(( UI_H - 9 )) "$@" \
          3>&1 1>&2 2>&3) || out="__keep__"
    if [[ $out == "__keep__" ]]; then      # Cancel keeps the defaults
      out=""; while (( $# >= 3 )); do [[ $3 == on ]] && out+="$1 "; shift 3; done
    fi
    out=$(echo $out)
  else
    local tags=() descs=() states=() i
    while (( $# >= 3 )); do tags+=("$1"); descs+=("$2"); states+=("$3"); shift 3; done
    while true; do
      echo; echo "  $title"; printf '%s\n' "$text" | sed 's/^/    /'
      for i in "${!tags[@]}"; do printf '    %2d) [%s] %s\n' $((i + 1)) "$([[ ${states[$i]} == on ]] && echo x || echo ' ')" "${descs[$i]}"; done
      read -r -p "    Numbers to switch on/off (e.g. 2 5), Enter when done: " n || n=""
      [[ -z $n ]] && break
      for i in $n; do
        [[ $i =~ ^[0-9]+$ ]] && (( i >= 1 && i <= ${#tags[@]} )) || continue
        if [[ ${states[$((i - 1))]} == on ]]; then states[$((i - 1))]=off; else states[$((i - 1))]=on; fi
      done
    done
    for i in "${!tags[@]}"; do [[ ${states[$i]} == on ]] && out+="${tags[$i]} "; done
    out=${out% }
  fi
  printf -v "$__var" '%s' "$out"
}
# ui_input VAR TITLE TEXT DEFAULT
ui_input() {
  local __var=$1 out=""
  if [[ $UI == whiptail ]]; then
    ui_size; out=$(whiptail --title "$2" --inputbox "$3" 12 "$UI_W" "$4" 3>&1 1>&2 2>&3) || out=$4
  else
    read -r -p "$3 [$4]: " out || out=""; out=${out:-$4}
  fi
  printf -v "$__var" '%s' "$out"
}

# ver_le A B: true if version A <= version B (12.4 <= 13.0)
ver_le() { [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" == "$1" ]]; }

ui_init

# Inside a Proxmox (LXC) container? The graphics cards' driver lives on the Proxmox host; the container needs the
# same driver version's programs and libraries (not its kernel part). stellai-proxmox.sh sets the container up.
IN_CT=$(systemd-detect-virt -c 2>/dev/null || true)
[[ $IN_CT == none ]] && IN_CT=""
nvidia_userspace_sync() {
  [[ -e /dev/nvidiactl ]] || return 0
  local want="" have run
  [[ -r /proc/driver/nvidia/version ]] && \
    want=$(grep -m1 'Kernel Module' /proc/driver/nvidia/version | grep -oE '[0-9]{3}\.[0-9]+(\.[0-9]+)?' | head -1)
  [[ -z $want && -s "$LLM_DIR/.host-nvidia-version" ]] && want=$(tr -dc '0-9.' < "$LLM_DIR/.host-nvidia-version")
  [[ -n $want ]] || return 0
  have=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || true)
  [[ $have == "$want" ]] && return 0
  say "Installing the NVIDIA driver programs $want to match the Proxmox host${have:+ (found $have)}"
  run="$LLM_DIR/NVIDIA-Linux-x86_64-$want.run"
  [[ -s "$run" ]] || curl -fL -o "$run" "https://download.nvidia.com/XFree86/Linux-x86_64/$want/NVIDIA-Linux-x86_64-$want.run" \
    || { warn "Couldn't download NVIDIA driver $want; the graphics cards won't work in this container"; return 0; }
  pkg_install build >/dev/null 2>&1 || true
  sudo sh "$run" --no-kernel-module --silent --no-x-check --no-nouveau-check --no-questions \
    || warn "The NVIDIA driver programs didn't install; see /var/log/nvidia-installer.log"
  # apt must never replace them with another version (that silently drops Stella back to the processor)
  printf 'Package: nvidia-driver* nvidia-utils* libnvidia-compute* libnvidia-gl* nvidia-kernel* nvidia-dkms*\nPin: release *\nPin-Priority: -1\n' \
    | sudo tee /etc/apt/preferences.d/stellai-no-nvidia-driver >/dev/null
}
if [[ -n $IN_CT ]]; then
  echo "    Running in a $IN_CT container (Proxmox): zram, power settings and the firewall are left to the host"
  nvidia_userspace_sync
fi
GPU_NAME=""
GPU_VENDOR=""
VRAM_GB=0
NV_CUDA=""   # newest CUDA version the NVIDIA driver supports
NV_CC=""     # GPU compute capability, e.g. 7.5
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
  GPU_VENDOR=nvidia
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
  VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -dc '0-9')
  VRAM_GB=$(( (${VRAM_MB:-0} + 512) / 1024 ))
  NV_CUDA=$(nvidia-smi | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -1)
  NV_CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -dc '0-9.')
elif lspci 2>/dev/null | grep -Ei 'vga|3d' | grep -qi nvidia; then
  warn "This computer has an NVIDIA graphics card, but its driver isn't installed."
  echo "    To use it: Menu > Driver Manager > choose the recommended NVIDIA driver > Apply Changes,"
  echo "    reboot, then run this script again."
  ask_yn nodriver "This computer has an NVIDIA graphics card, but its driver isn't installed (Menu > Driver Manager, reboot, run setup again).
Continue without the graphics card for now?" n || exit 1
else
  # AMD graphics card with its own memory: runs through Vulkan (the open-source Mesa driver)
  for d in /sys/class/drm/card[0-9]/device; do
    [[ "$(cat "$d/vendor" 2>/dev/null)" == "0x1002" && -r "$d/mem_info_vram_total" ]] || continue
    vb=$(cat "$d/mem_info_vram_total")
    vg=$(( (vb / 1048576 + 512) / 1024 ))
    if (( vg >= 4 && vg > VRAM_GB )); then
      GPU_VENDOR=amd; VRAM_GB=$vg
      GPU_NAME=$(lspci 2>/dev/null | grep -Ei 'vga|3d|display' | grep -Ei 'amd|ati' | head -1 | sed 's/.*: //' || true)
      GPU_NAME=${GPU_NAME:-AMD graphics}
    fi
  done
fi

LOGICAL_CPUS=$(nproc)

# 0b. What this computer is: the server, a client of another server, or both --------------------------------
# A 2-core laptop without a usable graphics card answers far slower than a desktop with a GPU, and every device
# shares the same chats anyway, so weak machines are offered "client" first: just the desktop chat and avatar
# screen, pointed at the stronger server. Answer ahead with ROLE=server|client|both (CLIENT_ONLY=1 = client).
WEAK=0
if [[ $VRAM_GB -eq 0 && $PHYS_CORES -le 2 ]] || [[ $RAM_GB -lt 8 ]]; then WEAK=1; fi
if [[ $WEAK -eq 1 ]]; then ROLE_DEF=client
elif [[ -n ${IN_CT:-} || -z ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then ROLE_DEF=server
else ROLE_DEF=both; fi
ROLE=${ROLE:-}
[[ "${CLIENT_ONLY:-}" == "1" ]] && ROLE=client
[[ "${CLIENT_ONLY:-}" == "0" && $ROLE_DEF == client ]] && ROLE_DEF=both
if [[ -z $ROLE ]]; then
  R_TEXT="Server: Stella's model runs here. Client: this computer chats with Stella running on another one."
  [[ $WEAK -eq 1 ]] && R_TEXT+="

This computer is slow for running Stella itself (${PHYS_CORES} cores, ${RAM_GB} GB RAM, no usable graphics card): as a client of a stronger server it gets the same chats and much faster answers, with nothing big installed."
  ui_menu ROLE "What should this computer be?" "$R_TEXT" "$ROLE_DEF" \
    both "Server and client: Stella runs here, and you chat with her here too" \
    server "Server only: Stella runs here for your phones and computers (no desktop shortcuts)" \
    client "Client only: chat with Stella on another computer (nothing big installed here)"
fi
[[ $ROLE == server || $ROLE == client ]] || ROLE=both
if [[ $ROLE == client ]]; then
    if ! command -v tailscale >/dev/null; then
      if ask_yn tailscale "Install Tailscale? It lets this computer find and reach your StellAI server, at home and away." y; then
        curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up || warn "Tailscale didn't finish; run 'sudo tailscale up' later"
      fi
    fi
    # StellAI servers among your Tailscale devices
    SRV=""
    if command -v tailscale >/dev/null && command -v python3 >/dev/null; then
      mapfile -t FOUND < <(tailscale status --json 2>/dev/null | python3 -c '
import json, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
try:
    st = json.load(sys.stdin)
except ValueError:
    sys.exit()
me = (st.get("Self") or {}).get("UserID")
peers = [p for p in (st.get("Peer") or {}).values() if p.get("Online") and p.get("UserID") == me]
def probe(p):
    ip = next((i for i in p.get("TailscaleIPs") or [] if "." in i), "")
    for path in ("/move/hello", "/health"):
        try:
            with urllib.request.urlopen(f"http://{ip}:8092{path}", timeout=2) as r:
                d = json.loads(r.read() or b"{}")
            if d.get("stellai") or d.get("ok"):
                name = p.get("HostName") or ip
                return ip + "\t" + name + (" (moved away: a client now)" if d.get("signpost") else "")
        except Exception:
            pass
    return None
with ThreadPoolExecutor(16) as ex:
    for line in ex.map(probe, peers):
        if line:
            print(line)' 2>/dev/null || true)
      if (( ${#FOUND[@]} )); then
        items=(); for l in "${FOUND[@]}"; do items+=("${l%%$'\t'*}" "${l#*$'\t'}"); done
        items+=(other "Another address")
        ui_menu SRV "Your StellAI server" "StellAI servers found on your Tailscale devices:" "${items[0]}" "${items[@]}"
        [[ $SRV == other ]] && SRV=""
      fi
    fi
    [[ -n $SRV ]] || \
    ui_input SRV "Other StellAI server" "Server address (its Tailscale address works at home and away, e.g. 100.101.102.103)" ""
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
    cat > "$LLM_DIR/open-avatar.sh" <<EOF
#!/usr/bin/env bash
# Stella's avatar screen, from the server $SRV (client mode)
xdg-open "http://$SRV:8092/avatar#key=\$(cat "$LLM_DIR/client-key.txt")"
EOF
    chmod +x "$LLM_DIR/open-avatar.sh"
    cat > "$HOME/.local/share/applications/stellai-avatar.desktop" <<EOF
[Desktop Entry]
Name=StellAI Avatar
Comment=Stella's avatar screen (runs on $SRV)
Exec=$LLM_DIR/open-avatar.sh
Icon=face-smile
Terminal=false
Type=Application
Categories=Utility;
EOF
    if curl -fsS -m 5 "http://$SRV:8092/health" >/dev/null 2>&1; then
      say "Done! Open \"StellAI\" from the menu (server $SRV answers)"
    else
      warn "Couldn't reach http://$SRV:8092 right now. Make sure StellAI Server is running there; then open \"StellAI\" from the menu."
    fi
    echo "    To run Stella on this computer after all: ROLE=both $0   (or ROLE=server)"
    exit 0
fi

# 0c. Plan the install: screens for graphics cards, memory, the model, the helper and extras ------------------
# The catalog, the ranking for this computer and the exact Hugging Face files come from stellai_tune.py (the
# same file the Windows app and the hub use). Its speed figures are estimates; step 5c measures the real ones.
command -v python3 >/dev/null || pkg_install python
TUNE_PY="$LLM_DIR/stellai_tune.py"
install_companion stellai_tune.py "$TUNE_PY"
ui_init
HW_JSON=$(python3 "$TUNE_PY" detect --dir "$LLM_DIR" 2>/dev/null || echo '{"gpus": []}')
hwq() { python3 -c 'import json, sys; h = json.loads(sys.argv[1]); exec(sys.argv[2])' "$HW_JSON" "$1"; }
HAS_AVX2=$(hwq 'print(1 if h.get("avx2") else 0)')
DISK_FREE_GB=$(hwq 'print(int(h.get("disk_free_gb") or 0))')
mapfile -t GPU_ROWS < <(hwq 'for g in h.get("gpus", []): print(g["index"], g["vram_gb"], g["name"], sep="\t")')

# Screen 1: what this computer has
HW_TEXT="Processor:  ${CPU_NAME:-unknown}
            ${PHYS_CORES} cores / ${LOGICAL_CPUS} threads, AVX2: $([[ $HAS_AVX2 == 1 ]] && echo yes || echo 'no (older CPU: the graphics cards matter more)')
Memory:     ${RAM_GB} GB
Disk free:  ${DISK_FREE_GB} GB in $LLM_DIR
Linux:      $DISTRO (packages from $PKG)
"
if (( ${#GPU_ROWS[@]} )); then
  for row in "${GPU_ROWS[@]}"; do IFS=$'\t' read -r gi gv gn <<< "$row"; HW_TEXT+="Graphics:   card $gi: $gn, $gv GB
"; done
else
  HW_TEXT+="Graphics:   none usable (Stella will run on the processor)
"
fi
case $GPU_VENDOR in
  nvidia) HW_TEXT+="
llama.cpp will be built for NVIDIA CUDA (driver supports up to CUDA ${NV_CUDA:-?}), Vulkan as a fallback." ;;
  amd) HW_TEXT+="
llama.cpp will be built for Vulkan on the AMD card." ;;
  *) HW_TEXT+="
llama.cpp will be built for exactly this processor." ;;
esac
ui_msg "StellAI setup: this computer" "$HW_TEXT

Next: which graphics cards and how much memory Stella may use, then the model, the helper and extras. Nothing is downloaded until you confirm the summary."

# Screen 2: graphics cards for Stella's model
LLAMA_GPUS_SEL=""
GPU_IDS_ALL=$(hwq 'print(",".join(str(g["index"]) for g in h.get("gpus", [])))')
if [[ $GPU_VENDOR == nvidia ]] && (( ${#GPU_ROWS[@]} >= 2 )); then
  items=()
  for row in "${GPU_ROWS[@]}"; do IFS=$'\t' read -r gi gv gn <<< "$row"; items+=("$gi" "card $gi: $gn ($gv GB)" on); done
  ui_check SEL "Graphics cards" "Which cards may Stella's model use? More cards fit a bigger model, or the same model faster.
Cards you leave out stay free for the helper, 3D shapes and other tools (Stella's model pauses for 3D shape jobs either way)." "${items[@]}"
  LLAMA_GPUS_SEL=$(echo "$SEL" | tr ' ' ',')
  [[ -z $LLAMA_GPUS_SEL ]] && warn "No card chosen: Stella's model will run on the processor"
elif (( ${#GPU_ROWS[@]} == 1 )); then
  LLAMA_GPUS_SEL=$GPU_IDS_ALL
fi
# only an explicit subset is written to model.conf (empty = every card)
[[ $LLAMA_GPUS_SEL == "$GPU_IDS_ALL" ]] && LLAMA_GPUS_CONF="" || LLAMA_GPUS_CONF=$LLAMA_GPUS_SEL
[[ $GPU_VENDOR == nvidia && -z $LLAMA_GPUS_SEL && ${#GPU_ROWS[@]} -ge 2 ]] && LLAMA_GPUS_CONF="none"

# Screen 3: memory budget
R_MOST=$(( RAM_GB - (RAM_GB >= 64 ? 6 : RAM_GB >= 24 ? 4 : 3) )); (( R_MOST < 2 )) && R_MOST=2
R_75=$(( RAM_GB * 3 / 4 )); R_50=$(( RAM_GB / 2 ))
RAM_PICK_DEF=most
ui_menu RAM_PICK "Memory for StellAI" "How much of the ${RAM_GB} GB of RAM may StellAI use? (Graphics memory comes on top.)
A dedicated server can give almost everything; a PC you also use for other things should keep more free." "$RAM_PICK_DEF" \
  most "$R_MOST GB: nearly all (a dedicated server)" \
  r75 "$R_75 GB: three quarters (a PC you also use)" \
  r50 "$R_50 GB: half (keep plenty free)" \
  custom "Type a number"
case $RAM_PICK in
  r75) RAM_BUDGET_GB=$R_75 ;; r50) RAM_BUDGET_GB=$R_50 ;;
  custom) ui_input RAM_BUDGET_GB "Memory for StellAI" "GB of RAM StellAI may use (1-$RAM_GB)" "$R_MOST"
          [[ $RAM_BUDGET_GB =~ ^[0-9]+$ ]] && (( RAM_BUDGET_GB >= 1 && RAM_BUDGET_GB <= RAM_GB )) || RAM_BUDGET_GB=$R_MOST ;;
  *) RAM_BUDGET_GB=$R_MOST ;;
esac

# Screen 4: the model (ranked for the cards and memory chosen above)
tune_rank() { python3 "$TUNE_PY" "$1" --hw "$HW_JSON" --ram "$RAM_BUDGET_GB" --gpus "${LLAMA_GPUS_SEL}" --menu 2>/dev/null; }
mapfile -t PICK_LINES < <(tune_rank rank)
mapfile -t ALL_LINES < <(tune_rank options)
MODEL_KEY=""; MODEL_QUANT=""; NET_CONTEXT=""; MODEL_HAS_MTP=0; CUSTOM_URL=""
pick_from_line() { IFS='|' read -r MODEL_KEY MODEL_QUANT NET_CONTEXT MODEL_HAS_MTP <<< "${1%%$'\t'*}"; }
CHOICE="${MODEL_CHOICE:-}"
case "$CHOICE" in 1) CHOICE=qwen3.5-4b ;; 2) CHOICE=qwen3.5-9b ;; 3) CHOICE=qwen3.6-35b-a3b ;; esac
if [[ -n $CHOICE ]]; then                         # answered ahead: the best version of that model that fits
  for l in "${PICK_LINES[@]}" "${ALL_LINES[@]}"; do
    [[ ${l%%|*} == "${CHOICE%%|*}" ]] || continue
    [[ $CHOICE == *"|"* && ${l%%$'\t'*} != "$CHOICE"* ]] && continue
    pick_from_line "$l"; break
  done
  [[ -n $MODEL_KEY ]] || warn "MODEL_CHOICE=$CHOICE doesn't fit this computer's memory; choose one instead"
fi
if [[ -z $MODEL_KEY && ${#ALL_LINES[@]} -eq 0 ]]; then
  warn "No model fits ${RAM_BUDGET_GB} GB of memory; installing the smallest"
  MODEL_KEY=qwen3.5-2b; MODEL_QUANT=Q4_K_M; NET_CONTEXT=8192
fi
while [[ -z $MODEL_KEY && -z $CUSTOM_URL ]]; do
  items=()
  for l in "${PICK_LINES[@]}"; do items+=("${l%%$'\t'*}" "${l#*$'\t'}"); done
  items+=(more "Show every model and size that fits (${#ALL_LINES[@]})" custom "My own model: a GGUF link from Hugging Face")
  DEF=${items[0]:-more}
  ui_menu SEL "Stella's model" "Ranked for this computer: ${RAM_BUDGET_GB} GB RAM$([[ -n $LLAMA_GPUS_SEL ]] && echo " + cards $LLAMA_GPUS_SEL") (speeds are estimates; the test at the end measures them).
Bigger models are smarter; 'split' means part of it runs from RAM." "$DEF" "${items[@]}"
  if [[ $SEL == more ]]; then
    items=()
    for l in "${ALL_LINES[@]}"; do items+=("${l%%$'\t'*}" "${l#*$'\t'}"); done
    items+=(back "Back to the picks")
    ui_menu SEL "Every model that fits" "Smartest first. Q4 and up keep nearly all of a model's quality; Q2/Q3 trade some for size." back "${items[@]}"
    [[ $SEL == back ]] && continue
  fi
  if [[ $SEL == custom ]]; then
    ui_input CUSTOM_URL "Your own model" "A .gguf link (https://huggingface.co/OWNER/REPO/resolve/main/FILE.gguf).
StellAI's tools and prompts are tuned for Qwen; other families may answer less reliably." ""
    [[ $CUSTOM_URL == https://huggingface.co/*.gguf ]] || { CUSTOM_URL=""; warn "That isn't a Hugging Face .gguf link"; }
    continue
  fi
  for l in "${ALL_LINES[@]}" "${PICK_LINES[@]}"; do [[ ${l%%$'\t'*} == "$SEL" ]] && { pick_from_line "$l"; break; }; done
done

# Screen 5: conversation length (context window)
if [[ -z $CUSTOM_URL ]]; then
  CTX_DEF=$NET_CONTEXT
  items=("$CTX_DEF" "$((CTX_DEF / 1024))K (recommended)")
  for c in 8192 16384 32768 65536 131072; do [[ $c == "$CTX_DEF" ]] || items+=("$c" "$((c / 1024))K"); done
  ui_menu NET_CONTEXT "Conversation length" "How much of a chat Stella keeps in mind at once (tokens; 1K is about 750 words).
Recommended here: $((CTX_DEF / 1024))K. Longer needs more memory and may slow her down; the test at the end checks it fits." "$CTX_DEF" \
    "${items[@]}"
else
  NET_CONTEXT=16384
fi
CONTEXT=$(( NET_CONTEXT < 16384 ? NET_CONTEXT : 16384 ))   # the local chat launcher

# Screen 6: the helper model for quick background jobs
HELPER_KEY=""; HELPER_QUANT=""; HELPER_GPU=""
MAIN_GB=$(PYTHONPATH="$LLM_DIR" python3 -c 'import sys, stellai_tune as t
k, q = sys.argv[1], sys.argv[2]
m = t.BY_KEY.get(k)
print(next((gb for tag, gb in m["quants"] if tag == q), 0) if m else 8)' "$MODEL_KEY" "$MODEL_QUANT" 2>/dev/null || echo 8)
eval "$(python3 "$TUNE_PY" helper --hw "$HW_JSON" --ram "$RAM_BUDGET_GB" --main-gb "$MAIN_GB" 2>/dev/null || echo "HELPER_KEY=''")"
if [[ -n $HELPER_KEY ]]; then
  items=(cpu "$HELPER_LABEL on the processor (recommended, ~${HELPER_SIZE_GB} GB of RAM)")
  [[ $HELPER_KEY == qwen3.5-2b ]] && items+=(small "Qwen3.5 0.8B on the processor (lighter, a bit less accurate)")
  for row in "${GPU_ROWS[@]}"; do
    IFS=$'\t' read -r gi gv gn <<< "$row"
    [[ ",$LLAMA_GPUS_SEL," == *",$gi,"* ]] || items+=("gpu$gi" "$HELPER_LABEL on card $gi ($gn), which Stella's model doesn't use")
  done
  items+=(none "No helper: the main model does everything")
  ui_menu SEL "Helper model" "A small second model takes quick background jobs (learning memories, checking which mode a message needs), so they don't queue behind Stella's main model." cpu "${items[@]}"
  case $SEL in
    none) HELPER_KEY="" ;;
    small) HELPER_KEY=qwen3.5-0.8b; HELPER_QUANT=Q8_0 ;;
    gpu*) HELPER_GPU=${SEL#gpu} ;;
  esac
fi

# Screen 7: extras (each is asked again later only if it isn't answered here)
TRY_MTP=0
[[ $MODEL_HAS_MTP == 1 ]] && ask_yn mtp "Speed-up test: download the version of $MODEL_KEY with Multi-Token Prediction and measure whether it writes faster here?
It's kept only if it's at least 10% faster: on some computers it's slower." y && TRY_MTP=1
if [[ -n ${WHISPER_NAME:-} ]]; then
  WHISPER_SIZE="your choice"            # e.g. WHISPER_NAME=ggml-small.en.bin ./stellai-setup.sh
else
  [[ $RAM_GB -ge 14 && $PHYS_CORES -ge 6 ]] && WDEF=small || WDEF=base
  ui_menu WSEL "Voice recognition" "Which speech model turns your voice into text?" "$WDEF" \
    small "small: more accurate (~470 MB)" base "base: faster, lighter (~150 MB)"
  if [[ $WSEL == small ]]; then WHISPER_NAME="ggml-small.en.bin"; WHISPER_SIZE="~470 MB"
  else WHISPER_NAME="ggml-base.en.bin"; WHISPER_SIZE="~150 MB"; fi
fi
WHISPER_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER_NAME"
WHISPER_MODEL="$LLM_DIR/models/$WHISPER_NAME"
x_items=(vision "Photos: Stella understands pictures (~0.6-0.9 GB)" on
         tailscale "Tailscale: the phone app works away from home" on
         searxng "Private web search (SearXNG in Docker, ~300 MB)" on
         blender "3D Modeler: Blender (~1.3 GB)" off)
[[ $GPU_VENDOR == nvidia && $VRAM_GB -ge 8 ]] && x_items+=(shapegen "Photo-to-3D shapes for the 3D Modeler (~11 GB, uses a graphics card)" off)
x_items+=(freecad "CAD Designer: FreeCAD" off kicad "PCB Designer: KiCad + circuits as code and autorouting" off
          test "Test run at the end: measure speed, check the conversation length fits" on)
if [[ -n $IN_CT || $ROLE == server || -z ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then SVC_DEF=on; else SVC_DEF=off; fi
x_items+=(service "Start StellAI Server by itself when the computer starts (a background service)" $SVC_DEF)
grep -qi thinkpad /sys/class/dmi/id/product_version 2>/dev/null && x_items+=(throttled "ThinkPad throttling fix (throttled)" off)
ui_check X_SEL "Extras" "Space bar switches an item on or off. You can add these later by running the setup again." "${x_items[@]}"
for ((i = 0; i < ${#x_items[@]}; i += 3)); do
  k=${x_items[$i]}; env="ANSWER_$k"
  [[ -n ${!env:-} ]] && continue                 # answered ahead
  if [[ " $X_SEL " == *" $k "* ]]; then ANS[$k]=y; else ANS[$k]=n; fi
done
[[ ${ANS[kicad]:-n} == y ]] && ANS[circuits]=y

# Exact files and sizes from Hugging Face (needs the internet; falls back to the known files)
RESOLVED_JSON="$LLM_DIR/.resolved-model.json"
MODEL_URLS=""; MODEL_PARTS=""; MMPROJ_URL=""; MMPROJ_NAME=""; MODEL_MTP=0
if [[ -n $CUSTOM_URL ]]; then
  MODEL_NAME=$(basename "${CUSTOM_URL%%\?*}"); MODEL_LABEL="Custom: ${MODEL_NAME%.gguf}"
  MODEL_REPO=$(echo "$CUSTOM_URL" | sed -n 's#https://huggingface.co/\([^/]*/[^/]*\)/.*#\1#p')
  MODEL_URLS=$CUSTOM_URL; MODEL_PARTS=$MODEL_NAME; MODEL_MB=0; MODEL_KEY=custom
  rm -f "$RESOLVED_JSON"
else
  echo "    Looking up the exact files on Hugging Face..."
  if python3 "$TUNE_PY" resolve "$MODEL_KEY" --quant "$MODEL_QUANT" $([[ $TRY_MTP == 1 ]] && echo --mtp) > "$RESOLVED_JSON" 2>/dev/null \
     && R_SHELL=$(python3 - "$RESOLVED_JSON" <<'PYR'
import json, shlex, sys
r = json.load(open(sys.argv[1]))
mm = r.get("mmproj") or {}
vals = {"MODEL_LABEL": r["label"] + " " + r["quant"] + (" (MTP)" if r["mtp"] else ""), "MODEL_REPO": r["repo"],
        "MODEL_NAME": r["model_file"], "MODEL_QUANT": r["quant"], "MODEL_MB": int(r["size_gb"] * 1024),
        "MODEL_MTP": 1 if r["mtp"] else 0, "MODEL_URLS": " ".join(f["url"] for f in r["files"]),
        "MODEL_PARTS": " ".join(f["name"] for f in r["files"]), "MMPROJ_URL": mm.get("url", ""),
        "MMPROJ_NAME": mm.get("name", "")}
print("\n".join(f"{k}={shlex.quote(str(v))}" for k, v in vals.items()))
PYR
); then
    eval "$R_SHELL"
  else
    warn "Couldn't look the files up on Hugging Face; using the files StellAI knew before"
    rm -f "$RESOLVED_JSON"
    case $MODEL_KEY in
      qwen3.5-0.8b|qwen3.5-2b|qwen3.5-4b) MODEL_KEY=qwen3.5-4b; MODEL_REPO="unsloth/Qwen3.5-4B-GGUF"; MODEL_NAME="Qwen3.5-4B-Q4_K_M.gguf"; MODEL_MB=2700 ;;
      qwen3.5-9b) MODEL_REPO="unsloth/Qwen3.5-9B-GGUF"
        if [[ $VRAM_GB -ge 5 && $VRAM_GB -lt 8 ]]; then MODEL_NAME="Qwen3.5-9B-Q3_K_M.gguf"; MODEL_MB=4600
        else MODEL_NAME="Qwen3.5-9B-Q4_K_M.gguf"; MODEL_MB=5700; fi ;;
      *) MODEL_KEY=qwen3.5-35b-a3b; MODEL_REPO="unsloth/Qwen3.5-35B-A3B-GGUF"
        if (( RAM_BUDGET_GB + VRAM_GB >= 40 )); then MODEL_NAME="Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf"; MODEL_MB=22200
        elif (( RAM_BUDGET_GB + VRAM_GB >= 30 )); then MODEL_NAME="Qwen3.5-35B-A3B-UD-IQ4_XS.gguf"; MODEL_MB=17500
        else MODEL_NAME="Qwen3.5-35B-A3B-UD-IQ3_S.gguf"; MODEL_MB=13600; fi ;;
    esac
    MODEL_LABEL=${MODEL_NAME%.gguf}
    MODEL_URLS="https://huggingface.co/$MODEL_REPO/resolve/main/$MODEL_NAME"; MODEL_PARTS=$MODEL_NAME
    MMPROJ_URL="https://huggingface.co/$MODEL_REPO/resolve/main/mmproj-F16.gguf"; MMPROJ_NAME="${MODEL_NAME%.gguf}.mmproj-F16.gguf"
  fi
fi
MODEL_FILE="$LLM_DIR/models/$MODEL_NAME"
MODEL_URL=${MODEL_URLS%% *}
GPU_ARGS=""

# Screen 8: summary and go
H_TEXT="none (the main model does everything)"
[[ -n $HELPER_KEY ]] && H_TEXT="$HELPER_KEY on $([[ -n $HELPER_GPU ]] && echo "card $HELPER_GPU" || echo "the processor")"
X_TEXT=""; for k in vision tailscale searxng blender shapegen freecad kicad test service throttled; do [[ ${ANS[$k]:-n} == y ]] && X_TEXT+="$k "; done
NEED_GB=$(( (MODEL_MB + 1023) / 1024 + 3 ))
ui_msg "Ready to install" "Model:        $MODEL_LABEL
              $MODEL_PARTS ($(( (MODEL_MB + 512) / 1024 )) GB)
Speed-up:     $([[ $MODEL_MTP == 1 ]] && echo "MTP version; the test decides whether to use it" || echo "none")
Conversation: $((NET_CONTEXT / 1024))K tokens
Cards:        $([[ -n $LLAMA_GPUS_SEL ]] && echo "$LLAMA_GPUS_SEL" || echo "none (processor)")
Memory:       up to ${RAM_BUDGET_GB} GB of ${RAM_GB} GB
Helper:       $H_TEXT
Voice:        $WHISPER_NAME
Extras:       ${X_TEXT:-none}

Downloads: about ${NEED_GB} GB plus build tools; ${DISK_FREE_GB} GB free.
Building llama.cpp takes 10-30 minutes."
if (( DISK_FREE_GB > 0 && DISK_FREE_GB < NEED_GB )); then
  ask_yn lowdisk "Only ${DISK_FREE_GB} GB is free, and this needs about ${NEED_GB} GB. Continue anyway?" n || exit 1
fi
ask_yn go "Start installing?" y || { echo "Nothing was installed. Run the setup again any time."; exit 0; }
echo "    Installing: $MODEL_LABEL ($MODEL_NAME); speech recognition: $WHISPER_NAME"

# 1. Packages ------------------------------------------------------------------
say "Installing build tools for $DISTRO"
pkg_install build cmake git curl curl-dev
pkg_install qrencode || warn "qrencode not installed: no pairing QR code (type the address and key instead)"

# 2. zram (compressed RAM swap) ------------------------------------------------
if [[ -n $IN_CT ]]; then
  echo "    zram and memory settings belong to the Proxmox host; skipped in the container"
else
say "Configuring zram (compressed swap in RAM)"
if [[ $PKG == apt ]]; then
  pkg_install zram || true
  sudo tee /etc/default/zramswap >/dev/null <<'EOF'
ALGO=zstd
PERCENT=60
PRIORITY=100
EOF
  sudo systemctl enable --now zramswap.service || warn "zram service did not start; check 'systemctl status zramswap'"
elif swapon --show=NAME --noheadings 2>/dev/null | grep -q zram; then
  echo "    zram is already on"
else
  pkg_install zram || true
  sudo tee /etc/systemd/zram-generator.conf >/dev/null <<'EOF'
[zram0]
zram-size = ram * 0.6
compression-algorithm = zstd
swap-priority = 100
EOF
  sudo systemctl daemon-reload
  sudo systemctl start systemd-zram-setup@zram0.service || warn "zram didn't start; it will after a reboot"
fi
# Prefer zram over disk swap, and keep the model in RAM when possible
echo 'vm.swappiness=100' | sudo tee /etc/sysctl.d/99-llm-zram.conf >/dev/null
echo 'vm.page-cluster=0' | sudo tee -a /etc/sysctl.d/99-llm-zram.conf >/dev/null
sudo sysctl --system >/dev/null || true
fi

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
CUDA_ARGS=()

# The CUDA toolkit must be no newer than the driver supports (else: "PTX was compiled with an unsupported
# toolchain") and must know this GPU. cuda_ok NVCC: checks one installed toolkit, sets NVCC and CUDA_VER.
cuda_ok() {
  local v
  [[ -x "$1" ]] || return 1
  v=$("$1" --version 2>/dev/null | sed -n 's/.*release \([0-9]*\.[0-9]*\).*/\1/p')
  [[ -n $v ]] || return 1
  if [[ -n $NV_CUDA ]] && ! ver_le "$v" "$NV_CUDA"; then
    echo "    CUDA toolkit $v at $1 is newer than the driver supports ($NV_CUDA); not using it"
    return 1
  fi
  if [[ -n $NV_CC ]] && "$1" --list-gpu-arch >/dev/null 2>&1 &&
     ! "$1" --list-gpu-arch | grep -q "compute_${NV_CC/./}\$"; then
    echo "    CUDA toolkit $v doesn't support this GPU (compute ${NV_CC}); not using it"
    return 1
  fi
  NVCC=$1; CUDA_VER=$v
}

find_cuda() {
  local c
  for c in "$(command -v nvcc 2>/dev/null)" $(ls -d /usr/local/cuda-*/bin/nvcc 2>/dev/null | sort -rV) /usr/local/cuda/bin/nvcc /opt/cuda/bin/nvcc; do
    [[ -n $c ]] && cuda_ok "$c" && return 0
  done
  return 1
}

# NVIDIA's own repository (Ubuntu, Mint, Pop!_OS, Debian): install the toolkit version that matches the driver
cuda_from_nvidia_repo() {
  local repo="" base v
  [[ $PKG == apt && $(uname -m) == x86_64 ]] || return 1
  case "${UBUNTU_CODENAME:-}" in
    focal) repo=ubuntu2004 ;; jammy) repo=ubuntu2204 ;; noble) repo=ubuntu2404 ;;
  esac
  if [[ -z $repo && ${ID:-} == debian ]]; then repo="debian${VERSION_ID%%.*}"; fi
  [[ -n $repo ]] || return 1
  base="https://developer.download.nvidia.com/compute/cuda/repos/$repo/x86_64"
  if ! dpkg -s cuda-keyring >/dev/null 2>&1; then
    curl -fsSL -o /tmp/cuda-keyring.deb "$base/cuda-keyring_1.1-1_all.deb" &&
      sudo dpkg -i /tmp/cuda-keyring.deb || return 1
    sudo apt-get update || return 1
  fi
  for v in 13.0 12.9 12.8 12.6 12.5 12.4 12.3 12.2 12.1 12.0; do
    [[ -z $NV_CUDA ]] || ver_le "$v" "$NV_CUDA" || continue
    # CUDA 13 dropped GPUs older than Turing (compute 7.5)
    if [[ $v == 13.* && -n $NV_CC ]] && ! ver_le 7.5 "$NV_CC"; then continue; fi
    if apt-cache show "cuda-toolkit-${v/./-}" >/dev/null 2>&1; then
      say "Installing CUDA toolkit $v from NVIDIA (matches your driver, large download ~3 GB)"
      sudo apt-get install -y "cuda-toolkit-${v/./-}" && cuda_ok "/usr/local/cuda-$v/bin/nvcc" && return 0
    fi
  done
  return 1
}

if [[ $GPU_VENDOR == nvidia ]]; then
  echo "    NVIDIA driver supports CUDA ${NV_CUDA:-?}; GPU compute capability ${NV_CC:-?}"
  if find_cuda; then
    say "Using the installed CUDA toolkit $CUDA_VER"
  elif cuda_from_nvidia_repo; then
    :
  else
    say "Installing the CUDA toolkit from $DISTRO (large download, ~3 GB)"
    case $PKG in
      apt) pkg_install gcc12; sudo apt-get install -y nvidia-cuda-toolkit || true ;;
      dnf) sudo dnf install -y cuda-toolkit || sudo dnf install -y cuda || true ;;
      pacman) sudo pacman -S --needed --noconfirm cuda || true ;;
      zypper) sudo zypper --non-interactive install cuda-toolkit || true ;;
    esac
    find_cuda || warn "No CUDA toolkit that fits driver ${NV_CUDA:-?} could be installed; using Vulkan instead"
  fi
  if [[ -n ${NVCC:-} ]]; then
    CUDA_ARGS=(-DGGML_CUDA=ON -DCMAKE_CUDA_COMPILER="$NVCC")
    # Build exactly for this GPU (also works with older CMake than "native" needs)
    if [[ -n $NV_CC ]]; then CUDA_ARGS+=(-DCMAKE_CUDA_ARCHITECTURES="${NV_CC/./}"); else CUDA_ARGS+=(-DCMAKE_CUDA_ARCHITECTURES=native); fi
    say "Building llama.cpp with CUDA $CUDA_VER (~15-30 min)"
    if build_llama "${CUDA_ARGS[@]}"; then
      BACKEND=cuda
    else
      # Older toolkits reject the newest gcc: retry with gcc 12
      pkg_install gcc12 || true
      if command -v g++-12 >/dev/null; then
        CUDA_ARGS+=(-DCMAKE_CUDA_HOST_COMPILER=g++-12)
        warn "Retrying the CUDA build with gcc 12"
        build_llama "${CUDA_ARGS[@]}" && BACKEND=cuda
      fi
    fi
  fi
fi
if [[ $BACKEND == cpu && ( $GPU_VENDOR == nvidia || $GPU_VENDOR == amd ) ]]; then
  [[ $GPU_VENDOR == nvidia ]] && warn "The CUDA build didn't work; trying the Vulkan GPU backend instead"
  [[ $GPU_VENDOR == amd ]] && say "Building llama.cpp with Vulkan for the AMD graphics card (~15-25 min)"
  if pkg_install vulkan && build_llama -DGGML_VULKAN=ON; then
    BACKEND=vulkan
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
if [[ $BACKEND == cuda ]] && build_whisper "${CUDA_ARGS[@]}"; then
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
pkg_install python node || warn "Couldn't install Python/Node.js packages"
# Mermaid needs Node.js 18.19 or newer; older distributions ship older ones, so fetch the official LTS build
NODE_BIN=""
nv=$(node --version 2>/dev/null | tr -d v || true)
if [[ -x "$LLM_DIR/node/bin/node" ]]; then
  NODE_BIN="$LLM_DIR/node/bin"
elif [[ -z $nv ]] || ! ver_le 18.19 "$nv"; then
  case $(uname -m) in x86_64) narch=x64 ;; aarch64) narch=arm64 ;; *) narch="" ;; esac
  if [[ -n $narch ]]; then
    say "Node.js ${nv:-missing} is too old for Mermaid; installing Node.js 22 LTS into $LLM_DIR/node"
    sums=$(curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt || true)
    line=$(grep " node-v[0-9.]*-linux-$narch.tar.xz\$" <<< "$sums" | head -1 || true)
    if [[ -n $line ]]; then
      nfile=${line##* }
      if curl -fsSL -o "/tmp/$nfile" "https://nodejs.org/dist/latest-v22.x/$nfile" &&
         [[ "$(sha256sum "/tmp/$nfile" | cut -d' ' -f1)" == "${line%% *}" ]]; then
        rm -rf "$LLM_DIR/node" && mkdir -p "$LLM_DIR/node" &&
          tar -xJf "/tmp/$nfile" -C "$LLM_DIR/node" --strip-components=1 && NODE_BIN="$LLM_DIR/node/bin"
      fi
      rm -f "/tmp/$nfile"
    fi
    [[ -n $NODE_BIN ]] || warn "Couldn't download Node.js; block diagrams may not work"
  fi
fi
NPM=npm
[[ -n $NODE_BIN ]] && NPM="$NODE_BIN/npm" && export PATH="$NODE_BIN:$PATH"
if [[ ! -x "$LLM_DIR/venv/bin/python" ]]; then
  python3 -m venv "$LLM_DIR/venv"
fi
"$LLM_DIR/venv/bin/pip" install --upgrade --quiet schemdraw matplotlib ||
  warn "Couldn't install schemdraw/matplotlib; schematics and timing diagrams won't work"
# Project files: PDF datasheets
"$LLM_DIR/venv/bin/pip" install --upgrade --quiet pypdf || pkg_install poppler ||
  warn "Couldn't install a PDF reader; PDF files in projects won't work"
mkdir -p "$LLM_DIR/mermaid" "$LLM_DIR/diagrams" "$LLM_DIR/images" "$LLM_DIR/wakewords" "$LLM_DIR/projects"
if ! "$NPM" install --prefix "$LLM_DIR/mermaid" --no-fund --no-audit @mermaid-js/mermaid-cli; then
  warn "Couldn't install Mermaid (needs Node.js 18.19 or newer); block diagrams won't work"
fi

# Hub: phone speech-to-text, chat sync between devices, and Stella's server tools
install_companion stellai_hub.py "$LLM_DIR/stellai_hub.py"
rm -f "$LLM_DIR/voice_bridge.py" "$LLM_DIR/laptop_hub.py"   # older versions

# 4. Download the model (big models come in several parts) ----------------------
fetch_model() {   # fetch_model URL DEST LABEL
  if [[ -s "$2" ]]; then echo "    $(basename "$2") already downloaded"; return 0; fi
  say "Downloading $3 - this can take a while"
  curl -L --fail --continue-at - -o "$2.part" "$1" && mv "$2.part" "$2"
}
read -r -a _URLS <<< "$MODEL_URLS"; read -r -a _PARTS <<< "$MODEL_PARTS"
for i in "${!_URLS[@]}"; do
  lbl=$MODEL_LABEL; (( ${#_URLS[@]} > 1 )) && lbl+=" (part $((i + 1)) of ${#_URLS[@]})"
  fetch_model "${_URLS[$i]}" "$LLM_DIR/models/${_PARTS[$i]}" "$lbl"
done

# 4a. The helper model (a small second model for quick background jobs)
HELPER_FILE=""
if [[ -n $HELPER_KEY ]]; then
  if H_JSON=$(python3 "$TUNE_PY" resolve "$HELPER_KEY" --quant "${HELPER_QUANT:-Q8_0}" 2>/dev/null); then
    H_URL=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["files"][0]["url"])' "$H_JSON")
    H_NAME=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["model_file"])' "$H_JSON")
    fetch_model "$H_URL" "$LLM_DIR/models/$H_NAME" "the helper model ($H_NAME)" && HELPER_FILE="$LLM_DIR/models/$H_NAME" \
      || warn "Couldn't download the helper model; the main model does its jobs"
  else
    warn "Couldn't look up the helper model on Hugging Face; the main model does its jobs"
  fi
fi

# 4b. Vision (photo understanding): the model's image encoder, used with --mmproj
MMPROJ_FILE="$LLM_DIR/models/${MMPROJ_NAME:-${MODEL_NAME%.gguf}.mmproj-F16.gguf}"
VISION_ARGS=""
if [[ ! -s "$MMPROJ_FILE" && -n $MMPROJ_URL ]]; then
  if ask_yn vision "Let Stella understand photos? Downloads the vision add-on (~0.6-0.9 GB)" y; then
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
# Remember the model in the library, so the phone app lists it and can switch back to it
if [[ -s "$RESOLVED_JSON" ]]; then
  PYTHONPATH="$LLM_DIR" python3 -c 'import json, sys, stellai_tune as t
t.record_model(sys.argv[1], json.load(open(sys.argv[2])), sys.argv[3])' "$LLM_DIR/models" "$RESOLVED_JSON" \
    "$([[ -s "$MMPROJ_FILE" ]] && basename "$MMPROJ_FILE")" || true
fi
# A prompt cache in RAM lets Stella pick up other chats without re-reading them; sized to the memory budget
SPARE_MB=$(( RAM_BUDGET_GB * 1024 - MODEL_MB - 2048 ))
if (( SPARE_MB >= 12288 )); then CACHE_RAM_MB=8192; elif (( SPARE_MB >= 6144 )); then CACHE_RAM_MB=4096
elif (( SPARE_MB >= 3072 )); then CACHE_RAM_MB=2048; else CACHE_RAM_MB=0; fi
SPEC_ARGS=""

# 5. Launcher ------------------------------------------------------------------
# Remember what's installed, for reference
# (the server launcher re-reads this on every start, and the phone app can switch models by rewriting it)
write_model_conf() {
  # Settings an earlier setup or the user added by hand (sleep time, vision on the GPU) are kept
  local keep=""
  [[ -f "$LLM_DIR/model.conf" ]] && keep=$(grep -E '^(SLEEP_IDLE_MIN|VISION_GPU)=' "$LLM_DIR/model.conf" || true)
  {
    printf 'MODEL_LABEL="%s"\nMODEL_FILE="%s"\nMODEL_KEY="%s"\nGPU_ARGS="%s"\nBACKEND="%s"\nNET_CONTEXT="%s"\n' \
      "$MODEL_LABEL" "$MODEL_FILE" "${MODEL_KEY:-}|${MODEL_QUANT:-}" "$GPU_ARGS" "$BACKEND" "$NET_CONTEXT"
    printf 'MMPROJ_FILE="%s"\nWHISPER_GPU="%s"\nSPEC_ARGS="%s"\nCACHE_RAM_MB="%s"\nRAM_BUDGET_GB="%s"\n' \
      "$([[ -s "$MMPROJ_FILE" ]] && echo "$MMPROJ_FILE")" "${WHISPER_GPU:-0}" "$SPEC_ARGS" "$CACHE_RAM_MB" "$RAM_BUDGET_GB"
    printf 'HELPER_FILE="%s"\nHELPER_GPU="%s"\n' "$HELPER_FILE" "$HELPER_GPU"
    [[ -n $LLAMA_GPUS_CONF ]] && printf 'LLAMA_GPUS="%s"\n' "$([[ $LLAMA_GPUS_CONF == none ]] && echo "-1" || echo "$LLAMA_GPUS_CONF")"
    [[ -n $keep ]] && printf '%s\n' "$keep"
  } > "$LLM_DIR/model.conf"
}
write_model_conf

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
[[ -d "$LLM_DIR/node/bin" ]] && export PATH="$LLM_DIR/node/bin:\$PATH"   # Node.js for Mermaid diagrams
IP=\$(hostname -I | awk '{print \$1}')
TS_IP=\$(tailscale ip -4 2>/dev/null | head -1)

# Firewall: Stella's two ports are open to your home network and to Tailscale only, never to everyone.
# (Rules from older versions, which allowed any address, are removed.)
if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  NETS="100.64.0.0/10 \$(ip -o -f inet addr show scope global 2>/dev/null \\
    | awk '\$2 !~ /^(tailscale|docker|br-|veth|virbr)/ {print \$4}' \\
    | python3 -c 'import ipaddress, sys
for line in sys.stdin:
    try:
        n = ipaddress.ip_interface(line.strip()).network
    except ValueError:
        continue
    if n.is_private:
        print(n)' | sort -u | tr '\n' ' ')"
  for P in $PORT 8092; do
    sudo ufw delete allow \$P/tcp >/dev/null 2>&1 || true
    for NET in \$NETS; do sudo ufw allow from "\$NET" to any port \$P proto tcp >/dev/null; done
  done
fi

set_gov() { for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo "\$1" | sudo tee "\$g" >/dev/null 2>&1; done; }
set_gov performance || true

# Moved: Stella lives on another server now ("move yourself to the server"). This computer is a client: only the
# hub runs, as a signpost that sends phones, browsers and avatar screens to her new address.
MOVED=""; [[ -f "$LLM_DIR/moved.json" ]] && MOVED=1
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
if [[ -z \$MOVED && -s "$EMBED_MODEL" ]]; then
  "$LLM_DIR/llama.cpp/build/bin/llama-server" -m "$EMBED_MODEL" --embedding --pooling mean -ngl 0 \\
    -c 8192 -b 2048 -ub 2048 -np 1 -t 2 --host 127.0.0.1 --port 8094 > "$LLM_DIR/embed.log" 2>&1 &
  EMBED_PID=\$!
  EXTRA_ARGS+=(--embed http://127.0.0.1:8094)
fi
if [[ -f "$LLM_DIR/searxng/settings.yml" ]]; then
  sudo -n docker start stellai-searxng >/dev/null 2>&1 || docker start stellai-searxng >/dev/null 2>&1 || true
  EXTRA_ARGS+=(--searxng http://127.0.0.1:8888)
fi
# The helper model (quick background jobs) on port 8095: on the processor, or on a card Stella's model doesn't use
HELPER_FILE=\$(sed -n 's/^HELPER_FILE="\(.*\)"/\1/p' "$LLM_DIR/model.conf")
HELPER_GPU=\$(sed -n 's/^HELPER_GPU="\(.*\)"/\1/p' "$LLM_DIR/model.conf")
if [[ -z \$MOVED && -n "\$HELPER_FILE" && -s "\$HELPER_FILE" ]]; then
  if [[ -n "\$HELPER_GPU" ]]; then H_DEV="CUDA_VISIBLE_DEVICES=\$HELPER_GPU"; H_NGL=99; else H_DEV="CUDA_VISIBLE_DEVICES=-1"; H_NGL=0; fi
  H_T=2; [[ \$THREADS -ge 8 ]] && H_T=4
  env \$H_DEV nice -n 4 "$LLM_DIR/llama.cpp/build/bin/llama-server" -m "\$HELPER_FILE" -c 8192 -np 2 -t \$H_T \
    -ngl \$H_NGL -fa on --cache-type-k q8_0 --cache-type-v q8_0 --jinja --host 127.0.0.1 --port 8095 \
    > "$LLM_DIR/helper.log" 2>&1 &
  HELPER_PID=\$!
  EXTRA_ARGS+=(--helper http://127.0.0.1:8095)
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
      --avatar-dir "$LLM_DIR/avatar" "\${UPDATE_ARGS[@]}" "\${EXTRA_ARGS[@]}" &
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
HUB_PID=\$!
HELPER_PIDS="\$HUB_PID \${EMBED_PID:-} \${HELPER_PID:-}"
WORKER_PIDS="\${EMBED_PID:-} \${HELPER_PID:-}"
if [[ -z \$MOVED && -x "$LLM_DIR/whisper.cpp/build/bin/whisper-server" && -s "$WHISPER_MODEL" ]]; then
  "$LLM_DIR/whisper.cpp/build/bin/whisper-server" -m "$WHISPER_MODEL" -t "\$THREADS" -bo 1 \\
    --host 127.0.0.1 --port 8093 > "$LLM_DIR/whisper.log" 2>&1 &
  HELPER_PIDS="\$HELPER_PIDS \$!"
  WORKER_PIDS="\$WORKER_PIDS \$!"
  HAS_WHISPER=1
fi
trap 'kill \$HELPER_PIDS 2>/dev/null; set_gov powersave || true' EXIT
# a client after a move: wait as a signpost; if Stella moves back here, start everything again
run_as_signpost() {
  kill \$WORKER_PIDS 2>/dev/null || true
  echo "================================================================"
  echo "  Stella moved to \$(python3 -c 'import json; print(json.load(open("'"$LLM_DIR/moved.json"'"))["name"])' 2>/dev/null || echo another server)."
  echo "  This computer is a client now: StellAI Web Chat opens her there."
  echo "================================================================"
  set_gov powersave || true
  wait \$HUB_PID
  if [[ ! -f "$LLM_DIR/moved.json" ]]; then echo "Stella is moving back here: starting everything"; exec "\$0"; fi
  exit 0
}
[[ -n \$MOVED ]] && run_as_signpost

echo "================================================================"
echo "  StellAI Server - enter these in the phone app's settings:"
if [[ -n "\$TS_IP" ]]; then
echo "  Server:   http://\$TS_IP:$PORT   (Tailscale: works at home and away)"
echo "            http://\$IP:$PORT   (home Wi-Fi only)"
else
echo "  Server:   http://\$IP:$PORT   (same Wi-Fi only; install Tailscale for away)"
fi
echo "  API key:  \$KEY"
if command -v qrencode >/dev/null; then
echo "  Or in the phone app: Settings → Scan to connect, and scan this:"
qrencode -t ANSIUTF8 -m 1 "stellai://pair?url=http%3A%2F%2F\${TS_IP:-\$IP}%3A$PORT&key=\$KEY"
fi
echo "  Desktop chat (any browser): http://\${TS_IP:-\$IP}:8092   (this PC: StellAI Web Chat in the menu)"
if [[ -z "\$HAS_WHISPER" ]]; then
echo "  (voice commands disabled: Whisper not installed - re-run the setup script)"
fi
echo "================================================================"

# Stella's model. model.conf is re-read on every start, so the phone app can switch models: it rewrites
# model.conf, creates .switch-model and stops llama-server, and this loop starts the new model.
rm -f "$LLM_DIR/.switch-model" "$LLM_DIR/.pause-model"
# The hub may pause the model (create .pause-model and stop llama-server) so a 3D shape job gets the graphics
# card; this loop waits, then loads the model again. The marker tells the hub this launcher can do that.
touch "$LLM_DIR/.launcher-can-pause"
# Newer llama-server options are only used when this build lists them (an unknown option would stop it starting).
LLAMA_HELP=\$("$LLM_DIR/llama.cpp/build/bin/llama-server" --help 2>&1 || true)
has_opt() { grep -q -- "\$1" <<< "\$LLAMA_HELP"; }
GPU_MB=\$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
while true; do
  source "$LLM_DIR/model.conf"
  CTX=\${NET_CONTEXT:-\$CONTEXT}
  VIS=""; FIT=""; VIS_GPU=0; IDLE=""
  if [[ -n "\${MMPROJ_FILE:-}" && -s "\${MMPROJ_FILE:-}" ]]; then
    VIS="--mmproj \$MMPROJ_FILE"; VIS_GPU=1
    # Graphics cards under 8 GB: the photo add-on (about 1 GB) runs on the CPU, so that memory holds more of the
    # model. Photos take a few seconds longer to read. To keep it on the GPU: add VISION_GPU="1" to model.conf
    if [[ "\$BACKEND" != cpu && "\${VISION_GPU:-0}" != 1 && "\${GPU_MB:-0}" =~ ^[0-9]+\$ && \${GPU_MB:-0} -gt 0 \\
          && \${GPU_MB:-0} -lt 7800 ]] && has_opt "--no-mmproj-offload"; then
      VIS="\$VIS --no-mmproj-offload"; VIS_GPU=0
    fi
  fi
  if [[ "\$BACKEND" != cpu ]]; then
    M=1024; [[ \$VIS_GPU == 1 ]] && M=\$((M + 1000)); [[ "\${WHISPER_GPU:-0}" == 1 ]] && M=\$((M + 700))
    FIT="--fit-target \$M"
  fi
  # The model leaves RAM and GPU memory after 30 quiet minutes and reloads on the next message (a few seconds).
  # Change it with SLEEP_IDLE_MIN="60" in model.conf; "0" keeps it loaded.
  SLEEP_MIN=\${SLEEP_IDLE_MIN:-30}
  if [[ "\$SLEEP_MIN" =~ ^[0-9]+\$ && \$SLEEP_MIN -gt 0 ]] && has_opt "--sleep-idle-seconds"; then
    IDLE="--sleep-idle-seconds \$((SLEEP_MIN * 60))"
  fi
  if [[ \$CTX -le 8192 ]]; then TB=1024; else TB=2048; fi
  # The speed-up the setup's test chose (MTP), and the RAM prompt cache, when this llama.cpp build has them
  SPEC=""; [[ -n "\${SPEC_ARGS:-}" ]] && has_opt "draft-mtp" && SPEC="\$SPEC_ARGS"
  CRAM=""; [[ "\${CACHE_RAM_MB:-}" =~ ^[0-9]+\$ ]] && has_opt "--cache-ram" && CRAM="--cache-ram \$CACHE_RAM_MB"
  # Two or more graphics cards: LLAMA_GPUS="0" in model.conf keeps Stella on card 0, leaving the others free
  # for 3D shape jobs and other tools. Empty = llama.cpp spreads her over every card.
  if [[ -n "\${LLAMA_GPUS:-}" ]]; then export CUDA_VISIBLE_DEVICES="\$LLAMA_GPUS"; else unset CUDA_VISIBLE_DEVICES; fi
  # nice 2: speech recognition and Stella's voice get the processor first while the model is writing
  KEY=\$(cat "$KEY_FILE")       # re-read: a move that arrives here brings its own key
  nice -n 2 "$LLM_DIR/llama.cpp/build/bin/llama-server" -m "\$MODEL_FILE" -c "\$CTX" \\
    -t "\$THREADS" -tb $LOGICAL_CPUS \$GPU_ARGS \$FIT \$VIS \$IDLE \$SPEC \$CRAM -fa on \\
    --reasoning-budget \$TB --reasoning-budget-message "I have thought enough; time to answer." \\
    --cache-type-k q8_0 --cache-type-v q8_0 --jinja --api-key "\$KEY" --host 0.0.0.0 --port $PORT &
  LLAMA_PID=\$!
  echo \$LLAMA_PID > "$LLM_DIR/llama.pid"
  wait \$LLAMA_PID
  if [[ -f "$LLM_DIR/.switch-model" ]]; then
    rm -f "$LLM_DIR/.switch-model"; echo "Switching to \$(sed -n 's/^MODEL_LABEL="\(.*\)"/\1/p' "$LLM_DIR/model.conf")..."
    sleep 2; continue
  fi
  if [[ -f "$LLM_DIR/moved.json" ]]; then run_as_signpost; fi
  if [[ -f "$LLM_DIR/.pause-model" ]]; then
    echo "Stella's model is paused while a 3D shape is made..."
    # never stay paused for more than 40 minutes, even if the hub forgets to say it's done
    while [[ -f "$LLM_DIR/.pause-model" ]] && [[ -z \$(find "$LLM_DIR/.pause-model" -mmin +40 2>/dev/null) ]]; do sleep 2; done
    rm -f "$LLM_DIR/.pause-model"; echo "Loading Stella's model again"; continue
  fi
  break
done
EOF
chmod +x "$LLM_DIR/start-server-network.sh"

# 5c. Test run: the real speed, whether the conversation length fits, and the MTP speed-up only if it helps -----
cat > "$LLM_DIR/stellai-tune.sh" <<EOF
#!/usr/bin/env bash
# Measures Stella's model on this computer and updates model.conf: the longest conversation window that loads
# (halving down to 8K if needed) and the MTP speed-up, kept only if it's at least 10% faster. Close StellAI Server
# first (the test needs the graphics cards to itself).
CONF="$LLM_DIR/model.conf"
source "\$CONF"
if pgrep -x llama-server >/dev/null; then echo "StellAI Server is running: close it first (as a service: sudo systemctl stop stellai), then run this again."; exit 1; fi
[[ -n "\${LLAMA_GPUS:-}" ]] && export CUDA_VISIBLE_DEVICES="\$LLAMA_GPUS"
ARGS="-fa on --cache-type-k q8_0 --cache-type-v q8_0 --jinja -t $PHYS_CORES -tb $LOGICAL_CPUS \${GPU_ARGS:-}"
if [[ "\${BACKEND:-cpu}" != cpu ]]; then
  M=1024; [[ -n "\${MMPROJ_FILE:-}" ]] && M=\$((M + 1000)); [[ "\${WHISPER_GPU:-0}" == 1 ]] && M=\$((M + 700))
  ARGS="\$ARGS --fit-target \$M"
fi
MTP=""
PYTHONPATH="$LLM_DIR" python3 -c 'import sys, stellai_tune as t
m = t.read_manifest(sys.argv[1]).get(sys.argv[2], {})
sys.exit(0 if m.get("mtp") else 1)' "$LLM_DIR/models" "\${MODEL_KEY:-}" && MTP="--mtp"
echo "Testing \$MODEL_LABEL (this loads the model a few times; big models take a few minutes each)..."
OUT=\$(python3 "$LLM_DIR/stellai_tune.py" bench --server "$LLM_DIR/llama.cpp/build/bin/llama-server" --model "\$MODEL_FILE" \\
  --ctx "\${NET_CONTEXT:-16384}" --args "\$ARGS" \$MTP --out "$LLM_DIR/speed-tune.json") || { echo "The test couldn't run."; exit 1; }
eval "\$OUT"
if [[ -n "\$TUNED_ERROR" ]]; then echo "The model didn't load: \$TUNED_ERROR"; exit 1; fi
SPEC=""; [[ "\$TUNED_MTP" == 1 ]] && SPEC="--spec-type draft-mtp --spec-draft-n-max 2"
sed -i -e "s/^NET_CONTEXT=.*/NET_CONTEXT=\"\$TUNED_CONTEXT\"/" -e "/^SPEC_ARGS=/d" "\$CONF"
echo "SPEC_ARGS=\"\$SPEC\"" >> "\$CONF"
PYTHONPATH="$LLM_DIR" python3 -c 'import json, sys, stellai_tune as t
d = t.read_manifest(sys.argv[1]); e = d.get(sys.argv[2])
if e:
    r = json.load(open(sys.argv[3])); e["tuned"] = {"context": r.get("context"), "mtp": r.get("mtp"), "tok_s": r.get("gen_tps")}
    t.write_manifest(sys.argv[1], d)' "$LLM_DIR/models" "\${MODEL_KEY:-}" "$LLM_DIR/speed-tune.json" 2>/dev/null || true
echo
echo "  Writes:        \$TUNED_TPS tokens/s"
echo "  Reads prompts: \$TUNED_PROMPT_TPS tokens/s"
echo "  Conversation:  \$((TUNED_CONTEXT / 1024))K tokens"
if [[ -n "\$MTP" ]]; then
  if [[ "\$TUNED_MTP" == 1 ]]; then echo "  MTP speed-up:  ON (\$TUNED_MTP_TPS tokens/s with it)"
  else echo "  MTP speed-up:  off (\${TUNED_MTP_TPS:-0} tokens/s with it: not faster here)"; fi
fi
EOF
chmod +x "$LLM_DIR/stellai-tune.sh"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/stellai-tune.desktop" <<EOF
[Desktop Entry]
Name=StellAI Speed Test
Comment=Measure Stella's model and keep the fastest settings (close StellAI Server first)
Exec=bash -c '$LLM_DIR/stellai-tune.sh; read -r -p "Press Enter to close. " _'
Icon=utilities-system-monitor
Terminal=true
Type=Application
Categories=Utility;
EOF
if [[ ${ANS[test]:-y} == y ]]; then
  if pgrep -x llama-server >/dev/null; then
    warn "StellAI Server is running, so the test would share the graphics cards. Close it, then run $LLM_DIR/stellai-tune.sh"
  else
    say "Test run: measuring Stella's model on this computer"
    if "$LLM_DIR/stellai-tune.sh" 2>&1 | tee "$LLM_DIR/speed-tune.log"; then
      ui_msg "Test results" "$(tail -n 6 "$LLM_DIR/speed-tune.log")

Saved in model.conf. Run $LLM_DIR/stellai-tune.sh again any time (close StellAI Server first)."
    else
      warn "The test didn't finish; StellAI uses the planned settings. Try again later: $LLM_DIR/stellai-tune.sh"
    fi
  fi
fi

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
cat > "$LLM_DIR/stellai-new-key.sh" <<EOF
#!/usr/bin/env bash
# Replaces Stella's API key. Every phone, tablet and browser has to be paired again afterwards, so use it when a
# device is lost or the key may have been seen by someone else. (Close StellAI Server first.)
set -e
if curl -fsS -m 3 http://127.0.0.1:8092/health >/dev/null 2>&1; then
  echo "Close StellAI Server first (Ctrl+C in its window), then run this again."; exit 1
fi
read -r -p "Replace the API key? Every device must be paired again. [y/N] " ans
[[ "\${ans,,}" == "y" ]] || { echo "Nothing changed."; exit 0; }
NEWKEY=\$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
( umask 077; printf '%s' "\${NEWKEY:0:32}" > "$KEY_FILE.new" )
mv -f "$KEY_FILE.new" "$KEY_FILE"
chmod 600 "$KEY_FILE"
echo "Done. Start StellAI Server: it shows the new key and a new QR code."
echo "On each phone: Settings, then Scan to connect. In a browser: open StellAI Web Chat from the menu again."
EOF
chmod +x "$LLM_DIR/stellai-new-key.sh"
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

# Avatar screen: Stella full screen with a voice (orb or your 3D character)
cat > "$LLM_DIR/open-avatar.sh" <<EOF
#!/usr/bin/env bash
# Opens Stella's avatar screen full screen (start "StellAI Server" first). Esc or F11 leaves full screen.
URL="http://127.0.0.1:8092/avatar#key=\$(cat "$KEY_FILE")"
for b in chromium chromium-browser google-chrome google-chrome-stable brave-browser; do
  if command -v "\$b" >/dev/null; then
    exec "\$b" --app="\$URL" --start-fullscreen --autoplay-policy=no-user-gesture-required \\
      --user-data-dir="$LLM_DIR/avatar-browser" --no-first-run
  fi
done
exec xdg-open "\$URL"   # other browsers: tap the screen once so Stella can talk
EOF
chmod +x "$LLM_DIR/open-avatar.sh"
# The avatar's shared folder ("bin"): drop characters (.vrm) and animations (.vrma/.fbx) here
mkdir -p "$LLM_DIR/avatar/bin/characters" "$LLM_DIR/avatar/bin/animations"
[[ -e "$HOME/StellAI Avatar Files" ]] || ln -s "$LLM_DIR/avatar/bin" "$HOME/StellAI Avatar Files"
cat > "$HOME/.local/share/applications/stellai-avatar.desktop" <<EOF
[Desktop Entry]
Name=StellAI Avatar
Comment=Stella full screen with a voice: talk with the microphone or Space
Exec=$LLM_DIR/open-avatar.sh
Icon=face-smile
Terminal=false
Type=Application
Categories=Utility;
EOF

# 5c. Optional: Tailscale (use the phone app away from home) ------------------
echo
if command -v tailscale >/dev/null; then
  say "Tailscale already installed: $(tailscale ip -4 2>/dev/null | head -1)"
else
  if ask_yn tailscale "Install Tailscale so the phone app works away from home?" y; then
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
  if ask_yn searxng "Install private web search for Stella (SearXNG in Docker, ~300 MB)? Without it she uses DuckDuckGo." y; then
    say "Installing SearXNG"
    if ! command -v docker >/dev/null; then pkg_install docker || warn "Couldn't install Docker"; fi
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

# 5e. Optional: Blender for the 3D Modeler mode ------------------------------
# Stella edits models in a hidden Blender on this computer. A portable copy goes in ~/llm/blender (nothing else
# on the system changes); the hub finds it, or any Blender already installed, by itself.
if [[ -x "$LLM_DIR/blender/blender" ]]; then
  say "Blender for the 3D Modeler mode already installed"
elif command -v blender >/dev/null 2>&1; then
  say "Using the Blender already on this computer for the 3D Modeler mode ($(command -v blender))"
else
  echo
  if ask_yn blender "Install Blender for Stella's 3D Modeler mode (~400 MB download, ~1.3 GB on disk)?" n; then
    say "Installing Blender"
    BL_URL=""
    if [[ $(uname -m) == x86_64 ]]; then
      for series in 4.5 4.2; do
        file=$(curl -fsSL "https://download.blender.org/release/Blender$series/" 2>/dev/null \
          | grep -oE "blender-$series\.[0-9]+-linux-x64\.tar\.xz" | sort -uV | tail -n1)
        if [[ -n "$file" ]]; then BL_URL="https://download.blender.org/release/Blender$series/$file"; break; fi
      done
    fi
    if [[ -n "$BL_URL" ]] && curl -fL --retry 3 -o "$LLM_DIR/blender.tar.xz" "$BL_URL"; then
      rm -rf "$LLM_DIR/blender" && mkdir -p "$LLM_DIR/blender"
      if tar -xJf "$LLM_DIR/blender.tar.xz" -C "$LLM_DIR/blender" --strip-components=1; then
        say "Blender installed in $LLM_DIR/blender"
      else
        warn "Couldn't unpack Blender; re-run this script to try again"
        rm -rf "$LLM_DIR/blender"
      fi
      rm -f "$LLM_DIR/blender.tar.xz"
    else
      warn "Couldn't download Blender from blender.org; trying the system package instead"
      pkg_install blender || warn "Blender isn't installed; the 3D Modeler mode will say so until it is"
    fi
    BL_EXE="$LLM_DIR/blender/blender"
    [[ -x "$BL_EXE" ]] || BL_EXE=$(command -v blender || true)
    if [[ -n "$BL_EXE" ]]; then
      if ask_yn vrm "Also add the VRM add-on (opens and saves VRoid/VRM characters properly)?" y; then
        "$BL_EXE" -b --online-mode -c extension sync >/dev/null 2>&1 && \
          "$BL_EXE" -b --online-mode -c extension install --enable vrm >/dev/null 2>&1 && \
          say "VRM add-on installed" || \
          warn "Couldn't add the VRM add-on automatically. Open $BL_EXE once, then Edit > Preferences > Get Extensions > search 'VRM' > Install. Without it, VRM files come in as plain glTF."
      fi
    fi
  fi
fi

# 5f. Optional: photo-to-3D generator (Hunyuan3D-2) for the 3D Modeler mode ------
# Makes rough 3D shapes from photos on the NVIDIA card. Own Python environment in ~/llm/shapegen (~6 GB with
# PyTorch, plus ~5 GB of model files on the first test). The hub pauses Stella's model during a job on a
# single card, or uses a second card if there is one.
SG="$LLM_DIR/shapegen"
if [[ -x "$SG/venv/bin/python" && -f "$SG/ok" ]]; then
  say "Photo-to-3D generator already installed"
elif [[ $GPU_VENDOR == nvidia && ${VRAM_GB:-0} -ge 8 ]]; then
  echo
  if ask_yn shapegen "Install the photo-to-3D generator for the 3D Modeler mode (~11 GB download, NVIDIA 8 GB+)?" n; then
    say "Installing the photo-to-3D generator (this takes a while)"
    pkg_install git python3-venv || true
    # PyTorch's default build needs a driver for CUDA 12; older drivers get the CUDA 11.8 build
    TORCH_INDEX=""; [[ -n "$NV_CUDA" && ${NV_CUDA%%.*} -lt 12 ]] && TORCH_INDEX="--index-url https://download.pytorch.org/whl/cu118"
    mkdir -p "$SG"
    if [[ -d "$SG/Hunyuan3D-2/.git" ]]; then git -C "$SG/Hunyuan3D-2" pull --ff-only || true
    else git clone --depth 1 https://github.com/Tencent-Hunyuan/Hunyuan3D-2 "$SG/Hunyuan3D-2"; fi
    if [[ -d "$SG/Hunyuan3D-2" ]] && python3 -m venv "$SG/venv" && \
       "$SG/venv/bin/pip" install --upgrade --quiet pip wheel && \
       "$SG/venv/bin/pip" install --quiet torch torchvision $TORCH_INDEX && \
       "$SG/venv/bin/pip" install --quiet -r "$SG/Hunyuan3D-2/requirements.txt" && \
       "$SG/venv/bin/pip" install --quiet -e "$SG/Hunyuan3D-2"; then
      mkdir -p "$LLM_DIR/3d"
      printf '{"python": "%s", "gpu": "auto"}\n' "$SG/venv/bin/python" > "$LLM_DIR/3d/generator.json"
      "$LLM_DIR/venv/bin/python" "$LLM_DIR/stellai_hub.py" --write-shape-script "$SG/stellai_shape.py" >/dev/null 2>&1 || \
        python3 "$LLM_DIR/stellai_hub.py" --write-shape-script "$SG/stellai_shape.py" >/dev/null
      say "Testing it (downloads the model the first time; a few minutes)"
      if (cd "$SG" && "$SG/venv/bin/python" "$SG/stellai_shape.py" --selftest 2>&1 | grep '@@SHAPE@@' | tail -n1 | grep -q '"ok": true'); then
        touch "$SG/ok"; say "Photo-to-3D generator works"
      else
        warn "The photo-to-3D test failed. Run: cd $SG && venv/bin/python stellai_shape.py --selftest   to see why."
      fi
    else
      warn "Couldn't install the photo-to-3D generator; re-run this script to try again"
    fi
  fi
fi
# (Which graphics cards Stella's model uses is chosen on the setup screens and kept in model.conf.)

# 5g. Optional: FreeCAD and KiCad for the CAD Designer and PCB Designer modes -----
if command -v freecadcmd >/dev/null 2>&1 || command -v FreeCADCmd >/dev/null 2>&1 || [[ -x "$LLM_DIR/freecad/FreeCAD.AppImage" ]]; then
  say "FreeCAD for the CAD Designer mode already installed"
else
  echo
  if ask_yn freecad "Install FreeCAD for Stella's CAD Designer mode (precise parts, 3D printing)?" n; then
    pkg_install freecad && say "FreeCAD installed" || warn "Couldn't install FreeCAD; get it from freecad.org (the AppImage can go in $LLM_DIR/freecad/FreeCAD.AppImage)"
  fi
fi
if command -v kicad-cli >/dev/null 2>&1; then
  say "KiCad for the PCB Designer mode already installed ($(kicad-cli version 2>/dev/null | head -n1))"
else
  echo
  if ask_yn kicad "Install KiCad for Stella's PCB Designer mode (circuit boards)?" n; then
    # KiCad's own PPA has the newest version on Ubuntu and Mint; the distro package is the fallback
    if command -v add-apt-repository >/dev/null 2>&1; then
      sudo add-apt-repository -y ppa:kicad/kicad-10.0-releases >/dev/null 2>&1 && sudo apt-get update -qq >/dev/null 2>&1 || true
    fi
    pkg_install kicad && say "KiCad installed" || warn "Couldn't install KiCad; get it from kicad.org"
  fi
fi
# Circuit as code for the PCB Designer: SKiDL (circuits written in Python) and Freerouting (the autorouter)
if command -v kicad-cli >/dev/null 2>&1; then
  if [[ -x "$LLM_DIR/skidl-venv/bin/python3" ]] && ls "$LLM_DIR"/freerouting/freerouting*.jar >/dev/null 2>&1; then
    say "Circuit-as-code tools (SKiDL + Freerouting) already installed"
  else
    echo
    if ask_yn circuits "Let Stella design circuits and route boards by herself (SKiDL + Freerouting autorouter, needs Java, ~150 MB)?" y; then
      if [[ ! -x "$LLM_DIR/skidl-venv/bin/python3" ]]; then
        python3 -m venv "$LLM_DIR/skidl-venv" && "$LLM_DIR/skidl-venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1
        "$LLM_DIR/skidl-venv/bin/pip" install -q skidl && say "SKiDL installed" || warn "Couldn't install SKiDL (pip install skidl)"
      fi
      if ! command -v java >/dev/null 2>&1; then
        case $PKG in
          apt) sudo apt-get install -y -qq default-jre-headless >/dev/null 2>&1 ;;
          dnf) sudo dnf install -y -q java-21-openjdk-headless >/dev/null 2>&1 ;;
          pacman) sudo pacman -S --noconfirm --needed jre-openjdk-headless >/dev/null 2>&1 ;;
          zypper) sudo zypper -n -q install java-21-openjdk-headless >/dev/null 2>&1 ;;
        esac
        command -v java >/dev/null 2>&1 && say "Java installed" || warn "Couldn't install Java 21; the autorouter needs it"
      fi
      if ! ls "$LLM_DIR"/freerouting/freerouting*.jar >/dev/null 2>&1; then
        mkdir -p "$LLM_DIR/freerouting"
        FR_URL=$(curl -fsSL https://api.github.com/repos/freerouting/freerouting/releases/latest 2>/dev/null \
          | grep -o '"browser_download_url": *"[^"]*\.jar"' | grep -v -i 'source' | head -n1 | sed 's/.*"\(http[^"]*\)"/\1/')
        if [[ -n "$FR_URL" ]] && curl -fsSL -o "$LLM_DIR/freerouting/$(basename "$FR_URL")" "$FR_URL"; then
          say "Freerouting installed ($(basename "$FR_URL"))"
        else
          warn "Couldn't download Freerouting; put freerouting-X.jar from github.com/freerouting/freerouting/releases in $LLM_DIR/freerouting/"
        fi
      fi
    fi
  fi
fi

# 6. Optional: Lenovo throttling fix ------------------------------------------
ans=n
if grep -qi thinkpad /sys/class/dmi/id/product_version 2>/dev/null; then
  echo
  ask_yn throttled "Install 'throttled' (fixes early CPU throttling on ThinkPads)?" n && ans=y
fi
if [[ "${ans,,}" == "y" ]]; then
  say "Installing throttled"
  if [[ $PKG == apt ]]; then
    sudo apt-get install -y python3-dev python3-venv python3-wheel libdbus-glib-1-dev \
      libgirepository1.0-dev libcairo2-dev python3-cairo-dev || \
      warn "Some throttled dependencies failed; install may not work"
  else
    warn "Install throttled's build dependencies for $DISTRO as listed in its README first"
  fi
  if [[ ! -d "$LLM_DIR/throttled" ]]; then
    git clone https://github.com/erpalma/throttled "$LLM_DIR/throttled"
  fi
  sudo "$LLM_DIR/throttled/install.sh" || warn "throttled install failed (see its GitHub README; Secure Boot can block it)"
fi

# 7. Optional: start StellAI Server at boot (servers, containers, no desktop) ------------------------------
if [[ ${ANS[service]:-n} == y ]]; then
  say "Setting up the StellAI service (starts by itself at boot)"
  sudo tee /etc/systemd/system/stellai.service >/dev/null <<EOF
[Unit]
Description=StellAI Server (Stella's model, chat sync, voice and tools)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$LLM_DIR
ExecStart=$LLM_DIR/start-server-network.sh
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now stellai.service && say "StellAI Server is running as a service" \
    || warn "The service didn't start; see: journalctl -u stellai -e"
  SVC_NOTE="  Service:         StellAI Server starts at boot. Status: systemctl status stellai
                   Pairing QR code / API key: journalctl -u stellai -e   (or cat $LLM_DIR/api-key.txt)
                   Stop it before StellAI Speed Test: sudo systemctl stop stellai"
fi

if [[ $ROLE == server ]]; then      # server only: no chat or avatar shortcuts on this computer
  rm -f "$HOME/.local/share/applications/stellai-chat.desktop" "$HOME/.local/share/applications/stellai-web-chat.desktop" \
        "$HOME/.local/share/applications/stellai-avatar.desktop"
fi
say "Done!"
[[ -n ${SVC_NOTE:-} ]] && echo "$SVC_NOTE"
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
