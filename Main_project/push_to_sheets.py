"""
push_to_sheets.py (v2) - send shortlisted leads to a call-friendly Google Sheet
==============================================================================
Reads shortlisted_lead_details.csv (made by lead_pipeline.py) and ADDS the leads to
a Google Sheet that is laid out for calling / follow-up:

  * dark header row, frozen header + business name, filter buttons on every column
  * colour-coded DROPDOWN status columns (like your reference sheet):
        Priority | Called | Picked Up | Call Status | Email Sent | WhatsApp Sent
  * Next Follow-Up date picker, Notes, Date Added
  * clickable Website links, best leads (HOT) first

NO DUPLICATES
  A lead is skipped if ANY of these already exists in the sheet:
  the website (domain), the phone number, or the email address.
  Duplicate phone/website cells are also highlighted in the sheet, in case you paste
  a lead in by hand. Your notes and statuses are never overwritten.

If your sheet still has the OLD layout, the script offers to convert it to the new
layout. Nothing is lost: old columns that don't exist in the new layout are kept at
the far right.

ONE-TIME SETUP: Google Cloud project + Sheets API + Drive API + service account JSON key,
then share the sheet with the service account email as EDITOR.
    pip install gspread google-auth pandas python-dotenv

Usage (PyCharm: just run it, then paste the paths in the Run window):
    python push_to_sheets.py
    python push_to_sheets.py --dry-run     # preview only, changes nothing
    python push_to_sheets.py --format      # re-apply the look (widths, colours, dropdowns)
    python push_to_sheets.py --yes         # don't ask before converting an old layout
"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================ SETTINGS - EDIT HERE ============================
# 1) PASTE YOUR GOOGLE SHEET LINK between the quotes below (the full URL from the
#    browser address bar). If you leave it empty, the script asks for it when you run.
SHEET_URL = ""

# 2) Name of the tab inside the sheet where leads are added (created if missing).
WORKSHEET_NAME = "Leads"

# (The path to your service_account.json key and the CSV are asked when you RUN
#  the script - just paste them in the Run window.)
# ==============================================================================

SHORTLIST_FILENAME = "shortlisted_lead_details.csv"
FORMAT_ROWS = 2000  # dropdowns / colours are applied down to this row

# Column order in the sheet. The first 9 come from the pipeline CSV; the rest are for you.
DESIRED_HEADER = [
    "Business Name", "Phone", "Email", "Website", "Priority", "Top Problem",
    "Recommended Pitch", "Outreach Message", "Evidence",
    "Called", "Picked Up", "Call Status", "Email Sent", "WhatsApp Sent",
    "Next Follow-Up", "Notes", "Date Added",
]

# Colours: (background, text) as hex.
GREEN = ("#B6D7A8", "#274E13")
RED = ("#F4CCCC", "#990000")
YELLOW = ("#FFF2CC", "#7F6000")
BLUE = ("#CFE2F3", "#0B5394")
PURPLE = ("#D9D2E9", "#351C75")
ORANGE = ("#F9CB9C", "#B45F06")
GRAY = ("#D9D9D9", "#434343")
TEAL = ("#B7E1CD", "#0B5345")

# Dropdown columns: value -> colour. Order here = order in the dropdown.
DROPDOWNS = {
    "Priority": {"HOT": RED, "WARM": YELLOW, "COLD": BLUE},
    "Called": {"CALLED": GREEN, "NOT CALLED": GRAY},
    "Picked Up": {"ANSWER": GREEN, "NO ANSWER": RED, "VOICEMAIL": YELLOW, "WRONG NUMBER": GRAY},
    "Call Status": {"BOOKED": GREEN, "INTERESTED": TEAL, "CALL BACK": YELLOW,
                    "NOT INTERESTED": ORANGE, "DQ": PURPLE, "NO ANSWER": BLUE},
    "Email Sent": {"SENT": BLUE, "REPLIED": GREEN, "BOUNCED": RED},
    "WhatsApp Sent": {"SENT": BLUE, "REPLIED": GREEN},
}
DATE_COLUMNS = ["Next Follow-Up", "Date Added"]
CENTER_COLUMNS = list(DROPDOWNS) + DATE_COLUMNS

COLUMN_WIDTHS = {
    "Business Name": 240, "Phone": 130, "Email": 220, "Website": 230, "Priority": 90,
    "Top Problem": 230, "Recommended Pitch": 150, "Outreach Message": 380, "Evidence": 280,
    "Called": 110, "Picked Up": 130, "Call Status": 150, "Email Sent": 120,
    "WhatsApp Sent": 135, "Next Follow-Up": 130, "Notes": 280, "Date Added": 115,
}
PRIORITY_RANK = {"HOT": 0, "WARM": 1, "COLD": 2}
HEADER_BG = "#0B2A3C"


# ---------------------------------------------------------------- duplicate logic
def norm_domain(url):
    u = (url or "").strip().lower()
    if u.startswith("=hyperlink"):  # a cell we wrote earlier, read back as formula text
        return ""
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith("www."):
        u = u[4:]
    return u.split("/")[0].split("?")[0].strip()


def phone_key(phone):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-8:] if len(digits) >= 7 else ""


def row_keys(website, phone, email, name):
    """Identity tokens for a lead. Two leads are duplicates if they share ANY token."""
    keys = set()
    d = norm_domain(website)
    if d:
        keys.add("w:" + d)
    p = phone_key(phone)
    if p:
        keys.add("p:" + p)
    e = (email or "").strip().lower()
    if e and "@" in e:
        keys.add("e:" + e)
    if not keys and (name or "").strip():
        keys.add("n:" + name.strip().lower())
    return keys


# ---------------------------------------------------------------- cell helpers
def safe_text(v):
    """Stops text from being read as a formula (=, +, -, @) when USER_ENTERED."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


