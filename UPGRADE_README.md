# UPGRADE_README.md — Web UI + Lead Generation Upgrade Guide

---

## What's New in This Version

| Module | Role |
|---|---|
| `APP_UI.py` | Streamlit web control panel — configure, run, and export from a browser tab |
| `LEAD_FINDER.py` | Enhanced async scraper — extracts email, Instagram, TikTok, Twitter/X, LinkedIn, and channel bio |
| `SHEETS_EXPORT.py` | One-click export to a formatted Google Sheet CRM |

---

## 1. Updated `requirements.txt`

Replace the contents of your `requirements.txt` with the following.
All versions are pinned for reproducibility.

```text
# ── Browser automation ──────────────────────────────────────────────────────
playwright==1.44.0

# ── Data handling ───────────────────────────────────────────────────────────
pandas==2.2.2

# ── HTML parsing (fallback extraction layer) ────────────────────────────────
beautifulsoup4==4.12.3
lxml==5.2.2

# ── Web UI ──────────────────────────────────────────────────────────────────
streamlit==1.35.0

# ── Google Sheets export ────────────────────────────────────────────────────
gspread==6.1.2
google-auth==2.29.0
google-auth-oauthlib==1.2.0

# ── NOTE: asyncio is a Python standard library module (Python 3.4+).
#    It does NOT need to be installed via pip.
```

### Install everything

```bash
# Ensure your virtual environment is active first
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

---

## 2. Final Project File Layout

```
yt-creator-scraper/
├── .venv/                    ← virtual environment
├── requirements.txt          ← updated dependencies
│
├── APP_UI.py                 ← Streamlit web UI  ← launch this
├── LEAD_FINDER.py            ← async scraping engine
├── SHEETS_EXPORT.py          ← Google Sheets export
│
├── scraper.py                ← original CLI scraper (kept for reference)
├── SETUP.md                  ← original environment setup guide
├── ARCHITECTURE.md           ← system design documentation
├── UPGRADE_README.md         ← this file
│
├── credentials.json          ← your Google service account key (you create this)
├── leads_export.csv          ← auto-created: local CSV backup after every lead
└── scraper.log               ← debug log (CLI scraper only)
```

---

## 3. Google Sheets API Setup (Free — Zero Cost)

This is a one-time, 10-minute process. After completing it you will have a
`credentials.json` file in your project folder and the export button will work.

---

### Step 1 — Create a Google Cloud Project

1. Go to **[console.cloud.google.com](https://console.cloud.google.com)**.
2. Sign in with any Google account (personal Gmail is fine).
3. Click the project dropdown at the top of the page → **New Project**.
4. Name it `yt-lead-scraper` (or anything you like) → click **Create**.
5. Wait 10–15 seconds for the project to provision, then select it from the dropdown.

---

### Step 2 — Enable the Google Sheets and Drive APIs

1. In the left sidebar, go to **APIs & Services → Library**.
2. Search for **"Google Sheets API"** → click the result → click **Enable**.
3. Go back to the library.
4. Search for **"Google Drive API"** → click the result → click **Enable**.

Both APIs must be enabled. The Drive API is needed so `gspread` can create and
share the spreadsheet on your behalf.

---

### Step 3 — Create a Service Account

A service account is a robot Google account that our script uses to write to
Sheets on your behalf. It requires no browser login at runtime.

1. Go to **APIs & Services → Credentials**.
2. Click **+ Create Credentials → Service Account**.
3. Fill in:
   - **Service account name:** `yt-scraper-bot`
   - **Service account ID:** (auto-filled — leave it)
   - **Description:** (optional)
4. Click **Create and Continue**.
5. On the "Grant this service account access" step → leave the role blank → click **Continue**.
6. On the "Grant users access" step → leave blank → click **Done**.

---

### Step 4 — Download the JSON Key File

1. You are now back on the **Credentials** page.
2. Under **Service Accounts**, click the email address of the account you just created.
3. Go to the **Keys** tab.
4. Click **Add Key → Create new key**.
5. Select **JSON** → click **Create**.
6. A file downloads automatically (named something like `yt-lead-scraper-xxxx.json`).
7. **Rename it to `credentials.json`** and move it into your project folder:

```
yt-creator-scraper/
└── credentials.json    ← place it here
```

> **Security note:** This file contains private credentials. Never commit it to
> git or share it publicly. Add `credentials.json` to your `.gitignore` file.

---

### Step 5 — Share Your Google Drive with the Service Account

The service account has its own Google Drive that is separate from yours.
To make exported sheets appear in **your** Google Drive:

1. Open the `credentials.json` file in a text editor.
2. Find the `"client_email"` field. It looks like:
   ```
   "client_email": "yt-scraper-bot@yt-lead-scraper.iam.gserviceaccount.com"
   ```
3. Copy that email address.
4. Go to **[drive.google.com](https://drive.google.com)** in your browser.
5. Click **New → Google Sheets** (just to open Drive — you can delete this test sheet).
6. In Drive's left sidebar, click **Shared with me** — the exported sheets will
   appear here automatically because `SHEETS_EXPORT.py` sets them to "anyone
   with the link can write".

**Alternative (recommended):** Share a specific Drive folder with the service
account email so all exports land in one organised place:

1. In Google Drive, create a folder called `YT Leads`.
2. Right-click the folder → **Share**.
3. Paste the service account email → set role to **Editor** → click **Send**.
4. All spreadsheets created by the script will appear in that folder.

---

### Step 6 — Verify Setup

Run this quick test from your terminal to confirm authentication works:

```python
# paste this into your Python REPL (with venv active)
import gspread
from google.oauth2.service_account import Credentials

