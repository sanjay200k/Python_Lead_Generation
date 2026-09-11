"""
Lead Qualification Pipeline v2 (fully standalone Python — no n8n needed)
=========================================================================
Input:  a CSV like qualified_leads_row9_audited.csv (already has
        health_score / opportunity_score / top_issues / etc. from
        your batch_website_audit.py step)
Output: a new CSV with AI qualification, email extraction/verification,
        priority, outreach message, etc. — same shape as the n8n
        "output lead data table".

Improvements over v1:
  - Automatic retry with exponential backoff on Groq API calls
    (handles 429 rate-limit and transient 5xx errors instead of just failing)
  - Resume support: re-running with --resume skips leads already written
    to output_csv, so a crash/interrupt doesn't cost you API calls again
  - Rows are written incrementally (one at a time, flushed to disk),
    not all at the end — a crash mid-run doesn't lose already-qualified leads
  - Proper logging to both console and a .log file (timestamps, levels)
  - Progress bar (tqdm) instead of raw print spam
  - .env file support (no more `export GROQ_API_KEY=...` every session)
  - Config via CLI flags instead of editing the script
  - Optional SQLite upsert alongside the CSV
  - Optional STATIC outreach message template (set USE_STATIC_OUTREACH_TEMPLATE
    below) — bypasses AI-written outreach copy and fills a fixed template
    instead. Flip the flag back to False any time to return to AI-generated
    messages; nothing else in the pipeline changes either way.
  - v2.1: the static template now translates the raw technical audit finding
    (e.g. "Page is set to noindex") into a plain-English observation AND a
    matching, logically-consistent pitch line, instead of pasting the raw
    audit string into the message twice. See PROBLEM_FRAMING_MAP below.

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
    --sleep 3.0          seconds between Groq calls (default 2.0, raise if rate-limited)
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
import sqlite3
import sys
import time
from pathlib import Path

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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
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

# Set to True to use the fixed outreach message template (STATIC_OUTREACH_TEMPLATE
# below) instead of the AI-generated outreach copy. Set back to False any time to
# revert to AI-written messages — nothing else in the pipeline changes either way.
USE_STATIC_OUTREACH_TEMPLATE = True

# All output CSVs default to this folder unless you type a different path
# when prompted (or pass a different output_csv on the CLI).
DEFAULT_OUTPUT_DIR = r"F:\AI automation\AI automation\Python_Lead_Generation - Copy\Python_Lead_Generation\Main_project\Final_Scraped_Data"

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


# ----------------------------------------------------------------------
# 1. Normalize
# ----------------------------------------------------------------------
def normalize_row(row: dict) -> dict:
    """Maps the raw audited-CSV columns to the lowercase snake_case fields
    every later stage expects (mirrors the n8n 'Normalize Lead Fields' node)."""

    def to_bool(v):
        if isinstance(v, bool):
            return v
        if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
            return False
        return str(v).strip().lower() == "true"

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

    r = {
        "title": row.get("Business Name"),
        "business_name": row.get("Business Name"),
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
        "ssl_valid": to_bool(row.get("ssl_valid")),
        "has_chatbot": to_bool(row.get("has_chatbot")),
        "chatbot_providers": row.get("chatbot_providers") or None,
        "has_whatsapp": to_bool(row.get("has_whatsapp")),
        "has_lead_form": to_bool(row.get("has_lead_form")),
        "has_phone_cta": to_bool(row.get("has_phone_cta")),
        "has_appointment_booking": to_bool(row.get("has_appointment_booking")),
        "has_trust_signals": to_bool(row.get("has_trust_signals")),
        "has_local_schema": to_bool(row.get("has_local_schema")),
        "critical_issue_count": to_num(row.get("critical_issue_count")) or 0,
        "high_issue_count": to_num(row.get("high_issue_count")) or 0,
        "top_issues_raw": row.get("top_issues") or "",
        "fetch_warning": row.get("fetch_warning") or None,
    }
    r["input_id"] = make_lead_id(r)
    return r


def make_lead_id(r: dict) -> str:
    """Stable, deterministic ID for dedupe/resume — prefers the Google Maps
    URL (usually unique per listing), then domain+phone, then business name.
    Unlike v1 (which used business_name as the id), this survives duplicate
    business names and re-running the pipeline."""
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
    local = lower.split("@")[0]
    if re.match(r"^[a-f0-9]{16,}$", local):
        return True
    # generic placeholder local-parts on ANY domain, e.g. "user@somegymsite.com"
    if local in ("user", "yourname", "youremail", "firstname.lastname", "your.name"):
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
confidence must be "High"/"Medium"/"Low". priority must be "HOT"/"WARM"/"COLD".
qualification_status must be "QUALIFIED"/"REVIEW"/"NOT_QUALIFIED".
contactability_status must be "CONTACTABLE"/"PARTIALLY_CONTACTABLE"/"NOT_CONTACTABLE".
recommended_action: QUALIFIED+email->EMAIL_OUTREACH, QUALIFIED+no email+phone->PHONE_OUTREACH,
QUALIFIED+no email+no phone+website->WEBSITE_ENRICHMENT, REVIEW->MANUAL_REVIEW,
NOT_QUALIFIED->ARCHIVE. Only produce "outreach" (subject+message, ~70-100 words,
personalized with the real business name and the single strongest evidence-based
problem) when qualification_status == "QUALIFIED"; otherwise outreach must be null.
passed_quality_gate = 1 iff qualification_status == "QUALIFIED", else 0.
Never invent facts not present in the input. Preserve scores exactly.

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
  "outreach": {"subject": "", "message": ""},
  "audit_summary": ""
}
"""


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