def finalize_cell(col, value):
    value = "" if value is None else str(value)
    if col == "Website" and value.lower().startswith(("http://", "https://")):
        url = value.strip().replace('"', '""')
        return f'=HYPERLINK("{url}","{url}")'
    return safe_text(value)


# ---------------------------------------------------------------- table planning (pure logic)
def plan_table(existing_values, csv_df):
    """Given the sheet's current values (first row = header) and the CSV, decide the final
    header, the rows to write, and what changed. No network access here.
    Returns dict: header, existing_rows, new_rows, skipped, migrated, is_new_sheet."""
    old_header = [h for h in (existing_values[0] if existing_values else [])]
    body = existing_values[1:] if existing_values else []
    is_new_sheet = not any(h.strip() for h in old_header)

    migrated = False
    if is_new_sheet:
        header, existing_rows = list(DESIRED_HEADER), []
    elif old_header[:len(DESIRED_HEADER)] == DESIRED_HEADER:
        header = list(old_header)
        existing_rows = [list(r) + [""] * (len(header) - len(r)) for r in body]
    else:
        migrated = True
        extras = [h for h in old_header if h.strip() and h not in DESIRED_HEADER]
        header = list(DESIRED_HEADER) + extras
        old_idx = {}
        for i, h in enumerate(old_header):
            old_idx.setdefault(h, i)
        existing_rows = []
        for r in body:
            r = list(r) + [""] * (len(old_header) - len(r))
            existing_rows.append([r[old_idx[h]] if h in old_idx else "" for h in header])

    idx = {h: i for i, h in enumerate(header)}

    def cell(row, col):
        return row[idx[col]] if col in idx and idx[col] < len(row) else ""

    seen = set()
    for row in existing_rows:
        seen |= row_keys(cell(row, "Website"), cell(row, "Phone"), cell(row, "Email"), cell(row, "Business Name"))

    csv_rows = csv_df.to_dict(orient="records")
    csv_rows.sort(key=lambda r: PRIORITY_RANK.get((r.get("Priority") or "").strip().upper(), 3))

    new_rows, skipped = [], 0
    today = date.today().isoformat()
    for r in csv_rows:
        keys = row_keys(r.get("Website"), r.get("Phone"), r.get("Email"), r.get("Business Name"))
        if keys & seen:
            skipped += 1
            continue
        seen |= keys
        out = [""] * len(header)
        for col in csv_df.columns:
            if col in idx:
                out[idx[col]] = r[col]
        out[idx["Date Added"]] = today
        new_rows.append(out)

    return {"header": header, "existing_rows": existing_rows, "new_rows": new_rows,
            "skipped": skipped, "migrated": migrated, "is_new_sheet": is_new_sheet}


