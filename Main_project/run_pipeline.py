"""
run_pipeline.py
---------------
Reads one row from your lead_search_criteria CSV, builds gosom (free,
open-source, locally-run Google Maps scraper) queries, runs them via Docker,
then filters the raw results against that row's criteria and writes:

  1) a per-row output, in TWO easy-to-read formats:
       - qualified_leads_row{N}.xlsx  (formatted spreadsheet -- open and go)
       - qualified_leads_row{N}.csv   (plain CSV, kept for compatibility)
  2) an upsert into a PERSISTENT master CSV that accumulates good leads
     across every row and every run you ever do (never wiped), plus a
     master_qualified_leads.xlsx snapshot regenerated from it so you always
     have a readable, formatted copy too.

Requirements:
    - Docker Desktop installed and RUNNING
    - pip install pandas openpyxl
    - optional: pip install pycountry (auto-fills the phone_region column)

Usage:
    Set CSV_PATH and ROW_NUMBER below and run this file. Run it once per
    row (or loop over rows -- see run_all_rows() at the bottom).
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd

# =========================================================================
# SEGMENT 1: CONFIG
# Everything you're likely to tweak lives here, in one place.
# =========================================================================

CSV_PATH = r"F:\AI automation\AI automation\Python_Lead_Generation - Copy\Python_Lead_Generation\Main_project\lead scaping data\fitness_ai_automation_leads.csv"
ROW_NUMBER = 48
# 1 = first row, 2 = second row, etc.
BASE_DEPTH = 5          # gosom scroll depth baseline; scaled by `priority` per row
EXIT_ON_INACTIVITY = "3m"
# Everything (work dir, master files, contacted list) is anchored next to THIS
# script, not to the shell's current directory -- launching the script from a
# different folder used to silently create a second, separate master file and
# break duplicate detection.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(BASE_DIR, "gosom_run")      # per-run raw scrape + per-row output files
DOCKER_IMAGE = "gosom/google-maps-scraper"          # consider pinning a version tag, e.g. ":v1.x.y"
# Hard wall-clock limit for ONE docker/gosom run (seconds). On expiry the
# container is killed and the row fails instead of hanging forever.
GOSOM_TIMEOUT_SECONDS = 2 * 60 * 60
# Extra flags appended to the gosom command. email_required / the `emails`
# column only work if gosom is told to extract emails -- check your gosom
# version's -h output and add e.g. ["-email"] here.
EXTRA_GOSOM_ARGS = []
CACHE_VOLUME = "gmaps-playwright-cache"             # named volume (browser cache only)

# Persistent, NEVER wiped -- accumulates good leads across every row/run.
MASTER_CSV_PATH = os.path.join(BASE_DIR, "master_leads.csv")
MASTER_EXCEL_PATH = os.path.join(BASE_DIR, "master_qualified_leads.xlsx")

# Optional, user-maintained. Add a `cid` (preferred) or `business_name`
# column yourself after you've reached out to someone. If this file
# doesn't exist, "already contacted" exclusion is skipped with a note.
CONTACTED_LEADS_PATH = os.path.join(BASE_DIR, "contacted_leads.csv")

PRIORITY_DEPTH_MAP = {"high": 7, "medium": 5, "low": 3}

# A handful of common informal country names that trip up pycountry's
# fuzzy matcher (it handles most spellings/variants fine on its own --
# "USA", "South Korea", "Vietnam", etc. all resolve correctly without
# help). Add an entry here if you scrape a country whose name your
# criteria CSV writes in a way that logs a "couldn't map country" note.
COUNTRY_NAME_ALIASES = {
    "uk": "united kingdom",
    "ivory coast": "cote d'ivoire",
}

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
# Only THESE query parameters are stripped -- other parameters (?id=3,
# ?page_id=2, ?lang=en ...) are part of a real URL and are kept.
TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAM_EXACT = {
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid",
    "_ga", "authuser", "rclk", "entry", "g_ep", "hl",   # last five: Google Maps link noise
}

# Excel hard-caps any single cell at 32,767 characters and silently
# truncates anything longer. Keep a safety margin below the real limit.
EXCEL_CELL_CHAR_LIMIT = 32000

# Pull out the actual complaint text from individual reviews under this
# star rating -- independent of the business's overall average rating.
NEGATIVE_REVIEW_RATING_THRESHOLD = 3

REVIEWS_BLOB_COLUMN_CANDIDATES = ["user_reviews_extended", "user_reviews", "reviews", "reviews_data"]
REVIEW_RATING_KEY_CANDIDATES = ["rating", "stars", "score", "star_rating"]
REVIEW_TEXT_KEY_CANDIDATES = ["text", "snippet", "comment", "body", "description", "review_text"]
REVIEW_AUTHOR_KEY_CANDIDATES = ["name", "author", "reviewer", "user", "username"]


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
    "poor": (None, 3.5),   # rated, but below 3.5 (previously mislabelled "average")
    "outdated": None,      # handled separately: no reviews in the last 12 months, if a date field exists
    "none": (None, None),  # no rating / no reviews at all
}


# =========================================================================
# SEGMENT 2: LOADING THE CRITERIA ROW
# =========================================================================

def read_criteria_csv(csv_path):
    # utf-8-sig tolerates the BOM Excel adds; blank rows are dropped, so
    # row numbers count DATA rows after that (not spreadsheet line numbers).
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]
    return df.dropna(how="all").reset_index(drop=True)


def load_criteria_row(csv_path, row_number):
    df = read_criteria_csv(csv_path)
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
    if not location:
        print("Warning: row has no city_area/area_extra/state_province/country -- "
              "refusing to run location-less queries.")
        return []

    # Ordered, case-insensitive de-dupe (a plain set made the order change
    # from run to run).
    terms = []
    seen = set()

    def add(term):
        term = str(term).strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)

    business_category = row.get("business_category")
    if pd.notna(business_category):
        add(business_category)

    keywords = row.get("keywords")
    if pd.notna(keywords):
        for kw in str(keywords).split(";"):
            add(kw)

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


# Cache country-string -> ISO code lookups (pycountry's fuzzy search
# isn't free) and only print the "couldn't map" note once per distinct
# unmapped country string, not once per row.
_PHONE_REGION_CACHE: dict[str, str] = {}
_PHONE_REGION_WARNED: set[str] = set()


def resolve_phone_region(country_raw):
    """Map a criteria row's free-text `country` value (e.g. "Singapore",
    "USA", "sg") to the ISO 3166-1 alpha-2 code (e.g. "SG", "US") that
    batch_website_audit.py's --phone-region / per-row phone_region
    column expects for phone-number validation.

    Returns "" (with a one-time console note) if the country is blank
    or can't be confidently mapped -- callers should treat that as
    "unknown region", not crash on it. Requires `pip install pycountry`;
    without it, this always returns "" and prints one note explaining why.
    """
    # NaN (blank CSV cell) is truthy and str(nan) == "nan", so check isna first.
    if country_raw is None or pd.isna(country_raw) or not str(country_raw).strip():
        return ""
    key = str(country_raw).strip()
    if key in _PHONE_REGION_CACHE:
        return _PHONE_REGION_CACHE[key]

    normalized = COUNTRY_NAME_ALIASES.get(key.lower(), key)
    code = ""
    try:
        import pycountry
        try:
            # Exact match on alpha-2/alpha-3/name/common name, case-insensitive.
            code = pycountry.countries.lookup(normalized).alpha_2
        except LookupError:
            # Fuzzy fallback, accepted ONLY when unambiguous (e.g. "Korea"
            # matches both Koreas -> rejected rather than guessed).
            matches = pycountry.countries.search_fuzzy(normalized)
            if len(matches) == 1:
                code = matches[0].alpha_2
    except ImportError:
        if "pycountry" not in _PHONE_REGION_WARNED:
            print("Note: `pip install pycountry` to auto-fill the 'phone_region' column "
                  "from your criteria CSV's country field -- left blank without it.")
            _PHONE_REGION_WARNED.add("pycountry")
    except (LookupError, IndexError):
        pass

    if not code and key not in _PHONE_REGION_WARNED:
        print(f"Note: couldn't map country '{key}' to an ISO region code for phone "
              f"validation -- 'phone_region' left blank for these leads. Add an entry "
              f"to COUNTRY_NAME_ALIASES above if you'll scrape this country regularly.")
        _PHONE_REGION_WARNED.add(key)

    _PHONE_REGION_CACHE[key] = code
    return code


# =========================================================================
# SEGMENT 3: RUNNING GOSOM (Docker)
# =========================================================================

def run_gosom(queries, work_dir, row_number, depth, lang_code):
    os.makedirs(work_dir, exist_ok=True)
    queries_path = os.path.join(work_dir, f"queries_row{row_number}.txt")
    results_path = os.path.join(work_dir, f"raw_results_row{row_number}.csv")

    with open(queries_path, "w", encoding="utf-8") as f:
        f.write("\n".join(queries))
    open(results_path, "w").close()

    container_name = f"gosom_row{row_number}_{int(time.time())}"
    cmd = [
        "docker", "run", "--rm", "--name", container_name,
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
    cmd += list(EXTRA_GOSOM_ARGS)

    print("Running gosom via Docker (first run may take a while to pull the image)...")
    print(" ".join(cmd))
    try:
        subprocess.run(cmd, check=True, timeout=GOSOM_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Killing the docker CLI process does NOT stop the container itself.
        subprocess.run(["docker", "kill", container_name], check=False)
        raise RuntimeError(f"gosom run for row {row_number} exceeded "
                           f"{GOSOM_TIMEOUT_SECONDS}s and was killed.")
    return results_path


# =========================================================================
# SEGMENT 4: NORMALIZING RAW GOSOM OUTPUT
# =========================================================================

# Columns we expect to find in a properly-normalized (one row per
# business) gosom export. Used as a post-reshape sanity check so a
# future gosom format change fails LOUDLY here instead of silently
# corrupting every downstream filter.
EXPECTED_NORMALIZED_COLUMNS = ["title", "link"]


# Identifier-like columns must stay TEXT: 19-20 digit CIDs lose precision (or
# become 1.2e+19) when pandas turns them into floats, and phone numbers lose
# leading zeros. Empty cells -> NaN, but literal strings such as "NA" (the ISO
# code for Namibia!) are kept as-is.
ID_TEXT_COLUMNS = {"cid": str, "phone": str, "phone_region": str, "input_id": str}


def read_csv_text(path, **kwargs):
    return pd.read_csv(path, encoding="utf-8-sig", dtype=ID_TEXT_COLUMNS,
                       keep_default_na=False, na_values=[""], **kwargs)


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
    df_raw = read_csv_text(results_path)
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
    values = df_raw.iloc[:, 2:]

    if "input_id" not in field_names:
        print("Warning: transposed raw results have no 'input_id' row -- "
              "can't tell where one business ends and the next begins. "
              "Returning as-is; filtering will likely fail.")
        return df_raw

    input_id_row_idx = field_names.index("input_id")
    # NaN-safe: blank ids are normalised to "" so NaN != NaN can't split every
    # column into its own record; groups with a blank id are padding, skipped.
    ids = ["" if pd.isna(v) else str(v).strip() for v in values.iloc[input_id_row_idx].tolist()]

    records = []
    start = 0
    for i in range(1, len(ids) + 1):
        if i == len(ids) or ids[i] != ids[start]:
            if ids[start] != "":
                records.append(dict(zip(field_names, values.iloc[:, start].tolist())))
            start = i

    print(f"Reshaped {len(ids)} value-columns into {len(records)} business rows.")
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
    """Strip tracking parameters (utm_*, fbclid, gclid, ...) and the fragment
    from a single URL. Other query parameters are kept: they can be part of
    the real address (?id=3, ?page_id=2, ?lang=en)."""
    if not isinstance(url, str) or not url.strip():
        return url
    try:
        parts = urlsplit(url.strip())
        kept = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAM_EXACT
            and not k.lower().startswith(TRACKING_PARAM_PREFIXES)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))
    except ValueError:
        return url


def link_identity(series):
    """Stable identity for a Maps/website URL: scheme+host+path only, so the
    same place compares equal across runs even if its query string differs
    (?hl=en vs ?hl=ta, authuser, ...). Blank/NaN -> ''."""
    def one(u):
        if not isinstance(u, str) or not u.strip():
            return ""
        try:
            p = urlsplit(u.strip())
        except ValueError:
            return u.strip().lower()
        return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path}".rstrip("/")
    return series.apply(one).astype(object)


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


def _extract_negative_from_reviews(reviews, threshold):
    negative_snippets = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        # gosom's review objects may use capitalised keys (Rating, Description,
        # Name ...); the candidate lists are lowercase, so compare lowercased.
        review = {str(k).lower(): v for k, v in review.items()}
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


def _extract_negative_from_row(raw_values, threshold):
    """Use the first reviews column that actually parses into reviews."""
    for raw in raw_values:
        reviews = _parse_reviews_blob(raw)
        if reviews:
            return _extract_negative_from_reviews(reviews, threshold)
    return "", 0


def add_negative_review_columns(df, threshold=NEGATIVE_REVIEW_RATING_THRESHOLD):
    blob_cols = [c for c in REVIEWS_BLOB_COLUMN_CANDIDATES if c in df.columns]
    if not blob_cols:
        print("Note: no reviews-blob column found (checked "
              f"{REVIEWS_BLOB_COLUMN_CANDIDATES}) -- negative_reviews left blank.")
        df["negative_reviews"] = ""
        df["negative_review_count"] = 0
        return df

    results = [
        _extract_negative_from_row(vals, threshold)
        for vals in df[blob_cols].itertuples(index=False, name=None)
    ]
    df["negative_reviews"] = [r[0] for r in results]
    df["negative_review_count"] = [r[1] for r in results]

    total_found = int((df["negative_review_count"] > 0).sum())
    if total_found:
        print(f"Found individual reviews under {threshold} stars on {total_found} "
              f"business(es) -- see 'negative_reviews' column.")
    return df


# =========================================================================
# SEGMENT 6: CONTACT-INFO REQUIREMENT FILTERS
# =========================================================================

# Values that mean "nothing here" even though the cell is not literally empty.
_EMPTY_MARKERS = {"", "nan", "none", "null", "[]", "{}", "n/a"}
_EMAIL_HINT = (" gosom only fills the emails column when it is told to extract "
               "emails -- add the flag (e.g. '-email') to EXTRA_GOSOM_ARGS.")


def _has_value(series):
    s = series.fillna("").astype(str).str.strip().str.lower()
    return ~s.isin(_EMPTY_MARKERS)


def apply_contact_requirements(df, row):
    """'Either' (or anything other than an explicit yes/no) means "don't
    filter on this field". An explicit 'yes' for a field whose column is
    absent from the results can't be satisfied by ANY lead, so it rejects
    them all LOUDLY instead of quietly ignoring the requirement."""
    mask = pd.Series(True, index=df.index)

    checks = (
        ("website_required", "website", ["website", "site"], ""),
        ("phone_required", "phone", ["phone", "phone_number"], ""),
        ("email_required", "email", ["emails", "email"], _EMAIL_HINT),
    )
    for flag_col, label, candidates, hint in checks:
        flag = str(row.get(flag_col, "")).strip().lower()
        if flag not in ("yes", "no"):
            continue
        col = resolve_column(df, candidates)
        if col is None:
            if flag == "yes":
                print(f"WARNING: {flag_col}=yes but the scraped results have no {label} "
                      f"column (checked {candidates}) -- no lead can satisfy this, so ALL "
                      f"leads are rejected for this row.{hint}")
                mask &= False
            else:
                print(f"Note: {flag_col}=no and results have no {label} column -- "
                      f"nothing to exclude.")
            continue
        has = _has_value(df[col])
        mask &= has if flag == "yes" else ~has

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
        # Whole string + each comma/semicolon piece + each slash-separated part,
        # so "Gym/Fitness center" matches an allowed "gym" AND an allowed
        # "gym/fitness center".
        if pd.isna(raw):
            return set()
        text = str(raw).strip().lower()
        tokens = {text}
        for piece in re.split(r"[,;]", text):
            piece = piece.strip()
            if piece:
                tokens.add(piece)
                tokens.update(s.strip() for s in piece.split("/") if s.strip())
        return tokens

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
    """Label for a business's overall review profile, used only by
    review_quality_filter. Distinct from negative_reviews, which looks at
    individual review text regardless of the overall average."""
    if pd.isna(rating) or pd.isna(review_count) or review_count == 0:
        return "none"
    if rating >= REVIEW_QUALITY_LABELS["good"][0]:
        return "good"
    if rating >= REVIEW_QUALITY_LABELS["average"][0]:
        return "average"
    return "poor"


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
    if df.empty:
        return df
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
        print("WARNING: review_quality_filter includes 'outdated' but this script "
              "has no reliable last-review-date field from gosom to evaluate "
              "that against -- 'outdated' is NOT being applied, so leads that "
              "should have been excluded as outdated are still in this row's results.")
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

def apply_exclude_terms(filtered, row, master_history, contacted):
    exclude_terms_raw = row.get("exclude_terms")
    if not (pd.notna(exclude_terms_raw) and str(exclude_terms_raw).strip()):
        return filtered
    if filtered.empty:
        return filtered

    raw_terms = [t.strip().lower() for t in str(exclude_terms_raw).split(";") if t.strip()]
    special_terms = {"duplicates", "already contacted"}
    literal_terms = [t for t in raw_terms if t not in special_terms]

    before = len(filtered)

    if literal_terms:
        name_col = resolve_column(filtered, NAME_LOOKUP_CANDIDATES)
        cat_col = resolve_column(filtered, CATEGORY_LOOKUP_CANDIDATES)
        if name_col or cat_col:
            # Whole-word match, not plain substring: "bar" must not match
            # "barber". (?<!\w)...(?!\w) instead of \b so terms that start or
            # end with a symbol ("c++", "24/7") still match.
            patterns = [re.compile(rf"(?<!\w){re.escape(term)}(?!\w)") for term in literal_terms]

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
        if "cid" not in filtered.columns and "link" not in filtered.columns:
            print("Warning: exclude_terms has 'duplicates' but raw results have neither "
                  "a 'cid' nor a 'link' column -- skipped.")
        elif not master_history["cids"] and not master_history["links"]:
            print("Note: exclude_terms has 'duplicates' but the master file has no "
                  "history yet -- nothing to compare against.")
        else:
            cid, link = lead_ids(filtered)
            seen = (((cid != "") & cid.isin(master_history["cids"]))
                    | ((link != "") & link.isin(master_history["links"])))
            filtered = filtered[~seen]
            print(f"exclude_terms 'duplicates' removed {before - len(filtered)} "
                  f"lead(s) already present in {MASTER_CSV_PATH}.")
        before = len(filtered)

    if "already contacted" in raw_terms:
        if contacted is None:
            print(f"Note: exclude_terms has 'already contacted' but "
                  f"{CONTACTED_LEADS_PATH} doesn't exist yet -- skipped. "
                  f"Create it with a 'cid' and/or 'business_name' column to enable this.")
        elif not contacted["cids"] and not contacted["names"]:
            print(f"Note: {CONTACTED_LEADS_PATH} has no usable 'cid' or 'business_name' "
                  f"values -- 'already contacted' skipped.")
        else:
            drop = pd.Series(False, index=filtered.index)
            if contacted["cids"] and "cid" in filtered.columns:
                drop |= normalize_cid(filtered["cid"]).isin(contacted["cids"])
            name_col = resolve_column(filtered, NAME_LOOKUP_CANDIDATES)
            if contacted["names"] and name_col:
                drop |= filtered[name_col].map(_norm_name).isin(contacted["names"])
            filtered = filtered[~drop]
            print(f"exclude_terms 'already contacted' removed "
                  f"{before - len(filtered)} lead(s).")

    return filtered


# =========================================================================
# SEGMENT 9: MASTER CSV HISTORY HELPERS (dedupe / contacted lookups)
# =========================================================================

def normalize_cid(series):
    """
    Canonical text form of a 'cid' column: stripped, a trailing '.0' (float
    upcast) removed, and EVERY kind of missing value -> "". Returning "" (not
    the string "nan") matters: callers treat "" as "no cid" and fall back to
    the Maps link, instead of treating all blank-cid leads as one business.
    """
    s = series.astype(object).where(series.notna(), "").astype(str).str.strip()
    s = s.str.replace(r"\.0$", "", regex=True)
    return s.mask(s.str.lower().isin({"nan", "none", "null", "<na>"}), "")


def _norm_name(value):
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def lead_ids(df):
    """Per-row identifiers: (normalised cid or "", link identity or "")."""
    empty = pd.Series("", index=df.index, dtype=object)
    cid = normalize_cid(df["cid"]) if "cid" in df.columns else empty
    link = link_identity(df["link"]) if "link" in df.columns else empty
    return cid, link


def dedupe_by_identity(df, keep="first"):
    """Drop rows that repeat a non-empty cid OR a non-empty link. Rows with
    neither identifier can't be compared and are all kept."""
    if df.empty:
        return df
    cid, link = lead_ids(df)
    dup = (((cid != "") & cid.duplicated(keep=keep))
           | ((link != "") & link.duplicated(keep=keep)))
    return df[~dup]