feature checklist:
ssl_valid: {r['ssl_valid']}
has_chatbot: {r['has_chatbot']} (provider: {r['chatbot_providers']})
has_whatsapp: {r['has_whatsapp']}
has_lead_form: {r['has_lead_form']}
has_phone_cta: {r['has_phone_cta']}
has_appointment_booking: {r['has_appointment_booking']}
has_trust_signals: {r['has_trust_signals']}
has_local_schema: {r['has_local_schema']}
"""


def _sleep_with_backoff(attempt, retry_after=None):
    wait = retry_after if retry_after else BASE_BACKOFF * (2 ** attempt)
    logger.warning(f"   retrying in {wait:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
    time.sleep(wait)


def call_groq(model, user_prompt):
    """Calls Groq with exponential backoff on 429 / 5xx / network errors.
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
                        {"role": "system", "content": AI_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
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
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                return None, f"groq returned malformed response: {e}"

        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            retry_after = float(retry_after) if retry_after else None
            if attempt < MAX_RETRIES - 1:
                _sleep_with_backoff(attempt, retry_after)
                continue
            return None, "groq rate-limited after max retries"

        if 500 <= resp.status_code < 600:
            if attempt < MAX_RETRIES - 1:
                _sleep_with_backoff(attempt)
                continue
            return None, f"groq HTTP {resp.status_code} after retries"

        # non-retryable 4xx (bad key, bad request, etc.)
        return None, f"groq HTTP {resp.status_code}: {resp.text[:200]}"

    return None, "groq: exhausted retries"


def call_ollama(model, user_prompt):
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {"temperature": 0.3},
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
# 6b. Static outreach template (used instead of AI-written messages when
#     USE_STATIC_OUTREACH_TEMPLATE = True). Only the outreach subject/message
#     are affected — qualification, scoring, and everything else still comes
#     from the AI step untouched.
#
#     v2.1 rewrite: the old version dropped the raw audit string (e.g.
#     "Missing recommended security headers") straight into the template
#     TWICE, verbatim, and closed every message with "so I had an idea for
#     automating that" regardless of whether automation was even the fix.
#     That produced messages that (a) used jargon no one would notice from
#     "looking at your Instagram", (b) repeated the same clause twice, and
#     (c) had a non-sequitur close for problems automation can't solve
#     (e.g. security headers, noindex tags).
#
#     v2.3 change (per user request): replaced the per-problem branching
#     template (PROBLEM_FRAMING_MAP / variant selection) with ONE fixed
#     universal message used for every qualified lead, regardless of what
#     primary_problem the audit found. The message makes a generic-but-
#     plausible "after-hours enquiry" observation that applies to almost
#     any local business, rather than referencing the specific technical
#     finding at all. Nothing else in the pipeline (qualification, scoring,
#     priority) is affected by this — only outreach_subject/outreach_message.
# ----------------------------------------------------------------------

STATIC_OUTREACH_TEMPLATE = """Hi {name},
I was checking out {business_name} and noticed something that may be costing you enquiries.
When someone visits your website outside business hours, there doesn't seem to be an easy way for them to ask a question or enquire right away. A potential member could simply leave and move on.
I build simple website + WhatsApp AI systems that answer questions, capture leads, and follow up automatically — even when your team is offline.
Would you be open to a quick 60-second walkthrough of how this could work for {business_name}?"""


def build_static_outreach(r: dict) -> dict:
    """Fills the fixed universal outreach template with just the business
    name — same message for every lead, independent of primary_problem.
    Only overrides outreach_subject/outreach_message — does not touch
    qualification_status, priority, scoring, etc."""
    name = "there"  # no contact-name field is scraped yet; swap in if you add one later
    business_name = r.get("business_name") or r.get("title") or "your business"

    message = STATIC_OUTREACH_TEMPLATE.format(
        name=name,
        business_name=business_name,
    )

    return {
        **r,
        "outreach_subject": f"Quick idea for {business_name}",
        "outreach_message": message,
    }


def ai_qualify(r):
    prompt = build_ai_user_prompt(r)

    if AI_PROVIDER == "ollama":
        raw, err = call_ollama(OLLAMA_MODEL, prompt)
    else:
        raw, err = call_groq(GROQ_MODEL_PRIMARY, prompt)
        if raw is None:
            logger.warning(f"   primary model failed ({err}) - trying fallback model")
            raw, err = call_groq(GROQ_MODEL_FALLBACK, prompt)

    parsed = parse_ai_json(raw) if raw else None

    if not parsed:
        return {
            **r,
            "primary_problem": r["mistakes_text"] or "unknown",
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

    outreach = parsed.get("outreach") or {}
    result = {
        **r,
        "business_name": parsed.get("business_name", r["title"]),
        "primary_problem": parsed.get("primary_problem"),
        "secondary_problems": parsed.get("secondary_problems", []),
        "evidence": parsed.get("evidence"),
        "confidence": parsed.get("confidence"),
        "priority": parsed.get("priority"),
        "commercial_opportunity_score": parsed.get("commercial_opportunity_score") or r.get("opportunity_score"),
        "qualification_status": parsed.get("qualification_status", "REVIEW"),
        "contactability_status": parsed.get("contactability_status"),
        "contact_channels": parsed.get("contact_channels"),
        "recommended_action": parsed.get("recommended_action", "MANUAL_REVIEW"),
        "review_reason": parsed.get("review_reason"),
        "passed_quality_gate": 1 if parsed.get("passed_quality_gate") in (1, True) else 0,
        "outreach_subject": outreach.get("subject"),
        "outreach_message": outreach.get("message"),
        "audit_summary": parsed.get("audit_summary", r["mistakes_text"]),
        "ai_parse_failed": False,
    }

    # Override with the static template if enabled — only for qualified leads,
    # matching the same condition the AI already applies to its own outreach.
    # Flip USE_STATIC_OUTREACH_TEMPLATE to False at the top of the file to
    # revert to the AI-generated outreach copy at any time.
    if USE_STATIC_OUTREACH_TEMPLATE and result.get("qualification_status") == "QUALIFIED":
        result = build_static_outreach(result)

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
    if message and word_count > 130:
        reasons.append(f"outreach message unusually long ({word_count} words)")

    if r.get("qualification_status") != "QUALIFIED" and message:
        reasons.append("outreach message present on a non-QUALIFIED lead - discarding it")

    if r.get("fetch_ok") is False:
        reasons.append("website fetch failed - verify site manually before outreach")
    if r.get("fetch_warning"):
        reasons.append(f"audit reliability warning: {r['fetch_warning']}")

    passed = (
        r.get("passed_quality_gate") == 1
        and not r.get("ai_parse_failed")
        and not (r.get("qualification_status") != "QUALIFIED" and message)
    )

    qualified = r.get("qualification_status") == "QUALIFIED"
    return {
        **r,
        "extracted_email": email,
        "outreach_message": message if qualified else None,
        "outreach_subject": r.get("outreach_subject") if qualified else None,
        "passed_quality_gate": 1 if passed else 0,
        "review_reason": "; ".join(dict.fromkeys(reasons)) or None,
    }


# ----------------------------------------------------------------------
# 8. Email deliverability (ZeroBounce, optional)
# ----------------------------------------------------------------------
def verify_email(email):
    if not ZEROBOUNCE_API_KEY:
        return "not_checked"
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
    except (pd.errors.EmptyDataError, Exception) as e:
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
        logger.info(f"   -> archived (low chance)")
        return archive_low_chance(r)

    r = website_mistake_check(r)

    if r["website"] and not r.get("email"):
        r = extract_email_for_lead(r)
    else:
        r["extracted_email"] = r.get("email")
        r["all_emails_found"] = [r["email"]] if r.get("email") else []

    if use_ai:
        r = ai_qualify(r)
        if AI_PROVIDER != "ollama":
            time.sleep(sleep_between_ai_calls)  # be gentle with Groq's free tier
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
    shortlist_field_keys = [key for key, _ in SHORTLIST_COLUMNS]
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

            # Only leads that actually cleared the quality gate and got an
            # outreach message written are worth putting in front of you -
            # that's the whole point of the shortlist CSV.
            if result.get("outreach_message"):
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