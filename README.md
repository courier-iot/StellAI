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

## Set up

1. On the laptop or desktop (whichever is your server), run **StellAI Server** (created by `stellai-setup.sh`). The terminal shows the **server address** and **API key**.
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
