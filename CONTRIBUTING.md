# Contributing

Thanks for taking an interest. Bug reports, patches and ideas are all welcome.

Read [AGENTS.md](AGENTS.md) first — it documents the non-obvious constraints
(layer-shell, `LD_PRELOAD`, why selection is drawn in-process rather than by
`slurp`). Most surprising-looking code is load-bearing.

## Signed commits are required

**Every commit must carry a verified signature.** Unsigned commits will not be
merged, and `main` is protected to reject them.

Set it up once:

```bash
# GPG
gpg --quick-generate-key "Your Name <you@example.com>" ed25519 sign 1y
gpg --list-secret-keys --keyid-format=long          # note the key id
git config --global user.signingkey <KEY_ID>
git config --global commit.gpgsign true
gpg --armor --export <KEY_ID>                       # add this to GitHub
```

Or SSH, which is simpler if you already push over SSH:

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
```

Then add the key to GitHub under *Settings → SSH and GPG keys* — as a
**signing** key, not just an authentication key. Check your work with:

```bash
git log --format='%h %G? %s'      # G = good signature
```

`G` means good, `N` means unsigned. Note that `git rebase` and especially
`git filter-branch` **drop signatures** — if you rewrite history, re-sign
before pushing:

```bash
git rebase --root --exec 'git commit --amend --no-edit -S'
```

## Development setup

```bash
git clone https://github.com/sarbojitrana/arcshot.git
cd arcshot
./install.sh          # installs to ~/.local
```

Run from the checkout without installing via `./bin/arcshot`. The launcher
resolves the package from either location.

Dependencies are listed in the README; `install.sh` checks them and tells you
which optional features you would be missing before writing anything.

## Testing your change

No unit tests — it is a GUI bound to a compositor. Verify against observable
state rather than by eye where you can:

```bash
./bin/arcshot --status                  # fast path, no GTK
hyprctl layers | grep arcshot           # surface mapped? what size?
ffprobe -v error -show_entries format=duration -of default=nw=1 out.mp4
```

Please say in the PR which compositor and version you tested on. Region and
window selection depend on wlroots; the project does not work on Mutter or
KWin, and patches that assume otherwise cannot be verified.

## Commit messages

One short subject line, imperative mood, no body. Aim for ~50 characters.

```
Add pointer toggle; close chooser before capture
```

Explanatory prose belongs in code comments or in `AGENTS.md`, where it stays
next to the thing it explains.

## Pull requests

- One change per PR.
- Say what you tested and on what.
- If you changed anything in the layer-shell, stacking or capture path, say how
  you confirmed the toolbar still stays out of the screenshot.

## Reporting bugs

Include:

- compositor and version (`hyprctl version`)
- `arcshot --status` output
- whether `wf-recorder`, `grim`, `slurp` and `gtk4-layer-shell` are installed
- for a blank or missing overlay: `hyprctl layers | grep arcshot`, and check
  whether a stale instance is holding the bus name:
  `gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.ListNames | tr ',' '\n' | grep arcshot`

## License

By contributing you agree that your work is licensed under the MIT License, as
in [LICENSE](LICENSE).
