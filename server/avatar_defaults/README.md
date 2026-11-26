# Stella's character and animations

These ship with StellAI. Each server copies them into its avatar bin on first start
(`avatar/bin/characters` and `avatar/bin/animations`), and Stella becomes the character on every avatar screen.

| File | What it is | When it plays |
|---|---|---|
| `Stella.vrm` | Stella, VRM 1.0 | the default character |
| `animations/Jumpscare@wake.vrma` | jumpscare | the avatar screen opens or wakes up, the first chat of the morning, her wake-up alarms, or when you ask |
| `animations/Peace@greeting+farewell.vrma` | peace sign | hello and goodbye |
| `animations/Twirl@dance.vrma` | twirl | dances (alongside the built-in hip sway) |
| `animations/Pull_the_trigger@quiet.vrma` | finger guns | when she's bored (after a long quiet) |

The part after `@` in a file name says when it plays; join several with `+`. You can change it any time in the
avatar settings. Deleting one of these files from the bin keeps it deleted; it won't come back on its own.

**Credit:** Stella by ry4. VRM license: https://vrm.dev/licenses/1.0/ (credit required, modification allowed,
redistribution allowed).

If you change these files, update the sha256 sums in `server/hub/42_avatar_files.py` (`DEFAULT_AVATAR`). The tests
check that they match.
