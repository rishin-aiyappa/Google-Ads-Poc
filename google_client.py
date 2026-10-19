"""Shared Google Ads API client for every script in this package.

Why one shared module instead of self-contained scripts: the auth rules below
have shared blast radius. Skypoint holds ONE OAuth credential (one MCC, one
refresh token) covering every client, so a retry loop on a bad credential does
not lock out one client -- it locks out all of them at once. Those rules live
in exactly one place here rather than being copy-pasted into five scripts
where they can quietly drift apart.

THE DIFFERENCE FROM META ADS, which is the whole reason this is a separate
build rather than a copy: Meta's credential is a static System User token --
it either works or it doesn't, and there is nothing to refresh. Google Ads is
an OAuth refresh-token flow. Every data call needs a short-lived access token
minted from the long-lived refresh token, which means:

  * there is a token-exchange round trip Meta does not have;
  * there are TWO kinds of 401 and they are handled oppositely (see
    GoogleAdsAuthError vs the refresh-and-retry path in request_with_retry);
  * the access token can expire mid-sync, which for Meta is impossible.

Do NOT port source-ga4's auth.py here. GA4 uses Workload Identity Federation
(Azure managed identity -> Google STS -> service-account impersonation)
because the Data API supports federated service accounts. The Google Ads API
does not support that for this scope. Copying the WIF pattern is a dead end.

Credentials are read from the environment (or a local .env) and never embedded
in source -- see .env.example.

Docs:
  GAQL / REST:   https://developers.google.com/google-ads/api/rest/overview
  OAuth:         https://developers.google.com/google-ads/api/docs/oauth/overview
  Account mgmt:  https://developers.google.com/google-ads/api/docs/account-management/get-account-hierarchy
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

import requests

# .env support is a convenience, not a requirement -- the scripts run fine on
# plain environment variables if python-dotenv isn't installed.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional dependency
    pass


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# v21 was already sunset (404 on every call). Supported versions rotate, so if
# 404s ever reappear check https://developers.google.com/google-ads/api/docs/sunset-dates
# and bump this. Google ships roughly three Google Ads API
# versions a year and sunsets each one about a year after release -- a stale
# version here fails every call with a clear "version not supported" error, so
# it is a cheap thing to get wrong and a cheap thing to check. Overridable with
# GOOGLE_ADS_API_VERSION.
DEFAULT_API_VERSION = "v25"

# Hard ceiling shared by every call in the process, matching the platform's
# existing connectors (source-paychex uses the same shape). More concurrency
# means more threads competing for this ceiling, never a way to exceed it.
DEFAULT_RPS = 5.0

ADS_HOST = "https://googleads.googleapis.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Google Ads conversions are credited back to the date of the CLICK, not the
# date of the conversion, and keep landing for the length of the account's
# conversion window. 30 days is Google's default; accounts can configure up to
# 90. The default is 90 (the widest allowed) so no window is cut short; it is
# still worth reading the real value from
# the account -- query conversion_action (CONVERSION_WINDOW_QUERY below) for the
# real per-account windows. Overridable with GOOGLE_ADS_RESTATEMENT_DAYS.
DEFAULT_RESTATEMENT_DAYS = 90

# Google Ads access tokens live ~3600s. Refresh with a margin rather than
# waiting for a 401, so a long sync is never caught mid-stream -- the same
# proactive-refresh discipline source-paychex uses for its 30-minute session.
TOKEN_REFRESH_MARGIN_SECONDS = 300


@dataclass(frozen=True)
class GoogleAdsConfig:
    """Everything needed to talk to the Google Ads API for one customer."""

    # --- the OAuth credential: shared across every client ------------------
    client_id: str
    client_secret: str
    refresh_token: str
    # LEGACY / OPTIONAL. Google sunset developer tokens on 2026-09-09 and the
    # API now ignores the header; access is gated by the Cloud project behind
    # the OAuth credential. Still read, and still sent if set, so an existing
    # .env keeps working -- but nothing requires it.
    developer_token: str = ""
    # --- per-client scoping inputs (NOT credentials) -----------------------
    customer_id: str = ""
    login_customer_id: str = ""
    # --- tuning ------------------------------------------------------------
    api_version: str = DEFAULT_API_VERSION
    requests_per_second: float = DEFAULT_RPS
    restatement_days: int = DEFAULT_RESTATEMENT_DAYS

    @property
    def has_credential(self) -> bool:
        """Enough to mint an access token and ask what this credential can
        reach -- which is the whole question during onboarding, before any
        client has linked their account to the MCC.

        """
        return bool(self.client_id and self.client_secret and self.refresh_token)

    @property
    def is_configured(self) -> bool:
        """Enough to pull data: the OAuth credential AND a customer to point
        it at. The developer token is not part of this -- it is optional and
        ignored by the API."""
        return bool(self.has_credential and self.customer_id)

    @property
    def customer(self) -> str:
        """Google Ads wants the customer id as bare digits. The UI shows it
        hyphenated (123-456-7890) and that form is rejected by the API with an
        error that does not mention hyphens -- so normalize rather than making
        a human debug it. Unset renders as a placeholder so
        narrated output reads as the call that would be made."""
        return _digits(self.customer_id) or "<CUSTOMER_ID>"

    @property
    def manager(self) -> str:
        """The MCC doing the authenticating. Sent as the login-customer-id
        header; see login_header()."""
        return _digits(self.login_customer_id)


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def load_config() -> GoogleAdsConfig:
    """Reads config from the environment. Missing credentials are not an
    error here -- load_config always succeeds, and the data paths refuse to
    run via require_live(). Nothing is ever fabricated to fill a gap."""
    return GoogleAdsConfig(
        client_id=os.getenv("GOOGLE_ADS_CLIENT_ID", "").strip(),
        client_secret=os.getenv("GOOGLE_ADS_CLIENT_SECRET", "").strip(),
        refresh_token=os.getenv("GOOGLE_ADS_REFRESH_TOKEN", "").strip(),
        developer_token=os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip(),
        customer_id=os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").strip(),
        login_customer_id=os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip(),
        api_version=os.getenv("GOOGLE_ADS_API_VERSION", DEFAULT_API_VERSION).strip(),
        requests_per_second=float(os.getenv("GOOGLE_ADS_RPS", DEFAULT_RPS)),
        restatement_days=int(os.getenv("GOOGLE_ADS_RESTATEMENT_DAYS", DEFAULT_RESTATEMENT_DAYS)),
    )


def mask(secret: str) -> str:
    """Secrets never reach a log in full. The platform's logging standard is
    field names only, never values -- this exists for the few places where a
    human genuinely needs to confirm *which* credential is loaded."""
    if not secret:
        return "<unset>"
    if len(secret) <= 12:
        return f"<set, len={len(secret)}>"
    return f"{secret[:4]}...{secret[-4:]} (len={len(secret)})"


# --------------------------------------------------------------------------
# Errors -- the distinction between these drives the retry policy
# --------------------------------------------------------------------------


class GoogleAdsError(RuntimeError):
    """Base for every error this client raises."""


class GoogleAdsNotConfiguredError(GoogleAdsError):
    """Credentials are missing, so there is nothing to pull.

    This package deliberately has NO mock/synthetic mode. An earlier revision
    did, and it was removed: synthetic rows are indistinguishable from real
    ones once they are in a CSV or a warehouse table, and a run started by
    mistake would quietly contaminate a client's data with invented numbers.

    Refusing to run is the safe failure. Every output this package produces is
    real vendor data or it is nothing.
    """


class GoogleAdsAuthError(GoogleAdsError):
    """The shared OAuth credential itself is bad, revoked, or unauthorized.

    NEVER retried. This covers the refresh-token exchange failing, and any
    authentication/authorization failure that a fresh access token cannot fix.
    The credential is shared across every client -- one retry loop takes down
    every Google Ads instance on the platform. Fail immediately, surface it,
    let a human re-mint the refresh token against the MCC.

    Note this is NOT the same as an access token expiring mid-sync. That case
    is normal, expected, and handled silently by minting a new one -- see
    request_with_retry.
    """


class GoogleAdsDeveloperTokenError(GoogleAdsAuthError):
    """The developer token is not approved for this account.

    Called out separately from the general auth case because it is the single
    most common Google Ads onboarding failure and it looks like a credentials
    problem when it isn't: a developer token at the Test tier can ONLY reach
    test accounts. Pointing it at a real client account fails here, with
    working credentials. The fix is applying for Basic access, not re-minting
    anything.
    """


class GoogleAdsPermissionError(GoogleAdsError):
    """Credential is valid but not permitted on this customer.

    Not retried -- the answer will not change on a second attempt. The usual
    cause is a missing or wrong login-customer-id header rather than an actual
    permission problem; see login_header().
    """


class GoogleAdsRateLimitError(GoogleAdsError):
    """Google is throttling, or the account hit a daily operation quota.
    Retried with backoff."""


class GoogleAdsAPIError(GoogleAdsError):
    """Anything else the API rejected."""


# The OAuth token endpoint signals a dead credential with these, in the
# top-level "error" string. All of them are fatal -- none is retryable.
OAUTH_FATAL = {
    "invalid_grant",        # refresh token expired, revoked, or wrong
    "invalid_client",       # client_id / client_secret wrong
    "unauthorized_client",
    "invalid_scope",
}

# Google Ads failures arrive as a GoogleAdsFailure in error.details[], keyed by
# the error-code family. These are the families that matter for retry policy.
AUTH_FAMILIES = {"authenticationError"}
PERMISSION_FAMILIES = {"authorizationError"}
QUOTA_FAMILIES = {"quotaError"}
INTERNAL_FAMILIES = {"internalError"}


def _google_ads_failure(body: dict) -> dict:
    """Digs the first GoogleAdsFailure error out of the REST error envelope.

    Shape: {"error": {"code", "message", "status",
                      "details": [{"@type": "...GoogleAdsFailure",
                                   "errors": [{"errorCode": {...},
                                               "message": "..."}]}]}}
    """
    for detail in ((body or {}).get("error") or {}).get("details") or []:
        errors = detail.get("errors") or []
        if errors:
            return errors[0]
    return {}


def classify_error(status: int, body: dict) -> tuple[str, str]:
    """Maps a Google Ads API error onto (kind, human message).

    kind is one of: auth, developer_token, permission, rate_limit, transient,
    not_available, bad_request, http.
    """
    envelope = (body or {}).get("error") or {}
    failure = _google_ads_failure(body)
    code_obj = failure.get("errorCode") or {}
    family, value = next(iter(code_obj.items()), ("", ""))

    msg = (failure.get("message") or envelope.get("message") or "").strip() or f"HTTP {status}"
    detail = f"{family}.{value}: {msg}" if family else msg

    # The Test-tier trap, checked before the general authorization case
    # because it arrives *as* an authorization error but needs a different fix.
    if isinstance(value, str) and (
        "DEVELOPER_TOKEN" in value or "CLOUD_PROJECT_NOT_APPROVED" in value
    ):
        return "developer_token", detail

    if family in AUTH_FAMILIES or envelope.get("status") == "UNAUTHENTICATED" or status == 401:
        return "auth", detail
    if family in PERMISSION_FAMILIES or status == 403:
        return "permission", detail
    if family in QUOTA_FAMILIES or envelope.get("status") == "RESOURCE_EXHAUSTED" or status == 429:
        return "rate_limit", detail
    if family in INTERNAL_FAMILIES or status >= 500:
        return "transient", detail
    if status == 404:
        return "not_available", detail
    if status == 400:
        return "bad_request", detail
    return "http", detail


# --------------------------------------------------------------------------
# Rate limiting -- one shared budget, separate from concurrency
# --------------------------------------------------------------------------


class RateLimitBudget:
    """A hard ceiling on requests per second, shared by every caller.

    Deliberately a ceiling and not a per-thread allowance: the platform's
    existing connectors run up to 10 workers against one budget, and the
    budget is what keeps the vendor happy regardless of worker count.
    """

    def __init__(self, requests_per_second: float = DEFAULT_RPS) -> None:
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
                now = self._next_allowed
            self._next_allowed = now + self._interval


# --------------------------------------------------------------------------
# Logging -- structured, field names only
# --------------------------------------------------------------------------


def log(event: str, **fields: Any) -> None:
    """Deliberately tiny. The real connector uses the platform's logging
    standard; this exists so the POC's output is greppable and so that no
    script is tempted to print a secret."""
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    parts = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    print(f"  [{stamp}] {event} {parts}".rstrip())


# --------------------------------------------------------------------------
# OAuth -- the part Meta Ads does not have
# --------------------------------------------------------------------------


class TokenManager:
    """Mints and caches the short-lived access token.

    Thread-safe, because the rate budget above is shared across workers and
    they would otherwise all try to refresh at once.

    The policy in one line: refresh BEFORE expiry, not after a 401. Waiting for
    a 401 means every long sync eventually takes an avoidable failed call, and
    on a shared credential that failed call is indistinguishable in the logs
    from a genuinely dead credential.
    """

    def __init__(self, cfg: GoogleAdsConfig, budget: RateLimitBudget) -> None:
        self._cfg = cfg
        self._budget = budget
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0
        self.mint_count = 0

    @property
    def minted(self) -> bool:
        return self._access_token is not None

    def token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            fresh_enough = (
                self._access_token
                and time.monotonic() < self._expires_at - TOKEN_REFRESH_MARGIN_SECONDS
            )
            if fresh_enough and not force_refresh:
                return self._access_token  # type: ignore[return-value]
            return self._mint()

    def _mint(self) -> str:
        """Exchange the refresh token for an access token.

        A failure here is ALWAYS fatal. There is no scenario where retrying a
        rejected refresh token succeeds, and Google rate-limits repeated bad
        token exchanges. This is the Google Ads equivalent of Meta's "401 on
        the first call" -- the credential itself is dead and a human has to
        re-mint it against the MCC.
        """
        self._budget.acquire()
        try:
            resp = requests.post(
                TOKEN_URL,
                data={
                    "client_id": self._cfg.client_id,
                    "client_secret": self._cfg.client_secret,
                    "refresh_token": self._cfg.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            # A network failure reaching Google's token endpoint is NOT a bad
            # credential -- it is the one retryable case in this method, and
            # the caller decides.
            raise GoogleAdsAPIError(f"network error during token exchange: {exc}") from exc

        if not resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            err = body.get("error", "")
            desc = body.get("error_description", resp.text[:160])
            hint = ""
            if err == "invalid_grant":
                hint = (
                    " The refresh token has been revoked or expired. Re-mint it "
                    "against the MCC with the one-time consent flow."
                )
            elif err == "invalid_client":
                hint = " client_id / client_secret do not match the OAuth client."
            raise GoogleAdsAuthError(
                f"Refresh-token exchange rejected ({err or resp.status_code}): {desc}.{hint} "
                "NOT retrying: this credential is shared across every client and "
                "Google throttles repeated bad token exchanges."
            )

        payload = resp.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 3600))
        self.mint_count += 1
        log("access_token_minted", expires_in_s=int(payload.get("expires_in", 3600)),
            mint_count=self.mint_count)
        return self._access_token


def login_header(cfg: GoogleAdsConfig) -> dict:
    """The login-customer-id header, when the customer sits under an MCC.

    Worth its own function because omitting it is the classic Google Ads
    failure: the call comes back USER_PERMISSION_DENIED, which reads like the
    credential is wrong, when in fact the credential is fine and the request
    simply never said which manager account was doing the authenticating.
    """
    if cfg.manager and cfg.manager != _digits(cfg.customer_id):
        return {"login-customer-id": cfg.manager}
    return {}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

MAX_RETRY_ATTEMPTS = 4


def request_with_retry(
    method: str,
    url: str,
    *,
    tokens: TokenManager,
    cfg: GoogleAdsConfig,
    json_body: dict | None = None,
    extra_headers: dict | None = None,
    timeout: int = 60,
    verbose: bool = False,
) -> requests.Response:
    """One Google Ads API call, with the retry policy the platform requires.

    THE TWO KINDS OF 401, handled oppositely -- this is the core of the module:

      1. The refresh-token exchange fails (inside TokenManager._mint).
         The shared credential is dead. FATAL. Never retried. Affects every
         client at once and needs a human.

      2. A data call returns 401 after we already held a valid access token.
         That is just the short-lived token expiring mid-sync. Completely
         normal. Mint a new one and retry the call ONCE, silently.

    Collapsing those two into one "401 -> retry" rule would hammer Google with
    a dead credential; collapsing them into one "401 -> fail" rule would break
    every sync that runs longer than an access token lives.

    Quota errors and genuine 5xx blips are the only other retryable cases.
    """
    attempt = 0
    refreshed_once = False

    while True:
        attempt += 1
        headers = {
            "Authorization": f"Bearer {tokens.token()}",
            "Content-Type": "application/json",
        }
        if cfg.developer_token:  # legacy; the API ignores it, so only send if set
            headers["developer-token"] = cfg.developer_token
        headers.update(login_header(cfg))
        headers.update(extra_headers or {})

        budget_wait = time.monotonic()
        try:
            resp = requests.request(
                method, url, headers=headers, json=json_body, timeout=timeout
            )
        except requests.RequestException as exc:
            if attempt >= MAX_RETRY_ATTEMPTS:
                raise GoogleAdsAPIError(f"network error after {attempt} attempts: {exc}") from exc
            delay = _backoff_delay(attempt)
            log("network_retry", attempt=attempt, sleep_s=round(delay, 1))
            time.sleep(delay)
            continue

        if verbose:
            log("request", method=method, status=resp.status_code,
                ms=int((time.monotonic() - budget_wait) * 1000))

        if resp.ok:
            return resp

        try:
            body = resp.json()
        except ValueError:
            body = {}
        kind, detail = classify_error(resp.status_code, body)

        # --- case 2: the access token expired mid-sync ----------------------
        if kind == "auth" and tokens.minted and not refreshed_once:
            refreshed_once = True
            log("access_token_expired", action="refresh_and_retry_once")
            tokens.token(force_refresh=True)
            continue

        # --- case 1 (and a second failure after refreshing) ------------------
        if kind == "auth":
            raise GoogleAdsAuthError(
                f"Google Ads rejected the credential -- {detail}. NOT retrying: "
                "a fresh access token did not fix it, so the shared OAuth "
                "credential itself is bad. A human needs to re-mint the refresh "
                "token against the MCC."
            )
        if kind == "developer_token":
            raise GoogleAdsDeveloperTokenError(
                f"{detail}  Google rejected this account's API access level. "
                "Developer tokens were sunset on 2026-09-09: apply for Explorer, "
                "then Basic if needed, from the Google Ads API Overview page in "
                "Cloud Console. If GOOGLE_ADS_DEVELOPER_TOKEN is set to a stale "
                "value, blank it. This is not a credential change."
            )
        if kind == "permission":
            raise GoogleAdsPermissionError(
                f"{detail}  Check login-customer-id: querying a client account "
                f"under an MCC without it returns exactly this error. "
                f"currently sending: {login_header(cfg) or '<none>'}"
            )
        if kind in ("bad_request", "not_available", "http"):
            raise GoogleAdsAPIError(detail)

        # Only rate_limit and transient reach here -- the two retryable kinds.
        if attempt >= MAX_RETRY_ATTEMPTS:
            cls = GoogleAdsRateLimitError if kind == "rate_limit" else GoogleAdsAPIError
            raise cls(f"{detail} (gave up after {attempt} attempts)")

        delay = _backoff_delay(attempt)
        log(f"{kind}_retry", attempt=attempt, sleep_s=round(delay, 1))
        time.sleep(delay)


def _backoff_delay(attempt: int) -> float:
    """Exponential with jitter, so parallel workers don't resynchronize."""
    return min(2.0 ** attempt, 60.0) + random.uniform(0, 1.0)


