"""
AffordRX Pricing Validation Pipeline
====================================

WHAT THIS SCRIPT DOES (in plain English):
  You scrape drug prices from GoodRx and save them in CSV files like all_data.csv.
  This script checks whether those prices look reasonable.

  It does that by comparing your scraped prices to NADAC — a free government
  database that lists what pharmacies typically PAY wholesalers for each drug
  (think: the pharmacy's "cost price", not what you pay at the counter).

  Example:
    - NADAC says 30 amlodipine tablets cost the pharmacy about $0.33 wholesale.
    - GoodRx might show you pay $8 at Walmart or $15 at CVS.
    - Both retail prices are WAY higher than $0.33 — that's normal!
    - So we compare MARKUP (retail ÷ wholesale), not raw dollars.

  If one pharmacy's price is wildly different from other pharmacies for the
  same drug, we flag it so you can re-scrape or fix bad data before it goes
  live on your website.

HOW TO RUN (from the project root folder):
  python scripts/pricing_pipeline.py
  python scripts/pricing_pipeline.py --csv all_data.csv --csv greenville_data.csv
  python scripts/pricing_pipeline.py --threshold 0.25
  python scripts/pricing_pipeline.py --skip-nadac-fetch

OUTPUT FILES (written to scripts/output/):
  - nadac_snapshot.json           — wholesale costs we looked up from CMS
  - pricing_validation_report.csv   — every row with extra columns explaining the math
  - pricing_flags.csv               — only the suspicious rows
  - pricing_summary.json            — one summary row per drug
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# CONSTANTS — numbers and URLs we use in many places
# ---------------------------------------------------------------------------

# This is the ID for the NADAC dataset on data.medicaid.gov (government site).
# NADAC = National Average Drug Acquisition Cost.
NADAC_DATASET_ID = "d5eaf378-dcef-5779-83de-acdd8347d68e"
NADAC_QUERY_URL = f"https://data.medicaid.gov/api/1/datastore/query/{NADAC_DATASET_ID}/0"

# Figure out folder paths automatically so the script works no matter where you run it from.
SCRIPT_DIR = Path(__file__).resolve().parent          # .../AffordRX.github.io/scripts
PROJECT_ROOT = SCRIPT_DIR.parent                      # .../AffordRX.github.io
BASELINE_FILE = SCRIPT_DIR / "drug_ndc_baseline.json" # maps drug names → NADAC search terms
OUTPUT_DIR = SCRIPT_DIR / "output"                    # where reports are saved

# DEFAULT_THRESHOLD = 0.30 means "flag if a price is more than 30% away from typical".
DEFAULT_THRESHOLD = 0.30

# A retail price below 1.2× wholesale is suspicious (pharmacy would lose money).
MIN_MARKUP = 1.2

# A retail price above 25× wholesale is suspicious (might be wrong scrape or mapping).
MAX_MARKUP = 25.0


# ---------------------------------------------------------------------------
# DATA CLASSES — small containers that group related fields together
# (Like a labeled box instead of loose variables everywhere.)
# ---------------------------------------------------------------------------

@dataclass
class DrugBaseline:
    """
    One drug's "baseline SKU" — the exact strength and count you saved on GoodRx.

    Example: Ibuprofen 200 mg, 30 tablets.
    We need this because NADAC prices are per pill/bottle, and your CSV price
    is for a whole package.
    """
    name: str              # Display name, e.g. "Ibuprofen"
    quantity: float        # How many units in the package, e.g. 30 tablets
    nadac_search: str      # Text to search in the government database
    prefer: List[str] = field(default_factory=list)   # Words that make a match more likely
    exclude: List[str] = field(default_factory=list)  # Words that mean "wrong product"


@dataclass
class NadacRecord:
    """One row returned from the NADAC government database."""
    ndc: str               # National Drug Code — unique product ID
    description: str       # Human-readable name, e.g. "IBUPROFEN 200 MG TABLET"
    nadac_per_unit: float  # Wholesale cost for ONE unit (one pill, one mL, etc.)
    effective_date: str    # When this price became active
    pricing_unit: str      # Usually "EA" (each) or "ML" (milliliter)
    otc: str               # "Y" if over-the-counter, "N" if prescription


@dataclass
class NadacDrugSummary:
    """
    Everything we know about one drug's NADAC lookup after processing.

    nadac_wholesale_total = nadac_per_unit × quantity
    (e.g. $0.01 per pill × 30 pills = $0.30 total wholesale for the bottle)
    """
    drug_name: str
    quantity: float
    nadac_per_unit: float
    nadac_wholesale_total: float
    ndc: str
    description: str
    effective_date: str
    pricing_unit: str
    otc: str
    lookup_status: str     # "ok_exact", "ok_contains", "not_found", etc.


# ---------------------------------------------------------------------------
# STEP 1: Load drug baseline definitions from JSON
# ---------------------------------------------------------------------------

def load_baselines(path: Path) -> Dict[str, DrugBaseline]:
    """
    Read drug_ndc_baseline.json and return a dictionary keyed by lowercase drug name.

    We use lowercase keys so "Ibuprofen" and "ibuprofen" match the same entry.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    baselines: Dict[str, DrugBaseline] = {}

    for row in payload.get("drugs", []):
        name = str(row["name"]).strip()
        baselines[name.lower()] = DrugBaseline(
            name=name,
            quantity=float(row.get("quantity", 30)),
            nadac_search=str(row["nadac_search"]).strip(),
            prefer=[str(x).upper() for x in row.get("prefer", [])],
            exclude=[str(x).upper() for x in row.get("exclude", [])],
        )
    return baselines