def to_sheet_rows(header, rows):
    return [[finalize_cell(header[i], v) for i, v in enumerate(row)] for row in rows]


# ---------------------------------------------------------------- formatting (Sheets API requests)
def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _col_letter(i):
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def build_format_requests(sheet_id, header, end_row, existing_rule_count):
    """All the Sheets API requests that style the tab. Safe to run again and again."""
    ncols = len(header)
    idx = {h: i for i, h in enumerate(header)}

    def rng(c0, c1, r0=1, r1=None):
        return {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1 if r1 is not None else end_row,
                "startColumnIndex": c0, "endColumnIndex": c1}

    reqs = []

    # start clean: remove old colour rules so re-running never stacks duplicates
    for _ in range(existing_rule_count):
        reqs.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}})

    # freeze header row + first column
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}})

    # body: vertically centred, single line (click a cell to read the full text)
    reqs.append({"repeatCell": {
        "range": rng(0, ncols),
        "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP",
                                        "textFormat": {"fontSize": 10}}},
        "fields": "userEnteredFormat(verticalAlignment,wrapStrategy,textFormat)"}})

    # header: dark background, white bold centred text
    reqs.append({"repeatCell": {
        "range": rng(0, ncols, 0, 1),
        "cell": {"userEnteredFormat": {
            "backgroundColor": _rgb(HEADER_BG), "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP",
            "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": _rgb("#FFFFFF")}}},
        "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)"}})

    # row heights
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 42}, "fields": "pixelSize"}})
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 1, "endIndex": end_row},
        "properties": {"pixelSize": 30}, "fields": "pixelSize"}})

    # column widths
    for name, i in idx.items():
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": COLUMN_WIDTHS.get(name, 130)}, "fields": "pixelSize"}})

    # centre the status / date columns
    for name in CENTER_COLUMNS:
        if name in idx:
            i = idx[name]
            reqs.append({"repeatCell": {
                "range": rng(i, i + 1),
                "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                "fields": "userEnteredFormat.horizontalAlignment"}})

    # date columns: date picker + tidy format
    for name in DATE_COLUMNS:
        if name in idx:
            i = idx[name]
            reqs.append({"setDataValidation": {
                "range": rng(i, i + 1),
                "rule": {"condition": {"type": "DATE_IS_VALID"}, "showCustomUi": True, "strict": False}}})
            reqs.append({"repeatCell": {
                "range": rng(i, i + 1),
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd mmm yyyy"}}},
                "fields": "userEnteredFormat.numberFormat"}})

    # dropdown columns: list validation + colour rules
    for name, options in DROPDOWNS.items():
        if name not in idx:
            continue
        i = idx[name]
        reqs.append({"setDataValidation": {
            "range": rng(i, i + 1),
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": v} for v in options]},
                     "showCustomUi": True, "strict": False}}})
        for value, (bg, fg) in options.items():
            reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [rng(i, i + 1)],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
                    "format": {"backgroundColor": _rgb(bg),
                               "textFormat": {"foregroundColor": _rgb(fg), "bold": True}}}}}})

    # highlight duplicate phone / website cells (e.g. if you paste a lead in by hand)
    for name in ("Phone", "Website"):
        if name in idx:
            i = idx[name]
            L = _col_letter(i)
            reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [rng(i, i + 1)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": f'=AND({L}2<>"",COUNTIF({L}$2:{L},{L}2)>1)'}]},
                    "format": {"backgroundColor": _rgb("#FFD966"),
                               "textFormat": {"foregroundColor": _rgb("#7F6000"), "bold": True}}}}}})

    # filter buttons on the header row
    reqs.append({"setBasicFilter": {"filter": {"range": {
        "sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": ncols}}}})
    return reqs


