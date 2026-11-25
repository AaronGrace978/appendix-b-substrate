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
const clean = m => String(m).replace(/^Error invoking remote method '[^']+': (Error: )?/, '').replace(/^\w+Error: /, '');
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

const PHASE_NUM = {DECOMPOSE:'01', ATTACK:'02', VERIFY:'03', CONCLUDE:'04'};
function phaseOf(line) {
  const m = line.match(/^\s*(?:#{1,4}\s*)?\**\s*([A-Za-z]+)\s*\**\s*:?\s*\**\s*(.*)$/);
  if (!m) return null;
  let word = m[1].toUpperCase(); if (word === 'CONCLUSION') word = 'CONCLUDE';
  if (!PHASE_NUM[word]) return null;
  // Uppercase heading word, or the word alone on its line; avoids "Attack this claim…" prose.
  if (m[1] !== m[1].toUpperCase() && m[2].trim()) return null;
  return {key: word, rest: m[2].trim()};
}

function inline(text, target) {
  for (const seg of text.split(/(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*?\*|_[^_\s][^_]*?_(?!\w))/)) {
    if (!seg) continue;
    if (/^`[^`]+`$/.test(seg)) el('code', '', seg.slice(1, -1), target);
    else if (/^(\*\*|__).+\1$/.test(seg)) inline(seg.slice(2, -2), el('strong', '', null, target));
    else if (/^[*_].+[*_]$/.test(seg) && seg.length > 2) el('em', '', seg.slice(1, -1), target);
    else target.append(seg);
  }
}

function renderBody(text, target) {
  target.replaceChildren(); target.classList.remove('streaming');
  const lines = String(text).replace(/\r/g, '').split('\n');
  let box = target, para = [], list = null, listKind = '';
  const flush = () => {
    if (para.length) { const p = el('p', '', null, box); para.forEach((l, i) => { if (i) p.append(el('br')); inline(l, p); }); para = []; }
  };
  const endList = () => { list = null; listKind = ''; };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^\s*```/);
    if (fence) {
      flush(); endList();
      const code = [];
      while (++i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]);
      el('code', '', code.join('\n'), el('pre', '', null, box));
      continue;
    }
    const ph = phaseOf(line);
    if (ph) {
      flush(); endList();
      box = el('section', 'sec', null, target);
      box.style.setProperty('--c', PHASE_COLORS[ph.key]);
      const h = el('div', 'sec-h', null, box); el('i', '', PHASE_NUM[ph.key], h); h.append(ph.key);
      if (ph.rest) para.push(ph.rest);
      continue;
    }
    if (!line.trim()) { flush(); endList(); continue; }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flush(); endList(); el('hr', '', null, box); continue; }
    const hd = line.match(/^\s*#{1,4}\s+(.*)$/);
    if (hd) { flush(); endList(); inline(hd[1].replace(/\*\*/g, ''), el('h3', '', null, box)); continue; }
    const li = line.match(/^\s*([-*•+]|\d+[.)])\s+(.*)$/);
    if (li) {
      flush();
      const kind = /\d/.test(li[1]) ? 'ol' : 'ul';
      if (!list || listKind !== kind) { list = el(kind, '', null, box); listKind = kind; if (kind === 'ol') list.start = parseInt(li[1], 10) || 1; }
      inline(li[2], el('li', '', null, list));
      continue;
    }
    if (list && /^\s{2,}\S/.test(line)) { const last = list.lastElementChild; last.append(' '); inline(line.trim(), last); continue; }
    endList(); para.push(line.trim());
  }
  flush();
}

function nearBottom() { const log = $('log'); return log.scrollHeight - log.scrollTop - log.clientHeight < 140; }
function toBottom() { const log = $('log'); log.scrollTop = log.scrollHeight; $('jump').hidden = true; }

