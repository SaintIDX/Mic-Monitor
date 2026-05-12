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

import math, threading, time
from datetime import datetime

# ── Constants ─────────────────────────────────────────────────────────────────
SAMPLE_RATE          = 48000
BLOCK_SIZE           = 512        # ~10.7 ms audio buffer
CHANNELS             = 1
DBFS_FLOOR           = -60.0
CAL_DURATION         = 1.5        # calibration window (seconds)
RECOVERY_SEC         = 2.0        # time below threshold before resuming (seconds)
GUI_FPS              = 30
DISPLAY_ALPHA        = 0.08       # meter smoothing (lower = calmer)
DETECT_ALPHA         = 0.30       # detection smoothing (faster response)
BASELINE_ALPHA       = 0.003      # adaptive baseline time constant (~6 s)
BASELINE_QUIET_RATIO = 0.65       # only update baseline when level < 65% of threshold

IDLE='idle'; CALIBRATING='calibrating'; MONITORING='monitoring'; TRIGGERED='triggered'

# ── Helpers ───────────────────────────────────────────────────────────────────
def dbfs_to_pct(dbfs: float) -> float:
    return max(0.0, min(100.0, (dbfs - DBFS_FLOOR) / (-DBFS_FLOOR) * 100.0))

def median(lst: list) -> float:
    if not lst: return 0.0
    s = sorted(lst); n = len(s); mid = n // 2
    return s[mid] if n % 2 else (s[mid-1] + s[mid]) / 2.0

def get_devices():
    in_list, out_list, in_map, out_map = [], [], {}, {}
    seen_in, seen_out = set(), set()
    for i, d in enumerate(sd.query_devices()):
        n = d['name']
        if 'sound mapper' in n.lower(): continue
        if d['max_input_channels'] > 0 and n not in seen_in:
            in_list.append(n); in_map[n] = i; seen_in.add(n)
        if d['max_output_channels'] > 0 and n not in seen_out:
            out_list.append(n); out_map[n] = i; seen_out.add(n)
    return in_list, out_list, in_map, out_map

