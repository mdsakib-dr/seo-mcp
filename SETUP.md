# SEO MCP — Setup Guide

Custom MCP server exposing **Ahrefs REST API v3** + **Google Analytics 4** over
**Streamable HTTP**. Built to sidestep the account-email binding of Ahrefs' own
hosted MCP server.

---

## Why not just use Ahrefs' hosted MCP?

Ahrefs' hosted MCP (`https://api.ahrefs.com/mcp/mcp`) authenticates via OAuth and
binds to whichever Ahrefs account email you consent with. If your Ahrefs seat is on
a different email than your Claude account, you're stuck.

This server uses the **REST API v3** instead, which authenticates with a plain
bearer token — no OAuth, no account binding. It also lets you fold GA4 into the
same server so an agent can cross-reference estimated vs. actual traffic in one place.

> **Terms note:** Ahrefs forbids driving their *MCP* endpoint from custom scripts or
> bridges. The *REST API* is explicitly the supported path for programmatic access.
> This server uses the REST API. You're inside the rules.

---

## 1. Get credentials

### Ahrefs API key

1. Go to <https://app.ahrefs.com/account/api-keys>
2. **Create API key** — pick **API key**, *not* "MCP key".
   An MCP-scoped key only works against Ahrefs' hosted MCP; the REST API rejects it.
3. Copy it. Optionally set a **monthly API-unit cap** on the key here — an agent
   looping over audit domains can burn units fast.

Requires a paid plan (Lite or above). Every call consumes API units from your
monthly allowance. Check the balance any time at
Account settings → Limits and usage, or via the `ahrefs_limits_and_usage` tool.

### GA4 service account

1. **Google Cloud Console** → create (or pick) a project
2. **APIs & Services → Library** → enable **Google Analytics Data API**
3. **IAM & Admin → Service Accounts** → Create service account → **Keys → Add key →
   JSON** → download it
4. Copy the service-account email (`...@....iam.gserviceaccount.com`)
5. **GA4 → Admin → Property Access Management** → **+** → paste that email → role
   **Viewer** → Add

   *Skipping step 5 is the #1 cause of GA4 403s.*

6. **GA4 → Admin → Property Settings** → copy the numeric **PROPERTY ID**

---

## 2. Install

```bash
cd seo-mcp
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

---

## 3. Configure

```bash
cp .env.example .env
```

Fill it in:

```dotenv
AHREFS_API_KEY=your_ahrefs_v3_key
GA4_PROPERTY_ID=123456789
GOOGLE_APPLICATION_CREDENTIALS=C:\keys\ga4-sa.json
MCP_HOST=0.0.0.0
MCP_PORT=8080
LOG_LEVEL=INFO
```

---

## 4. Run

```bash
python run.py
```

Expected:

```
INFO seo-mcp: Starting seo-mcp (Streamable HTTP) on http://0.0.0.0:8080/mcp
```

Your endpoint is **`http://localhost:8080/mcp`**.

### Confirm the transport is Streamable HTTP

```bash
curl -i -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

You want a `200` and an `Mcp-Session-Id` response header. That header is the
Streamable HTTP fingerprint — if it's absent, you're not on the right transport.

**The `Accept` header must list both `application/json` and `text/event-stream`.**
Streamable HTTP lets the server reply with either a plain JSON body or an SSE
stream on the *same* POST endpoint. Omit either type and you get a 406.

Do **not** use `transport="sse"`. Legacy SSE (two endpoints, `/sse` + `/messages`)
is deprecated and most clients have dropped it.

---

## 5. Connect a client

### Claude Code

```bash
claude mcp add --transport http seo-mcp http://localhost:8080/mcp
```

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "seo-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8080/mcp"]
    }
  }
}
```

Fully quit Claude Desktop (kill it in Task Manager) before relaunching — the config
is only read at startup.

### Claude Web — the catch

Custom connectors on Claude Web require **OAuth 2.1 with dynamic client
registration**. A bare bearer-token or unauthenticated HTTP endpoint will not
connect. Options:

- **Use Claude Desktop / Claude Code instead** (simplest — both accept this server as-is)
- **Put an OAuth 2.1 layer in front** — FastMCP supports auth providers; you'd add one
  and expose the server on a public HTTPS domain
- **Call it from n8n** — n8n's MCP Client node speaks Streamable HTTP and doesn't
  demand OAuth

### n8n

Add an **MCP Client** node → Transport: **HTTP Streamable** → Endpoint:
`https://your-domain/mcp`. Wire it into an AI Agent node as a tool source, same
pattern as your PipeBoard setup.

---

## 6. Deploy

```bash
docker build -t seo-mcp .

docker run -d --name seo-mcp -p 8080:8080 \
  -e AHREFS_API_KEY=xxx \
  -e GA4_PROPERTY_ID=123456789 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/ga4-sa.json \
  -v /local/path/ga4-sa.json:/secrets/ga4-sa.json:ro \
  seo-mcp
```

Behind a reverse proxy, **disable response buffering** on the `/mcp` location or
streamed responses will hang until the request completes:

```nginx
location /mcp {
    proxy_pass http://seo-mcp:8080;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 300s;
    proxy_set_header Connection '';
}
```

---

## Tools

| Tool | What it does |
|---|---|
| `ahrefs_limits_and_usage` | API unit balance + quota. Check this first on 403s. |
| `ahrefs_domain_rating` | DR and Ahrefs Rank |
| `ahrefs_backlinks_stats` | Aggregate link counts (cheap — size the job before pulling lists) |
| `ahrefs_metrics` | Organic traffic, keywords, traffic value |
| `ahrefs_organic_keywords` | Keywords the target ranks for |
| `ahrefs_top_pages` | Highest-traffic pages |
| `ahrefs_refdomains` | Referring domains |
| `ahrefs_backlinks` | Individual backlinks (most unit-expensive) |
| `ahrefs_broken_backlinks` | Links to your 4xx/5xx pages — reclamation targets |
| `ahrefs_anchors` | Anchor-text distribution |
| `ahrefs_organic_competitors` | SERP competitors |
| `ahrefs_keywords_overview` | Metrics for a specific keyword list |
| `ahrefs_matching_terms` | Keyword ideas containing the seeds |
| `ahrefs_related_terms` | Semantically related keyword ideas |
| `ahrefs_raw` | **Escape hatch** — any v3 endpoint, no code change |
| `ga4_run_report` | Arbitrary GA4 report (any metrics × dimensions) |
| `ga4_organic_landing_pages` | Organic landing pages + conversions |
| `ga4_channel_breakdown` | Traffic split by channel group |

---

## Gotchas

**`select` is mandatory.** Every Ahrefs *list* endpoint requires a comma-separated
`select` of column names. Omit it → HTTP 400. Defaults are baked into every tool,
but valid column names differ per endpoint — check
<https://docs.ahrefs.com/en/api/docs/introduction> when overriding.

**`date` is mandatory on Site Explorer.** Ahrefs is a time-series database; there is
no implicit "now". Pass today's date for current data.

**Ahrefs vs GA4 will disagree.** Ahrefs traffic is a crawl-derived *estimate*; GA4 is
*measured* first-party data. Treat GA4 as truth and Ahrefs as directional. The
server's `instructions` string already tells the agent this.

**API units are finite.** `ahrefs_backlinks` is the most expensive endpoint. Filter
with `where` rather than pulling large `limit`s and post-filtering.

**GA4 metric/dimension names are camelCase and case-sensitive.** `sessions` works,
`Sessions` 400s.
