"""
Lead Outreach Automation
-------------------------
Replicates the n8n workflow in pure Python:

1. Reads leads from an input CSV.
2. Skips leads whose Phone already exists in the permanent leads CSV (dedupe).
3. Assigns each new lead the next incremental ID.
4. Appends new leads to the permanent leads CSV.
5. Validates each lead's email (regex).
6. If the email is valid -> sends the outreach email via Brevo API.
7. Only logs to the outreach status CSV if the email was valid AND the send succeeded.
   (Leads with missing/invalid emails, or failed sends, are NOT added to outreach status.)
8. Skips a lead in outreach status logging if that Business_Name already has a row there
   (avoids duplicate status entries on repeated runs).

Usage:
    python lead_outreach.py

You will be prompted for the input CSV path when the script runs.
"""

import csv
import os
import re
import sys
import requests

# ============================================================
# CONFIG - fill these in
# ============================================================

BREVO_API_KEY = ""          # <-- put your Brevo API key here
SENDER_EMAIL = "sanjaygh19052001@gmail.com"
SENDER_NAME = "Sanjay"

PERMANENT_LEADS_CSV = r"F:\AI automation\AI automation\Python_Lead_Generation - Copy\Python_Lead_Generation\Main_project\out_reach details\permanent_leads.csv"
OUTREACH_STATUS_CSV = r"F:\AI automation\AI automation\Python_Lead_Generation - Copy\Python_Lead_Generation\Main_project\out_reach details\outreach_status.csv"

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# Columns expected in the input CSV (must match these headers)
INPUT_COLUMNS = [
    "Business_Name", "Website", "Phone", "Email",
    "Priority", "Top_Problem", "Evidence",
    "Recommended_Pitch", "Outreach_Message"
]

# Columns for the permanent leads CSV
PERMANENT_COLUMNS = ["i_d"] + INPUT_COLUMNS

# Columns for the outreach status CSV
STATUS_COLUMNS = ["Business_Name", "Email", "Status"]


# ============================================================
# HELPERS
# ============================================================

def normalize_phone(phone):
    """Strip spaces, dashes, and parentheses from a phone number for comparison."""
    if not phone:
        return ""
    return re.sub(r"[\s\-()]", "", str(phone)).strip()


def is_valid_email(email):
    """Return True if the email is non-empty and matches a standard email pattern."""
    if not email:
        return False
    return bool(EMAIL_REGEX.match(email.strip()))


def read_csv_as_dicts(path):
    """
    Read a CSV file into a list of dicts, with header names normalized so that
    minor differences (case, extra spaces, BOM from Excel) don't break lookups.
    Returns [] if the file doesn't exist.
    """
    if not os.path.exists(path):
        return []
    # utf-8-sig strips a leading BOM if the file was saved from Excel
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            # Build a map from normalized header -> original header
            normalized_map = {}
            for name in reader.fieldnames:
                if name is None:
                    continue
                norm = name.strip().lower().replace(" ", "_")
                normalized_map[norm] = name

            rows = []
            for row in reader:
                clean_row = {}
                for norm, original in normalized_map.items():
                    clean_row[norm] = (row.get(original) or "").strip()
                rows.append(clean_row)
            return rows
        return []


