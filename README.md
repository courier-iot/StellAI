# StellAI

Meet **Stella**, your personal voice assistant and chat app, powered by the AI model on your own computer (llama.cpp + Whisper). Nothing goes to the cloud. You can rename her in Settings → Assistant.

- **Wake word**: say "Hey Jarvis" (or "Alexa" / "Hey Mycroft") and give a command, hands-free.
- **Default assistant**: long-press the power or home button to talk, instead of Google.
- **Phone actions**: set and turn off alarms and timers, play/pause/skip music and play songs by name, what's playing, volumes, silent and Do Not Disturb, brightness, reading notifications, contacts and calendar, quick settings panels, opening apps, maps, flashlight, battery, plus drafting texts, dialing and calendar events (which you confirm). See [Phone access](#phone-access).
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

- **Linux** (Linux Mint, Ubuntu): run `server/stellai-setup.sh`. It detects your hardware, builds llama.cpp for your GPU and creates the **StellAI Server** launcher. Step by step: [docs/install-linux.md](docs/install-linux.md).
- **Proxmox VE** (a server running VMs): run `server/stellai-proxmox.sh` on the Proxmox host. It installs the NVIDIA driver on the host, creates an LXC container for Stella with the graphics cards shared into it (no PCIe passthrough, so HP Gen8 servers work), and runs the normal setup inside it, as a service that starts with the host. Step by step: [docs/install-proxmox.md](docs/install-proxmox.md).
- **Windows 10/11 (preview)**: install `StellAI-Setup.exe` from the "Build Windows installer" Actions run. See [windows/README.md](windows/README.md).

### Setup screens

The first question is **what the computer should be**: *server and client* (Stella runs here and you chat here too), *server only* (no desktop shortcuts; starts at boot as a service on Linux, with Windows in the tray), or *client only* (the desktop chat and avatar screen pointed at another server, with nothing big installed; it lists the StellAI servers it finds on your Tailscale devices). Then both setups walk through the same steps, with nothing downloaded until you confirm the summary. Linux shows menu screens in the terminal (arrow keys, space bar); `PLAIN_UI=1` gives plain questions. Windows shows them in the setup window.

