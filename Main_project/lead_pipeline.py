"""
Lead Qualification Pipeline v4 (fully standalone Python — no n8n needed)
=========================================================================
Input:  a CSV like qualified_leads_row_audited.csv (already has
        health_score / opportunity_score / top_issues / etc. from
        your batch_website_audit.py step)
Output: a new CSV with AI qualification, email extraction/verification,
        priority, outreach message, etc. — same shape as the n8n
        "output lead data table".

v4 change (this version): MULTI-KEY GROQ ROTATION. Instead of one
GROQ_API_KEY, you can supply several keys (ideally from separate Groq
accounts) via GROQ_API_KEYS=key1,key2,key3. A GroqKeyPool round-robins
across them and, when a key gets rate-limited (429), puts ONLY that key
on cooldown and immediately retries with the next key — instead of the
whole pipeline sleeping. This can dramatically cut wall-clock time as
long as the keys are on different Groq accounts (keys on the SAME
account share the same underlying quota, so rotating them won't help
in that case).

v3 change: outreach messages are no longer a single fixed template.
Each QUALIFIED lead gets its own Groq call (generate_ai_outreach) that
writes a UNIQUE message about that specific lead's REAL missing feature
(no chatbot/WhatsApp, no lead form, no phone CTA, no booking, or
whatever its top audit issue is) while following the same fixed 6-beat
scenario structure every time:
  1. notice a specific weakness on THIS business's site
  2. a short "someone visits your site" scenario
  3. they leave / a competitor gets them instead
  4. restate the weakness briefly
  5. pitch the specific fix that solves THAT problem
  6. ask for a 60-second walkthrough
If Groq fails twice, a local rule-based fallback (same 6-beat shape, no API
call) fills in so a lead never ends up with a blank outreach message.

Usage:
    1) Create a .env file next to this script:
           GROQ_API_KEYS=gsk_key1xxxxxxxx,gsk_key2xxxxxxxx,gsk_key3xxxxxxxx
       (or a single GROQ_API_KEY=gsk_xxxx if you only have one)
       (get free keys at https://console.groq.com/keys — use separate
        Groq accounts/emails per key for the rotation to actually help)

    2) Run:
           python lead_pipeline.py input.csv output.csv

    3) Re-running with the same output.csv resumes where it left off:
           python lead_pipeline.py input.csv output.csv --resume

Optional flags:
    --no-ai              skip the AI qualification step entirely (mechanics-only test)
    --sleep 3.0          seconds between Groq calls (default 6.0, raise if rate-limited)
                          with multiple keys you can usually lower this a lot (e.g. 1.0
                          or 0) since the pool self-throttles per-key on 429s
    --limit 20           only process the first N leads (useful for a quick test run)
    --sqlite path.db     also upsert results into a SQLite DB (in addition to the CSV)
    --resume             skip leads already present in output_csv (matched by input_id)
    --log-file path.log  where to write the run log (default: lead_pipeline.log)

Optional env vars (in .env or exported):
    GROQ_API_KEYS        -> comma-separated list of Groq API keys (preferred).
                            Ideally from DIFFERENT Groq accounts, since keys on
                            the same account share the same rate-limit quota.
    GROQ_API_KEY         -> single-key fallback if GROQ_API_KEYS isn't set
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
import itertools
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from threading import Lock

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
# NOTE: Never hardcode API keys in the script. Set GROQ_API_KEYS (comma
# separated) or GROQ_API_KEY in a .env file next to this script instead.
_raw_multi = os.environ.get("GROQ_API_KEYS", "")
GROQ_API_KEYS = [k.strip() for k in _raw_multi.split(",") if k.strip()]
if not GROQ_API_KEYS:
    _single = os.environ.get("GROQ_API_KEY", "")
    if _single:
        GROQ_API_KEYS = [_single]

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
DEFAULT_RATE_LIMIT_COOLDOWN = 60.0  # seconds, used when Groq gives no retry-after

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
# 0. Groq multi-key rotation pool
# ----------------------------------------------------------------------
class GroqKeyPool:
    """Round-robins across multiple Groq API keys.

    If a key gets rate-limited (HTTP 429), it's put on cooldown until the
    time Groq's `retry-after` header indicates, and the pool skips it
    until then — so as long as at least ONE key still has quota, the
    pipeline keeps moving instead of sleeping the whole run.

    A key that comes back invalid/revoked (401/403) is permanently
    benched for the rest of the run.

    NOTE: this only helps if the keys are on SEPARATE Groq accounts.
    Multiple keys under one account share that account's rate limit, so
    rotating between them hits the same ceiling as using just one.
    """

    def __init__(self, keys):
        self.keys = [k for k in keys if k]
        if not self.keys:
            raise ValueError(
                "No Groq API keys configured. Set GROQ_API_KEYS "
                "(comma-separated) or GROQ_API_KEY in your .env file."
            )
        self._cooldowns = {k: 0.0 for k in self.keys}  # key -> unix time it's free again
        self._cycle = itertools.cycle(self.keys)
        self._lock = Lock()
        logger.info(f"Groq key pool initialized with {len(self.keys)} key(s)")

    def available_count(self) -> int:
        now = time.time()
        return sum(1 for k in self.keys if self._cooldowns[k] <= now)

    def get_key(self) -> str:
        """Returns the next available key, preferring ones not on cooldown.
        If all are on cooldown, waits for whichever frees up soonest."""
        with self._lock:
            now = time.time()
            for _ in range(len(self.keys)):
                k = next(self._cycle)
                if self._cooldowns[k] <= now:
                    return k
            # all on cooldown - pick the one that frees soonest and wait for it
            soonest_key = min(self._cooldowns, key=self._cooldowns.get)
            wait = max(0.0, self._cooldowns[soonest_key] - now)
            if wait > 0:
                logger.warning(
                    f"   all {len(self.keys)} Groq key(s) are rate-limited or benched - "
                    f"waiting {wait:.1f}s for the next one to free up"
                )
                time.sleep(wait)
            return soonest_key

    def mark_rate_limited(self, key: str, retry_after: float = None):
        with self._lock:
            self._cooldowns[key] = time.time() + (retry_after or DEFAULT_RATE_LIMIT_COOLDOWN)

    def mark_bad_key(self, key: str):
        """Permanently benches a key (invalid/revoked) for this run."""
        with self._lock:
            self._cooldowns[key] = float("inf")

    @staticmethod
    def _mask(key: str) -> str:
        return f"...{key[-6:]}" if key and len(key) > 6 else "***"


groq_pool = GroqKeyPool(GROQ_API_KEYS) if GROQ_API_KEYS else None


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
# 6. AI qualification (Groq / Ollama) with retry + backoff + key rotation
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


def call_groq(model, user_prompt):
    """Calls Groq with exponential backoff + multi-key rotation on 429 /
    5xx / network errors, using the qualification system prompt
    (AI_SYSTEM_PROMPT). Non-retryable errors (4xx other than 429) fail
    immediately once every key has been tried."""
    return _call_groq_with_system(model, AI_SYSTEM_PROMPT, user_prompt, temperature=0.3)


def _call_groq_with_system(model, system_prompt, user_prompt, temperature=0.3):
    """Generic Groq caller with retry/backoff + key-pool rotation —
    parameterized system prompt and temperature so it can serve both
    qualification (low temp, strict JSON) and outreach generation
    (higher temp, more varied phrasing).

    On every attempt, pulls the next available key from groq_pool. A 429
    only cools down THAT key and moves to the next one immediately (no
    sleep) as long as another key is free. Only if every key is on
    cooldown does it wait."""
    if groq_pool is None:
        return None, "no Groq API key(s) configured"

    # cap attempts at max(MAX_RETRIES, number of keys) so a multi-key pool
    # gets a fair shot at trying every key at least once
    total_attempts = max(MAX_RETRIES, len(groq_pool.keys))

    last_err = None
    for attempt in range(total_attempts):
        key = groq_pool.get_key()
        try:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
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
            last_err = f"groq network error: {e}"
            logger.warning(f"   groq network error ({model}, key {GroqKeyPool._mask(key)}): {e}")
            if attempt < total_attempts - 1:
                time.sleep(BASE_BACKOFF)
                continue
            return None, f"{last_err} (after retries)"

        if resp.status_code == 200:
            try:
                return resp.json()["choices"][0]["message"]["content"], None
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                return None, f"groq returned malformed response: {e}"

        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            retry_after = float(retry_after) if retry_after else None
            groq_pool.mark_rate_limited(key, retry_after)
            logger.warning(
                f"   key {GroqKeyPool._mask(key)} rate-limited "
                f"(cooldown {retry_after or DEFAULT_RATE_LIMIT_COOLDOWN:.0f}s) - "
                f"{groq_pool.available_count()} key(s) still available, trying next"
            )
            last_err = "groq rate-limited"
            continue  # immediately try another key, no forced sleep

        if resp.status_code in (401, 403):
            groq_pool.mark_bad_key(key)
            logger.error(f"   key {GroqKeyPool._mask(key)} invalid/revoked - benching it for this run")
            last_err = f"groq HTTP {resp.status_code} (bad key)"
            continue

        if 500 <= resp.status_code < 600:
            last_err = f"groq HTTP {resp.status_code}"
            if attempt < total_attempts - 1:
                time.sleep(BASE_BACKOFF * (2 ** min(attempt, 4)))
                continue
            return None, f"{last_err} after retries"

        # non-retryable 4xx (bad request, etc.) — don't burn other keys on this
        return None, f"groq HTTP {resp.status_code}: {resp.text[:200]}"

    return None, last_err or "groq: exhausted retries across all keys"


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
# 6b. UNIQUE, PROBLEM-AWARE outreach (Groq-generated, scenario structure)
# ----------------------------------------------------------------------
# Replaces the old single fixed STATIC_OUTREACH_TEMPLATE. For every
# QUALIFIED lead, generate_ai_outreach() sends Groq that lead's real audit
# fields (has_chatbot, has_whatsapp, has_lead_form, has_phone_cta,
# has_appointment_booking, mistakes_text, primary_problem, evidence) and
# asks it to WRITE a message describing THAT business's actual weakness —
# never a generic one-size-fits-all message — while following a fixed
# 6-beat scenario structure so tone and length stay consistent:
#   1. notice a specific weakness on THIS business's site
#   2. a short "someone visits your site" scenario
#   3. they leave / a competitor gets them instead, because faster/easier
#   4. restate the weakness briefly
#   5. pitch the specific fix that solves THAT weakness
#   6. ask for a 60-second walkthrough, using the business name
#
# If Groq fails twice (network/rate-limit/parse failure), a local
# rule-based fallback (no API call) fills in with the same structure so a
# qualified lead is never left with a blank outreach message.
# ----------------------------------------------------------------------

OUTREACH_SYSTEM_PROMPT = """You are a cold-outreach copywriter for a small AI-automation
consultant who builds simple website + WhatsApp AI systems for local businesses
(instant replies, lead capture, appointment booking, follow-ups).

