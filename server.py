"""
============================================================================
 SEO MCP SERVER  —  Ahrefs REST API v3  +  Google Analytics 4 (Data API)
============================================================================

WHY THIS EXISTS
---------------
Ahrefs ships an official *hosted* MCP server (https://api.ahrefs.com/mcp/mcp),
but it authenticates via OAuth and binds to whichever Ahrefs account email you
consent with. That is a problem when:

  - the Ahrefs seat lives on a different email than the Claude account
  - you want one server that ALSO exposes Google Analytics data
  - you want to drive it from n8n / your own audit backend

Ahrefs' Terms explicitly FORBID hitting their MCP endpoint from custom scripts,
bridges, or standalone HTTP/JSON-RPC clients. But the Ahrefs *REST API v3* is
exactly what you're supposed to build on, and it authenticates with a plain
bearer token — no OAuth, no account binding.

So: this server talks to the REST API, not to Ahrefs' MCP, and wraps it as an
MCP server of our own. That is the supported path.

TRANSPORT
---------
Streamable HTTP (the current MCP standard transport; SSE is deprecated).
FastMCP serves it at:            http://<host>:<port>/mcp
`transport="http"` in FastMCP 2.x == Streamable HTTP. Do NOT use
transport="sse" — most clients have dropped it.

TOOL DESIGN NOTES
-----------------
  * Every Ahrefs list endpoint REQUIRES a `select` param (comma-separated
    column names). Omit it => HTTP 400. Every tool below ships a sane default.
  * `ahrefs_raw` is an escape hatch so the agent can reach endpoints we didn't
    wrap (site-audit, rank-tracker, batch-analysis, anything Ahrefs adds later)
    without a code change.
  * Errors are translated into ACTIONABLE messages — an LLM reading
    "403: plan doesn't include this endpoint" can self-correct; it cannot
    self-correct from a bare stack trace.
============================================================================
"""

import os
import json
import logging
from typing import Any, Optional

import httpx
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
# Log to stderr. NEVER log to stdout on a stdio transport — stdout is the
# JSON-RPC channel and any stray print() corrupts the protocol stream.
# (We're on HTTP here so it's safe either way, but keep the habit.)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("seo-mcp")


# ---------------------------------------------------------------------------
# CONFIG  (all via env vars — never hardcode keys)
# ---------------------------------------------------------------------------

# Ahrefs API v3 key. Generate at: https://app.ahrefs.com/account/api-keys
# IMPORTANT: create an *API key*, not an "MCP key". MCP-scoped keys only work
# against Ahrefs' own hosted MCP server; the REST API rejects them.
AHREFS_KEY = os.environ.get("AHREFS_API_KEY")
if not AHREFS_KEY:
    raise SystemExit(
        "AHREFS_API_KEY is not set.\n"
        "  1. Go to https://app.ahrefs.com/account/api-keys\n"
        "  2. Create an API key (v3 scope)\n"
        "  3. export AHREFS_API_KEY=your_key   (or put it in .env)"
    )

AHREFS_BASE = "https://api.ahrefs.com/v3"

# GA4 property. Accepts "123456789" or "properties/123456789" — we normalise.
GA4_PROPERTY = os.environ.get("GA4_PROPERTY_ID", "").strip()
if GA4_PROPERTY and not GA4_PROPERTY.startswith("properties/"):
    GA4_PROPERTY = f"properties/{GA4_PROPERTY}"

# GA4 auth is picked up implicitly by the Google client library from
# GOOGLE_APPLICATION_CREDENTIALS (path to a service-account JSON file).
# We don't read it here — google.auth does.


