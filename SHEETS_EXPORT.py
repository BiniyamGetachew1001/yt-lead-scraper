#!/usr/bin/env python3
"""
SHEETS_EXPORT.py — Google Sheets CRM Export
Converts the scraped lead list into a formatted outreach-ready Google Sheet.

Authentication uses a service account credentials.json file.
See UPGRADE_README.md for the full setup tutorial.
"""

import time
from typing import Any, Dict, List

import gspread
from google.oauth2.service_account import Credentials


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# OAuth 2.0 scopes required for reading and writing Sheets + Drive
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column headers — MUST stay in sync with the field mapping below
SHEET_HEADERS: List[str] = [
    "Channel Name",
    "Channel URL",
    "Subscribers",
    "Total Views",
    "Latest Upload Age (Days)",
    "Email",
    "Instagram",
    "TikTok",
    "Twitter / X",
    "LinkedIn",
    "Other Social Links",
    "Channel Description",
    "Channel Niche (Keyword)",
    "Outreach Status",
    "Notes",
    "Date Added",
]

# Maps LeadResult dict keys → 1-based column index in the sheet
# Columns 15 (Notes) and 16 (Date Added) are handled separately below
_FIELD_TO_COL: Dict[str, int] = {
    "channel_name":       1,
    "channel_url":        2,
    "subscribers":        3,
    "total_views":        4,
    "days_since_upload":  5,
    "email":              6,
    "instagram":          7,
    "tiktok":             8,
    "twitter":            9,
    "linkedin":           10,
    "other_links":        11,
    "description":        12,
    "keyword_found_by":   13,
    "outreach_status":    14,
    # col 15 = "Notes" → blank by default
    "scraped_at":         16,
}

# Colour used for the header row background (hex without #)
_HEADER_BG_COLOUR = {"red": 0.11, "green": 0.13, "blue": 0.19}  # dark navy
_HEADER_FG_COLOUR = {"red": 1.0,  "green": 1.0,  "blue": 1.0}   # white text


# ─────────────────────────────────────────────────────────────────────────────
# AUTHENTICATION
# ─────────────────────────────────────────────────────────────────────────────
def _get_client(credentials_path: str) -> gspread.Client:
    """
    Authenticate with the Google Sheets API using a service account key file.

    credentials_path — path to the downloaded service account JSON key file
                       (e.g., 'credentials.json' in the project root).
    """
    creds = Credentials.from_service_account_file(credentials_path, scopes=_SCOPES)
    return gspread.authorize(creds)


# ─────────────────────────────────────────────────────────────────────────────
# SHEET FORMATTING
# ─────────────────────────────────────────────────────────────────────────────
def _format_header_row(worksheet: gspread.Worksheet) -> None:
    """
    Apply formatting to the header row (row 1):
        • Bold, white text on a dark navy background
        • Freeze the header row so it stays visible when scrolling
        • Auto-resize columns A–P to fit content
    """
    sheet_id = worksheet.spreadsheet.id
    ws_id    = worksheet.id
    n_cols   = len(SHEET_HEADERS)

    # Build the batchUpdate request body
    requests = [
        # ── Header background + font ─────────────────────────────────────────
        {
            "repeatCell": {
                "range": {
                    "sheetId":          ws_id,
                    "startRowIndex":    0,
                    "endRowIndex":      1,
                    "startColumnIndex": 0,
                    "endColumnIndex":   n_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _HEADER_BG_COLOUR,
                        "textFormat": {
                            "bold":            True,
                            "fontSize":        10,
                            "foregroundColor": _HEADER_FG_COLOUR,
                        },
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment":   "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        },
        # ── Freeze header row ─────────────────────────────────────────────────
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # ── Auto-resize all data columns ──────────────────────────────────────
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId":    ws_id,
                    "dimension":  "COLUMNS",
                    "startIndex": 0,
                    "endIndex":   n_cols,
                }
            }
        },
        # ── Set a reasonable row height for the header ────────────────────────
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId":    ws_id,
                    "dimension":  "ROWS",
                    "startIndex": 0,
                    "endIndex":   1,
                },
                "properties": {"pixelSize": 36},
                "fields": "pixelSize",
            }
        },
    ]

    worksheet.spreadsheet.batch_update({"requests": requests})


