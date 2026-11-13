# Part of stellai_hub.py: build_hub.py joins server/hub/*.py in order into one file (one shared namespace).
# ----------------------------------------------------------------------------- avatar screen and voice
# A full-screen page (/avatar) with a 3D VRM character or an animated orb that listens, thinks and talks.
# Stella's voice is Piper (local neural text-to-speech). Everything is downloaded on first use.
import zipfile

AVATAR = {"dir": "", "lock": threading.Lock(), "status": {}}
THREE_VERSION = "0.180.0"
AVATAR_LIBS = {   # path under the lib folder -> URLs to try
    "build/three.module.js": [f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/build/three.module.js",
                              f"https://unpkg.com/three@{THREE_VERSION}/build/three.module.js"],
    "build/three.core.js": [f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/build/three.core.js",
                            f"https://unpkg.com/three@{THREE_VERSION}/build/three.core.js"],
    "examples/jsm/loaders/GLTFLoader.js": [
        f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/examples/jsm/loaders/GLTFLoader.js",
        f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/loaders/GLTFLoader.js"],
    "examples/jsm/utils/BufferGeometryUtils.js": [
        f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/examples/jsm/utils/BufferGeometryUtils.js",
        f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/utils/BufferGeometryUtils.js"],
    "three-vrm.module.min.js": ["https://cdn.jsdelivr.net/npm/@pixiv/three-vrm@3/lib/three-vrm.module.min.js",
                                "https://unpkg.com/@pixiv/three-vrm@3/lib/three-vrm.module.min.js"],
    # animations: .vrma (VRM Animation) and .fbx (Mixamo)
    "three-vrm-animation.module.min.js": [
        "https://cdn.jsdelivr.net/npm/@pixiv/three-vrm-animation@3/lib/three-vrm-animation.module.min.js",
        "https://unpkg.com/@pixiv/three-vrm-animation@3/lib/three-vrm-animation.module.min.js"],
}
for _rel in ("loaders/FBXLoader.js", "libs/fflate.module.js", "curves/NURBSCurve.js", "curves/NURBSUtils.js"):
    AVATAR_LIBS["examples/jsm/" + _rel] = [f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/examples/jsm/{_rel}",
                                           f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/{_rel}"]
# Wake word in the browser: ONNX Runtime Web + the same openWakeWord models as the phone app
ORT_VERSION = "1.20.1"
for _f in ("ort.wasm.min.js", "ort-wasm-simd-threaded.wasm", "ort-wasm-simd-threaded.mjs"):
    AVATAR_LIBS["ort/" + _f] = [f"https://cdn.jsdelivr.net/npm/onnxruntime-web@{ORT_VERSION}/dist/{_f}",
                                f"https://unpkg.com/onnxruntime-web@{ORT_VERSION}/dist/{_f}"]
OWW_MODELS = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx", "alexa_v0.1.onnx",
              "hey_mycroft_v0.1.onnx")
for _f in OWW_MODELS:
    AVATAR_LIBS["oww/" + _f] = [f"https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/{_f}"]
# Silero VAD: tells speech from silence/noise, so Stella knows when you've finished talking
AVATAR_LIBS["vad/silero_vad.onnx"] = [
    "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx",
    "https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx"]
LIB_TYPES = {".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
             ".wasm": "application/wasm", ".onnx": "application/octet-stream"}
PIPER_RELEASE = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
VOICES = {   # Piper voices (huggingface.co/rhasspy/piper-voices), ~60 MB each
    "en_US-lessac-medium": "Lessac: clear, warm (US)",
    "en_US-amy-medium": "Amy: bright, friendly (US)",
    "en_US-hfc_female-medium": "HFC: soft, calm (US)",
    "en_US-kristin-medium": "Kristin: expressive (US)",
    "en_GB-alba-medium": "Alba: gentle Scottish accent (UK)",
    "en_GB-jenny_dioco-medium": "Jenny: natural British (UK)",
}
DEFAULT_VOICE = "en_US-lessac-medium"


def _voice_url(name, ext):
    lang, rest = name.split("-", 1)
    speaker, quality = rest.rsplit("-", 1)
    return (f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{lang.split('_')[0]}/{lang}/{speaker}/"
            f"{quality}/{name}.onnx{ext}")


def _avatar_path(*parts):
    return os.path.join(AVATAR["dir"], *parts)


def avatar_config():
    cfg = {"style": "orb", "voice": DEFAULT_VOICE, "speak": True, "speak_others": True, "speed": 1.0,
           "name_tag": True, "camera": "upper", "height_cm": 172.7, "light": "auto", "wake": True,
           "wake_word": "hey_jarvis", "wake_sensitivity": 0.5, "gestures": True, "character": "",
           "engine": "piper", "kokoro_voice": "af_heart", "followup": True, "barge_in": False,
           "neon": False, "neon_glow": "#3dff9e", "neon_haze": "#ff3fb4", "neon_crowd": True}
    try:
        with open(_avatar_path("config.json")) as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in cfg})
    except (OSError, ValueError):
        pass
    if cfg["voice"] not in VOICES:
        cfg["voice"] = DEFAULT_VOICE
    chars = list_characters()
    if cfg.get("character") not in [c["file"] for c in chars]:
        cfg["character"] = chars[0]["file"] if chars else ""     # newest one
    cfg["characters"] = chars
    cfg["has_model"] = bool(cfg["character"])
    cfg["model_v"] = next((c["v"] for c in chars if c["file"] == cfg["character"]), 0)
    cfg["rev"] = avatar_rev()
    cfg["bin"] = _avatar_path("bin")
    if cfg["style"] == "vrm" and not cfg["has_model"]:
        cfg["style"] = "orb"
    cfg["voices"] = [{"id": k, "label": v} for k, v in VOICES.items()]
    cfg["animations"] = list_animations()
    cfg["kokoro_voices"] = [{"id": k, "label": v} for k, v in KOKORO_VOICES.items()]
    cfg["kokoro_available"] = KOKORO.available()
    cfg["wake_words"] = [{"id": "hey_jarvis", "label": "Hey Jarvis"}, {"id": "alexa", "label": "Alexa"},
                         {"id": "hey_mycroft", "label": "Hey Mycroft"}] + [
        {"id": "custom:" + n, "label": n.replace("_", " ").title()} for n in custom_wakewords()]
    cfg["status"] = dict(AVATAR["status"])
    return cfg


