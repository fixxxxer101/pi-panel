#!/bin/sh
# Pi Panel installer: puts the dashboard on the panel at every boot.
#
#   - app        -> ~/.local/bin/pi-panel.py
#   - shortcut   -> ~/Desktop/pi-panel.desktop (double-click to launch
#                   or bring it back after closing it) plus an entry in
#                   ~/.local/share/applications for the menu
#   - autostart  -> a line in ~/.config/labwc/autostart
#   - and starts it right away (no reboot needed the first time)
#
# Safe to re-run: files are replaced, the autostart line is added once.
set -e
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
DESKTOP_DIR="$HOME/Desktop"
AUTOSTART="$HOME/.config/labwc/autostart"

mkdir -p "$BIN" "$APP_DIR" "$DESKTOP_DIR" "$HOME/.config/labwc"

cp "$SRC/pi-panel.py" "$BIN/pi-panel.py"
chmod +x "$BIN/pi-panel.py"
echo "installed $BIN/pi-panel.py"

cat > "$APP_DIR/pi-panel.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Pi Panel
Comment=Panel dashboard: CPU, temp, GPU, RAM, reboot/shutdown
Exec=python3 $BIN/pi-panel.py
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
EOF
cp "$APP_DIR/pi-panel.desktop" "$DESKTOP_DIR/pi-panel.desktop"
chmod +x "$APP_DIR/pi-panel.desktop" "$DESKTOP_DIR/pi-panel.desktop"
echo "installed desktop shortcut $DESKTOP_DIR/pi-panel.desktop"

touch "$AUTOSTART"
if grep -q 'pi-panel\.py' "$AUTOSTART"; then
  echo "autostart line already present"
else
  printf 'python3 %s/pi-panel.py >/dev/null 2>&1 &\n' "$BIN" >> "$AUTOSTART"
  echo "added Pi Panel to labwc autostart"
fi

if pgrep -f 'pi-panel\.py' >/dev/null 2>&1; then
  echo "Pi Panel is already running"
else
  nohup python3 "$BIN/pi-panel.py" >/dev/null 2>&1 &
  echo "Pi Panel started - look at the small screen"
fi
echo "done. It will come up by itself on every boot."
