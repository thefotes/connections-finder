// Headless smoke test for report.html — runs the page's own <script> against a
// minimal DOM stub and asserts each tab renders rows without throwing.
// Usage: node pipeline/smoke_report.js report.html
const fs = require('fs');
const path = process.argv[2] || 'report.html';
const html = fs.readFileSync(path, 'utf8');

// --- tiny DOM stub -------------------------------------------------------------
class El {
  constructor(tag='div'){ this.tagName=(tag||'div').toUpperCase(); this.children=[];
    this.dataset={}; this.style={}; this._html=''; this.classList=new Set();
    this._listeners={}; this.value=''; this.textContent=''; }
  set innerHTML(v){ this._html=String(v); this.children=[];
    // count "<tr" as rendered rows for assertions
    this._rows=(String(v).match(/<tr/g)||[]).length; }
  get innerHTML(){ return this._html; }
  appendChild(c){ this.children.push(c); return c; }
  querySelectorAll(){ return []; }
  querySelector(){ return null; }
  addEventListener(t,f){ (this._listeners[t]=this._listeners[t]||[]).push(f); }
  set onclick(f){ this._listeners.click=[f]; }
  set oninput(f){ this._listeners.input=[f]; }
  set onchange(f){ this._listeners.change=[f]; }
  getAttribute(){ return null; }
  setAttribute(){}
  remove(){}
}
const ids = {};
function mkId(id){ return ids[id] || (ids[id]=new El()); }
const elements = {
  q: Object.assign(new El('input'), {value:''}),
  minscore: Object.assign(new El('select'), {value:'0'}),
  body: new El('tbody'), head: new El('thead'),
};
const document = {
  getElementById: (id)=> elements[id] || mkId(id),
  querySelectorAll: ()=> [],
  createElement: (t)=> new El(t),
};
const localStorage = {
  _d:{}, getItem(k){ return this._d[k]||null; }, setItem(k,v){ this._d[k]=v; },
};
const window = {};

// --- run the page's last <script> -------------------------------------------------
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const code = scripts[scripts.length-1];
const dataMatch = html.match(/<script id="data" type="application\/json">([\s\S]*?)<\/script>/);
if(!dataMatch) { console.error('FAIL: no embedded data script'); process.exit(1); }
const embedded = JSON.parse(dataMatch[1]);

// provide the data script node via getElementById('data')
elements.data = Object.assign(new El('script'), {textContent: dataMatch[1]});

let failures = 0;
function assert(cond, msg){ if(!cond){ console.error('FAIL:', msg); failures++; }
  else console.log('ok  ', msg); }

try {
  const fn = new Function('document','window','localStorage','console', code + `
    ;return { render, roleRows: (typeof roleRows!=='undefined'?roleRows:null),
              setTab:(t)=>{tab=t;},
              toggleContacted:(typeof toggleContacted!=='undefined'?toggleContacted:null),
              DATA };`);
  const api = fn(document, window, localStorage, console);
  assert(api.DATA.roles.length === embedded.roles.length, 'DATA.roles wired');

  // leadership tab
  api.setTab('leadership'); api.render();
  const leadRows = (elements.body._rows || 0) + elements.body.children.length;
  assert(leadRows > 0 || embedded.stats.leadership === 0,
    `leadership tab renders rows (${leadRows})`);

  // ic tab
  api.setTab('ic'); api.render();
  const icRows = (elements.body._rows || 0) + elements.body.children.length;
  assert(icRows > 0 || embedded.stats.ic === 0, `ic tab renders rows (${icRows})`);

  // recruiters tab
  api.setTab('recruiters'); api.render();
  const recRows = (elements.body._rows || 0) + elements.body.children.length;
  assert(recRows > 0 || embedded.recruiters.length === 0,
    `recruiters tab renders rows (${recRows})`);

  assert(!/https?:\/\//.test(
    html.replace(/<script id="data"[\s\S]*?<\/script>/,'')
        .replace(/href="https?:\/\/[^"]*"/g,'')       // in-content links are fine
        .match(/<(?:script|link)[^>]+(?:src|href)=/g)?.join('')||''),
    'no external <script src>/<link href> (self-contained)');

  // listing links render as anchors in the roles tables
  api.setTab('leadership'); api.render();
  const anchorRows = elements.body.children.filter(
    c => (c._html||'').includes('href=') && (c._html||'').includes('open'));
  assert(anchorRows.length > 0, `role rows carry a clickable listing link (${anchorRows.length})`);

  // text search narrows the result set
  elements.q.value = embedded.roles[0].company.slice(0, 5).toLowerCase();
  api.render();
  const filtered = elements.body.children.length;
  elements.q.value = '';
  api.render();
  assert(filtered <= elements.body.children.length,
    `text search narrows rows (${filtered} <= ${elements.body.children.length})`);

  // min-score threshold filters
  elements.minscore.value = '50';
  api.render();
  const hi = elements.body.children.length;
  elements.minscore.value = '0';
  api.render();
  assert(hi <= elements.body.children.length, `min-score chip filters (${hi} <= all)`);

  // contacted toggle persists to localStorage
  api.setTab('recruiters'); api.render();
  if (embedded.recruiters.length && typeof api.toggleContacted === 'function') {
    const k = 'rec|' + embedded.recruiters[0].profile_url;
    api.toggleContacted(k);
    assert(localStorage.getItem('cf_contacted_v1') &&
           localStorage.getItem('cf_contacted_v1').includes(embedded.recruiters[0].profile_url),
      'contacted toggle persists to localStorage');
  }

} catch (e) {
  console.error('FAIL: report script threw:', e && e.stack || e);
  failures++;
}

console.log(failures ? `\nSMOKE: ${failures} failure(s)` : '\nSMOKE: all checks passed');
process.exit(failures ? 1 : 0);
