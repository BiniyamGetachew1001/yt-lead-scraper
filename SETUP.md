# SETUP.md — YouTube Creator Discovery Scraper
## Environment & Dependencies Guide

---

### Prerequisites

| Requirement | Minimum Version | How to verify |
|---|---|---|
| Python | 3.9+ | `python3 --version` |
| pip | 23.0+ | `pip --version` |
| Disk space | ~300 MB | For Chromium binary |

---

### Step 1 — Create Your Project Directory

```bash
mkdir yt-creator-scraper
cd yt-creator-scraper
```

---

### Step 2 — Create and Activate a Virtual Environment

A virtual environment isolates every dependency inside this project folder
so nothing collides with your system Python or other projects.

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1

# If you get an ExecutionPolicy error, run this once in an admin PowerShell:
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**Windows (Command Prompt):**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

Your prompt will change to `(.venv) ...` when the environment is active.

Confirm you are using the venv interpreter, not the system Python:
```bash
# macOS / Linux
which python        # should show .venv/bin/python

# Windows
where python        # should show .venv\Scripts\python.exe
```

---

### Step 3 — Save requirements.txt

Create this file in your project root exactly as shown.
All versions are pinned to guarantee reproducible installs.

```text
# YouTube Creator Discovery Scraper — pinned dependencies
# Python 3.9+ required

# Browser automation — renders YouTube's JavaScript before extraction
playwright==1.44.0

# Tabular data and CSV checkpoint management
pandas==2.2.2

# HTML parsing (fallback extraction layer)
beautifulsoup4==4.12.3

# High-performance C parser backend for BeautifulSoup
lxml==5.2.2

# NOTE: asyncio is part of the Python standard library (Python 3.4+).
# It does NOT need to be installed via pip.
```

---

### Step 4 — Install All Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

The final lines of output should look like:
```
Successfully installed playwright-1.44.0 pandas-2.2.2 beautifulsoup4-4.12.3 lxml-5.2.2
```

---

### Step 5 — Provision the Chromium Browser Binary

Playwright does not bundle a browser. This one command downloads the
headless Chromium binary that the scraper will drive. Only Chromium is
needed; you do not need Firefox or WebKit.

```bash
playwright install chromium
```

Chromium is saved to a local cache directory (not your project folder):
- **macOS/Linux:** `~/.cache/ms-playwright/chromium-*/`
- **Windows:** `%USERPROFILE%\AppData\Local\ms-playwright\chromium-*\`

Verify success:
```bash
playwright --version
# Expected: Version 1.44.0
```

---

### Step 6 — Final Project Layout

```
yt-creator-scraper/
├── .venv/                    ← virtual environment (never edit manually)
├── requirements.txt          ← pinned dependencies
├── scraper.py                ← main application engine
├── SETUP.md                  ← this file
├── ARCHITECTURE.md           ← system design documentation
├── discovered_channels.csv   ← auto-created on first successful match
└── scraper.log               ← auto-created on first run (full debug log)
```

---

### Step 7 — Run the Scraper

```bash
# Make sure the venv is active first (you see (.venv) in your prompt)
python scraper.py
```

Live progress prints to your terminal. Everything — including verbose
DEBUG events — is also written to `scraper.log`.

To stop the scraper mid-run, press **Ctrl+C**. All channels accepted
before the interrupt are already written to `discovered_channels.csv`.
Restarting the script picks up exactly where it left off.

---

### Step 8 — Deactivate When Done

```bash
deactivate
```

---

### Legal Notice

YouTube's Terms of Service (section 5.B) restrict automated access.
This tool is provided for **educational and personal research purposes only**.
Use appropriate request delays, never scrape at high volume, and review
YouTube's current ToS before any commercial use.