def _set_outreach_status_dropdown(worksheet: gspread.Worksheet) -> None:
    """
    Add a dropdown validation to the 'Outreach Status' column (col 14)
    so the user can update lead status directly in the sheet.
    """
    ws_id = worksheet.id
    status_options = [
        "Not Contacted",
        "Email Sent",
        "Followed Up",
        "Replied",
        "Call Booked",
        "Converted",
        "Not Interested",
        "Bounced",
    ]
    condition_values = [{"userEnteredValue": s} for s in status_options]

    requests = [
        {
            "setDataValidation": {
                "range": {
                    "sheetId":          ws_id,
                    "startRowIndex":    1,      # skip header
                    "endRowIndex":      2000,   # cover up to 1999 data rows
                    "startColumnIndex": 13,     # col N (0-indexed) = col 14 (1-indexed)
                    "endColumnIndex":   14,
                },
                "rule": {
                    "condition": {
                        "type":   "ONE_OF_LIST",
                        "values": condition_values,
                    },
                    "showCustomUi":   True,
                    "strict":         False,
                },
            }
        }
    ]
    worksheet.spreadsheet.batch_update({"requests": requests})


# ─────────────────────────────────────────────────────────────────────────────
# LEAD → ROW CONVERTER
# ─────────────────────────────────────────────────────────────────────────────
def _lead_to_row(lead: Dict[str, Any]) -> List[Any]:
    """
    Convert a lead dict into an ordered list matching SHEET_HEADERS column order.
    Missing keys default to empty string / "Not Contacted".
    """
    row: List[Any] = [""] * len(SHEET_HEADERS)

    for field, col_idx in _FIELD_TO_COL.items():
        value = lead.get(field, "")
        # Format large integers with comma separators for readability
        if isinstance(value, int) and value > 999:
            value = f"{value:,}"
        row[col_idx - 1] = value if value is not None else ""

    # Notes column (index 14, 1-based col 15) stays blank — filled by the user
    # Date Added is already in scraped_at; no extra action needed

    return row


# ─────────────────────────────────────────────────────────────────────────────
# MAIN EXPORT FUNCTION
# ─────────────────────────────────────────────────────────────────────────────
def export_to_google_sheets(
    leads: List[Dict[str, Any]],
    sheet_name: str,
    credentials_path: str,
) -> str:
    """
    Export the full leads list to a Google Sheet.

    Behaviour:
        • If a sheet named `sheet_name` already exists in the service account's
          Drive, it is OPENED and new leads are APPENDED below existing rows.
        • If it does not exist, a new spreadsheet is CREATED, headers are written
          and formatted, and all leads are inserted.
        • In both cases, the function returns the URL of the sheet so the caller
          can surface it as a clickable link in the UI.

    Raises any exception to the caller so the UI can display it in an st.error().
    """
    if not leads:
        raise ValueError("No leads to export. Scrape some channels first.")

    client = _get_client(credentials_path)

    # ── Open or create the spreadsheet ───────────────────────────────────────
    try:
        spreadsheet = client.open(sheet_name)
        worksheet   = spreadsheet.sheet1
        is_new      = False
    except gspread.exceptions.SpreadsheetNotFound:
        spreadsheet = client.create(sheet_name)
        # Share with anyone with the link so the user can open it in a browser
        spreadsheet.share(None, perm_type="anyone", role="writer")
        worksheet   = spreadsheet.sheet1
        worksheet.update_title("Leads")
        is_new = True

    # ── Write headers (new sheet only) ───────────────────────────────────────
    if is_new:
        worksheet.update("A1", [SHEET_HEADERS])
        _format_header_row(worksheet)
        try:
            _set_outreach_status_dropdown(worksheet)
        except Exception:
            pass  # Dropdown validation is cosmetic — never fail the export over it
        time.sleep(0.5)  # Let the API settle before the data write

    # ── Determine the next empty row ─────────────────────────────────────────
    existing_values = worksheet.get_all_values()
    next_row        = len(existing_values) + 1  # 1-based, accounting for header

    # ── Convert leads to row arrays ───────────────────────────────────────────
    rows = [_lead_to_row(lead) for lead in leads]

    # ── Batch-write all rows in a single API call ─────────────────────────────
    # gspread uses A1 notation; we target from the next empty row downward.
    start_cell = f"A{next_row}"
    worksheet.update(start_cell, rows, value_input_option="USER_ENTERED")

    # ── Bold the Channel URL cells so they're easy to click in Sheets ─────────
    try:
        url_col   = 2  # column B
        ws_id     = worksheet.id
        n_rows    = len(rows)
        requests  = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId":          ws_id,
                        "startRowIndex":    next_row - 1,
                        "endRowIndex":      next_row - 1 + n_rows,
                        "startColumnIndex": url_col - 1,
                        "endColumnIndex":   url_col,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"foregroundColor": {"red": 0.26, "green": 0.52, "blue": 0.96}}
                        }
                    },
                    "fields": "userEnteredFormat.textFormat.foregroundColor",
                }
            }
        ]
        spreadsheet.batch_update({"requests": requests})
    except Exception:
        pass  # Cosmetic formatting — never fail the export over it

    return spreadsheet.url
