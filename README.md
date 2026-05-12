# 🎙 Mic Monitor

Real-time microphone audio limiter for Windows. Sits between your microphone and other applications via VB-Audio Virtual Cable — automatically mutes the output when the volume exceeds a set threshold, and resumes when the level drops back to normal.

**No audio is recorded or stored. No network connections.**

---

## How it works

```
Your microphone
      ↓
 Mic Monitor  ←— calibrates baseline, applies threshold
      ↓            mutes output when too loud
VB-Audio Virtual Cable (free virtual audio driver)
      ↓
Discord / Teams / OBS / Zoom  ←— sees "CABLE Output" as mic
```

---

## Features

- **Auto-calibration** — 1.5 s baseline measurement on startup
- **Adaptive baseline** — continuously tracks ambient noise level during quiet moments
- **Smooth meter** — gradient bar with baseline marker (blue) and threshold line (red)
- **Auto-resume** — output unmutes automatically after 2 s below threshold
- **0–100 % scale** — simple, no dB knowledge required
- Available in **English** and **Finnish**

---

## Requirements

- Windows 10 / 11
- [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) — free, one-time install
- Python 3.8+ (only if running from source)

---

## Option A — Download ready-made .exe (no Python needed)

Go to the [**Releases**](../../releases) page and download `mic-monitor.exe`.  
VB-Audio Virtual Cable still needs to be installed separately — see [GUIDE.txt](GUIDE.txt).

---

## Option B — Run from source

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run (English)
python mic-monitor-v3-eng.py

# 3. Run (Finnish)
python mikki-monitori-v3.py
```

---

## Build .exe yourself

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole mic-monitor-v3-eng.py
# Output: dist/mic-monitor-v3-eng.exe
```

---

## Usage

1. Install [VB-Audio Virtual Cable](https://vb-audio.com/Cable/)
2. Start Mic Monitor
3. Select your real microphone as **Input** 🎤
4. Select **CABLE Input (VB-Audio Virtual Cable)** as **Output** 🔊
5. In Discord / Teams / OBS — set microphone to **CABLE Output (VB-Audio Virtual Cable)**
6. Click **Start monitoring** — stay quiet for 1.5 s calibration
7. Adjust the **Mute threshold** slider until the red line sits above your normal speaking level

Full instructions: [GUIDE.txt](GUIDE.txt) / [OHJEET.txt](OHJEET.txt)

---

## File overview

| File | Description |
|---|---|
| `mic-monitor-v3-eng.py` | Main program — English |
| `mikki-monitori-v3.py` | Main program — Finnish |
| `GUIDE.txt` | Full installation & usage guide (EN) |
| `OHJEET.txt` | Full installation & usage guide (FI) |
| `requirements.txt` | Python dependencies |

---

## Security

- Source code is fully readable — verify every line yourself
- No network access, no disk writes, no background processes
- Audio buffer: ~10 ms in RAM, cleared on exit
- VB-Audio Virtual Cable is a widely-used, trusted audio driver (since 2004)

---

## License

MIT — free to use, modify and distribute.
