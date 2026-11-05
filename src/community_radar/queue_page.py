"""The triage page: the community team's queue, with platform/status/product filters and status updates in place."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html import escape

from . import analytics, followup
from .models import Thread
from .products import label
from .report import CSS, SIGNAL_LABELS, SOURCE_NAMES, top_nav
from .sources import Collector
from .triage import OPEN, STATUSES, TEAMS, Queue, build_queue

QUEUE_CSS = """
.filters{display:flex;flex-direction:column;gap:10px;margin:20px 0 14px}
.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chips b{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;width:70px}
.chip{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:16px;padding:4px 11px;
font:inherit;font-size:13px;cursor:pointer}.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.chip small{opacity:.7;margin-left:4px}
.tools{display:flex;gap:8px;flex-wrap:wrap}.tools input,.tools select,.edit select,.edit input,.edit textarea{
font:inherit;font-size:14px;padding:6px 9px;border-radius:8px;border:1px solid var(--line);background:var(--card);
color:var(--ink)}.tools input[type=search]{flex:1;min-width:200px}
.st{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:10px;white-space:nowrap}
.st.new{background:var(--accent-soft);color:var(--accent)}.st.in_progress{background:var(--warn-soft);color:var(--warn)}
.st.escalated{background:#efe5fb;color:#6b3fb0}.st.waiting_user{background:var(--line);color:var(--ink)}
.st.answered,.st.resolved{background:var(--good-soft);color:var(--good)}.st.dismissed{background:var(--line);color:var(--muted)}
@media (prefers-color-scheme:dark){.st.escalated{background:#2d2142;color:#c9a8ff}}
.badge.activity{background:var(--bad-soft);color:var(--bad);font-weight:600}
.src{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
td.act{white-space:nowrap;text-align:right}button.btn{font:inherit;font-size:13px;padding:5px 10px;border-radius:8px;
border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
tr.panel td{background:var(--bg)}.edit{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
.edit textarea{grid-column:1/-1;min-height:60px}.edit .row{grid-column:1/-1;display:flex;gap:8px;align-items:center}
.hist{margin:10px 0 0;padding-left:18px;font-size:13px}.hist li{margin:3px 0}.err{color:var(--bad);font-size:13px}
.hint{font-size:12px;color:var(--warn)}.empty{padding:24px;text-align:center;color:var(--muted)}
.who{font-size:13px;color:var(--muted);margin-left:auto}.who input{width:110px;font:inherit;font-size:13px;padding:4px 8px;
border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
"""

SCRIPT = """
const rows=[...document.querySelectorAll('tr.item')];const state={source:'all',status:'open',product:'',q:''};
const OPEN=%(open)s;const LABELS=%(labels)s;const TEAMS=%(teams)s;
const who=document.getElementById('who');who.value=localStorage.getItem('radarActor')||'';
who.onchange=()=>localStorage.setItem('radarActor',who.value.trim());
function visible(r){const d=r.dataset;
 if(state.source!=='all'&&d.source!==state.source)return false;
 if(state.status==='open'){if(d.open!=='1')return false}
 else if(state.status==='activity'){if(d.activity!=='1')return false}
 else if(state.status!=='all'&&d.status!==state.status)return false;
 if(state.product&&!d.products.split(',').includes(state.product))return false;
 if(state.q&&!d.text.includes(state.q))return false;return true}
function apply(){let n=0;rows.forEach(r=>{const v=visible(r);r.hidden=!v;const p=r.nextElementSibling;
 if(!v&&p&&p.classList.contains('panel'))p.remove();n+=v});document.getElementById('none').hidden=n>0;
 document.getElementById('shown').textContent=n}
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{state[c.dataset.k]=c.dataset.v;
 document.querySelectorAll('.chip[data-k="'+c.dataset.k+'"]').forEach(x=>x.classList.toggle('on',x===c));apply()});
document.getElementById('product').onchange=e=>{state.product=e.target.value;apply()};
document.getElementById('q').oninput=e=>{state.q=e.target.value.toLowerCase().trim();apply()};
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function history(url,box){box.innerHTML='<li>Loading…</li>';
 const r=await fetch('/api/triage/history?url='+encodeURIComponent(url));const ev=await r.json();
 box.innerHTML=ev.length?ev.map(e=>'<li><b>'+esc(e.at.slice(0,16).replace('T',' '))+'</b> · '+esc(e.actor)+' · '
 +esc(LABELS[e.from_status])+' → <b>'+esc(LABELS[e.to_status])+'</b>'+(e.team?' ('+esc(TEAMS[e.team])+')':'')
 +(e.assignee?' · '+esc(e.assignee):'')+(e.note?'<br><span class=muted>'+esc(e.note)+'</span>':'')+'</li>').join('')
 :'<li class=muted>No change yet: the thread is new.</li>'}
function open(r){const nx=r.nextElementSibling;if(nx&&nx.classList.contains('panel')){nx.remove();return}
 const d=r.dataset;const p=document.createElement('tr');p.className='panel';
 p.innerHTML='<td colspan=5><div class=edit><select name=status>'+Object.entries(LABELS).map(([k,v])=>
 '<option value="'+k+'"'+(k===d.status?' selected':'')+'>'+v+'</option>').join('')+'</select>'
 +'<select name=team><option value="">Team…</option>'+Object.entries(TEAMS).map(([k,v])=>'<option value="'+k+'"'
 +(k===d.team?' selected':'')+'>'+v+'</option>').join('')+'</select><input name=assignee placeholder="Assignee" value="'
 +esc(d.assignee)+'"><textarea name=note placeholder="Note: what was done, link to the internal ticket, why it is dismissed…"></textarea>'
 +'<div class=row><button class="btn primary" data-a=save>Save</button><button class=btn data-a=close>Close</button>'
 +'<span class=err></span></div></div><ul class=hist></ul></td>';
 r.after(p);history(d.url,p.querySelector('.hist'));
 p.querySelector('[data-a=close]').onclick=()=>p.remove();
 p.querySelector('[data-a=save]').onclick=async()=>{const f=k=>p.querySelector('[name='+k+']').value;
  const body={url:d.url,status:f('status'),team:f('team')||null,assignee:f('assignee'),note:f('note'),
  actor:who.value.trim()||'unknown'};const err=p.querySelector('.err');err.textContent='';
  const res=await fetch('/api/triage',{method:'POST',headers:{'Content-Type':'application/json','X-Radar-Triage':'1'},
  body:JSON.stringify(body)});const out=await res.json();if(!res.ok){err.textContent=out.error;return}
  Object.assign(d,{status:out.status,team:out.team||'',assignee:out.assignee||'',activity:'0',open:OPEN.includes(out.status)?'1':'0'});
  r.querySelector('.cell-status').innerHTML='<span class="st '+out.status+'">'+esc(LABELS[out.status])+'</span>'
  +(out.team?' <span class=muted>→ '+esc(TEAMS[out.team])+'</span>':'')+(out.assignee?'<br><span class=muted>'
  +esc(out.assignee)+'</span>':'');p.querySelector('[name=note]').value='';history(d.url,p.querySelector('.hist'))}}
document.querySelectorAll('button[data-a=edit]').forEach(b=>b.onclick=()=>open(b.closest('tr')));apply();
"""


async def load_queue(collector: Collector, days: int, refresh: bool = False) -> Queue:
    """Threads active over `days` with their triage state and the radar's follow-up hint."""
    threads = analytics.window(await collector.threads(refresh), days)
    found = await followup.follow_ups(collector, threads, days, None, min_days=0)
    hints = {f.thread.url: (f.status, f"{f.label}. {f.action}") for f in found}
    return build_queue(threads, collector.triage, hints)


async def find_thread(collector: Collector, url: str) -> Thread | None:
    return next((t for t in await collector.threads() if t.url == url), None)


def _age(moment: datetime, now: datetime) -> str:
    hours = (now - moment).total_seconds() / 3600
    return f"{max(1, round(hours))} h" if hours < 48 else f"{round(hours / 24)} d"


def render_queue(queue: Queue, days: int, demo_server: str = "") -> str:
    now = datetime.now(UTC)
    items = queue.items
    sources = sorted(
        {i.thread.source for i in items}, key=lambda s: list(SOURCE_NAMES).index(s) if s in SOURCE_NAMES else 99
    )
    by_source = {s: sum(1 for i in items if i.thread.source == s) for s in sources}
    source_chips = (
        f'<button class="chip on" data-k=source data-v=all>All<small>{len(items)}</small></button>'
        + "".join(
            f'<button class=chip data-k=source data-v="{escape(s)}">{escape(SOURCE_NAMES.get(s, s.title()))}'
            f"<small>{n}</small></button>"
            for s, n in by_source.items()
        )
    )
    status_chips = (
        f'<button class="chip on" data-k=status data-v=open>To handle<small>{queue.to_handle}</small></button>'
        f"<button class=chip data-k=status data-v=activity>New activity<small>{queue.new_activity}</small></button>"
        + "".join(
            f'<button class=chip data-k=status data-v="{k}">{v[0]}<small>{queue.counts.get(k, 0)}</small></button>'
            for k, v in STATUSES.items()
        )
        + f"<button class=chip data-k=status data-v=all>All<small>{len(items)}</small></button>"
    )
    products = sorted({p for i in items for p in i.thread.products}, key=label)
    product_select = (
        '<select id=product><option value="">All products</option>'
        + "".join(f'<option value="{escape(p)}">{escape(label(p))}</option>' for p in products)
        + "</select>"
    )
    rows = []
    for i in items:
        t = i.thread
        badges = "".join(
            f'<span class="badge {s}">{SIGNAL_LABELS.get(s, s)}</span>' for s in t.signals if s != "question"
        )
        if t.demo:
            badges += ' <span class="badge demo">demo</span>'
        if i.new_activity:
            badges += ' <span class="badge activity">new activity</span>'
        status = f'<span class="st {i.status}">{escape(i.label)}</span>'
        if i.team:
            status += f" <span class=muted>→ {escape(TEAMS.get(i.team, i.team))}</span>"
        if i.assignee:
            status += f"<br><span class=muted>{escape(i.assignee)}</span>"
        hint = f"<div class=hint>Radar: {escape(i.hint)}</div>" if i.hint else ""
        text = f"{t.title} {t.excerpt} {t.author} {t.channel}".lower()
        rows.append(
            f'<tr class=item data-url="{escape(t.url)}" data-source="{escape(t.source)}" data-status="{i.status}" '
            f'data-team="{escape(i.team or "")}" data-assignee="{escape(i.assignee or "")}" '
            f'data-activity="{int(i.new_activity)}" data-open="{int(i.needs_action)}" data-products="{escape(",".join(t.products))}" '
            f'data-text="{escape(text)}">'
            f"<td><span class=src>{escape(SOURCE_NAMES.get(t.source, t.source))}</span> "
            f'<span class=muted>{escape(t.channel)}</span><br><a href="{escape(t.url)}" target=_blank rel=noopener>'
            f"{escape(t.title)}</a> {badges}{hint}</td>"
            f"<td>{escape(', '.join(label(p) for p in t.products[:2]) or '–')}</td>"
            f"<td>{_age(t.last_activity, now)}</td><td class=cell-status>{status}</td>"
            f"<td class=act><button class=btn data-a=edit>Update</button></td></tr>"
        )
    script = SCRIPT % {
        "open": json.dumps(list(OPEN)),
        "labels": json.dumps({k: v[0] for k, v in STATUSES.items()}),
        "teams": json.dumps(TEAMS),
    }
    demo = (
        f"<p class=demo-note>Discord threads come from the demo server « {escape(demo_server)} »: fictional "
        "conversations, marked <b>DEMO</b>.</p>"
        if demo_server and any(i.thread.demo for i in items)
        else ""
    )
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Community Radar · Triage</title>
<style>{CSS}{QUEUE_CSS}</style></head><body><main>
{top_nav("triage", days)}
<header><h1>Triage queue</h1><p>Threads active over the last {days} days · <span id=shown>{len(items)}</span> shown ·
statuses live in the radar, nothing is posted on the platforms</p>{demo}</header>
<div class=filters>
<div class=chips><b>Platform</b>{source_chips}</div>
<div class=chips><b>Status</b>{status_chips}</div>
<div class=tools>{product_select}<input id=q type=search placeholder="Search title, text, author…">
<span class=who>You: <input id=who placeholder="your name"></span></div>
</div>
<table><tr><th>Thread</th><th>Product</th><th>Last activity</th><th>Status</th><th></th></tr>
{"".join(rows)}</table><p id=none class=empty hidden>Nothing here: every thread in this view is handled.</p>
<script>{script}</script>
</main></body></html>"""
