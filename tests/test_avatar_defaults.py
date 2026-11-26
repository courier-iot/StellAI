"""Stella's shipped character and animations, the new animation moments, wake-up alarms and the gesture tool."""
import hashlib
import json
import os
import shutil
import tempfile

from helpers import ROOT, call, check, done, start_hub

h, web, tmp, port = start_hub()
DEFAULTS = os.path.join(ROOT, "server", "avatar_defaults")

# ---- the shipped files match the sums in the hub (so a changed file without new sums fails here)
for rel, sha in h.DEFAULT_AVATAR.items():
    local = os.path.join(DEFAULTS, rel.split("/", 1)[1]) if rel.startswith("characters/") else os.path.join(DEFAULTS, rel)
    with open(local, "rb") as f:
        check(hashlib.sha256(f.read()).hexdigest() == sha, f"shipped {rel} matches DEFAULT_AVATAR")
with open(os.path.join(DEFAULTS, "Stella.vrm"), "rb") as f:
    head = f.read(20 + 200_000)
n = int.from_bytes(head[12:16], "little")
meta = json.loads(head[20:20 + n])["extensions"]["VRMC_vrm"]["meta"]
check(meta["name"] == "Stella" and meta["allowRedistribution"] is True and meta["creditNotation"] == "required",
      "Stella's license allows shipping her, with credit", meta)

# ---- seeding a fresh server: everything arrives and Stella becomes the 3D character
av = tempfile.mkdtemp(prefix="stellai-av-")
h.AVATAR["dir"] = av
added = h.seed_default_avatar(fetch=lambda *a: (_ for _ in ()).throw(AssertionError("shouldn't download")))
check(len(added) == len(h.DEFAULT_AVATAR), "every default file is added from the local copy", added)
cfg = h.avatar_config()
check(cfg["character"] == "Stella.vrm" and cfg["style"] == "vrm", "Stella is the character, in 3D", (cfg["character"], cfg["style"]))
anims = {a["name"]: a["when"] for a in cfg["animations"]}
check(anims == {"Peace": "greeting+farewell", "Twirl": "dance", "Pull the trigger": "quiet", "Jumpscare": "wake",
               "Cute surprise": "surprise", "Sleeping waiting start": "sleep", "Sleeping waiting": "sleeping",
               "Sleeping waiting end": "sleepend"},
      "the animations play at the right moments", anims)
check(h.seed_default_avatar() == [], "a second start adds nothing")

# a deleted default stays deleted; a user's own file with the same name is left alone
os.remove(os.path.join(av, "bin", "animations", "Twirl@dance.vrma"))
check(h.seed_default_avatar() == [], "a deleted default doesn't come back")
h.save_avatar_config({"character": "Stella.vrm", "style": "orb"})
check(h.avatar_config()["style"] == "orb", "the user's choice of look sticks after seeding")

# ---- an existing install (their own character chosen, no local copy): downloaded, checked, and Stella takes over once
av2 = tempfile.mkdtemp(prefix="stellai-av2-")
h.AVATAR["dir"] = av2
os.makedirs(os.path.join(av2, "bin", "characters"))
with open(os.path.join(av2, "bin", "characters", "Mine.vrm"), "wb") as f:
    f.write(b"glTF-mine")
os.makedirs(os.path.join(av2, "bin", "animations"))
with open(os.path.join(av2, "bin", "animations", "Twirl@dance.vrma"), "wb") as f:
    f.write(b"glTF-their-own-twirl")
h.save_avatar_config({"character": "Mine.vrm"})
urls = []
real = h._default_avatar_sources
h._default_avatar_sources = lambda: []


def fake_fetch(url, token="", timeout=60):
    urls.append(url)
    name = url.rsplit("/avatar_defaults/", 1)[1]
    import urllib.parse
    with open(os.path.join(DEFAULTS, *urllib.parse.unquote(name).split("/")), "rb") as f:
        return f.read()


added = h.seed_default_avatar("someone/StellAI", fetch=fake_fetch)
check("animations/Twirl@dance.vrma" not in added and len(added) == len(h.DEFAULT_AVATAR) - 1,
      "downloads what's missing, keeps the user's own file", added)
