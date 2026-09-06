"""
Website Quality & Mistake Auditor (v2)
----------------------------------------------------------------------------
Audits a URL for performance (Lighthouse via Docker, optional), technical
SEO, security, mobile/UX proxies, conversion/automation opportunities,
local-business schema, and broken internal links.

Unlike v1, this version follows a client-acquisition-audit spec:
  - TWO separate scores instead of one blended score:
      * WEBSITE HEALTH SCORE (/100)      — performance, SEO, accessibility,
        security, technical health, local schema. A technically excellent
        site is never punished for lacking AI/automation.
      * AUTOMATION & CONVERSION OPPORTUNITY SCORE (/100) — chatbot/live
        chat, lead-capture forms, WhatsApp CTA, phone CTA, appointment
        booking, trust signals, local-schema completeness.
  - Every failed check is recorded as a structured Issue (problem,
    evidence, why it matters, recommended fix, priority) instead of just
    a number, so the console/JSON/HTML reports can be turned directly
    into a client-facing proposal.
  - Basic business-info extraction (name, phone, address) from schema/
    OpenGraph/title, used to personalize outreach.
  - A handful of extra technical/SEO/UX checks that are practical to do
    from static HTML: robots.txt, sitemap.xml, favicon, mixed content,
    HTTP->HTTPS redirect, Open Graph tags, noindex, appointment-booking
    widgets, trust-signal keywords/schema.
  - Anything that genuinely requires human/visual judgement (first
    impression, visual hierarchy, competitor comparison, real screenshots)
    is explicitly listed as "needs manual/AI-vision review" rather than
    silently skipped or faked.

No API keys required. Lighthouse is optional (needs Docker); everything
else is plain Python (requests + BeautifulSoup). Playwright is also
optional but strongly recommended: many modern brochure/booking sites
(React, Vue, Wix, Squarespace-with-JS-widgets, etc.) inject their real
content with JavaScript, which `requests` can't execute — without
Playwright, such a site will falsely report a missing title, no forms,
no links, no chatbot, etc., because the raw HTML really is an empty
shell. Install with:
    pip install playwright && playwright install chromium
If Playwright isn't installed, this script still runs fine on ordinary
server-rendered sites, and clearly flags JS-shell pages it can't read.

CLI:
    python website_audit.py https://example.com
    python website_audit.py https://example.com --runs 5 --no-desktop
    python website_audit.py https://example.com --skip-lighthouse

Run with no arguments (e.g. from an IDE's Run button) to be prompted for
a URL interactively; this mode auto-skips Lighthouse.

Library:
    from website_audit import WebsiteAuditor, AuditConfig
    result = WebsiteAuditor(AuditConfig(url="https://example.com")).run()
    print(result.health_score, result.opportunity_score, result.issues)
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import socket
import ssl
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry
from requests.structures import CaseInsensitiveDict

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # optional dependency — see _fetch_html_rendered
    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger("website_auditor")


# =============================================================================
# CONSTANTS
# =============================================================================

DEFAULT_LIGHTHOUSE_RUNS = 3            # Lighthouse is noisy; 3-5 is standard
DEFAULT_MAX_LINKS = 25                 # cap on internal links crawled
DEFAULT_TIMEOUT = 12                   # seconds, per HTTP request
DEFAULT_LINK_DELAY = 0.6               # seconds between internal link checks
DEFAULT_RATE_LIMIT_RETRY_DELAY = 4.0   # seconds before retrying a 429

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

BLOCK_PAGE_SIGNATURES = (
    "country blocked", "access denied", "attention required",
    "checking your browser", "just a moment", "cf-error-details",
    "please verify you are a human", "request blocked",
    "you have been blocked", "sorry, you have been blocked",
    "ddos protection by", "this website is using a security service",
    "blocked by geolocation", "geo restricted", "geoblocked",
    "unusual traffic from your computer network",
    # Generic "interstitial / verification" splash pages — not always a
    # bot-block in the security-vendor sense, but equally not the real
    # site content, and equally invisible to a plain HTTP GET if the
    # actual redirect only happens via JavaScript or a manual click.
    "verifying your browser", "verifying you are human", "verifying your connection",
    "please wait while we verify", "please enable javascript to continue",
    "redirecting you", "you will be redirected", "you are being redirected",
    "click here if you are not redirected", "one moment please",
    "please wait...", "loading, please wait",
)

# Matches <meta http-equiv="refresh" content="3;url=/real-page"> style
# redirects, which a browser follows automatically but requests does not.
META_REFRESH_PATTERN = re.compile(
    r'<meta[^>]+http-equiv=["\']refresh["\'][^>]*content=["\'][^"\']*url=([^"\';]+)', re.IGNORECASE
)

# Inline "window.location = ..." / "location.replace(...)" redirects —
# another common way an interstitial/verification page sends a real
# browser onward, invisible to a plain HTTP GET.
JS_REDIRECT_PATTERN = re.compile(r'(?:window\.location(?:\.href)?\s*=|location\.replace\s*\()', re.IGNORECASE)


def extract_meta_refresh_target(html: str, base_url: str) -> str | None:
    match = META_REFRESH_PATTERN.search(html)
    if not match:
        return None
    target = match.group(1).strip().strip('"\'')
    return urljoin(base_url, target) if target else None

CHATBOT_SIGNATURES: dict[str, tuple[str, ...]] = {
    "Intercom": ("widget.intercom.io", "intercomcdn.com"),
    "Drift": ("js.driftt.com", "drift.com/api"),
    "Tidio": ("code.tidio.co",),
    "Zendesk Chat": ("ekr.zdassets.com", "zopim.com"),
    "LiveChat": ("cdn.livechatinc.com",),
    "Crisp": ("client.crisp.chat",),
    "Tawk.to": ("embed.tawk.to",),
    "HubSpot Chat": ("js.hs-scripts.com", "js.usemessages.com"),
    "Freshchat": ("wchat.freshchat.com", "freshchat.com"),
    "Olark": ("static.olark.com",),
    "ManyChat": ("widget.manychat.com",),
    "Chatbot.com": ("chatbot.com/widget",),
    "Facebook Messenger Plugin": ("connect.facebook.net", "fb-customerchat"),
    "Custom GPT/AI widget (generic)": ("chatgpt", "openai", "ai-chat", "aichat", "chatbot-widget"),
}

# Kept separate from CHATBOT_SIGNATURES: WhatsApp is a conversion CTA in its
# own right even when it isn't functioning as a "chatbot".
WHATSAPP_SIGNATURES = ("wa.me/", "api.whatsapp.com/send")

APPOINTMENT_BOOKING_SIGNATURES = (
    "calendly.com", "acuityscheduling.com", "squareup.com/appointments",
    "booksy.com", "setmore.com", "appointlet.com", "simplybook.me",
    "vagaro.com", "fresha.com", "book-now", "book an appointment",
    "schedule an appointment", "schedule a consultation",
)

TRUST_SIGNAL_KEYWORDS = (
    "testimonial", "testimonials", "review", "reviews", "verified by",
    "as seen in", "5 star", "5-star", "★★★★★", "trusted by",
)

SECURITY_HEADERS_CHECKED = (
    "Strict-Transport-Security",
    "Content-Security-Policy",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Permissions-Policy",
)

LOCAL_BUSINESS_SCHEMA_TYPES = (
    "LocalBusiness", "Dentist", "MedicalBusiness", "MedicalOrganization",
    "Restaurant", "Store", "Organization",
)

PHONE_PATTERN = re.compile(r"(\+?\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{3,4}")

# --- Scoring: two independent scores, per the audit spec -------------------
# A technically excellent site must never lose HEALTH points for lacking
# automation, and a site full of AI widgets must never gain HEALTH points
# for it either — that's what OPPORTUNITY is for.

HEALTH_WEIGHTS = {
    "performance": 20,
    "seo_technical": 20,
    "accessibility": 15,
    "ux_technical": 15,
    "security": 10,
    "technical_health": 10,
    "local_schema": 10,
}
MAX_HEALTH_SCORE = sum(HEALTH_WEIGHTS.values())  # 100

OPPORTUNITY_WEIGHTS = {
    "ai_chatbot_or_livechat": 25,
    "lead_capture_form": 15,
    "whatsapp_cta": 15,
    "phone_cta": 10,
    "appointment_booking": 15,
    "trust_signals": 10,
    "local_schema_completeness": 10,
}
MAX_OPPORTUNITY_SCORE = sum(OPPORTUNITY_WEIGHTS.values())  # 100

# HEALTH categories that come purely from Lighthouse; "untested" (excluded
# from the health total) rather than silently scored as 0 when Lighthouse
# doesn't run.
LIGHTHOUSE_ONLY_CATEGORIES = ("performance", "accessibility")

# Things the spec asks for that genuinely need a human or a vision-capable
# model looking at rendered pages/screenshots/competitors — never faked.
MANUAL_REVIEW_ITEMS = (
    "First impression / visual hierarchy",
    "Branding and image quality",
    "CTA placement and clarity (visual, above/below the fold)",
    "Mobile layout rendering (overflow, spacing, sticky elements)",
    "Navigation and menu usability as experienced by a real visitor",
    "Competitor / modern-design comparison",
    "Screenshots of concrete problems",
)


# =============================================================================
# CONFIG / RESULT TYPES
# =============================================================================

@dataclass
class AuditConfig:
    url: str
    lighthouse_runs: int = DEFAULT_LIGHTHOUSE_RUNS
    check_desktop: bool = True
    max_links: int = DEFAULT_MAX_LINKS
    request_timeout: int = DEFAULT_TIMEOUT
    link_check_delay: float = DEFAULT_LINK_DELAY
    rate_limit_retry_delay: float = DEFAULT_RATE_LIMIT_RETRY_DELAY
    reports_dir: str = field(default_factory=lambda: os.path.join(os.getcwd(), "reports"))
    skip_lighthouse: bool = False

    @property
    def screenshots_dir(self) -> str:
        return os.path.join(self.reports_dir, "screenshots")


@dataclass
class Issue:
    """One structured, report-ready finding: problem -> evidence -> why it
    matters -> recommended fix -> priority. This is the shape the spec's
    client report and outreach message are built from."""
    category: str          # e.g. "SEO", "Security", "Conversion", "Performance"
    problem: str
    evidence: str
    why_it_matters: str
    fix: str
    priority: str           # CRITICAL / HIGH / MEDIUM / LOW

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class AuditResult:
    url: str

    health_score: float
    max_health_score: float
    health_breakdown: dict[str, float]
    untested_health_categories: list[str]

    opportunity_score: float
    max_opportunity_score: float
    opportunity_breakdown: dict[str, float]

    issues: list[Issue]
    manual_review_needed: list[str]

    business_info: dict[str, Any]
    lighthouse_mobile: dict[str, Any] | None
    lighthouse_desktop: dict[str, Any] | None
    ssl_info: dict[str, Any]
    security_headers: dict[str, bool]
    mixed_content: dict[str, Any]
    http_to_https_redirect: bool | None
    robots_and_sitemap: dict[str, Any]
    favicon_found: bool
    open_graph: dict[str, Any]
    chatbot: dict[str, Any]
    whatsapp: dict[str, Any]
    appointment_booking: dict[str, Any]
    lead_capture_form: dict[str, Any]
    phone_cta: dict[str, Any]
    trust_signals: dict[str, Any]
    seo: dict[str, Any]
    schema: dict[str, Any]
    broken_links: dict[str, Any]
    fetch_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["issues"] = [issue.to_dict() for issue in self.issues]
        return data


# =============================================================================
# SMALL HELPERS
# =============================================================================

def normalize_url(raw: str) -> str:
    raw = raw.strip().strip('"').strip("'")
    if raw and not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    return raw


def normalize_host(netloc: str) -> str:
    return netloc.lower().removeprefix("www.")


def looks_like_block_page(title: str | None, body_text: str) -> str | None:
    haystack = f"{title or ''} {body_text[:2000]}".lower()
    for signature in BLOCK_PAGE_SIGNATURES:
        if signature in haystack:
            return f'page content matched block/challenge signature: "{signature}"'
    return None


# Anti-bot/CAPTCHA vendors leave technical fingerprints in the raw HTML
# (script src attributes, hidden div ids, cookie-check tokens) that survive
# even when the *visible* wording of the challenge page varies or hasn't
# rendered yet — a plain header retry does not get past these, since they
# require solving an actual JS/proof-of-work challenge, not just looking
# like a browser. Checking raw HTML (not just get_text) catches these.
CHALLENGE_MARKER_SIGNATURES = (
    "cdn-cgi/challenge-platform", "challenges.cloudflare.com", "cf_chl_opt",
    "cf-turnstile", "turnstile", "__cf_chl_rt_tk", "jschl_answer", "cf-please-wait",
    "captcha-delivery.com", "geo.captcha-delivery.com", "distil_r_captcha",
    "px-captcha", "akamai-bmp", "hcaptcha.com/captcha", "recaptcha/api.js",
)


def looks_like_challenge_page(raw_html: str) -> str | None:
    """Raw-HTML check (script tags, hidden markup) for known anti-bot/
    CAPTCHA vendors, independent of whatever visible text happens to be
    rendered. Complements looks_like_block_page, which only sees text."""
    haystack = raw_html[:20000].lower()
    for marker in CHALLENGE_MARKER_SIGNATURES:
        if marker in haystack:
            return f'page HTML contains a known anti-bot/CAPTCHA marker: "{marker}"'
    return None


@dataclass
class _RenderedResponse:
    """Minimal stand-in for requests.Response, populated from a headless
    browser render, so downstream checks (headers, .text, .url) don't need
    to know whether the page was fetched with requests or Playwright."""
    status_code: int
    headers: CaseInsensitiveDict
    url: str
    text: str


def looks_like_js_rendered_shell(html: str) -> bool:
    """Heuristic for 'this page's real content is injected by JavaScript
    after load, so a plain HTTP GET only sees an empty shell'. Common on
    React/Vue/Wix-style sites. We flag it when the raw HTML is non-trivial
    in size but has almost no visible text and almost no structural tags —
    a real static/SSR page essentially never looks like this."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()  # get_text() includes script/style contents otherwise
    visible_text_len = len(soup.get_text(strip=True))
    structural_tag_count = len(soup.find_all(["p", "h1", "h2", "h3", "form", "a", "li"]))
    return len(html) > 1500 and visible_text_len < 200 and structural_tag_count < 5


