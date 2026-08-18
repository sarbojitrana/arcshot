# Notes for agents and contributors

Context for anyone — human or AI — working on arcshot. Most of what follows is
non-obvious and was learned the hard way; changing it back will reintroduce a
bug that is easy to miss.

## What this is

A capture overlay for wlroots compositors. Press the key, the screen is
*already* live for a region drag, and a small toolbar floats over it so you can
switch to video / screen / window without a second keypress. Screenshots go to
the clipboard and to disk; recordings keep the toolbar up until you stop them.

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

**libadwaita outranks `STYLE_PROVIDER_PRIORITY_APPLICATION`.** The CSS provider
is installed at `PRIORITY_USER`. At APPLICATION priority libadwaita repaints
buttons via `background-image` and your `background-color` silently loses —
this is why the Stop button kept rendering in the accent colour instead of red.

**Hide the overlay before capturing.** `_go()` hides the window and waits
before invoking `grim`, otherwise the toolbar lands in its own screenshot.

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
will be non-zero.

Background jobs die when the invoking shell exits — launch with `setsid` if you
need the app to outlive your command.