check(all(u.startswith(h.RAW_BASE + "/someone/StellAI/HEAD/server/avatar_defaults/") for u in urls)
      and any("Peace%40greeting%2Bfarewell.vrma" in u for u in urls), "downloads from the update repo, names escaped", urls)
with open(os.path.join(av2, "bin", "animations", "Twirl@dance.vrma"), "rb") as f:
    check(f.read() == b"glTF-their-own-twirl", "their own Twirl wasn't overwritten")
check(h.avatar_config()["character"] == "Stella.vrm", "Stella becomes the character when she first arrives")

# a corrupted download is refused; offline is fine (tried again next start)
av3 = tempfile.mkdtemp(prefix="stellai-av3-")
h.AVATAR["dir"] = av3
check(h.seed_default_avatar(fetch=lambda *a: b"glTF-wrong") == [], "a download that doesn't match is skipped")


def offline(*a):
    raise OSError("no network")


check(h.seed_default_avatar(fetch=offline) == [], "offline: nothing added, no crash")
h._default_avatar_sources = real
check(len(h.seed_default_avatar()) == len(h.DEFAULT_AVATAR), "the next start (with a local copy) adds them all")

# ---- moments: several per file, new ones, junk refused
check(h.valid_when("greeting+farewell") == "greeting+farewell" and h.valid_when("wake") == "wake"
      and h.valid_when("off+dance") == "dance" and h.valid_when("nonsense") == "" and h.valid_when("off") == "off",
      "valid_when")
aid = next(a["id"] for a in h.list_animations() if a["name"] == "Twirl")
lst = h.update_animation(aid, {"when": "dance+happy"})
check(next(a["when"] for a in lst if a["name"] == "Twirl") == "dance+happy", "a file can play at several moments")
lst = h.update_animation(aid, {"when": "bogus"})
check(next(a["when"] for a in lst if a["name"] == "Twirl") == "dance+happy", "an unknown moment is ignored")

# ---- wake-up alarms: stored with the alarm flag, served with /reminders, answered like reminders
r, summary = h.tool_set_wake_alarm({"when": "2099-01-02T07:00:00-06:00", "repeat": "weekdays"})
check("Wake-up alarm set" in summary and r["label"] == "Wake up" and r["repeat"] == "weekdays", "set_wake_alarm", (r, summary))
st, lst = call(port, "GET", "/reminders")
al = [x for x in lst["reminders"] if x["alarm"]]
check(st == 200 and len(al) == 1 and al[0]["text"] == "Wake up", "alarms are listed with alarm: true", lst)
st, plain = call(port, "POST", "/reminders", {"text": "Water plants", "when": "2099-01-02T09:00:00-06:00"})
check(st == 200 and plain["alarm"] is False, "plain reminders stay plain", plain)
st, x = call(port, "POST", "/reminders", {"text": "Up", "when": "2099-01-03T06:00:00-06:00", "alarm": True})
check(st == 200 and x["alarm"] is True, "the API can set alarms too", x)
st, s2 = call(port, "POST", "/reminders/" + al[0]["id"], {"snooze_minutes": 9})
check(st == 200 and s2["alarm"] is True and not s2["done"], "snoozing keeps it an alarm", s2)
check({"set_wake_alarm", "avatar_gesture"} <= set(h.SERVER_TOOLS), "the new tools are registered")
names = h.select_tools("wake me up at 7 tomorrow")
names = [n if isinstance(n, str) else n["function"]["name"] for n in names] if names else []
check("set_wake_alarm" in names, "'wake me up' sends the wake-alarm tool", names)

# ---- the gesture tool tells every avatar screen
seq = h.AVATAR_BUS.seq
r, summary = h.tool_avatar_gesture({"animation": "Twirl"})
ev = h.AVATAR_BUS.since(seq)["events"]
check(len(ev) == 1 and ev[0]["state"] == "gesture" and ev[0]["text"] == "Twirl", "avatar_gesture posts a gesture event", ev)
check("Twirl" in r["her_animations"] and "Wave" in r["her_animations"], "it lists what she can play", r)
st, _ = call(port, "POST", "/avatar/event", {"state": "gesture", "text": "dance"})
check(st == 200, "screens accept gesture events")

for d in (av, av2, av3):
    shutil.rmtree(d, ignore_errors=True)
done()