# ── Application ───────────────────────────────────────────────────────────────
class App:
    # Color palette
    BG  = '#0d1117'
    S1  = '#161b22'
    S2  = '#21262d'
    BDR = '#30363d'
    FG  = '#e6edf3'
    GRY = '#8b949e'
    DIM = '#484f58'
    ACC = '#58a6ff'
    GRN = '#3fb950'
    YEL = '#d29922'
    RED = '#f85149'

    BAR_H = 72   # meter bar height in pixels

    # State → (dot color, status message)
    STATE_INFO = {
        IDLE:        (DIM,  'Stopped'),
        CALIBRATING: (ACC,  'Calibrating…'),
        MONITORING:  (GRN,  'Monitoring active'),
        TRIGGERED:   (YEL,  'Muted'),
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Mic Monitor")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        # audio state
        self.state           = IDLE
        self.stream          = None
        self.cal_samples     = []
        self.cal_start       = 0.0
        self.baseline_pct    = None
        self.recovery_start  = None
        self._threshold_pct  = 70
        self._detect_pct     = 0.0
        self._display_pct    = 0.0
        self._lock           = threading.Lock()
        self._grad_ids       = []

        self._configure_ttk()
        self._build_ui()
        self._load_devices()
        self._update_loop()

    # ──────────────────────────────────────────────
    # ttk dark theme
    # ──────────────────────────────────────────────
    def _configure_ttk(self):
        s = ttk.Style()
        s.theme_use('clam')
        s.configure('D.TCombobox',
            fieldbackground=self.S2, background=self.S2,
            foreground=self.FG, bordercolor=self.BDR,
            arrowcolor=self.GRY, selectbackground=self.S2,
            selectforeground=self.FG, padding=4)
        s.map('D.TCombobox',
            fieldbackground=[('readonly', self.S2)],
            foreground=[('readonly', self.FG)])

    # ──────────────────────────────────────────────
    # UI construction
    # ──────────────────────────────────────────────
    def _build_ui(self):
        C = self

        # ══ HEADER ════════════════════════════════
        hdr = tk.Frame(self.root, bg=C.S1, height=46)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)

        tk.Label(hdr, text='🎙', bg=C.S1, fg=C.ACC,
                 font=('Segoe UI', 12)).pack(side='left', padx=(14, 4))
        tk.Label(hdr, text='Mic Monitor', bg=C.S1, fg=C.FG,
                 font=('Segoe UI', 11, 'bold')).pack(side='left')

        # status indicator (right side)
        sf = tk.Frame(hdr, bg=C.S1)
        sf.pack(side='right', padx=14)
        self.dot = tk.Label(sf, text='●', bg=C.S1, fg=C.DIM,
                             font=('Segoe UI', 9))
        self.dot.pack(side='left', padx=(0, 5))
        self.status_lbl = tk.Label(sf, text='Stopped', bg=C.S1, fg=C.DIM,
                                    font=('Segoe UI', 8))
        self.status_lbl.pack(side='left')

        tk.Frame(self.root, bg=C.BDR, height=1).pack(fill='x')

        # ══ DEVICES (compact single row) ══════════
        dev = tk.Frame(self.root, bg=C.BG)
        dev.pack(fill='x', padx=14, pady=(8, 8))

        for col, (icon, tip, attr_v, attr_cb) in enumerate([
            ('🎤', 'Input (microphone)',      'in_var',  'in_cb'),
            ('🔊', 'Output (VB-Audio Cable)', 'out_var', 'out_cb'),
        ]):
            cell = tk.Frame(dev, bg=C.BG)
            cell.pack(side='left', expand=True, fill='x',
                      padx=(0, 8) if col == 0 else (0, 0))

            top = tk.Frame(cell, bg=C.BG)
            top.pack(fill='x')
            tk.Label(top, text=icon, bg=C.BG, fg=C.GRY,
                     font=('Segoe UI', 10)).pack(side='left')
            tk.Label(top, text=tip, bg=C.BG, fg=C.DIM,
                     font=('Segoe UI', 7)).pack(side='left', padx=(4, 0))

            var = tk.StringVar()
            setattr(self, attr_v, var)
            cb = ttk.Combobox(cell, textvariable=var, style='D.TCombobox',
                               state='readonly', width=22, font=('Segoe UI', 8))
            cb.pack(fill='x', pady=(2, 0))
            setattr(self, attr_cb, cb)

        tk.Frame(self.root, bg=C.BDR, height=1).pack(fill='x')

        # ══ METER ═════════════════════════════════
        mf = tk.Frame(self.root, bg=C.BG)
        mf.pack(fill='x', padx=14, pady=(12, 0))

        self.canvas = tk.Canvas(mf, height=C.BAR_H, bg='#151b23',
                                highlightthickness=1,
                                highlightbackground=C.BDR, bd=0)
        self.canvas.pack(fill='x')
        self.canvas.bind('<Configure>', self._on_bar_resize)

        # z-order (bottom to top):
        # 1. gradient segments  2. mask  3. baseline line  4. threshold line  5. threshold label
        self.mask_rect   = self.canvas.create_rectangle(0, 0, 0, C.BAR_H,
                                                         fill='#151b23', outline='')
        self.base_line   = self.canvas.create_line(0, 0, 0, 0,
                                                    fill=C.ACC, width=1, dash=(3, 4))
        self.thresh_line = self.canvas.create_line(0, 0, 0, C.BAR_H,
                                                    fill=C.RED, width=2)
        self.thresh_tag  = self.canvas.create_text(0, 4, text='',
                                                    fill=C.RED,
                                                    font=('Segoe UI', 7, 'bold'),
                                                    anchor='nw')

        # scale labels
        sc = tk.Frame(mf, bg=C.BG)
        sc.pack(fill='x', pady=(3, 0))
        for txt in ['0 %', '25 %', '50 %', '75 %', '100 %']:
            tk.Label(sc, text=txt, bg=C.BG, fg=C.DIM,
                     font=('Segoe UI', 7)).pack(side='left', expand=True)

        # info row: baseline left, threshold right
        info = tk.Frame(mf, bg=C.BG)
        info.pack(fill='x', pady=(6, 8))

        tk.Label(info, text='Baseline', bg=C.BG, fg=C.DIM,
                 font=('Segoe UI', 8)).pack(side='left')
        self.base_lbl = tk.Label(info, text='--', bg=C.BG, fg=C.ACC,
                                  font=('Segoe UI', 8, 'bold'))
        self.base_lbl.pack(side='left', padx=(3, 0))

        self.thresh_info = tk.Label(info, text='Threshold  70 %', bg=C.BG, fg=C.RED,
                                     font=('Segoe UI', 8, 'bold'))
        self.thresh_info.pack(side='right')

        tk.Frame(self.root, bg=C.BDR, height=1).pack(fill='x')

        # ══ SLIDER ════════════════════════════════
        sf2 = tk.Frame(self.root, bg=C.BG)
        sf2.pack(fill='x', padx=14, pady=(10, 10))

        sl_head = tk.Frame(sf2, bg=C.BG)
        sl_head.pack(fill='x')
        tk.Label(sl_head, text='Mute threshold', bg=C.BG, fg=C.GRY,
                 font=('Segoe UI', 8)).pack(side='left')
        self.sl_badge = tk.Label(sl_head, text='70 %', bg=C.BG, fg=C.RED,
                                  font=('Segoe UI', 9, 'bold'))
        self.sl_badge.pack(side='right')

        self.thresh_var = tk.IntVar(value=70)
        self.thresh_var.trace_add('write', self._on_thresh_change)

        tk.Scale(sf2, from_=1, to=100, orient='horizontal',
                 variable=self.thresh_var, bg=C.BG, fg=C.FG,
                 troughcolor=C.S2, highlightthickness=0, bd=0,
                 sliderrelief='flat', showvalue=False
                 ).pack(fill='x', pady=(4, 0))

        tk.Frame(self.root, bg=C.BDR, height=1).pack(fill='x')

        # ══ BUTTON ════════════════════════════════
        self.btn = tk.Button(self.root,
            text='▶  Start monitoring',
            bg='#1f6feb', fg=C.FG, activebackground='#388bfd',
            activeforeground=C.FG, font=('Segoe UI', 10, 'bold'),
            bd=0, pady=11, cursor='hand2', relief='flat',
            command=self.toggle)
        self.btn.pack(fill='x', padx=14, pady=12)

        # ══ MINI LOG ══════════════════════════════
        self.log = tk.Text(self.root, height=4, bg=C.BG, fg=C.DIM,
                           font=('Consolas', 7), bd=0, state='disabled',
                           wrap='word', relief='flat', cursor='arrow')
        self.log.pack(fill='x', padx=14, pady=(0, 10))
        for tag, col in [('cal',C.ACC),('start',C.GRN),
                          ('warn',C.YEL),('recover',C.YEL)]:
            self.log.tag_config(tag, foreground=col)

    # ──────────────────────────────────────────────
    # Gradient bar — built when canvas gets its width
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
                r = int(40 + 215 * (t / 0.55))
                g = 185
            else:
                r = 255
                g = int(185 * max(0, 1 - (t - 0.55) / 0.45))
            color = f'#{r:02x}{g:02x}1e'
            x1 = int(i * w / segs); x2 = int((i+1) * w / segs)
            gid = self.canvas.create_rectangle(x1, 0, x2, h,
                                                fill=color, outline='')
            self._grad_ids.append(gid)

        # z-order: gradient → mask → baseline → threshold → label
        self.canvas.tag_raise(self.mask_rect)
        self.canvas.tag_raise(self.base_line)
        self.canvas.tag_raise(self.thresh_line)
        self.canvas.tag_raise(self.thresh_tag)

        self._redraw_bar(self._display_pct)
        self._redraw_thresh(self.thresh_var.get())
        self._redraw_baseline()

    def _redraw_bar(self, pct: float):
        w = self.canvas.winfo_width()
        if w < 4: return
        x = int(pct / 100 * w)
        self.canvas.coords(self.mask_rect, x, 0, w, self.BAR_H)

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

    # ──────────────────────────────────────────────
    # Device loading
    # ──────────────────────────────────────────────
    def _load_devices(self):
        try:
            in_list, out_list, in_map, out_map = get_devices()
        except Exception as e:
            mb.showerror("Device error", f"Could not read audio devices:\n{e}"); return

        self.in_map = in_map; self.out_map = out_map
        self.in_cb['values']  = in_list
        self.out_cb['values'] = out_list

        try:
            def_in = sd.query_devices(kind='input')['name']
            best_in = next((n for n in in_list if def_in in n), in_list[0] if in_list else '')
        except Exception:
            best_in = in_list[0] if in_list else ''
        self.in_var.set(best_in)

        best_out = next((n for n in out_list
                         if any(k in n.lower() for k in ('cable input','vb-audio virtual cable'))),
                        out_list[0] if out_list else '')
        self.out_var.set(best_out)

    # ──────────────────────────────────────────────
    # Threshold slider change
    # ──────────────────────────────────────────────
    def _on_thresh_change(self, *_):
        t = self.thresh_var.get()
        self._threshold_pct = t
        self.sl_badge.config(text=f'{t} %')
        self.thresh_info.config(text=f'Threshold  {t} %')
        self._redraw_thresh(t)

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
        col, txt = self.STATE_INFO.get(state, (self.DIM, ''))
        if override_text: txt = override_text
        self.dot.config(fg=col)
        self.status_lbl.config(text=txt, fg=col)

    # ──────────────────────────────────────────────
    # Start / stop
    # ──────────────────────────────────────────────
    def toggle(self):
        (self._start if self.state == IDLE else self._stop)()

    def _start(self):
        in_n = self.in_var.get(); out_n = self.out_var.get()
        if not in_n or not out_n:
            mb.showwarning("Selection missing",
                           "Please select an input and output device."); return
        try:
            self.stream = sd.Stream(
                samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                channels=CHANNELS, dtype='float32',
                device=(self.in_map[in_n], self.out_map[out_n]),
                callback=self._audio_cb, latency='low')
            self.stream.start()
        except Exception as e:
            mb.showerror("Error", f"Could not open audio stream:\n{e}"); return

        self.cal_samples = []; self.cal_start = time.perf_counter()
        self.baseline_pct = None; self.recovery_start = None
        self._display_pct = 0.0; self._detect_pct = 0.0
        self.state = CALIBRATING

        self.btn.config(text='⏹  Stop', bg='#b91c1c', activebackground='#cf2f2f')
        self.in_cb.config(state='disabled')
        self.out_cb.config(state='disabled')
        self._set_status(CALIBRATING)
        self._log('Calibration started (1.5 s) — stay quiet', 'cal')

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
            # adaptive baseline — only update during quiet moments
            if self._detect_pct < t * BASELINE_QUIET_RATIO:
                self.baseline_pct = (BASELINE_ALPHA * raw
                                     + (1 - BASELINE_ALPHA) * self.baseline_pct)
            if self._detect_pct >= t:
                outdata[:] = 0
                self.state = TRIGGERED; self.recovery_start = None
                self.root.after(0, self._gui_triggered)
            else:
                outdata[:] = indata

        elif self.state == TRIGGERED:
            outdata[:] = 0
            if self._detect_pct < t:
                if self.recovery_start is None: self.recovery_start = now
                elif now - self.recovery_start >= RECOVERY_SEC:
                    self.state = MONITORING; self.recovery_start = None
                    self.root.after(0, self._gui_recovered)
            else:
                self.recovery_start = None
        else:
            outdata[:] = 0

    # GUI callbacks from audio thread
    def _gui_cal_done(self):
        self.base_lbl.config(text=f'{int(self.baseline_pct)} %')
        self._redraw_baseline()
        self._set_status(MONITORING)
        self._log(f'Ready — baseline {int(self.baseline_pct)} %, threshold {self._threshold_pct} %', 'start')

    def _gui_triggered(self):
        self._log('Level exceeded threshold — output muted', 'warn')

    def _gui_recovered(self):
        self._log('Level recovered — monitoring resumed', 'recover')

    # ──────────────────────────────────────────────
    # GUI update loop  (~30 fps)
    # ──────────────────────────────────────────────
    def _update_loop(self):
        disp = self._display_pct
        t    = self._threshold_pct

        self._redraw_bar(disp)

        # continuous adaptive baseline display
        if self.state in (MONITORING, TRIGGERED) and self.baseline_pct is not None:
            self.base_lbl.config(text=f'{int(self.baseline_pct)} %')
            self._redraw_baseline()

        # header status + countdown
        if self.state == TRIGGERED:
            if self.recovery_start is not None:
                rem = max(0.0, RECOVERY_SEC - (time.perf_counter() - self.recovery_start))
                self._set_status(TRIGGERED, f'Muted — resuming in {rem:.1f} s')
            else:
                self._set_status(TRIGGERED)
        elif self.state == CALIBRATING:
            rem = max(0.0, CAL_DURATION - (time.perf_counter() - self.cal_start))
            self._set_status(CALIBRATING, f'Calibrating… {rem:.1f} s')

        self.root.after(1000 // GUI_FPS, self._update_loop)

    def on_close(self):
        if self.stream: self.stream.stop(); self.stream.close()
        self.root.destroy()

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    root = tk.Tk()
    app  = App(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()
