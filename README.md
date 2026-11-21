# Pi Panel

A touch dashboard for the MHS-3.5" TFT (480x320) on the Raspberry Pi 5
running Wayland/labwc with the panel as its own DRM output (SPI-1) —
for example with the MHS35 DRM setup from
[fixxxxer101/LCD-show](https://github.com/fixxxxer101/LCD-show)
(upstream PR: [goodtft/LCD-show#434](https://github.com/goodtft/LCD-show/pull/434)).

## What it shows

Four tiles, refreshed every second:

- **CPU LOAD** — % busy from `/proc/stat`, with the current CPU clock
- **CHIP TEMP** — SoC temperature (`/sys/class/thermal`, fallback `vcgencmd`)
- **GPU** — the best figure the Pi actually offers, detected at startup:
  a real load % from the v3d devfreq node if the kernel exposes one,
  otherwise the V3D clock from `vcgencmd` (bar = share of the fastest
  clock seen this session). The tile label says which one it is.
- **RAM USED** — % and GB from `/proc/meminfo`

Header shows the hostname and the time; the ✕ in the corner closes the
app (reopen it from the desktop's Pi Panel shortcut, or it returns at
the next boot).

## Buttons

**REBOOT** and **SHUTDOWN** are two-step: tapping one opens a full
confirm screen with big YES / CANCEL buttons, and the confirm screen
cancels itself after 15 seconds. The actions run `systemctl reboot` /
`systemctl poweroff`; a failure is shown on screen and written to
`~/.local/state/pi-panel.log`.

## Placement

The app asks `wlr-randr` where the SPI panel is and pins itself there
(under XWayland it can position its own window in layout coordinates).
Nothing is hardcoded: panel-only boot puts it at 0,0, desk setup puts it
beside the HDMI screen, and it re-checks every 5 seconds and follows
the panel if the layout changes mid-session. It never opens on the
HDMI screen.

## Install

On the Pi:

```
git clone https://github.com/fixxxxer101/pi-panel.git
cd pi-panel && bash install.sh
```

That installs the app to `~/.local/bin`, puts a **Pi Panel shortcut on
the desktop** (double-click it any time to launch the dashboard or
bring it back after closing it), adds one line to the labwc autostart
(so it starts on boot), and starts it immediately.

Updates:

```
cd ~/pi-panel && git pull && bash install.sh
```

Re-running the installer is safe.

## Uninstall

```
rm ~/.local/bin/pi-panel.py ~/.local/share/applications/pi-panel.desktop ~/Desktop/pi-panel.desktop
sed -i '/pi-panel\.py/d' ~/.config/labwc/autostart
```

## Notes

- Pure Python 3 + Tkinter — both ship with Raspberry Pi OS; no pip
  installs, no extra services.
- Single instance: a second launch exits quietly (flock on
  `~/.local/state/pi-panel.lock`).
- Sensor or layout errors are logged, never fatal — the dashboard
  stays up.
- Also included in the LCD-show fork under `pi-panel/` — same app,
  two homes.
