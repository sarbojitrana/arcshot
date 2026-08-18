"""arcshot entry point.

Two things happen here before any GTK is imported, and both matter:

  * If a recording is running, the capture key stops it. That check is first,
    so stopping never waits for a UI to load.
  * A bare `arcshot` captures immediately with the remembered settings. The
    key should start a region drag, not present a form. The chooser is opt-in
    via --menu.
"""
import sys

from . import capture, config

AREAS = ("screen", "region", "window")


def _usage():
    print(
        "arcshot - screenshot and screen recording for wlroots compositors\n"
        "\n"
        "  arcshot              capture now with your remembered settings,\n"
        "                       or stop an active recording\n"
        "  arcshot --menu       open the chooser to change mode/area/audio\n"
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

    if "--menu" in argv:
        from .ui import App      # imported late so nothing else pays for GTK
        return App().run([])

    if not argv:
        cfg = config.load()
        return _capture_now(cfg, cfg["mode"], cfg["area"])

    print(f"unknown option: {argv[0]}", file=sys.stderr)
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
