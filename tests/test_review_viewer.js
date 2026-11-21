'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');

const source = fs.readFileSync(path.join(__dirname,'../assets/review-viewer/viewer.js'),'utf8')
  .split("$('cards').addEventListener('click'")[0];
const dom = new JSDOM('<header><button id="register-toggle"></button></header><nav></nav><main><section id="visual-register" hidden></section><h1 id="title"></h1><div id="impact"></div><p id="why"></p><div id="tools"></div><div id="cards"></div><select id="register-slice"><option value=""></option></select><select id="register-status"><option value=""></option><option value="open"></option><option value="corrected"></option><option value="clean"></option></select><input id="register-search"><button id="register-refresh"></button><p id="register-counts"></p><div id="register-project-status"></div><div id="register-entries"></div><p id="error"></p></main>', {url:'http://review.invalid/'});
const context = vm.createContext({document:dom.window.document, localStorage:dom.window.localStorage,
  URL, state:{slice_status:{oldOwner:'Needs review',newOwner:'Draft'}}, selected:null, affected:[], impactChanges:[]});
vm.runInContext(source,context);

test('impact summary shows the old and new owners and consumers for an impact-only change', () => {
  const before = {id:'design',title:'Design',stamp:'same',impact:{
    oldOwner:{kind:'Direct',reasons:['slice owner','consumes old-api']},
    retained:{kind:'Compatibility',reasons:['depends on design']}
  }};
  const after = {id:'design',title:'Design',stamp:'same',impact:{
    newOwner:{kind:'Compatibility',reasons:['consumes new-api']},
    retained:{kind:'Compatibility',reasons:['depends on design']}
  }};
  assert.equal(context.reviewSectionChanged(before,after),true);
  context.before = before;
  context.after = after;
  vm.runInContext('affected = [after]; impactChanges = [{before,after}]; state = {slice_status:{oldOwner:"Needs review",newOwner:"Draft"}};',context);
  context.impactSummary();
  const summary = dom.window.document.getElementById('impact').textContent;
  assert.match(summary,/Removed · Direct · Design/);
  assert.match(summary,/slice owner, consumes old-api/);
  assert.match(summary,/Added · Compatibility · Design/);
  assert.match(summary,/consumes new-api/);
  assert.match(summary,/Retained · Compatibility · Design/);
  assert.match(summary,/depends on design/);
});

test('material edits retain their impacted slices in the summary', () => {
  const before = {id:'schema',title:'Schema',stamp:'old',impact:{
    owner:{kind:'Direct',reasons:['slice owner']},consumer:{kind:'Compatibility',reasons:['consumes api']}
  }};
  const after = {...before,stamp:'new'};
  assert.equal(context.reviewSectionChanged(before,after),true);
  context.before = before;
  context.after = after;
  vm.runInContext('affected = [after]; impactChanges = [{before,after}]; state = {slice_status:{}};',context);
  context.impactSummary();
  const summary = dom.window.document.getElementById('impact').textContent;
  assert.match(summary,/Affected slices · 2/);
  assert.match(summary,/Retained · Direct · Schema/);
  assert.match(summary,/Retained · Compatibility · Schema/);
});

test('rationale and instruction changes are recognized even when the section stamp is unchanged', () => {
  const before = {id:'guide',stamp:'same',reason:'Old rationale',instruction:'Old instruction'};
  const after = {id:'guide',stamp:'same',reason:'New rationale',instruction:'New instruction'};
  assert.equal(context.reviewSectionChanged(before,after),true);
  assert.deepEqual(Array.from(context.rationaleInstructionChanges(before,after)),['reason','instruction']);
  assert.deepEqual(Array.from(context.rationaleInstructionChanges(before,{...after,reason:'Old rationale'})),['instruction']);
});

test('live corrections retain navigation focus and unchanged buttons', () => {
  const sections = [{id:'menu', title:'Menu'}, {id:'schema', title:'Schema'}];
  context.updateNavigation(sections, 'menu');
  const nav = dom.window.document.querySelector('nav');
  const menu = nav.firstChild;
  menu.focus();
  context.updateNavigation([sections[0], {id:'schema', title:'Response schema'}], 'schema');
  assert.equal(nav.firstChild, menu);
  assert.equal(dom.window.document.activeElement, menu);
  assert.equal(nav.lastChild.textContent, 'Response schema');
  assert.equal(nav.lastChild.getAttribute('aria-current'), 'true');
  context.updateNavigation(sections.slice(1), 'schema');
  assert.equal(nav.children.length, 1);
});

