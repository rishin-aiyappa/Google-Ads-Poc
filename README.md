# Google Ads Integration PoC — Skypoint

Google Ads only. The Meta Ads connector is a separate package (`Metaads- Poc/`)
and ships first; this one repeats the same three phases once Meta is proven.

Four Python modules plus a one-time token helper, `requests` only. No `google-ads`
SDK — the REST interface is called directly, so the extractor cannot break on an SDK version bump, and
the real connector runs on the Airbyte CDK anyway.

| File | What it does |
|------|--------------|
| `google_client.py` | Config, OAuth token manager, shared rate budget, error classification, GAQL helpers, normalization. Everything else goes through it. |
| `google_resources.py` | Catalogues 46 GAQL resources with what each needs; probes and classifies them live. |
| `google_incremental.py` | Watermark + restatement detection. |
| `google_extract.py` | Pulls a date window, rolls it up per campaign, writes the normalized CSV. |
| `get_refresh_token.py` | One-time helper to mint the OAuth refresh token. |

## Three run modes, picked automatically

| Mode | Triggered by | What it does |
|---|---|---|
| *(refuses)* | no OAuth credential | Nothing runs. Each script names what is missing and exits non-zero |
| `DISCOVERY` | credential, no customer | Not enough to pull data. The scripts refuse until `GOOGLE_ADS_CUSTOMER_ID` is set |
| `LIVE` | credential + customer | Full pulls |

No code changes between them — the mode follows what's in `.env`.

There is **deliberately no mock or synthetic mode.** An earlier revision had one and it was removed: invented rows are indistinguishable from real ones once they land in a CSV or a warehouse table, so a run started by mistake would contaminate real client data. Without credentials the scripts refuse to run and say what is missing. Every output this package produces is vendor data, or nothing.

The developer token is not part of any mode. Google sunset it on September 9,
2026 and the API ignores it, so `GOOGLE_ADS_DEVELOPER_TOKEN` may stay blank;
if it is set, it is still sent as a header, harmlessly.

## Where this stands

**Status as of 2026-10-20: working end to end.** The MCC exists: Skypoint Cloud Inc,
`775-317-9425`, set as `GOOGLE_ADS_LOGIN_CUSTOMER_ID` in `.env`.

- **Done:** OAuth client, refresh token, manager-link acceptance (the client
  account is `GOOGLE_ADS_CUSTOMER_ID` in `.env`), and Explorer API access
  (applied 2026-10-19; live pulls against the real account now succeed).
- **Done in code:** the API version bump to v25 and the page-size fix (steps 3
  and 4 below).
- **Validated:** a full pull returned real Kyrene School District data and
  matched the Google Ads UI. See `Google_Ads_Integration.md`, section 3.
- **Still open:** conversions by conversion action and anything below campaign
  level (see "What is NOT pulled"), and a clean restatement measurement a day
  or more after the first pulls.

**How we got here**, in order — each step unblocked the next:

1. **OAuth client** — done. The old developer-token flow (MCC → Admin → API
   Centre) is dead: Google sunset developer tokens on September 9, 2026, and
   access is now gated by the Cloud project behind the OAuth credentials. What
   we actually did:
   1. Created a Google Cloud project ("Skypoint Google Ads PoC") in Google
      Cloud Console, under `rishin.aiyappa@skypoint.ai`.
   2. Enabled the Google Ads API on that project (APIs & Services → Library →
      search "Google Ads API" → Enable).
   3. Configured the OAuth consent screen (Google Auth Platform → Overview →
      Get started): app name "Skypoint Google Ads PoC", support/developer email
      set, User type / Audience = **External**.
   4. Added `rishin.aiyappa@skypoint.ai` as a test user (Google Auth Platform →
      Audience → Test users). Required because the app is unverified: any
      account used to authorize must be added here first, or sign-in fails with
      "Access blocked… Error 403: access_denied".
   5. Created the OAuth client ID (Credentials → Create Credentials → OAuth
      client ID → Application type **Desktop app**, named "Skypoint Google Ads
      PoC Client") and copied the Client ID and Client Secret into `.env` as
      `GOOGLE_ADS_CLIENT_ID` / `GOOGLE_ADS_CLIENT_SECRET`.

   New integrations should apply for an API access level (**Explorer**, then
   **Basic** if needed) from the Google Ads API Overview page in Cloud Console,
   rather than requesting a developer token from the MCC.
   `GOOGLE_ADS_DEVELOPER_TOKEN` can stay blank — it is now optional and ignored
   by the API.
2. **Refresh token** — `python get_refresh_token.py`, signed in as an account
   listed as a test user (step 1.4). Because the app is unverified, Google shows
   a "Google hasn't verified this app" warning first: click *Advanced*, then
   continue. The script's local listener waits indefinitely for the redirect
   (Ctrl+C to abort), so the extra click is safe. Paste the value it prints
   into `.env` as `GOOGLE_ADS_REFRESH_TOKEN`.
3. **Use a supported API version.** `GOOGLE_ADS_API_VERSION` must be a version
   Google currently supports. The `v21` in the original config had already been
   sunset: `customers:listAccessibleCustomers` returned a 404. Fixed by setting
   `GOOGLE_ADS_API_VERSION=v25` in `.env`. Supported versions rotate (roughly
   three releases a year, each retired about a year later), so if a 404 on a
   previously working call reappears, check
   https://developers.google.com/google-ads/api/docs/sunset-dates and bump it.
4. **Don't send a page size on search requests.** The first live query failed
   with `requestError.PAGE_SIZE_NOT_SUPPORTED`. Google standardized page size
   across all API versions: search responses now always return a fixed 10,000
   rows per page and any explicit page size is rejected. In `google_client.py`
   we removed `pageSize` from the `gaql_search` request body and the
   `page_size` parameter from its signature, and in `google_resources.py` the
   `page_size=1` argument on the probe call (the probe already caps itself with
   `LIMIT 1` in the query). Paging still follows `nextPageToken`; use `LIMIT` in
   the query to cap rows.
5. **Apply for Explorer access.** A new Google Cloud project defaults to Test
   access only, which can reach test accounts but not real ones. Calls against a
   real account fail with
   `authorizationError.CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION`. To fix it:
   Cloud Console → APIs & Services → Enabled APIs & services → Google Ads API →
   **Access levels** card → Manage → Upgrade access level → Apply for access
   (the next level is **Explorer**). The application is auto-reviewed, typically
   within minutes. Applied 2026-10-19 and approved: real-account queries now
   work. Apply for Basic later only if Explorer's limits become a problem.
6. **A client accepts the manager link request** — client-side, not code. Done.

After step 2 the OAuth credential works, after step 5 queries reach the real
account, and after step 6 there is data to pull. All six are done.

## Setup for a new teammate

1. Python 3 (tested on 3.14). From this folder:
   `python -m venv .venv`, activate it, then `pip install -r requirements.txt`.
2. `copy .env.example .env` (macOS/Linux: `cp`), then fill in the three secrets:
   `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN`.
   Get them from whoever owns the Cloud project, over a secure channel. They are
   shared by the whole platform. Never commit `.env` (it is gitignored), and never
   paste them into a chat or ticket. Everything else in `.env.example` is already
   filled in (customer `8295303333`, MCC `7753179425`, API `v25`, lookback `90`).
   Leave `GOOGLE_ADS_DEVELOPER_TOKEN` blank.
3. `python google_extract.py --since 2025-01-01 --until <today>`. The CSV lands in
   `output/` (created automatically).

If it fails:

| Error | Meaning and fix |
|---|---|
| `... not configured`, naming a variable | That `.env` value is blank, or `.env` is not in the folder you ran from |
| `invalid_grant` / "re-mint the refresh token" | The refresh token is dead. While the OAuth app is in *Testing* status Google expires refresh tokens after 7 days, so this is expected periodically. Re-run `python get_refresh_token.py` and paste the new value into `.env` |
| `access_denied` / "Error 403" at sign-in | The Google account is not under Google Auth Platform → Audience → Test users. Add it, then retry |
| 404 on every call | `GOOGLE_ADS_API_VERSION` has been sunset. Check the sunset-dates page (see step 3 below) and bump it |
| `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` | The Cloud project's API access level has not been approved yet (step 5 below) |
| `USER_PERMISSION_DENIED` | `GOOGLE_ADS_LOGIN_CUSTOMER_ID` is missing or wrong. It must be the MCC |

## Quick start

```
pip install -r requirements.txt
python google_extract.py --since 2025-01-01 --until <today>   # window -> CSV in output/
python google_incremental.py   # watermark + restatement detection (no CSV)
python google_resources.py     # GAQL resource inventory (optional)
```

For the data, run `google_extract.py`. `google_incremental.py` is for tracking
restated days, and `google_resources.py` is an optional inventory.

With no credentials these refuse to run and say what is missing. `google_resources.py`
still prints its resource catalogue, since that involves no data.

## What is pulled

**Source:** Google Ads API v25, REST, via the Skypoint MCC (`775-317-9425`).
**Account:** Kyrene School District, customer `829-530-3333` (USD, America/Phoenix).

**Grain:** one row per campaign per day. Days with no activity have no row.

**Scope of the first full pull** (`--since 2025-01-01`, run 2026-10-20): 187 rows,
8 campaigns, 2026-03-13 to 2026-10-19. The account has no campaigns before
2026-03-13, so nothing earlier exists to pull. Spend totals $15,791.75: Performance
Max $12,935, Video $2,285, Display $289, Search $282. Totals matched the Google Ads
UI for Sep 12 – Oct 9, 2026 once the removed campaign is accounted for.

`google_extract.py` writes 48 columns to
`output/google_ads_campaign_daily_<customer>_<YYYY-MM-DD>_<HHMMSS>.csv`. The
date and time are in the name, so a second run never overwrites the first. Pick the
window with `--since YYYY-MM-DD --until YYYY-MM-DD` or `--days N`; the default is
the last 7 days. The columns are defined in `google_client.py` (`COLUMNS`,
`CAMPAIGN_DIMENSIONS`, `CAMPAIGN_KPIS`).

| Group | Columns |
|---|---|
| Keys | `date`, `campaign_id`, `campaign_name` |
| Campaign info | `campaign_status`, `channel_type`, `bidding_strategy`, `daily_budget` |
| Original metrics | `impressions`, `clicks`, `spend`, `ctr`, `cpc`, `cpm`, `reach`, `conversions` |
| Engagement | `interactions`, `interaction_rate`, `avg_cost_per_interaction`, `engagements`, `engagement_rate` |
| Conversions | `conversions_value`, `conversion_rate`, `cost_per_conversion`, `value_per_conversion`, `all_conversions`, `all_conversions_value`, `cost_per_all_conversions`, `view_through_conversions`, `cross_device_conversions` |
| Video | `video_views`, `video_view_rate`, `video_p25_rate`, `video_p50_rate`, `video_p75_rate`, `video_p100_rate` |
| Search position | `search_impression_share`, `search_lost_is_budget`, `search_lost_is_rank`, `search_top_impression_share`, `search_abs_top_impression_share`, `top_impression_pct`, `abs_top_impression_pct` |
| Click quality | `invalid_clicks`, `invalid_click_rate` |
| Account | `customer_id`, `customer_name`, `source`, `extracted_at` |

Notes on the values:

- Rates (`ctr`, `*_rate`, impression share) are percentages, e.g. `5.12`. Spend,
  cost and budget columns are in account currency, not micros.
- **Blank means "not returned", not zero.** `reach` is always blank (Google has
  no reach at this grain). `daily_budget` is blank where the API omits the
  budget amount (169 of 187 rows), and the impression-share columns are blank for
  campaign types where Google withholds them, such as Performance Max.
- `invalid_clicks` is counted separately from `clicks`, so it can exceed it.
- Average cost per view is not a column: Google v25 no longer accepts that
  field. Derive it as `spend / video_views`.
- Removed campaigns are included on purpose, since their spend really happened.
  Filter on `campaign_status` to exclude them. The Google Ads UI's default view
  hides them, which is why its top-card totals can be slightly lower than the CSV.
- The latest day is partial and will be restated. Load with an upsert on
  `(date, campaign_id)`, never an insert.

## Column definitions

Every column in the CSV, in file order. "Percent" means a 0-100 number (5.12 is
5.12%). "Currency" is the account currency (USD), in whole dollars, not micros.
Counts are for that campaign on that day. Google's own metric names are in the
last column, for looking things up in the API docs.

**Keys and campaign info**

| Column | What it is | Google field / unit |
|---|---|---|
| `date` | The day the activity happened, in the account's time zone (America/Phoenix). Conversions are credited back to the day of the click, so old days can change. | `segments.date`, `YYYY-MM-DD` |
| `campaign_id` | Google's unique number for the campaign. Together with `date` it identifies a row. | `campaign.id` |
| `campaign_name` | The campaign's name as set in Google Ads. | `campaign.name` |
| `campaign_status` | `ENABLED`, `PAUSED` or `REMOVED`. This is the status when the file was pulled, not on that `date`. | `campaign.status` |
| `channel_type` | Where the campaign runs: `SEARCH`, `DISPLAY`, `VIDEO`, `PERFORMANCE_MAX`, and so on. | `campaign.advertising_channel_type` |
| `bidding_strategy` | How the campaign bids, e.g. `MAXIMIZE_CONVERSIONS`, `TARGET_CPV`, `TARGET_SPEND` (maximize clicks). Current setting, not historical. | `campaign.bidding_strategy_type` |
| `daily_budget` | The campaign's daily budget, in currency. Current value, not historical. Blank when the API does not return it. | `campaign_budget.amount_micros` |

**Core performance**

| Column | What it is | Google field / unit |
|---|---|---|
| `impressions` | Times an ad was shown. | `metrics.impressions`, count |
| `clicks` | Valid clicks on the ads. Invalid clicks are not included (see `invalid_clicks`). | `metrics.clicks`, count |
| `spend` | Total cost for the day. | `metrics.cost_micros` / 1,000,000, currency |
| `ctr` | Click-through rate: clicks divided by impressions. | `metrics.ctr`, percent |
| `cpc` | Average cost per click. | `metrics.average_cpc`, currency |
| `cpm` | Average cost per 1,000 impressions. | `metrics.average_cpm`, currency |
| `reach` | Unique people reached. Always blank: Google does not report it at campaign-by-day level. | none |
| `conversions` | Conversions from the account's primary conversion actions. Can be fractional because data-driven attribution splits credit. | `metrics.conversions`, count |

**Engagement**

| Column | What it is | Google field / unit |
|---|---|---|
| `interactions` | The main action each ad type counts: clicks on search and display ads, views on video ads, and so on. | `metrics.interactions`, count |
| `interaction_rate` | Interactions divided by impressions. | `metrics.interaction_rate`, percent |
| `avg_cost_per_interaction` | Spend divided by interactions. | `metrics.average_cost`, currency |
| `engagements` | Times people engaged with the ad, e.g. a video view or expanding an ad. | `metrics.engagements`, count |
| `engagement_rate` | Engagements divided by impressions. | `metrics.engagement_rate`, percent |

**Conversions in more detail**

| Column | What it is | Google field / unit |
|---|---|---|
| `conversions_value` | Total value of the primary conversions. Equals `conversions` when no value is assigned to the conversion actions (it did in the Sep 12 - Oct 9 window). | `metrics.conversions_value`, currency |
| `conversion_rate` | Conversions divided by interactions. | `metrics.conversions_from_interactions_rate`, percent |
| `cost_per_conversion` | Spend divided by `conversions`. 0 when there were none. | `metrics.cost_per_conversion`, currency |
| `value_per_conversion` | `conversions_value` divided by `conversions`. | `metrics.value_per_conversion`, currency |
| `all_conversions` | Every conversion, including secondary conversion actions (calls, directions, YouTube follow-on views, and so on). Much larger than `conversions`. | `metrics.all_conversions`, count |
| `all_conversions_value` | Total value of all conversions. | `metrics.all_conversions_value`, currency |
| `cost_per_all_conversions` | Spend divided by `all_conversions`. | `metrics.cost_per_all_conversions`, currency |
| `view_through_conversions` | Conversions by people who saw a display or video ad, did not click it, and converted later. | `metrics.view_through_conversions`, count |
| `cross_device_conversions` | Conversions where the click was on one device and the conversion on another. Already counted in `all_conversions`. | `metrics.cross_device_conversions`, count |

**Video**

| Column | What it is | Google field / unit |
|---|---|---|
| `video_views` | TrueView views: a viewer watched at least 30 seconds (or the whole ad if shorter) or interacted with it. Zero for non-video campaigns. | `metrics.video_trueview_views`, count |
| `video_view_rate` | Video views divided by impressions. | `metrics.video_trueview_view_rate`, percent |
| `video_p25_rate` | Share of video plays that reached 25% of the video. | `metrics.video_quartile_p25_rate`, percent |
| `video_p50_rate` | Share that reached 50%. | `metrics.video_quartile_p50_rate`, percent |
| `video_p75_rate` | Share that reached 75%. | `metrics.video_quartile_p75_rate`, percent |
| `video_p100_rate` | Share that watched to the end. | `metrics.video_quartile_p100_rate`, percent |

**Search position (impression share)**

Blank when Google does not report them, which is common outside Search
campaigns. Blank is not zero.

| Column | What it is | Google field / unit |
|---|---|---|
| `search_impression_share` | Impressions received divided by the impressions the ads were eligible to get on the Search network. | `metrics.search_impression_share`, percent |
| `search_lost_is_budget` | Share of eligible impressions lost because the budget was too low. | `metrics.search_budget_lost_impression_share`, percent |
| `search_lost_is_rank` | Share of eligible impressions lost because of low ad rank (bid and quality). | `metrics.search_rank_lost_impression_share`, percent |
| `search_top_impression_share` | Impressions shown above the organic results, divided by the eligible ones. | `metrics.search_top_impression_share`, percent |
| `search_abs_top_impression_share` | Impressions shown in the very first position, divided by the eligible ones. | `metrics.search_absolute_top_impression_share`, percent |
| `top_impression_pct` | Share of the ads' own impressions shown above the organic results. | `metrics.top_impression_percentage`, percent |
| `abs_top_impression_pct` | Share of the ads' own impressions shown in the very first position. | `metrics.absolute_top_impression_percentage`, percent |

**Click quality**

| Column | What it is | Google field / unit |
|---|---|---|
| `invalid_clicks` | Clicks Google judged invalid (accidental or fraudulent) and did not charge for. Counted separately from `clicks`, so it can be larger. | `metrics.invalid_clicks`, count |
| `invalid_click_rate` | Invalid clicks as a share of all clicks, as Google calculates it. | `metrics.invalid_click_rate`, percent |

**Account and load metadata**

| Column | What it is | Google field / unit |
|---|---|---|
| `source` | Always `google_ads`. Lets rows from different sources share one table. | set by the extractor |
| `customer_id` | The Google Ads account the row came from, digits only (no hyphens). | from `GOOGLE_ADS_CUSTOMER_ID` |
| `customer_name` | The account's name, e.g. Kyrene School District. | `customer.descriptive_name` |
| `extracted_at` | When the file was pulled, in UTC. The same for every row in one run. | set by the extractor |

## What is NOT pulled

- **Conversions by conversion action.** The account has 16 conversion actions
  (Form, Lead form - Submit, Calls from ads, the GA4 web events, and others), but
  the CSV carries only the totals, so it cannot say which action produced a
  conversion. This is the biggest gap.
- **Anything below campaign level:** ad group, ad and keyword data, search terms,
  device, location, demographics (age, gender) and hour of day.
- **Who clicked.** Google Ads never returns individual users. `click_view` gives
  anonymous per-click rows (gclid, device, location), one day at a time for the
  last 90 days, and is not pulled. Tying a click to a person needs Kyrene's own
  site or CRM data.
- Account currency and time zone are not columns.

## The finding

Google Ads data is **not append-only**. A conversion is credited back to the
date of the *click* that caused it, not the date it happened — so a day's row
keeps changing after the fact. `google_incremental.py` is built to detect this: it
fingerprints every row, re-pulls, and reports which `(date, campaign_id)` pairs
came back with different numbers. It has run live twice, minutes apart, and
reported 0 restated rows. Two full pulls on 2026-10-19 and 2026-10-20 did differ
slightly (e.g. Display 7.00 vs 8.00 conversions), which looks like restatement but
was not compared row by row. Re-run it a day or more later for a proper
measurement.

The resulting load rule:

> Watermark = last complete date. Pull `(watermark − conversion window) …
> yesterday`. **UPSERT on `(date, campaign_id)`** — never INSERT.

**Where this differs from Meta**, and why the two connectors are not copies:
Meta's window is a fixed 28 days, the same for every account. Google's is a
**per-account setting** — 30 days by default, configurable up to 90, and it can
differ between conversion actions in the same account. So the lookback is a
value to *read*, not assume. Read the real windows from `conversion_action`
(`CONVERSION_WINDOW_QUERY` in `google_client.py`) and keep the configured
lookback at or above the widest one. For Kyrene the widest is 90 days, which is
what `GOOGLE_ADS_RESTATEMENT_DAYS` is set to.

## Credentials

Three secrets, not one — Google Ads is an OAuth flow rather than Meta's static
token. The developer token below is legacy and not one of the three:

| Value | Secret? | Notes |
|---|---|---|
| `GOOGLE_ADS_CLIENT_ID` / `_CLIENT_SECRET` | **Yes** | Skypoint app-level constants |
| `GOOGLE_ADS_REFRESH_TOKEN` | **Yes** | The real credential. One for the whole platform |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | If set | Legacy — optional and ignored by the API since the September 9, 2026 sunset; leave blank |
| `GOOGLE_ADS_CUSTOMER_ID` | No | Per-client scoping input; `isCredential: false` in Studio |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | No | The MCC. Sent as the `login-customer-id` header |

Nothing is hardcoded. `.env` is gitignored; only `.env.example` ships.

## Two gotchas the code handles so you don't have to

**Hyphens.** The UI shows customer ids as `775-317-9425`; the API rejects that
form with an error that never mentions hyphens. Stripped automatically.

**The `login-customer-id` header.** Querying a client account under an MCC
without it returns `USER_PERMISSION_DENIED` — which reads like bad credentials
but is really a missing header. Set automatically from the MCC, and named
explicitly in the error message when a permission error does occur.

See `Google_Ads_Integration.md` for the full picture and the production design.
