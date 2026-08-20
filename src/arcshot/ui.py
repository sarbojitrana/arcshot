"""The capture overlay.

Selection is drawn by this process rather than by slurp. That is not
gold-plating: slurp takes the pointer for the whole screen, so with slurp
armed the toolbar buttons cannot be clicked and the crosshair follows you
over the toolbar. Owning the surface means the toolbar keeps a normal cursor
and stays clickable while the rest of the screen is a crosshair.

Layout is one fullscreen layer-shell surface on the `overlay` layer with a
Gtk.Overlay inside it: a DrawingArea underneath that paints the dim and the
selection, and the toolbar floated on top of it.

The same surface does three jobs in turn -- fullscreen for selecting, a small
badge while a timer counts down, and a small control bar while recording --
by re-anchoring itself and letting its size follow the toolbar.
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
BOTTOM_GAP = 90       # resting place of the full toolbar, clear of most docks
REC_GAP = 12          # the recording bar hugs the edge -- every px here is a
                      # px trimmed off a full-screen recording
FRAME_GRACE_MS = 90   # compositing headroom once our empty frame is committed
CONCEAL_MAX_MS = 400  # fallback if the frame clock never reports a paint
SETTLE_MS = 150       # layer-shell configure round trip after re-anchoring
SLACK = 4             # px of doubt about where the compositor puts a surface
TIMERS = ((0, "Off"), (3, "3s"), (5, "5s"), (10, "10s"))

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
.arc-group.arc-small button { padding: 6px 12px; font-size: 11px; }
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
.arc-actionwrap button {
  border: 1px solid #00d9ff; border-radius: 10px; padding: 11px 18px;
  box-shadow: none;
}
/* How the toolbar gets out of a capture -- see _conceal(). Blanking the box
   is only half of it; the class exists mainly because *a style change is the
   one thing that reliably repaints this window*. Once the surface is sized
   by us rather than by its anchors, gtk_widget_set_opacity(), queue_draw()
   and set_visible() all queue nothing GTK will commit, and the compositor
   goes on showing the last frame for as long as you care to wait. */
.arc-gone {
  background: none; border-color: transparent; box-shadow: none;
}
"""


def _seg(items, active, on_change, small=False):
    """A row of toggles behaving as radio buttons."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
    box.add_css_class("arc-group")
    if small:
        box.add_css_class("arc-small")
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
        # connected after set_active, or building the row would fire a capture
        btn.connect("toggled", lambda b, k=key: b.get_active() and on_change(k))
        box.append(btn)
    return box


def _toggle(label, active, on_change):
    """An independent on/off toggle wearing the segmented-group styling."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    box.add_css_class("arc-group")
    box.add_css_class("arc-small")
    btn = Gtk.ToggleButton(label=label)
    btn.set_can_focus(False)
    btn.set_active(active)
    btn.connect("toggled", lambda b: on_change(b.get_active()))
    box.append(btn)
    return box, btn


def _plain(markup, on_click, wrap_class):
    """A button whose colour lives in its label.

    libadwaita paints a checked toggle with the accent colour from a provider
    that outranks even PRIORITY_USER, so the button itself cannot be coloured
    reliably. Colouring the label is the one approach nothing overrides.
    """
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    box.add_css_class(wrap_class)
    label = Gtk.Label()
    label.set_markup(markup)
    btn = Gtk.Button()
    btn.set_child(label)
    btn.set_can_focus(False)
    btn.set_hexpand(True)
    btn.connect("clicked", lambda *_: on_click())
    box.append(btn)
    return box, btn, label


