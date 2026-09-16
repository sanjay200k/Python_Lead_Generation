"""
gosom_lead_scraper.py
----------------------
Reads one row from your lead_search_criteria CSV, builds a gosom (free,
open-source, locally-run Google Maps scraper) query, runs it via Docker,
then filters the raw results against that row's criteria and writes:

  1) a per-row output, in TWO easy-to-read formats:
       - qualified_leads_row{N}.xlsx  (formatted spreadsheet -- open and go)
       - qualified_leads_row{N}.csv   (plain CSV, kept for compatibility)
  2) an upsert into a PERSISTENT master CSV that accumulates good leads
     across every row and every run you ever do (never wiped), plus a
     master_qualified_leads.xlsx snapshot regenerated from it every run
     so you always have a readable, formatted copy too.

Requirements:
    - Docker Desktop installed and RUNNING
    - pip install pandas openpyxl

Usage:
    Set CSV_PATH and ROW_NUMBER below and run this file. Run it once per
    row (or loop over rows -- see run_all_rows() at the bottom).
"""

import ast
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import pandas as pd

# =========================================================================
# SEGMENT 1: CONFIG
# Everything you're likely to tweak lives here, in one place.
# =========================================================================

CSV_PATH = r"F:\AI automation\AI automation\Python_Lead_Generation - Copy\Python_Lead_Generation\Main_project\lead scaping data\handyman.csv"
ROW_NUMBER = 1          # 1 = first row, 2 = second row, etc.
BASE_DEPTH = 5          # gosom scroll depth baseline; scaled by `priority` per row
EXIT_ON_INACTIVITY = "3m"
WORK_DIR = os.path.abspath("gosom_run")             # per-run raw scrape + per-row output files
DOCKER_IMAGE = "gosom/google-maps-scraper"
CACHE_VOLUME = "gmaps-playwright-cache"             # named volume (browser cache only)

# Persistent, NEVER wiped -- accumulates good leads across every row/run.
MASTER_CSV_PATH = os.path.abspath("master_leads.csv")
MASTER_EXCEL_PATH = os.path.abspath("master_qualified_leads.xlsx")

# Optional, user-maintained. Add a `cid` (preferred) or `business_name`
# column yourself after you've reached out to someone. If this file
# doesn't exist, "already contacted" exclusion is skipped with a note.
CONTACTED_LEADS_PATH = os.path.abspath("contacted_leads.csv")

PRIORITY_DEPTH_MAP = {"high": 7, "medium": 5, "low": 3}

# Fallback cap used when a criteria row leaves max_leads blank.
# Prevents an unbounded "qualified leads" file from shipping silently.
DEFAULT_MAX_LEADS_IF_BLANK = 25

LANGUAGE_TO_CODE = {
    "english": "en", "arabic": "ar", "french": "fr", "spanish": "es",
    "german": "de", "portuguese": "pt", "hindi": "hi", "tamil": "ta",
    "chinese": "zh", "japanese": "ja", "korean": "ko", "italian": "it",
    "russian": "ru", "dutch": "nl", "turkish": "tr",
}

# Candidate raw-column names gosom might use for each human-readable field.
OUTPUT_COLUMN_CANDIDATES = {
    "business name": ["title", "name", "business_name"],
    "category": ["category", "categories"],
    "address": ["address", "complete_address", "full_address"],
    "phone": ["phone", "phone_number"],
    "website": ["website", "site"],
    "email": ["emails", "email"],
    "google maps url": ["link", "google_maps_link", "url"],
    "rating": ["review_rating", "rating"],
    "review count": ["review_count", "reviews"],
    "social media urls": ["social_media", "social_links", "socials"],
    "negative reviews": ["negative_reviews"],
    "negative review count": ["negative_review_count"],
}
URL_LABELS = {"website", "google maps url"}  # get hyperlinked in the Excel output
NAME_LOOKUP_CANDIDATES = ["title", "name", "business_name"]
CATEGORY_LOOKUP_CANDIDATES = ["category", "categories"]

# Columns that should have tracking junk (?utm_source=... etc.) stripped
# off before anything else uses them.
URL_COLUMNS_TO_CLEAN = ["website", "site", "link", "google_maps_link", "url"]

# Excel hard-caps any single cell at 32,767 characters and silently
# truncates anything longer. Keep a safety margin below the real limit.
EXCEL_CELL_CHAR_LIMIT = 32000

# Pull out the actual complaint text from individual reviews under this
# star rating -- independent of the business's overall average rating.
NEGATIVE_REVIEW_RATING_THRESHOLD = 3

