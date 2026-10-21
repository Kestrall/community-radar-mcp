"""Weekly community report: one data snapshot, rendered as a standalone HTML page or a Telegram message."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from . import analytics
from .docs import DocsIndex
from .models import Thread
from .products import label
from .sources import Collector

SIGNAL_LABELS = {
    "incident": "incident",
    "bug": "bug",
    "billing": "billing",
    "security": "security",
    "feature_request": "feature request",
    "question": "question",
}


@dataclass
class Snapshot:
    days: int
    generated: datetime
    pulse: analytics.Pulse
    irritants: list[analytics.Irritant]
    unanswered: list[Thread]
    gaps: list[analytics.Gap]
    top_requests: list[Thread]
    errors: dict[str, str]


async def snapshot(collector: Collector, docs: DocsIndex, days: int = 7) -> Snapshot:
    threads = await collector.threads()
    requests = [t for t in analytics.window(threads, days) if t.source == "roadmap" and not t.solved]
    return Snapshot(
        days=days,
        generated=datetime.now(UTC),
        pulse=await analytics.pulse(collector, threads, days, None),
        irritants=analytics.irritants(threads, days, 5),
        unanswered=analytics.unanswered(threads, max(days, 30), None)[:8],
        gaps=analytics.content_gaps(threads, docs, max(days, 30), 6),
        top_requests=sorted(requests, key=lambda t: -t.reactions)[:6],
        errors=dict(collector.errors),
    )


def _trend(current: int, previous: int) -> str:
    if not previous:
        return "new" if current else "–"
    delta = round((current - previous) / previous * 100)
    return f"{'+' if delta >= 0 else ''}{delta}% vs previous period"


def _delay(hours: float | None) -> str:
    if hours is None:
        return "–"
    return f"{max(1, round(hours * 60))} min" if hours < 1 else f"{round(hours, 1)} h"


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{round(value * 100)}%"


SOURCE_NAMES = {"forum": "Forum", "github": "GitHub issues", "roadmap": "Roadmap", "reddit": "Reddit"}


# --- HTML -------------------------------------------------------------------------------------

CSS = """
:root{--bg:#f6f7fb;--card:#fff;--ink:#14182b;--muted:#5b6275;--line:#e3e6ef;--accent:#2357d9;--accent-soft:#e7eefc;
--bad:#c23b32;--bad-soft:#fbe9e7;--warn:#a86400;--warn-soft:#fdf1dd;--good:#1d7a4b;--good-soft:#e3f4ea}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f1220;--card:#171b2d;--ink:#e8eaf3;
--muted:#9aa1b8;--line:#2a3048;--accent:#7ea2ff;--accent-soft:#1f2a4d;--bad:#ff8a80;--bad-soft:#3a1d1d;--warn:#ffc266;
--warn-soft:#3a2d16;--good:#7bd8a5;--good-soft:#173326}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 16px 64px}
header h1{font-size:28px;margin:0 0 4px;letter-spacing:-.02em}header p{margin:0;color:var(--muted)}
h2{font-size:18px;margin:40px 0 12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:24px}
.kpi,.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.kpi b{display:block;font-size:28px;letter-spacing:-.02em}.kpi span{color:var(--muted);font-size:13px}
.kpi small{display:block;color:var(--muted);font-size:12px;margin-top:2px}
.bars div{display:grid;grid-template-columns:180px 1fr 40px;gap:8px;align-items:center;margin:6px 0;font-size:14px}
.bars i{display:block;height:10px;border-radius:5px;background:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.card h3{margin:0 0 4px;font-size:16px}.card .meta{color:var(--muted);font-size:13px;margin-bottom:8px}
.card ul{margin:0;padding-left:18px}.card li{margin:4px 0}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;margin-left:4px;
background:var(--accent-soft);color:var(--accent);white-space:nowrap}
.badge.incident,.badge.security{background:var(--bad-soft);color:var(--bad)}
.badge.bug,.badge.billing{background:var(--warn-soft);color:var(--warn)}
.badge.ok{background:var(--good-soft);color:var(--good)}
.score{float:right;font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;
overflow:hidden}td,th{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);font-size:14px;
vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}.muted{color:var(--muted)}
.note{margin-top:32px;color:var(--muted);font-size:13px}
@media (max-width:640px){.bars div{grid-template-columns:110px 1fr 32px}td,th{padding:8px}}
"""


def _badges(t: Thread) -> str:
    hidden = {"question", "feature_request"} if t.source == "roadmap" else {"question"}
    return "".join(f'<span class="badge {s}">{SIGNAL_LABELS.get(s, s)}</span>' for s in t.signals if s not in hidden)


def _link(t: Thread) -> str:
    return f'<a href="{escape(t.url)}">{escape(t.title)}</a>'


def render_html(s: Snapshot) -> str:
    p = s.pulse
    kpis = [
        (p.total, "active threads", f"over {s.days} days"),
        (p.new_threads, "new threads", _trend(p.new_threads, p.previous_new_threads)),
        (p.unanswered, "without any reply", "forum & GitHub"),
        (_pct(p.answered_rate), "answered", "at least one reply"),
        (
            _delay(p.median_first_response_hours),
            "median first response",
            f"forum, {p.first_response_sample} topics opened in the period",
        ),
        (_pct(p.solved_rate), "solved / closed", "forum & GitHub"),
    ]
    kpi_html = "".join(
        f"<div class=kpi><b>{v}</b><span>{escape(n)}</span><small>{escape(h)}</small></div>" for v, n, h in kpis
    )
    top = max(p.by_product.values(), default=1)
    bars = "".join(
        f"<div><span>{escape(name)}</span><i style='width:{max(4, round(n / top * 100))}%'></i><b>{n}</b></div>"
        for name, n in list(p.by_product.items())[:10]
    )
    sources = ", ".join(f"{SOURCE_NAMES.get(k, k)} {v}" for k, v in p.by_source.items())
    irritants = "".join(
        f"<div class=card><span class=score>score {i.score}</span><h3>{escape(i.label)}</h3>"
        f"<div class=meta>{i.threads} threads, {i.new_threads} new ({_trend(i.new_threads, i.previous_threads)}), "
        f"{i.negative_threads} negative</div><ul>"
        + "".join(
            f"<li>{_link(t)}{_badges(t)} <span class=muted>· {SOURCE_NAMES.get(t.source, t.source)}</span></li>"
            for t in i.examples
        )
        + "</ul></div>"
        for i in s.irritants
    )
    now = s.generated
    unanswered = (
        "".join(
            f"<tr><td>{_link(t)}{_badges(t)}</td><td>{SOURCE_NAMES.get(t.source, t.source)}</td>"
            f"<td>{(now - t.created).days} d</td><td>{', '.join(label(x) for x in t.products[:2]) or '–'}</td></tr>"
            for t in s.unanswered
        )
        or "<tr><td colspan=4 class=muted>Every recent thread got at least one reply.</td></tr>"
    )
    gaps = (
        "".join(
            f"<tr><td>{_link(g.thread)}</td><td>"
            + (
                f'<a href="{escape(g.best_guide.url())}">{escape(g.best_guide.title)}</a> <span class=muted>({g.best_score})</span>'
                if g.best_guide
                else "<span class=muted>no guide</span>"
            )
            + "</td></tr>"
            for g in s.gaps
        )
        or "<tr><td colspan=2 class=muted>No obvious gap: recent questions all match a guide.</td></tr>"
    )
    requests = (
        "".join(
            f"<tr><td>{_link(t)}</td><td>{t.reactions} 👍</td><td>{t.replies}</td>"
            f"<td>{', '.join(t.labels[:3]) or '–'}</td></tr>"
            for t in s.top_requests
        )
        or "<tr><td colspan=4 class=muted>No roadmap activity in this period.</td></tr>"
    )
    errors = f"<p class=note>Unavailable during this run: {escape(', '.join(s.errors))}.</p>" if s.errors else ""
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Community Radar</title>
<style>{CSS}</style></head><body><main>
<header><h1>Public Cloud Community Radar</h1>
<p>Last {s.days} days · generated {now:%Y-%m-%d %H:%M} UTC · {escape(sources)}</p></header>
<section class=kpis>{kpi_html}</section>
<h2>Where the conversation is</h2><div class="card bars">{bars or "<span class=muted>No activity.</span>"}</div>
<h2>Top irritants to bring to product teams</h2><div class=grid>{irritants}</div>
<h2>Waiting for an answer</h2><table><tr><th>Thread</th><th>Channel</th><th>Age</th><th>Product</th></tr>{unanswered}</table>
<h2>Content gaps: tutorial ideas</h2>
<table><tr><th>Community question</th><th>Closest official guide (match score)</th></tr>{gaps}</table>
<h2>Most wanted on the roadmap</h2>
<table><tr><th>Request</th><th>Votes</th><th>Comments</th><th>Labels</th></tr>{requests}</table>
<p class=note>Sources: community.ovhcloud.com (Public Cloud), github.com/ovh/public-cloud-roadmap,
ovh/terraform-provider-ovh, ovh/ovhcloud-cli, Reddit r/ovh, github.com/ovh/docs. Independent project built on public
data, not affiliated with OVHcloud.</p>{errors}
</main></body></html>"""


