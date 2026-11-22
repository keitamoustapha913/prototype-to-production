import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

// Mermaid sanitizes labels while parsing flowcharts. Supply an inert DOM whose
// script execution remains disabled; this CLI never renders the resulting SVG.
const { JSDOM } = await import('jsdom');
const { window } = new JSDOM('', { runScripts: 'outside-only' });
globalThis.window = window;
globalThis.document = window.document;
globalThis.DOMPurify = (await import('dompurify')).default(window);
const { default: mermaid } = await import('mermaid');

// Resolve the pinned dependency from the skill root, not the caller's working directory.
const require = createRequire(new URL('../package.json', import.meta.url));
const packageVersion = require('mermaid/package.json').version;
if (packageVersion !== '12.3.0') {
  throw new Error(`Expected pinned Mermaid 12.3.0, found ${packageVersion}`);
}

mermaid.initialize({ startOnLoad: false });
const sources = JSON.parse(readFileSync(0, 'utf8'));
const result = [];
for (const source of sources) {
  const parsed = await mermaid.parse(source);
  const type = parsed.diagramType;
  const ast = ['stateDiagram', 'sequence'].includes(type)
    ? (await mermaid.mermaidAPI.getDiagramFromText(source)).parser.parse(source)
    : [];
  const flat = [];
  const visit = (value) => {
    if (Array.isArray(value)) return value.forEach(visit);
    if (value && typeof value === 'object') {
      flat.push(value);
      Object.values(value).forEach(visit);
    }
  };
  visit(ast);
  const stateId = (id) => id === 'root_start' || id === 'root_end' ? '[*]' : id;
  const rawStateEdges = flat.filter((item) => item.stmt === 'relation')
    .map((item) => [stateId(item.state1?.id), stateId(item.state2?.id), item.description ?? ''])
    .filter(([from, to]) => from && to);
  const stateIds = [...new Set([
    ...flat.filter((item) => item.stmt === 'state').map((item) => item.id),
    ...(Array.isArray(ast) ? ast.filter((item) => typeof item === 'string') : []),
    ...rawStateEdges.flatMap(([from, to]) => [from, to]),
  ].filter((id) => id && !['[*]', 'root_start', 'root_end'].includes(id)))];
  // Mermaid emits the same AST shape for a quoted alias and an ID annotation.
  // Recognize explicit alias declarations in the source and verify them in the parsed AST.
  const aliases = new Map();
  if (type === 'stateDiagram') {
    const declaration = /(?:^|[;{}])[ \t]*state[ \t]+"([^"\r\n]+)"[ \t]+as[ \t]+([^\s;{}]+)(?=[ \t]*(?:[;{}]|%%|\r?$))/gm;
    for (const [, label, id] of source.matchAll(declaration)) {
      if (!stateIds.includes(id)
          || !flat.some(item => item.stmt === 'state' && item.id === id && item.description === label)) {
        throw new Error(`Mermaid state alias ${id} does not match its parsed declaration`);
      }
      if ((aliases.has(id) && aliases.get(id) !== label)
          || flat.some(item => item.stmt === 'state' && item.id === id
            && item.description && item.description !== label)) {
        throw new Error(`Mermaid diagram has conflicting state aliases for ${id}`);
      }
      aliases.set(id, label);
    }
  }
  const name = id => aliases.get(id)?.replace(/#(quot|34|35);/g,
    (_, entity) => entity === '35' ? '#' : '"') ?? id;
  const states = stateIds.map(name);
  if (states.includes('[*]') || new Set(states).size !== states.length) {
    throw new Error('Mermaid diagram has an ambiguous state label');
  }
  const stateEdges = rawStateEdges.map(([from, to, label]) => [name(from), name(to), label]);
  const participants = [...new Set(flat.filter((item) => item.type === 'addParticipant')
    .map((item) => item.actor).filter(Boolean))];
  const messages = flat.filter((item) => item.type === 'addMessage')
    .map((item) => [item.from, item.to, item.msg?.text ?? '']);
  const controlTypes = {
    altStart: ['alt', 'start', 'altText'], else: ['alt', 'branch', 'altText'], altEnd: ['alt', 'end', 'altText'],
    loopStart: ['loop', 'start', 'loopText'], loopEnd: ['loop', 'end', 'loopText'],
    optStart: ['opt', 'start', 'optText'], optEnd: ['opt', 'end', 'optText'],
    parStart: ['par', 'start', 'parText'], and: ['par', 'branch', 'parText'], parEnd: ['par', 'end', 'parText'],
    parAnd: ['par', 'branch', 'parText'],
    parOverStart: ['par_over', 'start', 'parOverText'],
    parOverAnd: ['par_over', 'branch', 'parOverText'],
    parOverEnd: ['par_over', 'end', 'parOverText'],
    criticalStart: ['critical', 'start', 'criticalText'],
    criticalOption: ['critical', 'branch', 'optionText'], option: ['critical', 'branch', 'optionText'],
    criticalEnd: ['critical', 'end', 'criticalText'],
    breakStart: ['break', 'start', 'breakText'], breakEnd: ['break', 'end', 'optText'],
  };
  const controlPrefix = /^(?:alt|else|loop|opt|par|critical|break)/i;
  const unsupportedControls = type === 'sequence'
    ? flat.filter((item) => typeof item.type === 'string' && controlPrefix.test(item.type)
      && !controlTypes[item.type] && !['rectStart', 'rectEnd'].includes(item.type))
    : [];
  if (unsupportedControls.length) {
    throw new Error(`Unsupported Mermaid sequence control AST events: ${unsupportedControls.map((item) => item.type).join(', ')}`);
  }
  const controls = type === 'sequence' ? flat.flatMap((item, index) => {
    const control = controlTypes[item.type];
    if (!control) return [];
    const [kind, action, labelField] = control;
    const labelValue = item[labelField];
    return [{ type: kind, action,
      position: flat.slice(0, index).filter((prior) => prior.type === 'addMessage').length,
      label: action === 'end' ? '' :
        (typeof labelValue === 'string' ? labelValue : labelValue?.text ?? '') }];
  }) : [];
  result.push({ type, states, state_edges: stateEdges, participants, messages, controls });
}
process.stdout.write(JSON.stringify(result));
