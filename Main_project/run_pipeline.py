"""
gosom_lead_scraper.py
----------------------
Reads one row from your lead_search_criteria CSV, builds a gosom (free,
open-source, locally-run Google Maps scraper) query, runs it via Docker,
then filters the raw results against the row's criteria and writes a
"qualified leads" CSV.

Requirements:
    - Docker Desktop installed and RUNNING
    - pandas installed in your venv (pip install pandas)
    - First run pulls the gosom image + Playwright browser (~270MB), so it
      will take a few minutes the first time. Subsequent runs are fast
      because the browser cache is kept in a named Docker volume.

Usage:
    Just set CSV_PATH and ROW_NUMBER below and run this file.
"""

import os
import subprocess
import pandas as pd

# ---------------- CONFIG ----------------
CSV_PATH = "lead scaping data/lead_search_criteria (2).csv"
ROW_NUMBER = 5          # 1 = first row, 2 = second row, etc.
DEPTH = 5               # gosom scroll depth -> roughly controls result count
EXIT_ON_INACTIVITY = "3m"
WORK_DIR = os.path.abspath("gosom_run")   # where queries.txt + results.csv live
DOCKER_IMAGE = "gosom/google-maps-scraper"
CACHE_VOLUME = "gmaps-playwright-cache"   # named volume, reused across runs
MASTER_LEADS_PATH = os.path.abspath("master_qualified_leads.csv")  # accumulates across every run
# -----------------------------------------


def load_criteria_row(csv_path, row_number):
    df = pd.read_csv(csv_path)
    df = df.dropna(how="all").reset_index(drop=True)
    total_rows = len(df)
    if row_number < 1 or row_number > total_rows:
        raise ValueError(f"Out of range! CSV has {total_rows} rows, but you asked for row {row_number}.")
    return df.iloc[row_number - 1]


def build_queries(row):
    """Turn business_type + keywords + location into one query per line."""
    location = str(row["location"]).strip()
    terms = set()

    business_type = row.get("business_type")
    if pd.notna(business_type) and str(business_type).strip():
        terms.add(str(business_type).strip())

    keywords = row.get("keywords")
    if pd.notna(keywords) and str(keywords).strip():
        for kw in str(keywords).split(","):
            kw = kw.strip()
            if kw:
                terms.add(kw)

    queries = [f"{term} in {location}" for term in terms]
    return queries


def run_gosom(queries, work_dir):
    os.makedirs(work_dir, exist_ok=True)
    queries_path = os.path.join(work_dir, "queries.txt")
    results_path = os.path.join(work_dir, "raw_results.csv")

    with open(queries_path, "w", encoding="utf-8") as f:
        f.write("\n".join(queries))

    # Make sure a results.csv file exists before mounting (Docker will
    # otherwise create it as a directory on some setups)
    if not os.path.exists(results_path):
        open(results_path, "w").close()

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{CACHE_VOLUME}:/opt",
        "-v", f"{queries_path}:/queries.txt:ro",
        "-v", f"{results_path}:/results.csv",
        DOCKER_IMAGE,
        "-input", "/queries.txt",
        "-results", "/results.csv",
        "-depth", str(DEPTH),
        "-exit-on-inactivity", EXIT_ON_INACTIVITY,
    ]

    print("Running gosom via Docker (first run may take a while to pull the image)...")
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    return results_path


def filter_leads(results_path, row):
    df = pd.read_csv(results_path)
    if df.empty:
        return df

    min_rating = float(row["min_rating"])
    max_rating = float(row["max_rating"])
    min_reviews = int(row["min_reviews"])
    website_required = str(row["website_required"]).strip().lower() == "yes"
    phone_required = str(row["phone_required"]).strip().lower() == "yes"

    df["review_rating"] = pd.to_numeric(df["review_rating"], errors="coerce")
    df["review_count"] = pd.to_numeric(df["review_count"], errors="coerce")

    mask = (
        df["review_rating"].between(min_rating, max_rating)
        & (df["review_count"] >= min_reviews)
    )

    if website_required:
        mask &= df["website"].notna() & (df["website"].astype(str).str.strip() != "")
    if phone_required:
        mask &= df["phone"].notna() & (df["phone"].astype(str).str.strip() != "")

    filtered = df[mask].copy()

    # Dedupe by cid (Google's stable place id) if present, else by link
    dedupe_col = "cid" if "cid" in filtered.columns else "link"
    filtered = filtered.drop_duplicates(subset=[dedupe_col])

    return filtered


def save_to_master(qualified, row, row_number, master_path):
    """Append this run's qualified leads to a master CSV that accumulates
    across every run, tagging each lead with which criteria row/business_type
    it came from, and deduping across ALL runs by cid (or link)."""
    if qualified.empty:
        return qualified

    tagged = qualified.copy()
    tagged.insert(0, "source_row", row_number)
    tagged.insert(1, "source_business_type", row["business_type"])
    tagged.insert(2, "source_location", row["location"])

    dedupe_col = "cid" if "cid" in tagged.columns else "link"

    if os.path.exists(master_path):
        existing = pd.read_csv(master_path)
        combined = pd.concat([existing, tagged], ignore_index=True)
        combined = combined.drop_duplicates(subset=[dedupe_col], keep="first")
    else:
        combined = tagged

    combined.to_csv(master_path, index=False)
    return combined


def main():
    row = load_criteria_row(CSV_PATH, ROW_NUMBER)
    print(f"--- Using row {ROW_NUMBER}: {row['business_type']} in {row['location']} ---")

    queries = build_queries(row)
    print("Queries to run:")
    for q in queries:
        print(" -", q)

    raw_results_path = run_gosom(queries, WORK_DIR)

    qualified = filter_leads(raw_results_path, row)
    out_path = os.path.join(WORK_DIR, f"qualified_leads_row{ROW_NUMBER}.csv")
    qualified.to_csv(out_path, index=False)

    master = save_to_master(qualified, row, ROW_NUMBER, MASTER_LEADS_PATH)

    print(f"\nRaw results: {raw_results_path}")
    print(f"Qualified leads this run ({len(qualified)}): {out_path}")
    print(f"Master leads file ({len(master)} total, deduped): {MASTER_LEADS_PATH}")


if __name__ == "__main__":
    main()