# ---------------------------------------------------------------------------
# MCP SERVER INSTANCE
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="seo-mcp",
    instructions=(
        "Ahrefs SEO data + Google Analytics 4 traffic data. "
        "Ahrefs numbers are ESTIMATES (crawl-derived); GA4 numbers are ACTUAL "
        "(first-party). When both are available for the same question, report "
        "GA4 as truth and Ahrefs as directional. Ahrefs list endpoints require "
        "a `select` param — defaults are provided but can be overridden. "
        "Every Ahrefs call burns API units from a monthly quota; call "
        "ahrefs_limits_and_usage if calls start failing with 403."
    ),
)


# ---------------------------------------------------------------------------
# SHARED HTTP CLIENT
# ---------------------------------------------------------------------------
# One long-lived AsyncClient = connection pooling + keep-alive. Creating a new
# client per request would add a TLS handshake to every single tool call.
_client = httpx.AsyncClient(
    base_url=AHREFS_BASE,
    headers={
        "Authorization": f"Bearer {AHREFS_KEY}",   # Ahrefs v3 = bearer token auth
        "Accept": "application/json",
    },
    timeout=httpx.Timeout(60.0, connect=10.0),     # backlink queries can be slow
)


async def _get(path: str, params: dict[str, Any]) -> dict:
    """
    Central Ahrefs GET helper.

    Does three jobs, so no tool below has to repeat them:
      1. Strips None values — httpx would otherwise serialise them as "None"
         and Ahrefs would 400.
      2. Maps HTTP status codes to messages the *agent* can act on.
      3. Logs the call for debugging.
    """
    clean = {k: v for k, v in params.items() if v is not None}
    log.info("Ahrefs GET %s %s", path, clean)

    r = await _client.get(path, params=clean)

    # --- Actionable error mapping ------------------------------------------
    if r.status_code == 400:
        raise ValueError(
            f"Ahrefs 400 (bad request) on {path}. Most common cause: a missing or "
            f"invalid `select` param — every list endpoint requires it, and the valid "
            f"column names differ per endpoint. Ahrefs said: {r.text[:400]}"
        )
    if r.status_code == 401:
        raise ValueError(
            "Ahrefs 401 (unauthorised). AHREFS_API_KEY is wrong, revoked, or is an "
            "MCP-scoped key instead of a REST API v3 key."
        )
    if r.status_code == 403:
        raise ValueError(
            f"Ahrefs 403 (forbidden) on {path}. Either your plan tier does not include "
            f"this endpoint, or your monthly API units are exhausted. "
            f"Call ahrefs_limits_and_usage to check the balance."
        )
    if r.status_code == 429:
        raise ValueError(
            "Ahrefs 429 (rate limited). Wait, then retry with a smaller `limit`."
        )
    r.raise_for_status()
    return r.json()


# ===========================================================================
#  AHREFS — ACCOUNT
# ===========================================================================

@mcp.tool
async def ahrefs_limits_and_usage() -> dict:
    """Ahrefs API unit balance, monthly quota, and per-request row limits.

    Call this FIRST when other Ahrefs tools start returning 403 — the usual
    cause is an exhausted monthly unit allowance, not a broken key.
    """
    return await _get("/subscription-info/limits-and-usage", {})


# ===========================================================================
#  AHREFS — SITE EXPLORER
# ===========================================================================
#
# Shared params across most Site Explorer endpoints:
#
#   target : a domain ("travilo.io"), a URL, or a path prefix
#   mode   : how `target` is interpreted —
#              "exact"      -> that one URL only
#              "prefix"     -> that URL and everything beneath it
#              "domain"     -> the domain, no subdomains          (default here)
#              "subdomains" -> the domain plus all subdomains
#   date   : YYYY-MM-DD snapshot. Ahrefs is a time-series DB; there is no
#            implicit "now". Pass today's date for current data.
#   country: 2-letter lowercase code ("us", "gb", "bd"). Omit = worldwide.
#   select : comma-separated columns. REQUIRED on list endpoints.
#   where  : JSON filter string, e.g. '{"field":"best_position","is":["lte",10]}'
# ===========================================================================

