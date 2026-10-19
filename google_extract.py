"""Pull campaign-level daily performance and write it to CSV.

This is the piece that actually produces something -- the other scripts prove
the connection, the inventory, and the incremental mechanism. Output is one
row per campaign per day in the normalized schema defined in
google_client.COLUMNS, which is deliberately identical to the Meta PoC's
schema except for the account identifier, so the two sources union in Silver
without reshaping.

CSV is a PoC convenience and nothing more. The real source-google-ads
connector writes only to the CLIENT'S OWN ADLS Gen2, via the platform's
shared_utils.streaming_upload -- never to local disk, and never to any
Skypoint-owned storage. That rule holds even though the credential doing the
fetching is Skypoint's.

    python google_extract.py
    python google_extract.py --days 30
    python google_extract.py --since 2026-09-01 --until 2026-09-30
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

from google_client import (
    COLUMNS,
    NOT_CONFIGURED_HELP,
    GoogleAdsAuthError,
    GoogleAdsError,
    RateLimitBudget,
    TokenManager,
    banner,
    fetch_campaign_daily,
    load_config,
    mode_line,
)

BUDGET = RateLimitBudget()
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def write_csv(rows: list[dict], path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)  # bare filename has no dir
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def summarize(rows: list[dict]) -> None:
    """A quick per-campaign rollup, so a human can sanity-check the pull
    without opening the CSV."""
    if not rows:
        print("   (no rows)")
        return

    by_campaign: dict[str, dict] = defaultdict(
        lambda: {"days": 0, "impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0.0}
    )
    for r in rows:
        agg = by_campaign[r["campaign_name"] or r["campaign_id"]]
        agg["days"] += 1
        agg["impressions"] += r["impressions"]
        agg["clicks"] += r["clicks"]
        agg["spend"] += r["spend"]
        agg["conversions"] += r["conversions"]

    print(f"   {'campaign':30s} {'days':>5} {'impr':>10} {'clicks':>8} "
          f"{'spend':>10} {'conv':>8} {'CPA':>8}")
    print("   " + "-" * 84)
    totals = {"days": 0, "impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0.0}
    for name, a in sorted(by_campaign.items(), key=lambda kv: -kv[1]["spend"]):
        cpa = a["spend"] / a["conversions"] if a["conversions"] else 0.0
        print(f"   {name[:30]:30s} {a['days']:>5} {a['impressions']:>10,} "
              f"{a['clicks']:>8,} {a['spend']:>10,.2f} {a['conversions']:>8.2f} "
              f"{cpa:>8.2f}")
        for k in totals:
            totals[k] += a[k]
    cpa = totals["spend"] / totals["conversions"] if totals["conversions"] else 0.0
    print("   " + "-" * 84)
    print(f"   {'TOTAL':30s} {'':>5} {totals['impressions']:>10,} "
          f"{totals['clicks']:>8,} {totals['spend']:>10,.2f} "
          f"{totals['conversions']:>8.2f} {cpa:>8.2f}")
    print("\n   Conversions are fractional by design: data-driven attribution")
    print("   splits credit across touchpoints, so 3.67 is a normal value.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7,
                        help="how many days back from yesterday (default 7)")
    parser.add_argument("--since", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--until", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--out", help="output CSV path (default output/google_ads_campaign_daily_<customer>_<date>_<time>.csv)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()

    until = args.until or date.today() - timedelta(days=1)
    since = args.since or until - timedelta(days=args.days - 1)
    if since > until:
        print(f"ERROR: --since {since} is after --until {until}")
        return 2

    print("=" * 68)
    print("Google Ads -- campaign daily extract")
    print(f"mode: {mode_line(cfg)}")
    if not cfg.is_configured:
        print()
        print(NOT_CONFIGURED_HELP)
        return 2

    banner("1", f"Extract  ->  {since} .. {until}  (campaign grain, one row per day)")
    tokens = TokenManager(cfg, BUDGET)
    try:
        rows = fetch_campaign_daily(cfg, since, until, tokens=tokens, verbose=True)
    except GoogleAdsAuthError as exc:
        print(f"\nFATAL  {exc}")
        return 1
    except GoogleAdsError as exc:
        print(f"\nERROR  {exc}")
        return 1
    print(f"   {len(rows)} rows")

    banner("2", "Rollup")
    summarize(rows)

    banner("3", "Write")
    account = cfg.customer
    # Date AND time, so a second run the same day never overwrites the first.
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    default_name = f"google_ads_campaign_daily_{account}_{stamp}.csv"
    path = args.out or os.path.join(OUTPUT_DIR, default_name)
    write_csv(rows, path)
    print(f"   {len(rows)} rows -> {os.path.relpath(path)}")
    print(f"   columns: {', '.join(COLUMNS)}")
    print("\n   PoC output only. The real connector writes to the client's own")
    print("   ADLS Gen2 via shared_utils.streaming_upload (buffered per stream,")
    print("   flushed at 10,000 rows or 10MB) -- never to local disk.")

    print("=" * 68)
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
