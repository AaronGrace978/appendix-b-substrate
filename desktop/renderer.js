'use strict';
const $ = id => document.getElementById(id);
const PROVIDERS = [['openai','OpenAI'],['anthropic','Anthropic'],['openrouter','OpenRouter'],['ollama-cloud','Ollama Cloud'],['ollama-local','Ollama Local']];
const CUSTOM = '__custom__';
const PHASE_COLORS = {DECOMPOSE:'var(--teal)', ATTACK:'var(--amber)', VERIFY:'var(--green)', CONCLUDE:'var(--violet)'};

const store = {
  get(k){ try { return localStorage.getItem('appendixb.'+k); } catch { return null; } },
  set(k,v){ try { localStorage.setItem('appendixb.'+k, v); } catch {} }
};

for (const [value,label] of PROVIDERS) { $('provider').add(new Option(label,value)); $('reviewProvider').add(new Option(label,value)); }
$('reviewProvider').add(new Option('No reviewer',''),0); $('reviewProvider').value='';
$('provider').value = store.get('provider') || 'openai';

/* ── status ───────────────────────────── */
function status(text, state='') { $('status').textContent = text; $('status').title = text; $('dot').className = 'dot ' + state; }

/* ── busy flags: the substrate process serves requests concurrently ── */
let chatId = null, labId = null, loading = false, cancelled = false, labCancelled = false;
function setChatBusy(on) {
  const send = $('send');
  if (on) { send.classList.add('stop'); send.innerHTML = 'Stop'; }
  else { send.classList.remove('stop'); send.innerHTML = 'Send <span class="mono" style="opacity:.6">↵</span>'; }
}
function setLabBusy(on) { $('run').disabled = on; $('cancel').disabled = !on; }
function setLoading(on) { loading = on; $('discover').disabled = on; $('reviewDiscover').disabled = on; }

/* ── model dropdowns ──────────────────── */
function modelValue(selectId, inputId) {
  return $(selectId).value === CUSTOM ? $(inputId).value.trim() : $(selectId).value;
}
function fillModels(selectId, inputId, models, keep) {
  const sel = $(selectId);
  const opts = models.map(m => new Option(m, m));
  opts.push(new Option('Custom model ID…', CUSTOM));
  sel.replaceChildren(...opts);
  if (keep && models.includes(keep)) sel.value = keep;
  else if (keep) { sel.value = CUSTOM; $(inputId).value = keep; }
  $(inputId).hidden = sel.value !== CUSTOM;
}
for (const [s,i] of [['modelSelect','model'],['reviewModelSelect','reviewModel']]) {
  $(s).onchange = () => { $(i).hidden = $(s).value !== CUSTOM; if (!$(i).hidden) $(i).focus(); if (s === 'modelSelect') saveModel(); };
}
$('model').onchange = saveModel;
function saveModel(){ const m = modelValue('modelSelect','model'); if (m) store.set('model.'+$('provider').value, m); updateReady(); loadState(); }

function config(review=false) {
  return review
    ? {name:$('reviewProvider').value, key:$('reviewKey').value, model:modelValue('reviewModelSelect','reviewModel')}
    : {name:$('provider').value, key:$('key').value, model:modelValue('modelSelect','model')};
}

