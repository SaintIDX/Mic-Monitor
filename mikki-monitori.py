#!/usr/bin/env python3
"""
Mikki-monitori v1.0
Reaaliaikainen äänentason suodatin VB-Audio Virtual Cablen kautta.

Ei tallenna mitään levylle.
Ei verkkoyhteyksiä.
Puskuri: ~10 ms RAM:ssa, häviää ohjelman sulkeutuessa.
"""

import tkinter as tk
import tkinter.messagebox as mb

# ── Tarkista riippuvuudet ennen kuin ikkuna avataan ──────────────────────────
try:
    import sounddevice as sd
    import numpy as np
except ImportError as _e:
    _root = tk.Tk()
    _root.withdraw()
    mb.showerror(
        "Puuttuva kirjasto",
        f"Kirjasto puuttuu: {_e}\n\n"
        "Asenna se komennolla:\n"
        "    pip install sounddevice numpy\n\n"
        "Avaa Komentokehote (cmd) ja aja komento, sitten käynnistä ohjelma uudelleen."
    )
    raise SystemExit(1)

import math
import threading
import time
from datetime import datetime

# ── Vakiot ───────────────────────────────────────────────────────────────────
SAMPLE_RATE   = 48000
BLOCK_SIZE    = 512       # ~10.7 ms puskuri per kutsu
CHANNELS      = 1         # mono riittää puheelle
DBFS_FLOOR    = -60.0     # dBFS-arvo joka vastaa skaalaa 0  (≈ 30 dB SPL)
CAL_DURATION  = 1.5       # kalibrointiaika sekunteina
RECOVERY_SEC  = 2.0       # kuinka kauan tason pitää olla rajan alla ennen jatkumista
GUI_FPS       = 25        # GUI-päivitystahti

IDLE        = 'idle'
CALIBRATING = 'calibrating'
MONITORING  = 'monitoring'
TRIGGERED   = 'triggered'


# ── Apufunktiot ───────────────────────────────────────────────────────────────
def dbfs_to_scale(dbfs: float) -> float:
    """dBFS → 0-100 skaala  (0 = 30 dB SPL, 100 = 90 dB SPL)"""
    return max(0.0, min(100.0, (dbfs - DBFS_FLOOR) / (0.0 - DBFS_FLOOR) * 100.0))

def scale_to_spl(s: float) -> int:
    return round(30 + s * 0.6)

def fmt(s: float) -> str:
    return f"{int(s)}  ({scale_to_spl(s)} dB)"

def median(lst: list) -> float:
    if not lst:
        return 0.0
    s = sorted(lst)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


# ── Laitteiden haku (deduplikointi nimellä) ───────────────────────────────────
def get_devices():
    """Palauttaa (in_list, out_list, in_map, out_map) deduplikoituina."""
    in_list, out_list = [], []
    in_map,  out_map  = {}, {}
    seen_in, seen_out = set(), set()

    for i, d in enumerate(sd.query_devices()):
        name = d['name']
        # Suodatetaan pois "Microsoft Sound Mapper" (geneerinen alias)
        if 'sound mapper' in name.lower():
            continue
        if d['max_input_channels'] > 0 and name not in seen_in:
            in_list.append(name)
            in_map[name] = i
            seen_in.add(name)
        if d['max_output_channels'] > 0 and name not in seen_out:
            out_list.append(name)
            out_map[name] = i
            seen_out.add(name)

    return in_list, out_list, in_map, out_map


