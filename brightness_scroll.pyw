# -*- coding: utf-8 -*-
"""
brightness_scroll.pyw
=====================

Scroll-wheel monitor brightness control for Windows 10 / 11.

Hover the small always-on-top window and roll the mouse wheel to change the
brightness of your monitor(s).

How it works
------------
External monitors are driven over **DDC/CI** (`dxva2.dll`, VCP code 0x10).
That is genuine hardware brightness - the exact setting the monitor's own OSD
buttons change - so it is stored by the monitor and survives reboots. Idle
laptop panels are usually reachable the same way.

If no DDC/CI-capable monitor is found (or `--gamma` is passed), the app falls
back to **software dimming** by rewriting the display's gamma ramp with
`gdi32.dll`. That stack works on virtually any display, but it does not change
the backlight and is reset when the display driver restarts.

No third-party packages required - standard library only.

Usage
-----
    pythonw brightness_scroll.pyw              # normal use (no console window)
    python  brightness_scroll.pyw              # with console, for debugging
    python  brightness_scroll.pyw --selftest   # probe backends and exit
    python  brightness_scroll.pyw --gamma      # force software dimming
    python  brightness_scroll.pyw --step 2     # 2% per wheel notch

Mouse:
    wheel over window .... +/- step (default 5%)
    shift + wheel ........ fine   (+/- 1%)
    ctrl  + wheel ........ coarse (+/- 10%)
    drag empty space ..... move the window
    drag the bar ......... jump straight to a value
    click the name ....... cycle: ALL monitors -> one monitor -> ...
    right-click .......... menu
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import Counter
from ctypes import POINTER, byref, wintypes

APP_NAME = "Brightness Scroll"
APP_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "BrightnessScroll"
)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# How long to wait for the wheel to stop spinning before pushing to hardware.
COALESCE_SECONDS = 0.06
# Minimum gap between two DDC/CI writes to the same monitor.
MIN_WRITE_INTERVAL = 0.09

FINE_STEP = 1
COARSE_STEP = 10

# Idle opacity keeps the widget out of the way; hover makes it readable.
IDLE_ALPHA = 0.88
HOVER_ALPHA = 0.98


# --------------------------------------------------------------------------
# Win32 plumbing
# --------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    POINTER(wintypes.RECT),
    wintypes.LPARAM,
)

user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC,
    POINTER(wintypes.RECT),
    MONITORENUMPROC,
    wintypes.LPARAM,
]
user32.EnumDisplayMonitors.restype = wintypes.BOOL

user32.EnumDisplayDevicesW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    POINTER(DISPLAY_DEVICEW),
    wintypes.DWORD,
]
user32.EnumDisplayDevicesW.restype = wintypes.BOOL

dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR,
    POINTER(wintypes.DWORD),
]
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HMONITOR,
    wintypes.DWORD,
    POINTER(PHYSICAL_MONITOR),
]
dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

dxva2.DestroyPhysicalMonitors.argtypes = [
    wintypes.DWORD,
    POINTER(PHYSICAL_MONITOR),
]
dxva2.DestroyPhysicalMonitors.restype = wintypes.BOOL

dxva2.GetMonitorBrightness.argtypes = [
    wintypes.HANDLE,
    POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD),
]
dxva2.GetMonitorBrightness.restype = wintypes.BOOL

dxva2.SetMonitorBrightness.argtypes = [wintypes.HANDLE, wintypes.DWORD]
dxva2.SetMonitorBrightness.restype = wintypes.BOOL

gdi32.CreateDCW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
]
gdi32.CreateDCW.restype = wintypes.HDC
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL
gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL

RAMP_TYPE = wintypes.WORD * 256 * 3

DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008


def clamp(value: int, low: int = 0, high: int = 100) -> int:
    return low if value < low else high if value > high else value


def enable_dpi_awareness() -> None:
    """Without this the window is blurry on scaled displays."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # per-monitor
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Backend 1: DDC/CI hardware brightness (preferred)
# --------------------------------------------------------------------------
class MonitorInfo:
    __slots__ = ("handle", "name", "minimum", "maximum")

    def __init__(self, handle, name, minimum, maximum):
        self.handle = handle
        self.name = name
        self.minimum = minimum
        self.maximum = maximum

    def percent_to_raw(self, percent: int) -> int:
        span = self.maximum - self.minimum
        return int(round(self.minimum + span * clamp(percent) / 100.0))


