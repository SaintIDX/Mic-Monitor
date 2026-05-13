#!/usr/bin/env python3
"""
Mic Monitor No-VB  —  system microphone limiter, no virtual cable needed.
Mutes the Windows microphone directly when level exceeds threshold.
All apps (Discord, Teams, OBS) receive the muted state automatically.
System tray only. Nothing is recorded. No network connections.

How it works:
  Reads microphone level via sounddevice InputStream.
  When level exceeds threshold → SetMute(True) on Windows mic endpoint.
  After recovery time → SetMute(False), resume monitoring.
  No output routing needed — works without VB-Audio Virtual Cable.

Requirements:  pip install sounddevice numpy pystray pillow pycaw comtypes
"""

import threading, time, math, json, os, sys
import tkinter as tk
from tkinter import ttk

try:
    import sounddevice as sd
    import numpy as np
except ImportError as e:
    sys.exit(f"Missing library: {e}\nRun: pip install sounddevice numpy")

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError as e:
    sys.exit(f"Missing library: {e}\nRun: pip install pystray pillow")

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
except ImportError as e:
    sys.exit(f"Missing library: {e}\nRun: pip install pycaw comtypes")

# ── Paths & constants ─────────────────────────────────────────────────────────
_DIR          = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(_DIR, 'novb_settings.json')

SAMPLE_RATE          = 48000
BLOCK_SIZE           = 512
CHANNELS             = 1
DBFS_FLOOR           = -60.0
CAL_DURATION         = 2.0
DEFAULT_RECOVERY_SEC = 2.0
DETECT_ALPHA         = 0.30
BASELINE_ALPHA       = 0.003
BASELINE_QUIET_RATIO = 0.65
WATCHDOG_INTERVAL    = 3.0

IDLE='idle'; CALIBRATING='calibrating'; MONITORING='monitoring'; TRIGGERED='triggered'

DEFAULT_PRESETS = [
    ('Low  —  40 %',       40),
    ('Medium  —  60 %',    60),
    ('Normal  —  70 %',    70),
    ('High  —  85 %',      85),
    ('Very high  —  95 %', 95),
]

# ── Dark palette ──────────────────────────────────────────────────────────────
BG  = '#0d1117'
BG2 = '#161b22'
BG3 = '#21262d'
FG  = '#e6edf3'
FG2 = '#8b949e'
ACC = '#58a6ff'
SEP = '#30363d'

# ── Helpers ───────────────────────────────────────────────────────────────────
def dbfs_to_pct(dbfs: float) -> float:
    return max(0.0, min(100.0, (dbfs - DBFS_FLOOR) / (-DBFS_FLOOR) * 100.0))

def median(lst: list) -> float:
    if not lst: return 0.0
    s = sorted(lst); n = len(s); m = n // 2
    return s[m] if n % 2 else (s[m-1] + s[m]) / 2.0

def get_default_input():
    """Return (device_index, device_name) for the system default input."""
    try:
        idx  = sd.default.device[0]
        name = sd.query_devices(idx)['name'] if idx is not None else ''
        if idx is None:
            raise ValueError
        return idx, name
    except Exception:
        for i, d in enumerate(sd.query_devices()):
            if d['max_input_channels'] > 0 and 'sound mapper' not in d['name'].lower():
                return i, d['name']
        return None, ''

# ── Windows mic mute controller ───────────────────────────────────────────────
class MicController:
    """Wraps IAudioEndpointVolume for the default microphone."""

    def __init__(self):
        self._vol           = None
        self._original_mute = False
        self._connect()

    def _connect(self):
        try:
            mic = AudioUtilities.GetMicrophone()
            if mic is None:
                return
            vol = mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._vol = vol.QueryInterface(IAudioEndpointVolume)
            self._original_mute = bool(self._vol.GetMute())
        except Exception:
            self._vol = None

    def set_mute(self, muted: bool) -> bool:
        if self._vol is None:
            self._connect()
        try:
            self._vol.SetMute(1 if muted else 0, None)
            return True
        except Exception:
            return False

    def restore(self):
        """Restore the mute state that was active before the program started."""
        try:
            self._vol.SetMute(1 if self._original_mute else 0, None)
        except Exception:
            pass

