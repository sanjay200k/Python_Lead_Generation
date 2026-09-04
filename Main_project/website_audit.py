"""
Website Quality & Mistake Auditor  (Lighthouse via Docker + custom checks)
---------------------------------------------------------------------------
Everything below runs locally. The only container involved is the official
Lighthouse image (femtopixel/google-lighthouse) — everything else (HTML
fetch, SSL check, header check, chatbot detection, broken-link crawl) is
plain Python, no external APIs, no API keys.

Requirements (install once):
    1. Docker Desktop installed and running
    2. docker pull femtopixel/google-lighthouse
    3. pip install requests beautifulsoup4

Usage:
    Set WEBSITE_LINK below, then run this file in PyCharm.
"""

# ============================================================================
# 1. IMPORTS
# ============================================================================
# Reason:
# Keep dependencies organized and easy to identify.

import subprocess
import json
import os
import re
import ssl
import socket
import base64
import statistics
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================================
# 2. CONFIGURATION & CONSTANTS
# ============================================================================
# Reason:
# Keep settings, URLs, limits, weights, and static definitions together so
# they can be tuned without hunting through the logic below.

WEBSITE_LINK = "https://www.nymidtowndental.com/"
LIGHTHOUSE_RUNS_PER_PROFILE = 3      # Lighthouse is noisy — 3-5 is the accepted minimum
CHECK_DESKTOP_TOO = True             # mobile is always checked; desktop is optional
MAX_LINKS_TO_CRAWL = 25              # cap on internal links checked for broken-link scan
REQUEST_TIMEOUT = 12
LINK_CHECK_DELAY = 0.6               # seconds between each internal link request
LINK_RATE_LIMIT_RETRY_DELAY = 4.0    # seconds to wait before retrying a 429

REPORTS_DIR = os.path.join(os.getcwd(), "reports")
SCREENSHOTS_DIR = os.path.join(REPORTS_DIR, "screenshots")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CHATBOT_SIGNATURES = {
    "Intercom": ["widget.intercom.io", "intercomcdn.com"],
    "Drift": ["js.driftt.com", "drift.com/api"],
    "Tidio": ["code.tidio.co"],
    "Zendesk Chat": ["ekr.zdassets.com", "zopim.com"],
    "LiveChat": ["cdn.livechatinc.com"],
    "Crisp": ["client.crisp.chat"],
    "Tawk.to": ["embed.tawk.to"],
    "HubSpot Chat": ["js.hs-scripts.com", "js.usemessages.com"],
    "Freshchat": ["wchat.freshchat.com", "freshchat.com"],
    "Olark": ["static.olark.com"],
    "ManyChat": ["widget.manychat.com"],
    "Chatbot.com": ["chatbot.com/widget"],
    "Facebook Messenger Plugin": ["connect.facebook.net", "fb-customerchat"],
    "WhatsApp Click-to-Chat": ["wa.me/", "api.whatsapp.com/send"],
    "Custom GPT/AI widget (generic)": ["chatgpt", "openai", "ai-chat", "aichat", "chatbot-widget"],
}

SECURITY_HEADERS_CHECKED = [
    "Strict-Transport-Security",
    "Content-Security-Policy",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Permissions-Policy",
]

WEIGHTS = {
    "performance": 25,
    "seo_technical": 15,
    "accessibility": 10,
    "security": 10,
    "ai_chatbot": 25,
    "broken_links": 10,
    "local_schema": 5,
}


# ============================================================================
# 3. DATA / RESULT STRUCTURES
# ============================================================================
# Reason:
# Keep shared result formats in one place. These are plain dict "shapes"
# documented here so every function that builds/consumes them agrees on the
# same keys (kept as dicts rather than classes to preserve original behavior).
#
# Lighthouse profile result:
#   {form_factor, runs_completed, scores_median, key_metrics,
#    opportunities, screenshot, raw_score_samples}
#
# SSL result:
#   {valid, issuer, expires, days_left, protocol, [error]}
#
# Security headers result: {header_name: bool, ...}
#
# Chatbot result: {detected, providers}
#
# SEO result:
#   {title, title_length, meta_description, meta_description_length,
#    has_viewport_tag, has_canonical_tag, h1_count, total_images,
#    images_missing_alt, robots_meta}
#
# Schema result: {schema_found, schema_types, phone_found, address_found}
#
# Broken-links result:
#   {checked_count, broken, broken_count, rate_limited, rate_limited_count}


