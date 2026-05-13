#!/usr/bin/env python3
"""
Mic Monitor v3.0  —  real-time audio level limiter
Routes microphone through VB-Audio Virtual Cable, muting output when too loud.
Nothing is recorded or stored. No network connections.
"""

import tkinter as tk
from tkinter import ttk
import tkinter.messagebox as mb

try:
    import sounddevice as sd
    import numpy as np
except ImportError as _e:
    _r = tk.Tk(); _r.withdraw()
    mb.showerror("Missing library",
        f"{_e}\n\nInstall with:\n    pip install sounddevice numpy")
    raise SystemExit(1)

import math, threading, time, json, os
from datetime import datetime

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────
SAMPLE_RATE          = 48000
BLOCK_SIZE           = 512
CHANNELS             = 1
DBFS_FLOOR           = -60.0
CAL_DURATION         = 1.5
GUI_FPS              = 30
DISPLAY_ALPHA        = 0.08
DETECT_ALPHA         = 0.30
BASELINE_ALPHA       = 0.003
BASELINE_QUIET_RATIO = 0.65

IDLE='idle'; CALIBRATING='calibrating'; MONITORING='monitoring'; TRIGGERED='triggered'

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')

DEFAULT_SETTINGS = {
    "in_device":            "",
    "out_device":           "",
    "threshold":            70,
    "recovery_sec":         2.0,
    "peak_hold_sec":        2.0,
    "dark_mode":            True,
    "launch_to_tray":       False,
    "audio_mode":           "standard",
    "output_mode":          "gate",
    "compressor_reduction": 80,
    "show_baseline_label":  True,
    "show_threshold_label": True,
    "show_log":             True,
    "show_peak_hold":       False,
}

# ── Color palettes ────────────────────────────────────────────────────────────
DARK = dict(
    BG='#0d1117', S1='#161b22', S2='#21262d', BDR='#30363d',
    FG='#e6edf3', GRY='#8b949e', DIM='#484f58',
    ACC='#58a6ff', GRN='#3fb950', YEL='#d29922', RED='#f85149',
    METER_BG='#151b23',
)
LIGHT = dict(
    BG='#f6f8fa', S1='#ffffff', S2='#eaeef2', BDR='#d0d7de',
    FG='#1f2328', GRY='#57606a', DIM='#8c959f',
    ACC='#0969da', GRN='#1a7f37', YEL='#9a6700', RED='#cf222e',
    METER_BG='#e8edf2',
)

# ── Helpers ───────────────────────────────────────────────────────────────────
def dbfs_to_pct(dbfs: float) -> float:
    return max(0.0, min(100.0, (dbfs - DBFS_FLOOR) / (-DBFS_FLOOR) * 100.0))

def median(lst: list) -> float:
    if not lst: return 0.0
    s = sorted(lst); n = len(s); mid = n // 2
    return s[mid] if n % 2 else (s[mid-1] + s[mid]) / 2.0

def get_devices(wasapi_only=False):
    in_list, out_list, in_map, out_map = [], [], {}, {}
    seen_in, seen_out = set(), set()
    wasapi_ha = None
    if wasapi_only:
        for i, h in enumerate(sd.query_hostapis()):
            if 'wasapi' in h['name'].lower():
                wasapi_ha = i; break
    for i, d in enumerate(sd.query_devices()):
        n = d['name']
        if 'sound mapper' in n.lower(): continue
        if wasapi_only and wasapi_ha is not None and d['hostapi'] != wasapi_ha:
            continue
        if d['max_input_channels'] > 0 and n not in seen_in:
            in_list.append(n); in_map[n] = i; seen_in.add(n)
        if d['max_output_channels'] > 0 and n not in seen_out:
            out_list.append(n); out_map[n] = i; seen_out.add(n)
    return in_list, out_list, in_map, out_map