def apply_formatting(sh, ws, header):
    end_row = FORMAT_ROWS
    if ws.row_count < end_row:
        ws.add_rows(end_row - ws.row_count)
    if ws.col_count < len(header):
        ws.add_cols(len(header) - ws.col_count)
    rule_count = 0
    for s in sh.fetch_sheet_metadata().get("sheets", []):
        if s["properties"]["sheetId"] == ws.id:
            rule_count = len(s.get("conditionalFormats", []))
    sh.batch_update({"requests": build_format_requests(ws.id, header, end_row, rule_count)})


# ---------------------------------------------------------------- Google connection
def _service_email(credentials_file):
    try:
        import json
        with open(credentials_file, encoding="utf-8") as f:
            return json.load(f).get("client_email", "the service account")
    except Exception:
        return "the service account"


def _permission_message(credentials_file, err):
    email = _service_email(credentials_file)
    return (f"ERROR: Google refused to write to the sheet ({err}).\n"
            f"The service account can see the sheet but is NOT an Editor.\n"
            f"Fix: open the sheet > Share > find {email} > set the role to EDITOR "
            f"(not Viewer) > Save. Then run this script again.")


def connect(credentials_file, sheet_id, worksheet_name):
    import gspread
    from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

    if not os.path.exists(credentials_file):
        sys.exit(f"ERROR: credentials file not found: {credentials_file}")
    try:
        gc = gspread.service_account(filename=credentials_file)
    except Exception as e:
        sys.exit(f"ERROR: could not read the credentials file ({e}). "
                 f"Make sure it is the SERVICE ACCOUNT json key, not an OAuth client file.")
    try:
        sh = gc.open_by_url(sheet_id) if sheet_id.startswith("http") else gc.open_by_key(sheet_id)
    except (SpreadsheetNotFound, APIError) as e:
        sys.exit(f"ERROR: cannot open the sheet ({e}).\n"
                 f"Check the link, that the Google Sheets API is enabled, and that the sheet is shared "
                 f"as EDITOR with: {_service_email(credentials_file)}")
    try:
        ws = sh.worksheet(worksheet_name)
    except WorksheetNotFound:
        try:
            ws = sh.add_worksheet(title=worksheet_name, rows=FORMAT_ROWS, cols=len(DESIRED_HEADER) + 3)
            print(f"Created new tab '{worksheet_name}'.")
        except APIError as e:
            sys.exit(_permission_message(credentials_file, e))
    return sh, ws


