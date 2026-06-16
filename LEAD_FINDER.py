#!/usr/bin/env python3
"""
LEAD_FINDER.py — YouTube Lead Generation Engine
Async Playwright scraper enhanced for video editing cold outreach.

Callable from the UI via run_lead_finder_thread(), or standalone via:
    asyncio.run(_run_async_pipeline(config, ...))
"""

import asyncio
import queue
import random
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

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

VIEWPORTS: List[Dict[str, int]] = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 720},
]

# YouTube channel-filtered search URL
# sp=EgIQAg%3D%3D is the protobuf-encoded "Type: Channel" filter
YOUTUBE_SEARCH_URL = (
    "https://www.youtube.com/results"
    "?search_query={query}"
    "&sp=EgIQAg%3D%3D"
)

# Compiled regex: email addresses
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Compiled regex patterns for classifying social media URLs
_SOCIAL_RE: Dict[str, re.Pattern] = {
    "instagram": re.compile(r"instagram\.com/", re.IGNORECASE),
    "twitter":   re.compile(r"(?:twitter|x)\.com/", re.IGNORECASE),
    "tiktok":    re.compile(r"tiktok\.com/", re.IGNORECASE),
    "linkedin":  re.compile(r"linkedin\.com/", re.IGNORECASE),
}

# Domains that should never be classified as social/other links
_INTERNAL_DOMAINS = {
    "youtube.com", "youtu.be", "google.com", "goo.gl",
    "bit.ly", "ow.ly",  # common shorteners — keep as "other"
}


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class LeadFinderConfig:
    """All user-editable run parameters, passed from the UI."""
    keywords:              List[str]
    max_channels_total:    int   = 50
    max_per_keyword:       int   = 12
    min_subs:              int   = 1_000
    max_subs:              int   = 250_000
    min_total_views:       int   = 25_000
    max_days_since_upload: int   = 45
    jitter_seconds:        Tuple[float, float] = (2.5, 6.5)
    scroll_pause_seconds:  Tuple[float, float] = (1.2, 2.8)
    max_scroll_attempts:   int   = 5
    backoff_initial:       float = 60.0
    backoff_max:           float = 600.0
    backoff_multiplier:    float = 2.0


@dataclass
class LeadResult:
    """One fully-hydrated lead record ready for export."""
    channel_name:        str
    channel_url:         str
    subscribers:         int
    total_views:         int
    video_count:         int
    latest_upload_date:  str
    days_since_upload:   Optional[int]
    email:               str
    instagram:           str
    twitter:             str
    tiktok:              str
    linkedin:            str
    other_links:         str
    description:         str
    keyword_found_by:    str
    scraped_at:          str
    outreach_status:     str = "Not Contacted"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_name":        self.channel_name,
            "channel_url":         self.channel_url,
            "subscribers":         self.subscribers,
            "total_views":         self.total_views,
            "video_count":         self.video_count,
            "latest_upload_date":  self.latest_upload_date,
            "days_since_upload":   self.days_since_upload,
            "email":               self.email,
            "instagram":           self.instagram,
            "twitter":             self.twitter,
            "tiktok":              self.tiktok,
            "linkedin":            self.linkedin,
            "other_links":         self.other_links,
            "description":         self.description,
            "keyword_found_by":    self.keyword_found_by,
            "scraped_at":          self.scraped_at,
            "outreach_status":     self.outreach_status,
        }


# ─────────────────────────────────────────────────────────────────────────────
# QUEUE LOGGER
# Sends structured log messages to a queue.Queue so the Streamlit UI can
# display them without blocking the async event loop.
# ─────────────────────────────────────────────────────────────────────────────
class QueueLogger:
    """Thread-safe logger that routes messages to a queue.Queue."""

    def __init__(self, q: queue.Queue) -> None:
        self._q = q

    def _put(self, level: str, msg: str) -> None:
        try:
            self._q.put_nowait((level, msg))
        except queue.Full:
            pass  # Never block the async event loop over a log message

    def info(self, msg: str)    -> None: self._put("INFO",    msg)
    def success(self, msg: str) -> None: self._put("SUCCESS", msg)
    def warning(self, msg: str) -> None: self._put("WARNING", msg)
    def error(self, msg: str)   -> None: self._put("ERROR",   msg)
    def debug(self, msg: str)   -> None: self._put("DEBUG",   msg)