# --------------------------------------------------------------------------
# GAQL
# --------------------------------------------------------------------------


def gaql_search(
    cfg: GoogleAdsConfig,
    query: str,
    *,
    tokens: TokenManager,
    customer_id: str | None = None,
    max_pages: int = 100,
    verbose: bool = False,
) -> Iterator[tuple[int, list[dict]]]:
    """Runs a GAQL query, yielding (page_number, results).

    Uses `search` with an explicit pageToken rather than `searchStream`.
    searchStream returns everything in one unpaginated response, which is
    faster for big pulls but gives the rate budget and the retry logic nothing
    to hold on to -- a failure mid-stream means re-running the whole query.
    Paged `search` is the better shape for a connector that must be
    restartable. The production connector can switch to searchStream for
    backfills if throughput ever justifies it.
    """
    cust = _digits(customer_id or cfg.customer_id)
    url = f"{ADS_HOST}/{cfg.api_version}/customers/{cust}/googleAds:search"
    page_token: str | None = None
    page = 0

    while page < max_pages:
        page += 1
        # No pageSize: the API now fixes it at 10,000 rows and rejects any
        # value with requestError.PAGE_SIZE_NOT_SUPPORTED. Use LIMIT in the
        # query to cap rows instead.
        body: dict = {"query": query}
        if page_token:
            body["pageToken"] = page_token

        resp = request_with_retry(
            "POST", url, tokens=tokens, cfg=cfg, json_body=body, verbose=verbose
        )
        payload = resp.json()
        yield page, payload.get("results", [])

        page_token = payload.get("nextPageToken")
        if not page_token:
            return

    # Fell out of the loop with a nextPageToken still pending: stopping here
    # would hand back a truncated result that looks complete.
    raise GoogleAdsAPIError(
        f"gaql_search stopped at max_pages={max_pages} with more pages remaining; "
        "refusing to return a truncated result. Narrow the date window or raise max_pages."
    )


