# StellAI to-do

Ordered from most to least useful for everyday use and technical/creative work (programming, electronics, diagrams).
Effort is a rough guide: **S** about an hour, **M** an afternoon, **L** a day or more.

1. [ ] **Quick launch** (app, S)
   A Quick Settings tile and a home-screen widget to talk to Stella in one tap.

### Cleanup (agreed)

- [x] **Tests in the project** (0.27.0): `tests/run_tests.py`, run by GitHub on every upload.
- [x] **No copies inside other files** (0.27.0): the setup installs the real hub and catalog; the hub is built from its sections by `server/build_hub.py`.
- [x] **The server in sections** (0.27.0): `server/hub/*.py`, and one route table for its web addresses (`docs/api.md`).
- [x] **One copy of Stella's instructions and modes** (0.27.0): on the server (`/prompts`), cached by the apps.
- [ ] **The reply loop on the server** (all, L): the phone and the desktop chat each run tools, Auto mode and auto-continue themselves; the server could do it once for every app.
- [ ] **Small tidy-ups** (S): one settings folder for the server's state files; split the phone's chat screen into smaller pieces.

### Long chats that don't run out, and working more like Claude (done in 0.24.0 to 0.26.0)

2. [x] **Bigger context windows** (0.14.1): 32K for 4B, 16–64K for 9B, 64K for 35B on a graphics card, sized so the model stays on the card; 8–16K without one.
3. [x] **Background rolling summaries** (server + clients, M): summarize the oldest turns while idle from ~60% full, fold old summaries together; no waiting.
4. [x] **Recall inside the current chat** (server, M): search the summarized-away part of the same chat and bring exact passages back.
5. [x] **Pinned working notes per chat** (all, M): goals, decisions, names, story facts; always included, never summarized.
6. [x] **Documents beside the chat** (0.26.0): long code and chapters live in a side panel Stella edits by section or passage, with versions; the chat keeps a reference.
7. [x] **Automatic memory** (server, M): pull lasting facts from quiet chats into Memory, with a review list.
8. [x] **Code runner** (server, M): run Python on the server to check maths, test code and analyze files.
9. [x] **Personality and working style** (prompt, S): direct, asks one clarifying question, admits uncertainty, checks its work.
10. [x] **Conveniences** (all, S): auto-continue at the length limit; "Continue in a new chat" carrying notes and summary.

## Also discussed, not yet built

