# StellAI to-do

Ordered from most to least useful for everyday use and technical/creative work (programming, electronics, diagrams).
Effort is a rough guide: **S** about an hour, **M** an afternoon, **L** a day or more.

1. [ ] **Synced desktop chat** (server, M)
   A browser chat served by the StellAI server that shares the same synced chats as the phone, so you can switch devices mid-conversation.

2. [ ] **Projects** (server + app, M)
   Group chats by project, each with its own pinned notes, files and instructions (e.g. "StellAI app", "Power supply PCB").

3. [ ] **Datasheet library** (server, L)
   Drop PDFs on the server; Stella searches them to answer questions like "what's the ADC reference on the RP2040?"

4. [ ] **Reminders and notes** (server + app, M)
   "Remind me to reflow the board at 5", "add flux to my parts list". Synced to all devices.

5. [ ] **Coding mode** (app, S)
   One tap to switch on thinking mode and a coding-focused prompt for hard bugs.

6. [ ] **Web search** (server, M)
   A private search engine (SearXNG) on the server so Stella can look up current docs and part availability.

7. [ ] **GitHub access** (server, M)
   Stella reads your repositories (StellAI included) to explain code and help debug.

8. [ ] **Quick launch** (app, S)
   A Quick Settings tile and a home-screen widget to talk to Stella in one tap.

9. [ ] **Server controls in the app** (server + app, M)
   See whether the server is up, which model is loaded, and switch models.

## Also discussed, not yet built

- [ ] **Search past conversations**: find relevant old chats by meaning and bring them into the current answer. Builds on memory.
- [ ] **"Hey Stella" wake word**: the app support is done; train the model in the Colab notebook (see the app README) and drop `hey_stella.onnx` into `~/llm/wakewords/` on the server.
- [ ] **Security hardening**: firewall rules limited to Tailscale and your home network, Android backup turned off for the app, and a command to replace the API key.

## Done

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