def list_accessible_customers(cfg: GoogleAdsConfig, tokens: TokenManager) -> list[str]:
    """Every customer this OAuth credential can reach, as bare ids.

    The direct analogue of Meta's GET /me/adaccounts, and the cheapest possible
    proof that the credential works. Notably this does NOT need a customer id,
    which is what makes discovery mode possible before any client exists.
    """
    url = f"{ADS_HOST}/{cfg.api_version}/customers:listAccessibleCustomers"
    resp = request_with_retry("GET", url, tokens=tokens, cfg=cfg)
    names = resp.json().get("resourceNames", [])
    return [n.rsplit("/", 1)[-1] for n in names]


# The MCC's own view of everything linked beneath it. customer_client.level 0
# is the manager itself, 1+ are the accounts linked under it -- which is
# exactly "which clients have granted us access".
HIERARCHY_QUERY = """
    SELECT
        customer_client.id,
        customer_client.descriptive_name,
        customer_client.currency_code,
        customer_client.time_zone,
        customer_client.manager,
        customer_client.status,
        customer_client.level
    FROM customer_client
    WHERE customer_client.status = 'ENABLED'
    ORDER BY customer_client.level
"""

# The account's real conversion windows. This is what turns the restatement
# lookback from a guess into a measured value -- see DEFAULT_RESTATEMENT_DAYS.
CONVERSION_WINDOW_QUERY = """
    SELECT
        conversion_action.name,
        conversion_action.category,
        conversion_action.status,
        conversion_action.click_through_lookback_window_days,
        conversion_action.view_through_lookback_window_days
    FROM conversion_action
    WHERE conversion_action.status = 'ENABLED'
"""