REVIEWS_BLOB_COLUMN_CANDIDATES = ["user_reviews", "reviews", "reviews_data", "reviews_per_rating"]
REVIEW_RATING_KEY_CANDIDATES = ["rating", "stars", "score", "star_rating"]
REVIEW_TEXT_KEY_CANDIDATES = ["text", "snippet", "comment", "body", "description", "review_text"]
REVIEW_AUTHOR_KEY_CANDIDATES = ["name", "author", "reviewer", "user", "username"]
REVIEW_DATE_KEY_CANDIDATES = ["date", "published_at", "time", "relative_date", "review_date"]

MAX_NEGATIVE_REVIEWS_PER_LEAD = 5

# review_quality_filter support: a business's "quality label" is derived
# from its OVERALL average rating (not individual reviews -- that's what
# negative_reviews covers). A row's review_quality_filter value is a
# semicolon list of labels to EXCLUDE, matching the exclude_terms style
# already used elsewhere in this CSV. "Any" / blank disables this filter.
REVIEW_QUALITY_LABELS = {
    # label: (min_rating_inclusive, max_rating_exclusive)
    "good": (4.5, 5.01),
    "average": (3.5, 4.5),
    "outdated": None,   # handled separately: no reviews in the last 12 months, if a date field exists
    "none": (None, None),  # no rating / no reviews at all
}


# =========================================================================
# SEGMENT 2: LOADING THE CRITERIA ROW
# =========================================================================

def load_criteria_row(csv_path, row_number):
    df = pd.read_csv(csv_path)
    df = df.dropna(how="all").reset_index(drop=True)
    total_rows = len(df)
    if row_number < 1 or row_number > total_rows:
        raise ValueError(f"Out of range! CSV has {total_rows} rows, but you asked for row {row_number}.")
    return df.iloc[row_number - 1]


def build_location(row):
    parts = []
    for col in ("city_area", "area_extra", "state_province", "country"):
        val = row.get(col)
        if pd.notna(val) and str(val).strip():
            parts.append(str(val).strip())
    return ", ".join(parts)


def build_queries(row):
    location = build_location(row)
    terms = set()

    business_category = row.get("business_category")
    if pd.notna(business_category) and str(business_category).strip():
        terms.add(str(business_category).strip())

    keywords = row.get("keywords")
    if pd.notna(keywords) and str(keywords).strip():
        for kw in str(keywords).split(";"):
            kw = kw.strip()
            if kw:
                terms.add(kw)

    if not terms:
        print("Warning: row has no business_category or keywords -- no queries to run.")
        return []

    return [f"{term} in {location}" for term in terms]


def resolve_depth_and_lang(row):
    priority = str(row.get("priority", "")).strip().lower()
    depth = PRIORITY_DEPTH_MAP.get(priority, BASE_DEPTH)

    lang_code = None
    language_raw = row.get("language")
    if pd.notna(language_raw) and str(language_raw).strip():
        first_lang = str(language_raw).split(";")[0].strip().lower()
        lang_code = LANGUAGE_TO_CODE.get(first_lang)
        if lang_code is None:
            print(f"Note: language '{first_lang}' has no known gosom locale code -- "
                  f"leaving -lang unset for this run.")
    return depth, lang_code


def resolve_column(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


# =========================================================================
# SEGMENT 3: RUNNING GOSOM (Docker)
# =========================================================================

def run_gosom(queries, work_dir, row_number, depth, lang_code):
    os.makedirs(work_dir, exist_ok=True)
    queries_path = os.path.join(work_dir, "queries.txt")
    results_path = os.path.join(work_dir, f"raw_results_row{row_number}.csv")

    with open(queries_path, "w", encoding="utf-8") as f:
        f.write("\n".join(queries))
    open(results_path, "w").close()

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{CACHE_VOLUME}:/opt",
        "-v", f"{queries_path}:/queries.txt:ro",
        "-v", f"{results_path}:/results.csv",
        DOCKER_IMAGE,
        "-input", "/queries.txt",
        "-results", "/results.csv",
        "-depth", str(depth),
        "-exit-on-inactivity", EXIT_ON_INACTIVITY,
    ]
    if lang_code:
        cmd += ["-lang", lang_code]

    print("Running gosom via Docker (first run may take a while to pull the image)...")
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    return results_path


# =========================================================================
# SEGMENT 4: NORMALIZING RAW GOSOM OUTPUT
# =========================================================================

