# Notes for agents and contributors

Context for anyone — human or AI — working on arcshot. Most of what follows is
non-obvious and was learned the hard way; changing it back will reintroduce a
bug that is easy to miss.

## What this is

A capture overlay for wlroots compositors. Press the key, the screen is
*already* live for a region drag, and a small toolbar floats over it so you can
switch to video / screen / window without a second keypress. Screen and window
captures fire as soon as you pick them — an optional timer buys you a moment to
arrange things first. Screenshots go to the clipboard and to disk; recordings
keep a Pause / Mic / Sound / Stop bar up, placed where the recording cannot
film it.

**The overlay must never appear in its own output.** That is the whole point of
the tool and it is easy to break: read `_conceal()` and the two entries about
it below before touching anything that draws.

## Layout

```
bin/arcshot            entry point; resolves the package from checkout or install
src/arcshot/
  __main__.py          argument handling; decides UI vs direct capture
  ui.py                the overlay (GTK4 + libadwaita + gtk4-layer-shell)
  capture.py           grim / wf-recorder / hyprctl backends
  config.py            persisted choices in ~/.config/arcshot/config.json
install.sh             installs to ~/.local, or /usr/local with --system
```

## Things that will bite you

**The toolbar is a layer-shell surface, not an ordinary window.** An
xdg-toplevel always sits *below* layer-shell surfaces, so it would be invisible
and unclickable over a fullscreen selection. It uses `gtk4-layer-shell` on the
`overlay` layer.

**`gtk4-layer-shell` must be linked before `libwayland-client`.** Python cannot
control link order, so `__main__._ensure_layer_shell()` re-execs once with
`LD_PRELOAD` set. Without it you get a *silent* fallback to a normal window
plus a `Failed to initialize layer surface` warning. Only the overlay path pays
this cost — `--stop` and the direct capture flags never import GTK.

**Selection is drawn in-process, not by `slurp`.** The overlay paints the dim
and the selection rectangle itself on a `Gtk.DrawingArea`. This is deliberate:
`slurp` takes the pointer for the whole screen, so with it running the toolbar
buttons cannot be clicked and the crosshair follows you over the toolbar.
`slurp` is still used by the CLI paths (`--shot` / `--rec`), where there is no
toolbar to interact with.

**The app is `NON_UNIQUE`.** With GTK's default single-instance behaviour, a
stale process still holding the bus name makes every later launch exit 0 with
no window and nothing in the logs — the capture key just quietly stops working.

**Keep a reference to the window.** `do_activate` assigns to `self.win`. Without
a reference the Python wrapper is garbage collected, the window goes with it,
and the app exits having mapped nothing.

**`wf-recorder` is stopped with `SIGINT`, never `SIGTERM`.** It needs the
interrupt to finalise the MP4 container; anything else leaves a truncated file
most players refuse to open.

**`wf-recorder` has no cursor switch** — it always draws the pointer. The
"Include pointer" row is therefore hidden in video mode rather than offering a
control that does nothing. Screenshots use `grim -c`.

**Picking Screen or Window captures immediately** (`_set_area`), and the
Capture / Record button fires the same path — Selection is the only mode that
waits for a gesture. The window is resolved when the shutter fires, not when
the button is pressed, so a timer can be used to go and hover something else;
if the pointer is still on the toolbar the focused window is used instead.

**A running timer shrinks the surface to the countdown badge and drops the
keyboard** (`KeyboardMode.NONE`). The point of the timer is to arrange
something on screen, which is impossible under a fullscreen surface that grabs
every click and key. Small badge, everything else clickable.

**A capture must not fire while the toolbar is being built.** `_seg()` connects
its `toggled` handler *after* `set_active()` for exactly this reason: with the
handler attached first, restoring a saved area of `screen` would capture the
moment the overlay opened.

**libadwaita outranks `STYLE_PROVIDER_PRIORITY_APPLICATION`.** The CSS provider
is installed at `PRIORITY_USER`. At APPLICATION priority libadwaita repaints
buttons via `background-image` and your `background-color` silently loses —
this is why the Stop button kept rendering in the accent colour instead of red.

**Never *unmap* the overlay to keep it out of a capture.** Hiding the window
starts the compositor's close animation, and `grim` then photographs the
toolbar mid-fade — Hyprland's `fadeLayersOut` is 450ms out of the box and it
is a user setting, so no fixed sleep is safe. `_conceal()` leaves the surface
mapped and empties it instead — rows hidden, `.arc-gone` on the bar — then
waits for a frame before capturing. There is no animation to sit out: the
surface leaves the output as soon as the empty frame lands.

**`GtkOverlay` stops laying out its overlay children while its main child is
hidden.** This is the single nastiest thing in this codebase. The toolbar is an
overlay child of the `Gtk.Overlay` whose main child is the canvas, so hiding
the canvas freezes the toolbar's layout: `set_visible(False)` on a row,
`set_opacity(0)`, `queue_draw()` and a `set_default_size()` resize all become
no-ops that GTK never commits, and the compositor keeps showing the last frame
*indefinitely* — a full second later `grim` still photographs it. Style changes
are the exception; they repaint. So the canvas is **never hidden**, only
emptied: `_draw()` returns early while concealing, counting down or recording.
If a toolbar ever appears in a capture again, check for a `canvas.set_visible`
before anything else.

