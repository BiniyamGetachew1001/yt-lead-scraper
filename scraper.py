#!/usr/bin/env python3
"""
YouTube Creator Discovery Scraper
==================================
Free, local, no-API channel discovery using Python + Playwright (async).

Usage:
    python scraper.py

Output files (auto-created):
    discovered_channels.csv  —  filtered channel records, appended incrementally
    scraper.log              —  full DEBUG-level log with timestamps

Restart behaviour:
    The script reads discovered_channels.csv on startup and skips any channel
    URL already present. You can safely Ctrl+C and restart without losing work.
"""

import asyncio
import csv
import logging
import os
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# Edit only this block to customise your discovery run.
# Every other function reads exclusively from this dict.
# ─────────────────────────────────────────────────────────────────────────────
CONFIG: Dict[str, Any] = {
    # ── Audience size filter (inclusive) ─────────────────────────────────────
    # Channels with subscriber counts outside this window are rejected.
    "MIN_SUBS": 1_000,
    "MAX_SUBS": 500_000,

    # ── Activity filter ───────────────────────────────────────────────────────
    # Minimum cumulative view count across all channel videos.
    "MIN_TOTAL_VIEWS": 50_000,

    # Channels whose most recent public upload is older than this are rejected.
    "MAX_DAYS_SINCE_UPLOAD": 30,

    # ── Discovery targets ─────────────────────────────────────────────────────
    # Plain keywords or hashtags (# is URL-encoded automatically).
    "KEYWORDS": [
        "python tutorials",
        "data science beginner",
        "#learnpython",
        "machine learning explained",
    ],

    # Maximum channel URLs to collect per keyword search page.
    # Set to None to collect every channel result YouTube returns.
    "MAX_CHANNELS_PER_KEYWORD": 20,

    # ── Output paths ──────────────────────────────────────────────────────────
    "OUTPUT_CSV": "discovered_channels.csv",
    "LOG_FILE":   "scraper.log",

    # ── Timing / politeness ───────────────────────────────────────────────────
    # Random sleep range (seconds) between every page.goto() call.
    # Wider ranges are slower but far less likely to trigger rate limits.
    "JITTER_SECONDS": (2.5, 6.5),

    # Random sleep between each downward scroll on search result pages.
    "SCROLL_PAUSE_SECONDS": (1.2, 2.8),

    # How many scroll events to fire on each search results page.
    # More scrolls surface more lazy-loaded channel cards.
    "MAX_SCROLL_ATTEMPTS": 5,

    # ── Backoff / rate-limit recovery ─────────────────────────────────────────
    # wait = min(INITIAL × MULTIPLIER^attempt, MAX)
    "BACKOFF_INITIAL_SECONDS": 60,
    "BACKOFF_MAX_SECONDS":     600,
    "BACKOFF_MULTIPLIER":      2.0,
}


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# These are implementation details — edit only if YouTube changes its layout.
# ─────────────────────────────────────────────────────────────────────────────

# Real desktop User-Agent strings pulled from common browser analytics data.
# One is chosen at random per browser session to vary the fingerprint.
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",

    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",

    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
    "Gecko/20100101 Firefox/126.0",
]

# Common desktop screen resolutions — one chosen randomly per session.
VIEWPORTS: List[Dict[str, int]] = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 720},
]

# YouTube search URL with the "Type: Channel" filter baked in.
# sp=EgIQAg%3D%3D is the URL-encoded, protobuf-serialised channel filter.
YOUTUBE_SEARCH_URL = (
    "https://www.youtube.com/results"
    "?search_query={query}"
    "&sp=EgIQAg%3D%3D"
)

# Column order for the output CSV — every record dict must use these exact keys.
CSV_COLUMNS: List[str] = [
    "channel_name",
    "channel_url",
    "subscribers",
    "total_views",
    "video_count",
    "latest_upload_date",
    "days_since_upload",
    "keyword_found_by",
    "scraped_at",
]


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# Two handlers: INFO+ to stdout (clean terminal view),
#               DEBUG+ to scraper.log (full audit trail).
# Built once at module load; imported as the module-level LOG everywhere.
# ─────────────────────────────────────────────────────────────────────────────
def _build_logger(log_file: str) -> logging.Logger:
    """Construct and return the dual-sink logger used by the entire module."""
    logger = logging.getLogger("yt_scraper")
    logger.setLevel(logging.DEBUG)  # root level; each handler filters independently

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Terminal: INFO and above — progress, ACCEPT/REJECT events, warnings
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(formatter)

    # File: DEBUG and above — every extraction attempt, scroll step, stack trace
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(stdout_handler)
    logger.addHandler(file_handler)
    return logger


