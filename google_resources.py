"""Catalogue the GAQL resources available on a Google Ads account.

The analogue of geotab_tables.py, which probed 55 MyGeotab entity types and
reported which were accessible, empty, or denied. Same idea here, but the
underlying model is different in a way worth naming:

Geotab exposes a fixed set of ENTITY TYPES you call Get/GetFeed against.
Meta exposes EDGES hanging off an ad account node.
Google Ads exposes RESOURCES you write SQL-ish queries against -- GAQL -- and
the same metrics can be segmented many different ways. So "what tables exist"
is really "what resources can this account report on", and the answer depends
on what the account actually uses: a search-only account has no shopping or
PMax asset-group resources with data in them.

Below are the 46 reportable resources worth knowing about for an ads
reporting pipeline, each with what it is and what it needs. With credentials
set, every one is probed live and classified.

    python google_resources.py
    python google_resources.py --all      # include the heavy/segmented ones
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from google_client import (
    GoogleAdsAuthError,
    GoogleAdsConfig,
    GoogleAdsDeveloperTokenError,
    GoogleAdsError,
    GoogleAdsPermissionError,
    RateLimitBudget,
    TokenManager,
    banner,
    gaql_search,
    load_config,
    mode_line,
)

BUDGET = RateLimitBudget()


# (resource, probe_field, group, note)
#
# probe_field is the cheapest selectable field on that resource -- enough to
# prove the resource answers at all without pulling anything heavy.
RESOURCES: list[tuple[str, str, str, str]] = [
    # --- account structure: the reference data a warehouse needs first -----
    ("customer",                  "customer.id",                   "core",      "The account itself: name, currency, timezone"),
    ("customer_client",           "customer_client.id",            "core",      "Accounts linked under an MCC -- the hierarchy"),
    ("customer_client_link",      "customer_client_link.status",   "core",      "Manager->client link state, incl. pending invites"),
    ("customer_manager_link",     "customer_manager_link.status",  "core",      "The same link seen from the client side"),
    ("campaign",                  "campaign.id",                   "core",      "Campaigns: name, status, channel, dates"),
    ("campaign_budget",           "campaign_budget.id",            "core",      "Budget amounts and delivery method"),
    ("ad_group",                  "ad_group.id",                   "core",      "Ad groups under each campaign"),
    ("ad_group_ad",               "ad_group_ad.ad.id",             "core",      "The individual ads and their creative"),
    ("ad_group_criterion",        "ad_group_criterion.criterion_id", "core",    "Keywords, audiences, placements at ad-group level"),
    ("campaign_criterion",        "campaign_criterion.criterion_id", "core",    "Campaign-level targeting and exclusions"),
    ("bidding_strategy",          "bidding_strategy.id",           "core",      "Portfolio bid strategies"),
    ("conversion_action",         "conversion_action.id",          "core",      "Conversion definitions AND their lookback windows"),

    # --- performance: the metrics a reporting pipeline actually lands ------
    ("campaign (daily)",          "campaign.id",                   "perf",      "campaign + segments.date -- the PoC's main extract"),
    ("ad_group (daily)",          "ad_group.id",                   "perf",      "Same metrics one level down"),
    ("ad_group_ad (daily)",       "ad_group_ad.ad.id",             "perf",      "Creative-level performance"),
    ("keyword_view",              "ad_group_criterion.criterion_id", "perf",    "Per-keyword metrics"),
    ("search_term_view",          "search_term_view.search_term",  "perf",      "Actual queries that triggered ads"),
    ("campaign_search_term_insight", "campaign_search_term_insight.id", "perf", "Newer grouped search-theme reporting"),
    ("geographic_view",           "geographic_view.country_criterion_id", "perf", "Performance by location of interest/presence"),
    ("user_location_view",        "user_location_view.country_criterion_id", "perf", "Performance by physical user location"),
    ("age_range_view",            "ad_group_criterion.criterion_id", "perf",    "Demographic split -- age"),
    ("gender_view",               "ad_group_criterion.criterion_id", "perf",    "Demographic split -- gender"),
    ("parental_status_view",      "ad_group_criterion.criterion_id", "perf",    "Demographic split -- parental status"),
    ("managed_placement_view",    "ad_group_criterion.criterion_id", "perf",    "Display placements you chose"),
    ("detail_placement_view",     "detail_placement_view.placement", "perf",    "Display placements you actually appeared on"),
    ("topic_view",                "ad_group_criterion.criterion_id", "perf",    "Display topic targeting"),
    ("display_keyword_view",      "ad_group_criterion.criterion_id", "perf",    "Display keyword targeting"),
    ("landing_page_view",         "landing_page_view.unexpanded_final_url", "perf", "Performance by landing page"),
    ("click_view",                "click_view.gclid",              "perf",      "Individual clicks -- 90-day retention, ONE day per query"),

    # --- shopping / PMax: present only if the account runs them ------------
    ("shopping_performance_view", "segments.product_item_id",      "shopping",  "Shopping metrics by product"),
    ("product_group_view",        "ad_group_criterion.criterion_id", "shopping", "Shopping product group tree"),
    ("asset",                     "asset.id",                      "shopping",  "Images, text, video assets"),
    ("asset_group",               "asset_group.id",                "shopping",  "Performance Max asset groups"),
    ("asset_group_asset",         "asset_group_asset.field_type",  "shopping",  "Which assets sit in which PMax group"),
    ("campaign_asset",            "campaign_asset.field_type",     "shopping",  "Assets attached at campaign level"),
    ("customer_asset",            "customer_asset.field_type",     "shopping",  "Account-level assets"),

    # --- audiences, labels, admin ------------------------------------------
    ("audience",                  "audience.id",                   "admin",     "Saved audience definitions"),
    ("user_list",                 "user_list.id",                  "admin",     "Remarketing lists"),
    ("label",                     "label.id",                      "admin",     "Labels for grouping entities"),
    ("campaign_label",            "campaign_label.campaign",       "admin",     "Label attachments"),
    ("change_event",              "change_event.change_date_time", "admin",     "Who changed what -- 30-day retention"),
    ("change_status",             "change_status.last_change_date_time", "admin", "Lighter-weight change feed"),
    ("recommendation",            "recommendation.type",           "admin",     "Google's own optimisation suggestions"),
    ("billing_setup",             "billing_setup.id",              "admin",     "Payment profile linkage"),
    ("account_budget",            "account_budget.id",             "admin",     "Account-level budget / PO"),
    ("paid_organic_search_term_view", "paid_organic_search_term_view.search_term", "admin", "Paid+organic -- needs a Search Console link"),
]

# Resources that only answer with a date segment in the query, and in one
# case (click_view) only for a single day at a time.
NEEDS_DATE = {
    "campaign (daily)", "ad_group (daily)", "ad_group_ad (daily)",
    "keyword_view", "search_term_view", "campaign_search_term_insight",
    "geographic_view", "user_location_view", "age_range_view", "gender_view",
    "parental_status_view", "managed_placement_view", "detail_placement_view",
    "topic_view", "display_keyword_view", "landing_page_view", "click_view",
    "shopping_performance_view", "product_group_view",
}
SINGLE_DAY_ONLY = {"click_view"}

HEAVY = {"shopping", "admin"}


def _resource_name(label: str) -> str:
    """'campaign (daily)' is a probe label, not a GAQL resource name."""
    return label.split(" (")[0]


def build_probe(label: str, probe_field: str) -> str:
    resource = _resource_name(label)
    if label in SINGLE_DAY_ONLY:
        day = (date.today() - timedelta(days=2)).isoformat()
        return (f"SELECT {probe_field}, segments.date FROM {resource} "
                f"WHERE segments.date = '{day}' LIMIT 1")
    if label in NEEDS_DATE:
        since = (date.today() - timedelta(days=7)).isoformat()
        until = (date.today() - timedelta(days=1)).isoformat()
        return (f"SELECT {probe_field}, segments.date, metrics.impressions "
                f"FROM {resource} "
                f"WHERE segments.date BETWEEN '{since}' AND '{until}' LIMIT 1")
    return f"SELECT {probe_field} FROM {resource} LIMIT 1"


def catalogue(include_heavy: bool) -> int:
    rows = [r for r in RESOURCES if include_heavy or r[2] not in HEAVY]
    banner("1", f"Resource catalogue ({len(rows)} of {len(RESOURCES)})")
    current = None
    for label, _field, group, note in rows:
        if group != current:
            current = group
            print(f"\n   -- {group} " + "-" * (58 - len(group)))
        print(f"   {label:32s} {note}")
    return len(rows)


def probe(cfg: GoogleAdsConfig, include_heavy: bool) -> int:
    rows = [r for r in RESOURCES if include_heavy or r[2] not in HEAVY]
    tokens = TokenManager(cfg, BUDGET)

    banner("2", f"Live probe -- {len(rows)} resources on customer {cfg.customer}")
    print("   Each is one GAQL query with LIMIT 1. Date-segmented resources get")
    print("   a 7-day window; click_view gets a single day (its own constraint).\n")
    print(f"   {'resource':32s} {'status':14s} detail")
    print("   " + "-" * 84)

    has_data, empty, denied, unavailable = [], [], [], []

    for label, field, _group, _note in rows:
        query = build_probe(label, field)
        try:
            results: list = []
            for _page, batch in gaql_search(cfg, query, tokens=tokens, max_pages=1):
                results.extend(batch)
            if results:
                has_data.append(label)
                print(f"   {label:32s} {'has data':14s}")
            else:
                empty.append(label)
                print(f"   {label:32s} {'empty':14s} accessible, no rows in window")
        except GoogleAdsDeveloperTokenError as exc:
            print(f"\n   Stopping: {exc}")
            return 1
        except GoogleAdsPermissionError as exc:
            denied.append(label)
            print(f"   {label:32s} {'denied':14s} {str(exc)[:38]}")
        except GoogleAdsError as exc:
            unavailable.append(label)
            print(f"   {label:32s} {'unavailable':14s} {str(exc)[:38]}")

    banner("3", "Summary")
    print(f"   has data    : {len(has_data)}")
    print(f"   empty       : {len(empty)}   (accessible; the account doesn't use it)")
    print(f"   denied      : {len(denied)}")
    print(f"   unavailable : {len(unavailable)}")
    print(f"   total       : {len(rows)}")
    if empty:
        print("\n   'empty' is the expected answer for most of these on a typical")
        print("   account -- a search-only advertiser has no shopping or PMax rows.")
        print("   It is information, not a failure.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true",
                        help="include shopping/PMax and admin resources")
    args = parser.parse_args()

    cfg = load_config()
    print("=" * 68)
    print("Google Ads -- GAQL resource inventory")
    print(f"mode: {mode_line(cfg)}")

    count = catalogue(args.all)

    if not cfg.is_configured:
        banner("2", "Live probe")
        print("   Skipped -- needs an OAuth credential and a customer id.")
        print("   The catalogue above is the part that does not")
        print(f"   depend on access; {count} resources documented.")
        print("=" * 68)
        print("DONE  (catalogue only)")
        return 0

    try:
        rc = probe(cfg, args.all)
    except GoogleAdsAuthError as exc:
        print(f"\nFATAL  {exc}")
        return 1
    print("=" * 68)
    print("DONE")
    return rc


if __name__ == "__main__":
    sys.exit(main())