**`_conceal()` waits on the frame counter, not on the next `after-paint`.** A
frame cycle is often already in flight when concealment starts, and that frame
was drawn before the change; taking its `after-paint` as the all-clear
photographs the toolbar. `Gdk.FrameClock.get_frame_counter()` is sampled first
and only a *later* frame counts.

**CSS `opacity` does not hide child widgets**, only the widget's own box, so
`.arc-gone` cannot be the whole story — the rows are hidden as well. The class
still matters, and not for its looks: a style change is the one edit that
reliably repaints this window (see the `GtkOverlay` entry).

**The overlay sets `set_exclusive_zone(-1)`.** Without it a layer surface is
handed only what panels have not reserved — with waybar up the overlay maps at
`0,32 1920x1048`. Canvas coordinates are passed to `grim -g` as screen
coordinates, so every capture came out 32px adrift and a "whole screen" shot
quietly lost the bottom rows. It still assumes one monitor whose origin is
`0,0`; a second output at an offset would need the monitor position added.

**`wf-recorder` films the control bar like anything else.** The only way to
keep it out of the file is to keep it out of the recorded rectangle, so
`_place_bar()` walks a list of anchors and takes the first that does not
intersect the region — bottom-centre first, so recordings that never reach it
see no movement. A *whole-screen* recording leaves no free corner, so
`_trim_for_bar()` shortens the frame by the bar's strip instead (1920x1080
becomes 1920x948 here): the controls stay usable and stay out of the file.
Only the full-screen case is trimmed — a region or window is a rectangle the
user chose, and quietly shrinking it would be rude; those fall back to no bar
at all, with `--pause` and the capture key as the controls. The surface also
needs `set_default_size()`: an unanchored layer-shell dimension takes the
window's size, and a GtkWindow asked for nothing falls back to 200x200.

**The toolbar is measured *before* concealment, not after.** A concealed
toolbar measures 0x0, and both the trim and the placement are computed from
its size, so `_fire()` switches to the recording layout and stashes
`rec_size` while the bar is still on screen.

**Pause is stop-and-start, because `wf-recorder` has no pause.** Each run is a
segment; `stop_recording()` concatenates them with `ffmpeg -c copy`, so a pause
costs no time in the finished file (verified: 3s + 2s paused + 3s → 5.9s). One
segment is renamed rather than concatenated. If ffmpeg is missing the parts are
left on disk rather than thrown away.

**Audio is always recorded from a null sink we own.** `wf-recorder` takes one
`--audio` device and cannot be re-pointed while running, so recording the mic
or the monitor directly would freeze that choice for the whole recording and
could never mix both. `mix_start()` builds `arcshot_mix` and the Mic / Sound
buttons load and unload `module-loopback` into it — the device wf-recorder was
given never changes. The mix is created even with both sources off, so a
toggle mid-recording has somewhere to go and every segment has the same stream
layout for the concat. Everything is unloaded in `stop_recording()`, and
`session()` unloads it too if wf-recorder dies on its own.

**`os.kill(pid, 0)` lies about children we started.** A finished child is a
zombie until reaped and answers `kill(0)` happily, so pause and stop each sat
out the full 3s timeout waiting for a process that had already exited. Keep the
`Popen` in `_PROCS` and use `poll()`/`wait()`; `_alive()` also reads
`/proc/<pid>/stat` for pids from another process.

**The segment clock stops at the interrupt, not when the process exits.**
Finalising the container adds no frames, so counting the wait made the elapsed
time run ahead of the file by ~3 seconds.

## Testing

There is no unit test suite; this is a GUI that talks to a compositor. Verify
changes by running it and checking observable state:

```bash
./bin/arcshot --status                 # no GTK involved
hyprctl layers | grep arcshot          # is the surface mapped, and how big
grim /tmp/x.png                        # screenshot the result and look at it
ffprobe -v error -show_entries stream=codec_type /tmp/rec.mp4
```

Useful check when a capture looks wrong: count near-`#00d9ff` pixels in the
toolbar region of the output. If the toolbar leaked into the screenshot they
will be non-zero. Check the geometry in the same breath — the surface in
`hyprctl layers` must match the monitor in `hyprctl monitors` exactly, or
selections are offset by whatever a panel reserved.

Driving the overlay by hand is slow and does not cover the recording paths. It
is worth writing a throwaway subclass of `App` that overrides `do_activate`,
points the config at a temp directory, stubs out `config.save`, and drives
`win._set_area(...)` or `win._trigger(...)` from a timeout — that exercises
countdown, conceal, placement and capture end to end. Run it with
`LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so` and
`ARCSHOT_LAYER_SHELL_PRELOADED=1` set, since it bypasses `__main__`. Give it a
watchdog that quits after ~25s: a probe that raises inside a GLib callback
leaves a fullscreen overlay sitting on the user's screen.

The recording state machine can be tested without any GTK at all — import
`capture`, call `start_recording` / `pause_recording` / `resume_recording` /
`stop_recording` with sleeps between, and check the result with `ffprobe`. That
is the quickest way to check pause accounting and the concat.

Background jobs die when the invoking shell exits — launch with `setsid` if you
need the app to outlive your command.

**Install before believing a fix works.** The key binding runs
`~/.local/bin/arcshot`, which loads `~/.local/lib/arcshot` — not the checkout.
A fix that is only in the working tree changes nothing for the user pressing
Print; run `./install.sh`.