# ============================================================================
# 4. LIGHTHOUSE
# ============================================================================
# Reason:
# All Lighthouse and Docker-related functions belong together.

def run_lighthouse_once(url, form_factor, run_index):
    """Run a single Lighthouse pass in Docker and return the parsed JSON report."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_name = f"report_{form_factor}_{run_index}.json"
    out_path_host = os.path.join(REPORTS_DIR, out_name)

    command = [
        "docker", "run", "--rm",
        "--shm-size=1gb",
        "-v", f"{REPORTS_DIR}:/home/chrome/report",
        "femtopixel/google-lighthouse",
        url,
        "--output=json",
        f"--output-path=/home/chrome/report/{out_name}",
        "--chrome-flags=--disable-dev-shm-usage --no-sandbox",
    ]
    if form_factor == "desktop":
        command.append("--preset=desktop")

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"  [!] Lighthouse run {run_index} ({form_factor}) failed: {e.stderr[-500:] if e.stderr else e}")
        return None
    except FileNotFoundError:
        print("  [!] Docker not found. Make sure Docker Desktop is installed and running.")
        return None

    if not os.path.exists(out_path_host):
        print(f"  [!] Report file not created for run {run_index} ({form_factor}).")
        return None

    with open(out_path_host, "r", encoding="utf-8") as f:
        return json.load(f)


def _lighthouse_category_scores(reports, category):
    """Collect a category's 0-100 scores across all completed Lighthouse runs."""
    vals = []
    for r in reports:
        cat_data = r.get("categories", {}).get(category)
        if cat_data and cat_data.get("score") is not None:
            vals.append(cat_data["score"] * 100)
    return vals


def _lighthouse_median_scores(reports):
    """Compute the median score per Lighthouse category across all runs."""
    medians = {}
    for cat in ["performance", "accessibility", "best-practices", "seo"]:
        vals = _lighthouse_category_scores(reports, cat)
        medians[cat] = round(statistics.median(vals), 1) if vals else None
    return medians


def _lighthouse_pick_representative_report(reports, perf_vals):
    """Pick the run whose performance score is closest to the median (most typical run)."""
    if perf_vals:
        med = statistics.median(perf_vals)
        return min(
            reports,
            key=lambda r: abs((r["categories"]["performance"]["score"] * 100) - med)
            if r.get("categories", {}).get("performance", {}).get("score") is not None else 999,
        )
    return reports[0]


def _lighthouse_extract_key_metrics(audits):
    """Pull the headline performance metrics out of a Lighthouse audits block."""
    key_metrics = {}
    for key in ["first-contentful-paint", "largest-contentful-paint", "speed-index",
                "total-blocking-time", "cumulative-layout-shift", "interactive"]:
        a = audits.get(key)
        if a:
            key_metrics[a.get("title", key)] = a.get("displayValue")
    return key_metrics


def _lighthouse_save_screenshot(audits, form_factor):
    """Decode and save the final-screenshot audit if Lighthouse produced one."""
    shot = audits.get("final-screenshot", {}).get("details", {}).get("data")
    if not shot:
        return None
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    screenshot_path = os.path.join(SCREENSHOTS_DIR, f"screenshot_{form_factor}.png")
    b64 = shot.split(",", 1)[-1]
    with open(screenshot_path, "wb") as f:
        f.write(base64.b64decode(b64))
    return screenshot_path


