"""
Lead Qualification Pipeline v5 (fully standalone Python — no n8n needed)
=========================================================================
Input:  a CSV like qualified_leads_row_audited.csv (already has
        health_score / opportunity_score / top_issues / etc. from
        your batch_website_audit.py step)
Output: a new CSV with AI qualification, email extraction/verification,
        priority, outreach message, etc. — same shape as the n8n
        "output lead data table".

v5 changes (fixes on top of v4)
-------------------------------
 A. Sellable-gap scope: a lead only counts as QUALIFIED if the audit shows a gap
    this business actually fixes (no AI chatbot/instant replies, no lead form,
    no tap-to-call, no online booking). Technical-only issues (security headers,
    schema, viewport, alt text...) -> REVIEW, and messages never pitch them.
    An out-of-scope-pitch validator (like the security-claim one) enforces it.
 B. Leads with NO website go to REVIEW (no AI call, no website-pitch message).
 C. Blank business names are recovered from the Google Maps URL.
 D. Leads whose website fetch failed are NOT shortlisted (gate = 0, action
    MANUAL_REVIEW). Set STRICT_FETCH_CHECK = False to relax this.
 E. primary_problem and evidence are now built/aligned in code (one clean
    problem + a tidy "Missing: ... | Audit issues: ..." evidence line).
 F. Emails get a free MX/DNS check when no ZeroBounce key is set
    (email_verified = mx_ok / no_mx / mx_unknown).
 G. Startup Groq key check + mid-run abort on 401/403, so a bad key stops the
    run immediately instead of failing every lead.
 H. recommended_action is enforced in code (email -> EMAIL_OUTREACH, else phone ->
    PHONE_OUTREACH ...). AI-failure rows now show only the top issue.

v4 changes (fixes on top of v3)
-------------------------------
 1. Tri-state audit flags: a BLANK ssl_valid / has_chatbot / has_whatsapp /
    has_lead_form / has_phone_cta / has_appointment_booking /
    has_trust_signals / has_local_schema is now None ("unknown"), not False.
    Only an explicit "False" counts as a real gap, so failed/partial audits
    can no longer create fake weaknesses or bypass the security-claim check.
 2. Ollama mode now also generates outreach through Ollama (no hidden Groq
    dependency). All AI calls go through call_llm().
 3. shortlisted_lead_details.csv now requires passed_quality_gate == 1
    AND an outreach message (matches what the comment always claimed).
 4. Fallback problem order fixed: a real invalid-SSL problem is checked
    BEFORE the milder missing-schema one.
 5. Fallback templates rewritten: each problem carries its own full story +
    pitch sentence, so the security-header / schema cases read logically.
    The fallback now also includes the "restate + pitch" beats properly.
 6. Security-claim validator is now regex-based and much broader
    (e.g. "flagged as unsafe", "red warning", "browser marks it...").
    It checks the subject line as well as the message body.
 7. Minor: duplicate `pass` removed, DEFAULT_OUTPUT_DIR can be overridden
    with the LEAD_OUTPUT_DIR env var, safer JSON / retry-after parsing.

Every QUALIFIED lead still gets its own AI-written message about that
specific lead's REAL missing feature, following the fixed 6-beat structure:
  1. notice a specific weakness on THIS business's site
  2. a short "someone visits your site" scenario
  3. they leave / a competitor gets them instead
  4. restate the weakness briefly
  5. pitch the specific fix that solves THAT problem
  6. ask for a 60-second walkthrough

Usage:
    1) Create a .env file next to this script:
           GROQ_API_KEY=gsk_xxxxxxxxxxxx
       (get a free key at https://console.groq.com/keys)

    2) Run:
           python lead_pipeline.py input.csv output.csv

    3) Re-running with the same output.csv resumes where it left off:
           python lead_pipeline.py input.csv output.csv --resume

Optional flags:
    --no-ai              skip the AI qualification step entirely (mechanics-only test)
    --sleep 3.0          seconds between Groq calls (default 6.0, raise if rate-limited)
    --limit 20           only process the first N leads (useful for a quick test run)
    --sqlite path.db     also upsert results into a SQLite DB (in addition to the CSV)
    --resume             skip leads already present in output_csv (matched by input_id)
    --log-file path.log  where to write the run log (default: lead_pipeline.log)

Optional env vars (in .env or exported):
    GROQ_API_KEY         -> required unless AI_PROVIDER=ollama or --no-ai is used
    ZEROBOUNCE_API_KEY   -> enables real email deliverability check
                            (omit it and email_verified will stay "not_checked")
    AI_PROVIDER          -> "groq" (default, cloud, free tier) or "ollama" (local, unlimited)
    OLLAMA_URL           -> default http://localhost:11434/api/chat
    OLLAMA_MODEL         -> default qwen2.5:14b
    LEAD_OUTPUT_DIR      -> default folder for output CSVs

Requires:
    pip install pandas requests tqdm python-dotenv
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote_plus

import pandas as pd
import requests

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):  # fallback no-op progress bar
        return iterable

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    def _load_env_fallback(path=".env"):
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    _load_env_fallback()


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
# NOTE: Never hardcode API keys in the script. Set GROQ_API_KEY in a .env
# file next to this script (or as an environment variable) instead.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
ZEROBOUNCE_API_KEY = os.environ.get("ZEROBOUNCE_API_KEY", "")

GROQ_MODEL_PRIMARY = "openai/gpt-oss-120b"
GROQ_MODEL_FALLBACK = "openai/gpt-oss-20b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

AI_PROVIDER = os.environ.get("AI_PROVIDER", "groq")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")

REQUEST_TIMEOUT = 15
MAX_RETRIES = 5
BASE_BACKOFF = 2.0  # seconds, doubles each retry

# All output CSVs default to this folder unless you type a different path
# when prompted (or pass a different output_csv on the CLI). Override with
# the LEAD_OUTPUT_DIR environment variable if you move machines/folders.
DEFAULT_OUTPUT_DIR = os.environ.get(
    "LEAD_OUTPUT_DIR",
    r"F:\AI automation\AI automation\Python_Lead_Generation - Copy\Python_Lead_Generation\Main_project\Final_Scraped_Data",
)

# Filename for the second, "talk to these leads" CSV — a slimmed-down view
# saved in the same folder as the main output_csv, containing only the
# columns you need to review a lead and reach out.
SHORTLIST_FILENAME = "shortlisted_lead_details.csv"

# Maps the internal field name -> the friendly header you want in the
# shortlist CSV, in the exact column order requested.
SHORTLIST_COLUMNS = [
    ("business_name", "Business Name"),
    ("website", "Website"),
    ("phone", "Phone"),
    ("extracted_email", "Email"),
    ("priority", "Priority"),
    ("primary_problem", "Top Problem"),
    ("evidence", "Evidence"),
    ("recommended_action", "Recommended Pitch"),
    ("outreach_message", "Outreach Message"),
]

DENY_EMAIL_DOMAINS = [
    "example.com", "sentry.io", "wixpress.com", "godaddy.com", "cloudflare.com",
    "w3.org", "schema.org", "gstatic.com", "gravatar.com", "fontawesome.com",
    "google.com", "googleapis.com", "wordpress.org", "wp.com",
    "domain.com", "yourdomain.com", "email.com", "test.com", "yoursite.com",
    "company.com", "mydomain.com",
]

# Full placeholder emails that show up verbatim in template/boilerplate HTML
# (form placeholder text, demo markup) rather than being a real contact.
PLACEHOLDER_EMAILS = {
    "user@domain.com", "your@email.com", "youremail@domain.com", "name@example.com",
    "email@example.com", "someone@example.com", "info@yourdomain.com",
    "test@test.com", "your@domain.com", "email@domain.com", "you@domain.com",
    "example@example.com", "your.email@example.com", "firstname.lastname@example.com",
}

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Audit feature flags that are tri-state (True / False / None=unknown).
TRI_STATE_FLAGS = [
    "ssl_valid", "has_chatbot", "has_whatsapp", "has_lead_form", "has_phone_cta",
    "has_appointment_booking", "has_trust_signals", "has_local_schema",
]

# If True, a lead whose website fetch failed is not shortlisted and is routed
# to MANUAL_REVIEW (verify the site by hand first).
STRICT_FETCH_CHECK = True

# The ONLY problems this consultant sells a fix for (AI + WhatsApp systems).
# A lead is only QUALIFIED when at least one of these is an explicit gap.
SCOPE_GAPS = [
    {"flag": "has_chatbot", "label": "AI chatbot / instant replies",
     "problem": "No AI chatbot or instant-reply system",
     "keywords": ["chatbot", "live-chat", "live chat", "instant repl", "whatsapp"]},
    {"flag": "has_lead_form", "label": "lead-capture form",
     "problem": "No lead-capture form",
     "keywords": ["lead-capture", "lead capture", "lead form", "enquiry form", "contact form"]},
    {"flag": "has_phone_cta", "label": "tap-to-call button",
     "problem": "No clear tap-to-call button",
     "keywords": ["tap-to-call", "phone cta", "call button", "click-to-call", "clickable phone"]},
    {"flag": "has_appointment_booking", "label": "online appointment booking",
     "problem": "No online appointment booking",
     "keywords": ["appointment", "booking"]},
]


class AIAuthError(Exception):
    """Raised when the AI provider rejects the API key (401/403)."""


logger = logging.getLogger("lead_pipeline")


def setup_logging(log_path: str):
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)


def fmt_flag(v):
    """Render a tri-state flag for prompts: True / False / 'unknown'."""
    return "unknown" if v is None else v


# ----------------------------------------------------------------------
# 1. Normalize
# ----------------------------------------------------------------------
def name_from_maps_url(url):
    """Recovers a business name from a Google Maps place URL when the scraped
    name cell is blank (e.g. .../maps/place/NP+Engineering+Pte+Ltd/data=...)."""
    m = re.search(r"/maps/place/([^/@?]+)", url or "")
    if not m:
        return None
    name = unquote_plus(m.group(1)).strip()
    return name or None


def normalize_row(row: dict) -> dict:
    """Maps the raw audited-CSV columns to the lowercase snake_case fields
    every later stage expects (mirrors the n8n 'Normalize Lead Fields' node).

    Audit feature flags are TRI-STATE: "true" -> True, "false" -> False,
    blank/anything else -> None (unknown). Unknown is never treated as a
    real gap downstream."""

    def to_tri(v):
        if isinstance(v, bool):
            return v
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        s = str(v).strip().lower()
        if s in ("true", "1", "yes", "y"):
            return True
        if s in ("false", "0", "no", "n"):
            return False
        return None

    def to_num(v):
        try:
            if v is None or str(v).strip() == "":
                return None
            return float(v)
        except (ValueError, TypeError):
            return None

    website = (row.get("Website") or "").strip()
    # Trust the actual website value first; audit_status is a secondary signal
    # only used when the website field itself is blank.
    has_website = bool(website) and row.get("audit_status") != "SKIPPED_NO_WEBSITE"
    biz_name = (row.get("Business Name") or "").strip() or name_from_maps_url(row.get("Google Maps URL"))

    r = {
        "title": biz_name,
        "business_name": biz_name,
        "category": row.get("Category"),
        "complete_address": row.get("Address"),
        "phone": row.get("Phone"),
        "website": website if has_website else None,
        "email": row.get("Email") or None,
        "google_maps_url": row.get("Google Maps URL"),
        "review_rating": to_num(row.get("Rating")),
        "review_count": to_num(row.get("Review Count")),
        "social_media_urls": row.get("Social Media URLs"),

        "audit_status": row.get("audit_status") or None,
        "audit_error": row.get("error") or None,
        "health_score": to_num(row.get("health_score")),
        "max_health_score": to_num(row.get("max_health_score")),
        "opportunity_score": to_num(row.get("opportunity_score")),
        "max_opportunity_score": to_num(row.get("max_opportunity_score")),
        "chatbot_providers": row.get("chatbot_providers") or None,
        "critical_issue_count": to_num(row.get("critical_issue_count")) or 0,
        "high_issue_count": to_num(row.get("high_issue_count")) or 0,
        "top_issues_raw": row.get("top_issues") or "",
        "fetch_warning": row.get("fetch_warning") or None,
    }
    for flag in TRI_STATE_FLAGS:
        r[flag] = to_tri(row.get(flag))

    r["input_id"] = make_lead_id(r)
    return r


def make_lead_id(r: dict) -> str:
    """Stable, deterministic ID for dedupe/resume — prefers the Google Maps
    URL (usually unique per listing), then domain+phone, then business name."""
    gmaps = (r.get("google_maps_url") or "").strip().lower()
    domain = norm_domain(r.get("website")) or ""
    phone = norm_phone(r.get("phone")) or ""
    name = (r.get("business_name") or "").strip().lower()
    basis = gmaps or (domain + "|" + phone if (domain or phone) else "") or name or "unknown"
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------
# 2. Dedupe within this run (by domain / phone)
# ----------------------------------------------------------------------
def norm_domain(url):
    if not url:
        return None
    d = re.sub(r"^https?://", "", url, flags=re.I)
    d = re.sub(r"^www\.", "", d, flags=re.I).split("/")[0].lower()
    return d or None


def norm_phone(phone):
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    return digits[-10:] if len(digits) >= 7 else None


def dedupe(rows):
    seen_domains, seen_phones, out = set(), set(), []
    for r in rows:
        domain = norm_domain(r["website"])
        phone = norm_phone(r["phone"])
        dup = (domain and domain in seen_domains) or (phone and phone in seen_phones)
        if domain:
            seen_domains.add(domain)
        if phone:
            seen_phones.add(phone)
        if not dup:
            out.append(r)
    return out


# ----------------------------------------------------------------------
# 3. Low-chance gate (skip dead-weight leads before spending any API calls)
# ----------------------------------------------------------------------
def is_low_chance(r):
    no_contact = not r["website"] and not r["phone"] and not r["email"]
    low_opportunity = (
        isinstance(r["opportunity_score"], (int, float))
        and r["opportunity_score"] < 15
        and (r["critical_issue_count"] or 0) == 0
        and (r["high_issue_count"] or 0) == 0
    )
    return no_contact or low_opportunity


def archive_low_chance(r):
    no_contact = not r["website"] and not r["phone"] and not r["email"]
    reason = ("no website, phone, or email found - lead cannot be contacted through any channel"
              if no_contact else
              "opportunity_score is low with no critical/high issues found - no meaningful problem to pitch")
    return {
        **r,
        "primary_problem": None,
        "secondary_problems": [],
        "mistakes_text": r["top_issues_raw"] or "not audited - filtered before analysis",
        "evidence": reason,
        "confidence": "High",
        "priority": "COLD",
        "commercial_opportunity_score": r["opportunity_score"],
        "qualification_status": "NOT_QUALIFIED",
        "contactability_status": "NOT_CONTACTABLE" if no_contact else "PARTIALLY_CONTACTABLE",
        "contact_channels": {
            "email": bool(r["email"]), "phone": bool(r["phone"]),
            "website": bool(r["website"]), "contact_form": False, "other": False,
        },
        "recommended_action": "ARCHIVE",
        "review_reason": reason,
        "passed_quality_gate": 0,
        "outreach_subject": None,
        "outreach_message": None,
        "audit_summary": r["top_issues_raw"] or reason,
        "email_verified": "not_checked",
        "extracted_email": r["email"],
        "fetch_ok": None,
    }


# ----------------------------------------------------------------------
# 4. Website mistake-check (parse top_issues into severity buckets)
# ----------------------------------------------------------------------
SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def parse_issues(raw):
    if not raw:
        return []
    out = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        m = re.match(r"^(CRITICAL|HIGH|MEDIUM|LOW)\s*:\s*(.+)$", entry, re.I)
        if m:
            out.append({"severity": m.group(1).upper(), "text": m.group(2).strip()})
        else:
            out.append({"severity": "MEDIUM", "text": entry})
    return out


def website_mistake_check(r):
    if r["audit_status"] == "SKIPPED_NO_WEBSITE" or not r["website"]:
        mistakes, top_sev = ["no website"], "CRITICAL"
        medium_count = low_count = None
    elif r["audit_status"] and r["audit_status"] != "OK":
        mistakes = [f"website could not be fully audited (status: {r['audit_status']})"]
        top_sev = "UNVERIFIED"
        medium_count = low_count = None
    else:
        parsed = sorted(parse_issues(r["top_issues_raw"]),
                         key=lambda p: SEVERITY_RANK.get(p["severity"], 9))
        mistakes = [f"{p['severity']}: {p['text']}" for p in parsed]
        top_sev = parsed[0]["severity"] if parsed else "NONE"
        medium_count = sum(1 for p in parsed if p["severity"] == "MEDIUM")
        low_count = sum(1 for p in parsed if p["severity"] == "LOW")

    opp = r["opportunity_score"]
    lead_tier = "Unscored"
    if isinstance(opp, (int, float)):
        lead_tier = "Hot" if opp >= 40 else "Warm" if opp >= 15 else "Cold"

    return {
        **r,
        "mistakes_found": mistakes,
        "mistakes_text": "; ".join(mistakes) or "no major issues detected",
        "top_severity": top_sev,
        "medium_issue_count": medium_count,
        "low_issue_count": low_count,
        "lead_tier": lead_tier,
        "fetch_ok": (r["audit_status"] == "OK") if r["audit_status"] else True,
    }


# ----------------------------------------------------------------------
# 5. Email extraction (homepage, then /contact fallback)
# ----------------------------------------------------------------------
def is_junk_email(email: str) -> bool:
    lower = email.lower()
    if lower in PLACEHOLDER_EMAILS:
        return True
    if re.search(r"\.(png|jpg|jpeg|gif|svg|webp)$", lower):
        return True
    if any(lower.endswith("@" + d) for d in DENY_EMAIL_DOMAINS):
        return True

    if "@" not in lower:
        return True
    local, _, domain = lower.partition("@")

    if re.match(r"^[a-f0-9]{16,}$", local):
        return True
    # generic placeholder local-parts on ANY domain, e.g. "user@somegymsite.com"
    if local in ("user", "yourname", "youremail", "firstname.lastname", "your.name", "shared"):
        return True

    # domain looks like a semver / package version string (e.g. "0.22.0-staging.fb")
    # rather than a real registered domain — these come from scraped JS bundle
    # strings, not actual contact addresses.
    if re.match(r"^\d+\.\d+(\.\d+)?", domain):
        return True
    # "staging", "test", "localhost", "internal" subdomains are dev artifacts,
    # not a real business contact email
    if re.search(r"\b(staging|localhost|internal|dev)\b", domain):
        return True
    # reject domains ending in a TLD that isn't a real, deliverable TLD —
    # catches junk like ".fb", ".local", ".test", ".invalid", ".example"
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    if tld in ("fb", "local", "test", "invalid", "example", "internal", "corp"):
        return True
    # domain must have at least one dot and a plausible TLD length (2-24 chars,
    # letters only) — rejects malformed scrape artifacts generally
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,24}$", domain):
        return True

    return False


def extract_emails_from_html(html: str):
    matches = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", html or "")
    uniq = list(dict.fromkeys(m.lower().strip() for m in matches))
    return [e for e in uniq if not is_junk_email(e)]


def fetch_url(url, timeout=REQUEST_TIMEOUT):
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        return resp.status_code, resp.text
    except requests.RequestException as e:
        logger.debug(f"fetch_url failed for {url}: {e}")
        return None, ""


def extract_email_for_lead(r):
    if not r["website"]:
        return {**r, "extracted_email": None, "all_emails_found": [], "fetch_status": None}

    # Don't bother hitting a site the audit already flagged as unreachable —
    # saves an HTTP timeout on a dead site.
    if r.get("audit_status") and r["audit_status"] not in ("OK",):
        return {**r, "extracted_email": None, "all_emails_found": [], "fetch_status": None}

    base = r["website"] if re.match(r"^https?://", r["website"]) else "https://" + r["website"]
    base = base.rstrip("/")

    status, html = fetch_url(base)
    emails = extract_emails_from_html(html) if status and 200 <= status < 300 else []

    if not emails:
        status2, html2 = fetch_url(base + "/contact")
        if status2 and 200 <= status2 < 300:
            emails = extract_emails_from_html(html2)

    return {
        **r,
        "extracted_email": emails[0] if emails else None,
        "all_emails_found": emails,
        "fetch_status": status,
        "fetch_ok": r.get("fetch_ok", True) and bool(status and 200 <= status < 300),
    }


# ----------------------------------------------------------------------
# 6. AI qualification (Groq / Ollama) with retry + backoff
# ----------------------------------------------------------------------
AI_SYSTEM_PROMPT = """You are a senior B2B lead qualification analyst specializing in website
audits, SEO, CRO, technical website issues, and sales opportunity identification.