def write_csv_header_if_missing(path, columns):
    """Create the CSV (and its parent folder, if needed) with a header row if it doesn't exist."""
    folder = os.path.dirname(path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()


def append_row(path, columns, row_dict):
    """Append a single row dict to a CSV, writing header first if needed."""
    write_csv_header_if_missing(path, columns)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writerow({col: row_dict.get(col, "") for col in columns})


def get_next_id(permanent_rows):
    """Find the highest existing i_d in the permanent leads and return the next one."""
    max_id = 0
    for row in permanent_rows:
        try:
            val = int(row.get("i_d", 0))
            if val > max_id:
                max_id = val
        except (ValueError, TypeError):
            continue
    return max_id + 1


def send_outreach_email(to_email, business_name, outreach_message):
    """
    Send an email via Brevo's transactional email API.
    Returns True on success (2xx response), False otherwise.
    """
    html_content = (outreach_message or "").strip().replace("\n", "<br>")
    if not html_content:
        print(f"    [!] No Outreach_Message text found for {business_name} -> skipping send.")
        return False

    payload = {
        "sender": {"email": SENDER_EMAIL, "name": SENDER_NAME},
        "to": [{"email": to_email}],
        "subject": f"Quick note about {business_name}'s website" if business_name else "Quick note about your website",
        "htmlContent": html_content
    }
    headers = {
        "api-key": BREVO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        response = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=20)
        if response.status_code in (200, 201, 202):
            return True
        else:
            print(f"    [!] Brevo send failed for {business_name} "
                  f"({response.status_code}): {response.text[:200]}")
            return False
    except requests.RequestException as e:
        print(f"    [!] Network error sending to {business_name}: {e}")
        return False


# ============================================================
# MAIN WORKFLOW
# ============================================================

def main():
    input_path = input("Enter path to the input CSV file: ").strip().strip('"')

    if not os.path.exists(input_path):
        print(f"Error: file not found -> {input_path}")
        sys.exit(1)

    if BREVO_API_KEY in ("", "YOUR_BREVO_API_KEY_HERE"):
        print("Error: please set BREVO_API_KEY in the script before running.")
        sys.exit(1)

    # ---- Load existing state ----
    permanent_rows = read_csv_as_dicts(PERMANENT_LEADS_CSV)
    status_rows = read_csv_as_dicts(OUTREACH_STATUS_CSV)

    existing_phones = {normalize_phone(r.get("phone")) for r in permanent_rows if r.get("phone")}
    existing_status_names = {r.get("business_name", "").strip().lower() for r in status_rows}

    next_id = get_next_id(permanent_rows)

    # ---- Load input leads ----
    input_rows = read_csv_as_dicts(input_path)
    if not input_rows:
        print("No rows found in input CSV. Nothing to do.")
        return

    print(f"Loaded {len(input_rows)} lead(s) from input CSV.")
    print(f"Detected input columns: {list(input_rows[0].keys())}\n")

    added_to_permanent = 0
    skipped_duplicate_phone = 0
    emails_sent = 0
    invalid_or_missing_email = 0
    send_failures = 0
    skipped_duplicate_status = 0

    for row in input_rows:
        business_name = (row.get("business_name") or "").strip()
        phone = row.get("phone") or ""
        email = (row.get("email") or "").strip()

        norm_phone = normalize_phone(phone)

        # ---- 1. Duplicate check against permanent leads (by phone) ----
        if norm_phone and norm_phone in existing_phones:
            print(f"[SKIP] Duplicate phone, already in permanent leads: {business_name}")
            skipped_duplicate_phone += 1
            continue

        # ---- 2. Append to permanent leads CSV ----
        permanent_row = {
            "Business_Name": business_name,
            "Website": row.get("website", ""),
            "Phone": phone,
            "Email": email,
            "Priority": row.get("priority", ""),
            "Top_Problem": row.get("top_problem", ""),
            "Evidence": row.get("evidence", ""),
            "Recommended_Pitch": row.get("recommended_pitch", ""),
            "Outreach_Message": row.get("outreach_message", ""),
            "i_d": next_id
        }
        append_row(PERMANENT_LEADS_CSV, PERMANENT_COLUMNS, permanent_row)

        if norm_phone:
            existing_phones.add(norm_phone)
        next_id += 1
        added_to_permanent += 1
        print(f"[ADDED] {business_name} -> permanent_leads.csv (id={permanent_row['i_d']})")

        # ---- 3. Validate email ----
        if not is_valid_email(email):
            print(f"    [-] Invalid/missing email for {business_name} -> not sent, not logged.")
            invalid_or_missing_email += 1
            continue

        # ---- 4. Avoid duplicate outreach-status logging ----
        if business_name.lower() in existing_status_names:
            print(f"    [-] {business_name} already has an outreach status entry, skipping send.")
            skipped_duplicate_status += 1
            continue

        # ---- 5. Send email ----
        outreach_message = row.get("outreach_message", "")
        success = send_outreach_email(email, business_name, outreach_message)

        # ---- 6. Log to outreach status CSV only if sent successfully ----
        if success:
            append_row(OUTREACH_STATUS_CSV, STATUS_COLUMNS,
                       {"Business_Name": business_name, "Email": email, "Status": "Not Replied"})
            existing_status_names.add(business_name.lower())
            emails_sent += 1
            print(f"    [+] Email sent to {email}, logged as 'Not Replied'.")
        else:
            send_failures += 1
            print(f"    [!] Email send failed for {business_name} -> not logged.")

    # ---- Summary ----
    print("\n----- SUMMARY -----")
    print(f"Total input rows:              {len(input_rows)}")
    print(f"Added to permanent leads:      {added_to_permanent}")
    print(f"Skipped (duplicate phone):     {skipped_duplicate_phone}")
    print(f"Emails sent & logged:          {emails_sent}")
    print(f"Invalid/missing email:         {invalid_or_missing_email}")
    print(f"Skipped (already in status):   {skipped_duplicate_status}")
    print(f"Send failures:                 {send_failures}")


if __name__ == "__main__":
    main()