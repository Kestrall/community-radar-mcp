# community-radar-mcp

An MCP server that gives an AI assistant a live view of the **OVHcloud Public Cloud community**: what users ask,
what hurts, what is waiting for an answer, what is already on the roadmap, and which questions the official
documentation does not cover yet.

It is built for the daily work of a Tech Community Manager / Developer Advocate: triage, answer with the right
guide, report irritants to product teams with data, and turn recurring questions into tutorials.

> Independent project built on public data. Not affiliated with or endorsed by OVHcloud.

## What it watches

| Channel | Source | What is read |
|---|---|---|
| Forum | [community.ovhcloud.com](https://community.ovhcloud.com/c/public-cloud/11), Public Cloud category (Discourse JSON API) | topics, replies, views, likes, accepted answers, staff replies |
| Public roadmap | [ovh/public-cloud-roadmap](https://github.com/ovh/public-cloud-roadmap) | requests, product labels, 👍 votes, shipped/closed state |
| GitHub issues | [ovh/terraform-provider-ovh](https://github.com/ovh/terraform-provider-ovh), [ovh/ovhcloud-cli](https://github.com/ovh/ovhcloud-cli) | bug reports and questions from IaC/CLI users |
| Reddit | [r/ovh](https://www.reddit.com/r/ovh) | newest posts (official API when credentials are set, RSS otherwise) |
| Official guides | [ovh/docs](https://github.com/ovh/docs) | ~500 Public Cloud guides, indexed for search (bundled, rebuildable) |

Every thread is classified by **product** (Compute, Managed Kubernetes, Object Storage, Block Storage, Network,
Managed Databases, AI, Data, API/CLI/IaC, Registry, Billing, IAM) and **signal** (incident, bug, billing,
security, feature request, question), in English and French.

## Tools

| Tool | Use it to… |
|---|---|
| `community_pulse` | get the numbers: active and new threads, trend vs previous period, answered/solved rates, forum median first-response time, by channel and product |
| `find_irritants` | rank products by pain (negative signals, engagement, growth) with example threads: the input for a product-team report |
| `unanswered_questions` | triage what nobody answered yet, oldest and most viewed first |
| `search_threads` | find what the community already said about a topic |
| `roadmap_status` | answer "is this planned?" with matching roadmap items, their state and votes |
| `search_docs` | point a user to the right official guide (English or French link) |
| `content_gaps` | list community questions no guide clearly covers: tutorial and FAQ candidates |
| `thread_context` | read a whole forum topic or GitHub issue (staff flagged) with related guides and roadmap items, before drafting a reply |

Prompts: `weekly_report` (report for product managers), `draft_reply` (on-brand answer plus internal escalation
notes, never posted automatically), `tutorial_from_gap`. Resources: `radar://sources`, `radar://taxonomy`.

The server only collects, classifies and measures. Reasoning and writing are left to the client model, so answers
stay grounded in links the server returns.

## Quick start

```sh
pip install git+https://github.com/Kestrall/community-radar-mcp
export GITHUB_TOKEN=...          # optional but recommended: a read-only token lifts GitHub's 60 requests/hour limit
community-radar                  # MCP over stdio
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "community-radar": { "command": "community-radar", "env": { "GITHUB_TOKEN": "..." } }
  }
}
```

Claude Code: `claude mcp add community-radar -- community-radar`, or over HTTP once deployed:
`claude mcp add --transport http community-radar http://<host>:8000/mcp`.

Then ask, for example:

- "What are the Public Cloud irritants of the last two weeks? Write it as a note for the product managers."
- "Which questions are still waiting for an answer? Draft a reply to the most urgent one."
- "Is Karpenter support for Managed Kubernetes on the roadmap, and how many people want it?"
- "Which tutorial is missing from the docs, judging by the forum?"

## Report and digest

Over HTTP the server also serves, next to `/mcp`:

- `GET /report?days=7`: a standalone HTML report (KPIs, conversation by product, top irritants, questions waiting
  for an answer, content gaps, most wanted roadmap items), light and dark mode.
- `GET /digest.json?days=7`: the same snapshot as Telegram-ready messages, used by an n8n workflow that posts the
  weekly digest every Monday.
- `GET /health`

From the shell: `community-radar report --days 7 -o radar.html`, `community-radar digest --days 7`.

## Configuration

| Variable | Default | |
|---|---|---|
| `GITHUB_TOKEN` | – | read-only token (fine-grained, public repositories) |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | – | Reddit "script" app for the official API; RSS fallback otherwise |
| `RADAR_CACHE_TTL` | `900` | cache for community data, in seconds (the full roadmap is cached 6 h) |
| `RADAR_DOCS_INDEX` | bundled | path to a guide index rebuilt with `community-radar build-docs` |
| `RADAR_HOST`, `RADAR_PORT` | `127.0.0.1`, `8000` | HTTP transport |
| `RADAR_ALLOWED_HOSTS` | – | comma-separated `host:port` list, enables DNS rebinding protection |

## Deployment

`deploy/` holds a systemd unit and an install script for a small Debian LXC container (1 vCPU, 512 MB):
copy the sources to `/opt/community-radar`, run `deploy/install.sh`, then edit `/etc/community-radar.env`.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest --cov      # no network access needed: HTTP is mocked
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## License

MIT
