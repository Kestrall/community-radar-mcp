"""The Sources page: add, disable or remove watched GitHub repositories, Discord servers, subreddits and feeds."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

from .config import LIMIT
from .report import CSS, top_nav
from .sources import Collector


async def source_status(collector: Collector) -> dict[str, Any]:
    """Threads seen and last error for each configured source, as shown next to it on the page."""
    threads = await collector.threads()
    counts: Counter[str] = Counter()
    for t in threads:
        if t.source == "discord":
            match = re.search(r"/channels/(\d+)/", t.url)
            counts[f"discord:{match.group(1) if match else ''}"] += 1
        elif t.source == "roadmap":
            counts["roadmap"] += 1
        else:
            counts[f"{t.source}:{t.channel}"] += 1
    errors = dict(collector.errors)
    if "roadmap-recent" in errors:
        errors["roadmap"] = errors.pop("roadmap-recent")
    names = {g: d.name for g, d in collector.discords.items()}
    return {
        "counts": dict(counts),
        "errors": {
            k if k.startswith(("discord:", "roadmap", "reddit")) else f"github:{k}": v for k, v in errors.items()
        },
        "discord_names": names,
        "discord_token": bool(os.getenv("DISCORD_BOT_TOKEN")),
        "reddit_api": collector.reddit_enabled,
    }


PAGE_CSS = """
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:14px;margin-top:20px}
.card h2{margin:0 0 2px;font-size:17px}.card p.help{margin:0 0 12px;color:var(--muted);font-size:13px}
.src-row{display:grid;grid-template-columns:auto 1fr auto auto;gap:8px;align-items:center;padding:7px 0;
border-top:1px solid var(--line)}.src-row.off .fields{opacity:.5}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:6px}
.fields input{font:inherit;font-size:13px;padding:5px 8px;border-radius:7px;border:1px solid var(--line);
background:var(--card);color:var(--ink);min-width:0}
.fields label{font-size:12px;color:var(--muted);display:flex;gap:4px;align-items:center}
.state{font-size:12px;color:var(--muted);white-space:nowrap}.state.bad{color:var(--bad)}.state.ok{color:var(--good)}
.x{border:0;background:none;color:var(--muted);font-size:18px;cursor:pointer;padding:0 4px}.x:hover{color:var(--bad)}
.add{margin-top:8px;border:1px dashed var(--line);background:none;color:var(--accent);border-radius:8px;
padding:6px 12px;font:inherit;font-size:13px;cursor:pointer;width:100%}.add:disabled{opacity:.4;cursor:default}
.bar{position:sticky;bottom:0;margin-top:18px;padding:12px 0;background:var(--bg);display:flex;gap:12px;align-items:center}
.bar button{font:inherit;padding:8px 16px;border-radius:8px;border:0;background:var(--accent);color:#fff;cursor:pointer}
.msg{font-size:14px}.msg.bad{color:var(--bad)}.msg.ok{color:var(--good)}
.roadmap input{font:inherit;font-size:14px;padding:6px 9px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--ink);width:100%}.warn{color:var(--warn);font-size:13px;margin:0 0 10px}
"""

SCRIPT = r"""
const CFG=%(config)s;const ST=%(status)s;const LIMIT=%(limit)d;
const KINDS={
 github:{title:'GitHub repositories',help:'Issues of these repositories are watched (bugs, questions). owner/repo',
  fields:[['repo','owner/repo','text']],key:i=>'github:'+i.repo,blank:{repo:'',enabled:true}},
 discord:{title:'Discord servers',help:'Servers the radar bot was invited to (View Channels + Read Message History). Same bot for every server.',
  fields:[['guild','server id','text'],['label','label (optional)','text'],['channels','channels (optional, comma-separated)','text'],['demo','demo data','checkbox']],
  key:i=>'discord:'+i.guild,blank:{guild:'',label:'',channels:'',demo:false,enabled:true}},
 reddit:{title:'Subreddits',help:'Read by your own collector (n8n) from each subreddit feed and pushed to /ingest, or by the official API when approved. Public Cloud posts only.',
  fields:[['subreddit','subreddit (without r/)','text']],key:i=>'reddit:r/'+i.subreddit,blank:{subreddit:'',enabled:true}},
 feeds:{title:'RSS / Atom feeds',help:'Any feed (blog, status page, other forum…), read by your collector and pushed to /ingest. Public Cloud items only.',
  fields:[['url','https://… feed URL','text'],['label','label','text']],key:i=>'rss:'+i.label,blank:{url:'',label:'',enabled:true}},
};
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function state(kind,item){const k=KINDS[kind].key(item);const err=ST.errors[k]||(kind==='reddit'&&ST.errors.reddit);
 if(!item.enabled)return '<span class=state>off</span>';
 if(err)return '<span class="state bad" title="'+esc(err)+'">error</span>';
 const n=ST.counts[k]||0;let extra='';if(kind==='discord'&&ST.discord_names[item.guild])extra=' · '+esc(ST.discord_names[item.guild]);
 return '<span class="state'+(n?' ok':'')+'">'+n+' threads'+extra+'</span>'}
