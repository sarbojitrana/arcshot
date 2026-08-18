#!/usr/bin/env bash
#
#   arcshot installer
#
#     ./install.sh              install for the current user (~/.local)
#     sudo ./install.sh --system install system-wide (/usr/local)
#     ./install.sh --uninstall  remove
#
set -euo pipefail

PREFIX="$HOME/.local"
SYSTEM=0
UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --system)    PREFIX=/usr/local; SYSTEM=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --prefix=*)  PREFIX="${a#--prefix=}" ;;
    -h|--help)   sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$PREFIX/bin"
LIB="$PREFIX/lib/arcshot"
APPS="$PREFIX/share/applications"

C=$'\e[38;2;0;217;255m'; G=$'\e[38;2;48;209;88m'; A=$'\e[38;2;255;176;0m'; Z=$'\e[0m'
ok(){ printf '  %s✓%s %s\n' "$G" "$Z" "$*"; }
warn(){ printf '  %s!%s %s\n' "$A" "$Z" "$*"; }

if (( SYSTEM )) && [[ $EUID -ne 0 ]]; then
  echo "--system needs root. Re-run with sudo." >&2; exit 1
fi

if (( UNINSTALL )); then
  rm -f  "$BIN/arcshot" "$APPS/arcshot.desktop"
  rm -rf "$LIB"
  ok "removed arcshot from $PREFIX"
  command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS" 2>/dev/null || true
  exit 0
fi

# ------------------------------------------------------------ dependencies
missing_hard=()
for b in python3 grim slurp; do command -v "$b" >/dev/null || missing_hard+=("$b"); done
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null \
  || missing_hard+=("python-gobject with GTK4 + libadwaita")
if (( ${#missing_hard[@]} )); then
  echo "Missing required dependencies: ${missing_hard[*]}" >&2
  echo "On Arch:  sudo pacman -S python-gobject gtk4 libadwaita grim slurp" >&2
  exit 1
fi

for b in wf-recorder wl-copy notify-send; do
  command -v "$b" >/dev/null || warn "$b not found — $(
    case $b in
      wf-recorder) echo 'screen RECORDING will not work';;
      wl-copy)     echo 'captures will not be copied to the clipboard';;
      notify-send) echo 'no desktop notifications';;
    esac)"
done

# ----------------------------------------------------------------- install
install -d "$BIN" "$LIB" "$APPS"
rm -rf "$LIB/arcshot"
cp -r "$SRC/src/arcshot" "$LIB/"
find "$LIB" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
install -m755 "$SRC/bin/arcshot" "$BIN/arcshot"
install -m644 "$SRC/data/arcshot.desktop" "$APPS/arcshot.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS" 2>/dev/null || true

ok "arcshot -> $BIN/arcshot"
ok "package -> $LIB/arcshot"
ok "launcher -> $APPS/arcshot.desktop"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "$BIN is not on your PATH — add it to your shell rc" ;;
esac

cat <<TIP

  Bind it to Print in Hyprland (~/.config/hypr/conf/custom.conf):

    bind = , Print, exec, arcshot

  The same key stops an active recording, so one key does both.

TIP