# Columns we expect to find in a properly-normalized (one row per
# business) gosom export. Used as a post-reshape sanity check so a
# future gosom format change fails LOUDLY here instead of silently
# corrupting every downstream filter.
EXPECTED_NORMALIZED_COLUMNS = ["title", "link"]


def normalize_raw_csv(results_path):
    """
    gosom is expected to write one row per business. Some gosom
    versions/configs instead write a TRANSPOSED csv: a 'Column' header,
    field names running down the second column (C1=input_id, C2=link,
    ...), and each business's values spread across dozens of repeated
    columns to the right. This detects that shape and reshapes it back
    to one row per business. If your gosom output is already normal,
    this is a no-op.
    """
    df_raw = pd.read_csv(results_path)
    if df_raw.empty:
        return df_raw

    first_col = df_raw.columns[0]
    looks_transposed = (
        str(first_col).strip().lower() == "column"
        and df_raw.shape[1] > 1
        and not bool((df_raw.iloc[:, 0].astype(str) == "input_id").any())
        and bool((df_raw.iloc[:, 0].astype(str).str.match(r"^C\d+$")).all())
    )
    if not looks_transposed:
        return df_raw

    print(f"Note: {results_path} is in transposed format -- reshaping to "
          f"one row per business before filtering.")

    field_names = df_raw.iloc[:, 1].tolist()
    value_cols = list(df_raw.columns[2:])

    if "input_id" not in field_names:
        print("Warning: transposed raw results have no 'input_id' row -- "
              "can't tell where one business ends and the next begins. "
              "Returning as-is; filtering will likely fail.")
        return df_raw

    input_id_row_idx = field_names.index("input_id")
    input_id_values = df_raw.iloc[input_id_row_idx, 2:].tolist()

    records = []
    start = 0
    for i in range(1, len(input_id_values) + 1):
        if i == len(input_id_values) or input_id_values[i] != input_id_values[start]:
            col = value_cols[start]
            record = {field_names[r]: df_raw.iloc[r][col] for r in range(len(field_names))}
            records.append(record)
            start = i

    print(f"Reshaped {len(input_id_values)} value-columns into {len(records)} business rows.")
    reshaped = pd.DataFrame(records)

    # Sanity check: if the reshape produced something that doesn't even
    # have a business-name/link column, the transpose heuristic guessed
    # wrong for this gosom version -- surface that loudly rather than
    # silently feeding garbage into the filters below.
    if not any(c in reshaped.columns for c in EXPECTED_NORMALIZED_COLUMNS):
        print("WARNING: reshaped data is missing all expected columns "
              f"{EXPECTED_NORMALIZED_COLUMNS} -- the transposed-CSV reshape "
              "likely doesn't match this gosom version's export format. "
              "Downstream filtering results cannot be trusted for this run.")
    return reshaped


# =========================================================================
# SEGMENT 5: URL CLEANING
# =========================================================================

def clean_url(url):
    """Strip the query string and fragment from a single URL."""
    if not isinstance(url, str) or not url.strip():
        return url
    try:
        parts = urlsplit(url.strip())
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return url


def clean_url_columns(df, columns=URL_COLUMNS_TO_CLEAN):
    cleaned_any = False
    for col in columns:
        if col in df.columns:
            df[col] = df[col].apply(clean_url)
            cleaned_any = True
    if cleaned_any:
        print("Cleaned tracking parameters (utm_source etc.) from URL columns.")
    return df


# =========================================================================
# SEGMENT 5b: EXCEL CELL-LENGTH SAFETY
# =========================================================================

def truncate_for_excel(df, char_limit=EXCEL_CELL_CHAR_LIMIT):
    df = df.copy()
    for col in df.columns:
        if not (df[col].dtype == object or pd.api.types.is_string_dtype(df[col])):
            continue
        lengths = df[col].astype(str).str.len()
        too_long = lengths > char_limit
        if too_long.any():
            print(f"Note: column '{col}' has {too_long.sum()} cell(s) over "
                  f"{char_limit} characters -- truncating before writing to Excel.")
            df.loc[too_long, col] = (
                df.loc[too_long, col].astype(str).str.slice(0, char_limit)
                + " ...[TRUNCATED]"
            )
    return df


# =========================================================================
# SEGMENT 5c: NEGATIVE REVIEW EXTRACTION
# Pulls individual reviewer comments under NEGATIVE_REVIEW_RATING_THRESHOLD
# stars out of gosom's raw reviews blob -- independent of the business's
# overall average rating (which review_quality_filter, below, uses).
# =========================================================================

