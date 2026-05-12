#!/usr/bin/env python3
"""
Mikki-monitori v2.0
Reaaliaikainen äänentason suodatin VB-Audio Virtual Cablen kautta.

Ei tallenna mitään levylle. Ei verkkoyhteyksiä.
Puskuri: ~10 ms RAM:ssa, häviää ohjelman sulkeutuessa.
"""

import tkinter as tk
import tkinter.messagebox as mb

try:
    import sounddevice as sd
    import numpy as np
except ImportError as _e:
    _root = tk.Tk()
    _root.withdraw()
    mb.showerror("Puuttuva kirjasto",
                 f"Kirjasto puuttuu: {_e}\n\n"
                 "Asenna komennolla:\n    pip install sounddevice numpy\n\n"
                 "Avaa cmd ja aja komento, sitten käynnistä uudelleen.")
    raise SystemExit(1)

import math, threading, time
from datetime import datetime

# ── Vakiot ────────────────────────────────────────────────────────────────────
SAMPLE_RATE   = 48000
BLOCK_SIZE    = 512          # ~10.7 ms puskuri
CHANNELS      = 1
DBFS_FLOOR    = -60.0        # tämä dBFS vastaa 0 %
CAL_DURATION  = 1.5          # kalibrointiaika (s)
RECOVERY_SEC  = 2.0          # palautumisaika (s)
GUI_FPS       = 30
DISPLAY_ALPHA = 0.10         # näyttöpehmeytys: pienempi = rauhallisempi (0.0-1.0)
DETECT_ALPHA  = 0.30         # havaitsemispehmeytys: reagoi nopeammin kuin näyttö

IDLE = 'idle';  CALIBRATING = 'calibrating'
MONITORING = 'monitoring';  TRIGGERED = 'triggered'

# Adaptiivinen baseline: päivittyy vain hiljaisina hetkinä
# alpha=0.003 → aikavakio ~6 s  (93 callbackia/s × 1/0.003 ≈ 360 framea ≈ 3.9 s)
BASELINE_ALPHA = 0.003
# Päivitetään vain kun taso on tämän verran alle kynnyksen (hiljainen hetki)
BASELINE_QUIET_MARGIN = 0.65   # < 65 % kynnyksestä = hiljainen

# ── Apufunktiot ───────────────────────────────────────────────────────────────
def dbfs_to_pct(dbfs: float) -> float:
    return max(0.0, min(100.0, (dbfs - DBFS_FLOOR) / (0.0 - DBFS_FLOOR) * 100.0))

def median(lst):
    if not lst: return 0.0
    s = sorted(lst); n = len(s); mid = n // 2
    return s[mid] if n % 2 else (s[mid-1] + s[mid]) / 2.0

def get_devices():
    in_list, out_list, in_map, out_map = [], [], {}, {}
    seen_in, seen_out = set(), set()
    for i, d in enumerate(sd.query_devices()):
        name = d['name']
        if 'sound mapper' in name.lower(): continue
        if d['max_input_channels'] > 0 and name not in seen_in:
            in_list.append(name); in_map[name] = i; seen_in.add(name)
        if d['max_output_channels'] > 0 and name not in seen_out:
            out_list.append(name); out_map[name] = i; seen_out.add(name)
    return in_list, out_list, in_map, out_map

