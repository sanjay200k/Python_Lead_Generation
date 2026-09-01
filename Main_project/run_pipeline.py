"""
gosom_lead_scraper.py
----------------------
Reads one row from your lead_search_criteria CSV, builds a gosom (free,
open-source, locally-run Google Maps scraper) query, runs it via Docker,
then filters the raw results against the row's criteria and writes a
"qualified leads" CSV.

Each run is self-contained: it only outputs leads found in THAT run.
Nothing is merged, deduped, or carried over from previous runs. If you
rerun the same ROW_NUMBER, the output file for that row is simply
overwritten with fresh results.

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
CSV_PATH = r"F:\AI automation\AI automation\Python_Lead_Generation - Copy\Python_Lead_Generation\Main_project\lead scaping data\lead_search_criteria (2).csv"
ROW_NUMBER = 1          # 1 = first row, 2 = second row, etc.
DEPTH = 5               # gosom scroll depth -> roughly controls result count
EXIT_ON_INACTIVITY = "3m"
WORK_DIR = os.path.abspath("gosom_run")   # where queries.txt + raw results live
DOCKER_IMAGE = "gosom/google-maps-scraper"
CACHE_VOLUME = "gmaps-playwright-cache"   # named volume (browser cache only, no lead data -- safe to keep)

# Single output file. Cleared and refilled with ONLY this run's leads every
# time you run the script -- never merged/appended with older runs.
MASTER_LEADS_PATH = os.path.abspath("master_qualified_leads.csv")
# -----------------------------------------


def clear_master_file(master_path):
    """Empty out the master leads file at the start of every run so it
    never carries data from a previous run. The file itself is kept
    (not deleted) -- it's just wiped back to empty."""
    open(master_path, "w").close()
    print(f"Cleared: {master_path}")


def load_criteria_row(csv_path, row_number):
    df = pd.read_csv(csv_path)
    df = df.dropna(how="all").reset_index(drop=True)
    total_rows = len(df)
    if row_number < 1 or row_number > total_rows:
        raise ValueError(f"Out of range! CSV has {total_rows} rows, but you asked for row {row_number}.")
    return df.iloc[row_number - 1]


def build_location(row):
    """Combine city_area, state_province, country (skipping any blanks)
    into a single location string, e.g. 'New York City, New York, USA'."""
    parts = []
    for col in ("city_area", "state_province", "country"):
        val = row.get(col)
        if pd.notna(val) and str(val).strip():
            parts.append(str(val).strip())
    return ", ".join(parts)


def build_queries(row):
    """Turn business_category + keywords + location into one query per line."""
    location = build_location(row)
    terms = set()

    business_category = row.get("business_category")
    if pd.notna(business_category) and str(business_category).strip():
        terms.add(str(business_category).strip())

    keywords = row.get("keywords")
    if pd.notna(keywords) and str(keywords).strip():
        # keywords are semicolon-separated in the new CSV, e.g. "dentist; dental clinic; orthodontist"
        for kw in str(keywords).split(";"):
            kw = kw.strip()
            if kw:
                terms.add(kw)

    queries = [f"{term} in {location}" for term in terms]
    return queries


def run_gosom(queries, work_dir, row_number):
    os.makedirs(work_dir, exist_ok=True)
    queries_path = os.path.join(work_dir, "queries.txt")
    results_path = os.path.join(work_dir, f"raw_results_row{row_number}.csv")

    with open(queries_path, "w", encoding="utf-8") as f:
        f.write("\n".join(queries))

    # Always start this file empty so old scrape data can't leak into it
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
    try:
        df = pd.read_csv(results_path)
    except pd.errors.EmptyDataError:
        print("Warning: gosom produced no data for this run (empty raw results).")
        return pd.DataFrame()

    if df.empty:
        return df

    min_rating = float(row["min_rating"])
    min_reviews = int(row["min_reviews"])

    website_required = str(row.get("website_required", "")).strip().lower()
    phone_required = str(row.get("phone_required", "")).strip().lower()
    email_required = str(row.get("email_required", "")).strip().lower()

    df["review_rating"] = pd.to_numeric(df["review_rating"], errors="coerce")
    df["review_count"] = pd.to_numeric(df["review_count"], errors="coerce")

    mask = (
        (df["review_rating"] >= min_rating)
        & (df["review_count"] >= min_reviews)
    )

    def has_value(col):
        return df[col].notna() & (df[col].astype(str).str.strip() != "")

    # "Yes" = must have it, "No" = must NOT have it, "Either"/anything else = no filter
    if website_required == "yes" and "website" in df.columns:
        mask &= has_value("website")
    elif website_required == "no" and "website" in df.columns:
        mask &= ~has_value("website")

    if phone_required == "yes" and "phone" in df.columns:
        mask &= has_value("phone")
    elif phone_required == "no" and "phone" in df.columns:
        mask &= ~has_value("phone")

    if email_required == "yes" and "email" in df.columns:
        mask &= has_value("email")
    elif email_required == "no" and "email" in df.columns:
        mask &= ~has_value("email")

    filtered = df[mask].copy()

    # Dedupe only WITHIN this run's results (by cid if present, else link)
    dedupe_col = "cid" if "cid" in filtered.columns else "link"
    if dedupe_col in filtered.columns:
        filtered = filtered.drop_duplicates(subset=[dedupe_col])

    # Respect max_leads cap from the criteria row, if present
    max_leads = row.get("max_leads")
    if pd.notna(max_leads):
        filtered = filtered.head(int(max_leads))

    return filtered


def main():
    # Step 1: wipe the master file clean before doing anything else
    clear_master_file(MASTER_LEADS_PATH)

    row = load_criteria_row(CSV_PATH, ROW_NUMBER)
    print(f"--- Using row {ROW_NUMBER}: {row['business_category']} in {build_location(row)} ---")

    queries = build_queries(row)
    print("Queries to run:")
    for q in queries:
        print(" -", q)

    raw_results_path = run_gosom(queries, WORK_DIR, ROW_NUMBER)

    qualified = filter_leads(raw_results_path, row)

    # Step 2: write ONLY this run's leads into the (now-empty) master file
    qualified.to_csv(MASTER_LEADS_PATH, index=False)

    print(f"\nRaw results (this run only): {raw_results_path}")
    print(f"Qualified leads written ({len(qualified)}): {MASTER_LEADS_PATH}")


if __name__ == "__main__":
    main()