def save_avatar_config(new):
    cur = {k: v for k, v in avatar_config().items()
           if k not in ("has_model", "model_v", "voices", "status", "animations", "wake_words", "characters", "rev", "bin",
                        "kokoro_voices", "kokoro_available")}
    if new.get("style") in ("orb", "vrm"):
        cur["style"] = new["style"]
    if new.get("voice") in VOICES:
        cur["voice"] = new["voice"]
    if "height_cm" in new:
        cur["height_cm"] = round(max(140.0, min(200.0, float(new["height_cm"]))), 1)
    if new.get("light") in ("auto", "on", "off"):
        cur["light"] = new["light"]
    if "wake_sensitivity" in new:
        cur["wake_sensitivity"] = round(max(0.2, min(0.9, float(new["wake_sensitivity"]))), 2)
    if isinstance(new.get("wake_word"), str) and re.fullmatch(r"(custom:)?[A-Za-z0-9_.-]{1,64}", new["wake_word"]):
        cur["wake_word"] = new["wake_word"]
    if new.get("engine") in ("piper", "kokoro"):
        cur["engine"] = new["engine"]
    if new.get("kokoro_voice") in KOKORO_VOICES:
        cur["kokoro_voice"] = new["kokoro_voice"]
    for k in ("neon_glow", "neon_haze"):
        if isinstance(new.get(k), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", new[k]):
            cur[k] = new[k].lower()
    for k in ("speak", "speak_others", "name_tag", "wake", "gestures", "followup", "barge_in", "neon", "neon_crowd"):
        if k in new:
            cur[k] = bool(new[k])
    if "speed" in new:
        cur["speed"] = max(0.6, min(1.6, float(new["speed"])))
    if new.get("camera") in ("face", "upper", "full"):
        cur["camera"] = new["camera"]
    if isinstance(new.get("character"), str) and new["character"] in [c["file"] for c in list_characters()]:
        cur["character"] = new["character"]
    if new.get("remove_model"):
        name = new.get("remove_model") if isinstance(new.get("remove_model"), str) else cur.get("character")
        if name and _safe_bin_name(name):
            try:
                os.remove(_avatar_path("bin", "characters", name))
            except OSError:
                pass
        if not list_characters():
            cur["style"] = "orb"
    os.makedirs(AVATAR["dir"], exist_ok=True)
    with open(_avatar_path("config.json.part"), "w") as f:
        json.dump(cur, f, indent=1)
    os.replace(_avatar_path("config.json.part"), _avatar_path("config.json"))
    if cur["voice"] != TTS.voice:
        threading.Thread(target=ensure_voice, args=(cur["voice"],), daemon=True).start()
    if cur.get("engine") == "kokoro":
        KOKORO.prepare()
    return avatar_config()


def _fetch_first(urls, dest):
    err = None
    for u in urls:
        try:
            _download_file(u, dest, {})
            return
        except (urllib.error.URLError, OSError) as e:
            err = e
            try:
                os.remove(dest + ".part")
            except OSError:
                pass
    raise OSError(f"couldn't download {os.path.basename(dest)}: {err}")


def ensure_avatar_libs():
    """three.js and three-vrm for the 3D character (~1.6 MB, once)."""
    missing = [p for p in AVATAR_LIBS if not os.path.isfile(_avatar_path("lib", p))]
    if not missing:
        AVATAR["status"]["libs"] = "ready"
        return True
    AVATAR["status"]["libs"] = "downloading"
    try:
        for p in missing:
            dest = _avatar_path("lib", p)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _fetch_first(AVATAR_LIBS[p], dest)
        AVATAR["status"]["libs"] = "ready"
        return True
    except OSError as e:
        AVATAR["status"]["libs"] = f"failed: {e}"
        log(f"avatar: {e}")
        return False


def piper_exe():
    d = _avatar_path("piper")
    for name in ("piper.exe", "piper"):
        if os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    return ""


def ensure_piper():
    """The Piper speech engine (~25 MB, once)."""
    if piper_exe():
        return True
    import platform
    import tarfile
    machine = platform.machine().lower()
    if os.name == "nt":
        asset = "piper_windows_amd64.zip"
    elif machine in ("x86_64", "amd64"):
        asset = "piper_linux_x86_64.tar.gz"
    elif machine in ("aarch64", "arm64"):
        asset = "piper_linux_aarch64.tar.gz"
    elif machine.startswith("armv7"):
        asset = "piper_linux_armv7l.tar.gz"
    else:
        AVATAR["status"]["voice"] = f"failed: no Piper build for {machine}"
        return False
    AVATAR["status"]["voice"] = "downloading the speech engine"
    tmp = _avatar_path(asset)
    try:
        os.makedirs(AVATAR["dir"], exist_ok=True)
        _download_file(PIPER_RELEASE + asset, tmp, {})
        target = os.path.realpath(AVATAR["dir"])
        if asset.endswith(".zip"):
            with zipfile.ZipFile(tmp) as z:
                for n in z.namelist():
                    if not os.path.realpath(os.path.join(target, n)).startswith(target + os.sep):
                        raise OSError("unsafe file in the Piper download")
                z.extractall(target)
        else:
            with tarfile.open(tmp) as t:
                for m in t.getmembers():
                    if not os.path.realpath(os.path.join(target, m.name)).startswith(target + os.sep) \
                            or m.issym() or m.islnk() and not os.path.realpath(
                                os.path.join(target, m.linkname)).startswith(target + os.sep):
                        raise OSError("unsafe file in the Piper download")
                t.extractall(target)
        exe = piper_exe()
        if exe and os.name != "nt":
            os.chmod(exe, 0o755)
        return bool(exe)
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
        AVATAR["status"]["voice"] = f"failed: {e}"
        log(f"avatar voice: {e}")
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def ensure_voice(name):
    if not ensure_piper():
        return False
    model = _avatar_path("voices", name + ".onnx")
    if os.path.isfile(model) and os.path.isfile(model + ".json"):
        AVATAR["status"]["voice"] = "ready"
        return True
    AVATAR["status"]["voice"] = f"downloading the voice {VOICES.get(name, name).split(':')[0]}"
    try:
        os.makedirs(os.path.dirname(model), exist_ok=True)
        _download_file(_voice_url(name, ".json"), model + ".json", {})
        _download_file(_voice_url(name, ""), model, {})
        AVATAR["status"]["voice"] = "ready"
        return True
    except (urllib.error.URLError, OSError) as e:
        AVATAR["status"]["voice"] = f"failed: {e}"
        log(f"avatar voice: {e}")
        return False


def prepare_avatar():
    """Downloads what the avatar screen needs, once, in the background (called when the page first opens)."""
    if AVATAR.get("preparing"):
        return
    AVATAR["preparing"] = True

    def work():
        try:
            ensure_avatar_libs()
            cfg = avatar_config()
            if ensure_voice(cfg["voice"]):
                try:
                    TTS.say("Hello.", cfg["voice"], cfg["speed"])     # keep the voice loaded: the first reply starts sooner
                except (RuntimeError, OSError):
                    pass
            if cfg.get("engine") == "kokoro":
                KOKORO.prepare()
        finally:
            AVATAR["preparing"] = False
    threading.Thread(target=work, daemon=True).start()


SPEAKABLE_DROP = [
    (re.compile(r"```[\s\S]*?(```|$)"), " (the code is on screen) "),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"https?://[^\s,)]+"), " a link "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"[*_#>|~]+"), " "),
    (re.compile(r"^\s*[-+]\s+", re.M), ""),
    (re.compile(r"\s+"), " "),
]


