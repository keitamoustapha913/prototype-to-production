'use strict';
const $ = id => document.getElementById(id);
const nav = document.querySelector('nav');
let removed = {};
let state, selected, affected = [], impactChanges = [], scopeSlice = '', etag, serial = 0, diagramId = 0;
let registerAttempts = [];
let registerMetadata = {};
const renderCache = new Map();
const el = (tag, text) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
};
const storeKey = name => 'blueprint-review:' + state.root + ':' + name;
function remember() {
  if (state) localStorage.setItem(storeKey('position'), JSON.stringify({
    selected, baseline: $('baseline').value, context: $('context').value, scroll: scrollY, scopeSlice
  }));
}
async function read(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text().then(s => new DOMParser().parseFromString(s, 'text/html').body.textContent.trim()));
  return response;
}
function button(label, action) {
  const b = el('button', label);
  b.onclick = action;
  return b;
}
function choose(key, slice = '') {
  selected = key;
  scopeSlice = slice;
  $('context').value = 'Component';
  remember();
  display().catch(showError);
}
function showError(err) { $('error').textContent = err.message; }
function sectionUrls(fragment, item) {
  const registered = Object.values(state.sections);
  const base = new URL(item.source,'https://review.invalid/');
  for (const link of fragment.querySelectorAll('a,img')) {
    const raw = link.getAttribute(link.tagName === 'A' ? 'href' : 'src') || '';
    let target;
    try {
      const url = new URL(raw,base);
      if (url.origin === base.origin) target = registered.find(s => s.source === decodeURIComponent(url.pathname.slice(1)));
    } catch {}
    const label = link.textContent || link.getAttribute('alt') || raw;
    const replacement = el(target ? 'button' : 'span',label);
    if (target) replacement.dataset.reviewSection = target.id;
    link.replaceWith(replacement);
  }
}
let mermaidReady;
const diagramCache = new Map();
async function diagram(source) {
  const visible = [...document.querySelectorAll('img[data-mermaid-source]')]
    .find(image => image.dataset.mermaidSource === source);
  if (visible) return visible.cloneNode(true);
  if (!mermaidReady) mermaidReady = new Promise((resolve,reject) => {
    const script = document.createElement('script');
    script.src = '/mermaid.js';
    script.onload = () => {
      mermaid.initialize({startOnLoad:false,securityLevel:'strict',suppressErrorRendering:true});
      resolve();
    };
    script.onerror = () => {
      mermaidReady = null;
      script.remove();
      reject(new Error('Mermaid is unavailable. Run npm ci in the skill directory.'));
    };
    document.head.append(script);
  });
  await mermaidReady;
  if (!diagramCache.has(source)) {
    if (diagramCache.size >= 64) diagramCache.delete(diagramCache.keys().next().value);
    diagramCache.set(source, mermaid.render('diagram' + (++diagramId), source).then(result => result.svg));
  }
  let svg;
  try { svg = await diagramCache.get(source); }
  catch (err) { diagramCache.delete(source); throw err; }
  const img = el('img');
  img.alt = 'Mermaid diagram';
  img.dataset.mermaidSource = source;
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  return img;
}
async function illustration(s) {
  if (s.error) throw new Error(s.error);
  const key = JSON.stringify([s.kind,s.source,s.blob,s.reference]);
  if (!renderCache.has(key)) {
    if (renderCache.size >= 64) renderCache.delete(renderCache.keys().next().value);
    renderCache.set(key, (async () => {
      const response = await read('/blob/' + s.blob);
      const box = el('div');
      if (['svg','png','jpeg','jpg','webp'].includes(s.kind)) {
        const img = el('img');
        const type = s.kind === 'svg' ? 'image/svg+xml' : 'image/' + s.kind.replace('jpg','jpeg');
        const blob = new Blob([await response.blob()], {type});
        img.src = await new Promise((resolve,reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(blob);
        });
        img.alt = s.title;
        await img.decode();
        box.append(img);
      } else {
        const source = await response.text();
        if (s.kind === 'md') {
          box.append(DOMPurify.sanitize(marked.parse(source), {USE_PROFILES:{html:true}, RETURN_DOM_FRAGMENT:true,
            FORBID_TAGS:['style','form','iframe','video','audio','source','object','embed','link'], FORBID_ATTR:['style','srcset']}));
          sectionUrls(box,s);
          for (const code of box.querySelectorAll('pre > code.language-mermaid')) code.parentNode.replaceWith(await diagram(code.textContent));
        } else if (s.kind === 'mmd') box.append(await diagram(source));
        else if (s.kind === 'json') box.append(el('pre', JSON.stringify(JSON.parse(source), null, 2)));
        else {
          const frame = el('iframe');
          frame.title = s.title;
          frame.setAttribute('sandbox','');
          frame.srcdoc = '<meta http-equiv="Content-Security-Policy" content="default-src &apos;none&apos;; style-src &apos;unsafe-inline&apos;; img-src data:">' + source;
          box.append(frame);
        }
      }
      if (s.reference) {
        const group = el('div'); group.className = 'pair';
        const ref = el('div'); ref.append(el('small','Existing screen'), await illustration({...s.reference,title:'Existing screen'}));
        const proposal = el('div'); proposal.append(el('small','Proposed layout'),box);
        group.append(ref,proposal);
        return group;
      }
      return box;
    })());
  }
  try { return (await renderCache.get(key)).cloneNode(true); }
  catch (err) { renderCache.delete(key); throw err; }
}
async function panel(s, label, changed, latest) {
  const node = el('section');
  node.className = 'panel' + (changed ? ' changed' : '');
  node.append(el('h3', label));
  if (!s) { node.append(el('p','No earlier version')); return node; }
  const lastKey = storeKey('rendered:' + s.id);
  try {
    const rendered = await illustration(s);
    node.append(rendered);
    if (latest) {
      const last = JSON.parse(localStorage.getItem(lastKey) || 'null');
      if (last?.section.stamp !== s.stamp) {
        const revision = state.revision;
        // Store the rendered illustration as well as its source for recovery if a renderer fails.
        const url = '/last-render/' + encodeURIComponent(state.root) + '/' + s.id;
        try {
          const cache = await caches.open('living-review-previews');
          await cache.put(url, new Response(rendered.innerHTML));
        } catch { /* Source revisions also persist in the initiative. */ }
        localStorage.setItem(lastKey,JSON.stringify({section:s, revision, url}));
      }
    }
  } catch (err) {
    node.append(Object.assign(el('p', err.message), {className:'failure'}));
    const last = JSON.parse(localStorage.getItem(lastKey) || 'null');
    if (last) {
      node.append(el('small','Last successful preview · revision ' + last.revision));
      try {
        node.append(await illustration(last.section));
      } catch {
        const saved = last.url && await caches.match(last.url).catch(() => null);
        if (saved) {
          const frame = el('iframe');
          frame.setAttribute('sandbox','');
          frame.title = 'Last successful preview';
          frame.srcdoc = '<meta http-equiv="Content-Security-Policy" content="default-src &apos;none&apos;; img-src data:; style-src &apos;unsafe-inline&apos;; frame-src about:">' + await saved.text();
          node.append(frame);
        }
      }
    }
    if (s.blob && ['md','json','html','mmd','svg'].includes(s.kind)) node.append(el('pre', await (await read('/blob/' + s.blob)).text()));
  }
  return node;
}
function impactSummary() {
  const holder = $('impact');
  holder.replaceChildren();
  if (affected.length) {
    holder.append(el('h2','Updated sections · ' + affected.length));
    for (const item of affected) holder.append(button(item.title, () => choose(item.id)));
  }
  const impacts = new Map();
  for (const change of impactChanges) {
    const slices = new Set([...Object.keys(change.before?.impact || {}), ...Object.keys(change.after?.impact || {})]);
    for (const slice of slices) {
      const before = change.before?.impact?.[slice], after = change.after?.impact?.[slice];
      if (!impacts.has(slice)) impacts.set(slice, []);
      impacts.get(slice).push({change, before, after, slice});
    }
  }
  if (!impacts.size) return;
  holder.append(el('h2','Affected slices · ' + impacts.size));
  for (const [slice, entries] of impacts) {
    const line = el('div');
    line.append(el('strong',slice + ' · ' + (state.slice_status[slice] || 'Draft') + ' '));
    for (const {change, before, after} of entries) {
      const item = change.after || change.before;
      const same = before && after && JSON.stringify(before) === JSON.stringify(after);
      const links = same ? [['Retained',before]] : before && after ? [['Changed · before',before],['Changed · after',after]] :
        before ? [['Removed',before]] : [['Added',after]];
      for (const [label, details] of links) if (details) {
        line.append(button(label + ' · ' + details.kind + ' · ' + item.title, () => choose(item.id,slice)),
          el('small',details.reasons.join(', ')));
      }
    }
    holder.append(line);
  }
}
function reviewSectionChanged(before, after) {
  if (before === after) return false;
  return !before || !after || before.stamp !== after.stamp ||
    JSON.stringify(before.impact || {}) !== JSON.stringify(after.impact || {}) ||
    before.reason !== after.reason || before.instruction !== after.instruction;
}
function rationaleInstructionChanges(before, after) {
  return ['reason','instruction'].filter(field => before?.[field] !== after?.[field]);
}
function validChoice(select, value, fallback) {
  return [...select.options].some(option => option.value === value) ? value : fallback;
}
const navigationButtons = new Map();
function updateNavigation(sections, selectedId) {
  const ids = new Set(sections.map(section => section.id));
  for (const [id, control] of navigationButtons) {
    if (!ids.has(id)) {
      control.remove();
      navigationButtons.delete(id);
    }
  }
  let position = nav.firstElementChild;
  for (const {id, title} of sections) {
    let control = navigationButtons.get(id);
    if (!control) {
      control = button(title, () => choose(id));
      navigationButtons.set(id, control);
    }
    if (control.textContent !== title) control.textContent = title;
    const active = String(id === selectedId);
    if (control.getAttribute('aria-current') !== active) control.setAttribute('aria-current', active);
    if (control !== position) nav.insertBefore(control, position);
    position = control.nextElementSibling;
  }
}
async function display() {
  const ticket = ++serial;
  const sections = Object.values({...state.sections, ...removed});
  const item = sections.find(s => s.id === selected) || sections[0];
  updateNavigation(sections, item?.id);
  impactSummary();
  if (!item) {
    $('cards').replaceChildren(); $('tools').hidden = true;
    $('title').textContent = 'Add review sections';
    $('why').textContent = 'Review catalog · ' + state.root + '/living-review/sections.json';
    return;
  }
  $('tools').hidden = false;
  selected = item.id;
  $('title').textContent = (scopeSlice ? scopeSlice + ' / ' : '') + item.title;
  const references = [
    ...(item.features || []).map(id => 'Feature ' + id),
    ...(item.requirements || []).map(id => 'Requirement ' + id),
    ...(item.decisions || []).map(id => 'Decision ' + id)
  ];
  if (item.pending_note) references.unshift('Draft · update the correction instruction and reason before approval');
  $('why').textContent = [item.instruction, item.reason, ...references].filter(Boolean).join(' · ');
  const level = $('context').value;
  const visible = sections.filter(s => s === item || level === 'Application' ||
    level === 'Blueprint' ||
    (level === 'Slice' && (scopeSlice ? s.impact[scopeSlice] : s.slice === item.slice)) ||
    (level === 'Feature' && s.features.some(f => item.features.includes(f))));
  let outline = $('outline');
  if (!outline) { outline = el('section'); outline.id = 'outline'; $('cards').before(outline); }
  outline.replaceChildren();
  if (level === 'Application') {
    outline.append(el('h2','Application structure'));
    const groups = new Map();
    for (const s of sections) {
      const group = s.slice || 'Blueprint';
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(s);
    }
    for (const [group,items] of groups) {
      const row = el('p'); row.append(el('strong',group + ' '));
      for (const s of items) row.append(button(s.title, () => choose(s.id)));
      outline.append(row);
    }
  }
  const old = new Map([...$('cards').children].map(c => [c.dataset.id,c]));
  const cards = [];
  for (const s of visible) {
    const scope = scopeSlice && (s.id === selected || s.impact?.[scopeSlice]) ? scopeSlice : s.slice;
    const approved = state.approved_by_scope ? state.approved_by_scope[scope]?.[s.id] : state.approved[s.id];
    const receipt = state.current_by_scope ? state.current_by_scope[scope]?.[s.id] : state.current[s.id];
    const binding = state.current_by_scope ? state.visual_review?.[scope]?.[s.id] : s.stamp;
    const status = s.removed ? 'Removed' :
      state.slice_status[scope] === 'Needs review' ? 'Needs review' :
      binding && receipt === binding ? 'Approved' : approved ? 'Needs review' : 'Draft';
    const signature = JSON.stringify([s, status, approved, $('baseline').value, s.id === selected]);
    let card = old.get(s.id);
    if (!card || card.dataset.signature !== signature) {
      card = el('article'); card.dataset.id = s.id; card.dataset.signature = signature;
      card.append(el('h2',s.title + ' · ' + status), el('small',state.root + '/' + s.source));
      const pair = el('div'); pair.className = 'pair';
      let prior;
      if (s.id === selected) {
        if ($('baseline').value === 'approved') {
          if (approved) {
            prior = await (await read('/approved/' + s.id + '/' + approved)).json();
            if (prior) prior.label = 'Last approved · ' + approved.slice(0,10);
          }
        } else {
          prior = await (await read('/previous/' + s.id + '/' + state.revision)).json();
          if (prior) prior.label = 'Before · revision ' + prior.revision;
        }
      }
      if (s.removed && !prior) prior = {section:s, label:'Before removal'};
      if (prior?.section) pair.append(await panel(prior.section, prior.label, false, false));
      pair.append(s.removed ? el('p','Removed in revision ' + s.removal_revision) : await panel(s,prior?.section ? 'After · revision ' + state.revision : 'Current', !!prior?.section,true));
      card.append(pair);
      if (prior?.section?.blob && s.blob && !s.removed && ['md','json','mmd','svg','html'].includes(s.kind)) {
        const changes = await sourceChanges(prior.section,s);
        if (changes) card.append(changes);
      }
      if (prior?.section) {
        const notes = el('details');
        for (const field of rationaleInstructionChanges(prior.section,s)) {
          if (!notes.firstChild) notes.append(el('summary','Changed rationale or instruction'));
          notes.append(el('p','Before · ' + field + ': ' + (prior.section[field] || '')), el('p','After · ' + field + ': ' + (s[field] || '')));
        }
        if (notes.firstChild) card.append(notes);
      }
    }
    cards.push(card);
  }
  if (ticket !== serial) return;
  const holder = $('cards');
  for (const c of [...holder.children]) if (!cards.includes(c)) c.remove();
  cards.forEach((c,i) => { if (holder.children[i] !== c) holder.insertBefore(c, holder.children[i] || null); });
}
async function update() {
  try {
    const headers = etag ? {'If-None-Match':etag} : {};
    if (state) headers['X-Review-Delta'] = '1';
    const res = await fetch('/state',{headers,cache:'no-store'});
    if (res.status === 304) return;
    if (!res.ok) throw new Error(new DOMParser().parseFromString(await res.text(),'text/html').body.textContent.trim());
    const body = await res.json(), prev = state;
    const delta = prev && res.headers.get('X-Review-Delta') === '1' ? body : null;
    const next = delta ? mergeState(prev, delta) : body;
    state = next;
    let position = scrollY;
    if (!prev) {
      const saved = JSON.parse(localStorage.getItem(storeKey('position')) || '{}');
      selected = saved.selected; scopeSlice = saved.scopeSlice || '';
      $('baseline').value = validChoice($('baseline'), saved.baseline, 'previous');
      $('context').value = validChoice($('context'), saved.context, 'Component');
      position = saved.scroll ?? 0;
    }
    const previous = prev?.sections || await (await read('/revision/' + Math.max(0,next.revision-1))).json();
    if (!prev || next.revision !== prev.revision || JSON.stringify(next.removals) !== JSON.stringify(prev.removals)) {
      removed = {};
      const changedRemovals = [];
      for (const [id, record] of Object.entries(next.removals || {})) {
        if (!record.active) continue;
        const item = {...record.section, removed:true, removal_revision:record.detected_revision,
          instruction:record.instruction, reason:record.reason, correction_id:record.correction_id,
          decisions:record.decisions, pending_note:record.note_pending, removal_stamp:record.stamp};
        removed[id] = item;
        const changed = prev ? prev.removals?.[id]?.stamp !== record.stamp :
          (record.updated_revision || record.detected_revision) === next.revision;
        if (changed) changedRemovals.push(item);
      }
      const sectionPatch = delta?.maps?.sections;
      const candidates = delta ? [...Object.keys(sectionPatch?.set || {}), ...Object.keys(sectionPatch?.children || {})]
        .map(id => next.sections[id]) : Object.values(next.sections);
      affected = (next.revision > 1 ? candidates
        .filter(s => reviewSectionChanged(previous[s.id],s)) : []).concat(changedRemovals);
      impactChanges = affected.map(item => ({
        before: item.removed ? next.removals[item.id].section : previous[item.id],
        after: item.removed ? undefined : next.sections[item.id]
      }));
      if (affected.length && (prev || !selected)) {
        const belongsToScope = s => s && (s.slice === scopeSlice || s.impact?.[scopeSlice]);
        const currentSections = {...next.sections, ...removed};
        if (scopeSlice) {
          const inScope = affected.find(s => s.id === selected && belongsToScope(s)) || affected.find(belongsToScope);
          if (inScope) selected = inScope.id;
          else if (!currentSections[selected]) {
            selected = affected[0].id;
            if (!belongsToScope(currentSections[selected])) scopeSlice = '';
          } else if (!belongsToScope(currentSections[selected])) scopeSlice = '';
        } else {
          selected = affected.find(s => s.id === selected)?.id || affected[0].id;
        }
      }
      if (scopeSlice && !next.slice_status?.[scopeSlice] &&
          !Object.values(next.sections).some(s => s.slice === scopeSlice || s.impact?.[scopeSlice])) scopeSlice = '';
      $('baseline').value = validChoice($('baseline'), $('baseline').value, 'previous');
      $('context').value = validChoice($('context'), $('context').value, 'Component');
    }
    $('revision').textContent = 'Revision ' + state.revision;
    $('error').textContent = '';
    const renderPosition = scrollY;
    await display();
    setRegisterMode(!$('visual-register').hidden);
    if ((!prev || next.revision !== prev.revision) && scrollY === renderPosition) scrollTo(0,position);
    etag = res.headers.get('ETag');
    remember();
  } catch (err) { showError(err); etag = null; }
  finally { setTimeout(update,750); }
}