You will be given ONE lead's real website-audit data. Write ONE outreach message
about THIS business's ACTUAL problem — never a generic message that could apply
to any business. If has_chatbot=false and has_whatsapp=false, the problem is
"no instant reply after hours." If has_lead_form=false, the problem is "visitors
who aren't ready to call can't leave their details." If has_phone_cta=false, the
problem is "no easy way to call, especially on mobile." If has_appointment_booking
=false, the problem is "can't book outside business hours." If none of those are
missing, use the single most severe item in the audit issues text. Pick exactly
ONE problem — the most severe one — do not list several.

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
    - actual expired/invalid SSL certificate or non-HTTPS site -> browser DOES show a
      real "Not Secure" / certificate warning — this is the ONLY case where a visible
      browser security warning is accurate

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
- Use the business name naturally, at most twice.
- Return VALID JSON ONLY, no markdown fences, in this exact schema:
{"subject": "short 4-7 word subject line naming the specific problem", "message": "the full message with \\n between the beats"}
"""


def build_outreach_user_prompt(r: dict) -> str:
    return f"""
business_name: {r.get('business_name') or r.get('title')}
category: {r.get('category')}

ssl_valid: {r.get('ssl_valid')}
has_chatbot: {r.get('has_chatbot')}
has_whatsapp: {r.get('has_whatsapp')}
has_lead_form: {r.get('has_lead_form')}
has_phone_cta: {r.get('has_phone_cta')}
has_appointment_booking: {r.get('has_appointment_booking')}
has_trust_signals: {r.get('has_trust_signals')}