# ---- Speech-friendly text: say "five volts", not "five V"
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
        "seventeen eighteen nineteen".split()
_TENS = "  twenty thirty forty fifty sixty seventy eighty ninety".split(" ")


def number_words(n):
    """Whole numbers to words (up to the trillions), e.g. 1250 -> 'one thousand two hundred fifty'."""
    n = int(n)
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + number_words(n % 100))
    for value, name in ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")):
        if n >= value:
            return number_words(n // value) + " " + name + ("" if n % value == 0 else " " + number_words(n % value))
    return str(n)


def say_number(s):
    """'3.3' -> 'three point three', '1,200' -> 'one thousand two hundred', '0.5' -> 'zero point five'."""
    s = s.replace(",", "")
    neg = s.startswith("-")
    s = s.lstrip("-+")
    if "." in s:
        whole, frac = s.split(".", 1)
        out = number_words(int(whole or 0)) + " point " + " ".join(_ONES[int(d)] for d in frac if d.isdigit())
    else:
        out = number_words(int(s)) if len(s) <= 15 else s
    return ("minus " if neg else "") + out


# unit: (singular, plural); matched right after a number, e.g. 5V, 3.3 V, 220µF, 12 mAh, 25°C
_UNITS = {
    "V": ("volt", "volts"), "mV": ("millivolt", "millivolts"), "kV": ("kilovolt", "kilovolts"),
    "A": ("amp", "amps"), "mA": ("milliamp", "milliamps"), "µA": ("microamp", "microamps"), "uA": ("microamp", "microamps"),
    "W": ("watt", "watts"), "mW": ("milliwatt", "milliwatts"), "kW": ("kilowatt", "kilowatts"),
    "kWh": ("kilowatt hour", "kilowatt hours"), "Wh": ("watt hour", "watt hours"), "mAh": ("milliamp hour", "milliamp hours"),
    "Ah": ("amp hour", "amp hours"),
    "Ω": ("ohm", "ohms"), "ohm": ("ohm", "ohms"), "kΩ": ("kilohm", "kilohms"), "MΩ": ("megohm", "megohms"),
    "k": ("k", "k"),
    "F": ("farad", "farads"), "µF": ("microfarad", "microfarads"), "uF": ("microfarad", "microfarads"),
    "nF": ("nanofarad", "nanofarads"), "pF": ("picofarad", "picofarads"),
    "H": ("henry", "henries"), "mH": ("millihenry", "millihenries"), "µH": ("microhenry", "microhenries"),
    "uH": ("microhenry", "microhenries"),
    "Hz": ("hertz", "hertz"), "kHz": ("kilohertz", "kilohertz"), "MHz": ("megahertz", "megahertz"),
    "GHz": ("gigahertz", "gigahertz"),
    "°C": ("degree Celsius", "degrees Celsius"), "°F": ("degree Fahrenheit", "degrees Fahrenheit"),
    "°": ("degree", "degrees"), "%": ("percent", "percent"),
    "mm": ("millimetre", "millimetres"), "cm": ("centimetre", "centimetres"), "m": ("metre", "metres"),
    "km": ("kilometre", "kilometres"), "km/h": ("kilometre per hour", "kilometres per hour"),
    "mph": ("mile per hour", "miles per hour"), "kg": ("kilogram", "kilograms"), "g": ("gram", "grams"),
    "mg": ("milligram", "milligrams"), "lb": ("pound", "pounds"), "lbs": ("pounds", "pounds"),
    "oz": ("ounce", "ounces"), "ft": ("foot", "feet"), "in": ("inch", "inches"), "mi": ("mile", "miles"),
    "ml": ("millilitre", "millilitres"), "mL": ("millilitre", "millilitres"), "L": ("litre", "litres"),
    "ms": ("millisecond", "milliseconds"), "s": ("second", "seconds"), "min": ("minute", "minutes"),
    "h": ("hour", "hours"), "hr": ("hour", "hours"), "hrs": ("hours", "hours"),
    "GB": ("gigabyte", "gigabytes"), "MB": ("megabyte", "megabytes"), "KB": ("kilobyte", "kilobytes"),
    "kB": ("kilobyte", "kilobytes"), "TB": ("terabyte", "terabytes"), "Mbps": ("megabit per second", "megabits per second"),
    "fps": ("frame per second", "frames per second"), "rpm": ("RPM", "RPM"), "AWG": ("gauge", "gauge"),
}
# units that are also ordinary words or letters only count when they're stuck to the number ("5m", "10s", "3in")
_ATTACHED_ONLY = {"m", "s", "in", "h", "g", "A", "F", "H", "L", "k", "mi", "min"}
_UNIT_RX = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)(\s?)(" + "|".join(
    sorted((re.escape(u) for u in _UNITS), key=len, reverse=True)) + r")(?![\w°])")
