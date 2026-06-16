# ARCHITECTURE.md — System Design & Data Models

---

## System Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    YouTube Creator Discovery Scraper                      │
│                                                                            │
│  CONFIG block (keywords, filter thresholds)                               │
│       │                                                                    │
│       ▼                                                                    │
│  ┌─────────────────────────┐     ┌──────────────────────────────────────┐ │
│  │  State Persistence      │◄───►│  Target Generation Pipeline          │ │
│  │  Engine                 │     │  keyword → encoded URL → channel     │ │
│  │  (CSV checkpoint)       │     │  URL list                            │ │
│  └─────────────────────────┘     └─────────────────┬────────────────────┘ │
│                                                     │                      │
│                                                     ▼                      │
│                                  ┌──────────────────────────────────────┐  │
│                                  │  Playwright Browser Context           │  │
│                                  │  Anti-bot evasion layer:              │  │
│                                  │  • Random User-Agent                  │  │
│                                  │  • Random viewport                    │  │
│                                  │  • webdriver flag masked              │  │
│                                  │  • chrome runtime spoofed             │  │
│                                  │  • cookie wall auto-dismissed         │  │
│                                  └───────────────┬──────────────────────┘  │
│                                                  │                          │
│                                    ┌─────────────┴─────────────┐           │
│                                    │                            │           │
│                                    ▼                            ▼           │
│                          ┌──────────────────┐       ┌────────────────────┐ │
│                          │  /about tab      │       │  /videos tab       │ │
│                          │  Extraction      │       │  Extraction        │ │
│                          │  • channel name  │       │  • latest upload   │ │
│                          │  • subscribers   │       │    date string     │ │
│                          │  • total views   │       └────────┬───────────┘ │
│                          │  • video count   │                │             │
│                          └────────┬─────────┘                │             │
│                                   │                          │             │
│                                   └──────────┬───────────────┘             │
│                                              │                              │
│                                              ▼                              │
│                              ┌───────────────────────────────┐             │
│                              │  Metric Normalization Engine  │             │
│                              │  "1.4M"     → 1,400,000       │             │
│                              │  "45K"      → 45,000          │             │
│                              │  "3 days ago" → datetime obj  │             │
│                              └──────────────┬────────────────┘             │
│                                             │                               │
│                                             ▼                               │
│                              ┌───────────────────────────────┐             │
│                              │  Filter Engine                │             │
│                              │  MIN_SUBS / MAX_SUBS          │             │
│                              │  MIN_TOTAL_VIEWS              │             │
│                              │  MAX_DAYS_SINCE_UPLOAD        │             │
│                              └──────────────┬────────────────┘             │
│                                             │                               │
│                               PASS ─────────┘─────────── REJECT (logged)  │
│                                │                                            │
│                                ▼                                            │
│                   ┌────────────────────────────┐                           │
│                   │  discovered_channels.csv   │                           │
│                   │  (one row written per      │                           │
│                   │   accepted channel,        │                           │
│                   │   immediately on pass)     │                           │
│                   └────────────────────────────┘                           │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Target Generation Pipeline

### Stage A — Keyword to Search URL

Each string in `CONFIG["KEYWORDS"]` is URL-encoded and injected into
YouTube's channel-filtered search endpoint:

```
Input keyword:  "python tutorials"
After encoding: "python+tutorials"

Assembled URL:
  https://www.youtube.com/results
    ?search_query=python+tutorials
    &sp=EgIQAg%3D%3D
         ││││││││└── URL-encoded '='
         │└─────────── Base64: EgIQAg==
         └──────────── Protobuf-encoded "Type: Channel" filter

Hashtag example: "#learnpython" → "%23learnpython"
```

The `sp=EgIQAg%3D%3D` parameter is YouTube's internal serialization of the
"Type → Channel" search filter toggle. Without it, results include videos,
playlists, and Shorts mixed with channels.

### Stage B — Channel URL Extraction

The rendered search results DOM contains `ytd-channel-renderer` custom
elements — one per channel result card. Each card has a primary anchor
(`a#channel-name` or `a#main-link`) whose `href` is the channel path.