top audit issues (highest severity first): {r.get('mistakes_text')}
primary_problem (from qualification step): {r.get('primary_problem')}
evidence: {r.get('evidence')}

REMINDER: ssl_valid is given above as a real boolean. You may ONLY describe a
visitor seeing a browser security warning, "not secure" label, or missing lock
icon if ssl_valid is explicitly False. If ssl_valid is True (or not shown as
False), do not use ANY of that language for ANY issue, including missing
security headers — headers are invisible to a normal visitor regardless of
ssl_valid.
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
# message, whether from Groq or the local fallback, gets checked here
# before it's allowed into the pipeline. If it fails, the lead falls back
# to the rule-based local template, which is built to never make this
# mistake in the first place.
# ----------------------------------------------------------------------
FALSE_SECURITY_SYMPTOM_PHRASES = [
    "browser warning", "security warning", "not secure", "isn't secure",
    "is not secure", "unsecure", "insecure connection", "lock icon",
    "no lock icon", "shows a warning", "sees a warning", "security alert",
    "certificate warning", "ssl warning", "gets a warning about",
    "warns visitors", "warning about the connection", "warning about insecure",
]


def message_makes_false_security_claim(message: str, r: dict) -> bool:
    """Returns True if the message claims a visible browser/security warning
    while the lead's real ssl_valid is True (or unknown) — i.e. the only
    case that produces a real browser warning is an actual invalid/expired
    SSL certificate. Missing security headers, missing schema, etc. are
    never visible to an ordinary visitor, so any such claim for those is
    always false and must be blocked regardless of what the model wrote."""
    if not message:
        return False
    ssl_actually_invalid = (r.get("ssl_valid") is False)
    if ssl_actually_invalid:
        return False  # a real cert problem legitimately CAN produce a browser warning
    lower = message.lower()
    return any(phrase in lower for phrase in FALSE_SECURITY_SYMPTOM_PHRASES)