1. **This computer**: processor (and whether it has AVX2), memory, every graphics card, free disk space.
2. **Graphics cards**: with two or more, tick the ones Stella's model may use. Cards left out stay free for the helper and 3D jobs.
3. **Memory**: how much RAM StellAI may use (nearly all on a dedicated server, less on a PC you also use).
4. **The model**, ranked for those cards and that memory: ★ best overall, ⚡ fastest, ⌨ best coder and ◆ smartest (if it's slower), each with its size and an estimated speed. *Show every model* lists every model and size that fits; you can also paste your own Hugging Face `.gguf` link. The choices come from a catalog (`server/stellai_tune.py`): Qwen3.5 0.8B to 122B-A10B and Qwen3.6 27B / 35B-A3B. The exact files are looked up on Hugging Face at install time, so big models split into parts work too. It updates along with the server.
5. **Conversation length**, with the recommended size for that model.
6. **Helper model** (see below): on the processor, on a spare card, or none.
7. **Extras**: photos, Tailscale, web search, the 3D Modeler, CAD and PCB designers, voice model size, the MTP speed-up test and the test run.
8. **Summary**, with download size and free disk space. Then it installs, and the **test run** measures the real speed.

Unattended runs can answer ahead: `MODEL_CHOICE=qwen3.6-27b ANSWER_blender=y ANSWER_test=n ./stellai-setup.sh` (the old `MODEL_CHOICE=1/2/3` still works).

### Helper model

A small second model (Qwen3.5 2B, or 0.8B on smaller machines) runs next to Stella for quick background jobs: learning memories from chats, and deciding which mode a message needs when Auto mode's keywords can't tell. Those jobs stop queueing behind the main model. It runs on the processor by default (about 2 GB of RAM), or on a graphics card Stella's model doesn't use. If it isn't running, the main model does those jobs as before. The health check shows it.

### Test run and the MTP speed-up

After installing, the test run loads the model, measures how fast it writes and reads, and checks the conversation length really fits (halving it until it loads). Models with **Multi-Token Prediction** (Qwen3.5/3.6 MTP versions) are then tried with llama.cpp's MTP speed-up. It's kept only if it's at least 10% faster on this computer: it helps a lot on some machines and is slower on others. Results go into the settings. To run it again: **StellAI Speed Test** in the Linux menu (close StellAI Server first), or **Test & tune** in the Windows app.

### Tuned automatically for your hardware

The setup (Linux script or Windows installer) checks the computer and sets things up for it:

- **Slow computer?** A 2-core machine without a usable graphics card (like the T470) is offered *client mode*: it just opens Stella on your stronger server, with the same chats and much faster answers, and installs nothing big.
- **Model quality:** each model's most accurate version that fits the memory and cards you gave StellAI. A smaller version is offered too when it fits entirely on the graphics card and runs much faster.
- **Prompt cache in RAM:** sized to the memory budget, so going back to another chat doesn't mean re-reading it from the start.
- **GPU use:** llama.cpp fits as much of the model onto the graphics card as it can (leaving room for the vision add-on and voice), instead of a fixed split.
- **Threads:** physical cores for writing replies, all threads for reading long prompts.
- **Voice:** Whisper runs on the graphics card when there is one, with quick single-pass decoding.
- **Thinking:** Settings → *Thinking before answering*: **Auto** (default) thinks only when a question looks hard (debugging, maths, design, long questions) and answers simple things instantly. Thinking is capped (1K tokens on small setups, 2K on bigger ones) so it can't fill Stella's memory.
- **Sampling:** Qwen's recommended settings for Qwen3.5 are used (precise for code; a mild repetition penalty elsewhere).

### Light on resources

- **Instant answers:** the time, the date, sums, "add milk to my shopping list", "what's on my parts list" and clear reminders ("remind me to call Sam in 20 minutes", "…at 5:30 pm") are answered by the server itself, without the AI model. Anything else, or anything ambiguous ("remind me at 5"), goes to the model as before. Off in Writer, Tutor, Brainstorm and custom modes.
- **The model sleeps when idle:** after 30 quiet minutes it leaves RAM and GPU memory, and the next message loads it again (a few seconds). Change it with `SLEEP_IDLE_MIN="60"` in `~/llm/model.conf` (Linux) or `"sleep_idle_min": 60` in the Windows config; `0` keeps it loaded.
- **Small graphics cards (under 8 GB):** the photo add-on runs on the CPU, so about 1 GB more of the card holds the model. Photos take a few seconds longer to read. To undo: `VISION_GPU="1"` (Linux) or `"vision_gpu": true` (Windows).
- **Lighter character on phones:** phones, tablets and light-mode screens load a copy of the 3D character with textures capped at 1024 px (made once on the server; the original is untouched). Smaller download, much less graphics memory.
- **Voice:** silent recordings skip speech recognition, short phrases Stella repeats ("Okay.", "Reminder set.") are kept as audio, and speech recognition gets the processor ahead of the model.
- New model-server options are only used when the installed llama.cpp build supports them.

### Longer conversations

The context window (how much of a chat Stella holds at once) is sized for the computer: as long as fits without pushing the model off the graphics card, and short where there is no card, because a CPU takes minutes to re-read a long chat. Examples: RTX 2060 6 GB: 4B 32K, 9B 16K, 35B 64K. Tesla T4 16 GB: 9B and 35B 64K. No graphics card: 8K, or 16K with 6+ cores. Windows works it out at every start; on Linux re-run setup or switch models. To set your own: `NET_CONTEXT="32768"` in `~/llm/model.conf`, or `"context": 32768` in the Windows config.

### Speed test

**Speed test** (phone app → Server, the desktop chat's server window, or the Windows app) measures this computer as it is set up: how fast Stella reads and writes, how long she takes to wake after sleeping, and how long hearing and speaking take. Each run is compared with the last one on the same model, so you can see whether a change helped. It takes up to a minute and Stella can't chat meanwhile.

### Security

- **Firewall:** Stella's ports (8080, 8092) are open to your home network and Tailscale only. Linux applies this each time the server starts; on Windows new installs get it, and older ones can switch with **Limit firewall** in the app's Options.
- **Replacing the API key** (lost phone, key seen by someone): Linux `~/llm/stellai-new-key.sh`, Windows **New API key…** in Options. Every device then pairs again with the new QR code.
- **Wrong keys are logged** in the server's activity, with the address they came from.
- **The phone app is left out of Google's backup** and phone-to-phone transfers, so the server address and key stay on the phone.
- Timers and alarms asked for on a phone ("set a timer for 10 minutes", "wake me up at 6:30 am") are instant answers too.

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

## Moving Stella to another computer

Say **"Stella, move yourself to the server"** (any Tailscale device name works, or an address). She checks that StellAI is running there, asks you to confirm, then moves everything: chats, memories, projects, reminders and lists, photos, diagrams, wake words, the avatar and its settings, modes and the API key. Models stay: the other computer uses its own. It takes a few minutes, and new messages wait meanwhile.

- The **avatar screen** says she's moving, and then that she's there.
- **Phones, browsers and avatar screens follow her by themselves**: the old computer stays up as a signpost and points them to the new address. Nothing needs pairing again, because the API key moves with her.
- The **old computer becomes a client**: its model, voice and helper stop, so it uses no resources beyond the small signpost. Move her back the same way any time.
- The receiving server only accepts a move from a device on your own Tailscale account.

To test it, use two computers with StellAI installed (any model on the second): move A → B, check, then move B → A. Each move starts from a fresh backup, so the original data is never touched until the new server has it.

## Desktop chat

Chat with Stella from any computer's browser at **http://SERVER:8092** (on the server itself: *StellAI Web Chat* in the Linux menu, or *Open chat in browser* in the Windows app). It's the same chats as your phones, synced live, with modes, projects (including adding files by drag and drop), the library, reminders and lists, photos (paste or the camera button), diagrams and the context meter. Enter the API key once per browser. Phone actions (alarms, apps, texts) only work on the phone.

## Server screen: status, models, health check, backups

Drawer → **Server** on the phone, or **Server** at the bottom of the desktop chat's sidebar.

- **Status**: whether the model is loaded, which model, context size, GPU/CPU, RAM and GPU memory, uptime.
- **Switch model**: the models that fit this machine are listed with their size. Tap **Download** to fetch one (progress shows; it keeps going if you close the app), then **Switch**. Stella is away for about a minute while the new model loads; chats and settings stay the same. On Linux this rewrites `~/llm/model.conf` and restarts only the model server.
- **Health check** (*Check everything*): AI model, voice, meaning search, web search, chat database, disk space, backups, PDF reading, diagrams, GitHub, Tailscale and updates, each with a fix when something is wrong. On the server itself: `~/llm/stellai-doctor.sh` (Linux, also in the app menu as *StellAI Health Check*) or *Check everything* in the Windows app.
- **Backups**: every night (between 2 and 5 am when Stella is idle) the server zips your chats, memories, projects, photos, diagrams and wake words into `~/llm/backups` (Windows: `%LOCALAPPDATA%\StellAI\backups`) and keeps the last 14. The GitHub token is left out. *Back up now* makes one immediately, and you can set a second folder (a USB drive, a NAS mount) that every backup is copied to. To restore: `~/llm/stellai-restore.sh` on Linux, or *Restore a backup…* in the Windows app; the old database is kept beside the restored one.

## Avatar screen

Stella gets a face and a voice: a full-screen page with an animated **space orb** or your own **3D character**, which listens, thinks, talks with a natural voice and moves her mouth while speaking. Each screen speaks only the conversations held on it, so a reply is never said twice; in gear → Voice you can let a screen also speak replies to the StellAI phone app, or to every device (handy for a Pi screen in the kitchen). Subtitles show in a small window below her (or beside her, over her film-style, or off — also per screen).

**Phone app in the background:** StellAI stays connected when you switch apps (Settings → "Stay connected in the background", on by default), with a quiet notification while it's on. If your phone still cuts it off, use the "Allow running without battery limits" button under that switch.

- **On the server PC**: menu → **StellAI Avatar** (Linux) or **Avatar screen** in the Windows app.
- **Spare phone or tablet**: install the StellAI app, connect it, then drawer → **✦ Avatar screen**. The screen stays on; talk with the big mic button or the app's wake word.
- **Raspberry Pi / CM5 (dedicated screen)**: see [docs/cm5-display.md](docs/cm5-display.md). One script sets it up to start full screen at login.
- **Any browser**: `http://SERVER:8092/avatar` (enter the API key once). The microphone needs a secure page: the server PC itself, Tailscale HTTPS (`sudo tailscale serve --bg 8092`), or the Chrome flag *unsafely-treat-insecure-origin-as-secure*.

**Talking**: say the **wake word** (Hey Jarvis, Alexa, Hey Mycroft, or your own custom one from the server's wakewords folder), press **Space**, or tap the microphone. Wake word detection runs on the screen device itself (the same openWakeWord models as the phone); no audio leaves it until the wake word is heard. While she's talking, a clearer "wake word" is needed (so she doesn't wake herself), and saying it interrupts her.

**The 3D character** is a female `.vrm` character: make one free in [VRoid Studio](https://vroid.com/en/studio) or download one whose license allows personal use from [VRoid Hub](https://hub.vroid.com/en), then the gear → **Upload character**. She's scaled to the height you choose (default **5′8″**, adjustable 4′7″–6′7″). She blinks, breathes, sways, glances around, looks up while thinking, leans in while listening, lip-syncs to her voice, and smiles or looks sorry depending on what she says.

**Animations**: built-in gestures (a wave when the screen opens, looking around, head tilt, weight shift, shoulder roll, a stretch after 10 quiet minutes, a happy bounce (eyes closed in a big smile, hands behind her back, bouncing on the beat with her head rocking side to side)) play every 20–60 seconds while idle, blending smoothly in and out and stopping the moment you talk. Add your own: the gear → **Add animation** with **.vrma** files (VRoid Hub, BOOTH) or **Mixamo .fbx** files (download "Without Skin"), and choose when each plays: while idle, when the screen opens, after a long quiet, when she's happy, or for love and goodnight messages. She **blows you a kiss** (hand to her lips, then sweeping it out to you with a wink) after replies like "love you" or "goodnight", or from the gear → Try. Ask her to **dance** and she does a hip-sway dance: hands behind her head, hips swaying side to side with a little bounce and her hair swinging (it also turns up now and then while she's idle). Uploaded animations are preferred over the built-in ones.

**Shared folder ("bin")**: every character and animation lives in one folder on the server: `~/StellAI Avatar Files` on Linux (a shortcut to `~/llm/avatar/bin`), or **Avatar files** in the Windows app. Put `.vrm` characters in `characters` and `.vrma`/`.fbx` animations in `animations` (name a file `wave@greeting.fbx`, `stretch@quiet.fbx` or `hop@happy.fbx` to choose when it plays; otherwise it plays while idle). Every avatar screen checks the folder about once a second: new and changed files load without restarting, deleted ones disappear, and each screen's stored copy is replaced rather than piling up. Uploading through the gear on any screen puts the file in the same folder. With several characters, pick the active one in the gear. Settings changed on one screen apply to all of them right away.

**Light mode** (automatic on a Raspberry Pi or when a device draws slowly; or on/off in the gear (settings)): fewer pixels and 30 frames a second, so a CM5/Pi 5 stays smooth and phones use less battery. The avatar is drawn on the screen device; the server only sends the files and the voice.

**The voice** is [Piper](https://github.com/rhasspy/piper), a fast neural voice that runs on the server's CPU, private and offline. Six female voices (gear → Voice, with a play preview) and adjustable speed. The first time the avatar screen opens, the server downloads the voice engine, a voice, the 3D and wake word files (~110 MB together); the health check shows the progress. Backups include your character, animations and avatar settings.

## Talking with Stella

Voice conversations are tuned to feel like talking, not dictating:

- **Spoken style**: when you talk to her, she answers like a person speaking: one to three short sentences, no lists or markdown, and no slow "thinking" unless the question is genuinely hard. Ask for detail and she'll go deeper.
- **Knows when you've finished**: a speech detector (Silero VAD on the avatar screen; loudness on the phone) waits longer when you trail off or say "um…", and answers quickly after a clear sentence.
- **Transcribes while you talk**: on the avatar screen, finished phrases are transcribed while you keep talking, so the text is ready almost as soon as you stop.
- **Starts speaking sooner**: she starts with the first natural phrase of her reply instead of waiting for a full sentence (phone and avatar screen), and the server keeps her voice loaded.
- **"One sec, let me check"**: if she needs a tool (web search, reminders, files), she says so instead of going quiet.
- **Follow-ups without the wake word**: after she answers, she keeps listening for about 5 seconds so you can just reply. "Thanks" or "that's all" ends it (she'll say "Anytime!"). Turn off in Settings (phone) or the gear → Conversation (avatar).
- **Interrupt her by talking** (avatar screen, optional): just start speaking while she's talking. Best with a speakerphone that cancels echo, so she doesn't hear herself; off by default.
- **Says things the way people do**: "5V" is "five volts", "3:30" is "three thirty", "$3k" is "three thousand dollars", "e.g." is "for example"; emoji, symbols and links are left out.
- **Mouth shapes from the sound**: the 3D character's lips follow the actual sounds of her voice (open, round or wide), not just the volume.
- **Kokoro voice** (optional, Linux and Windows servers): gear → Voice → Kokoro for a noticeably more natural voice (7 female voices). The server sets it up once in the background (Linux: ~175 MB into its own Python; Windows: ~200 MB including a small private copy of Python, since the Windows app has none of its own) and Piper keeps speaking until it's ready; if Kokoro ever fails, Piper takes over. Kokoro runs as its own process, so it can't slow down or crash the rest of the server.

## Search past chats

Type in the search box at the top of the drawer (phone) or sidebar (desktop). Titles match instantly; the server also searches what was said in every chat, by keywords and by meaning, and shows the matching passage. Stella can search too: ask "what did we decide about the power supply last week?" and she looks through earlier chats (the `search_chats` tool). New messages are indexed within a minute.

## Reminders and lists

- "Remind me to reflow the board at 5" or "…every weekday at 9" → a reminder that **rings on all your phones** at that time, even if the app is closed (Done / Snooze 10 min on the notification; dismissing on one phone clears it on the others within about 15 minutes).
- "Add flux and solder wick to my parts list", "what's on my shopping list?", "tick off flux".
- See and edit them in the menu → **Reminders & lists** (or in the desktop chat).

Wake-up alarms still go to the phone's clock app ("set an alarm for 6:45").

## Library and file search

the menu → **Library** holds documents Stella can search from **any** chat (datasheets, manuals, notes). Project files work the same way inside their project. Search combines keywords with *meaning* (a small 140 MB search model on the server), so "how fast can it charge the battery?" finds the TP4056 page about charge current even though the words differ.

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
| ◆ 3D Modeler | Blender models, props, VRM characters | Careful, one checked step at a time |
| 📐 CAD Designer | Precise parts in FreeCAD, 3D printing | Measured in millimetres, one checked step at a time |
| 🔌 PCB Designer | Circuit boards in KiCad | Designs circuits as code, autoroutes, explains the checks, fab files |

### Auto mode: one chat, many topics

In a **General** chat Stella picks the mode that fits each message by herself, so you can talk about your app, then a bracket, then a circuit board without switching. A chip like **⇄ Auto: CAD Designer (new topic)** shows each change; tap it to redo her last reply in another mode or to keep the chat in one mode.

- **Sticky**: she stays in the current mode for follow-ups and only switches when she's sure. In the 3D Modeler, CAD Designer and PCB Designer she won't leave a job half-done unless you clearly change topic ("different project", "now let's…").
- **Learns from you**: when you correct her with the chip, she remembers the phrase for next time. She can also switch herself mid-answer when she realises another mode fits.
- **Focused**: earlier topics in other modes are folded into a short note that keeps exact numbers and decisions, so a long mixed chat doesn't confuse her. Say "back to the app" to pick an earlier topic up again.
- **Settings → Switching modes by herself**: *Auto* (switch when sure), *Ask first* (only suggest), or *Off*. Picking a mode yourself always wins.

Make your own in the mode picker → **Manage modes** (or copy a built-in one and change it): instructions, creativity, whether she thinks first, fewer tools, and *story memory*, which makes context refreshes keep characters, plot and style instead of tasks. Your modes and each chat's mode sync to all your devices.

Tip for a novel: create a project for it (characters and world in the notes, chapters as files) and chat in **Writer** mode inside it.

## 3D Modeler (Blender on the server)

In **◆ 3D Modeler** mode Stella works on 3D models in a hidden Blender on your server. It's slow on purpose: every change is checked before she tells you about it.

1. **Get a model in**: drop a `.blend`, `.vrm`, `.glb`, `.fbx`, `.obj` or `.stl` file into the **3D inbox** folder (`~/llm/3d/inbox` on Linux, `3d\inbox` next to the chat database on Windows). Your avatar characters already on the server can be used as well. Or start from an empty scene.
2. **Ask for one change at a time**: "add a halo ring 6 cm above her head, tilted 8°, glowing lime".
3. For each change Stella writes a small Blender script. The server opens the newest version, runs it, saves a **new numbered version** (nothing is ever overwritten), measures what changed, renders front/side/back/¾ views, and has the vision add-on compare the pictures with the goal. If the script fails or changes nothing, nothing is saved.
4. **Undo**: "go back to version 7" (saved as a new version, so that can be undone too). The newest 40 versions and the first are kept.
5. **Export**: "export it as glb" (or fbx, obj, stl, vrm, blend). The file lands in the workspace's `exports` folder.

**From pictures** (anything: a prop, a gadget, furniture, a creature, a character, hair):

- **Reference pictures**: send photos in the chat and ask Stella to use them ("this is the front, this is the side"). She keeps them with the workspace, and every visual check then puts your photos next to her renders and says what still differs. Works for editing existing models to look like a photo.
- **Photo-to-3D**: "make this from the photos" runs a local photo-to-3D generator (Tencent's open Hunyuan3D-2 on your NVIDIA card) and adds a rough shape as a new version. Then she turns it to face front, scales it from a real measurement you give her (photos can't show size), cleans it up and adds materials, one checked step at a time. One photo works; a front photo plus side/back photos works better. Best for solid objects and props, rough for characters, poor for whole rooms.
- A shape takes several minutes. It needs about 7–9 GB of free graphics memory: with one card, StellAI pauses Stella's model during the job (Linux, after re-running setup) and loads her again afterwards; with a second card the job runs there and Stella keeps talking. With 2+ cards, `LLAMA_GPUS="0"` in `~/llm/model.conf` keeps Stella on card 0 (setup offers this). Install: Linux setup's "photo-to-3D" question (~11 GB, NVIDIA 8 GB+). The Hunyuan3D models are under Tencent's community license (not licensed for use in the EU, UK or South Korea).

Good at: hard-surface parts, props and accessories, materials and glow, cleanup, attaching parts to bones, exports. Not good at: sculpting faces and bodies; for those she talks you through it in Blender and checks your result.

Needs Blender on the server: Linux setup offers a portable copy (plus the VRM add-on); on Windows install Blender from blender.org. Any Blender already installed is found automatically, or pass `--blender /path/to/blender`. Quick renders use the graphics card; if Blender can't, it switches to slower CPU renders by itself. The pictures are checked only if the vision add-on is installed (the numbers are always measured).

Safety: Stella's scripts can only change the scene. Opening, saving and exporting files, other imports, shell and network access and Blender settings are blocked and handled by StellAI itself. This is a guard rail against mistakes, not a locked sandbox, so keep the server for your own use. Blender leaves memory after 15 quiet minutes.

## CAD Designer (FreeCAD) and PCB Designer (KiCad)

Two more modes that work like the 3D Modeler: every change is one small script, saved as a **new numbered version** only if it works and changes something, then measured, pictured and checked. "Go back to version 4" works the same way, and so do reference pictures.

**📐 CAD Designer** uses FreeCAD (headless) for exact parts: brackets, enclosures, mounts, adapters, fan ducts, replacement parts. Each step reports sizes in millimetres, volumes and problems (not a solid, separate pieces, broken geometry); pictures come from the server's Blender (without Blender you get the numbers only). Start empty or from a `.FCStd`, `.step`, `.stl` or `.iges` in the 3D inbox. Export `step` (other CAD), `stl` (3D printing, also importable into Tinkercad), `fcstd`, or `blender` (hands the part to the 3D Modeler).

**🔌 PCB Designer** uses KiCad. Drop a KiCad project into the 3D inbox (a `.zip` of the project folder, the folder itself, or a `.kicad_pcb`), or start an empty board. Stella runs KiCad's electrical rules check and design rules check and explains each problem, shows the board (KiCad 9 or newer draws it), and makes board changes as steps: moving and rotating parts, the board outline, tracks and text. Schematics aren't scripted; she talks you through those in KiCad and confirms with the checks. Exports: `fab` (Gerbers and drill files zipped for a board maker), `bom`, `pos` (pick and place), `step` (3D, also dropped into the 3D inbox so the CAD Designer can build an enclosure around it), `pdf` (schematic), `project`.

**Circuits as code.** For a new board Stella writes the circuit itself as a short Python script ([SKiDL](https://github.com/devbisme/skidl)): the parts, their KiCad footprints and what connects to what. StellAI turns it into a schematic and netlist, lays the parts out **wide on a grid** (connectors down the left, room between parts, thick 0.5 mm tracks, through-hole parts preferred), autoroutes it with [Freerouting](https://github.com/freerouting/freerouting), runs the checks and shows you the board, all as one numbered version. Change the circuit and run it again: parts you moved keep their places. Start with simple designs; she gets better over time:

- **Recipes**: sub-circuits that worked (an LED indicator, a 3.3 V regulator, a button with a pull-up) are saved and reused instead of re-invented. They start as drafts and are marked confirmed when you say they worked.
- **Confirmed pinouts**: parts with pins nobody has checked are listed after each build; she asks you to check them against the datasheet, and remembers the ones you confirm.
- **Notes**: what went wrong and the fix, so the same mistake isn't made twice.

Setup installs SKiDL, Java and Freerouting after KiCad (about 150 MB). Without the autorouter the board is still built and placed, with the connections shown as ratsnest lines to route in KiCad.

Install: Linux setup offers FreeCAD and KiCad (KiCad from its own PPA when it can). Windows: install them from freecad.org and kicad.org; StellAI finds them in Program Files. Board edits need KiCad's Python (`pcbnew`), which comes with KiCad. The same guard rails as the 3D Modeler apply: scripts can only change the design, not files, the network or the program's settings.

## Projects

Group related chats (say "Power supply PCB" or "StellAI app") and give them shared context. Open the menu → **PROJECTS +**.

- **Instructions**: how Stella should work in this project ("3.3 V logic, KiCad 9, 0805 parts").
- **Notes**: short facts she always knows here. She adds notes herself when you say things like "note that we picked the TPS5430".
- **Files**: datasheets (PDF), code, text, Word documents and images. Tap a file to see the text Stella reads from it. Small files are given to her whole; for big ones she gets the parts related to your question and can search the rest herself.
- **Chats**: start one from the project, or move any chat in with ⋮ → *Move to project…*. Project chats show a ◆ tag.

Projects are stored on the server, so every phone and tablet sees the same ones.

## Long chats

- **Summaries in the background:** while Stella is idle, the server folds the oldest part of any busy chat into its rolling summary once it passes about 60% of her context, keeping the newest third word for word. Earlier summaries are folded in, so there's always one. You no longer wait for "Refreshing context" in the middle of a reply (it's still there as a fallback).
- **Recall:** when the summary is too vague ("what values did we pick for the LED resistors?"), she looks up the exact earlier words of the same chat (`recall_chat`).
- **Pinned notes:** facts for one chat (goals, decisions, names, numbers, story facts) that are always included and never summarized away. She pins them herself when something is decided; add or remove your own from the chat menu (phone) or the pin button (desktop chat).

- **Continue in a new chat:** from the chat menu (phone) or the context meter (desktop chat). The server starts a fresh chat from this one's summary and pinned notes, in the same project and mode.
- **Long replies don't get cut off:** if a reply runs out of room mid-answer, Stella makes room (summarizes the older part) and carries on where she stopped, up to twice.

## Documents beside the chat

Long code, chapters and write-ups go in documents next to the chat instead of in it (the `document` tool). The chat only keeps a one-line reference ("Updated “Chapter 3” (v4)") with an **open** button, so a long program or a novel doesn't fill Stella's memory each time it changes.

- **Edited in pieces:** Stella changes an exact passage, rewrites one section (a Markdown heading, or a function or class in code), adds after a section or at the end. She reads the outline first, so she doesn't need the whole text in mind.
- **Versions:** every change is a numbered version (the last 60 are kept); go back to any of them, yours or hers.
- **You can edit too:** in the side panel (desktop chat, the document button) or **Documents** in the phone's chat menu. If Stella changed it while you were typing, the save is refused rather than overwriting her change.
- Copy or download a document (code downloads with its file extension). Each chat keeps its own documents; Stella knows their names and sections in every reply.
- Offered in the Programmer and Writer modes, in any chat that already has one, and whenever a message asks to write or edit something long.

## Code runner

Stella can run short Python scripts on the server (`run_python`) to check maths and unit conversions, test code, simulate circuits and filters, analyse CSV files and make plots (pictures show in the chat). numpy and matplotlib are there. Each run starts in an empty folder (plus any chat photos or project files it asks for), has a time limit (60 s by default), a 2 GB memory limit and, where Linux allows it, no network. The code is checked before it runs: no other programs, no network modules, no deleting or reaching outside its folder. That's a guard rail against mistakes, not a sandbox against someone hostile; it's your own server and your own assistant. Offered in the Programmer, Electronics, CAD, PCB and Tutor modes, and whenever a message mentions scripts, plots, data or simulation.

## Working style

Stella is direct, asks one short question when a request is ambiguous in a way that changes the answer (otherwise she states the assumption she made), says plainly when she isn't sure, checks numbers, units and code before answering (with the code runner or calculators when she can), and owns up to mistakes.

## Context meter and refresh

Stella can only keep a limited amount of a chat in mind at once (her *context*: about 8K tokens on the laptop, 16K with the 35B model). The bar under the top bar shows how full it is.

When it gets near full, she **refreshes** automatically: she summarizes the older part of the chat and continues from that summary, keeping the most recent messages word for word. A "Context refreshed" card marks the spot; tap it to read the summary. You can refresh any time with ⋮ → **Refresh context** (handy when switching topics). Every message stays visible and synced; only what Stella is sent gets shorter.

## Sync

Each device keeps its own copy of the chats and syncs with the server when it opens, after every reply, and every minute while in use. Open the **menu** to switch chats, start a new one, delete one (deleted everywhere), or sync now. Any phone or tablet with the same server address and API key sees the same chats.

## Memory

The server reads chats that have gone quiet (20 minutes) and picks out lasting facts, on the helper model when it runs. By default they're **suggestions**: Memory (phone) or Memory (desktop chat) lists them to **Keep** or **Drop**, including "replace X with Y" when a fact changed. Switch to *Save by itself* or *Off* there.

Stella keeps a list of lasting facts about you on the server, shared by all your devices: your projects, tools, preferences and routines. She saves facts when you say "remember…", and with **Learn from conversations** on (Settings → Memory) the server picks out new facts once a chat has been quiet for about a minute. Review, add or delete them under Settings → Memory → View and edit memories.

## Photos

Tap the camera next to the message box to take a photo or choose one. Stella can read circuits, error screens, sketches and datasheet tables. This needs the vision add-on on the server: re-run `stellai-setup.sh` and answer yes when it asks. Photos are resized to 1280 px and shared with your other devices.

## Message actions

Long-press any message to **Copy**, **Share**, **Retry** (Stella's last reply) or **Edit & resend** (your messages). Editing replaces that message and everything after it, on all your devices.

## Custom wake word ("Hey Stella")

Train a model (see below), then copy the `.onnx` file into `~/llm/wakewords/` on the server. It shows up with a ✦ in Settings → Voice assistant → Wake word, and the app downloads it the first time you use it.

Training: open the [openwakeword-colab-2026 notebook](https://colab.research.google.com/github/alfiedennen/openwakeword-colab-2026/blob/main/train_wakeword.ipynb) in Google Colab, set `TARGET_PHRASE = "hey stella"` and `MODEL_NAME = "hey_stella"`, choose a GPU runtime and **Run all**. It takes about 1.5 hours on Colab Pro, or about 2.5 hours on the free tier (keep the tab open), and downloads `hey_stella.onnx` at the end.

## Phone access

Some phone actions need an access that Android keeps off until you allow it: **Settings → Phone access** in the app shows each one with an **Allow** button.

| Access | Lets Stella |
|---|---|
| Notifications & music | read notifications ("did anyone message me?"), say what's playing, control and choose music in the app that's playing. Without it, play/pause/skip still works (like headset buttons) |
| Do Not Disturb | turn DND on/off, set the phone to silent |
| Brightness ("Modify system settings") | change the screen brightness |
| Contacts & calendar | find numbers by name ("text Mom"), read upcoming events |

What Android doesn't let any app do, so Stella can't either: **list or delete alarms** (she turns them off through the clock app, which may ask you to confirm; she can tell you when the next one rings), and **switch Wi-Fi, Bluetooth, mobile data or airplane mode** (she opens the panel and you tap the switch). Playing a song by name works with music apps that accept play requests (Spotify, YouTube Music and most others). With **Stellar Disc** 3.4.0 or newer installed, "play ..." goes there unless another app is already playing or you name one, and "download Midnight City by M83" sends it to Stellar Disc's Uplink (as music or video, at your Autopilot quality). Uplink opens filled in for you to tap Download, unless you switch on *Let assistants start downloads* in Stellar Disc's settings.

Android 13+ and "restricted setting": because StellAI is installed from a file, Android greys out Notification access at first. Open **App info → ⋮ → Allow restricted settings**, then allow it (the app links there).

## Adding your own phone actions

All tools live in `app/src/main/java/com/stellai/app/PhoneTools.kt`: add a definition in `definitions()` and a branch in `execute()`. Push, and GitHub builds a new APK.

## Limits worth knowing

- Stella needs the server on and reachable. If it isn't, the app says it can't reach the server.
- Phone-based wake word detection uses more battery than "Hey Google", which runs on a dedicated low-power chip that only Google's app can use.
- Small local models sometimes pick the wrong action. Check the ✓/✗ line after each one.
- While a call or another app is using the microphone, Android silences the wake word.