# ─────────────────────────────────────────────────────────────────────────────
# CUSTOM EXCEPTION
# ─────────────────────────────────────────────────────────────────────────────
class RateLimitError(Exception):
    """Raised when YouTube signals throttling, a 429, or a CAPTCHA."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# METRIC NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────
def normalize_count(raw: str) -> int:
    """
    Convert a YouTube abbreviated count string to an integer.

        "1.4M subscribers" → 1_400_000
        "45K views"        → 45_000
        ""  / None         → 0
    """
    if not raw:
        return 0
    cleaned = raw.replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*([KMBkmb]?)", cleaned)
    if not m:
        return 0
    try:
        number = float(m.group(1))
    except ValueError:
        return 0
    suffix = m.group(2).upper()
    return int(number * {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1))


def parse_relative_date(raw: str) -> Optional[datetime]:
    """
    Convert "3 days ago", "2 weeks ago", "Streamed 4 hours ago" etc. to
    an absolute datetime by subtracting the implied timedelta from now().
    """
    if not raw:
        return None
    text = raw.lower().strip()
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
        match = re.search(pattern, text)
        if match:
            return datetime.now() - delta_fn(int(match.group(1)))
    return None


def days_since(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (datetime.now() - dt).days


# ─────────────────────────────────────────────────────────────────────────────
# ANTI-BOT HELPERS
# ─────────────────────────────────────────────────────────────────────────────
async def random_jitter(min_s: float, max_s: float) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def dismiss_consent_wall(page: Page) -> None:
    """Click through cookie / privacy consent modals if they appear."""
    selectors = [
        "button[aria-label='Accept all']",
        "button[aria-label='Accept All']",
        "button[aria-label='Reject all']",
        "button.VfPpkd-LgbsSe[jsname='b3VHJd']",
        "#dialog button:first-child",
        "form[action*='consent'] button",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2_000):
                await btn.click()
                await asyncio.sleep(0.9)
                return
        except Exception:
            continue


async def detect_rate_limit(page: Page) -> bool:
    """Return True if the page signals throttling or a CAPTCHA block."""
    url = page.url
    try:
        body = await page.inner_text("body", timeout=5_000)
    except Exception:
        body = ""
    return any([
        "sorry/index" in url,
        "captcha" in url.lower(),
        "unusual traffic" in body.lower(),
        "recaptcha" in body.lower(),
        "our systems have detected unusual traffic" in body.lower(),
    ])


async def backoff(attempt: int, cfg: LeadFinderConfig, log: QueueLogger) -> int:
    """Exponential backoff on rate-limit events. Returns next attempt count."""
    wait = min(cfg.backoff_initial * (cfg.backoff_multiplier ** attempt), cfg.backoff_max)
    log.warning(
        f"⚠ RATE LIMIT detected — backing off {wait:.0f}s (attempt #{attempt + 1}). "
        "If this keeps happening, increase JITTER_SECONDS in the config."
    )
    await asyncio.sleep(wait)
    return attempt + 1


# ─────────────────────────────────────────────────────────────────────────────
# BROWSER FACTORY
# ─────────────────────────────────────────────────────────────────────────────
async def create_context(pw: Any) -> Tuple[Browser, BrowserContext]:
    """Launch Chromium with a randomised anti-bot fingerprint."""
    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)

    browser: Browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
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
            "DNT": "1",
            "Sec-Fetch-Mode": "navigate",
        },
    )

    # Inject fingerprint masking before every page's own scripts run
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
        window.chrome = { runtime: {} };
        window.console.debug = () => {};
    """)

    return browser, context


# ─────────────────────────────────────────────────────────────────────────────
# PAGE SCROLL HELPER
# ─────────────────────────────────────────────────────────────────────────────
async def scroll_page(page: Page, cfg: LeadFinderConfig) -> None:
    """Scroll the page to trigger lazy-loaded content."""
    for _ in range(cfg.max_scroll_attempts):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 1.8)")
        await random_jitter(*cfg.scroll_pause_seconds)


