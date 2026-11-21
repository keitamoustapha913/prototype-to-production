import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { marked } from 'marked';

const source = readFileSync(0, 'utf8');
const dom = new JSDOM(marked.parse(source));
const diagrams = [...dom.window.document.querySelectorAll('pre > code.language-mermaid')]
  .map((code) => code.textContent);
process.stdout.write(JSON.stringify(diagrams));
