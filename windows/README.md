# StellAI Server for Windows (preview)

One installer that sets up Stella's AI on a Windows 10/11 PC and lets your phones connect to it.

**Preview:** not code-signed yet, so Windows SmartScreen will warn you. Click **More info → Run anyway**.

## Get the installer

1. Push this repo to GitHub. The **Actions** tab runs **Build Windows installer** (about 10 minutes) whenever `windows/` or `server/` changes. You can also start it by hand: Actions → Build Windows installer → **Run workflow**.
2. Open the finished run and download **StellAI-Setup-windows** under Artifacts. Unzip it to get `StellAI-Setup-0.1.0.exe`.
3. The **selftest-screenshots** artifact shows the app's two screens as they looked on GitHub's Windows machine, plus the hardware it detected there.

After the first build, each one on the main branch is also published under **Releases** as `windows-vX.Y.Z`, so you can download the installer there without signing in.

## What the installer does

- Installs the small StellAI Server app (about 60 MB) for your user (`%LOCALAPPDATA%\Programs\StellAI`), with Start menu and desktop shortcuts. No admin rights needed, which is what lets updates install silently.
- Opens ports 8080 and 8092 in Windows Firewall for **private** networks only (home Wi-Fi and Tailscale), so phones can connect. This is the one step that needs admin, so Windows asks once on the first install. Set your Wi-Fi to *Private* in Windows settings if phones can't reach it.
- Optionally starts StellAI with Windows, in the tray.

## First run

On a slow PC (2 cores, no graphics card) the app suggests **Use another server…**: enter your StellAI server's address and API key, and this PC just opens Stella there, with nothing big to install. You can switch back any time.

The app checks your computer:

| Detected | Runs on |
|---|---|
| NVIDIA GPU with 4 GB+ and a recent driver | CUDA (fastest) |
| AMD or Intel Arc GPU with 4 GB+, or an NVIDIA GPU with an old driver | Vulkan |
| Anything else (e.g. laptop integrated graphics) | CPU |

Then the setup window walks through the same steps as on Linux: which graphics cards Stella may use (with two or more), how much memory StellAI may use, the model (ranked for this PC: best overall, fastest, best coder, smartest, or *Show every model that fits*, or your own Hugging Face `.gguf` link), the conversation length, the helper model, and extras. The summary line shows the total download and free disk space.

Then it downloads the official prebuilt llama.cpp and whisper.cpp for your hardware, the model (all its parts, for big models), the helper, the voice recognition model and (optionally) the vision add-on. After that the **test run** measures the model and keeps the MTP speed-up only if it's faster here; **Test & tune** on the dashboard runs it again. Downloads resume if interrupted. Everything goes to `%LOCALAPPDATA%\StellAI`: models, chats, memories, photos, diagrams, logs and the API key.

## Every day

- **Open chat in browser** opens the desktop chat (same chats as your phone).
- **Web search**: Stella uses DuckDuckGo. With Docker Desktop installed, *Options → Set up private web search* runs SearXNG on this PC instead.
- The first install also downloads a small search model (~140 MB) for meaning-based search in project files and the library.

- **Start / Stop** the server. Green dots mean Stella, voice and sync are ready.
- **Connect your phone**: scan the QR code with the phone camera, then tap **Connect** in StellAI. Or type the address and API key in the app's Settings.
- **Tailscale**: install it on the PC and phone (same account) and pick the Tailscale address to use Stella away from home.
- Closing the window keeps the server running in the tray. Right-click the tray icon → **Quit** to stop everything.
- **Change model** to switch models or re-detect hardware (for example after adding a GPU).
- **Kokoro voice**: pick it on the avatar screen (gear → Voice). The first time, StellAI downloads a private copy of Python (from python.org) and the Kokoro voice into `%LOCALAPPDATA%\StellAI\avatar\kokoro` (~200 MB); Piper speaks until it's ready.
- **Avatar screen** opens Stella full screen with a face and voice (orb or your 3D character); see the main README.
- **Check everything** runs the health check and shows what needs fixing.
- **Back up now** / **Restore a backup…**: backups are also made every night into `%LOCALAPPDATA%\StellAI\backups` (last 14 kept).
- The phone app's **Server** screen and the desktop chat's **Server** button show status, switch models and run the same checks.
- **CAD Designer and PCB Designer modes**: install FreeCAD (freecad.org) and KiCad (kicad.org) with their normal installers; StellAI finds them in Program Files. For circuits as code, also run `pip install skidl` with KiCad's Python (`C:\Program Files\KiCad\<version>\bin\python.exe -m pip install skidl`), install Java 21 (adoptium.net), and put `freerouting-X.jar` from github.com/freerouting/freerouting/releases in a `freerouting` folder next to `chats.db`.
- **3D Modeler mode**: install Blender from blender.org (the normal installer); StellAI finds it in Program Files by itself. For VRoid/VRM characters, also add the VRM add-on in Blender (Edit → Preferences → Get Extensions → search "VRM"). Drop models into `%LOCALAPPDATA%\StellAI\3d\inbox`. Reference pictures work the same as on Linux. The photo-to-3D generator isn't installed automatically on Windows yet: set it up from the Hunyuan3D-2 instructions and put `{"python": "C:\\path\\to\\venv\\Scripts\\python.exe"}` in `%LOCALAPPDATA%\StellAI\3d\generator.json`; it runs when a graphics card has 7–9 GB free (StellAI can't pause Stella's model on Windows).
- **Wake words folder**: drop a trained `hey_stella.onnx` here and it appears in the phone app's wake word list.

## If the AI model won't start on an NVIDIA GPU

llama.cpp publishes CUDA builds for several CUDA versions. A build newer than your NVIDIA driver fails with *"the provided PTX was compiled with an unsupported toolchain"*. StellAI picks the newest build your driver supports, and if a build still fails it switches by itself to an older CUDA build, then to Vulkan (which still uses the GPU), and says so in the log. Updating the NVIDIA driver (GeForce Experience / nvidia.com) lets it use the newest, fastest build: after updating, use **Change model → re-detect**.

## Updates

The app checks this repo's releases every 30 minutes. When there's a newer version it downloads it, checks the SHA-256 checksum published with it, waits until Stella has been idle for 2 minutes, then reinstalls itself silently and restarts in the tray. Your models, chats, memories, projects and settings (all in `%LOCALAPPDATA%\StellAI`) stay as they are.

Under *Options* you can see the version, **Check now** / **Install now**, and turn automatic installs off (it then only tells you an update is available).

## Differences from Linux

- Uses prebuilt engines instead of compiling them, so there's no build step.
- Mermaid flowcharts work only if you install Node.js and then `npm install -g @mermaid-js/mermaid-cli`. Without it, Stella draws flowcharts with schemdraw instead. Schematics and timing diagrams work out of the box.
- Diagram code is limited by a 40-second time limit rather than Linux's CPU and memory limits.

## Build or run from source

```
pip install -r windows/requirements.txt
python windows/stellai_app.py
python windows/test_winsetup.py
```
