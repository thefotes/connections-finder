/* Connections Finder board — vanilla JS, no build step.
   Ports the report.html UI (dynamic track tabs, search, chips, min-score,
   expandable rows) and adds outreach logging backed by data/outreach.db. */

let DATA = null;      // bootstrap payload: tracks, roles, recruiters, outreaches
let tab = 'recruiters';
const filters = new Set();
const q = document.getElementById('q');
const minscore = document.getElementById('minscore');

const TRACKS = () => DATA?.tracks || [];
const trackOf = id => TRACKS().find(t => t.id === id);

function buildTabs(){
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = TRACKS().map((t,i) =>
      `<div class="tab${i===0?' active':''}" data-tab="${t.id}">${esc(t.name)}</div>`
    ).join('') + '<div class="tab" data-tab="recruiters">Recruiters</div>';
  tabs.querySelectorAll('.tab').forEach(t => t.onclick = () => {
    tabs.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active'); tab = t.dataset.tab; render();
  });
}

async function load() {
  const res = await fetch('/api/bootstrap');
  if (!res.ok) {
    const e = await res.json().catch(() => ({}));
    document.getElementById('statline').textContent = e.error || 'failed to load data';
    return;
  }
  DATA = await res.json();
  document.getElementById('genmeta').textContent = '· generated ' + (DATA.meta.generated_at || '');
  const s = DATA.stats || {};
  const byTrack = s.by_track || {};
  document.getElementById('statline').textContent =
    `${s.roles_total ?? DATA.roles.length} roles (` +
    `${TRACKS().map(t => `${byTrack[t.id] ?? 0} ${t.name}`).join(' · ')}) · ` +
    `${s.tier_1 ?? 0} remote-US · ${s.tier_2 ?? 0} onsite metro · ${s.recruiters_total ?? DATA.recruiters.length} recruiters · ` +
    `${s.industry_flagged ?? 0} industry-flagged`;
  buildTabs();
  tab = TRACKS()[0] ? TRACKS()[0].id : tab;
  render();
}

// -- helpers ---------------------------------------------------------------
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function scoreClass(v){ return v>=50?'s-hi':v>=30?'s-md':'s-lo'; }
function money(n){ return n==null?'':'$'+Math.round(n/1000)+'k'; }
function band(r){
  if(r.comp_min||r.comp_max) return money(r.comp_min)+'–'+money(r.comp_max);
  if(r.compensation_raw) return String(r.compensation_raw).slice(0,24);
  return '<span class="muted">—</span>';
}
function roleKey(r){ return r.tab+'|'+r.company+'|'+r.title+'|'+r.url; }
function isContacted(r){
  if (tab === 'recruiters') return !!r.outreach_status;
  // a role counts as contacted only if it is attached to an outreach; a
  // company-level outreach dims via the badge, it does not hide the row
  return DATA.outreaches.some(o => o.role_urls.includes(r.url));
}
function outreachBadge(r){
  const s = r.outreach_status;
  if (!s) return '';
  if (s.startsWith('company:')) return ` <span class="pill osh">outreach @ company: ${esc(s.slice(8))}</span>`;
  return ` <span class="pill ${s==='dead'?'osdead':'osh'}">${esc(s)}</span>`;
}

// -- tabs / filters / sort ---------------------------------------------------
document.querySelectorAll('.chip').forEach(c => c.onclick = () => {
  c.classList.toggle('on');
  filters.has(c.dataset.f) ? filters.delete(c.dataset.f) : filters.add(c.dataset.f);
  render();
});
q.oninput = render; minscore.onchange = render;

let sortKey = 'final_score', sortDir = -1;
function setSort(k){ if(sortKey===k) sortDir*=-1; else {sortKey=k; sortDir=-1;} render(); }

// -- rows ----------------------------------------------------------------------
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
  if(filters.has('hidecontacted')) rows = rows.filter(r => !isContacted(r));
  const mn = +minscore.value; if(mn) rows = rows.filter(r => r.final_score >= mn);
  rows.sort((a,b)=>{
    let x=a[sortKey], y=b[sortKey];
    if(typeof x==='string'){x=x||'';y=y||'';return x<y?-sortDir:x>y?sortDir:0;}
    return ((x||0)-(y||0))*sortDir;
  });
  return rows;
}

