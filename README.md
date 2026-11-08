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

### Tuned automatically for your hardware

The setup (Linux script or Windows installer) checks the computer and sets things up for it:

- **Slow computer?** A 2-core machine without a usable graphics card (like the T470) is offered *client mode*: it just opens Stella on your stronger server, with the same chats and much faster answers, and installs nothing big.
- **Model quality:** for the 35B model it picks the most accurate version that fits RAM + GPU memory (3-bit at ~24 GB total, 4-bit at ~30 GB, high-quality 4-bit at ~40 GB, e.g. a Tesla T4 server).
- **GPU use:** llama.cpp fits as much of the model onto the graphics card as it can (leaving room for the vision add-on and voice), instead of a fixed split.
- **Threads:** physical cores for writing replies, all threads for reading long prompts.
- **Voice:** Whisper runs on the graphics card when there is one, with quick single-pass decoding.
- **Thinking:** Settings → *Thinking before answering*: **Auto** (default) thinks only when a question looks hard (debugging, maths, design, long questions) and answers simple things instantly. Thinking is capped (1K tokens on small setups, 2K on bigger ones) so it can't fill Stella's memory.
- **Sampling:** Qwen's recommended settings for Qwen3.5 are used (precise for code; a mild repetition penalty elsewhere).

### Automatic updates

Both servers keep themselves up to date from this GitHub repo, checking every 30 minutes. They only install an update when Stella has been idle for a couple of minutes, and your chats, memories, projects, models and settings are never touched.

- **Linux**: the server program (`stellai_hub.py`) downloads the new version, tests it, then restarts itself. If a new version fails to start, it goes back to the previous one automatically and skips that version. To turn updates off: `touch ~/llm/no-auto-update`. Changes to the setup script itself (new engine builds, launchers) still need a re-run of `stellai-setup.sh`.
- **Windows**: every build on the main branch is published as a release (`windows-v0.1.N`). The app downloads it, checks its checksum, and reinstalls itself silently, without an admin prompt. See *Options* in the app to check now or turn automatic installs off.

## Set up