def _parse_reviews_blob(raw_value):
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, dict):
        return [raw_value]
    if not isinstance(raw_value, str) or not raw_value.strip():
        return []

    text = raw_value.strip()
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
            if isinstance(parsed, dict):
                return [parsed]
            if isinstance(parsed, list):
                return parsed
        except (ValueError, SyntaxError):
            continue
    return []


def _first_present_key(d, candidates):
    for k in candidates:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _extract_negative_from_one_business(raw_value, threshold):
    reviews = _parse_reviews_blob(raw_value)
    if not reviews:
        return "", 0

    negative_snippets = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        rating = safe_float(_first_present_key(review, REVIEW_RATING_KEY_CANDIDATES))
        review_text = _first_present_key(review, REVIEW_TEXT_KEY_CANDIDATES)
        if rating is None or rating >= threshold:
            continue
        if not review_text or not str(review_text).strip():
            continue
        author = _first_present_key(review, REVIEW_AUTHOR_KEY_CANDIDATES)
        cleaned_text = re.sub(r"\s+", " ", str(review_text).strip())
        label = f"{author}: " if author else ""
        negative_snippets.append(f"({rating:g}\u2605) {label}{cleaned_text}")

    total_negative = len(negative_snippets)
    kept = negative_snippets[:MAX_NEGATIVE_REVIEWS_PER_LEAD]
    if total_negative > MAX_NEGATIVE_REVIEWS_PER_LEAD:
        kept.append(f"...(+{total_negative - MAX_NEGATIVE_REVIEWS_PER_LEAD} more)")
    return " | ".join(kept), total_negative


def add_negative_review_columns(df, threshold=NEGATIVE_REVIEW_RATING_THRESHOLD):
    blob_col = resolve_column(df, REVIEWS_BLOB_COLUMN_CANDIDATES)
    if not blob_col:
        print("Note: no reviews-blob column found (checked "
              f"{REVIEWS_BLOB_COLUMN_CANDIDATES}) -- negative_reviews left blank.")
        df["negative_reviews"] = ""
        df["negative_review_count"] = 0
        return df

    results = df[blob_col].apply(lambda v: _extract_negative_from_one_business(v, threshold))
    df["negative_reviews"] = results.apply(lambda t: t[0])
    df["negative_review_count"] = results.apply(lambda t: t[1])

    total_found = int((df["negative_review_count"] > 0).sum())
    if total_found:
        print(f"Found individual reviews under {threshold} stars on {total_found} "
              f"business(es) -- see 'negative_reviews' column.")
    return df


# =========================================================================
# SEGMENT 6: CONTACT-INFO REQUIREMENT FILTERS
# =========================================================================

def apply_contact_requirements(df, row):
    def has_value(col):
        return df[col].notna() & (df[col].astype(str).str.strip() != "")

    mask = pd.Series(True, index=df.index)

    # "Either" (or anything other than an explicit yes/no) means "don't
    # filter on this field" -- this already falls through correctly below
    # since we only special-case "yes" and "no".
    website_required = str(row.get("website_required", "")).strip().lower()
    if website_required == "yes" and "website" in df.columns:
        mask &= has_value("website")
    elif website_required == "no" and "website" in df.columns:
        mask &= ~has_value("website")

    phone_required = str(row.get("phone_required", "")).strip().lower()
    if phone_required == "yes" and "phone" in df.columns:
        mask &= has_value("phone")
    elif phone_required == "no" and "phone" in df.columns:
        mask &= ~has_value("phone")

    email_required = str(row.get("email_required", "")).strip().lower()
    email_col = resolve_column(df, ["emails", "email"])
    if email_required == "yes" and email_col:
        mask &= has_value(email_col)
    elif email_required == "no" and email_col:
        mask &= ~has_value(email_col)

    return mask


# =========================================================================
# SEGMENT 7: CATEGORY WHITELIST
# =========================================================================

