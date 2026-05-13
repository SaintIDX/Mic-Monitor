# Mic Monitor — Next Development Session

## Current state
- Main file: `mic-monitor-v3-eng.py`
- Working: calibration, adaptive baseline, threshold muting, 2s recovery, gradient meter
- UI: tkinter, dark theme, compact layout, ttk.Combobox for devices
- Audio: sounddevice.Stream, BLOCK_SIZE=512, CHANNELS=1, DBFS_FLOOR=-60, DISPLAY_ALPHA=0.08

---

## Goal: Add Settings Page + Persistence + System Tray

### Architecture change
Add a two-page UI inside the same window:
- **Main page** — current layout (meter, slider, button, log)
- **Settings page** — opened via ⚙ gear icon in header, replaces main content area

Navigation: small gear icon (⚙) top-right in header toggles between pages.
Settings are saved to `settings.json` in the same folder as the .py file.

---

## Settings page — full spec

### 1. DEVICES (persist last selection)
- Save `in_device` and `out_device` names to settings.json on change
- Load and auto-select on startup

### 2. APPEARANCE
- [ ] Dark mode / Light mode toggle (default: dark)
  - Dark: current palette (#0d1117 bg etc.)
  - Light: white/light gray bg, dark text, same accent colors
- [ ] Launch to system tray on startup (default: off)

### 3. MAIN PAGE WIDGETS (checkboxes — show/hide on main page)
- [x] Baseline info label (below meter)
- [x] Threshold info label (below meter)
- [x] Mini log (bottom of window)
- [ ] Peak hold indicator on meter bar (default: off)

### 4. MONITORING BEHAVIOR
- Recovery time: slider 0.5–10.0 s (default 2.0 s) — currently hardcoded RECOVERY_SEC=2.0
- Peak hold duration: slider 0.5–5.0 s (default 2.0 s) — how long peak marker stays visible

### 5. AUDIO ENGINE
- Audio mode: radio buttons
  - ○ Standard (MME) — default, most compatible
  - ○ WASAPI shared — lower latency ~5ms
  - ○ WASAPI exclusive — lowest latency ~3ms, may block other apps
- Output mode: radio buttons
  - ○ Noise gate — mutes completely when over threshold (current behavior)
  - ○ Compressor — smoothly reduces volume instead of full mute
    - If compressor: add "Reduction amount" slider (0–100%, default 80%)

---

## System tray spec
- Use `pystray` library (pip install pystray pillow)
- Tray icon: simple colored circle (green=monitoring, yellow=triggered, gray=stopped)
- Tray right-click menu:
  - Show window
  - Start / Stop monitoring
  - Quit
- "Minimize to tray" button in header (→ icon) — hides window, keeps running
- If "Launch to tray on startup" is on: window starts hidden, only tray icon shows

---

## Peak hold spec
- When enabled: show a thin white vertical line on the meter bar
- Line sits at the highest recent value
- Stays for `peak_hold_duration` seconds, then drops
- Implemented in _update_loop using:
  - `self._peak_pct` — current peak value
  - `self._peak_time` — perf_counter timestamp when peak was set
  - Update: if display_pct > peak_pct → set peak_pct=display_pct, peak_time=now
  - Decay: if now - peak_time > peak_hold_duration → peak_pct = display_pct
- Canvas element: `self.peak_line = canvas.create_line(...)` white, width=2

---

## settings.json structure
```json
{
  "in_device": "Microphone (Razer BlackShark V2 Pro)",
  "out_device": "CABLE Input (2- VB-Audio Virtual Cable)",
  "threshold": 70,
  "recovery_sec": 2.0,
  "peak_hold_sec": 2.0,
  "dark_mode": true,
  "launch_to_tray": false,
  "audio_mode": "standard",
  "output_mode": "gate",
  "compressor_reduction": 80,
  "show_baseline_label": true,
  "show_threshold_label": true,
  "show_log": true,
  "show_peak_hold": false
}
```
Load on startup with `json.load`, save on any change with `json.dump`.
If file doesn't exist, use defaults above.

---

## Compressor mode spec (output_mode = "compressor")
Instead of `outdata[:] = 0` when triggered, use:
```python
reduction = 1.0 - (self._compressor_reduction / 100.0)  # e.g. 0.20 for 80% reduction
outdata[:] = indata * reduction
```
State machine stays the same (TRIGGERED/MONITORING), only output changes.

---

## WASAPI mode spec
In `_start()`, change `sd.Stream()` call based on `audio_mode` setting:
```python
# standard (MME)
device=(self.in_map[in_n], self.out_map[out_n]), latency='low'

# WASAPI shared
device=(wasapi_in_idx, wasapi_out_idx), latency='low',
extra_settings=sd.WasapiSettings(exclusive=False)

# WASAPI exclusive
device=(wasapi_in_idx, wasapi_out_idx), latency='low',
extra_settings=sd.WasapiSettings(exclusive=True)
```
For WASAPI modes, filter device list to only show WASAPI devices in dropdowns.

---

## New dependencies
```
pystray>=0.19.0
Pillow>=9.0.0
```
Add to requirements.txt.

---

## Files to create/modify
- `mic-monitor-v3-eng.py` — main file, add all features
- `requirements.txt` — add pystray, Pillow
- `settings.json` — auto-created on first run
- `README.md` — update features list

---

## Implementation order (suggested)
1. settings.json load/save + persist devices & threshold
2. Settings page UI (gear icon, page toggle, all controls)
3. Light/dark mode
4. Recovery time slider wired to RECOVERY_SEC
5. Peak hold (canvas line + logic)
6. System tray (pystray)
7. Compressor mode
8. WASAPI mode