# ---------------------------------------------------------------------------
# STEP 2: Talk to the CMS NADAC API (free, no API key needed)
# ---------------------------------------------------------------------------

def post_json(url: str, body: dict, timeout: int = 45) -> dict:
    """
    Send a POST request with JSON body and get JSON back.

    Think of it like filling out a search form on a website, but the computer
    does it automatically and gets structured data back.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rank_nadac_candidates(records: Sequence[dict], baseline: DrugBaseline) -> List[dict]:
    """
    NADAC search can return multiple products (e.g. ibuprofen 200 mg vs 600 mg).

    We score each result:
      +2 points for each "prefer" word found (TABLET, CAPSULE, etc.)
      -5 points for each "exclude" word (COD = codeine combo, wrong drug)

    Highest score wins. Tie-breaker: newer effective_date.
    """
    def score(rec: dict) -> Tuple[int, str]:
        desc = str(rec.get("ndc_description", "")).upper()
        points = 0
        for token in baseline.prefer:
            if token in desc:
                points += 2
        for token in baseline.exclude:
            if token in desc:
                points -= 5
        return (points, str(rec.get("effective_date", "")))

    return sorted(records, key=score, reverse=True)


def fetch_nadac_by_description(description: str, operator: str = "=", limit: int = 25) -> List[dict]:
    """
    Search NADAC by drug description text.

    operator can be:
      "="           — exact match only
      "contains"    — description must include this text
      "starts with" — description must begin with this text
    """
    body = {
        "conditions": [{"property": "ndc_description", "operator": operator, "value": description}],
        "limit": limit,
        "sorts": [{"property": "effective_date", "order": "desc"}],  # newest first
    }
    payload = post_json(NADAC_QUERY_URL, body)
    return payload.get("results", [])


def lookup_nadac_for_drug(baseline: DrugBaseline) -> Tuple[Optional[NadacRecord], str]:
    """
    Try up to 3 search strategies to find NADAC data for one drug.

    Strategy order (stop at first success):
      1. Exact match on full search string
      2. "Contains" partial match
      3. "Starts with" first word only (fallback when names differ slightly)

    Returns (record, status) or (None, "not_found").
    """
    first_token = baseline.nadac_search.split()[0] if baseline.nadac_search else ""
    attempts = [
        ("exact", "=", baseline.nadac_search),
        ("contains", "contains", baseline.nadac_search),
        ("starts_with", "starts with", first_token),
    ]

    for label, operator, term in attempts:
        if not term:
            continue
        try:
            results = fetch_nadac_by_description(term, operator=operator)
        except urllib.error.URLError as exc:
            return None, f"api_error:{exc}"

        if not results:
            continue

        ranked = rank_nadac_candidates(results, baseline)
        best = ranked[0]
        try:
            per_unit = float(best["nadac_per_unit"])
        except (KeyError, TypeError, ValueError):
            continue

        record = NadacRecord(
            ndc=str(best.get("ndc", "")),
            description=str(best.get("ndc_description", "")),
            nadac_per_unit=per_unit,
            effective_date=str(best.get("effective_date", "")),
            pricing_unit=str(best.get("pricing_unit", "")),
            otc=str(best.get("otc", "")),
        )
        return record, f"ok_{label}"

    return None, "not_found"


def build_nadac_summaries(baselines: Dict[str, DrugBaseline], pause_s: float = 0.15) -> Dict[str, NadacDrugSummary]:
    """
    Look up NADAC for every drug in the baseline file.

    pause_s: small delay between API calls so we don't hammer the government server.
    """
    summaries: Dict[str, NadacDrugSummary] = {}

    for key in sorted(baselines):
        baseline = baselines[key]
        record, status = lookup_nadac_for_drug(baseline)

        if record is None:
            # Lookup failed — store zeros so we know to use fallback checks later.
            summaries[key] = NadacDrugSummary(
                drug_name=baseline.name,
                quantity=baseline.quantity,
                nadac_per_unit=0.0,
                nadac_wholesale_total=0.0,
                ndc="",
                description="",
                effective_date="",
                pricing_unit="",
                otc="",
                lookup_status=status,
            )
        else:
            # Multiply per-unit wholesale by package size to match your GoodRx scrape.
            wholesale_total = record.nadac_per_unit * baseline.quantity
            summaries[key] = NadacDrugSummary(
                drug_name=baseline.name,
                quantity=baseline.quantity,
                nadac_per_unit=record.nadac_per_unit,
                nadac_wholesale_total=round(wholesale_total, 4),
                ndc=record.ndc,
                description=record.description,
                effective_date=record.effective_date,
                pricing_unit=record.pricing_unit,
                otc=record.otc,
                lookup_status=status,
            )

        time.sleep(pause_s)

    return summaries


# ---------------------------------------------------------------------------
# STEP 3: Load your scraped GoodRx prices from CSV
# ---------------------------------------------------------------------------

def parse_price(raw: str) -> Optional[float]:
    """
    Turn "$12.34" or "12.34" into the number 12.34.
    Returns None if the text is empty or not a number.
    """
    if raw is None:
        return None
    cleaned = str(raw).strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def load_scraped_csv(path: Path) -> List[dict]:
    """
    Read all_data.csv (or greenville_data.csv).

    Expected columns: Name, Pharmacy, Price
    (Also accepts lowercase: name, pharmacy, price)

    Skips rows that are missing a drug name, pharmacy, or valid price.
    """
    rows: List[dict] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = (row.get("Name") or row.get("name") or "").strip()
            pharmacy = (row.get("Pharmacy") or row.get("pharmacy") or "").strip()
            price = parse_price(row.get("Price") or row.get("price") or "")
            if not name or not pharmacy or price is None:
                continue
            rows.append({"name": name, "pharmacy": pharmacy, "price": price})
    return rows


def median_by_drug(rows: Sequence[dict]) -> Dict[str, float]:
    """
    For each drug, find the median scraped price across all pharmacies.

    Median = middle value when sorted (less affected by one crazy outlier than average).
    Used as a fallback when NADAC lookup fails.
    """
    buckets: Dict[str, List[float]] = {}
    for row in rows:
        key = row["name"].strip().lower()
        buckets.setdefault(key, []).append(float(row["price"]))
    return {drug: statistics.median(prices) for drug, prices in buckets.items() if prices}


def median_markup_by_drug(
    rows: Sequence[dict],
    nadac_summaries: Dict[str, NadacDrugSummary],
) -> Dict[str, float]:
    """
    For each drug, compute the median RETAIL MARKUP across pharmacies.

    retail_markup = scraped_price / nadac_wholesale_total

    Example: $8 retail ÷ $0.33 wholesale ≈ 24× markup (normal for generics).
    """
    buckets: Dict[str, List[float]] = {}
    for row in rows:
        drug_key = row["name"].strip().lower()
        nadac = nadac_summaries.get(drug_key)
        if not nadac or not nadac.lookup_status.startswith("ok") or nadac.nadac_wholesale_total <= 0:
            continue
        markup = float(row["price"]) / nadac.nadac_wholesale_total
        buckets.setdefault(drug_key, []).append(markup)

    return {drug: statistics.median(values) for drug, values in buckets.items() if values}


# ---------------------------------------------------------------------------
# STEP 4: Validate each row and decide whether to flag it
# ---------------------------------------------------------------------------

def validate_rows(
    rows: Sequence[dict],
    baselines: Dict[str, DrugBaseline],
    nadac_summaries: Dict[str, NadacDrugSummary],
    threshold: float,
    source_label: str,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    Main validation logic. For every drug + pharmacy row:

    1. If we have NADAC data:
       - Compute retail_markup = price / wholesale_total
       - Compare to median markup for that drug across chains
       - Flag if markup drifts more than `threshold` (default 30%)
       - Flag if price is impossibly low or impossibly high vs wholesale

    2. If NADAC lookup failed:
       - Fall back to comparing price to median scraped price (peer_drift)
       - Flag that NADAC was missing

    Returns three lists:
      - report: every row with extra columns
      - flags: only suspicious rows
      - summary_rows: per-drug counts
    """
    medians = median_by_drug(rows)
    median_markups = median_markup_by_drug(rows, nadac_summaries)
    report: List[dict] = []
    flags: List[dict] = []
    drug_stats: Dict[str, dict] = {}

    for row in rows:
        drug_key = row["name"].strip().lower()
        price = float(row["price"])
        baseline = baselines.get(drug_key)
        nadac = nadac_summaries.get(drug_key)
        median_price = medians.get(drug_key)

        nadac_wholesale = nadac.nadac_wholesale_total if nadac else 0.0
        retail_markup = None
        median_retail_markup = median_markups.get(drug_key)
        pct_markup_drift = None
        pct_from_median = None
        flag_reasons: List[str] = []

        if nadac and nadac.lookup_status.startswith("ok") and nadac_wholesale > 0:
            # --- Primary path: we have government wholesale cost ---
            retail_markup = price / nadac_wholesale

            if median_retail_markup and median_retail_markup > 0:
                # How far is THIS pharmacy's markup from the typical markup?
                pct_markup_drift = abs(retail_markup - median_retail_markup) / median_retail_markup
                if pct_markup_drift > threshold:
                    flag_reasons.append(f"markup_drift>{threshold:.0%}")

            if price < nadac_wholesale * MIN_MARKUP:
                flag_reasons.append("below_wholesale_floor")

            if price > nadac_wholesale * MAX_MARKUP:
                flag_reasons.append("above_retail_ceiling")

            # If the typical markup for the whole drug looks crazy, our baseline
            # mapping (strength/qty/search text) might be wrong — not the scrape.
            if median_retail_markup is not None and (
                median_retail_markup < MIN_MARKUP or median_retail_markup > MAX_MARKUP
            ):
                flag_reasons.append("review_baseline_mapping")
        else:
            # --- Fallback path: no NADAC — compare to other scraped prices only ---
            if median_price and median_price > 0:
                pct_from_median = abs(price - median_price) / median_price
                if pct_from_median > threshold:
                    flag_reasons.append(f"peer_drift>{threshold:.0%}")

            if nadac and not nadac.lookup_status.startswith("ok"):
                flag_reasons.append(f"nadac_{nadac.lookup_status}")
            elif baseline is None:
                flag_reasons.append("unmapped_drug")
            elif nadac is None:
                flag_reasons.append("nadac_missing")

        # Build one output row with all the numbers so you can inspect in Excel.
        out = {
            "source": source_label,
            "name": row["name"],
            "pharmacy": row["pharmacy"],
            "scraped_price": round(price, 2),
            "median_scraped_price": round(median_price, 2) if median_price is not None else "",
            "pct_from_median": round(pct_from_median, 4) if pct_from_median is not None else "",
            "nadac_description": nadac.description if nadac else "",
            "nadac_ndc": nadac.ndc if nadac else "",
            "nadac_per_unit": nadac.nadac_per_unit if nadac else "",
            "baseline_quantity": baseline.quantity if baseline else "",
            "nadac_wholesale_total": round(nadac_wholesale, 4) if nadac_wholesale else "",
            "retail_markup_vs_nadac": round(retail_markup, 2) if retail_markup is not None else "",
            "median_retail_markup": round(median_retail_markup, 2) if median_retail_markup is not None else "",
            "pct_markup_drift": round(pct_markup_drift, 4) if pct_markup_drift is not None else "",
            "nadac_lookup_status": nadac.lookup_status if nadac else "missing_baseline",
            "flagged": "yes" if flag_reasons else "no",
            "flag_reasons": "; ".join(flag_reasons),
        }
        report.append(out)
        if flag_reasons:
            flags.append(out)

        # Track how many rows per drug were flagged (for pricing_summary.json).
        stats = drug_stats.setdefault(
            drug_key,
            {
                "name": row["name"],
                "source": source_label,
                "row_count": 0,
                "flagged_count": 0,
                "median_scraped_price": round(median_price, 2) if median_price is not None else None,
                "nadac_wholesale_total": round(nadac_wholesale, 4) if nadac_wholesale else None,
                "median_retail_markup": round(median_retail_markup, 2) if median_retail_markup is not None else None,
                "nadac_lookup_status": nadac.lookup_status if nadac else "missing_baseline",
            },
        )
        stats["row_count"] += 1
        if flag_reasons:
            stats["flagged_count"] += 1

    summary_rows = sorted(drug_stats.values(), key=lambda item: item["flagged_count"], reverse=True)
    return report, flags, summary_rows