function render(){
 document.getElementById('roadmap').value=CFG.roadmap;
 const box=document.getElementById('cards');box.innerHTML='';
 for(const [kind,def] of Object.entries(KINDS)){
  const card=document.createElement('div');card.className='card';
  let warn='';if(kind==='discord'&&!ST.discord_token)warn='<p class=warn>DISCORD_BOT_TOKEN is not set on the server: Discord stays off.</p>';
  if(kind==='reddit'&&!ST.reddit_api)warn='<p class=warn>No Reddit API credentials: subreddits come from your collector through /ingest.</p>';
  card.innerHTML='<h2>'+def.title+' <span class=muted style="font-size:13px">'+CFG[kind].length+'/'+LIMIT+'</span></h2><p class=help>'+def.help+'</p>'+warn;
  CFG[kind].forEach((item,i)=>{const row=document.createElement('div');row.className='src-row'+(item.enabled?'':' off');
   row.innerHTML='<input type=checkbox title="enabled"'+(item.enabled?' checked':'')+' data-f=enabled><div class=fields>'
   +def.fields.map(([f,ph,t])=>t==='checkbox'?'<label><input type=checkbox data-f="'+f+'"'+(item[f]?' checked':'')+'> '+ph+'</label>'
   :'<input data-f="'+f+'" placeholder="'+esc(ph)+'" value="'+esc(item[f])+'">').join('')+'</div>'+state(kind,item)
   +'<button class=x title="remove" data-a=rm>×</button>';
   row.querySelectorAll('[data-f]').forEach(el=>el.oninput=el.onchange=()=>{item[el.dataset.f]=el.type==='checkbox'?el.checked:el.value;
    if(el.dataset.f==='enabled')row.classList.toggle('off',!el.checked);dirty()});
   row.querySelector('[data-a=rm]').onclick=()=>{CFG[kind].splice(i,1);dirty();render()};card.appendChild(row)});
  const add=document.createElement('button');add.className='add';add.textContent='+ Add';add.disabled=CFG[kind].length>=LIMIT;
  add.onclick=()=>{CFG[kind].push({...def.blank});dirty();render();
   const cards=document.querySelectorAll('#cards .card');const last=[...cards[Object.keys(KINDS).indexOf(kind)].querySelectorAll('.src-row')].pop();
   if(last)last.querySelector('.fields input').focus()};
  card.appendChild(add);box.appendChild(card)}}
const msg=document.getElementById('msg');function dirty(){msg.className='msg';msg.textContent='Unsaved changes'}
document.getElementById('roadmap').oninput=e=>{CFG.roadmap=e.target.value;dirty()};
document.getElementById('save').onclick=async()=>{msg.className='msg';msg.textContent='Saving…';
 const res=await fetch('/api/sources',{method:'POST',headers:{'Content-Type':'application/json','X-Radar-Triage':'1'},body:JSON.stringify(CFG)});
 const out=await res.json();if(!res.ok){msg.className='msg bad';msg.textContent=out.error;return}
 msg.className='msg ok';msg.textContent='Saved. Reloading the sources…';setTimeout(()=>location.reload(),600)};
render();
"""


def render_sources(config: dict[str, Any], status: dict[str, Any], days: int = 7) -> str:
    script = SCRIPT % {"config": json.dumps(config), "status": json.dumps(status), "limit": LIMIT}
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Community Radar · Sources</title>
<style>{CSS}{PAGE_CSS}</style></head><body><main>
{top_nav("sources", days)}
<header><h1>Sources</h1><p>What the radar watches. Changes apply right away; tokens stay in the server environment,
never on this page.</p></header>
<div class="card roadmap" style="margin-top:20px"><h2>Public roadmap</h2>
<p class=help>GitHub repository whose issues are the product roadmap (requests and votes).</p>
<input id=roadmap placeholder="owner/repo"></div>
<div id=cards class=cards></div>
<div class=bar><button id=save>Save sources</button><span id=msg class=msg></span></div>
<script>{script}</script>
</main></body></html>"""