# ── Pääluokka ─────────────────────────────────────────────────────────────────
class App:
    BG   = '#0d1117'
    BG2  = '#161b22'
    BDR  = '#21262d'
    FG   = '#e6edf3'
    ACC  = '#58a6ff'
    RED  = '#f85149'
    GRN  = '#3fb950'
    YEL  = '#d29922'
    GRY  = '#8b949e'
    DIM  = '#484f58'

    BAR_H = 110   # mittaripalkin korkeus pikseleinä

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Mikki-monitori v2.0")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self.state           = IDLE
        self.stream          = None
        self.cal_samples     = []
        self.cal_start       = 0.0
        self.baseline_pct    = None
        self.recovery_start  = None
        self._threshold_pct  = 70      # välimuisti audio-säikeelle
        self._lock           = threading.Lock()
        self._raw_pct        = 0.0     # viimeisin raaka arvo audio-säikeestä
        self._detect_pct     = 0.0     # pehmeä arvo havaitsemiseen
        self._display_pct    = 0.0     # pehmeä arvo näyttöä varten (rauhallinen)
        self._gradient_ids   = []      # canvas-elementit gradientille

        self._build_ui()
        self._populate_devices()
        self._update_loop()

    # ──────────────────────────────────────────────────────────────
    # UI
    # ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        C = self
        W = 480   # ikkunan leveys

        # otsikko
        tk.Label(self.root, text='🎙  Mikki-monitori', bg=C.BG, fg=C.ACC,
                 font=('Segoe UI', 14, 'bold')).pack(pady=(16, 10))

        # ── laitevalinta ──────────────────────────────────────────
        df = tk.Frame(self.root, bg=C.BG2, highlightbackground=C.BDR,
                      highlightthickness=1)
        df.pack(fill='x', padx=16, pady=(0, 14))

        self.in_var = tk.StringVar(); self.out_var = tk.StringVar()
        for row, (lbl, var, mattr) in enumerate([
            ('Sisääntulo  (oikea mikki):', self.in_var, 'in_menu'),
            ('Ulostulo    (VB-Audio Cable Input):', self.out_var, 'out_menu'),
        ]):
            tk.Label(df, text=lbl, bg=C.BG2, fg=C.GRY,
                     font=('Segoe UI', 9)).grid(row=row, column=0, sticky='w',
                                                padx=10, pady=7)
            m = tk.OptionMenu(df, var, '')
            m.config(bg=C.BG2, fg=C.FG, activebackground=C.BDR,
                     activeforeground=C.FG, highlightthickness=0, bd=0,
                     font=('Segoe UI', 9), width=30)
            m['menu'].config(bg=C.BG2, fg=C.FG, activebackground='#30363d')
            m.grid(row=row, column=1, padx=(4, 10), pady=7, sticky='w')
            setattr(self, mattr, m)

        # ── iso mittarialue ───────────────────────────────────────
        mf = tk.Frame(self.root, bg=C.BDR, highlightbackground=C.BDR,
                      highlightthickness=1)
        mf.pack(fill='x', padx=16, pady=(0, 14))

        inner = tk.Frame(mf, bg=C.BG, pady=16)
        inner.pack(fill='x', padx=1, pady=1)

        # gradienttipalkki
        bar_frame = tk.Frame(inner, bg=C.BG)
        bar_frame.pack(fill='x', padx=20, pady=(4, 4))

        self.canvas = tk.Canvas(bar_frame, height=C.BAR_H, bg='#1a1f28',
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill='x')
        self.canvas.bind('<Configure>', self._on_bar_resize)

        # kynnysviiva (päivitetään dynaamisesti)
        self.thresh_line = self.canvas.create_line(0, 0, 0, C.BAR_H,
                                                    fill=C.RED, width=3)
        self.thresh_tag  = self.canvas.create_text(0, 8, text='',
                                                    fill=C.RED,
                                                    font=('Segoe UI', 8, 'bold'),
                                                    anchor='n')
        # peittosuorakulmio (vaimentaa palkin oikean puolen)
        self.mask_rect = self.canvas.create_rectangle(0, 0, 0, C.BAR_H,
                                                       fill='#1a1f28', outline='')

        # asteikkotekstit
        scale_f = tk.Frame(inner, bg=C.BG)
        scale_f.pack(fill='x', padx=20, pady=(2, 0))
        for txt in ['0 %', '25 %', '50 %', '75 %', '100 %']:
            tk.Label(scale_f, text=txt, bg=C.BG, fg=C.DIM,
                     font=('Segoe UI', 7)).pack(side='left', expand=True)

        # ── perustaso-chip ────────────────────────────────────────
        bf = tk.Frame(self.root, bg=C.BG)
        bf.pack(fill='x', padx=16, pady=(0, 12))

        chip = tk.Frame(bf, bg=C.BDR, highlightbackground=C.BDR,
                        highlightthickness=1)
        chip.pack(side='left', fill='x', expand=True, padx=(0, 6))
        tk.Label(chip, text='PERUSTASO (KALIBROITU)', bg=C.BG, fg=C.DIM,
                 font=('Segoe UI', 7)).pack(pady=(6, 2), padx=1)
        self.baseline_lbl = tk.Label(chip, text='--', bg=C.BG, fg=C.GRY,
                                      font=('Segoe UI', 11, 'bold'))
        self.baseline_lbl.pack(pady=(0, 6))

        chip2 = tk.Frame(bf, bg=C.BDR, highlightbackground=C.BDR,
                         highlightthickness=1)
        chip2.pack(side='left', fill='x', expand=True)
        tk.Label(chip2, text='PYSÄYTYSRAJA', bg=C.BG, fg=C.DIM,
                 font=('Segoe UI', 7)).pack(pady=(6, 2), padx=1)
        self.threshold_lbl = tk.Label(chip2, text='70 %', bg=C.BG, fg=C.RED,
                                       font=('Segoe UI', 11, 'bold'))
        self.threshold_lbl.pack(pady=(0, 6))

        # ── kynnysslider (absoluuttinen 0-100 %) ─────────────────
        sf = tk.Frame(self.root, bg=C.BG)
        sf.pack(fill='x', padx=16, pady=(0, 12))

        sh = tk.Frame(sf, bg=C.BG)
        sh.pack(fill='x')
        tk.Label(sh, text='Pysäytysraja (absoluuttinen taso)',
                 bg=C.BG, fg=C.GRY, font=('Segoe UI', 9)).pack(side='left')
        self.thresh_badge = tk.Label(sh, text='70 %', bg=C.BG,
                                      fg=C.RED, font=('Segoe UI', 9, 'bold'))
        self.thresh_badge.pack(side='right')

        self.thresh_var = tk.IntVar(value=70)
        self.thresh_var.trace_add('write', self._on_thresh_change)
        tk.Scale(sf, from_=1, to=100, orient='horizontal',
                 variable=self.thresh_var, bg=C.BG, fg=C.FG,
                 troughcolor=C.BDR, highlightthickness=0, bd=0,
                 sliderrelief='flat', showvalue=False
                 ).pack(fill='x', pady=(4, 0))

        # ── statusbaari ───────────────────────────────────────────
        self.status_lbl = tk.Label(self.root, text='Odottaa käynnistystä',
                                    bg='#21262d', fg=C.GRY, pady=9,
                                    font=('Segoe UI', 9, 'bold'))
        self.status_lbl.pack(fill='x', padx=16, pady=(0, 8))

        # ── nappi ─────────────────────────────────────────────────
        self.btn = tk.Button(self.root, text='▶  Käynnistä monitorointi',
                              bg='#238636', fg='white', activebackground='#2ea043',
                              activeforeground='white', font=('Segoe UI', 10, 'bold'),
                              bd=0, pady=11, cursor='hand2', command=self.toggle)
        self.btn.pack(fill='x', padx=16, pady=(0, 10))

        # ── loki ──────────────────────────────────────────────────
        lf = tk.Frame(self.root, bg=C.BG, highlightbackground=C.BDR,
                      highlightthickness=1)
        lf.pack(fill='x', padx=16, pady=(0, 16))
        self.log = tk.Text(lf, height=5, bg=C.BG, fg=C.GRY,
                           font=('Consolas', 8), bd=0, state='disabled', wrap='word')
        self.log.pack(fill='x', padx=8, pady=8)
        for tag, col in [('cal', C.ACC), ('start', C.GRN),
                          ('warn', C.YEL), ('recover', C.YEL), ('info', C.GRY)]:
            self.log.tag_config(tag, foreground=col)

    # ──────────────────────────────────────────────────────────────
    # Gradienttipalkki — rakennetaan kun canvas saa oikean leveyden
    # ──────────────────────────────────────────────────────────────
    def _on_bar_resize(self, event=None):
        w = self.canvas.winfo_width()
        h = self.BAR_H
        if w < 2: return

        # poista vanhat gradient-segmentit
        for gid in self._gradient_ids:
            self.canvas.delete(gid)
        self._gradient_ids.clear()

        # piirrä 100 segmenttiä vihreästä punaiseen
        segs = 120
        for i in range(segs):
            t = i / segs
            # vihreä → keltainen → punainen
            if t < 0.6:
                r = int(255 * (t / 0.6))
                g = 185
            else:
                r = 255
                g = int(185 * (1 - (t - 0.6) / 0.4))
            b = 30
            color = f'#{r:02x}{g:02x}{b:02x}'
            x1 = int(i * w / segs)
            x2 = int((i + 1) * w / segs)
            gid = self.canvas.create_rectangle(x1, 0, x2, h,
                                                fill=color, outline='')
            self._gradient_ids.append(gid)

        # nosta peittosuorakulmio ja viivat gradient-segmenttien päälle
        self.canvas.tag_raise(self.mask_rect)
        self.canvas.tag_raise(self.thresh_line)
        self.canvas.tag_raise(self.thresh_tag)

        # piirrä heti oikeilla arvoilla
        self._draw_bar(self._display_pct)
        self._draw_thresh_line(self.thresh_var.get())

    def _draw_bar(self, pct: float):
        w = self.canvas.winfo_width()
        if w < 2: return
        x = int(pct / 100 * w)
        self.canvas.coords(self.mask_rect, x, 0, w, self.BAR_H)

    def _draw_thresh_line(self, pct: int):
        w = self.canvas.winfo_width()
        if w < 2: return
        x = int(pct / 100 * w)
        self.canvas.coords(self.thresh_line, x, 0, x, self.BAR_H)
        # label: vaihda puoli jos lähellä reunaa
        anchor = 'ne' if x > w * 0.85 else 'nw'
        self.canvas.coords(self.thresh_tag, x, 4)
        self.canvas.itemconfig(self.thresh_tag,
                                text=f' {pct} % ', anchor=anchor)

    # ──────────────────────────────────────────────────────────────
    # Laitteet
    # ──────────────────────────────────────────────────────────────
    def _populate_devices(self):
        try:
            in_list, out_list, in_map, out_map = get_devices()
        except Exception as e:
            mb.showerror("Laitevirhe", f"Laitteita ei voitu lukea:\n{e}")
            return
        self.in_map = in_map; self.out_map = out_map

        def fill(menu_w, var, items, keywords=None):
            m = menu_w['menu']; m.delete(0, 'end')
            for it in items:
                m.add_command(label=it, command=lambda v=it: var.set(v))
            if keywords:
                match = next((n for n in items
                              if any(k in n.lower() for k in keywords)), '')
                var.set(match or (items[0] if items else ''))
            else:
                var.set(items[0] if items else '')

        try:
            def_in = sd.query_devices(kind='input')['name']
        except Exception:
            def_in = ''
        best_in = next((n for n in in_list if def_in and def_in in n),
                       in_list[0] if in_list else '')
        m = self.in_menu['menu']; m.delete(0, 'end')
        for it in in_list:
            m.add_command(label=it, command=lambda v=it: self.in_var.set(v))
        self.in_var.set(best_in)

        fill(self.out_menu, self.out_var, out_list,
             keywords=('cable input', 'vb-audio virtual cable'))

    # ──────────────────────────────────────────────────────────────
    # Kynnys-sliderin muutos
    # ──────────────────────────────────────────────────────────────
    def _on_thresh_change(self, *_):
        t = self.thresh_var.get()
        self._threshold_pct = t
        self.thresh_badge.config(text=f'{t} %')
        self.threshold_lbl.config(text=f'{t} %')
        self._draw_thresh_line(t)

    # ──────────────────────────────────────────────────────────────
    # Loki ja status
    # ──────────────────────────────────────────────────────────────
    def _log(self, msg: str, tag: str = 'info'):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log.config(state='normal')
        self.log.insert('1.0', f"[{ts}]  {msg}\n", tag)
        if int(self.log.index('end-1c').split('.')[0]) > 60:
            self.log.delete('60.0', 'end')
        self.log.config(state='disabled')

    def _status(self, text: str, style: str = 'idle'):
        fg, bg = {
            'idle':    (self.GRY, '#21262d'),
            'cal':     (self.ACC, '#0c1829'),
            'active':  (self.GRN, '#0c1a0c'),
            'recover': (self.YEL, '#1a1500'),
        }.get(style, (self.GRY, '#21262d'))
        self.status_lbl.config(text=text, fg=fg, bg=bg)

    # ──────────────────────────────────────────────────────────────
    # Monitorointi
    # ──────────────────────────────────────────────────────────────
    def toggle(self):
        (self._start if self.state == IDLE else self._stop)()

    def _start(self):
        in_n = self.in_var.get(); out_n = self.out_var.get()
        if not in_n or not out_n:
            mb.showwarning("Valinta puuttuu", "Valitse sisääntulo ja ulostulo.")
            return
        try:
            self.stream = sd.Stream(
                samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                channels=CHANNELS, dtype='float32',
                device=(self.in_map[in_n], self.out_map[out_n]),
                callback=self._cb, latency='low')
            self.stream.start()
        except Exception as e:
            mb.showerror("Virhe", f"Äänivirran avaus epäonnistui:\n{e}"); return

        self.cal_samples = []; self.cal_start = time.perf_counter()
        self.baseline_pct = None; self.recovery_start = None
        self._display_pct = 0.0; self._detect_pct = 0.0
        self.state = CALIBRATING

        self.btn.config(text='⏹  Pysäytä', bg='#b91c1c', activebackground='#991515')
        self.in_menu.config(state='disabled'); self.out_menu.config(state='disabled')
        self._status('🔵  Kalibroidaan — pysy hiljaa...', 'cal')
        self._log('Kalibrointi käynnistetty (1.5 s)', 'cal')

    def _stop(self):
        self.state = IDLE
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None
        self.btn.config(text='▶  Käynnistä monitorointi',
                         bg='#238636', activebackground='#2ea043')
        self.in_menu.config(state='normal'); self.out_menu.config(state='normal')
        self._status('Pysäytetty manuaalisesti', 'idle')
        self._log('Pysäytetty manuaalisesti')

    # ──────────────────────────────────────────────────────────────
    # Audio-callback — audio-säie, EI GUI-kutsuja suoraan
    # ──────────────────────────────────────────────────────────────
    def _cb(self, indata, outdata, frames, cb_time, status):
        rms  = float(np.sqrt(np.mean(indata ** 2)))
        dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -100.0
        raw  = dbfs_to_pct(dbfs)

        # eksponentiaalinen liukuva keskiarvo kahdelle tasolle
        self._detect_pct  = DETECT_ALPHA * raw  + (1 - DETECT_ALPHA)  * self._detect_pct
        self._display_pct = DISPLAY_ALPHA * raw  + (1 - DISPLAY_ALPHA) * self._display_pct

        with self._lock:
            self._raw_pct = raw

        now = time.perf_counter()
        t   = self._threshold_pct   # absoluuttinen kynnys %

        if self.state == CALIBRATING:
            self.cal_samples.append(raw)
            outdata[:] = indata
            if now - self.cal_start >= CAL_DURATION:
                self.baseline_pct = median(self.cal_samples)
                self.state = MONITORING
                self.root.after(0, self._on_cal_done)

        elif self.state == MONITORING:
            # adaptiivinen baseline: päivitetään vain hiljaisina hetkinä
            if (self.baseline_pct is not None
                    and self._detect_pct < t * BASELINE_QUIET_MARGIN):
                self.baseline_pct = (BASELINE_ALPHA * raw
                                     + (1 - BASELINE_ALPHA) * self.baseline_pct)

            if self._detect_pct >= t:
                outdata[:] = 0
                self.state = TRIGGERED; self.recovery_start = None
                self.root.after(0, self._on_triggered)
            else:
                outdata[:] = indata

        elif self.state == TRIGGERED:
            outdata[:] = 0
            if self._detect_pct < t:
                if self.recovery_start is None:
                    self.recovery_start = now
                elif now - self.recovery_start >= RECOVERY_SEC:
                    self.state = MONITORING; self.recovery_start = None
                    self.root.after(0, self._on_recovered)
            else:
                self.recovery_start = None
        else:
            outdata[:] = 0

    # ── GUI-tapahtumat audio-säikeestä ─────────────────────────────
    def _on_cal_done(self):
        b = self.baseline_pct
        self.baseline_lbl.config(text=f'{int(b)} %', fg=self.ACC)
        self._status('🔴  Monitorointi käynnissä', 'active')
        self._log(f'Kalibrointi valmis — perustaso: {int(b)} %', 'cal')
        self._log(f'Pysäytysraja: {self._threshold_pct} %', 'start')

    def _on_triggered(self):
        self._status('⚠️  Taso ylittyi — jatkuu kun taso laskee 2 s', 'recover')
        self._log('Taso ylitti rajan — ulostulo mykistetty', 'warn')

    def _on_recovered(self):
        self._status('🔴  Monitorointi käynnissä', 'active')
        self._log('Taso palautui — monitorointi jatkuu', 'recover')

    # ──────────────────────────────────────────────────────────────
    # GUI-päivityssilmukka
    # ──────────────────────────────────────────────────────────────
    def _update_loop(self):
        disp = self._display_pct
        t    = self._threshold_pct

        # palkki
        self._draw_bar(disp)

        # baseline-chip päivittyy jatkuvasti adaptiivisen arvon mukaan
        if self.baseline_pct is not None and self.state in (MONITORING, TRIGGERED):
            self.baseline_lbl.config(text=f'{int(self.baseline_pct)} %', fg=self.ACC)

        # laskurit statusbaarissa
        if self.state == TRIGGERED and self.recovery_start is not None:
            rem = max(0.0, RECOVERY_SEC - (time.perf_counter() - self.recovery_start))
            self.status_lbl.config(text=f'⚠️  Taso laskenut — jatkuu {rem:.1f} s kuluttua')
        elif self.state == CALIBRATING:
            rem = max(0.0, CAL_DURATION - (time.perf_counter() - self.cal_start))
            self.status_lbl.config(text=f'🔵  Kalibroidaan — pysy hiljaa... {rem:.1f} s')

        self.root.after(1000 // GUI_FPS, self._update_loop)

    def on_close(self):
        if self.stream:
            self.stream.stop(); self.stream.close()
        self.root.destroy()

# ── Käynnistys ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    root = tk.Tk()
    app  = App(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()