def apply_category_whitelist(df, row):
    """
    Only keeps leads whose category is in `allowed_categories` (semicolon
    separated in the criteria CSV). Matching is done against EACH
    category token in gosom's category field, split on comma/semicolon,
    because gosom sometimes returns multi-category strings like
    "Plumber, Water heater installation service" -- an exact full-string
    match against that would wrongly reject a legitimate lead just
    because of a second tag. Any overlap with the whitelist is enough.
    """
    allowed_raw = row.get("allowed_categories")
    if not (pd.notna(allowed_raw) and str(allowed_raw).strip()):
        return df

    allowed = {c.strip().lower() for c in str(allowed_raw).split(";") if c.strip()}
    cat_col = resolve_column(df, CATEGORY_LOOKUP_CANDIDATES)
    if not cat_col:
        print("Warning: allowed_categories is set but no category column "
              "was found in the results -- whitelist skipped.")
        return df

    def category_tokens(raw):
        if pd.isna(raw):
            return set()
        return {t.strip().lower() for t in re.split(r"[,;/]", str(raw)) if t.strip()}

    token_sets = df[cat_col].apply(category_tokens)
    mask = token_sets.apply(lambda toks: bool(toks & allowed))

    before = len(df)
    filtered = df[mask]
    removed = before - len(filtered)
    if removed:
        dropped_categories = sorted(
            set().union(*token_sets[~mask]) if (~mask).any() else set()
        )
        print(f"allowed_categories whitelist removed {removed} lead(s) "
              f"outside {sorted(allowed)}. Dropped categories seen: {dropped_categories}")
    return filtered


# =========================================================================
# SEGMENT 7b: REVIEW QUALITY FILTER
# =========================================================================

def _derive_quality_label(rating, review_count):
    """Best-effort label for a business's overall review profile, used
    only by review_quality_filter. Distinct from negative_reviews, which
    looks at individual review text regardless of the overall average."""
    if pd.isna(rating) or pd.isna(review_count) or review_count == 0:
        return "none"
    if rating >= REVIEW_QUALITY_LABELS["good"][0]:
        return "good"
    if rating >= REVIEW_QUALITY_LABELS["average"][0]:
        return "average"
    return "average"  # below 3.5 still gets excluded upstream by min_rating in most rows


def apply_review_quality_filter(df, row):
    """
    review_quality_filter is a semicolon list of quality labels to
    EXCLUDE (same convention as exclude_terms), e.g.
    'Outdated; average; good; none' in the UK rows of this CSV means
    "only keep leads that don't fall into any of those buckets" -- in
    practice that combination excludes everything, so a row using this
    filter should normally list a SUBSET of labels, not all four. We
    apply it as written and log what got excluded so a misconfigured
    row (like listing every label) is obvious from the console output
    rather than silently producing zero leads.

    'outdated' requires a review-date field gosom doesn't reliably
    provide; if none is found we skip that one sub-condition and say so,
    rather than guessing.
    """
    quality_raw = row.get("review_quality_filter")
    if not (pd.notna(quality_raw) and str(quality_raw).strip()):
        return df
    labels_to_exclude = {l.strip().lower() for l in str(quality_raw).split(";") if l.strip()}
    labels_to_exclude -= {"any", ""}
    if not labels_to_exclude:
        return df

    unknown = labels_to_exclude - set(REVIEW_QUALITY_LABELS.keys())
    if unknown:
        print(f"Warning: review_quality_filter has unrecognized label(s) {sorted(unknown)} "
              f"-- ignoring those, valid labels are {sorted(REVIEW_QUALITY_LABELS.keys())}.")
    labels_to_exclude &= set(REVIEW_QUALITY_LABELS.keys())
    if not labels_to_exclude:
        return df

    if "outdated" in labels_to_exclude:
        print("Note: review_quality_filter includes 'outdated' but this script "
              "has no reliable last-review-date field from gosom to evaluate "
              "that against -- 'outdated' is not applied this run.")
        labels_to_exclude.discard("outdated")

    if not labels_to_exclude:
        return df

    labels = df.apply(
        lambda r: _derive_quality_label(
            safe_float(r.get("review_rating")), safe_float(r.get("review_count"))
        ),
        axis=1,
    )
    mask = ~labels.isin(labels_to_exclude)
    before = len(df)
    filtered = df[mask]
    removed = before - len(filtered)
    if removed:
        print(f"review_quality_filter (excluding {sorted(labels_to_exclude)}) "
              f"removed {removed} lead(s).")
    return filtered


# =========================================================================
# SEGMENT 8: EXCLUDE TERMS (blacklist, duplicates, already-contacted)
# =========================================================================

