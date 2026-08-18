"""The capture overlay.

Selection is drawn by this process rather than by slurp. That is not
gold-plating: slurp takes the pointer for the whole screen, so with slurp
armed the toolbar buttons cannot be clicked and the crosshair follows you
over the toolbar. Owning the surface means the toolbar keeps a normal cursor
and stays clickable while the rest of the screen is a crosshair.

Layout is one fullscreen layer-shell surface on the `overlay` layer with a
Gtk.Overlay inside it: a DrawingArea underneath that paints the dim and the
selection, and the toolbar floated on top of it.
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gtk4LayerShell", "1.0")
import cairo  # noqa: E402
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LS  # noqa: E402

from . import capture, config  # noqa: E402

APP_ID = "dev.arcshot.Arcshot"
MIN_DRAG = 8          # px; below this a drag is treated as a stray click

CSS = """
@define-color accent_bg_color #00d9ff;
@define-color accent_fg_color #05202a;
@define-color accent_color    #00d9ff;
@define-color destructive_bg_color #ff453a;
@define-color destructive_fg_color #1a0505;
@define-color destructive_color    #ff453a;

/* GtkWindow paints via its .background style class; without clearing that too
   the fullscreen surface is opaque and the dim reads as solid black. */
window, window.background, .arc-canvas {
  background-color: transparent;
  background-image: none;
}
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
label.arc-sub   { font-size: 11px; color: #86a5b3; }
label.arc-hint  { font-size: 10px; color: #6b8b99; }
label.arc-title { font-size: 13px; font-weight: 700; color: #00d9ff; letter-spacing: 1px; }
label.arc-rec   { font-size: 13px; font-weight: 700; color: #ff5f57; letter-spacing: 1px; }
/* NOT .destructive-action -- libadwaita repaints that via background-image and
   wins, giving a washed pink. A plain button takes our colours directly. */
/* Reuses the .arc-group selector, which is the one shape of rule libadwaita
   does not repaint over. A plain `button.arc-stop { background-color }` and
   even `.arc-bar button.arc-stop` both lose; :checked inside .arc-group wins. */
.arc-stopwrap button {
  border: 1px solid #ff5f57; border-radius: 10px; padding: 11px 18px;
  box-shadow: none;
}
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
        self.sel = None            # (x, y, w, h) while dragging / hovering
        self.dragging = False
        self.busy = False
        self.recording = False
        self.rects = []            # window rectangles, for window mode

        LS.init_for_window(self)
        LS.set_layer(self, LS.Layer.OVERLAY)
        LS.set_namespace(self, "arcshot")
        for edge in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(self, edge, True)
        LS.set_keyboard_mode(self, LS.KeyboardMode.EXCLUSIVE)

        self.stack = Gtk.Overlay()

        # ---- the canvas: dim + selection, crosshair cursor -----------------
        self.canvas = Gtk.DrawingArea()
        self.canvas.add_css_class("arc-canvas")
        self.canvas.set_draw_func(self._draw)
        self.canvas.set_cursor(Gdk.Cursor.new_from_name("crosshair"))

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.canvas.add_controller(drag)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.canvas.add_controller(motion)

        click = Gtk.GestureClick()
        click.connect("released", self._click)
        self.canvas.add_controller(click)

        self.stack.set_child(self.canvas)

        # ---- the toolbar: normal cursor, sits above the canvas -------------
        self.bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.bar.add_css_class("arc-bar")
        self.bar.set_halign(Gtk.Align.CENTER)
        self.bar.set_valign(Gtk.Align.END)
        self.bar.set_margin_bottom(90)
        self.bar.set_cursor(Gdk.Cursor.new_from_name("default"))
        self._build_bar()
        self.stack.add_overlay(self.bar)

        self.set_child(self.stack)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

        self._sync()

    # -------------------------------------------------------------- toolbar
    def _build_bar(self):
        cfg = self.cfg
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.title = Gtk.Label(label="◉ ARCSHOT", xalign=0)
        self.title.add_css_class("arc-title")
        self.title.set_hexpand(True)
        self.state = Gtk.Label(label="", xalign=1)
        self.state.add_css_class("arc-sub")
        head.append(self.title)
        head.append(self.state)
        self.bar.append(head)

        self.row_mode = _seg([("image", "  Image"), ("video", "  Video")],
                             cfg["mode"], self._set_mode)
        self.row_area = _seg([("region", "Selection"), ("screen", "Screen"),
                              ("window", "Window")], cfg["area"], self._set_area)
        self.bar.append(self.row_mode)
        self.bar.append(self.row_area)

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
        self.bar.append(self.opt)

        self.hint = Gtk.Label(label="drag to capture      Esc cancel")
        self.hint.add_css_class("arc-hint")
        self.bar.append(self.hint)

        # shown only while recording. A permanently-checked ToggleButton so it
        # picks up the one styling shape libadwaita leaves alone (see CSS).
        # A plain Button with a marked-up Label. NOT a ToggleButton: libadwaita
        # paints checked toggles with the accent colour from a provider that
        # outranks even PRIORITY_USER, so the button always came out cyan.
        # Colouring the label is the one approach nothing overrides.
        self.stop_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.stop_wrap.add_css_class("arc-stopwrap")
        stop_lbl = Gtk.Label()
        stop_lbl.set_markup(
            '<span foreground="#ff5f57" weight="bold">■  Stop recording</span>')
        self.stop_btn = Gtk.Button()
        self.stop_btn.set_child(stop_lbl)
        self.stop_btn.set_can_focus(False)
        self.stop_btn.set_hexpand(True)
        self.stop_btn.connect("clicked", lambda *_: self._stop())
        self.stop_wrap.append(self.stop_btn)
        self.stop_wrap.set_visible(False)
        self.bar.append(self.stop_wrap)

    def _sync(self):
        video = self.cfg["mode"] == "video"
        self.opt_label.set_label("Audio" if video else "Include pointer")
        self.audio_src.set_visible(video)
        self.sw.set_active(bool(self.cfg["audio"] if video else self.cfg.get("cursor")))
        self.state.set_label("recording" if video else "screenshot")
        hint = {"region": "drag to capture      Esc cancel",
                "screen": "click anywhere to capture      Esc cancel",
                "window": "click a window to capture      Esc cancel"}
        self.hint.set_label(hint[self.cfg["area"]])
        if self.cfg["area"] == "window":
            self.rects = capture.window_rects() or []
        self.canvas.queue_draw()

    # ---------------------------------------------------------------- state
    def _set_switch(self, sw, _):
        self.cfg["audio" if self.cfg["mode"] == "video" else "cursor"] = sw.get_active()

    def _set_audio_src(self, dd, _):
        self.cfg["audio_source"] = "system" if dd.get_selected() == 0 else "mic"

    def _set_mode(self, k):
        self.cfg["mode"] = k
        self._sync()

    def _set_area(self, k):
        self.cfg["area"] = k
        self.sel = None
        self._sync()

    # --------------------------------------------------------------- canvas
    def _draw(self, _area, cr, w, h):
        if self.recording:
            return                       # nothing to dim once we are rolling
        cr.set_source_rgba(0, 0, 0, 0.18)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        if not self.sel:
            return
        x, y, sw, sh = self.sel
        cr.set_operator(cairo.OPERATOR_SOURCE)   # punch a hole in the dim
        cr.set_source_rgba(0, 0, 0, 0)
        cr.rectangle(x, y, sw, sh)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgba(0.0, 0.85, 1.0, 0.95)
        cr.set_line_width(2)
        cr.rectangle(x + 1, y + 1, sw - 2, sh - 2)
        cr.stroke()

    def _motion(self, _c, x, y):
        if self.busy or self.recording or self.dragging:
            return
        area = self.cfg["area"]
        if area == "screen":
            alloc = self.canvas.get_allocation()
            self.sel = (0, 0, alloc.width, alloc.height)
            self.canvas.queue_draw()
        elif area == "window":
            hit = None
            for rx, ry, rw, rh in self.rects:
                if rx <= x <= rx + rw and ry <= y <= ry + rh:
                    hit = (rx, ry, rw, rh)      # last match == topmost
            if hit != self.sel:
                self.sel = hit
                self.canvas.queue_draw()

    def _click(self, _g, n_press, x, y):
        if self.busy or self.recording or self.dragging:
            return
        if self.cfg["area"] in ("screen", "window") and self.sel:
            self._go(self.sel)

    def _drag_begin(self, _g, x, y):
        if self.cfg["area"] != "region" or self.busy or self.recording:
            return
        self.dragging = True
        self._origin = (x, y)
        self.sel = (x, y, 0, 0)
        self.canvas.queue_draw()

    def _drag_update(self, _g, dx, dy):
        if not self.dragging:
            return
        ox, oy = self._origin
        self.sel = (min(ox, ox + dx), min(oy, oy + dy), abs(dx), abs(dy))
        self.canvas.queue_draw()

    def _drag_end(self, _g, dx, dy):
        if not self.dragging:
            return
        self.dragging = False
        if abs(dx) < MIN_DRAG or abs(dy) < MIN_DRAG:
            self.sel = None
            self.canvas.queue_draw()
            return
        self._go(self.sel)

    # -------------------------------------------------------------- capture
    def _go(self, rect):
        if self.busy:
            return
        self.busy = True
        config.save(self.cfg)
        x, y, w, h = (int(round(v)) for v in rect)
        geom = f"{x},{y} {w}x{h}"
        # Take the whole overlay off screen first, or it lands in the shot.
        self.set_visible(False)
        GLib.timeout_add(260, self._do, geom)

    def _do(self, geom):
        try:
            if self.cfg["mode"] == "image":
                path = capture.take_screenshot(self.cfg, geom)
                capture.notify("Screenshot saved", path)
                self.get_application().quit()
                return False
            path = capture.start_recording(self.cfg, geom)
            capture.notify("Recording started", path, icon="media-record")
            self._enter_recording()
        except capture.CaptureError as exc:
            capture.notify("arcshot failed", str(exc),
                           icon="dialog-error", urgent=True)
            self.get_application().quit()
        return False

    def _enter_recording(self):
        """Shrink to just the toolbar and wait for Stop."""
        self.recording = True
        self.busy = False
        self.sel = None
        for edge in (LS.Edge.TOP, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(self, edge, False)
        LS.set_keyboard_mode(self, LS.KeyboardMode.ON_DEMAND)
        self.canvas.set_visible(False)
        self.row_mode.set_visible(False)
        self.row_area.set_visible(False)
        self.opt.set_visible(False)
        self.hint.set_visible(False)
        self.stop_wrap.set_visible(True)
        self.title.set_markup(
            '<span foreground="#ff5f57" weight="bold">◉ RECORDING</span>')
        self.state.set_label("")
        self._t0 = GLib.get_monotonic_time()
        GLib.timeout_add_seconds(1, self._tick)
        self.set_visible(True)

    def _tick(self):
        if not self.recording:
            return False
        secs = int((GLib.get_monotonic_time() - self._t0) / 1_000_000)
        self.state.set_label(f"{secs // 60:02d}:{secs % 60:02d}")
        return True

    def _stop(self):
        self.recording = False
        path = capture.stop_recording()
        capture.notify("Recording saved", path or "", icon="media-record")
        self.get_application().quit()

    # ------------------------------------------------------------------ key
    def _key(self, _c, keyval, *_a):
        if keyval == Gdk.KEY_Escape:
            if self.recording:
                self._stop()
            else:
                self.get_application().quit()
            return True
        return False


class App(Adw.Application):
    def __init__(self):
        # NON_UNIQUE: a stale instance holding the bus name would otherwise
        # make every later launch exit 0 with no window and no log line.
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS.encode())
        # USER, not APPLICATION. libadwaita paints a checked ToggleButton with
        # the accent colour from its own provider, which outranks APPLICATION -
        # that is why the Stop button kept coming out cyan instead of red.
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_USER)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        # Keep a reference. Without one the Python wrapper is collected and the
        # window goes with it -- the app then exits 0 having mapped nothing.
        self.win = Overlay(self, config.load())
        self.win.present()