function statusButtons(r){
  const s = r.outreach_status && !r.outreach_status.startsWith('company:')
    ? r.outreach_status : null;
  const oid = s ? DATA.outreaches.find(o => o.role_urls.includes(r.url))?.id : null;
  if (!oid) return '';
  const opts = ['contacted','applied','active','dead'].map(v =>
    `<button class="btn ${v===s?'primary':''}" onclick="setStatus(${oid},'${v}');event.stopPropagation();">${v===s?'● '+v:v}</button>`
  ).join(' ');
  return `<h4>Outreach status</h4><div>${opts}</div>`;
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
  rows.forEach(r=>{
    const tr = document.createElement('tr');
    tr.className = 'role' + (isContacted(r) ? ' contacted':'');
    const bc = r.best_connection;
    tr.innerHTML =
      `<td class="score ${scoreClass(r.final_score)}">${r.final_score.toFixed(1)}</td>`+
      `<td>${esc(r.company)}</td>`+
      `<td>${esc(r.title)}`+
        (r.industry_flag?` <span class="pill flag">${esc(r.industry_flag)}</span>`:'')+
        (r.comp_penalty_flag?` <span class="pill flag">low band</span>`:'')+
        outreachBadge(r)+`</td>`+
      `<td><span class="pill ${r.location_tier==='tier_1'?'t1':'t2'}">`+
        `${r.location_tier==='tier_1'?'Remote US':'Onsite metro'}</span></td>`+
      `<td>${band(r)}</td>`+
      `<td>${r.connection_count}</td>`+
      `<td>${bc?esc(bc.name)+' <span class="muted">— '+esc((bc.position||'').slice(0,42))+'</span>':'<span class="muted">none</span>'}</td>`+
      `<td><a href="${esc(r.url)}" target="_blank" rel="noopener">open ↗</a></td>`;
    const det = document.createElement('tr');
    det.className='detail'; det.style.display='none';
    det.innerHTML = `<td colspan="8">${detailHtml(r)}</td>`;
    tr.onclick = e => { if(e.target.tagName==='A'||e.target.tagName==='BUTTON') return;
      det.style.display = det.style.display==='none'?'':'none'; };
    body.appendChild(tr); body.appendChild(det);
  });
  if(!rows.length) body.innerHTML = '<tr><td colspan="8" class="muted">no rows match</td></tr>';
}

