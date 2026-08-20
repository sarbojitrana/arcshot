"""Screenshot and screen-recording backends (grim / slurp / wf-recorder).

The recording side is a small state machine rather than one process, because
wf-recorder cannot pause and cannot be re-pointed at a different audio device
once it is running:

  * Pause stops the recorder and resume starts another one. Each run is a
    segment; `stop` concatenates them, so a pause costs no time in the file.
  * Audio is always recorded from one null sink that we own. Turning the
    microphone or the system sound on and off loads and unloads a loopback
    into that sink, which wf-recorder never notices -- the device it was
    given keeps producing samples either way.

State lives in METAFILE so `--stop` and `--pause` work from any process.
"""
import datetime
import json
import os
import shutil
import signal
import subprocess
import time

RUNTIME = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
PIDFILE = os.path.join(RUNTIME, "arcshot-recording.pid")
METAFILE = os.path.join(RUNTIME, "arcshot-recording.json")
MIX_SINK = "arcshot_mix"
SOURCES = ("mic", "system")


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
    """CLI path only: let the user click a window via `slurp -r`."""
    _require("slurp")
    rects = window_rects()
    if not rects:
        return _slurp_region()
    data = "\n".join(f"{x},{y} {w}x{h}" for x, y, w, h in rects)
    p = subprocess.run(["slurp", "-r"], input=data,
                       capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return p.stdout.strip()


def _hypr(*args):
    if not shutil.which("hyprctl"):
        return None
    try:
        return json.loads(subprocess.run(["hyprctl", "-j", *args],
                                         capture_output=True, text=True,
                                         check=True).stdout)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def window_rects():
    """(x, y, w, h) for every visible window on the active workspace.

    Ordered back-to-front, so when several contain the pointer the LAST match
    is the topmost one.
    """
    clients = _hypr("clients")
    active = _hypr("activeworkspace")
    if clients is None or active is None:
        return None
    ws = active.get("id")
    rects = []
    for c in clients:
        if c.get("hidden") or not c.get("mapped", True):
            continue
        if c.get("workspace", {}).get("id") != ws:
            continue
        x, y = c.get("at", [0, 0])
        w, h = c.get("size", [0, 0])
        if w > 0 and h > 0:
            rects.append((x, y, w, h))
    return rects or None


def cursor_pos():
    """Where the pointer is now, in compositor coordinates."""
    pos = _hypr("cursorpos")
    if not pos:
        return None
    try:
        return int(pos["x"]), int(pos["y"])
    except (KeyError, TypeError, ValueError):
        return None


def window_at(x, y):
    """Topmost window containing the point."""
    hit = None
    for rx, ry, rw, rh in window_rects() or ():
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            hit = (rx, ry, rw, rh)       # last match == topmost
    return hit


def active_window_rect():
    """The focused window -- the fallback when the pointer is over our own bar."""
    win = _hypr("activewindow")
    if not win:
        return None
    x, y = win.get("at", [0, 0])
    w, h = win.get("size", [0, 0])
    return (x, y, w, h) if w > 0 and h > 0 else None


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


# ------------------------------------------------------------- audio mixing
def _pactl(*args):
    return subprocess.run(["pactl", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _source_name(kind):
    """The PulseAudio/PipeWire source feeding one side of the mix."""
    try:
        if kind == "mic":
            return _pactl("get-default-source") or None
        sink = _pactl("get-default-sink")
        return f"{sink}.monitor" if sink else None
    except (subprocess.CalledProcessError, OSError):
        return None


def _unload(module):
    try:
        _pactl("unload-module", str(module))
    except (subprocess.CalledProcessError, OSError):
        pass


def mix_start(mic, system):
    """A sink of our own for wf-recorder to record.

    Recording the mic *or* the speakers directly would be simpler, but then
    the choice is frozen for the whole recording and the two can never be
    mixed. Recording a sink we control means the toggles are just loopbacks
    coming and going underneath a device that never changes.
    """
    if not shutil.which("pactl"):
        return None
    try:
        sink = int(_pactl("load-module", "module-null-sink",
                          f"sink_name={MIX_SINK}",
                          f"sink_properties=device.description={MIX_SINK}"))
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None
    mix = {"sink": sink, "mic": None, "system": None}
    for kind, on in (("mic", mic), ("system", system)):
        if on:
            mix_set(mix, kind, True)
    return mix


def mix_set(mix, kind, on):
    """Load or unload one source's loopback. True if anything changed."""
    if not mix or kind not in SOURCES:
        return False
    if on and mix.get(kind) is None:
        src = _source_name(kind)
        if not src:
            return False
        try:
            mix[kind] = int(_pactl("load-module", "module-loopback",
                                   f"source={src}", f"sink={MIX_SINK}",
                                   "latency_msec=20"))
        except (subprocess.CalledProcessError, ValueError, OSError):
            return False
        return True
    if not on and mix.get(kind) is not None:
        _unload(mix[kind])
        mix[kind] = None
        return True
    return False


def mix_stop(mix):
    if not mix:
        return
    for key in SOURCES:                  # loopbacks first, then the sink
        if mix.get(key) is not None:
            _unload(mix[key])
    if mix.get("sink") is not None:
        _unload(mix["sink"])


# --------------------------------------------------------------- recording
def _read_state():
    try:
        with open(METAFILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_state(st):
    tmp = METAFILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, METAFILE)


_PROCS = {}     # pid -> Popen, for the recorders this process started


def _alive(pid):
    """Is this recorder still running?

    A finished child we started is a zombie until it is reaped, and a zombie
    still answers kill(0) -- believe that and pause and stop both sit out
    their whole timeout waiting for a process that is already gone.
    """
    proc = _PROCS.get(pid)
    if proc is not None:
        return proc.poll() is None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(") ", 1)[1].split(" ", 1)[0] != "Z"
    except (OSError, IndexError):
        return True


def _forget(paths=(PIDFILE, METAFILE)):
    for f in paths:
        try:
            os.remove(f)
        except OSError:
            pass


def session():
    """The recording in progress -- running or paused -- else None."""
    st = _read_state()
    if not st:
        _forget()
        return None
    if st.get("paused") or (st.get("pid") and _alive(st["pid"])):
        return st
    # wf-recorder is gone without us stopping it; leave the segments on disk
    # but let go of the sink, or it lingers in the mixer for the session.
    mix_stop(st.get("mix"))
    _forget()
    return None


def is_recording():
    return session() is not None


def is_paused():
    st = session()
    return bool(st and st.get("paused"))


def elapsed():
    """Seconds of recorded material, pauses excluded."""
    st = session()
    if not st:
        return 0.0
    secs = st.get("elapsed", 0.0)
    if not st.get("paused"):
        secs += max(0.0, time.time() - st.get("seg_started", time.time()))
    return secs


def audio_state():
    st = session()
    if not st:
        return {k: False for k in SOURCES}
    return {k: bool(st.get(k)) for k in SOURCES}


def _segment_path(st):
    return f"{os.path.splitext(st['path'])[0]}.part{len(st['segments']) + 1}.mp4"


def _spawn_segment(st):
    seg = _segment_path(st)
    cmd = ["wf-recorder", "-y", "-f", seg]
    if st.get("geometry"):
        cmd += ["-g", st["geometry"]]
    if st.get("mix"):
        cmd.append(f"--audio={MIX_SINK}.monitor")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    _PROCS[proc.pid] = proc
    st["segments"].append(seg)
    st["pid"] = proc.pid
    st["seg_started"] = time.time()
    with open(PIDFILE, "w") as fh:
        fh.write(str(proc.pid))


def _wait_gone(pid, timeout=3.0):
    proc = _PROCS.pop(pid, None)
    if proc is not None:
        try:
            proc.wait(timeout=timeout)      # reaps it, so no zombie is left
        except subprocess.TimeoutExpired:
            pass
        return
    deadline = time.time() + timeout
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.05)


def _end_segment(st):
    pid = st.pop("pid", None)
    # Clock the segment before the interrupt: finalising the container adds
    # no frames, so it must not add seconds to what the recording says either.
    st["elapsed"] = st.get("elapsed", 0.0) + max(
        0.0, time.time() - st.get("seg_started", time.time()))
    if pid and _alive(pid):
        # SIGINT, never SIGTERM: wf-recorder needs the interrupt to finalise
        # the MP4 container, and a truncated segment breaks the concat too.
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
        _wait_gone(pid)
    _forget((PIDFILE,))


def start_recording(cfg, geometry):
    # NOTE: wf-recorder has no cursor switch -- it always draws the pointer.
    # The cursor setting therefore applies to screenshots only, and the UI
    # hides it in video mode rather than pretending it does something.
    _require("wf-recorder")
    if is_recording():
        raise CaptureError("already recording")
    os.makedirs(cfg["video_dir"], exist_ok=True)
    path = os.path.join(cfg["video_dir"], f"arcshot_{_stamp()}.mp4")
    # The mix is built even with both sources off, so that turning one on
    # mid-recording has somewhere to go. wf-recorder then always has an audio
    # device, which also keeps every segment the same shape for the concat.
    st = {"path": path, "geometry": geometry, "started": _stamp(),
          "segments": [], "elapsed": 0.0, "paused": False,
          "mic": bool(cfg.get("mic")), "system": bool(cfg.get("system_audio"))}
    st["mix"] = mix_start(st["mic"], st["system"])
    _spawn_segment(st)
    _write_state(st)
    return path


def pause_recording():
    st = session()
    if not st or st.get("paused"):
        return False
    _end_segment(st)
    st["paused"] = True
    _write_state(st)
    return True


def resume_recording():
    st = session()
    if not st or not st.get("paused"):
        return False
    _spawn_segment(st)
    st["paused"] = False
    _write_state(st)
    return True


def toggle_pause():
    """Returns True if the recording is paused afterwards."""
    if is_paused():
        resume_recording()
        return False
    pause_recording()
    return is_paused()


def set_audio(kind, on):
    st = session()
    if not st:
        return False
    changed = mix_set(st.get("mix"), kind, on)
    st[kind] = bool(on)
    _write_state(st)
    return changed


def _join(st):
    """One file out of the segments. Pauses are gaps, not frozen video."""
    segs = [s for s in st["segments"]
            if os.path.exists(s) and os.path.getsize(s) > 0]
    if not segs:
        return None
    if len(segs) == 1:
        os.replace(segs[0], st["path"])
        return st["path"]
    if not shutil.which("ffmpeg"):
        return segs[0]                   # parts kept: better than nothing
    listing = st["path"] + ".txt"
    with open(listing, "w") as fh:
        for seg in segs:
            fh.write("file '%s'\n" % seg.replace("'", "'\\''"))
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "concat", "-safe", "0", "-i", listing,
                        "-c", "copy", st["path"]],
                       capture_output=True, text=True)
    _forget((listing,))
    if p.returncode != 0:
        return segs[0]
    _forget(segs)
    return st["path"]


def stop_recording():
    st = session()
    if not st:
        return None
    if not st.get("paused"):
        _end_segment(st)
    mix_stop(st.get("mix"))
    path = _join(st)
    _forget()
    return path