function registerImage(path, alt, rect, captionText = alt) {
  if (typeof path !== 'string' || !path) return el('span', 'Screenshot unavailable');
  const figure = el('figure');
  figure.className = 'register-image';
  const stage = el('div'); stage.className = 'register-image-stage';
  const img = el('img');
  const src = evidenceUrl(path);
  img.src = src;
  img.alt = alt;
  img.loading = 'lazy';
  img.onerror = () => { stage.replaceChildren(el('span', 'Screenshot unavailable')); };
  stage.append(img);
  if (rect && [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite)) {
    const mark = el('span');
    mark.className = 'register-region';
    mark.setAttribute('aria-label', 'Annotated region');
    mark.style.left = `${Math.max(0, Math.min(1, rect.x)) * 100}%`;
    mark.style.top = `${Math.max(0, Math.min(1, rect.y)) * 100}%`;
    mark.style.width = `${Math.max(0, Math.min(1, rect.width)) * 100}%`;
    mark.style.height = `${Math.max(0, Math.min(1, rect.height)) * 100}%`;
    stage.append(mark);
  }
  figure.append(stage);
  const caption = el('figcaption');
  caption.append(el('strong', captionText + ' · '));
  const open = el('a', 'Open original at full resolution');
  open.href = src;
  open.target = '_blank';
  open.rel = 'noopener noreferrer';
  caption.append(open);
  figure.append(caption);
  return figure;
}
function evidenceUrl(path) { return '/visual-integrity/evidence/' + encodeURIComponent(path); }
function annotatedEvidenceLinks(comparison) {
  const links = el('p'); links.className = 'register-annotated-links';
  for (const [field, label] of [['annotated_before','marked Before'],['annotated_latest','marked Latest']]) {
    const path = comparison[field];
    if (typeof path !== 'string' || !path) continue;
    const open = el('a', `Open ${label}`); open.href = evidenceUrl(path); open.target = '_blank'; open.rel = 'noopener noreferrer';
    const download = el('a', `Download ${label}`); download.href = evidenceUrl(path); download.download = path.split('/').at(-1);
    links.append(open, document.createTextNode(' · '), download);
  }
  return links;
}
function attemptText(attempt) {
  return JSON.stringify([attempt.slice_id, attempt.status, attempt.attempt_id, attempt.observations,
    attempt.findings, attempt.cases, attempt.coverage, attempt.protected_visual_decisions]).toLowerCase();
}
function summarizeVisualRegister(attempts, slices = {}) {
  const findings = new Map();
  const latestAttempts = new Map();
  const cleanAttempts = [];
  const incompleteAttempts = [];
  for (const attempt of attempts) {
    const latest = latestAttempts.get(attempt.slice_id);
    if (!latest || attempt.attempt_number >= latest.attempt_number) latestAttempts.set(attempt.slice_id, attempt);
    if (attempt.status === 'unresolved' && (attempt.findings || []).every(finding => finding.status === 'noted')) {
      incompleteAttempts.push(attempt);
    }
    if (attempt.status === 'clean') cleanAttempts.push(attempt);
    for (const finding of attempt.findings || []) {
      const id = finding.finding_id;
      if (typeof id !== 'string' || !id) continue;
      const context = finding.case_id || finding.dimension || '';
      const key = `${attempt.slice_id}\0${context}\0${id}`;
      if (!findings.has(key)) findings.set(key, {slice_id:attempt.slice_id, finding_id:id, finding,
        status:'open', latest_attempt:attempt, history:[]});
      const current = findings.get(key);
      current.finding = finding;
      current.latest_attempt = attempt;
      current.status = attempt.status === 'corrected' && attempt.rechecked_findings?.includes(id)
        ? 'corrected' : {improved: 'improved', noted: 'noted'}[finding.status] || 'open';
      current.history.push({attempt, finding});
    }
  }
  const coverageAttempts = incompleteAttempts.map(attempt => {
    const assignment = slices[attempt.slice_id];
    const retired = assignment?.status === 'cancelled' || Boolean(assignment?.superseded_by);
    return {attempt, historical:retired || latestAttempts.get(attempt.slice_id) !== attempt};
  });
  return {findings:[...findings.values()], cleanAttempts, incompleteAttempts:coverageAttempts};
}
const findingLabels = {
  open: 'Unresolved', corrected: 'Verified fixed', improved: 'Improved — not verified', noted: 'For review'
};
const classificationLabels = {
  approved_difference: 'Approved design difference', confirmed_defect: 'Confirmed implementation defect',
  design_concern: 'Potential design concern', approval_conflict: 'Approval or requirements conflict'
};
function appendCoverage(holder, attempt) {
  if (!attempt.coverage?.length) return;
  const details = el('details');
  details.append(el('summary', 'Visual QA coverage and protected decisions'));
  for (const row of attempt.coverage) {
    details.append(el('h4', [row.component, row.state, row.case_id, row.viewport && `${row.viewport.width}×${row.viewport.height}`].filter(Boolean).join(' · ')), el('p', row.reason));
    const list = el('ul');
    for (const [kind, check] of Object.entries(row.checks)) {
      list.append(el('li', [kind, check.result.replaceAll('_', ' '), check.note].join(' · ')));
    }
    if (row.acceptance_ids?.length) list.append(el('li', 'Acceptance checks · ' + row.acceptance_ids.join(', ')));
    if (row.protected_decision_ids?.length) {
      list.append(el('li', 'Protected decisions · ' + row.protected_decision_ids.join(', ')));
    }
    details.append(list);
  }
  for (const decision of attempt.protected_visual_decisions || []) {
    details.append(el('h4', `${decision.decision_id} · ${decision.component} · ${decision.property}`),
      el('p', decision.requirement), el('p', decision.purpose), el('p', decision.verification));
    if (decision.exchange_ids?.length) details.append(el('p', 'A+ decisions · ' + decision.exchange_ids.join(', ')));
  }
  holder.append(details);
}
function registerCase(item, finding = null) {
  const section = el('section'); section.className = 'register-case';
  const details = el('p');
  details.textContent = [item.case_id, item.state?.route,
    item.viewport ? `${item.viewport.width}×${item.viewport.height}` : '',
    item.state?.state_setup ? JSON.stringify(item.state.state_setup) : item.state?.interaction]
    .filter(Boolean).join(' · ');
  if (details.textContent) section.append(el('h3', item.case_id || 'Captured state'), details);
  if (item.browser_evidence) {
    const measurements = item.browser_evidence.blueprint_measurements;
    if (measurements?.length) {
      const list = el('ul');
      for (const measurement of measurements) {
        const status = measurement.actual === measurement.expected ? 'Pass' : 'Fail';
        list.append(el('li', `${measurement.check_id} · ${status} · Expected ${measurement.expected} · Observed ${measurement.actual ?? 'unavailable'}`));
      }
      section.append(el('h4', 'Approved visual measurements'), list);
    }
    const evidence = el('details');
    evidence.append(el('summary', 'Capture state and browser measurements'),
      el('pre', JSON.stringify({context: item.state, ...item.browser_evidence}, null, 2)));
    section.append(evidence);
  }
  const shots = el('div'); shots.className = 'register-shot-grid';
  if (item.full_screenshot) shots.append(registerImage(item.full_screenshot, 'Full screenshot'));
  if (item.closeup_screenshot) shots.append(registerImage(item.closeup_screenshot, 'Close-up screenshot'));
  if (shots.childNodes.length) section.append(shots);
  const comparisons = (item.comparisons || []).filter(comparison => !finding ||
    item.case_id === finding.case_id && comparison.finding_id === finding.finding_id);
  for (const comparison of comparisons) {
    const pair = el('div'); pair.className = 'register-shot-grid register-comparison';
    pair.append(registerImage(comparison.before, 'Before screenshot', comparison.normalized_rect, 'Before'),
      registerImage(comparison.latest, 'Latest screenshot', comparison.latest_rect || comparison.normalized_rect, 'Latest'));
    const comparisonCard = el('section');
    comparisonCard.append(el('h4', comparison.label || 'Before / latest comparison'), pair);
    if (comparison.annotated_before || comparison.annotated_latest) comparisonCard.append(annotatedEvidenceLinks(comparison));
    section.append(comparisonCard);
  }
  return section;
}
function appendFindingNotes(holder, finding) {
  const classification = classificationLabels[finding?.classification];
  if (classification) holder.append(el('p', classification));
  if (finding?.protected_decision_ids?.length) {
    holder.append(el('p', 'Protected decisions · ' + finding.protected_decision_ids.join(', ')));
  }
  for (const field of ['classification_reason','evidence','repair_scope','diagnosis','correction','verification']) {
    const value = finding?.[field];
    if (typeof value === 'string' && value.trim()) {
      holder.append(el('p', `${field[0].toUpperCase() + field.slice(1).replaceAll('_', ' ')}: ${value}`));
    }
  }
}
function registerAttempt(attempt, label, finding) {
  const article = el('article'); article.className = 'register-history-attempt status-' + attempt.status;
  article.append(el('h4', `${label || 'Attempt'} ${attempt.attempt_number} · ${attempt.status}`),
    el('small', `${attempt.attempt_id} · ${attempt.recorded_at || ''}`));
  if (finding) {
    article.append(el('p', finding.description || 'Finding details were not supplied.'));
    appendFindingNotes(article, finding);
  }
  appendCoverage(article, attempt);
  for (const item of attempt.cases || []) article.append(registerCase(item, finding));
  return article;
}
function latestFindingComparison(item) {
  for (const occurrence of [...item.history].reverse()) {
    for (const reviewedCase of occurrence.attempt.cases || []) {
      if (reviewedCase.case_id !== item.finding.case_id) continue;
      const comparison = (reviewedCase.comparisons || []).find(entry => entry.finding_id === item.finding_id);
      if (comparison) return {attempt:occurrence.attempt, case:reviewedCase, comparison};
    }
  }
  return null;
}
function renderVisualRegister(attempts = registerAttempts) {
  const entries = $('register-entries');
  const statusFilter = $('register-status').value;
  const search = $('register-search').value.trim().toLowerCase();
  const slices = [...new Set(attempts.map(a => a.slice_id).filter(x => typeof x === 'string'))].sort();
  const select = $('register-slice');
  const selectedSlice = select.value;
  const allSlices = el('option', 'All slices'); allSlices.value = '';
  select.replaceChildren(allSlices);
  for (const slice of slices) { const option = el('option', slice); option.value = slice; select.add(option); }
  select.value = slices.includes(selectedSlice) ? selectedSlice : '';
  const summary = summarizeVisualRegister(attempts, registerMetadata.slices);
  const openCount = summary.findings.filter(f => f.status === 'open').length;
  const correctedCount = summary.findings.filter(f => f.status === 'corrected').length;
  $('register-counts').textContent = `${attempts.length} attempts · ${summary.cleanAttempts.length} clean evidence records · ${summary.findings.length} distinct findings (${openCount} open, ${correctedCount} corrected) · ${slices.length} slices`;
  const projectStatus = $('register-project-status');
  projectStatus.replaceChildren();
  for (const warning of registerMetadata.history_warnings || []) {
    const message = el('p', warning); message.setAttribute('role', 'alert'); projectStatus.append(message);
  }
  if (registerMetadata.next_action) projectStatus.append(el('p', 'Project next action: ' + registerMetadata.next_action));
  const independent = Object.entries(registerMetadata.slices || {}).filter(([, item]) => item.independent);
  if (independent.length) {
    const list = el('ul');
    for (const [slice, item] of independent) {
      list.append(el('li', `Independent slice ${slice} · ${item.status || 'status unavailable'}${item.blockers?.length ? ' · ' + item.blockers.join('; ') : ''}`));
    }
    const section = el('section'); section.append(el('h3', 'Independent slice status'), list);
    projectStatus.append(section);
  }
  const candidates = [
    ...summary.findings.filter(f => !statusFilter || f.status === statusFilter).map(f => ({kind:'finding',value:f})),
    ...summary.cleanAttempts.filter(() => !statusFilter || statusFilter === 'clean').map(a => ({kind:'clean',value:a})),
    ...summary.incompleteAttempts.filter(a => !statusFilter || (statusFilter === 'open' && !a.historical))
      .map(a => ({kind:'incomplete',value:a.attempt,historical:a.historical}))
  ].filter(entry => (!select.value || entry.value.slice_id === select.value) &&
    (!search || (entry.kind === 'finding'
      ? JSON.stringify([entry.value.slice_id,entry.value.finding_id,entry.value.finding,entry.value.history]).toLowerCase().includes(search)
      : attemptText(entry.value).includes(search))));
  const visible = candidates.reverse();
  entries.replaceChildren();
  if (!visible.length) { entries.append(el('p', 'No current findings or clean evidence match these filters.')); return; }
  for (const entry of visible) {
    if (entry.kind !== 'finding') {
      const attempt = entry.value;
      const card = el('article'); card.className = 'register-attempt status-' + (entry.historical ? 'historical' : attempt.status);
      const label = entry.kind === 'clean' ? 'Clean evidence' :
        entry.historical ? 'Past unresolved coverage' : 'Unresolved coverage';
      card.append(el('h2', `${attempt.slice_id} · ${label} · Attempt ${attempt.attempt_number}`),
        el('small', `${attempt.attempt_id} · ${attempt.recorded_at || ''}`));
      for (const observation of attempt.observations || []) {
        const text = typeof observation === 'string' ? observation :
          [observation.dimension, observation.result || observation.status, observation.note || observation.observation]
            .filter(Boolean).join(' · ') || JSON.stringify(observation);
        card.append(el('p', text));
      }
      appendCoverage(card, attempt);
      for (const item of attempt.cases || []) card.append(registerCase(item));
      entries.append(card);
      continue;
    }
    const item = entry.value, finding = item.finding;
    const card = el('article'); card.className = 'register-finding status-' + item.status;
    card.append(el('h2', `${item.slice_id} · ${finding.case_id || finding.dimension || 'Finding'} · ${finding.severity || 'Finding'} · ${item.finding_id} · ${findingLabels[item.status]}`));
    card.append(el('p', finding.description || finding.summary || finding.observed || 'Finding details were not supplied.'));
    appendFindingNotes(card, finding);
    const dependencies = Array.isArray(finding.dependency_ids) ? finding.dependency_ids :
      Array.isArray(finding.dependencies) ? finding.dependencies : [];
    const meta = [finding.expected && `Expected: ${finding.expected}`, finding.observed && `Observed: ${finding.observed}`,
      finding.remaining_defect && `Remaining defect: ${finding.remaining_defect}`,
      finding.recheck && `Recheck: ${finding.recheck}`,
      dependencies.length && `Dependencies: ${dependencies.join(', ')}`,
      item.status === 'corrected' ? 'Verified by Codex · Optional user review' :
        (finding.optional_review_label || (finding.optional_review ? 'Optional review' : ''))].filter(Boolean);
    if (meta.length) card.append(el('p', meta.join(' · ')));
    const retryPolicy = item.latest_attempt.retry_policy || {};
    const failedRepairs = retryPolicy.failed_repairs?.[item.finding_id];
    if (Number.isInteger(failedRepairs) && failedRepairs > 0) {
      card.append(el('p', `Unsuccessful repair attempts: ${failedRepairs}.`));
    }
    if (retryPolicy.automatic_repair_stopped?.includes(item.finding_id)) {
      card.append(el('p', 'Automatic retries stopped; next step: ' +
        (retryPolicy.next_step || 'fresh investigation with a new diagnosis') + '.'));
    }
    const dependents = item.latest_attempt.dependent_slices || [];
    if (dependents.length) card.append(el('p', 'Dependent slices affected at this inspection: ' + dependents.join(', ') + '.'));
    const latestComparison = latestFindingComparison(item);
    if (latestComparison) {
      const comparisonCard = el('section'); comparisonCard.className = 'register-current-comparison';
      comparisonCard.append(el('h3', `Latest repair comparison · ${latestComparison.case.case_id} · Attempt ${latestComparison.attempt.attempt_number}`));
      const pair = el('div'); pair.className = 'register-shot-grid register-comparison';
      pair.append(registerImage(latestComparison.comparison.before, 'Before screenshot', latestComparison.comparison.normalized_rect, 'Before'),
        registerImage(latestComparison.comparison.latest, 'Latest screenshot', latestComparison.comparison.latest_rect || latestComparison.comparison.normalized_rect, 'Latest'));
      comparisonCard.append(pair);
      if (latestComparison.comparison.annotated_before || latestComparison.comparison.annotated_latest) {
        comparisonCard.append(annotatedEvidenceLinks(latestComparison.comparison));
      }
      card.append(comparisonCard);
    }
    const history = el('details');
    history.append(el('summary', `Attempt history · ${item.history.length} finding records`));
    for (const occurrence of item.history) history.append(registerAttempt(occurrence.attempt,
      `Finding ${occurrence.finding.status || occurrence.attempt.status}`, occurrence.finding));
    card.append(history);
    entries.append(card);
  }
}
async function loadVisualRegister() {
  const response = await read('/visual-integrity');
  const result = await response.json();
  registerAttempts = Array.isArray(result) ? result : result.attempts || [];
  registerMetadata = result.project || {};
  renderVisualRegister();
}
function mergeState(previous, delta) {
  const next = {...previous,...delta.fields};
  function applyMap(before, patch) {
    const value = {...before,...patch.set};
    for (const key of patch.delete) delete value[key];
    for (const [key, child] of Object.entries(patch.children)) value[key] = applyMap(value[key] || {},child);
    return value;
  }
  for (const [key, patch] of Object.entries(delta.maps)) next[key] = applyMap(previous[key] || {},patch);
  return next;
}
$('register-toggle').onclick = () => {
  const panel = $('visual-register');
  panel.hidden = !panel.hidden;
  $('register-toggle').setAttribute('aria-expanded', String(!panel.hidden));
  setRegisterMode(!panel.hidden);
  if (!panel.hidden) loadVisualRegister().catch(showError);
};
function setRegisterMode(show) {
  const panel = $('visual-register');
  panel.hidden = !show;
  $('title').hidden = !panel.hidden;
  $('impact').hidden = !panel.hidden;
  $('why').hidden = !panel.hidden;
  $('tools').hidden = !panel.hidden;
  $('cards').hidden = !panel.hidden;
  const outline = $('outline');
  if (outline) outline.hidden = !panel.hidden;
}
$('register-slice').onchange = () => renderVisualRegister();
$('register-status').onchange = () => renderVisualRegister();
$('register-search').oninput = () => renderVisualRegister();
$('register-refresh').onclick = () => loadVisualRegister().catch(showError);
$('cards').addEventListener('click', event => {
  const link = event.target.closest('button[data-review-section]');
  if (link) choose(link.dataset.reviewSection);
});
for (const id of ['context','baseline']) $(id).onchange = () => { remember(); display().catch(showError); };
let scrollTimer;
window.addEventListener('scroll',() => {
  clearTimeout(scrollTimer);
  scrollTimer = setTimeout(remember,150);
}, {passive:true});
window.addEventListener('pagehide',remember);
update();