Return VALID JSON ONLY — one JSON object, no markdown, no code fences, no explanation.

Separate LEAD QUALITY from CONTACTABILITY. A missing email must never by itself
cause qualification_status = NOT_QUALIFIED. Choose exactly one primary_problem
(specific, not vague). Never promise revenue/traffic/customer increases.
A feature value of "unknown" means the audit could not determine it — never treat
"unknown" as missing/false and never build a problem on it.
The input lists "sellable_gaps" — the ONLY problems this consultant can fix (AI instant
replies, lead capture, tap-to-call, online booking). primary_problem MUST be one of the
sellable_gaps. If sellable_gaps is "none", qualification_status must be "REVIEW"
(technical issues like security headers, schema, alt text or viewport tags are NOT
sellable and must never be chosen as primary_problem).
confidence must be "High"/"Medium"/"Low". priority must be "HOT"/"WARM"/"COLD".
qualification_status must be "QUALIFIED"/"REVIEW"/"NOT_QUALIFIED".
contactability_status must be "CONTACTABLE"/"PARTIALLY_CONTACTABLE"/"NOT_CONTACTABLE".
recommended_action: QUALIFIED+email->EMAIL_OUTREACH, QUALIFIED+no email+phone->PHONE_OUTREACH,
QUALIFIED+no email+no phone+website->WEBSITE_ENRICHMENT, REVIEW->MANUAL_REVIEW,
NOT_QUALIFIED->ARCHIVE. passed_quality_gate = 1 iff qualification_status == "QUALIFIED", else 0.
Never invent facts not present in the input. Preserve scores exactly.

