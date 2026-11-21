'use strict';
const $ = id => document.getElementById(id);
const nav = document.querySelector('nav');
let removed = {};
let state, selected, affected = [], impactChanges = [], scopeSlice = '', etag, serial = 0, diagramId = 0;
let registerAttempts = [];
let registerMetadata = {};
let reviewRequest = 0;
let completionSignature;
let selectedCompletionAudit;
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
  setReviewMode('');
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
    if (next.completion_report && (!prev || next.completion_report !== prev.completion_report ||
        !$('completion-review')?.hidden || !$('visual-register').hidden) && $('completion-review')) {
      loadVisualRegister().then(() => {
        if (!prev && location.hash === '#final-review' && registerMetadata.final_review) setReviewMode('completion');
      }).catch(showError);
    }
    updateReviewVisibility();
    if ((!prev || next.revision !== prev.revision) && scrollY === renderPosition) scrollTo(0,position);
    etag = res.headers.get('ETag');
    remember();
  } catch (err) { showError(err); etag = null; }
  finally { setTimeout(update,750); }
}

function registerImage(path, alt, rect, captionText = alt) {
  if (typeof path !== 'string' || !path) return el('span', 'Screenshot unavailable');
  return registerImageFromUrl(evidenceUrl(path), alt, rect, captionText);
}
function registerImageFromUrl(src, alt, rect, captionText = alt) {
  const figure = el('figure');
  figure.className = 'register-image';
  const stage = el('div'); stage.className = 'register-image-stage';
  const img = el('img');
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
    if (attempt.status === 'unresolved' && (attempt.findings || []).every(finding => ['noted', 'deferred'].includes(finding.status))) {
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
        ? 'corrected' : {improved: 'improved', noted: 'noted', deferred: 'warning'}[finding.status] || 'open';
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
  open: 'Unresolved', corrected: 'Verified fixed', improved: 'Improved — not verified', noted: 'For review', warning: 'Documented warning'
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
    for (const [kind, check] of Object.entries({...row.checks, ...(row.accessibility && {accessibility: row.accessibility})})) {
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
  for (const field of ['classification_reason','evidence','nonblocking_reason','repair_scope','diagnosis','correction','verification']) {
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
function crossScreenCard(change) {
  const card = el('article');
  const status = change.current_status || change.status;
  card.className = 'register-attempt status-' + ({verified:'clean', historical:'historical', abandoned:'historical'}[status] || 'unresolved');
  card.append(el('h2', `${change.summary} · ${status.replaceAll('_', ' ')}`),
    el('p', `Cross-screen change ${change.change_id} · Originating slice ${change.slice_id}`),
    el('p', `Shared source · ${change.component_paths.join(', ')}`),
    el('p', `Affected and dependent slices · ${change.affected_slices.join(', ')}`));
  if (change.abandonment_reason) card.append(el('p', change.abandonment_reason));
  const latest = change.attempts.at(-1);
  for (const screen of change.screens) {
    const section = el('section'); section.className = 'register-case';
    section.append(el('h3', screen.route), el('p', screen.reason),
      el('p', screen.slice_ids.join(', ') || 'Existing unassigned screen'));
    for (const spec of screen.cases) {
      const row = latest?.cases.find(item => item.case_id === spec.case_id);
      const state = spec.state.interaction;
      const {width, height} = spec.renderCase.viewport;
      const detail = el('details');
      const caseStatus = ['stale', 'evidence unavailable'].includes(status) ? 'not_verified' : row?.status || 'not_verified';
      detail.append(el('summary', `${spec.case_id} · ${state} · ${width}×${height} · ${caseStatus.replaceAll('_', ' ')}`),
        el('p', spec.reason), el('p', spec.expectation));
      if (spec.approved_case) detail.append(el('p', 'Approved case · ' + spec.approved_case));
      const original = change.baseline_context?.[spec.case_id];
      if (original?.baseline_id) detail.append(el('p', 'Discovery baseline · ' + original.baseline_id));
      if (original?.revision) detail.append(el('p', 'Original source revision · ' + original.revision));
      if (spec.baseline_comparison_note) detail.append(el('p', spec.baseline_comparison_note));
      const gap = change.baseline_readiness?.gaps[spec.case_id];
      if (gap) detail.append(el('p', gap));
      for (const decision of spec.protections || []) {
        detail.append(el('p', [decision.decision_id, decision.requirement].filter(Boolean).join(' · ')));
      }
      const history = [...change.attempts].reverse();
      if (!history.length && change.baseline_cases[spec.case_id]) {
        detail.append(registerCase(change.baseline_cases[spec.case_id]));
      }
      for (const attempt of history) {
        const result = attempt.cases.find(item => item.case_id === spec.case_id);
        if (!result) continue;
        const evidence = el('details');
        evidence.open = attempt === latest;
        evidence.append(el('summary', `${attempt.recorded_at} · ${result.status.replaceAll('_', ' ')}`));
        appendCrossScreenResult(evidence, result, attempt.findings, attempt.retry_policy);
        detail.append(evidence);
      }
      section.append(detail);
    }
    card.append(section);
  }
  return card;
}
function appendCrossScreenResult(holder, result, findings, retryPolicy) {
  if (result.unavailable_reason) holder.append(el('p', result.unavailable_reason));
  const beforeBrowser = result.before?.browser_evidence?.browser_version?.product;
  const afterBrowser = result.after?.browser_evidence?.browser_version?.product;
  if (beforeBrowser && afterBrowser && beforeBrowser !== afterBrowser) {
    holder.append(el('p', `Capture browser changed · ${beforeBrowser} → ${afterBrowser}`));
  }
  const checks = el('ul');
  for (const [kind, check] of Object.entries({...result.checks, ...(result.accessibility && {accessibility: result.accessibility})})) {
    checks.append(el('li', `${kind} · ${check.result.replaceAll('_', ' ')} · ${check.note}`));
  }
  holder.append(checks);
  for (const finding of findings.filter(item => item.case_id === result.case_id)) {
    holder.append(el('h4', `${finding.finding_id} · ${finding.severity} · ${finding.status}`), el('p', finding.description));
    appendFindingNotes(holder, finding);
    if (finding.comparison) {
      const comparison = finding.comparison;
      const pair = el('div'); pair.className = 'register-shot-grid register-comparison';
      if (comparison.before) pair.append(registerImage(comparison.before, 'Defective component', comparison.normalized_rect));
      pair.append(registerImage(comparison.latest, finding.status === 'fixed' ? 'Corrected component' : 'Warning evidence',
        comparison.normalized_rect));
      holder.append(pair);
      if (comparison.annotated_latest) holder.append(annotatedEvidenceLinks(comparison));
    }
    if (retryPolicy?.automatic_repair_stopped?.includes(finding.finding_id)) {
      holder.append(el('p', 'Automatic retries stopped. Record a new diagnosis to resume repair.'));
    }
  }
  for (const [index, comparison] of result.comparisons.entries()) {
    holder.append(el('h4', `${index === 0 ? 'Full screen' : 'Component'} · ${comparison.pixels_changed ? 'Changed pixels marked' : 'Identical pixels'}`));
    const grid = el('div'); grid.className = 'register-shot-grid register-comparison';
    grid.append(registerImage(comparison.before, 'Original', comparison.normalized_rect),
      registerImage(comparison.latest, 'Updated', comparison.normalized_rect));
    holder.append(grid);
    if (comparison.annotated_before) holder.append(annotatedEvidenceLinks(comparison));
  }
  for (const [label, capture, build] of [['Original capture', result.before, result.before_build],
    ['Updated capture', result.after, result.after_build], ['Keyboard evidence', result.accessibility_capture, result.after_build]]) {
    if (!capture) continue;
    const evidence = el('details');
    evidence.append(el('summary', label));
    if (build) evidence.append(el('p', 'Served build · ' + build));
    if (!result.comparisons.length || capture === result.accessibility_capture) evidence.append(registerCase(capture));
    else evidence.append(el('pre', JSON.stringify({context:capture.state, ...capture.browser_evidence}, null, 2)));
    holder.append(evidence);
  }
}
function visualBaselineCard(baseline, approvalId) {
  const {definition, original, gap, approved, actual} = baseline;
  const card = el('article');
  card.className = 'register-attempt' + (baseline.available ? '' : ' status-unresolved');
  card.append(el('h2', `${definition.baseline_id} · ${definition.state.route}`),
    el('p', definition.reason), el('p', `Discovery features · ${definition.baseline_feature_ids.join(', ')}`),
    el('p', `Source · ${definition.source_paths.join(', ')}`));
  if (baseline.slice_ids.length) card.append(el('p', `Slices · ${baseline.slice_ids.join(', ')}`));
  const dependent = (baseline.affected_slice_ids || []).filter(slice => !baseline.slice_ids.includes(slice));
  if (dependent.length) card.append(el('p', `Dependent slices · ${dependent.join(', ')}`));
  if (gap) card.append(el('p', `${baseline.available ? 'Recovered gap' : 'Original unavailable'} · ${gap.reason}`));
  if (baseline.evidence_error) card.append(el('p', baseline.evidence_error));

  if (original && baseline.available) card.append(baselineCapture('Original UI', original));
  const captureRows = [...actual].reverse().filter(capture => !approvalId || capture.approval_id === approvalId);
  if (approvalId) {
    const grid = el('div');
    grid.className = 'completion-comparison-grid';
    const reference = approved[approvalId];
    const latest = captureRows[0];
    const {width, height} = definition.renderCase?.viewport || {};
    const design = reference?.prototype_cases?.find(row => row.reference_file &&
      row.viewport?.width === width && row.viewport?.height === height &&
      (!latest || row.case_id === latest.case.case_id || row.case_id === latest.case.based_on));
    grid.append(registerImage(baseline.available && original?.case.full_screenshot, 'Original UI'));
    if (design) grid.append(registerImageFromUrl(
      '/visual-integrity/design/' + encodeURIComponent(approvalId) + '/' + encodeURIComponent(design.case_id), 'Approved design'));
    else grid.append(el('p', reference ? 'Approved design is recorded in the specifications below.' : 'Approved design reference unavailable.'));
    grid.append(registerImage(latest?.case.full_screenshot, 'Implemented UI'));
    card.append(grid);
  }
  for (const [id, reference] of Object.entries(approved)) {
    if (approvalId && id !== approvalId) continue;
    const section = el('details');
    section.append(el('summary', `Approved design · ${reference.version || id}`),
      el('pre', JSON.stringify(reference, null, 2)));
    const {width, height} = definition.renderCase?.viewport || {};
    for (const design of reference.prototype_cases || []) {
      if (design.reference_file && design.viewport?.width === width && design.viewport?.height === height) {
        const url = '/visual-integrity/design/' + encodeURIComponent(id) + '/' + encodeURIComponent(design.case_id);
        section.append(registerImageFromUrl(url, `Approved design · ${design.case_id}`));
      }
    }
    card.append(section);
  }
  for (const capture of captureRows) {
    const section = baselineCapture(`Implemented UI · ${capture.slice_id}`, capture);
    if (original && baseline.available) {
      const shots = el('div');
      shots.className = 'register-shot-grid register-comparison';
      shots.append(registerImage(original.case.full_screenshot, 'Original UI'),
        registerImage(capture.case.full_screenshot, 'Implemented UI'));
      section.append(shots);
    }
    card.append(section);
  }
  return card;
}
function baselineCapture(label, receipt) {
  const section = el('details');
  section.append(el('summary', `${label} · ${receipt.captured_at}`));
  if (receipt.revision) section.append(el('p', 'Source revision · ' + receipt.revision));
  if (receipt.baseline_comparison_note) section.append(el('p', receipt.baseline_comparison_note));
  section.append(registerCase(receipt.case));
  const provenance = el('details');
  provenance.append(el('summary', 'Served build and source'),
    el('pre', JSON.stringify({build: receipt.build, approval: receipt.approval_id, sources: receipt.sources,
      served: receipt.served, url: receipt.url}, null, 2)));
  section.append(provenance);
  return section;
}
const qualityLabels = {
  in_implementation: 'In implementation',
  blocked: 'Blocked',
  technically_complete: 'Technically complete',
  technically_complete_with_warnings: 'Technically complete — with warnings',
  finally_accepted: 'Finally accepted',
  verified: 'Verified',
  verified_with_warnings: 'Verified with warnings',
  needs_correction: 'Needs correction',
  not_verified: 'Not verified',
  pass: 'Passed',
  fail: 'Failed',
};
function openRegisterEvidence(slice = '', key = '', filter = '') {
  setReviewMode('register');
  $('register-slice').value = slice;
  $('register-status').value = filter;
  $('register-search').value = key;
  renderVisualRegister();
  $('register-entries').tabIndex = -1;
  $('register-entries').focus();
}
function qualityEvidenceButton(slice, key, filter = '') {
  const button = el('button', 'View evidence');
  button.type = 'button';
  button.onclick = () => openRegisterEvidence(slice, key || '', filter);
  return button;
}
function renderQualityReport(quality) {
  const panel = $('visual-quality-report');
  if (!panel) return;
  panel.hidden = !quality;
  const results = $('quality-results');
  results.replaceChildren();
  if (!quality) return;
  const slices = Object.entries(quality.slices);
  const verified = slices.filter(([,row]) => ['verified', 'verified_with_warnings'].includes(row.status)).length;
  const stats = el('ul'); stats.className = 'quality-stats';
  for (const [count, label] of [[verified, 'slices verified'], [quality.repairs.length, 'defects repaired'],
    [quality.warnings.length, 'nonblocking warnings'], [quality.defects.length, 'blocking visual defects']]) {
    const item = el('li');
    item.append(el('strong', String(count)), el('span', label));
    stats.append(item);
  }
  results.append(el('p', qualityLabels[quality.status]), stats);
  if (quality.audit) {
    if (quality.audit_sha256) {
      const identity = el('details');
      identity.append(el('summary', 'Audit identity'), el('p', quality.audit.audit_id), el('code', quality.audit_sha256));
      results.append(identity);
    }
    results.append(el('p', `${quality.audit_current ? 'Current' : 'Historical'} audit · ${quality.audit.audit_id}`));
    if (!quality.audit_current) results.append(el('p', 'Rerun the audit for the current implementation.'));
    const checks = el('ul');
    for (const check of quality.audit.checks) {
      const item = el('li');
      const observation = el('details');
      observation.append(el('summary', `${qualityLabels[check.result]} · ${check.scenario} · ${check.slice_ids.join(', ')}`));
      if (Object.hasOwn(check, 'expected')) observation.append(el('pre', JSON.stringify({
        expected: check.expected, observed: check.observation
      }, null, 2)));
      if (check.diagnostic) observation.append(el('pre', check.diagnostic));
      checks.append(item);
      item.append(observation);
    }
    if (checks.childNodes.length) {
      const evidence = el('details');
      evidence.append(el('summary', 'Integration and browser journey results'), checks);
      results.append(evidence);
    }
  }
  if (quality.acceptance_current) results.append(el('p', `Accepted by the user · ${quality.acceptance.accepted_at}`));
  else if (quality.acceptance) results.append(el('p', `Prior user acceptance · ${quality.acceptance.audit_id}`));
  for (const reason of quality.blockers) results.append(el('p', reason));
  if (slices.length) {
    const details = el('details');
    details.append(el('summary', 'Slice verification'));
    const list = el('ul');
    for (const [key, row] of slices) {
      const item = el('li', `${key} · ${qualityLabels[row.status]}`);
      for (const reason of row.reasons) item.append(el('p', reason));
      if (row.ui) item.append(qualityEvidenceButton(key));
      list.append(item);
    }
    details.append(list);
    results.append(details);
  }
  for (const [title, records] of [['Documented warnings', quality.warnings],
    ['Verified corrections', quality.repairs], ['Blocking visual defects', quality.defects]]) {
    if (!records.length) continue;
    const details = el('details');
    details.open = records === quality.warnings;
    details.append(el('summary', title));
    for (const record of records) {
      const item = el('div');
      item.append(el('h3', `${record.slice_id} · ${record.finding_id} · ${record.severity}`), el('p', record.description));
      if (record.comparison_count) {
        for (let index = 0; index < record.comparison_count; index++) {
          const path = `/visual-quality/warning/${encodeURIComponent(record.finding_id)}/${index}`;
          const pair = el('div'); pair.className = 'register-shot-grid register-comparison';
          pair.append(registerImageFromUrl(path + '/before', 'Before'), registerImageFromUrl(path + '/after', 'After'));
          item.append(pair);
        }
      } else {
        item.append(qualityEvidenceButton(record.slice_id, record.finding_id));
      }
      details.append(item);
    }
    results.append(details);
  }
}

function renderVisualRegister(attempts = registerAttempts) {
  const entries = $('register-entries');
  const statusFilter = $('register-status').value;
  const search = $('register-search').value.trim().toLowerCase();
  const changes = registerMetadata.cross_screen_changes || [];
  const baselines = registerMetadata.visual_baselines || [];
  const slices = [...new Set([...attempts.map(a => a.slice_id),
    ...Object.keys(registerMetadata.visual_quality?.slices || {}),
    ...changes.flatMap(change => change.affected_slices),
    ...baselines.flatMap(baseline => baseline.affected_slice_ids || baseline.slice_ids)]
    .filter(x => typeof x === 'string'))].sort();
  const select = $('register-slice');
  const selectedSlice = select.value;
  const allSlices = el('option', 'All slices'); allSlices.value = '';
  select.replaceChildren(allSlices);
  for (const slice of slices) { const option = el('option', slice); option.value = slice; select.add(option); }
  select.value = slices.includes(selectedSlice) ? selectedSlice : '';
  const summary = summarizeVisualRegister(attempts, registerMetadata.slices);
  const openCount = summary.findings.filter(f => f.status === 'open').length;
  const correctedCount = summary.findings.filter(f => f.status === 'corrected').length;
  $('register-counts').textContent = `${attempts.length} attempts · ${summary.cleanAttempts.length} clean evidence records · ${summary.findings.length} distinct findings (${openCount} open, ${correctedCount} corrected) · ${slices.length} slices${changes.length ? ` · ${changes.length} cross-screen changes` : ''}`;
  const projectStatus = $('register-project-status');
  projectStatus.replaceChildren();
  renderQualityReport(registerMetadata.visual_quality);
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
    ...summary.findings.filter(f => !statusFilter || f.status === statusFilter ||
      (statusFilter === 'warnings' && (f.status === 'warning' || f.finding.classification === 'design_concern'))).map(f => ({kind:'finding',value:f})),
    ...summary.cleanAttempts.filter(() => !statusFilter || statusFilter === 'clean').map(a => ({kind:'clean',value:a})),
    ...summary.incompleteAttempts.filter(a => !statusFilter || (statusFilter === 'open' && !a.historical))
      .map(a => ({kind:'incomplete',value:a.attempt,historical:a.historical}))
  ].filter(entry => (!select.value || entry.value.slice_id === select.value) &&
    (!search || (entry.kind === 'finding'
      ? JSON.stringify([entry.value.slice_id,entry.value.finding_id,entry.value.finding,entry.value.history]).toLowerCase().includes(search)
      : attemptText(entry.value).includes(search))));
  const visible = candidates.reverse();
  entries.replaceChildren();
  const matches = changes.filter(change => {
    if (select.value && !change.affected_slices.includes(select.value)) return false;
    if (search && !JSON.stringify(change).toLowerCase().includes(search)) return false;
    const findings = change.attempts.flatMap(attempt => attempt.findings);
    const latestFindings = change.attempts.at(-1)?.findings || [];
    const status = change.current_status || change.status;
    return !statusFilter || (statusFilter === 'open' && !['verified','abandoned','historical'].includes(status)) ||
      (statusFilter === 'clean' && status === 'verified' && !findings.some(finding => finding.status === 'fixed')) ||
      (statusFilter === 'corrected' && status === 'verified' && findings.some(finding => finding.status === 'fixed')) ||
      (statusFilter === 'improved' && latestFindings.some(finding => finding.status === 'improved')) ||
      (statusFilter === 'noted' && findings.some(finding => finding.status === 'noted')) ||
      (statusFilter === 'warnings' && findings.some(finding =>
        finding.status === 'deferred' || finding.classification === 'design_concern'));
  });
  const visibleBaselines = baselines.filter(baseline =>
    (!select.value || (baseline.affected_slice_ids || baseline.slice_ids).includes(select.value)) &&
    (!search || JSON.stringify(baseline).toLowerCase().includes(search)) &&
    (!statusFilter || statusFilter === 'baselines' || (statusFilter === 'open' && !baseline.available)));
  for (const baseline of visibleBaselines) entries.append(visualBaselineCard(baseline));
  for (const change of matches.reverse()) entries.append(crossScreenCard(change));
  if (!visible.length && !matches.length && !visibleBaselines.length) {
    entries.append(el('p', 'No current findings or clean evidence match these filters.'));
    return;
  }
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
function completionSection(parent, id, title) {
  const section = el('section');
  section.id = id;
  section.tabIndex = -1;
  section.append(el('h2', title));
  parent.append(section);
  return section;
}
function renderCompletionReview() {
  const review = registerMetadata.final_review;
  const quality = registerMetadata.visual_quality;
  const holder = $('completion-results');
  if (!holder) return;
  $('completion-toggle').hidden = !review;
  const signature = JSON.stringify([review, selectedCompletionAudit, state?.revision, registerMetadata.visual_baselines, quality?.status,
    quality?.audit_current, quality?.acceptance_current,
    quality?.audit?.audit_id, registerMetadata.history_warnings]);
  if (signature === completionSignature) return;
  completionSignature = signature;
  const openSections = new Set([...holder.querySelectorAll('details[open] > summary')].map(node => node.textContent));
  holder.replaceChildren();
  if (!review) {
    holder.append(el('p', registerMetadata.history_warnings?.at(-1) || 'A technical completion report is pending.'));
    return;
  }
  const {corrections, acceptances} = review;
  const audit = review.prior_reports?.find(row => row.audit_id === selectedCompletionAudit) || review.audit;
  const slices = audit.review_scope || (audit === review.audit ? review.slices : {});
  if (review.prior_reports?.length) {
    const label = el('label', 'Completion report ');
    const picker = el('select');
    for (const report of [review.audit, ...review.prior_reports]) {
      const option = el('option', `${report.recorded_at} · ${report.audit_id}`);
      option.value = report.audit_id;
      picker.append(option);
    }
    picker.value = audit.audit_id;
    picker.onchange = () => {
      selectedCompletionAudit = picker.value;
      renderCompletionReview();
      holder.querySelector('select').focus();
    };
    label.append(picker);
    holder.append(label);
  }
  const pending = corrections.filter(row => !['verified', 'withdrawn', 'deferred'].includes(row.status));
  const isCurrent = quality.audit_current && quality.audit?.audit_id === audit.audit_id;
  const phase = !isCurrent ?
    (acceptances.some(record => record.audit_id === audit.audit_id) ? 'Historical report · accepted at the time' :
      'Historical technical report') : quality.acceptance_current ? 'Finally accepted' : pending.length ?
    'Review feedback pending' : 'Awaiting acceptance';
  holder.append(el('p', phase), el('p', `${qualityLabels[audit.status]} · ${audit.audit_id}`));
  const rows = Object.values(audit.slices);
  const verified = rows.filter(row => ['verified', 'verified_with_warnings'].includes(row.status)).length;
  const metrics = el('ul'); metrics.className = 'quality-stats';
  for (const [count, title] of [[`${verified}/${rows.length}`, 'slices verified'], [audit.repairs.length, 'defects repaired'],
    [audit.warnings.length, 'warnings remaining'], [audit.defects.length, 'blocking defects in this report']]) {
    const metric = el('li'); metric.append(el('strong', String(count)), el('span', title)); metrics.append(metric);
  }
  holder.append(metrics);
  const links = el('div'); links.className = 'completion-links'; holder.append(links);
  const add = (id, title) => {
    const section = completionSection(holder, id, title);
    links.append(button(title, () => { section.focus(); section.scrollIntoView?.(); }));
    return section;
  };

  const delivered = add('completion-delivery', 'Completed screens and features');
  for (const workstream of audit.plan?.workstreams || []) {
    const area = el('details');
    area.append(el('summary', workstream.title));
    for (const key of workstream.slice_ids) {
      area.append(button(slices[key]?.title || key, () => {
        const slice = document.getElementById('completion-slice-' + key);
        if (slice) { slice.open = true; slice.querySelector('summary').focus(); }
      }));
    }
    delivered.append(area);
  }
  const features = new Map();
  for (const [key, row] of Object.entries(slices)) {
    for (const feature of row.features) {
      if (!features.has(feature.feature_id)) features.set(feature.feature_id, {title: feature.title, sliceIds: [], paths: feature.spec_paths});
      features.get(feature.feature_id).sliceIds.push(key);
    }
  }
  for (const [id, feature] of features) {
    const area = el('details');
    area.append(el('summary', feature.title));
    for (const section of Object.values(state?.sections || {}).filter(item => item.features?.includes(id) ||
        feature.paths.includes(item.source))) {
      area.append(button(section.title, () => choose(section.id)));
    }
    for (const key of feature.sliceIds) area.append(el('p', slices[key].title));
    delivered.append(area);
  }
  for (const [key, row] of Object.entries(audit.slices)) {
    const slice = el('details');
    slice.id = 'completion-slice-' + key;
    slice.append(el('summary', `${slices[key]?.title || key} · ${qualityLabels[row.status]}`));
    slice.append(el('p', key));
    for (const reason of row.reasons) slice.append(el('p', reason));
    for (const section of Object.values(state?.sections || {}).filter(item => item.slice === key)) {
      slice.append(button(section.title, () => choose(section.id, key)));
    }
    if (row.ui) slice.append(button('Screenshots and regression checks', () => openRegisterEvidence(key)));
    delivered.append(slice);
  }

  const comparison = add('completion-comparisons', 'Original → Approved → Implemented');
  const verifiedCaptures = audit.verified_capture_ids && new Set(audit.verified_capture_ids);
  const originals = (registerMetadata.visual_baselines || []).map(baseline => ({
    ...baseline,
    actual: baseline.actual.filter(capture => capture.approval_id === audit.approval_id &&
      (verifiedCaptures ? verifiedCaptures.has(capture.case.capture_id) :
        Date.parse(capture.captured_at) <= Date.parse(audit.recorded_at))),
  })).filter(baseline => baseline.approved?.[audit.approval_id] || baseline.actual.length);
  for (const baseline of originals) {
    const item = el('details');
    item.append(el('summary', `${baseline.definition.state.route} · ${baseline.definition.baseline_id}`),
      visualBaselineCard(baseline, audit.approval_id));
    comparison.append(item);
  }
  if (!originals.length) comparison.append(el('p', 'No original UI comparisons were recorded for this report.'));

  for (const [id, title, records] of [
    ['completion-repairs', 'Automatic visual repairs', audit.repairs],
    ['completion-warnings', 'Remaining warnings', audit.warnings]
  ]) {
    const section = add(id, title);
    if (!records.length) section.append(el('p', id === 'completion-repairs' ? 'No automatic visual repairs were recorded.' : 'No warnings were recorded.'));
    for (const row of records) {
      const finding = el('details');
      finding.append(el('summary', `${row.description} · ${row.severity}`), el('p', row.slice_id));
      if (row.comparison_count) {
        for (let index = 0; index < row.comparison_count; index++) {
          const path = `/visual-quality/warning/${encodeURIComponent(row.finding_id)}/${index}`;
          const pair = el('div'); pair.className = 'register-shot-grid register-comparison';
          pair.append(registerImageFromUrl(path + '/before', 'Before'), registerImageFromUrl(path + '/after', 'After'));
          finding.append(pair);
        }
      } else {
        finding.append(qualityEvidenceButton(row.slice_id, row.finding_id));
      }
      section.append(finding);
    }
  }

  const evidence = add('completion-verification', 'Verification evidence');
  for (const check of audit.checks) {
    const detail = el('details');
    detail.append(el('summary', `${qualityLabels[check.result]} · ${check.scenario}`), el('p', check.slice_ids.join(', ')),
      el('pre', JSON.stringify({expected: check.expected, observed: check.observation}, null, 2)));
    if (check.diagnostic) detail.append(el('pre', check.diagnostic));
    evidence.append(detail);
  }
  evidence.append(button('Open Visual Review Register', () => openRegisterEvidence()));
  const feedback = add('completion-feedback', 'Review feedback and corrections');
  feedback.append(el('p', 'Tell Codex which screen or interaction needs a correction. You can also ask for a guided review of any feature, a group of slices, or the whole application.'));
  for (const finding of corrections) {
    const original = finding.history[0];
    const detail = el('details');
    detail.append(el('summary', `${original.user_feedback} · ${finding.status.replaceAll('_', ' ')}`),
      el('p', `${finding.finding_id} · ${Object.keys(original.scope).join(', ')} · ${original.audit_id}`));
    for (const [key, row] of Object.entries(original.scope)) {
      for (const section of Object.values(state?.sections || {}).filter(item =>
        item.slice === key || row.features.some(feature => feature.spec_paths.includes(item.source)))) {
        detail.append(button(section.title, () => choose(section.id, section.slice || '')));
      }
      if (row.protected_decision_ids.length) detail.append(el('p', 'Protected decisions · ' + row.protected_decision_ids.join(', ')));
    }
    for (const event of finding.history.slice(1)) {
      detail.append(el('p', `${event.status.replaceAll('_', ' ')} · ${event.recorded_at}`));
      for (const text of [event.rationale, event.summary, event.user_response]) {
        if (text) detail.append(el('p', text));
      }
      for (const comparison of event.comparisons || []) {
        const pair = el('div'); pair.className = 'register-shot-grid register-comparison';
        const rect = comparison.difference?.normalized_rect;
        pair.append(registerImage(comparison.before.closeup_screenshot, 'Before correction', rect),
          registerImage(comparison.after.closeup_screenshot, 'After correction', rect));
        detail.append(el('p', `${comparison.slice_id} · ${comparison.after.case_id}`), pair);
        if (comparison.difference?.annotated_latest) detail.append(annotatedEvidenceLinks(comparison.difference));
        if (comparison.comparison_note) detail.append(el('p', comparison.comparison_note));
      }
      if (event.verification) {
        const checks = el('details');
        checks.append(el('summary', 'Saved revalidation results'),
          el('pre', JSON.stringify({verification: event.verification, evidence: event.archived_evidence}, null, 2)));
        detail.append(checks);
      }
    }
    feedback.append(detail);
  }
  const acceptance = add('completion-acceptance', 'Final acceptance history');
  const identity = el('details');
  identity.append(el('summary', 'Report identity'), el('p', audit.audit_id), el('p', audit.approval_id));
  if (isCurrent) identity.append(el('code', quality.audit_sha256));
  acceptance.append(identity);
  for (const record of acceptances) {
    const detail = el('details');
    detail.append(el('summary', `${record.accepted_at} · ${record.audit_id}`), el('p', record.user_response));
    if (record.audit_sha256) detail.append(el('code', record.audit_sha256));
    acceptance.append(detail);
  }
  if (isCurrent && !quality.acceptance_current && !pending.length) {
    acceptance.append(el('p', 'Give Codex your explicit acceptance when you are ready.'));
  }
  for (const summary of holder.querySelectorAll('details > summary')) {
    if (openSections.has(summary.textContent)) summary.parentElement.open = true;
  }
}
async function loadVisualRegister() {
  const ticket = ++reviewRequest;
  const response = await read('/visual-integrity');
  const result = await response.json();
  if (ticket !== reviewRequest) return;
  registerAttempts = Array.isArray(result) ? result : result.attempts || [];
  registerMetadata = {...result.project, cross_screen_changes: result.cross_screen_changes || [],
    visual_baselines: result.visual_baselines || []};
  renderCompletionReview();
  if (!$('visual-register').hidden) renderVisualRegister();
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
  const show = $('visual-register').hidden;
  setReviewMode(show ? 'register' : '');
  if (show) loadVisualRegister().catch(showError);
};
if ($('completion-toggle')) {
  $('completion-toggle').onclick = () => {
    const show = $('completion-review').hidden;
    setReviewMode(show ? 'completion' : '');
    if (show) {
      $('completion-title').focus();
      loadVisualRegister().catch(showError);
    }
  };
  $('completion-refresh').onclick = () => loadVisualRegister().catch(showError);
}
function setReviewMode(mode) {
  $('visual-register').hidden = mode !== 'register';
  $('register-toggle').setAttribute('aria-expanded', String(mode === 'register'));
  if ($('completion-review')) {
    $('completion-review').hidden = mode !== 'completion';
    $('completion-toggle').setAttribute('aria-expanded', String(mode === 'completion'));
  }
  updateReviewVisibility();
}
function updateReviewVisibility() {
  const hide = !$('visual-register').hidden || ($('completion-review') && !$('completion-review').hidden);
  for (const id of ['title', 'impact', 'why', 'tools', 'cards', 'outline']) {
    if ($(id)) $(id).hidden = Boolean(hide);
  }
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