async function discover(review=false, quiet=false) {
  if (loading) return;
  const p = config(review);
  if (!p.name) { status('Choose a reviewer provider first.', 'err'); return; }
  const btn = review ? $('reviewDiscover') : $('discover');
  setLoading(true); btn.classList.add('spin'); status('Loading models from '+labelOf(p.name)+'…', 'busy');
  try {
    const models = await window.lab.request({action:'models', provider:p});
    const keep = review ? p.model : (p.model || store.get('model.'+p.name));
    fillModels(review ? 'reviewModelSelect' : 'modelSelect', review ? 'reviewModel' : 'model', models, keep);
    status(models.length + ' models from ' + labelOf(p.name) + '.', 'ok');
    if (!review) saveModel();
  } catch (e) {
    fillModels(review ? 'reviewModelSelect' : 'modelSelect', review ? 'reviewModel' : 'model', [], p.model);
    status(quiet && /key required/i.test(e.message) ? 'Enter a '+labelOf(p.name)+' key, then refresh models.' : clean(e.message), 'err');
  } finally { setLoading(false); btn.classList.remove('spin'); }
}
const labelOf = v => (PROVIDERS.find(p => p[0] === v) || [,v])[1];
const clean = m => String(m).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
$('discover').onclick = () => discover(false);
$('reviewDiscover').onclick = () => discover(true);
$('provider').onchange = () => { store.set('provider', $('provider').value); $('key').value=''; discover(false, true); };
$('key').addEventListener('keydown', e => { if (e.key === 'Enter') discover(false); });
function updateReady(){ const m = modelValue('modelSelect','model'); if (!chatId && !labId && m) status(labelOf($('provider').value)+' · '+m, 'ok'); }
async function loadState() {
  const p = config(); if (!p.model) return;
  try { gauges(await window.lab.request({action:'state', provider:{name:p.name, model:p.model}}), p.model); } catch {}
}

/* ── tabs ─────────────────────────────── */
for (const tab of document.querySelectorAll('nav button')) tab.onclick = () => {
  for (const t of document.querySelectorAll('nav button')) t.setAttribute('aria-selected', t === tab);
  for (const v of document.querySelectorAll('.view')) v.classList.toggle('active', v.id === tab.dataset.view);
  if (tab.dataset.view === 'chatView') $('prompt').focus();
};

/* ── chat ─────────────────────────────── */
let history = [];
$('protocol').checked = store.get('protocol') === '1';
$('protocol').onchange = () => store.set('protocol', $('protocol').checked ? '1' : '0');

function el(tag, cls, text, parent) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; if (parent) parent.append(e); return e; }