Do NOT produce outreach copy in this step — outreach is generated separately.
Set "outreach" to null always.

Output schema:
{
  "business_name": "", "primary_problem": "", "secondary_problems": [],
  "evidence": "", "confidence": "High", "priority": "HOT",
  "commercial_opportunity_score": null,
  "qualification_status": "QUALIFIED",
  "contactability_status": "CONTACTABLE",
  "contact_channels": {"email": false, "phone": false, "website": false, "contact_form": false, "other": false},
  "recommended_action": "EMAIL_OUTREACH",
  "review_reason": "", "passed_quality_gate": 1,
  "outreach": null,
  "audit_summary": ""
}
"""


def sellable_gaps_text(r):
    return ", ".join(r.get("scope_gap_labels") or []) or "none"


def top_issue_text(r):
    """Just the single top audit issue (used instead of dumping the whole list)."""
    first = (r.get("mistakes_text") or "").split(";")[0].strip()
    first = re.sub(r"^(CRITICAL|HIGH|MEDIUM|LOW)\s*:\s*", "", first, flags=re.I)
    return first or "unknown"


def build_evidence(r):
    """Deterministic, tidy evidence line built from real audit data."""
    parts = []
    if r.get("scope_gap_labels"):
        parts.append("Missing: " + ", ".join(r["scope_gap_labels"]))
    sev = []
    if r.get("critical_issue_count"):
        sev.append(f"{int(r['critical_issue_count'])} critical")
    if r.get("high_issue_count"):
        sev.append(f"{int(r['high_issue_count'])} high")
    if sev:
        parts.append("Audit issues: " + ", ".join(sev))
    if r.get("health_score") is not None and r.get("max_health_score"):
        parts.append(f"Health {r['health_score']:g}/{r['max_health_score']:g}")
    if r.get("opportunity_score") is not None and r.get("max_opportunity_score"):
        parts.append(f"Opportunity {r['opportunity_score']:g}/{r['max_opportunity_score']:g}")
    return " | ".join(parts) or "No sellable gap found in audit"


def align_primary_problem(ai_problem, r):
    """Keeps the AI's wording only if it is about a real sellable gap;
    otherwise replaces it with the first real sellable gap."""
    gaps = [g for g in SCOPE_GAPS if r.get(g["flag"]) is False]
    if not gaps:
        return ai_problem
    text = (ai_problem or "").lower()
    for g in gaps:
        if any(k in text for k in g["keywords"]):
            return ai_problem
    return gaps[0]["problem"]


def review_no_website(r):
    """Leads with no website: not part of the AI/WhatsApp offer - park for a
    human decision instead of generating a website-pitch message."""
    has_contact = bool(r.get("phone") or r.get("email"))
    return {
        **r,
        "primary_problem": "No website",
        "secondary_problems": [],
        "evidence": "No website listed - outside the AI/WhatsApp automation offer; needs a custom pitch",
        "confidence": "High",
        "priority": (r.get("lead_tier") or "Cold").upper(),
        "commercial_opportunity_score": r.get("opportunity_score"),
        "qualification_status": "REVIEW",
        "contactability_status": "PARTIALLY_CONTACTABLE" if has_contact else "NOT_CONTACTABLE",
        "contact_channels": {
            "email": bool(r.get("email")), "phone": bool(r.get("phone")),
            "website": False, "contact_form": False, "other": False,
        },
        "recommended_action": "MANUAL_REVIEW",
        "review_reason": "no website - not shortlisted; decide manually if a website pitch is worth it",
        "passed_quality_gate": 0,
        "outreach_subject": None,
        "outreach_message": None,
        "audit_summary": r.get("mistakes_text"),
        "email_verified": "not_checked",
        "extracted_email": r.get("email"),
        "fetch_ok": None,
    }


def build_ai_user_prompt(r):
    return f"""
id: {r['input_id']}
title: {r['title']}
category: {r['category']}
website: {r['website']}
phone: {r['phone']}
email: {r['extracted_email']}
address: {r['complete_address']}
rating: {r['review_rating']}
review_count: {r['review_count']}

