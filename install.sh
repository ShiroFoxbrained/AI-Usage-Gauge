#!/usr/bin/env bash
# Adds AI-Usage-Gauge to your application menu, and (with --autostart) starts
# it in the system tray when you log in. Run it from the folder you want to
# keep the app in; the launchers point at this folder.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
VER="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$DIR/claude_usage.py")"

python3 -c 'import PyQt6' 2>/dev/null || {
  echo "PyQt6 is missing. Install it first, e.g.:"
  echo "  Arch/CachyOS:   sudo pacman -S python-pyqt6"
  echo "  Debian/Ubuntu:  sudo apt install python3-pyqt6"
  echo "  Fedora:         sudo dnf install python3-pyqt6"
  echo "  any distro:     pip install --user PyQt6"
  exit 1
}

entry() {  # entry <file> <extra Exec args>
  mkdir -p "$(dirname "$1")"
  cat > "$1" <<DESKTOP
[Desktop Entry]
Type=Application
Name=AI-Usage-Gauge $VER
Comment=Claude Code plan limits and local usage history
Exec=python3 "$DIR/claude_usage.py"$2
Icon=$DIR/icon.png
Terminal=false
Categories=Utility;
StartupWMClass=AI-Usage-Gauge
DESKTOP
  echo "wrote $1"
}

entry "$HOME/.local/share/applications/claude-usage.desktop" ""
if [[ "${1:-}" == "--autostart" ]]; then
  entry "$HOME/.config/autostart/claude-usage-tray.desktop" " --tray"
fi
echo "Done. Open \"AI-Usage-Gauge\" from your app menu."