# Everything beyond the original ten columns. (kind, GAQL field, column)
#   int     whole number, "" never -- missing reads as 0
#   float   plain number (conversions, currency already in units)
#   micros  currency in micros -> divide by 1,000,000
#   micros? as micros, but missing -> None. The API omits the budget amount
#           for most campaigns here; that is "not returned", not a $0 budget.
#   pct     a fraction (0.0512) -> percent (5.12), like ctr. Missing -> 0
#   share   like pct, but missing means "not reported" -> None, not 0.
#           Impression share is withheld for some campaign types and days;
#           a blank and a measured 0% are different facts.
#   text    enum / string, passed through
CAMPAIGN_DIMENSIONS = [
    ("text",   "campaign.status",                    "campaign_status"),
    ("text",   "campaign.advertising_channel_type",  "channel_type"),
    ("text",   "campaign.bidding_strategy_type",     "bidding_strategy"),
    ("micros?", "campaign_budget.amount_micros",     "daily_budget"),
]

CAMPAIGN_KPIS = [
    ("int",    "metrics.interactions",                        "interactions"),
    ("pct",    "metrics.interaction_rate",                    "interaction_rate"),
    ("micros", "metrics.average_cost",                        "avg_cost_per_interaction"),
    ("int",    "metrics.engagements",                         "engagements"),
    ("pct",    "metrics.engagement_rate",                     "engagement_rate"),
    ("float",  "metrics.conversions_value",                   "conversions_value"),
    ("pct",    "metrics.conversions_from_interactions_rate",  "conversion_rate"),
    ("micros", "metrics.cost_per_conversion",                 "cost_per_conversion"),
    ("float",  "metrics.value_per_conversion",                "value_per_conversion"),
    ("float",  "metrics.all_conversions",                     "all_conversions"),
    ("float",  "metrics.all_conversions_value",               "all_conversions_value"),
    ("micros", "metrics.cost_per_all_conversions",            "cost_per_all_conversions"),
    ("float",  "metrics.view_through_conversions",            "view_through_conversions"),
    ("float",  "metrics.cross_device_conversions",            "cross_device_conversions"),
    ("int",    "metrics.video_trueview_views",                       "video_views"),
    ("pct",    "metrics.video_trueview_view_rate",                   "video_view_rate"),
    ("pct",    "metrics.video_quartile_p25_rate",             "video_p25_rate"),
    ("pct",    "metrics.video_quartile_p50_rate",             "video_p50_rate"),
    ("pct",    "metrics.video_quartile_p75_rate",             "video_p75_rate"),
    ("pct",    "metrics.video_quartile_p100_rate",            "video_p100_rate"),
    ("share",  "metrics.search_impression_share",             "search_impression_share"),
    ("share",  "metrics.search_budget_lost_impression_share", "search_lost_is_budget"),
    ("share",  "metrics.search_rank_lost_impression_share",   "search_lost_is_rank"),
    ("share",  "metrics.search_top_impression_share",         "search_top_impression_share"),
    ("share",  "metrics.search_absolute_top_impression_share", "search_abs_top_impression_share"),
    ("share",  "metrics.top_impression_percentage",           "top_impression_pct"),
    ("share",  "metrics.absolute_top_impression_percentage",  "abs_top_impression_pct"),
    ("int",    "metrics.invalid_clicks",                      "invalid_clicks"),
    ("pct",    "metrics.invalid_click_rate",                  "invalid_click_rate"),
]


