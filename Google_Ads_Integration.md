# Google Ads Integration — Skypoint

**Status:** PoC complete and runnable end to end. MCC created; OAuth client,
refresh token done, Explorer access applied for. Kyrene School District linked.
**Written:** 2026-10-05
**Owner:** Rishin Aiyappa (Skypoint)
**Access level:** read-only reporting via a shared OAuth credential at the MCC

Google Ads only. Meta Ads is a separate package and ships first.

---

## 1. Overview

This integration connects Skypoint to clients' Google Ads accounts through a
single Manager (MCC) account that Skypoint owns. Google Ads exposes a
JSON/REST API queried with **GAQL** — a SQL-like language over a fixed set of
reportable *resources* (campaigns, ad groups, ads, keywords, search terms,
conversion actions, and the metrics segmented by each).

```
Skypoint                                  Google
┌──────────────────────┐                 ┌────────────────────────────┐
│ ONE OAuth credential │  refresh_token  │ oauth2.googleapis.com      │
│ (client_id, secret,  ├────────────────►│   -> access_token (~1h)    │
│  refresh_token)      │                 └────────────────────────────┘
│ + developer_token    │                 ┌────────────────────────────┐
└──────────┬───────────┘  GAQL over REST │ googleads.googleapis.com   │
           │             ───────────────►│ MCC 775-317-9425           │
           │   login-customer-id: MCC    │  └─ client accounts        │
           │   customer_id: the client   │     (none linked yet)      │
           ▼
Raw batches → the client's own ADLS Gen2 (production phase)
```

**One credential, many clients.** The MCC is Google's own multi-client
mechanism, not a workaround. Every call carries a `login-customer-id` (the MCC
doing the authenticating) and a `customer_id` (the account being read); one
OAuth credential at manager level reaches any account linked beneath it simply
by changing `customer_id`. Onboarding a client is a configuration entry, not
new credential work.

No SDK is required — plain HTTPS POSTs with a JSON body work (`requests`).

---

## 2. Account setup

| Item | Value | State |
|------|-------|-------|
| Manager account (MCC) | Skypoint Cloud Inc — `775-317-9425` | **Done** |
| OAuth client (Cloud Console, Desktop app) | `client_id` / `client_secret` | **Done** |
| Refresh token | minted once against the MCC | **Done** |
| API access level | Cloud Console → Google Ads API → Access levels (Explorer) | Applied 2026-10-19 |
| Developer token | sunset 2026-09-09; blank and ignored | **Not needed** |
| Client account link | client accepts the manager invite | **Done** (Kyrene) |

The MCC id is a non-secret identifier and is pre-filled in `.env.example`. The
other three are secrets and appear in no file in this package.

### Authentication flow

1. **One-time, platform-level:** create the OAuth client → run
   `get_refresh_token.py` → obtain a developer token. These are the same for
   every client.
2. **One-time, per client:** the client accepts the manager link request. No
   credential is exchanged; access is granted account-side. **← the remaining
   step after (1).**
3. **Every run:** exchange `refresh_token` for a short-lived `access_token`,
   then make GAQL calls carrying `developer-token` and `login-customer-id`.

### The auth model, and why it is not Meta's

| | Meta Ads | Google Ads |
|---|---|---|
| Credential | Static System User token | OAuth refresh token |
| Expiry | Never | Access token ~1h; refresh token until revoked |
| Round trips per run | 0 extra | 1 token exchange, plus refreshes |
| Kinds of 401 | One (fatal) | **Two, handled oppositely** |
| Secrets | 1 | 3 |

**The two kinds of 401** are the core of `google_client.py`:

| Where | Meaning | Policy |
|---|---|---|
| The refresh-token exchange fails | The shared credential is dead or revoked | **Fatal. Never retried.** Affects every client at once; needs a human to re-mint against the MCC |
| A data call 401s after a valid access token existed | The short-lived token expired mid-sync | **Normal.** Mint a new one, retry the call once, silently |

Collapsing these into one "401 → retry" rule would hammer Google with a dead
credential. Collapsing them into one "401 → fail" rule would break every sync
that runs longer than an access token lives.

Tokens are refreshed **proactively**, five minutes before expiry, rather than
waiting for a 401 — the same discipline `source-paychex` uses for its
30-minute session.

> **Do not port `source-ga4`'s `auth.py`.** GA4 uses Workload Identity
> Federation (Azure managed identity → Google STS → service-account
> impersonation) because the Data API supports federated service accounts. The
> Google Ads API does not support that for this scope. Copying the WIF pattern
> is a dead end.

---

## 3. PoC validation status

| Check | Result |
|-------|--------|
| No-credentials behaviour | **Pass** — every data path refuses and names what is missing; nothing is fabricated or written |
| Schema normalization | **Pass** — 14 columns, matching the Meta package |
| Restatement demonstration | **Pending** — needs two real pulls on a live account; there is no synthetic data to fake it with |
| Hyphen / `login-customer-id` handling | **Pass** — verified without network calls |
| OAuth exchange | **Pending** — no OAuth client yet |
| Live resource probe | **Pending** — needs developer token + a client account |