# ---------------------------------------------------------------- prompts
def clean_path_input(raw):
    raw = (raw or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    return raw.strip()


def prompt_for_csv():
    default = str(Path(os.environ.get("LEAD_OUTPUT_DIR", ".")) / SHORTLIST_FILENAME)
    while True:
        raw = input(f"Paste the path to shortlisted_lead_details.csv\n"
                    f"(or press Enter for default: {default}): ")
        path = clean_path_input(raw) or default
        if os.path.isdir(path):
            path = str(Path(path) / SHORTLIST_FILENAME)
        if not os.path.exists(path):
            print(f"  File not found: {path}\n  Try again.\n")
            continue
        return path


def prompt_for_key():
    while True:
        path = clean_path_input(input("Paste the path to your service_account.json key file: "))
        if not path:
            print("  Please paste the path.")
            continue
        if os.path.isdir(path):
            jsons = sorted(Path(path).glob("*.json"))
            if not jsons:
                print(f"  No .json file found in that folder: {path}\n")
                continue
            path = str(jsons[0])
            print(f"  Using: {path}")
        elif not os.path.exists(path) and os.path.exists(path + ".json"):
            path = path + ".json"
            print(f"  Using: {path}")
        if not os.path.exists(path):
            print(f"  File not found: {path}\n")
            continue
        return path


def prompt_for_sheet():
    while True:
        raw = clean_path_input(input("Paste your Google Sheet URL (or ID): "))
        if raw:
            return raw
        print("  Please paste the sheet URL.")


def ask_yes(question, default=True):
    raw = input(f"{question} {'[Y/n]' if default else '[y/N]'}: ").strip().lower()
    return default if not raw else raw in ("y", "yes")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Add shortlisted leads to a call-friendly Google Sheet")
    ap.add_argument("csv", nargs="?", default=None, help="Path to shortlisted_lead_details.csv")
    ap.add_argument("--sheet-id", default=os.environ.get("GOOGLE_SHEET_ID", "") or SHEET_URL)
    ap.add_argument("--worksheet", default=os.environ.get("GOOGLE_WORKSHEET", "") or WORKSHEET_NAME)
    ap.add_argument("--credentials", default=os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""))
    ap.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing")
    ap.add_argument("--format", action="store_true", help="Re-apply the look (widths, colours, dropdowns)")
    ap.add_argument("--yes", action="store_true", help="Convert an old layout without asking")
    args = ap.parse_args()

    if not args.credentials:
        args.credentials = prompt_for_key()

    csv_path = clean_path_input(args.csv) if args.csv else prompt_for_csv()
    if os.path.isdir(csv_path):
        csv_path = str(Path(csv_path) / SHORTLIST_FILENAME)
    if not os.path.exists(csv_path):
        sys.exit(f"ERROR: CSV not found: {csv_path}")
    if not args.sheet_id:
        args.sheet_id = prompt_for_sheet()

    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    if df.empty:
        sys.exit("The shortlist CSV has no leads - nothing to add.")
    print(f"Read {len(df)} leads from {csv_path}")

    sh, ws = connect(args.credentials, args.sheet_id, args.worksheet)
    plan = plan_table(ws.get_all_values(), df)
    header, new_rows = plan["header"], plan["new_rows"]

    print(f"{len(new_rows)} new, {plan['skipped']} skipped as duplicates "
          f"(same website, phone or email already in the sheet).")

    if plan["migrated"]:
        print("Your sheet uses the OLD layout. It can be converted to the new call-friendly layout "
              "(all existing rows are kept; old extra columns move to the far right).")
        if not args.dry_run and not args.yes and not ask_yes("Convert it now?", True):
            sys.exit("Stopped - nothing was changed.")

    if args.dry_run:
        for r in new_rows[:10]:
            print("  would add:", r[0], "|", r[1])
        print("Dry run - nothing written.")
        return

    from gspread.exceptions import APIError
    try:
        if plan["is_new_sheet"] or plan["migrated"]:
            all_rows = plan["existing_rows"] + new_rows
            ws.clear()
            ws.update(range_name="A1", values=[header] + to_sheet_rows(header, all_rows),
                      value_input_option="USER_ENTERED")
        elif new_rows:
            ws.append_rows(to_sheet_rows(header, new_rows), value_input_option="USER_ENTERED")

        if plan["is_new_sheet"] or plan["migrated"] or args.format:
            apply_formatting(sh, ws, header)
            print("Applied the call-friendly layout (colours, dropdowns, filters).")
    except APIError as e:
        sys.exit(_permission_message(args.credentials, e))

    print(f"Done. Added {len(new_rows)} leads to '{args.worksheet}'.")


if __name__ == "__main__":
    main()