def load_master_csv(csv_path=MASTER_CSV_PATH):
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    try:
        return read_csv_text(csv_path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def load_master_history(csv_path=MASTER_CSV_PATH):
    """Identifiers of every lead already in the master file (cid AND link)."""
    existing = load_master_csv(csv_path)
    if existing.empty:
        return {"cids": set(), "links": set()}
    cid, link = lead_ids(existing)
    return {"cids": set(cid[cid != ""]), "links": set(link[link != ""])}


def load_contacted(contacted_path):
    """None if the file doesn't exist; otherwise {'cids': set, 'names': set}.
    Both columns are honoured when present."""
    if not os.path.exists(contacted_path):
        return None
    try:
        df = read_csv_text(contacted_path)
    except pd.errors.EmptyDataError:
        return {"cids": set(), "names": set()}
    cids, names = set(), set()
    if "cid" in df.columns:
        c = normalize_cid(df["cid"])
        cids = set(c[c != ""])
    if "business_name" in df.columns:
        names = {n for n in df["business_name"].map(_norm_name) if n}
    return {"cids": cids, "names": names}


def safe_float(value, default=None):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return default
        result = float(value)
        return default if pd.isna(result) else result
    except (TypeError, ValueError):
        return default


def safe_int(value, default=None):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return default
        if isinstance(value, str):
            value = float(value.strip())     # accepts "25" and "25.0"
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


# =========================================================================
# SEGMENT 10: MAIN FILTER PIPELINE
# =========================================================================

def _parse_threshold(row, name, parser, default):
    """Blank cell -> default (no filter). Non-blank but invalid -> warn, default."""
    raw = row.get(name)
    if raw is None or pd.isna(raw) or (isinstance(raw, str) and not raw.strip()):
        return default
    value = parser(raw)
    if value is None:
        print(f"Warning: {name} '{raw}' isn't a valid number -- treating as no filter ({default}).")
        return default
    return value


def filter_leads(results_path, row, master_history, contacted):
    try:
        df = normalize_raw_csv(results_path)
    except pd.errors.EmptyDataError:
        print("Warning: gosom produced no data for this run (empty raw results).")
        return pd.DataFrame()
    if df.empty:
        return df

    df = clean_url_columns(df)
    df = add_negative_review_columns(df)

    min_rating = _parse_threshold(row, "min_rating", safe_float, 0.0)
    min_reviews = _parse_threshold(row, "min_reviews", safe_int, 0)

    for target, candidates in (("review_rating", ["review_rating", "rating"]),
                               ("review_count", ["review_count"])):
        source_col = resolve_column(df, candidates)
        if source_col is None:
            print(f"WARNING: results have no {candidates} column -- every lead is "
                  f"treated as having no {target}.")
            df[target] = float("nan")
        else:
            df[target] = pd.to_numeric(df[source_col], errors="coerce")

    # A business with no rating / review count at all is treated as 0 for the
    # threshold comparison, so it passes only when the thresholds are 0 (blank
    # criteria no longer silently drop unreviewed businesses).
    n_missing = int((df["review_rating"].isna() | df["review_count"].isna()).sum())
    if n_missing:
        print(f"Note: {n_missing} lead(s) have missing rating and/or review_count data "
              f"from gosom -- treated as 0, so they only pass when min_rating and "
              f"min_reviews are 0.")

    mask = ((df["review_rating"].fillna(0) >= min_rating)
            & (df["review_count"].fillna(0) >= min_reviews))
    mask &= apply_contact_requirements(df, row)

    filtered = df[mask].copy()

    if "cid" in filtered.columns:
        filtered["cid"] = normalize_cid(filtered["cid"])
    before_dedupe = len(filtered)
    filtered = dedupe_by_identity(filtered, keep="first")
    if before_dedupe != len(filtered):
        print(f"Removed {before_dedupe - len(filtered)} duplicate lead(s) within this run.")
    if not filtered.empty:
        cid, link = lead_ids(filtered)
        n_unidentified = int(((cid == "") & (link == "")).sum())
        if n_unidentified:
            print(f"Warning: {n_unidentified} lead(s) have neither a cid nor a link -- they "
                  f"can't be de-duplicated against each other or the master file.")

    filtered = apply_category_whitelist(filtered, row)
    filtered = apply_review_quality_filter(filtered, row)
    filtered = apply_exclude_terms(filtered, row, master_history, contacted)

    radius = row.get("search_radius_km")
    if pd.notna(radius) and str(radius).strip():
        print(f"WARNING: search_radius_km = '{radius}' is NOT enforced -- gosom "
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
    blank = (max_leads_raw is None or pd.isna(max_leads_raw)
             or (isinstance(max_leads_raw, str) and not max_leads_raw.strip()))
    if blank:
        print(f"*** max_leads was left BLANK for this row. Applying the default "
              f"cap of {DEFAULT_MAX_LEADS_IF_BLANK}. ***")
        max_leads, source = DEFAULT_MAX_LEADS_IF_BLANK, "default (blank)"
    else:
        max_leads = safe_int(max_leads_raw)
        if max_leads is None or max_leads <= 0:
            # head(-1) would return everything EXCEPT the last row, and 0 would
            # silently deliver nothing -- both are almost certainly typos.
            print(f"Warning: max_leads value '{max_leads_raw}' isn't a positive whole "
                  f"number -- falling back to default cap of {DEFAULT_MAX_LEADS_IF_BLANK}.")
            max_leads, source = DEFAULT_MAX_LEADS_IF_BLANK, "default (invalid)"
        else:
            source = "CSV"

    filtered = filtered.head(max_leads)

    print(f"Requested max_leads: {max_leads} (source: {source}) "
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


def _remove_quietly(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _sanitize_for_excel(df):
    """Strip control characters openpyxl refuses to write (IllegalCharacterError)."""
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            df[col] = df[col].map(
                lambda v: ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v
            )
    return df


def write_excel(df, path, url_labels=None):
    """Returns True if the file was written. Never raises just because the
    target is open in Excel -- it warns and returns False instead."""
    if df.empty:
        if os.path.exists(path):
            # Don't leave an old workbook behind that looks like current output.
            try:
                os.remove(path)
                print(f"Note: 0 rows -- removed stale {path}.")
            except OSError as exc:
                print(f"WARNING: 0 rows, but couldn't remove stale {path}: {exc}")
        else:
            print(f"Note: nothing to write to {path} (0 rows).")
        return False

    df = _sanitize_for_excel(truncate_for_excel(df))

    url_labels = url_labels or set()
    root, ext = os.path.splitext(path)
    tmp_path = f"{root}.tmp{ext}"
    try:
        with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Leads")
            ws = writer.sheets["Leads"]

            from openpyxl.styles import Font
            from openpyxl.utils import get_column_letter

            header_font = Font(bold=True)
            for col_idx, col_name in enumerate(df.columns, start=1):
                cell = ws.cell(row=1, column=col_idx)
                cell.font = header_font

                column_values = df.iloc[:, col_idx - 1]

                # Width from a sample, not a full pass over a huge master file.
                sample = column_values.iloc[:500].astype(str).str.len().tolist()
                max_len = max([len(str(col_name))] + sample)
                ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 60)

                # openpyxl turns any string starting with "=" into a FORMULA.
                # Scraped text is untrusted -- force those cells to plain text.
                starts_eq = column_values.astype(str).str.startswith("=").to_numpy()
                for pos in starts_eq.nonzero()[0]:
                    ws.cell(row=int(pos) + 2, column=col_idx).data_type = "s"

                if str(col_name).strip().lower() in url_labels:
                    for row_idx in range(2, len(df) + 2):
                        c = ws.cell(row=row_idx, column=col_idx)
                        if c.value and str(c.value).startswith("http"):
                            c.hyperlink = str(c.value)
                            c.style = "Hyperlink"

            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
        os.replace(tmp_path, path)
    except PermissionError:
        _remove_quietly(tmp_path)
        print(f"WARNING: couldn't write {path} -- is it open in Excel? Close it and "
              f"re-run (or call export_master_excel()). No CSV data was lost.")
        return False
    except Exception:
        _remove_quietly(tmp_path)
        raise

    print(f"Wrote {len(df)} row(s) to {path}")
    return True


# =========================================================================
# SEGMENT 12: MASTER CSV UPSERT
# =========================================================================

def _atomic_write_csv(df, path):
    """Write to a temp file, then swap it in, so a crash mid-write can never
    truncate the persistent master file."""
    tmp_path = path + ".tmp"
    df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
    try:
        os.replace(tmp_path, path)
    except PermissionError:
        _remove_quietly(tmp_path)
        raise RuntimeError(f"Couldn't update {path} -- is it open in Excel? "
                           f"Close it and re-run this row.")


def upsert_master_csv(filtered, row, row_number, csv_path=MASTER_CSV_PATH):
    existing = load_master_csv(csv_path)
    if filtered.empty:
        return existing

    now = datetime.now(timezone.utc).isoformat()
    tagged = filtered.drop(
        columns=[c for c in ("source_row", "source_business_category", "source_location",
                             "added_at", "last_seen_at") if c in filtered.columns]
    ).copy()
    tagged.insert(0, "source_row", row_number)
    tagged.insert(1, "source_business_category", row.get("business_category"))
    tagged.insert(2, "source_location", build_location(row))
    tagged["added_at"] = now
    tagged["last_seen_at"] = now
    if "cid" in tagged.columns:
        tagged["cid"] = normalize_cid(tagged["cid"])

    if not existing.empty:
        existing = existing.reset_index(drop=True)
        if "cid" in existing.columns:
            existing["cid"] = normalize_cid(existing["cid"])

        # Match each new lead to an existing master row by cid, else by link.
        t_cid, t_link = lead_ids(tagged)
        e_cid, e_link = lead_ids(existing)
        cid_map = {c: i for i, c in e_cid.items() if c}
        link_map = {l: i for i, l in e_link.items() if l}
        match = []
        for c, l in zip(t_cid, t_link):
            if c and c in cid_map:
                match.append(cid_map[c])
            elif l and l in link_map:
                match.append(link_map[l])
            else:
                match.append(None)

        # Preserve manual, hand-added columns (e.g. 'status', 'notes') on rows
        # that already exist, and keep each lead's ORIGINAL added_at.
        manual_cols = [c for c in existing.columns if c not in tagged.columns]
        for col in manual_cols:
            tagged[col] = [existing.at[m, col] if m is not None else None for m in match]
        if manual_cols and any(m is not None for m in match):
            print(f"Preserved manual column(s) {manual_cols} for leads already in the master file.")
        if "added_at" in existing.columns:
            prior = [existing.at[m, "added_at"] if m is not None else None for m in match]
            tagged["added_at"] = [
                p if (p is not None and pd.notna(p) and str(p).strip()) else now
                for p in prior
            ]

    combined = pd.concat([existing, tagged], ignore_index=True, sort=False)
    if "cid" in combined.columns:
        combined["cid"] = normalize_cid(combined["cid"])
    combined = dedupe_by_identity(combined, keep="last")

    _atomic_write_csv(combined, csv_path)
    print(f"Master CSV now has {len(combined)} total accumulated leads: {csv_path}")
    return combined


def export_master_excel():
    """Regenerate master_qualified_leads.xlsx from the master CSV."""
    return write_excel(load_master_csv(MASTER_CSV_PATH), MASTER_EXCEL_PATH,
                       url_labels={"website", "link"})


# =========================================================================
# SEGMENT 13: ORCHESTRATION (one row / all rows / main)
# =========================================================================

def run_one_row(row_number, export_master=True):
    row = load_criteria_row(CSV_PATH, row_number)
    print(f"\n=== Row {row_number}: {row.get('business_category', '')} in {build_location(row)} ===")

    depth, lang_code = resolve_depth_and_lang(row)
    queries = build_queries(row)
    print("Queries to run:")
    for q in queries:
        print(" -", q)
    if not queries:
        print("Nothing to run for this row -- skipping.")
        return 0

    raw_results_path = run_gosom(queries, WORK_DIR, row_number, depth, lang_code)

    master_history = load_master_history(MASTER_CSV_PATH)
    contacted = load_contacted(CONTACTED_LEADS_PATH)

    qualified = filter_leads(raw_results_path, row, master_history, contacted)

    # Tag every lead with the ISO region code for its source row's
    # `country`, so a later `batch_website_audit.py` run on a
    # multi-country master/output file can validate each lead's phone
    # number against the RIGHT country automatically (per-row), instead
    # of relying on one global --phone-region flag that only suits a
    # single-country batch.
    if not qualified.empty:
        qualified = qualified.copy()
        qualified["phone_region"] = resolve_phone_region(row.get("country"))

    # 1) Per-row outputs FIRST. The master update below is the step that makes
    #    "duplicates" exclusion hide these leads on a re-run, so it must only
    #    happen once this row's deliverables are safely on disk.
    display_df = reshape_output_columns(qualified, row)
    if not display_df.empty and "phone_region" in qualified.columns:
        # reshape_output_columns rebuilds the frame from scratch when
        # output_columns is customized, which would otherwise silently
        # drop this column unless the user explicitly requests it --
        # keep it always, since it's what makes the audit step work
        # correctly for mixed-country batches.
        display_df["phone_region"] = qualified["phone_region"]
    row_csv_path = os.path.join(WORK_DIR, f"qualified_leads_row{row_number}.csv")
    row_xlsx_path = os.path.join(WORK_DIR, f"qualified_leads_row{row_number}.xlsx")
    display_df.to_csv(row_csv_path, index=False, encoding="utf-8-sig")
    write_excel(display_df, row_xlsx_path, url_labels=URL_LABELS)

    # 2) Then the persistent master.
    upsert_master_csv(qualified, row, row_number, MASTER_CSV_PATH)
    if export_master:
        export_master_excel()

    print(f"Raw results: {raw_results_path}")
    print(f"Row output ({len(display_df)} leads): {row_xlsx_path}  /  {row_csv_path}")
    return len(display_df)


def run_all_rows():
    total = len(read_criteria_csv(CSV_PATH))
    delivered, failures = {}, []
    for i in range(1, total + 1):
        try:
            # The master workbook is rebuilt ONCE at the end, not after every row.
            delivered[i] = run_one_row(i, export_master=False)
        except Exception as exc:     # one bad row must not stop the batch
            print(f"\n!!! Row {i} FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            failures.append((i, exc))

    export_master_excel()

    print(f"\n=== Batch finished: {len(delivered)}/{total} row(s) ran, "
          f"{sum(delivered.values())} lead(s) delivered ===")
    if failures:
        print("Failed rows: " + ", ".join(f"{i} ({type(e).__name__})" for i, e in failures))


def main():
    run_one_row(ROW_NUMBER)
    # To run every row in the CSV instead, comment the line above and
    # uncomment the line below:
    # run_all_rows()


if __name__ == "__main__":
    main()