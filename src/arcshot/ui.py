"""The capture overlay.

The important behaviour: when this opens, the screen is ALREADY armed for a
region drag. You do not pick a mode and then press a shutter -- you just drag,
and that is the capture. The toolbar is there to change your mind.

That is possible because slurp draws on the wlroots `overlay` layer, so the
toolbar has to live on the overlay layer too or it would be painted under the
selection. Hence gtk4-layer-shell rather than an ordinary window: a normal
xdg-toplevel always sits below layer-shell surfaces and would be invisible and
unclickable while slurp was up.
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LS  # noqa: E402

from . import capture, config  # noqa: E402

APP_ID = "dev.arcshot.Arcshot"

CSS = """
@define-color accent_bg_color #00d9ff;
@define-color accent_fg_color #05202a;
@define-color accent_color    #00d9ff;

/* the surface sits above slurp's tint, so it needs its own opaque ground -
   a transparent GTK window lets the selection wash bleed through and the
   toolbar reads washed-out grey */
window { background-color: transparent; }
.arc-bar {
  background-color: #101820;
  border: 1px solid #3a5160;
  border-radius: 16px;
  padding: 14px;
  box-shadow: 0 6px 24px rgba(0,0,0,0.55);
}
.arc-group button {
  background-image: none; background-color: #1a242f; color: #b9d2dd;
  border: 1px solid #2e3f4c; border-radius: 10px; padding: 10px 14px;
  font-weight: 600; box-shadow: none;
}
.arc-group button:hover { background-color: #22303c; color: #d6f0f8; }
.arc-group button:checked {
  background-color: #00d9ff; color: #05202a; border-color: #00d9ff;
}
.arc-sub  { font-size: 11px; color: #86a5b3; }
.arc-hint { font-size: 10px; color: #6b8b99; }
.arc-title { font-size: 13px; font-weight: 700; color: #00d9ff; letter-spacing: 1px; }
"""


def _seg(items, active, on_change):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
    box.add_css_class("arc-group")
    first = None
    for key, label in items:
        btn = Gtk.ToggleButton(label=label)
        btn.set_can_focus(False)
        if first is None:
            first = btn
        else:
            btn.set_group(first)
        if key == active:
            btn.set_active(True)
        btn.connect("toggled", lambda b, k=key: b.get_active() and on_change(k))
        box.append(btn)
    return box


class Overlay(Gtk.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app)
        self.cfg = cfg
        self._slurp = None
        self._busy = False

        # layer-shell must be initialised before the window is realised
        LS.init_for_window(self)
        LS.set_layer(self, LS.Layer.OVERLAY)
        LS.set_namespace(self, "arcshot")
        LS.set_anchor(self, LS.Edge.BOTTOM, True)
        LS.set_margin(self, LS.Edge.BOTTOM, 90)
        # ON_DEMAND: we want Esc/Enter, but slurp must keep the pointer
        LS.set_keyboard_mode(self, LS.KeyboardMode.ON_DEMAND)

        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        bar.add_css_class("arc-bar")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label(label="◉ ARCSHOT", xalign=0)
        title.add_css_class("arc-title")
        title.set_hexpand(True)
        self.state = Gtk.Label(label="", xalign=1)
        self.state.add_css_class("arc-sub")
        head.append(title)
        head.append(self.state)
        bar.append(head)

        bar.append(_seg([("image", "  Image"), ("video", "  Video")],
                        cfg["mode"], self._set_mode))
        bar.append(_seg([("region", "Selection"), ("screen", "Screen"),
                         ("window", "Window")], cfg["area"], self._set_area))

        # contextual row: pointer for stills, audio for recordings
        self.opt = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.opt_label = Gtk.Label(label="", xalign=0)
        self.opt_label.add_css_class("arc-sub")
        self.opt_label.set_hexpand(True)
        self.audio_src = Gtk.DropDown.new_from_strings(["System", "Microphone"])
        self.audio_src.set_can_focus(False)
        self.audio_src.set_selected(0 if cfg.get("audio_source") == "system" else 1)
        self.audio_src.connect("notify::selected", self._set_audio_src)
        self.sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.sw.set_can_focus(False)
        self.sw.connect("notify::active", self._set_switch)
        self.opt.append(self.opt_label)
        self.opt.append(self.audio_src)
        self.opt.append(self.sw)
        bar.append(self.opt)

        hint = Gtk.Label(label="drag to capture      Esc cancel")
        hint.add_css_class("arc-hint")
        bar.append(hint)

        self.set_child(bar)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

        self._sync()

    # ------------------------------------------------------------ state
    def _sync(self):
        video = self.cfg["mode"] == "video"
        self.opt_label.set_label("Audio" if video else "Include pointer")
        self.audio_src.set_visible(video)
        self.sw.set_active(bool(self.cfg["audio"] if video else self.cfg.get("cursor")))
        self.state.set_label("recording" if video else "screenshot")

    def _set_switch(self, sw, _):
        key = "audio" if self.cfg["mode"] == "video" else "cursor"
        self.cfg[key] = sw.get_active()

    def _set_audio_src(self, dd, _):
        self.cfg["audio_source"] = "system" if dd.get_selected() == 0 else "mic"

    def _set_mode(self, k):
        self.cfg["mode"] = k
        self._sync()
        self._arm()                      # re-arm so the drag does the new thing

    def _set_area(self, k):
        self.cfg["area"] = k
        self._arm()

    # ------------------------------------------------------------- arming
    def _cancel_slurp(self):
        if self._slurp is not None:
            try:
                self._slurp.force_exit()
            except GLib.Error:
                pass
            self._slurp = None

    def _arm(self):
        """Make the screen live for the current area, without blocking the UI."""
        if self._busy:
            return
        self._cancel_slurp()
        area = self.cfg["area"]

        if area == "screen":
            # nothing to point at - the whole output is the target
            self._run(None)
            return

        argv, stdin_data = capture.slurp_argv(area)
        try:
            flags = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
            if stdin_data is not None:
                flags |= Gio.SubprocessFlags.STDIN_PIPE
            proc = Gio.Subprocess.new(argv, flags)
        except GLib.Error as exc:
            capture.notify("arcshot failed", str(exc), icon="dialog-error", urgent=True)
            self._finish()
            return
        self._slurp = proc
        proc.communicate_utf8_async(stdin_data, None, self._slurp_done, proc)
        # Both surfaces live on the `overlay` layer, and within a layer the
        # most recently mapped surface wins. slurp is spawned after us, so
        # without this the selection tint is painted OVER the toolbar and it
        # reads washed-out grey. Re-map to put us back on top.
        GLib.timeout_add(90, self._restack)

    def _restack(self):
        if self._slurp is not None and not self._busy:
            self.set_visible(False)
            self.set_visible(True)
        return False

    def _slurp_done(self, proc, res, _data):
        if proc is not self._slurp:
            return                       # superseded by a newer arm()
        try:
            ok, out, _err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        self._slurp = None
        geom = (out or "").strip()
        if not ok or not geom:
            return                       # cancelled: leave the toolbar up
        self._run(geom)

    # ------------------------------------------------------------- capture
    def _run(self, geom):
        self._busy = True
        config.save(self.cfg)
        self.set_visible(False)
        GLib.timeout_add(120, self._do, geom)

    def _do(self, geom):
        cfg = self.cfg
        try:
            if cfg["mode"] == "image":
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
        self._finish()
        return False

    def _finish(self):
        self._cancel_slurp()
        self.get_application().quit()

    def _key(self, _c, keyval, *_a):
        if keyval == Gdk.KEY_Escape:
            self._finish()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._arm()
            return True
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
        win = Overlay(self, config.load())
        win.present()
        # arm immediately so the drag is live the moment the toolbar appears
        GLib.timeout_add(80, lambda: (win._arm(), False)[1])