LOG = _build_logger(CONFIG["LOG_FILE"])


# ─────────────────────────────────────────────────────────────────────────────
# CUSTOM EXCEPTIONS
# ─────────────────────────────────────────────────────────────────────────────
class RateLimitError(Exception):
    """
    Raised when YouTube responds with a CAPTCHA, a 429-equivalent page,
    or any other signal that the session is being throttled.
    The main pipeline catches this and triggers exponential backoff.
    """
    pass


# ─────────────────────────────────────────────────────────────────────────────
# METRIC NORMALIZATION ENGINE
# Converts human-readable YouTube strings to plain Python integers.
# ─────────────────────────────────────────────────────────────────────────────
def normalize_count(raw: str) -> int:
    """
    Convert a YouTube abbreviated count string into a plain integer.

    Handles all formats observed in YouTube's rendered DOM:
        "1.4M subscribers"  →  1_400_000
        "45K views"         →  45_000
        "2.3B"              →  2_300_000_000
        "1,234,567"         →  1_234_567
        "987"               →  987
        ""  / None          →  0
    """
    if not raw:
        return 0

    # Remove thousands-separator commas so "1,234,567" becomes "1234567"
    # before the regex runs on it.
    cleaned = raw.replace(",", "").strip()

    # Capture the numeric portion (including optional decimal) and the
    # optional K / M / B multiplier suffix.
    match = re.search(r"([\d.]+)\s*([KMBkmb]?)", cleaned)
    if not match:
        return 0

    number_str = match.group(1)
    suffix = match.group(2).upper()

    try:
        number = float(number_str)
    except ValueError:
        return 0

    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(number * multipliers.get(suffix, 1))


def parse_relative_date(raw: str) -> Optional[datetime]:
    """
    Convert a YouTube relative date string to an absolute datetime object.

    YouTube Videos tabs express recency as "X unit(s) ago" strings. We
    reconstruct the absolute timestamp by subtracting the implied timedelta
    from the current clock time at parse time.

        "3 days ago"           →  datetime.now() - timedelta(days=3)
        "2 weeks ago"          →  datetime.now() - timedelta(weeks=2)
        "5 months ago"         →  datetime.now() - timedelta(days=150)
        "1 year ago"           →  datetime.now() - timedelta(days=365)
        "Streamed 4 hours ago" →  datetime.now() - timedelta(hours=4)
        unrecognised / None    →  None
    """
    if not raw:
        return None

    text = raw.lower().strip()

    # Each tuple pairs a regex pattern with a lambda that builds the timedelta.
    # re.search is used (not match) so the pattern matches anywhere in the
    # string, handling prefixes like "Streamed" or "Premiered".
    patterns: List[Tuple[str, Any]] = [
        (r"(\d+)\s+second", lambda n: timedelta(seconds=n)),
        (r"(\d+)\s+minute", lambda n: timedelta(minutes=n)),
        (r"(\d+)\s+hour",   lambda n: timedelta(hours=n)),
        (r"(\d+)\s+day",    lambda n: timedelta(days=n)),
        (r"(\d+)\s+week",   lambda n: timedelta(weeks=n)),
        (r"(\d+)\s+month",  lambda n: timedelta(days=n * 30)),
        (r"(\d+)\s+year",   lambda n: timedelta(days=n * 365)),
    ]

    for pattern, delta_fn in patterns:
        m = re.search(pattern, text)
        if m:
            return datetime.now() - delta_fn(int(m.group(1)))

    return None


def days_since(dt: Optional[datetime]) -> Optional[int]:
    """Return whole days elapsed between a past datetime and now, or None."""
    if dt is None:
        return None
    return (datetime.now() - dt).days


# ─────────────────────────────────────────────────────────────────────────────
# STATE PERSISTENCE ENGINE
# Incremental CSV write + startup recovery via checkpoint reading.
# ─────────────────────────────────────────────────────────────────────────────
def load_existing_channels(csv_path: str) -> Set[str]:
    """
    Read the output CSV on startup and return the set of channel URLs that
    have already been scraped, so the main loop can skip them.

    Only the 'channel_url' column is read — pandas discards all other
    columns — keeping memory usage flat regardless of row count.

    If the file does not exist or cannot be parsed, an empty set is
    returned and the run starts fresh without raising an error.
    """
    path = Path(csv_path)

    if not path.exists():
        LOG.info("No existing checkpoint file — starting a fresh discovery run.")
        return set()

    try:
        df = pd.read_csv(path, usecols=["channel_url"])
        existing: Set[str] = set(df["channel_url"].dropna().tolist())
        LOG.info(
            f"Checkpoint loaded — {len(existing)} previously scraped channel(s) "
            f"will be skipped on this run."
        )
        return existing
    except Exception as exc:
        LOG.warning(
            f"Could not parse checkpoint '{csv_path}': {exc}. "
            f"Starting fresh (existing file will be appended to, not overwritten)."
        )
        return set()