Extraction sequence:
1. Wait for at least one `ytd-channel-renderer` in the DOM (15 s timeout).
2. Scroll the page `MAX_SCROLL_ATTEMPTS` times to trigger lazy-loading.
3. `query_selector_all("ytd-channel-renderer")` collects all cards.
4. For each card, read the `href` attribute and make it absolute.
5. Deduplicate within the keyword result set.
6. Truncate to `MAX_CHANNELS_PER_KEYWORD` (`None` = no cap).

### Stage C — Per-Channel Data Collection

Two additional page navigations per channel:

| Destination | Suffix appended | Data collected |
|---|---|---|
| About tab | `/about` | name, subscribers, total views, video count |
| Videos tab | `/videos` | relative upload date of newest video |

Both suffix patterns work across all YouTube channel URL formats:
`/@handle`, `/c/Name`, and `/channel/UCxxxxxxxxxxxxxxxx`.

---

## 2. Metric Normalization Engine

YouTube renders all counts as abbreviated human-readable strings in the DOM.
The normalization layer converts every value to a plain Python `int` before
any filter comparison.

### `normalize_count(raw: str) → int`

Two-pass transform:

**Pass 1 — Strip formatting noise:**
```
"1,234,567 views"  →  "1234567 views"      (remove commas)
"  1.4M subs  "    →  "1.4M subs"          (strip whitespace)
```

**Pass 2 — Regex extract + suffix multiply:**
```
Regex: r"([\d.]+)\s*([KMBkmb]?)"

Input string       number_str  suffix  float   multiplier  result
─────────────────────────────────────────────────────────────────────
"1.4M subscribers" "1.4"       "M"     1.4     1_000_000   1_400_000
"45K views"        "45"        "K"     45.0    1_000       45_000
"2.3B"             "2.3"       "B"     2.3     1_000_000_000  2_300_000_000
"1234567"          "1234567"   ""      1234567 1           1_234_567
"987"              "987"       ""      987.0   1           987
""  / None                                                  0
```

Multiplier table:
```python
{"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
# Empty suffix → multiplier 1 (plain integer)
```

### `parse_relative_date(raw: str) → Optional[datetime]`

YouTube Videos tabs express recency as relative strings. The engine
converts each to an absolute `datetime` by subtracting the inferred
`timedelta` from `datetime.now()` at parse time.

```
Input                    Matched pattern       timedelta subtracted
───────────────────────────────────────────────────────────────────
"3 days ago"         →   r"(\d+)\s+day"    →  timedelta(days=3)
"2 weeks ago"        →   r"(\d+)\s+week"   →  timedelta(weeks=2)
"5 months ago"       →   r"(\d+)\s+month"  →  timedelta(days=150)
"1 year ago"         →   r"(\d+)\s+year"   →  timedelta(days=365)
"4 hours ago"        →   r"(\d+)\s+hour"   →  timedelta(hours=4)
"Streamed 2 days ago"  ← substring "2 days ago" is still matched
```

`days_since(dt)` then yields `(datetime.now() - dt).days` as the
integer age used in the `MAX_DAYS_SINCE_UPLOAD` filter check.

---

## 3. State Persistence Engine

### Design: Incremental CSV with Startup Recovery

Results are written to a CSV **one row at a time**, immediately after a
channel passes all filters. If the process crashes (network error, CAPTCHA,
Ctrl+C), every channel accepted before the crash is already on disk.
Restarting recovers from exactly where the run left off.

### Startup Recovery Protocol

```
On startup:
  └─► Does OUTPUT_CSV exist?
        │
        ├─ NO  → existing_channels = empty set{}
        │        LOG: "Starting fresh run"
        │
        └─ YES → pd.read_csv(path, usecols=["channel_url"])
                 → existing_channels = set of all saved channel_url values
                 LOG: "N previously scraped channels will be skipped"

During the run, for each candidate channel_url:
  └─► channel_url in existing_channels?
        │
        ├─ YES → skip (LOG.debug)
        └─ NO  → process → if PASS → append row → add to existing_channels
```

Loading only `usecols=["channel_url"]` keeps memory flat even with tens
of thousands of rows in the checkpoint file.