def _camel(field: str) -> tuple[str, str]:
    """'campaign_budget.amount_micros' -> ('campaignBudget', 'amountMicros').
    GAQL is snake_case; the REST response is camelCase."""
    resource, name = field.split(".", 1)

    def camel(snake: str) -> str:
        head, *rest = snake.split("_")
        return head + "".join(w.title() for w in rest)

    return camel(resource), camel(name)


def campaign_daily_query(since: date, until: date) -> str:
    """One row per campaign per day.

    segments.date is what makes it daily -- without it the metrics come back
    rolled up across the whole range, the same trap as Meta's time_increment.
    Google omits rows with no activity entirely, so a zero-impression day is
    simply absent rather than present as zeros.
    """
    fields = [
        "segments.date",
        "campaign.id",
        "campaign.name",
        *(f for _k, f, _c in CAMPAIGN_DIMENSIONS if f != "campaign.name"),
        "metrics.impressions",
        "metrics.clicks",
        "metrics.cost_micros",
        "metrics.ctr",
        "metrics.average_cpc",
        "metrics.average_cpm",
        "metrics.conversions",
        *(f for _k, f, _c in CAMPAIGN_KPIS),
        "customer.descriptive_name",
    ]
    select = ",\n        ".join(fields)
    return f"""
    SELECT
        {select}
    FROM campaign
    WHERE segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}'
    """


