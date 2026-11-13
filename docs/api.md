# The hub's web addresses

Every address the StellAI hub answers (port 8092), in the order they're checked. They're defined in
`server/hub/60_http.py`, each right below its `@routes.add(...)` line. **Access:** *always* works even after Stella
has moved to another server; *no key* needs no API key (the web pages and the avatar's files); *API key* needs the
`Authorization: Bearer <key>` header.

This list is made by `python3 server/stellai_hub.py --list-routes`; the tests fail if it's out of date.

| Method | Path | Access |
|---|---|---|
| POST | `/move/receive` | always |
| POST | `/transcribe` | API key |
| POST | `/tts` | API key |
| POST | `/avatar/event` | API key |
| POST | `/avatar/anims/([0-9a-f]{12})` | API key |
| POST | `/avatar/config` | API key |
| POST | `/sync` | API key |
| POST | `/chats/([^/]{1,64})/notes` | API key |
| POST | `/memories` | API key |
| POST | `/memories/suggestions/([0-9a-f]{1,12})` | API key |
| POST | `/memories/settings` | API key |
| POST | `/chats/([^/]{1,64})/docs` | API key |
| POST | `/docs/([0-9a-f]{10})` | API key |
| POST | `/chats/([^/]{1,64})/continue` | API key |
| POST | `/memories/learn` | API key |
| POST | `/llm/v1/chat/completions` | API key |
| POST | `/quick` | API key |
| POST | `/speedtest` | API key |
| POST | `/route` | API key |
| POST | `/context` | API key |
| POST | `/summarize` | API key |
| POST | `/backups` | API key |
| POST | `/backups/config` | API key |
| POST | `/server/model` | API key |
| POST | `/server/download` | API key |
| POST | `/reminders` | API key |
| POST | `/reminders/…` | API key |
| POST | `/lists/…` | API key |
| POST | `/github` | API key |
| POST | `/modes` | API key |
| POST | `/projects` | API key |
| POST | `/projects/([0-9a-f]{12})/notes` | API key |
| POST | `/tools/…` | API key |
| PUT | `/projects/(library|[0-9a-f]{12})/files` | API key |
| PUT | `/avatar/anims` | API key |
| PUT | `/avatar/model` | API key |
| PUT | `/images/([0-9a-f]{16})\.jpg` | API key |
| DELETE | `/modes/(c-[0-9a-f]{10})` | API key |
| DELETE | `/docs/([0-9a-f]{10})` | API key |
| DELETE | `/chats/([^/]{1,64})/notes/([0-9a-f]{1,12})` | API key |
| DELETE | `/lists/items/([0-9a-f]{10})` | API key |
| DELETE | `/projects/(library)/(files)/([0-9a-f]{1,32})` | API key |
| DELETE | `/projects/([0-9a-f]{12})(?:/(notes|files)/([0-9a-f]{1,32}))?` | API key |
| DELETE | `/memories/([0-9a-f]{1,32})` | API key |
| GET | `/health` | always |
| GET | `/move/hello` | always |
| GET | `/move/status` | no key |
| GET | `/avatar/avatar.js` | no key |
| GET | `/avatar/lib/…` | no key |
| GET | `/` | no key |
| GET | `/index.html` | no key |
| GET | `/chat` | no key |
| GET | `/avatar` | no key |
| GET | `/favicon.ico` | no key |
| GET | `/avatar/config` | API key |
| GET | `/avatar/events` | API key |
| GET | `/avatar/bin/…` | API key |
| GET | `/avatar/model.vrm` | API key |
| GET | `/tools` | API key |
| GET | `/reminders` | API key |
| GET | `/lists` | API key |
| GET | `/github` | API key |
| GET | `/search` | API key |
| GET | `/speedtest` | API key |
| GET | `/doctor` | API key |
| GET | `/backups` | API key |
| GET | `/server` | API key |
| GET | `/memories` | API key |
| GET | `/chats/([^/]{1,64})/notes` | API key |
| GET | `/memories/suggestions` | API key |
| GET | `/chats/([^/]{1,64})/docs` | API key |
| GET | `/docs/([0-9a-f]{10})` | API key |
| GET | `/docs/([0-9a-f]{10})/versions/(\d+)` | API key |
| GET | `/projects` | API key |
| GET | `/prompts` | API key |
| GET | `/modes` | API key |
| GET | `/library` | API key |
| GET | `/projects/(library|[0-9a-f]{12})/files/([0-9a-f]{16})\.txt` | API key |
| GET | `/wakewords` | API key |
| GET | `/wakewords/(.+)\.onnx` | API key |
| GET | `/images/([0-9a-f]{16})\.jpg` | API key |
| GET | `/diagrams/([0-9a-f]{16})\.png` | API key |