_MONEY_RX = re.compile(r"([$€£])\s?(\d[\d,]*)(?:\.(\d\d))?(\s?(?:k|K|million|billion)\b)?(?!\d|\.\d)")
_TIME_RX = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?:\s?([AaPp])\.?\s?[Mm]\.?)?(?!\d)")
_RANGE_RX = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?[-–]\s?(\d+(?:\.\d+)?)(?![\w.])")
_ABBREV = [(re.compile(p, re.I), r) for p, r in [
    (r"\be\.g\.", "for example"), (r"\bi\.e\.", "that is"), (r"\betc\.", "et cetera"), (r"\bvs\.?(?=\s)", "versus"),
    (r"\bapprox\.", "approximately"), (r"\bw/o\b", "without"), (r"\bw/(?=\s)", "with"),
]]
_SYMBOLS = [(re.compile(p), r) for p, r in [
    (r"(?<=\d)\s?[x×]\s?(?=\d)", " by "), (r"\s&\s", " and "), (r"(?<=\s)\+(?=\s)", " plus "),
    (r"(?<=\s)=(?=\s)", " equals "), (r"~(?=\s?\d)", "about "), (r"≈", " about "), (r"±", " plus or minus "),
    (r"(?<=\s)/(?=\s)", " or "), (r"->|→", " to "), (r"\s[-–—]\s", ", "), (r"…", "..."),
]]
_EMOJI_RX = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]+")


