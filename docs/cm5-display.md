# Stella's screen on a Raspberry Pi CM5 (official IO Board)

A dedicated always-on screen for the avatar: it starts full screen at login, listens for the wake word, and talks. The CM5 only draws the avatar and listens for the wake word. The AI, the voice and speech recognition run on the StellAI server.

## Parts

| Part | Recommendation |
|---|---|
| Module | CM5 Lite (no eMMC), 4 GB RAM or more. 2 GB is tight with the 3D character. |
| Board | Official CM5 IO Board |
| Storage | NVMe SSD in the IO Board's M.2 slot (fastest), or a microSD card (the Lite needs one or the other) |
| Cooling | CM5 heatsink with a fan on the IO Board's fan header. Drawing the 3D character all day keeps the chip busy. |
| Screen | Any HDMI monitor. Its speakers can carry Stella's voice. |
| Sound + mic | A USB speakerphone (a conference "puck"): mic and speaker in one, with echo cancellation, so Stella doesn't hear herself. Or a USB mic plus HDMI or USB speakers. The CM5 has no headphone jack. |
| Optional | A USB button that sends the Space key, for push-to-talk. |
| Power | The IO Board's USB-C power supply (5 V, 5 A recommended with an NVMe and USB audio). |

## 1. Install Raspberry Pi OS

1. In **Raspberry Pi Imager**, choose **Raspberry Pi OS (64-bit)**, the version *with desktop*.
2. Click the gear / "Edit settings" button and set the hostname (e.g. `stella-screen`), your username and password, Wi-Fi, and the time zone.
3. Write to the NVMe or the microSD card:
   - **NVMe:** use a USB adapter to write it, or first boot from a microSD card and copy across with *SD Card Copier*.
   - **NVMe boot order:** if the board doesn't boot from NVMe, run `sudo raspi-config` → Advanced Options → Boot Order → NVMe/USB.
4. Boot it, finish the welcome wizard, then run:
   ```
   sudo apt update && sudo apt full-upgrade -y
   ```

## 2. Run the display setup

```
wget https://raw.githubusercontent.com/courier-iot/StellAI/main/server/stellai-display-setup.sh
bash stellai-display-setup.sh
```

It asks for the server address and API key (the StellAI Server window shows both). Then it:
- installs Chromium if needed;
- writes the start-up file that opens the avatar full screen at login, with the microphone and sound allowed automatically;
- turns off screen blanking and turns on desktop auto-login;
- lists your speakers and microphone so you can check them.

Reboot with `sudo reboot`. Stella appears after the desktop loads.

## 3. Tune it

Tap the gear (top right) on the avatar screen. Settings are saved on the server and shared by every avatar screen.
- **Look:** Space orb or 3D character (upload a female `.vrm`), height (default 5′8″) and camera framing.
- **Animations:** built-in gestures, plus your own `.vrma` or Mixamo `.fbx` files, and when each plays.
- **Voice:** choice of voice, speed, and whether she also speaks replies to the phone.
- **Wake word:** the word and its sensitivity. Raise the sensitivity if she misses you; lower it if she wakes by mistake.
- **Light mode:** automatic on a Pi. It draws at 30 frames a second with fewer pixels, which keeps the CM5 smooth and cool.

## Sound tips

- **Choosing outputs:** `wpctl status` lists outputs ("Sinks") and inputs ("Sources"); the default is marked `*`. Change it with `wpctl set-default <ID>`, or click the speaker icon in the taskbar.
- **She wakes herself up:** use a speakerphone with echo cancellation, move the speaker away from the mic, or lower the wake word sensitivity.
- **She doesn't hear you:** raise the sensitivity, check the mic level in the taskbar sound settings, or use Space or a USB button.

## Expected performance

| What | CM5 / Pi 5 |
|---|---|
| Space orb | smooth at 1080p |
| 3D character, light mode | about 30 fps at 720p–1080p with a typical VRoid model |
| 3D character, full quality | about 20–30 fps at 1080p |
| Wake word | a small share of one CPU core |

## Changing things later

- **Server moved:** edit the address in `~/stellai-display.sh`.
- **Stop it starting at login:** remove the `stellai-display` line from `~/.config/labwc/autostart`.
- **Leave full screen:** Alt+F4.
