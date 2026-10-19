"""Demonstrate the incremental extraction strategy for Google Ads.

The analogue of geotab_feed.py -- but, as with Meta, the answer is genuinely
different, and that difference is the most important finding in this PoC.

Geotab hands you a CDC cursor: GetFeed(fromVersion) returns exactly the
records added since your stored token. Append-only, no gaps, no duplicates,
no date math.

Google Ads has no such thing. GAQL is a date-windowed query, and the rows it
returns for a given date KEEP CHANGING afterwards, because a conversion is
credited back to the date of the CLICK that caused it, not the date it
happened. Someone who clicks on the 3rd and buys on the 20th adds a
conversion to the 3rd, seventeen days later.

So the incremental pattern is:
    watermark  = last complete date successfully loaded
    pull       = (watermark - conversion window) .. yesterday
    load       = UPSERT on (date, campaign_id), never INSERT

WHERE THIS DIFFERS FROM META, and it is the reason the two connectors are not
copies of each other:

  * Meta's window is a fixed 28 days -- the widest standard attribution
    window, the same for every account.
  * Google's window is a PER-ACCOUNT SETTING. The default is 30 days, but an
    account can configure up to 90, and different conversion actions in the
    same account can differ. Hardcoding 28 or 30 here would silently lose
    conversions on any account configured wider.

That makes the lookback a value to READ, not assume. read the real
per-account windows from conversion_action (CONVERSION_WINDOW_QUERY in
google_client.py) and keep the configured lookback at or above the widest one.

    python google_incremental.py           # run it twice to see the diff
    python google_incremental.py --reset   # forget the stored watermark
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from google_client import (
    NOT_CONFIGURED_HELP,
    GoogleAdsAuthError,
    GoogleAdsConfig,
    GoogleAdsError,
    RateLimitBudget,
    TokenManager,
    banner,
    campaign_daily_query,
    fetch_campaign_daily,
    load_config,
    mode_line,
    row_key,
)

BUDGET = RateLimitBudget()

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_PATH = os.path.join(STATE_DIR, "google_ads_watermark.json")

# How far back the very first run reaches when there is no watermark yet.
# In production this is the backfill horizon agreed with the client.
INITIAL_BACKFILL_DAYS = 14


# --------------------------------------------------------------------------
# Watermark -- the analogue of Geotab's stored toVersion token
# --------------------------------------------------------------------------


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def fingerprint(rows: list[dict]) -> dict:
    """Just the volatile metrics, keyed by the merge key. In production this
    is a control table, not a JSON file -- but the shape is the same: one row
    per grain, holding what we last believed to be true."""
    return {
        row_key(r): {
            "spend": r["spend"],
            "conversions": r["conversions"],
            "impressions": r["impressions"],
        }
        for r in rows
    }


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true",
                        help="delete the stored watermark and exit")
    parser.add_argument("--days", type=int, default=INITIAL_BACKFILL_DAYS,
                        help=f"first-run backfill horizon (default {INITIAL_BACKFILL_DAYS})")
    args = parser.parse_args()

    if args.reset:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
            print(f"Removed {STATE_PATH}")
        else:
            print("No stored watermark to remove.")
        return 0

    cfg = load_config()
    print("=" * 68)
    print("Google Ads -- incremental extraction strategy")
    print(f"mode: {mode_line(cfg)}")
    if not cfg.is_configured:
        print()
        print(NOT_CONFIGURED_HELP)
        return 2

    yesterday = date.today() - timedelta(days=1)
    state = load_state()
    lookback = cfg.restatement_days

    # ---- 1) Work out the window from the watermark ------------------------
    banner("1", "Window selection")
    print(f"   conversion-window lookback : {lookback} days")
    print("   (per-account setting, not a constant -- keep it at or above the widest")
    print("    click_through_lookback_window_days on the account's conversion actions)")
    if state:
        watermark = date.fromisoformat(state["last_complete_date"])
        since = watermark - timedelta(days=lookback)
        print(f"   stored watermark   : {watermark}")
        print(f"   -> re-pulling from : {since}  (watermark minus the window)")
        print("   The overlap is not waste. It is what makes the load self-healing:")
        print("   a missed run catches up, and a restated day gets corrected.")
    else:
        since = yesterday - timedelta(days=args.days - 1)
        print(f"   no watermark -- first run, backfilling {args.days} days")
        print("   The restatement diff in step 3 needs a stored prior run to")
        print("   compare against, so it appears from the SECOND run onwards.")
    until = yesterday
    print(f"   window             : {since} .. {until}")

    # ---- 2) Pull -----------------------------------------------------------
    banner("2", "Pull  ->  POST customers/<id>/googleAds:search")
    print("   GAQL:")
    for line in campaign_daily_query(since, until).strip().splitlines():
        print(f"     {line.strip()}")
    print("\n   segments.date is what makes this daily -- without it the metrics")
    print("   come back rolled up across the whole range. Paging is by")
    print("   nextPageToken, which we send back as pageToken on the next call.")
    print("   Days with no activity are omitted entirely rather than returned")
    print("   as zeros, so row counts are not days x campaigns.\n")

    tokens = TokenManager(cfg, BUDGET)
    try:
        rows = fetch_campaign_daily(cfg, since, until, tokens=tokens)
    except GoogleAdsAuthError as exc:
        print(f"\nFATAL  {exc}")
        return 1
    except GoogleAdsError as exc:
        print(f"\nERROR  {exc}")
        return 1
    print(f"   {len(rows)} rows over {(until - since).days + 1} days")

    # ---- 3) Diff against what we last believed -----------------------------
    banner("3", "Diff against the previous run")
    previous = state.get("rows", {})
    current = fingerprint(rows)

    if not previous:
        print("   No previous run to compare against. Run this script again to see")
        print("   which rows come back changed.")
    else:
        new_keys = [k for k in current if k not in previous]
        restated = [k for k in current if k in previous and current[k] != previous[k]]
        unchanged = [k for k in current if k in previous and current[k] == previous[k]]

        print(f"   new rows       : {len(new_keys)}")
        print(f"   RESTATED rows  : {len(restated)}   <-- same key, different numbers")
        print(f"   unchanged      : {len(unchanged)}")

        for key in restated[:6]:
            was, now = previous[key], current[key]
            day, campaign = key.split("|")
            print(f"     {day} campaign={campaign}  "
                  f"conversions {was['conversions']} -> {now['conversions']}"
                  f"   spend {was['spend']} -> {now['spend']}")
        if len(restated) > 6:
            print(f"     ... and {len(restated) - 6} more")

        if restated:
            print("\n   This is the finding. Those rows were already loaded once. An")
            print("   append-only load would now hold both versions and double-count")
            print("   every restated conversion. The load MUST upsert on")
            print("   (date, campaign_id) -- the merge key google_client.row_key builds.")
            print("\n   Note the shape of the change: spend is stable, conversions")
            print("   move. Anything derived from conversions -- CPA, ROAS,")
            print("   conversion rate -- inherits that instability.")

    # ---- 4) Advance the watermark ------------------------------------------
    banner("4", "Advance the watermark")
    save_state({
        "last_run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_complete_date": until.isoformat(),
        "lookback_days": lookback,
        "row_count": len(rows),
        "rows": current,
    })
    print(f"   last_complete_date = {until}")
    print(f"   lookback_days      = {lookback}")
    print(f"   written to {os.path.relpath(STATE_PATH)}")
    print("   In production this is one row in a control table, exactly like the")
    print("   Geotab design stores toVersion per feed entity -- same idea, except")
    print("   the value is a date rather than a vendor-issued cursor, and the")
    print("   lookback travels with it because it is per-account.")

    print("=" * 68)
    print("DONE  -- re-run after a day or more to see live restatement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