def apply_exclude_terms(filtered, row, master_history_cids, contacted_ids):
    exclude_terms_raw = row.get("exclude_terms")
    if not (pd.notna(exclude_terms_raw) and str(exclude_terms_raw).strip()):
        return filtered

    raw_terms = [t.strip().lower() for t in str(exclude_terms_raw).split(";") if t.strip()]
    special_terms = {"duplicates", "already contacted"}
    literal_terms = [t for t in raw_terms if t not in special_terms]

    before = len(filtered)

    if literal_terms:
        name_col = resolve_column(filtered, NAME_LOOKUP_CANDIDATES)
        cat_col = resolve_column(filtered, CATEGORY_LOOKUP_CANDIDATES)
        if name_col or cat_col:
            # Word-boundary match, not plain substring: a literal term like
            # "bar" should not match "barber" or "wine bar" just because
            # the letters appear inside a longer word/phrase.
            patterns = [re.compile(rf"\b{re.escape(term)}\b") for term in literal_terms]

            def is_excluded(r):
                haystack = ""
                if name_col:
                    haystack += str(r.get(name_col, "")).lower() + " "
                if cat_col:
                    haystack += str(r.get(cat_col, "")).lower()
                return any(p.search(haystack) for p in patterns)

            filtered = filtered[~filtered.apply(is_excluded, axis=1)]
        else:
            print(f"Warning: exclude_terms has literal terms {literal_terms} but no "
                  f"business-name/category column was found -- skipped.")
        print(f"exclude_terms (literal: {', '.join(literal_terms)}) removed "
              f"{before - len(filtered)} lead(s).")
        before = len(filtered)

    if "duplicates" in raw_terms:
        if "cid" in filtered.columns:
            filtered = filtered[~normalize_cid(filtered["cid"]).isin(master_history_cids)]
            print(f"exclude_terms 'duplicates' removed {before - len(filtered)} "
                  f"lead(s) already present in {MASTER_CSV_PATH}.")
        else:
            print("Warning: exclude_terms has 'duplicates' but no 'cid' column "
                  "exists in raw results -- skipped.")
        before = len(filtered)

    if "already contacted" in raw_terms:
        if contacted_ids is None:
            print(f"Note: exclude_terms has 'already contacted' but "
                  f"{CONTACTED_LEADS_PATH} doesn't exist yet -- skipped. "
                  f"Create it with a 'cid' or 'business_name' column to enable this.")
        elif "cid" in filtered.columns:
            filtered = filtered[~normalize_cid(filtered["cid"]).isin(contacted_ids)]
            print(f"exclude_terms 'already contacted' removed "
                  f"{before - len(filtered)} lead(s).")

    return filtered


# =========================================================================
# SEGMENT 9: MASTER CSV HISTORY HELPERS (dedupe / contacted lookups)
# =========================================================================

def normalize_cid(series):
    """
    A 'cid' column can drift between dtypes across runs/files: plain int
    ('1234'), or float-upcast after pandas saw a NaN somewhere in the
    column ('1234.0'). Left unnormalized, the same business's cid would
    fail to match itself across runs and dedupe/duplicate-exclusion would
    quietly stop working. Strip a trailing '.0' so both forms compare equal.
    """
    s = series.astype(str).str.strip()
    return s.str.replace(r"\.0$", "", regex=True)


def load_master_csv(csv_path=MASTER_CSV_PATH):
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    try:
        return pd.read_csv(csv_path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def load_master_history_cids(csv_path=MASTER_CSV_PATH):
    existing = load_master_csv(csv_path)
    if "cid" in existing.columns:
        return set(normalize_cid(existing["cid"].dropna()))
    return set()


def load_contacted_ids(contacted_path):
    if not os.path.exists(contacted_path):
        return None
    try:
        df = pd.read_csv(contacted_path)
    except pd.errors.EmptyDataError:
        return set()
    if "cid" in df.columns:
        return set(normalize_cid(df["cid"].dropna()))
    if "business_name" in df.columns:
        return set(df["business_name"].dropna().astype(str).str.lower())
    return set()


def safe_float(value, default=None):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=None):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


# =========================================================================
# SEGMENT 10: MAIN FILTER PIPELINE
# =========================================================================