class Overlay(Gtk.ApplicationWindow):
    # Anchors the small bar may use, in the order they are tried.
    PLACES = ("B", "T", "BR", "TR", "BL", "TL")
    EDGES = {"T": LS.Edge.TOP, "B": LS.Edge.BOTTOM,
             "L": LS.Edge.LEFT, "R": LS.Edge.RIGHT}

    def __init__(self, app, cfg):
        super().__init__(application=app)
        self.cfg = cfg
        self.sel = None            # (x, y, w, h) while dragging / hovering
        self.dragging = False
        self.busy = False
        self.concealed = False     # painting nothing, so captures cannot see us
        self.counting = False      # a timer is running
        self.recording = False
        self.paused = False
        self.syncing = False       # setting widgets, not reacting to the user
        self.rects = []            # window rectangles, for window mode
        self.screen = (0, 0)       # surface size; the monitor we are covering

        LS.init_for_window(self)
        LS.set_layer(self, LS.Layer.OVERLAY)
        LS.set_namespace(self, "arcshot")
        for edge in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(self, edge, True)
        # Opt out of other surfaces' exclusive zones. Panels reserve space,
        # and without this the overlay is handed what is left instead of the
        # output: with waybar up it maps at 0,32 1920x1048, every canvas
        # coordinate is 32px adrift of the screen coordinate grim wants, and
        # a "whole screen" shot silently loses the bottom 32 rows.
        LS.set_exclusive_zone(self, -1)
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
        self.bar.set_margin_bottom(BOTTOM_GAP)
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

        # screenshots: the pointer switch
        self.row_shot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        lbl = Gtk.Label(label="Include pointer", xalign=0)
        lbl.add_css_class("arc-sub")
        lbl.set_hexpand(True)
        self.cursor_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.cursor_sw.set_can_focus(False)
        self.cursor_sw.set_active(bool(cfg.get("cursor")))
        self.cursor_sw.connect("notify::active", self._set_cursor)
        self.row_shot.append(lbl)
        self.row_shot.append(self.cursor_sw)
        self.bar.append(self.row_shot)

        # video: two independent sources, both live during a recording
        self.row_vid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vlbl = Gtk.Label(label="Audio", xalign=0)
        vlbl.add_css_class("arc-sub")
        vlbl.set_hexpand(True)
        mic_box, self.mic_btn = _toggle("Mic", bool(cfg.get("mic")),
                                        lambda on: self._set_source("mic", on))
        sys_box, self.sys_btn = _toggle("System sound",
                                        bool(cfg.get("system_audio")),
                                        lambda on: self._set_source("system", on))
        self.row_vid.append(vlbl)
        self.row_vid.append(mic_box)
        self.row_vid.append(sys_box)
        self.bar.append(self.row_vid)

        self.row_timer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tlbl = Gtk.Label(label="Timer", xalign=0)
        tlbl.add_css_class("arc-sub")
        tlbl.set_hexpand(True)
        self.row_timer.append(tlbl)
        self.row_timer.append(_seg(TIMERS, int(cfg.get("timer") or 0),
                                   self._set_timer, small=True))
        self.bar.append(self.row_timer)

        self.action_wrap, self.action_btn, self.action_lbl = _plain(
            '<span foreground="#00d9ff" weight="bold">◉  Capture</span>',
            self._capture_now, "arc-actionwrap")
        self.bar.append(self.action_wrap)

        self.hint = Gtk.Label(label="drag to capture      Esc cancel")
        self.hint.add_css_class("arc-hint")
        self.bar.append(self.hint)

        # ---- shown only while a timer runs ---------------------------------
        self.count_wrap, _btn, self.count_lbl = _plain(
            '<span foreground="#00d9ff" weight="bold">◉  3</span>',
            self._cancel_countdown, "arc-actionwrap")
        self.count_wrap.set_visible(False)
        self.bar.append(self.count_wrap)

        # ---- shown only while recording ------------------------------------
        self.row_rec = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pause_box, self.pause_btn, self.pause_lbl = _plain(
            '<span foreground="#00d9ff" weight="bold">❚❚  Pause</span>',
            self._toggle_pause, "arc-actionwrap")
        rec_mic, self.rec_mic_btn = _toggle(
            "Mic", bool(cfg.get("mic")), lambda on: self._live_source("mic", on))
        rec_sys, self.rec_sys_btn = _toggle(
            "Sound", bool(cfg.get("system_audio")),
            lambda on: self._live_source("system", on))
        stop_box, self.stop_btn, _lbl = _plain(
            '<span foreground="#ff5f57" weight="bold">■  Stop</span>',
            self._stop, "arc-stopwrap")
        for w in (pause_box, rec_mic, rec_sys, stop_box):
            self.row_rec.append(w)
        self.row_rec.set_visible(False)
        self.bar.append(self.row_rec)

    def _sync(self):
        """The idle toolbar: every row's visibility decided in one place."""
        video = self.cfg["mode"] == "video"
        area = self.cfg["area"]
        for row in (self.row_mode, self.row_area, self.row_timer, self.hint):
            row.set_visible(True)
        self.count_wrap.set_visible(False)
        self.row_rec.set_visible(False)
        self.row_shot.set_visible(not video)
        self.row_vid.set_visible(video)
        self.state.set_label("recording" if video else "screenshot")
        self.action_wrap.set_visible(area != "region")
        self.action_lbl.set_markup(
            '<span foreground="#00d9ff" weight="bold">%s</span>'
            % ("●  Record" if video else "◉  Capture"))
        hint = {"region": "drag to capture      Esc cancel",
                "screen": "click Capture, or anywhere      Esc cancel",
                "window": "click a window, or Capture      Esc cancel"}
        self.hint.set_label(hint[area])
        if area == "window":
            self.rects = capture.window_rects() or []
        self.canvas.queue_draw()

    # ---------------------------------------------------------------- state
    def _set_cursor(self, sw, _):
        self.cfg["cursor"] = sw.get_active()

    def _set_source(self, kind, on):
        if self.syncing:
            return
        self.cfg["mic" if kind == "mic" else "system_audio"] = on

    def _set_timer(self, secs):
        self.cfg["timer"] = int(secs)

    def _set_mode(self, k):
        self.cfg["mode"] = k
        self._sync()

    def _set_area(self, k):
        self.cfg["area"] = k
        self.sel = None
        self._sync()
        # Switching to a target that needs no drag is itself the go-ahead:
        # region is the only mode where a further gesture is required.
        if k != "region" and not (self.busy or self.recording):
            GLib.timeout_add(120, self._capture_now)   # let the toggle paint

    # --------------------------------------------------------------- canvas
    def _draw(self, _area, cr, w, h):
        # The canvas is never hidden, only emptied -- GtkOverlay stops laying
        # out its overlay children while its main child is hidden, which
        # freezes the toolbar on screen (see _conceal).
        if not (self.concealed or self.counting or self.recording):
            self.screen = (w, h)
        if self.concealed or self.counting or self.recording:
            return                       # paint nothing: dim included
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
            self.sel = (0, 0) + self.screen
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
            self._capture_now()

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
        rect = self.sel
        self._trigger(lambda: rect)

    # -------------------------------------------------------------- capture
    def _on_bar(self, x, y):
        """Is the pointer over the toolbar itself?"""
        if not self.bar.get_visible():
            return False
        ok, rect = self.bar.compute_bounds(self)
        return bool(ok and rect.origin.x <= x <= rect.origin.x + rect.size.width
                    and rect.origin.y <= y <= rect.origin.y + rect.size.height)

    def _window_target(self):
        """The window to capture, decided when the shutter actually fires.

        Read late on purpose: with a timer running the whole point is that
        the pointer has moved somewhere since the button was pressed. And
        when the pointer is still resting on the button that started this,
        the window underneath the toolbar is not what was meant -- the one
        the user was last working in is.
        """
        pos = capture.cursor_pos()
        if pos and not self._on_bar(*pos):
            hit = capture.window_at(*pos)
            if hit:
                return hit
        return capture.active_window_rect() or self.sel

    def _capture_now(self):
        area = self.cfg["area"]
        if area == "screen":
            self._trigger(lambda: (0, 0) + self.screen)
        elif area == "window":
            self._trigger(self._window_target)
        return False        # so it can be used as a GLib timeout callback

    def _trigger(self, resolve):
        """Begin a capture: hold the settings, run the timer, then fire."""
        if self.busy or self.recording:
            return
        self.busy = True
        config.save(self.cfg)
        secs = int(self.cfg.get("timer") or 0)
        if secs:
            self._countdown(secs, resolve)
        else:
            self._fire(resolve)

    def _fire(self, resolve):
        rect = resolve()
        if not rect:
            self.busy = False
            return
        x, y, w, h = (int(round(v)) for v in rect)
        if w < 1 or h < 1:
            self.busy = False
            return
        if self.cfg["mode"] == "video":
            # Lay the controls out and measure them *now*, while they are
            # still on screen: a concealed toolbar measures 0x0, and both the
            # trim and the placement are worked out from that size.
            self._recording_layout()
            nat = self.bar.get_preferred_size()[1]
            self.rec_size = (nat.width, nat.height)
        self._conceal(lambda: self._do((x, y, w, h)))

    # ------------------------------------------------------------- countdown
    def _countdown(self, secs, resolve):
        """Get out of the way, count, then fire.

        The surface shrinks to the badge instead of staying fullscreen, which
        is what makes the rest of the screen usable while the clock runs --
        the user is counting down precisely so they can go and arrange
        something. Keyboard goes back to the desktop for the same reason.
        """
        self.counting = True
        self.left = secs
        for row in (self.row_mode, self.row_area, self.row_shot, self.row_vid,
                    self.row_timer, self.action_wrap, self.hint):
            row.set_visible(False)
        self.count_wrap.set_visible(True)
        self.state.set_label("")
        self.bar.set_margin_bottom(0)
        LS.set_keyboard_mode(self, LS.KeyboardMode.NONE)
        self._paint_count()
        nat = self.bar.get_preferred_size()[1]     # the badge, not the bar
        self._anchor("B", BOTTOM_GAP, (nat.width, nat.height))

        def tick():
            if not self.counting:
                return False
            self.left -= 1
            if self.left > 0:
                self._paint_count()
                return True
            self.counting = False
            self._fire(resolve)
            return False

        GLib.timeout_add_seconds(1, tick)

    def _paint_count(self):
        self.count_lbl.set_markup(
            '<span foreground="#00d9ff" weight="bold">◉  %d</span>'
            '<span foreground="#6b8b99">    cancel</span>' % self.left)

    def _cancel_countdown(self):
        if not self.counting:
            return
        self.counting = False
        self.busy = False
        self._reveal()
        self.bar.set_margin_bottom(BOTTOM_GAP)
        for edge in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(self, edge, True)
            LS.set_margin(self, edge, 0)
        LS.set_keyboard_mode(self, LS.KeyboardMode.EXCLUSIVE)
        self._sync()

    # --------------------------------------------------------------- conceal
    def _rows(self):
        rows, child = [], self.bar.get_first_child()
        while child:
            rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _conceal(self, then):
        """Stop contributing any pixels, then run `then`.

        Hiding the window is *not* the way to do this. Unmapping a layer
        surface starts the compositor's close animation, and the capture then
        catches the toolbar mid-fade -- Hyprland's fadeLayersOut alone is
        450ms by default, and it is a user setting, so no fixed sleep is safe.
        Staying mapped while painting nothing has no animation to wait out:
        the surface is gone from the output as soon as our empty frame lands.

        Getting that frame *out* is the fiddly part. Hiding the rows is what
        empties the toolbar, but on its own it changes nothing on screen:
        this window only repaints reliably when its style changes, so the
        .arc-gone class is doing double duty -- it blanks the bar's own box
        and it is the edit that makes GTK draw and commit at all.
        """
        self.concealed = True
        self.hidden = [row for row in self._rows() if row.get_visible()]
        for row in self.hidden:
            row.set_visible(False)
        self.bar.add_css_class("arc-gone")
        self.canvas.set_cursor(Gdk.Cursor.new_from_name("default"))
        self.canvas.queue_draw()

        done = []

        def fire():
            if not done:
                done.append(True)
                then()
            return False

        surface = self.get_surface()
        clock = surface.get_frame_clock() if surface else None
        if clock is None:
            GLib.timeout_add(CONCEAL_MAX_MS, fire)
            return
        # Count frames rather than take the next after-paint: a cycle can
        # already be in flight when we get here, and that frame was drawn
        # before the change. Anything past this counter has it.
        state = {"base": clock.get_frame_counter()}

        def painted(clk):
            if clk.get_frame_counter() <= state["base"]:
                return
            # The empty frame is committed; the compositor still has to
            # composite it before the screen we hand over is the real one.
            handler = state.pop("id", None)
            if handler is not None:
                clk.disconnect(handler)
            GLib.timeout_add(FRAME_GRACE_MS, fire)

        state["id"] = clock.connect("after-paint", painted)
        GLib.timeout_add(CONCEAL_MAX_MS, fire)     # in case no frame arrives

    def _reveal(self):
        """Put back exactly the rows concealment took away."""
        self.concealed = False
        self.bar.remove_css_class("arc-gone")
        for row in getattr(self, "hidden", ()):
            row.set_visible(True)
        self.hidden = []

    def _do(self, rect):
        try:
            if self.cfg["mode"] == "image":
                path = capture.take_screenshot(self.cfg, "{},{} {}x{}".format(*rect))
                capture.notify("Screenshot saved", path)
                self.get_application().quit()
                return False
            rect = self._trim_for_bar(rect)
            path = capture.start_recording(self.cfg, "{},{} {}x{}".format(*rect))
            self.recording = True
            self.busy = False
            if self._place_bar(rect):
                capture.notify("Recording started", path, icon="media-record")
                GLib.timeout_add(SETTLE_MS, self._show_bar)
            else:
                # Nowhere to put the controls that the recording would not
                # film, so there are none and the key does the stopping.
                capture.notify("Recording started",
                               "Press your capture key again to stop.\n" + path,
                               icon="media-record")
                self.get_application().quit()
        except capture.CaptureError as exc:
            capture.notify("arcshot failed", str(exc),
                           icon="dialog-error", urgent=True)
            self.get_application().quit()
        return False

    # ------------------------------------------------------------- placement
    def _size(self):
        """Toolbar size: the measurement taken before concealing, if there is
        one, because a concealed toolbar measures nothing."""
        if getattr(self, "rec_size", None):
            return self.rec_size
        nat = self.bar.get_preferred_size()[1]
        return nat.width, nat.height

    def _anchor(self, place, gap, size=None):
        """Shrink the surface onto the toolbar and pin it to one spot.

        A layer-shell dimension that is not anchored to both of its edges
        takes its size from the window, and a GtkWindow with nothing asked of
        it falls back to 200x200 -- which both clips the toolbar and makes
        any rectangle worked out from its size a fiction.
        """
        bw, bh = size or self._size()
        self.set_default_size(bw, bh)
        for key, edge in self.EDGES.items():
            on = key in place
            LS.set_anchor(self, edge, on)
            LS.set_margin(self, edge, gap if on else 0)
        return bw, bh

    def _bar_rect(self, place, bw, bh, gap):
        sw, sh = self.screen
        if "L" in place:
            x = gap
        elif "R" in place:
            x = sw - bw - gap
        else:
            x = (sw - bw) // 2
        y = gap if "T" in place else sh - bh - gap
        return x, y

    def _place_bar(self, rect):
        """Anchor the controls somewhere `rect` does not cover; False if nowhere.

        wf-recorder films whatever the compositor puts inside the region, so
        the only way to keep the controls out of the file is to keep them out
        of the region.
        """
        sw, sh = self.screen
        bw, bh = self._size()
        if not (sw and sh and bw and bh):
            return False
        rx, ry, rw, rh = rect
        for place in self.PLACES:
            x, y = self._bar_rect(place, bw, bh, REC_GAP)
            if x < 0 or y < 0:
                continue
            # SLACK: the compositor centres in its own arithmetic, so the
            # surface can land a pixel off what we worked out here.
            if not (x + bw + SLACK <= rx or x - SLACK >= rx + rw
                    or y + bh + SLACK <= ry or y - SLACK >= ry + rh):
                continue                       # the recording would film it
            # The exclusive zone set in __init__ is what makes this rectangle
            # true: without it a panel's reserved strip would shove the bar
            # off the spot just cleared and back towards the region.
            self._anchor(place, REC_GAP)
            return True
        return False

    def _trim_for_bar(self, rect):
        """Give the controls a strip of their own on a whole-screen recording.

        A recording of the whole screen has no free corner by definition, so
        either the controls appear in the video or the video stops short of
        them. Stopping short is what was asked for; only the full-screen case
        is trimmed, since a region or a window is a rectangle the user chose
        and shrinking that would be an odd thing to do behind their back.
        """
        sw, sh = self.screen
        if tuple(rect) != (0, 0, sw, sh):
            return rect
        h = sh - self._size()[1] - 2 * REC_GAP
        # h264 will not take odd dimensions
        return (0, 0, sw - sw % 2, max(2, h - h % 2))

    # ------------------------------------------------------------- recording
    def _recording_layout(self):
        for row in (self.row_mode, self.row_area, self.row_shot, self.row_vid,
                    self.row_timer, self.action_wrap, self.hint,
                    self.count_wrap):
            row.set_visible(False)
        self.syncing = True
        self.rec_mic_btn.set_active(bool(self.cfg.get("mic")))
        self.rec_sys_btn.set_active(bool(self.cfg.get("system_audio")))
        self.syncing = False
        self.row_rec.set_visible(True)
        self.title.set_markup(
            '<span foreground="#ff5f57" weight="bold">◉ REC</span>')
        self.state.set_label("00:00")
        self.bar.set_margin_bottom(0)

    def _show_bar(self):
        if not self.recording:
            return False
        self._reveal()
        self._tick()
        GLib.timeout_add_seconds(1, self._tick)
        return False

    def _tick(self):
        if not self.recording:
            return False
        if not capture.is_recording():
            self.get_application().quit()    # stopped from another process
            return False
        secs = int(capture.elapsed())
        self.state.set_label("%02d:%02d%s" % (secs // 60, secs % 60,
                                              "  paused" if self.paused else ""))
        return True

    def _toggle_pause(self):
        if not self.recording:
            return
        self.paused = capture.toggle_pause()
        self.pause_lbl.set_markup(
            '<span foreground="#00d9ff" weight="bold">%s</span>'
            % ("▶  Resume" if self.paused else "❚❚  Pause"))
        self.title.set_markup(
            '<span foreground="%s" weight="bold">◉ %s</span>'
            % (("#86a5b3", "PAUSED") if self.paused else ("#ff5f57", "REC")))
        self._tick()

    def _live_source(self, kind, on):
        """Mic and system sound, switched while the recording runs."""
        if self.syncing:
            return
        self.cfg["mic" if kind == "mic" else "system_audio"] = on
        if self.recording:
            capture.set_audio(kind, on)

    def _stop(self):
        self.recording = False
        path = capture.stop_recording()
        capture.notify("Recording saved", path or "", icon="media-record")
        self.get_application().quit()

    # ------------------------------------------------------------------ key
    def _key(self, _c, keyval, *_a):
        if keyval == Gdk.KEY_Escape:
            if self.counting:
                self._cancel_countdown()
            elif self.recording:
                self._stop()
            else:
                self.get_application().quit()
            return True
        if keyval == Gdk.KEY_space and self.recording:
            self._toggle_pause()
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
