#!/usr/bin/env python3
"""
Mikki-monitori v3.0  —  kompakti reaaliaikainen äänentason suodatin
Ei tallenna mitään levylle. Ei verkkoyhteyksiä.
"""

import tkinter as tk
from tkinter import ttk
import tkinter.messagebox as mb

try:
    import sounddevice as sd
    import numpy as np
except ImportError as _e:
    _r = tk.Tk(); _r.withdraw()
    mb.showerror("Puuttuva kirjasto",
        f"{_e}\n\nAsenna komennolla:\n    pip install sounddevice numpy")
    raise SystemExit(1)

import math, threading, time
from datetime import datetime

# ── Vakiot ────────────────────────────────────────────────────────────────────
SAMPLE_RATE          = 48000
BLOCK_SIZE           = 512        # ~10.7 ms
CHANNELS             = 1
DBFS_FLOOR           = -60.0
CAL_DURATION         = 1.5
RECOVERY_SEC         = 2.0
GUI_FPS              = 30
DISPLAY_ALPHA        = 0.08       # mittarin pehmeys (pienempi = rauhallisempi)
DETECT_ALPHA         = 0.30       # havaitsemisen pehmeys (nopeampi)
BASELINE_ALPHA       = 0.003      # adaptiivisen baselinen aikavakio (~6 s)
BASELINE_QUIET_RATIO = 0.65       # päivitetään vain kun taso < 65 % kynnyksestä

IDLE='idle'; CALIBRATING='calibrating'; MONITORING='monitoring'; TRIGGERED='triggered'

# ── Apufunktiot ───────────────────────────────────────────────────────────────
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

