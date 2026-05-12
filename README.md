# 🎙 Mic Monitor

Real-time microphone audio limiter for Windows. Automatically mutes output when volume spikes — bangs, handling noise, shouting — and resumes after 2 seconds of normal levels. Routes audio through VB-Audio Virtual Cable so any app sees it as a microphone.

> Built with vibecoding and testing. No audio recorded. No network connections.

---

## How it works

```
Microphone  →  Mic Monitor  →  VB-Audio Virtual Cable  →  Discord / Teams / OBS
```

Calibrates your baseline on startup, then mutes output when the level exceeds your set threshold. Resumes automatically when quiet.

---

## Requirements

- Windows 10 / 11
- [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) — free, install once
- Python 3.8+ (only if running from source)

---

## Run from source

```bash
pip install -r requirements.txt
python mic-monitor-v3-eng.py
```

## Build .exe

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole mic-monitor-v3-eng.py
```

Or download a pre-built `.exe` from [Releases](../../releases).

---

## Quick start

1. Install [VB-Audio Virtual Cable](https://vb-audio.com/Cable/)
2. Start Mic Monitor
3. Select your microphone as **Input** 🎤 and **CABLE Input** as **Output** 🔊
4. In Discord / Teams / OBS — set microphone to **CABLE Output (VB-Audio Virtual Cable)**
5. Click **Start** — stay quiet 1.5 s for calibration
6. Adjust the threshold slider until the red line sits above your normal speaking level

Full instructions: [GUIDE.txt](GUIDE.txt)
