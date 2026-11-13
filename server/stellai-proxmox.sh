#!/usr/bin/env bash
# StellAI on Proxmox VE: run this ON THE PROXMOX HOST, as root (the Proxmox shell: node > Shell, or SSH).
#
# It does three things, and can be run again safely (it picks up where it stopped):
#   1. installs the NVIDIA driver on the host (with DKMS, so it's rebuilt after Proxmox kernel updates)
#   2. creates an Ubuntu 24.04 LXC container for Stella, with the graphics cards shared into it
#   3. runs the normal StellAI setup (stellai-setup.sh) inside that container
#
# Why a container and not a VM: containers share the host's driver, so no PCIe passthrough is needed (HP ProLiant
# Gen8 servers block that with their "RMRR" firmware problem), and several containers can use the same cards.
#
# Usage:  bash stellai-proxmox.sh            (stellai-setup.sh next to it is used; otherwise it's downloaded)
#         NVIDIA_VERSION=580.159.04 bash stellai-proxmox.sh   (a specific driver version)
set -euo pipefail

STELLAI_REPO="${STELLAI_REPO:-courier-iot/StellAI}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE=/root/.stellai-proxmox
mkdir -p "$STATE"

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m!!  %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mXX  %s\033[0m\n' "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run this as root on the Proxmox host (the node's Shell in the web interface)."
command -v pct >/dev/null && command -v pveversion >/dev/null || die "This isn't a Proxmox VE host (no pct/pveversion). On plain Linux, use stellai-setup.sh."

