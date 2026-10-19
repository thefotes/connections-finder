// Headless smoke test for the web app's client bundle — runs app/static/app.js
// against a minimal DOM stub with a stubbed fetch, and asserts each tab actually
// renders rows.
//
// This exists because a bare `node --check` passes on code that throws the moment
// it runs, and because testing the HTTP API with curl proves nothing about whether
// the page renders: a single syntax error in app.js shipped an empty board while
// /api/bootstrap happily returned 410 roles.
//
// Usage: node app/smoke_app.js [bootstrap.json]
const fs = require('fs');
const path = require('path');

const APP = path.join(__dirname, 'static', 'app.js');
const code = fs.readFileSync(APP, 'utf8');
const src = process.argv[2] && process.argv[2] !== '-' ? process.argv[2] : 0;  // 0 = stdin fd
const payload = JSON.parse(fs.readFileSync(src, 'utf8'));

class El {
  constructor(tag='div'){ this.tagName=(tag||'div').toUpperCase(); this.children=[];
    this.dataset={}; this.style={}; this._html=''; this.classList={
      add(){}, remove(){}, toggle(){}, contains(){ return false; } };
    this._listeners={}; this.value=''; this.textContent=''; this.hidden=false; }
  set innerHTML(v){ this._html=String(v); this.children=[];
    this._rows=(String(v).match(/<tr/g)||[]).length; }
  get innerHTML(){ return this._html; }
  appendChild(c){ this.children.push(c); return c; }
  querySelectorAll(){ return []; }
  querySelector(){ return null; }
  addEventListener(t,f){ (this._listeners[t]=this._listeners[t]||[]).push(f); }
  set onclick(f){ this._listeners.click=[f]; }
  set oninput(f){ this._listeners.input=[f]; }
  set onchange(f){ this._listeners.change=[f]; }
  set onsubmit(f){ this._listeners.submit=[f]; }
  getAttribute(){ return null; }
  setAttribute(){}
  remove(){}
  focus(){}
  close(){}
  showModal(){}
}
const ids = {};
const elements = {
  q: Object.assign(new El('input'), {value:''}),
  minscore: Object.assign(new El('select'), {value:'0'}),
  body: new El('tbody'), head: new El('thead'),
};
function mkId(id){ return elements[id] || ids[id] || (ids[id]=new El()); }
const document = {
  getElementById: mkId,
  querySelectorAll: ()=> [],
  querySelector: ()=> null,
  createElement: (t)=> new El(t),
  addEventListener(){},
  body: new El('body'),
};
const window = { location:{ reload(){} } };
const fetch = async (url) => ({
  ok: true, status: 200,
  json: async () => (url.includes('/api/bootstrap') ? payload : { ok: true }),
});

let failures = 0;
function assert(cond, msg){
  if (cond) { console.log('ok  ', msg); } else { console.error('FAIL:', msg); failures++; }
}
function rowCount(){ return (elements.body._rows || 0) + elements.body.children.length; }

(async () => {
  let api;
  try {
    const fn = new Function('document','window','fetch','console','alert','confirm',
      code + `
      ;return { load, render, setTab:(t)=>{tab=t;}, getData:()=>DATA };`);
    api = fn(document, window, fetch, console, ()=>{}, ()=>true);
  } catch (e) {
    console.error('FAIL: app.js did not evaluate —', e.message);
    process.exit(1);
  }

  await api.load();
  const DATA = api.getData();
  assert(DATA && Array.isArray(DATA.roles), 'bootstrap payload wired into DATA');
  assert(DATA.roles.length === payload.roles.length,
    `all roles loaded (${DATA ? DATA.roles.length : 0})`);

  const tabs = [['recruiters', payload.recruiters.length]];
  for (const t of payload.tracks || []) {
    tabs.unshift([t.id, payload.roles.filter(r=>r.tab===t.id).length]);
  }
  for (const [t, expect] of tabs) {
    elements.body.innerHTML = '';
    elements.body.children = [];
    api.setTab(t); api.render();
    assert(rowCount() > 0 || expect === 0, `${t} tab renders rows (${rowCount()} of ${expect})`);
  }

  console.log(failures ? `SMOKE: ${failures} failure(s)` : 'SMOKE: all checks passed');
  process.exit(failures ? 1 : 0);
})();
