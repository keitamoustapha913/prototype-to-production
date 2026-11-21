const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {JSDOM} = require('jsdom');

const source = ['comparisons.js', 'viewer.js']
  .map(file => fs.readFileSync(path.join(__dirname, '../assets/review-viewer', file), 'utf8'))
  .join('\n');
function viewer(blobText, withStorage = false) {
  const options = {runScripts:'outside-only'};
  if (withStorage) options.url = 'http://review.invalid/';
  const dom = new JSDOM('<header><button id="register-toggle"></button></header><nav></nav><main><p id="error"></p><p id="revision"></p><section id="visual-register" hidden></section><h1 id="title"></h1><div id="impact"></div><p id="why"></p><div id="tools"></div><div id="cards"></div><select id="baseline"></select><select id="context"></select><select id="register-slice"><option value=""></option></select><select id="register-status"><option value=""></option><option value="open"></option><option value="corrected"></option><option value="clean"></option></select><input id="register-search"><button id="register-refresh"></button><p id="register-counts"></p><div id="register-entries"></div>', options);
  dom.window.setTimeout = () => 0;
  dom.window.fetch = async url => url === '/state' ? {status:304} : {
    ok:true,
    text:async () => blobText[url.split('/').pop()]
  };
  dom.window.eval(source + '\nwindow.__testSourceChanges = sourceChanges; window.__testIllustration = illustration; window.__setState = value => { state = value; }; window.__mergeState = mergeState; window.__setChoices = (key,slice) => { selected = key; scopeSlice = slice; }; window.__runUpdate = update; window.__viewerChoices = () => ({selected, scopeSlice, context:$(\'context\').value, baseline:$(\'baseline\').value});');
  return dom;
}

function section(id, values = {}) {
  return {id, title:id, source:id + '.md', kind:'html', blob:id, stamp:'1', slice:'slice-a', impact:{'slice-a':{kind:'guide', reasons:[]}}, features:['feature-a'], instruction:'', reason:'', ...values};
}
function stateResponse(value) {
  return {status:200, ok:true, json:async () => value, headers:{get:() => 'etag'}};
}

test('state delta patches nested maps without replacing unchanged entries', () => {
  const dom = viewer({});
  const merged = dom.window.__mergeState({revision:1,sections:{keep:{blob:'a'},change:{blob:'old'},gone:{blob:'x'}},
    visual_review:{'':{keep:'1',change:'old',gone:'x'}}},
  {fields:{revision:2},maps:{sections:{set:{change:{blob:'new'},added:{blob:'b'}},delete:['gone'],children:{}},
    visual_review:{set:{},delete:[],children:{'':{set:{change:'new',added:'b'},delete:['gone'],children:{}}}}}});
  assert.equal(merged.revision,2);
  assert.equal(merged.sections.keep.blob,'a');
  assert.equal(merged.sections.change.blob,'new');
  assert.ok(merged.sections.added);
  assert.ok(!merged.sections.gone);
  assert.deepEqual({...merged.visual_review['']},{keep:'1',change:'new',added:'b'});
  dom.window.close();
});
async function changes(dom,before,after) {
  return dom.window.__testSourceChanges({blob:'old',kind:before.kind},{blob:'new',kind:after.kind});
}

test('JSON comparison reports only changed fields with nested paths', async () => {
  const dom = viewer({'old':'{"user":{"name":"Ada","active":true},"keep":1}',
    'new':'{"user":{"name":"Grace","active":true},"keep":1}'});
  const node = await changes(dom,{kind:'json'},{kind:'json'});
  assert.match(node.textContent,/Changed JSON fields · 1/);
  assert.match(node.textContent,/user\.name/);
  assert.match(node.textContent,/Ada/);
  assert.match(node.textContent,/Grace/);
  assert.doesNotMatch(node.textContent,/keep/);
  dom.window.close();
});

test('JSON comparison safely identifies escaped keys and own prototype-named fields', async () => {
  const dom = viewer({'old':'{"key.with\\\"quote":"old","__proto__":"before","toString":"same"}',
    'new':'{"key.with\\\"quote":"new","__proto__":"after","toString":"changed"}'});
  const node = await changes(dom,{kind:'json'},{kind:'json'});
  assert.ok(node.textContent.includes('$["key.with\\"quote"]'));
  assert.match(node.textContent,/\$\["__proto__"\]/);
  assert.match(node.textContent,/\$\.toString/);
  assert.match(node.textContent,/before/);
  assert.match(node.textContent,/after/);
  dom.window.close();
});