@mcp.tool
async def ahrefs_domain_rating(target: str, date: str) -> dict:
    """Domain Rating (DR) and Ahrefs Rank for a target.

    Args:
        target: domain or URL, e.g. "travilo.io"
        date:   YYYY-MM-DD snapshot date
    """
    return await _get("/site-explorer/domain-rating", {"target": target, "date": date})


@mcp.tool
async def ahrefs_backlinks_stats(target: str, date: str, mode: str = "domain") -> dict:
    """Aggregate backlink counts: live backlinks, referring domains, dofollow split.

    Cheap (1 row) — use this before pulling the full backlink list, to size the job.
    """
    return await _get(
        "/site-explorer/backlinks-stats",
        {"target": target, "date": date, "mode": mode},
    )


@mcp.tool
async def ahrefs_metrics(
    target: str,
    date: str,
    mode: str = "domain",
    country: Optional[str] = None,
    volume_mode: str = "monthly",
) -> dict:
    """Headline metrics: organic traffic, organic keywords, traffic value, paid stats.

    Args:
        volume_mode: "monthly" or "average" — how search volume is aggregated.
    """
    return await _get(
        "/site-explorer/metrics",
        {
            "target": target,
            "date": date,
            "mode": mode,
            "country": country,
            "volume_mode": volume_mode,
        },
    )


@mcp.tool
async def ahrefs_organic_keywords(
    target: str,
    date: str,
    country: str,
    select: str = "keyword,best_position,volume,keyword_difficulty,traffic,cpc,sup",
    limit: int = 50,
    order_by: str = "traffic:desc",
    where: Optional[str] = None,
    mode: str = "domain",
) -> dict:
    """Keywords the target currently ranks for in organic search.

    Args:
        order_by: "column:asc" or "column:desc", e.g. "best_position:asc"
        where:    JSON filter, e.g. '{"field":"best_position","is":["lte",10]}'
                  to get only top-10 rankings.
    """
    return await _get(
        "/site-explorer/organic-keywords",
        {
            "target": target, "date": date, "country": country,
            "select": select, "limit": limit, "order_by": order_by,
            "where": where, "mode": mode,
        },
    )


@mcp.tool
async def ahrefs_top_pages(
    target: str,
    date: str,
    country: Optional[str] = None,
    select: str = "url,sum_traffic,traffic_value,keywords,top_keyword,top_keyword_volume",
    limit: int = 50,
    order_by: str = "sum_traffic:desc",
    mode: str = "domain",
) -> dict:
    """Highest-organic-traffic pages on the target domain.

    Cross-check these against ga4_organic_landing_pages — where Ahrefs and GA4
    disagree badly, Ahrefs' crawl is stale or the page is gated.
    """
    return await _get(
        "/site-explorer/top-pages",
        {
            "target": target, "date": date, "country": country,
            "select": select, "limit": limit, "order_by": order_by, "mode": mode,
        },
    )


@mcp.tool
async def ahrefs_refdomains(
    target: str,
    select: str = "domain,domain_rating,links_to_target,dofollow_links,first_seen,last_seen",
    limit: int = 50,
    order_by: str = "domain_rating:desc",
    where: Optional[str] = None,
    mode: str = "domain",
) -> dict:
    """Referring domains linking to the target (one row per linking domain)."""
    return await _get(
        "/site-explorer/refdomains",
        {
            "target": target, "select": select, "limit": limit,
            "order_by": order_by, "where": where, "mode": mode,
        },
    )


@mcp.tool
async def ahrefs_backlinks(
    target: str,
    select: str = "url_from,url_to,anchor,domain_rating_source,is_dofollow,first_seen,link_type",
    limit: int = 50,
    order_by: str = "domain_rating_source:desc",
    where: Optional[str] = None,
    mode: str = "domain",
) -> dict:
    """Individual backlinks pointing to the target (one row per link).

    This is the most unit-expensive Ahrefs endpoint. Keep `limit` tight and
    filter with `where` rather than pulling everything and post-filtering.
    """
    return await _get(
        "/site-explorer/backlinks",
        {
            "target": target, "select": select, "limit": limit,
            "order_by": order_by, "where": where, "mode": mode,
        },
    )