def _lighthouse_extract_opportunities(audits):
    """List audits that scored below 0.9 as improvement opportunities."""
    opportunities = []
    for a in audits.values():
        if a.get("score") is not None and a["score"] < 0.9 and a.get("displayValue"):
            opportunities.append(f"{a.get('title')}: {a.get('displayValue')}")
    return opportunities


def run_lighthouse_profile(url, form_factor, runs):
    """Run Lighthouse `runs` times for one form factor and summarize the results."""
    print(f"Running Lighthouse ({form_factor}) — {runs} run(s), this can take a while...")
    reports = []
    for i in range(1, runs + 1):
        print(f"  run {i}/{runs}...")
        data = run_lighthouse_once(url, form_factor, i)
        if data:
            reports.append(data)

    if not reports:
        return None

    medians = _lighthouse_median_scores(reports)
    perf_vals = _lighthouse_category_scores(reports, "performance")
    best_report = _lighthouse_pick_representative_report(reports, perf_vals)
    audits = best_report.get("audits", {})

    return {
        "form_factor": form_factor,
        "runs_completed": len(reports),
        "scores_median": medians,
        "key_metrics": _lighthouse_extract_key_metrics(audits),
        "opportunities": _lighthouse_extract_opportunities(audits),
        "screenshot": _lighthouse_save_screenshot(audits, form_factor),
        "raw_score_samples": {"performance": perf_vals},
    }


# ============================================================================
# 5. HTML / HTTP
# ============================================================================
# Reason:
# Keep webpage fetching and HTTP handling separate from the checks that
# consume the response.

def fetch_html(url):
    """Fetch a URL and return the raw requests.Response object."""
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    return resp


# ============================================================================
# 6. SECURITY
# ============================================================================
# Reason:
# Keep SSL and security-header checks together.

def check_ssl(url):
    """Connect directly to the host and inspect its TLS certificate."""
    hostname = urlparse(url).hostname
    result = {"valid": False, "issuer": None, "expires": None, "days_left": None, "protocol": None}
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=REQUEST_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                result["protocol"] = ssock.version()
                issuer = dict(x[0] for x in cert.get("issuer", []))
                result["issuer"] = issuer.get("organizationName") or issuer.get("commonName")
                expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                result["expires"] = expires.strftime("%Y-%m-%d")
                result["days_left"] = (expires - datetime.now(timezone.utc)).days
                result["valid"] = result["days_left"] > 0
    except Exception as e:
        result["error"] = str(e)
    return result


def check_security_headers(resp):
    """Check presence of common security-related HTTP response headers."""
    return {h: h in resp.headers for h in SECURITY_HEADERS_CHECKED}


# ============================================================================
# 7. SEO
# ============================================================================
# Reason:
# Keep all technical SEO checks together.

def check_technical_seo(html, soup):
    """Inspect title, meta description, viewport, canonical, headings, and image alt text."""
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    meta_desc = soup.find("meta", attrs={"name": "description"})
    meta_desc_content = meta_desc.get("content", "").strip() if meta_desc else None
    viewport = soup.find("meta", attrs={"name": "viewport"})
    canonical = soup.find("link", attrs={"rel": "canonical"})
    h1s = soup.find_all("h1")
    imgs = soup.find_all("img")
    imgs_missing_alt = [img for img in imgs if not img.get("alt", "").strip()]
    robots_meta = soup.find("meta", attrs={"name": "robots"})

    return {
        "title": title,
        "title_length": len(title) if title else 0,
        "meta_description": meta_desc_content,
        "meta_description_length": len(meta_desc_content) if meta_desc_content else 0,
        "has_viewport_tag": bool(viewport),
        "has_canonical_tag": bool(canonical),
        "h1_count": len(h1s),
        "total_images": len(imgs),
        "images_missing_alt": len(imgs_missing_alt),
        "robots_meta": robots_meta.get("content") if robots_meta else None,
    }


# ============================================================================
# 8. AI / CHATBOT DETECTION
# ============================================================================
# Reason:
# Keep chatbot detection independent from other audit checks.

