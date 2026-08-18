"""arcshot entry point.

The key insight for the keybinding: if a recording is already running, the
same key that opened this menu should STOP it rather than opening the menu
again. That is handled here, before any GTK is touched, so stopping a
recording never has to wait for a UI to load.
"""
import sys

from . import capture, config


def _usage():
    print(
        "arcshot - screenshot and screen recording for wlroots compositors\n"
        "\n"
        "  arcshot              open the chooser (or stop an active recording)\n"
        "  arcshot --stop       stop an active recording\n"
        "  arcshot --toggle     stop if recording, otherwise open the chooser\n"
        "  arcshot --shot AREA  screenshot now: screen | region | window\n"
        "  arcshot --rec AREA   record now:     screen | region | window\n"
        "  arcshot --status     print 'recording' or 'idle'\n"
        "  arcshot --help\n"
    )


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

    # Default and --toggle: an active recording takes priority over the menu.
    if "--stop" not in argv and capture.is_recording():
        path = capture.stop_recording()
        capture.notify("Recording saved", path or "", icon="media-record")
        return 0

    for flag, mode in (("--shot", "image"), ("--rec", "video")):
        if flag in argv:
            i = argv.index(flag)
            area = argv[i + 1] if len(argv) > i + 1 else "region"
            if area not in ("screen", "region", "window"):
                print(f"unknown area: {area}", file=sys.stderr)
                return 2
            cfg = config.load()
            cfg["mode"], cfg["area"] = mode, area
            try:
                geom, cancelled = capture.resolve_geometry(area)
                if cancelled:
                    return 0
                if mode == "image":
                    p = capture.take_screenshot(cfg, geom)
                    capture.notify("Screenshot saved", p)
                else:
                    p = capture.start_recording(cfg, geom)
                    capture.notify("Recording started",
                                   "Run 'arcshot --stop' to finish.\n" + p,
                                   icon="media-record")
            except capture.CaptureError as exc:
                capture.notify("arcshot failed", str(exc),
                               icon="dialog-error", urgent=True)
                return 1
            return 0

    from .ui import App          # imported late so --stop needs no GTK
    return App().run([])


if __name__ == "__main__":
    sys.exit(main())