def generate_ai_outreach(r: dict) -> dict:
    """Calls Groq to write a unique, structured outreach message for this
    specific lead, then runs it through message_makes_false_security_claim()
    as a hard code-level check (not just a prompt instruction). If Groq's
    output fails the check, or fails to parse, or fails twice on the API,
    falls back to the local rule-based template, which is built to never
    make a false visible-symptom claim."""
    prompt = build_outreach_user_prompt(r)

    raw, err = _call_groq_with_system(GROQ_MODEL_PRIMARY, OUTREACH_SYSTEM_PROMPT, prompt, temperature=0.6)
    if raw is None:
        logger.warning(f"   outreach primary model failed ({err}) - trying fallback model")
        raw, err = _call_groq_with_system(GROQ_MODEL_FALLBACK, OUTREACH_SYSTEM_PROMPT, prompt, temperature=0.6)

    parsed = parse_outreach_json(raw) if raw else None

    if not parsed:
        logger.warning(f"   outreach generation failed for '{r.get('business_name')}' ({err}) - using local fallback")
        return _local_fallback_outreach(r)

    candidate_message = parsed.get("message")

    if message_makes_false_security_claim(candidate_message, r):
        logger.warning(
            f"   outreach for '{r.get('business_name')}' claimed a false visible "
            f"security symptom (ssl_valid={r.get('ssl_valid')}) - discarding AI "
            f"message and using verified local fallback instead"
        )
        return _local_fallback_outreach(r)

    return {
        **r,
        "outreach_subject": parsed.get("subject") or f"Quick idea for {r.get('business_name')}",
        "outreach_message": candidate_message,
    }