def append_channel_to_csv(csv_path: str, record: Dict[str, Any]) -> None:
    """
    Append a single accepted channel record to the CSV file immediately
    after it passes all filters.

    Opening in append mode ('a') and writing one row at a time guarantees
    that a crash cannot lose more than the single in-flight channel being
    processed at the moment of failure.

    The header row is written only when the file is new or empty. On all
    subsequent calls the header already exists and is not repeated.
    """
    path = Path(csv_path)
    write_header = not path.exists() or path.stat().st_size == 0

    with open(csv_path, mode="a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(record)

    LOG.debug(
        f"Saved → {record.get('channel_name')!r} | "
        f"{record.get('subscribers', 0):,} subs | "
        f"{record.get('total_views', 0):,} views"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ANTI-BOT EVASION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
async def random_jitter(min_s: float, max_s: float) -> None:
    """Sleep for a uniformly random duration in [min_s, max_s] seconds."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def dismiss_consent_wall(page: Page) -> None:
    """
    Attempt to click through YouTube's cookie / privacy consent modal.

    YouTube serves different consent flows by region and session state.
    Multiple known button selectors are tried in sequence; the first
    visible one is clicked. If no consent wall is present, the function
    returns silently without error.
    """
    # These selectors cover the main Google consent form, the YouTube
    # in-product cookie banner, and generic dialog fallbacks.
    consent_selectors = [
        "button[aria-label='Accept all']",
        "button[aria-label='Reject all']",
        "button[aria-label='Accept All']",
        # Google unified consent form primary action button
        "button.VfPpkd-LgbsSe[jsname='b3VHJd']",
        # Generic: first button inside any dialog overlay
        "#dialog button:first-child",
        # Consent action form
        "form[action*='consent'] button",
    ]

    for selector in consent_selectors:
        try:
            btn = page.locator(selector).first
            # 2 000 ms timeout — if not visible quickly, move to next selector
            if await btn.is_visible(timeout=2_000):
                await btn.click()
                LOG.debug(f"Consent wall dismissed via selector: {selector!r}")
                await asyncio.sleep(1.0)
                return  # Only one click needed
        except Exception:
            continue


async def detect_rate_limit(page: Page) -> bool:
    """
    Return True when the current page indicates YouTube is blocking or
    throttling this session.

    Checked signals:
      - URL redirect to Google's sorry/index or a captcha endpoint
      - Body text containing "unusual traffic" language
      - Body text containing a reCAPTCHA widget reference
    """
    current_url = page.url

    try:
        body_text = await page.inner_text("body", timeout=5_000)
    except Exception:
        body_text = ""

    block_signals = [
        "sorry/index" in current_url,
        "captcha" in current_url.lower(),
        "unusual traffic" in body_text.lower(),
        "our systems have detected unusual traffic" in body_text.lower(),
        "recaptcha" in body_text.lower(),
        # YouTube sometimes shows an interstitial before account selection
        "accounts.google.com" in current_url,
    ]

    return any(block_signals)


async def backoff_on_rate_limit(attempt: int) -> int:
    """
    Sleep for an exponentially increasing duration when a rate-limit event
    is detected, then return the incremented attempt counter.

    Formula: wait = min(INITIAL × MULTIPLIER^attempt, MAX)

    With defaults (initial=60 s, multiplier=2.0, max=600 s):
        attempt 0 →  60 s
        attempt 1 → 120 s
        attempt 2 → 240 s
        attempt 3 → 480 s
        attempt 4 → 600 s  (ceiling)

    A prominent WARNING is logged to both stdout and the log file so the
    operator sees it immediately. The scraper does NOT crash.
    """
    wait_s = min(
        CONFIG["BACKOFF_INITIAL_SECONDS"] * (CONFIG["BACKOFF_MULTIPLIER"] ** attempt),
        CONFIG["BACKOFF_MAX_SECONDS"],
    )

    LOG.warning(
        "\n" + "━" * 60 + "\n"
        "  [RATE LIMIT / CAPTCHA DETECTED]\n"
        f"  Backing off for {wait_s:.0f} seconds (attempt #{attempt + 1}).\n"
        "  If this happens repeatedly, try:\n"
        "    • Increasing JITTER_SECONDS in CONFIG\n"
        "    • Reducing MAX_CHANNELS_PER_KEYWORD\n"
        "    • Waiting a few minutes before restarting\n"
        + "━" * 60
    )

    await asyncio.sleep(wait_s)
    return attempt + 1  # Caller stores this for the next backoff calculation


# ─────────────────────────────────────────────────────────────────────────────
# BROWSER CONTEXT FACTORY
# ─────────────────────────────────────────────────────────────────────────────
async def create_browser_context(pw: Any) -> Tuple[Browser, BrowserContext]:
    """
    Launch a headless Chromium instance and return a (Browser, BrowserContext)
    pair configured with a randomised, human-like fingerprint.

    Evasion techniques applied:
      1. --disable-blink-features=AutomationControlled
         Suppresses the Blink engine flag that exposes CDP-driven sessions
         to JavaScript fingerprinting libraries.
      2. Random User-Agent from a pool of real desktop browser strings.
      3. Random viewport from a pool of common desktop resolutions.
      4. add_init_script — runs before every page's own scripts and:
           • Sets navigator.webdriver = undefined
           • Injects a non-empty navigator.plugins array
           • Stubs window.chrome.runtime (absent in headless Chromium)
           • Suppresses console.debug (used by some fingerprint probes)
    """
    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)

    browser: Browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",   # prevent /dev/shm exhaustion on Linux
            "--disable-extensions",
            "--disable-gpu",
            f"--window-size={vp['width']},{vp['height']}",
        ],
    )

    context: BrowserContext = await browser.new_context(
        user_agent=ua,
        viewport=vp,
        locale="en-US",
        timezone_id="America/New_York",
        color_scheme="light",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            # DNT header is present in real browsers
            "DNT": "1",
            "Sec-Fetch-Mode": "navigate",
        },
    )

    # This JavaScript is injected into every page's context before any
    # page scripts run, overriding the properties that reveal automation.
    await context.add_init_script("""
        // Primary automation signal: navigator.webdriver is true in CDP sessions
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // Headless Chrome has zero plugins; real browsers have several
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });

        // window.chrome.runtime is absent in headless — inject a minimal stub
        window.chrome = { runtime: {} };

        // Suppress console.debug channel used by some fingerprint libraries
        window.console.debug = () => {};
    """)

    LOG.debug(
        f"Browser context ready | UA: {ua[:65]}... | Viewport: {vp['width']}x{vp['height']}"
    )
    return browser, context


# ─────────────────────────────────────────────────────────────────────────────
# PAGE SCROLLING HELPER
# ─────────────────────────────────────────────────────────────────────────────
async def scroll_to_load(page: Page) -> None:
    """
    Scroll the page downward in incremental steps to trigger YouTube's
    Intersection Observer-based lazy loader, revealing channel cards that
    are not visible in the initial viewport.

    Each step scrolls 1.8 × the viewport height and waits a random pause
    before the next step to avoid scroll-speed fingerprinting.
    """
    for i in range(CONFIG["MAX_SCROLL_ATTEMPTS"]):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 1.8)")
        await random_jitter(*CONFIG["SCROLL_PAUSE_SECONDS"])
        LOG.debug(f"  Scroll step {i + 1}/{CONFIG['MAX_SCROLL_ATTEMPTS']} complete.")


# ─────────────────────────────────────────────────────────────────────────────
# YOUTUBE SEARCH — channel URL discovery
# ─────────────────────────────────────────────────────────────────────────────
async def search_channels(page: Page, keyword: str) -> List[str]:
    """
    Navigate to a YouTube channel-filtered search results page and return
    a list of absolute channel URLs found on that page.

    Steps:
        1. URL-encode the keyword (spaces → '+', '#' → '%23').
        2. Navigate to the search URL.
        3. Dismiss any cookie / consent wall.
        4. Detect rate limiting — raise RateLimitError if present.
        5. Wait for ytd-channel-renderer elements to appear.
        6. Scroll the page to trigger lazy-loaded results.
        7. Extract the href from each channel card's primary anchor.
        8. Deduplicate and enforce MAX_CHANNELS_PER_KEYWORD cap.
    """
    # URL-encode keyword: space → '+', '#' → '%23'
    encoded_keyword = keyword.replace(" ", "+").replace("#", "%23")
    search_url = YOUTUBE_SEARCH_URL.format(query=encoded_keyword)

    LOG.info(f"Searching for channels: '{keyword}'")
    LOG.debug(f"  Search URL: {search_url}")

    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        LOG.warning(f"Page load timeout for keyword '{keyword}'. Skipping this keyword.")
        return []

    await dismiss_consent_wall(page)

    if await detect_rate_limit(page):
        raise RateLimitError(f"Rate limit or CAPTCHA on search results for '{keyword}'.")

    # YouTube renders channel results as ytd-channel-renderer custom elements.
    # Wait up to 15 s for at least one to appear.
    try:
        await page.wait_for_selector("ytd-channel-renderer", timeout=15_000)
    except PlaywrightTimeoutError:
        LOG.warning(
            f"No 'ytd-channel-renderer' elements appeared for '{keyword}'. "
            f"YouTube may have returned zero channel results, or its layout changed."
        )
        return []

    # Scroll to reveal lazy-loaded cards below the fold
    await scroll_to_load(page)

    channel_urls: List[str] = []
    renderers = await page.query_selector_all("ytd-channel-renderer")

    for renderer in renderers:
        # Try the known anchor selectors in order of reliability.
        # YouTube occasionally changes which anchor carries the channel href.
        link_el = await renderer.query_selector(
            "a#channel-name, a#main-link, a#avatar-link"
        )
        if not link_el:
            continue

        href = await link_el.get_attribute("href")
        if not href:
            continue

        # YouTube returns relative paths such as /@channelname or /c/Name.
        # Prepend the origin to make them absolute.
        if href.startswith("/"):
            href = "https://www.youtube.com" + href

        # Deduplicate within this keyword's result set
        if href not in channel_urls:
            channel_urls.append(href)

    LOG.info(f"  Found {len(channel_urls)} unique channel URL(s) for '{keyword}'.")

    # [:None] returns the full list when the cap is None
    return channel_urls[: CONFIG["MAX_CHANNELS_PER_KEYWORD"]]


# ─────────────────────────────────────────────────────────────────────────────
# CHANNEL /about TAB EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_channel_about(page: Page, channel_url: str) -> Dict[str, Any]:
    """
    Navigate to the channel's /about tab and extract:
        channel_name  — display name shown in the channel header
        subscribers   — subscriber count as an integer
        total_views   — cumulative view count across all videos
        video_count   — total number of public videos

    Extraction uses a layered approach for resilience against layout changes:
        Layer 1 — CSS selectors for known YouTube element IDs / class names
        Layer 2 — Full body text extraction + regex (works even if DOM restructured)
        Layer 3 — Page <title> tag as a last-resort name fallback
    """
    about_url = channel_url.rstrip("/") + "/about"

    # Return this default structure if any extraction step fails entirely
    result: Dict[str, Any] = {
        "channel_name": "",
        "subscribers":  0,
        "total_views":  0,
        "video_count":  0,
    }

    try:
        await page.goto(about_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        LOG.warning(f"Timeout loading about tab: {about_url}")
        return result

    await dismiss_consent_wall(page)

    if await detect_rate_limit(page):
        raise RateLimitError(f"Rate limit or CAPTCHA on about tab for {channel_url}.")

    # ── Layer 1: CSS selector extraction ─────────────────────────────────────

    # Channel name — multiple selector variants cover old + new YouTube header
    for name_sel in [
        "#channel-name yt-formatted-string",
        "yt-formatted-string#text.ytd-channel-name",
        "#inner-header-container #text",
        "h1.ytd-channel-name",
        "#page-header h1",
    ]:
        try:
            el = await page.query_selector(name_sel)
            if el:
                name_text = (await el.inner_text()).strip()
                if name_text:
                    result["channel_name"] = name_text
                    LOG.debug(f"  Channel name via '{name_sel}': {name_text!r}")
                    break
        except Exception:
            continue

    # Subscriber count — the #subscriber-count span is the most reliable source
    try:
        await page.wait_for_selector("#subscriber-count", timeout=8_000)
        sub_el = await page.query_selector("#subscriber-count")
        if sub_el:
            raw_subs = await sub_el.inner_text()
            result["subscribers"] = normalize_count(raw_subs)
            LOG.debug(f"  Subscribers (CSS): {raw_subs!r} → {result['subscribers']:,}")
    except Exception:
        pass  # Fall through to the body-text layer below

    # ── Layer 2: Full body text + regex extraction ────────────────────────────
    # Even when CSS selectors fail (YouTube layout change), the rendered page
    # text still contains the stats as human-readable strings. Regex is
    # resilient to structural changes as long as the text content is present.
    try:
        body_text = await page.inner_text("body")

        # Subscriber count fallback (only if CSS selector above yielded 0)
        if result["subscribers"] == 0:
            sub_m = re.search(
                r"([\d.,]+\s*[KMB]?)\s+subscriber",
                body_text,
                re.IGNORECASE,
            )
            if sub_m:
                result["subscribers"] = normalize_count(sub_m.group(1))
                LOG.debug(
                    f"  Subscribers (regex): {sub_m.group(1)!r} → {result['subscribers']:,}"
                )

        # Total view count — present on the About tab as "X views"
        views_m = re.search(
            r"([\d.,]+\s*[KMB]?)\s+view",
            body_text,
            re.IGNORECASE,
        )
        if views_m:
            result["total_views"] = normalize_count(views_m.group(1))
            LOG.debug(
                f"  Total views (regex): {views_m.group(1)!r} → {result['total_views']:,}"
            )

        # Video count — present on the About tab as "X videos"
        vids_m = re.search(
            r"([\d,]+)\s+video",
            body_text,
            re.IGNORECASE,
        )
        if vids_m:
            result["video_count"] = normalize_count(vids_m.group(1))
            LOG.debug(
                f"  Video count (regex): {vids_m.group(1)!r} → {result['video_count']:,}"
            )

    except Exception as exc:
        LOG.debug(f"  Layer-2 body-text extraction failed for {channel_url}: {exc}")

    # ── Layer 3: Page title fallback for channel name ─────────────────────────
    if not result["channel_name"]:
        try:
            html_content = await page.content()
            title_m = re.search(r"<title>\s*(.+?)\s*[-–|]", html_content)
            if title_m:
                result["channel_name"] = title_m.group(1).strip()
                LOG.debug(
                    f"  Channel name (title fallback): {result['channel_name']!r}"
                )
        except Exception:
            pass

    LOG.debug(
        f"  About result → name={result['channel_name']!r} | "
        f"subs={result['subscribers']:,} | "
        f"views={result['total_views']:,} | "
        f"videos={result['video_count']:,}"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CHANNEL /videos TAB EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_latest_upload(page: Page, channel_url: str) -> Optional[str]:
    """
    Navigate to the channel's /videos tab and return the relative date string
    of the most recently published video (e.g., "3 days ago").

    Returns None if the channel has no public videos or the date cannot
    be determined.

    Extraction uses two layers:
        Layer 1 — CSS selectors targeting the metadata span inside the first
                  video card across three known YouTube grid layout variants
        Layer 2 — Full body text regex scan as a universal fallback
    """
    videos_url = channel_url.rstrip("/") + "/videos"

    try:
        await page.goto(videos_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        LOG.warning(f"Timeout loading videos tab: {videos_url}")
        return None

    await dismiss_consent_wall(page)

    if await detect_rate_limit(page):
        raise RateLimitError(f"Rate limit or CAPTCHA on videos tab for {channel_url}.")

    # Wait for the video grid to appear — without this, the DOM may not
    # yet contain any video cards and extraction returns nothing.
    try:
        await page.wait_for_selector(
            "ytd-grid-video-renderer, ytd-rich-item-renderer",
            timeout=12_000,
        )
    except PlaywrightTimeoutError:
        LOG.debug(f"  No video grid found for {channel_url} — possibly no public videos.")
        return None

    # ── Layer 1: CSS selector extraction ─────────────────────────────────────
    # YouTube uses different layout renderers for the Videos tab depending on
    # the account type, channel age, and A/B tests. We try all known variants.
    date_selectors = [
        # Classic grid layout
        "ytd-grid-video-renderer #metadata-line span:last-child",
        # Rich grid layout (newer channels / updated UI)
        "ytd-rich-item-renderer #metadata-line span:last-child",
        # List layout fallback
        "ytd-video-meta-block #metadata-line span",
        # Compact list item
        "ytd-compact-video-renderer #metadata-line span:last-child",
    ]

    for selector in date_selectors:
        try:
            elements = await page.query_selector_all(selector)
            for el in elements:
                text = (await el.inner_text()).strip()
                # Only accept this span if it contains a relative date pattern.
                # Spans on the same line also carry view counts — we skip those.
                if re.search(
                    r"\d+\s+(second|minute|hour|day|week|month|year)",
                    text,
                    re.IGNORECASE,
                ):
                    LOG.debug(f"  Latest upload (CSS '{selector}'): {text!r}")
                    return text
        except Exception:
            continue

    # ── Layer 2: Full body text regex scan ───────────────────────────────────
    # If no CSS selector matched, scan the entire rendered body text.
    # The first match is the most recent video's date (videos are listed
    # newest-first on the /videos tab).
    try:
        body_text = await page.inner_text("body")
        all_dates = re.findall(
            r"\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago",
            body_text,
            re.IGNORECASE,
        )
        if all_dates:
            LOG.debug(f"  Latest upload (body regex fallback): {all_dates[0]!r}")
            return all_dates[0]
    except Exception as exc:
        LOG.debug(f"  Body text fallback failed on videos tab for {channel_url}: {exc}")

    LOG.debug(f"  Could not determine latest upload date for {channel_url}.")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# FILTER ENGINE
# ─────────────────────────────────────────────────────────────────────────────
def passes_filters(
    subscribers: int,
    total_views: int,
    days_since_upload: Optional[int],
) -> Tuple[bool, str]:
    """
    Evaluate a channel's metrics against all CONFIG thresholds.

    Returns a (passed, reason) tuple:
        passed  — True if ALL conditions are satisfied, False otherwise
        reason  — 'PASS' on success; a human-readable rejection message otherwise

    All checks are evaluated in order; the first failure short-circuits.
    """
    if subscribers < CONFIG["MIN_SUBS"]:
        return False, f"Subs {subscribers:,} < MIN {CONFIG['MIN_SUBS']:,}"

    if subscribers > CONFIG["MAX_SUBS"]:
        return False, f"Subs {subscribers:,} > MAX {CONFIG['MAX_SUBS']:,}"

    if total_views < CONFIG["MIN_TOTAL_VIEWS"]:
        return False, f"Views {total_views:,} < MIN {CONFIG['MIN_TOTAL_VIEWS']:,}"

    if days_since_upload is None:
        return False, "Latest upload date could not be determined"

    if days_since_upload > CONFIG["MAX_DAYS_SINCE_UPLOAD"]:
        return False, (
            f"Last upload {days_since_upload}d ago "
            f"> MAX {CONFIG['MAX_DAYS_SINCE_UPLOAD']}d"
        )

    return True, "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# CORE CHANNEL PROCESSING PIPELINE
# ─────────────────────────────────────────────────────────────────────────────
async def process_channel(
    page: Page,
    channel_url: str,
    keyword: str,
    existing_channels: Set[str],
) -> Optional[Dict[str, Any]]:
    """
    Full extraction + filter pipeline for a single channel URL.

    Steps:
        1. Normalise the URL and skip if already in the checkpoint set.
        2. Jitter delay before the first network request.
        3. Scrape /about tab → channel name, subs, views, video count.
        4. Jitter delay before the second network request.
        5. Scrape /videos tab → latest upload date string.
        6. Convert date string → datetime → integer age in days.
        7. Apply all CONFIG filters.
        8. Return a fully populated record dict on PASS, or None on rejection.

    Any RateLimitError raised inside the extraction calls propagates up to
    the main pipeline loop where it triggers exponential backoff.
    """
    # Normalise by stripping trailing slashes so that trailing-slash variants
    # of the same URL don't bypass the deduplication check.
    normalized_url = channel_url.rstrip("/")

    if normalized_url in existing_channels or channel_url in existing_channels:
        LOG.debug(f"Skipping (already in checkpoint): {normalized_url}")
        return None

    LOG.info(f"Processing: {normalized_url}")

    # ── /about tab ────────────────────────────────────────────────────────────
    await random_jitter(*CONFIG["JITTER_SECONDS"])
    about = await scrape_channel_about(page, normalized_url)

    # ── /videos tab ───────────────────────────────────────────────────────────
    await random_jitter(*CONFIG["JITTER_SECONDS"])
    latest_raw = await scrape_latest_upload(page, normalized_url)

    # Convert the raw relative string to an absolute datetime, then to days
    latest_dt = parse_relative_date(latest_raw or "")
    upload_age = days_since(latest_dt)

    # ── Filter evaluation ─────────────────────────────────────────────────────
    passed, reason = passes_filters(
        subscribers=about["subscribers"],
        total_views=about["total_views"],
        days_since_upload=upload_age,
    )

    label = about["channel_name"] or normalized_url

    if not passed:
        LOG.info(f"  REJECTED [{reason}]: {label}")
        return None

    LOG.info(
        f"  ACCEPTED: {label} | "
        f"{about['subscribers']:,} subs | "
        f"{about['total_views']:,} views | "
        f"Uploaded {upload_age}d ago"
    )

    return {
        "channel_name":       about["channel_name"],
        "channel_url":        normalized_url,
        "subscribers":        about["subscribers"],
        "total_views":        about["total_views"],
        "video_count":        about["video_count"],
        "latest_upload_date": latest_dt.strftime("%Y-%m-%d") if latest_dt else "",
        "days_since_upload":  upload_age,
        "keyword_found_by":   keyword,
        "scraped_at":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────
async def run_discovery_pipeline() -> None:
    """
    Top-level coroutine. Orchestrates the full discovery run:
        • Loads the checkpoint CSV to determine which channels to skip.
        • Iterates over each keyword in CONFIG["KEYWORDS"].
        • Discovers channel URLs via YouTube search.
        • Processes each channel URL through the extraction + filter pipeline.
        • Persists each passing channel immediately to the output CSV.

    Error handling model:
        RateLimitError         → exponential backoff, continue to next keyword
        PlaywrightTimeoutError → skip individual channel, continue
        KeyboardInterrupt      → log clean shutdown, close browser
        Any other Exception    → log warning + debug stack trace, skip channel
    """
    LOG.info("=" * 60)
    LOG.info("YouTube Creator Discovery Scraper — Starting")
    LOG.info(
        f"Filters: subs {CONFIG['MIN_SUBS']:,}–{CONFIG['MAX_SUBS']:,} | "
        f"min views {CONFIG['MIN_TOTAL_VIEWS']:,} | "
        f"max upload age {CONFIG['MAX_DAYS_SINCE_UPLOAD']}d"
    )
    LOG.info(
        f"Keywords ({len(CONFIG['KEYWORDS'])}): "
        + ", ".join(f"'{k}'" for k in CONFIG["KEYWORDS"])
    )
    LOG.info("=" * 60)

    # Load the checkpoint set before opening the browser.
    existing_channels: Set[str] = load_existing_channels(CONFIG["OUTPUT_CSV"])

    total_accepted = 0
    backoff_attempt = 0  # Reset to 0 on every successful request

    async with async_playwright() as pw:
        browser, context = await create_browser_context(pw)
        page = await context.new_page()

        try:
            for keyword in CONFIG["KEYWORDS"]:
                LOG.info(f"\n{'─' * 55}")
                LOG.info(f"Keyword: \"{keyword}\"")
                LOG.info(f"{'─' * 55}")

                # ── Discover channel URLs for this keyword ────────────────────
                channel_urls: List[str] = []

                try:
                    channel_urls = await search_channels(page, keyword)
                    backoff_attempt = 0  # Success — reset the backoff counter

                except RateLimitError as exc:
                    LOG.warning(f"Rate limit during search: {exc}")
                    backoff_attempt = await backoff_on_rate_limit(backoff_attempt)
                    LOG.warning(f"Skipping keyword '{keyword}' after rate limit.")
                    continue

                except PlaywrightTimeoutError:
                    LOG.warning(
                        f"Playwright timeout during search for '{keyword}'. "
                        f"Skipping this keyword."
                    )
                    continue

                if not channel_urls:
                    LOG.info(
                        f"No channel URLs returned for '{keyword}'. "
                        f"Moving to next keyword."
                    )
                    continue

                # ── Process each discovered channel URL ───────────────────────
                for channel_url in channel_urls:
                    try:
                        record = await process_channel(
                            page=page,
                            channel_url=channel_url,
                            keyword=keyword,
                            existing_channels=existing_channels,
                        )
                        backoff_attempt = 0  # Reset on clean completion

                        if record:
                            append_channel_to_csv(CONFIG["OUTPUT_CSV"], record)
                            # Add to the in-memory set immediately so that
                            # later keywords don't re-process this channel.
                            existing_channels.add(record["channel_url"])
                            total_accepted += 1

                    except RateLimitError as exc:
                        LOG.warning(f"Rate limit during channel processing: {exc}")
                        backoff_attempt = await backoff_on_rate_limit(backoff_attempt)
                        # Break the inner channel loop and move to the next
                        # keyword after the backoff sleep completes.
                        LOG.warning("Moving to next keyword after rate limit backoff.")
                        break

                    except PlaywrightTimeoutError:
                        LOG.warning(
                            f"Playwright timeout processing {channel_url}. "
                            f"Skipping this channel."
                        )
                        continue

                    except KeyboardInterrupt:
                        # Re-raise immediately so the outer try/finally
                        # handles browser cleanup before the process exits.
                        raise

                    except Exception as exc:
                        # Log the error but do not stop the run — one bad
                        # channel should never abort the entire pipeline.
                        LOG.warning(
                            f"Unexpected error on {channel_url}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        LOG.debug("Full stack trace:", exc_info=True)
                        continue

        except KeyboardInterrupt:
            LOG.info(
                "\nKeyboardInterrupt received — "
                "saving progress and shutting down cleanly."
            )

        finally:
            # Always close the browser, even if an exception propagated here.
            await page.close()
            await context.close()
            await browser.close()

            LOG.info("\n" + "=" * 60)
            LOG.info(
                f"Run complete — {total_accepted} channel(s) accepted and saved "
                f"to '{CONFIG['OUTPUT_CSV']}'."
            )
            LOG.info(f"Full log written to '{CONFIG['LOG_FILE']}'.")
            LOG.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_discovery_pipeline())