# --- Telegram ---------------------------------------------------------------------------------


def render_telegram(s: Snapshot) -> list[str]:
    """Telegram HTML messages (≤ 4096 characters each)."""
    p = s.pulse
    e = escape
    head = (
        f"📡 <b>Public Cloud Community Radar</b> · last {s.days} days\n\n"
        f"• {p.total} active threads, {p.new_threads} new ({_trend(p.new_threads, p.previous_new_threads)})\n"
        f"• {p.unanswered} without reply · answered {_pct(p.answered_rate)} · solved {_pct(p.solved_rate)}\n"
        + (
            f"• Forum median first response: {_delay(p.median_first_response_hours)} "
            f"({p.first_response_sample} new topics)\n"
            if p.median_first_response_hours is not None
            else ""
        )
    )
    irritants = "\n".join(
        f"\n🔥 <b>{e(i.label)}</b>: {i.threads} threads, {i.negative_threads} negative\n"
        + "\n".join(f'   ▸ <a href="{e(t.url)}">{e(t.title[:90])}</a>' for t in i.examples[:3])
        for i in s.irritants[:3]
    )
    waiting = "\n".join(f'▸ <a href="{e(t.url)}">{e(t.title[:90])}</a>' for t in s.unanswered[:5])
    gaps = "\n".join(f'▸ <a href="{e(g.thread.url)}">{e(g.thread.title[:90])}</a>' for g in s.gaps[:3])
    parts = [head, "<b>Top irritants</b>" + irritants]
    if waiting:
        parts.append(f"⏳ <b>Waiting for an answer</b>\n{waiting}")
    if gaps:
        parts.append(f"📝 <b>Tutorial ideas</b>\n{gaps}")
    if s.errors:
        parts.append(f"⚠️ Unavailable: {e(', '.join(s.errors))}")
    messages, current = [], ""
    for part in parts:
        if current and len(current) + len(part) > 3900:
            messages.append(current)
            current = ""
        current = f"{current}\n\n{part}" if current else part
    return messages + [current] if current else messages