# ── Settings window ───────────────────────────────────────────────────────────
class SettingsWindow:
    """Small tkinter window for editing presets and monitoring settings."""

    def __init__(self, monitor):
        self._mon = monitor
        self._win = tk.Toplevel(monitor._root)
        self._build()

    def _build(self):
        win = self._win
        win.title('Mic Monitor No-VB — Settings')
        win.configure(bg=BG)
        win.resizable(False, False)
        win.attributes('-topmost', True)
        win.grab_set()  # modal

        # ── Header ────────────────────────────────────────────────────────────
        tk.Label(win, text='THRESHOLD PRESETS',
                 bg=BG, fg=FG2, font=('Segoe UI', 8, 'bold')
                 ).grid(row=0, column=0, columnspan=3,
                        sticky='w', padx=16, pady=(16, 4))

        tk.Label(win, text='Label', bg=BG, fg=FG2,
                 font=('Segoe UI', 8)).grid(row=1, column=0, padx=(16, 4), sticky='w')
        tk.Label(win, text='%', bg=BG, fg=FG2,
                 font=('Segoe UI', 8)).grid(row=1, column=1, padx=4, sticky='w')

        # ── Preset rows ───────────────────────────────────────────────────────
        self._label_vars = []
        self._value_vars = []

        for i, (label, value) in enumerate(self._mon._presets):
            lvar = tk.StringVar(value=label)
            vvar = tk.StringVar(value=str(value))

            e_lbl = tk.Entry(win, textvariable=lvar, width=24,
                             bg=BG2, fg=FG, insertbackground=FG,
                             relief='flat', font=('Segoe UI', 9),
                             highlightthickness=1, highlightbackground=SEP,
                             highlightcolor=ACC)
            e_lbl.grid(row=i + 2, column=0, padx=(16, 4), pady=3, sticky='w')

            e_val = tk.Spinbox(win, textvariable=vvar,
                               from_=1, to=100, width=5,
                               bg=BG2, fg=FG, insertbackground=FG,
                               buttonbackground=BG3, relief='flat',
                               font=('Segoe UI', 9),
                               highlightthickness=1, highlightbackground=SEP,
                               highlightcolor=ACC)
            e_val.grid(row=i + 2, column=1, padx=4, pady=3, sticky='w')

            tk.Label(win, text='%', bg=BG, fg=FG2,
                     font=('Segoe UI', 9)).grid(row=i + 2, column=2,
                                                padx=(0, 16), sticky='w')
            self._label_vars.append(lvar)
            self._value_vars.append(vvar)

        # ── Separator ─────────────────────────────────────────────────────────
        sep_row = len(self._mon._presets) + 2
        tk.Frame(win, bg=SEP, height=1).grid(
            row=sep_row, column=0, columnspan=3,
            sticky='ew', padx=16, pady=(8, 4))

        # ── Recovery time ─────────────────────────────────────────────────────
        tk.Label(win, text='MONITORING',
                 bg=BG, fg=FG2, font=('Segoe UI', 8, 'bold')
                 ).grid(row=sep_row + 1, column=0, columnspan=3,
                        sticky='w', padx=16, pady=(4, 4))

        tk.Label(win, text='Recovery time', bg=BG, fg=FG,
                 font=('Segoe UI', 9)
                 ).grid(row=sep_row + 2, column=0, padx=(16, 4),
                        pady=4, sticky='w')

        self._rec_var = tk.StringVar(value=f'{self._mon._recovery_sec:.1f}')
        rec_spin = tk.Spinbox(win, textvariable=self._rec_var,
                              from_=0.5, to=10.0, increment=0.5, width=5,
                              bg=BG2, fg=FG, insertbackground=FG,
                              buttonbackground=BG3, relief='flat',
                              font=('Segoe UI', 9), format='%.1f',
                              highlightthickness=1, highlightbackground=SEP,
                              highlightcolor=ACC)
        rec_spin.grid(row=sep_row + 2, column=1, padx=4, pady=4, sticky='w')
        tk.Label(win, text='s', bg=BG, fg=FG2,
                 font=('Segoe UI', 9)).grid(row=sep_row + 2, column=2, sticky='w')

        # ── Separator ─────────────────────────────────────────────────────────
        tk.Frame(win, bg=SEP, height=1).grid(
            row=sep_row + 3, column=0, columnspan=3,
            sticky='ew', padx=16, pady=(8, 4))

        # ── Buttons ───────────────────────────────────────────────────────────
        btn = tk.Frame(win, bg=BG)
        btn.grid(row=sep_row + 4, column=0, columnspan=3, pady=(4, 16))

        tk.Button(btn, text='Save', command=self._save,
                  bg=ACC, fg='#0d1117', font=('Segoe UI', 9, 'bold'),
                  relief='flat', padx=20, pady=6,
                  cursor='hand2', activebackground='#79c0ff'
                  ).pack(side='left', padx=(16, 6))

        tk.Button(btn, text='Cancel', command=win.destroy,
                  bg=BG3, fg=FG, font=('Segoe UI', 9),
                  relief='flat', padx=20, pady=6,
                  cursor='hand2', activebackground=BG2
                  ).pack(side='left', padx=(0, 16))

        # Center on screen
        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        sx = (win.winfo_screenwidth()  - w) // 2
        sy = (win.winfo_screenheight() - h) // 2
        win.geometry(f'+{sx}+{sy}')

    def _save(self):
        presets = []
        for i, (lvar, vvar) in enumerate(zip(self._label_vars, self._value_vars)):
            label = lvar.get().strip() or f'Preset {i + 1}'
            try:
                value = max(1, min(100, int(float(vvar.get()))))
            except Exception:
                value = 70
            presets.append((label, value))

        try:
            rec = max(0.5, min(10.0, float(self._rec_var.get())))
        except Exception:
            rec = DEFAULT_RECOVERY_SEC

        self._mon._presets      = presets
        self._mon._recovery_sec = rec
        self._mon._save_settings()
        self._mon._rebuild_menu()
        self._win.destroy()