def detect_ai_chatbot(html):
    """Scan raw HTML for known chatbot/live-chat vendor script signatures."""
    html_lower = html.lower()
    found = []
    for name, signatures in CHATBOT_SIGNATURES.items():
        for sig in signatures:
            if sig.lower() in html_lower:
                found.append(name)
                break
    return {"detected": len(found) > 0, "providers": found}


# ============================================================================
# 9. LOCAL BUSINESS / SCHEMA
# ============================================================================
# Reason:
# Keep JSON-LD and local-business checks together.

def check_local_business_schema(soup):
    """Look for LocalBusiness-style JSON-LD schema, plus a phone/address fallback scan."""
    result = {"schema_found": False, "schema_types": [], "phone_found": False, "address_found": False}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            t = c.get("@type") if isinstance(c, dict) else None
            if t:
                types = t if isinstance(t, list) else [t]
                for ty in types:
                    if any(key in str(ty) for key in
                           ["LocalBusiness", "Dentist", "MedicalBusiness", "Restaurant", "Store", "Organization"]):
                        result["schema_found"] = True
                        result["schema_types"].append(ty)
            if isinstance(c, dict) and c.get("telephone"):
                result["phone_found"] = True
            if isinstance(c, dict) and c.get("address"):
                result["address_found"] = True

    body_text = soup.get_text(" ", strip=True)
    if not result["phone_found"] and re.search(r"(\+?\d[\d\-\s\(\)]{8,}\d)", body_text):
        result["phone_found"] = True

    return result


# ============================================================================
# 10. BROKEN LINKS
# ============================================================================
# Reason:
# Keep link extraction, crawling, retries, and broken-link detection together.

def _extract_internal_links(base_url, soup, max_links):
    """Collect up to max_links same-domain hrefs, skipping mailto/tel/anchor links."""
    domain = urlparse(base_url).netloc
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("mailto:") or href.startswith("tel:") or href.startswith("#"):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).netloc == domain:
            links.add(full)
        if len(links) >= max_links:
            break
    return links