@mcp.tool
async def ahrefs_broken_backlinks(
    target: str,
    select: str = "url_from,url_to,anchor,http_code,domain_rating_source",
    limit: int = 50,
    mode: str = "domain",
) -> dict:
    """Backlinks pointing at 4xx/5xx URLs on the target.

    These are link-reclamation opportunities: someone already linked to you,
    the target page just 404s. Fix with a redirect and you recover the equity.
    """
    return await _get(
        "/site-explorer/broken-backlinks",
        {"target": target, "select": select, "limit": limit, "mode": mode},
    )


@mcp.tool
async def ahrefs_anchors(
    target: str,
    select: str = "anchor,refdomains,dofollow_refdomains,backlinks",
    limit: int = 50,
    order_by: str = "refdomains:desc",
    mode: str = "domain",
) -> dict:
    """Anchor-text distribution of backlinks pointing to the target.

    Over-optimised exact-match anchors are a spam signal — this is how you audit for it.
    """
    return await _get(
        "/site-explorer/anchors",
        {
            "target": target, "select": select, "limit": limit,
            "order_by": order_by, "mode": mode,
        },
    )


@mcp.tool
async def ahrefs_organic_competitors(
    target: str,
    date: str,
    country: str,
    select: str = "competitor_domain,shared_keywords,competitor_keywords,target_keywords",
    limit: int = 20,
    mode: str = "domain",
) -> dict:
    """Domains competing for the same organic keywords as the target.

    Note: these are SERP competitors, which often differ from business competitors.
    """
    return await _get(
        "/site-explorer/organic-competitors",
        {
            "target": target, "date": date, "country": country,
            "select": select, "limit": limit, "mode": mode,
        },
    )


# ===========================================================================
#  AHREFS — KEYWORDS EXPLORER
# ===========================================================================

@mcp.tool
async def ahrefs_keywords_overview(
    keywords: str,
    country: str,
    select: str = "keyword,volume_monthly,difficulty,cpc,clicks,global_volume,parent_topic",
) -> dict:
    """Metrics for a specific list of keywords.

    Args:
        keywords: comma-separated, e.g. "time tracking software,employee monitoring"
        country:  2-letter code, e.g. "us"
    """
    return await _get(
        "/keywords-explorer/overview",
        {"keywords": keywords, "country": country, "select": select},
    )


@mcp.tool
async def ahrefs_matching_terms(
    keywords: str,
    country: str,
    select: str = "keyword,volume,difficulty,cpc,clicks",
    limit: int = 50,
    order_by: str = "volume:desc",
    match_mode: str = "terms",
) -> dict:
    """Keyword ideas CONTAINING the seed terms — the main keyword-discovery tool.

    Args:
        match_mode: "terms" (words in any order) or "phrase" (exact phrase).
    """
    return await _get(
        "/keywords-explorer/matching-terms",
        {
            "keywords": keywords, "country": country, "select": select,
            "limit": limit, "order_by": order_by, "match_mode": match_mode,
        },
    )


@mcp.tool
async def ahrefs_related_terms(
    keywords: str,
    country: str,
    select: str = "keyword,volume,difficulty,cpc",
    limit: int = 50,
    order_by: str = "volume:desc",
) -> dict:
    """Keyword ideas SEMANTICALLY related to the seeds (they need not contain the seed words).

    Use this to find topic clusters that matching-terms would miss.
    """
    return await _get(
        "/keywords-explorer/related-terms",
        {
            "keywords": keywords, "country": country, "select": select,
            "limit": limit, "order_by": order_by,
        },
    )


