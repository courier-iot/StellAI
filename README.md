# StellAI

Meet **Stella**, your personal voice assistant and chat app, powered by the AI model on your own computer (llama.cpp + Whisper). Nothing goes to the cloud. You can rename her in Settings → Assistant.

- **Wake word**: say "Hey Jarvis" (or "Alexa" / "Hey Mycroft") and give a command, hands-free.
- **Default assistant**: long-press the power or home button to talk, instead of Google.
- **Phone actions**: alarms, timers, opening apps, maps, flashlight, volume, battery, plus drafting texts, dialing and calendar events (which you confirm).
- **Spoken replies** through the phone's text-to-speech voice.
- **Chats sync** across every phone and tablet, stored on your server.
- **Space theme**: starfield background, HUD-style chat, glowing mic, and 8 accent colors (Settings → Assistant → Theme color).

## Get the APK (no PC tools needed)

1. Create a GitHub repository and upload this folder's contents, including the hidden `.github` folder.
2. The **Actions** tab runs "Build APK" on every push (~5–8 minutes). It also downloads the openWakeWord models into the app.
3. Open the finished run, download **stellai-apk** under Artifacts, unzip it on your phone, and install `app-debug.apk`.

To install a newer build over an older one, you may need to uninstall the old one first. Your chats are safe on the server and come back after a sync.

## The server

The AI runs on your computer. Pick one:

- **Linux** (Linux Mint, Ubuntu): run `server/stellai-setup.sh`. It detects your hardware, builds llama.cpp for your GPU and creates the **StellAI Server** launcher.
- **Windows 10/11 (preview)**: install `StellAI-Setup.exe` from the "Build Windows installer" Actions run. See [windows/README.md](windows/README.md).

### Automatic updates

Both servers keep themselves up to date from this GitHub repo, checking every 30 minutes. They only install an update when Stella has been idle for a couple of minutes, and your chats, memories, projects, models and settings are never touched.

- **Linux**: the server program (`stellai_hub.py`) downloads the new version, tests it, then restarts itself. If a new version fails to start, it goes back to the previous one automatically and skips that version. To turn updates off: `touch ~/llm/no-auto-update`. Changes to the setup script itself (new engine builds, launchers) still need a re-run of `stellai-setup.sh`.
- **Windows**: every build on the main branch is published as a release (`windows-v0.1.N`). The app downloads it, checks its checksum, and reinstalls itself silently, without an admin prompt. See *Options* in the app to check now or turn automatic installs off.

## Set up

1. On the laptop or desktop (whichever is your server), start **StellAI Server**. It shows the **server address** and **API key** (on Windows, also a QR code: scan it with the phone's camera and tap **Connect**).
2. In the app's **Settings**, enter both and tap **Test connection**. Both lines should show ✓.
3. **Use the Tailscale address** (`http://100.x.x.x:8080`) so the app works both at home and away. Install Tailscale on the phone and sign in with the same account as the server.

### Make it your voice assistant

In Settings → **Voice assistant**:

1. Turn on **Wake word**, pick a phrase, and allow microphone and notifications.
2. Tap **Allow background actions** and allow "Display over other apps". Without it, Android stops voice commands from opening apps while StellAI isn't on screen.
3. Tap **Open assistant settings** → **Digital assistant app** → choose **StellAI**. Long-pressing power or home now starts listening.
4. Save.

A notification, "Listening for Hey Jarvis", stays while the wake word is on. Android requires it. Tap **Stop listening** in it to turn listening off.

**After a reboot, open the app once** to restart wake-word listening. Android doesn't let apps start the microphone in the background on their own.

### How a voice command flows

Wake word heard on the phone (beep) → you speak → it stops after a short pause (second beep) → the audio goes to Whisper on the server → the text goes to the AI → the AI runs phone actions and answers out loud.

## Projects

Group related chats (say "Power supply PCB" or "StellAI app") and give them shared context. Open the menu ☰ → **PROJECTS +**.

- **Instructions**: how Stella should work in this project ("3.3 V logic, KiCad 9, 0805 parts").
- **Notes**: short facts she always knows here. She adds notes herself when you say things like "note that we picked the TPS5430".
- **Files**: datasheets (PDF), code, text, Word documents and images. Tap a file to see the text Stella reads from it. Small files are given to her whole; for big ones she gets the parts related to your question and can search the rest herself.
- **Chats**: start one from the project, or move any chat in with ⋮ → *Move to project…*. Project chats show a ◆ tag.

Projects are stored on the server, so every phone and tablet sees the same ones.

## Context meter and refresh

Stella can only keep a limited amount of a chat in mind at once (her *context*: about 8K tokens on the laptop, 16K with the 35B model). The bar under the top bar shows how full it is.

When it gets near full, she **refreshes** automatically: she summarizes the older part of the chat and continues from that summary, keeping the most recent messages word for word. A "Context refreshed" card marks the spot; tap it to read the summary. You can refresh any time with ⋮ → **Refresh context** (handy when switching topics). Every message stays visible and synced; only what Stella is sent gets shorter.

## Sync

Each device keeps its own copy of the chats and syncs with the server when it opens, after every reply, and every minute while in use. Open the **☰ menu** to switch chats, start a new one, delete one (deleted everywhere), or sync now. Any phone or tablet with the same server address and API key sees the same chats.

## Memory

Stella keeps a list of lasting facts about you on the server, shared by all your devices: your projects, tools, preferences and routines. She saves facts when you say "remember…", and with **Learn from conversations** on (Settings → Memory) the server picks out new facts once a chat has been quiet for about a minute. Review, add or delete them under Settings → Memory → View and edit memories.

## Photos

Tap 📷 next to the message box to take a photo or choose one. Stella can read circuits, error screens, sketches and datasheet tables. This needs the vision add-on on the server: re-run `stellai-setup.sh` and answer yes when it asks. Photos are resized to 1280 px and shared with your other devices.

## Message actions

Long-press any message to **Copy**, **Share**, **Retry** (Stella's last reply) or **Edit & resend** (your messages). Editing replaces that message and everything after it, on all your devices.

## Custom wake word ("Hey Stella")

Train a model (see below), then copy the `.onnx` file into `~/llm/wakewords/` on the server. It shows up with a ✦ in Settings → Voice assistant → Wake word, and the app downloads it the first time you use it.

Training: open the [openwakeword-colab-2026 notebook](https://colab.research.google.com/github/alfiedennen/openwakeword-colab-2026/blob/main/train_wakeword.ipynb) in Google Colab, set `TARGET_PHRASE = "hey stella"` and `MODEL_NAME = "hey_stella"`, choose a GPU runtime and **Run all**. It takes about 1.5 hours on Colab Pro, or about 2.5 hours on the free tier (keep the tab open), and downloads `hey_stella.onnx` at the end.

## Adding your own phone actions

All tools live in `app/src/main/java/com/stellai/app/PhoneTools.kt`: add a definition in `definitions()` and a branch in `execute()`. Push, and GitHub builds a new APK.

## Limits worth knowing

- Stella needs the server on and reachable. If it isn't, the app says it can't reach the server.
- Phone-based wake word detection uses more battery than "Hey Google", which runs on a dedicated low-power chip that only Google's app can use.
- Small local models sometimes pick the wrong action. Check the ✓/✗ line after each one.
- While a call or another app is using the microphone, Android silences the wake word.
