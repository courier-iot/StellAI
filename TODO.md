# StellAI to-do

Ordered from most to least useful for everyday use and technical/creative work (programming, electronics, diagrams).
Effort is a rough guide: **S** about an hour, **M** an afternoon, **L** a day or more.

1. [ ] **Quick launch** (app, S)
   A Quick Settings tile and a home-screen widget to talk to Stella in one tap.

### Next: long chats that don't run out, and working more like Claude (agreed, not started)

2. [ ] **Bigger context windows** (setup, S): 32K for 4B, 32–64K for 9B, 64K for 35B, sized from free RAM/VRAM (Qwen3.5's hybrid attention keeps the cost low).
3. [ ] **Background rolling summaries** (server + clients, M): summarize the oldest turns while idle from ~60% full, fold old summaries together; no waiting.
4. [ ] **Recall inside the current chat** (server, M): search the summarized-away part of the same chat and bring exact passages back.
5. [ ] **Pinned working notes per chat** (all, M): goals, decisions, names, story facts; always included, never summarized.
6. [ ] **Documents beside the chat** (all, L): long code and chapters live in a side panel Stella edits by section; the chat keeps a reference.
7. [ ] **Automatic memory** (server, M): pull lasting facts from quiet chats into Memory, with a review list.
8. [ ] **Code runner** (server, M): run Python on the server to check maths, test code and analyze files.
9. [ ] **Personality and working style** (prompt, S): direct, asks one clarifying question, admits uncertainty, checks its work.
10. [ ] **Conveniences** (all, S): auto-continue at the length limit; "Continue in a new chat" carrying notes and summary.

## Also discussed, not yet built

- [ ] **"Hey Stella" wake word**: the app support is done; train the model in the Colab notebook (see the app README) and drop `hey_stella.onnx` into `~/llm/wakewords/` on the server.
- [ ] **Windows installer, full release**: code signing (no SmartScreen warning), a fixed APK signing key, and testing on real Windows PCs.
- [ ] **Security hardening**: firewall rules limited to Tailscale and your home network, Android backup turned off for the app, and a command to replace the API key.

## Done

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