def _unit_say(m):
    num, gap, unit = m.group(1), m.group(2), m.group(3)
    if gap and unit in _ATTACHED_ONLY:
        return m.group(0)
    one, many = _UNITS[unit]
    if unit == "k":                       # 10k (resistor) -> ten k
        return say_number(num) + " k"
    plural = num.replace(",", "") not in ("1", "1.0", "-1")
    return say_number(num) + " " + (many if plural else one)


def _money_say(m):
    sym, whole, cents, scale = m.groups()
    name = {"$": "dollars", "€": "euros", "£": "pounds"}[sym]
    out = say_number(whole)
    if scale:
        sc = scale.strip().lower()
        out += " thousand" if sc == "k" else " " + sc
    elif whole.replace(",", "") == "1":
        name = name[:-1]
    out += " " + name
    if cents and cents != "00":
        out += " and " + number_words(int(cents)) + " cents"
    return out


def _time_say(m):
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    words = number_words(h) + ("" if mi == 0 and ap else " o'clock" if mi == 0 else
                               " oh " + number_words(mi) if mi < 10 else " " + number_words(mi))
    return words + (" " + ("AM" if ap.lower() == "a" else "PM") if ap else "")


def speech_text(text):
    """Turn written text into what a person would say: units, money, times, ranges, abbreviations, symbols."""
    for rx, rep in _ABBREV:
        text = rx.sub(rep, text)
    text = _MONEY_RX.sub(_money_say, text)
    text = _TIME_RX.sub(_time_say, text)
    text = _RANGE_RX.sub(lambda m: f"{m.group(1)} to {m.group(2)}", text)
    for rx, rep in _SYMBOLS:
        text = rx.sub(rep, text)
    text = _UNIT_RX.sub(_unit_say, text)
    text = _EMOJI_RX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def speakable(text):
    """What to say out loud for a reply: no code blocks, markdown symbols or long links; units and
    symbols as words."""
    for rx, rep in SPEAKABLE_DROP:
        text = rx.sub(rep, text)
    return speech_text(text)[:1200]