# ── Application ───────────────────────────────────────────────────────────────
class App:
    BAR_H = 72

    # state → (palette-key for color, default status text)
    STATE_INFO = {
        IDLE:        ('DIM', 'Stopped'),
        CALIBRATING: ('ACC', 'Calibrating…'),
        MONITORING:  ('GRN', 'Monitoring active'),
        TRIGGERED:   ('YEL', 'Muted'),
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Mic Monitor")
        self.root.resizable(False, False)

        self._load_settings()

        # Runtime audio state
        self.state           = IDLE
        self.stream          = None
        self.cal_samples     = []
        self.cal_start       = 0.0
        self.baseline_pct    = None
        self.recovery_start  = None
        self._threshold_pct  = self.settings['threshold']
        self._detect_pct     = 0.0
        self._display_pct    = 0.0
        self._lock           = threading.Lock()
        self._grad_ids       = []
        self._recovery_sec   = float(self.settings['recovery_sec'])
        self._peak_hold_sec  = float(self.settings['peak_hold_sec'])
        self._peak_pct       = 0.0
        self._peak_time      = 0.0
        self._on_settings_page = False
        self._tray_icon      = None

        self._configure_ttk()
        self._build_ui()
        self._load_devices()
        self._apply_theme()
        self._update_loop()

        if self.settings.get('launch_to_tray') and TRAY_AVAILABLE:
            self.root.after(200, self._minimize_to_tray)

    # ──────────────────────────────────────────────
    # Settings persistence
    # ──────────────────────────────────────────────
    def _load_settings(self):
        self.settings = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_PATH, 'r') as f:
                self.settings.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_settings(self):
        try:
            with open(SETTINGS_PATH, 'w') as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass

    # ──────────────────────────────────────────────
    # Palette helper
    # ──────────────────────────────────────────────
    def p(self, key=None):
        pal = DARK if self.settings.get('dark_mode', True) else LIGHT
        return pal if key is None else pal[key]

    # ──────────────────────────────────────────────
    # ttk styling
    # ──────────────────────────────────────────────
    def _configure_ttk(self):
        s = ttk.Style()
        s.theme_use('clam')
        self._update_ttk_style(s)

    def _update_ttk_style(self, s=None):
        if s is None: s = ttk.Style()
        pal = self.p()
        s.configure('D.TCombobox',
            fieldbackground=pal['S2'], background=pal['S2'],
            foreground=pal['FG'], bordercolor=pal['BDR'],
            arrowcolor=pal['GRY'], selectbackground=pal['S2'],
            selectforeground=pal['FG'], padding=4)
        s.map('D.TCombobox',
            fieldbackground=[('readonly', pal['S2'])],
            foreground=[('readonly', pal['FG'])])
        s.configure('Vertical.TScrollbar',
            background=pal['S2'], troughcolor=pal['BG'],
            bordercolor=pal['BDR'], arrowcolor=pal['GRY'])

    # ──────────────────────────────────────────────
    # UI construction
    # ──────────────────────────────────────────────
    def _build_ui(self):
        pal = self.p()

        # ══ HEADER ════════════════════════════════
        self.hdr = tk.Frame(self.root, bg=pal['S1'], height=46)
        self.hdr.pack(fill='x')
        self.hdr.pack_propagate(False)

        self.hdr_icon = tk.Label(self.hdr, text='🎙', bg=pal['S1'], fg=pal['ACC'],
                                  font=('Segoe UI', 12))
        self.hdr_icon.pack(side='left', padx=(14, 4))
        self.hdr_title = tk.Label(self.hdr, text='Mic Monitor', bg=pal['S1'], fg=pal['FG'],
                                   font=('Segoe UI', 11, 'bold'))
        self.hdr_title.pack(side='left')

        # Right side buttons (pack right-to-left)
        self.gear_btn = tk.Button(self.hdr, text='⚙', bg=pal['S1'], fg=pal['GRY'],
            activebackground=pal['S2'], activeforeground=pal['FG'],
            font=('Segoe UI', 12), bd=0, relief='flat', cursor='hand2',
            command=self._toggle_page)
        self.gear_btn.pack(side='right', padx=(0, 6))

        if TRAY_AVAILABLE:
            self.tray_btn = tk.Button(self.hdr, text='→', bg=pal['S1'], fg=pal['GRY'],
                activebackground=pal['S2'], activeforeground=pal['FG'],
                font=('Segoe UI', 12, 'bold'), bd=0, relief='flat', cursor='hand2',
                command=self._minimize_to_tray)
            self.tray_btn.pack(side='right', padx=(0, 2))
        else:
            self.tray_btn = None

        self.status_frame = tk.Frame(self.hdr, bg=pal['S1'])
        self.status_frame.pack(side='right', padx=(0, 8))
        self.dot = tk.Label(self.status_frame, text='●', bg=pal['S1'], fg=pal['DIM'],
                             font=('Segoe UI', 9))
        self.dot.pack(side='left', padx=(0, 5))
        self.status_lbl = tk.Label(self.status_frame, text='Stopped', bg=pal['S1'],
                                    fg=pal['DIM'], font=('Segoe UI', 8))
        self.status_lbl.pack(side='left')

        self.hdr_sep = tk.Frame(self.root, bg=pal['BDR'], height=1)
        self.hdr_sep.pack(fill='x')

        # ══ CONTENT AREA ══════════════════════════
        self.content = tk.Frame(self.root, bg=pal['BG'])
        self.content.pack(fill='both', expand=True)

        self._build_main_page()
        self._build_settings_page()
        self.main_page.pack(fill='both', expand=True)

    # ──────────────────────────────────────────────
    # Main page
    # ──────────────────────────────────────────────
    def _build_main_page(self):
        pal = self.p()
        self.main_page = tk.Frame(self.content, bg=pal['BG'])

        # ── Devices ────────────────────────────────
        self.dev_frame = tk.Frame(self.main_page, bg=pal['BG'])
        self.dev_frame.pack(fill='x', padx=14, pady=(8, 8))

        self._dev_icon_labels = []
        self._dev_tip_labels  = []
        self._dev_top_frames  = []
        self._dev_cell_frames = []

        for col, (icon, tip, attr_v, attr_cb, skey) in enumerate([
            ('🎤', 'Input (microphone)',      'in_var',  'in_cb',  'in_device'),
            ('🔊', 'Output (VB-Audio Cable)', 'out_var', 'out_cb', 'out_device'),
        ]):
            cell = tk.Frame(self.dev_frame, bg=pal['BG'])
            cell.pack(side='left', expand=True, fill='x',
                      padx=(0, 8) if col == 0 else (0, 0))
            self._dev_cell_frames.append(cell)

            top = tk.Frame(cell, bg=pal['BG'])
            top.pack(fill='x')
            self._dev_top_frames.append(top)

            lbl_icon = tk.Label(top, text=icon, bg=pal['BG'], fg=pal['GRY'],
                                 font=('Segoe UI', 10))
            lbl_icon.pack(side='left')
            self._dev_icon_labels.append(lbl_icon)

            lbl_tip = tk.Label(top, text=tip, bg=pal['BG'], fg=pal['DIM'],
                                font=('Segoe UI', 7))
            lbl_tip.pack(side='left', padx=(4, 0))
            self._dev_tip_labels.append(lbl_tip)

            var = tk.StringVar()
            setattr(self, attr_v, var)
            cb = ttk.Combobox(cell, textvariable=var, style='D.TCombobox',
                               state='readonly', width=22, font=('Segoe UI', 8))
            cb.pack(fill='x', pady=(2, 0))
            setattr(self, attr_cb, cb)

            var.trace_add('write',
                lambda *_, av=attr_v, sk=skey: self._on_device_change(av, sk))

        self.dev_sep = tk.Frame(self.main_page, bg=pal['BDR'], height=1)
        self.dev_sep.pack(fill='x')

        # ── Meter ──────────────────────────────────
        self.mf = tk.Frame(self.main_page, bg=pal['BG'])
        self.mf.pack(fill='x', padx=14, pady=(12, 0))

        self.canvas = tk.Canvas(self.mf, height=self.BAR_H, bg=pal['METER_BG'],
                                highlightthickness=1,
                                highlightbackground=pal['BDR'], bd=0)
        self.canvas.pack(fill='x')
        self.canvas.bind('<Configure>', self._on_bar_resize)

        self.mask_rect   = self.canvas.create_rectangle(0, 0, 0, self.BAR_H,
                                                         fill=pal['METER_BG'], outline='')
        self.base_line   = self.canvas.create_line(0, 0, 0, 0,
                                                    fill=pal['ACC'], width=1, dash=(3, 4))
        self.thresh_line = self.canvas.create_line(0, 0, 0, self.BAR_H,
                                                    fill=pal['RED'], width=2)
        self.thresh_tag  = self.canvas.create_text(0, 4, text='',
                                                    fill=pal['RED'],
                                                    font=('Segoe UI', 7, 'bold'),
                                                    anchor='nw')
        self.peak_line   = self.canvas.create_line(0, 0, 0, 0,
                                                    fill='white', width=2)

        sc = tk.Frame(self.mf, bg=pal['BG'])
        sc.pack(fill='x', pady=(3, 0))
        self.scale_labels = []
        for txt in ['0 %', '25 %', '50 %', '75 %', '100 %']:
            lbl = tk.Label(sc, text=txt, bg=pal['BG'], fg=pal['DIM'],
                           font=('Segoe UI', 7))
            lbl.pack(side='left', expand=True)
            self.scale_labels.append(lbl)

        self.info_frame = tk.Frame(self.mf, bg=pal['BG'])
        self.info_frame.pack(fill='x', pady=(6, 8))

        self.base_lbl_text = tk.Label(self.info_frame, text='Baseline',
                                       bg=pal['BG'], fg=pal['DIM'], font=('Segoe UI', 8))
        self.base_lbl = tk.Label(self.info_frame, text='--',
                                  bg=pal['BG'], fg=pal['ACC'], font=('Segoe UI', 8, 'bold'))
        self.thresh_info = tk.Label(self.info_frame,
                                     text=f'Threshold  {self.settings["threshold"]} %',
                                     bg=pal['BG'], fg=pal['RED'], font=('Segoe UI', 8, 'bold'))

        self.meter_sep = tk.Frame(self.main_page, bg=pal['BDR'], height=1)
        self.meter_sep.pack(fill='x')

        # ── Threshold slider ────────────────────────
        self.slider_frame = tk.Frame(self.main_page, bg=pal['BG'])
        self.slider_frame.pack(fill='x', padx=14, pady=(10, 10))

        sl_head = tk.Frame(self.slider_frame, bg=pal['BG'])
        sl_head.pack(fill='x')
        self.sl_head_lbl = tk.Label(sl_head, text='Mute threshold',
                                     bg=pal['BG'], fg=pal['GRY'], font=('Segoe UI', 8))
        self.sl_head_lbl.pack(side='left')
        self.sl_badge = tk.Label(sl_head, text=f'{self.settings["threshold"]} %',
                                  bg=pal['BG'], fg=pal['RED'], font=('Segoe UI', 9, 'bold'))
        self.sl_badge.pack(side='right')

        self.thresh_var = tk.IntVar(value=self.settings['threshold'])
        self.thresh_var.trace_add('write', self._on_thresh_change)

        self.thresh_scale = tk.Scale(self.slider_frame, from_=1, to=100, orient='horizontal',
            variable=self.thresh_var, bg=pal['BG'], fg=pal['FG'],
            troughcolor=pal['S2'], highlightthickness=0, bd=0,
            sliderrelief='flat', showvalue=False)
        self.thresh_scale.pack(fill='x', pady=(4, 0))

        self.slider_sep = tk.Frame(self.main_page, bg=pal['BDR'], height=1)
        self.slider_sep.pack(fill='x')

        # ── Start/Stop button ──────────────────────
        self.btn = tk.Button(self.main_page,
            text='▶  Start monitoring',
            bg='#1f6feb', fg=pal['FG'],
            activebackground='#388bfd', activeforeground=pal['FG'],
            font=('Segoe UI', 10, 'bold'),
            bd=0, pady=11, cursor='hand2', relief='flat',
            command=self.toggle)
        self.btn.pack(fill='x', padx=14, pady=12)

        # ── Mini log ───────────────────────────────
        self.log = tk.Text(self.main_page, height=4, bg=pal['BG'], fg=pal['DIM'],
                           font=('Consolas', 7), bd=0, state='disabled',
                           wrap='word', relief='flat', cursor='arrow')
        for tag, ck in [('cal','ACC'),('start','GRN'),('warn','YEL'),('recover','YEL')]:
            self.log.tag_config(tag, foreground=pal[ck])

        self._apply_widget_visibility()

    # ──────────────────────────────────────────────
    # Settings page
    # ──────────────────────────────────────────────
    def _build_settings_page(self):
        pal = self.p()
        self.settings_page = tk.Frame(self.content, bg=pal['BG'])

        self.settings_canvas = tk.Canvas(self.settings_page, bg=pal['BG'],
                                          highlightthickness=0)
        sb = ttk.Scrollbar(self.settings_page, orient='vertical',
                           command=self.settings_canvas.yview)
        self.settings_inner = tk.Frame(self.settings_canvas, bg=pal['BG'])

        self.settings_inner.bind('<Configure>',
            lambda e: self.settings_canvas.configure(
                scrollregion=self.settings_canvas.bbox('all')))
        self._settings_win = self.settings_canvas.create_window(
            (0, 0), window=self.settings_inner, anchor='nw')
        self.settings_canvas.configure(yscrollcommand=sb.set)
        self.settings_canvas.bind('<Configure>',
            lambda e: self.settings_canvas.itemconfig(
                self._settings_win, width=e.width))
        self.settings_canvas.bind('<MouseWheel>',
            lambda e: self.settings_canvas.yview_scroll(
                -1 * (e.delta // 120), 'units'))

        self.settings_canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        self._build_settings_content()

    def _build_settings_content(self):
        pal = self.p()
        inner = self.settings_inner
        for w in inner.winfo_children():
            w.destroy()

        def section(title):
            hdr = tk.Frame(inner, bg=pal['S1'])
            hdr.pack(fill='x', padx=12, pady=(10, 0))
            tk.Label(hdr, text=title, bg=pal['S1'], fg=pal['GRY'],
                     font=('Segoe UI', 8, 'bold')).pack(side='left', padx=10, pady=6)
            tk.Frame(inner, bg=pal['BDR'], height=1).pack(fill='x', padx=12)
            body = tk.Frame(inner, bg=pal['S2'])
            body.pack(fill='x', padx=12, pady=(0, 4))
            return body

        def row(parent, label, build_fn):
            r = tk.Frame(parent, bg=pal['S2'])
            r.pack(fill='x', padx=10, pady=5)
            tk.Label(r, text=label, bg=pal['S2'], fg=pal['FG'],
                     font=('Segoe UI', 8), width=22, anchor='w').pack(side='left')
            build_fn(r)
            return r

        def radio(parent, text, var, value, cmd):
            tk.Radiobutton(parent, text=text, variable=var, value=value,
                bg=pal['S2'], fg=pal['FG'], selectcolor=pal['BDR'],
                activebackground=pal['S2'], activeforeground=pal['FG'],
                font=('Segoe UI', 8), command=cmd).pack(side='left', padx=(0, 8))

        def check(parent, text, var, cmd):
            tk.Checkbutton(parent, text=text, variable=var,
                bg=pal['S2'], fg=pal['FG'], selectcolor=pal['BDR'],
                activebackground=pal['S2'], activeforeground=pal['FG'],
                font=('Segoe UI', 8), command=cmd).pack(side='left')

        def scale_badge(parent, var, fmt, from_, to, res, cmd):
            badge = tk.Label(parent, text=fmt(var.get()),
                bg=pal['S2'], fg=pal['ACC'], font=('Segoe UI', 8, 'bold'), width=6)
            badge.pack(side='right')
            def _on(val, b=badge, f=fmt, c=cmd):
                b.config(text=f(float(val)))
                c(float(val))
            tk.Scale(parent, from_=from_, to=to, resolution=res, orient='horizontal',
                variable=var, bg=pal['S2'], fg=pal['FG'],
                troughcolor=pal['BDR'], highlightthickness=0, bd=0,
                sliderrelief='flat', showvalue=False,
                command=_on).pack(fill='x', side='left', expand=True)
            return badge

        # ── APPEARANCE ───────────────────────────
        ap = section('APPEARANCE')
        self._dark_var = tk.BooleanVar(value=self.settings.get('dark_mode', True))
        def build_theme(parent):
            radio(parent, 'Dark',  self._dark_var, True,  self._on_dark_mode_change)
            radio(parent, 'Light', self._dark_var, False, self._on_dark_mode_change)
        row(ap, 'Theme', build_theme)

        self._launch_tray_var = tk.BooleanVar(value=self.settings.get('launch_to_tray', False))
        def build_tray_startup(parent):
            state = 'normal' if TRAY_AVAILABLE else 'disabled'
            tk.Checkbutton(parent, text='Launch to tray on startup',
                variable=self._launch_tray_var, bg=pal['S2'], fg=pal['FG'],
                selectcolor=pal['BDR'], activebackground=pal['S2'],
                font=('Segoe UI', 8), state=state,
                command=self._on_launch_tray_change).pack(side='left')
            if not TRAY_AVAILABLE:
                tk.Label(parent, text='(pip install pystray pillow)',
                    bg=pal['S2'], fg=pal['DIM'], font=('Segoe UI', 7)).pack(side='left', padx=(6, 0))
        row(ap, 'System tray', build_tray_startup)

        # ── MAIN PAGE WIDGETS ─────────────────────
        wp = section('MAIN PAGE WIDGETS')
        self._show_baseline_var  = tk.BooleanVar(value=self.settings.get('show_baseline_label', True))
        self._show_threshold_var = tk.BooleanVar(value=self.settings.get('show_threshold_label', True))
        self._show_log_var       = tk.BooleanVar(value=self.settings.get('show_log', True))
        self._show_peak_var      = tk.BooleanVar(value=self.settings.get('show_peak_hold', False))
        for label, var, key in [
            ('Baseline info label',   self._show_baseline_var,  'show_baseline_label'),
            ('Threshold info label',  self._show_threshold_var, 'show_threshold_label'),
            ('Mini log',              self._show_log_var,       'show_log'),
            ('Peak hold indicator',   self._show_peak_var,      'show_peak_hold'),
        ]:
            def build_check(parent, v=var, k=key):
                check(parent, '', v, lambda vv=v, kk=k: self._on_widget_toggle(vv, kk))
            row(wp, label, build_check)

        # ── MONITORING BEHAVIOR ───────────────────
        mb_sec = section('MONITORING BEHAVIOR')
        self._recovery_var = tk.DoubleVar(value=self.settings.get('recovery_sec', 2.0))
        row(mb_sec, 'Recovery time',
            lambda parent: scale_badge(parent, self._recovery_var,
                lambda v: f'{v:.1f} s', 0.5, 10.0, 0.5, self._on_recovery_change))

        self._peak_dur_var = tk.DoubleVar(value=self.settings.get('peak_hold_sec', 2.0))
        row(mb_sec, 'Peak hold duration',
            lambda parent: scale_badge(parent, self._peak_dur_var,
                lambda v: f'{v:.1f} s', 0.5, 5.0, 0.5, self._on_peak_dur_change))

        # ── AUDIO ENGINE ──────────────────────────
        ae = section('AUDIO ENGINE')
        self._audio_mode_var = tk.StringVar(value=self.settings.get('audio_mode', 'standard'))
        def build_audio_mode(parent):
            f = tk.Frame(parent, bg=pal['S2'])
            f.pack(side='left')
            for val, lbl in [('standard', 'Standard (MME)'),
                              ('wasapi_shared', 'WASAPI shared'),
                              ('wasapi_exclusive', 'WASAPI exclusive')]:
                tk.Radiobutton(f, text=lbl, variable=self._audio_mode_var, value=val,
                    bg=pal['S2'], fg=pal['FG'], selectcolor=pal['BDR'],
                    activebackground=pal['S2'], font=('Segoe UI', 8),
                    command=self._on_audio_mode_change).pack(anchor='w', pady=1)
        row(ae, 'Audio mode', build_audio_mode)

        self._output_mode_var = tk.StringVar(value=self.settings.get('output_mode', 'gate'))
        def build_output_mode(parent):
            f = tk.Frame(parent, bg=pal['S2'])
            f.pack(side='left')
            tk.Radiobutton(f, text='Noise gate (mute completely)',
                variable=self._output_mode_var, value='gate',
                bg=pal['S2'], fg=pal['FG'], selectcolor=pal['BDR'],
                activebackground=pal['S2'], font=('Segoe UI', 8),
                command=self._on_output_mode_change).pack(anchor='w', pady=1)
            tk.Radiobutton(f, text='Compressor (reduce volume)',
                variable=self._output_mode_var, value='compressor',
                bg=pal['S2'], fg=pal['FG'], selectcolor=pal['BDR'],
                activebackground=pal['S2'], font=('Segoe UI', 8),
                command=self._on_output_mode_change).pack(anchor='w', pady=1)
        row(ae, 'Output mode', build_output_mode)

        self._comp_var = tk.IntVar(value=self.settings.get('compressor_reduction', 80))
        self._comp_row = row(ae, 'Reduction amount',
            lambda parent: scale_badge(parent, self._comp_var,
                lambda v: f'{int(v)} %', 0, 100, 1, self._on_comp_change))
        self._update_comp_row_visibility()

        tk.Frame(inner, bg=pal['BG'], height=16).pack()

    def _update_comp_row_visibility(self):
        if not hasattr(self, '_comp_row') or self._comp_row is None: return
        if self._output_mode_var.get() == 'compressor':
            self._comp_row.pack(fill='x', padx=10, pady=5)
        else:
            self._comp_row.pack_forget()

    # ──────────────────────────────────────────────
    # Page toggling
    # ──────────────────────────────────────────────
    def _toggle_page(self):
        if self._on_settings_page:
            self.settings_page.pack_forget()
            self.main_page.pack(fill='both', expand=True)
            self._on_settings_page = False
            self.gear_btn.config(text='⚙')
        else:
            self.main_page.pack_forget()
            self.settings_page.pack(fill='both', expand=True)
            self._on_settings_page = True
            self.gear_btn.config(text='✕')

    # ──────────────────────────────────────────────
    # Theme application
    # ──────────────────────────────────────────────
    def _apply_theme(self):
        pal = self.p()
        self.root.configure(bg=pal['BG'])
        self._update_ttk_style()

        # Header
        for w in [self.hdr, self.status_frame]:
            w.config(bg=pal['S1'])
        self.hdr_icon.config(bg=pal['S1'], fg=pal['ACC'])
        self.hdr_title.config(bg=pal['S1'], fg=pal['FG'])
        self.dot.config(bg=pal['S1'], fg=pal['DIM'])
        self.status_lbl.config(bg=pal['S1'], fg=pal['DIM'])
        self.gear_btn.config(bg=pal['S1'], fg=pal['GRY'],
                             activebackground=pal['S2'], activeforeground=pal['FG'])
        if self.tray_btn:
            self.tray_btn.config(bg=pal['S1'], fg=pal['GRY'],
                                 activebackground=pal['S2'], activeforeground=pal['FG'])
        self.hdr_sep.config(bg=pal['BDR'])
        self.content.config(bg=pal['BG'])

        # Main page backgrounds
        self.main_page.config(bg=pal['BG'])
        self.dev_frame.config(bg=pal['BG'])
        for f in self._dev_cell_frames + self._dev_top_frames:
            f.config(bg=pal['BG'])
        for lbl in self._dev_icon_labels:
            lbl.config(bg=pal['BG'], fg=pal['GRY'])
        for lbl in self._dev_tip_labels:
            lbl.config(bg=pal['BG'], fg=pal['DIM'])
        self.dev_sep.config(bg=pal['BDR'])

        self.mf.config(bg=pal['BG'])
        self.canvas.config(bg=pal['METER_BG'], highlightbackground=pal['BDR'])
        self.canvas.itemconfig(self.mask_rect, fill=pal['METER_BG'])
        self.canvas.itemconfig(self.base_line, fill=pal['ACC'])
        self.canvas.itemconfig(self.thresh_line, fill=pal['RED'])
        self.canvas.itemconfig(self.thresh_tag, fill=pal['RED'])
        for lbl in self.scale_labels:
            lbl.config(bg=pal['BG'], fg=pal['DIM'])
        self.info_frame.config(bg=pal['BG'])
        self.base_lbl_text.config(bg=pal['BG'], fg=pal['DIM'])
        self.base_lbl.config(bg=pal['BG'], fg=pal['ACC'])
        self.thresh_info.config(bg=pal['BG'], fg=pal['RED'])
        self.meter_sep.config(bg=pal['BDR'])

        self.slider_frame.config(bg=pal['BG'])
        self.sl_head_lbl.config(bg=pal['BG'], fg=pal['GRY'])
        self.sl_badge.config(bg=pal['BG'], fg=pal['RED'])
        self.thresh_scale.config(bg=pal['BG'], fg=pal['FG'], troughcolor=pal['S2'])
        self.slider_sep.config(bg=pal['BDR'])

        self.log.config(bg=pal['BG'], fg=pal['DIM'])
        for tag, ck in [('cal','ACC'),('start','GRN'),('warn','YEL'),('recover','YEL')]:
            self.log.tag_config(tag, foreground=pal[ck])

        # Settings page
        if hasattr(self, 'settings_canvas'):
            self.settings_page.config(bg=pal['BG'])
            self.settings_canvas.config(bg=pal['BG'])
            self.settings_inner.config(bg=pal['BG'])
            self._build_settings_content()

        self._on_bar_resize()

    def _apply_widget_visibility(self):
        s = self.settings
        # Unpack all info children, then re-pack visible ones in order
        self.base_lbl_text.pack_forget()
        self.base_lbl.pack_forget()
        self.thresh_info.pack_forget()
        if s.get('show_baseline_label', True):
            self.base_lbl_text.pack(side='left')
            self.base_lbl.pack(side='left', padx=(3, 0))
        if s.get('show_threshold_label', True):
            self.thresh_info.pack(side='right')
        if s.get('show_log', True):
            self.log.pack(fill='x', padx=14, pady=(0, 10))
        else:
            self.log.pack_forget()

    # ──────────────────────────────────────────────
    # Settings change handlers
    # ──────────────────────────────────────────────
    def _on_device_change(self, attr_var, setting_key):
        self.settings[setting_key] = getattr(self, attr_var).get()
        self._save_settings()

    def _on_thresh_change(self, *_):
        t = self.thresh_var.get()
        self._threshold_pct = t
        self.settings['threshold'] = t
        self.sl_badge.config(text=f'{t} %')
        self.thresh_info.config(text=f'Threshold  {t} %')
        self._redraw_thresh(t)
        self._save_settings()

    def _on_dark_mode_change(self):
        self.settings['dark_mode'] = self._dark_var.get()
        self._save_settings()
        self._apply_theme()

    def _on_launch_tray_change(self):
        self.settings['launch_to_tray'] = self._launch_tray_var.get()
        self._save_settings()

    def _on_widget_toggle(self, var, key):
        self.settings[key] = var.get()
        self._save_settings()
        self._apply_widget_visibility()

    def _on_recovery_change(self, val):
        v = float(val)
        self._recovery_sec = v
        self.settings['recovery_sec'] = v
        self._save_settings()

    def _on_peak_dur_change(self, val):
        v = float(val)
        self._peak_hold_sec = v
        self.settings['peak_hold_sec'] = v
        self._save_settings()

    def _on_audio_mode_change(self):
        self.settings['audio_mode'] = self._audio_mode_var.get()
        self._save_settings()
        self._load_devices()

    def _on_output_mode_change(self):
        self.settings['output_mode'] = self._output_mode_var.get()
        self._save_settings()
        self._update_comp_row_visibility()

    def _on_comp_change(self, val):
        self.settings['compressor_reduction'] = int(float(val))
        self._save_settings()

    # ──────────────────────────────────────────────
    # Gradient bar
    # ──────────────────────────────────────────────
    def _on_bar_resize(self, _=None):
        w = self.canvas.winfo_width()
        h = self.BAR_H
        if w < 4: return

        for gid in self._grad_ids: self.canvas.delete(gid)
        self._grad_ids.clear()

        segs = 160
        for i in range(segs):
            t = i / segs
            if t < 0.55:
                r = int(40 + 215 * (t / 0.55)); g = 185
            else:
                r = 255; g = int(185 * max(0, 1 - (t - 0.55) / 0.45))
            color = f'#{r:02x}{g:02x}1e'
            x1 = int(i * w / segs); x2 = int((i+1) * w / segs)
            gid = self.canvas.create_rectangle(x1, 0, x2, h, fill=color, outline='')
            self._grad_ids.append(gid)

        self.canvas.tag_raise(self.mask_rect)
        self.canvas.tag_raise(self.base_line)
        self.canvas.tag_raise(self.thresh_line)
        self.canvas.tag_raise(self.thresh_tag)
        self.canvas.tag_raise(self.peak_line)

        self._redraw_bar(self._display_pct)
        self._redraw_thresh(self.thresh_var.get())
        self._redraw_baseline()

    def _redraw_bar(self, pct: float):
        w = self.canvas.winfo_width()
        if w < 4: return
        x = int(pct / 100 * w)
        self.canvas.coords(self.mask_rect, x, 0, w, self.BAR_H)
        self.canvas.itemconfig(self.mask_rect, fill=self.p('METER_BG'))

    def _redraw_thresh(self, pct: int):
        w = self.canvas.winfo_width()
        if w < 4: return
        x = int(pct / 100 * w)
        self.canvas.coords(self.thresh_line, x, 0, x, self.BAR_H)
        if x > w * 0.82:
            self.canvas.coords(self.thresh_tag, x - 3, 4)
            self.canvas.itemconfig(self.thresh_tag, text=f'{pct} %', anchor='ne')
        else:
            self.canvas.coords(self.thresh_tag, x + 3, 4)
            self.canvas.itemconfig(self.thresh_tag, text=f'{pct} %', anchor='nw')

    def _redraw_baseline(self):
        w = self.canvas.winfo_width()
        if w < 4 or self.baseline_pct is None:
            self.canvas.coords(self.base_line, 0, 0, 0, 0); return
        x = int(self.baseline_pct / 100 * w)
        self.canvas.coords(self.base_line, x, 0, x, self.BAR_H)

    def _redraw_peak(self):
        w = self.canvas.winfo_width()
        if w < 4 or not self.settings.get('show_peak_hold', False):
            self.canvas.coords(self.peak_line, 0, 0, 0, 0); return
        x = int(self._peak_pct / 100 * w)
        self.canvas.coords(self.peak_line, x, 2, x, self.BAR_H - 2)

    # ──────────────────────────────────────────────
    # Device loading
    # ──────────────────────────────────────────────
    def _load_devices(self):
        try:
            mode = self.settings.get('audio_mode', 'standard')
            wasapi_only = mode in ('wasapi_shared', 'wasapi_exclusive')
            in_list, out_list, in_map, out_map = get_devices(wasapi_only=wasapi_only)
        except Exception as e:
            mb.showerror("Device error", f"Could not read audio devices:\n{e}"); return

        self.in_map = in_map; self.out_map = out_map
        self.in_cb['values'] = in_list
        self.out_cb['values'] = out_list

        saved_in  = self.settings.get('in_device', '')
        saved_out = self.settings.get('out_device', '')

        if saved_in and saved_in in in_list:
            self.in_var.set(saved_in)
        else:
            try:
                def_in = sd.query_devices(kind='input')['name']
                best_in = next((n for n in in_list if def_in in n),
                               in_list[0] if in_list else '')
            except Exception:
                best_in = in_list[0] if in_list else ''
            self.in_var.set(best_in)

        if saved_out and saved_out in out_list:
            self.out_var.set(saved_out)
        else:
            best_out = next((n for n in out_list
                             if any(k in n.lower()
                                    for k in ('cable input', 'vb-audio virtual cable'))),
                            out_list[0] if out_list else '')
            self.out_var.set(best_out)

    # ──────────────────────────────────────────────
    # Log
    # ──────────────────────────────────────────────
    def _log(self, msg: str, tag: str = 'info'):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log.config(state='normal')
        self.log.insert('1.0', f"[{ts}]  {msg}\n", tag)
        if int(self.log.index('end-1c').split('.')[0]) > 60:
            self.log.delete('60.0', 'end')
        self.log.config(state='disabled')

    # ──────────────────────────────────────────────
    # Header status
    # ──────────────────────────────────────────────
    def _set_status(self, state: str, override_text: str = ''):
        pal = self.p()
        col_key, txt = self.STATE_INFO.get(state, ('DIM', ''))
        if override_text: txt = override_text
        self.dot.config(fg=pal[col_key])
        self.status_lbl.config(text=txt, fg=pal[col_key])

    # ──────────────────────────────────────────────
    # Start / Stop
    # ──────────────────────────────────────────────
    def toggle(self):
        (self._start if self.state == IDLE else self._stop)()

    def _start(self):
        in_n = self.in_var.get(); out_n = self.out_var.get()
        if not in_n or not out_n:
            mb.showwarning("Selection missing",
                           "Please select an input and output device."); return
        try:
            mode = self.settings.get('audio_mode', 'standard')
            kw = dict(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                      channels=CHANNELS, dtype='float32',
                      device=(self.in_map[in_n], self.out_map[out_n]),
                      callback=self._audio_cb, latency='low')
            if mode in ('wasapi_shared', 'wasapi_exclusive'):
                try:
                    kw['extra_settings'] = sd.WasapiSettings(
                        exclusive=(mode == 'wasapi_exclusive'))
                except AttributeError:
                    mb.showwarning("WASAPI",
                        "WASAPI settings not supported. Falling back to standard mode.")
            self.stream = sd.Stream(**kw)
            self.stream.start()
        except Exception as e:
            mb.showerror("Error", f"Could not open audio stream:\n{e}"); return

        self.cal_samples = []; self.cal_start = time.perf_counter()
        self.baseline_pct = None; self.recovery_start = None
        self._display_pct = 0.0; self._detect_pct = 0.0
        self._peak_pct = 0.0; self._peak_time = 0.0
        self.state = CALIBRATING

        self.btn.config(text='⏹  Stop', bg='#b91c1c', activebackground='#cf2f2f')
        self.in_cb.config(state='disabled')
        self.out_cb.config(state='disabled')
        self._set_status(CALIBRATING)
        self._log('Calibration started (1.5 s) — stay quiet', 'cal')
        self._update_tray_icon()

    def _stop(self):
        self.state = IDLE
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None
        self.btn.config(text='▶  Start monitoring',
                         bg='#1f6feb', activebackground='#388bfd')
        self.in_cb.config(state='readonly')
        self.out_cb.config(state='readonly')
        self._set_status(IDLE)
        self._log('Stopped')
        self._update_tray_icon()

    # ──────────────────────────────────────────────
    # Audio callback  (audio thread — no GUI calls!)
    # ──────────────────────────────────────────────
    def _audio_cb(self, indata, outdata, frames, cb_time, status):
        rms  = float(np.sqrt(np.mean(indata ** 2)))
        dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -100.0
        raw  = dbfs_to_pct(dbfs)

        self._detect_pct  = DETECT_ALPHA  * raw + (1 - DETECT_ALPHA)  * self._detect_pct
        self._display_pct = DISPLAY_ALPHA * raw + (1 - DISPLAY_ALPHA) * self._display_pct

        now = time.perf_counter()
        t   = self._threshold_pct

        if self.state == CALIBRATING:
            self.cal_samples.append(raw)
            outdata[:] = indata
            if now - self.cal_start >= CAL_DURATION:
                self.baseline_pct = median(self.cal_samples)
                self.state = MONITORING
                self.root.after(0, self._gui_cal_done)

        elif self.state == MONITORING:
            if self._detect_pct < t * BASELINE_QUIET_RATIO:
                self.baseline_pct = (BASELINE_ALPHA * raw
                                     + (1 - BASELINE_ALPHA) * self.baseline_pct)
            if self._detect_pct >= t:
                self._apply_output(outdata, indata, muted=True)
                self.state = TRIGGERED; self.recovery_start = None
                self.root.after(0, self._gui_triggered)
            else:
                outdata[:] = indata

        elif self.state == TRIGGERED:
            self._apply_output(outdata, indata, muted=True)
            if self._detect_pct < t:
                if self.recovery_start is None:
                    self.recovery_start = now
                elif now - self.recovery_start >= self._recovery_sec:
                    self.state = MONITORING; self.recovery_start = None
                    self.root.after(0, self._gui_recovered)
            else:
                self.recovery_start = None
        else:
            outdata[:] = 0

    def _apply_output(self, outdata, indata, muted: bool):
        if not muted:
            outdata[:] = indata
        elif self.settings.get('output_mode') == 'compressor':
            reduction = 1.0 - (self.settings.get('compressor_reduction', 80) / 100.0)
            outdata[:] = indata * reduction
        else:
            outdata[:] = 0

    def _gui_cal_done(self):
        self.base_lbl.config(text=f'{int(self.baseline_pct)} %')
        self._redraw_baseline()
        self._set_status(MONITORING)
        self._log(f'Ready — baseline {int(self.baseline_pct)} %, '
                  f'threshold {self._threshold_pct} %', 'start')
        self._update_tray_icon()

    def _gui_triggered(self):
        self._log('Level exceeded threshold — output muted', 'warn')
        self._update_tray_icon()

    def _gui_recovered(self):
        self._log('Level recovered — monitoring resumed', 'recover')
        self._update_tray_icon()

    # ──────────────────────────────────────────────
    # GUI update loop  (~30 fps)
    # ──────────────────────────────────────────────
    def _update_loop(self):
        disp = self._display_pct
        now  = time.perf_counter()

        self._redraw_bar(disp)

        # Peak hold
        if self.settings.get('show_peak_hold', False) and self.state != IDLE:
            if disp > self._peak_pct:
                self._peak_pct = disp
                self._peak_time = now
            elif now - self._peak_time > self._peak_hold_sec:
                self._peak_pct = disp
            self._redraw_peak()
        else:
            self.canvas.coords(self.peak_line, 0, 0, 0, 0)

        # Baseline display
        if self.state in (MONITORING, TRIGGERED) and self.baseline_pct is not None:
            self.base_lbl.config(text=f'{int(self.baseline_pct)} %')
            self._redraw_baseline()

        # Header countdown
        if self.state == TRIGGERED:
            if self.recovery_start is not None:
                rem = max(0.0, self._recovery_sec - (now - self.recovery_start))
                self._set_status(TRIGGERED, f'Muted — resuming in {rem:.1f} s')
            else:
                self._set_status(TRIGGERED)
        elif self.state == CALIBRATING:
            rem = max(0.0, CAL_DURATION - (now - self.cal_start))
            self._set_status(CALIBRATING, f'Calibrating… {rem:.1f} s')

        self.root.after(1000 // GUI_FPS, self._update_loop)

    # ──────────────────────────────────────────────
    # System tray
    # ──────────────────────────────────────────────
    def _tray_color(self):
        if self.state == MONITORING:  return (63, 185, 80, 255)    # green
        if self.state == TRIGGERED:   return (210, 153, 34, 255)   # yellow
        if self.state == CALIBRATING: return (88, 166, 255, 255)   # blue
        return (72, 79, 88, 255)                                    # gray

    def _make_tray_image(self):
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([4, 4, 60, 60], fill=self._tray_color())
        return img

    def _start_tray(self):
        if not TRAY_AVAILABLE or self._tray_icon is not None: return
        menu = pystray.Menu(
            pystray.MenuItem('Show window', self._tray_show, default=True),
            pystray.MenuItem('Start / Stop monitoring', self._tray_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Quit', self._tray_quit),
        )
        self._tray_icon = pystray.Icon(
            'mic-monitor', self._make_tray_image(), 'Mic Monitor', menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _stop_tray(self):
        if self._tray_icon:
            try: self._tray_icon.stop()
            except Exception: pass
            self._tray_icon = None

    def _update_tray_icon(self):
        if not TRAY_AVAILABLE or self._tray_icon is None: return
        try: self._tray_icon.icon = self._make_tray_image()
        except Exception: pass

    def _minimize_to_tray(self):
        if not TRAY_AVAILABLE:
            mb.showinfo("System Tray",
                "Install pystray and Pillow to enable system tray:\n"
                "  pip install pystray pillow")
            return
        self._start_tray()
        self.root.withdraw()

    def _tray_show(self, icon=None, item=None):
        self.root.after(0, self._restore_window)

    def _restore_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_toggle(self, icon=None, item=None):
        self.root.after(0, self.toggle)

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self.on_close)

    # ──────────────────────────────────────────────
    # Close
    # ──────────────────────────────────────────────
    def on_close(self):
        self._stop_tray()
        if self.stream:
            self.stream.stop(); self.stream.close()
        self.root.destroy()

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    root = tk.Tk()
    app  = App(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()
