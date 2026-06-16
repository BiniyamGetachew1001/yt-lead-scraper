#!/usr/bin/env python3
"""
APP_UI.py — YouTube Cold Outreach Lead Generator
Streamlit control panel for the lead generation pipeline.

Launch:
    streamlit run APP_UI.py
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd
import streamlit as st

from LEAD_FINDER import LeadFinderConfig, run_lead_finder_thread
from SHEETS_EXPORT import export_to_excel


# ─────────────────────────────────────────────────────────────────────────────
# CLOUD BOOTSTRAP
# On Streamlit Community Cloud the Playwright Chromium binary is not pre-built
# into the image. This cached function downloads it once per server lifetime
# (subsequent reruns skip it instantly). Harmless locally — it just confirms
# the binary is already present.
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def _bootstrap_playwright() -> str:
    """
    Ensure a usable Chromium binary exists.

    Cloud (Streamlit Community Cloud / Linux):
        Chromium is installed as a system package via packages.txt.
        We detect it and skip the Playwright download entirely — avoids the
        glibc / Debian Trixie package-conflict that breaks the apt install.

    Local (Windows / macOS):
        No system Chromium — run `playwright install chromium` to pull the
        bundled binary into ~/.cache/ms-playwright/.
    """
    _system_paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/usr/lib/chromium/chromium",
    ]
    for p in _system_paths:
        if Path(p).exists():
            return f"system:{p}"

    # Local fallback — download Playwright's bundled Chromium
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        return "playwright-bundled" if result.returncode == 0 else f"warn:{result.stderr[:200]}"
    except Exception as exc:
        return f"error:{exc}"


_pw_status = _bootstrap_playwright()


# ─────────────────────────────────────────────────────────────────────────────
# CREDENTIALS HELPER
# On Streamlit Cloud, credentials.json must not be committed to git.
# Instead, paste its contents into App Settings → Secrets as [gcp_service_account].
# This function handles both the local file path and the cloud secrets path.
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_credentials(local_path: str) -> str:
    """
    Return a valid path to credentials.json.
    Kept for future Google Sheets re-integration — not used by the Excel export.

    Local:  returns local_path as-is.
    Cloud:  reconstructs credentials.json from st.secrets['gcp_service_account'].
    """
    try:
        if "gcp_service_account" in st.secrets:
            creds = dict(st.secrets["gcp_service_account"])
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="yt_creds_", delete=False,
            )
            json.dump(creds, tmp)
            tmp.close()
            return tmp.name
    except Exception:
        pass
    return local_path

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG  (must be the very first Streamlit call)
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="YT Lead Generator",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL CSS — dark terminal aesthetic
# ─────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
/* ── Hide Streamlit default chrome ───────────────────────────────────────── */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding-top: 1.4rem; padding-bottom: 1rem; max-width: 100%; }

/* ── Colour tokens ───────────────────────────────────────────────────────── */
:root {
    --bg-card:     #1a1d27;
    --bg-terminal: #0d1117;
    --accent:      #ff4b4b;
    --green:       #21c97a;
    --yellow:      #ffd166;
    --blue:        #4299e1;
    --text-muted:  #8892a4;
    --border:      #2d3748;
}

/* ── Sidebar ─────────────────────────────────────────────────────────────── */
[data-testid="stSidebar"] {
    background-color: var(--bg-card);
    border-right: 1px solid var(--border);
}

/* ── Metric cards ────────────────────────────────────────────────────────── */
.metric-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem 1.25rem 0.85rem;
    margin-bottom: 0;
}
.metric-value { font-size: 2rem; font-weight: 700; color: var(--accent); line-height: 1.1; }
.metric-value.green  { color: var(--green); }
.metric-value.yellow { color: var(--yellow); }
.metric-value.blue   { color: var(--blue); }
.metric-label {
    font-size: 0.68rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-top: 0.3rem;
}

/* ── Log terminal ────────────────────────────────────────────────────────── */
.log-terminal {
    background: var(--bg-terminal);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.9rem 1rem;
    font-family: 'JetBrains Mono', 'Fira Code', 'Courier New', monospace;
    font-size: 0.74rem;
    line-height: 1.65;
    max-height: 360px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
}
.log-INFO    { color: #90b8d4; }
.log-SUCCESS { color: #21c97a; }
.log-WARNING { color: #ffd166; }
.log-ERROR   { color: #fc8181; }
.log-DEBUG   { color: #566070; }

/* ── Section divider labels ──────────────────────────────────────────────── */
.section-label {
    font-size: 0.68rem;
    font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.12em;
    padding-bottom: 0.3rem;
    border-bottom: 1px solid var(--border);
    margin-bottom: 0.6rem;
    margin-top: 0.9rem;
}

/* ── Button aesthetics ───────────────────────────────────────────────────── */
.stButton > button {
    border-radius: 6px;
    font-weight: 600;
    font-size: 0.85rem;
    padding: 0.48rem 0.8rem;
    width: 100%;
    border: none;
    transition: opacity 0.15s;
}
.stButton > button:hover { opacity: 0.88; }
.btn-start  > button { background: var(--green) !important;  color: #000 !important; }
.btn-pause  > button { background: var(--yellow) !important; color: #000 !important; }
.btn-stop   > button { background: #e53e3e !important;       color: #fff !important; }
.btn-export > button { background: var(--blue) !important;   color: #fff !important; }
</style>
""",
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE  — initialise missing keys on every cold start
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULTS = {
    "scraper_state":   "idle",   # idle | running | paused | completed
    "results":         [],       # list[dict] — one entry per accepted lead
    "logs":            [],       # list[tuple[str,str]] — (level, message)
    "progress":        0.0,      # 0.0 – 1.0
    "channels_done":   0,
    "leads_found":     0,
    "_thread":         None,
    "_log_q":          None,
    "_progress_q":     None,
    "_results_q":      None,
    "_pause_ev":       None,
    "_stop_ev":        None,
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR — all user-facing configuration lives here
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎬 YT Lead Generator")
    st.caption("Video Editing Outreach Tool")
    st.markdown("---")

    st.markdown('<div class="section-label">Target Niche</div>', unsafe_allow_html=True)
    keywords_raw = st.text_area(
        label="Keywords / Hashtags (comma-separated)",
        value=(
            "video editing tutorial, vlog, content creator, "
            "youtube automation, #videography, filmmaking tips"
        ),
        height=105,
        help="Each keyword triggers a separate YouTube channel search.",
        key="kw_input",
    )

    st.markdown('<div class="section-label">Scrape Limits</div>', unsafe_allow_html=True)
    max_channels_total = st.number_input(
        "Max Channels Per Session",
        min_value=5, max_value=500, value=50, step=5,
        help="Hard cap on total channels processed across all keywords.",
        key="max_total",
    )
    max_per_keyword = st.number_input(
        "Max Channels Per Keyword",
        min_value=1, max_value=100, value=12, step=1,
        key="max_kw",
    )

    st.markdown('<div class="section-label">Subscriber Range</div>', unsafe_allow_html=True)
    sub_options = [
        0, 500, 1_000, 2_500, 5_000, 10_000, 25_000,
        50_000, 100_000, 250_000, 500_000, 1_000_000,
    ]
    sub_min, sub_max = st.select_slider(
        "Subscriber Count Range",
        options=sub_options,
        value=(1_000, 250_000),
        format_func=lambda x: f"{x:,}",
        key="sub_range",
    )

    st.markdown('<div class="section-label">View & Recency Filters</div>', unsafe_allow_html=True)
    min_views = st.select_slider(
        "Minimum Total Channel Views",
        options=[0, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000],
        value=25_000,
        format_func=lambda x: f"{x:,}",
        key="min_views",
    )
    max_days = st.slider(
        "Max Days Since Last Upload",
        min_value=7, max_value=365, value=45, step=7,
        format="%d days",
        key="max_days",
    )

    st.markdown('<div class="section-label">Excel Export</div>', unsafe_allow_html=True)
    excel_path = st.text_input(
        "Output filename",
        value="leads_export.xlsx",
        key="excel_path",
        help="Saved in the project folder. Opens in Excel, Google Sheets, or LibreOffice.",
    )

    st.markdown("---")
    st.caption("💾 Leads also auto-save to `leads_export.csv` after every match.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PAGE HEADER
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("# 🎬 YouTube Cold Outreach — Lead Generator")
st.markdown(
    "Discovers video creators matching your niche and extracts contact details "
    "for personalised video editing outreach campaigns."
)
st.markdown("---")


# ─────────────────────────────────────────────────────────────────────────────
# METRIC CARDS  (4 across)
# ─────────────────────────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns(4)

with c1:
    st.markdown(
        f'<div class="metric-card">'
        f'<div class="metric-value green">{st.session_state.leads_found}</div>'
        f'<div class="metric-label">Leads Found</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
with c2:
    done = st.session_state.channels_done
    st.markdown(
        f'<div class="metric-card">'
        f'<div class="metric-value">{done}</div>'
        f'<div class="metric-label">Channels Processed</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
with c3:
    conv = (
        round(st.session_state.leads_found / done * 100, 1) if done > 0 else 0.0
    )
    st.markdown(
        f'<div class="metric-card">'
        f'<div class="metric-value blue">{conv}%</div>'
        f'<div class="metric-label">Conversion Rate</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
with c4:
    _state_icon = {
        "idle":      "⚪ Idle",
        "running":   "🟢 Running",
        "paused":    "🟡 Paused",
        "completed": "✅ Done",
    }.get(st.session_state.scraper_state, "⚪ Idle")
    st.markdown(
        f'<div class="metric-card">'
        f'<div class="metric-value" style="font-size:1.2rem;color:#e8eaf0">{_state_icon}</div>'
        f'<div class="metric-label">Status</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

st.markdown("<br>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# CONTROL BUTTONS
# ─────────────────────────────────────────────────────────────────────────────
state = st.session_state.scraper_state

btn_c1, btn_c2, btn_c3, btn_c4 = st.columns(4)

# ── START / RESUME ────────────────────────────────────────────────────────────
with btn_c1:
    start_label    = "▶  Resume" if state == "paused" else "▶  Start Scraping"
    start_disabled = state == "running"
    st.markdown('<div class="btn-start">', unsafe_allow_html=True)
    start_clicked = st.button(start_label, disabled=start_disabled, use_container_width=True, key="btn_start")
    st.markdown("</div>", unsafe_allow_html=True)

# ── PAUSE ─────────────────────────────────────────────────────────────────────
with btn_c2:
    pause_disabled = state not in ("running",)
    st.markdown('<div class="btn-pause">', unsafe_allow_html=True)
    pause_clicked = st.button("⏸  Pause", disabled=pause_disabled, use_container_width=True, key="btn_pause")
    st.markdown("</div>", unsafe_allow_html=True)

# ── STOP ─────────────────────────────────────────────────────────────────────
with btn_c3:
    stop_disabled = state not in ("running", "paused")
    st.markdown('<div class="btn-stop">', unsafe_allow_html=True)
    stop_clicked = st.button("⏹  Stop", disabled=stop_disabled, use_container_width=True, key="btn_stop")
    st.markdown("</div>", unsafe_allow_html=True)

# ── EXPORT ───────────────────────────────────────────────────────────────────
with btn_c4:
    export_disabled = len(st.session_state.results) == 0
    st.markdown('<div class="btn-export">', unsafe_allow_html=True)
    export_clicked = st.button(
        "📊  Export to Excel",
        disabled=export_disabled,
        use_container_width=True,
        key="btn_export",
    )
    st.markdown("</div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# BUTTON HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
if start_clicked:
    if state == "paused":
        # Resume: unblock the pause event
        st.session_state._pause_ev.set()
        st.session_state.scraper_state = "running"
        st.rerun()

    else:
        # Fresh start
        keywords: List[str] = [k.strip() for k in keywords_raw.split(",") if k.strip()]
        if not keywords:
            st.error("Please enter at least one keyword in the sidebar.")
            st.stop()

        cfg = LeadFinderConfig(
            keywords=keywords,
            max_channels_total=int(max_channels_total),
            max_per_keyword=int(max_per_keyword),
            min_subs=int(sub_min),
            max_subs=int(sub_max),
            min_total_views=int(min_views),
            max_days_since_upload=int(max_days),
        )

        log_q      = queue.Queue()
        progress_q = queue.Queue()
        results_q  = queue.Queue()
        pause_ev   = threading.Event()
        stop_ev    = threading.Event()
        pause_ev.set()  # Start in "not paused" state

        # Reset display state
        st.session_state.results       = []
        st.session_state.logs          = []
        st.session_state.progress      = 0.0
        st.session_state.channels_done = 0
        st.session_state.leads_found   = 0
        st.session_state._log_q        = log_q
        st.session_state._progress_q   = progress_q
        st.session_state._results_q    = results_q
        st.session_state._pause_ev     = pause_ev
        st.session_state._stop_ev      = stop_ev

        t = threading.Thread(
            target=run_lead_finder_thread,
            args=(cfg, log_q, progress_q, results_q, pause_ev, stop_ev),
            daemon=True,
        )
        t.start()
        st.session_state._thread       = t
        st.session_state.scraper_state = "running"
        st.rerun()

if pause_clicked and state == "running":
    st.session_state._pause_ev.clear()   # block the scraper at next checkpoint
    st.session_state.scraper_state = "paused"
    st.rerun()

if stop_clicked:
    if st.session_state._stop_ev:
        st.session_state._stop_ev.set()
    if st.session_state._pause_ev:
        st.session_state._pause_ev.set()  # unblock if currently paused
    st.session_state.scraper_state = "completed"
    st.rerun()

if export_clicked:
    with st.spinner("Building Excel workbook…"):
        try:
            saved_path = export_to_excel(
                leads=st.session_state.results,
                output_path=excel_path,
            )
            # Read the file back so we can offer an in-browser download button
            with open(saved_path, "rb") as fh:
                xlsx_bytes = fh.read()

            st.success(f"✅ Excel file ready — {len(st.session_state.results)} leads exported.")
            st.download_button(
                label="⬇️  Download leads_export.xlsx",
                data=xlsx_bytes,
                file_name=Path(excel_path).name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_xlsx",
            )
            st.caption(
                "💡 To view in Google Sheets: open sheets.google.com → New → "
                "Upload → select this file. No Google Cloud account needed."
            )
        except Exception as exc:
            st.error(f"Export failed: {type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# PROGRESS BAR
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("#### Scraping Progress")
prog_bar   = st.progress(st.session_state.progress)
prog_label = st.empty()
prog_label.caption(
    f"{int(st.session_state.progress * 100)}% complete — "
    f"{st.session_state.channels_done} processed, "
    f"{st.session_state.leads_found} leads"
)
st.markdown("<br>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# LOG + RESULTS — two-column live view
# ─────────────────────────────────────────────────────────────────────────────
log_col, res_col = st.columns([1, 1], gap="large")

with log_col:
    st.markdown("#### 📟 Live Activity Log")
    log_placeholder = st.empty()

with res_col:
    st.markdown("#### 📋 Leads Preview")
    res_placeholder = st.empty()


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — drain all three queues into session state
# ─────────────────────────────────────────────────────────────────────────────
def _drain_queues() -> None:
    """Pull pending items from all inter-thread queues into session state."""
    log_q      = st.session_state._log_q
    progress_q = st.session_state._progress_q
    results_q  = st.session_state._results_q

    if log_q is not None:
        while not log_q.empty():
            try:
                level, msg = log_q.get_nowait()
                ts = datetime.now().strftime("%H:%M:%S")
                st.session_state.logs.append((level, f"[{ts}] {msg}"))
            except queue.Empty:
                break
        # Keep the log list bounded to avoid memory creep across long runs
        if len(st.session_state.logs) > 400:
            st.session_state.logs = st.session_state.logs[-400:]

    if progress_q is not None:
        latest = None
        while not progress_q.empty():
            try:
                latest = progress_q.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            pct, done, leads = latest
            st.session_state.progress      = pct
            st.session_state.channels_done = done
            st.session_state.leads_found   = leads

    if results_q is not None:
        while not results_q.empty():
            try:
                lead = results_q.get_nowait()
                st.session_state.results.append(lead)
                _auto_save(st.session_state.results)
            except queue.Empty:
                break


def _auto_save(results: list) -> None:
    """Write current results to leads_export.csv after every new lead."""
    if not results:
        return
    try:
        pd.DataFrame(results).to_csv("leads_export.csv", index=False, encoding="utf-8")
    except Exception:
        pass  # Never crash the UI over a background file write


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — render log terminal
# ─────────────────────────────────────────────────────────────────────────────
def _render_log() -> None:
    if not st.session_state.logs:
        log_placeholder.info("Waiting for scraper to start…")
        return

    recent = st.session_state.logs[-100:]
    html_lines = []
    for level, msg in recent:
        css = {
            "SUCCESS": "log-SUCCESS",
            "WARNING": "log-WARNING",
            "ERROR":   "log-ERROR",
            "DEBUG":   "log-DEBUG",
        }.get(level, "log-INFO")
        safe = (
            msg.replace("&", "&amp;")
               .replace("<", "&lt;")
               .replace(">", "&gt;")
        )
        html_lines.append(f'<span class="{css}">{safe}</span>')

    log_placeholder.markdown(
        '<div class="log-terminal">' + "<br>".join(html_lines) + "</div>",
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — render results preview table
# ─────────────────────────────────────────────────────────────────────────────
def _render_results() -> None:
    if not st.session_state.results:
        res_placeholder.info("No leads yet — results appear here as they pass filters.")
        return

    df = pd.DataFrame(st.session_state.results)
    show_cols = [
        c for c in [
            "channel_name", "subscribers", "email",
            "instagram", "days_since_upload", "outreach_status",
        ]
        if c in df.columns
    ]
    res_placeholder.dataframe(
        df[show_cols].tail(40),
        use_container_width=True,
        hide_index=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# LIVE UPDATE LOOP
# On every Streamlit rerun, drain queues + render. If the scraper is still
# running, sleep 0.75 s then force another rerun to keep the display live.
# ─────────────────────────────────────────────────────────────────────────────
_drain_queues()
_render_log()
_render_results()

prog_bar.progress(st.session_state.progress)
prog_label.caption(
    f"{int(st.session_state.progress * 100)}% complete — "
    f"{st.session_state.channels_done} processed, "
    f"{st.session_state.leads_found} leads"
)

# Detect when the background thread finishes naturally
if st.session_state.scraper_state == "running":
    t = st.session_state._thread
    if t is not None and not t.is_alive():
        # Thread exited — run is complete
        st.session_state.scraper_state = "completed"
        st.session_state.progress = 1.0
        st.session_state.logs.append(("SUCCESS", "✅ Scraping run complete!"))
        st.rerun()
    else:
        # Thread still alive — force a rerun to pick up new queue items
        time.sleep(0.75)
        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# FULL RESULTS TABLE — collapsible expander shown when results exist
# ─────────────────────────────────────────────────────────────────────────────
if st.session_state.results:
    st.markdown("---")
    with st.expander(
        f"📥 Full Leads Table ({len(st.session_state.results)} leads) — click to expand",
        expanded=False,
    ):
        df_full = pd.DataFrame(st.session_state.results)
        st.dataframe(df_full, use_container_width=True, hide_index=True)

        csv_bytes = df_full.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="⬇️  Download leads_export.csv",
            data=csv_bytes,
            file_name="leads_export.csv",
            mime="text/csv",
        )