function addMessage(role, text, meta='') {
  $('empty')?.remove();
  const stick = nearBottom();
  const row = el('div', 'msg ' + role, null, $('thread'));
  if (role === 'user') { el('div', 'bubble', text, row); if (stick || true) toBottom(); return row; }
  el('div', 'av', role === 'error' ? '!' : 'B', row);
  const col = el('div', '', null, row);
  const who = el('div', 'who', null, col);
  who.append(role === 'error' ? 'Error' : 'Model');
  el('em', 'meta', meta, who);
  if (role === 'assistant') {
    const copy = el('button', 'copy', 'Copy', who); copy.type = 'button';
    copy.onclick = async () => { try { await navigator.clipboard.writeText(row.dataset.raw || ''); copy.textContent = 'Copied'; setTimeout(() => copy.textContent = 'Copy', 1200); } catch {} };
  }
  row.dataset.raw = text;
  const body = el('div', 'body', null, col);
  if (role === 'assistant') renderBody(text, body); else body.textContent = text;
  if (stick) toBottom();
  return row;
}
$('log').addEventListener('scroll', () => { if (nearBottom()) $('jump').hidden = true; });
$('jump').onclick = toBottom;

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
  for (const old of document.querySelectorAll('#thread .msg.error')) old.remove();
  history.push({role:'user', content:text});
  addMessage('user', text);
  $('prompt').value = ''; grow();
  const row = addMessage('assistant', '');
  const body = row.querySelector('.body');
  body.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
  body.classList.add('streaming');
  cancelled = false;
  const id = await window.lab.newId(); chatId = id; setChatBusy(true);
  status('Streaming · ' + p.model, 'busy');
  if ($('protocol').checked) scanPipeline(true);
  let streamed = '', first = true;
  let thinkBox = null, thinkText = null, thought = '';
  const ensureThink = () => {
    if (thinkBox) return;
    thinkBox = document.createElement('details'); thinkBox.className = 'think'; thinkBox.open = true;
    const sum = el('summary', '', null, thinkBox); el('span', 'pulse', null, sum); el('span', 'lbl', 'Reasoning…', sum);
    thinkText = el('div', 'think-text', null, thinkBox);
    body.before(thinkBox);
  };
  const off = window.lab.onDelta(m => {
    if (m.id !== id) return;
    const stick = nearBottom();
    if (m.thinking) {
      ensureThink(); thought += m.thinking; thinkText.textContent = thought;
      thinkText.scrollTop = thinkText.scrollHeight;
      if (first) body.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
    }
    if (m.delta) {
      if (first) { body.textContent = ''; first = false; if (thinkBox) { thinkBox.open = false; thinkBox.querySelector('.lbl').textContent = 'Reasoning'; thinkBox.classList.add('done'); } }
      streamed += m.delta; body.textContent = streamed;
    }
    if (stick) $('log').scrollTop = $('log').scrollHeight; else $('jump').hidden = false;
  });
  try {
    const r = await window.lab.request({action:'chat', stream:true, provider:p, messages:history, protocol:$('protocol').checked}, id);
    if (thinkBox) { thinkBox.classList.add('done'); thinkBox.querySelector('.lbl').textContent = 'Reasoning · ' + (r.thinking || thought).length.toLocaleString() + ' chars'; if (r.text) thinkBox.open = false; }
    const reply = r.text || streamed;
    if (!reply.trim() && (r.thinking || thought)) {
      // Reasoning only: keep the reasoning visible, explain, and let the user retry.
      body.classList.remove('streaming'); body.replaceChildren();
      el('p', 'notice', 'No final answer: ' + (r.notice || 'the model only returned reasoning.'), body);
      history.pop(); $('prompt').value = text; grow();
      row.querySelector('.who .meta').textContent = r.model + ' · ' + r.seconds.toFixed(1) + 's · reasoning only';
      status('Reasoning only, no answer. Your prompt is back in the box to retry.', 'err');
      return;
    }
    if (!reply.trim()) throw Error(r.cancelled ? 'Stopped before any text arrived.' : 'Empty reply');
    history.push({role:'assistant', content:reply});
    const stick = nearBottom();
    row.dataset.raw = reply; renderBody(reply, body);
    const u = r.usage || {};
    const tokens = (u.output_tokens ?? u.completion_tokens ?? u.eval_count);
    row.querySelector('.who .meta').textContent = r.model + ' · ' + r.seconds.toFixed(1) + 's' + (tokens != null ? ' · ' + tokens + ' tok' : '') + (r.cancelled ? ' · stopped' : '') + (r.truncated ? ' · cut off at output limit' : '');
    renderChecks(r.checks, body.parentElement);
    if (stick) toBottom();
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