# ── Sovellus ──────────────────────────────────────────────────────────────────
class App:
    # Väripaletti
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

    BAR_H = 72   # mittaripalkin korkeus

    # Tila → (dot-väri, statusviesti)
    STATE_INFO = {
        IDLE:        (DIM,  'Pysäytetty'),
        CALIBRATING: (ACC,  'Kalibroidaan…'),
        MONITORING:  (GRN,  'Monitorointi käynnissä'),
        TRIGGERED:   (YEL,  'Mykistetty'),
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Mikki-monitori")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        # ── audio-tila ─────────────────────────────
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
    # ttk-tyyli tummalle teemalle
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
    # UI-rakennus
    # ──────────────────────────────────────────────
    def _build_ui(self):
        C = self

        # ══ HEADER ════════════════════════════════
        hdr = tk.Frame(self.root, bg=C.S1, height=46)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)

        tk.Label(hdr, text='🎙', bg=C.S1, fg=C.ACC,
                 font=('Segoe UI', 12)).pack(side='left', padx=(14, 4))
        tk.Label(hdr, text='Mikki-monitori', bg=C.S1, fg=C.FG,
                 font=('Segoe UI', 11, 'bold')).pack(side='left')

        # status-alue oikealla
        status_frame = tk.Frame(hdr, bg=C.S1)
        status_frame.pack(side='right', padx=14)
        self.dot  = tk.Label(status_frame, text='●', bg=C.S1, fg=C.DIM,
                              font=('Segoe UI', 9))
        self.dot.pack(side='left', padx=(0, 5))
        self.status_lbl = tk.Label(status_frame, text='Pysäytetty',
                                    bg=C.S1, fg=C.DIM, font=('Segoe UI', 8))
        self.status_lbl.pack(side='left')

        tk.Frame(self.root, bg=C.BDR, height=1).pack(fill='x')

        # ══ LAITTEET (yksi kompakti rivi) ════════════
        dev = tk.Frame(self.root, bg=C.BG)
        dev.pack(fill='x', padx=14, pady=(8, 8))

        for col, (icon, tip, attr_v, attr_cb) in enumerate([
            ('🎤', 'Sisääntulo (mikki)',        'in_var',  'in_cb'),
            ('🔊', 'Ulostulo (VB-Audio Cable)', 'out_var', 'out_cb'),
        ]):
            cell = tk.Frame(dev, bg=C.BG)
            cell.pack(side='left', expand=True, fill='x',
                      padx=(0, 8) if col == 0 else (0, 0))

            # ikoni + tooltip-teksti pienellä
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

        # ══ MITTARI ═══════════════════════════════
        mf = tk.Frame(self.root, bg=C.BG)
        mf.pack(fill='x', padx=14, pady=(12, 0))

        self.canvas = tk.Canvas(mf, height=C.BAR_H, bg='#151b23',
                                highlightthickness=1,
                                highlightbackground=C.BDR, bd=0)
        self.canvas.pack(fill='x')
        self.canvas.bind('<Configure>', self._on_bar_resize)

        # lagat z-järjestyksessä (alimmaisesta ylimmäiseen):
        # 1. gradient-suorakulmiot  2. mask  3. baseline-viiva  4. raja-viiva  5. raja-teksti
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

        # asteikko
        sc = tk.Frame(mf, bg=C.BG)
        sc.pack(fill='x', pady=(3, 0))
        for txt in ['0 %', '25 %', '50 %', '75 %', '100 %']:
            tk.Label(sc, text=txt, bg=C.BG, fg=C.DIM,
                     font=('Segoe UI', 7)).pack(side='left', expand=True)

        # ── info-rivi ─────────────────────────────
        info = tk.Frame(mf, bg=C.BG)
        info.pack(fill='x', pady=(6, 8))

        # vasemmalla: perustaso
        tk.Label(info, text='Perustaso', bg=C.BG, fg=C.DIM,
                 font=('Segoe UI', 8)).pack(side='left')
        self.base_lbl = tk.Label(info, text='--', bg=C.BG, fg=C.ACC,
                                  font=('Segoe UI', 8, 'bold'))
        self.base_lbl.pack(side='left', padx=(3, 0))

        # oikealla: raja
        self.thresh_info = tk.Label(info, text='Raja  70 %', bg=C.BG, fg=C.RED,
                                     font=('Segoe UI', 8, 'bold'))
        self.thresh_info.pack(side='right')

        tk.Frame(self.root, bg=C.BDR, height=1).pack(fill='x')

        # ══ SLIDER ════════════════════════════════
        sf = tk.Frame(self.root, bg=C.BG)
        sf.pack(fill='x', padx=14, pady=(10, 10))

        sl_head = tk.Frame(sf, bg=C.BG)
        sl_head.pack(fill='x')
        tk.Label(sl_head, text='Pysäytysraja', bg=C.BG, fg=C.GRY,
                 font=('Segoe UI', 8)).pack(side='left')
        self.sl_badge = tk.Label(sl_head, text='70 %', bg=C.BG, fg=C.RED,
                                  font=('Segoe UI', 9, 'bold'))
        self.sl_badge.pack(side='right')

        self.thresh_var = tk.IntVar(value=70)
        self.thresh_var.trace_add('write', self._on_thresh_change)

        tk.Scale(sf, from_=1, to=100, orient='horizontal',
                 variable=self.thresh_var, bg=C.BG, fg=C.FG,
                 troughcolor=C.S2, highlightthickness=0, bd=0,
                 sliderrelief='flat', showvalue=False
                 ).pack(fill='x', pady=(4, 0))

        tk.Frame(self.root, bg=C.BDR, height=1).pack(fill='x')

        # ══ NAPPI ═════════════════════════════════
        self.btn = tk.Button(self.root,
            text='▶  Käynnistä monitorointi',
            bg='#1f6feb', fg=C.FG, activebackground='#388bfd',
            activeforeground=C.FG, font=('Segoe UI', 10, 'bold'),
            bd=0, pady=11, cursor='hand2', relief='flat',
            command=self.toggle)
        self.btn.pack(fill='x', padx=14, pady=12)

        # ══ MINI-LOKI ═════════════════════════════
        self.log = tk.Text(self.root, height=4, bg=C.BG, fg=C.DIM,
                           font=('Consolas', 7), bd=0, state='disabled',
                           wrap='word', relief='flat', cursor='arrow')
        self.log.pack(fill='x', padx=14, pady=(0, 10))
        for tag, col in [('cal',C.ACC),('start',C.GRN),
                          ('warn',C.YEL),('recover',C.YEL)]:
            self.log.tag_config(tag, foreground=col)

    # ──────────────────────────────────────────────
    # Gradienttipalkki — rakennetaan kun canvas saa leveyden
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

        # z-järjestys: gradient → mask → baseline → threshold → text
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
        # teksti: vaihda puoli lähellä reunaa
        if x > w * 0.82:
            self.canvas.coords(self.thresh_tag, x - 3, 4)
            self.canvas.itemconfig(self.thresh_tag,
                                    text=f'{pct} %', anchor='ne')
        else:
            self.canvas.coords(self.thresh_tag, x + 3, 4)
            self.canvas.itemconfig(self.thresh_tag,
                                    text=f'{pct} %', anchor='nw')

    def _redraw_baseline(self):
        w = self.canvas.winfo_width()
        if w < 4 or self.baseline_pct is None:
            self.canvas.coords(self.base_line, 0, 0, 0, 0)
            return
        x = int(self.baseline_pct / 100 * w)
        self.canvas.coords(self.base_line, x, 0, x, self.BAR_H)

    # ──────────────────────────────────────────────
    # Laitteiden lataus
    # ──────────────────────────────────────────────
    def _load_devices(self):
        try:
            in_list, out_list, in_map, out_map = get_devices()
        except Exception as e:
            mb.showerror("Laitevirhe", f"Laitteita ei voitu lukea:\n{e}"); return

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
    # Kynnyksen muutos
    # ──────────────────────────────────────────────
    def _on_thresh_change(self, *_):
        t = self.thresh_var.get()
        self._threshold_pct = t
        self.sl_badge.config(text=f'{t} %')
        self.thresh_info.config(text=f'Raja  {t} %')
        self._redraw_thresh(t)

    # ──────────────────────────────────────────────
    # Loki
    # ──────────────────────────────────────────────
    def _log(self, msg: str, tag: str = 'info'):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log.config(state='normal')
        self.log.insert('1.0', f"[{ts}]  {msg}\n", tag)
        if int(self.log.index('end-1c').split('.')[0]) > 60:
            self.log.delete('60.0', 'end')
        self.log.config(state='disabled')

    # ──────────────────────────────────────────────
    # Header-status
    # ──────────────────────────────────────────────
    def _set_status(self, state: str, override_text: str = ''):
        col, txt = self.STATE_INFO.get(state, (self.DIM, ''))
        if override_text: txt = override_text
        self.dot.config(fg=col)
        self.status_lbl.config(text=txt, fg=col)

    # ──────────────────────────────────────────────
    # Monitoroinnin käynnistys / pysäytys
    # ──────────────────────────────────────────────
    def toggle(self):
        (self._start if self.state == IDLE else self._stop)()

    def _start(self):
        in_n = self.in_var.get(); out_n = self.out_var.get()
        if not in_n or not out_n:
            mb.showwarning("Valinta puuttuu",
                           "Valitse sisääntulo- ja ulostulolaite."); return
        try:
            self.stream = sd.Stream(
                samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                channels=CHANNELS, dtype='float32',
                device=(self.in_map[in_n], self.out_map[out_n]),
                callback=self._audio_cb, latency='low')
            self.stream.start()
        except Exception as e:
            mb.showerror("Virhe", f"Äänivirran avaus epäonnistui:\n{e}"); return

        self.cal_samples = []; self.cal_start = time.perf_counter()
        self.baseline_pct = None; self.recovery_start = None
        self._display_pct = 0.0; self._detect_pct = 0.0
        self.state = CALIBRATING

        self.btn.config(text='⏹  Pysäytä',
                         bg='#b91c1c', activebackground='#cf2f2f')
        self.in_cb.config(state='disabled')
        self.out_cb.config(state='disabled')
        self._set_status(CALIBRATING)
        self._log('Kalibrointi käynnistetty (1.5 s)', 'cal')

    def _stop(self):
        self.state = IDLE
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None
        self.btn.config(text='▶  Käynnistä monitorointi',
                         bg='#1f6feb', activebackground='#388bfd')
        self.in_cb.config(state='readonly')
        self.out_cb.config(state='readonly')
        self._set_status(IDLE)
        self._log('Pysäytetty')

    # ──────────────────────────────────────────────
    # Audio-callback  (audio-säie — ei GUI-kutsuja!)
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
            # adaptiivinen baseline — päivitetään vain hiljaisina hetkinä
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

    # ── GUI-tapahtumat audio-säikeestä ─────────────
    def _gui_cal_done(self):
        self.base_lbl.config(text=f'{int(self.baseline_pct)} %')
        self._redraw_baseline()
        self._set_status(MONITORING)
        self._log(f'Valmis — perustaso {int(self.baseline_pct)} %, raja {self._threshold_pct} %', 'start')

    def _gui_triggered(self):
        self._log('Taso ylitti rajan — mykistetty', 'warn')

    def _gui_recovered(self):
        self._log('Taso palautui — monitorointi jatkuu', 'recover')

    # ──────────────────────────────────────────────
    # GUI-päivityssilmukka  (~30 fps)
    # ──────────────────────────────────────────────
    def _update_loop(self):
        disp = self._display_pct
        t    = self._threshold_pct

        # palkki
        self._redraw_bar(disp)

        # adaptiivinen baseline jatkuvana päivityksenä
        if self.state in (MONITORING, TRIGGERED) and self.baseline_pct is not None:
            self.base_lbl.config(text=f'{int(self.baseline_pct)} %')
            self._redraw_baseline()

        # header-status + laskuri
        if self.state == TRIGGERED:
            if self.recovery_start is not None:
                rem = max(0.0, RECOVERY_SEC - (time.perf_counter() - self.recovery_start))
                self._set_status(TRIGGERED, f'Mykistetty — jatkuu {rem:.1f} s')
            else:
                self._set_status(TRIGGERED)
        elif self.state == CALIBRATING:
            rem = max(0.0, CAL_DURATION - (time.perf_counter() - self.cal_start))
            self._set_status(CALIBRATING, f'Kalibroidaan… {rem:.1f} s')

        self.root.after(1000 // GUI_FPS, self._update_loop)

    def on_close(self):
        if self.stream: self.stream.stop(); self.stream.close()
        self.root.destroy()

# ── Käynnistys ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    root = tk.Tk()
    app  = App(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()