### CSV Write Mechanics

```
File opened in append mode ("a") on every write.
Header row written ONLY when the file is new or empty.

  Run 1, channel A accepted → write header row, write row A
  Run 1, channel B accepted → append row B (no header)
  [crash]
  Run 2 (restart)           → load checkpoint, skip A and B
  Run 2, channel C accepted → append row C (no header — already present)
```

The resulting file is always valid and importable with a single
`pd.read_csv("discovered_channels.csv")`.

### Output CSV Schema

| Column | Python type | Example value |
|---|---|---|
| `channel_name` | str | "Tech With Tim" |
| `channel_url` | str | "https://www.youtube.com/@TechWithTim" |
| `subscribers` | int | 1400000 |
| `total_views` | int | 87500000 |
| `video_count` | int | 412 |
| `latest_upload_date` | str (YYYY-MM-DD) | "2024-05-30" |
| `days_since_upload` | int | 4 |
| `keyword_found_by` | str | "python tutorials" |
| `scraped_at` | str (YYYY-MM-DD HH:MM:SS) | "2024-06-01 14:23:11" |

---

## 4. Anti-Bot Evasion Layer

| Technique | Where applied | Detail |
|---|---|---|
| User-Agent rotation | `create_browser_context` | Pool of 6 real desktop UA strings; one chosen randomly per session |
| Viewport randomization | `create_browser_context` | 5 common desktop resolutions; one chosen randomly per session |
| WebDriver flag mask | `add_init_script` | `navigator.webdriver = undefined` injected before every page load |
| Chrome runtime spoof | `add_init_script` | `window.chrome = { runtime: {} }` injected to pass fingerprint checks |
| Plugin count spoof | `add_init_script` | `navigator.plugins` set to a non-empty array |
| Console debug suppress | `add_init_script` | `console.debug = () => {}` blocks debug-channel fingerprinting |
| Random jitter delays | Between every `page.goto` | `random.uniform(JITTER_SECONDS)` sleep — default 2.5–6.5 s |
| Scroll pause jitter | During result scrolling | `random.uniform(SCROLL_PAUSE_SECONDS)` — default 1.2–2.8 s |
| Consent wall dismissal | After every navigation | Tries multiple known YouTube accept/dismiss selectors |
| Rate limit detection | After every navigation | Checks URL and body text for block/captcha signals |
| Exponential backoff | On `RateLimitError` | 60 s → 120 s → 240 s → 480 s → 600 s (cap) |

---

## 5. Rate Limit Backoff Model

```
Formula:
  wait = min(BACKOFF_INITIAL × BACKOFF_MULTIPLIER^attempt, BACKOFF_MAX)

Default values: initial=60 s, multiplier=2.0, max=600 s

  attempt 0  →  min(60  × 2^0, 600)  =   60 s
  attempt 1  →  min(60  × 2^1, 600)  =  120 s
  attempt 2  →  min(60  × 2^2, 600)  =  240 s
  attempt 3  →  min(60  × 2^3, 600)  =  480 s
  attempt 4  →  min(60  × 2^4, 600)  =  600 s  (ceiling reached)
  attempt 5+ →  600 s                           (ceiling maintained)
```

Each backoff event logs a prominent WARNING to both stdout and `scraper.log`.
The scraper does NOT crash — it resumes the keyword list after sleeping.

---

## 6. Logging Architecture

```
logging.Logger "yt_scraper"
    │
    ├── StreamHandler (stdout)
    │       level: INFO
    │       Shows: search progress, ACCEPT/REJECT per channel, warnings
    │
    └── FileHandler (scraper.log, append mode)
            level: DEBUG
            Shows: everything above + selector attempts, scroll steps,
                   exact extracted text, stack traces on errors
```

Both handlers share the same formatter:
```
2024-06-01 14:23:11 [INFO    ] Processing: https://www.youtube.com/@TechWithTim
2024-06-01 14:23:14 [INFO    ]   ACCEPTED: Tech With Tim | 1,400,000 subs | ...
2024-06-01 14:23:16 [DEBUG   ]   About tab → name='Tech With Tim' subs=1400000 ...
```