# ─────────────────────────────────────────────────────────────────────────────
# YOUTUBE SEARCH  — returns list of channel URLs
# ─────────────────────────────────────────────────────────────────────────────
async def search_channels(
    page: Page,
    keyword: str,
    cfg: LeadFinderConfig,
    log: QueueLogger,
) -> List[str]:
    """
    Navigate to a YouTube channel-filtered search page and return a list of
    absolute channel URLs for this keyword.
    """
    encoded = keyword.replace(" ", "+").replace("#", "%23")
    url = YOUTUBE_SEARCH_URL.format(query=encoded)

    log.info(f"🔍 Searching: '{keyword}'")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        log.warning(f"Timeout on search page for '{keyword}'. Skipping.")
        return []

    await dismiss_consent_wall(page)

    if await detect_rate_limit(page):
        raise RateLimitError(f"Rate limit on search for '{keyword}'.")

    try:
        await page.wait_for_selector("ytd-channel-renderer", timeout=15_000)
    except PlaywrightTimeoutError:
        log.warning(f"No channel results found for '{keyword}'.")
        return []

    await scroll_page(page, cfg)

    urls: List[str] = []
    for renderer in await page.query_selector_all("ytd-channel-renderer"):
        link = await renderer.query_selector("a#channel-name, a#main-link, a#avatar-link")
        if not link:
            continue
        href = await link.get_attribute("href") or ""
        if href.startswith("/"):
            href = "https://www.youtube.com" + href
        if href and href not in urls:
            urls.append(href)

    log.info(f"  → {len(urls)} channel URL(s) found for '{keyword}'.")
    return urls[: cfg.max_per_keyword]


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
async def extract_email(page: Page, log: QueueLogger) -> str:
    """
    Attempt to extract a public email address from the About page.

    Strategy:
        1. Try clicking the "View email address" reveal button.
           If a Google reCAPTCHA appears, dismiss it and log a clear alert.
        2. Regardless of step 1, run an EMAIL_RE regex scan over all visible
           body text to catch plain-text emails in the channel description.

    Returns the first valid email found, or empty string.
    """
    found_emails: List[str] = []

    # ── Step 1: Reveal button ─────────────────────────────────────────────────
    button_selectors = [
        "#email-reveal-button",
        "button[aria-label*='email' i]",
        "button:has-text('View email address')",
        "#channel-links button",
    ]

    for sel in button_selectors:
        try:
            btn = page.locator(sel).first
            if not await btn.is_visible(timeout=2_000):
                continue
            await btn.click()
            await asyncio.sleep(1.8)

            # Check if clicking triggered a CAPTCHA
            page_html = await page.content()
            if "recaptcha" in page_html.lower() or "captcha" in page_html.lower():
                log.warning(
                    "⚠ CAPTCHA triggered when revealing email — "
                    "skipping button method. Will scan description text."
                )
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                break

            # Try to read the revealed email element
            for email_sel in ["#email-text", ".email-container", "#reveal-email"]:
                email_el = await page.query_selector(email_sel)
                if email_el:
                    raw = (await email_el.inner_text()).strip()
                    if "@" in raw:
                        found_emails.append(raw)
            break
        except Exception:
            continue

    # ── Step 2: Regex scan of all visible body text ───────────────────────────
    try:
        body_text = await page.inner_text("body")
        for candidate in EMAIL_RE.findall(body_text):
            # Filter out YouTube's own addresses and common false positives
            domain = candidate.split("@")[-1].lower()
            if domain not in {
                "youtube.com", "google.com", "example.com",
                "sentry.io", "noreply.github.com",
            } and "noreply" not in candidate.lower():
                found_emails.append(candidate)
    except Exception as exc:
        log.debug(f"Email body-text scan failed: {exc}")

    # Deduplicate while preserving order
    seen: Set[str] = set()
    unique: List[str] = []
    for e in found_emails:
        el = e.lower()
        if el not in seen:
            seen.add(el)
            unique.append(e)

    return unique[0] if unique else ""


# ─────────────────────────────────────────────────────────────────────────────
# SOCIAL MEDIA LINK EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
def _classify_url(url: str) -> str:
    """Return the social platform key for a URL, or 'other'."""
    for platform, pattern in _SOCIAL_RE.items():
        if pattern.search(url):
            return platform
    return "other"


def _decode_yt_redirect(href: str) -> str:
    """
    YouTube wraps external links in a redirect:
        https://www.youtube.com/redirect?...&q=ACTUAL_URL_ENCODED
    Extract and decode the actual destination URL.
    """
    if "youtube.com/redirect" in href:
        m = re.search(r"[?&]q=([^&]+)", href)
        if m:
            return unquote(m.group(1))
    return href