# ---------------------------------------------------------------------------
# STEP 5: Write output files and print a human-readable summary
# ---------------------------------------------------------------------------

def write_csv(path: Path, rows: Sequence[dict]) -> None:
    """Save a list of dictionaries as a CSV file."""
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_snapshot(path: Path, summaries: Dict[str, NadacDrugSummary]) -> None:
    """
    Save NADAC lookup results to JSON so the next run can skip re-fetching
    (use --skip-nadac-fetch).
    """
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": NADAC_DATASET_ID,
        "drugs": [
            {
                "name": s.drug_name,
                "quantity": s.quantity,
                "nadac_per_unit": s.nadac_per_unit,
                "nadac_wholesale_total": s.nadac_wholesale_total,
                "ndc": s.ndc,
                "description": s.description,
                "effective_date": s.effective_date,
                "pricing_unit": s.pricing_unit,
                "otc": s.otc,
                "lookup_status": s.lookup_status,
            }
            for s in summaries.values()
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def print_summary(report: Sequence[dict], flags: Sequence[dict], threshold: float, source_label: str) -> None:
    """Print a short summary to the terminal after processing one CSV."""
    total = len(report)
    flagged = len(flags)
    nadac_ok = sum(1 for r in report if str(r.get("nadac_lookup_status", "")).startswith("ok"))
    print()
    print(f"=== Pricing validation ({source_label}) ===")
    print(f"Rows analyzed:      {total}")
    print(f"Rows flagged:       {flagged}")
    print(f"NADAC mapped rows:  {nadac_ok}")
    print(f"Drift threshold:    {threshold:.0%}")
    print()

    if flags:
        print("Top flagged rows:")
        for row in flags[:15]:
            print(
                f"  - {row['name']} @ {row['pharmacy']}: "
                f"${row['scraped_price']} ({row['flag_reasons']})"
            )
        if flagged > 15:
            print(f"  ... and {flagged - 15} more (see pricing_flags.csv)")
    else:
        print("No rows exceeded the drift threshold.")


def run_for_csv(
    csv_path: Path,
    source_label: str,
    baselines: Dict[str, DrugBaseline],
    nadac_summaries: Dict[str, NadacDrugSummary],
    threshold: float,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Load one CSV file and run validation on all its rows."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    rows = load_scraped_csv(csv_path)
    if not rows:
        raise ValueError(f"No usable rows found in {csv_path}")

    return validate_rows(rows, baselines, nadac_summaries, threshold, source_label)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Read command-line options like --csv, --threshold, --skip-nadac-fetch."""
    parser = argparse.ArgumentParser(description="Validate scraped drug prices against CMS NADAC.")
    parser.add_argument(
        "--csv",
        action="append",
        default=[],
        help="CSV path relative to project root (default: all_data.csv). Repeat for multiple files.",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Label for each --csv (default derives from filename).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Drift threshold as a fraction (default: {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--baseline-file",
        type=Path,
        default=BASELINE_FILE,
        help="Path to drug_ndc_baseline.json",
    )
    parser.add_argument(
        "--skip-nadac-fetch",
        action="store_true",
        help="Reuse scripts/output/nadac_snapshot.json instead of calling the CMS API.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Main entry point — runs when you type: python scripts/pricing_pipeline.py

    Order of operations:
      1. Load baseline drug definitions
      2. Fetch or load cached NADAC wholesale prices
      3. Validate each CSV you asked for
      4. Write report files to scripts/output/
    """
    args = parse_args(argv)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    baselines = load_baselines(args.baseline_file)
    snapshot_path = OUTPUT_DIR / "nadac_snapshot.json"

    if args.skip_nadac_fetch and snapshot_path.exists():
        # Reuse last week's NADAC data — faster, no internet needed for CMS.
        cached = json.loads(snapshot_path.read_text(encoding="utf-8"))
        nadac_summaries = {
            str(item["name"]).lower(): NadacDrugSummary(
                drug_name=item["name"],
                quantity=float(item["quantity"]),
                nadac_per_unit=float(item.get("nadac_per_unit") or 0),
                nadac_wholesale_total=float(item.get("nadac_wholesale_total") or 0),
                ndc=str(item.get("ndc") or ""),
                description=str(item.get("description") or ""),
                effective_date=str(item.get("effective_date") or ""),
                pricing_unit=str(item.get("pricing_unit") or ""),
                otc=str(item.get("otc") or ""),
                lookup_status=str(item.get("lookup_status") or "cached"),
            )
            for item in cached.get("drugs", [])
        }
        print(f"Loaded NADAC snapshot: {snapshot_path}")
    else:
        # Hit the government API once per drug (~51 calls, ~1 minute total).
        print(f"Fetching NADAC for {len(baselines)} baseline drugs from CMS...")
        nadac_summaries = build_nadac_summaries(baselines)
        write_snapshot(snapshot_path, nadac_summaries)
        print(f"Wrote NADAC snapshot: {snapshot_path}")

    # Default to all_data.csv if you didn't pass --csv.
    csv_jobs = args.csv or ["all_data.csv"]
    source_labels = args.source or []
    while len(source_labels) < len(csv_jobs):
        csv_name = Path(csv_jobs[len(source_labels)]).stem
        source_labels.append(csv_name)

    all_report: List[dict] = []
    all_flags: List[dict] = []
    all_summary: List[dict] = []

    for csv_rel, source_label in zip(csv_jobs, source_labels):
        csv_path = PROJECT_ROOT / csv_rel
        report, flags, summary = run_for_csv(
            csv_path=csv_path,
            source_label=source_label,
            baselines=baselines,
            nadac_summaries=nadac_summaries,
            threshold=args.threshold,
        )
        all_report.extend(report)
        all_flags.extend(flags)
        all_summary.extend(summary)
        print_summary(report, flags, args.threshold, source_label)

    report_path = OUTPUT_DIR / "pricing_validation_report.csv"
    flags_path = OUTPUT_DIR / "pricing_flags.csv"
    summary_path = OUTPUT_DIR / "pricing_summary.json"
    write_csv(report_path, all_report)
    write_csv(flags_path, all_flags)
    summary_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "threshold": args.threshold,
                "sources": source_labels,
                "drugs": all_summary,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print(f"Full report: {report_path}")
    print(f"Flags only:  {flags_path}")
    print(f"Summary:     {summary_path}")
    return 0


# This line means: "only run main() if someone executed this file directly,
# not if they imported it from another Python file."
if __name__ == "__main__":
    sys.exit(main())
