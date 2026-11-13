# Installing StellAI on a Proxmox server

Stella runs in her own **LXC container** on Proxmox: a lightweight Linux system that shares the host's graphics
driver. No PCIe passthrough is needed, which matters on HP ProLiant Gen8 servers (their firmware blocks
passthrough with the "RMRR" error), and the same cards stay usable by other containers.

One script on the Proxmox host does everything: the NVIDIA driver on the host, the container, the cards shared
into it, and the normal StellAI setup inside it.

Written for an HP ProLiant DL380p Gen8 with NVIDIA Tesla T4 cards; other servers are the same minus the HP steps.

## Part 1: Prepare the server (HP ProLiant Gen8)

1. **Fit the cards.** The T4 is a low-profile, single-slot card powered by the slot (70 W), so it fits the
   DL380p's riser slots. It has no fan of its own and relies on the server's airflow: keep the lid on and all
   fans in.
2. **Open the BIOS (RBSU):** press **F9** at boot.
   - **System Options → Processor Options → Intel Virtualization Technology: Enabled** (for VMs).
   - **Power Management → HP Power Profile: Maximum Performance** (Stella's speed depends on the processor and
     memory not throttling).
   - **Advanced Options → Fan and Thermal Options → Thermal Configuration: Increased Cooling** (keeps the
     passive T4s cool).
   - Save and exit (**F10**).
3. **Disks.** Proxmox installs on the Smart Array controller's logical drive like any disk. Put Stella's container
   on the fastest storage you have (SSD best): the models are large and load from it.

## Part 2: Install Proxmox VE

1. Download the **Proxmox VE ISO** from proxmox.com (Downloads).
2. Get it to the server:
   - **iLO virtual media:** open the server's iLO in a browser, **Remote Console → Virtual Drives → Image File
     CD-ROM/DVD**, choose the ISO, reboot; or
   - write it to a USB stick (balenaEtcher or Rufus in DD mode) and boot from it (**F11** boot menu).
3. Choose **Install Proxmox VE (Graphical)** and follow it: target disk, country and time zone, a root password
   and email, and the network (a fixed address such as `192.168.1.10/24`, your router as gateway and DNS).
4. When it reboots, open `https://SERVER-IP:8006` in a browser on another computer and log in as **root**
   (realm *Linux PAM*). Accept the certificate warning (it's the server's own certificate).
5. **Updates without a subscription:** **node → Updates → Repositories**: disable the two *enterprise*
   repositories and **Add** the *No-Subscription* one. Then **Updates → Refresh → Upgrade**, and reboot if the
   kernel was updated.

## Part 3: Install StellAI

1. Open the host's shell: in the web interface, click your node (e.g. *pve*) → **Shell**. (Or `ssh root@SERVER-IP`.)
2. Get StellAI:
   ```bash
   apt-get install -y git
   git clone https://github.com/courier-iot/StellAI.git
   cd StellAI/server
   ```
3. Run the Proxmox script:
   ```bash
   bash stellai-proxmox.sh
   ```
4. **The NVIDIA driver.** If the host doesn't have one yet, the script switches off the open *nouveau* driver
   first and asks you to **reboot once**. After the reboot, open the Shell again and run the same command; it
   continues where it stopped. Then it builds the NVIDIA driver for the Proxmox kernel (a few minutes). With DKMS,
   it rebuilds itself after Proxmox kernel updates.
5. **The container questions:**

   | Question | Suggestion |
   |---|---|
   | Container ID | Keep the suggested number. |
   | Name | `stellai` |
   | RAM | Keep the suggestion (all but 4–8 GB), minus whatever your VMs need. |
   | CPU threads | All of them, unless VMs need some. |
   | Storage | The fastest one (SSD). |
   | Disk size | 200 GB is room for a few models (the 35B is ~22 GB, the 122B 45–75 GB). |
   | Network bridge | `vmbr0` |
   | Address | A fixed one like `192.168.1.50/24,gw=192.168.1.1` makes the phone setup easier; `dhcp` works too. |

6. **The StellAI setup screens** then appear, inside the container. They're the same as on a normal Linux PC:
   follow [install-linux.md, step 3](install-linux.md#3-answer-the-setup-screens). On the DL380p:
   - **What should this computer be?** **Server only** (it's preselected in a container).
   - **Graphics cards:** tick both T4s for the biggest, fastest model, or only one to keep the other free for 3D
     jobs and the helper.
   - **Memory:** *nearly all* (the container is dedicated to Stella).
   - **Extras:** keep **Start StellAI Server by itself** on (the container has no desktop).
7. At the end, the script shows the **server address** and **API key**.

## Part 4: Connect your phone

Same as on Linux ([install-linux.md, step 6](install-linux.md#6-connect-your-phone)). For Tailscale, the setup
installs it in the container; open the sign-in link it prints, on any device. Then use the container's
Tailscale address (`http://100.x.x.x:8080`) in the app, so it works at home and away.

## Living with it

| Task | How |
|---|---|
| Open a shell in Stella's container | Web interface: container → **Console**; or on the host: `pct enter ID`, then `su - stellai` |
| Server status / log, QR code | `pct exec ID -- journalctl -u stellai -e` |
| Restart Stella | `pct exec ID -- systemctl restart stellai`, or restart the container |
| Speed test | In the container: `sudo systemctl stop stellai`, then `~/llm/stellai-tune.sh`, then `sudo systemctl start stellai` |
| Health check | In the container: `~/llm/stellai-doctor.sh` |
| Change model, cards or extras | In the container: `bash ~/stellai-setup.sh` |
| Back up the whole container | Web interface: container → **Backup → Backup now** (plus StellAI's own nightly backups in `~/llm/backups`) |

StellAI updates itself from GitHub inside the container, as on any Linux server.

### Updating the NVIDIA driver on the host

The driver programs inside the container must be **exactly** the host's version. Proxmox kernel updates are fine
(DKMS rebuilds the same version). If you deliberately install a **new driver version** on the host, run
`bash stellai-proxmox.sh` again afterwards: it installs the matching version in the container. Until then the
health check's *Graphics cards* line says **version mismatch**, and Stella runs (slowly) on the processor.

## Troubleshooting

| Problem | Fix |
|---|---|
| `nvidia-smi` fails on the host after installing | Reboot the host, then run the script again. |
| The driver build fails | `apt install proxmox-default-headers`, reboot, run the script again. Check `/var/log/nvidia-installer.log`. |
| Health check: *version mismatch* | Run `bash stellai-proxmox.sh` on the host again. |
| The container can't see the cards (`nvidia-smi` fails inside) | On the host: `nvidia-smi` must work first; then run the script again (it re-shares `/dev/nvidia*`). |
| Tailscale won't start in the container | Run the script again (it shares `/dev/net/tun`), then restart the container. |
| Web search (SearXNG / Docker) won't start | The container needs *nesting* and *keyctl*: the script sets both; check **container → Options → Features**. |
