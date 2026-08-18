"""arcshot entry point.

Two things happen here before any GTK is imported, and both matter:

  * If a recording is running, the capture key stops it. That check is first,
    so stopping never waits for a UI to load.
  * A bare `arcshot` captures immediately with the remembered settings. The
    key should start a region drag, not present a form. The chooser is opt-in
    via --menu.
"""
import ctypes.util
import os
import sys

from . import capture, config

_LAYER_SHELL_GUARD = "ARCSHOT_LAYER_SHELL_PRELOADED"


def _ensure_layer_shell():
    """Re-exec with LD_PRELOAD so gtk4-layer-shell loads before libwayland.

    gtk4-layer-shell has to be linked ahead of libwayland-client or it cannot
    hook surface creation, and the toolbar silently comes up as an ordinary
    window -- which is invisible underneath slurp. Python has no say in link
    order, so the documented workaround is LD_PRELOAD. Only the UI path pays
    for this; --stop and the direct capture flags never get here.
    """
    if os.environ.get(_LAYER_SHELL_GUARD):
        return
    lib = ctypes.util.find_library("gtk4-layer-shell")
    if lib and not os.path.isabs(lib):
        for d in ("/usr/lib", "/usr/lib64", "/usr/local/lib"):
            cand = os.path.join(d, lib)
            if os.path.exists(cand):
                lib = cand
                break
    if not lib or not os.path.exists(lib):
        return                       # let GTK warn; a plain window still works
    pre = os.environ.get("LD_PRELOAD", "")
    if lib in pre.split(":"):
        return
    env = dict(os.environ)
    env["LD_PRELOAD"] = f"{lib}:{pre}" if pre else lib
    env[_LAYER_SHELL_GUARD] = "1"
    os.execve(sys.executable, [sys.executable, "-m", "arcshot"] + sys.argv[1:], env)

AREAS = ("screen", "region", "window")


def _usage():
    print(
        "arcshot - screenshot and screen recording for wlroots compositors\n"
        "\n"
        "  arcshot              open the capture overlay with the selection\n"
        "                       already armed, or stop an active recording\n"
        "  arcshot --stop       stop an active recording\n"
        "  arcshot --shot AREA  screenshot now: screen | region | window\n"
        "  arcshot --rec AREA   record now:     screen | region | window\n"
        "  arcshot --status     print 'recording' or 'idle'\n"
        "  arcshot --help\n"
    )


def _capture_now(cfg, mode, area):
    """Run the capture with no UI at all."""
    cfg = dict(cfg)
    cfg["mode"], cfg["area"] = mode, area
    try:
        geom, cancelled = capture.resolve_geometry(area)
        if cancelled:
            return 0
        if mode == "image":
            path = capture.take_screenshot(cfg, geom)
            capture.notify("Screenshot saved", path)
        else:
            path = capture.start_recording(cfg, geom)
            capture.notify("Recording started",
                           "Press your capture key again to stop.\n" + path,
                           icon="media-record")
    except capture.CaptureError as exc:
        capture.notify("arcshot failed", str(exc),
                       icon="dialog-error", urgent=True)
        return 1
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--help" in argv or "-h" in argv:
        _usage()
        return 0

    if "--status" in argv:
        print("recording" if capture.is_recording() else "idle")
        return 0

    if "--stop" in argv:
        path = capture.stop_recording()
        if path:
            capture.notify("Recording saved", path, icon="media-record")
            return 0
        capture.notify("Nothing to stop", "No recording is running.")
        return 1

    # An active recording always wins: the same key that started it stops it.
    if capture.is_recording():
        path = capture.stop_recording()
        capture.notify("Recording saved", path or "", icon="media-record")
        return 0

    for flag, mode in (("--shot", "image"), ("--rec", "video")):
        if flag in argv:
            i = argv.index(flag)
            area = argv[i + 1] if len(argv) > i + 1 else "region"
            if area not in AREAS:
                print(f"unknown area: {area}", file=sys.stderr)
                return 2
            return _capture_now(config.load(), mode, area)

    # Bare invocation opens the overlay -- which arms the selection straight
    # away, so the key press lands you in a live region drag with the toolbar
    # available if you want a different mode. --menu is kept as an alias.
    if not argv or "--menu" in argv:
        _ensure_layer_shell()   # never returns if it re-execs
        from .ui import App     # imported late so nothing else pays for GTK
        return App().run([])

    print(f"unknown option: {argv[0]}", file=sys.stderr)
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