creds = Credentials.from_service_account_file(
    "credentials.json",
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ],
)
client = gspread.authorize(creds)
sheet  = client.create("Test Connection")
print("✅ Success! Sheet URL:", sheet.url)
sheet.delete()   # clean up the test sheet
```

If you see the URL printed, everything is configured correctly.

---

## 4. Launching the Web UI

Make sure your virtual environment is active, then run:

```bash
streamlit run APP_UI.py
```

Streamlit will automatically open your default browser to:

```
http://localhost:8501
```

If the browser doesn't open automatically, copy that URL and paste it manually.

To stop the server, press **Ctrl+C** in the terminal.

---

## 5. How to Use the UI

| Control | Location | Purpose |
|---|---|---|
| Keywords field | Left sidebar | Comma-separated niches to search |
| Subscriber range | Left sidebar | Slider — reject channels outside this band |
| Min total views | Left sidebar | Slider — reject low-traffic channels |
| Max upload age | Left sidebar | Slider — reject inactive channels |
| Sheet name + credentials path | Left sidebar | Configure Google Sheets export |
| **▶ Start Scraping** | Main area | Launches the background scraper thread |
| **⏸ Pause** | Main area | Suspends between channels — click Resume to continue |
| **⏹ Stop** | Main area | Stops the run cleanly; all previous leads are saved |
| **📊 Export to Sheets** | Main area | Pushes all current leads to Google Sheets |
| Live log window | Main area | Real-time terminal output streamed from the scraper |
| Leads preview table | Main area | Compact view of the latest accepted leads |
| Full table expander | Bottom | All leads + CSV download button |

---

## 6. Output Files

| File | Created by | Contents |
|---|---|---|
| `leads_export.csv` | Auto-saved after every lead | Full lead data — works offline without Sheets |
| Google Sheet | Export button | Formatted CRM with dropdown Outreach Status column |

---

## 7. Troubleshooting

### "ModuleNotFoundError: No module named 'streamlit'"
Your virtual environment is not active. Run:
```bash
source .venv/bin/activate   # macOS/Linux
.venv\Scripts\Activate.ps1  # Windows PowerShell
```

### "SpreadsheetNotFound" when exporting
The service account hasn't been granted access to your Drive folder.
Re-read Step 5 above and share the folder with the service account email.

### "credentials.json not found"
The file must be in the **same folder** as `APP_UI.py`, or you must enter
the correct absolute path in the sidebar's credentials path field.

### Scraper finds 0 channels
YouTube's layout changes periodically. Try:
- Using more generic keywords (e.g., "vlog" instead of "vlog Japan winter 2023")
- Increasing `Max Channels Per Keyword` in the sidebar
- Running the original `scraper.py` from the terminal to see raw error output

### reCAPTCHA / rate limit warnings in the log
This is normal when scraping at speed. The scraper will automatically back off
and retry. To reduce frequency:
- Increase **Jitter Seconds** — edit `jitter_seconds` in `LeadFinderConfig`
- Reduce **Max Channels Per Session**
- Spread runs across multiple sessions with breaks in between
