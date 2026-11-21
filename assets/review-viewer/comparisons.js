'use strict';

const MAX_LCS_CELLS = 3000000;
function lineDiff(left, right) {
  const edits = [];
  let prefixLength = 0;
  let leftEnd = left.length;
  let rightEnd = right.length;

  while (prefixLength < leftEnd && prefixLength < rightEnd && left[prefixLength] === right[prefixLength]) {
    edits.push({type:'same', line:left[prefixLength]});
    prefixLength++;
  }

  const suffix = [];
  while (leftEnd > prefixLength && rightEnd > prefixLength && left[leftEnd - 1] === right[rightEnd - 1]) {
    leftEnd--;
    rightEnd--;
    suffix.push({type:'same', line:left[leftEnd]});
  }
  diffLineRange(left, prefixLength, leftEnd, right, prefixLength, rightEnd, edits);
  for (const edit of suffix.reverse()) edits.push(edit);
  return edits;
}
function diffLineRange(left, leftStart, leftEnd, right, rightStart, rightEnd, edits) {
  const leftLength = leftEnd - leftStart;
  const rightLength = rightEnd - rightStart;
  if (!leftLength) {
    for (let rightIndex = rightStart; rightIndex < rightEnd; rightIndex++) {
      edits.push({type:'ins', line:right[rightIndex]});
    }
    return;
  }
  if (!rightLength) {
    for (let leftIndex = leftStart; leftIndex < leftEnd; leftIndex++) {
      edits.push({type:'del', line:left[leftIndex]});
    }
    return;
  }
  if ((leftLength + 1) * (rightLength + 1) > MAX_LCS_CELLS) {
    const anchors = findUniqueLineAnchors(left, leftStart, leftEnd, right, rightStart, rightEnd);
    if (!anchors.length) {
      appendReplacement(left, leftStart, leftEnd, right, rightStart, rightEnd, edits);
      return;
    }

    let previousLeftEnd = leftStart;
    let previousRightEnd = rightStart;
    for (const anchor of anchors) {
      diffLineRange(left, previousLeftEnd, anchor.leftIndex, right, previousRightEnd, anchor.rightIndex, edits);
      edits.push({type:'same', line:left[anchor.leftIndex]});
      previousLeftEnd = anchor.leftIndex + 1;
      previousRightEnd = anchor.rightIndex + 1;
    }
    diffLineRange(left, previousLeftEnd, leftEnd, right, previousRightEnd, rightEnd, edits);
    return;
  }
  appendLcsDiff(left, leftStart, leftEnd, right, rightStart, rightEnd, edits);
}
function appendReplacement(left, leftStart, leftEnd, right, rightStart, rightEnd, edits) {
  for (let leftIndex = leftStart; leftIndex < leftEnd; leftIndex++) {
    edits.push({type:'del', line:left[leftIndex]});
  }
  for (let rightIndex = rightStart; rightIndex < rightEnd; rightIndex++) {
    edits.push({type:'ins', line:right[rightIndex]});
  }
}
function findUniqueLineAnchors(left, leftStart, leftEnd, right, rightStart, rightEnd) {
  const leftCounts = new Map();
  const rightCounts = new Map();
  const rightIndexes = new Map();
  for (let leftIndex = leftStart; leftIndex < leftEnd; leftIndex++) {
    const line = left[leftIndex];
    leftCounts.set(line, (leftCounts.get(line) || 0) + 1);
  }
  for (let rightIndex = rightStart; rightIndex < rightEnd; rightIndex++) {
    const line = right[rightIndex];
    rightCounts.set(line, (rightCounts.get(line) || 0) + 1);
    rightIndexes.set(line, rightIndex);
  }

  const candidates = [];
  for (let leftIndex = leftStart; leftIndex < leftEnd; leftIndex++) {
    const line = left[leftIndex];
    if (leftCounts.get(line) === 1 && rightCounts.get(line) === 1) {
      candidates.push({leftIndex, rightIndex:rightIndexes.get(line)});
    }
  }
  if (!candidates.length) return [];

  const longestIncreasingRightIndexes = [];
  const candidateAtLength = [];
  const previousCandidate = new Array(candidates.length).fill(-1);
  for (let candidateIndex = 0; candidateIndex < candidates.length; candidateIndex++) {
    const rightIndex = candidates[candidateIndex].rightIndex;
    let low = 0;
    let high = longestIncreasingRightIndexes.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (longestIncreasingRightIndexes[middle] < rightIndex) low = middle + 1;
      else high = middle;
    }
    longestIncreasingRightIndexes[low] = rightIndex;
    previousCandidate[candidateIndex] = low ? candidateAtLength[low - 1] : -1;
    candidateAtLength[low] = candidateIndex;
  }

  const anchors = [];
  for (let candidateIndex = candidateAtLength[candidateAtLength.length - 1]; candidateIndex >= 0;
       candidateIndex = previousCandidate[candidateIndex]) {
    anchors.push(candidates[candidateIndex]);
  }
  return anchors.reverse();
}
function appendLcsDiff(left, leftStart, leftEnd, right, rightStart, rightEnd, edits) {
  const leftLength = leftEnd - leftStart;
  const rightLength = rightEnd - rightStart;
  const columnCount = rightLength + 1;
  const table = new Uint32Array((leftLength + 1) * columnCount);
  for (let leftOffset = leftLength - 1; leftOffset >= 0; leftOffset--) {
    for (let rightOffset = rightLength - 1; rightOffset >= 0; rightOffset--) {
      const cell = leftOffset * columnCount + rightOffset;
      if (left[leftStart + leftOffset] === right[rightStart + rightOffset]) {
        table[cell] = table[(leftOffset + 1) * columnCount + rightOffset + 1] + 1;
      } else {
        table[cell] = Math.max(
          table[(leftOffset + 1) * columnCount + rightOffset],
          table[leftOffset * columnCount + rightOffset + 1]
        );
      }
    }
  }

  let leftOffset = 0;
  let rightOffset = 0;
  while (leftOffset < leftLength && rightOffset < rightLength) {
    const leftLine = left[leftStart + leftOffset];
    const rightLine = right[rightStart + rightOffset];
    if (leftLine === rightLine) {
      edits.push({type:'same', line:leftLine});
      leftOffset++;
      rightOffset++;
    } else if (table[(leftOffset + 1) * columnCount + rightOffset] >= table[leftOffset * columnCount + rightOffset + 1]) {
      edits.push({type:'del', line:leftLine});
      leftOffset++;
    } else {
      edits.push({type:'ins', line:rightLine});
      rightOffset++;
    }
  }
  while (leftOffset < leftLength) {
    edits.push({type:'del', line:left[leftStart + leftOffset]});
    leftOffset++;
  }
  while (rightOffset < rightLength) {
    edits.push({type:'ins', line:right[rightStart + rightOffset]});
    rightOffset++;
  }
}
function appendDiffGroups(details, edits, category = () => 'Changed source') {
  const groups = [];
  let group = null;
  for (const edit of edits) {
    if (edit.type === 'same') { group = null; continue; }
    const title = category(edit.line);
    if (!group || group.title !== title) {
      group = {title, old:[], next:[]};
      groups.push(group);
    }
    group[edit.type === 'del' ? 'old' : 'next'].push(edit.line);
  }
  for (const group of groups) {
    details.append(el('h4',group.title));
    for (const [tag,part] of [['del',group.old],['ins',group.next]]) {
      if (!part.length) continue;
      const pre = el('pre'); pre.append(el(tag,part.join('\n'))); details.append(pre);
    }
  }
}
function jsonChanges(before,after) {
  const changes = [];
  const walk = (left,right,path) => {
    if (Object.is(left,right)) return;
    if (left && right && typeof left === 'object' && typeof right === 'object' && Array.isArray(left) === Array.isArray(right)) {
      const keys = [...new Set([...Object.keys(left),...Object.keys(right)])].sort((a,b) => {
        const an = Number(a), bn = Number(b);
        return Number.isInteger(an) && Number.isInteger(bn) ? an - bn : a.localeCompare(b);
      });
      for (const key of keys) {
        const plainName = /^[A-Za-z_$][\w$]*$/.test(key) && !['__proto__','prototype','constructor'].includes(key);
        const childPath = Array.isArray(left) ? `${path}[${key}]` : plainName ? `${path}.${key}` : `${path}[${JSON.stringify(key)}]`;
        walk(Object.hasOwn(left,key) ? left[key] : undefined,Object.hasOwn(right,key) ? right[key] : undefined,childPath);
      }
      return;
    }
    changes.push({path:path || '$', before:left, after:right});
  };
  walk(before,after,'$');
  return changes;
}
function appendJsonChanges(details, changes) {
  for (const change of changes) {
    const row = el('div'); row.className = 'field-change';
    row.append(el('strong',change.path));
    for (const [tag,value] of [['del',change.before],['ins',change.after]]) {
      if (value === undefined) continue;
      const pre = el('pre'); pre.append(el(tag,JSON.stringify(value,null,2))); row.append(pre);
    }
    details.append(row);
  }
}
function mermaidCategory(line, diagramType) {
  const text = line.trim();
  if (/^(?:participant|actor)\b/i.test(text)) return 'Participants';
  if (/^(?:sequenceDiagram|graph|flowchart)\b/i.test(text)) return 'Diagram type';
  if (diagramType === 'sequenceDiagram' && /(?:->>|-->>|->|-->)/.test(text)) return 'Messages';
  if (/(?:-->|---|==>|-\.->|<--|<-->)/.test(text)) return 'Edges';
  return 'Diagram statements';
}
async function sourceChanges(before,after) {
  if (before.blob === after.blob) return null;
  const raws = await Promise.all([before,after].map(async s => (await read('/blob/' + s.blob)).text()));
  const details = el('details');
  if (before.kind === 'json' && after.kind === 'json') {
    try {
      const changes = jsonChanges(JSON.parse(raws[0]),JSON.parse(raws[1]));
      if (!changes.length) return null;
      details.append(el('summary','Changed JSON fields · ' + changes.length));
      appendJsonChanges(details,changes);
      return details;
    } catch {}
  }
  const edits = lineDiff(raws[0].split('\n'),raws[1].split('\n'));
  if (!edits.some(edit => edit.type !== 'same')) return null;
  details.append(el('summary',before.kind === 'mmd' && after.kind === 'mmd' ? 'Changed diagram statements' : 'Changed source lines'));
  const diagramType = before.kind === 'mmd' && after.kind === 'mmd' ?
    ((raws[1].match(/^\s*(sequenceDiagram|graph|flowchart)\b/m) || raws[0].match(/^\s*(sequenceDiagram|graph|flowchart)\b/m) || [])[1] || '') : '';
  appendDiffGroups(details,edits,before.kind === 'mmd' && after.kind === 'mmd' ? line => mermaidCategory(line,diagramType) : () => 'Changed source');
  return details;
}