test('Mermaid comparison labels participant and message edits', async () => {
  const dom = viewer({'old':'sequenceDiagram\nparticipant A as Client\nparticipant B as API\nA->>B: old\nB-->>A: done',
    'new':'sequenceDiagram\nparticipant A as Client\nparticipant B as Service\nA->>B: new\nB-->>A: done'});
  const node = await changes(dom,{kind:'mmd'},{kind:'mmd'});
  assert.match(node.textContent,/Participants/);
  assert.match(node.textContent,/Messages/);
  assert.match(node.textContent,/API/);
  assert.match(node.textContent,/Service/);
  assert.match(node.textContent,/old/);
  assert.match(node.textContent,/new/);
  dom.window.close();
});

test('Mermaid flowchart comparison labels changed edges', async () => {
  const dom = viewer({'old':'flowchart TD\nA[Start] --> B[Done]',
    'new':'flowchart TD\nA[Start] ==> B[Done]'});
  const node = await changes(dom,{kind:'mmd'},{kind:'mmd'});
  assert.match(node.textContent,/Edges/);
  assert.match(node.textContent,/A\[Start\] --> B\[Done\]/);
  assert.match(node.textContent,/A\[Start\] ==> B\[Done\]/);
  dom.window.close();
});

test('source comparison preserves separate hunks', async () => {
  const dom = viewer({'old':'alpha\nbeta\ngamma\ndelta\nepsilon',
    'new':'ALPHA\nbeta\ngamma\ndelta\nEPSILON'});
  const node = await changes(dom,{kind:'md'},{kind:'md'});
  assert.equal(node.querySelectorAll('h4').length,2);
  assert.match(node.textContent,/alpha/);
  assert.match(node.textContent,/ALPHA/);
  assert.match(node.textContent,/epsilon/);
  assert.match(node.textContent,/EPSILON/);
  dom.window.close();
});

test('prose edits reuse visible Mermaid output without rendering diagrams again', async () => {
  const mermaidSource = 'flowchart TD\nA --> B';
  const dom = viewer({old:`Intro before\n\n\`\`\`mermaid\n${mermaidSource}\n\`\`\``,
    new:`Revised prose\n\n\`\`\`mermaid\n${mermaidSource}\n\`\`\``});
  let calls = 0;
  dom.window.mermaid = {
    initialize() {},
    render: async () => { calls++; return {svg:'<svg></svg>'}; }
  };
  const append = dom.window.document.head.append.bind(dom.window.document.head);
  dom.window.document.head.append = (...nodes) => {
    append(...nodes);
    for (const node of nodes) if (node.src === '/mermaid.js') queueMicrotask(() => node.onload());
  };
  dom.window.marked = {parse: text => {
    const match = text.match(/^```mermaid\s*\n([\s\S]*?)^```\s*$/m);
    return '<p>' + text.split('\n')[0] + '</p>' + (match ? '<pre><code class="language-mermaid">' + match[1] + '</code></pre>' : '');
  }};
  dom.window.DOMPurify = {sanitize: html => {
    const template = dom.window.document.createElement('template');
    template.innerHTML = html;
    return template.content;
  }};
  dom.window.__setState({sections:{}});
  const first = await dom.window.__testIllustration({kind:'md',source:'guide.md',blob:'old',title:'Guide'});
  dom.window.document.body.append(first);
  assert.equal(calls,1);
  const revised = await dom.window.__testIllustration({kind:'md',source:'guide.md',blob:'new',title:'Guide'});
  assert.equal(calls,1);
  assert.equal(revised.querySelectorAll('img[data-mermaid-source]').length,1);
  dom.window.close();
});

test('large documents retain distant edits as separate hunks', async () => {
  const oldLines = Array.from({length:2100},(_,index) => `line-${index}`);
  const newLines = oldLines.slice();
  newLines[100] = 'changed-100';
  newLines[1900] = 'changed-1900';
  const dom = viewer({'old':oldLines.join('\n'),'new':newLines.join('\n')});
  const node = await changes(dom,{kind:'md'},{kind:'md'});
  assert.equal(node.querySelectorAll('h4').length,2);
  assert.match(node.textContent,/line-100/);
  assert.match(node.textContent,/changed-100/);
  assert.match(node.textContent,/line-1900/);
  assert.match(node.textContent,/changed-1900/);
  dom.window.close();
});