audit_status: {r['audit_status']}
health_score: {r['health_score']} / {r['max_health_score']}
opportunity_score: {r['opportunity_score']} / {r['max_opportunity_score']}
critical_issue_count: {r['critical_issue_count']}
high_issue_count: {r['high_issue_count']}
medium_issue_count: {r.get('medium_issue_count')}
low_issue_count: {r.get('low_issue_count')}
verified issues (highest severity first): {r['mistakes_text']}
audit reliability note: {r.get('fetch_warning') or 'none'}

sellable_gaps: {sellable_gaps_text(r)}

feature checklist (unknown = audit could not determine):
ssl_valid: {fmt_flag(r.get('ssl_valid'))}
has_chatbot: {fmt_flag(r.get('has_chatbot'))} (provider: {r['chatbot_providers']})
has_whatsapp: {fmt_flag(r.get('has_whatsapp'))}
has_lead_form: {fmt_flag(r.get('has_lead_form'))}
has_phone_cta: {fmt_flag(r.get('has_phone_cta'))}
has_appointment_booking: {fmt_flag(r.get('has_appointment_booking'))}
has_trust_signals: {fmt_flag(r.get('has_trust_signals'))}
has_local_schema: {fmt_flag(r.get('has_local_schema'))}
"""


def _sleep_with_backoff(attempt, retry_after=None):
    wait = retry_after if retry_after else BASE_BACKOFF * (2 ** attempt)
    logger.warning(f"   retrying in {wait:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
    time.sleep(wait)


def _call_groq_with_system(model, system_prompt, user_prompt, temperature=0.3):
    """Generic Groq caller with retry/backoff — parameterized system prompt
    and temperature so it can serve both qualification (low temp, strict
    JSON) and outreach generation (higher temp, more varied phrasing).
    Non-retryable errors (4xx other than 429) fail immediately."""
    if not GROQ_API_KEY:
        return None, "no GROQ_API_KEY set"

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                },
                timeout=30,
            )
        except requests.RequestException as e:
            logger.warning(f"   groq network error ({model}): {e}")
            if attempt < MAX_RETRIES - 1:
                _sleep_with_backoff(attempt)
                continue
            return None, f"groq network error after retries: {e}"

        if resp.status_code == 200:
            try:
                return resp.json()["choices"][0]["message"]["content"], None
            except (KeyError, IndexError, TypeError, ValueError) as e:
                return None, f"groq returned malformed response: {e}"

        if resp.status_code == 429:
            try:
                retry_after = float(resp.headers.get("retry-after"))
            except (TypeError, ValueError):
                retry_after = None
            if attempt < MAX_RETRIES - 1:
                _sleep_with_backoff(attempt, retry_after)
                continue
            return None, "groq rate-limited after max retries"

        if 500 <= resp.status_code < 600:
            if attempt < MAX_RETRIES - 1:
                _sleep_with_backoff(attempt)
                continue
            return None, f"groq HTTP {resp.status_code} after retries"

        if resp.status_code in (401, 403):
            raise AIAuthError(
                f"Groq rejected the API key (HTTP {resp.status_code}). Create a new key at "
                f"console.groq.com/keys and put it in .env as GROQ_API_KEY=gsk_... "
                f"(no quotes/spaces), then run again."
            )

        # other non-retryable 4xx (bad request, etc.)
        return None, f"groq HTTP {resp.status_code}: {resp.text[:200]}"

    return None, "groq: exhausted retries"


def call_ollama_with_system(model, system_prompt, user_prompt, temperature=0.3):
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {"temperature": temperature},
                "format": "json",
            },
            timeout=120,
        )
        if resp.status_code != 200:
            return None, f"ollama HTTP {resp.status_code}: {resp.text[:200]}"
        content = resp.json()["message"]["content"]
        return content, None
    except requests.exceptions.ConnectionError:
        return None, "could not reach Ollama - is 'ollama serve' running?"
    except Exception as e:
        return None, str(e)


def mask_key(key):
    return (key[:4] + "..." + key[-4:]) if key and len(key) > 10 else "(too short)"


def check_groq_key():
    """One cheap request at startup. Returns (True/False/None, message):
    True = key works, False = key rejected, None = could not tell (network)."""
    try:
        resp = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=15,
        )
    except requests.RequestException as e:
        return None, f"Could not reach Groq to test the key ({e}) - continuing anyway."
    if resp.status_code == 200:
        return True, f"Groq key OK ({mask_key(GROQ_API_KEY)})."
    if resp.status_code in (401, 403):
        return False, (f"Groq rejected the API key {mask_key(GROQ_API_KEY)} (HTTP {resp.status_code}). "
                       f"Create a new key at console.groq.com/keys and put it in a .env file next to "
                       f"this script as GROQ_API_KEY=gsk_... (no quotes/spaces).")
    return None, f"Groq key test returned HTTP {resp.status_code} - continuing anyway."


def call_llm(system_prompt, user_prompt, temperature=0.3):
    """Single entry point for ALL AI calls (qualification AND outreach), so
    AI_PROVIDER is respected everywhere. Groq: primary model, then fallback
    model. Ollama: local model."""
    if AI_PROVIDER == "ollama":
        return call_ollama_with_system(OLLAMA_MODEL, system_prompt, user_prompt, temperature)

    raw, err = _call_groq_with_system(GROQ_MODEL_PRIMARY, system_prompt, user_prompt, temperature)
    if raw is None:
        logger.warning(f"   primary model failed ({err}) - trying fallback model")
        raw, err = _call_groq_with_system(GROQ_MODEL_FALLBACK, system_prompt, user_prompt, temperature)
    return raw, err


def parse_ai_json(text):
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


# ----------------------------------------------------------------------
# 6b. UNIQUE, PROBLEM-AWARE outreach (AI-generated, scenario structure)
# ----------------------------------------------------------------------
# For every QUALIFIED lead, generate_ai_outreach() sends the model that
# lead's real audit fields and asks it to WRITE a message describing THAT
# business's actual weakness while following a fixed 6-beat structure.
# If the AI fails (or fails the hard validator), a local rule-based
# fallback with the same structure fills in, so a qualified lead is never
# left with a blank outreach message.
# ----------------------------------------------------------------------

OUTREACH_SYSTEM_PROMPT = """You are a cold-outreach copywriter for a small AI-automation
consultant who builds simple website + WhatsApp AI systems for local businesses
(instant replies, lead capture, appointment booking, follow-ups).

You will be given ONE lead's real website-audit data. Write ONE outreach message
about THIS business's ACTUAL problem — never a generic message that could apply
to any business. A feature value of "unknown" means the audit could not determine
it: it is NOT a problem and must never be described as one. Only treat a feature
as missing when its value is explicitly false.
The problem you write about MUST be the primary_problem given below, and it must be one
of the sellable_gaps. Mapping: no AI chatbot/instant reply -> "no instant answer after
hours"; no lead form -> "visitors who aren't ready to call can't leave their details";
no phone CTA -> "no easy way to call, especially on mobile"; no appointment booking ->
"can't book outside business hours". Pick exactly ONE problem — do not list several.
Technical issues (security headers, SSL, schema, viewport tags, alt text, title tags,
page speed, SEO) are NOT what this consultant sells: never write the message about them
and never offer to fix them.