# ===========================================================================
#  AHREFS — ESCAPE HATCH
# ===========================================================================

@mcp.tool
async def ahrefs_raw(path: str, params_json: str = "{}") -> dict:
    """Call ANY Ahrefs v3 endpoint that has no dedicated tool above.

    Use for: /site-audit/*, /rank-tracker/*, /batch-analysis/*, /serp-overview/*,
    or any endpoint Ahrefs adds after this server was written.

    Args:
        path:        endpoint path with leading slash, WITHOUT the /v3 prefix.
                     e.g. "/site-explorer/pages-by-traffic"
        params_json: JSON object of query params as a string.
                     e.g. '{"target":"travilo.io","select":"url","limit":10}'

    Remember: list endpoints still require `select`.
    """
    try:
        params = json.loads(params_json)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"params_json is not valid JSON ({e}). It must be a JSON object string, "
            f'e.g. \'{{"target":"example.com","select":"url","limit":10}}\''
        )
    if not isinstance(params, dict):
        raise ValueError("params_json must be a JSON OBJECT, not an array or scalar.")
    if not path.startswith("/"):
        path = "/" + path
    return await _get(path, params)


# ===========================================================================
#  GOOGLE ANALYTICS 4
# ===========================================================================
#
# Auth: a service-account JSON, path in GOOGLE_APPLICATION_CREDENTIALS.
# The service-account email must be granted Viewer on the GA4 property
# (GA4 Admin -> Property Access Management -> add the ...iam.gserviceaccount.com
# address as Viewer). Without that step every call 403s.
#
# The Google client is SYNCHRONOUS. We build it lazily (first use) rather than
# at import, so the server still boots and serves Ahrefs tools even if GA4
# credentials are missing or misconfigured.
# ===========================================================================

_ga_singleton = None


def _ga_client():
    """Lazily construct and cache the GA4 client."""
    global _ga_singleton
    if _ga_singleton is None:
        try:
            from google.analytics.data_v1beta import BetaAnalyticsDataClient
            _ga_singleton = BetaAnalyticsDataClient()
        except Exception as e:
            raise ValueError(
                f"Could not initialise the GA4 client ({e}). Check that "
                f"GOOGLE_APPLICATION_CREDENTIALS points to a valid service-account "
                f"JSON file and that the Google Analytics Data API is enabled in "
                f"that GCP project."
            )
    return _ga_singleton


def _resolve_property(property_id: Optional[str]) -> str:
    """Normalise a property id to the 'properties/NNNN' form the API wants."""
    prop = (property_id or GA4_PROPERTY or "").strip()
    if not prop:
        raise ValueError(
            "No GA4 property specified. Set the GA4_PROPERTY_ID env var, or pass "
            "property_id to the tool. Find it in GA4 Admin -> Property Settings."
        )
    if not prop.startswith("properties/"):
        prop = f"properties/{prop}"
    return prop