# ---- Screens (whiptail is part of Proxmox) --------------------------------------------------------------------
W=0; [[ -t 0 && -t 1 ]] && command -v whiptail >/dev/null && W=1
msg() { if (( W )); then whiptail --title "$1" --msgbox "$2" 20 78; else say "$1"; echo "$2" | sed 's/^/    /'; fi; }
yesno() {   # yesno TEXT [y|n]
  if (( W )); then
    if [[ ${2:-y} == y ]]; then whiptail --title "StellAI on Proxmox" --yesno "$1" 14 78
    else whiptail --title "StellAI on Proxmox" --defaultno --yesno "$1" 14 78; fi
  else
    local a; read -r -p "$1 [$([[ ${2:-y} == y ]] && echo Y/n || echo y/N)] " a || a=""
    a=${a,,}; [[ -z $a ]] && a=${2:-y}; [[ $a == y* ]]
  fi
}
ask() {   # ask VAR TEXT DEFAULT
  local out
  if (( W )); then out=$(whiptail --title "StellAI on Proxmox" --inputbox "$2" 12 78 "$3" 3>&1 1>&2 2>&3) || out=$3
  else read -r -p "$2 [$3]: " out || out=""; out=${out:-$3}; fi
  printf -v "$1" '%s' "$out"
}
menu() {   # menu VAR TEXT DEFAULT TAG DESC ...
  local __v=$1 text=$2 def=$3 out n i=1 tags=()
  shift 3
  if (( W )); then
    out=$(whiptail --title "StellAI on Proxmox" --default-item "$def" --menu "$text" 20 78 10 "$@" 3>&1 1>&2 2>&3) || out=$def
  else
    echo "  $text"
    while (( $# >= 2 )); do tags+=("$1"); printf '    %d) %s\n' "$i" "$2"; i=$((i + 1)); shift 2; done
    read -r -p "    Number [Enter = $def]: " n || n=""
    if [[ $n =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#tags[@]} )); then out=${tags[$((n - 1))]}; else out=$def; fi
  fi
  printf -v "$__v" '%s' "$out"
}

PVE_VER=$(pveversion | sed -n 's#^pve-manager/\([0-9.]*\).*#\1#p')
RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1048576 + 0.5}' /proc/meminfo)
CORES=$(nproc)
mapfile -t NV_PCI < <(lspci -nn 2>/dev/null | grep -i 'nvidia' | grep -Ei 'vga|3d|display' || true)

msg "StellAI on Proxmox" "Proxmox VE ${PVE_VER:-?}, ${CORES} CPU threads, ${RAM_GB} GB RAM.
NVIDIA cards found: ${#NV_PCI[@]}
$(printf '  %s\n' "${NV_PCI[@]:-}" | cut -c1-74)

This will:
 1. install the NVIDIA driver on this host (if it isn't already)
 2. create a container for Stella with the cards shared into it
 3. run the StellAI setup inside it

Nothing else on this host is changed. Your VMs keep running."

# ---- 1. The NVIDIA driver on the host ---------------------------------------------------------------------------
host_driver() { nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || true; }
DRIVER=$(host_driver)
if (( ${#NV_PCI[@]} == 0 )); then
  warn "No NVIDIA graphics card found: Stella will run on the processor in the container"
elif [[ -n $DRIVER ]]; then
  say "NVIDIA driver $DRIVER is already working on this host"
else
  say "Installing the NVIDIA driver on the host"
  # nouveau (the open driver) must be off first; that needs one reboot
  if [[ ! -f /etc/modprobe.d/blacklist-nouveau.conf ]]; then
    printf 'blacklist nouveau\noptions nouveau modeset=0\n' > /etc/modprobe.d/blacklist-nouveau.conf
    update-initramfs -u -k all >/dev/null
  fi
  if lsmod | grep -q '^nouveau'; then
    msg "Reboot needed" "The open 'nouveau' driver is switched off now, but it's still loaded.
Reboot this host (your VMs restart with it), then run this script again:

  bash $HERE/$(basename "$0")"
    exit 0
  fi
  apt-get update -qq
  # kernel headers for this and future Proxmox kernels (DKMS rebuilds the driver after kernel updates)
  apt-get install -y -qq build-essential dkms pkg-config wget curl \
    "proxmox-headers-$(uname -r)" >/dev/null 2>&1 \
    || apt-get install -y -qq build-essential dkms pkg-config wget curl "pve-headers-$(uname -r)" >/dev/null
  apt-get install -y -qq proxmox-default-headers >/dev/null 2>&1 || apt-get install -y -qq pve-headers >/dev/null 2>&1 || true
  VER=${NVIDIA_VERSION:-}
  if [[ -z $VER ]]; then
    VER=$(curl -fsSL https://download.nvidia.com/XFree86/Linux-x86_64/latest.txt 2>/dev/null | awk '{print $1}' || true)
    VER=${VER:-580.159.04}
  fi
  RUN="$STATE/NVIDIA-Linux-x86_64-$VER.run"
  [[ -s $RUN ]] || curl -fL -o "$RUN" "https://download.nvidia.com/XFree86/Linux-x86_64/$VER/NVIDIA-Linux-x86_64-$VER.run" \
    || die "Couldn't download NVIDIA driver $VER. Pick a version from https://www.nvidia.com/drivers and run: NVIDIA_VERSION=x.y.z bash $0"
  say "Building NVIDIA driver $VER for kernel $(uname -r) (a few minutes)"
  sh "$RUN" --dkms --silent --no-x-check --no-nouveau-check --no-questions \
    || die "The NVIDIA driver didn't install; see /var/log/nvidia-installer.log"
  DRIVER=$(host_driver)
  [[ -n $DRIVER ]] || die "The driver installed but nvidia-smi doesn't see the cards; reboot the host and run this script again."
  say "NVIDIA driver $DRIVER works on the host"
fi
if [[ -n $DRIVER ]]; then
  # load the modules at boot and make the device files the container needs (nvidia-uvm is what CUDA uses)
  mkdir -p /etc/modules-load.d
  printf 'nvidia\nnvidia_uvm\n' > /etc/modules-load.d/stellai-nvidia.conf
  mkdir -p /etc/udev/rules.d
  cat > /etc/udev/rules.d/70-stellai-nvidia.rules <<'EOF'
KERNEL=="nvidia", RUN+="/bin/bash -c '/usr/bin/nvidia-smi -L >/dev/null && /bin/chmod 0666 /dev/nvidia*'"
KERNEL=="nvidia_uvm", RUN+="/bin/bash -c '/usr/bin/nvidia-modprobe -c0 -u && /bin/chmod 0666 /dev/nvidia-uvm*'"
EOF
  modprobe nvidia_uvm 2>/dev/null || true
  nvidia-modprobe -c0 -u 2>/dev/null || true
  nvidia-smi -L >/dev/null 2>&1 || true
  chmod 0666 /dev/nvidia* 2>/dev/null || true
  # keep the driver ready between jobs (faster model loads)
  nvidia-smi -pm 1 >/dev/null 2>&1 || true
fi

# ---- 2. The container ----------------------------------------------------------------------------------------
CTID=""
[[ -s $STATE/ctid ]] && CTID=$(cat "$STATE/ctid")
if [[ -n $CTID ]] && pct status "$CTID" >/dev/null 2>&1; then
  say "Using the StellAI container $CTID from before"
else
  CTID=$(pvesh get /cluster/nextid 2>/dev/null || echo 200)
  ask CTID "Container ID for Stella" "$CTID"
  ask CT_NAME "Container name (its hostname on your network)" "stellai"
  RES_GB=$(( RAM_GB >= 64 ? 8 : 4 ))
  ask MEM_GB "RAM for Stella's container, in GB (the host and your VMs keep the rest; ${RAM_GB} GB total)" $(( RAM_GB - RES_GB ))
  ask CT_CORES "CPU threads for the container (${CORES} on this host)" "$CORES"
  mapfile -t STORES < <(pvesm status --content rootdir 2>/dev/null | awk 'NR > 1 && $3 == "active" {printf "%s\t%s %d GB free\n", $1, $2, $6 / 1048576}')
  (( ${#STORES[@]} )) || die "No storage here can hold containers (content type 'rootdir'). Add one under Datacenter > Storage."
  items=(); for l in "${STORES[@]}"; do items+=("${l%%$'\t'*}" "${l#*$'\t'}"); done
  menu STORAGE "Where should the container's disk go? Models are big (5-75 GB each): pick a fast SSD/NVMe if you have one." "${items[0]}" "${items[@]}"
  ask DISK_GB "Disk size for the container in GB (room for the models you want: 35B is ~22 GB, 122B ~45-75 GB)" 200
  mapfile -t BRIDGES < <(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | cut -d@ -f1)
  ask BRIDGE "Network bridge (${BRIDGES[*]:-vmbr0})" "${BRIDGES[0]:-vmbr0}"
  ask IPCONF "Address: dhcp, or a fixed one like 192.168.1.50/24,gw=192.168.1.1 (fixed is easier for the phone app)" "dhcp"
  if [[ $IPCONF == dhcp ]]; then NET="name=eth0,bridge=$BRIDGE,ip=dhcp"
  else NET="name=eth0,bridge=$BRIDGE,ip=${IPCONF%%,gw=*}$([[ $IPCONF == *gw=* ]] && echo ",gw=${IPCONF##*gw=}")"; fi

  say "Getting the Ubuntu 24.04 container template"
  pveam update >/dev/null 2>&1 || true
  TEMPLATE=$(pveam available --section system 2>/dev/null | awk '/ubuntu-24.04-standard/ {print $2}' | sort -V | tail -1)
  [[ -n $TEMPLATE ]] || die "Couldn't find the Ubuntu 24.04 template (pveam available). Check the host's internet connection."
  TSTORE=$(pvesm status --content vztmpl 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1; exit}')
  TSTORE=${TSTORE:-local}
  pveam list "$TSTORE" 2>/dev/null | grep -q "$TEMPLATE" || pveam download "$TSTORE" "$TEMPLATE"

  say "Creating container $CTID ($CT_NAME)"
  pct create "$CTID" "$TSTORE:vztmpl/$TEMPLATE" --hostname "$CT_NAME" --cores "$CT_CORES" \
    --memory $(( MEM_GB * 1024 )) --swap 4096 --rootfs "$STORAGE:$DISK_GB" --net0 "$NET" \
    --unprivileged 1 --features nesting=1,keyctl=1 --onboot 1 --ostype ubuntu --start 0
  echo "$CTID" > "$STATE/ctid"
fi

# Share the cards (and /dev/net/tun for Tailscale) into the container
CONF=/etc/pve/lxc/$CTID.conf
DEVS=()
for d in /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset /dev/nvidia[0-9]* /dev/nvidia-caps/*; do
  [[ -e $d ]] && DEVS+=("$d")
done
[[ -e /dev/net/tun ]] && DEVS+=(/dev/net/tun)
if (( ${#DEVS[@]} )); then
  # newer Proxmox (8.1+) has managed device entries; older needs raw LXC lines
  if dpkg --compare-versions "${PVE_VER:-0}" ge 8.1; then
    sed -i '/^dev[0-9]*:/d' "$CONF"
    i=0
    for d in "${DEVS[@]}"; do pct set "$CTID" "--dev$i" "$d,mode=0666" >/dev/null; i=$((i + 1)); done
  else
    sed -i '/# stellai-devices/,/# stellai-devices-end/d' "$CONF"
    {
      echo "# stellai-devices"
      for maj in $(for d in "${DEVS[@]}"; do stat -c '%t' "$d"; done | sort -u); do
        echo "lxc.cgroup2.devices.allow: c $((16#$maj)):* rwm"
      done
      for d in "${DEVS[@]}"; do echo "lxc.mount.entry: $d ${d#/} none bind,optional,create=file"; done
      echo "# stellai-devices-end"
    } >> "$CONF"
  fi
  say "Shared into the container: ${DEVS[*]}"
fi

pct status "$CTID" | grep -q running || pct start "$CTID"
say "Waiting for the container's network"
for _ in $(seq 1 60); do pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 && break; sleep 2; done

# ---- 3. StellAI inside the container --------------------------------------------------------------------------
say "Preparing the container (user 'stellai', basic tools)"
pct exec "$CTID" -- bash -c 'export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq sudo curl ca-certificates whiptail pciutils git >/dev/null
  id stellai >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo stellai
  echo "stellai ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/stellai && chmod 440 /etc/sudoers.d/stellai'
# the same driver version's programs and libraries (no kernel part: the host's is used)
if [[ -n $DRIVER ]]; then
  RUN="$STATE/NVIDIA-Linux-x86_64-$DRIVER.run"
  [[ -s $RUN ]] || curl -fL -o "$RUN" "https://download.nvidia.com/XFree86/Linux-x86_64/$DRIVER/NVIDIA-Linux-x86_64-$DRIVER.run" || true
  if [[ -s $RUN ]]; then
    pct push "$CTID" "$RUN" "/home/stellai/llm/NVIDIA-Linux-x86_64-$DRIVER.run" --user 1000 --group 1000 2>/dev/null \
      || { pct exec "$CTID" -- mkdir -p /home/stellai/llm; pct exec "$CTID" -- chown stellai:stellai /home/stellai/llm
           pct push "$CTID" "$RUN" "/home/stellai/llm/NVIDIA-Linux-x86_64-$DRIVER.run"; }
  fi
fi
# the version the container must match (a fallback for when /proc/driver/nvidia isn't visible inside it)
[[ -n $DRIVER ]] && pct exec "$CTID" -- bash -c "mkdir -p /home/stellai/llm && echo $DRIVER > /home/stellai/llm/.host-nvidia-version"
SETUP="$HERE/stellai-setup.sh"
if [[ ! -s $SETUP ]]; then
  SETUP="$STATE/stellai-setup.sh"
  curl -fsSL -o "$SETUP" "https://raw.githubusercontent.com/$STELLAI_REPO/HEAD/server/stellai-setup.sh" \
    || die "Couldn't get stellai-setup.sh; put it next to this script and run it again."
fi
pct push "$CTID" "$SETUP" /home/stellai/stellai-setup.sh
pct exec "$CTID" -- chown -R stellai:stellai /home/stellai

say "Starting the StellAI setup inside container $CTID"
echo "    (the same screens as on a normal Linux PC; choose 'Start StellAI Server by itself' under Extras)"
pct exec "$CTID" -- su - stellai -c "ANSWER_service=y bash ~/stellai-setup.sh" || {
  warn "The setup inside the container stopped. Open it again with:  pct enter $CTID   then:  su - stellai -c 'bash ~/stellai-setup.sh'"
  exit 1
}

IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
KEY=$(pct exec "$CTID" -- cat /home/stellai/llm/api-key.txt 2>/dev/null || true)
msg "StellAI is running" "Container $CTID is Stella's server. It starts with the host.

  Server address:  http://${IP:-CONTAINER-IP}:8080
  API key:         ${KEY:-run: pct exec $CTID -- cat /home/stellai/llm/api-key.txt}
  Desktop chat:    http://${IP:-CONTAINER-IP}:8092

Open a shell in it:   pct enter $CTID   (then: su - stellai)
Its log:              pct exec $CTID -- journalctl -u stellai -e

If you update the NVIDIA driver on this host later, run this script again: it updates the container to match."
