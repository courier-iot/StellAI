# Installing StellAI on Linux

For a desktop, laptop or server running Linux Mint, Ubuntu or Debian (Fedora, Arch and openSUSE work too). For a
Proxmox server, use [install-proxmox.md](install-proxmox.md) instead.

You'll need about an hour, mostly waiting: llama.cpp is compiled for your exact hardware (10–30 minutes) and the
model downloads (2–75 GB depending on the model).

## Before you start

1. **Graphics card driver (NVIDIA only).** Mint: **Menu → Driver Manager**, pick the recommended NVIDIA driver,
   **Apply Changes**, reboot. Ubuntu: **Software & Updates → Additional Drivers**. Check it in a terminal:
   `nvidia-smi` should list your card. AMD cards need nothing extra.
2. **Disk space.** At least the model's size plus about 15 GB for the tools. The setup shows the exact amount
   before downloading anything.
3. **Internet.** The setup downloads the build tools, llama.cpp, the model and the voice model.

## 1. Get the files

Download the project from GitHub (green **Code** button → **Download ZIP**) and unzip it, or:

```bash
git clone https://github.com/courier-iot/StellAI.git
cd StellAI/server
```

## 2. Run the setup

Open a terminal in the `server` folder and run it as your normal user (not with `sudo`; it asks for your
password when it needs it):

```bash
chmod +x stellai-setup.sh
./stellai-setup.sh
```

## 3. Answer the setup screens

Use the arrow keys to move, the **space bar** to tick boxes, **Enter** to confirm and **Tab** to reach OK/Cancel.

| Screen | What to pick |
|---|---|
| What should this computer be? | **Server and client** for a desktop you also chat on, **Server only** for a machine that just serves Stella, **Client only** to use Stella running on another computer (it lists the StellAI servers on your Tailscale devices; then you only enter the API key). |
| This computer | Just a summary. Check it found your graphics card(s). |
| Graphics cards | Only with 2+ cards. Tick the ones Stella's model may use. Leaving one out keeps it free for the helper and 3D jobs. |
| Memory for StellAI | *Nearly all* on a dedicated server; *three quarters* on a PC you also use. |
| Stella's model | **★ Best overall** is the safe choice. **⌨ Best coder** for lots of code, CAD and PCB work. *Show every model* lists every size that fits. |
| Conversation length | Keep the recommended one. The test run checks it fits. |
| Helper model | Keep the recommended one (on the processor). |
| Extras | Photos, Tailscale and web search are on by default. Add the 3D Modeler, CAD and PCB designers if you want them. Keep **Test run** on. On a computer without a screen, keep **Start StellAI Server by itself** on. |
| Ready to install | Check the summary and download size, then **Yes**. |

Then it works by itself. Near the end it may ask about Tailscale sign-in (a link to open on any device) and the
extras you picked.

## 4. The test run

The setup loads the model a few times to measure it: how fast it writes and reads, whether the conversation
length fits (it halves it until it does), and whether the MTP speed-up helps on your computer (kept only if it's
at least 10% faster). The results screen shows the numbers. Run it again any time from the menu: **StellAI Speed
Test** (close StellAI Server first).

## 5. Start StellAI

- **Desktop:** open **StellAI Server** from the menu. A terminal window shows the server address, the API key and
  a QR code. Leave it open.
- **As a service** (if you ticked *Start StellAI Server by itself*): it's already running and starts at every boot.
  - Status: `systemctl status stellai`
  - Address, key and QR code: `journalctl -u stellai -e`
  - Stop / start: `sudo systemctl stop stellai` / `sudo systemctl start stellai`

## 6. Connect your phone

1. Install the StellAI app (the APK from the GitHub release).
2. Install **Tailscale** on the phone and sign in with the same account as the server.
3. In the app: **Settings → Scan to connect**, scan the QR code, tap **Connect**. Or type the server address
   (`http://100.x.x.x:8080`, the Tailscale one works at home and away) and the API key.
4. **Test connection**: both lines should show ✓.

Desktop chat in any browser: `http://SERVER:8092`.

## 7. Check everything

Run **StellAI Health Check** from the menu (or `~/llm/stellai-doctor.sh`). Every line should be OK; each problem
comes with what to do about it.

## Later

- **Updates:** the server updates itself from GitHub every 30 minutes when Stella is idle. Re-run
  `stellai-setup.sh` only for new engine builds, a different model or new extras. Your chats, memories and
  settings are kept.
- **Another model:** the phone app's **Server** screen lists the models that fit and can download and switch.
- **Backups:** nightly into `~/llm/backups`; restore with `~/llm/stellai-restore.sh`.

## Moving Stella here or away

Say "Stella, move yourself to *name*" on the computer she's on now (see the README's *Moving Stella* section). Both computers need StellAI installed and Tailscale signed in to the same account. The one she leaves becomes a client.

## Troubleshooting

| Problem | Fix |
|---|---|
| "This computer has an NVIDIA graphics card, but its driver isn't installed" | Install the driver (Before you start, step 1), reboot, run the setup again. |
| The setup stops during the CUDA build | Run it again; it retries with an older compiler and falls back to Vulkan. |
| The test run says the model didn't load | Pick a smaller model or a shorter conversation length (run the setup again). |
| The phone can't connect | Same Tailscale account on both? Is StellAI Server running? Try the health check. |
| Very slow replies | Run **StellAI Speed Test**; check the health check's *Graphics cards* line. |