function detailHtml(r){
  const conns = (r.connections||[]).map(c =>
    `<tr><td>${(c.connection_score??0).toFixed(1)}</td>`+
    `<td><a href="${esc(c.profile_url)}" target="_blank" rel="noopener">${esc(c.name)}</a></td>`+
    `<td>${esc(c.position||'')}</td>`+
    `<td class="muted">${esc(c.why_ranked||'')}</td>`+
    `<td class="muted">${c.message_count||0} DMs${c.last_message_date?' · '+c.last_message_date:''}</td></tr>`
  ).join('');
  return `<div class="kv">
    <div>Final score</div><div>${r.final_score.toFixed(2)} &nbsp;<span class="muted">= conn_factor(${r.company_connection_score}) × role ${r.role_score} × loc ${r.location_tier}</span></div>
    <div>Role sub-scores</div><div>domain ${r.domain_match} · scope ${r.scope_match} · industry ${r.industry_fit} · comp ${r.comp_signal} · LLM overall ${r.overall_score_llm}</div>
    <div>Seniority read</div><div>${esc(r.seniority_read||'')}${trackOf(r.tab)?.manages_people?` · manages managers: ${r.manages_managers}`:''}</div>
    <div>Location</div><div>${esc(r.location_raw||'')} <span class="muted">(${esc(r.location_reason||'')})</span></div>
    <div>Comp</div><div>${band(r)} <span class="muted">${esc(r.compensation_raw||'')}</span></div>
    <div>Posted</div><div class="muted">${esc(r.posted_at||'')}</div>
   </div>
   <h4>Rationale</h4><div>${esc(r.rationale||'')}</div>
   <h4>Concerns</h4><div>${esc(r.concerns||'—')}</div>
   <h4>Connections at ${esc(r.company)} (${r.connection_count})</h4>
   <table class="conns"><tbody>${conns||'<tr><td class=muted>none</td></tr>'}</tbody></table>
   <h4>Description excerpt</h4><div class="muted">${esc(r.description_excerpt||'')}</div>
   ${statusButtons(r)}
   <p><button class="btn primary" onclick='openOutreach(${JSON.stringify(r.url)});event.stopPropagation();'>
     ${isContacted(r)?'edit outreach':'log outreach'}</button>
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
  if(filters.has('hidecontacted')) rows = rows.filter(r=>!r.outreach_status);
  rows.sort((a,b)=>{let x=a[sortKey==='final_score'?'recruiter_score':sortKey],
    y=b[sortKey==='final_score'?'recruiter_score':sortKey];
    if(typeof x==='string'){x=x||'';y=y||'';return x<y?-sortDir:x>y?sortDir:0;}
    return ((x||0)-(y||0))*sortDir;});
  body.innerHTML='';
  rows.forEach(r=>{
    const tr=document.createElement('tr'); tr.className='role'+(r.outreach_status?' contacted':'');
    tr.innerHTML=`<td>${r.rank||''}</td><td class="score ${scoreClass(r.recruiter_score||0)}">${(r.recruiter_score||0).toFixed(1)}</td>`+
      `<td>${esc(r.name)}${r.outreach_status?' '+outreachBadge(r):''}</td>`+
      `<td>${esc(r.position||'')}</td><td>${esc(r.company||'')}</td>`+
      `<td>${r.message_count||0}</td><td class="muted">${esc(r.last_message_date||'—')}</td>`+
      `<td><a href="${esc(r.profile_url)}" target="_blank" rel="noopener">profile ↗</a></td>`;
    const det=document.createElement('tr'); det.className='detail'; det.style.display='none';
    det.innerHTML=`<td colspan="8"><div class="kv">
      <div>Why ranked</div><div>${esc(r.why_ranked||'')}</div>
      <div>Era</div><div>${esc(r.era||'none')}</div>
      <div>First / last DM</div><div class="muted">${esc(r.first_message_date||'—')} → ${esc(r.last_message_date||'—')}</div>
      <div>Connection score</div><div>${r.connection_score}</div>
     </div>
     <p><button class="btn primary" onclick='toggleRecruiter(${JSON.stringify(r.profile_url)},${JSON.stringify(r.name)});event.stopPropagation();'>
      ${r.outreach_status?'✓ contacted — undo':'mark as contacted'}</button></p></td>`;
    tr.onclick=e=>{if(e.target.tagName==='A')return;det.style.display=det.style.display==='none'?'':'none';};
    body.appendChild(tr); body.appendChild(det);
  });
  if(!rows.length) body.innerHTML='<tr><td colspan="8" class="muted">no rows</td></tr>';
}

// -- outreach writes ----------------------------------------------------------
async function api(method, path, body){
  const res = await fetch(path, {method, headers:{'Content-Type':'application/json'},
    body: body ? JSON.stringify(body) : undefined});
  const obj = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(obj.error || res.statusText), {status: res.status, obj});
  return obj;
}
async function reload(){ await load(); }

async function setStatus(oid, status){
  try { await api('PATCH', '/api/outreach/'+oid, {status}); await reload(); }
  catch(e){ alert(e.message); }
}

async function toggleRecruiter(profileUrl, name){
  const key = 'recruiter:'+profileUrl;
  const existing = DATA.outreaches.find(o => o.company_normalized === key);
  try {
    if (existing) {
      await api('DELETE', '/api/outreach/'+existing.id);
    } else {
      await api('POST', '/api/outreach', {company_normalized: key, company_display: name,
        person_profile_url: profileUrl, person_name: name, status: 'contacted'});
    }
    await reload();
  } catch(e){ alert(e.message); }
}

// -- outreach panel -------------------------------------------------------------
const overlay = document.getElementById('overlay');
const panel = document.getElementById('panel');
overlay.onclick = e => { if (e.target === overlay) closePanel(); };
function closePanel(){ overlay.style.display = 'none'; panel.innerHTML = ''; }
function openPanel(){ overlay.style.display = 'flex'; }

function openOutreach(roleUrl){
  const role = DATA.roles.find(r => r.url === roleUrl);
  if (!role) return;
  const existing = DATA.outreaches.find(o => o.company_normalized === role.company_normalized);
  if (existing) return openEdit(existing, role);
  openCreate(role);
}

function openCreate(role){
  const conns = role.connections || [];
  const companyRoles = DATA.roles.filter(r => r.company_normalized === role.company_normalized);
  const owned = new Set(DATA.outreaches.flatMap(o => o.role_urls));
  panel.innerHTML = `
    <h3>Log outreach — ${esc(role.company)}</h3>
    <label>Person (one)</label>
    <div class="people" id="people">${conns.length ? conns.map((c,i)=>`
      <label><input type="radio" name="person" value="${i}" ${i===0?'checked':''}>
        <a href="${esc(c.profile_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(c.name)}</a>
        <span class="muted">${esc(c.position||'')} · score ${(c.connection_score??0).toFixed(1)}</span>
      </label>`).join('') : '<label class="muted">no connections at this company — outreach will be cold</label>'}
    </div>
    <label>Additional roles at this company (this role is pre-checked; roles owned by another outreach are disabled)</label>
    <div class="roles-list" id="rolelist">${companyRoles.map(r=>`
      <label><input type="checkbox" class="xrole" data-url="${esc(r.url)}"
        ${r.url===role.url?'checked':owned.has(r.url)?'disabled':''}>
        ${esc(r.title)} <span class="muted">${r.url===role.url?'(this role)':owned.has(r.url)?'(owned by another outreach)':''}
        · score ${r.final_score.toFixed(1)}</span></label>`).join('')}
    </div>
    <label>Status</label>
    <select id="ostatus">${['contacted','applied','active','dead'].map(s=>`<option ${s==='contacted'?'selected':''}>${s}</option>`).join('')}</select>
    <label>Notes (free text — write your own message elsewhere; this is for outcomes and context)</label>
    <textarea id="onotes"></textarea>
    <div class="actions">
      <button class="btn primary" id="save">Save</button>
      <button class="btn" onclick="closePanel()">Cancel</button>
      <span class="err" id="perr"></span>
    </div>`;
  document.getElementById('save').onclick = () => saveCreate(role);
  openPanel();
}

async function saveCreate(role){
  const err = document.getElementById('perr');
  const checked = [...panel.querySelectorAll('.xrole:checked')].map(x => x.dataset.url);
  const person = panel.querySelector('input[name=person]:checked');
  const c = person != null ? (role.connections||[])[+person.value] : null;
  try {
    await api('POST', '/api/outreach', {
      company_normalized: role.company_normalized,
      company_display: role.company,
      person_profile_url: c?.profile_url || null,
      person_name: c?.name || null,
      status: document.getElementById('ostatus').value,
      notes: document.getElementById('onotes').value,
      role_urls: checked,
    });
    closePanel(); await reload();
  } catch(e){
    if (e.status === 409 && e.obj?.outreach_id) {
      closePanel();
      openEdit(DATA.outreaches.find(o => o.id === e.obj.outreach_id), role);
      return;
    }
    err.textContent = e.message;
  }
}

function openEdit(o, role){
  const companyRoles = DATA.roles.filter(r => r.company_normalized === o.company_normalized);
  const attached = new Set(o.role_urls);
  const ownedElsewhere = new Map();
  for (const other of DATA.outreaches) {
    if (other.id === o.id) continue;
    for (const u of other.role_urls) ownedElsewhere.set(u, other.company_display);
  }
  panel.innerHTML = `
    <h3>Edit outreach — ${esc(o.company_display)}</h3>
    <div class="edit-head">One open outreach per company. Created ${esc(o.contacted_at||'—')} · last updated ${esc(o.updated_at)}</div>
    <label>Person</label>
    <div class="people">
      ${(role?.connections||[]).map(c=>`
      <label><input type="radio" name="person" data-u="${esc(c.profile_url)}"
        ${o.person_profile_url===c.profile_url?'checked':''}>
        <a href="${esc(c.profile_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(c.name)}</a>
        <span class="muted">${esc(c.position||'')}</span></label>`).join('')||
      '<label class="muted">cold — no connections listed</label>'}
    </div>
    <label>Roles attached (disabled = owned by another outreach)</label>
    <div class="roles-list">${companyRoles.map(r=>`
      <label><input type="checkbox" class="xrole" data-url="${esc(r.url)}"
        ${attached.has(r.url)?'checked':ownedElsewhere.has(r.url)?'disabled':''}>
        ${esc(r.title)} ${ownedElsewhere.has(r.url)?`<span class="muted">(owned by: ${esc(ownedElsewhere.get(r.url))})</span>`:''}
        <span class="muted">· score ${r.final_score.toFixed(1)}</span></label>`).join('')}
    </div>
    <label>Status</label>
    <select id="ostatus">${['contacted','applied','active','dead'].map(s=>
      `<option ${s===o.status?'selected':''}>${s}</option>`).join('')}</select>
    <label>Notes</label>
    <textarea id="onotes">${esc(o.notes||'')}</textarea>
    <div class="actions">
      <button class="btn primary" id="save">Save changes</button>
      <button class="btn" onclick="closePanel()">Cancel</button>
      <span class="err" id="perr"></span>
    </div>`;
  document.getElementById('save').onclick = () => saveEdit(o, role);
  openPanel();
}

async function saveEdit(o, role){
  const err = document.getElementById('perr');
  const person = panel.querySelector('input[name=person]:checked');
  const checked = [...panel.querySelectorAll('.xrole:checked')].map(x => x.dataset.url);
  try {
    await api('PATCH', '/api/outreach/'+o.id, {
      status: document.getElementById('ostatus').value,
      notes: document.getElementById('onotes').value,
      person_profile_url: person?.dataset.u || null,
      person_name: person ? (role?.connections||[]).find(c=>c.profile_url===person.dataset.u)?.name || o.person_name : null,
    });
    const toAdd = checked.filter(u => !o.role_urls.includes(u));
    const toDrop = o.role_urls.filter(u => !checked.includes(u));
    if (toAdd.length) await api('POST', '/api/outreach/attach-roles', {outreach_id: o.id, role_urls: toAdd});
    for (const u of toDrop) await api('POST', '/api/outreach/detach-role', {role_url: u});
    closePanel(); await reload();
  } catch(e){ err.textContent = e.message; }
}

load();