async def extract_social_links(page: Page, log: QueueLogger) -> Dict[str, str]:
    """
    Extract and classify all external social media links from the About page.

    Returns a dict with keys: instagram, twitter, tiktok, linkedin, other_links.
    """
    result: Dict[str, Any] = {
        "instagram":   "",
        "twitter":     "",
        "tiktok":      "",
        "linkedin":    "",
        "other_links": [],
    }

    raw_hrefs: List[str] = []

    # ── Method 1: query anchor elements in known link-section selectors ───────
    link_selectors = [
        "ytd-channel-external-link-view-model a",
        "#links-section a[href]",
        "#link-list-container a[href]",
        "ytd-about-channel-renderer a[href]",
        "#channel-links a[href]",
    ]
    for sel in link_selectors:
        try:
            for el in await page.query_selector_all(sel):
                href = await el.get_attribute("href") or ""
                if href:
                    raw_hrefs.append(href)
        except Exception:
            continue

    # ── Method 2: regex over raw HTML for YouTube redirect URLs ──────────────
    try:
        html = await page.content()
        for encoded in re.findall(r'href="(https://www\.youtube\.com/redirect[^"]+)"', html):
            raw_hrefs.append(encoded)
        # Also grab direct external hrefs embedded in the HTML
        for direct in re.findall(r'href="(https?://[^"]+)"', html):
            if "youtube.com" not in direct and "youtu.be" not in direct:
                raw_hrefs.append(direct)
    except Exception:
        pass

    # ── Decode, deduplicate, and classify ────────────────────────────────────
    seen: Set[str] = set()
    for raw in raw_hrefs:
        decoded = _decode_yt_redirect(raw)
        if decoded in seen or not decoded.startswith("http"):
            continue
        seen.add(decoded)

        platform = _classify_url(decoded)
        if platform == "other":
            # Skip YouTube-internal links that slipped through
            parsed = urlparse(decoded)
            if parsed.netloc in {"www.youtube.com", "youtube.com", "youtu.be"}:
                continue
            result["other_links"].append(decoded)
        elif not result[platform]:
            result[platform] = decoded

    result["other_links"] = " | ".join(result["other_links"][:5])  # cap at 5 links

    log.debug(
        f"  Social → IG:{bool(result['instagram'])} "
        f"TW:{bool(result['twitter'])} "
        f"TT:{bool(result['tiktok'])} "
        f"LI:{bool(result['linkedin'])} "
        f"other:{len(result['other_links'].split(' | ')) if result['other_links'] else 0}"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CHANNEL DESCRIPTION EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
async def extract_description(page: Page) -> str:
    """
    Extract the channel's bio / About description text.
    Tries multiple known selectors then falls back to a broad body-text search.
    Capped at 2 000 characters to keep CSV rows manageable.
    """
    selectors = [
        "#description yt-formatted-string",
        "#channel-description",
        "#about-description",
        "ytd-channel-about-metadata-renderer #description",
        "#description-container #description",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text[:2_000]
        except Exception:
            continue

    # Broad fallback: grab the about section container
    for broad in ["#about-section", "#about-container", "#metadata-container"]:
        try:
            el = await page.query_selector(broad)
            if el:
                text = (await el.inner_text()).strip()
                if len(text) > 30:  # ignore single-word matches
                    return text[:2_000]
        except Exception:
            continue

    return ""


# ─────────────────────────────────────────────────────────────────────────────
# COMBINED ABOUT-TAB SCRAPER
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_about_tab(
    page: Page,
    channel_url: str,
    log: QueueLogger,
) -> Dict[str, Any]:
    """
    Navigate to {channel_url}/about and extract:
        channel_name, subscribers, total_views, video_count,
        email, instagram, twitter, tiktok, linkedin, other_links, description.

    All fields default gracefully to empty/0 — a failure on one field never
    prevents the remaining fields from being extracted.
    """
    about_url = channel_url.rstrip("/") + "/about"
    data: Dict[str, Any] = {
        "channel_name": "", "subscribers": 0, "total_views": 0,
        "video_count": 0, "email": "", "instagram": "", "twitter": "",
        "tiktok": "", "linkedin": "", "other_links": "", "description": "",
    }

    try:
        await page.goto(about_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        log.warning(f"Timeout on about tab: {about_url}")
        return data

    await dismiss_consent_wall(page)

    if await detect_rate_limit(page):
        raise RateLimitError(f"Rate limit on about tab for {channel_url}.")

    # ── Channel name ──────────────────────────────────────────────────────────
    for name_sel in [
        "#channel-name yt-formatted-string",
        "yt-formatted-string#text.ytd-channel-name",
        "#inner-header-container #text",
        "#page-header h1",
        "h1.ytd-channel-name",
    ]:
        try:
            el = await page.query_selector(name_sel)
            if el:
                name = (await el.inner_text()).strip()
                if name:
                    data["channel_name"] = name
                    break
        except Exception:
            continue

    # ── Subscriber count (CSS selector first, body-text regex fallback) ───────
    try:
        await page.wait_for_selector("#subscriber-count", timeout=8_000)
        el = await page.query_selector("#subscriber-count")
        if el:
            data["subscribers"] = normalize_count(await el.inner_text())
    except Exception:
        pass

    try:
        body = await page.inner_text("body")

        if data["subscribers"] == 0:
            m = re.search(r"([\d.,]+\s*[KMB]?)\s+subscriber", body, re.IGNORECASE)
            if m:
                data["subscribers"] = normalize_count(m.group(1))

        m = re.search(r"([\d.,]+\s*[KMB]?)\s+view", body, re.IGNORECASE)
        if m:
            data["total_views"] = normalize_count(m.group(1))

        m = re.search(r"([\d,]+)\s+video", body, re.IGNORECASE)
        if m:
            data["video_count"] = normalize_count(m.group(1))

        if not data["channel_name"]:
            html = await page.content()
            tm = re.search(r"<title>\s*(.+?)\s*[-–|]", html)
            if tm:
                data["channel_name"] = tm.group(1).strip()
    except Exception as exc:
        log.debug(f"Body-text extraction failed for {channel_url}: {exc}")

    # ── Email ─────────────────────────────────────────────────────────────────
    try:
        data["email"] = await extract_email(page, log)
    except Exception as exc:
        log.debug(f"Email extraction error: {exc}")

    # ── Social links ──────────────────────────────────────────────────────────
    try:
        social = await extract_social_links(page, log)
        data.update(social)
    except Exception as exc:
        log.debug(f"Social link extraction error: {exc}")

    # ── Description ───────────────────────────────────────────────────────────
    try:
        data["description"] = await extract_description(page)
    except Exception as exc:
        log.debug(f"Description extraction error: {exc}")

    log.debug(
        f"  About → {data['channel_name']!r} | "
        f"subs={data['subscribers']:,} | views={data['total_views']:,} | "
        f"email={bool(data['email'])} | ig={bool(data['instagram'])}"
    )
    return data


# ─────────────────────────────────────────────────────────────────────────────
# VIDEOS TAB SCRAPER  — latest upload date
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_latest_upload(
    page: Page,
    channel_url: str,
    log: QueueLogger,
) -> Optional[str]:
    """
    Navigate to {channel_url}/videos and return the relative date string of the
    most recently uploaded public video, or None if unavailable.
    """
    videos_url = channel_url.rstrip("/") + "/videos"

    try:
        await page.goto(videos_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        log.warning(f"Timeout on videos tab: {videos_url}")
        return None

    await dismiss_consent_wall(page)

    if await detect_rate_limit(page):
        raise RateLimitError(f"Rate limit on videos tab for {channel_url}.")

    try:
        await page.wait_for_selector(
            "ytd-grid-video-renderer, ytd-rich-item-renderer",
            timeout=12_000,
        )
    except PlaywrightTimeoutError:
        log.debug(f"No video grid for {channel_url}.")
        return None

    # Try CSS selectors across all known YouTube layout variants
    for sel in [
        "ytd-grid-video-renderer #metadata-line span:last-child",
        "ytd-rich-item-renderer #metadata-line span:last-child",
        "ytd-video-meta-block #metadata-line span",
        "ytd-compact-video-renderer #metadata-line span:last-child",
    ]:
        try:
            for el in await page.query_selector_all(sel):
                text = (await el.inner_text()).strip()
                if re.search(r"\d+\s+(second|minute|hour|day|week|month|year)", text, re.I):
                    return text
        except Exception:
            continue

    # Body-text fallback: find first relative date anywhere on the page
    try:
        body = await page.inner_text("body")
        dates = re.findall(
            r"\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago",
            body, re.IGNORECASE,
        )
        if dates:
            return dates[0]
    except Exception:
        pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# FILTER ENGINE
# ─────────────────────────────────────────────────────────────────────────────
def passes_filters(
    cfg: LeadFinderConfig,
    subscribers: int,
    total_views: int,
    upload_age: Optional[int],
) -> Tuple[bool, str]:
    """
    Check a channel's metrics against all configured thresholds.
    Returns (passed, reason_string).
    """
    if subscribers < cfg.min_subs:
        return False, f"subs {subscribers:,} < min {cfg.min_subs:,}"
    if subscribers > cfg.max_subs:
        return False, f"subs {subscribers:,} > max {cfg.max_subs:,}"
    if total_views < cfg.min_total_views:
        return False, f"views {total_views:,} < min {cfg.min_total_views:,}"
    if upload_age is None:
        return False, "upload date unknown"
    if upload_age > cfg.max_days_since_upload:
        return False, f"last upload {upload_age}d ago > max {cfg.max_days_since_upload}d"
    return True, "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE CHANNEL PIPELINE
# ─────────────────────────────────────────────────────────────────────────────
async def process_channel(
    page: Page,
    channel_url: str,
    keyword: str,
    cfg: LeadFinderConfig,
    log: QueueLogger,
    seen: Set[str],
) -> Optional[Dict[str, Any]]:
    """
    Full extraction + filter pipeline for one channel URL.

    Returns a dict ready for the results queue, or None if rejected/already seen.
    Any RateLimitError raised inside propagates up to the main loop.
    """
    norm_url = channel_url.rstrip("/")
    if norm_url in seen or channel_url in seen:
        log.debug(f"Skip (checkpoint): {norm_url}")
        return None

    log.info(f"🔎 Processing: {norm_url}")

    await random_jitter(*cfg.jitter_seconds)
    about = await scrape_about_tab(page, norm_url, log)

    await random_jitter(*cfg.jitter_seconds)
    latest_raw = await scrape_latest_upload(page, norm_url, log)

    latest_dt  = parse_relative_date(latest_raw or "")
    upload_age = days_since(latest_dt)

    passed, reason = passes_filters(
        cfg, about["subscribers"], about["total_views"], upload_age
    )
    label = about["channel_name"] or norm_url

    if not passed:
        log.info(f"  ✗ REJECTED [{reason}]: {label}")
        return None

    log.success(
        f"  ✓ LEAD: {label} | "
        f"{about['subscribers']:,} subs | "
        f"{about['total_views']:,} views | "
        f"Uploaded {upload_age}d ago | "
        f"Email: {'✓' if about['email'] else '✗'}"
    )

    lead = LeadResult(
        channel_name=about["channel_name"],
        channel_url=norm_url,
        subscribers=about["subscribers"],
        total_views=about["total_views"],
        video_count=about["video_count"],
        latest_upload_date=latest_dt.strftime("%Y-%m-%d") if latest_dt else "",
        days_since_upload=upload_age,
        email=about["email"],
        instagram=about["instagram"],
        twitter=about["twitter"],
        tiktok=about["tiktok"],
        linkedin=about["linkedin"],
        other_links=about["other_links"],
        description=about["description"],
        keyword_found_by=keyword,
        scraped_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return lead.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ASYNC PIPELINE
# ─────────────────────────────────────────────────────────────────────────────
async def _run_async_pipeline(
    cfg: LeadFinderConfig,
    log_q: queue.Queue,
    progress_q: queue.Queue,
    results_q: queue.Queue,
    pause_ev: threading.Event,
    stop_ev: threading.Event,
) -> None:
    """
    Master coroutine. Iterates over all keywords, discovers channels,
    processes each through the extraction + filter pipeline, and pushes
    results to the three queues for real-time UI consumption.

    Pause / Stop protocol:
        pause_ev.wait() — blocks while the event is cleared (paused state).
        stop_ev.is_set() — signals an immediate clean exit.
    """
    log = QueueLogger(log_q)

    log.info("=" * 54)
    log.info("YouTube Lead Generation Engine — Starting")
    log.info(
        f"Filters: {cfg.min_subs:,}–{cfg.max_subs:,} subs | "
        f"{cfg.min_total_views:,}+ views | "
        f"≤{cfg.max_days_since_upload}d since upload"
    )
    log.info(f"Keywords: {', '.join(cfg.keywords)}")
    log.info("=" * 54)

    seen_channels: Set[str] = set()
    channels_done = 0
    leads_found   = 0
    backoff_count = 0
    total_est     = cfg.max_channels_total  # denominator for progress %

    async with async_playwright() as pw:
        browser, context = await create_context(pw)
        page = await context.new_page()

        try:
            for keyword in cfg.keywords:
                if stop_ev.is_set():
                    break

                log.info(f"\n{'─' * 44}")
                log.info(f'Keyword: "{keyword}"')
                log.info(f"{'─' * 44}")

                # ── Discover channel URLs ─────────────────────────────────────
                channel_urls: List[str] = []
                try:
                    channel_urls = await search_channels(page, keyword, cfg, log)
                    backoff_count = 0
                except RateLimitError:
                    backoff_count = await backoff(backoff_count, cfg, log)
                    log.warning(f"Skipping '{keyword}' after rate limit.")
                    continue
                except PlaywrightTimeoutError:
                    log.warning(f"Timeout searching for '{keyword}'. Skipping.")
                    continue

                if not channel_urls:
                    log.info(f"No URLs returned for '{keyword}'.")
                    continue

                # ── Process each URL ──────────────────────────────────────────
                for channel_url in channel_urls:
                    if stop_ev.is_set():
                        break

                    # ── Pause checkpoint ──────────────────────────────────────
                    if not pause_ev.is_set():
                        log.info("⏸ Scraper paused — waiting for resume…")
                        pause_ev.wait()  # blocks here until UI clicks Resume
                        log.info("▶ Resumed.")

                    if channels_done >= cfg.max_channels_total:
                        log.info("Session channel cap reached. Stopping.")
                        stop_ev.set()
                        break

                    try:
                        record = await process_channel(
                            page, channel_url, keyword, cfg, log, seen_channels
                        )
                        backoff_count = 0
                        channels_done += 1
                        seen_channels.add(channel_url.rstrip("/"))

                        if record:
                            leads_found += 1
                            results_q.put_nowait(record)

                        # Push progress update
                        pct = min(channels_done / total_est, 1.0)
                        progress_q.put_nowait((pct, channels_done, leads_found))

                    except RateLimitError:
                        backoff_count = await backoff(backoff_count, cfg, log)
                        log.warning("Moving to next keyword after rate limit backoff.")
                        break

                    except PlaywrightTimeoutError:
                        log.warning(f"Timeout on {channel_url}. Skipping.")
                        channels_done += 1
                        continue

                    except Exception as exc:
                        log.warning(f"Error on {channel_url}: {type(exc).__name__}: {exc}")
                        channels_done += 1
                        continue

        except Exception as exc:
            log.error(f"Fatal pipeline error: {exc}")

        finally:
            await page.close()
            await context.close()
            await browser.close()

    # Final progress = 100%
    progress_q.put_nowait((1.0, channels_done, leads_found))
    log.success("=" * 54)
    log.success(
        f"Run complete — {leads_found} lead(s) found from "
        f"{channels_done} channels processed."
    )
    log.success("=" * 54)


# ─────────────────────────────────────────────────────────────────────────────
# THREAD ENTRY POINT  — called by APP_UI.py via threading.Thread
# ─────────────────────────────────────────────────────────────────────────────
def run_lead_finder_thread(
    cfg: LeadFinderConfig,
    log_q: queue.Queue,
    progress_q: queue.Queue,
    results_q: queue.Queue,
    pause_ev: threading.Event,
    stop_ev: threading.Event,
) -> None:
    """
    Creates a new asyncio event loop and runs the async pipeline inside it.
    Must be invoked from a non-async context (e.g., threading.Thread target).
    Streamlit runs in its own event loop — using a dedicated loop here
    prevents 'This event loop is already running' errors.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            _run_async_pipeline(cfg, log_q, progress_q, results_q, pause_ev, stop_ev)
        )
    except Exception as exc:
        try:
            log_q.put_nowait(("ERROR", f"Thread-level fatal error: {exc}"))
        except Exception:
            pass
    finally:
        loop.close()
