#!/usr/bin/env bash
# StellAI display: turns a Raspberry Pi (Pi 4/5, CM4/CM5) running Raspberry Pi OS with desktop into
# Stella's avatar screen. It opens full screen at login, listens for the wake word, and talks.
#
#   wget https://raw.githubusercontent.com/courier-iot/StellAI/main/server/stellai-display-setup.sh
#   bash stellai-display-setup.sh
#
# You need the server's address and API key (StellAI Server shows them; the Tailscale address works too).
set -euo pipefail
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

[[ $EUID -ne 0 ]] || { echo "Run this as your normal user (not with sudo); it asks for the password when needed."; exit 1; }

say "StellAI display setup"
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "this computer")
echo "    Device: $MODEL"
read -r -p "Server address (e.g. 192.168.1.20 or stellai-server): " HOST
HOST=${HOST#http://}; HOST=${HOST#https://}; HOST=${HOST%%/*}; HOST=${HOST%%:*}
[[ -n $HOST ]] || { echo "No address given."; exit 1; }
read -r -s -p "API key: " KEY; echo
[[ -n $KEY ]] || { echo "No key given."; exit 1; }
BASE="http://$HOST:8092"

say "Checking the server"
if curl -fsS -m 5 "$BASE/health" >/dev/null; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Authorization: Bearer $KEY" "$BASE/avatar/config")
  [[ $code == 200 ]] || warn "The server answered, but the API key was refused (HTTP $code). Check the key."
else
  warn "Couldn't reach $BASE. Make sure StellAI Server is running; continuing anyway."
fi

say "Installing the browser"
BROWSER=""
for b in chromium chromium-browser; do command -v "$b" >/dev/null && BROWSER=$b && break; done
if [[ -z $BROWSER ]]; then
  sudo apt-get update && (sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser)
  for b in chromium chromium-browser; do command -v "$b" >/dev/null && BROWSER=$b && break; done
fi
[[ -n $BROWSER ]] || { echo "Couldn't install Chromium."; exit 1; }

say "Making it start at login"
LAUNCH="$HOME/stellai-display.sh"
cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
# Opens Stella's avatar screen full screen. Edit the address here if the server moves.
sleep 4   # give the network a moment after login
exec $BROWSER --kiosk --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \\
  --autoplay-policy=no-user-gesture-required --use-fake-ui-for-media-stream \\
  --unsafely-treat-insecure-origin-as-secure=$BASE --user-data-dir="\$HOME/.stellai-display" \\
  --password-store=basic "$BASE/avatar#key=$KEY"
EOF
chmod 700 "$LAUNCH"
# Wayland desktop (Raspberry Pi OS Bookworm and newer, labwc)
mkdir -p "$HOME/.config/labwc"
touch "$HOME/.config/labwc/autostart"
grep -q stellai-display "$HOME/.config/labwc/autostart" || echo "$LAUNCH &" >> "$HOME/.config/labwc/autostart"
# Older X11 desktop (LXDE)
mkdir -p "$HOME/.config/lxsession/LXDE-pi"
if [[ -f /etc/xdg/lxsession/LXDE-pi/autostart && ! -f "$HOME/.config/lxsession/LXDE-pi/autostart" ]]; then
  cp /etc/xdg/lxsession/LXDE-pi/autostart "$HOME/.config/lxsession/LXDE-pi/autostart"
fi
touch "$HOME/.config/lxsession/LXDE-pi/autostart"
grep -q stellai-display "$HOME/.config/lxsession/LXDE-pi/autostart" || echo "@$LAUNCH" >> "$HOME/.config/lxsession/LXDE-pi/autostart"

say "Keeping the screen on and logging in automatically"
if command -v raspi-config >/dev/null; then
  sudo raspi-config nonint do_blanking 1 || warn "Couldn't turn off screen blanking (Raspberry Pi Configuration → Display)"
  sudo raspi-config nonint do_boot_behaviour B4 || warn "Couldn't turn on desktop auto-login (Raspberry Pi Configuration → System)"
else
  warn "raspi-config not found: turn off screen blanking and turn on auto-login in the system settings"
fi

say "Sound and microphone"
if command -v wpctl >/dev/null; then
  wpctl status 2>/dev/null | sed -n '/Sinks:/,/Sources:/p' | head -12 || true
  echo "    (The default output is marked *. Change it with: wpctl set-default ID)"
fi
arecord -l 2>/dev/null | grep -q card && echo "    Microphone found." || warn "No microphone found: plug in a USB microphone or speakerphone."

cat <<EOF

Done. Reboot to start Stella's screen:  sudo reboot

  - Say the wake word (default "Hey Jarvis"), or press Space, to talk.
  - Change the character, height, voice, wake word or light mode with the ⚙ button (top right).
  - To leave the full screen: Alt+F4. To stop it starting: remove the line from ~/.config/labwc/autostart
EOF