def _pick_fallback_problem(r):
    if not r.get("has_chatbot") and not r.get("has_whatsapp"):
        return {
            "weakness": "there's no way for a visitor to get an instant answer outside business hours",
            "scenario": "at night or on a weekend, looking for your service",
            "friction": "there's no one to answer, so they get no reply",
            "fix": "answer questions and capture enquiries instantly",
        }
    if not r.get("has_lead_form"):
        return {
            "weakness": "there's no way to leave contact details without calling first",
            "scenario": "browsing your site, not ready to call yet",
            "friction": "they can't leave their number anywhere on the page",
            "fix": "capture enquiries even when someone isn't ready to call",
        }
    if not r.get("has_phone_cta"):
        return {
            "weakness": "there's no clear call button, especially on mobile",
            "scenario": "on their phone, looking to contact you quickly",
            "friction": "they have to hunt for a number instead of tapping to call",
            "fix": "make it effortless to reach you, and follow up if they don't",
        }
    if not r.get("has_appointment_booking"):
        return {
            "weakness": "there's no way to book an appointment online",
            "scenario": "after you've closed for the day",
            "friction": "they can't book anything and have to remember to call tomorrow",
            "fix": "let people book automatically, any time of day",
        }
    if not r.get("has_trust_signals"):
        return {
            "weakness": "the site doesn't show any reviews or credentials up front",
            "scenario": "comparing a few options they haven't used before",
            "friction": "they have nothing on the page to reassure them you're established",
            "fix": "surface trust signals and answer follow-up questions instantly",
        }

    mistakes_text_lower = (r.get("mistakes_text") or "").lower()

    # honest, non-visible framing for known backend-only issues — never claim
    # a visitor "sees" or "gets a warning" for these
    if "security header" in mistakes_text_lower:
        return {
            "weakness": "the site is missing some standard security protections",
            "scenario": "browsing normally, with no visible sign anything is wrong",
            "friction": "the risk is invisible to them, but it leaves the site more "
                        "exposed to attacks and can fail security checks some "
                        "partners or corporate clients run before working with you",
            "fix": "add the missing protections so the site passes those checks cleanly",
        }
    if "schema" in mistakes_text_lower or not r.get("has_local_schema"):
        return {
            "weakness": "the site is missing structured data that helps it show up in local search",
            "scenario": "searching Google or Maps for your service nearby",
            "friction": "your business is less likely to appear prominently, so they "
                        "never even reach your site",
            "fix": "add the missing local search markup to improve that visibility",
        }
    if "ssl" in mistakes_text_lower and not r.get("ssl_valid", True):
        return {
            "weakness": "the site doesn't have a valid SSL certificate",
            "scenario": "visiting the site on any modern browser",
            "friction": "they see an actual 'Not Secure' warning before the page even loads",
            "fix": "fix the certificate so visitors see a secure, trustworthy site",
        }

    top_issue = (r.get("mistakes_text") or "").split(";")[0].strip().lower() \
        or "a slower response setup than visitors expect"
    return {
        "weakness": top_issue,
        "scenario": "using the site normally",
        "friction": "it quietly costs you enquiries without being obvious to a casual visitor",
        "fix": "fix the underlying issue and add a faster way to respond and follow up",
    }


