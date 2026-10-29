# Laptop AI

An Android voice assistant and chat app powered by the AI model on your own laptop (llama.cpp + Whisper). Nothing goes to the cloud.

- **Wake word**: say "Hey Jarvis" (or "Alexa" / "Hey Mycroft") and give a command, hands-free.
- **Default assistant**: long-press the power or home button to talk, instead of Google.
- **Phone actions**: alarms, timers, opening apps, maps, flashlight, volume, battery, plus drafting texts, dialing and calendar events (which you confirm).
- **Spoken replies** through the phone's text-to-speech voice.
- **Chats sync** across every phone and tablet, stored on the laptop.

## Get the APK (no PC tools needed)

1. Create a GitHub repository and upload this folder's contents, including the hidden `.github` folder.
2. The **Actions** tab runs "Build APK" on every push (~5–8 minutes). It also downloads the openWakeWord models into the app.
3. Open the finished run, download **laptop-ai-apk** under Artifacts, unzip it on your phone, and install `app-debug.apk`.

To install a newer build over an older one, you may need to uninstall the old one first. Your chats are safe on the laptop and come back after a sync.

## Set up

1. On the laptop or desktop (whichever is your server), run **Local AI Server (Phone)** (created by `local-ai-setup.sh`). The terminal shows the **server address** and **API key**.
2. In the app's **Settings**, enter both and tap **Test connection**. Both lines should show ✓.
3. **Use the Tailscale address** (`http://100.x.x.x:8080`) so the app works both at home and away. Install Tailscale on the phone and sign in with the same account as the laptop.

### Make it your voice assistant

In Settings → **Voice assistant**:

1. Turn on **Wake word**, pick a phrase, and allow microphone and notifications.
2. Tap **Allow background actions** and allow "Display over other apps". Without it, Android stops voice commands from opening apps while Laptop AI isn't on screen.
3. Tap **Open assistant settings** → **Digital assistant app** → choose **Laptop AI**. Long-pressing power or home now starts listening.
4. Save.

A notification, "Listening for Hey Jarvis", stays while the wake word is on. Android requires it. Tap **Stop listening** in it to turn listening off.

**After a reboot, open the app once** to restart wake-word listening. Android doesn't let apps start the microphone in the background on their own.

### How a voice command flows

Wake word heard on the phone (beep) → you speak → it stops after a short pause (second beep) → the audio goes to Whisper on the laptop → the text goes to the AI → the AI runs phone actions and answers out loud.

## Sync

Each device keeps its own copy of the chats and syncs with the laptop when it opens, after every reply, and every minute while in use. Open the **☰ menu** to switch chats, start a new one, delete one (deleted everywhere), or sync now. Any phone or tablet with the same server address and API key sees the same chats.

## Adding your own phone actions

All tools live in `app/src/main/java/com/localai/remote/PhoneTools.kt`: add a definition in `definitions()` and a branch in `execute()`. Push, and GitHub builds a new APK.

## Limits worth knowing

- The assistant needs the laptop on and reachable. If it isn't, the app says it can't reach the laptop.
- Phone-based wake word detection uses more battery than "Hey Google", which runs on a dedicated low-power chip that only Google's app can use.
- Small local models sometimes pick the wrong action. Check the ✓/✗ line after each one.
- While a call or another app is using the microphone, Android silences the wake word.