test('large Mermaid comparisons keep valid typed edits', async () => {
  const oldLines = ['flowchart TD',...Array.from({length:2100},(_,index) => `node${index} --> node${index+1}`)];
  const newLines = oldLines.slice();
  newLines[100] = 'node99 ==> node100';
  newLines[1900] = 'node1899 ==> node1900';
  const dom = viewer({'old':oldLines.join('\n'),'new':newLines.join('\n')});
  const node = await changes(dom,{kind:'mmd'},{kind:'mmd'});
  assert.match(node.textContent,/Edges/);
  assert.match(node.textContent,/node99 --> node100/);
  assert.match(node.textContent,/node99 ==> node100/);
  assert.match(node.textContent,/node1899 --> node1900/);
  assert.match(node.textContent,/node1899 ==> node1900/);
  dom.window.close();
});

test('live revision keeps valid context, slice, baseline and scroll while refreshing comparisons', async () => {
  const dom = viewer({}, true);
  const win = dom.window;
  const old = section('guide');
  const revised = section('guide', {stamp:'2'});
  const previousState = {root:'project', revision:1, sections:{guide:old}, slice_status:{'slice-a':'Draft'},
    current:{}, approved:{guide:'approved-stamp'}};
  const nextState = {...previousState, revision:2, sections:{guide:revised}};
  win.__setState(previousState);
  win.document.getElementById('context').innerHTML = '<option>Component</option><option>Feature</option><option>Slice</option><option>Blueprint</option><option>Application</option>';
  win.document.getElementById('baseline').innerHTML = '<option value="previous">Previous</option><option value="approved">Approved</option>';
  win.document.getElementById('context').value = 'Feature';
  win.document.getElementById('baseline').value = 'approved';
  win.__setState(previousState);
  win.__setChoices('guide','slice-a');
  win.eval("etag = 'old-etag';");
  assert.equal(win.__viewerChoices().scopeSlice, 'slice-a');
  let restoredPosition;
  win.scrollTo = (_x, y) => { restoredPosition = y; };
  Object.defineProperty(win, 'scrollY', {value:417, configurable:true});
  win.fetch = async url => {
    if (url === '/state') return stateResponse(nextState);
    if (url.startsWith('/approved/')) return stateResponse({section:old});
    if (url.startsWith('/previous/')) return stateResponse({section:old, revision:1});
    return {ok:true, text:async () => '<p>guide</p>'};
  };
  await win.__runUpdate();
  assert.equal(win.document.getElementById('error').textContent, '');
  assert.deepEqual(JSON.parse(JSON.stringify(win.__viewerChoices())), {selected:'guide', scopeSlice:'slice-a', context:'Feature', baseline:'approved'});
  assert.equal(restoredPosition,417);
  assert.equal(win.document.getElementById('revision').textContent,'Revision 2');
  assert.match(win.document.getElementById('cards').textContent,/Last approved/);
  win.close();
});

test('live revision drops a slice scope that no longer exists', async () => {
  const dom = viewer({}, true);
  const win = dom.window;
  const old = section('guide');
  const revised = section('guide', {stamp:'2', slice:'slice-b', impact:{'slice-b':{kind:'guide', reasons:[]}}});
  const previousState = {root:'project', revision:1, sections:{guide:old}, slice_status:{'slice-a':'Draft'}, current:{}, approved:{}};
  win.__setState(previousState);
  win.document.getElementById('context').innerHTML = '<option>Component</option><option>Feature</option><option>Slice</option><option>Blueprint</option><option>Application</option>';
  win.document.getElementById('baseline').innerHTML = '<option value="previous">Previous</option><option value="approved">Approved</option>';
  win.document.getElementById('context').value = 'Slice';
  win.document.getElementById('baseline').value = 'approved';
  win.__setChoices('guide','slice-a');
  win.eval("etag = 'old-etag';");
  win.scrollTo = () => {};
  win.fetch = async url => url === '/state' ? stateResponse({...previousState, revision:2, sections:{guide:revised}, slice_status:{'slice-b':'Draft'}}) :
    url.startsWith('/approved/') ? stateResponse(null) : url.startsWith('/previous/') ? stateResponse({section:old, revision:1}) : {ok:true, text:async () => '<p>guide</p>'};
  await win.__runUpdate();
  assert.deepEqual(JSON.parse(JSON.stringify(win.__viewerChoices())), {selected:'guide', scopeSlice:'', context:'Slice', baseline:'approved'});
  win.close();
});