Everything marked *Pending* becomes a single command once the credentials
exist: `python google_extract.py`, then `python google_resources.py`.

---

## 4. Available data — GAQL resources

Google Ads has no table catalogue in the Geotab sense. It exposes reportable
*resources* queried with GAQL, and the same metrics can be segmented many
ways. `google_resources.py` catalogues **46** worth knowing about for a
reporting pipeline, grouped by role:

| Group | Count | Examples |
|---|---:|---|
| Account structure (reference data) | 12 | `customer`, `customer_client`, `campaign`, `ad_group`, `ad_group_ad`, `campaign_budget`, `conversion_action` |
| Performance (the metrics that land) | 17 | `campaign`+`segments.date`, `keyword_view`, `search_term_view`, `geographic_view`, `age_range_view`, `landing_page_view`, `click_view` |
| Shopping / Performance Max | 7 | `shopping_performance_view`, `asset_group`, `asset_group_asset` |
| Audiences, labels, admin | 10 | `audience`, `user_list`, `change_event`, `recommendation`, `billing_setup` |

Which of these have data depends on what the account actually runs — a
search-only advertiser has no shopping or PMax rows. "Empty" is information,
not a failure.

Two resources carry their own constraints: `click_view` has 90-day retention
and accepts **one day per query**; `paid_organic_search_term_view` needs a
Search Console link.

### The output schema

One row per campaign per day. Deliberately identical to the Meta package's
schema except for the account identifier, so the two sources union in Silver
without reshaping.

```
date, campaign_id, campaign_name, impressions, clicks, spend,
ctr, cpc, cpm, reach, conversions, source, customer_id, extracted_at
```

Four mappings are not straight passthroughs:

- **`spend` and `cpc` are micros.** `cost_micros` and `average_cpc` come back
  multiplied by 1,000,000. Forgetting to divide produces numbers that look
  plausible, which is what makes it dangerous.
- **`ctr` units differ between vendors.** Google reports a fraction (`0.0512`);
  Meta reports a percent (`5.12`). Normalized to percent here so the two agree.
- **`reach` does not exist.** Google has no reach equivalent at this grain.
  Explicitly `None`, never `0` — "not measured" and "measured as zero" are
  different facts.
- **`conversions` is fractional.** Data-driven attribution splits credit across
  touchpoints, so `3.67` is a normal value. The column is a DOUBLE, not an INT.

Two response-shape traps the client handles: the REST interface returns int64
fields as JSON **strings** while doubles come back as numbers; and GAQL is
written in `snake_case` while the response comes back in `camelCase`
(`cost_micros` → `costMicros`). Querying one and reading the other silently
yields zeros.

Days with no activity are **omitted entirely** rather than returned as zeros,
so row counts are not days × campaigns.

---

## 5. Incremental extraction — the finding

Google Ads provides no CDC cursor. There is no `GetFeed` equivalent, no
version token. GAQL is a date-windowed query, and the rows it returns for a
given date **keep changing afterwards**: a conversion is credited back to the
date of the click that caused it, not the date it happened. Someone who clicks
on the 3rd and buys on the 20th adds a conversion to the 3rd, seventeen days
later.

Spend and impressions stabilise within a day or two. **Conversions do not** —
and neither does anything derived from them: CPA, ROAS, conversion rate.

```
watermark  = last complete date successfully loaded
pull       = (watermark - conversion window) .. yesterday
load       = UPSERT on (date, campaign_id), never INSERT
```

`google_incremental.py` is built to detect this: it fingerprints every row it
pulls, stores the watermark, and on the next run reports which
`(date, campaign_id)` pairs came back with different numbers. It has **not yet
been run against live data**, so this is documented Google behaviour rather than
something the PoC has observed; the first two live runs a day apart will measure it.

### The lookback is a value to read, not assume

This is the substantive difference from Meta and the reason the two connectors
are not copies of each other:

| | Meta Ads | Google Ads |
|---|---|---|
| Window | Fixed **28 days** | **Per-account setting** |
| Default | — | 30 days |
| Range | Same for every account | Up to 90, and can differ per conversion action |

Hardcoding a number would silently lose conversions on any account configured
wider. The real `click_through_lookback_window_days` on each action comes from
`conversion_action` (`CONVERSION_WINDOW_QUERY` in `google_client.py`); keep the
configured lookback at or above the widest one so no days are missed.

The overlap is not waste. It corrects days whose numbers moved, and it is also
why a missed run heals itself: the window already reaches back past the gap, so
there is no catch-up branch to write, schedule, or test. Today is deliberately
excluded — a partial day is guaranteed to be restated and adds only noise.

### Extraction strategy

| Data class | Resources | Method | Cadence |
|---|---|---|---|
| Reference (small, bounded) | `campaign`, `ad_group`, `ad_group_ad`, `campaign_budget`, `conversion_action`, `customer_client` | Full refresh via GAQL | Daily |
| Performance (restating) | `campaign`/`ad_group`/`ad_group_ad` + `segments.date`, `keyword_view`, `search_term_view` | Rolling window + UPSERT | Daily |
| Append-only | `change_event` | Date-windowed, 30-day retention | Daily |