# ── Pääluokka ─────────────────────────────────────────────────────────────────
class MikkiMonitori:

    # ── värit ─────────────────────────────────────────────────────
    BG  = '#0d1117'
    BG2 = '#161b22'
    FG  = '#e6edf3'
    ACC = '#58a6ff'
    RED = '#f85149'
    GRY = '#8b949e'
    GRN = '#3fb950'

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Mikki-monitori v1.0")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        # tila
        self.state          = IDLE
        self.stream         = None
        self.cal_samples    = []
        self.cal_start      = 0.0
        self.baseline       = None    # skaalayksiköissä 0-100
        self.recovery_start = None    # perf_counter-aikaleima

        # audio-säikeen ja GUI-säikeen jaettu muuttuja
        self._current_scale = 0.0
        self._lock          = threading.Lock()
        self._delta_db      = 20      # välimuisti — päivitetään delta-sliderin muuttuessa

        self._build_ui()
        self._populate_devices()
        self._update_loop()

    # ─────────────────────────────────────────────────────────────
    # UI-rakennus
    # ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        C = self   # lyhenne väreille

        # otsikko
        tk.Label(self.root, text='🎙  Mikki-monitori', bg=C.BG, fg=C.ACC,
                 font=('Segoe UI', 14, 'bold')).pack(pady=(16, 10))

        # ── laitevalinta ──────────────────────────────────────────
        df = tk.Frame(self.root, bg=C.BG2, highlightbackground='#30363d',
                      highlightthickness=1)
        df.pack(fill='x', padx=16, pady=(0, 12))

        self.in_var  = tk.StringVar()
        self.out_var = tk.StringVar()

        for row, (lbl, var, attr) in enumerate([
            ('Sisääntulo  (oikea mikki):',        self.in_var,  'in_menu'),
            ('Ulostulo    (VB-Audio Cable Input):', self.out_var, 'out_menu'),
        ]):
            tk.Label(df, text=lbl, bg=C.BG2, fg=C.GRY,
                     font=('Segoe UI', 9)).grid(row=row, column=0, sticky='w',
                                                padx=10, pady=7)
            menu = tk.OptionMenu(df, var, '')
            menu.config(bg=C.BG2, fg=C.FG, activebackground='#21262d',
                        activeforeground=C.FG, highlightthickness=0, bd=0,
                        font=('Segoe UI', 9), width=32)
            menu['menu'].config(bg=C.BG2, fg=C.FG, activebackground='#30363d')
            menu.grid(row=row, column=1, padx=(4, 10), pady=7, sticky='w')
            setattr(self, attr, menu)

        # ── taso-mittari ──────────────────────────────────────────
        mf = tk.Frame(self.root, bg=C.BG, highlightbackground='#21262d',
                      highlightthickness=1)
        mf.pack(fill='x', padx=16, pady=(0, 10))

        tk.Label(mf, text='NYKYINEN TASO', bg=C.BG, fg=C.GRY,
                 font=('Segoe UI', 7)).pack(anchor='w', padx=14, pady=(10, 0))

        self.db_label = tk.Label(mf, text='--', bg=C.BG, fg=C.ACC,
                                  font=('Segoe UI', 20, 'bold'))
        self.db_label.pack(anchor='e', padx=14)

        bar_bg = tk.Frame(mf, bg='#21262d', height=28)
        bar_bg.pack(fill='x', padx=14, pady=(4, 2))
        bar_bg.pack_propagate(False)

        self.bar = tk.Canvas(bar_bg, height=28, bg='#21262d',
                             highlightthickness=0, bd=0)
        self.bar.pack(fill='both', expand=True)
        self.bar_rect = self.bar.create_rectangle(0, 0, 0, 28,
                                                   fill=C.GRN, outline='')
        self.bar_line = self.bar.create_line(0, 0, 0, 0, fill=C.RED, width=2)

        sf = tk.Frame(mf, bg=C.BG)
        sf.pack(fill='x', padx=14, pady=(2, 10))
        for txt in ['30 dB', '45 dB', '60 dB', '75 dB', '90 dB']:
            tk.Label(sf, text=txt, bg=C.BG, fg='#484f58',
                     font=('Segoe UI', 7)).pack(side='left', expand=True)

        # ── info-chipit ───────────────────────────────────────────
        cf = tk.Frame(self.root, bg=C.BG)
        cf.pack(fill='x', padx=16, pady=(0, 10))
        self.lbl_baseline   = self._chip(cf, 'Perustaso (kalibroitu)')
        self.lbl_threshold  = self._chip(cf, 'Pysäytysraja')

        # ── delta-slider ──────────────────────────────────────────
        slroot = tk.Frame(self.root, bg=C.BG)
        slroot.pack(fill='x', padx=16, pady=(0, 10))

        slhead = tk.Frame(slroot, bg=C.BG)
        slhead.pack(fill='x')
        tk.Label(slhead, text='Pysäytysraja — nousu perustasosta',
                 bg=C.BG, fg=C.GRY, font=('Segoe UI', 9)).pack(side='left')
        self.delta_badge = tk.Label(slhead, text='+20 dB', bg=C.BG,
                                     fg=C.RED, font=('Segoe UI', 9, 'bold'))
        self.delta_badge.pack(side='right')

        self.delta_var = tk.IntVar(value=20)
        self.delta_var.trace_add('write', self._on_delta_change)
        tk.Scale(slroot, from_=1, to=50, orient='horizontal',
                 variable=self.delta_var, bg=C.BG, fg=C.FG,
                 troughcolor='#21262d', highlightthickness=0, bd=0,
                 sliderrelief='flat', showvalue=False
                 ).pack(fill='x', pady=(4, 0))

        # ── statusbaari ───────────────────────────────────────────
        self.status_lbl = tk.Label(self.root, text='Odottaa käynnistystä',
                                    bg='#21262d', fg=C.GRY, pady=8,
                                    font=('Segoe UI', 9, 'bold'))
        self.status_lbl.pack(fill='x', padx=16, pady=(0, 8))

        # ── nappi ─────────────────────────────────────────────────
        self.main_btn = tk.Button(
            self.root, text='▶  Käynnistä monitorointi',
            bg='#238636', fg='white', activebackground='#2ea043',
            activeforeground='white', font=('Segoe UI', 10, 'bold'),
            bd=0, pady=10, cursor='hand2', command=self.toggle)
        self.main_btn.pack(fill='x', padx=16, pady=(0, 10))

        # ── loki ──────────────────────────────────────────────────
        lf = tk.Frame(self.root, bg=C.BG, highlightbackground='#21262d',
                      highlightthickness=1)
        lf.pack(fill='x', padx=16, pady=(0, 16))

        self.log = tk.Text(lf, height=6, bg=C.BG, fg=C.GRY,
                           font=('Consolas', 8), bd=0, state='disabled', wrap='word')
        self.log.pack(fill='x', padx=8, pady=8)
        self.log.tag_config('cal',     foreground='#58a6ff')
        self.log.tag_config('start',   foreground='#3fb950')
        self.log.tag_config('warn',    foreground='#d29922')
        self.log.tag_config('recover', foreground='#d29922')
        self.log.tag_config('stop',    foreground='#f85149')

    def _chip(self, parent, label_text):
        f = tk.Frame(parent, bg=self.BG, highlightbackground='#21262d',
                     highlightthickness=1)
        f.pack(side='left', expand=True, fill='x', padx=(0, 6))
        tk.Label(f, text=label_text.upper(), bg=self.BG, fg='#484f58',
                 font=('Segoe UI', 7)).pack(pady=(6, 2))
        v = tk.Label(f, text='--', bg=self.BG, fg=self.GRY,
                     font=('Segoe UI', 10, 'bold'))
        v.pack(pady=(0, 6))
        return v

    # ─────────────────────────────────────────────────────────────
    # Laitteiden täyttö pudotusvalikoihin
    # ─────────────────────────────────────────────────────────────
    def _populate_devices(self):
        try:
            in_list, out_list, in_map, out_map = get_devices()
        except Exception as e:
            mb.showerror("Laitevirhe", f"Äänilaitteita ei voitu lukea:\n{e}")
            return

        self.in_map  = in_map
        self.out_map = out_map

        def fill(menu_widget, var, items, auto_keywords=None):
            m = menu_widget['menu']
            m.delete(0, 'end')
            for item in items:
                m.add_command(label=item, command=lambda v=item: var.set(v))
            # automaattinen valinta
            if auto_keywords:
                match = next((n for n in items
                              if any(k in n.lower() for k in auto_keywords)), '')
                var.set(match if match else (items[0] if items else ''))
            else:
                var.set(items[0] if items else '')

        # sisääntulo: valitaan oletuslaite
        try:
            default_in_name = sd.query_devices(kind='input')['name']
        except Exception:
            default_in_name = ''

        m = self.in_menu['menu']
        m.delete(0, 'end')
        for item in in_list:
            m.add_command(label=item,
                          command=lambda v=item: self.in_var.set(v))
        best_in = next((n for n in in_list if default_in_name and default_in_name in n),
                       in_list[0] if in_list else '')
        self.in_var.set(best_in)

        # ulostulo: etsi VB-Audio / Cable automaattisesti
        fill(self.out_menu, self.out_var, out_list,
             auto_keywords=('cable input', 'vb-audio', 'virtual cable'))

    # ─────────────────────────────────────────────────────────────
    # Delta-sliderin muutos
    # ─────────────────────────────────────────────────────────────
    def _on_delta_change(self, *_):
        d = self.delta_var.get()
        self._delta_db = d
        self.delta_badge.config(text=f'+{d} dB')
        self._refresh_chips()

    def _threshold_scale(self):
        if self.baseline is None:
            return None
        return min(100.0, self.baseline + self._delta_db / 0.6)

    def _refresh_chips(self):
        t = self._threshold_scale()
        if t is not None:
            self.lbl_threshold.config(text=fmt(t), fg=self.RED)
        else:
            self.lbl_threshold.config(text='--', fg=self.GRY)
        if self.baseline is not None:
            self.lbl_baseline.config(text=fmt(self.baseline), fg=self.ACC)

    # ─────────────────────────────────────────────────────────────
    # Loki ja statusbaari
    # ─────────────────────────────────────────────────────────────
    def _log(self, msg: str, tag: str = 'info'):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log.config(state='normal')
        self.log.insert('1.0', f"[{ts}]  {msg}\n", tag)
        if int(self.log.index('end-1c').split('.')[0]) > 50:
            self.log.delete('50.0', 'end')
        self.log.config(state='disabled')

    def _set_status(self, text: str, style: str = 'idle'):
        palette = {
            'idle':    (self.GRY, '#21262d'),
            'cal':     (self.ACC, '#0c1829'),
            'active':  (self.GRN, '#0c1a0c'),
            'recover': ('#d29922', '#1a1500'),
            'stopped': (self.RED, '#1a0505'),
        }
        fg, bg = palette.get(style, palette['idle'])
        self.status_lbl.config(text=text, fg=fg, bg=bg)

    # ─────────────────────────────────────────────────────────────
    # Monitoroinnin käynnistys / pysäytys
    # ─────────────────────────────────────────────────────────────
    def toggle(self):
        if self.state == IDLE:
            self._start()
        else:
            self._stop()

    def _start(self):
        in_name  = self.in_var.get()
        out_name = self.out_var.get()
        if not in_name or not out_name:
            mb.showwarning("Valinta puuttuu", "Valitse sisääntulo- ja ulostulolaite.")
            return

        try:
            self.stream = sd.Stream(
                samplerate = SAMPLE_RATE,
                blocksize  = BLOCK_SIZE,
                channels   = CHANNELS,
                dtype      = 'float32',
                device     = (self.in_map[in_name], self.out_map[out_name]),
                callback   = self._audio_callback,
                latency    = 'low',
            )
            self.stream.start()
        except Exception as e:
            mb.showerror("Virtausvirhe", f"Äänivirran avaus epäonnistui:\n{e}")
            return

        self.cal_samples    = []
        self.cal_start      = time.perf_counter()
        self.baseline       = None
        self.recovery_start = None
        self.state          = CALIBRATING

        self.main_btn.config(text='⏹  Pysäytä',
                              bg='#b91c1c', activebackground='#991515')
        self.in_menu.config(state='disabled')
        self.out_menu.config(state='disabled')
        self._set_status('🔵  Kalibroidaan — pysy hiljaa...', 'cal')
        self._log('Kalibrointi käynnistetty (1.5 s)', 'cal')

    def _stop(self):
        self.state = IDLE
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        self.main_btn.config(text='▶  Käynnistä monitorointi',
                              bg='#238636', activebackground='#2ea043')
        self.in_menu.config(state='normal')
        self.out_menu.config(state='normal')
        self._set_status('Pysäytetty manuaalisesti', 'idle')
        self._log('Pysäytetty manuaalisesti')

    # ─────────────────────────────────────────────────────────────
    # Audio-callback — suoritetaan audio-säikeessä
    # EI tkinter-kutsuja tässä suoraan!
    # ─────────────────────────────────────────────────────────────
    def _audio_callback(self, indata, outdata, frames, cb_time, status):
        # laske äänentaso
        rms   = float(np.sqrt(np.mean(indata ** 2)))
        dbfs  = 20.0 * math.log10(rms) if rms > 1e-9 else -100.0
        scale = dbfs_to_scale(dbfs)

        with self._lock:
            self._current_scale = scale

        now = time.perf_counter()
        t   = self._threshold_scale()   # None tai 0-100 skaala-arvo

        if self.state == CALIBRATING:
            self.cal_samples.append(scale)
            outdata[:] = indata          # läpipäästö kalibroinnin aikana
            if now - self.cal_start >= CAL_DURATION:
                self.baseline = median(self.cal_samples)
                self.state    = MONITORING
                self.root.after(0, self._gui_calibration_done)

        elif self.state == MONITORING:
            if t is not None and scale >= t:
                outdata[:] = 0           # mykistä
                self.state          = TRIGGERED
                self.recovery_start = None
                self.root.after(0, self._gui_triggered)
            else:
                outdata[:] = indata      # päästä läpi

        elif self.state == TRIGGERED:
            outdata[:] = 0               # hiljaa koko triggered-ajan

            if t is None or scale < t:
                if self.recovery_start is None:
                    self.recovery_start = now
                elif now - self.recovery_start >= RECOVERY_SEC:
                    self.state          = MONITORING
                    self.recovery_start = None
                    self.root.after(0, self._gui_recovered)
            else:
                self.recovery_start = None   # nousi uudestaan → nollaa laskuri

        else:
            outdata[:] = 0

    # ─────────────────────────────────────────────────────────────
    # GUI-päivitykset — kutsutaan aina pääsäikeestä (root.after)
    # ─────────────────────────────────────────────────────────────
    def _gui_calibration_done(self):
        self._refresh_chips()
        self._set_status('🔴  Monitorointi käynnissä', 'active')
        self._log(f'Kalibrointi valmis — perustaso: {fmt(self.baseline)}', 'cal')
        self._log(f'Pysäytysraja: {fmt(self._threshold_scale())}', 'start')

    def _gui_triggered(self):
        self._set_status('⚠️  Taso ylittyi — jatkuu kun taso laskee 2 s', 'recover')
        self._log('Taso ylitti rajan — ulostulo mykistetty', 'warn')

    def _gui_recovered(self):
        self._set_status('🔴  Monitorointi käynnissä', 'active')
        self._log('Taso palautui — monitorointi jatkuu', 'recover')

    # ─────────────────────────────────────────────────────────────
    # GUI-päivityssilmukka  (~25 fps)
    # ─────────────────────────────────────────────────────────────
    def _update_loop(self):
        with self._lock:
            scale = self._current_scale

        w = self.bar.winfo_width()
        if w > 1:
            x = int(scale / 100 * w)
            t = self._threshold_scale()
            col = (self.RED if (t and scale >= t)
                   else ('#d29922' if scale >= 60 else self.GRN))
            self.bar.itemconfig(self.bar_rect, fill=col)
            self.bar.coords(self.bar_rect, 0, 0, x, 28)

            if t is not None:
                tx = int(t / 100 * w)
                self.bar.coords(self.bar_line, tx, 0, tx, 28)
            else:
                self.bar.coords(self.bar_line, 0, 0, 0, 0)

        if self.state in (CALIBRATING, MONITORING, TRIGGERED):
            t   = self._threshold_scale()
            col = (self.RED if (t and scale >= t)
                   else ('#d29922' if self.state == TRIGGERED else self.GRN))
            self.db_label.config(text=fmt(scale), fg=col)
        else:
            self.db_label.config(text='--', fg=self.ACC)

        # aikalaskuri statusbaarissa
        if self.state == TRIGGERED and self.recovery_start is not None:
            rem = max(0.0, RECOVERY_SEC - (time.perf_counter() - self.recovery_start))
            self.status_lbl.config(
                text=f'⚠️  Taso laskenut — jatkuu {rem:.1f} s kuluttua')
        elif self.state == CALIBRATING:
            rem = max(0.0, CAL_DURATION - (time.perf_counter() - self.cal_start))
            self.status_lbl.config(
                text=f'🔵  Kalibroidaan — pysy hiljaa... {rem:.1f} s')

        self.root.after(1000 // GUI_FPS, self._update_loop)

    # ─────────────────────────────────────────────────────────────
    def on_close(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
        self.root.destroy()


# ── Käynnistys ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    root = tk.Tk()
    app  = MikkiMonitori(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()