def filter_leads(results_path, row, master_history_cids, contacted_ids):
    try:
        df = normalize_raw_csv(results_path)
    except pd.errors.EmptyDataError:
        print("Warning: gosom produced no data for this run (empty raw results).")
        return pd.DataFrame()
    if df.empty:
        return df

    df = clean_url_columns(df)
    df = add_negative_review_columns(df)

    min_rating = safe_float(row.get("min_rating"), default=0.0)
    min_reviews = safe_int(row.get("min_reviews"), default=0)
    if min_rating is None:
        print(f"Warning: min_rating '{row.get('min_rating')}' isn't a valid number -- treating as no filter (0).")
        min_rating = 0.0
    if min_reviews is None:
        print(f"Warning: min_reviews '{row.get('min_reviews')}' isn't a valid number -- treating as no filter (0).")
        min_reviews = 0

    df["review_rating"] = pd.to_numeric(df.get("review_rating"), errors="coerce")
    df["review_count"] = pd.to_numeric(df.get("review_count"), errors="coerce")

    # A business with a genuinely missing rating/review_count (NaN, not
    # "0") is different from one that scored below your threshold -- but
    # NaN >= x is always False in pandas, so it would silently fail the
    # filter either way and there'd be no way to tell the two cases apart
    # from the console output. Count and report missing-data drops
    # separately so a run that's excluding "no data" leads (as opposed to
    # "low rating" leads) is visible.
    missing_rating_mask = df["review_rating"].isna()
    missing_reviews_mask = df["review_count"].isna()
    n_missing = int((missing_rating_mask | missing_reviews_mask).sum())
    if n_missing:
        print(f"Note: {n_missing} lead(s) have missing rating and/or review_count "
              f"data from gosom -- these will NOT pass the min_rating/min_reviews "
              f"filter (NaN never satisfies a >= threshold), even though that's "
              f"different from actually scoring too low.")

    mask = (df["review_rating"] >= min_rating) & (df["review_count"] >= min_reviews)
    mask &= apply_contact_requirements(df, row)

    filtered = df[mask].copy()

    dedupe_col = "cid" if "cid" in filtered.columns else "link"
    if dedupe_col == "cid":
        filtered["cid"] = normalize_cid(filtered["cid"])
    if dedupe_col in filtered.columns:
        filtered = filtered.drop_duplicates(subset=[dedupe_col])

    filtered = apply_category_whitelist(filtered, row)
    filtered = apply_review_quality_filter(filtered, row)
    filtered = apply_exclude_terms(filtered, row, master_history_cids, contacted_ids)

    radius = row.get("search_radius_km")
    if pd.notna(radius) and str(radius).strip():
        print(f"Note: search_radius_km = '{radius}' is NOT enforced -- gosom "
              f"searches by text query only, this script has no geocoding "
              f"step to filter by distance.")

    toggle_values = {c: row.get(c) for c in ("toggle_1", "toggle_2", "toggle_3", "toggle_4", "toggle_5")
                      if c in row.index}
    if toggle_values:
        print(f"Note: toggle columns for this row (meaning not yet defined, "
              f"not applied): {toggle_values}")

    sort_cols = [c for c in ("review_count", "review_rating") if c in filtered.columns]
    if sort_cols:
        filtered = filtered.sort_values(by=sort_cols, ascending=False)

    qualified_before_cap = len(filtered)

    max_leads_raw = row.get("max_leads")
    max_leads = safe_int(max_leads_raw, default=None)

    if pd.notna(max_leads_raw) and max_leads is None:
        print(f"Warning: max_leads value '{max_leads_raw}' isn't valid -- "
              f"falling back to default cap of {DEFAULT_MAX_LEADS_IF_BLANK}.")
        max_leads = DEFAULT_MAX_LEADS_IF_BLANK
    elif not pd.notna(max_leads_raw):
        print(f"*** max_leads was left BLANK for this row. Applying the default "
              f"cap of {DEFAULT_MAX_LEADS_IF_BLANK}. ***")
        max_leads = DEFAULT_MAX_LEADS_IF_BLANK

    filtered = filtered.head(max_leads)

    print(f"Requested max_leads: {max_leads} (source: "
          f"{'CSV' if pd.notna(max_leads_raw) else 'default fallback'}) "
          f"| Qualified before cap: {qualified_before_cap} | Delivered: {len(filtered)}")

    return filtered


# =========================================================================
# SEGMENT 11: DISPLAY / OUTPUT SHAPING
# =========================================================================

def reshape_output_columns(filtered, row):
    output_columns_raw = row.get("output_columns")
    if not (pd.notna(output_columns_raw) and str(output_columns_raw).strip()):
        return filtered.copy()

    requested_labels = [c.strip() for c in str(output_columns_raw).split(";") if c.strip()]
    selected = {}
    for label in requested_labels:
        candidates = OUTPUT_COLUMN_CANDIDATES.get(label.strip().lower())
        actual_col = resolve_column(filtered, candidates) if candidates else None
        if actual_col:
            selected[label] = filtered[actual_col]
        else:
            print(f"Warning: output_columns requested '{label}' but no matching "
                  f"column was found -- left blank.")
            selected[label] = pd.Series([""] * len(filtered), index=filtered.index)
    return pd.DataFrame(selected)