1. On the laptop or desktop (whichever is your server), start **StellAI Server**. It shows the **server address** and **API key** and a QR code. In the app, tap **Settings → Scan to connect**, scan the code and tap **Connect** (the phone's camera app works too).
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

## What setup installs (detected automatically)

Both installers look at the computer first and install only what fits it:

- **Linux**: works on Mint/Ubuntu/Debian (apt), Fedora (dnf), Arch (pacman) and openSUSE (zypper), with package names translated for each. NVIDIA cards get a CUDA build. Setup reads the driver's CUDA version and the card's compute capability, reuses an installed toolkit if it fits, otherwise installs the matching `cuda-toolkit-X-Y` from NVIDIA (Ubuntu/Mint/Debian) or the distribution's own, and compiles exactly for that card. AMD cards with their own memory get a Vulkan build (Mesa). Everything else gets a CPU build tuned to the processor. Node.js older than 18.19 is replaced by a private Node.js 22 LTS for diagrams, and zram uses zramswap or zram-generator as the distribution prefers.
- **Windows**: picks the llama.cpp build your NVIDIA driver can run (newest CUDA version up to what the driver supports), Vulkan for AMD/Intel cards, or the CPU build; the matching whisper.cpp build; and installs the Microsoft Visual C++ runtime if it's missing. The setup page lists all of this under **Will install**, with a tip when a driver update would make it faster.

## Desktop chat

Chat with Stella from any computer's browser at **http://SERVER:8092** (on the server itself: *StellAI Web Chat* in the Linux menu, or *Open chat in browser* in the Windows app). It's the same chats as your phones, synced live, with modes, projects (including adding files by drag and drop), the library, reminders and lists, photos (paste or 📷), diagrams and the context meter. Enter the API key once per browser. Phone actions (alarms, apps, texts) only work on the phone.

## Server screen: status, models, health check, backups

Drawer → **🖥 Server** on the phone, or **🖥 Server** at the bottom of the desktop chat's sidebar.

- **Status**: whether the model is loaded, which model, context size, GPU/CPU, RAM and GPU memory, uptime.
- **Switch model**: the models that fit this machine are listed with their size. Tap **Download** to fetch one (progress shows; it keeps going if you close the app), then **Switch**. Stella is away for about a minute while the new model loads; chats and settings stay the same. On Linux this rewrites `~/llm/model.conf` and restarts only the model server.
- **Health check** (*Check everything*): AI model, voice, meaning search, web search, chat database, disk space, backups, PDF reading, diagrams, GitHub, Tailscale and updates, each with a fix when something is wrong. On the server itself: `~/llm/stellai-doctor.sh` (Linux, also in the app menu as *StellAI Health Check*) or *Check everything* in the Windows app.
- **Backups**: every night (between 2 and 5 am when Stella is idle) the server zips your chats, memories, projects, photos, diagrams and wake words into `~/llm/backups` (Windows: `%LOCALAPPDATA%\StellAI\backups`) and keeps the last 14. The GitHub token is left out. *Back up now* makes one immediately, and you can set a second folder (a USB drive, a NAS mount) that every backup is copied to. To restore: `~/llm/stellai-restore.sh` on Linux, or *Restore a backup…* in the Windows app; the old database is kept beside the restored one.

## Avatar screen

Stella gets a face and a voice: a full-screen page with an animated **space orb** or your own **3D character**, which listens, thinks, talks with a natural voice and moves her mouth while speaking. Replies to the phone and the desktop chat are spoken there too (can be turned off).

- **On the server PC**: menu → **StellAI Avatar** (Linux) or **Avatar screen** in the Windows app.
- **Spare phone or tablet**: install the StellAI app, connect it, then drawer → **✦ Avatar screen**. The screen stays on; talk with the big mic button or the app's wake word.
- **Raspberry Pi / CM5 (dedicated screen)**: see [docs/cm5-display.md](docs/cm5-display.md). One script sets it up to start full screen at login.
- **Any browser**: `http://SERVER:8092/avatar` (enter the API key once). The microphone needs a secure page: the server PC itself, Tailscale HTTPS (`sudo tailscale serve --bg 8092`), or the Chrome flag *unsafely-treat-insecure-origin-as-secure*.

**Talking**: say the **wake word** (Hey Jarvis, Alexa, Hey Mycroft, or your own custom one from the server's wakewords folder), press **Space**, or tap 🎤. Wake word detection runs on the screen device itself (the same openWakeWord models as the phone); no audio leaves it until the wake word is heard. While she's talking, a clearer "wake word" is needed (so she doesn't wake herself), and saying it interrupts her.

**The 3D character** is a female `.vrm` character: make one free in [VRoid Studio](https://vroid.com/en/studio) or download one whose license allows personal use from [VRoid Hub](https://hub.vroid.com/en), then ⚙ → **Upload character**. She's scaled to the height you choose (default **5′8″**, adjustable 4′7″–6′7″). She blinks, breathes, sways, glances around, looks up while thinking, leans in while listening, lip-syncs to her voice, and smiles or looks sorry depending on what she says.

**Animations**: built-in gestures (a wave when the screen opens, looking around, head tilt, weight shift, shoulder roll, a stretch after 10 quiet minutes, a happy bounce (eyes closed in a big smile, hands behind her back, bouncing on the beat with her head rocking side to side)) play every 20–60 seconds while idle, blending smoothly in and out and stopping the moment you talk. Add your own: ⚙ → **Add animation** with **.vrma** files (VRoid Hub, BOOTH) or **Mixamo .fbx** files (download "Without Skin"), and choose when each plays: while idle, when the screen opens, after a long quiet, when she's happy, or for love and goodnight messages. She **blows you a kiss** (hand to her lips, then sweeping it out to you with a wink) after replies like "love you" or "goodnight", or from ⚙ → Try. Ask her to **dance** and she does a hip-sway dance: hands behind her head, hips swaying side to side with a little bounce and her hair swinging (it also turns up now and then while she's idle). Uploaded animations are preferred over the built-in ones.

**Shared folder ("bin")**: every character and animation lives in one folder on the server: `~/StellAI Avatar Files` on Linux (a shortcut to `~/llm/avatar/bin`), or **Avatar files** in the Windows app. Put `.vrm` characters in `characters` and `.vrma`/`.fbx` animations in `animations` (name a file `wave@greeting.fbx`, `stretch@quiet.fbx` or `hop@happy.fbx` to choose when it plays; otherwise it plays while idle). Every avatar screen checks the folder about once a second: new and changed files load without restarting, deleted ones disappear, and each screen's stored copy is replaced rather than piling up. Uploading through ⚙ on any screen puts the file in the same folder. With several characters, pick the active one in ⚙. Settings changed on one screen apply to all of them right away.

**Light mode** (automatic on a Raspberry Pi or when a device draws slowly; or on/off in ⚙): fewer pixels and 30 frames a second, so a CM5/Pi 5 stays smooth and phones use less battery. The avatar is drawn on the screen device; the server only sends the files and the voice.

**The voice** is [Piper](https://github.com/rhasspy/piper), a fast neural voice that runs on the server's CPU, private and offline. Six female voices (⚙ → Voice, with a ▶ preview) and adjustable speed. The first time the avatar screen opens, the server downloads the voice engine, a voice, the 3D and wake word files (~110 MB together); the health check shows the progress. Backups include your character, animations and avatar settings.

## Talking with Stella

Voice conversations are tuned to feel like talking, not dictating:

- **Spoken style**: when you talk to her, she answers like a person speaking: one to three short sentences, no lists or markdown, and no slow "thinking" unless the question is genuinely hard. Ask for detail and she'll go deeper.
- **Knows when you've finished**: a speech detector (Silero VAD on the avatar screen; loudness on the phone) waits longer when you trail off or say "um…", and answers quickly after a clear sentence.
- **Transcribes while you talk**: on the avatar screen, finished phrases are transcribed while you keep talking, so the text is ready almost as soon as you stop.
- **Starts speaking sooner**: she starts with the first natural phrase of her reply instead of waiting for a full sentence (phone and avatar screen), and the server keeps her voice loaded.
- **"One sec, let me check"**: if she needs a tool (web search, reminders, files), she says so instead of going quiet.
- **Follow-ups without the wake word**: after she answers, she keeps listening for about 5 seconds so you can just reply. "Thanks" or "that's all" ends it (she'll say "Anytime!"). Turn off in Settings (phone) or ⚙ → Conversation (avatar).
- **Interrupt her by talking** (avatar screen, optional): just start speaking while she's talking. Best with a speakerphone that cancels echo, so she doesn't hear herself; off by default.
- **Says things the way people do**: "5V" is "five volts", "3:30" is "three thirty", "$3k" is "three thousand dollars", "e.g." is "for example"; emoji, symbols and links are left out.
- **Mouth shapes from the sound**: the 3D character's lips follow the actual sounds of her voice (open, round or wide), not just the volume.
- **Kokoro voice** (optional, Linux and Windows servers): ⚙ → Voice → Kokoro for a noticeably more natural voice (7 female voices). The server sets it up once in the background (Linux: ~175 MB into its own Python; Windows: ~200 MB including a small private copy of Python, since the Windows app has none of its own) and Piper keeps speaking until it's ready; if Kokoro ever fails, Piper takes over. Kokoro runs as its own process, so it can't slow down or crash the rest of the server.

## Search past chats

Type in the search box at the top of the drawer (phone) or sidebar (desktop). Titles match instantly; the server also searches what was said in every chat, by keywords and by meaning, and shows the matching passage. Stella can search too: ask "what did we decide about the power supply last week?" and she looks through earlier chats (the `search_chats` tool). New messages are indexed within a minute.

## Reminders and lists

- "Remind me to reflow the board at 5" or "…every weekday at 9" → a reminder that **rings on all your phones** at that time, even if the app is closed (Done / Snooze 10 min on the notification; dismissing on one phone clears it on the others within about 15 minutes).
- "Add flux and solder wick to my parts list", "what's on my shopping list?", "tick off flux".
- See and edit them in ☰ → **⏰ Reminders & lists** (or in the desktop chat).

Wake-up alarms still go to the phone's clock app ("set an alarm for 6:45").

## Library and file search

☰ → **📚 Library** holds documents Stella can search from **any** chat (datasheets, manuals, notes). Project files work the same way inside their project. Search combines keywords with *meaning* (a small 140 MB search model on the server), so "how fast can it charge the battery?" finds the TP4056 page about charge current even though the words differ.

## Web search

Stella can look things up online (docs, part availability, error messages) and read pages and PDF datasheets, citing the links. On Linux the setup offers **SearXNG**, a private search engine running on your server in Docker; otherwise (and on Windows without Docker Desktop) she uses DuckDuckGo. For safety she can only open public internet addresses, never devices on your home network.

## GitHub

Settings → **GitHub**: add a fine-grained token (Contents read & write, Pull requests read & write, Actions read, Issues read) and a default repository. Then ask things like "why did my last build fail?", "explain ChatScreen.kt" or "fix it". Stella can read code, search it, read commits, issues and **build logs**, and **commit fixes to a new branch and open a pull request**. Every commit and pull request shows you the exact changes first and only happens if you tap **Approve**; she never commits to your main branch. The token is stored on your server, not on the phone.

## Modes

Each chat has a **mode**, a mindset that changes how Stella thinks and writes. Tap the chip under the top bar (e.g. **✦ GENERAL ▾**) to switch, before your first message or any time during a chat (it applies from her next reply).

| Mode | For | Feel |
|---|---|---|
| ✦ General | Everyday help and phone actions | Short and practical |
| ⌨ Programmer | Writing and debugging code | Precise, thinks first, full code blocks |
| ✒ Writer | Novels, stories, worldbuilding | Creative, long prose, keeps continuity |
| ⚡ Electronics | Circuits, PCBs, firmware | Uses the calculators and diagrams |
| 🎓 Tutor | Learning something | Step by step, hints before answers |
| 💡 Brainstorm | Ideas | Lots of varied ideas |

Make your own in the mode picker → **Manage modes** (or copy a built-in one and change it): instructions, creativity, whether she thinks first, fewer tools, and *story memory*, which makes context refreshes keep characters, plot and style instead of tasks. Your modes and each chat's mode sync to all your devices.

Tip for a novel: create a project for it (characters and world in the notes, chapters as files) and chat in **Writer** mode inside it.

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
