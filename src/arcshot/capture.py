"""Screenshot and screen-recording backends (grim / slurp / wf-recorder)."""
import datetime
import json
import os
import shutil
import signal
import subprocess

RUNTIME = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
PIDFILE = os.path.join(RUNTIME, "arcshot-recording.pid")
METAFILE = os.path.join(RUNTIME, "arcshot-recording.json")


class CaptureError(Exception):
    pass


def _require(*bins):
    missing = [b for b in bins if not shutil.which(b)]
    if missing:
        raise CaptureError("missing: " + ", ".join(missing))


def _stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def notify(title, body="", icon="camera-photo", urgent=False):
    if not shutil.which("notify-send"):
        return
    cmd = ["notify-send", "-a", "arcshot", "-i", icon, title, body]
    if urgent:
        cmd[1:1] = ["-u", "critical"]
    subprocess.run(cmd, check=False)


# --------------------------------------------------------------- geometry
def _slurp_region():
    _require("slurp")
    p = subprocess.run(["slurp"], capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        return None                      # user pressed Esc - not an error
    return p.stdout.strip()


def _slurp_window():
    """Let the user click a window. slurp -r reads candidate rects on stdin."""
    _require("slurp", "hyprctl")
    try:
        raw = subprocess.run(["hyprctl", "-j", "clients"],
                             capture_output=True, text=True, check=True).stdout
        clients = json.loads(raw)
    except (subprocess.CalledProcessError, ValueError):
        return _slurp_region()

    try:
        ws = json.loads(subprocess.run(["hyprctl", "-j", "activeworkspace"],
                                       capture_output=True, text=True,
                                       check=True).stdout)["id"]
    except Exception:                                        # noqa: BLE001
        ws = None

    rects = []
    for c in clients:
        if c.get("hidden") or not c.get("mapped", True):
            continue
        if ws is not None and c.get("workspace", {}).get("id") != ws:
            continue
        x, y = c.get("at", [0, 0])
        w, h = c.get("size", [0, 0])
        if w > 0 and h > 0:
            rects.append(f"{x},{y} {w}x{h}")
    if not rects:
        return _slurp_region()

    p = subprocess.run(["slurp", "-r"], input="\n".join(rects),
                       capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return p.stdout.strip()


def _window_rects():
    """Rectangles for every visible window on the active workspace."""
    if not shutil.which("hyprctl"):
        return None
    try:
        clients = json.loads(subprocess.run(
            ["hyprctl", "-j", "clients"], capture_output=True,
            text=True, check=True).stdout)
        ws = json.loads(subprocess.run(
            ["hyprctl", "-j", "activeworkspace"], capture_output=True,
            text=True, check=True).stdout)["id"]
    except (subprocess.CalledProcessError, ValueError, KeyError):
        return None
    rects = []
    for c in clients:
        if c.get("hidden") or not c.get("mapped", True):
            continue
        if c.get("workspace", {}).get("id") != ws:
            continue
        x, y = c.get("at", [0, 0])
        w, h = c.get("size", [0, 0])
        if w > 0 and h > 0:
            rects.append(f"{x},{y} {w}x{h}")
    return "\n".join(rects) if rects else None


def slurp_argv(area):
    """(argv, stdin) for arming a selection without blocking the caller.

    Used by the overlay, which runs slurp asynchronously so the toolbar stays
    responsive while the screen is live.
    """
    _require("slurp")
    if area == "window":
        rects = _window_rects()
        if rects:
            return ["slurp", "-r"], rects
    return ["slurp"], None


def resolve_geometry(area):
    """Return (geometry_or_None, cancelled)."""
    if area == "screen":
        return None, False
    geom = _slurp_region() if area == "region" else _slurp_window()
    if geom is None:
        return None, True
    return geom, False


# -------------------------------------------------------------- screenshot
def take_screenshot(cfg, geometry):
    _require("grim")
    os.makedirs(cfg["image_dir"], exist_ok=True)
    path = os.path.join(cfg["image_dir"], f"arcshot_{_stamp()}.png")

    cmd = ["grim"]
    if cfg.get("cursor"):
        cmd.append("-c")
    if geometry:
        cmd += ["-g", geometry]
    cmd.append(path)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise CaptureError(p.stderr.strip() or "grim failed")

    if cfg.get("copy_to_clipboard") and shutil.which("wl-copy"):
        with open(path, "rb") as fh:
            subprocess.run(["wl-copy", "--type", "image/png"], stdin=fh, check=False)

    if cfg.get("open_editor") and shutil.which("swappy"):
        subprocess.Popen(["swappy", "-f", path], start_new_session=True)

    return path


# --------------------------------------------------------------- recording
def _audio_source(kind):
    """Resolve a PulseAudio/PipeWire source name for wf-recorder --audio."""
    if not shutil.which("pactl"):
        return None
    try:
        if kind == "mic":
            return subprocess.run(["pactl", "get-default-source"],
                                  capture_output=True, text=True,
                                  check=True).stdout.strip() or None
        sink = subprocess.run(["pactl", "get-default-sink"],
                              capture_output=True, text=True,
                              check=True).stdout.strip()
        return f"{sink}.monitor" if sink else None
    except subprocess.CalledProcessError:
        return None


def is_recording():
    try:
        with open(PIDFILE) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        for f in (PIDFILE, METAFILE):
            try:
                os.remove(f)
            except OSError:
                pass
        return None


def start_recording(cfg, geometry):
    # NOTE: wf-recorder has no cursor switch -- it always draws the pointer.
    # The cursor setting therefore applies to screenshots only, and the UI
    # hides it in video mode rather than pretending it does something.
    _require("wf-recorder")
    if is_recording():
        raise CaptureError("already recording")
    os.makedirs(cfg["video_dir"], exist_ok=True)
    path = os.path.join(cfg["video_dir"], f"arcshot_{_stamp()}.mp4")

    cmd = ["wf-recorder", "-f", path]
    if geometry:
        cmd += ["-g", geometry]
    if cfg.get("audio"):
        src = _audio_source(cfg.get("audio_source", "system"))
        cmd.append(f"--audio={src}" if src else "--audio")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    with open(PIDFILE, "w") as fh:
        fh.write(str(proc.pid))
    with open(METAFILE, "w") as fh:
        json.dump({"path": path, "started": _stamp(),
                   "audio": bool(cfg.get("audio"))}, fh)
    return path


def stop_recording():
    pid = is_recording()
    if not pid:
        return None
    path = None
    try:
        with open(METAFILE) as fh:
            path = json.load(fh).get("path")
    except (OSError, ValueError):
        pass
    # wf-recorder needs SIGINT to finalise the container; SIGTERM truncates it.
    try:
        os.kill(pid, signal.SIGINT)
    except OSError:
        pass
    for f in (PIDFILE, METAFILE):
        try:
            os.remove(f)
        except OSError:
            pass
    return path