# --------------------------------------------------------------------------
# Normalization -- the one shared row shape everything in this POC produces
# --------------------------------------------------------------------------

# Deliberately identical to the Meta PoC's COLUMNS except for the account
# identifier, so the two sources can be unioned in Silver without reshaping.
COLUMNS = [
    "date",
    "campaign_id",
    "campaign_name",
    *(c for _k, _f, c in CAMPAIGN_DIMENSIONS),
    "impressions",
    "clicks",
    "spend",
    "ctr",
    "cpc",
    "cpm",
    "reach",
    "conversions",
    *(c for _k, _f, c in CAMPAIGN_KPIS),
    "source",
    # POC metadata, mirroring how the Geotab Bronze design keys its tables.
    "customer_id",
    "customer_name",
    "extracted_at",
]

MICROS = 1_000_000


def _as_float(value: Any) -> float:
    """The REST interface returns int64 fields as JSON STRINGS ("1234") while
    doubles come back as real numbers -- so both have to survive this, and a
    missing field has to read as zero rather than blowing up."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    return int(_as_float(value))


def _extended_fields(result: dict) -> dict:
    """The dimension and KPI columns driven by CAMPAIGN_DIMENSIONS /
    CAMPAIGN_KPIS. Reads each from its camelCase response key."""
    out: dict = {}
    for kind, field, column in (*CAMPAIGN_DIMENSIONS, *CAMPAIGN_KPIS):
        resource, key = _camel(field)
        raw = (result.get(resource) or {}).get(key)
        if kind == "text":
            out[column] = raw
        elif kind == "share":
            out[column] = None if raw is None else round(_as_float(raw) * 100, 4)
        elif kind == "pct":
            out[column] = round(_as_float(raw) * 100, 4)
        elif kind == "micros?":
            out[column] = None if raw is None else round(_as_float(raw) / MICROS, 4)
        elif kind == "micros":
            out[column] = round(_as_float(raw) / MICROS, 4)
        elif kind == "int":
            out[column] = _as_int(raw)
        else:  # float
            out[column] = round(_as_float(raw), 4)
    return out


def normalize_row(result: dict, cfg: GoogleAdsConfig, extracted_at: str) -> dict:
    """One raw GAQL result -> one row of the shared schema.

    Two conversions that are easy to get wrong:
      * cost_micros and average_cpc are MICROS. Dividing by 1,000,000 is not
        optional and the numbers look plausible if you forget, which is what
        makes it dangerous.
      * GAQL is written in snake_case but the REST response comes back in
        camelCase (cost_micros -> costMicros). Querying one and reading the
        other silently yields zeros.
    """
    segments = result.get("segments") or {}
    campaign = result.get("campaign") or {}
    metrics = result.get("metrics") or {}

    impressions = _as_int(metrics.get("impressions"))
    spend = _as_float(metrics.get("costMicros")) / MICROS
    # average_cpm is already per-thousand, in micros. Fall back to computing it
    # when the account doesn't report it.
    cpm = _as_float(metrics.get("averageCpm")) / MICROS
    if not cpm and impressions:
        cpm = spend / impressions * 1000

    return {
        "date": segments.get("date"),
        "campaign_id": str(campaign.get("id") or ""),
        "campaign_name": campaign.get("name"),
        "impressions": impressions,
        "clicks": _as_int(metrics.get("clicks")),
        "spend": round(spend, 2),
        # Google reports ctr as a fraction (0.0512); Meta reports it as a
        # percent (5.12). Normalized to percent here so the two sources agree.
        "ctr": round(_as_float(metrics.get("ctr")) * 100, 4),
        "cpc": round(_as_float(metrics.get("averageCpc")) / MICROS, 4),
        "cpm": round(cpm, 4),
        # Google Ads has no reach equivalent at this grain. Explicitly None
        # rather than 0 -- "not measured" and "measured as zero" are different
        # facts and a dashboard that averages them should not confuse the two.
        "reach": None,
        # Fractional by design: data-driven attribution splits credit across
        # touchpoints, so 3.67 conversions is a normal value, not a bug.
        "conversions": round(_as_float(metrics.get("conversions")), 2),
        **_extended_fields(result),
        "source": "google_ads",
        "customer_id": _digits(cfg.customer_id),
        "customer_name": (result.get("customer") or {}).get("descriptiveName"),
        "extracted_at": extracted_at,
    }


# --------------------------------------------------------------------------
# Shared script scaffolding
# --------------------------------------------------------------------------


NOT_CONFIGURED_HELP = """Not configured -- nothing to pull, and this package will not invent anything.