test('visual register renders counts, attempt history, screenshot pairs, and annotated regions safely', () => {
  const images = {case_id:'checkout',state:{route:'/checkout'},viewport:{width:1280,height:800},
    full_screenshot:'visual-integrity/slice-a/evidence/full.png',closeup_screenshot:'visual-integrity/slice-a/evidence/close.png',
    comparisons:[{finding_id:'clip',before:'visual-integrity/slice-a/evidence/before.png',latest:'visual-integrity/slice-a/evidence/latest.png',
      annotated_before:'visual-integrity/slice-a/evidence/annotated-before.png',annotated_latest:'visual-integrity/slice-a/evidence/annotated-latest.png',
      normalized_rect:{x:.1,y:.2,width:.3,height:.4},latest_rect:{x:.5,y:.3,width:.2,height:.1}}]};
  images.browser_evidence = {blueprint_measurements: [
    {check_id:'menu-gap', expected:'16px', actual:'4px'},
    {check_id:'trigger-radius', expected:'8px', actual:'8px'}
  ]};
  const attempts = [
    {slice_id:'slice-a',status:'unresolved',attempt_id:'a1',attempt_number:1,recorded_at:'2026-01-01',cases:[images],
      findings:[{finding_id:'clip',case_id:'checkout',severity:'major',description:'Menu is clipped',status:'open',
        correction:'Attempt 1: adjusted padding.'}],rechecked_findings:[]},
    {slice_id:'slice-a',status:'corrected',attempt_id:'a2',attempt_number:2,recorded_at:'2026-01-02',cases:[images],
      findings:[{finding_id:'clip',case_id:'checkout',severity:'major',description:'Menu is clipped',status:'fixed',
        correction:'Attempt 2: moved the menu into the viewport.',verification:'Opened and closed the menu at the approved viewport.'}],rechecked_findings:['clip']},
    {slice_id:'slice-b',status:'clean',attempt_id:'b1',attempt_number:1,recorded_at:'2026-01-03',cases:[{...images,
      full_screenshot:'visual-integrity/slice-b/evidence/full.png'}],findings:[],rechecked_findings:[],observations:[{dimension:'interaction',result:'PASS',note:'<img src=x onerror=alert(1)>'}]}
  ];
  context.attempts = attempts;
  vm.runInContext('renderVisualRegister(attempts)', context);
  const d = dom.window.document;
  assert.match(d.getElementById('register-counts').textContent, /3 attempts · 1 clean evidence records · 1 distinct findings \(0 open, 1 corrected\) · 2 slices/);
  assert.match(d.getElementById('register-entries').textContent, /menu-gap · Fail · Expected 16px · Observed 4px/);
  assert.match(d.getElementById('register-entries').textContent, /trigger-radius · Pass · Expected 8px · Observed 8px/);
  assert.equal(d.querySelectorAll('.register-finding').length, 1);
  assert.equal(d.querySelectorAll('.register-attempt.status-clean').length, 1);
  assert.equal(d.querySelectorAll('.register-comparison .register-image img').length, 8);
  assert.equal(d.querySelector('.register-region').style.left, '10%');
  assert.equal(d.querySelectorAll('#register-entries img').length, 14);
  assert.equal(d.querySelector('#register-entries img[onerror]'), null);
  assert.match(d.getElementById('register-entries').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(d.getElementById('register-entries').textContent, /Attempt history · 2 finding records/);
  assert.match(d.getElementById('register-entries').textContent, /Correction: Attempt 2: moved the menu into the viewport/);
  assert.match(d.getElementById('register-entries').textContent, /Correction: Attempt 1: adjusted padding/);
  assert.match(d.getElementById('register-entries').textContent, /Verification: Opened and closed the menu at the approved viewport/);
  assert.match(d.getElementById('register-entries').textContent, /Verified by Codex · Optional user review/);
  assert.equal(d.querySelectorAll('.register-current-comparison .register-image img').length, 2);
  assert.equal(d.querySelector('.register-current-comparison').closest('details'), null);
  assert.match(d.querySelector('.register-current-comparison').textContent, /Before ·/);
  assert.match(d.querySelector('.register-current-comparison').textContent, /Latest ·/);
  assert.equal(d.querySelectorAll('.register-current-comparison a[target="_blank"][rel="noopener noreferrer"]').length, 4);
  assert.equal(d.querySelectorAll('.register-current-comparison a[download]').length, 2);
  assert.match(d.querySelector('.register-current-comparison').textContent, /Open marked Before/);
  assert.match(d.querySelector('.register-current-comparison').textContent, /Download marked Latest/);
  assert.equal(d.querySelectorAll('.register-current-comparison .register-region')[0].style.left, '10%');
  assert.equal(d.querySelectorAll('.register-current-comparison .register-region')[1].style.left, '50%');
  d.getElementById('register-status').value = 'open';
  vm.runInContext('renderVisualRegister(attempts)', context);
  assert.match(d.getElementById('register-entries').textContent, /No current findings or clean evidence/);
  assert.equal(d.querySelector('#register-slice option[value="slice-a"]').textContent, 'slice-a');
});

test('visual register filters by slice, status, and free-text search', () => {
  const attempts = [
    {slice_id:'slice-a',status:'clean',attempt_id:'a1',attempt_number:1,cases:[],findings:[]},
    {slice_id:'slice-b',status:'unresolved',attempt_id:'b1',attempt_number:2,cases:[],
      findings:[{finding_id:'label',severity:'minor',description:'missing label',status:'open'}]}
  ];
  context.attempts = attempts;
  vm.runInContext('renderVisualRegister(attempts)', context);
  const d = dom.window.document;
  d.getElementById('register-slice').value = 'slice-b';
  d.getElementById('register-status').value = 'open';
  d.getElementById('register-search').value = 'missing label';
  vm.runInContext('renderVisualRegister(attempts)', context);
  assert.equal(d.querySelectorAll('.register-finding').length, 1);
  assert.match(d.getElementById('register-entries').textContent, /slice-b/);
  assert.doesNotMatch(d.getElementById('register-entries').textContent, /slice-a/);
});

test('unresolved coverage reflects each slice latest attempt and retains earlier evidence in history', () => {
  context.attempts = [
    {slice_id:'slice-a',status:'unresolved',attempt_id:'a1',attempt_number:1,
      observations:[{note:'Earlier missing coverage'}], cases:[], findings:[]},
    {slice_id:'slice-a',status:'clean',attempt_id:'a2',attempt_number:2,cases:[],findings:[]},
    {slice_id:'slice-b',status:'unresolved',attempt_id:'b1',attempt_number:1,
      observations:[{note:'Current missing coverage'}], cases:[],
      findings:[{finding_id:'spacing',case_id:'menu',status:'noted',classification:'design_concern'}]},
    {slice_id:'slice-c',status:'unresolved',attempt_id:'c1',attempt_number:1,cases:[],findings:[]},
    {slice_id:'slice-d',status:'unresolved',attempt_id:'d1',attempt_number:1,cases:[],findings:[]}
  ];
  const d = dom.window.document;
  d.getElementById('register-slice').value = '';
  d.getElementById('register-search').value = '';
  d.getElementById('register-status').value = 'open';
  vm.runInContext(`registerMetadata = {slices: {
    'slice-c': {status:'cancelled'}, 'slice-d': {status:'blocked',superseded_by:'blueprint-v2'}
  }}; renderVisualRegister(attempts)`, context);
  const entries = d.getElementById('register-entries');
  assert.doesNotMatch(entries.textContent, /Earlier missing coverage/);
  assert.match(entries.textContent, /slice-b · Unresolved coverage · Attempt 1/);
  assert.match(entries.textContent, /Current missing coverage/);
  assert.doesNotMatch(entries.textContent, /slice-[cd] · Unresolved coverage/);

  d.getElementById('register-status').value = '';
  vm.runInContext('renderVisualRegister(attempts)', context);
  assert.match(entries.textContent, /slice-a · Past unresolved coverage · Attempt 1/);
  assert.match(entries.textContent, /Earlier missing coverage/);
  assert.match(entries.textContent, /slice-c · Past unresolved coverage/);
  assert.match(entries.textContent, /slice-d · Past unresolved coverage/);
  vm.runInContext('registerMetadata = {}', context);
});

test('finding comparison matches both case and finding ID', () => {
  const attempt = {attempt_number:2,cases:[
    {case_id:'other-case',comparisons:[{finding_id:'menu',before:'wrong-case-before'}]},
    {case_id:'home',comparisons:[{finding_id:'other-finding',before:'wrong-id-before'},
      {finding_id:'menu',before:'right-before',latest:'right-latest',normalized_rect:{x:.1,y:.1,width:.2,height:.2}}]}
  ]};
  context.targetFinding = {finding_id:'menu',finding:{case_id:'home'},history:[{attempt,finding:{finding_id:'menu'}}]};
  const found = vm.runInContext('latestFindingComparison(targetFinding)',context);
  assert.equal(found.case.case_id,'home');
  assert.equal(found.comparison.before,'right-before');
});

test('register includes retry stop and independent/dependent slice status when available', () => {
  const attempt = {slice_id:'slice-a',status:'unresolved',attempt_id:'a3',attempt_number:3,
    retry_policy:{failed_repairs:{menu:2},automatic_repair_stopped:['menu'],next_step:'fresh investigation with a new diagnosis'},
    dependent_slices:['slice-b'],findings:[{finding_id:'menu',case_id:'home',dimension:'geometry',status:'open',description:'Menu remains clipped.'}],cases:[]};
  context.attempts = [attempt];
  dom.window.document.getElementById('register-slice').value = '';
  dom.window.document.getElementById('register-status').value = '';
  dom.window.document.getElementById('register-search').value = '';
  vm.runInContext('registerMetadata = {history_warnings:["slice-a: incomplete visual history"],next_action:"Repair slice-a and continue eligible independent slices",slices:{"slice-c":{status:"reported",independent:true}}}; renderVisualRegister(attempts)', context);
  const text = dom.window.document.getElementById('register-entries').textContent +
    dom.window.document.getElementById('register-project-status').textContent;
  assert.match(text, /Unsuccessful repair attempts: 2/);
  assert.match(text, /Automatic retries stopped; next step: fresh investigation with a new diagnosis/);
  assert.match(text, /Dependent slices affected at this inspection: slice-b/);
  assert.match(text, /Independent slice slice-c · reported/);
  assert.equal(dom.window.document.querySelector("#register-project-status [role=alert]").textContent, "slice-a: incomplete visual history");
});


test('an unverified repair and a protected difference keep their own register outcomes', () => {
  const attempts = [
    {slice_id:'slice-a',status:'improved_not_verified',attempt_number:1,
      findings:[{finding_id:'clip',case_id:'menu',status:'improved',classification:'confirmed_defect'},
        {finding_id:'shape',case_id:'menu',status:'noted',classification:'approved_difference'}]},
    {slice_id:'slice-a',status:'clean',attempt_number:2,findings:[]},
  ];
  const {findings} = context.summarizeVisualRegister(attempts);
  assert.deepEqual(Array.from(findings, item => item.status), ['improved', 'noted']);
});

test('cross-screen evidence is grouped by the change and searchable from a consuming slice', () => {
  const checks = Object.fromEntries(['visual','interaction','blueprint']
    .map(kind => [kind,{result:'not_checked',note:'Fixture unavailable'}]));
  context.sharedChanges = [{
    change_id:'shared-dropdown', summary:'Correct dropdown spacing', slice_id:'erp-slice', status:'unresolved',
    component_paths:['src/Dropdown.tsx'], affected_slices:['erp-slice','settings-slice'],
    screens:[{route:'/settings', reason:'Imports the shared dropdown', slice_ids:['settings-slice'], cases:[{
      case_id:'keyboard', state:{interaction:'open by keyboard'}, renderCase:{viewport:{width:390,height:844}},
      reason:'Narrow keyboard interaction', expectation:'Focus stays with the menu'
    }]}], baseline_cases:{}, attempts:[{recorded_at:'2026-11-21', findings:[], cases:[
      {case_id:'keyboard',status:'not_verified',checks,unavailable_reason:'Settings fixture unavailable',comparisons:[]}
    ]}]
  }];
  vm.runInContext("registerMetadata = {cross_screen_changes: sharedChanges}; $('register-slice').value = ''; $('register-status').value = 'open'; $('register-search').value = ''; renderVisualRegister([]);",context);
  const select = dom.window.document.getElementById('register-slice');
  select.value = 'settings-slice';
  vm.runInContext("renderVisualRegister([]);",context);
  const content = dom.window.document.getElementById('register-entries').textContent;
  assert.match(content,/Correct dropdown spacing · unresolved/);
  assert.match(content,/shared-dropdown · Originating slice erp-slice/);
  assert.match(content,/\/settings/);
  assert.match(content,/not verified/);
  assert.match(content,/Settings fixture unavailable/);
  const change = context.sharedChanges[0];
  change.current_status = 'stale';
  change.attempts[0].cases[0].status = 'verified';
  vm.runInContext("renderVisualRegister([]);",context);
  const register = dom.window.document.getElementById('register-entries');
  const [caseSummary, attemptSummary] = register.querySelectorAll('summary');
  assert.match(caseSummary.textContent,/not verified/);
  assert.match(attemptSummary.textContent,/2026-11-21 · verified/);
  vm.runInContext("registerMetadata = {}; $('register-status').value = '';",context);
});

test('baseline register shows the three design references and preserves unavailable originals', () => {
  const document = dom.window.document;
  const status = document.getElementById('register-status');
  status.add(new dom.window.Option('Original UI baselines', 'baselines'));
  context.baselineRows = [{
    definition: {baseline_id:'open-menu', state:{route:'/settings'}, reason:'Narrow toolbar',
      renderCase:{viewport:{width:390,height:844}},
      baseline_feature_ids:['menu'], source_paths:['src/menu.js']},
    slice_ids:['settings'], available:true, gap:null,
    original:{case:{case_id:'menu', full_screenshot:'visual-integrity/discovery-baselines/evidence/aaa.png'},
      revision:'original-source', captured_at:'original-time'},
    approved:{'approval-1':{version:'blueprint-1', features:[{classification:'KEEP'}],
      prototype_cases:[{case_id:'menu', reference_file:'references/approved.png', viewport:{width:390,height:844}}]}},
    actual:[{slice_id:'settings', case:{case_id:'menu', full_screenshot:'visual-integrity/settings/evidence/bbb.png'},
      revision:'implementation-source', captured_at:'updated-time'}],
  }, {
    definition: {baseline_id:'missing-menu', state:{route:'/projects'}, reason:'Project fixture',
      baseline_feature_ids:['project-menu'], source_paths:['src/project.js']},
    slice_ids:['projects'], available:false, gap:{reason:'Fixture unavailable'}, approved:{}, actual:[],
  }];
  vm.runInContext("registerMetadata = {visual_baselines: baselineRows}; $('register-slice').value = ''; $('register-status').value = 'baselines'; renderVisualRegister([]);", context);
  const entries = document.getElementById('register-entries');
  assert.match(entries.textContent, /Original UI.*original-source.*Approved design.*blueprint-1.*Implemented UI.*implementation-source/s);
  assert.match(entries.textContent, /Original unavailable.*Fixture unavailable/);
  assert.equal(entries.querySelectorAll('.register-comparison img').length, 2);
  assert.ok(entries.querySelector('img[src="/visual-integrity/design/approval-1/menu"]'));
  status.value = 'open';
  vm.runInContext('renderVisualRegister([])', context);
  assert.match(entries.textContent, /missing-menu/);
  assert.doesNotMatch(entries.textContent, /open-menu/);
  vm.runInContext("registerMetadata = {}; $('register-status').value = '';", context);
});


test('project quality displays documented warnings, check results and historical acceptance', () => {
  const panel = dom.window.document.createElement('section');
  panel.id = 'visual-quality-report';
  const results = dom.window.document.createElement('div');
  results.id = 'quality-results';
  panel.append(results);
  dom.window.document.body.append(panel);
  try {
    const quality = {
      status:'technically_complete_with_warnings', blockers:[], defects:[], repairs:[],
      slices:{'items-slice':{status:'verified_with_warnings', ui:true, reasons:[]}},
      warnings:[{slice_id:'items-slice', finding_id:'small-gap', severity:'minor',
        description:'The decorative gap is slightly wider.', comparison_count:1}],
      audit_current:true, audit_sha256:'abc', audit:{audit_id:'audit-1',
        checks:[{result:'pass', scenario:'Keyboard navigation', slice_ids:['items-slice']}]},
      acceptance_current:false,
    };
    context.renderQualityReport(quality);
    assert.match(results.textContent, /Technically complete — with warnings/);
    assert.match(results.textContent, /Verified with warnings/);
    assert.match(results.textContent, /Passed · Keyboard navigation/);
    assert.equal(results.querySelectorAll('img').length, 2);
    assert.doesNotMatch(results.textContent, /Accepted by the user/);
    context.renderQualityReport({...quality, status:'in_implementation', audit_current:false,
      acceptance:{audit_id:'audit-1'}, acceptance_current:false});
    assert.match(results.textContent, /Historical audit/);
    assert.match(results.textContent, /Prior user acceptance/);
  } finally {
    panel.remove();
  }
});