# ── Monitor ───────────────────────────────────────────────────────────────────
class NoVBMonitor:

    def __init__(self):
        self._load_settings()

        self.state          = IDLE
        self.stream         = None
        self._detect_pct    = 0.0
        self.baseline_pct   = 0.0
        self.cal_samples    = []
        self.cal_start      = 0.0
        self.recovery_start = None
        self._lock          = threading.Lock()
        self._running       = True

        self.in_idx  = None
        self.in_name = ''

        self._icon            = None
        self._prev_state      = None
        self._mic_ctrl        = MicController()
        self._root            = None       # tkinter root (main thread)
        self._pending_settings = False     # flag: open settings window

    # ── Settings ──────────────────────────────────────────────────────────────
    def _load_settings(self):
        self._threshold          = 70
        self._start_with_windows = False
        self._notifications      = True
        self._recovery_sec       = DEFAULT_RECOVERY_SEC
        self._presets            = list(DEFAULT_PRESETS)
        try:
            with open(SETTINGS_PATH) as f:
                raw = json.load(f)
            self._threshold = max(1, min(100, int(raw.get('threshold', 70))))
            self._start_with_windows = bool(raw.get('start_with_windows', False))
            self._notifications      = bool(raw.get('notifications', True))
            self._recovery_sec = max(0.5, min(10.0,
                                   float(raw.get('recovery_sec', DEFAULT_RECOVERY_SEC))))
            raw_p = raw.get('presets', [])
            if isinstance(raw_p, list) and len(raw_p) == 5:
                self._presets = [
                    (str(p[0]), max(1, min(100, int(p[1])))) for p in raw_p
                ]
        except Exception:
            pass

    def _save_settings(self):
        try:
            with open(SETTINGS_PATH, 'w') as f:
                json.dump({
                    'threshold':          self._threshold,
                    'start_with_windows': self._start_with_windows,
                    'notifications':      self._notifications,
                    'recovery_sec':       self._recovery_sec,
                    'presets':            self._presets,
                }, f, indent=2)
        except Exception:
            pass

    def _rebuild_menu(self):
        if self._icon:
            try:
                self._icon.menu = self._build_menu()
            except Exception:
                pass

    # ── Windows autostart ─────────────────────────────────────────────────────
    def _autostart_cmd(self) -> str:
        if getattr(sys, 'frozen', False):
            return f'"{sys.executable}"'
        return f'"{sys.executable}" "{os.path.abspath(__file__)}"'

    def _set_autostart(self, enabled: bool) -> bool:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Run',
                0, winreg.KEY_SET_VALUE)
            if enabled:
                winreg.SetValueEx(key, 'MicMonitorNoVB', 0,
                                  winreg.REG_SZ, self._autostart_cmd())
            else:
                try:    winreg.DeleteValue(key, 'MicMonitorNoVB')
                except FileNotFoundError: pass
            winreg.CloseKey(key)
            return True
        except Exception:
            return False

    # ── Toast notification ────────────────────────────────────────────────────
    def _show_toast(self, message: str):
        """Small overlay popup in the bottom-right corner (3 s auto-dismiss)."""
        if not self._notifications or self._root is None:
            return
        try:
            toast = tk.Toplevel(self._root)
            toast.overrideredirect(True)
            toast.attributes('-topmost', True)
            toast.attributes('-alpha', 0.93)
            w, h = 290, 68
            sw   = toast.winfo_screenwidth()
            sh   = toast.winfo_screenheight()
            toast.geometry(f'{w}x{h}+{sw - w - 20}+{sh - h - 60}')
            toast.configure(bg=BG2)

            tk.Frame(toast, bg=ACC, width=4).pack(side='left', fill='y')
            body = tk.Frame(toast, bg=BG2)
            body.pack(side='left', fill='both', expand=True, padx=10)

            tk.Label(body, text='🎙  Mic Monitor No-VB',
                     bg=BG2, fg=ACC,
                     font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(10, 1))
            tk.Label(body, text=message,
                     bg=BG2, fg=FG,
                     font=('Segoe UI', 9)).pack(anchor='w')

            toast.after(3000, toast.destroy)
        except Exception:
            pass

    def _notify(self, message: str):
        """Schedule a toast on the main (tkinter) thread."""
        if not self._notifications or self._root is None:
            return
        try:
            self._root.after(0, lambda: self._show_toast(message))
        except Exception:
            pass

    # ── Tray helpers ──────────────────────────────────────────────────────────
    def _status_text(self) -> str:
        if self.state == CALIBRATING: return 'Calibrating…'
        if self.state == MONITORING:  return f'Monitoring  ({self._threshold} %)'
        if self.state == TRIGGERED:   return 'Mic muted'
        return 'Stopped'

    def _icon_color(self) -> tuple:
        if self.state == MONITORING:  return (63,  185,  80, 255)
        if self.state == TRIGGERED:   return (210, 153,  34, 255)
        if self.state == CALIBRATING: return (88,  166, 255, 255)
        return                               (72,   79,  88, 255)

    def _make_image(self) -> Image.Image:
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([4, 4, 60, 60], fill=self._icon_color())
        return img

    def _refresh_icon(self):
        if not self._icon: return
        try:
            self._icon.icon  = self._make_image()
            self._icon.title = f'Mic Monitor No-VB  —  {self._status_text()}'
        except Exception:
            pass

    # ── Tray menu ─────────────────────────────────────────────────────────────
    def _build_menu(self) -> pystray.Menu:

        def threshold_action(value):
            def action(icon, item):
                self._threshold = value
                self._save_settings()
                self._refresh_icon()
            return action

        threshold_items = [
            pystray.MenuItem(
                label,
                threshold_action(value),
                checked=lambda item, v=value: self._threshold == v,
                radio=True,
            )
            for label, value in self._presets
        ]

        def toggle_autostart(icon, item):
            self._start_with_windows = not self._start_with_windows
            if not self._set_autostart(self._start_with_windows):
                self._start_with_windows = False
            self._save_settings()

        def toggle_notif(icon, item):
            self._notifications = not self._notifications
            self._save_settings()

        def open_settings(icon, item):
            self._pending_settings = True

        return pystray.Menu(
            pystray.MenuItem(
                lambda item: f'🎙  {self._status_text()}',
                None, enabled=False),
            pystray.MenuItem(
                lambda item: f'Mic:  {self.in_name or "—"}',
                None, enabled=False),
            pystray.MenuItem(
                '✓ No VB-Audio required',
                None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Threshold', pystray.Menu(*threshold_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Recalibrate', self._tray_recalibrate),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Settings…', open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Start with Windows',
                toggle_autostart,
                checked=lambda item: self._start_with_windows),
            pystray.MenuItem('Notifications',
                toggle_notif,
                checked=lambda item: self._notifications),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Quit', self._tray_quit),
        )

    # ── Tray actions ──────────────────────────────────────────────────────────
    def _tray_recalibrate(self, icon=None, item=None):
        if self.state in (MONITORING, TRIGGERED):
            self._mic_ctrl.set_mute(False)
            self.cal_samples.clear()
            self.cal_start = time.perf_counter()
            self.state     = CALIBRATING
            self._refresh_icon()

    def _tray_quit(self, icon=None, item=None):
        self._running = False
        self._mic_ctrl.set_mute(False)
        self._mic_ctrl.restore()
        self._stop_stream()
        if self._icon:
            self._icon.stop()

    # ── Audio callback  (input only) ──────────────────────────────────────────
    def _input_cb(self, indata, frames, time_info, status):
        rms  = float(np.sqrt(np.mean(indata ** 2)))
        dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -100.0
        raw  = dbfs_to_pct(dbfs)

        self._detect_pct = DETECT_ALPHA * raw + (1 - DETECT_ALPHA) * self._detect_pct
        now = time.perf_counter()
        t   = self._threshold

        if self.state == CALIBRATING:
            if len(self.cal_samples) < 500:
                self.cal_samples.append(raw)
            if now - self.cal_start >= CAL_DURATION:
                with self._lock:
                    self.baseline_pct = median(self.cal_samples)
                self.state = MONITORING

        elif self.state == MONITORING:
            if self._detect_pct < t * BASELINE_QUIET_RATIO:
                with self._lock:
                    self.baseline_pct = (BASELINE_ALPHA * raw
                                         + (1 - BASELINE_ALPHA) * self.baseline_pct)
            if self._detect_pct >= t:
                self._mic_ctrl.set_mute(True)
                self.state          = TRIGGERED
                self.recovery_start = now

        elif self.state == TRIGGERED:
            # Time-based recovery — level-based won't work because SetMute
            # zeros our own InputStream readings.
            if now - self.recovery_start >= self._recovery_sec:
                self._mic_ctrl.set_mute(False)
                self.state          = MONITORING
                self.recovery_start = None

    # ── Stream management ─────────────────────────────────────────────────────
    def _start_stream(self) -> bool:
        self._stop_stream()
        self.in_idx, self.in_name = get_default_input()
        if self.in_idx is None:
            return False
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                channels=CHANNELS, dtype='float32',
                device=self.in_idx,
                callback=self._input_cb, latency='low')
            self.stream.start()
        except Exception:
            self.stream = None
            return False
        self.cal_samples.clear()
        self.cal_start = time.perf_counter()
        self.state     = CALIBRATING
        return True

    def _stop_stream(self):
        if self.stream:
            try: self.stream.stop(); self.stream.close()
            except Exception: pass
            self.stream = None

    # ── Background threads ────────────────────────────────────────────────────
    def _watchdog_loop(self):
        while self._running:
            time.sleep(WATCHDOG_INTERVAL)
            if not self._running: break
            if self.state not in (IDLE, CALIBRATING):
                if self.stream is None or not self.stream.active:
                    self._mic_ctrl.set_mute(False)
                    self.state = IDLE
                    self._refresh_icon()
                    time.sleep(1.0)
                    if self._running:
                        self._start_stream()
                        self._refresh_icon()

    def _icon_refresh_loop(self):
        while self._running:
            time.sleep(0.4)
            if self.state != self._prev_state:
                prev             = self._prev_state
                self._prev_state = self.state
                self._refresh_icon()
                if self.state == TRIGGERED and prev == MONITORING:
                    self._notify('Microphone muted')
                elif self.state == MONITORING and prev == TRIGGERED:
                    self._notify('Microphone active')

    # ── Entry point ───────────────────────────────────────────────────────────
    def run(self):
        if not self._start_stream():
            self.state = IDLE

        threading.Thread(target=self._watchdog_loop,     daemon=True).start()
        threading.Thread(target=self._icon_refresh_loop, daemon=True).start()

        self._icon = pystray.Icon(
            'mic-monitor-novb',
            self._make_image(),
            f'Mic Monitor No-VB  —  {self._status_text()}',
            menu=self._build_menu(),
        )
        self._icon.run_detached()

        # Main thread: tkinter hidden root for toasts and settings window
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.configure(bg=BG)

        try:
            while self._running:
                if self._pending_settings:
                    self._pending_settings = False
                    SettingsWindow(self)
                self._root.update()
                time.sleep(0.05)
        except tk.TclError:
            pass
        finally:
            try:
                self._root.destroy()
            except Exception:
                pass


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    NoVBMonitor().run()