- [ ] **Pairing computers** (explained, not started): (1) helper machine for voice, search and embeddings; (2) two brains: a fast small model on one machine, the big one on another; (3) one model split across machines with llama.cpp RPC (wired network only). Two graphics cards in one PC beats all three.
- [ ] **Resource savings left out**: smaller (Q8) vision file (couldn't check that the download exists), SearXNG on demand, Windows Defender exclusion and power plan (manual for now).

- [ ] **"Hey Stella" wake word**: the app support is done; train the model in the Colab notebook (see the app README) and drop `hey_stella.onnx` into `~/llm/wakewords/` on the server.
- [ ] **Windows installer, full release**: code signing (no SmartScreen warning), a fixed APK signing key, and testing on real Windows PCs.
- [ ] **Check on real hardware**: the 0.12.5 background service (does the connection survive switching apps on the owner's phone?), the subtitle window and the in-app avatar on the phone, Kokoro on the Windows backup.

## Done

- [x] **Memory review, code runner, working style, conveniences** (app 0.25.0): server learns from quiet chats into a keep/drop list (or saves, or off); `run_python` with plots, time/memory limits and no network; direct, one-question, honest working style; long replies continue by themselves; Continue in a new chat.
- [x] **Long chats** (app 0.24.0): background rolling summaries on the server (from ~60% full, newest ~35% kept, old summaries folded in), `recall_chat` for the exact earlier words of the same chat, 📌 pinned notes per chat (Stella pins decisions herself; never summarized).
- [x] **Move Stella + server/client/both** (app 0.23.0): "move yourself to the server" by voice (Tailscale names or addresses, confirmation, same-account check), the package restored on the new server with the API key, the old server as a signpost that redirects phones, browsers and avatar screens, then a client; avatar announcements. Setup's first question: server only, client only (finds servers on Tailscale) or both, on Linux and Windows.
- [x] **Proxmox + install guides** (app 0.22.0): `stellai-proxmox.sh` (host NVIDIA driver with DKMS, Ubuntu LXC with the cards, /dev/net/tun, nesting for Docker, then the normal setup); container-aware setup (matching driver programs, pinned; no zram or sysctl); start-at-boot service; health check catches driver version mismatches; step-by-step guides for Linux and Proxmox. 
- [x] **Setup screens + model catalog + helper** (app 0.21.0): Linux menu screens and Windows setup window (cards, memory budget, ranked models, conversation length, helper, extras, summary); shared catalog with exact Hugging Face files (split models, MTP versions); helper model for background jobs; test run that sizes the context and keeps MTP only if faster; RAM prompt cache. Next: per-mode models (a coder model for Programmer/CAD/PCB), a speed test the phone app can start.
- [x] **Auto mode + circuits as code** (app 0.20.0): General chats switch modes by themselves (sticky, learns from corrections, chips to redo or pin, topic lanes folded with facts kept, "back to …"); PCB Designer writes circuits in SKiDL, places them wide, autoroutes with Freerouting, and keeps recipes, confirmed pinouts and notes. Next: offer to move a big topic into its own chat; smarter placement (group parts by connection); live KiCad add-on.
- [x] **CAD Designer + PCB Designer** (app 0.19.0): FreeCAD and KiCad modes with versioned, measured, pictured steps; rules checks explained; fab exports; STEP hand-offs between PCB, CAD and Blender. Next: live FreeCAD/KiCad window add-ons.
- [x] **More phone powers** (app 0.17.0): turn off alarms and timers, next alarm, music control / now playing / play by name, volumes per stream, silent and Do Not Disturb, brightness, notifications, contacts, calendar reading, settings panels; Settings → Phone access. 0.17.1: "play ..." goes to Stellar Disc by default when nothing else is playing. 0.18.0: "download ..." sends songs to Stellar Disc's Uplink. Next: Home Assistant controls.
- [x] **Picture to model** (app 0.16.0): reference pictures per 3D workspace (every visual check compares with them; chat photos now carry their id so tools can use them); photo-to-3D shapes with Hunyuan3D-2 mini / multi-view on the NVIDIA card, pausing Stella's model on one card or using a second card; `LLAMA_GPUS` keeps Stella on one card. Next: Windows install for the generator, optional texturing (Hunyuan3D-Paint).
- [x] **3D Modeler mode** (app 0.15.0): Blender kept running in the background on the server; each step is a checked bpy script saved as a new version, with measured changes, four-angle renders and a vision check; revert, history, export (glb/fbx/obj/stl/vrm/blend); Linux setup offers Blender and the VRM add-on. Next: the live add-on for an open Blender window, reusing the same commands.

- [x] **Speed test, instant timers and alarms, security** (app 0.14.0): measured reading/writing/waking/hearing/speaking with a comparison to the last run; phone timers and alarms without the model; firewall limited to the home network and Tailscale; replace-the-API-key command; wrong keys logged; Android backup off.

- [x] **Lighter on resources** (app 0.13.1, merged with the neon look and 0.12.6): instant answers without the model (time, date, sums, lists, clear reminders); the model sleeps after 30 idle minutes; vision on the CPU for graphics cards under 8 GB; lighter 3D character for phones and light-mode screens; silence skips Whisper; repeated short phrases are cached as audio.

- [x] **Avatar screens on phones** (0.12.4–0.12.5): each screen speaks only its own conversations, with a per-screen choice (this screen / + phone app / every device); subtitles in a small window below or beside her (or film-style, or off); no flicker on touch; the in-app avatar starts at once instead of waiting for the chat history; the phone app stays connected in the background (quiet notification, battery-limits button).
- [x] **Line icons instead of emoji** in the Android app.
- [x] **Kokoro voice on Windows**: installs its own embedded Python; falls back to Piper if anything fails.
- [x] **Natural voice conversations**: spoken reply style, smarter end-of-speech, transcribing while you talk, speaking sooner, "let me check" acknowledgements, follow-ups without the wake word, interrupt by talking, speech-friendly text, sound-based mouth shapes, optional Kokoro voice.

- [ ] **Voice, later (needs more GPU)**: full-duplex speech models (listening and talking at once, natural "mm-hm"s), e.g. Moshi, once the Tesla T4 is in.
- [ ] **Your animation ideas**: hand-made versions of the list you're putting together (send it over).
- [x] **Avatar animations, wake word, light mode, height**: built-in gestures plus uploaded .vrma/Mixamo .fbx with when-to-play; wake word in the browser (openWakeWord via ONNX Runtime Web); automatic light mode on a Pi; height (default 5′8″); CM5/Raspberry Pi display setup script.
- [x] **Avatar screen**: full-screen orb or 3D VRM character with Piper voice and lip sync; on the server PC, a spare phone/tablet (app), any browser or a Raspberry Pi kiosk; speaks replies from every device.

- [x] **Package autodetect**: installers pick packages for the distribution, GPU, driver and runtimes (Linux: apt/dnf/pacman/zypper, matched CUDA toolkit, Vulkan for AMD, private Node.js; Windows: driver-matched CUDA build, Visual C++ runtime, Vulkan check).

- [x] **Server screen**: status, model switching and downloads from the phone or desktop chat.
- [x] **Health check**: `stellai-doctor` checks every part of the server and says how to fix what's wrong (app, desktop chat, Linux menu, Windows app).
- [x] **Backups**: nightly zips of everything (keeps 14), optional copy to a second folder, one-step restore.
- [x] **Search past chats**: keyword + meaning search across all chats from the drawer/sidebar, and Stella can search them herself.

- [x] **Hardware tuning**: client mode for slow machines; best 35B quality for RAM + GPU; automatic GPU fitting (--fit) with room for vision and voice; thread split; GPU Whisper with single-pass decoding; Auto thinking (hard questions only, capped); Qwen's recommended sampling.

- [x] **Desktop web chat**: the server serves a browser chat at port 8092 with the same synced chats, modes, projects (drag-and-drop files), library, reminders, photos, diagrams and context refresh.
- [x] **Smarter file search + library**: meaning-based search (nomic-embed, ~140 MB) combined with keywords for project files, plus a shared document library searchable from any chat.
- [x] **Reminders and lists**: reminders ring on all phones (exact alarms, Done/Snooze, repeat daily/weekdays/weekly/monthly); named lists; screens on phone and desktop.
- [x] **Web search**: SearXNG (Docker) or DuckDuckGo, plus reading pages and PDFs; private network addresses blocked.
- [x] **GitHub access**: read code, search, commits, issues and build logs; commit to a new branch and open pull requests after on-screen approval.
- [x] **Smaller, faster prompts**: compact tool descriptions; only the tools (phone and server) a message needs, kept stable per chat; only relevant memories; project excerpts and the time ride on the newest message so the instructions never change (the model server reuses its cache); tool results capped to the space left; 32K context for the 35B model on a 5 GB+ GPU. A "hello" now costs ~1K tokens of overhead instead of ~3K.

- [x] **Modes (mindsets)**: per-chat modes (General, Programmer, Writer, Electronics, Tutor, Brainstorm) plus your own; each sets instructions, creativity, thinking, tools and how refreshes summarize (story memory for fiction). Synced. Replaces the planned "Coding mode" (now the Programmer mode).

- [x] **Automatic updates**: Linux and Windows servers check the repo every 30 minutes and update themselves when Stella is idle, keeping all data. Linux rolls back a version that fails to start; Windows verifies a checksum and reinstalls silently.

- [x] **Projects**: group chats under a project with its own instructions, pinned notes and files (PDF, Word, code, text, images). Small files go to Stella whole; big ones are searched for the parts that matter. Stella can save project notes and search project files herself. Synced through the server.
- [x] **Context refresh**: a context meter under the top bar; when a chat nears Stella's limit she summarizes the older part and continues (automatic, or ⋮ → Refresh context). Exact token counts from the server.

- [x] **Windows installer (preview)**: `StellAI-Setup.exe` built by GitHub Actions; detects CPU/RAM/GPU, picks CUDA, Vulkan or CPU and a model, downloads everything on first run; tray app with start/stop, start with Windows, firewall rule and a QR code to pair phones.
- [x] **QR pairing**: scan the server's QR code with the phone camera and confirm to connect.

- [x] **Long-term memory**: remember/forget tools, automatic learning from quiet chats, a Memory screen to view, add and delete facts; shared by all devices.
- [x] **Photo input**: take or choose photos; Stella sees them through Qwen3.5's vision add-on; photos sync to other devices.
- [x] **Message actions**: long-press to copy, share, retry, or edit and resend (synced).
- [x] **Custom wake word support**: trained `.onnx` models on the server appear in the app's wake word list.
- [x] **Formatted replies with code blocks**: Markdown replies (headings, lists, bold, links), syntax-highlighted code with a Copy button.
- [x] **Engineering calculator tools**: exact maths on the server (SI values like 4.7k, 10uF), Ohm's law, voltage dividers with standard-value suggestions, LED resistors, RC filters, series/parallel, E12/E24/E96 lookup.
- [x] **Diagrams in chat**: schematics (schemdraw), block diagrams and flowcharts (Mermaid), timing diagrams (WaveDrom) rendered on the server, shown in chat, tap to zoom.
- [x] Local model server with GPU support (desktop) and CPU build (laptop)
- [x] Phone app: chat, phone actions, voice commands, spoken replies
- [x] Wake word and default-assistant support
- [x] Chat sync across devices over Tailscale
- [x] Renamed to StellAI; assistant named Stella (renameable)
- [x] Space theme with 8 accent colors