class PiperTTS:
    """A Piper process kept running (the voice stays loaded): one JSON line in, one WAV file out."""

    def __init__(self):
        self.proc, self.voice, self.speed, self.lock, self.lines = None, "", 1.0, threading.Lock(), None

    def _start(self, voice, speed):
        import queue
        self.stop()
        exe = piper_exe()
        model = _avatar_path("voices", voice + ".onnx")
        if not exe or not os.path.isfile(model):
            raise RuntimeError("the voice isn't downloaded yet")
        env = dict(os.environ)
        if os.name != "nt":
            env["LD_LIBRARY_PATH"] = os.path.dirname(exe) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        self.proc = subprocess.Popen(
            [exe, "--model", model, "--json-input", "--output_dir", _avatar_path("tts"),
             "--length_scale", str(round(1.0 / max(0.5, speed), 3)), "--sentence_silence", "0.15", "--quiet"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=os.path.dirname(exe),
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.voice, self.speed = voice, speed
        lines = self.lines = queue.Queue()
        out = self.proc.stdout

        def reader():   # Piper prints each finished file's name; read it without blocking the caller forever
            for raw in iter(out.readline, b""):
                lines.put(raw.decode("utf-8", "replace").strip())
            lines.put(None)
        threading.Thread(target=reader, daemon=True).start()

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def say(self, text, voice, speed=1.0):
        import queue
        text = speakable(text)
        if not text:
            return b""
        out_dir = _avatar_path("tts")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, uuid.uuid4().hex + ".wav")
        with self.lock:
            for attempt in range(2):
                if not self.proc or self.proc.poll() is not None or self.voice != voice or self.speed != speed:
                    self._start(voice, speed)
                try:
                    self.proc.stdin.write((json.dumps({"text": text, "output_file": out}) + "\n").encode("utf-8"))
                    self.proc.stdin.flush()
                    deadline, got = time.time() + 60, False
                    while time.time() < deadline:
                        line = self.lines.get(timeout=max(0.1, deadline - time.time()))
                        if line is None:
                            break
                        if os.path.basename(out) in line:
                            got = True
                            break
                    if got and os.path.isfile(out):
                        break
                except (OSError, ValueError, queue.Empty):
                    pass
                self.stop()
            else:
                raise RuntimeError("the speech engine stopped")
        try:
            with open(out, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(out)
            except OSError:
                pass


TTS = PiperTTS()

# ---- Kokoro: a more natural-sounding voice (82M-parameter model, runs on the CPU; optional)
KOKORO_VOICES = {
    "af_heart": "Heart: warm, natural (US)", "af_bella": "Bella: bright, expressive (US)",
    "af_nicole": "Nicole: soft, close (US)", "af_sarah": "Sarah: clear, friendly (US)", "af_sky": "Sky: light, young (US)",
    "bf_emma": "Emma: calm (UK)", "bf_isabella": "Isabella: polished (UK)",
}
KOKORO_FILES = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


KOKORO_WORKER = r'''# StellAI Kokoro worker: keeps the voice model loaded. One JSON line in, one WAV file out (its path printed).
import json, sys, wave
import numpy as np
from kokoro_onnx import Kokoro

model = Kokoro(sys.argv[1], sys.argv[2])
print("ready", flush=True)
for line in sys.stdin:
    try:
        req = json.loads(line)
        samples, rate = model.create(req["text"], voice=req["voice"], speed=float(req.get("speed", 1.0)),
                                     lang=req.get("lang", "en-us"))
        with wave.open(req["out"], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(rate))
            w.writeframes((np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes())
        print(req["out"], flush=True)
    except Exception as e:  # noqa: BLE001 - report and keep serving
        print("error " + str(e).replace("\n", " "), flush=True)
'''
# Windows: the StellAI app is a packaged program without its own Python, so Kokoro gets a small private copy of
# the official Python (the "embeddable" build from python.org) next to the voice files.
WIN_PYTHON_URL = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


class KokoroTTS:
    """Kokoro runs in its own process (a worker), with the Python that has kokoro-onnx installed:
    the hub's own on Linux, a private embedded Python on Windows."""

    def __init__(self):
        self.proc, self.lines, self.lock, self.preparing = None, None, threading.Lock(), False

    def _frozen(self):
        return bool(getattr(sys, "frozen", False))

    def available(self):
        import platform
        if self._frozen():
            return os.name == "nt" and platform.machine().lower() in ("amd64", "x86_64")
        return True

    def python(self):
        if self._frozen():
            return _avatar_path("kokoro", "python", "python.exe")
        return sys.executable

    def _importable(self):
        py = self.python()
        if not os.path.isfile(py):
            return False
        try:
            return subprocess.run([py, "-c", "import kokoro_onnx, numpy"], capture_output=True, timeout=120,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def ready(self):
        if self.proc and self.proc.poll() is None:
            return True
        return AVATAR["status"].get("kokoro") == "ready" or (
            all(os.path.isfile(_avatar_path("kokoro", f)) for f in KOKORO_FILES) and os.path.isfile(self.python())
            and os.path.isfile(_avatar_path("kokoro", ".installed")))

    def _setup_windows_python(self):
        d = _avatar_path("kokoro", "python")
        if os.path.isfile(os.path.join(d, "python.exe")):
            return
        AVATAR["status"]["kokoro"] = "downloading Python for the voice engine"
        os.makedirs(d, exist_ok=True)
        z = _avatar_path("kokoro", "python-embed.zip")
        _download_file(WIN_PYTHON_URL, z, {})
        target = os.path.realpath(d)
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if not os.path.realpath(os.path.join(target, n)).startswith(target + os.sep):
                    raise OSError("unsafe file in the Python download")
            zf.extractall(target)
        os.remove(z)
        # The embeddable Python ignores installed packages until "import site" is switched on
        for f in os.listdir(d):
            if f.endswith("._pth"):
                p = os.path.join(d, f)
                with open(p) as fh:
                    txt = fh.read()
                txt = txt.replace("#import site", "import site")
                if "Lib\\site-packages" not in txt:
                    txt += "\nLib\\site-packages\n"
                with open(p, "w") as fh:
                    fh.write(txt)
        AVATAR["status"]["kokoro"] = "installing pip"
        gp = os.path.join(d, "get-pip.py")
        _download_file(GET_PIP_URL, gp, {})
        r = subprocess.run([os.path.join(d, "python.exe"), gp, "--no-warn-script-location", "--quiet"],
                           capture_output=True, text=True, timeout=900, cwd=d,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise OSError("installing pip failed: " + (r.stderr or r.stdout).strip()[-300:])

    def prepare(self):
        """Sets up Kokoro once, in the background: (Windows: a private Python), the kokoro-onnx package
        (~60 MB with onnxruntime) and the model (~115 MB)."""
        if self.preparing or not self.available() or (self.ready() and self._importable()):
            if self.ready() and not self.preparing:
                AVATAR["status"]["kokoro"] = "ready"
            return
        self.preparing = True

        def work():
            try:
                os.makedirs(_avatar_path("kokoro"), exist_ok=True)
                if self._frozen():
                    self._setup_windows_python()
                if not self._importable():
                    AVATAR["status"]["kokoro"] = "installing the voice engine"
                    r = subprocess.run([self.python(), "-m", "pip", "install", "--quiet", "--no-warn-script-location",
                                        "kokoro-onnx>=0.4"], capture_output=True, text=True, timeout=1800,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if r.returncode != 0 or not self._importable():
                        raise OSError("installing kokoro-onnx failed: " + (r.stderr or r.stdout).strip()[-300:])
                for name, url in KOKORO_FILES.items():
                    if not os.path.isfile(_avatar_path("kokoro", name)):
                        AVATAR["status"]["kokoro"] = f"downloading {name}"
                        _download_file(url, _avatar_path("kokoro", name), {})
                with open(_avatar_path("kokoro", "worker.py"), "w") as f:
                    f.write(KOKORO_WORKER)
                open(_avatar_path("kokoro", ".installed"), "w").close()
                AVATAR["status"]["kokoro"] = "ready"
                try:
                    self.say("Hello.", "af_heart")          # load it now so the first reply is quick
                except Exception:   # noqa: BLE001 - warming up is optional
                    pass
            except (OSError, subprocess.SubprocessError, urllib.error.URLError, zipfile.BadZipFile) as e:
                AVATAR["status"]["kokoro"] = f"failed: {e}"
                log(f"kokoro: {e}")
            finally:
                self.preparing = False
        threading.Thread(target=work, daemon=True).start()

    def _start(self):
        import queue
        self.stop()
        with open(_avatar_path("kokoro", "worker.py"), "w") as f:     # always the version that matches this hub
            f.write(KOKORO_WORKER)
        self.proc = subprocess.Popen(
            [self.python(), _avatar_path("kokoro", "worker.py"), _avatar_path("kokoro", "kokoro-v1.0.int8.onnx"),
             _avatar_path("kokoro", "voices-v1.0.bin")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=_avatar_path("kokoro"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = self.lines = queue.Queue()
        out = self.proc.stdout

        def reader():
            for raw in iter(out.readline, b""):
                lines.put(raw.decode("utf-8", "replace").strip())
            lines.put(None)
        threading.Thread(target=reader, daemon=True).start()
        first = lines.get(timeout=180)          # loading the model takes a few seconds
        if first != "ready":
            self.stop()
            raise RuntimeError("the Kokoro voice didn't start")

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def say(self, text, voice, speed=1.0):
        import queue
        text = speakable(text)
        if not text:
            return b""
        out = _avatar_path("tts", uuid.uuid4().hex + ".wav")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                self._start()
            req = {"text": text, "voice": voice if voice in KOKORO_VOICES else "af_heart",
                   "speed": max(0.6, min(1.6, speed)), "lang": "en-gb" if voice.startswith("b") else "en-us", "out": out}
            self.proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            try:
                line = self.lines.get(timeout=60)
            except queue.Empty:
                self.stop()
                raise RuntimeError("the Kokoro voice took too long")
            if line is None or line.startswith("error"):
                if line is None:
                    self.stop()
                raise RuntimeError("Kokoro: " + (line or "stopped"))
        try:
            with open(out, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(out)
            except OSError:
                pass


KOKORO = KokoroTTS()



def _tts_speakable_ok():
    return bool(piper_exe()) and os.path.isfile(_avatar_path("voices", avatar_config()["voice"] + ".onnx"))


class PhraseCache:
    """Short spoken phrases ("Okay.", "Reminder set.") are kept as audio, so repeats don't run the voice engine."""
    MAX_CHARS, MAX_ITEMS, MAX_BYTES = 90, 150, 24_000_000

    def __init__(self):
        import collections
        self.items, self.size, self.lock = collections.OrderedDict(), 0, threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self.lock:
            wav = self.items.get(key)
            if wav is None:
                self.misses += 1
                return None
            self.items.move_to_end(key)
            self.hits += 1
            return wav

    def put(self, key, wav):
        if not wav or len(wav) > 2_000_000:
            return
        with self.lock:
            if key in self.items:
                self.size -= len(self.items.pop(key))
            self.items[key] = wav
            self.size += len(wav)
            while len(self.items) > self.MAX_ITEMS or self.size > self.MAX_BYTES:
                self.size -= len(self.items.popitem(last=False)[1])


PHRASES = PhraseCache()


class AvatarBus:
    """What Stella is doing on any device (listening, thinking, replying), for avatar screens to show.
    Screens poll /avatar/events?since=N about once a second."""

    def __init__(self):
        self.events, self.seq, self.lock = [], 0, threading.Lock()

    def post(self, ev):
        state = str(ev.get("state", ""))
        if state not in ("idle", "listening", "thinking", "talking", "reply"):
            raise ValueError("unknown state")
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "ts": time.time(), "state": state,
                                "source": str(ev.get("source", ""))[:64],
                                "device": str(ev.get("device", ""))[:40],
                                "text": str(ev.get("text", ""))[:4000],
                                "voice": bool(ev.get("voice"))})
            self.events = self.events[-50:]
            return self.seq

    def since(self, seq):
        with self.lock:
            return {"seq": self.seq, "now": time.time(), "events": [e for e in self.events if e["seq"] > seq and time.time() - e["ts"] < 120]}


AVATAR_BUS = AvatarBus()

WAKE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def custom_wakewords():
    if not WAKEWORD_DIR:
        return []
    try:
        return sorted(f[:-5] for f in os.listdir(WAKEWORD_DIR) if f.endswith(".onnx") and WAKE_NAME.fullmatch(f[:-5])
                      and ".." not in f)
    except (OSError, NameError):
        return []