---

## 6. Reporting suitability

**Strong fits**
1. **Spend and delivery** — campaign/ad-group/ad daily performance, stable
   within a day. Safe for finance reporting.
2. **Search intent** — `search_term_view` is the query-level data that has no
   Meta equivalent, and is usually the highest-value Google-specific asset.
3. **Cross-platform roll-up** — the shared 14-column schema unions directly
   with the Meta extract in Silver.
4. **Conversion analysis** — provided the restatement rule above is honored.

**Limitations / notes**
- Conversions are fractional and restate for up to 90 days; any dashboard
  comparing "yesterday" to "last month" is comparing a half-formed number to a
  settled one unless the grain states its age.
- `reach` is unavailable, so a Meta/Google union cannot report combined reach.
- Developer-token tier caps daily operations; Test tier cannot reach real
  client accounts at all.
- Ad-set/ad-level detail and audience breakdowns are the same mechanism with
  more rows, not a different one.

---

## 7. Production path (proposed, not yet built)

The same three repos as every other Skypoint V2 connector:

| Step | Repo | What gets built for `google-ads` |
|---|---|---|
| 1 | `skypoint-studio` | Seed `connector-v2-google-ads.json`. Field definition carries **only** `customerId`, `isCredential: false` — none of the four secrets appear in a form field |
| 2 | `skypoint-connector-hub` | `validator.ts`: exchange the refresh token, then one GAQL read — `SELECT customer.descriptive_name FROM customer LIMIT 1`, scoped under `login_customer_id`. **Two round trips, one attempt each, no retry loop** |
| 3 | `skypoint-skyconnect` | `source-google-ads/` — the `sync.py` / `run.py` two-process split, writing to the client's own ADLS Gen2 |

### What each PoC file becomes

| PoC file | Becomes |
|---|---|
| `google_client.py` — `TokenManager`, error classification, rate budget | `source_google_ads/auth.py` plus the CDK stream base. The two-kinds-of-401 policy ports over unchanged; it is the part that matters |
| `normalize_row` / `COLUMNS` | The `insights` stream's record shape |
| `gaql_search` / `campaign_daily_query` | The stream's `read_records` |
| `google_incremental.py` watermark logic | The stream's incremental cursor and the upsert contract |
| `google_extract.py` CSV writer | **Deleted.** Replaced by `shared_utils.streaming_upload` |
| `google_resources.py` | Not ported — a discovery tool; its job ends at the inventory |
| `get_refresh_token.py` | Not ported — run once by a human, never by the pipeline |

Streams: `campaigns`, `ad_groups`, `ads`, and a metrics/insights stream pulled
via GAQL, plus a `V2_ENTITY_TO_STREAM` dict translating Studio's entity names.
An unmapped entity produces a warning and is skipped — never a silent fallback
to the raw name.

Only the credential is shared across clients. **The data never is** — synced
output lands solely in that client's own ADLS Gen2.

---

## 8. Rules enforced in this code

- **Auth failures are never retried** at the credential level. One shared
  credential means one retry loop takes down every client at once.
- **Access-token expiry is not an auth failure** and is handled silently —
  the distinction is implemented, not just documented.
- **Classification on the GAQL error family**, not HTTP status. The Test-tier
  developer-token rejection gets its own error class because it arrives *as*
  an authorization error but needs a completely different fix (apply for
  Basic, not re-mint anything).
- **ZZ-invalid negative test** corrupts `customer_id` only. The refresh token
  and developer token are never touched, and the test says so out loud.
- **One shared rate budget** at 5 req/s, independent of concurrency.
- **Field names in logs, never values.** Secrets masked even where a human
  needs to confirm which one loaded.
- **No credentials in source.** `.env` gitignored; only `.env.example` ships.
- **No writes to Skypoint-owned storage.** The CSV writer is PoC scaffolding
  and is deleted at Phase 3, not migrated.

---

## 9. Open items

- [x] ~~Create the Manager (MCC) account~~ — **done**, Skypoint Cloud Inc
      `775-317-9425`.
- [x] **Create the OAuth client** — Cloud Console → Credentials → OAuth 2.0
      Client ID, type Desktop app.
- [x] **Mint the refresh token** — `python get_refresh_token.py`.
- [x] **API access** — developer tokens are gone; Explorer access is applied
      for in Cloud Console (Basic later if needed).
- [x] **Confirm the API version** — `v25` (v21 was sunset); re-check
      https://developers.google.com/google-ads/api/docs/sunset-dates yearly.
- [x] **A client accepts the manager link request** — done (Kyrene).
- [ ] **Settle the Key Vault decision** — shared credential across tenants;
      Option A (copy into each client's tenant vault) recommended. Applies to
      Meta and Google Ads identically and should be settled once.
- [ ] **Confirm the conversion-window lookback** per client once an account is
      linked — query `conversion_action` (`CONVERSION_WINDOW_QUERY`); it just needs an account to read.
- [ ] **Decide grain and backfill horizon** — ad-group/ad level and search
      terms multiply row counts substantially.
