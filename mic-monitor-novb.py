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
RECOVERY_SEC         = 2.0
DETECT_ALPHA         = 0.30
BASELINE_ALPHA       = 0.003
BASELINE_QUIET_RATIO = 0.65
WATCHDOG_INTERVAL    = 3.0

IDLE='idle'; CALIBRATING='calibrating'; MONITORING='monitoring'; TRIGGERED='triggered'

THRESHOLD_PRESETS = [
    ('Low  —  40 %',       40),
    ('Medium  —  60 %',    60),
    ('Normal  —  70 %',    70),
    ('High  —  85 %',      85),
    ('Very high  —  95 %', 95),
]

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
        """Restore the mute state that was set before the program started."""
        try:
            self._vol.SetMute(1 if self._original_mute else 0, None)
        except Exception:
            pass

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

        self._icon       = None
        self._prev_state = None
        self._mic_ctrl   = MicController()

    # ── Settings ──────────────────────────────────────────────────────────────
    def _load_settings(self):
        self._threshold          = 70
        self._start_with_windows = False
        self._notifications      = True
        try:
            with open(SETTINGS_PATH) as f:
                raw = json.load(f)
            self._threshold          = max(1, min(100, int(raw.get('threshold', 70))))
            self._start_with_windows = bool(raw.get('start_with_windows', False))
            self._notifications      = bool(raw.get('notifications', True))
        except Exception:
            pass

    def _save_settings(self):
        try:
            with open(SETTINGS_PATH, 'w') as f:
                json.dump({'threshold':          self._threshold,
                           'start_with_windows': self._start_with_windows,
                           'notifications':      self._notifications}, f)
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

    def _notify(self, message: str):
        if not self._notifications or not self._icon: return
        try:
            self._icon.notify(message, 'Mic Monitor No-VB')
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
            pystray.MenuItem(label, threshold_action(value),
                checked=lambda item, v=value: self._threshold == v,
                radio=True)
            for label, value in THRESHOLD_PRESETS
        ]

        def toggle_autostart(icon, item):
            self._start_with_windows = not self._start_with_windows
            if not self._set_autostart(self._start_with_windows):
                self._start_with_windows = False
            self._save_settings()

        def toggle_notif(icon, item):
            self._notifications = not self._notifications
            self._save_settings()

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

    # ── Audio callback  (input only — no output stream) ───────────────────────
    def _input_cb(self, indata, frames, time_info, status):
        rms  = float(np.sqrt(np.mean(indata ** 2)))
        dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -100.0
        raw  = dbfs_to_pct(dbfs)

        # Note: when mic is muted via pycaw, indata will be near zero.
        # Detection therefore uses raw audio only in CALIBRATING / MONITORING.
        # TRIGGERED state uses time-based recovery instead of level detection.

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
                self.recovery_start = now   # start timer immediately

        elif self.state == TRIGGERED:
            # Time-based recovery: wait RECOVERY_SEC, then unmute.
            # If noise is still happening, MONITORING will re-trigger on next block.
            if now - self.recovery_start >= RECOVERY_SEC:
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
        self._icon.run()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    NoVBMonitor().run()