@mcp.tool
async def ga4_run_report(
    start_date: str,
    end_date: str,
    metrics: str = "sessions,totalUsers,screenPageViews,conversions",
    dimensions: str = "date",
    limit: int = 100,
    property_id: Optional[str] = None,
    dimension_filter_field: Optional[str] = None,
    dimension_filter_value: Optional[str] = None,
) -> dict:
    """Run an arbitrary GA4 report. This is the general-purpose GA4 tool.

    Args:
        start_date / end_date:
            "YYYY-MM-DD", or relative: "today", "yesterday", "28daysAgo", "90daysAgo".
        metrics:
            Comma-separated GA4 metric API names. Common ones:
            sessions, totalUsers, newUsers, screenPageViews, bounceRate,
            engagementRate, averageSessionDuration, conversions, totalRevenue
        dimensions:
            Comma-separated GA4 dimension API names. Common ones:
            date, sessionSource, sessionMedium, sessionDefaultChannelGroup,
            pagePath, landingPage, country, deviceCategory, sessionCampaignName
        dimension_filter_field / dimension_filter_value:
            Optional exact-match filter. e.g. field "sessionDefaultChannelGroup"
            with value "Organic Search" to isolate SEO traffic.

    Returns a flat list of row dicts — dimensions and metrics merged per row.
    """
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest, Filter, FilterExpression,
    )

    prop = _resolve_property(property_id)

    req = RunReportRequest(
        property=prop,
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        metrics=[Metric(name=m.strip()) for m in metrics.split(",") if m.strip()],
        dimensions=[Dimension(name=d.strip()) for d in dimensions.split(",") if d.strip()],
        limit=limit,
    )

    if dimension_filter_field and dimension_filter_value:
        req.dimension_filter = FilterExpression(
            filter=Filter(
                field_name=dimension_filter_field,
                string_filter=Filter.StringFilter(value=dimension_filter_value),
            )
        )

    log.info("GA4 runReport %s %s..%s dims=%s mets=%s",
             prop, start_date, end_date, dimensions, metrics)

    try:
        resp = _ga_client().run_report(req)
    except Exception as e:
        raise ValueError(
            f"GA4 runReport failed: {e}. Most common causes: (a) the service account "
            f"is not added as a Viewer on property {prop}; (b) a metric or dimension "
            f"API name is misspelled — they are camelCase and case-sensitive."
        )

    # Flatten the protobuf response into plain dicts the LLM can read directly.
    dim_names = [h.name for h in resp.dimension_headers]
    met_names = [h.name for h in resp.metric_headers]
    rows = [
        {
            **{dim_names[i]: v.value for i, v in enumerate(row.dimension_values)},
            **{met_names[i]: v.value for i, v in enumerate(row.metric_values)},
        }
        for row in resp.rows
    ]
    return {"property": prop, "row_count": resp.row_count, "rows": rows}


@mcp.tool
async def ga4_organic_landing_pages(
    start_date: str = "28daysAgo",
    end_date: str = "yesterday",
    limit: int = 50,
    property_id: Optional[str] = None,
) -> dict:
    """Shortcut: top ORGANIC-SEARCH landing pages, with sessions and conversions.

    This is the GA4 half of the SEO picture. Pair it with ahrefs_top_pages:
    Ahrefs tells you what it *thinks* drives traffic (estimated, from crawl data),
    GA4 tells you what *actually* did (measured, first-party).
    """
    return await ga4_run_report(
        start_date=start_date,
        end_date=end_date,
        metrics="sessions,totalUsers,conversions,engagementRate",
        dimensions="landingPage",
        limit=limit,
        property_id=property_id,
        dimension_filter_field="sessionDefaultChannelGroup",
        dimension_filter_value="Organic Search",
    )


@mcp.tool
async def ga4_channel_breakdown(
    start_date: str = "28daysAgo",
    end_date: str = "yesterday",
    property_id: Optional[str] = None,
) -> dict:
    """Shortcut: sessions / users / conversions split by default channel group.

    Answers "where is traffic actually coming from" — Organic Search, Paid Search,
    Direct, Referral, Organic Social, Email, etc.
    """
    return await ga4_run_report(
        start_date=start_date,
        end_date=end_date,
        metrics="sessions,totalUsers,conversions,engagementRate",
        dimensions="sessionDefaultChannelGroup",
        limit=50,
        property_id=property_id,
    )


# ===========================================================================
#  ENTRYPOINT
# ===========================================================================

if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))

    log.info("Starting seo-mcp (Streamable HTTP) on http://%s:%s/mcp", host, port)

    # transport="http" == Streamable HTTP in FastMCP 2.x.
    # Endpoint is served at /mcp by default. Do NOT use transport="sse":
    # SSE is the deprecated legacy transport and most clients have dropped it.
    mcp.run(
        transport="http",
        host=host,
        port=port,
        path="/mcp",
    )
