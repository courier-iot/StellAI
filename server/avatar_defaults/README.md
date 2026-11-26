# Stella's character and animations

These ship with StellAI. Each server copies them into its avatar bin on first start
(`avatar/bin/characters` and `avatar/bin/animations`), and Stella becomes the character on every avatar screen.

| File | What it is | When it plays |
|---|---|---|
| `Stella.vrm` | Stella, VRM 1.0 | the default character |
| `animations/Jumpscare@wake.vrma` | jumpscare | the avatar screen opens or wakes up, the first chat of the morning, her wake-up alarms, or when you ask |
| `animations/Peace@greeting+farewell.vrma` | peace sign | hello and goodbye |
| `animations/Twirl@dance.vrma` | twirl | dances (alongside the built-in hip sway) |
| `animations/Pull_the_trigger@quiet.vrma` | finger guns | when she's bored (10 quiet minutes) |
| `animations/Cute_surprise@surprise.vrma` | cute surprised pose, held 3 s | when she's surprised ("wow", "no way", "oh my") or asked |
| `animations/Sleeping_waiting_start@sleep.vrma` | hand to her head, then she sits back on thin air | after 20 quiet minutes |
| `animations/Sleeping_waiting@sleeping.vrma` | lounging, legs out and crossed (loops) | until something happens |
| `animations/Sleeping_waiting_end@sleepend.vrma` | back on her feet | when someone talks, the screen wakes, or a gesture is asked for |

The part after `@` in a file name says when it plays; join several with `+`. You can change it any time in the
avatar settings. Deleting one of these files from the bin keeps it deleted; it won't come back on its own.

**Credits:** Stella by ry4. Cute surprise: the free "stand-me" pose (NCPT). Sleeping/waiting: the free
"USANEKO AFK SiuSiu" emote (USANEKO). Both shared with permission to redistribute; converted from Unity Humanoid
clips with `tools/unity2vrma.py`. Stella: VRM license: https://vrm.dev/licenses/1.0/ (credit required, modification allowed,
redistribution allowed).

To convert another Unity Humanoid animation: `python3 tools/unity2vrma.py thing.unitypackage
server/avatar_defaults/Stella.vrm out/` (needs numpy), then rename each file with its moment after `@`.

If you change these files, update the sha256 sums in `server/hub/42_avatar_files.py` (`DEFAULT_AVATAR`). The tests
check that they match.
