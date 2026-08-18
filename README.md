# Arcshot

A small screenshot and screen-recording chooser for **Hyprland** and other
wlroots compositors. Press your capture key, pick what you want, done — and it
remembers what you picked last time.

![preview](preview.png)

It is the GNOME screenshot flow, on Wayland, without GNOME.

---

## What it does

- **The screen is live the moment it opens.** Press the key and drag a region
  straight away — no picking a mode, no shutter button.
- **The toolbar stays usable while the screen is live.** Normal cursor over the
  toolbar, crosshair everywhere else, and the buttons actually click, so you
  can switch to video / screen / window without cancelling first.
- **Recordings keep the toolbar up** with an elapsed timer and a Stop button.
  Screenshots just take the shot and get out of the way.
- **Image or video**, **whole screen / region / window**, and for video,
  **audio on or off** with a system-or-microphone source.
- **Include the pointer or not** on screenshots.
- **Remembers your last choice**, so the common case is press-key, press-Enter.
- Screenshots land on the **clipboard** as well as on disk.
- Optional hand-off to **swappy** for annotation.

## Install

```bash
git clone https://github.com/sarbojitrana/arcshot.git
cd arcshot
./install.sh
```

Installs to `~/.local` (no root). `sudo ./install.sh --system` puts it in
`/usr/local` instead; `./install.sh --uninstall` removes it.

Then bind it. In `~/.config/hypr/conf/custom.conf`:

```
bind = , Print,      exec, arcshot                # overlay, selection armed
bind = SUPER, Print, exec, arcshot --shot screen  # whole screen, no overlay
```

## Requirements

| | |
|---|---|
| **Required** | `python-gobject` (GTK 4 + libadwaita), `gtk4-layer-shell`, `grim`, `slurp` |
| **Recording** | `wf-recorder` |
| **Clipboard** | `wl-clipboard` |
| **Notifications** | `libnotify` |
| **Annotation** (optional) | `swappy` |

On Arch:

```bash
sudo pacman -S python-gobject gtk4 libadwaita gtk4-layer-shell grim slurp wf-recorder wl-clipboard libnotify swappy
```

The installer checks all of these and tells you which optional features you'd
be missing before it writes anything.

## Command line

The GUI is optional — every mode has a flag, so you can bind them directly.

```
arcshot                 open the capture overlay, or stop an active recording
arcshot --stop          stop an active recording
arcshot --shot AREA     screenshot now      (screen | region | window)
arcshot --rec  AREA     start recording now (screen | region | window)
arcshot --status        prints 'recording' or 'idle'
```

`--status` is there so you can put a recording indicator in your bar. A waybar
module:

```json
"custom/arcshot": {
  "exec": "arcshot --status",
  "interval": 2,
  "format": "{}",
  "on-click": "arcshot --stop"
}
```

## Configuration

`~/.config/arcshot/config.json`, written whenever you capture from the GUI:

```json
{
  "mode": "image",
  "area": "region",
  "audio": false,
  "audio_source": "system",
  "cursor": false,
  "copy_to_clipboard": true,
  "open_editor": false,
  "image_dir": "",
  "video_dir": ""
}
```

`image_dir` / `video_dir` default to your XDG Pictures and Videos folders plus
`Screenshots` / `Recordings`. Set `open_editor` to `true` to have every
screenshot open in swappy.

## Notes on how it works

**Stopping a recording never loads GTK.** The "is something already recording?"
check happens in `__main__` before the UI is imported, so the stop path is a
PID check and a signal.

**Recordings are stopped with `SIGINT`, not `SIGTERM`.** `wf-recorder` needs the
interrupt to finalise the MP4 container; killing it any other way leaves a
truncated file that most players refuse.

**Window selection** asks Hyprland for the geometry of every window on the
current workspace and feeds those rectangles to `slurp -r`, so you click a
window rather than dragging a box around it. On a compositor without `hyprctl`
it falls back to region select.

**The overlay draws its own selection instead of shelling out to `slurp`.**
That is not gold-plating. `slurp` takes the pointer for the entire screen, so
with it armed the toolbar buttons cannot be clicked and the crosshair follows
you over the toolbar — there is no way to change mode without cancelling first.
Owning the surface means the canvas gets a crosshair, the toolbar keeps a normal
cursor, and both work at once.

The layout is one fullscreen `gtk4-layer-shell` surface on the `overlay` layer
holding a `Gtk.Overlay`: a `DrawingArea` painting the dim and selection, with the
toolbar floated on top. `slurp` is still used by the `--shot` / `--rec` CLI
flags, where there is no toolbar to compete with.

Three traps worth writing down:

- `gtk4-layer-shell` must be linked *before* `libwayland-client`, which Python
  cannot control. arcshot re-execs itself once with `LD_PRELOAD` set. Only the
  overlay path does this — `--stop` and the capture flags never load GTK.
- The window needs a Python reference held on the application. Without one the
  wrapper is garbage-collected, the window goes with it, and the process exits 0
  having mapped nothing.
- libadwaita paints checked toggle buttons with the accent colour from a
  provider that outranks even `PRIORITY_USER`, so a red Stop button cannot be
  done with CSS on the widget. Its label carries the colour as Pango markup.

**The overlay is taken off screen before the grab**, with a 260 ms gap so the
compositor has really dropped it — otherwise the toolbar lands in your own
screenshot.

**Placement** is handled by layer-shell: bottom-anchored with a 90px margin.
No compositor window rule needed.

## Limitations

- **The pointer toggle applies to screenshots only.** `wf-recorder` has no
  cursor switch — it always draws the pointer — so the UI hides that row in
  video mode rather than offering a control that does nothing.
- Region and window select need `slurp`, so this is wlroots-only. It will not
  work on GNOME's Mutter or on KDE.
- `wf-recorder` records one output at a time; multi-monitor capture into a
  single file isn't supported.
- The audio source is whatever PipeWire/Pulse reports as default at the moment
  recording starts. Changing your default sink mid-recording won't follow.

## License

MIT — see [LICENSE](LICENSE).
