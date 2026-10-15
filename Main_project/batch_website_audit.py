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
    python batch_website_audit.py qualified_leads_row.csv
    python batch_website_audit.py qualified_leads_row.csv --out results.csv
    python batch_website_audit.py qualified_leads_row.csv --limit 5   # test run first
    python batch_website_audit.py qualified_leads_row.csv --resume    # skip already-done leads
    python batch_website_audit.py qualified_leads_row.csv --resume --retry-failed
    python batch_website_audit.py qualified_leads_row.csv --workers 8  # audit 8 sites at once
    python batch_website_audit.py qualified_leads_row.csv --full-lighthouse  # slow, full scores

OUTPUT CSV COLUMNS:
    Every column from your input leads CSV, unchanged (e.g. Business Name,
    Category, Address, Phone, Website, Email, Google Maps URL, Rating,
    Review Count, Social Media URLs — whatever your CSV has), PLUS the
    audit columns appended after them:
        audit_status, error, error_label,
        health_score, max_health_score,
        opportunity_score, max_opportunity_score,
        ssl_valid, has_chatbot, chatbot_providers, has_whatsapp,
        has_lead_form, has_phone_cta, has_appointment_booking, has_trust_signals,
        has_local_schema, critical_issue_count, high_issue_count,
        top_issues (semicolon-separated "priority: problem"),
        fetch_warning,
        emails_found, social_links_found, pitch_angle, lead_priority (0-100), priority_reason,
        review_type (NEW — "Automatic" or "Manual Review Required")
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
from concurrent.futures import ThreadPoolExecutor

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
#
# "review_type" is the NEW column: it flags whether a row's audit result
# came from a clean, real-page fetch ("Automatic") or whether the fetch
# hit a CAPTCHA / anti-bot wall / JS-rendered shell and so the detections
# (chatbot, forms, etc.) may be false negatives that need a human to
# check in a real browser ("Manual Review Required").
AUDIT_FIELDS = [
    "audit_status", "error",
    "health_score", "max_health_score",
    "opportunity_score", "max_opportunity_score",
    "ssl_valid", "has_chatbot", "chatbot_providers", "has_whatsapp",
    "has_lead_form", "has_phone_cta", "has_appointment_booking", "has_trust_signals",
    "has_local_schema", "phone_number", "phone_valid", "critical_issue_count", "high_issue_count",
    "top_issues", "fetch_warning",
    "review_type",
    # --- added in v2 ---
    "error_label", "emails_found", "social_links_found",
    "pitch_angle", "lead_priority", "priority_reason",
]

REVIEW_AUTOMATIC = "Automatic"
REVIEW_MANUAL = "Manual Review Required"
REVIEW_NO_WEBSITE = "N/A - No Website"


def build_output_fields(input_fieldnames: list[str]) -> list[str]:
    """Original CSV columns (in their original order), followed by any
    audit columns not already present under the same name."""
    return input_fieldnames + [f for f in AUDIT_FIELDS if f not in input_fieldnames]


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


def find_phone_region_column(fieldnames: list[str]) -> str | None:
    """Looks for a per-lead ISO region column (e.g. one added by
    run_pipeline.py's `phone_region` column for multi-country batches).
    Returns None if the input CSV doesn't have one -- callers should
    fall back to the single global --phone-region flag in that case."""
    for candidate in ("phone_region", "country_code", "region"):
        for name in fieldnames:
            if name.strip().lower() == candidate:
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


def determine_review_type(audit_status: str, fetch_warning: str | None) -> str:
    """Decide whether this row's results can be trusted as-is ("Automatic")
    or need a human to double check the live site in a browser
    ("Manual Review Required").

    Manual review is required when:
      - the audit failed outright (audit_status != "OK"), or
      - the fetch succeeded but hit a CAPTCHA/anti-bot wall or a
        JS-rendered/interstitial shell (fetch_warning is non-empty) —
        in that case, "no chatbot" / "no lead form" / etc. results may
        simply be things the tool couldn't see, not things that are
        actually missing.
    """
    if audit_status == "SKIPPED_NO_WEBSITE":
        return REVIEW_NO_WEBSITE   # nothing to review: the pitch IS "you need a website"
    if audit_status != "OK":
        return REVIEW_MANUAL
    if fetch_warning and fetch_warning.strip():
        return REVIEW_MANUAL
    return REVIEW_AUTOMATIC


