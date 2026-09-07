"""
Batch Website Auditor (fast mode)
----------------------------------------------------------------------------
Reads a leads CSV (must have a "Website" column — the qualified_leads style
CSV your pipeline produces) and runs the fast, important checks from
website_audit.py on every website in it: AI chatbot/live-chat, WhatsApp
CTA, lead-capture form, phone CTA, appointment booking, trust signals,
SSL validity, and the top issues found — writing one row per lead to a
new CSV, ready to feed into outreach/scoring.

SKIPPED FOR SPEED (this is what was making the batch take hours):
  - Lighthouse (performance/accessibility) is OFF by default. It's by far
    the slowest part (each run spins up a Docker container and takes
    20-90+ seconds, x6 per site). Pass --full-lighthouse to turn it back
    on if you want those scores for a smaller batch.
  - Internal broken-link crawling is always skipped (per your earlier
    choice) — it's the next-slowest check and least useful for a first
    outreach pass.

With both of those off, a typical site takes just a few seconds, so 30
leads should finish in well under 5 minutes instead of hours.

REQUIREMENTS:
  - This file must sit in the SAME FOLDER as website_audit.py.

USAGE:
    python batch_website_audit.py qualified_leads_row1.csv
    python batch_website_audit.py qualified_leads_row1.csv --out results.csv
    python batch_website_audit.py qualified_leads_row1.csv --limit 5   # test run first
    python batch_website_audit.py qualified_leads_row1.csv --resume    # skip already-done sites
    python batch_website_audit.py qualified_leads_row1.csv --full-lighthouse  # slow, full scores

OUTPUT CSV COLUMNS:
    Every column from your input leads CSV, unchanged (e.g. Business Name,
    Category, Address, Phone, Website, Email, Google Maps URL, Rating,
    Review Count, Social Media URLs — whatever your CSV has), PLUS the
    audit columns appended after them:
        audit_status, error,
        health_score, max_health_score,
        opportunity_score, max_opportunity_score,
        ssl_valid, has_chatbot, chatbot_providers, has_whatsapp,
        has_lead_form, has_phone_cta, has_appointment_booking, has_trust_signals,
        has_local_schema, critical_issue_count, high_issue_count,
        top_issues (semicolon-separated "priority: problem"),
        fetch_warning
    Nothing from the input is dropped — downstream tools (e.g. the n8n
    lead-enrichment flow) need the original category/address/phone/email
    columns alongside the audit results.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time

# website_audit.py must be importable — same directory as this script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from website_audit import (  # noqa: E402
    AuditConfig,
    WebsiteAuditor,
    normalize_url,
    PRIORITY_ORDER,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_auditor")

# Appended AFTER whatever columns the input CSV already has (see
# build_output_fields() in main()). Kept as a separate list so audit_one()
# always knows exactly which keys it's responsible for filling in.
AUDIT_FIELDS = [
    "audit_status", "error",
    "health_score", "max_health_score",
    "opportunity_score", "max_opportunity_score",
    "ssl_valid", "has_chatbot", "chatbot_providers", "has_whatsapp",
    "has_lead_form", "has_phone_cta", "has_appointment_booking", "has_trust_signals",
    "has_local_schema", "critical_issue_count", "high_issue_count",
    "top_issues", "fetch_warning",
]


def build_output_fields(input_fieldnames: list[str]) -> list[str]:
    """Original CSV columns (in their original order), followed by any
    audit columns not already present under the same name."""
    return input_fieldnames + [f for f in AUDIT_FIELDS if f not in input_fieldnames]


def _skip_link_crawl(self, soup, raw_html):  # noqa: ANN001 - matches original signature
    """Drop-in replacement for WebsiteAuditor._crawl_internal_links that
    skips crawling internal links entirely (per user's speed choice),
    while keeping the same return shape so scoring code doesn't break."""
    return {
        "checked_count": 0, "broken": [], "broken_count": 0,
        "forbidden": [], "forbidden_count": 0,
        "rate_limited": [], "rate_limited_count": 0, "ignored_count": 0,
    }


WebsiteAuditor._crawl_internal_links = _skip_link_crawl


def read_leads(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def find_website_column(fieldnames: list[str]) -> str:
    for name in fieldnames:
        if name.strip().lower() == "website":
            return name
    raise ValueError(f"No 'Website' column found. Columns present: {fieldnames}")


def find_name_column(fieldnames: list[str]) -> str | None:
    for candidate in ("Business Name", "Name", "business_name"):
        for name in fieldnames:
            if name.strip().lower() == candidate.lower():
                return name
    return None


def count_issues_by_priority(issues) -> dict[str, int]:
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for issue in issues:
        if issue.priority in counts:
            counts[issue.priority] += 1
    return counts


def top_issues_string(issues, n: int = 5) -> str:
    sorted_issues = sorted(issues, key=lambda i: PRIORITY_ORDER.get(i.priority, 9))
    return "; ".join(f"{i.priority}: {i.problem}" for i in sorted_issues[:n])


def audit_one(lead: dict, website_url: str, output_fields: list[str], args) -> dict:
    """Runs the audit for one lead and returns a full output row: every
    original input column (carried through unchanged) plus the audit
    columns filled in."""
    row = {field: "" for field in output_fields}
    row.update(lead)  # carry through every original column as-is

    url = normalize_url(website_url)
    config = AuditConfig(
        url=url,
        lighthouse_runs=args.runs,
        check_desktop=not args.no_desktop,
        skip_lighthouse=not args.full_lighthouse,
        reports_dir=args.reports_dir,
        lighthouse_timeout=args.lighthouse_timeout,
    )

    try:
        result = WebsiteAuditor(config).run()
    except Exception as exc:  # noqa: BLE001 - keep the batch alive on any single failure
        row["audit_status"] = "FAILED"
        row["error"] = str(exc)
        logger.error("  FAILED: %s -> %s", website_url, exc)
        return row

    row["audit_status"] = "OK"
    row["health_score"] = result.health_score
    row["max_health_score"] = result.max_health_score
    row["opportunity_score"] = result.opportunity_score
    row["max_opportunity_score"] = result.max_opportunity_score
    row["fetch_warning"] = result.fetch_warning or ""

    row["ssl_valid"] = result.ssl_info.get("valid")
    row["has_chatbot"] = result.chatbot["detected"]
    row["chatbot_providers"] = ", ".join(result.chatbot.get("providers", []))
    row["has_whatsapp"] = result.whatsapp["detected"]
    row["has_lead_form"] = result.lead_capture_form["detected"]
    row["has_phone_cta"] = result.phone_cta["detected"]
    row["has_appointment_booking"] = result.appointment_booking["detected"]
    row["has_trust_signals"] = result.trust_signals["detected"]
    row["has_local_schema"] = result.schema.get("schema_found")

    counts = count_issues_by_priority(result.issues)
    row["critical_issue_count"] = counts["CRITICAL"]
    row["high_issue_count"] = counts["HIGH"]
    row["top_issues"] = top_issues_string(result.issues)

    return row


def prompt_for_csv_path() -> str | None:
    raw = input("Enter the path to the leads CSV (must have a Website column): ").strip().strip('"').strip("'")
    if not raw:
        print("No path provided.")
        return None
    if not os.path.isfile(raw):
        print(f"File not found: {raw}")
        return None
    return raw


def load_already_done_websites(out_path: str, website_col: str) -> set[str]:
    """Return the set of website URLs already present in an existing output
    CSV, so a resumed run can skip re-auditing them."""
    if not os.path.isfile(out_path):
        return set()
    with open(out_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Output CSV uses the same website column name as the input CSV
        # (e.g. "Website"), falling back to lowercase "website" just in
        # case an older-format output file is being resumed against.
        col = website_col if (reader.fieldnames and website_col in reader.fieldnames) else "website"
        return {
            (row.get(col) or "").strip()
            for row in reader
            if (row.get(col) or "").strip()
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-audit every website in a leads CSV.")
    parser.add_argument("input_csv", nargs="?", default=None,
                         help="Path to the leads CSV (must have a Website column). "
                              "If omitted, you'll be prompted for it.")
    parser.add_argument("--out", default=None, help="Output CSV path (default: <input>_audited.csv)")
    parser.add_argument("--full-lighthouse", action="store_true",
                         help="Also run Lighthouse (performance/accessibility). SLOW — each site "
                              "takes 1-5+ minutes via Docker. Off by default for speed.")
    parser.add_argument("--runs", type=int, default=1,
                         help="Lighthouse runs per form factor, only used with --full-lighthouse (default: 1)")
    parser.add_argument("--no-desktop", action="store_true",
                         help="With --full-lighthouse, skip the desktop pass (mobile only)")
    parser.add_argument("--limit", type=int, default=None, help="Only audit the first N leads (for testing)")
    parser.add_argument("--reports-dir", default=os.path.join(os.getcwd(), "batch_reports"),
                         help="Where per-site Lighthouse JSON/screenshots get written")
    parser.add_argument("--lighthouse-timeout", type=int, default=180,
                         help="Per Lighthouse run timeout in seconds (default: 180)")
    parser.add_argument("--delay", type=float, default=0.3,
                         help="Seconds to pause between sites, be polite (default: 0.3)")
    parser.add_argument("--resume", action="store_true",
                         help="Skip websites that already have a row in the output CSV "
                              "(from a previous run that stopped partway) instead of "
                              "re-auditing everything from scratch.")
    ran_with_no_args = len(sys.argv) == 1
    args = parser.parse_args()

    if args.input_csv is None:
        args.input_csv = prompt_for_csv_path()
        if args.input_csv is None:
            return 1

    out_path = args.out or (os.path.splitext(args.input_csv)[0] + "_audited.csv")

    leads = read_leads(args.input_csv)
    if not leads:
        logger.error("No rows found in %s", args.input_csv)
        return 1

    fieldnames = list(leads[0].keys())
    website_col = find_website_column(fieldnames)
    name_col = find_name_column(fieldnames)
    output_fields = build_output_fields(fieldnames)

    if args.limit:
        leads = leads[: args.limit]

    already_done = load_already_done_websites(out_path, website_col) if args.resume else set()
    if already_done:
        logger.info("Resuming: %d website(s) already in %s will be skipped.",
                     len(already_done), out_path)

    logger.info("Auditing %d website(s) from %s ...", len(leads), args.input_csv)

    # Open the output CSV once, up front, and flush a row after every single
    # site — so a crash/hang/Ctrl+C partway through never loses prior work.
    # In --resume mode we append to the existing file instead of truncating it.
    file_mode = "a" if (args.resume and os.path.isfile(out_path)) else "w"
    write_header = not (file_mode == "a")

    with open(out_path, file_mode, newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=output_fields)
        if write_header:
            writer.writeheader()

        for i, lead in enumerate(leads, start=1):
            website = (lead.get(website_col) or "").strip()
            name = (lead.get(name_col) or "").strip() if name_col else ""

            if website and website in already_done:
                logger.info("[%d/%d] %s -> already audited, skipping (--resume)", i, len(leads), name or "(unnamed)")
                continue

            if not website:
                logger.info("[%d/%d] %s -> no website listed, skipping", i, len(leads), name or "(unnamed)")
                row = {field: "" for field in output_fields}
                row.update(lead)  # carry through original columns even when skipped
                row["audit_status"] = "SKIPPED_NO_WEBSITE"
                writer.writerow(row)
                out_f.flush()
                continue

            logger.info("[%d/%d] Auditing %s (%s) ...", i, len(leads), name or "(unnamed)", website)
            start = time.monotonic()
            row = audit_one(lead, website, output_fields, args)
            elapsed = time.monotonic() - start
            logger.info("  done in %.0fs — status=%s health=%s/100 opportunity=%s/100",
                         elapsed, row["audit_status"], row["health_score"], row["opportunity_score"])
            writer.writerow(row)
            out_f.flush()

            if i < len(leads):
                time.sleep(args.delay)

    logger.info("Done. Results are in %s (written incrementally, so partial results "
                "were already saved even if this run was interrupted).", out_path)

    if ran_with_no_args:
        input("\nPress Enter to close...")

    return 0


if __name__ == "__main__":
    sys.exit(main())