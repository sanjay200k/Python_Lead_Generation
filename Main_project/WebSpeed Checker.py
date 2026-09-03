"""
Website Quality Checker (using Lighthouse via Docker)
------------------------------------------------------
Requirements (install once):
    1. Docker Desktop installed and running
    2. Pull the image (run in terminal one time):
           docker pull femtopixel/google-lighthouse

Usage:
    Set WEBSITE_LINK below and run: python website_quality_checker.py
"""

import subprocess
import json
import os

# ==== SET YOUR WEBSITE LINK HERE ====
WEBSITE_LINK = "https://weence.com/dentists/new-york-1/nyc-dental-center/"
# =====================================

REPORTS_DIR = os.path.join(os.getcwd(), "reports")
REPORT_FILE = os.path.join(REPORTS_DIR, "report.json")


def run_lighthouse_docker(url):
    os.makedirs(REPORTS_DIR, exist_ok=True)

    print(f"Running Lighthouse (Docker) on: {url}")
    print("This can take 30-60 seconds...\n")

    command = [
        "docker", "run", "--rm",
        "--shm-size=1gb",
        "-v", f"{REPORTS_DIR}:/home/chrome/report",
        "femtopixel/google-lighthouse",
        url,
        "--output=json",
        "--output-path=/home/chrome/report/report.json",
        "--chrome-flags=--disable-dev-shm-usage --no-sandbox"
    ]

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        print("Error running Lighthouse in Docker:", e)
        return None
    except FileNotFoundError:
        print("Docker not found. Make sure Docker Desktop is installed and running.")
        return None

    if not os.path.exists(REPORT_FILE):
        print("Report file was not created. Check Docker logs above.")
        return None

    with open(REPORT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def print_results(data):
    if not data:
        return

    categories = data.get("categories", {})
    audits = data.get("audits", {})

    print("=" * 50)
    print(f"URL: {data.get('finalUrl')}")
    print("=" * 50)

    print("\n--- SCORES (out of 100) ---")
    for name, cat in categories.items():
        score = cat.get("score")
        score_pct = round(score * 100) if score is not None else "N/A"
        print(f"{cat.get('title')}: {score_pct}")

    print("\n--- KEY PERFORMANCE METRICS ---")
    important_metrics = [
        "first-contentful-paint",
        "largest-contentful-paint",
        "speed-index",
        "total-blocking-time",
        "cumulative-layout-shift",
        "interactive",
    ]

    for key in important_metrics:
        audit = audits.get(key)
        if audit:
            print(f"{audit.get('title')}: {audit.get('displayValue')}")

    print("\n--- TOP OPPORTUNITIES / ISSUES ---")
    for key, audit in audits.items():
        if audit.get("score") is not None and audit["score"] < 0.9 and audit.get("displayValue"):
            print(f"- {audit.get('title')}: {audit.get('displayValue')}")

    print("\nFull raw report saved to:", REPORT_FILE)


if __name__ == "__main__":
    result = run_lighthouse_docker(WEBSITE_LINK)
    print_results(result)