CRITICAL — DO NOT INVENT A VISIBLE SYMPTOM A REAL VISITOR WOULD NOT SEE.
Every problem you describe falls into exactly one of these two categories.
You must know which one your chosen problem is in, and write accordingly:

  VISIBLE to an ordinary visitor (describe what they'd literally see/experience):
    - no chatbot / no WhatsApp -> no way to get an instant answer, especially after hours
    - no lead capture form -> nowhere to leave contact details if not ready to call
    - no phone CTA -> no tap-to-call button, have to hunt for a number (esp. on mobile)
    - no appointment booking -> can't book anything outside business hours
    - no trust signals (reviews/certifications shown) -> nothing on the page to reassure
      an unfamiliar visitor they're dealing with a legitimate business
    - actual expired/invalid SSL certificate or non-HTTPS site (ssl_valid explicitly
      false) -> browser DOES show a real "Not Secure" / certificate warning — this is
      the ONLY case where a visible browser security warning is accurate

  NOT VISIBLE to an ordinary visitor — these are backend/technical risks. NEVER
  claim a visitor "sees a warning", "gets a security alert", or "notices" these.
  Instead frame them honestly as a quieter, longer-term risk:
    - missing security headers (CSP, X-Frame-Options, HSTS, etc.) -> frame as: the
      site is more exposed to attacks like clickjacking or data injection than it
      needs to be, and can fail basic security checks some partners/insurers or
      corporate clients run before working with a business — NOT a visible warning
    - missing schema/structured data (has_local_schema=false) -> frame as: the
      business is less likely to show up well in Google's local search results and
      Maps — a lost-opportunity framing, not something a visitor "sees" on the page
    - slow page speed or technical SEO issues with no visible symptom given -> frame
      as: quietly costing search visibility or making visitors bounce before the
      page even finishes loading — only claim "visitor sees X" if X is genuinely
      what a slow/broken page produces (e.g. a blank page, a spinner that never ends)

If you are not sure whether an issue is visible or backend-only, default to the
backend/quiet framing. It is always safer to undersell a problem than to invent a
symptom that isn't real — a false "browser shows a warning" claim is easy for the
business owner (or their web developer) to disprove and destroys your credibility
instantly.

The message MUST follow this exact 6-beat structure, in this order, using plain
short sentences, no jargon, no exclamation points, no emojis:
1. Opening: "Hi there, I was looking at {business_name} and noticed [specific weakness]."
2. A short concrete scenario: someone visits the site at a specific time/context
   looking for their service, and hits that exact friction — using the CORRECT
   visible-vs-backend framing from above.
3. They leave and a competitor gets them instead, because the competitor made it
   easier, faster, or more reassuring — state this plainly, don't oversell it.
4. One sentence restating the specific weakness (brief, not repeated verbatim from step 1).
5. One sentence pitching the specific fix that solves THAT weakness (not a generic
   "AI system" pitch — name what it does: answers instantly / captures the lead /
   lets them book / secures the site / etc., matching the actual problem).
6. Closing question offering a 60-second example for THIS business, using its name.

Rules:
- Total length: 90-140 words.
- Never invent facts not present in the input data.
- Never claim a visitor "sees", "notices", or "gets a warning" for a backend-only
  issue (see the list above) — describe the real, quieter consequence instead.
- Never promise specific revenue, customer counts, or percentage increases.
- Never use the word "revolutionize", "unlock", "game-changer", or similar hype.
- Never mention pricing.
- The only fix you may pitch is an AI + WhatsApp system: instant replies, lead capture, booking, follow-ups. Never offer web design, responsive/viewport fixes, SEO, security, schema or SSL work.
- Use the business name naturally, at most twice.
- Return VALID JSON ONLY, no markdown fences, in this exact schema:
{"subject": "short 4-7 word subject line naming the specific problem", "message": "the full message with \\n between the beats"}
"""


def build_outreach_user_prompt(r: dict) -> str:
    return f"""
business_name: {r.get('business_name') or r.get('title')}
category: {r.get('category')}

(unknown = audit could not determine it; NOT a problem)
ssl_valid: {fmt_flag(r.get('ssl_valid'))}
has_chatbot: {fmt_flag(r.get('has_chatbot'))}
has_whatsapp: {fmt_flag(r.get('has_whatsapp'))}
has_lead_form: {fmt_flag(r.get('has_lead_form'))}
has_phone_cta: {fmt_flag(r.get('has_phone_cta'))}
has_appointment_booking: {fmt_flag(r.get('has_appointment_booking'))}
has_trust_signals: {fmt_flag(r.get('has_trust_signals'))}

top audit issues (highest severity first): {r.get('mistakes_text')}
sellable_gaps (the problem must come from these): {sellable_gaps_text(r)}
primary_problem (write about THIS one): {r.get('primary_problem')}
evidence: {r.get('evidence')}

REMINDER: You may ONLY describe a visitor seeing a browser security warning,
"not secure" label, or missing lock icon if ssl_valid is explicitly False above.
If ssl_valid is True or unknown, do not use ANY of that language for ANY issue,
including missing security headers — headers are invisible to a normal visitor
regardless of ssl_valid.
"""


def parse_outreach_json(text):
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and parsed.get("message"):
            return parsed
    except json.JSONDecodeError:
        pass
    return None


# ----------------------------------------------------------------------
# HARD VALIDATOR — this is what actually guarantees no false claims, not
# the prompt wording (models can still ignore instructions). Any outreach
# text, whether from the AI or the local fallback, gets checked here
# before it's allowed into the pipeline. Regex-based so it catches
# rewordings like "flagged as unsafe", "red warning", "browser marks it".
# ----------------------------------------------------------------------
_FALSE_SECURITY_PATTERNS = [
    r"\bnot secure\b", r"\bisn'?t secure\b", r"\bis not secure\b", r"\bunsecure\b",
    r"\binsecure (connection|site|website|page)\b",
    r"\bunsafe\b", r"\bnot safe\b",
    r"\b(padlock|lock (icon|symbol))\b",
    r"\b(security|browser|certificate|ssl|https|connection) (warning|alert|notice|error|banner)s?\b",
    r"\bwarning (message|banner|screen|page|sign|label|icon)s?\b",
    r"\bred (warning|banner|screen|label|alert)s?\b",
    r"\b(browser|chrome|safari|firefox|edge)\b.{0,50}\b(warn|warns|warning|flag|flags|flagged|marks?|marked|alerts?|blocks?|blocked)\b",
    r"\bflagged?( it)?( as)? (unsafe|insecure|not secure|risky|dangerous|suspicious)\b",
    r"\b(sees?|gets?|receives?|notices?|hits?|is shown|are shown|shown|shows?|displays?)\b.{0,40}\b(warning|alert)s?\b",
    r"\bwarns? (visitors|users|people|customers)\b",
    r"\bconnection is not private\b", r"\bnot private\b",
]
_FALSE_SECURITY_RE = re.compile("|".join(_FALSE_SECURITY_PATTERNS), re.I | re.S)


def message_makes_false_security_claim(message: str, r: dict) -> bool:
    """Returns True if the text claims a visible browser/security warning
    while the lead's real ssl_valid is NOT explicitly False. Only a truly
    invalid/expired SSL certificate produces a real browser warning;
    missing security headers, missing schema, etc. are never visible to an
    ordinary visitor. Unknown (None) ssl_valid is treated as 'not proven
    invalid', so such claims are blocked."""
    if not message:
        return False
    if r.get("ssl_valid") is False:
        return False  # a real cert problem legitimately CAN produce a browser warning
    return bool(_FALSE_SECURITY_RE.search(message))


_OUT_OF_SCOPE_RE = re.compile(
    r"\b(viewport|meta tags?|meta description|responsive design|mobile[- ]friendly (layout|design|site|website)"
    r"|alt text|title tag|security headers?|content security|ssl|https|structured data|schema"
    r"|page speed|seo|redesign|web design|new website|build (you )?a website)\b",
    re.I,
)


def message_pitches_out_of_scope_fix(text: str) -> bool:
    """True if the text talks about / offers work outside the AI + WhatsApp
    offer (viewport tags, security headers, SEO, schema, redesigns...)."""
    return bool(text and _OUT_OF_SCOPE_RE.search(text))


def generate_ai_outreach(r: dict) -> dict:
    """Asks the AI to write a unique, structured outreach message for this
    specific lead, then runs subject + body through the hard validator.
    If the AI output fails to parse, fails the check, or the API fails,
    falls back to the local rule-based template, which is built to never
    make a false visible-symptom claim."""
    prompt = build_outreach_user_prompt(r)

    raw, err = call_llm(OUTREACH_SYSTEM_PROMPT, prompt, temperature=0.6)
    parsed = parse_outreach_json(raw) if raw else None

    if not parsed:
        logger.warning(f"   outreach generation failed for '{r.get('business_name')}' "
                       f"({err or 'parse failure'}) - using local fallback")
        return _local_fallback_outreach(r)

    candidate_message = parsed.get("message")
    candidate_subject = parsed.get("subject") or ""

    if (message_pitches_out_of_scope_fix(candidate_message)
            or message_pitches_out_of_scope_fix(candidate_subject)):
        logger.warning(
            f"   outreach for '{r.get('business_name')}' pitched an out-of-scope fix - "
            f"discarding AI message and using in-scope local fallback instead"
        )
        return _local_fallback_outreach(r)

    if (message_makes_false_security_claim(candidate_message, r)
            or message_makes_false_security_claim(candidate_subject, r)):
        logger.warning(
            f"   outreach for '{r.get('business_name')}' claimed a false visible "
            f"security symptom (ssl_valid={r.get('ssl_valid')}) - discarding AI "
            f"message and using verified local fallback instead"
        )
        return _local_fallback_outreach(r)

    return {
        **r,
        "outreach_subject": candidate_subject or f"Quick idea for {r.get('business_name')}",
        "outreach_message": candidate_message,
    }


def _pick_fallback_problem(r):
    """Returns {weakness, story, pitch, subject} for the most relevant
    SELLABLE problem (AI chatbot / lead form / tap-to-call / booking). Only
    EXPLICIT False flags count as gaps - None (unknown) never does. Technical
    issues (security headers, schema, SSL...) are deliberately not pitched."""

    if r.get("has_chatbot") is False:
        return {
            "subject": "No instant replies after hours",
            "weakness": "there's no instant automated reply when someone reaches out outside business hours",
            "story": ("Picture someone visiting your website at night or on a weekend, looking for "
                      "your service. They're interested, but there's no one to answer, so they leave "
                      "and a competitor who replies faster gets them instead."),
            "pitch": ("That gap is easy to close: I build simple AI + WhatsApp systems that answer "
                      "questions and capture enquiries instantly."),
        }
    if r.get("has_lead_form") is False:
        return {
            "subject": "Nowhere to leave contact details",
            "weakness": "there's no way to leave contact details without calling first",
            "story": ("Picture someone browsing your site who isn't ready to call yet. They can't "
                      "leave their number anywhere on the page, so they leave and a competitor with "
                      "a simple enquiry form gets them instead."),
            "pitch": ("I build simple AI + WhatsApp systems that capture those enquiries and follow "
                      "up, even when someone isn't ready to call."),
        }
    if r.get("has_phone_cta") is False:
        return {
            "subject": "No easy tap-to-call on mobile",
            "weakness": "there's no clear call button, especially on mobile",
            "story": ("Picture someone on their phone who wants to reach you quickly. They have to "
                      "hunt for a number instead of tapping to call, so they leave and a competitor "
                      "who makes it easier gets them instead."),
            "pitch": ("I build simple AI + WhatsApp systems that make it effortless to reach you, "
                      "and follow up if someone doesn't."),
        }
    if r.get("has_appointment_booking") is False:
        return {
            "subject": "Can't book outside business hours",
            "weakness": "there's no way to book an appointment online",
            "story": ("Picture someone visiting after you've closed for the day. They can't book "
                      "anything and would have to remember to call tomorrow, so a competitor they "
                      "can book with straight away gets them instead."),
            "pitch": ("I build simple AI + WhatsApp systems that let people book automatically, "
                      "any time of day."),
        }

    # No sellable gap (such leads are normally sent to REVIEW before we get here).
    return {
        "subject": "A quick idea for your website",
        "weakness": "a few things that could be costing you enquiries quietly",
        "story": ("It isn't obvious to a casual visitor, but people who don't get a quick, easy "
                  "response often go with whichever business makes things easier."),
        "pitch": ("I build simple AI + WhatsApp systems that reply instantly and follow up, so "
                  "fewer enquiries slip away."),
    }


def _local_fallback_outreach(r: dict) -> dict:
    """Same 6-beat shape as the AI version, built with rule-based branching —
    used only if the AI fails or fails the validator. Includes its own
    defensive check against the exact class of bug the validator exists to
    prevent."""
    business_name = r.get("business_name") or r.get("title") or "your business"
    p = _pick_fallback_problem(r)
    message = (
        f"Hi there,\n"
        f"I was looking at {business_name} and noticed {p['weakness']}.\n"
        f"{p['story']}\n"
        f"{p['pitch']}\n"
        f"Would you like a 60-second example of how this could work for {business_name}?"
    )
    subject = p.get("subject") or f"Quick idea for {business_name}"

    if (message_makes_false_security_claim(message, r)
            or message_makes_false_security_claim(subject, r)):
        # Should never happen — every branch above avoids this — but if a
        # future edit breaks that, fall back to a fully generic, always-true
        # framing that makes no claim about any specific feature.
        logger.error(
            f"   local fallback for '{business_name}' unexpectedly failed its own "
            f"safety check - using generic safe framing instead"
        )
        subject = f"Quick idea for {business_name}"
        message = (
            f"Hi there,\n"
            f"I was looking at {business_name} and noticed a few things on the "
            f"site that could be costing you enquiries without being obvious day to day.\n"
            f"People who don't get a quick, easy response often go with whichever "
            f"business makes things easier.\n"
            f"I build simple AI + WhatsApp systems that reply instantly and capture "
            f"the enquiry automatically, even when your team is offline.\n"
            f"Would you like a 60-second example of how this could work for {business_name}?"
        )

    return {
        **r,
        "outreach_subject": subject,
        "outreach_message": message,
    }


def ai_qualify(r):
    prompt = build_ai_user_prompt(r)
    raw, err = call_llm(AI_SYSTEM_PROMPT, prompt, temperature=0.3)
    parsed = parse_ai_json(raw) if raw else None

    if not parsed:
        return {
            **r,
            "primary_problem": top_issue_text(r),
            "secondary_problems": [],
            "evidence": f"AI response could not be parsed or fetched ({err or 'parse failure'})",
            "confidence": "Low",
            "priority": r.get("lead_tier", "Cold").upper() if r.get("lead_tier") else "COLD",
            "qualification_status": "REVIEW",
            "contactability_status": "PARTIALLY_CONTACTABLE",
            "contact_channels": {
                "email": bool(r["extracted_email"]), "phone": bool(r["phone"]),
                "website": bool(r["website"]), "contact_form": False, "other": False,
            },
            "recommended_action": "MANUAL_REVIEW",
            "review_reason": "AI output failed to parse/return - needs manual analysis",
            "commercial_opportunity_score": None,
            "passed_quality_gate": 0,
            "outreach_subject": None,
            "outreach_message": None,
            "audit_summary": r["mistakes_text"],
            "ai_parse_failed": True,
        }

    result = {
        **r,
        "business_name": parsed.get("business_name") or r["title"],
        "primary_problem": align_primary_problem(parsed.get("primary_problem"), r),
        "secondary_problems": parsed.get("secondary_problems", []),
        "evidence": build_evidence(r),
        "confidence": parsed.get("confidence"),
        "priority": parsed.get("priority"),
        "commercial_opportunity_score": parsed.get("commercial_opportunity_score") or r.get("opportunity_score"),
        "qualification_status": parsed.get("qualification_status", "REVIEW"),
        "contactability_status": parsed.get("contactability_status"),
        "contact_channels": parsed.get("contact_channels"),
        "recommended_action": parsed.get("recommended_action", "MANUAL_REVIEW"),
        "review_reason": parsed.get("review_reason"),
        "passed_quality_gate": 1 if parsed.get("passed_quality_gate") in (1, True) else 0,
        "outreach_subject": None,
        "outreach_message": None,
        "audit_summary": parsed.get("audit_summary", r["mistakes_text"]),
        "ai_parse_failed": False,
    }

    # Only leads with a gap we actually sell a fix for can be QUALIFIED.
    if result.get("qualification_status") == "QUALIFIED" and not r.get("scope_gap_labels"):
        result["qualification_status"] = "REVIEW"
        result["recommended_action"] = "MANUAL_REVIEW"
        result["passed_quality_gate"] = 0
        result["review_reason"] = ("no gap matching the services offered (AI/WhatsApp automation) - "
                                   "technical issues only")

    # Generate a UNIQUE, problem-specific outreach message for qualified
    # leads only — this is a separate AI call from qualification, using
    # this lead's real audit data so the message matches its actual issue.
    if result.get("qualification_status") == "QUALIFIED":
        result = generate_ai_outreach(result)

    return result


# ----------------------------------------------------------------------
# 7. Quality gate (integrity checks, mirrors n8n "Quality Gate" node)
# ----------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def quality_gate(r):
    reasons = []
    if r.get("review_reason"):
        reasons.append(r["review_reason"])

    email = r.get("extracted_email")
    if email and not EMAIL_RE.match(email):
        reasons.append("extracted email failed format validation")
        email = None

    if r.get("ai_parse_failed"):
        reasons.append("AI output failed to parse as JSON")

    message = r.get("outreach_message") or ""
    word_count = len(message.split()) if message else 0
    if message and word_count > 160:
        reasons.append(f"outreach message unusually long ({word_count} words)")

    if r.get("qualification_status") != "QUALIFIED" and message:
        reasons.append("outreach message present on a non-QUALIFIED lead - discarding it")

    if r.get("fetch_ok") is False:
        reasons.append("website fetch failed - verify site manually before outreach")
    if r.get("fetch_warning"):
        reasons.append(f"audit reliability warning: {r['fetch_warning']}")

    qualified = r.get("qualification_status") == "QUALIFIED"
    fetch_blocked = STRICT_FETCH_CHECK and r.get("fetch_ok") is False
    passed = (
        r.get("passed_quality_gate") == 1
        and qualified
        and not r.get("ai_parse_failed")
        and not fetch_blocked
    )

    # Enforce the recommended action in code instead of trusting the AI.
    action = r.get("recommended_action")
    if qualified:
        if fetch_blocked:
            action = "MANUAL_REVIEW"
        elif email:
            action = "EMAIL_OUTREACH"
        elif r.get("phone"):
            action = "PHONE_OUTREACH"
        elif r.get("website"):
            action = "WEBSITE_ENRICHMENT"

    return {
        **r,
        "extracted_email": email,
        "recommended_action": action,
        "outreach_message": message if qualified else None,
        "outreach_subject": r.get("outreach_subject") if qualified else None,
        "passed_quality_gate": 1 if passed else 0,
        "review_reason": "; ".join(dict.fromkeys(reasons)) or None,
    }


# ----------------------------------------------------------------------
# 8. Email deliverability (ZeroBounce, optional)
# ----------------------------------------------------------------------
def _domain_can_receive_mail(domain):
    """True / False / None(unknown). Uses dnspython MX lookup if installed,
    otherwise falls back to a plain DNS resolve of the domain."""
    try:
        import dns.resolver
        import dns.exception
    except ImportError:
        dns = None
    if dns is not None:
        try:
            dns.resolver.resolve(domain, "MX", lifetime=5)
            return True
        except dns.resolver.NoAnswer:
            pass  # no MX record - fall through to the plain lookup below
        except dns.resolver.NXDOMAIN:
            return False
        except dns.exception.DNSException:
            return None
    try:
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        return False


def verify_email(email):
    """ZeroBounce if a key is set; otherwise a free domain-level check
    (mx_ok / no_mx / mx_unknown). The free check proves the domain can
    receive mail, NOT that the mailbox exists."""
    if not ZEROBOUNCE_API_KEY:
        if not EMAIL_RE.match(email or ""):
            return "invalid_format"
        ok = _domain_can_receive_mail(email.rsplit("@", 1)[1])
        return "mx_ok" if ok else "no_mx" if ok is False else "mx_unknown"
    try:
        resp = requests.get(
            "https://api.zerobounce.net/v2/validate",
            params={"api_key": ZEROBOUNCE_API_KEY, "email": email},
            timeout=10,
        )
        data = resp.json()
        return data.get("status", "unknown")
    except Exception as e:
        logger.debug(f"ZeroBounce error for {email}: {e}")
        return "verification_failed"


# ----------------------------------------------------------------------
# Output helpers (CSV incremental writer + optional SQLite upsert)
# ----------------------------------------------------------------------
OUTPUT_COLUMNS = [
    "input_id", "title", "business_name", "category", "phone", "website",
    "review_rating", "review_count", "complete_address", "extracted_email",
    "email", "google_maps_url", "social_media_urls", "priority",
    "primary_problem", "mistakes_text", "audit_status", "health_score",
    "opportunity_score", "critical_issue_count", "high_issue_count",
    "medium_issue_count", "low_issue_count", "ssl_valid", "has_chatbot",
    "has_whatsapp", "has_lead_form", "has_phone_cta", "has_appointment_booking",
    "has_trust_signals", "has_local_schema", "evidence", "confidence",
    "qualification_status", "contactability_status", "recommended_action",
    "commercial_opportunity_score", "secondary_problems", "review_reason",
    "passed_quality_gate", "outreach_subject", "outreach_message",
    "audit_summary", "email_verified",
]


def make_output_row(r: dict) -> dict:
    row = {}
    for col in OUTPUT_COLUMNS:
        val = r.get(col)
        if isinstance(val, (list, dict)):
            val = json.dumps(val, ensure_ascii=False)
        row[col] = val
    return row


def load_already_processed_ids(output_csv: str) -> set:
    if not os.path.exists(output_csv):
        return set()
    try:
        existing = pd.read_csv(output_csv, dtype=str, keep_default_na=False)
        if "input_id" in existing.columns:
            return set(existing["input_id"].tolist())
    except Exception as e:
        logger.warning(f"Could not read existing output_csv for resume: {e}")
    return set()


def init_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    cols_sql = ", ".join(f'"{c}" TEXT' for c in OUTPUT_COLUMNS if c != "input_id")
    conn.execute(f'CREATE TABLE IF NOT EXISTS leads (input_id TEXT PRIMARY KEY, {cols_sql})')
    conn.commit()
    return conn


def upsert_sqlite(conn: sqlite3.Connection, row: dict):
    placeholders = ", ".join("?" for _ in OUTPUT_COLUMNS)
    cols = ", ".join(f'"{c}"' for c in OUTPUT_COLUMNS)
    values = [row.get(c) for c in OUTPUT_COLUMNS]
    conn.execute(f'INSERT OR REPLACE INTO leads ({cols}) VALUES ({placeholders})', values)
    conn.commit()


# ----------------------------------------------------------------------
# Pipeline driver
# ----------------------------------------------------------------------
def process_lead(r: dict, use_ai: bool, sleep_between_ai_calls: float) -> dict:
    if is_low_chance(r):
        logger.info("   -> archived (low chance)")
        return archive_low_chance(r)

    r = website_mistake_check(r)
    r["scope_gap_labels"] = [g["label"] for g in SCOPE_GAPS if r.get(g["flag"]) is False]

    if not r["website"]:
        logger.info("   -> REVIEW (no website - outside the automation offer)")
        return review_no_website(r)

    if r["website"] and not r.get("email"):
        r = extract_email_for_lead(r)
    else:
        r["extracted_email"] = r.get("email")
        r["all_emails_found"] = [r["email"]] if r.get("email") else []

    if use_ai:
        r = ai_qualify(r)
        if AI_PROVIDER != "ollama":
            # one sleep covers both the qualification call and (if QUALIFIED)
            # the extra outreach-generation call, so free-tier rate limits
            # still get respected between leads
            time.sleep(sleep_between_ai_calls)
    else:
        r = {
            **r, "primary_problem": None, "evidence": None, "confidence": None,
            "priority": r.get("lead_tier", "Unscored").upper(),
            "qualification_status": "REVIEW", "contactability_status": None,
            "recommended_action": "MANUAL_REVIEW", "review_reason": "AI step skipped (--no-ai)",
            "commercial_opportunity_score": None, "passed_quality_gate": 0,
            "outreach_subject": None, "outreach_message": None,
            "audit_summary": r["mistakes_text"], "ai_parse_failed": False,
        }

    r = quality_gate(r)

    if r["passed_quality_gate"] == 1 and r.get("extracted_email"):
        r["email_verified"] = verify_email(r["extracted_email"])
    else:
        r["email_verified"] = "not_checked"

    logger.info(f"   -> {r.get('qualification_status')} / {r.get('priority')}")
    return r


def run_pipeline(input_csv: str, output_csv: str, use_ai: bool = True,
                  sleep_between_ai_calls: float = 2.0, limit: int = None,
                  resume: bool = False, sqlite_path: str = None):
    if os.path.isdir(output_csv):
        raise ValueError(
            f"output_csv '{output_csv}' is a folder, not a file. "
            f"Give a full file path, e.g. '{os.path.join(output_csv, 'qualified_leads.csv')}'."
        )

    out_dir = os.path.dirname(output_csv)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)
        logger.info(f"Created output folder: {out_dir}")

    df = pd.read_csv(input_csv, dtype=str, keep_default_na=False)
    raw_rows = df.to_dict(orient="records")

    normalized = [normalize_row(r) for r in raw_rows]
    normalized = dedupe(normalized)
    logger.info(f"Loaded {len(raw_rows)} rows -> {len(normalized)} after dedupe")

    if resume:
        already_done = load_already_processed_ids(output_csv)
        if already_done:
            before = len(normalized)
            normalized = [r for r in normalized if r["input_id"] not in already_done]
            logger.info(f"Resume: skipping {before - len(normalized)} leads already in {output_csv}")

    if limit:
        normalized = normalized[:limit]
        logger.info(f"Limiting to first {limit} leads")

    if not normalized:
        logger.info("Nothing to process.")
        return

    write_header = not (resume and os.path.exists(output_csv) and os.path.getsize(output_csv) > 0)
    file_mode = "a" if (resume and os.path.exists(output_csv)) else "w"

    # Shortlist CSV always lives next to the main output_csv.
    shortlist_csv = os.path.join(os.path.dirname(output_csv) or ".", SHORTLIST_FILENAME)
    shortlist_write_header = not (resume and os.path.exists(shortlist_csv) and os.path.getsize(shortlist_csv) > 0)
    shortlist_mode = "a" if (resume and os.path.exists(shortlist_csv)) else "w"
    shortlist_headers = [label for _, label in SHORTLIST_COLUMNS]

    sqlite_conn = init_sqlite(sqlite_path) if sqlite_path else None

    shortlisted_count = 0

    with open(output_csv, file_mode, newline="", encoding="utf-8") as f, \
         open(shortlist_csv, shortlist_mode, newline="", encoding="utf-8") as sf:

        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        if write_header:
            writer.writeheader()
            f.flush()

        shortlist_writer = csv.DictWriter(sf, fieldnames=shortlist_headers)
        if shortlist_write_header:
            shortlist_writer.writeheader()
            sf.flush()

        for r in tqdm(normalized, desc="Qualifying leads", unit="lead"):
            logger.info(f"Processing: {r['title']}")
            try:
                result = process_lead(r, use_ai, sleep_between_ai_calls)
            except AIAuthError:
                raise
            except Exception as e:
                logger.error(f"   unhandled error on '{r['title']}': {e}")
                result = {
                    **r, "primary_problem": None, "evidence": f"unhandled pipeline error: {e}",
                    "confidence": "Low", "priority": "COLD", "qualification_status": "REVIEW",
                    "contactability_status": None, "recommended_action": "MANUAL_REVIEW",
                    "review_reason": f"unhandled exception: {e}", "commercial_opportunity_score": None,
                    "passed_quality_gate": 0, "outreach_subject": None, "outreach_message": None,
                    "audit_summary": None, "email_verified": "not_checked",
                    "extracted_email": r.get("email"),
                }

            out_row = make_output_row(result)
            writer.writerow(out_row)
            f.flush()  # incremental write - crash-safe

            if sqlite_conn:
                upsert_sqlite(sqlite_conn, out_row)

            # Only leads that actually cleared the quality gate AND got an
            # outreach message written are worth putting in front of you -
            # that's the whole point of the shortlist CSV.
            if result.get("outreach_message") and result.get("passed_quality_gate") == 1:
                shortlist_row = {}
                for key, label in SHORTLIST_COLUMNS:
                    val = result.get(key)
                    if isinstance(val, (list, dict)):
                        val = json.dumps(val, ensure_ascii=False)
                    shortlist_row[label] = val
                shortlist_writer.writerow(shortlist_row)
                sf.flush()
                shortlisted_count += 1

    if sqlite_conn:
        sqlite_conn.close()

    logger.info(f"Done. Wrote {len(normalized)} rows to {output_csv}"
                + (f" and upserted into {sqlite_path}" if sqlite_path else ""))
    logger.info(f"Shortlisted {shortlisted_count} leads with outreach messages -> {shortlist_csv}")


