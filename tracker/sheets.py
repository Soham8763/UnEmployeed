"""
tracker/sheets.py — Google Sheets read/write for JobHunter.

Uses gspread with service account credentials.
The Sheet must be shared with the service account email address.

Functions:
  init_sheet()                 → Returns worksheet object
  append_row(data_dict)        → Appends a new application row
  update_status(row, status)   → Updates the Status column of a given row
"""

import gspread
from google.oauth2.service_account import Credentials
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Scopes required for Google Sheets access
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column order in the Google Sheet (must match header row)
COLUMNS = [
    "Date",
    "Platform",
    "Company",
    "Role",
    "Location",
    "Remote",
    "Salary",
    "Team Size",
    "Score",
    "Reasoning",
    "Missing Keywords",
    "ATS Keywords",
    "Resume Version",
    "Status",
    "Applied At",
    "Follow-up Sent",
    "URL",
    "JD Snippet",
]


def init_sheet(
    credentials_path: str,
    sheet_id: str,
    worksheet_name: str = "Applications",
) -> gspread.Worksheet:
    """
    Authenticate and return the target worksheet.

    If the worksheet doesn't exist, it creates one with the correct headers.
    The Google Sheet must be shared with the service account email in
    sheets_credentials.json → client_email.
    """
    creds = Credentials.from_service_account_file(credentials_path, scopes=_SCOPES)
    client = gspread.authorize(creds)

    spreadsheet = client.open_by_key(sheet_id)

    # Get or create worksheet
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name, rows=1000, cols=len(COLUMNS)
        )
        # Write header row
        worksheet.update("A1", [COLUMNS])
        logger.info(f"Created worksheet '{worksheet_name}' with headers.")

    # Verify headers exist (in case sheet is empty)
    first_row = worksheet.row_values(1)
    if not first_row:
        worksheet.update("A1", [COLUMNS])
        logger.info("Added header row to existing worksheet.")

    return worksheet


def append_row(worksheet: gspread.Worksheet, data: dict) -> int:
    """
    Append a new row to the sheet from a data dictionary.

    Keys in `data` should match COLUMNS names. Missing keys default to "".
    Returns the row number of the appended row.
    """
    row_values = [str(data.get(col, "")) for col in COLUMNS]
    worksheet.append_row(row_values, value_input_option="USER_ENTERED")
    # Return the row number (header is row 1, so new row = total rows)
    row_count = len(worksheet.get_all_values())
    logger.info(f"Appended row {row_count}: {data.get('Company', '?')} — {data.get('Role', '?')}")
    return row_count


def update_status(
    worksheet: gspread.Worksheet,
    row_index: int,
    status: str,
) -> None:
    """
    Update the Status column of a specific row.

    row_index is 1-based (row 1 = header, row 2 = first data row).
    """
    status_col = COLUMNS.index("Status") + 1  # gspread is 1-indexed
    worksheet.update_cell(row_index, status_col, status)
    logger.info(f"Updated row {row_index} status → '{status}'")


def find_row_by_url(worksheet: gspread.Worksheet, url: str) -> Optional[int]:
    """
    Find a row by its URL column value.

    Returns 1-based row index, or None if not found.
    """
    url_col = COLUMNS.index("URL") + 1
    try:
        cell = worksheet.find(url, in_column=url_col)
        return cell.row if cell else None
    except gspread.exceptions.CellNotFound:
        return None
