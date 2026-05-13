#!/usr/bin/env python3
"""
Mic Monitor Lite  —  automatic background audio limiter
No main window. Runs silently in the Windows system tray.
Auto-selects microphone and CABLE Input on startup, auto-calibrates,
then monitors continuously. Right-click the tray icon to adjust settings.

Requirements:  pip install sounddevice numpy pystray pillow
Nothing is recorded. No network connections.
"""

import threading, time, math, json, os, sys
from datetime import datetime

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

# ── Paths & constants ─────────────────────────────────────────────────────────
_DIR          = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(_DIR, 'lite_settings.json')

SAMPLE_RATE          = 48000
BLOCK_SIZE           = 512
CHANNELS             = 1
DBFS_FLOOR           = -60.0
CAL_DURATION         = 2.0        # seconds of quiet needed for calibration
RECOVERY_SEC         = 2.0        # seconds below threshold before resuming
DETECT_ALPHA         = 0.30       # fast EMA for triggering
BASELINE_ALPHA       = 0.003      # slow EMA for adaptive baseline
BASELINE_QUIET_RATIO = 0.65
WATCHDOG_INTERVAL    = 3.0        # seconds between stream health checks

IDLE='idle'; CALIBRATING='calibrating'; MONITORING='monitoring'; TRIGGERED='triggered'

# Threshold presets shown in tray menu
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

def auto_select_devices():
    """
    Return (in_idx, out_idx, in_name, out_name).
    Input : system default mic, or first available input.
    Output: first device whose name contains 'cable input' or 'vb-audio',
            otherwise the first available output.
    """
    in_idx = out_idx = None
    in_name = out_name = ''
    try:
        default_in_name = sd.query_devices(kind='input')['name']
    except Exception:
        default_in_name = ''

    for i, d in enumerate(sd.query_devices()):
        n  = d['name']
        nl = n.lower()
        if 'sound mapper' in nl:
            continue
        if d['max_input_channels'] > 0:
            if in_idx is None:
                in_idx = i; in_name = n               # first available
            if default_in_name and default_in_name in n:
                in_idx = i; in_name = n               # prefer system default
        if d['max_output_channels'] > 0:
            if any(k in nl for k in ('cable input', 'vb-audio virtual cable')):
                out_idx = i; out_name = n             # prefer CABLE Input

    if out_idx is None:                               # fallback: first output
        for i, d in enumerate(sd.query_devices()):
            if d['max_output_channels'] > 0 and 'sound mapper' not in d['name'].lower():
                out_idx = i; out_name = d['name']; break

    return in_idx, out_idx, in_name, out_name