# ----------------------------------------------------------------------
# Interactive mode (no CLI args) — asks for the CSV path and just runs
# ----------------------------------------------------------------------
def clean_path_input(raw: str) -> str:
    """Strips whitespace and the surrounding quotes Windows adds when you
    use 'Copy as path' in Explorer, so pasting a path just works. Also
    strips a trailing slash/backslash some folder paths carry."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    raw = raw.strip()
    if len(raw) > 3 and raw[-1] in ("\\", "/"):  # keep bare drive roots like C:\ intact
        raw = raw[:-1]
    return raw


def prompt_for_input_csv() -> str:
    while True:
        raw = input("Enter the path to your input CSV file: ")
        path = clean_path_input(raw)
        if not path:
            print("  Please enter a path.")
            continue
        if os.path.isdir(path):
            print(f"  '{path}' is a folder, not a file - point me at the .csv file inside it.")
            continue
        if not os.path.exists(path):
            print(f"  File not found: {path}")
            continue
        if not path.lower().endswith(".csv"):
            confirm = input(f"  '{path}' doesn't end in .csv - use it anyway? [y/N]: ").strip().lower()
            if confirm != "y":
                continue
        return path


def prompt_for_output_csv(input_path: str) -> str:
    default_output = str(Path(DEFAULT_OUTPUT_DIR) / (Path(input_path).stem + "_qualified.csv"))
    raw = input(f"Enter output CSV path [Enter for default: {default_output}]: ")
    path = clean_path_input(raw)
    if not path:
        return default_output
    if os.path.isdir(path):
        # They gave a folder - save the default filename into it instead of crashing later.
        chosen = os.path.join(path, Path(default_output).name)
        print(f"  '{path}' is a folder - saving output to: {chosen}")
        return chosen
    return path


def prompt_yes_no(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{question} {suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def run_interactive() -> dict:
    print("=" * 60)
    print(" Lead Qualification Pipeline")
    print("=" * 60)

    input_csv = prompt_for_input_csv()
    output_csv = prompt_for_output_csv(input_csv)

    resume = False
    if os.path.exists(output_csv) and os.path.getsize(output_csv) > 0:
        resume = prompt_yes_no(
            f"'{output_csv}' already exists - resume (skip leads already in it) instead of overwriting?",
            default=True,
        )

    use_ai = True
    if AI_PROVIDER != "ollama" and not GROQ_API_KEY:
        print("\nNo GROQ_API_KEY found (env var or .env file next to this script).")
        continue_without_ai = prompt_yes_no(
            "Continue WITHOUT AI qualification (mechanics-only run)?", default=False
        )
        if not continue_without_ai:
            print("Add GROQ_API_KEY=... to a .env file next to this script, then run again.")
            sys.exit(1)
        use_ai = False

    return {
        "input_csv": input_csv,
        "output_csv": output_csv,
        "use_ai": use_ai,
        "sleep": 6.0,
        "limit": None,
        "resume": resume,
        "sqlite": None,
        "log_file": "lead_pipeline.log",
    }


def run_cli(argv) -> dict:
    parser = argparse.ArgumentParser(description="Standalone Python lead qualification pipeline")
    parser.add_argument("input_csv", help="Path to the audited leads CSV")
    parser.add_argument("output_csv", nargs="?", default=None,
                         help="Path to write the enriched output CSV (default: <input>_qualified.csv)")
    parser.add_argument("--no-ai", action="store_true", help="Skip the AI qualification step")
    parser.add_argument("--sleep", type=float, default=6.0, help="Seconds between AI calls (default 6.0)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N leads")
    parser.add_argument("--sqlite", type=str, default=None, help="Also upsert results into this SQLite DB")
    parser.add_argument("--resume", action="store_true",
                         help="Skip leads already present in output_csv (matched by input_id)")
    parser.add_argument("--log-file", type=str, default="lead_pipeline.log", help="Path to the run log")
    args = parser.parse_args(argv)

    output_csv = args.output_csv or str(
        Path(DEFAULT_OUTPUT_DIR) / (Path(args.input_csv).stem + "_qualified.csv")
    )

    return {
        "input_csv": args.input_csv,
        "output_csv": output_csv,
        "use_ai": not args.no_ai,
        "sleep": args.sleep,
        "limit": args.limit,
        "resume": args.resume,
        "sqlite": args.sqlite,
        "log_file": args.log_file,
    }


if __name__ == "__main__":
    # No CLI args -> friendly interactive mode: just asks for the CSV path and runs.
    # CLI args still work exactly as before for anyone who wants flags/automation.
    if len(sys.argv) > 1:
        cfg = run_cli(sys.argv[1:])
    else:
        cfg = run_interactive()

    setup_logging(cfg["log_file"])

    if cfg["use_ai"] and AI_PROVIDER != "ollama" and not GROQ_API_KEY:
        logger.error("No GROQ_API_KEY found (env var or .env file). "
                      "Use --no-ai to test without it, set AI_PROVIDER=ollama for local, "
                      "or add GROQ_API_KEY=... to a .env file next to this script.")
        sys.exit(1)

    if not os.path.exists(cfg["input_csv"]):
        logger.error(f"Input CSV not found: {cfg['input_csv']}")
        sys.exit(1)

    if cfg["use_ai"] and AI_PROVIDER != "ollama":
        key_ok, key_msg = check_groq_key()
        if key_ok is False:
            logger.error(key_msg)
            print(f"\nERROR: {key_msg}")
            if len(sys.argv) <= 1:
                try:
                    input("\nPress Enter to exit...")
                except EOFError:
                    pass
            sys.exit(1)
        logger.info(key_msg)

    logger.info(f"Starting pipeline: input={cfg['input_csv']} output={cfg['output_csv']} "
                f"ai={'off' if not cfg['use_ai'] else AI_PROVIDER} "
                f"resume={cfg['resume']} limit={cfg['limit']}")

    try:
        run_pipeline(
            input_csv=cfg["input_csv"],
            output_csv=cfg["output_csv"],
            use_ai=cfg["use_ai"],
            sleep_between_ai_calls=cfg["sleep"],
            limit=cfg["limit"],
            resume=cfg["resume"],
            sqlite_path=cfg["sqlite"],
        )
    except AIAuthError as e:
        logger.error(str(e))
        print(f"\nERROR: {e}")
        print("Stopped early - nothing after this point was processed. Fix the key, then run "
              "again with --resume to continue.")
        if len(sys.argv) <= 1:
            try:
                input("\nPress Enter to exit...")
            except EOFError:
                pass
        sys.exit(1)
    except (PermissionError, IsADirectoryError, ValueError) as e:
        logger.error(f"Could not write output: {e}")
        print(f"\nERROR: {e}")
        print("(If the output CSV is currently open in Excel, close it and try again.)")
        if len(sys.argv) <= 1:
            try:
                input("\nPress Enter to exit...")
            except EOFError:
                pass
        sys.exit(1)

    if len(sys.argv) <= 1:
        try:
            input("\nDone. Press Enter to exit...")
        except EOFError:
            pass