def _check_single_link(link):
    """Request one link, retrying once on a 429 (rate limit) before giving up."""
    try:
        r = requests.get(link, headers={"User-Agent": UA}, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 429:
            time.sleep(LINK_RATE_LIMIT_RETRY_DELAY)
            r = requests.get(link, headers={"User-Agent": UA}, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return {"url": link, "status": r.status_code}
    except requests.RequestException as e:
        return {"url": link, "status": "error", "error": str(e)}


def crawl_broken_links(base_url, soup, max_links=MAX_LINKS_TO_CRAWL):
    """
    Checks internal links for real breakage (404/410/5xx) while NOT counting
    429 (Too Many Requests) as broken -- that's the server's own rate-limiter
    kicking in, not a dead page. Rate-limited links are reported separately
    and don't count against the site's score.
    """
    links = _extract_internal_links(base_url, soup, max_links)

    checked, broken, rate_limited = [], [], []
    for link in links:
        time.sleep(LINK_CHECK_DELAY)  # be polite -- avoids tripping the site's rate limiter
        result = _check_single_link(link)
        checked.append(result)

        if result["status"] == 429:
            rate_limited.append(result)
        elif result["status"] == "error" or result["status"] >= 400:
            broken.append(result)

    return {
        "checked_count": len(checked),
        "broken": broken,
        "broken_count": len(broken),
        "rate_limited": rate_limited,
        "rate_limited_count": len(rate_limited),
    }


# ============================================================================
# 11. SCORING
# ============================================================================
# Reason:
# Keep all scoring logic together, with each category as its own function so
# any single scoring rule can be modified without touching the others.

def score_performance(lh_mobile, lh_desktop):
    """Weighted 60/40 mobile/desktop performance score, scaled to WEIGHTS['performance']."""
    perf_vals = []
    if lh_mobile and lh_mobile["scores_median"].get("performance") is not None:
        perf_vals.append(lh_mobile["scores_median"]["performance"] * 0.6)
    if lh_desktop and lh_desktop["scores_median"].get("performance") is not None:
        perf_vals.append(lh_desktop["scores_median"]["performance"] * 0.4)

    if len(perf_vals) == 2:
        perf_pct = sum(perf_vals) / (0.6 + 0.4)
    elif perf_vals:
        perf_pct = perf_vals[0] / 0.6
    else:
        perf_pct = 0

    return round((perf_pct / 100) * WEIGHTS["performance"], 1)


def score_accessibility(lh_mobile):
    """Accessibility score taken directly from the mobile Lighthouse run."""
    acc_pct = lh_mobile["scores_median"].get("accessibility") if lh_mobile else 0
    return round(((acc_pct or 0) / 100) * WEIGHTS["accessibility"], 1)


def _manual_seo_checklist_pct(seo):
    """Score the 5 manual on-page SEO checks as a percentage."""
    manual_points = 0
    manual_total = 5
    if seo["title"] and 10 <= seo["title_length"] <= 60:
        manual_points += 1
    if seo["meta_description"] and 50 <= seo["meta_description_length"] <= 160:
        manual_points += 1
    if seo["has_viewport_tag"]:
        manual_points += 1
    if seo["h1_count"] == 1:
        manual_points += 1
    if seo["total_images"] == 0 or seo["images_missing_alt"] / max(seo["total_images"], 1) < 0.2:
        manual_points += 1
    return (manual_points / manual_total) * 100


def score_seo(lh_mobile, seo):
    """SEO score: 50% Lighthouse SEO category, 50% manual on-page checklist."""
    lh_seo_pct = lh_mobile["scores_median"].get("seo") if lh_mobile else 0
    manual_pct = _manual_seo_checklist_pct(seo)
    seo_combined_pct = ((lh_seo_pct or 0) * 0.5) + (manual_pct * 0.5)
    return round((seo_combined_pct / 100) * WEIGHTS["seo_technical"], 1)


def score_security(ssl_info, headers):
    """Security score: valid SSL + each present security header, out of 7 points."""
    sec_points = 0
    sec_total = 7
    if ssl_info.get("valid"):
        sec_points += 1
    sec_points += sum(1 for v in headers.values() if v)
    return round((sec_points / sec_total) * WEIGHTS["security"], 1)


def score_chatbot(chatbot):
    """All-or-nothing score for whether a chatbot/live-chat widget was detected."""
    return WEIGHTS["ai_chatbot"] if chatbot["detected"] else 0


def score_broken_links(links):
    """Score based on the proportion of checked internal links that were broken."""
    if links["checked_count"] == 0:
        link_pct = 100
    else:
        link_pct = max(0, 100 - (links["broken_count"] / links["checked_count"]) * 100)
    return round((link_pct / 100) * WEIGHTS["broken_links"], 1)


def score_local_schema(schema):
    """All-or-nothing score for whether local-business schema was found."""
    return WEIGHTS["local_schema"] if schema["schema_found"] else 0


def compute_score(lh_mobile, lh_desktop, ssl_info, headers, chatbot, seo, schema, links):
    """Combine all individual category scores into a breakdown dict and a total."""
    breakdown = {
        "performance": score_performance(lh_mobile, lh_desktop),
        "accessibility": score_accessibility(lh_mobile),
        "seo_technical": score_seo(lh_mobile, seo),
        "security": score_security(ssl_info, headers),
        "ai_chatbot": score_chatbot(chatbot),
        "broken_links": score_broken_links(links),
        "local_schema": score_local_schema(schema),
    }
    total = round(sum(breakdown.values()), 1)
    return breakdown, total


# ============================================================================
# 12. REPORTING
# ============================================================================
# Reason:
# Keep console, JSON, and HTML report generation separate from audit logic.

def print_report(url, lh_mobile, lh_desktop, ssl_info, headers, chatbot, seo, schema, links, breakdown, total):
    """Print a full human-readable audit report to the console."""
    print("\n" + "=" * 60)
    print(f"WEBSITE AUDIT REPORT — {url}")
    print("=" * 60)

    print(f"\nOVERALL SCORE: {total}/100")
    print("-- Score breakdown --")
    for k, v in breakdown.items():
        print(f"  {k.replace('_', ' ').title():<20} {v}/{WEIGHTS[k]}")

    for lh, label in [(lh_mobile, "MOBILE"), (lh_desktop, "DESKTOP")]:
        if not lh:
            continue
        print(f"\n--- LIGHTHOUSE ({label}, median of {lh['runs_completed']} runs) ---")
        for cat, val in lh["scores_median"].items():
            print(f"  {cat.title()}: {val}")
        print("  Key metrics:")
        for k, v in lh["key_metrics"].items():
            print(f"    {k}: {v}")
        if lh["screenshot"]:
            print(f"  Screenshot saved: {lh['screenshot']}")
        if lh["opportunities"]:
            print("  Top issues:")
            for o in lh["opportunities"][:8]:
                print(f"    - {o}")

    print("\n--- SSL / HTTPS ---")
    print(f"  Valid: {ssl_info.get('valid')} | Issuer: {ssl_info.get('issuer')} | "
          f"Expires: {ssl_info.get('expires')} ({ssl_info.get('days_left')} days left) | "
          f"Protocol: {ssl_info.get('protocol')}")

    print("\n--- SECURITY HEADERS ---")
    for h, present in headers.items():
        print(f"  [{'x' if present else ' '}] {h}")

    print("\n--- AI CHATBOT / LIVE CHAT ---")
    print(f"  Detected: {chatbot['detected']}")
    if chatbot["providers"]:
        print(f"  Provider(s): {', '.join(chatbot['providers'])}")
    else:
        print("  >>> No chatbot detected — strong upsell angle for automation pitch <<<")

    print("\n--- TECHNICAL SEO ---")
    print(f"  Title: \"{seo['title']}\" ({seo['title_length']} chars)")
    print(f"  Meta description: {seo['meta_description_length']} chars "
          f"{'(missing!)' if not seo['meta_description'] else ''}")
    print(f"  Viewport tag: {seo['has_viewport_tag']} | Canonical tag: {seo['has_canonical_tag']}")
    print(f"  H1 count: {seo['h1_count']} {'(should be exactly 1)' if seo['h1_count'] != 1 else ''}")
    print(f"  Images missing alt text: {seo['images_missing_alt']}/{seo['total_images']}")

    print("\n--- LOCAL BUSINESS SCHEMA ---")
    print(f"  Schema found: {schema['schema_found']} {schema['schema_types']}")
    print(f"  Phone detected: {schema['phone_found']} | Address detected: {schema['address_found']}")

    print("\n--- BROKEN LINKS (internal, capped at "
          f"{MAX_LINKS_TO_CRAWL}) ---")
    print(f"  Checked: {links['checked_count']} | Broken: {links['broken_count']} | "
          f"Rate-limited (not counted as broken): {links.get('rate_limited_count', 0)}")
    if links["broken"]:
        print("  Genuinely broken:")
        for b in links["broken"][:10]:
            print(f"    - {b['url']} -> {b['status']}")
    if links.get("rate_limited"):
        print("  Rate-limited (site throttled our checks -- not a real defect):")
        for b in links["rate_limited"][:10]:
            print(f"    - {b['url']} -> {b['status']}")

    print(f"\nFull raw JSON report saved to: {os.path.join(REPORTS_DIR, 'full_report.json')}")


def save_json_report(path, **kwargs):
    """Dump the full raw audit data to a JSON file."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(kwargs, f, indent=2, default=str)


def save_html_report(path, url, breakdown, total, chatbot, seo, ssl_info, links, schema):
    """Write a short client-presentable HTML summary of the audit."""
    rows = "".join(
        f"<tr><td>{k.replace('_',' ').title()}</td><td>{v}/{WEIGHTS[k]}</td></tr>"
        for k, v in breakdown.items()
    )
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Website Audit — {url}</title>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; max-width: 800px; margin: 40px auto; color:#1a1a2e; }}
h1 {{ font-size: 20px; }} .score {{ font-size: 48px; font-weight: 800; color:#0f3460; }}
table {{ width:100%; border-collapse: collapse; margin: 16px 0; }}
td {{ padding: 8px; border-bottom: 1px solid #eee; }}
.flag {{ background:#fff3cd; padding:10px; border-radius:6px; margin:8px 0; }}
</style></head><body>
<h1>Website Audit Report</h1>
<p><strong>URL:</strong> {url}</p>
<div class="score">{total}/100</div>
<table>{rows}</table>
<div class="flag"><strong>AI Chatbot:</strong> {"Detected (" + ', '.join(chatbot['providers']) + ")" if chatbot['detected'] else "NOT detected — automation opportunity"}</div>
<div class="flag"><strong>SSL:</strong> {"Valid" if ssl_info.get('valid') else "INVALID/MISSING"} (expires {ssl_info.get('expires')})</div>
<div class="flag"><strong>Broken internal links:</strong> {links['broken_count']} of {links['checked_count']} checked</div>
<div class="flag"><strong>Local business schema:</strong> {"Present" if schema['schema_found'] else "Missing"}</div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ============================================================================
# 13. MAIN / ORCHESTRATION
# ============================================================================
# Reason:
# main() should only coordinate the entire audit workflow: run checks,
# calculate the score, then generate reports.

def run_all_checks(url):
    """Run every audit check (Lighthouse + HTML-level checks) and return raw results."""
    lh_mobile = run_lighthouse_profile(url, "mobile", LIGHTHOUSE_RUNS_PER_PROFILE)
    lh_desktop = run_lighthouse_profile(url, "desktop", LIGHTHOUSE_RUNS_PER_PROFILE) if CHECK_DESKTOP_TOO else None

    print("\nRunning HTML-level checks (SSL, headers, chatbot, SEO, schema, links)...")
    resp = fetch_html(url)
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    ssl_info = check_ssl(url)
    headers = check_security_headers(resp)
    chatbot = detect_ai_chatbot(html)
    seo = check_technical_seo(html, soup)
    schema = check_local_business_schema(soup)
    links = crawl_broken_links(url, soup)

    return {
        "lh_mobile": lh_mobile,
        "lh_desktop": lh_desktop,
        "ssl_info": ssl_info,
        "headers": headers,
        "chatbot": chatbot,
        "seo": seo,
        "schema": schema,
        "links": links,
    }


def generate_reports(url, results, breakdown, total):
    """Produce the console, JSON, and HTML reports from collected results and scores."""
    print_report(
        url, results["lh_mobile"], results["lh_desktop"], results["ssl_info"],
        results["headers"], results["chatbot"], results["seo"], results["schema"],
        results["links"], breakdown, total,
    )

    save_json_report(
        os.path.join(REPORTS_DIR, "full_report.json"),
        url=url, score=total, breakdown=breakdown,
        lighthouse_mobile=results["lh_mobile"], lighthouse_desktop=results["lh_desktop"],
        ssl=results["ssl_info"], security_headers=results["headers"], chatbot=results["chatbot"],
        seo=results["seo"], schema=results["schema"], broken_links=results["links"],
    )
    save_html_report(
        os.path.join(REPORTS_DIR, "audit_report.html"),
        url, breakdown, total, results["chatbot"], results["seo"], results["ssl_info"],
        results["links"], results["schema"],
    )
    print(f"\nClient-presentable HTML report saved to: {os.path.join(REPORTS_DIR, 'audit_report.html')}")


def main():
    url = WEBSITE_LINK
    print(f"Auditing: {url}\n")

    results = run_all_checks(url)
    breakdown, total = compute_score(
        results["lh_mobile"], results["lh_desktop"], results["ssl_info"],
        results["headers"], results["chatbot"], results["seo"],
        results["schema"], results["links"],
    )
    generate_reports(url, results, breakdown, total)


if __name__ == "__main__":
    main()