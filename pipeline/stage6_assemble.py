"""Stage 6 — assemble + render.

data/05-jobs-scored.jsonl + data/01-connections-scored.jsonl
    -> data/06-final.json
    -> report.html   (single self-contained file, JSON embedded, no server/CDN)

Final ranking: company_connection_score x role_score, with location tier as
a strong term. Exact combination and weights are in config.py and echoed into the JSON.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

import config
from pipeline.common import DATA, ROOT, log, read_jsonl, write_json

_ML_TITLE_RE = [re.compile(p, re.I) for p in config.ML_TITLE_PATTERNS]


def connection_factor(company_score: float) -> float:
    f = config.CONNECTION_FACTOR_FLOOR
    return round(f + (1.0 - f) * max(0.0, min(1.0, company_score / 100.0)), 4)


def final_score(company_score: float, role_score: float, tier: str) -> float:
    cf = connection_factor(company_score)
    rf = max(0.0, min(1.0, role_score / 10.0))
    lm = config.LOCATION_MULTIPLIER.get(tier, 0.5)
    return round(cf * rf * lm * config.FINAL_SCORE_SCALE, 2)


def ml_deboost(title: str) -> tuple[bool, str | None]:
    """Deterministic, title-only de-boost for config.ML_TITLE_PATTERNS (off when empty):
    roles you are not credible for regardless of domain fit. De-boost, never drop."""
    for rx in _ML_TITLE_RE:
        m = rx.search(title or "")
        if m:
            return True, f"title matches ML pattern: {m.group(0)!r}"
    return False, None


def load_outreach() -> dict[str, dict]:
    """Read data/outreach.db READ-ONLY. Returns
    {company_normalized: {"status": str, "role_urls": set[str]}}.

    A missing DB is a normal state, not an error — return {} and stage 6 behaves
    exactly as it did before outreach tracking existed. A malformed DB is logged
    and likewise treated as no data; it must never break the pipeline.
    """
    db = DATA / "outreach.db"
    if not db.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        role_urls: dict[int, set[str]] = {}
        for rr in con.execute("SELECT outreach_id, role_url FROM outreach_role"):
            role_urls.setdefault(rr["outreach_id"], set()).add(rr["role_url"])
        for r in con.execute("SELECT id, company_normalized, status FROM outreach"):
            out[r["company_normalized"]] = {
                "status": r["status"],
                "role_urls": role_urls.get(r["id"], set()),
            }
        con.close()
    except Exception as e:  # noqa: BLE001 — no outreach data is valid; a bad DB must not abort
        log(f"Stage 6: data/outreach.db present but unreadable ({e}); proceeding without it")
        return {}
    log(f"Stage 6: outreach.db -> {len(out)} companies with outreach history")
    return out


def apply_outreach(fs: float, company_norm: str, role_url: str,
                   outreach: dict[str, dict]) -> tuple[float, str | None]:
    """Outreach-aware ranking adjustment. Returns (adjusted_score, outreach_status_for_badge)."""
    o = outreach.get(company_norm)
    if not o:
        return fs, None
    status = o["status"]
    if status == "dead":
        return round(fs * config.OUTREACH_DEAD_MULT, 2), status
    if status in ("contacted", "applied", "active"):
        if role_url in o["role_urls"]:
            return fs, status  # this role IS the outreach — no de-boost, just badge it
        return round(fs * config.OUTREACH_TOUCHED_MULT, 2), status
    return fs, status


def main() -> None:
    roles = read_jsonl(DATA / "05-jobs-scored.jsonl")
    conns = read_jsonl(DATA / "01-connections-scored.jsonl")
    recruiters = read_jsonl(DATA / "01-recruiters.jsonl")
    if not roles:
        raise SystemExit("Stage 6: data/05-jobs-scored.jsonl missing/empty. Run stage 5.")

    conns_by_company: dict[str, list[dict]] = {}
    for c in conns:
        conns_by_company.setdefault(c["normalized_company"], []).append(c)

    def conn_view(c: dict) -> dict:
        return {
            "name": c["name"],
            "profile_url": c["profile_url"],
            "position": c["raw_position"],
            "connection_score": c["connection_score"],
            "why_ranked": c.get("why_ranked", ""),
            "era": c.get("era", "none"),
            "seniority_bucket": c.get("seniority_bucket", ""),
            "function_bucket": c.get("function_bucket", ""),
            "message_count": c.get("message_count", 0),
            "last_message_date": c.get("last_message_date"),
        }

    outreach = load_outreach()

    out_roles = []
    for r in roles:
        company_conns = sorted(
            conns_by_company.get(r["company_normalized"], []),
            key=lambda c: c["connection_score"], reverse=True,
        )
        cviews = [conn_view(c) for c in company_conns]
        best = cviews[0] if cviews else None
        cscore = r.get("company_connection_score")
        if cscore is None:
            cscore = company_conns[0]["company_connection_score"] if company_conns else 0.0
        pre_deboost = final_score(cscore or 0.0, r["role_score"], r["location_tier"])
        ml_applied, ml_reason = ml_deboost(r["title"])
        fs = round(pre_deboost * config.ML_DEBOOST_MULT, 2) if ml_applied else pre_deboost
        post_ml = fs
        fs, outreach_status = apply_outreach(fs, r["company_normalized"], r["url"], outreach)
        outreach_deboosted = fs < post_ml
        sc = r["score"]
        out_roles.append({
            "final_score": fs,
            "pre_deboost_score": pre_deboost,
            "ml_deboost_applied": ml_applied,
            "ml_deboost_reason": ml_reason,
            "outreach_status": outreach_status,
            "outreach_deboosted": outreach_deboosted,
            "tab": "leadership" if r["title_bucket"] == "leadership" else "ic",
            "company": r["company"],
            "company_normalized": r["company_normalized"],
            "title": r["title"],
            "url": r["url"],
            "ats": r["ats"],
            "location_tier": r["location_tier"],
            "location_raw": r["location_raw"],
            "location_reason": r["location_reason"],
            "department": r.get("department", ""),
            "employment_type": r.get("employment_type", ""),
            "compensation_raw": r.get("compensation_raw", ""),
            "comp_min": sc.get("comp_min"),
            "comp_max": sc.get("comp_max"),
            "comp_penalty_flag": r.get("comp_penalty_flag", False),
            "industry_flag": r.get("industry_flag"),
            "role_score": r["role_score"],
            "role_composite": r["role_composite"],
            "overall_score_llm": sc.get("overall_score"),
            "domain_match": sc.get("domain_match"),
            "scope_match": sc.get("scope_match"),
            "industry_fit": sc.get("industry_fit"),
            "comp_signal": sc.get("comp_signal"),
            "seniority_read": sc.get("seniority_read", ""),
            "manages_managers": sc.get("manages_managers"),
            "rationale": sc.get("rationale", ""),
            "concerns": sc.get("concerns", ""),
            "company_connection_score": round(cscore or 0.0, 2),
            "connection_count": len(cviews),
            "best_connection": best,
            "connections": cviews,
            "posted_at": r.get("posted_at", ""),
            "description_excerpt": (r.get("description_text") or "")[:1200],
        })

    out_roles.sort(key=lambda x: x["final_score"], reverse=True)

    rec_view = []
    for r in recruiters:
        rec_view.append({
            "rank": r.get("recruiter_rank"),
            "recruiter_score": r.get("recruiter_score"),
            "name": r["name"],
            "profile_url": r["profile_url"],
            "position": r["raw_position"],
            "company": r["raw_company"],
            "message_count": r.get("message_count", 0),
            "first_message_date": r.get("first_message_date"),
            "last_message_date": r.get("last_message_date"),
            "era": r.get("era", "none"),
            "seniority_bucket": r.get("seniority_bucket", ""),
            "connection_score": r.get("connection_score"),
            "why_ranked": r.get("why_ranked", ""),
        })

    stats = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "roles_total": len(out_roles),
        "leadership": sum(1 for r in out_roles if r["tab"] == "leadership"),
        "ic": sum(1 for r in out_roles if r["tab"] == "ic"),
        "tier_1": sum(1 for r in out_roles if r["location_tier"] == "tier_1"),
        "tier_2": sum(1 for r in out_roles if r["location_tier"] == "tier_2"),
        "recruiters_total": len(rec_view),
        "industry_flagged": sum(1 for r in out_roles if r["industry_flag"]),
        "ml_deboosted": sum(1 for r in out_roles if r["ml_deboost_applied"]),
        "outreach_deboosted": sum(1 for r in out_roles if r["outreach_deboosted"]),
    }

    final = {
        "meta": {
            "generated_at": stats["generated_at"],
            "ranking_formula": "final = connection_factor(company_connection_score) "
                               "* (role_score/10) * location_multiplier * 100",
            "config": {
                "CONNECTION_WEIGHTS": config.CONNECTION_WEIGHTS,
                "ROLE_WEIGHTS": config.ROLE_WEIGHTS,
                "ROLE_USE_LLM_OVERALL": config.ROLE_USE_LLM_OVERALL,
                "CONNECTION_FACTOR_FLOOR": config.CONNECTION_FACTOR_FLOOR,
                "LOCATION_MULTIPLIER": config.LOCATION_MULTIPLIER,
                "RECRUITER_WEIGHTS": config.RECRUITER_WEIGHTS,
                "ML_TITLE_PATTERNS": config.ML_TITLE_PATTERNS,
                "ML_DEBOOST_MULT": config.ML_DEBOOST_MULT,
                "OUTREACH_DEAD_MULT": config.OUTREACH_DEAD_MULT,
                "OUTREACH_TOUCHED_MULT": config.OUTREACH_TOUCHED_MULT,
            },
        },
        "stats": stats,
        "roles": out_roles,
        "recruiters": rec_view,
    }
    write_json(DATA / "06-final.json", final)
    log(f"Stage 6: wrote data/06-final.json (roles={stats['roles_total']} "
        f"leadership={stats['leadership']} ic={stats['ic']} recruiters={stats['recruiters_total']})")

    render_report(final, ROOT / "report.html")
    log("Stage 6: wrote report.html")


def render_report(final: dict, path: Path) -> None:
    payload = json.dumps(final, ensure_ascii=False, default=str)
    html = _REPORT_TEMPLATE.replace("/*__DATA__*/", payload)
    path.write_text(html, encoding="utf-8")


_REPORT_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connections Finder</title>
<style>
  :root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--fg:#e6e8ec;--mut:#9aa3b2;
        --accent:#6ea8fe;--good:#4ade80;--warn:#fbbf24;--bad:#f87171;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{padding:16px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:10}
  h1{font-size:16px;margin:0 0 4px}
  .sub{color:var(--mut);font-size:12px}
  .tabs{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
  .tab{padding:6px 12px;border:1px solid var(--line);border-radius:6px;cursor:pointer;background:var(--card);color:var(--fg)}
  .tab.active{border-color:var(--accent);color:var(--accent)}
  .controls{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center}
  input[type=search]{flex:1;min-width:200px;padding:7px 10px;background:var(--card);border:1px solid var(--line);border-radius:6px;color:var(--fg)}
  .chip{padding:5px 10px;border:1px solid var(--line);border-radius:999px;cursor:pointer;background:var(--card);color:var(--mut);font-size:12px}
  .chip.on{border-color:var(--accent);color:var(--accent)}
  select{padding:6px 8px;background:var(--card);border:1px solid var(--line);border-radius:6px;color:var(--fg)}
  main{padding:12px 20px 60px}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--mut);font-weight:600;font-size:12px;cursor:pointer;position:sticky;top:0;background:var(--bg)}
  tr.role{cursor:pointer}
  tr.role:hover{background:#161a21}
  .score{font-weight:700}
  .s-hi{color:var(--good)} .s-md{color:var(--warn)} .s-lo{color:var(--mut)}
  .pill{display:inline-block;padding:1px 7px;border-radius:999px;border:1px solid var(--line);font-size:11px;color:var(--mut)}
  .pill.t1{border-color:var(--good);color:var(--good)}
  .pill.t2{border-color:var(--warn);color:var(--warn)}
  .pill.flag{border-color:var(--bad);color:var(--bad)}
  a{color:var(--accent)}
  .detail{background:#12151b}
  .detail td{padding:14px 18px}
  .detail h4{margin:10px 0 4px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
  .conns{width:100%;margin-top:4px}
  .conns td{border-bottom:1px dashed var(--line);padding:5px 8px}
  .muted{color:var(--mut)}
  .contacted{opacity:.4}
  .kv{display:grid;grid-template-columns:130px 1fr;gap:2px 10px;font-size:13px}
  .btn{padding:3px 8px;border:1px solid var(--line);border-radius:5px;background:var(--card);color:var(--fg);cursor:pointer;font-size:11px}
  .count{color:var(--mut);font-size:12px;margin-left:6px}
  code{background:#00000030;padding:1px 4px;border-radius:4px}
</style>
</head>
<body>
<header>
  <h1>Connections Finder <span class="count" id="genmeta"></span></h1>
  <div class="sub" id="statline"></div>
  <div class="tabs" id="tabs">
    <div class="tab active" data-tab="leadership">Leadership roles</div>
    <div class="tab" data-tab="ic">IC roles</div>
    <div class="tab" data-tab="recruiters">Recruiters</div>
  </div>
  <div class="controls" id="controls">
    <input type="search" id="q" placeholder="search company, title, connection, rationale…">
    <span class="chip" data-f="tier_1">Remote (US)</span>
    <span class="chip" data-f="tier_2">Onsite metro</span>
    <span class="chip" data-f="hasband">Has comp band</span>
    <span class="chip" data-f="flagged">Industry flag</span>
    <span class="chip" data-f="hidecontacted">Hide contacted</span>
    <label class="muted">min score <select id="minscore">
      <option value="0">0</option><option value="10">10</option><option value="20">20</option>
      <option value="30">30</option><option value="40">40</option><option value="50">50</option>
      <option value="60">60</option></select></label>
  </div>
</header>
<main>
  <table id="grid"><thead id="head"></thead><tbody id="body"></tbody></table>
</main>
<script id="data" type="application/json">/*__DATA__*/</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const LS = 'cf_contacted_v1';
let contacted = {};
try { contacted = JSON.parse(localStorage.getItem(LS) || '{}'); } catch(e){}
let tab = 'leadership';
const filters = new Set();
const q = document.getElementById('q');
const minscore = document.getElementById('minscore');

document.getElementById('genmeta').textContent = '· generated ' + (DATA.meta.generated_at||'');
const s = DATA.stats;
document.getElementById('statline').textContent =
  `${s.roles_total} roles (${s.leadership} leadership · ${s.ic} IC) · ` +
  `${s.tier_1} remote-US · ${s.tier_2} onsite metro · ${s.recruiters_total} recruiters · ` +
  `${s.industry_flagged} industry-flagged`;

document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active'); tab = t.dataset.tab; render();
});
document.querySelectorAll('.chip').forEach(c => c.onclick = () => {
  c.classList.toggle('on');
  filters.has(c.dataset.f) ? filters.delete(c.dataset.f) : filters.add(c.dataset.f);
  render();
});
q.oninput = render; minscore.onchange = render;

let sortKey = 'final_score', sortDir = -1;
function setSort(k){ if(sortKey===k) sortDir*=-1; else {sortKey=k; sortDir=-1;} render(); }

function scoreClass(v){ return v>=50?'s-hi':v>=30?'s-md':'s-lo'; }
function money(n){ return n==null?'':'$'+Math.round(n/1000)+'k'; }
function band(r){
  if(r.comp_min||r.comp_max) return money(r.comp_min)+'–'+money(r.comp_max);
  if(r.compensation_raw) return String(r.compensation_raw).slice(0,24);
  return '<span class="muted">—</span>';
}
function key(r){ return r.tab+'|'+r.company+'|'+r.title+'|'+r.url; }

function roleRows(){
  let rows = DATA.roles.filter(r => r.tab === tab);
  const term = q.value.toLowerCase().trim();
  if(term) rows = rows.filter(r => (
    r.company+' '+r.title+' '+(r.rationale||'')+' '+(r.concerns||'')+' '+
    (r.connections||[]).map(c=>c.name+' '+c.position).join(' ')
  ).toLowerCase().includes(term));
  if(filters.has('tier_1')) rows = rows.filter(r => r.location_tier==='tier_1');
  if(filters.has('tier_2')) rows = rows.filter(r => r.location_tier==='tier_2');
  if(filters.has('hasband')) rows = rows.filter(r => r.comp_min||r.comp_max);
  if(filters.has('flagged')) rows = rows.filter(r => r.industry_flag);
  if(filters.has('hidecontacted')) rows = rows.filter(r => !contacted[key(r)]);
  const mn = +minscore.value; if(mn) rows = rows.filter(r => r.final_score >= mn);
  rows.sort((a,b)=>{
    let x=a[sortKey], y=b[sortKey];
    if(typeof x==='string'){x=x||'';y=y||'';return x<y?-sortDir:x>y?sortDir:0;}
    return ((x||0)-(y||0))*sortDir;
  });
  return rows;
}

function render(){
  const head = document.getElementById('head');
  const body = document.getElementById('body');
  if(tab==='recruiters'){ renderRecruiters(head, body); return; }
  head.innerHTML = '<tr>' +
    ['final_score:Score','company:Company','title:Title','location_tier:Loc',
     'band:Comp','connection_count:# conn','best:Best connection','url:Link']
    .map(c=>{const [k,l]=c.split(':');return `<th data-k="${k}">${l}</th>`}).join('') + '</tr>';
  head.querySelectorAll('th').forEach(th=>th.onclick=()=>setSort(th.dataset.k));
  const rows = roleRows();
  body.innerHTML = '';
  rows.forEach((r,i)=>{
    const tr = document.createElement('tr');
    tr.className = 'role' + (contacted[key(r)] ? ' contacted':'');
    const bc = r.best_connection;
    tr.innerHTML =
      `<td class="score ${scoreClass(r.final_score)}">${r.final_score.toFixed(1)}</td>`+
      `<td>${esc(r.company)}</td>`+
      `<td>${esc(r.title)}`+
        (r.industry_flag?` <span class="pill flag">${esc(r.industry_flag)}</span>`:'')+
        (r.comp_penalty_flag?` <span class="pill flag">low band</span>`:'')+`</td>`+
      `<td><span class="pill ${r.location_tier==='tier_1'?'t1':'t2'}">`+
        `${r.location_tier==='tier_1'?'Remote US':'Onsite metro'}</span></td>`+
      `<td>${band(r)}</td>`+
      `<td>${r.connection_count}</td>`+
      `<td>${bc?esc(bc.name)+' <span class="muted">— '+esc((bc.position||'').slice(0,42))+'</span>':'<span class="muted">none</span>'}</td>`+
      `<td><a href="${esc(r.url)}" target="_blank" rel="noopener">open ↗</a></td>`;
    const det = document.createElement('tr');
    det.className='detail'; det.style.display='none';
    det.innerHTML = `<td colspan="8">${detailHtml(r)}</td>`;
    tr.onclick = e => { if(e.target.tagName==='A') return;
      det.style.display = det.style.display==='none'?'':'none'; };
    body.appendChild(tr); body.appendChild(det);
  });
  if(!rows.length) body.innerHTML = '<tr><td colspan="8" class="muted">no rows match</td></tr>';
}

function detailHtml(r){
  const conns = (r.connections||[]).map(c =>
    `<tr><td>${c.connection_score.toFixed(1)}</td>`+
    `<td><a href="${esc(c.profile_url)}" target="_blank" rel="noopener">${esc(c.name)}</a></td>`+
    `<td>${esc(c.position||'')}</td>`+
    `<td class="muted">${esc(c.why_ranked||'')}</td>`+
    `<td class="muted">${c.message_count||0} DMs${c.last_message_date?' · '+c.last_message_date:''}</td></tr>`
  ).join('');
  return `<div class="kv">
    <div>Final score</div><div>${r.final_score.toFixed(2)} &nbsp;<span class="muted">= conn_factor(${r.company_connection_score}) × role ${r.role_score} × loc ${r.location_tier}</span></div>
    <div>Role sub-scores</div><div>domain ${r.domain_match} · scope ${r.scope_match} · industry ${r.industry_fit} · comp ${r.comp_signal} · LLM overall ${r.overall_score_llm}</div>
    <div>Seniority read</div><div>${esc(r.seniority_read||'')} · manages managers: ${r.manages_managers}</div>
    <div>Location</div><div>${esc(r.location_raw||'')} <span class="muted">(${esc(r.location_reason||'')})</span></div>
    <div>Comp</div><div>${band(r)} <span class="muted">${esc(r.compensation_raw||'')}</span></div>
    <div>Posted</div><div class="muted">${esc(r.posted_at||'')}</div>
   </div>
   <h4>Rationale</h4><div>${esc(r.rationale||'')}</div>
   <h4>Concerns</h4><div>${esc(r.concerns||'—')}</div>
   <h4>Connections at ${esc(r.company)} (${r.connection_count})</h4>
   <table class="conns"><tbody>${conns||'<tr><td class=muted>none</td></tr>'}</tbody></table>
   <h4>Description excerpt</h4><div class="muted">${esc(r.description_excerpt||'')}</div>
   <p><button class="btn" onclick='toggleContacted(${JSON.stringify(key(r))})'>
     ${contacted[key(r)]?'✓ contacted — undo':'mark as contacted'}</button>
   &nbsp;<a href="${esc(r.url)}" target="_blank" rel="noopener">open listing ↗</a></p>`;
}

function renderRecruiters(head, body){
  head.innerHTML = '<tr>'+
    ['rank:#','recruiter_score:Score','name:Name','position:Title','company:Company',
     'message_count:DMs','last_message_date:Last DM','link:LinkedIn']
    .map(c=>{const [k,l]=c.split(':');return `<th data-k="${k}">${l}</th>`}).join('')+'</tr>';
  head.querySelectorAll('th').forEach(th=>th.onclick=()=>setSort(th.dataset.k));
  let rows = DATA.recruiters.slice();
  const term = q.value.toLowerCase().trim();
  if(term) rows = rows.filter(r=>(r.name+' '+r.position+' '+r.company).toLowerCase().includes(term));
  if(filters.has('hidecontacted')) rows = rows.filter(r=>!contacted['rec|'+r.profile_url]);
  rows.sort((a,b)=>{let x=a[sortKey==='final_score'?'recruiter_score':sortKey],
    y=b[sortKey==='final_score'?'recruiter_score':sortKey];
    if(typeof x==='string'){x=x||'';y=y||'';return x<y?-sortDir:x>y?sortDir:0;}
    return ((x||0)-(y||0))*sortDir;});
  body.innerHTML='';
  rows.forEach(r=>{
    const tr=document.createElement('tr'); tr.className='role'+(contacted['rec|'+r.profile_url]?' contacted':'');
    tr.innerHTML=`<td>${r.rank||''}</td><td class="score ${scoreClass(r.recruiter_score||0)}">${(r.recruiter_score||0).toFixed(1)}</td>`+
      `<td>${esc(r.name)}</td><td>${esc(r.position||'')}</td><td>${esc(r.company||'')}</td>`+
      `<td>${r.message_count||0}</td><td class="muted">${esc(r.last_message_date||'—')}</td>`+
      `<td><a href="${esc(r.profile_url)}" target="_blank" rel="noopener">profile ↗</a></td>`;
    const det=document.createElement('tr'); det.className='detail'; det.style.display='none';
    det.innerHTML=`<td colspan="8"><div class="kv">
      <div>Why ranked</div><div>${esc(r.why_ranked||'')}</div>
      <div>Era</div><div>${esc(r.era||'none')}</div>
      <div>First / last DM</div><div class="muted">${esc(r.first_message_date||'—')} → ${esc(r.last_message_date||'—')}</div>
      <div>Connection score</div><div>${r.connection_score}</div>
     </div><p><button class="btn" onclick='toggleContacted("rec|"+${JSON.stringify(r.profile_url)})'>
      ${contacted['rec|'+r.profile_url]?'✓ contacted — undo':'mark as contacted'}</button></p></td>`;
    tr.onclick=e=>{if(e.target.tagName==='A')return;det.style.display=det.style.display==='none'?'':'none';};
    body.appendChild(tr); body.appendChild(det);
  });
  if(!rows.length) body.innerHTML='<tr><td colspan="8" class="muted">no rows</td></tr>';
}

function toggleContacted(k){
  contacted[k] = !contacted[k];
  if(!contacted[k]) delete contacted[k];
  try{ localStorage.setItem(LS, JSON.stringify(contacted)); }catch(e){}
  render();
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
