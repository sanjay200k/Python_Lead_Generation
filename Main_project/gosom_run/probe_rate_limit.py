"""
Tiny standalone probe — checks whether a site is still rate-limiting/blocking
you. Costs the site nothing extra to speak of (one plain GET, same as a
browser visit), so you can run this as often as you like while waiting out
a block, without burning a full audit run.

Usage:
    One-shot check:
        python probe_rate_limit.py

    Auto-retry every N minutes until it clears (default 10 min):
        python probe_rate_limit.py --loop
        python probe_rate_limit.py --loop --interval 15

    Override the URL for this run without editing the file:
        python probe_rate_limit.py --url https://example.com/page
"""

import argparse
import sys
import time
from datetime import datetime

import requests

DEFAULT_URL = "https://weence.com/dentists/new-york-1/nyc-dental-center/"
DEFAULT_INTERVAL_MINUTES = 10
TIMEOUT = 12

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def looks_rate_limited(resp):
    if resp.status_code == 429:
        return True
    snippet = resp.text[:3000].lower()
    return any(marker in snippet for marker in
               ["too many requests", "rate limit exceeded", "err_too_many_requests"])


def extract_title(html_text):
    snippet = html_text[:1500]
    lowered = snippet.lower()
    start = lowered.find("<title>")
    end = lowered.find("</title>")
    if start != -1 and end != -1:
        return snippet[start + 7:end].strip()
    return "(no <title> found in first 1500 chars)"


def probe_once(url):
    """Returns True if clear, False if blocked, None if the request itself failed."""
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        print(f"  [!] Request failed entirely: {e}")
        return None

    blocked = looks_rate_limited(resp)
    title = extract_title(resp.text)

    print(f"  Status code : {resp.status_code}")
    print(f"  Page title  : \"{title}\"")
    print(f"  Content len : {len(resp.text)} bytes")

    if blocked:
        print("  [BLOCKED] Still looks like a rate-limit / anti-bot page.")
    else:
        print("  [CLEAR] This does not look like a block page.")

    return not blocked


def main():
    parser = argparse.ArgumentParser(description="Check whether a site's rate-limit/anti-bot block has cleared.")
    parser.add_argument("--url", default=DEFAULT_URL, help="URL to probe")
    parser.add_argument("--loop", action="store_true",
                         help="Keep retrying at a fixed interval until the block clears")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_MINUTES,
                         help=f"Minutes between retries when --loop is set (default {DEFAULT_INTERVAL_MINUTES})")
    parser.add_argument("--max-attempts", type=int, default=0,
                         help="With --loop, stop after this many attempts (0 = unlimited)")
    args = parser.parse_args()

    attempt = 1
    while True:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{stamp}] Probing (attempt {attempt}): {args.url}")

        clear = probe_once(args.url)

        if clear:
            print("\n>>> Block has cleared — safe to run the full audit now. <<<")
            sys.stdout.write("\a")
            sys.stdout.flush()
            return

        if not args.loop:
            if clear is False:
                print("\n-> Wait longer before running the full audit again.")
            return

        if args.max_attempts and attempt >= args.max_attempts:
            print(f"\n[!] Reached --max-attempts={args.max_attempts} without clearing. Stopping.")
            return

        wait_seconds = args.interval * 60
        print(f"  Still blocked — next check in {args.interval:g} min "
              f"({datetime.now().strftime('%H:%M:%S')} -> retry around "
              f"{datetime.fromtimestamp(time.time() + wait_seconds).strftime('%H:%M:%S')})...")
        time.sleep(wait_seconds)
        attempt += 1


if __name__ == "__main__":
    main()