# Arcshot

A small screenshot and screen-recording chooser for **Hyprland** and other
wlroots compositors. Press your capture key, pick what you want, done — and it
remembers what you picked last time.

![preview](preview.png)

It is the GNOME screenshot flow, on Wayland, without GNOME.

---

## What it does

- **One key for everything.** Press it to open the chooser. Press it *again
  while recording* and the recording stops — no second binding, no hunting for
  a tray icon.
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
bind = , Print, exec, arcshot
```

## Requirements

| | |
|---|---|
| **Required** | `python-gobject` (GTK 4 + libadwaita), `grim`, `slurp` |
| **Recording** | `wf-recorder` |
| **Clipboard** | `wl-clipboard` |
| **Notifications** | `libnotify` |
| **Annotation** (optional) | `swappy` |

On Arch:

```bash
sudo pacman -S python-gobject gtk4 libadwaita grim slurp wf-recorder wl-clipboard libnotify swappy
```

The installer checks all of these and tells you which optional features you'd
be missing before it writes anything.

## Command line

The GUI is optional — every mode has a flag, so you can bind them directly.

```
arcshot                 open the chooser, or stop an active recording
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

**The window is closed, not hidden, before capturing.** The application is held
open with `Gtk.Application.hold()` so it survives its own window going away.
Merely hiding the surface left it visible for a frame or two, which looked like
the dialog lingering during region select.

**Placement is left to the compositor.** Wayland clients cannot position
themselves, so put it where you want with a rule. For bottom-centre in
Hyprland:

```
windowrule {
    match:class = (dev.arcshot.Arcshot)
    float = true
    move = 744 674
}
```

Note that in Hyprland 0.56's keyed rule syntax a percentage `move` silently
ignores the Y component, and the `50%-190` arithmetic form only applies X —
absolute pixels apply on both axes.

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
