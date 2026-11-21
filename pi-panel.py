#!/usr/bin/env python3
"""Pi Panel - a touch dashboard for the MHS-3.5" TFT on Raspberry Pi.

Shows CPU load, SoC temperature, GPU (best source the Pi offers) and RAM,
with big touch buttons for reboot and shutdown (via a confirm screen).
Designed for the 480x320 landscape panel (output SPI-1): it finds the
panel's live position with wlr-randr and pins itself there, follows the
panel if the layout changes (hotplug), and is started from the labwc
autostart by install.sh. Runs under XWayland so it can place its own
window in the global layout coordinates.

Stat sources (all standard Raspberry Pi OS, no extra packages):
  CPU load   /proc/stat deltas
  CPU clock  /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
  SoC temp   /sys/class/thermal/thermal_zone0/temp (fallback: vcgencmd)
  RAM        /proc/meminfo (MemTotal / MemAvailable)
  GPU        first available of:
               1. /sys/class/devfreq/*v3d*/load   (real load %)
               2. vcgencmd measure_clock v3d      (clock, bar vs session max)
             The tile label says which one it is - never a made-up %.

Power actions use `systemctl reboot` / `systemctl poweroff` (logind
allows the active local session). Failures are shown on the confirm
screen and logged to ~/.local/state/pi-panel.log.

Usage: pi-panel.py [--selftest]
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import time

STATE_DIR = os.path.expanduser("~/.local/state")
LOG_FILE = os.path.join(STATE_DIR, "pi-panel.log")
LOCK_FILE = os.path.join(STATE_DIR, "pi-panel.lock")

PANEL_W, PANEL_H = 480, 320
REFRESH_MS = 1000          # stats refresh
REPOS_MS = 5000            # panel-position re-check
CONFIRM_TIMEOUT_MS = 15000

# ---------------------------------------------------------------- probes

def read_cpu_times():
    with open("/proc/stat") as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def cpu_percent(prev):
    total, idle = read_cpu_times()
    if prev is None:
        return None, (total, idle)
    dt = total - prev[0]
    di = idle - prev[1]
    if dt <= 0:
        return None, (total, idle)
    return 100.0 * (1.0 - di / dt), (total, idle)


def read_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        pass
    if shutil.which("vcgencmd"):
        try:
            out = subprocess.run(["vcgencmd", "measure_temp"],
                                 capture_output=True, text=True, timeout=2)
            m = re.search(r"temp=([\d.]+)", out.stdout)
            if m:
                return float(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def read_ram():
    total = avail = None
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1])
            if total and avail:
                break
    if not total or avail is None:
        return None
    used = total - avail
    return used / total * 100.0, used / 1048576.0, total / 1048576.0


def read_cpu_clock_ghz():
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
            return int(f.read().strip()) / 1e6
    except (OSError, ValueError):
        return None


class GpuSource:
    """Detects the best GPU metric the machine offers, once."""

    def __init__(self):
        self.kind = None          # 'load' | 'clock' | None
        self.load_path = None
        self.session_max_hz = 0
        for d in sorted(glob.glob("/sys/class/devfreq/*")):
            if "v3d" not in os.path.basename(d).lower():
                continue
            p = os.path.join(d, "load")
            if os.path.exists(p):
                self.kind, self.load_path = "load", p
                break
        if self.kind is None and shutil.which("vcgencmd"):
            self.kind = "clock"

    @property
    def label(self):
        return {"load": "GPU LOAD", "clock": "GPU CLOCK"}.get(self.kind, "GPU")

    def read(self):
        """Return (value_text, fraction_or_None)."""
        if self.kind == "load":
            try:
                with open(self.load_path) as f:
                    m = re.match(r"\s*(\d+)", f.read())
                if m:
                    v = min(100, int(m.group(1)))
                    return "%d%%" % v, v / 100.0
            except OSError:
                pass
            return "--", None
        if self.kind == "clock":
            try:
                out = subprocess.run(["vcgencmd", "measure_clock", "v3d"],
                                     capture_output=True, text=True, timeout=2)
                m = re.search(r"=(\d+)", out.stdout)
                if m:
                    hz = int(m.group(1))
                    self.session_max_hz = max(self.session_max_hz, hz)
                    frac = hz / self.session_max_hz if self.session_max_hz else None
                    return "%d MHz" % round(hz / 1e6), frac
            except (OSError, subprocess.SubprocessError):
                pass
            return "--", None
        return "n/a", None


def panel_geometry():
    """Live (x, y, w, h) of the SPI panel from wlr-randr, or None.

    Logical size accounts for the compositor transform (the panel's only
    mode is 320x480 portrait; transform 90 makes it 480x320 logical).
    """
    if not shutil.which("wlr-randr"):
        return None
    try:
        out = subprocess.run(["wlr-randr"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    block = None
    for chunk in re.split(r"\n(?=\S)", out):
        if chunk.startswith("SPI-"):
            block = chunk
            break
    if block is None:
        return None
    # a disabled output lists no Position and no modes -> parse fails -> None
    pos = re.search(r"Position:\s*(\d+),(\d+)", block)
    mode = re.search(r"(\d+)x(\d+) px[^\n]*\(current\)", block)
    if not pos or not mode:
        # fall back to the first listed mode if none is flagged current
        mode = re.search(r"(\d+)x(\d+) px", block)
        if not pos or not mode:
            return None
    w, h = int(mode.group(1)), int(mode.group(2))
    tr = re.search(r"Transform:\s*(\d+)", block)
    if tr and int(tr.group(1)) in (90, 270):
        w, h = h, w
    return int(pos.group(1)), int(pos.group(2)), w, h


def log(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass

# ---------------------------------------------------------------- selftest

def selftest():
    prev = None
    pct, prev = cpu_percent(prev)
    time.sleep(0.3)
    pct, prev = cpu_percent(prev)
    print("cpu_percent:", None if pct is None else round(pct, 1))
    print("cpu_clock_ghz:", read_cpu_clock_ghz())
    print("temp_c:", read_temp_c())
    print("ram:", read_ram())
    gpu = GpuSource()
    print("gpu kind:", gpu.kind, "| label:", gpu.label, "| read:", gpu.read())
    print("panel_geometry:", panel_geometry())
    return 0

# ---------------------------------------------------------------- app

BG = "#101418"
TILE = "#1a2129"
TEXT = "#e8eef4"
MUTED = "#8fa3b5"
GREEN = "#57d977"
AMBER = "#f5b942"
RED = "#f0564a"
ACCENT = "#4da3ff"
FONT = "DejaVu Sans"


def bar_color(kind, frac):
    if frac is None:
        return ACCENT
    if kind == "temp":
        return GREEN if frac < 65 else (AMBER if frac < 80 else RED)
    return GREEN if frac < 0.70 else (AMBER if frac < 0.90 else RED)


class Tile:
    def __init__(self, parent, label, row, col):
        import tkinter as tk
        self.frame = frame = tk.Frame(parent, bg=TILE, padx=12, pady=6)
        frame.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
        tk.Label(frame, text=label, bg=TILE, fg=MUTED,
                 font=(FONT, 10, "bold"), anchor="w").pack(fill="x")
        self.value = tk.Label(frame, text="--", bg=TILE, fg=TEXT,
                              font=(FONT, 26, "bold"), anchor="w")
        self.value.pack(fill="x")
        self.sub = tk.Label(frame, text="", bg=TILE, fg=MUTED,
                            font=(FONT, 9), anchor="w")
        self.sub.pack(fill="x")
        self.track = tk.Frame(frame, bg="#0b0e11", height=7)
        self.track.pack(fill="x", side="bottom", pady=(6, 0))
        self.track.pack_propagate(False)
        self.fill = tk.Frame(self.track, bg=ACCENT)
        self.fill.place(x=0, y=0, relwidth=0, relheight=1)

    def set(self, value_text, sub_text, frac, color):
        self.value.config(text=value_text)
        self.sub.config(text=sub_text)
        if frac is None:
            self.fill.place_configure(relwidth=0)
        else:
            self.fill.place_configure(relwidth=max(0.0, min(1.0, frac)))
            self.fill.config(bg=color)


def press_button(parent, command, **kw):
    """A button that fires on PRESS, not on release.

    This panel's resistive touch drifts during a press-and-hold, so a
    release can land off the button and a normal click reads as
    cancelled (verified live: mouse clicks worked, touch did nothing).
    Every Pi Panel button therefore triggers on ButtonPress-1 and has
    no release command at all - exactly one trigger path.
    """
    import tkinter as tk
    b = tk.Button(parent, **kw)
    b.bind("<ButtonPress-1>", lambda e: command())
    return b


class App:
    def __init__(self, root):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.cpu_prev = None
        self.gpu = GpuSource()
        self.geo = None
        self.confirm_action = None
        self._confirm_after = None

        root.title("Pi Panel")
        root.configure(bg=BG)
        root.overrideredirect(True)
        # size BEFORE any widgets exist: Tk otherwise maps the window at
        # the content's natural size and the panel size never wins
        geo0 = panel_geometry() or (0, 0, PANEL_W, PANEL_H)
        self.geo = geo0
        root.geometry("%dx%d+%d+%d" % (geo0[2], geo0[3], geo0[0], geo0[1]))
        root.minsize(geo0[2], geo0[3])
        root.maxsize(geo0[2], geo0[3])

        main = tk.Frame(root, bg=BG)
        main.place(x=0, y=0, relwidth=1, relheight=1)
        self.main = main

        # header: title left, date/time + close right. The bar height is
        # locked at 32px (pack_propagate off): the bigger close glyph
        # fits inside it without growing the bar.
        header = tk.Frame(main, bg=BG, height=32)
        header.pack(fill="x", padx=10, pady=(6, 0))
        header.pack_propagate(False)
        tk.Label(header, text="Pi 5 Panel By: Trinity Nexus", bg=BG,
                 fg=MUTED, font=(FONT, 11, "bold")).pack(side="left")
        # the destroy is deferred 50ms so the press event finishes
        # dispatching before the window goes away
        press_button(header, lambda: root.after(50, root.destroy),
                     text="✕", bg=BG, fg=MUTED, activebackground=BG,
                     activeforeground=TEXT, bd=0, highlightthickness=0,
                     font=(FONT, 19, "bold"), padx=3, pady=0,
                     width=2).pack(side="right")
        self.clock = tk.Label(header, text="", bg=BG, fg=MUTED,
                              font=(FONT, 10, "bold"), anchor="e")
        self.clock.pack(side="right", padx=(0, 4))

        # power buttons FIRST (packed to the bottom): pack gives space in
        # call order, so the buttons can never be starved by the grid
        row = tk.Frame(main, bg=BG)
        row.pack(side="bottom", fill="x", padx=10, pady=(2, 10))
        press_button(row, lambda: self.ask("reboot"),
                     text="REBOOT", bg="#2a3542", fg=TEXT,
                     activebackground="#3a4a5c", activeforeground=TEXT,
                     bd=0, highlightthickness=0, font=(FONT, 15, "bold"),
                     height=1).pack(side="left", fill="x", expand=True,
                                    padx=(0, 5))
        press_button(row, lambda: self.ask("poweroff"),
                     text="SHUTDOWN", bg="#5a2622", fg=TEXT,
                     activebackground="#74322c", activeforeground=TEXT,
                     bd=0, highlightthickness=0, font=(FONT, 15, "bold"),
                     height=1).pack(side="left", fill="x", expand=True,
                                    padx=(5, 0))

        # stat tiles 2x2 fill whatever height is left
        grid = tk.Frame(main, bg=BG)
        grid.pack(fill="both", expand=True, padx=6, pady=4)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        grid.rowconfigure(0, weight=1)
        grid.rowconfigure(1, weight=1)
        self.t_cpu = Tile(grid, "CPU LOAD", 0, 0)
        self.t_temp = Tile(grid, "CHIP TEMP", 0, 1)
        self.t_gpu = Tile(grid, self.gpu.label, 1, 0)
        self.t_ram = Tile(grid, "RAM USED", 1, 1)

        # confirm screen (covers everything)
        conf = tk.Frame(root, bg=BG)
        self.confirm = conf
        self.confirm_text = tk.Label(conf, text="", bg=BG, fg=TEXT,
                                     font=(FONT, 21, "bold"))
        self.confirm_text.pack(pady=(64, 8))
        self.confirm_msg = tk.Label(conf, text="", bg=BG, fg=AMBER,
                                    font=(FONT, 12))
        self.confirm_msg.pack(pady=(0, 14))
        btns = tk.Frame(conf, bg=BG)
        btns.pack()
        self.confirm_yes = press_button(btns, self.do_power,
                                        text="YES", bg=RED, fg="white",
                                        activebackground="#c23e35",
                                        activeforeground="white", bd=0,
                                        highlightthickness=0,
                                        font=(FONT, 18, "bold"), width=9,
                                        height=2)
        self.confirm_yes.pack(side="left", padx=8)
        press_button(btns, self.hide_confirm,
                     text="CANCEL", bg="#2a3542", fg=TEXT,
                     activebackground="#3a4a5c", activeforeground=TEXT,
                     bd=0, highlightthickness=0,
                     font=(FONT, 18, "bold"), width=9,
                     height=2).pack(side="left", padx=8)

        self.reposition(force=True)
        self.tick()
        self.root.after(REPOS_MS, self.reposition_loop)

    # placement ------------------------------------------------------
    def reposition(self, force=False):
        geo = panel_geometry()
        if geo is None:
            return
        if force or geo != self.geo:
            self.root.geometry("%dx%d+%d+%d" % (geo[2], geo[3], geo[0], geo[1]))
            self.root.minsize(geo[2], geo[3])
            self.root.maxsize(geo[2], geo[3])
            self.geo = geo

    def reposition_loop(self):
        try:
            self.reposition()
        except Exception as e:                       # never die on layout
            log("reposition error: %r" % e)
        self.root.after(REPOS_MS, self.reposition_loop)

    # stats ----------------------------------------------------------
    def tick(self):
        try:
            self.update_stats()
        except Exception as e:                       # never die on a sensor
            log("stats error: %r" % e)
        self.root.after(REFRESH_MS, self.tick)

    def update_stats(self):
        self.clock.config(text=time.strftime("%m/%d/%Y  %-I:%M %p"))
        pct, self.cpu_prev = cpu_percent(self.cpu_prev)
        ghz = read_cpu_clock_ghz()
        if pct is None:
            self.t_cpu.set("--", "", None, ACCENT)
        else:
            sub = "%.1f GHz" % ghz if ghz else ""
            self.t_cpu.set("%d%%" % round(pct), sub, pct / 100.0,
                           bar_color("pct", pct / 100.0))
        temp = read_temp_c()
        if temp is None:
            self.t_temp.set("--", "SoC", None, ACCENT)
        else:
            self.t_temp.set("%.0f°C" % temp, "SoC", temp / 100.0,
                            bar_color("temp", temp))
        text, frac = self.gpu.read()
        self.t_gpu.set(text, "", frac, bar_color("pct", frac))
        ram = read_ram()
        if ram is None:
            self.t_ram.set("--", "", None, ACCENT)
        else:
            pct_r, used_gb, total_gb = ram
            self.t_ram.set("%d%%" % round(pct_r),
                           "%.1f / %.1f GB" % (used_gb, total_gb),
                           pct_r / 100.0, bar_color("pct", pct_r / 100.0))

    # power ----------------------------------------------------------
    def ask(self, action):
        self.confirm_action = action
        word = "Reboot" if action == "reboot" else "Shut down"
        self.confirm_text.config(text="%s the Pi?" % word)
        self.confirm_msg.config(text="")
        self.confirm.place(x=0, y=0, relwidth=1, relheight=1)
        self.confirm.tkraise()
        self._confirm_after = self.root.after(CONFIRM_TIMEOUT_MS,
                                              self.hide_confirm)

    def hide_confirm(self):
        if self._confirm_after is not None:
            self.root.after_cancel(self._confirm_after)
            self._confirm_after = None
        self.confirm.place_forget()
        self.confirm_action = None

    def do_power(self):
        action = self.confirm_action
        if self._confirm_after is not None:
            self.root.after_cancel(self._confirm_after)
            self._confirm_after = None
        try:
            r = subprocess.run(["systemctl", action],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                self.confirm_text.config(text="Going down…")
                self.confirm_msg.config(text="")
                return
            log("systemctl %s failed rc=%d: %s"
                % (action, r.returncode, r.stderr.strip()))
            self.confirm_msg.config(
                text="That didn't work — logged to pi-panel.log")
        except (OSError, subprocess.SubprocessError) as e:
            log("systemctl %s error: %r" % (action, e))
            self.confirm_msg.config(
                text="That didn't work — logged to pi-panel.log")
        self.root.after(3000, self.hide_confirm)


def main():
    if "--selftest" in sys.argv:
        return selftest()
    # single instance: hold an flock for the life of the process
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    import tkinter as tk
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
