"""Persisted choices. The point of the app is that it remembers."""
import json
import os

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "arcshot")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "mode": "image",        # image | video
    "area": "region",       # screen | region | window
    "mic": False,           # record the microphone
    "system_audio": False,  # record what the speakers are playing
    "timer": 0,             # seconds of countdown before capturing
    "cursor": False,            # include the pointer (screenshots only)
    "copy_to_clipboard": True,
    "open_editor": False,
    "image_dir": "",        # blank -> XDG Pictures/Screenshots
    "video_dir": "",        # blank -> XDG Videos/Recordings
}


def _xdg_dir(kind, fallback):
    """Resolve an XDG user dir without depending on xdg-user-dir being present."""
    cfg = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "user-dirs.dirs")
    key = f"XDG_{kind}_DIR"
    try:
        with open(cfg) as fh:
            for line in fh:
                if line.startswith(key):
                    val = line.split("=", 1)[1].strip().strip('"')
                    return os.path.expandvars(val.replace("$HOME", os.path.expanduser("~")))
    except OSError:
        pass
    return os.path.expanduser(fallback)


def load():
    data = dict(DEFAULTS)
    stored = {}
    try:
        with open(CONFIG_FILE) as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        pass
    data.update(stored)
    # Mic and system sound used to be one switch plus an either/or dropdown.
    # Carry that choice over rather than silently resetting someone's audio.
    if "audio" in stored and "mic" not in stored:
        src = stored.get("audio_source", "system")
        data["mic"] = bool(stored["audio"]) and src == "mic"
        data["system_audio"] = bool(stored["audio"]) and src != "mic"
    if not data["image_dir"]:
        data["image_dir"] = os.path.join(_xdg_dir("PICTURES", "~/Pictures"), "Screenshots")
    if not data["video_dir"]:
        data["video_dir"] = os.path.join(_xdg_dir("VIDEOS", "~/Videos"), "Recordings")
    return data


def save(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    persist = {k: v for k, v in data.items() if k in DEFAULTS}
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(persist, fh, indent=2)
    os.replace(tmp, CONFIG_FILE)
