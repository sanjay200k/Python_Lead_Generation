"""
export_report.py
-----------------
Converts website_audit.py's full_report.json into structured pandas
tables and exports them as CSV (and optionally a single multi-sheet
Excel file), so results can be filtered/sorted/compared across audits
instead of read as console text.

Usage:
    python export_report.py reports/full_report.json
    python export_report.py reports/full_report.json --excel
    python export_report.py reports/full_report.json --out reports/tables
"""

from __future__ import annotations

import argparse
import json
import os

import pandas as pd


def load_report(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_scores_table(data: dict) -> pd.DataFrame:
    """One row per score category (health + opportunity combined)."""
    rows = []
    for category, score in data["health_breakdown"].items():
        rows.append({
            "score_type": "Health",
            "category": category.replace("_", " ").title(),
            "score": score,
            "untested": category in data.get("untested_health_categories", []),
        })
    for category, score in data["opportunity_breakdown"].items():
        rows.append({
            "score_type": "Opportunity",
            "category": category.replace("_", " ").title(),
            "score": score,
            "untested": False,
        })
    return pd.DataFrame(rows)


def build_issues_table(data: dict) -> pd.DataFrame:
    """One row per issue found, sorted by priority."""
    priority_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    df = pd.DataFrame(data["issues"])
    if df.empty:
        return df
    df["priority_rank"] = df["priority"].map(priority_order)
    df = df.sort_values(["priority_rank", "category"]).drop(columns="priority_rank")
    return df.reset_index(drop=True)


def build_lighthouse_errors_table(data: dict) -> pd.DataFrame:
    """One row per Lighthouse error/warning message (timeouts, failed runs, missing Docker)."""
    errors = data.get("lighthouse_errors") or []
    return pd.DataFrame({"lighthouse_note": errors})


def build_lighthouse_table(data: dict) -> pd.DataFrame:
    """One row per form-factor (mobile/desktop), category scores as columns."""
    rows = []
    for key in ("lighthouse_mobile", "lighthouse_desktop"):
        lh = data.get(key)
        if not lh:
            continue
        row = {"form_factor": lh["form_factor"], "runs_completed": lh["runs_completed"]}
        row.update(lh["scores_median"])
        for metric_name, value in lh["key_metrics"].items():
            row[metric_name] = value
        rows.append(row)
    return pd.DataFrame(rows)


def build_summary_row(data: dict) -> pd.DataFrame:
    """Single-row overview — handy for stacking multiple audits into one CSV."""
    return pd.DataFrame([{
        "url": data["url"],
        "business_name": data.get("business_info", {}).get("name"),
        "phone": data.get("business_info", {}).get("telephone"),
        "health_score": data["health_score"],
        "max_health_score": data["max_health_score"],
        "opportunity_score": data["opportunity_score"],
        "max_opportunity_score": data["max_opportunity_score"],
        "issue_count": len(data["issues"]),
        "critical_issue_count": sum(1 for i in data["issues"] if i["priority"] == "CRITICAL"),
        "high_issue_count": sum(1 for i in data["issues"] if i["priority"] == "HIGH"),
        "broken_links": data.get("broken_links", {}).get("broken_count"),
        "forbidden_links_needs_manual_check": data.get("broken_links", {}).get("forbidden_count"),
        "ignored_links": data.get("broken_links", {}).get("ignored_count"),
        "lighthouse_ran": bool(data.get("lighthouse_mobile")),
        "lighthouse_error_count": len(data.get("lighthouse_errors") or []),
        "fetch_warning": data.get("fetch_warning"),
    }])


def main() -> None:
    parser = argparse.ArgumentParser(description="Export audit JSON to structured pandas tables.")
    parser.add_argument("report_path", help="Path to full_report.json")
    parser.add_argument("--out", default=None, help="Output directory (default: same folder as report)")
    parser.add_argument("--excel", action="store_true", help="Also write a single multi-sheet .xlsx")
    args = parser.parse_args()

    data = load_report(args.report_path)
    out_dir = args.out or os.path.dirname(args.report_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    tables = {
        "summary": build_summary_row(data),
        "scores": build_scores_table(data),
        "issues": build_issues_table(data),
        "lighthouse": build_lighthouse_table(data),
        "lighthouse_errors": build_lighthouse_errors_table(data),
    }

    for name, df in tables.items():
        csv_path = os.path.join(out_dir, f"{name}.csv")
        df.to_csv(csv_path, index=False)
        print(f"Saved {name} ({len(df)} rows) -> {csv_path}")

    if args.excel:
        xlsx_path = os.path.join(out_dir, "audit_tables.xlsx")
        with pd.ExcelWriter(xlsx_path) as writer:
            for name, df in tables.items():
                df.to_excel(writer, sheet_name=name[:31], index=False)
        print(f"Saved combined workbook -> {xlsx_path}")

    # Quick console preview
    print("\n=== Summary ===")
    print(tables["summary"].to_string(index=False))
    print("\n=== Scores ===")
    print(tables["scores"].to_string(index=False))
    if not tables["issues"].empty:
        print("\n=== Top issues ===")
        print(tables["issues"][["priority", "category", "problem"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()