def _local_fallback_outreach(r: dict) -> dict:
    """Same 6-beat scenario shape as the AI version, built with simple
    rule-based branching — used only if Groq fails twice or fails the
    validator. Includes its own defensive check against the exact class
    of bug this whole layer exists to prevent."""
    business_name = r.get("business_name") or r.get("title") or "your business"
    p = _pick_fallback_problem(r)
    message = (
        f"Hi there,\n"
        f"I was looking at {business_name} and noticed {p['weakness']}.\n"
        f"Here's a quick scenario: someone visits your website {p['scenario']}. "
        f"They're interested, but {p['friction']}, so they leave and a competitor "
        f"who makes it easier gets them instead.\n"
        f"I build simple AI + WhatsApp systems that {p['fix']}.\n"
        f"Would you like a 60-second example of how this could work for {business_name}?"
    )

    if message_makes_false_security_claim(message, r):
        # Should never happen — every branch in _pick_fallback_problem() is
        # written to avoid this — but if a future edit breaks that, fall
        # back to the one framing that's always true regardless of the
        # specific issue: a slower response setup than visitors expect.
        logger.error(
            f"   local fallback for '{business_name}' unexpectedly failed its own "
            f"safety check - using generic safe framing instead"
        )
        message = (
            f"Hi there,\n"
            f"I was looking at {business_name} and noticed a few things on the "
            f"site that are likely costing you enquiries without being obvious "
            f"day to day.\n"
            f"A visitor looking for your service outside business hours has no "
            f"quick way to get an answer, so they often end up going with "
            f"whichever business responds first.\n"
            f"I build simple AI + WhatsApp systems that reply instantly and "
            f"capture the enquiry automatically, even when your team is offline.\n"
            f"Would you like a 60-second example of how this could work for {business_name}?"
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
        "outreach_subject": None,
        "outreach_message": None,
        "audit_summary": parsed.get("audit_summary", r["mistakes_text"]),
        "ai_parse_failed": False,
    }

    # Generate a UNIQUE, problem-specific outreach message for qualified
    # leads only — this is a separate Groq call from qualification, using
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
        if AI_PROVIDER != "ollama" and sleep_between_ai_calls > 0:
            # With a single key this paces every call to respect rate
            # limits. With multiple keys on separate accounts you can
            # usually set --sleep much lower (or 0) since 429s are
            # absorbed by rotating to another key instead of sleeping.
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
    if AI_PROVIDER != "ollama" and not GROQ_API_KEYS:
        print("\nNo Groq API key(s) found (GROQ_API_KEYS or GROQ_API_KEY, env var or .env file).")
        continue_without_ai = prompt_yes_no(
            "Continue WITHOUT AI qualification (mechanics-only run)?", default=False
        )
        if not continue_without_ai:
            print("Add GROQ_API_KEYS=key1,key2,key3 (or GROQ_API_KEY=...) to a .env file next to this script, then run again.")
            sys.exit(1)
        use_ai = False

    if use_ai and AI_PROVIDER != "ollama" and len(GROQ_API_KEYS) > 1:
        print(f"\nUsing {len(GROQ_API_KEYS)} Groq API keys with automatic rotation.")
        default_sleep = 1.0
    else:
        default_sleep = 6.0

    return {
        "input_csv": input_csv,
        "output_csv": output_csv,
        "use_ai": use_ai,
        "sleep": default_sleep,
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
    parser.add_argument("--sleep", type=float, default=6.0,
                         help="Seconds between AI calls (default 6.0). With multiple Groq "
                              "keys from separate accounts you can usually lower this a lot.")
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

    if cfg["use_ai"] and AI_PROVIDER != "ollama" and not GROQ_API_KEYS:
        logger.error("No Groq API key(s) found (GROQ_API_KEYS or GROQ_API_KEY, env var or .env file). "
                      "Use --no-ai to test without it, set AI_PROVIDER=ollama for local, "
                      "or add GROQ_API_KEYS=key1,key2,key3 to a .env file next to this script.")
        sys.exit(1)

    if cfg["use_ai"] and AI_PROVIDER != "ollama":
        logger.info(f"Groq keys configured: {len(GROQ_API_KEYS)}"
                     + (" (rotation enabled)" if len(GROQ_API_KEYS) > 1 else ""))

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