test('last approved and section status use the selected impacted slice', async () => {
  const dom = viewer({}, true);
  const win = dom.window;
  const shared = section('guide', {stamp:'owner-live', impact:{
    'slice-a': {kind:'Direct', reasons:['slice owner']},
    'slice-b': {kind:'Compatibility', reasons:['depends on slice-a']},
    'slice-c': {kind:'Compatibility', reasons:['depends on slice-a']}
  }});
  const state = {root:'project', revision:3, sections:{guide:shared},
    slice_status:{'slice-a':'Approved','slice-b':'Needs review','slice-c':'Awaiting review'},
    approved:{guide:'consumer-approved'}, current:{guide:'owner-live'},
    approved_by_scope:{'slice-a':{guide:'owner-live'},'slice-b':{guide:'consumer-approved'}},
    current_by_scope:{'slice-a':{guide:'owner-live'},'slice-b':{guide:'consumer-approved'}},
    visual_review:{'slice-a':{guide:'owner-live'},'slice-b':{guide:'consumer-live'},'slice-c':{guide:'third-live'}}};
  win.__setState(state);
  win.scrollTo = () => {};
  win.document.getElementById('baseline').innerHTML = '<option value="approved">Last approved</option>';
  win.fetch = async url => {
    if (url === '/state') return stateResponse(state);
    if (url === '/approved/guide/owner-live') return stateResponse({section:{...shared, blob:'owner'}});
    if (url === '/approved/guide/consumer-approved') return stateResponse({section:{...shared, blob:'consumer'}});
    const illustrations = {'/blob/guide':'Current guide', '/blob/owner':'Owner receipt', '/blob/consumer':'Consumer receipt'};
    return {ok:true, text:async () => '<p>' + illustrations[url] + '</p>'};
  };
  const cards = win.document.getElementById('cards');
  win.__setChoices('guide', 'slice-a');
  await win.__runUpdate();
  assert.match(cards.querySelector('h2').textContent, /guide · Approved/);
  assert.match(cards.textContent, /Owner receipt/);
  assert.doesNotMatch(cards.textContent, /Consumer receipt/);

  win.__setChoices('guide', 'slice-b');
  await win.__runUpdate();
  assert.match(cards.querySelector('h2').textContent, /guide · Needs review/);
  assert.match(cards.textContent, /Consumer receipt/);

  state.slice_status['slice-b'] = 'Approved';
  state.visual_review['slice-b'].guide = 'consumer-approved';
  await win.__runUpdate();
  assert.match(cards.querySelector('h2').textContent, /guide · Approved/);

  win.__setChoices('guide', 'slice-c');
  await win.__runUpdate();
  assert.match(cards.querySelector('h2').textContent, /guide · Draft/);
  assert.doesNotMatch(cards.textContent, /Last approved/);
  win.close();
});

test('unrelated live edit keeps the selected section inside the active slice', async () => {
  const dom = viewer({}, true);
  const win = dom.window;
  const inScope = section('in-scope', {title:'In scope'});
  const outsideOld = section('outside', {title:'Outside', slice:'slice-b', impact:{'slice-b':{kind:'guide', reasons:[]}}});
  const outsideNew = {...outsideOld, stamp:'2'};
  const previousState = {root:'project', revision:1, sections:{'in-scope':inScope, outside:outsideOld},
    slice_status:{'slice-a':'Draft','slice-b':'Draft'}, current:{}, approved:{}};
  win.__setState(previousState);
  win.document.getElementById('context').innerHTML = '<option>Component</option><option>Feature</option><option>Slice</option><option>Blueprint</option><option>Application</option>';
  win.document.getElementById('baseline').innerHTML = '<option value="previous">Previous</option><option value="approved">Approved</option>';
  win.document.getElementById('context').value = 'Slice';
  win.__setChoices('in-scope','slice-a');
  win.eval("etag = 'old-etag';");
  win.scrollTo = () => {};
  win.fetch = async url => url === '/state' ? stateResponse({...previousState, revision:2, sections:{'in-scope':inScope, outside:outsideNew}}) :
    url.startsWith('/previous/') ? stateResponse({section:inScope, revision:1}) : {ok:true, text:async () => '<p>guide</p>'};
  await win.__runUpdate();
  assert.deepEqual(JSON.parse(JSON.stringify(win.__viewerChoices())), {selected:'in-scope', scopeSlice:'slice-a', context:'Slice', baseline:'previous'});
  assert.equal(win.document.getElementById('title').textContent,'slice-a / In scope');
  assert.match(win.document.getElementById('impact').textContent,/Outside/);
  win.close();
});