def needs_headless_render(html: str) -> bool:
    """True if the raw HTML is either an empty JS-rendered shell, or an
    interstitial/verification page whose only job is to redirect a real
    browser onward via inline JavaScript (window.location / location.replace).
    Meta-refresh redirects are handled separately (we just follow them),
    since those don't need a browser at all — a JS redirect does."""
    return looks_like_js_rendered_shell(html) or bool(JS_REDIRECT_PATTERN.search(html))


def build_requests_session(timeout: int) -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": USER_AGENT})

    original_request = session.request

    def request_with_default_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(*args, **kwargs)

    session.request = request_with_default_timeout  # type: ignore[method-assign]
    return session


# =============================================================================
# AUDITOR
# =============================================================================

class WebsiteAuditor:
    """Runs a full audit against a single URL and returns a scored,
    reportable AuditResult. Safe to import and call from another pipeline —
    all diagnostics go through `logger`, never `print()`."""

    def __init__(self, config: AuditConfig):
        self.config = config
        self.session = build_requests_session(config.request_timeout)
        self.issues: list[Issue] = []

    def _add_issue(self, category: str, problem: str, evidence: str,
                    why_it_matters: str, fix: str, priority: str) -> None:
        self.issues.append(Issue(category, problem, evidence, why_it_matters, fix, priority))

    # -- Orchestration -----------------------------------------------------

    def run(self) -> AuditResult:
        cfg = self.config
        logger.info("Auditing: %s", cfg.url)

        lh_mobile, lh_desktop = self._run_lighthouse_if_available()

        logger.info("Running HTML-level checks (SSL, headers, chatbot, SEO, schema, links)...")
        response, fetch_warning = self._fetch_html()
        soup = BeautifulSoup(response.text, "html.parser")

        ssl_info = self._check_ssl()
        headers = self._check_security_headers(response)
        mixed_content = self._check_mixed_content(response.text)
        http_redirect = self._check_http_to_https_redirect()
        robots_sitemap = self._check_robots_and_sitemap()
        favicon_found = self._check_favicon(soup)
        open_graph = self._check_open_graph(soup)
        chatbot = self._detect_ai_chatbot(response.text)
        whatsapp = self._detect_whatsapp(response.text)
        appointment = self._detect_appointment_booking(response.text)
        lead_form = self._detect_lead_capture_form(soup)
        phone_cta = self._detect_phone_cta(soup)
        seo = self._check_technical_seo(soup)
        schema, schema_business_info = self._check_local_business_schema(soup)
        trust_signals = self._detect_trust_signals(soup, schema)
        links = self._crawl_internal_links(soup)
        business_info = self._extract_business_info(soup, seo, open_graph, schema_business_info)

        untested_health = list(LIGHTHOUSE_ONLY_CATEGORIES) if lh_mobile is None else []
        health_breakdown = self._score_health(
            lh_mobile, lh_desktop, ssl_info, headers, mixed_content, http_redirect,
            robots_sitemap, favicon_found, seo, schema, links,
        )
        opportunity_breakdown = self._score_opportunity(
            chatbot, lead_form, whatsapp, phone_cta, appointment, trust_signals, schema,
        )

        health_total = round(sum(health_breakdown.values()), 1)
        health_max = MAX_HEALTH_SCORE - sum(HEALTH_WEIGHTS[c] for c in untested_health)
        opportunity_total = round(sum(opportunity_breakdown.values()), 1)

        return AuditResult(
            url=cfg.url,
            health_score=health_total,
            max_health_score=health_max,
            health_breakdown=health_breakdown,
            untested_health_categories=untested_health,
            opportunity_score=opportunity_total,
            max_opportunity_score=MAX_OPPORTUNITY_SCORE,
            opportunity_breakdown=opportunity_breakdown,
            issues=self.issues,
            manual_review_needed=list(MANUAL_REVIEW_ITEMS),
            business_info=business_info,
            lighthouse_mobile=lh_mobile,
            lighthouse_desktop=lh_desktop,
            ssl_info=ssl_info,
            security_headers=headers,
            mixed_content=mixed_content,
            http_to_https_redirect=http_redirect,
            robots_and_sitemap=robots_sitemap,
            favicon_found=favicon_found,
            open_graph=open_graph,
            chatbot=chatbot,
            whatsapp=whatsapp,
            appointment_booking=appointment,
            lead_capture_form=lead_form,
            phone_cta=phone_cta,
            trust_signals=trust_signals,
            seo=seo,
            schema=schema,
            broken_links=links,
            fetch_warning=fetch_warning,
        )

    # -- Lighthouse ----------------------------------------------------------

    @staticmethod
    def _docker_available() -> bool:
        try:
            subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)
            return True
        except Exception:
            return False

    def _run_lighthouse_if_available(self) -> tuple[dict | None, dict | None]:
        if self.config.skip_lighthouse:
            return None, None

        if not self._docker_available():
            logger.warning(
                "Docker isn't running/installed — skipping Lighthouse "
                "(performance/accessibility will be marked untested, not scored as 0). "
                "Pass --skip-lighthouse to silence this check next time."
            )
            return None, None

        mobile = self._run_lighthouse_profile("mobile")
        desktop = self._run_lighthouse_profile("desktop") if self.config.check_desktop else None
        return mobile, desktop

    def _run_lighthouse_once(self, form_factor: str, run_index: int) -> dict | None:
        os.makedirs(self.config.reports_dir, exist_ok=True)
        out_name = f"report_{form_factor}_{run_index}.json"
        out_path = os.path.join(self.config.reports_dir, out_name)

        command = [
            "docker", "run", "--rm",
            "--shm-size=1gb",
            "-v", f"{self.config.reports_dir}:/home/chrome/report",
            "femtopixel/google-lighthouse",
            self.config.url,
            "--output=json",
            f"--output-path=/home/chrome/report/{out_name}",
            "--chrome-flags=--disable-dev-shm-usage --no-sandbox",
        ]
        if form_factor == "desktop":
            command.append("--preset=desktop")

        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Lighthouse run %d (%s) failed: %s",
                run_index, form_factor, (exc.stderr or "")[-500:],
            )
            return None
        except FileNotFoundError:
            logger.error("Docker not found — this shouldn't happen after _docker_available() passed.")
            return None

        if not os.path.exists(out_path):
            logger.warning("Report file not created for run %d (%s).", run_index, form_factor)
            return None

        with open(out_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _run_lighthouse_profile(self, form_factor: str) -> dict | None:
        runs = self.config.lighthouse_runs
        logger.info("Running Lighthouse (%s) — %d run(s)...", form_factor, runs)

        reports = [
            report for i in range(1, runs + 1)
            if (report := self._run_lighthouse_once(form_factor, i)) is not None
        ]
        if not reports:
            return None

        medians = self._median_scores(reports)
        best_report = self._pick_representative_report(reports, medians)
        audits = best_report.get("audits", {})

        return {
            "form_factor": form_factor,
            "runs_completed": len(reports),
            "scores_median": medians,
            "key_metrics": self._extract_key_metrics(audits),
            "opportunities": self._extract_opportunities(audits),
            "screenshot": self._save_screenshot(audits, form_factor),
        }

    @staticmethod
    def _median_scores(reports: list[dict]) -> dict[str, float | None]:
        def scores_for(category: str) -> list[float]:
            return [
                r["categories"][category]["score"] * 100
                for r in reports
                if r.get("categories", {}).get(category, {}).get("score") is not None
            ]

        return {
            category: round(statistics.median(vals), 1) if (vals := scores_for(category)) else None
            for category in ("performance", "accessibility", "best-practices", "seo")
        }

    @staticmethod
    def _pick_representative_report(reports: list[dict], medians: dict[str, float | None]) -> dict:
        target = medians.get("performance")
        if target is None:
            return reports[0]

        def distance(report: dict) -> float:
            score = report.get("categories", {}).get("performance", {}).get("score")
            return abs(score * 100 - target) if score is not None else float("inf")

        return min(reports, key=distance)

    @staticmethod
    def _extract_key_metrics(audits: dict) -> dict[str, str]:
        metric_keys = (
            "first-contentful-paint", "largest-contentful-paint", "speed-index",
            "total-blocking-time", "cumulative-layout-shift", "interactive",
        )
        return {
            audits[key]["title"]: audits[key].get("displayValue")
            for key in metric_keys if key in audits
        }

    @staticmethod
    def _extract_opportunities(audits: dict) -> list[str]:
        return [
            f"{audit.get('title')}: {audit.get('displayValue')}"
            for audit in audits.values()
            if audit.get("score") is not None and audit["score"] < 0.9 and audit.get("displayValue")
        ]

    def _save_screenshot(self, audits: dict, form_factor: str) -> str | None:
        data_url = audits.get("final-screenshot", {}).get("details", {}).get("data")
        if not data_url:
            return None

        os.makedirs(self.config.screenshots_dir, exist_ok=True)
        path = os.path.join(self.config.screenshots_dir, f"screenshot_{form_factor}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(data_url.split(",", 1)[-1]))
        return path

    def _record_lighthouse_issues(self, lh_mobile: dict | None) -> None:
        if not lh_mobile:
            return
        perf = lh_mobile["scores_median"].get("performance")
        if perf is not None and perf < 50:
            self._add_issue(
                category="Performance",
                problem="Mobile page-load performance is poor.",
                evidence=f"Lighthouse mobile performance score: {perf}/100.",
                why_it_matters="Slow mobile load times increase bounce rate and lose visitors "
                               "before they ever see the offer, and factor into Google's mobile ranking.",
                fix="Optimize images, defer non-critical JS/CSS, and enable caching/compression.",
                priority="CRITICAL",
            )
        elif perf is not None and perf < 90:
            self._add_issue(
                category="Performance",
                problem="Mobile page-load performance has room to improve.",
                evidence=f"Lighthouse mobile performance score: {perf}/100.",
                why_it_matters="Every extra second of load time can measurably reduce conversions.",
                fix="Address the top Lighthouse opportunities (images, render-blocking resources).",
                priority="MEDIUM",
            )
        for issue_text in lh_mobile.get("opportunities", [])[:5]:
            self._add_issue(
                category="Performance",
                problem=issue_text.split(":")[0],
                evidence=issue_text,
                why_it_matters="Contributes to the slower mobile load time flagged above.",
                fix="See Lighthouse's guidance for this specific audit.",
                priority="MEDIUM",
            )

    # -- Fetching --------------------------------------------------------------

    def _fetch_html(self) -> tuple[requests.Response | _RenderedResponse, str | None]:
        """Fetch the page, then sanity-check what we got before running any
        checks against it. Several failure modes are handled differently:
          - Block/challenge page (Cloudflare etc., or an interstitial page
            whose visible text matches a known "verifying.../redirecting..."
            phrase) -> retry once with fuller browser-like headers.
          - Meta-refresh redirect (<meta http-equiv="refresh" ...>) -> just
            follow it ourselves; a browser does this automatically but
            requests does not.
          - JS-rendered shell OR an inline JS redirect (window.location=...,
            location.replace(...)) -> retry with a real headless browser via
            Playwright, if installed, since no amount of header-tweaking
            makes `requests` execute JavaScript.
        This matters because every downstream check (title, forms, links,
        chatbot, schema...) reads from this HTML; fetching an interstitial
        or a blank shell silently produces a report full of false "missing"
        findings instead of auditing the real site.
        """
        response, warning = self._fetch_html_static()

        if warning:
            if PLAYWRIGHT_AVAILABLE:
                logger.warning(
                    "Static fetch still looks blocked/challenged (%s) — trying a real headless "
                    "browser, since some JS-based challenges auto-resolve for a genuine browser "
                    "engine but never can for `requests`...", warning,
                )
                rendered = self._fetch_html_rendered()
                if rendered is not None:
                    rendered_warning = self._detect_block_page(rendered) or (
                        "rendered page still looks empty/shell-like" if looks_like_js_rendered_shell(rendered.text) else None
                    )
                    if not rendered_warning:
                        logger.info("Headless-browser render got past it — using the rendered HTML.")
                        return rendered, None
                return response, (
                    f"{warning}. A headless-browser retry did not get past it either — this site "
                    "likely requires solving a human-facing CAPTCHA/verification step, which this "
                    "tool does not attempt to bypass. Verify this lead manually in a real browser."
                )
            return response, (
                f"{warning}. Install Playwright (`pip install playwright && playwright install "
                "chromium`) — some JS-based challenges auto-resolve for a real browser engine but "
                "never can for a plain HTTP request. If it still doesn't resolve, this site likely "
                "requires a human-facing CAPTCHA, which this tool does not attempt to bypass."
            )

        refresh_target = extract_meta_refresh_target(response.text, response.url)
        if refresh_target and normalize_host(urlparse(refresh_target).netloc) == \
                normalize_host(urlparse(response.url).netloc):
            logger.info("Meta-refresh redirect detected — following to %s", refresh_target)
            try:
                followed = self.session.get(refresh_target, allow_redirects=True)
                if not self._detect_block_page(followed):
                    response = followed
            except requests.RequestException as exc:
                logger.warning("Failed to follow meta-refresh redirect: %s", exc)

        if needs_headless_render(response.text):
            logger.warning(
                "Raw HTML looks like an interstitial/verification page or a JS-rendered shell — "
                "the real site content likely isn't visible to a plain HTTP GET."
            )
            if PLAYWRIGHT_AVAILABLE:
                rendered = self._fetch_html_rendered()
                if rendered is not None and not looks_like_js_rendered_shell(rendered.text):
                    logger.info("Headless-browser render succeeded — using the fully rendered HTML.")
                    return rendered, None
                return response, (
                    "page appears to be an interstitial/verification page or renders its content via "
                    "JavaScript, and the headless-browser re-fetch did not return meaningfully more "
                    "content — checks below likely show false negatives (missing title/forms/links "
                    "etc. that a browser would actually see)"
                )
            return response, (
                "page appears to be an interstitial/verification page or renders its content via "
                "JavaScript (raw HTML has almost no real text, or contains a script-based redirect) — "
                "install Playwright (`pip install playwright && playwright install chromium`) for "
                "accurate results; checks below likely show false negatives"
            )

        return response, None

    def _fetch_html_static(self) -> tuple[requests.Response, str | None]:
        response = self.session.get(self.config.url, allow_redirects=True)
        warning = self._detect_block_page(response)

        if warning:
            logger.warning("First fetch looks blocked (%s) — retrying with browser headers...", warning)
            try:
                retry_response = self.session.get(
                    self.config.url, headers=BROWSER_HEADERS, allow_redirects=True
                )
                retry_warning = self._detect_block_page(retry_response)
                if not retry_warning:
                    logger.info("Retry succeeded — got real content on second attempt.")
                    return retry_response, None
                return retry_response, retry_warning
            except requests.RequestException as exc:
                logger.warning("Retry with browser headers failed: %s", exc)

        return response, warning

    def _fetch_html_rendered(self) -> _RenderedResponse | None:
        """Render the page with a real (headless) browser via Playwright,
        so JS-injected content shows up in the HTML we hand to BeautifulSoup.
        Returns None (never raises) if Playwright isn't usable — callers
        fall back to the static fetch and surface a clear warning instead."""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    page = browser.new_page(user_agent=USER_AGENT)
                    response = page.goto(
                        self.config.url, wait_until="networkidle",
                        timeout=self.config.request_timeout * 1000,
                    )
                    content = page.content()
                    final_url = page.url
                    status = response.status if response else 200
                    headers = response.headers if response else {}
                finally:
                    browser.close()
            return _RenderedResponse(
                status_code=status, headers=CaseInsensitiveDict(headers), url=final_url, text=content,
            )
        except Exception as exc:  # noqa: BLE001 - optional path, never fatal
            logger.warning("Headless-browser render failed (%s) — falling back to static HTML.", exc)
            return None

    @staticmethod
    def _detect_block_page(response: requests.Response) -> str | None:
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else None
        return (
            looks_like_block_page(title, soup.get_text(" ", strip=True))
            or looks_like_challenge_page(response.text)
        )

    # -- Individual checks -------------------------------------------------

    def _check_ssl(self) -> dict[str, Any]:
        hostname = urlparse(self.config.url).hostname
        result: dict[str, Any] = {
            "valid": False, "issuer": None, "expires": None,
            "days_left": None, "protocol": None,
        }
        try:
            context = ssl.create_default_context()
            with socket.create_connection((hostname, 443), timeout=self.config.request_timeout) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as tls_sock:
                    cert = tls_sock.getpeercert()
                    result["protocol"] = tls_sock.version()

                    issuer = dict(x[0] for x in cert.get("issuer", []))
                    result["issuer"] = issuer.get("organizationName") or issuer.get("commonName")

                    expires = datetime.strptime(
                        cert["notAfter"], "%b %d %H:%M:%S %Y %Z"
                    ).replace(tzinfo=timezone.utc)
                    result["expires"] = expires.strftime("%Y-%m-%d")
                    result["days_left"] = (expires - datetime.now(timezone.utc)).days
                    result["valid"] = result["days_left"] > 0
        except Exception as exc:  # noqa: BLE001 - broad on purpose, diagnostic check
            result["error"] = str(exc)

        if not result["valid"]:
            self._add_issue(
                category="Security",
                problem="SSL certificate is missing, invalid, or expired.",
                evidence=result.get("error") or f"days_left={result.get('days_left')}",
                why_it_matters="Browsers show a 'Not Secure' warning, which destroys trust instantly "
                               "and can block the site from ranking well.",
                fix="Install/renew a valid SSL certificate (e.g. via Let's Encrypt or the host's SSL tool).",
                priority="CRITICAL",
            )
        elif result["days_left"] is not None and result["days_left"] < 21:
            self._add_issue(
                category="Security",
                problem="SSL certificate is expiring soon.",
                evidence=f"{result['days_left']} days left (expires {result['expires']}).",
                why_it_matters="An expired certificate will suddenly block visitors with a security warning.",
                fix="Renew the certificate now, ideally with auto-renewal enabled.",
                priority="HIGH",
            )
        return result

    def _check_security_headers(self, response: requests.Response) -> dict[str, bool]:
        headers = {header: header in response.headers for header in SECURITY_HEADERS_CHECKED}
        missing = [h for h, present in headers.items() if not present]
        if missing:
            priority = "HIGH" if "Strict-Transport-Security" in missing or "Content-Security-Policy" in missing else "MEDIUM"
            self._add_issue(
                category="Security",
                problem="Missing recommended security headers.",
                evidence=f"Missing: {', '.join(missing)}.",
                why_it_matters="These headers protect visitors from clickjacking, MIME-sniffing, and "
                               "injection attacks, and are checked by some security-conscious visitors/tools.",
                fix="Add the missing headers at the web server or CDN level.",
                priority=priority,
            )
        return headers

    def _check_mixed_content(self, html: str) -> dict[str, Any]:
        is_https = self.config.url.lower().startswith("https://")
        insecure_refs = re.findall(r'(?:src|href)=["\']http://[^"\']+', html) if is_https else []
        result = {"checked": is_https, "insecure_reference_count": len(insecure_refs),
                  "sample": insecure_refs[:5]}
        if is_https and insecure_refs:
            self._add_issue(
                category="Security",
                problem="Mixed content: some resources load over insecure HTTP on an HTTPS page.",
                evidence=f"{len(insecure_refs)} insecure reference(s) found, e.g. {insecure_refs[0]}",
                why_it_matters="Browsers may block these resources or show a 'not fully secure' warning.",
                fix="Change these resource URLs to HTTPS (or protocol-relative).",
                priority="MEDIUM",
            )
        return result

    def _check_http_to_https_redirect(self) -> bool | None:
        parsed = urlparse(self.config.url)
        if parsed.scheme != "https":
            return None
        http_url = f"http://{parsed.netloc}{parsed.path or '/'}"
        try:
            response = self.session.get(http_url, allow_redirects=True, timeout=self.config.request_timeout)
            redirected_to_https = response.url.lower().startswith("https://")
        except requests.RequestException:
            return None
        if not redirected_to_https:
            self._add_issue(
                category="Security",
                problem="HTTP does not redirect to HTTPS.",
                evidence=f"Requesting {http_url} did not land on an https:// URL.",
                why_it_matters="Visitors who type the address without 'https' (or follow an old link) "
                               "stay on an insecure connection.",
                fix="Add a server-level 301 redirect from HTTP to HTTPS.",
                priority="HIGH",
            )
        return redirected_to_https

    def _check_robots_and_sitemap(self) -> dict[str, Any]:
        parsed = urlparse(self.config.url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        result: dict[str, Any] = {"robots_txt_found": False, "sitemap_found": False, "sitemap_url": None}

        try:
            robots_resp = self.session.get(urljoin(base, "/robots.txt"))
            result["robots_txt_found"] = robots_resp.status_code == 200
            if result["robots_txt_found"]:
                match = re.search(r"(?im)^sitemap:\s*(\S+)", robots_resp.text)
                if match:
                    result["sitemap_url"] = match.group(1)
        except requests.RequestException:
            pass

        sitemap_url = result["sitemap_url"] or urljoin(base, "/sitemap.xml")
        try:
            sitemap_resp = self.session.get(sitemap_url)
            result["sitemap_found"] = sitemap_resp.status_code == 200
            result["sitemap_url"] = sitemap_url if result["sitemap_found"] else result["sitemap_url"]
        except requests.RequestException:
            pass

        if not result["robots_txt_found"]:
            self._add_issue(
                category="Technical SEO",
                problem="No robots.txt found.",
                evidence=f"GET {urljoin(base, '/robots.txt')} did not return 200.",
                why_it_matters="robots.txt helps search engines crawl the site efficiently; its absence "
                               "isn't fatal but is a quick, free fix.",
                fix="Add a simple robots.txt allowing crawlers and pointing to the sitemap.",
                priority="LOW",
            )
        if not result["sitemap_found"]:
            self._add_issue(
                category="Technical SEO",
                problem="No XML sitemap found.",
                evidence=f"Checked {sitemap_url}.",
                why_it_matters="A sitemap helps search engines discover and index all pages, especially "
                               "on smaller or newer sites with few external links.",
                fix="Generate an XML sitemap and reference it in robots.txt.",
                priority="MEDIUM",
            )
        return result

    def _check_favicon(self, soup: BeautifulSoup) -> bool:
        found = bool(soup.find("link", rel=lambda v: v and "icon" in v.lower()))
        if not found:
            parsed = urlparse(self.config.url)
            try:
                resp = self.session.get(f"{parsed.scheme}://{parsed.netloc}/favicon.ico")
                found = resp.status_code == 200 and len(resp.content) > 0
            except requests.RequestException:
                found = False
        if not found:
            self._add_issue(
                category="Technical Health",
                problem="No favicon found.",
                evidence="No <link rel=\"icon\"> tag and no /favicon.ico.",
                why_it_matters="A missing favicon looks unfinished in browser tabs, bookmarks, and search results.",
                fix="Add a favicon.ico (and modern <link rel=\"icon\"> tags) in the site's <head>.",
                priority="LOW",
            )
        return found

    def _check_open_graph(self, soup: BeautifulSoup) -> dict[str, Any]:
        tags = {
            "og:title": soup.find("meta", property="og:title"),
            "og:description": soup.find("meta", property="og:description"),
            "og:image": soup.find("meta", property="og:image"),
        }
        result = {name: bool(tag and tag.get("content")) for name, tag in tags.items()}
        if not any(result.values()):
            self._add_issue(
                category="SEO",
                problem="No Open Graph tags found.",
                evidence="og:title / og:description / og:image are all missing.",
                why_it_matters="Links shared on social media or messaging apps show a blank/ugly preview, "
                               "reducing click-through from shares.",
                fix="Add og:title, og:description, and og:image meta tags.",
                priority="LOW",
            )
        return result

    def _detect_ai_chatbot(self, html: str) -> dict[str, Any]:
        html_lower = html.lower()
        providers = [
            name for name, signatures in CHATBOT_SIGNATURES.items()
            if any(sig in html_lower for sig in signatures)
        ]
        detected = bool(providers)
        if not detected:
            self._add_issue(
                category="Automation Opportunity",
                problem="No AI chatbot or live-chat widget detected.",
                evidence="No known chat/AI widget signature found in the page HTML "
                         "(detection is limited to static HTML — a widget could still load dynamically).",
                why_it_matters="Visitors with quick questions, especially after hours, have no immediate "
                               "way to get answers, which may cost leads to a competitor who responds faster.",
                fix="Add an AI receptionist / live-chat widget to answer FAQs and qualify leads 24/7.",
                priority="HIGH",
            )
        return {"detected": detected, "providers": providers}

    def _detect_whatsapp(self, html: str) -> dict[str, Any]:
        detected = any(sig in html.lower() for sig in WHATSAPP_SIGNATURES)
        if not detected:
            self._add_issue(
                category="Automation Opportunity",
                problem="No WhatsApp click-to-chat link detected.",
                evidence="No wa.me/ or api.whatsapp.com/send link found in the page HTML.",
                why_it_matters="In many markets, WhatsApp is visitors' preferred contact channel; its "
                               "absence adds friction versus a competitor with one-tap WhatsApp contact.",
                fix="Add a WhatsApp click-to-chat button (wa.me link) near the main CTA.",
                priority="MEDIUM",
            )
        return {"detected": detected}

    def _detect_appointment_booking(self, html: str) -> dict[str, Any]:
        html_lower = html.lower()
        detected = any(sig in html_lower for sig in APPOINTMENT_BOOKING_SIGNATURES)
        if not detected:
            self._add_issue(
                category="Conversion",
                problem="No appointment/booking system detected.",
                evidence="No known booking-widget signature or 'book an appointment' phrasing found.",
                why_it_matters="Requiring visitors to call during business hours to book loses "
                               "after-hours and phone-averse visitors.",
                fix="Add an online booking widget (e.g. Calendly, Acuity, Setmore) to the site.",
                priority="MEDIUM",
            )
        return {"detected": detected}

    def _detect_lead_capture_form(self, soup: BeautifulSoup) -> dict[str, Any]:
        forms = soup.find_all("form")
        meaningful_forms = [
            f for f in forms
            if f.find(["input", "textarea"], attrs={"type": lambda t: t not in ("submit", "button", "hidden")})
            or f.find("textarea")
        ]
        detected = bool(meaningful_forms)
        field_count = None
        if meaningful_forms:
            field_count = len(meaningful_forms[0].find_all(["input", "textarea", "select"]))
        result = {"detected": detected, "form_count": len(meaningful_forms), "fields_in_first_form": field_count}
        if not detected:
            self._add_issue(
                category="Conversion",
                problem="No lead-capture form detected.",
                evidence="No <form> with meaningful input fields found on this page.",
                why_it_matters="Visitors who aren't ready to call have no low-friction way to leave "
                               "their details, so interested visitors may simply leave.",
                fix="Add a short contact/quote-request form (name, contact info, message) above the fold.",
                priority="HIGH",
            )
        elif field_count is not None and field_count > 7:
            self._add_issue(
                category="Conversion",
                problem="Lead-capture form is long.",
                evidence=f"First form has {field_count} fields.",
                why_it_matters="Long forms measurably reduce completion rates.",
                fix="Trim the form to only the fields needed for a first response.",
                priority="LOW",
            )
        return result

    def _detect_phone_cta(self, soup: BeautifulSoup) -> dict[str, Any]:
        tel_links = soup.find_all("a", href=re.compile(r"^tel:", re.IGNORECASE))
        detected = bool(tel_links)
        if not detected:
            self._add_issue(
                category="Conversion",
                problem="No clickable phone (tel:) link detected.",
                evidence="No <a href=\"tel:...\"> found.",
                why_it_matters="On mobile, a non-clickable phone number requires visitors to copy/retype "
                               "it, adding friction to the easiest possible conversion.",
                fix="Wrap the phone number in a tel: link, especially in the header/footer.",
                priority="MEDIUM",
            )
        return {"detected": detected, "count": len(tel_links)}

    def _detect_trust_signals(self, soup: BeautifulSoup, schema: dict[str, Any]) -> dict[str, Any]:
        body_text = soup.get_text(" ", strip=True).lower()
        keyword_hit = any(keyword in body_text for keyword in TRUST_SIGNAL_KEYWORDS)
        has_rating_schema = schema.get("has_aggregate_rating", False)
        detected = keyword_hit or has_rating_schema
        if not detected:
            self._add_issue(
                category="Conversion",
                problem="No visible trust signals (testimonials/reviews) detected.",
                evidence="No testimonial/review keywords and no AggregateRating schema found.",
                why_it_matters="New visitors have no social proof that other customers had a good "
                               "experience, which can slow down the decision to contact/buy.",
                fix="Add a few real customer testimonials or reviews, ideally with review schema markup.",
                priority="MEDIUM",
            )
        return {"detected": detected, "keyword_match": keyword_hit, "rating_schema_found": has_rating_schema}

    def _check_technical_seo(self, soup: BeautifulSoup) -> dict[str, Any]:
        title = soup.title.string.strip() if soup.title and soup.title.string else None
        meta_description = soup.find("meta", attrs={"name": "description"})
        description_text = meta_description.get("content", "").strip() if meta_description else None
        images = soup.find_all("img")
        images_missing_alt = [img for img in images if not img.get("alt", "").strip()]
        robots_meta = soup.find("meta", attrs={"name": "robots"})
        robots_content = robots_meta.get("content") if robots_meta else None

        result = {
            "title": title,
            "title_length": len(title) if title else 0,
            "meta_description": description_text,
            "meta_description_length": len(description_text) if description_text else 0,
            "has_viewport_tag": bool(soup.find("meta", attrs={"name": "viewport"})),
            "has_canonical_tag": bool(soup.find("link", attrs={"rel": "canonical"})),
            "h1_count": len(soup.find_all("h1")),
            "total_images": len(images),
            "images_missing_alt": len(images_missing_alt),
            "robots_meta": robots_content,
            "noindex": bool(robots_content and "noindex" in robots_content.lower()),
        }

        if not title:
            self._add_issue("SEO", "Page is missing a <title> tag.", "No <title> found.",
                             "The title is the primary text shown in search results and browser tabs.",
                             "Add a unique, descriptive <title> (roughly 10-60 characters).", "HIGH")
        elif not (10 <= result["title_length"] <= 60):
            self._add_issue("SEO", "Title tag length is outside the recommended range.",
                             f'Title is {result["title_length"]} characters: "{title}"',
                             "Titles that are too short waste an opportunity; too long get truncated in search results.",
                             "Rewrite the title to roughly 10-60 characters.", "LOW")
        if not description_text:
            self._add_issue("SEO", "Page is missing a meta description.", "No <meta name=\"description\"> found.",
                             "Search engines often show the meta description as the search-result snippet; "
                             "without one, Google picks arbitrary page text.",
                             "Write a compelling 50-160 character meta description.", "MEDIUM")
        elif not (50 <= result["meta_description_length"] <= 160):
            self._add_issue("SEO", "Meta description length is outside the recommended range.",
                             f'Description is {result["meta_description_length"]} characters.',
                             "Descriptions that are too short under-sell the page; too long get truncated.",
                             "Rewrite the meta description to roughly 50-160 characters.", "LOW")
        if not result["has_viewport_tag"]:
            self._add_issue("Mobile", "No mobile viewport meta tag.", "No <meta name=\"viewport\"> found.",
                             "Without it, mobile browsers render a desktop-width layout and zoom out, "
                             "which is a major usability failure on phones.",
                             "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">.",
                             "CRITICAL")
        if result["h1_count"] != 1:
            reason = "no H1 tag" if result["h1_count"] == 0 else f'{result["h1_count"]} H1 tags'
            self._add_issue("SEO", "Page does not have exactly one H1.", f"Found {reason}.",
                             "A single clear H1 helps both visitors and search engines understand the "
                             "page's main topic at a glance.",
                             "Use exactly one H1 that states the page's main heading.", "LOW")
        if images and (len(images_missing_alt) / len(images)) >= 0.2:
            self._add_issue("Accessibility", "Many images are missing alt text.",
                             f"{len(images_missing_alt)} of {len(images)} images have no alt attribute.",
                             "Alt text is read aloud by screen readers and used by Google Images; missing "
                             "it excludes visually-impaired visitors and loses image-search traffic.",
                             "Add descriptive alt text to all meaningful images.", "MEDIUM")
        if result["noindex"]:
            self._add_issue("SEO", "Page is set to noindex.", f'robots meta content: "{robots_content}"',
                             "A noindex tag tells search engines not to show this page in results at all — "
                             "this may be intentional, but is worth confirming.",
                             "Remove the noindex directive if this page should be discoverable in search.",
                             "CRITICAL")
        if not result["has_canonical_tag"]:
            self._add_issue("SEO", "No canonical tag found.", "No <link rel=\"canonical\"> found.",
                             "Without a canonical tag, duplicate/parameterized URLs can split ranking signals.",
                             "Add a self-referencing canonical tag to every indexable page.", "LOW")

        return result

    def _check_local_business_schema(self, soup: BeautifulSoup) -> tuple[dict[str, Any], dict[str, Any]]:
        result: dict[str, Any] = {
            "schema_found": False, "schema_types": [],
            "phone_found": False, "address_found": False,
            "has_aggregate_rating": False, "has_opening_hours": False,
        }
        business_info: dict[str, Any] = {"name": None, "telephone": None, "address": None}

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for node in self._flatten_jsonld(data):
                self._inspect_jsonld_node(node, result, business_info)

        if not result["phone_found"]:
            body_text = soup.get_text(" ", strip=True)
            if PHONE_PATTERN.search(body_text):
                result["phone_found"] = True

        if not result["schema_found"]:
            self._add_issue(
                category="Local SEO",
                problem="No LocalBusiness (or similar) structured data found.",
                evidence="No JSON-LD node with a recognized local-business @type found.",
                why_it_matters="Structured data helps Google show rich results (hours, ratings, address) "
                               "directly in search, and powers map/voice-search discovery.",
                fix="Add JSON-LD LocalBusiness schema with name, address, phone, and hours.",
                priority="MEDIUM",
            )
        elif not result["has_opening_hours"]:
            self._add_issue(
                category="Local SEO",
                problem="LocalBusiness schema is missing opening hours.",
                evidence=f"Schema types found: {result['schema_types']}, no openingHoursSpecification/openingHours.",
                why_it_matters="Opening hours in schema let Google show 'Open now' directly in search results.",
                fix="Add openingHoursSpecification to the LocalBusiness schema.",
                priority="LOW",
            )
        return result, business_info

    @staticmethod
    def _flatten_jsonld(data: Any) -> list[dict]:
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            return [node for node in data["@graph"] if isinstance(node, dict)]
        if isinstance(data, list):
            return [node for node in data if isinstance(node, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    @staticmethod
    def _inspect_jsonld_node(node: dict, result: dict[str, Any], business_info: dict[str, Any]) -> None:
        node_type = node.get("@type")
        if node_type:
            types = node_type if isinstance(node_type, list) else [node_type]
            for type_name in types:
                if any(key in str(type_name) for key in LOCAL_BUSINESS_SCHEMA_TYPES):
                    result["schema_found"] = True
                    result["schema_types"].append(type_name)
        if node.get("telephone"):
            result["phone_found"] = True
            business_info.setdefault("telephone", node["telephone"])
        if node.get("address"):
            result["address_found"] = True
            address = node["address"]
            if isinstance(address, dict):
                parts = [address.get(k) for k in
                         ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
                business_info.setdefault("address", ", ".join(p for p in parts if p))
            elif isinstance(address, str):
                business_info.setdefault("address", address)
        if node.get("name") and not business_info.get("name"):
            business_info["name"] = node["name"]
        if node.get("aggregateRating"):
            result["has_aggregate_rating"] = True
        if node.get("openingHoursSpecification") or node.get("openingHours"):
            result["has_opening_hours"] = True

    def _extract_business_info(self, soup: BeautifulSoup, seo: dict[str, Any],
                                open_graph_tags: dict[str, Any],
                                schema_business_info: dict[str, Any]) -> dict[str, Any]:
        name = schema_business_info.get("name")
        if not name:
            site_name_tag = soup.find("meta", property="og:site_name")
            if site_name_tag and site_name_tag.get("content"):
                name = site_name_tag["content"].strip()
        if not name and seo.get("title"):
            name = seo["title"].split("|")[0].split("-")[0].strip()

        return {
            "name": name,
            "telephone": schema_business_info.get("telephone"),
            "address": schema_business_info.get("address"),
        }

    def _crawl_internal_links(self, soup: BeautifulSoup) -> dict[str, Any]:
        internal_links = self._collect_internal_links(soup)

        checked, broken, rate_limited = [], [], []
        for link in internal_links:
            time.sleep(self.config.link_check_delay)
            try:
                response = self.session.get(link, allow_redirects=True)
                if response.status_code == 429:
                    time.sleep(self.config.rate_limit_retry_delay)
                    response = self.session.get(link, allow_redirects=True)

                checked.append({"url": link, "status": response.status_code})
                if response.status_code == 429:
                    rate_limited.append({"url": link, "status": response.status_code})
                elif response.status_code >= 400:
                    broken.append({"url": link, "status": response.status_code})
            except requests.RequestException as exc:
                checked.append({"url": link, "status": "error"})
                broken.append({"url": link, "status": "error", "error": str(exc)})

        if broken:
            sample = ", ".join(f"{b['url']} -> {b['status']}" for b in broken[:3])
            self._add_issue(
                category="Technical Health",
                problem="Broken internal links found.",
                evidence=f"{len(broken)} of {len(checked)} checked links are broken. e.g. {sample}",
                why_it_matters="Broken links frustrate visitors, waste crawl budget, and can pass "
                               "negative signals to search engines.",
                fix="Fix or remove the broken links, or add redirects to the correct pages.",
                priority="HIGH" if len(broken) > 2 else "MEDIUM",
            )

        return {
            "checked_count": len(checked),
            "broken": broken,
            "broken_count": len(broken),
            "rate_limited": rate_limited,
            "rate_limited_count": len(rate_limited),
        }

    def _collect_internal_links(self, soup: BeautifulSoup) -> list[str]:
        target_host = normalize_host(urlparse(self.config.url).netloc)

        links: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if href.startswith(("mailto:", "tel:", "#")):
                continue

            full_url = urljoin(self.config.url, href)
            if normalize_host(urlparse(full_url).netloc) == target_host:
                links.add(full_url)
            if len(links) >= self.config.max_links:
                break

        return list(links)

    # -- Scoring -----------------------------------------------------------

    def _score_health(self, lh_mobile, lh_desktop, ssl_info, headers, mixed_content,
                       http_redirect, robots_sitemap, favicon_found, seo, schema, links) -> dict[str, float]:
        self._record_lighthouse_issues(lh_mobile)
        return {
            "performance": self._score_performance(lh_mobile, lh_desktop),
            "accessibility": self._score_accessibility(lh_mobile),
            "seo_technical": self._score_seo(lh_mobile, seo),
            "ux_technical": self._score_ux_technical(seo),
            "security": self._score_security(ssl_info, headers, mixed_content, http_redirect),
            "technical_health": self._score_technical_health(robots_sitemap, favicon_found, links),
            "local_schema": HEALTH_WEIGHTS["local_schema"] if schema["schema_found"] else 0,
        }

    def _score_opportunity(self, chatbot, lead_form, whatsapp, phone_cta,
                            appointment, trust_signals, schema) -> dict[str, float]:
        local_completeness_pct = sum([
            schema["schema_found"], schema["phone_found"],
            schema["address_found"], schema["has_opening_hours"],
        ]) / 4
        return {
            "ai_chatbot_or_livechat": OPPORTUNITY_WEIGHTS["ai_chatbot_or_livechat"] if chatbot["detected"] else 0,
            "lead_capture_form": OPPORTUNITY_WEIGHTS["lead_capture_form"] if lead_form["detected"] else 0,
            "whatsapp_cta": OPPORTUNITY_WEIGHTS["whatsapp_cta"] if whatsapp["detected"] else 0,
            "phone_cta": OPPORTUNITY_WEIGHTS["phone_cta"] if phone_cta["detected"] else 0,
            "appointment_booking": OPPORTUNITY_WEIGHTS["appointment_booking"] if appointment["detected"] else 0,
            "trust_signals": OPPORTUNITY_WEIGHTS["trust_signals"] if trust_signals["detected"] else 0,
            "local_schema_completeness": round(local_completeness_pct * OPPORTUNITY_WEIGHTS["local_schema_completeness"], 1),
        }

    @staticmethod
    def _score_performance(lh_mobile, lh_desktop) -> float:
        weighted_scores = []
        if lh_mobile and lh_mobile["scores_median"].get("performance") is not None:
            weighted_scores.append(lh_mobile["scores_median"]["performance"] * 0.6)
        if lh_desktop and lh_desktop["scores_median"].get("performance") is not None:
            weighted_scores.append(lh_desktop["scores_median"]["performance"] * 0.4)

        if len(weighted_scores) == 2:
            performance_pct = sum(weighted_scores)
        elif weighted_scores:
            performance_pct = weighted_scores[0] / 0.6
        else:
            performance_pct = 0
        return round((performance_pct / 100) * HEALTH_WEIGHTS["performance"], 1)

    @staticmethod
    def _score_accessibility(lh_mobile) -> float:
        accessibility_pct = (lh_mobile["scores_median"].get("accessibility") if lh_mobile else 0) or 0
        return round((accessibility_pct / 100) * HEALTH_WEIGHTS["accessibility"], 1)

    @staticmethod
    def _score_seo(lh_mobile, seo: dict[str, Any]) -> float:
        lighthouse_seo_pct = (lh_mobile["scores_median"].get("seo") if lh_mobile else 0) or 0

        checks_passed = sum([
            bool(seo["title"] and 10 <= seo["title_length"] <= 60),
            bool(seo["meta_description"] and 50 <= seo["meta_description_length"] <= 160),
            seo["has_canonical_tag"],
            seo["h1_count"] == 1,
            seo["total_images"] == 0 or seo["images_missing_alt"] / seo["total_images"] < 0.2,
            not seo["noindex"],
        ])
        manual_pct = (checks_passed / 6) * 100

        combined_pct = (lighthouse_seo_pct * 0.5) + (manual_pct * 0.5)
        return round((combined_pct / 100) * HEALTH_WEIGHTS["seo_technical"], 1)

    @staticmethod
    def _score_ux_technical(seo: dict[str, Any]) -> float:
        """Only the UX signals that are actually verifiable from static
        HTML. Real UX/UI (first impression, visual hierarchy, layout on a
        rendered mobile screen) needs a human or vision model — see
        MANUAL_REVIEW_ITEMS — and is deliberately NOT scored here."""
        checks_passed = sum([
            seo["has_viewport_tag"],
            seo["h1_count"] >= 1,
        ])
        pct = (checks_passed / 2) * 100
        return round((pct / 100) * HEALTH_WEIGHTS["ux_technical"], 1)

    @staticmethod
    def _score_security(ssl_info: dict[str, Any], headers: dict[str, bool],
                         mixed_content: dict[str, Any], http_redirect: bool | None) -> float:
        checks = [ssl_info.get("valid", False), *headers.values()]
        if mixed_content.get("checked"):
            checks.append(mixed_content.get("insecure_reference_count", 0) == 0)
        if http_redirect is not None:
            checks.append(http_redirect)
        points = sum(bool(c) for c in checks)
        return round((points / len(checks)) * HEALTH_WEIGHTS["security"], 1)

    @staticmethod
    def _score_technical_health(robots_sitemap: dict[str, Any], favicon_found: bool,
                                 links: dict[str, Any]) -> float:
        broken_pct_ok = 1.0
        if links["checked_count"] > 0:
            broken_pct_ok = max(0.0, 1 - (links["broken_count"] / links["checked_count"]))
        checks_pct = statistics.mean([
            1.0 if robots_sitemap["robots_txt_found"] else 0.0,
            1.0 if robots_sitemap["sitemap_found"] else 0.0,
            1.0 if favicon_found else 0.0,
            broken_pct_ok,
        ])
        return round(checks_pct * HEALTH_WEIGHTS["technical_health"], 1)


# =============================================================================
# REPORTING (console / JSON / HTML)
# =============================================================================

PRIORITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def print_report(result: AuditResult) -> None:
    print("\n" + "=" * 70)
    print(f"WEBSITE GROWTH AUDIT — {result.url}")
    print("=" * 70)

    if result.fetch_warning:
        print(f"\n*** WARNING: {result.fetch_warning} ***")
        print("*** Results may reflect a block/challenge page, not the real site. ***")

    if result.business_info.get("name"):
        print(f"\nBusiness: {result.business_info['name']}")
    if result.business_info.get("telephone"):
        print(f"Phone: {result.business_info['telephone']}")
    if result.business_info.get("address"):
        print(f"Address: {result.business_info['address']}")

    print(f"\nWEBSITE HEALTH SCORE:        {result.health_score}/{result.max_health_score}")
    if result.untested_health_categories:
        labels = ", ".join(c.replace("_", " ").title() for c in result.untested_health_categories)
        print(f"  (Untested: {labels} — excluded from the total, not scored as failing)")
    print(f"AUTOMATION & CONVERSION OPPORTUNITY: {result.opportunity_score}/{result.max_opportunity_score}")

    print("\n-- Health breakdown --")
    for category, score in result.health_breakdown.items():
        tag = " (untested)" if category in result.untested_health_categories else ""
        print(f"  {category.replace('_', ' ').title():<28} {score}/{HEALTH_WEIGHTS[category]}{tag}")

    print("\n-- Opportunity breakdown --")
    for category, score in result.opportunity_breakdown.items():
        print(f"  {category.replace('_', ' ').title():<28} {score}/{OPPORTUNITY_WEIGHTS[category]}")

    sorted_issues = sorted(result.issues, key=lambda i: PRIORITY_ORDER.get(i.priority, 9))
    print(f"\n-- Issues found ({len(sorted_issues)}) --")
    for issue in sorted_issues:
        print(f"\n  [{issue.priority}] ({issue.category}) {issue.problem}")
        print(f"    Evidence: {issue.evidence}")
        print(f"    Why it matters: {issue.why_it_matters}")
        print(f"    Fix: {issue.fix}")

    print(f"\n-- Needs manual/AI-vision review (not auto-tested) --")
    for item in result.manual_review_needed:
        print(f"  - {item}")

    for lighthouse_result, label in [(result.lighthouse_mobile, "MOBILE"), (result.lighthouse_desktop, "DESKTOP")]:
        if not lighthouse_result:
            continue
        print(f"\n--- LIGHTHOUSE ({label}, median of {lighthouse_result['runs_completed']} runs) ---")
        for category, value in lighthouse_result["scores_median"].items():
            print(f"  {category.title()}: {value}")
        print("  Key metrics:")
        for metric, value in lighthouse_result["key_metrics"].items():
            print(f"    {metric}: {value}")
        if lighthouse_result["screenshot"]:
            print(f"  Screenshot saved: {lighthouse_result['screenshot']}")

    print("\n--- SSL / HTTPS ---")
    ssl_info = result.ssl_info
    print(f"  Valid: {ssl_info.get('valid')} | Issuer: {ssl_info.get('issuer')} | "
          f"Expires: {ssl_info.get('expires')} ({ssl_info.get('days_left')} days left)")

    print("\n--- SECURITY HEADERS ---")
    for header, present in result.security_headers.items():
        print(f"  [{'x' if present else ' '}] {header}")

    print("\n--- INTERNAL LINKS ---")
    links = result.broken_links
    print(f"  Checked: {links['checked_count']} | Broken: {links['broken_count']} | "
          f"Rate-limited (not counted as broken): {links['rate_limited_count']}")


def build_outreach_message(result: AuditResult) -> str:
    """A short, non-spammy personalized outreach draft, per the audit spec:
    mention the business, 2-3 genuine findings, offer to help, ask if
    they'd like to see the full audit. Always grounded in actual issues
    found above — never invents anything."""
    name = result.business_info.get("name") or "there"
    top_issues = sorted(result.issues, key=lambda i: PRIORITY_ORDER.get(i.priority, 9))[:3]

    if not top_issues:
        findings_line = "the site is in solid technical shape overall"
    else:
        findings_line = ", ".join(issue.problem.rstrip(".").lower() for issue in top_issues)

    return (
        f"Hi {name},\n\n"
        f"I was reviewing your website ({result.url}) and noticed a few areas that could be "
        f"improved, particularly {findings_line}.\n\n"
        "I work on website performance, SEO, conversion optimization, and AI automation for "
        "local businesses, and I put together a quick audit showing what I'd change and why.\n\n"
        "If you're interested, I'd be happy to send over the full audit and recommendations.\n\n"
        "Best,\n[Your Name]"
    )


def save_json_report(path: str, result: AuditResult) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, default=str)


def save_html_report(path: str, result: AuditResult) -> None:
    health_rows = "".join(
        f"<tr><td>{c.replace('_', ' ').title()}</td><td>{s}/{HEALTH_WEIGHTS[c]}"
        f"{' (untested)' if c in result.untested_health_categories else ''}</td></tr>"
        for c, s in result.health_breakdown.items()
    )
    opportunity_rows = "".join(
        f"<tr><td>{c.replace('_', ' ').title()}</td><td>{s}/{OPPORTUNITY_WEIGHTS[c]}</td></tr>"
        for c, s in result.opportunity_breakdown.items()
    )

    sorted_issues = sorted(result.issues, key=lambda i: PRIORITY_ORDER.get(i.priority, 9))
    priority_colors = {"CRITICAL": "#f8d7da", "HIGH": "#fde2c8", "MEDIUM": "#fff3cd", "LOW": "#e2f0d9"}
    issues_html = "".join(
        f'<div class="issue" style="background:{priority_colors.get(i.priority, "#eee")}">'
        f'<strong>[{i.priority}] {i.category}:</strong> {i.problem}<br>'
        f'<em>Evidence:</em> {i.evidence}<br>'
        f'<em>Why it matters:</em> {i.why_it_matters}<br>'
        f'<em>Recommended fix:</em> {i.fix}</div>'
        for i in sorted_issues
    )

    manual_html = "".join(f"<li>{item}</li>" for item in result.manual_review_needed)

    warning_html = (
        f'<div class="flag flag-warning"><strong>Unreliable result:</strong> '
        f"{result.fetch_warning}. Verify manually in a browser.</div>"
        if result.fetch_warning else ""
    )

    business = result.business_info
    business_html = (
        f"<p><strong>Business:</strong> {business.get('name') or '—'} | "
        f"<strong>Phone:</strong> {business.get('telephone') or '—'} | "
        f"<strong>Address:</strong> {business.get('address') or '—'}</p>"
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Website Growth Audit — {result.url}</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; max-width: 860px; margin: 40px auto; color: #1a1a2e; }}
  h1 {{ font-size: 22px; }}
  h2 {{ font-size: 16px; margin-top: 32px; border-bottom: 2px solid #eee; padding-bottom: 4px; }}
  .scores {{ display: flex; gap: 24px; margin: 16px 0; }}
  .score-box {{ flex: 1; text-align: center; padding: 16px; border-radius: 8px; background: #f4f6fb; }}
  .score {{ font-size: 40px; font-weight: 800; color: #0f3460; }}
  .score-max {{ font-size: 16px; color: #888; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
  td {{ padding: 8px; border-bottom: 1px solid #eee; }}
  .flag {{ background: #fff3cd; padding: 10px; border-radius: 6px; margin: 8px 0; }}
  .flag-warning {{ background: #f8d7da; }}
  .issue {{ padding: 10px 14px; border-radius: 6px; margin: 8px 0; }}
</style>
</head>
<body>
  <h1>Website Growth Audit</h1>
  <p><strong>URL:</strong> {result.url}</p>
  {business_html}
  {warning_html}
  <div class="scores">
    <div class="score-box">
      <div class="score">{result.health_score}<span class="score-max">/{result.max_health_score}</span></div>
      <div>Website Health</div>
    </div>
    <div class="score-box">
      <div class="score">{result.opportunity_score}<span class="score-max">/{result.max_opportunity_score}</span></div>
      <div>Automation &amp; Conversion Opportunity</div>
    </div>
  </div>

  <h2>Health Breakdown</h2>
  <table>{health_rows}</table>

  <h2>Automation &amp; Conversion Opportunity Breakdown</h2>
  <table>{opportunity_rows}</table>

  <h2>Issues Found ({len(sorted_issues)})</h2>
  {issues_html}

  <h2>Needs Manual / AI-Vision Review</h2>
  <ul>{manual_html}</ul>
</body>
</html>"""

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# =============================================================================
# CLI
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit a website's health and automation/conversion opportunity."
    )
    parser.add_argument(
        "url", nargs="?", default=None,
        help="Full URL to audit, e.g. https://example.com. If omitted, you'll be prompted.",
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_LIGHTHOUSE_RUNS,
                         help=f"Lighthouse runs per form factor (default: {DEFAULT_LIGHTHOUSE_RUNS})")
    parser.add_argument("--no-desktop", action="store_true",
                         help="Skip the desktop Lighthouse pass (mobile always runs)")
    parser.add_argument("--skip-lighthouse", action="store_true",
                         help="Skip Lighthouse entirely (no Docker required) — HTML checks only")
    parser.add_argument("--max-links", type=int, default=DEFAULT_MAX_LINKS,
                         help=f"Max internal links to crawl (default: {DEFAULT_MAX_LINKS})")
    parser.add_argument("--out", default=os.path.join(os.getcwd(), "reports"),
                         help="Output directory for reports/screenshots (default: ./reports)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug-level logging")
    return parser


def prompt_for_url() -> str | None:
    url = normalize_url(input("Enter the URL to audit: "))
    if not url or not urlparse(url).netloc:
        print("No valid URL provided.")
        return None
    return url


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ran_interactively = not argv

    if ran_interactively:
        url = prompt_for_url()
        if url is None:
            return 1
        argv = [url, "--skip-lighthouse"]

    args = build_arg_parser().parse_args(argv)

    if args.url is None:
        args.url = prompt_for_url()
        if args.url is None:
            return 1
    else:
        args.url = normalize_url(args.url)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if ran_interactively:
        logger.info(
            "No CLI args given — running with --skip-lighthouse. "
            "Pass a URL as an argument (without --skip-lighthouse) for full Lighthouse scores."
        )

    config = AuditConfig(
        url=args.url,
        lighthouse_runs=args.runs,
        check_desktop=not args.no_desktop,
        max_links=args.max_links,
        reports_dir=args.out,
        skip_lighthouse=args.skip_lighthouse,
    )

    try:
        result = WebsiteAuditor(config).run()
    except requests.RequestException as exc:
        logger.error("Could not reach %s: %s", config.url, exc)
        return 1

    print_report(result)
    save_json_report(os.path.join(config.reports_dir, "full_report.json"), result)
    save_html_report(os.path.join(config.reports_dir, "audit_report.html"), result)

    outreach_path = os.path.join(config.reports_dir, "outreach_message.txt")
    os.makedirs(config.reports_dir, exist_ok=True)
    with open(outreach_path, "w", encoding="utf-8") as f:
        f.write(build_outreach_message(result))

    logger.info("Client-presentable HTML report saved to: %s",
                os.path.join(config.reports_dir, "audit_report.html"))
    logger.info("Outreach message draft saved to: %s", outreach_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())