function renderBody(text, target) {
  const parts = text.replace(/\n{3,}/g, '\n\n').split(/```[\w+-]*\n?/);
  parts.forEach((part, i) => {
    if (i % 2) { el('pre', '', part.replace(/\n$/, ''), target); return; }
    for (const line of part.split('\n')) {
      const m = line.match(/^\s*(?:#{1,4}\s*)?\**(DECOMPOSE|ATTACK|VERIFY|CONCLUDE|CONCLUSION)\**\s*:?\**\s*(.*)$/i);
      if (m) {
        const key = m[1].toUpperCase() === 'CONCLUSION' ? 'CONCLUDE' : m[1].toUpperCase();
        const badge = el('span', 'ph', key, target); badge.style.color = PHASE_COLORS[key]; badge.style.borderColor = PHASE_COLORS[key];
        if (m[2]) { inline(m[2], target); target.append('\n'); }
        continue;
      }
      inline(line, target); target.append('\n');
    }
  });
  if (target.lastChild && target.lastChild.nodeType === 3) target.lastChild.textContent = target.lastChild.textContent.replace(/\n+$/, '');
}
function inline(line, target) {
  line.split(/(`[^`]+`)/).forEach(seg => {
    if (/^`[^`]+`$/.test(seg)) el('code', '', seg.slice(1, -1), target);
    else target.append(seg.replace(/\*\*(.+?)\*\*/g, '$1'));
  });
}

function addMessage(role, text, meta='') {
  $('empty')?.remove();
  const row = el('div', 'msg ' + role, null, $('thread'));
  el('div', 'av', role === 'user' ? 'YOU' : role === 'error' ? '!' : 'B', row);
  const col = el('div', '', null, row);
  const who = el('div', 'who', role === 'user' ? 'You' : role === 'error' ? 'Error' : 'Model', col);
  if (meta) el('em', '', meta, who);
  const body = el('div', 'body', null, col);
  if (role === 'assistant') renderBody(text, body); else body.textContent = text;
  $('log').scrollTop = $('log').scrollHeight;
  return row;
}

function renderChecks(checks, col) {
  if (!checks || !checks.length) return;
  const box = el('div', 'checks', null, col);
  el('div', 'label', 'Independent checks · computed in Python', box);
  for (const c of checks) {
    const chip = el('div', 'check ' + c.verdict, null, box);
    el('b', '', c.verdict === 'supported' ? '✓' : '✗', chip);
    el('span', '', c.claim, chip);
    el('em', '', c.detail, chip);
    chip.title = c.kind;
  }
}

async function send() {
  if (chatId) { cancelled = true; window.lab.cancel(chatId); return; }
  const text = $('prompt').value.trim();
  if (!text) return;
  const p = config();
  if (!p.model) { status('Choose a model first — refresh the list or pick Custom model ID.', 'err'); $('modelSelect').focus(); return; }
  history.push({role:'user', content:text});
  addMessage('user', text);
  $('prompt').value = ''; grow();
  const row = addMessage('assistant', '');
  const body = row.querySelector('.body');
  body.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
  cancelled = false;
  const id = await window.lab.newId(); chatId = id; setChatBusy(true);
  status('Streaming · ' + p.model, 'busy');
  if ($('protocol').checked) scanPipeline(true);
  let streamed = '', first = true;
  const off = window.lab.onDelta(m => {
    if (m.id !== id) return;
    if (first) { body.textContent = ''; first = false; }
    streamed += m.delta; body.textContent = streamed;
    const log = $('log'); if (log.scrollHeight - log.scrollTop - log.clientHeight < 160) log.scrollTop = log.scrollHeight;
  });
  try {
    const r = await window.lab.request({action:'chat', stream:true, provider:p, messages:history, protocol:$('protocol').checked}, id);
    const reply = r.text || streamed;
    if (!reply.trim()) throw Error(r.cancelled ? 'Stopped before any text arrived.' : 'Empty reply');
    history.push({role:'assistant', content:reply});
    body.replaceChildren(); renderBody(reply, body);
    const u = r.usage || {};
    const tokens = (u.output_tokens ?? u.completion_tokens ?? u.eval_count);
    el('em', '', r.model + ' · ' + r.seconds.toFixed(1) + 's' + (tokens != null ? ' · ' + tokens + ' tok' : '') + (r.cancelled ? ' · stopped' : ''), row.querySelector('.who'));
    renderChecks(r.checks, body.parentElement);
    if (r.state) gauges(r.state, r.model);
    $('usage').textContent = Math.floor(history.length / 2) + ' turns' + (r.checks && r.checks.length ? ' · ' + r.checks.length + ' claims checked' : '');
    status(labelOf(p.name) + ' · ' + p.model, 'ok');
    const phases = ['decompose','attack','verify','conclude'].filter(ph => new RegExp('\\b' + ph, 'i').test(reply));
    if ($('protocol').checked || phases.length >= 3) lightPipeline(phases);
  } catch (e) {
    history.pop(); row.remove();
    if (!streamed) { $('thread').lastChild?.remove(); $('prompt').value = text; grow(); }
    else { history.push({role:'user', content:text}, {role:'assistant', content:streamed}); addMessage('assistant', streamed, 'partial'); }
    if (cancelled) status('Stopped.', ''); else { addMessage('error', clean(e.message)); status(clean(e.message), 'err'); }
  } finally { off(); chatId = null; setChatBusy(false); scanPipeline(false); $('prompt').focus(); }
}
$('send').onclick = send;
$('prompt').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
function grow(){ const t = $('prompt'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 240) + 'px'; }
$('prompt').addEventListener('input', grow);
$('clear').onclick = () => { if (chatId) return; history = []; $('thread').replaceChildren(); $('usage').textContent=''; lightPipeline([]); $('prompt').focus(); };
for (const chip of document.querySelectorAll('.chip')) chip.onclick = () => { $('prompt').value = chip.textContent; grow(); $('prompt').focus(); };

/* ── protocol rail ────────────────────── */
function scanPipeline(on) { for (const ph of document.querySelectorAll('.phase')) { ph.classList.toggle('scan', on); if (on) ph.classList.remove('on'); } }
function lightPipeline(names) {
  document.querySelectorAll('.phase').forEach((ph, i) => setTimeout(() => ph.classList.toggle('on', names.includes(ph.dataset.phase)), i * 140));
}
function gauges(state, model) {
  const box = $('gauges'); box.replaceChildren();
  if (model) el('div', 'muted mono', model, box).style.fontSize = '11px';
  for (const [name, value] of Object.entries(state)) {
    if (typeof value !== 'number') continue;
    const g = el('div', 'gauge', null, box);
    el('span', '', name.replace(/_/g, ' '), g); el('span', '', value.toFixed(2), g);
    const bar = el('div', 'bar', null, g); const fill = el('span', '', null, bar);
    requestAnimationFrame(() => fill.style.width = Math.max(0, Math.min(1, value)) * 100 + '%');
  }
}

/* ── experiment ───────────────────────── */
$('mode').onchange = () => { $('letters').hidden = $('mode').value !== 'letters'; $('research').hidden = !$('letters').hidden; };
$('cancel').onclick = () => { if (labId) { labCancelled = true; window.lab.cancel(labId); } };

function renderRun(x) {
  const r = x.result, card = el('div', 'result', null, $('results'));
  const head = el('header', '', null, card);
  el('span', 'verdict v-' + String(r.verdict).toLowerCase(), String(r.verdict).toUpperCase(), head);
  el('b', '', x.adaptive ? 'Adaptive' : 'Fixed', head);
  el('span', 'meta', x.provider + ' / ' + x.model + ' · ' + x.calls + ' calls · ' + x.seconds.toFixed(1) + 's', head);
  const inner = el('div', 'inner', null, card);
  el('div', 'muted', r.reason, inner);
  const metrics = el('div', 'metrics', null, inner);
  for (const [name, value] of Object.entries(x.state)) {
    if (typeof value !== 'number') continue;
    const g = el('div', 'gauge', null, metrics); el('span', '', name.replace(/_/g,' '), g); el('span', '', value.toFixed(2), g);
    const bar = el('div', 'bar', null, g); el('span', '', null, bar).style.width = Math.max(0, Math.min(1, value)) * 100 + '%';
  }
  for (const c of r.candidates) {
    const d = el('div', 'cand', null, inner);
    d.style.borderLeftColor = c.verdict === 'supported' ? 'var(--green)' : c.verdict === 'refuted' ? 'var(--red)' : 'var(--amber)';
    el('div', 'label', c.strategy + ' · ' + c.verdict, d); el('pre', '', c.claim, d);
    for (const ev of c.evidence) el('p', '', ev.source + ': ' + ev.detail, d);
  }
  const det = el('details', '', null, inner); el('summary', '', 'Observable trace and token usage', det);
  el('pre', '', JSON.stringify({events:x.events, usage:x.usage}, null, 2), det);
  gauges(x.state);
}

$('run').onclick = async () => {
  if (labId) return;
  labCancelled = false; setLabBusy(true); $('results').replaceChildren(); scanPipeline(true);
  try {
    const p = config(), r = config(true), condition = $('condition').value;
    if (!p.model) throw Error('Choose a proposer model in the top bar');
    if (r.name && !r.model) throw Error('Choose a reviewer model');
    const req = {action:'run', provider:p, reviewer:r.name ? r : null, mode:$('mode').value, task:$('task').value,
      word:$('word').value, letter:$('letter').value, budget:Number($('budget').value), fresh:condition === 'compare'};
    for (const adaptive of condition === 'compare' ? [false, true] : [condition === 'adaptive']) {
      if (labCancelled) break;
      status('Running ' + (adaptive ? 'adaptive' : 'fixed') + ' condition…', 'busy');
      labId = await window.lab.newId();
      renderRun(await window.lab.request({...req, adaptive}, labId));
    }
    lightPipeline(['decompose','attack','verify','conclude']);
    status(labCancelled ? 'Cancelled.' : 'Experiment finished. Open research results are not proof.', labCancelled ? '' : 'ok');
  } catch (e) { status(labCancelled ? 'Cancelled.' : clean(e.message), labCancelled ? '' : 'err'); }
  finally { labId = null; setLabBusy(false); scanPipeline(false); }
};

/* ── boot ─────────────────────────────── */
discover(false, true);
$('prompt').focus();