Set these in .env (copy .env.example):
    GOOGLE_ADS_CLIENT_ID / _CLIENT_SECRET   the OAuth client, from Cloud Console
    GOOGLE_ADS_REFRESH_TOKEN                run `python get_refresh_token.py`
    GOOGLE_ADS_CUSTOMER_ID                  the client's account

(GOOGLE_ADS_DEVELOPER_TOKEN is optional and ignored by the API -- leave blank.)

There is deliberately no mock/synthetic mode. Invented rows are
indistinguishable from real ones once written, so a run started by mistake
would contaminate real data. This package emits vendor data or nothing."""


def require_live(cfg: GoogleAdsConfig) -> None:
    """Gate every data path. Raises rather than returning anything fabricated."""
    if not cfg.is_configured:
        raise GoogleAdsNotConfiguredError(NOT_CONFIGURED_HELP)



def banner(step: str, title: str) -> None:
    print("=" * 68)
    print(f"{step}) {title}")


def mode_line(cfg: GoogleAdsConfig) -> str:
    if cfg.is_configured:
        return (
            f"LIVE  -- customer={cfg.customer} mcc={cfg.manager or '<none>'} "
            f"api={cfg.api_version} refresh_token={mask(cfg.refresh_token)} "
            f"ceiling={cfg.requests_per_second}/s"
        )
    if cfg.has_credential:
        return (
            "DISCOVERY  -- OAuth credential present, GOOGLE_ADS_CUSTOMER_ID not set. "
            f"refresh_token={mask(cfg.refresh_token)} api={cfg.api_version}. "
            "Can ask what this credential reaches; cannot pull data yet."
        )
    return "NOT CONFIGURED  -- OAuth credential not set. No data path will run."


def default_window(days: int = 7) -> tuple[date, date]:
    """Yesterday backwards. Today is deliberately excluded -- a partial day is
    guaranteed to be restated and just adds noise."""
    until = date.today() - timedelta(days=1)
    since = until - timedelta(days=days - 1)
    return since, until


def fetch_campaign_daily(
    cfg: GoogleAdsConfig,
    since: date,
    until: date,
    *,
    tokens: TokenManager | None = None,
    verbose: bool = False,
) -> list[dict]:
    """One normalized row per campaign per day across the window.

    Pages GAQL until Google stops handing back a nextPageToken. Raises
    GoogleAdsNotConfiguredError rather than returning anything when credentials
    are missing -- every row this returns came from Google.
    """
    require_live(cfg)
    extracted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    assert tokens is not None, "fetch_campaign_daily needs a TokenManager"
    rows: list[dict] = []
    for page, results in gaql_search(
        cfg, campaign_daily_query(since, until), tokens=tokens, verbose=verbose
    ):
        log("gaql_page", page=page, rows=len(results))
        rows.extend(normalize_row(r, cfg, extracted_at) for r in results)
    return rows


def row_key(row: dict) -> str:
    """The natural key of a normalized row.

    This is the merge key in Bronze. It exists because Google Ads rows are not
    append-only -- the same (date, campaign) is re-issued with different
    numbers as attribution matures, so a warehouse load has to upsert on this,
    never insert.
    """
    return f"{row['date']}|{row['campaign_id']}"