# ── Core ──────────────────────────────────────────────────────────────────────
class LiteMonitor:

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

        self.in_idx = self.out_idx = None
        self.in_name = self.out_name = ''

        self._icon      = None
        self._prev_state = None     # for change-detection in refresh loop

    # ── Settings ──────────────────────────────────────────────────────────────
    def _load_settings(self):
        self._threshold = 70
        try:
            with open(SETTINGS_PATH) as f:
                raw = json.load(f)
            t = raw.get('threshold', 70)
            self._threshold = max(1, min(100, int(t)))
        except Exception:
            pass

    def _save_settings(self):
        try:
            with open(SETTINGS_PATH, 'w') as f:
                json.dump({'threshold': self._threshold}, f)
        except Exception:
            pass

    # ── Tray icon helpers ─────────────────────────────────────────────────────
    def _status_text(self) -> str:
        if self.state == CALIBRATING: return 'Calibrating…'
        if self.state == MONITORING:  return f'Monitoring  ({self._threshold} %)'
        if self.state == TRIGGERED:   return 'Muted'
        return 'Stopped'

    def _icon_color(self) -> tuple:
        if self.state == MONITORING:  return (63,  185,  80, 255)   # green
        if self.state == TRIGGERED:   return (210, 153,  34, 255)   # yellow
        if self.state == CALIBRATING: return (88,  166, 255, 255)   # blue
        return                               (72,   79,  88, 255)   # gray

    def _make_image(self) -> Image.Image:
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([4, 4, 60, 60], fill=self._icon_color())
        return img

    def _refresh_icon(self):
        """Update tray icon image and tooltip. Safe to call from any thread."""
        if self._icon is None:
            return
        try:
            self._icon.icon  = self._make_image()
            self._icon.title = f'Mic Monitor Lite  —  {self._status_text()}'
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

        def threshold_checked(value):
            def check(item):
                return self._threshold == value
            return check

        threshold_items = [
            pystray.MenuItem(
                label,
                threshold_action(value),
                checked=threshold_checked(value),
                radio=True,
            )
            for label, value in THRESHOLD_PRESETS
        ]

        return pystray.Menu(
            # Live status (text rebuilt on each menu open via lambda)
            pystray.MenuItem(
                lambda item: f'🎙  {self._status_text()}',
                None, enabled=False),
            pystray.MenuItem(
                lambda item: f'In :  {self.in_name  or "—"}',
                None, enabled=False),
            pystray.MenuItem(
                lambda item: f'Out:  {self.out_name or "—"}',
                None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Threshold', pystray.Menu(*threshold_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Recalibrate', self._tray_recalibrate),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Quit', self._tray_quit),
        )

    # ── Tray actions ──────────────────────────────────────────────────────────
    def _tray_recalibrate(self, icon=None, item=None):
        if self.state in (MONITORING, TRIGGERED):
            self.cal_samples.clear()
            self.cal_start = time.perf_counter()
            self.state     = CALIBRATING
            self._refresh_icon()

    def _tray_quit(self, icon=None, item=None):
        self._running = False
        self._stop_stream()
        if self._icon:
            self._icon.stop()

    # ── Audio stream ──────────────────────────────────────────────────────────
    def _audio_cb(self, indata, outdata, frames, cb_time, status):
        rms  = float(np.sqrt(np.mean(indata ** 2)))
        dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -100.0
        raw  = dbfs_to_pct(dbfs)

        self._detect_pct = DETECT_ALPHA * raw + (1 - DETECT_ALPHA) * self._detect_pct
        now = time.perf_counter()
        t   = self._threshold

        if self.state == CALIBRATING:
            if len(self.cal_samples) < 500:       # hard cap
                self.cal_samples.append(raw)
            outdata[:] = indata
            if now - self.cal_start >= CAL_DURATION:
                with self._lock:
                    self.baseline_pct = median(self.cal_samples)
                self.state = MONITORING

        elif self.state == MONITORING:
            if self._detect_pct < t * BASELINE_QUIET_RATIO:
                with self._lock:                  # protect read-modify-write
                    self.baseline_pct = (BASELINE_ALPHA * raw
                                         + (1 - BASELINE_ALPHA) * self.baseline_pct)
            if self._detect_pct >= t:
                outdata[:] = 0
                self.state = TRIGGERED
                self.recovery_start = None
            else:
                outdata[:] = indata

        elif self.state == TRIGGERED:
            outdata[:] = 0
            if self._detect_pct < t:
                if self.recovery_start is None:
                    self.recovery_start = now
                elif now - self.recovery_start >= RECOVERY_SEC:
                    self.state = MONITORING
                    self.recovery_start = None
            else:
                self.recovery_start = None
        else:
            outdata[:] = 0

    def _start_stream(self) -> bool:
        """Find devices, open stream, begin calibration. Returns True on success."""
        self._stop_stream()
        self.in_idx, self.out_idx, self.in_name, self.out_name = auto_select_devices()

        if self.in_idx is None or self.out_idx is None:
            return False
        try:
            self.stream = sd.Stream(
                samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                channels=CHANNELS, dtype='float32',
                device=(self.in_idx, self.out_idx),
                callback=self._audio_cb, latency='low')
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
        """Checks stream health; auto-restarts if device disconnects."""
        while self._running:
            time.sleep(WATCHDOG_INTERVAL)
            if not self._running:
                break
            if self.state not in (IDLE, CALIBRATING):
                if self.stream is None or not self.stream.active:
                    self.state = IDLE
                    self._refresh_icon()
                    time.sleep(1.0)
                    if self._running:
                        self._start_stream()
                        self._refresh_icon()

    def _icon_refresh_loop(self):
        """Updates the tray icon when state changes (avoids calling from audio thread)."""
        while self._running:
            time.sleep(0.4)
            if self.state != self._prev_state:
                self._prev_state = self.state
                self._refresh_icon()

    # ── Entry point ───────────────────────────────────────────────────────────
    def run(self):
        ok = self._start_stream()
        if not ok:
            # Still launch — watchdog will retry
            self.state = IDLE

        threading.Thread(target=self._watchdog_loop,    daemon=True).start()
        threading.Thread(target=self._icon_refresh_loop, daemon=True).start()

        self._icon = pystray.Icon(
            'mic-monitor-lite',
            self._make_image(),
            f'Mic Monitor Lite  —  {self._status_text()}',
            menu=self._build_menu(),
        )
        self._icon.run()          # blocks until _tray_quit calls icon.stop()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    LiteMonitor().run()