def write_excel(df, path, url_labels=None):
    if df.empty:
        print(f"Note: nothing to write to {path} (0 rows).")
        return

    df = truncate_for_excel(df)

    url_labels = url_labels or set()
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Leads")
        ws = writer.sheets["Leads"]

        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter

        header_font = Font(bold=True)
        for col_idx, col_name in enumerate(df.columns, start=1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = header_font

            max_len = max(
                [len(str(col_name))] + [len(str(v)) for v in df.iloc[:, col_idx - 1].tolist()]
            )
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 60)

            if str(col_name).strip().lower() in url_labels:
                for row_idx in range(2, len(df) + 2):
                    c = ws.cell(row=row_idx, column=col_idx)
                    if c.value and str(c.value).startswith("http"):
                        c.hyperlink = str(c.value)
                        c.style = "Hyperlink"

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    print(f"Wrote {len(df)} row(s) to {path}")


# =========================================================================
# SEGMENT 12: MASTER CSV UPSERT
# =========================================================================

def upsert_master_csv(filtered, row, row_number, csv_path=MASTER_CSV_PATH):
    if filtered.empty:
        return load_master_csv(csv_path)

    tagged = filtered.copy()
    tagged.insert(0, "source_row", row_number)
    tagged.insert(1, "source_business_category", row.get("business_category"))
    tagged.insert(2, "source_location", build_location(row))
    tagged["added_at"] = datetime.now(timezone.utc).isoformat()

    existing = load_master_csv(csv_path)
    dedupe_col = "cid" if "cid" in tagged.columns else "link"

    if dedupe_col == "cid":
        tagged["cid"] = normalize_cid(tagged["cid"])
        if "cid" in existing.columns:
            existing["cid"] = normalize_cid(existing["cid"])

    # Preserve any manual, hand-added columns on rows that already exist
    # in the master file (e.g. a 'status' or 'notes' column a user added
    # by hand) instead of blindly overwriting the whole row on re-scrape.
    if not existing.empty and dedupe_col in existing.columns:
        manual_cols = [c for c in existing.columns if c not in tagged.columns]
        if manual_cols:
            carry_over = existing[[dedupe_col] + manual_cols].drop_duplicates(
                subset=[dedupe_col], keep="last"
            )
            tagged = tagged.merge(carry_over, on=dedupe_col, how="left")
            print(f"Preserved manual column(s) {manual_cols} for leads already in the master file.")

    combined = pd.concat([existing, tagged], ignore_index=True, sort=False)
    if dedupe_col in combined.columns:
        combined[dedupe_col] = combined[dedupe_col].astype(str)
        combined = combined.drop_duplicates(subset=[dedupe_col], keep="last")

    combined.to_csv(csv_path, index=False)
    print(f"Master CSV now has {len(combined)} total accumulated leads: {csv_path}")
    return combined


# =========================================================================
# SEGMENT 13: ORCHESTRATION (one row / all rows / main)
# =========================================================================

def run_one_row(row_number):
    row = load_criteria_row(CSV_PATH, row_number)
    print(f"\n=== Row {row_number}: {row['business_category']} in {build_location(row)} ===")

    depth, lang_code = resolve_depth_and_lang(row)
    queries = build_queries(row)
    print("Queries to run:")
    for q in queries:
        print(" -", q)
    if not queries:
        print("Nothing to run for this row -- skipping.")
        return

    raw_results_path = run_gosom(queries, WORK_DIR, row_number, depth, lang_code)

    master_history_cids = load_master_history_cids(MASTER_CSV_PATH)
    contacted_ids = load_contacted_ids(CONTACTED_LEADS_PATH)

    qualified = filter_leads(raw_results_path, row, master_history_cids, contacted_ids)

    master_df = upsert_master_csv(qualified, row, row_number, MASTER_CSV_PATH)
    write_excel(master_df, MASTER_EXCEL_PATH, url_labels={"website", "link"})

    display_df = reshape_output_columns(qualified, row)
    row_csv_path = os.path.join(WORK_DIR, f"qualified_leads_row{row_number}.csv")
    row_xlsx_path = os.path.join(WORK_DIR, f"qualified_leads_row{row_number}.xlsx")
    display_df.to_csv(row_csv_path, index=False)
    write_excel(display_df, row_xlsx_path, url_labels=URL_LABELS)

    print(f"Raw results: {raw_results_path}")
    print(f"Row output ({len(display_df)} leads): {row_xlsx_path}  /  {row_csv_path}")


def run_all_rows():
    df = pd.read_csv(CSV_PATH).dropna(how="all").reset_index(drop=True)
    for i in range(1, len(df) + 1):
        run_one_row(i)


def main():
    run_one_row(ROW_NUMBER)
    # To run every row in the CSV instead, comment the line above and
    # uncomment the line below:
    # run_all_rows()


if __name__ == "__main__":
    main()