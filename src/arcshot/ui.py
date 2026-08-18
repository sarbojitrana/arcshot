"""The little chooser that appears when you hit PrtScrn."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from . import capture, config  # noqa: E402

APP_ID = "dev.arcshot.Arcshot"

CSS = """
.arc-root { background-color: #141d26; }
.arc-title { font-size: 15px; font-weight: 700; color: #00d9ff; letter-spacing: 1px; }
.arc-sub   { font-size: 11px; color: #86a5b3; }
.arc-group button {
  background-image: none; background-color: #1a242f; color: #b9d2dd;
  border: 1px solid #2e3f4c; border-radius: 9px; padding: 9px 6px;
  font-weight: 600; box-shadow: none;
}
.arc-group button:hover { background-color: #22303c; color: #d6f0f8; }
.arc-group button:checked {
  background-color: #00d9ff; color: #05202a; border-color: #00d9ff;
}
/* Fighting libadwaita's own `button` background with a custom class does not
   work - it repaints via background-image. Use its accent machinery instead:
   the button carries .suggested-action and we just redefine the accent. */
@define-color accent_bg_color #00d9ff;
@define-color accent_fg_color #05202a;
@define-color accent_color    #00d9ff;

button.arc-go { font-weight: 700; border-radius: 10px; padding: 11px 8px; }
.arc-hint { font-size: 10px; color: #6b8b99; }
.arc-rec { color: #ff453a; font-weight: 700; }
"""


def _seg(labels, active, on_change):
    """A segmented row of linked toggle buttons."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                  homogeneous=True)
    box.add_css_class("arc-group")
    first = None
    for key, label in labels:
        btn = Gtk.ToggleButton(label=label)
        if first is None:
            first = btn
        else:
            btn.set_group(first)
        if key == active:
            btn.set_active(True)
        btn.connect("toggled", lambda b, k=key: b.get_active() and on_change(k))
        box.append(btn)
    return box


class Window(Adw.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app, title="arcshot")
        self.cfg = cfg
        self.set_default_size(380, -1)
        self.set_resizable(False)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.add_css_class("arc-root")
        root.set_margin_top(18); root.set_margin_bottom(18)
        root.set_margin_start(18); root.set_margin_end(18)

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        t = Gtk.Label(label="◉  ARCSHOT", xalign=0); t.add_css_class("arc-title")
        s = Gtk.Label(label="capture", xalign=0); s.add_css_class("arc-sub")
        self.sub = s
        head.append(t); head.append(s)
        root.append(head)

        root.append(_seg([("image", " Image"), ("video", " Video")],
                         cfg["mode"], self._set_mode))
        root.append(_seg([("screen", "Screen"), ("region", "Region"),
                          ("window", "Window")], cfg["area"], self._set_area))

        # audio row - only meaningful while recording
        self.audio_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        al = Gtk.Label(label="Audio", xalign=0); al.add_css_class("arc-sub")
        al.set_hexpand(True)
        self.audio_sw = Gtk.Switch(active=cfg["audio"], valign=Gtk.Align.CENTER)
        self.audio_sw.connect("notify::active", self._set_audio)
        self.audio_src = Gtk.DropDown.new_from_strings(["System", "Microphone"])
        self.audio_src.set_selected(0 if cfg.get("audio_source") == "system" else 1)
        self.audio_src.connect("notify::selected", self._set_audio_src)
        self.audio_box.append(al)
        self.audio_box.append(self.audio_src)
        self.audio_box.append(self.audio_sw)
        root.append(self.audio_box)

        self.go = Gtk.Button(label="Capture")
        self.go.add_css_class("suggested-action")
        self.go.add_css_class("arc-go")
        self.go.connect("clicked", lambda *_: self._go())
        root.append(self.go)

        hint = Gtk.Label(label="Enter capture     Esc cancel     choices are remembered")
        hint.add_css_class("arc-hint")
        root.append(hint)

        self.set_content(root)

        esc = Gtk.EventControllerKey()
        esc.connect("key-pressed", self._key)
        self.add_controller(esc)

        self._sync()

    # ----------------------------------------------------------- state
    def _set_mode(self, k): self.cfg["mode"] = k; self._sync()
    def _set_area(self, k): self.cfg["area"] = k; self._sync()
    def _set_audio(self, sw, _): self.cfg["audio"] = sw.get_active()
    def _set_audio_src(self, dd, _):
        self.cfg["audio_source"] = "system" if dd.get_selected() == 0 else "mic"

    def _sync(self):
        video = self.cfg["mode"] == "video"
        self.audio_box.set_visible(video)
        self.go.set_label("Start recording" if video else "Capture")
        self.sub.set_label("screen recording" if video else "screenshot")

    def _key(self, _c, keyval, *_a):
        if keyval == Gdk.KEY_Escape:
            self.close(); return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._go(); return True
        return False

    # ----------------------------------------------------------- action
    def _go(self):
        config.save(self.cfg)
        self.set_visible(False)
        # let the compositor actually drop the window before we grab anything
        GLib.timeout_add(180, self._run)

    def _run(self):
        cfg = self.cfg
        try:
            geom, cancelled = capture.resolve_geometry(cfg["area"])
            if cancelled:
                self.get_application().quit()
                return False

            if cfg["mode"] == "image":
                path = capture.take_screenshot(cfg, geom)
                capture.notify("Screenshot saved",
                               f"{path}\ncopied to clipboard" if cfg["copy_to_clipboard"]
                               else path)
            else:
                path = capture.start_recording(cfg, geom)
                capture.notify("Recording started",
                               "Press your capture key again to stop.\n" + path,
                               icon="media-record")
        except capture.CaptureError as exc:
            capture.notify("arcshot failed", str(exc),
                           icon="dialog-error", urgent=True)
        self.get_application().quit()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        Window(self, config.load()).present()
