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
| Reddit | [r/ovh](https://www.reddit.com/r/ovh) | newest posts, through the official API, only when approved credentials are set |
| Discord | a server the radar's bot was invited to (e.g. the OVHcloud Discord) | forum posts, threads and questions asked in text channels with their replies, staff recognised by role |
| Official guides | [ovh/docs](https://github.com/ovh/docs) | ~500 Public Cloud guides, indexed for search (bundled, rebuildable) |

Every thread is classified by **product** (Compute, Managed Kubernetes, Object Storage, Block Storage, Network,
Managed Databases, AI, Data, API/CLI/IaC, Registry, Billing, IAM) and **signal** (incident, bug, billing,
security, feature request, question), in English and French.

Reddit requires approved access to its Data API
([Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)),
so the radar does not scrape it: the channel stays off until credentials are configured.

Discord works the same way: no scraping, no user account, only a read-only bot that the server admins invited
(see [Discord](#discord)).

## Tools

| Tool | Use it to… |
|---|---|
| `community_pulse` | get the numbers: active and new threads, trend vs previous period, answered/solved rates, forum median first-response time, by channel and product |
| `find_irritants` | rank products by pain (negative signals, engagement, growth) with example threads: the input for a product-team report |
| `unanswered_questions` | triage what nobody answered yet, oldest and most viewed first |
| `follow_ups` | find threads where OVHcloud owes the user a message, with the evidence and what to say (see below) |
| `search_threads` | find what the community already said about a topic |
| `roadmap_status` | answer "is this planned?" with matching roadmap items, their state and votes |
| `search_docs` | point a user to the right official guide (English or French link) |
| `content_gaps` | list community questions no guide clearly covers: tutorial and FAQ candidates |
| `triage_queue` | the team's queue: status, owner team, notes, threads with new activity first |
| `update_triage` | record a triage decision (status, team, note) in the radar, never on the platform |
| `thread_context` | read a whole forum topic, GitHub issue or Discord conversation (staff flagged) with its follow-up status, related guides and roadmap items, before drafting a reply |

### Follow-ups: beyond "unanswered"

Counting comments is not enough. An issue can be assigned, put in a milestone and linked to a fix while the reporter
still sees no human reply: the work is done, the communication is not. `follow_ups` reads each GitHub issue timeline
(comments, labels, assignments, milestones, linked pull requests) and each forum conversation, then sorts threads
by what the community team should do next:

| Status | Meaning | What to do |
|---|---|---|
| `fixed_untold` | a linked fix was merged, the issue is open and nobody told the reporter | announce the fix and its release |
| `silent_progress` | assigned, milestoned or linked to an open pull request, without a word to the reporter | post a status update |
| `closed_silently` | closed by a maintainer with no linked fix and no comment | explain the closure |
| `no_response` | no maintainer activity at all | reply or triage |
| `triaged_silently` | labelled by maintainers, never answered by a human | acknowledge and say what happens next |
| `waiting_on_us` | the user answered after our last message | follow up |
| `community_answered` | forum topic answered by peers only, no solution marked | check the answer and mark it |

Issues opened by maintainers (internal work items) are left out, and maintainers whose organisation membership is
private (they comment as `CONTRIBUTOR`) are recognised by what they do on the issue: labelling, assigning, closing.

Prompts: `weekly_report` (report for product managers), `draft_reply` (on-brand answer plus internal escalation
notes, never posted automatically), `tutorial_from_gap`. Resources: `radar://sources`, `radar://taxonomy`.

The server only collects, classifies and measures. Reasoning and writing are left to the client model, so answers
stay grounded in links the server returns.

## Quick start

```sh
pip install git+https://github.com/Kestrall/community-radar-mcp
export GITHUB_TOKEN=...          # recommended: a classic token with no scope lifts GitHub's 60 requests/hour limit
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

## Discord

Most developer communities live on Discord, and questions asked there never reach a forum or a ticket. The radar
reads a server through the official REST API with a bot token, read-only, and only where the server admins let it:

1. Create an application on the [Discord developer portal](https://discord.com/developers/applications), add a
   bot and turn on the **Message Content** intent (without it, Discord hides message text from bots).
2. The server admins invite it with **View Channels** and **Read Message History** only (permissions `66560`):
   `https://discord.com/oauth2/authorize?client_id=<application id>&scope=bot&permissions=66560`.
   Channel permissions decide what it sees: private channels stay private.
3. Set `DISCORD_BOT_TOKEN` and `DISCORD_GUILD_ID` (server id: Developer Mode, right-click the server, Copy ID).

What becomes a thread for the radar:

- **forum channels**: each post, with its title and tags (a tag such as *Solved* or *Résolu* marks it solved);
- **threads** opened from a message in a text channel;
- **questions in text channels**: a message long enough or asking something, with the replies that point to it.
  Greetings, bots and system messages are left out.

Staff answers are recognised by role name (`DISCORD_STAFF_ROLES`), which is what `follow_ups` needs to tell a
community answer from an official one. Nothing is posted, nothing is written to disk: messages stay in the
in-memory cache. The bot needs no gateway connection, so there is no extra process to run.

## Report and digest

Over HTTP the server also serves, next to `/mcp`:

- `GET /report?days=7&source=discord`: a standalone HTML report (`source` optional: forum, github, roadmap, reddit or discord) (KPIs, conversation by product, top irritants, questions waiting
  for an answer, content gaps, most wanted roadmap items), light and dark mode.
- `GET /digest.json?days=7`: the same snapshot as Telegram-ready messages, used by an n8n workflow that posts the
  weekly digest every Monday.
- `POST /ingest`: threads pushed by an external collector, see below.
- `GET /health`

From the shell: `community-radar report --days 7 -o radar.html`, `community-radar digest --days 7`.

## Triage queue

`GET /triage` is the community team's queue: every thread the radar sees, filterable by platform, status, product
and text, with its status, owner team and full history (who changed what, when, with which note).

| Status | Meaning |
|---|---|
| `new` | not triaged yet (default) |
| `in_progress` | someone is on it |
| `escalated` | handed to Support, Product, Engineering or Docs, waiting for their answer (team required) |
| `waiting_user` | we asked the user for details |
| `answered` | we replied, not confirmed solved |
| `resolved` | solved, nothing left to do |
| `dismissed` | duplicate, off-topic or spam (note required) |

The radar keeps watching handled threads: when the community writes again on a thread that is escalated, waiting on
the user, answered, resolved or dismissed, it comes back to the top flagged **new activity**. Each thread also shows
the radar's follow-up hint (fix merged but reporter not told, waiting on us…).

From an assistant: `triage_queue` lists the queue, `update_triage` records a decision ("escalate this to the product
team with this note"). Statuses live in the radar only: nothing is ever posted on the platforms. Status changes need
a JSON body and an `X-Radar-Triage` header, so another website open in the same browser cannot forge them.

## Importing other channels

Some channels are better collected outside the radar, by a tool you already run (an n8n workflow, a cron job), under
your own responsibility for each platform's terms. Push them to `POST /ingest` with
`Authorization: Bearer $RADAR_INGEST_TOKEN`:

```json
{"source": "stackoverflow", "channel": "Stack Overflow [ovh]", "items": [
  {"id": "79123456", "title": "…", "url": "https://stackoverflow.com/q/79123456", "created": "2026-11-05T08:00:00Z",
   "author": "…", "text": "…", "replies": 2, "score": 3, "labels": ["kubernetes"]}
]}
```

Threads are validated, deduplicated by id, classified like every other channel and kept 120 days. They appear in
every tool and in the report, marked as imported. `forum`, `github`, `roadmap` and `discord` are reserved, and an
imported `reddit` channel is hidden as soon as the official Reddit API is configured.

## Configuration

| Variable | Default | |
|---|---|---|
| `GITHUB_TOKEN` | – | classic token **without any scope** (public data, 5,000 requests/hour). Fine-grained tokens hide cross-references in issue timelines, so linked pull requests go unseen |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | – | Reddit "script" app for the official API; the Reddit channel is off without them |
| `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID` | – | bot token and server id; the Discord channel is off without them |
| `DISCORD_CHANNELS` | all readable | comma-separated channel names or ids to watch |
| `DISCORD_STAFF_ROLES` | `staff\|team\|ovhcloud\|admin\|modo\|moderat` | regular expression on role names that mark staff answers |
| `DISCORD_INCLUDE_WEBHOOKS` | `false` | count webhook posts as members (demo servers only: elsewhere webhooks are feeds) |
| `DISCORD_DEMO` | `false` | mark Discord threads as fictional demo data (badge and banner in the report, `demo` in tool results) |
| `DISCORD_PAGES` | `5` | pages read per channel (100 messages or 50 archived posts each) |
| `RADAR_INGEST_TOKEN` | – | enables `POST /ingest` for threads pushed by an external collector (bearer token) |
| `RADAR_INBOX_PATH` | in memory | JSON file that keeps imported threads (120 days) |
| `RADAR_TRIAGE_PATH` | in memory | SQLite file that keeps triage statuses and their history |
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

Code: MIT. The bundled guide index is derived from [ovh/docs](https://github.com/ovh/docs) and stays under
CC BY-NC-SA 4.0, see [NOTICE.md](NOTICE.md).