class DDCBackend:
    """Real monitor brightness through DDC/CI."""

    label = "DDC/CI hardware"
    writable = True

    def __init__(self):
        self.monitors: list[MonitorInfo] = []
        self.errors: list[str] = []
        self._arrays = []  # keep PHYSICAL_MONITOR arrays alive (owned handles)
        self.scan()

    # -- discovery ---------------------------------------------------------
    @staticmethod
    def _hmonitors():
        found = []

        @MONITORENUMPROC
        def _cb(hmon, _hdc, _rect, _data):
            found.append(hmon)
            return True

        user32.EnumDisplayMonitors(None, None, _cb, 0)
        return found

    def scan(self) -> None:
        self.release()
        self.monitors.clear()
        self.errors.clear()

        for index, hmon in enumerate(self._hmonitors()):
            count = wintypes.DWORD(0)
            if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                hmon, byref(count)
            ):
                continue
            if count.value == 0:
                continue

            array = (PHYSICAL_MONITOR * count.value)()
            if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, array):
                continue
            self._arrays.append(array)

            for position, phys in enumerate(array):
                minimum = wintypes.DWORD(0)
                current = wintypes.DWORD(0)
                maximum = wintypes.DWORD(0)
                if not dxva2.GetMonitorBrightness(
                    phys.hPhysicalMonitor,
                    byref(minimum),
                    byref(current),
                    byref(maximum),
                ):
                    continue
                if maximum.value <= minimum.value:
                    continue  # monitor reports brightness as unsupported
                name = phys.szPhysicalMonitorDescription.strip() or (
                    f"Display {index + 1}.{position}"
                )
                self.monitors.append(
                    MonitorInfo(
                        phys.hPhysicalMonitor, name, minimum.value, maximum.value
                    )
                )

    def release(self) -> None:
        for array in self._arrays:
            try:
                dxva2.DestroyPhysicalMonitors(len(array), array)
            except Exception:
                pass
        self._arrays.clear()

    # -- operations --------------------------------------------------------
    def get(self, index: int, samples: int = 3):
        """Read the current brightness as a percentage.

        DDC/CI is a low-speed serial link and over analog VGA it happily
        returns the occasional wrong number (26, 26, 21, ... for one unchanged
        setting), which would make the app start on a bogus value. So sample a
        few times and keep the most common result.
        """
        readings = []
        for attempt in range(max(1, samples)):
            if attempt:
                time.sleep(0.02)
            value = self._read_once(index)
            if value is not None:
                readings.append(value)
        if not readings:
            return None
        if len(readings) == 1:
            return readings[0]
        counts = Counter(readings)
        best = max(counts.values())
        candidates = sorted(value for value, count in counts.items() if count == best)
        return candidates[len(candidates) // 2]

    def _read_once(self, index: int):
        monitor = self.monitors[index]
        minimum = wintypes.DWORD(0)
        current = wintypes.DWORD(0)
        maximum = wintypes.DWORD(0)
        ok = dxva2.GetMonitorBrightness(
            monitor.handle, byref(minimum), byref(current), byref(maximum)
        )
        if not ok:
            return None
        if maximum.value > minimum.value:
            monitor.minimum = minimum.value
            monitor.maximum = maximum.value
        span = maximum.value - minimum.value
        if span <= 0:
            return None
        return clamp(int(round((current.value - minimum.value) * 100.0 / span)))

    def set(self, index: int, percent: int) -> bool:  # noqa: A003
        monitor = self.monitors[index]
        raw = monitor.percent_to_raw(percent)
        return bool(dxva2.SetMonitorBrightness(monitor.handle, raw))


# --------------------------------------------------------------------------
# Backend 2: gamma-ramp software dimming (fallback)
# --------------------------------------------------------------------------
class GammaBackend:
    """Software dimming. Any display works, but the backlight is untouched."""

    label = "software (gamma ramp)"
    writable = True

    def __init__(self):
        self.monitors: list[MonitorInfo] = []
        self.errors: list[str] = []
        self._devices: list[wintypes.HDC] = []
        self._originals: list[RAMP_TYPE] = []
        self._last: dict[int, int] = {}
        self.scan()

    def scan(self) -> None:
        self.release()
        self.monitors.clear()
        self.errors.clear()

        index = 0
        while True:
            device = DISPLAY_DEVICEW()
            device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            if not user32.EnumDisplayDevicesW(None, index, byref(device), 0):
                break
            index += 1
            flags = device.StateFlags
            if not flags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
                continue
            if flags & DISPLAY_DEVICE_MIRRORING_DRIVER:
                continue

            hdc = gdi32.CreateDCW(device.DeviceName, None, None, None)
            if not hdc:
                continue
            original = RAMP_TYPE()
            if not gdi32.GetDeviceGammaRamp(hdc, byref(original)):
                gdi32.DeleteDC(hdc)
                continue

            self._devices.append(hdc)
            self._originals.append(original)
            name = (device.DeviceString or device.DeviceName).strip()
            self.monitors.append(
                MonitorInfo(len(self.monitors), name, 0, 100)
            )

    def release(self) -> None:
        self.restore()
        for hdc in self._devices:
            try:
                gdi32.DeleteDC(hdc)
            except Exception:
                pass
        self._devices.clear()
        self._originals.clear()

    def restore(self) -> None:
        for hdc, original in zip(self._devices, self._originals):
            try:
                gdi32.SetDeviceGammaRamp(hdc, byref(original))
            except Exception:
                pass

    def get(self, index: int):
        # A gamma ramp cannot be read back as a brightness percentage, so the
        # app tracks its own value (100% = ramp left untouched).
        return self._last.get(index, 100)

    def set(self, index: int, percent: int) -> bool:  # noqa: A003
        if index >= len(self._devices):
            return False
        scale = max(0.05, clamp(percent) / 100.0)
        ramp = RAMP_TYPE()
        for channel in range(3):
            row = ramp[channel]
            for level in range(256):
                row[level] = int(
                    clamp_val(int(level / 255.0 * 65535.0 * scale), 0, 65535)
                )
        ok = bool(gdi32.SetDeviceGammaRamp(self._devices[index], byref(ramp)))
        if ok:
            self._last[index] = clamp(percent)
        return ok


def clamp_val(value, low, high):
    return low if value < low else high if value > high else value


# --------------------------------------------------------------------------
# Background writer: coalesces wheel bursts so slow DDC/CI links cope
# --------------------------------------------------------------------------
class Writer(threading.Thread):
    def __init__(self, backend, events: "queue.Queue"):
        super().__init__(daemon=True, name="brightness-writer")
        self.backend = backend
        self.events = events
        self._lock = threading.Lock()
        self._pending: dict[int, int] = {}
        self._last_written: dict[int, int] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()

    def request(self, index: int, percent: int) -> None:
        with self._lock:
            if self._last_written.get(index) == percent:
                return
            self._pending[index] = percent
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def forget(self) -> None:
        """Drop the de-duplication cache (monitor handles may have changed)."""
        with self._lock:
            self._last_written.clear()
            self._pending.clear()
            self._wake.clear()

    def run(self) -> None:
        last_time: dict[int, float] = {}
        while True:
            self._wake.wait()
            if self._stop.is_set():
                return
            time.sleep(COALESCE_SECONDS)

            with self._lock:
                batch = self._pending
                self._pending = {}
                self._wake.clear()

            for index, percent in batch.items():
                gap = MIN_WRITE_INTERVAL - (time.monotonic() - last_time.get(index, 0))
                if gap > 0:
                    time.sleep(gap)
                try:
                    ok = self.backend.set(index, percent)
                except Exception as exc:  # pragma: no cover - defensive
                    ok = False
                    self.events.put(("error", f"write error: {exc}"))
                last_time[index] = time.monotonic()
                if ok:
                    self._last_written[index] = percent
                else:
                    self.events.put(("error", "the monitor refused the write"))
                    # Do not let a failed value block the next attempt.
                    self._last_written.pop(index, None)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "x": None,
    "y": None,
    "step": 5,
    "gamma": False,
    # Off by default: the wheel works on hover without focus, so there is no
    # reason to pull focus away from whatever you are actually working in.
    "focus_on_hover": False,
    "active": None,
}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            config.update({k: v for k, v in stored.items() if k in config})
    except Exception:
        pass
    return config