def classify_error(exc_or_text) -> str:
    """Short human label instead of a multi-line traceback in the CSV."""
    t = str(exc_or_text).lower()
    if any(k in t for k in ("nameresolution", "name or service not known", "getaddrinfo",
                             "nodename nor servname", "failed to resolve")):
        return "Domain not found (DNS)"
    if "timed out" in t or "timeout" in t:
        return "Timeout"
    if "certificate" in t or "ssl" in t:
        return "SSL error"
    if "connection refused" in t or "max retries" in t or "connectionerror" in t:
        return "Connection failed"
    if any(code in t for code in ("500", "502", "503", "504")):
        return "Server error (5xx)"
    if "403" in t or "forbidden" in t:
        return "Blocked (403)"
    return "Other error"


def lead_key(lead: dict, website_col: str) -> str:
    """Stable identity for resume. Keyed on Maps URL (unique per business),
    falling back to name+website, so no-website leads are recognised too
    (the old website-only key re-added them on every resume)."""
    maps = (lead.get("Google Maps URL") or "").strip()
    if maps:
        return maps
    name = (lead.get("Business Name") or lead.get("Name") or "").strip().lower()
    return f"{name}|{(lead.get(website_col) or '').strip().lower()}"


def _to_float(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


def pitch_angle(row: dict) -> str:
    """Single best sales hook for this lead."""
    status = row.get("audit_status")
    if status == "SKIPPED_NO_WEBSITE":
        return "No website - pitch a new site"
    if status != "OK":
        return "Site unreachable - verify, then pitch rebuild"
    if str(row.get("ssl_valid")) == "False":
        return "Insecure / no valid SSL"
    if str(row.get("has_whatsapp")) == "False":
        return "No WhatsApp chat button"
    if str(row.get("has_appointment_booking")) == "False":
        return "No online booking / trial sign-up"
    if str(row.get("has_chatbot")) == "False":
        return "No chatbot / live chat"
    if str(row.get("has_lead_form")) == "False":
        return "No lead-capture form"
    return "Optimise: SEO / speed"


def score_priority(row: dict) -> tuple[int, str]:
    """0-100 lead priority: how good a PROSPECT this is (popular business
    with the biggest fixable gap), not how good its website is."""
    rating = _to_float(row.get("Rating"))
    reviews = _to_float(row.get("Review Count"))
    pts, why = 0, []
    # demand / business size (max 40)
    if reviews >= 1000: pts += 25; why.append(f"{int(reviews)} reviews")
    elif reviews >= 300: pts += 18; why.append(f"{int(reviews)} reviews")
    elif reviews >= 100: pts += 10
    elif reviews >= 30: pts += 5
    if rating >= 4.5: pts += 15; why.append(f"{rating} rating")
    elif rating >= 4.0: pts += 8
    # size of the gap (max 45)
    status = row.get("audit_status")
    if status == "SKIPPED_NO_WEBSITE":
        pts += 45; why.append("no website")
    elif status == "OK":
        opp = _to_float(row.get("opportunity_score")); mx = _to_float(row.get("max_opportunity_score"), 100) or 100
        pts += round(35 * (1 - opp / mx))
        if _to_float(row.get("critical_issue_count")) > 0:
            pts += 10; why.append("critical issues")
    else:
        pts += 25; why.append("site down/unreachable")
    # reachability (max 15)
    if (row.get("phone_number") or row.get("Phone") or "").strip(): pts += 8
    if (row.get("Email") or row.get("emails_found") or "").strip(): pts += 7
    # unreliable audit -> slightly discount
    if row.get("review_type") == REVIEW_MANUAL and status == "OK":
        pts -= 5
    return max(0, min(100, pts)), ", ".join(why)


def finalize_row(row: dict) -> dict:
    row["pitch_angle"] = pitch_angle(row)
    score, why = score_priority(row)
    row["lead_priority"], row["priority_reason"] = score, why
    return row


def audit_one(lead: dict, website_url: str, output_fields: list[str], args,
              phone_region_col: str | None = None) -> dict:
    """Runs the audit for one lead and returns a full output row: every
    original input column (carried through unchanged) plus the audit
    columns filled in."""
    row = {field: "" for field in output_fields}
    row.update(lead)  # carry through every original column as-is

    # Per-lead region (e.g. from run_pipeline.py's `phone_region` column,
    # for a batch spanning multiple countries) takes priority over the
    # single global --phone-region flag; falls back to it when the lead
    # has no region of its own or the CSV doesn't have that column at all.
    lead_region = (lead.get(phone_region_col) or "").strip() if phone_region_col else ""
    effective_phone_region = lead_region or args.phone_region

    url = normalize_url(website_url)
    config = AuditConfig(
        url=url,
        lighthouse_runs=args.runs,
        check_desktop=not args.no_desktop,
        skip_lighthouse=not args.full_lighthouse,
        skip_link_crawl=True,   # per earlier speed choice; no monkey-patching needed
        reports_dir=args.reports_dir,
        lighthouse_timeout=args.lighthouse_timeout,
        default_phone_region=effective_phone_region,
    )

    try:
        result = WebsiteAuditor(config).run()
    except Exception as exc:  # noqa: BLE001 - keep the batch alive on any single failure
        row["audit_status"] = "FAILED"
        row["error_label"] = classify_error(exc)
        row["error"] = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        row["review_type"] = determine_review_type("FAILED", None)
        logger.error("  FAILED: %s -> %s", website_url, row["error_label"])
        return finalize_row(row)

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
    row["phone_number"] = result.business_info.get("telephone") or ""
    # None means "phonenumbers isn't installed / no number found to check",
    # not the same as False ("checked and invalid") — keep that distinction
    # in the CSV rather than collapsing it to a blank/0.
    phone_valid = result.business_info.get("telephone_valid")
    row["phone_valid"] = "" if phone_valid is None else phone_valid

    counts = count_issues_by_priority(result.issues)
    row["critical_issue_count"] = counts["CRITICAL"]
    row["high_issue_count"] = counts["HIGH"]
    row["top_issues"] = top_issues_string(result.issues)

    row["review_type"] = determine_review_type(row["audit_status"], row["fetch_warning"])

    emails = result.business_info.get("emails") or []
    socials = result.business_info.get("social_links") or []
    row["emails_found"] = "; ".join(emails)
    row["social_links_found"] = "; ".join(socials)
    # Fill the blank original contact columns instead of leaving them empty
    # (never overwrite something the input already had).
    if emails and not (lead.get("Email") or "").strip() and "Email" in row:
        row["Email"] = emails[0]
    if socials and "Social Media URLs" in row and not (lead.get("Social Media URLs") or "").strip():
        row["Social Media URLs"] = "; ".join(socials)
    if config.url != url:
        note = f"URL fallback used: {config.url}"
        row["fetch_warning"] = f"{row['fetch_warning']} | {note}" if row["fetch_warning"] else note
        # a URL variant is not an unreliable fetch -- keep review_type as computed above
        row["review_type"] = determine_review_type("OK", result.fetch_warning)

    return finalize_row(row)


def prompt_for_csv_path() -> str | None:
    raw = input("Enter the path to the leads CSV (must have a Website column): ").strip().strip('"').strip("'")
    if not raw:
        print("No path provided.")
        return None
    if not os.path.isfile(raw):
        print(f"File not found: {raw}")
        return None
    return raw


def load_already_done(out_path: str, website_col: str, retry_failed: bool) -> set[str]:
    """Keys (see lead_key) of leads already present in an existing output
    CSV. With retry_failed, rows whose audit FAILED are not counted as done."""
    if not os.path.isfile(out_path):
        return set()
    done = set()
    with open(out_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if retry_failed and row.get("audit_status") == "FAILED":
                continue
            done.add(lead_key(row, website_col))
    return done


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
    parser.add_argument("--phone-region", default="IN",
                         help="ISO 3166-1 alpha-2 region used to validate phone numbers that have "
                              "no country code (default: IN). Passed through to AuditConfig.")
    parser.add_argument("--workers", type=int, default=4,
                         help="Sites audited in parallel (default: 4). Use 1 if you hit rate limits "
                              "or Playwright problems.")
    parser.add_argument("--retry-failed", action="store_true",
                         help="With --resume, re-audit rows that previously FAILED.")
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
    phone_region_col = find_phone_region_column(fieldnames)
    output_fields = build_output_fields(fieldnames)

    if phone_region_col:
        logger.info("Found a '%s' column — using each lead's own region for phone "
                     "validation (falling back to --phone-region %s where it's blank).",
                     phone_region_col, args.phone_region)
    else:
        logger.info("No per-lead region column found — validating all phone numbers "
                     "against --phone-region %s.", args.phone_region)

    if args.limit:
        leads = leads[: args.limit]

    already_done = load_already_done(out_path, website_col, args.retry_failed) if args.resume else set()
    if already_done:
        logger.info("Resuming: %d website(s) already in %s will be skipped.",
                     len(already_done), out_path)

    logger.info("Auditing %d website(s) from %s ...", len(leads), args.input_csv)

    manual_review_count = 0
    appending = args.resume and os.path.isfile(out_path)
    # utf-8-sig so Excel opens non-ASCII names correctly (BOM only on new files)
    with open(out_path, "a" if appending else "w", newline="",
              encoding="utf-8" if appending else "utf-8-sig") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=output_fields)
        if not appending:
            writer.writeheader()

        total = len(leads)
        pending = []   # (index, lead, website, name) needing a network audit
        ready = {}     # index -> finished row (skips)
        for i, lead in enumerate(leads, start=1):
            website = (lead.get(website_col) or "").strip()
            name = (lead.get(name_col) or "").strip() if name_col else ""
            if lead_key(lead, website_col) in already_done:
                logger.info("[%d/%d] %s -> already done, skipping (--resume)", i, total, name or "(unnamed)")
                continue
            if not website:
                row = {field: "" for field in output_fields}
                row.update(lead)
                row["audit_status"] = "SKIPPED_NO_WEBSITE"
                row["review_type"] = determine_review_type("SKIPPED_NO_WEBSITE", None)
                ready[i] = finalize_row(row)
            else:
                pending.append((i, lead, website, name))

        def work(item):
            i, lead, website, name = item
            t0 = time.monotonic()
            row = audit_one(lead, website, output_fields, args, phone_region_col)
            time.sleep(args.delay)
            return i, name, website, row, time.monotonic() - t0

        # Rows are written in the ORIGINAL input order as soon as every
        # earlier row is finished, so a crash still leaves a clean prefix.
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {item[0]: pool.submit(work, item) for item in pending}
            for i in range(1, total + 1):
                if i in ready:
                    row = ready[i]
                    logger.info("[%d/%d] no website listed -> SKIPPED", i, total)
                elif i in futures:
                    _, name, website, row, elapsed = futures[i].result()
                    logger.info("[%d/%d] %s (%s): %s health=%s opp=%s priority=%s (%.0fs)",
                                i, total, name or "(unnamed)", website, row["audit_status"],
                                row["health_score"], row["opportunity_score"], row["lead_priority"], elapsed)
                else:
                    continue
                if row.get("review_type") == REVIEW_MANUAL:
                    manual_review_count += 1
                writer.writerow(row)
                out_f.flush()

    logger.info("Done. Results are in %s (written incrementally, so partial results "
                "were already saved even if this run was interrupted).", out_path)
    logger.info("%d of %d lead(s) are flagged '%s' — verify those sites manually in a "
                "real browser before using their results for scoring/outreach.",
                manual_review_count, len(leads), REVIEW_MANUAL)

    if ran_with_no_args:
        input("\nPress Enter to close...")

    return 0


if __name__ == "__main__":
    sys.exit(main())