def save_config(config: dict) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Startup shortcut (HKCU Run key -- no admin needed, no console flash)
# --------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "BrightnessScroll"


def startup_script_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "brightness_scroll.pyw")


def startup_command() -> str:
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{startup_script_path()}"'


def startup_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return bool(value)
    except OSError:
        return False


def set_startup(enabled: bool) -> bool:
    import winreg

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except OSError:
                    pass
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------
BG_BORDER = "#39404d"
BG_BORDER_HOT = "#6ea8fe"
BG_CARD = "#1c1f26"
FG = "#e9edf5"
FG_DIM = "#8a93a3"
ACCENT = "#ffb057"
TRACK = "#2c313c"
ERR = "#ff6b6b"

FONT = "Segoe UI"


# --------------------------------------------------------------------------
# The widget
# --------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk, backend, config: dict, args) -> None:
        self.root = root
        self.backend = backend
        self.config = config
        self.args = args
        self.step = args.step if args.step is not None else config.get("step", 5)

        count = len(backend.monitors)
        self.count = count
        self.values: list[int] = [100] * count
        self.known: list[bool] = [False] * count

        saved_active = args.active if args.active is not None else config.get("active")
        if saved_active is None or not (0 <= int(saved_active) < count):
            saved_active = 0 if count == 1 else None
        self.active: int | None = saved_active

        self.events: "queue.Queue" = queue.Queue()
        self.writer = Writer(backend, self.events)
        self.writer.start()

        self.message = ""
        self._is_error = False
        self.message_until = 0.0
        self._drag_origin = None
        self._bar_drag = False
        self._inside = False
        self._rescan_index = 1

        self._scale = self._dpi_scale()
        self._build_ui()
        self._read_initial_values()
        self._poll_events()
        self.root.after(110, self._watch_hover)
        self.root.after(400, self._refresh_footer)

    # -- helpers -----------------------------------------------------------
    def _dpi_scale(self) -> float:
        try:
            self.root.update_idletasks()
            return max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except Exception:
            return 1.0

    def px(self, value: float) -> int:
        return max(1, int(round(value * self._scale)))

    # -- construction ------------------------------------------------------
    def _build_ui(self) -> None:
        root = self.root
        root.title(APP_NAME)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG_BORDER)
        try:
            root.attributes("-alpha", IDLE_ALPHA)
        except Exception:
            pass

        pad = self.px(13)
        bar_w = self.px(224)
        bar_h = self.px(9)

        self.outer = tk.Frame(root, bg=BG_BORDER, bd=0)
        self.outer.pack(fill="both", expand=True)

        self.card = tk.Frame(self.outer, bg=BG_CARD, bd=0)
        self.card.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(self.card, bg=BG_CARD)
        header.pack(fill="x", padx=pad, pady=(self.px(10), 0))

        tk.Label(
            header, text="BRIGHTNESS", bg=BG_CARD, fg=FG_DIM,
            font=(FONT, 8, "bold"),
        ).pack(side="left")

        self.close_label = tk.Label(
            header, text="\u00d7", bg=BG_CARD, fg=FG_DIM,
            font=(FONT, 12), cursor="hand2", padx=self.px(4),
        )
        self.close_label.pack(side="right")

        self.value_label = tk.Label(
            self.card, text="\u2013\u2013", bg=BG_CARD, fg=FG,
            font=(FONT, 25, "bold"),
        )
        self.value_label.pack(pady=(self.px(1), 0))
        self.value_label.bind("<Double-Button-1>", lambda _e: self.set_all(50))

        self.bar = tk.Canvas(
            self.card, width=bar_w, height=bar_h, bg=BG_CARD,
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self.bar.pack(padx=pad, pady=(self.px(7), 0))
        self._bar_w = bar_w
        self._bar_h = bar_h
        self._track = self.bar.create_rectangle(
            0, 0, bar_w, bar_h, fill=TRACK, outline=""
        )
        self._fill = self.bar.create_rectangle(
            0, 0, 0, bar_h, fill=ACCENT, outline=""
        )
        self.bar.bind("<Button-1>", self._bar_press)
        self.bar.bind("<B1-Motion>", self._bar_move)
        self.bar.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_bar_drag", False))

        self.footer = tk.Label(
            self.card, text="", bg=BG_CARD, fg=FG_DIM,
            font=(FONT, 8), cursor="hand2",
        )
        self.footer.pack(padx=pad, pady=(self.px(6), self.px(10)))
        self.footer.bind("<Button-1>", self._cycle_active)

        # -- bindings ------------------------------------------------------
        root.bind_all("<MouseWheel>", self.on_wheel)
        for widget in (self.outer, self.card, header, self.value_label, self.footer):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Button-3>", self._show_menu)

        self.close_label.bind("<Button-1>", lambda _e: self.quit())

        root.bind("<Up>", lambda _e: self.nudge(+self.step))
        root.bind("<Down>", lambda _e: self.nudge(-self.step))
        root.bind("<Prior>", lambda _e: self.nudge(+COARSE_STEP))
        root.bind("<Next>", lambda _e: self.nudge(-COARSE_STEP))
        root.bind("<Home>", lambda _e: self.set_all(0))
        root.bind("<End>", lambda _e: self.set_all(100))

        self._build_menu()

        # Size the window to its content, then place it.
        root.update_idletasks()
        width = self.outer.winfo_reqwidth()
        height = self.outer.winfo_reqheight()
        x, y = self._initial_position(width, height)
        root.geometry(f"{width}x{height}+{x}+{y}")
        self.width, self.height = width, height

    def _initial_position(self, width: int, height: int):
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        if self.args.x is not None and self.args.y is not None:
            x, y = self.args.x, self.args.y
        elif self.config.get("x") is not None and self.config.get("y") is not None:
            x, y = int(self.config["x"]), int(self.config["y"])
        else:
            x = screen_w - width - self.px(18)
            y = (screen_h - height) // 2
        x = clamp_val(x, -self.px(40), max(0, screen_w - self.px(60)))
        y = clamp_val(y, 0, max(0, screen_h - self.px(30)))
        return int(x), int(y)

    def _build_menu(self) -> None:
        self.menu = tk.Menu(self.root, tearoff=0, font=(FONT, 9))
        self.menu.add_command(
            label="Hover this window, then scroll the wheel",
            state="disabled",
        )
        self.menu.add_separator()
        self.menu.add_command(label="Reset to 50%", command=lambda: self.set_all(50))
        self._rescan_index = 3
        self.menu.add_command(
            label=f"Rescan monitors  ({self.count} found)", command=self.rescan
        )
        self.menu.add_separator()

        self.step_var = tk.IntVar(value=self.step)
        for name, value in (("fine", FINE_STEP), ("normal", 5), ("coarse", COARSE_STEP)):
            self.menu.add_radiobutton(
                label=f"Step: {name}  ({value}%)",
                variable=self.step_var,
                value=value,
                command=self._on_step_change,
            )

        self.menu.add_separator()
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        self.menu.add_checkbutton(
            label="Run at login", variable=self.startup_var, command=self._toggle_startup
        )
        self.focus_var = tk.BooleanVar(value=bool(self.config.get("focus_on_hover", False)))
        self.menu.add_checkbutton(
            label="Take focus on hover (for arrow keys)", variable=self.focus_var,
            command=self._toggle_focus,
        )
        self.menu.add_separator()
        self.menu.add_command(
            label=f"Brightness source: {self.backend.label}", state="disabled"
        )
        self.menu.add_command(label="Exit", command=self.quit)

    # -- focus / hover polish ---------------------------------------------
    def _pointer_inside(self) -> bool:
        try:
            px, py = self.root.winfo_pointerxy()
            x, y = self.root.winfo_x(), self.root.winfo_y()
        except Exception:
            return False
        return x <= px < x + self.width and y <= py < y + self.height

    def _watch_hover(self) -> None:
        """Polled instead of using <Enter>/<Leave>: those fire for every child
        widget, so a card that fills the window makes the border flicker."""
        inside = self._pointer_inside()
        if inside != self._inside:
            self._inside = inside
            self.outer.configure(bg=BG_BORDER_HOT if inside else BG_BORDER)
            try:
                self.root.attributes("-alpha", HOVER_ALPHA if inside else IDLE_ALPHA)
            except Exception:
                pass
            if inside and self.config.get("focus_on_hover", False):
                try:
                    self.root.focus_force()
                except Exception:
                    pass
        self.root.after(110, self._watch_hover)

    # -- window dragging ---------------------------------------------------
    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root,
                             self.root.winfo_x(), self.root.winfo_y())
        try:
            self.root.focus_force()
        except Exception:
            pass

    def _drag_move(self, event) -> None:
        if not self._drag_origin:
            return
        x0, y0, wx, wy = self._drag_origin
        self.root.geometry(f"+{wx + event.x_root - x0}+{wy + event.y_root - y0}")

    def _drag_end(self, _event=None) -> None:
        self._drag_origin = None
        self._remember_position()

    def _remember_position(self) -> None:
        self.config["x"] = self.root.winfo_x()
        self.config["y"] = self.root.winfo_y()
        save_config(self.config)

    # -- bar ---------------------------------------------------------------
    def _bar_value(self, event) -> int:
        ratio = clamp_val(event.x / float(self._bar_w), 0.0, 1.0)
        return int(round(ratio * 100))

    def _bar_press(self, event) -> None:
        self._bar_drag = True
        self.set_all(self._bar_value(event))

    def _bar_move(self, event) -> None:
        if self._bar_drag:
            self.set_all(self._bar_value(event))

    # -- brightness logic --------------------------------------------------
    def target_indices(self) -> list:
        if self.active is None:
            return list(range(self.count))
        return [self.active]

    def _effective_active(self):
        if self.active is not None:
            return self.active
        return 0 if self.count else None

    def on_wheel(self, event) -> str:
        if self.count == 0:
            return "break"
        if event.delta == 0:
            return "break"
        state = getattr(event, "state", 0)
        step = self.step
        if state & 0x0001:      # shift
            step = FINE_STEP
        elif state & 0x0004:    # control
            step = COARSE_STEP
        self.nudge(step if event.delta > 0 else -step)
        return "break"

    def nudge(self, delta: int) -> None:
        for index in self.target_indices():
            base = self.values[index] if self.known[index] else 50
            self._apply(index, clamp(base + delta))

    def set_all(self, percent: int) -> None:
        for index in self.target_indices():
            self._apply(index, clamp(percent))

    def _apply(self, index: int, percent: int) -> None:
        self.values[index] = percent
        self.known[index] = True
        self.writer.request(index, percent)
        self._refresh_display()
        self._persist_active()

    def _persist_active(self) -> None:
        if self.config.get("active") != self.active:
            self.config["active"] = self.active
            save_config(self.config)

    # -- monitor selection -------------------------------------------------
    def _cycle_active(self, _event=None) -> None:
        if self.count <= 1:
            return
        if self.active is None:
            self.active = 0
        elif self.active + 1 >= self.count:
            self.active = None
        else:
            self.active += 1
        self._refresh_display()
        self._persist_active()
        self._refresh_footer()
        self._flash("target: " + ("all monitors" if self.active is None
                                  else self.backend.monitors[self.active].name))

    def _set_step(self, step: int) -> None:
        self.step = step
        self.config["step"] = step
        save_config(self.config)
        self._flash(f"step: {step}% per notch")

    def _on_step_change(self) -> None:
        self._set_step(self.step_var.get())

    def _toggle_focus(self) -> None:
        self.config["focus_on_hover"] = bool(self.focus_var.get())
        save_config(self.config)
        self._flash("take focus on hover: " + ("on" if self.focus_var.get() else "off"))

    # -- rendering ---------------------------------------------------------
    def _refresh_display(self) -> None:
        index = self._effective_active()
        shown = index is not None and self.known[index]
        value = self.values[index] if shown else 0
        if shown:
            self.value_label.configure(text=f"{value}%", fg=FG)
        else:
            self.value_label.configure(text="\u2013\u2013", fg=FG_DIM)
        filled = int(self._bar_w * value / 100.0)
        self.bar.coords(self._fill, 0, 0, filled, self._bar_h)

    def _refresh_footer(self) -> None:
        if time.time() < self.message_until and self.message:
            self.footer.configure(text=self.message, fg=ERR if self._is_error else FG_DIM)
        else:
            self.footer.configure(text=self._footer_text(), fg=FG_DIM)
        self.root.after(400, self._refresh_footer)

    def _footer_text(self) -> str:
        if self.count == 0:
            return "no controllable monitor"
        if self.count == 1:
            return self._shorten(self.backend.monitors[0].name, 34)
        if self.active is None:
            return f"all {self.count} monitors  \u00b7  click to pick"
        return f"{self._shorten(self.backend.monitors[self.active].name, 24)}  \u00b7  click"

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[: limit - 1] + "\u2026"

    def _flash(self, text: str, error: bool = False, seconds: float = 2.0) -> None:
        self.message = text
        self._is_error = error
        self.message_until = time.time() + seconds
        self._refresh_footer_now()

    def _refresh_footer_now(self) -> None:
        if self.message and time.time() < self.message_until:
            self.footer.configure(text=self.message, fg=ERR if self._is_error else FG_DIM)
        else:
            self.footer.configure(text=self._footer_text(), fg=FG_DIM)

    # -- background plumbing ----------------------------------------------
    def _read_initial_values(self) -> None:
        def work() -> None:
            values, known = [], []
            for index in range(self.count):
                try:
                    value = self.backend.get(index)
                except Exception:
                    value = None
                known.append(value is not None)
                values.append(value if value is not None else 100)
            self.events.put(("values", (values, known, list(self.backend.errors))))

        if self.count:
            threading.Thread(target=work, daemon=True, name="initial-read").start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "values":
                    values, known, errors = payload
                    for index, (value, is_known) in enumerate(zip(values, known)):
                        self.values[index] = value
                        self.known[index] = is_known
                    self._refresh_display()
                    if errors:
                        self._flash(errors[0], error=True, seconds=4.0)
                elif kind == "error":
                    self._flash(str(payload), error=True, seconds=3.0)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_events)

    # -- actions -----------------------------------------------------------
    def rescan(self) -> None:
        self.backend.scan()
        self.writer.forget()
        self.count = len(self.backend.monitors)
        self.values = [100] * self.count
        self.known = [False] * self.count
        if self.active is not None and self.active >= self.count:
            self.active = None if self.count != 1 else 0
        self._read_initial_values()
        self._refresh_display()
        self.menu.entryconfigure(
            self._rescan_index, label=f"Rescan monitors  ({self.count} found)"
        )
        self._flash(f"{self.count} monitor(s) found")

    def _toggle_startup(self) -> None:
        want = bool(self.startup_var.get())
        if set_startup(want):
            self._flash("will run at login" if want else "login entry removed")
        else:
            self.startup_var.set(startup_enabled())
            self._flash("could not change login entry", error=True)

    def _show_menu(self, event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def quit(self) -> None:
        self._remember_position()
        self.writer.stop()
        try:
            self.backend.release()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
def make_backend(force_gamma: bool):
    """Pick the best backend: real DDC/CI brightness, else software dimming."""
    if not force_gamma:
        ddc = DDCBackend()
        if ddc.monitors:
            return ddc
        gamma = GammaBackend()
        gamma.errors.append("no DDC/CI monitor found - using software dimming")
        return gamma
    return GammaBackend()


def selftest() -> int:
    enable_dpi_awareness()
    print(f"{APP_NAME} - self test")
    print(f"python           : {sys.version.split()[0]}")
    print(f"config           : {CONFIG_PATH}")
    print()
    print("--- DDC/CI ---")
    ddc = DDCBackend()
    if ddc.monitors:
        for index, monitor in enumerate(ddc.monitors):
            percent = ddc.get(index)
            print(f"  [{index}] {monitor.name}")
            print(f"       range {monitor.minimum}-{monitor.maximum}, "
                  f"current {percent}%")
            # Write back the value it already has: proves writes work without
            # changing anything the user would notice.
            if percent is not None:
                ok = ddc.set(index, percent)
                print(f"       write test: {'OK' if ok else 'FAILED'}")
    else:
        print("  no DDC/CI monitor found")
    print()
    print("--- gamma ramp ---")
    gamma = GammaBackend()
    if gamma.monitors:
        for index, monitor in enumerate(gamma.monitors):
            print(f"  [{index}] {monitor.name}")
    else:
        print("  no display device accepted a gamma ramp")
    print()
    print("--- summary ---")
    print(f"  DDC/CI monitors : {len(ddc.monitors)}")
    print(f"  gamma displays  : {len(gamma.monitors)}")
    print(f"  would use       : "
          f"{'DDC/CI hardware' if ddc.monitors else 'software dimming'}")
    ddc.release()
    gamma.release()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="brightness_scroll",
        description="Scroll-wheel monitor brightness for Windows.",
    )
    parser.add_argument("--gamma", action="store_true",
                        help="force software (gamma ramp) dimming")
    parser.add_argument("--step", type=int, default=None,
                        help="percent change per wheel notch (default 5)")
    parser.add_argument("--active", type=int, default=None,
                        help="0-based monitor index to control")
    parser.add_argument("--x", type=int, default=None, help="initial window X")
    parser.add_argument("--y", type=int, default=None, help="initial window Y")
    parser.add_argument("--selftest", action="store_true",
                        help="probe the backends, print a report and exit")
    parser.add_argument("--reset-config", action="store_true",
                        help="forget saved position and settings")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.reset_config:
        try:
            os.remove(CONFIG_PATH)
        except OSError:
            pass

    enable_dpi_awareness()
    config = load_config()
    if args.gamma:
        config["gamma"] = True
    backend = make_backend(force_gamma=bool(config.get("gamma")))

    root = tk.Tk()
    app = App(root, backend, config, args)

    if not backend.monitors:
        app._flash("no controllable monitor found", error=True, seconds=6.0)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
