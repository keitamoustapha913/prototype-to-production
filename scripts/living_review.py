"""Local blueprint review documents, immutable revisions and a read-only viewer."""
import hashlib
import json
from collections import OrderedDict, namedtuple
from copy import deepcopy
import mimetypes
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

try:
    from scripts import review_revisions
    from scripts.review_watcher import FileChanges
except ModuleNotFoundError:
    import review_revisions
    from review_watcher import FileChanges

ROOT = Path(__file__).resolve().parents[1]
CATALOG = 'living-review/sections.json'
FORMATS = {'md', 'mmd', 'svg', 'html', 'json', 'png', 'jpg', 'jpeg', 'webp'}
LIBRARIES = {'mermaid.js': 'mermaid/dist/mermaid.min.js', 'markdown.js': 'marked/lib/marked.umd.js',
             'sanitize.js': 'dompurify/dist/purify.min.js'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def local(root, relative):
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()) or '..' in Path(relative).parts:
        raise ValueError(f'Expected an initiative-relative path: {relative}')
    return path


def strings(value):
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ValueError('Expected a list of nonempty strings')
    return value


CachedSection = namedtuple('CachedSection', ['inputs', 'section', 'source_stamp', 'reference_stamp'])


class Documents:
    def __init__(self, root):
        self.root = root
        self.cache = {}
        self.parsed_json = {}
        self.excerpts = {}
        self.observed = {}
        self.sampled = set()
        self.removal_notes = {}
        self.section_cache = {}
        self.section_specs = {}
        self.control_paths = {CATALOG, "manifest.json"}
        self.evaluated_ids = None
        self.catalog_specs = None
        self.catalog_removal_notes = []
        self.section_path_ids = {}
        self.packet_snapshot = None
        self.packet_signature = None

    def stamp(self, relative):
        self.sampled.add(relative)
        try:
            stat = local(self.root, relative).stat()
            stamp = stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
        except FileNotFoundError:
            stamp = None
        self.observed[relative] = stamp
        return stamp

    def read(self, relative):
        path = local(self.root, relative)
        stamp = self.observed[relative] if relative in self.sampled else self.stamp(relative)
        if stamp is None:
            raise FileNotFoundError(f'Missing review source: {path}')
        if relative not in self.cache or self.cache[relative][0] != stamp:
            data = path.read_bytes()
            self.cache[relative] = stamp, data, digest(data)
        return self.cache[relative][1:]

    def json(self, path):
        data, checksum = self.read(path)
        if path not in self.parsed_json or self.parsed_json[path][0] != checksum:
            self.parsed_json[path] = checksum, json.loads(data)
        return self.parsed_json[path][1]

    def content(self, spec):
        data, checksum = self.read(spec['source'])
        heading = spec.get('heading')
        if not heading:
            return data, checksum
        key = (spec['source'], heading)
        if key in self.excerpts and self.excerpts[key][0] == checksum:
            return self.excerpts[key][1]
        # Match headings outside fenced examples and retain their subsections.
        lines = data.decode().splitlines(keepends=True)
        start = end = None
        level = 0
        fence = ''
        for i, line in enumerate(lines):
            marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
            if marker:
                token = marker[1]
                if not fence:
                    fence = token
                elif token.startswith(fence):
                    fence = ''
                continue
            match = re.match(r'^(#{1,6}) +(.+?)\s*$', line)
            if fence or not match:
                continue
            if start is not None and end is None and len(match[1]) <= level:
                end = i
            if match[2] == heading:
                if start is not None:
                    raise ValueError(f'Ambiguous heading: {heading}')
                start, level = i, len(match[1])
        if start is None:
            raise ValueError(f'Missing heading: {heading}')
        data = ''.join(lines[start:end]).encode()
        result = data, digest(data)
        self.excerpts[key] = checksum, result
        return result

    def sections(self, manifest, dirty_paths=None, previous_sections=None):
        changed = set(dirty_paths or ())
        incremental = (dirty_paths is not None and previous_sections is not None and self.catalog_specs is not None
                       and changed and all(path in self.section_path_ids for path in changed))
        if incremental:
            affected_ids = set().union(*(self.section_path_ids[path] for path in changed))
            selected_specs = [self.section_specs[key] for key in affected_ids]
            sections = previous_sections.copy()
            self.evaluated_ids = affected_ids
            catalog = {'sections': self.catalog_specs, 'removals': self.catalog_removal_notes}
            packets, packet_signature = self.packet_snapshot, self.packet_signature
        else:
            self.evaluated_ids = None
            sections = {}
            self.observed[CATALOG] = self.stamp(CATALOG)
            catalog = self.json(CATALOG) if self.observed[CATALOG] is not None else {'sections': []}
            selected_specs = catalog.get('sections', []) if isinstance(catalog, dict) else []
            self.catalog_specs = deepcopy(selected_specs)
            self.catalog_removal_notes = deepcopy(catalog.get('removals', [])) if isinstance(catalog, dict) else []
        registry = manifest.get('slice_review', {})
        if not isinstance(catalog, dict) or not isinstance(catalog.get('sections'), list):
            raise ValueError('The review catalog requires a sections array')
        removal_notes = catalog.get('removals', [])
        if not isinstance(removal_notes, list):
            raise ValueError('The review catalog removals field requires an array')
        self.removal_notes = {}
        for note in removal_notes:
            if (not isinstance(note, dict) or not isinstance(note.get('id'), str)
                    or not note['id'] or note['id'] in self.removal_notes
                    or not isinstance(note.get('instruction'), str)
                    or not isinstance(note.get('reason'), str)
                    or not isinstance(note.get('correction_id', ''), str)
                    or not isinstance(note.get('decisions', []), list)
                    or not isinstance(note.get('consequential', False), bool)):
                raise ValueError('Each removal note needs a distinct section id, text, decisions, and consequential flag')
            decisions = strings(note.get('decisions', []))
            self.removal_notes[note['id']] = {
                'instruction': note['instruction'].strip(), 'reason': note['reason'].strip(),
                'correction_id': note.get('correction_id', '').strip(), 'decisions': decisions,
                'consequential': note.get('consequential', False),
            }
        if not catalog['sections']:
            if not incremental:
                self.section_cache.clear()
                self.section_path_ids.clear()
                self.packet_snapshot, self.packet_signature = {}, digest(encode({}))
            return sections
        if not incremental:
            packets = {k: self.json(v['contract_path']) for k, v in registry.get('selected', {}).items()}
            packet_signature = digest(encode(packets))
            self.packet_snapshot, self.packet_signature = packets, packet_signature
            self.control_paths = {CATALOG, 'manifest.json'}
            self.control_paths.update(v['contract_path'] for v in registry.get('selected', {}).values())
            self.section_path_ids = {}
            self.section_specs = {}
            for spec in self.catalog_specs:
                if isinstance(spec, dict) and isinstance(spec.get('id'), str):
                    self.section_specs[spec['id']] = spec
                    for path in (spec.get('source'), spec.get('reference')):
                        if path:
                            self.section_path_ids.setdefault(path, set()).add(spec['id'])
        active_ids = set()
        for spec in selected_specs:
            if not isinstance(spec, dict) or not all(isinstance(spec.get(field), str) for field in ('id','source')):
                raise ValueError('Every section requires a string id and source')
            for field in ('title', 'slice', 'heading', 'reason', 'instruction', 'reference', 'correction_id'):
                if field in spec and not isinstance(spec[field], str):
                    raise ValueError(f'{field} must be text')
            key = spec['id']
            active_ids.add(key)
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', key) or (key in sections and not incremental):
                raise ValueError(f'Invalid or duplicate section ID: {key}')
            path = spec['source']
            kind = Path(path).suffix[1:].lower()
            if spec.get('heading') and kind != 'md':
                raise ValueError(f'{key}: headings apply to Markdown sources')
            if kind not in FORMATS:
                raise ValueError(f'Unsupported review format: {path}')
            scope = spec.get('slice', '')
            cached = self.section_cache.get(key)
            trusted = dirty_paths is not None and cached is not None
            if trusted and cached.section['source'] == path and path not in changed:
                source_stamp = cached.source_stamp
            else:
                source_stamp = self.stamp(path)
            self.observed[path] = source_stamp
            reference_path = spec.get('reference')
            reference_stamp = None
            if reference_path:
                if trusted and cached.section.get('reference', {}).get('source') == reference_path and reference_path not in changed:
                    reference_stamp = cached.reference_stamp
                else:
                    reference_stamp = self.stamp(reference_path)
                self.observed[reference_path] = reference_stamp
            inputs = (spec, packet_signature, source_stamp, reference_stamp)
            if cached and cached.inputs == inputs:
                sections[key] = cached.section.copy()
                continue
            features = strings(spec.get('features', []))
            requirements = strings(spec.get('requirements', []))
            contracts = strings(spec.get('contracts', []))
            files = strings(spec.get('owned_paths', []))
            review_paths = [path]
            if spec.get('reference') and spec['reference'] != path:
                review_paths.append(spec['reference'])
            impact = {}
            for k, p in packets.items():
                reasons = []
                if k == scope:
                    reasons.append('slice owner')
                reasons.extend('feature: ' + f for f in features if f in p.get('feature_ids', []))
                reasons.extend('requirement: ' + r for r in requirements if r in p.get('baseline_ids', []))
                consumers = []
                for name in contracts:
                    shared = p.get('shared_contracts', {}).get(name, {})
                    if shared.get('role') == 'provides':
                        reasons.append('provides ' + name)
                    elif shared.get('role') == 'consumes':
                        consumers.append('consumes ' + name)
                for owner in p.get('allowed_paths', []):
                    reasons.extend(f'owns {owner}: {file}' for file in files if file == owner or
                                   (owner.endswith('/**') and file.startswith(owner[:-2])) or
                                   (file.endswith('/**') and owner.startswith(file[:-2])))
                for review_path in review_paths:
                    if (review_path in p.get('artifact_hashes', {}) or review_path in p.get('asset_hashes', {})
                            or review_path == p.get('guide_path')):
                        reasons.append(f'references {review_path}')
                if reasons or consumers:
                    impact[k] = {'kind': 'Direct' if reasons else 'Compatibility', 'reasons': reasons + consumers}
            if scope and scope not in impact:
                impact[scope] = {'kind': 'Draft owner', 'reasons': [f'Draft section assigned to {scope}']}
            while True:
                added = {k: {'kind': 'Compatibility', 'reasons': ['depends on ' + dep for dep in p['dependencies'] if dep in impact]}
                         for k, p in packets.items() if k not in impact and set(p['dependencies']) & impact.keys()}
                if not added:
                    break
                impact.update(added)
            local(self.root, path)
            error = None
            try:
                _, checksum = self.content(spec)
            except (FileNotFoundError, UnicodeError, ValueError) as exc:
                checksum, error = None, str(exc)
            section = dict(id=key, title=spec.get('title', key), source=path, kind=kind, blob=checksum,
                           heading=spec.get('heading'), slice=scope, features=features, requirements=requirements,
                           contracts=contracts, owned_paths=files)
            if spec.get('reference'):
                reference = spec['reference']
                ref_kind = Path(reference).suffix[1:].lower()
                if ref_kind not in {'png', 'jpg', 'jpeg', 'webp', 'svg'}:
                    raise ValueError(f'{key}: reference must be an image')
                try:
                    _, ref_hash = self.read(reference)
                except FileNotFoundError as exc:
                    ref_hash, error = None, str(exc)
                section['reference'] = dict(source=reference, kind=ref_kind, blob=ref_hash)
            section['content_stamp'] = digest(encode(section))
            if not isinstance(spec.get('consequential', False), bool):
                raise ValueError(f'{key}: consequential must be true or false')
            section.update(consequential=spec.get('consequential', False),
                           decisions=strings(spec.get('decisions', [])),
                           reason=spec.get('reason', '').strip(), instruction=spec.get('instruction', '').strip(),
                           correction_id=spec.get('correction_id', '').strip())
            section['stamp'] = digest(encode([section, impact.get(scope)]))
            section.update(error=error, impact=impact)
            sections[key] = section
            self.section_cache[key] = CachedSection(inputs, section.copy(), source_stamp, reference_stamp)
        if not incremental:
            for key in self.section_cache.keys() - active_ids:
                del self.section_cache[key]
        return sections


def scope_stamp(section, scope):
    if scope == section['slice']:
        return section['stamp']
    return digest(encode([section['stamp'], scope, section['impact'][scope]]))


def bindings(sections, scope):
    return {k: scope_stamp(s, scope) for k, s in sections.items()
            if (scope in s['impact'] if scope else not s['slice'])}


def removal_bindings(removals, scope):
    """Bind removed sections and their durable explanations to each relevant receipt."""
    result = {}
    for key, removal in removals.items():
        if not removal.get('active'):
            continue
        scopes = set(removal.get('affected_scopes', []))
        if scope in scopes:
            result[f'removed:{key}'] = removal['stamp']
    return result


def review_bindings(sections, scope, removals=None):
    return bindings(sections, scope) | removal_bindings(removals or {}, scope)


def note_signature(section):
    return digest(encode([section.get(field, '') for field in ('instruction', 'reason', 'correction_id')]))


def track_corrections(sections, previous, previous_revision=None):
    for key, section in sections.items():
        prior = previous.get(key, {})
        pending = prior.get('pending_note')
        content_changed = bool(prior.get('content_stamp') and
                               (section['content_stamp'] != prior['content_stamp']
                                or section.get('reference') != prior.get('reference')))
        note_changed = bool(prior and note_signature(section) != note_signature(prior))
        impact_changed = bool(prior and section.get('impact') != prior.get('impact'))
        if content_changed:
            # Hold the original display baseline while a correction is still
            # being drafted. Each intermediate source save gets its own
            # revision, but all compare against the beginning of that draft.
            section['correction_base_revision'] = (
                prior.get('correction_base_revision') if pending and prior.get('correction_base_revision')
                else previous_revision)
        elif note_changed:
            # A note-only change compares with the previous note. If it
            # documents a source draft, retain that draft's original baseline.
            section['correction_base_revision'] = (
                prior.get('correction_base_revision') if pending and prior.get('correction_base_revision')
                else previous_revision)
        elif impact_changed:
            # An impact-only catalog update starts a fresh comparison; an old
            # correction boundary would point at stale content.
            section.pop('correction_base_revision', None)
        elif prior.get('correction_base_revision'):
            section['correction_base_revision'] = prior['correction_base_revision']
        if content_changed and not pending:
            pending = note_signature(prior)
        has_note = bool(section['instruction'] and section['reason'])
        if pending and has_note and note_signature(section) != pending:
            pending = None
        if not has_note and (section['consequential'] or section['instruction'] or section['reason']
                             or prior.get('instruction') or prior.get('reason')):
            pending = pending or note_signature(section)
        if pending:
            section['pending_note'] = pending


def checkpoint(root, manifest, scope='', receipt=None):
    docs = Documents(root)
    sections = docs.sections(manifest)
    with History(root) as history:
        saved = history.sync(docs)
    removals = saved.get('removals', {})
    current = review_bindings(sections, scope, removals)
    if receipt is not None and current != receipt.get('visual_review', {}):
        raise ValueError(f'Review illustrations changed for {scope or "blueprint"}; review the revised specification')
    # Shared sections keep their owner's decision authority in every impacted review.
    decisions_by_owner = {}
    for key in bindings(sections, scope):
        section = sections[key]
        if set(section['decisions']) - manifest.get('conversation', {}).keys():
            raise ValueError(f'{key}: record the linked conversation decision first')
        if section['consequential']:
            if not section['decisions']:
                raise ValueError(f'{key}: link the consequential A+ decision')
            decisions_by_owner.setdefault(section['slice'], set()).update(section['decisions'])
    for key, removal in removals.items():
        if f'removed:{key}' not in current:
            continue
        if removal.get('note_pending'):
            raise ValueError(f'{key}: record the removal instruction and reason before approval')
        if (removal['section'].get('consequential') or removal.get('consequential')) and not removal.get('decisions'):
            raise ValueError(f'{key}: link the consequential removal decision before approval')
        if removal.get('decisions'):
            decisions_by_owner.setdefault(removal['section']['slice'], set()).update(removal['decisions'])
    if decisions_by_owner:
        try:
            from . import project_state
        except ImportError:
            import project_state
        try:
            review = project_state.slice_review_state(root, manifest)
            blueprint_decisions = set()
            for owner, ids in decisions_by_owner.items():
                if owner:
                    active_details = {record['exchange_id'] for record in review.active_slice_details(owner)
                                      if record.get('status') == 'decided'
                                      and review.blueprint.text(record.get('response'))}
                    ids = ids - active_details
                blueprint_decisions.update(ids)
            review.blueprint.decisions(sorted(blueprint_decisions), allow_empty=True)
        except project_state.StateError as exc:
            raise ValueError(str(exc)) from exc
    if bindings(sections, scope) and receipt is None:
        try:
            from scripts.review_media import mermaid_diagrams, validate_raster, validate_svg
        except ModuleNotFoundError:
            from review_media import mermaid_diagrams, validate_raster, validate_svg

        for k in bindings(sections, scope):
            item = sections[k]
            if item['error']:
                raise ValueError(item['error'])
            data = docs.content(item)[0]
            if item['kind'] in {'png', 'jpg', 'jpeg', 'webp'}:
                validate_raster(data, item['kind'], k)
            reference = item.get('reference')
            if reference and reference['kind'] in {'png', 'jpg', 'jpeg', 'webp'}:
                image_data = docs.read(reference['source'])[0]
                validate_raster(image_data, reference['kind'], k)
            if reference and reference['kind'] == 'svg':
                image_data = docs.read(reference['source'])[0]
                validate_svg(image_data, k)
            if item['kind'] == 'svg':
                validate_svg(data, k)
            if item['kind'] == 'json':
                json.loads(data)
            if item['kind'] in ('mmd', 'md'):
                import subprocess
                source = data.decode()
                if item['kind'] == 'md':
                    try:
                        from scripts.slice_guide import markdown_links
                    except ModuleNotFoundError:
                        from slice_guide import markdown_links
                    for target in markdown_links(source):
                        url = urlsplit(target)
                        if url.scheme or url.netloc or not url.path:
                            continue
                        linked = (local(root, item['source']).parent / unquote(url.path)).resolve()
                        if not linked.is_relative_to(root.resolve()) or not linked.is_file():
                            raise ValueError(f'{k}: missing or out-of-scope Markdown reference {target}')
                diagrams = [source] if item['kind'] == 'mmd' else mermaid_diagrams(source)
                if diagrams:
                    proc = subprocess.run(['node', str(ROOT / 'scripts/parse_mermaid.mjs')], input=json.dumps(diagrams),
                                          text=True, capture_output=True, timeout=30)
                    if proc.returncode:
                        raise ValueError(f'{k}: {proc.stderr.strip()}')
        with History(root) as history:
            saved = history.sync(docs)
            for key in current:
                if saved['sections'].get(key, {}).get('pending_note'):
                    raise ValueError(f'{key}: record the current correction instruction and reason before approval')
            if review_bindings(saved['sections'], scope, saved.get('removals', {})) != current:
                raise ValueError('Review sources changed during approval. Inspect the latest revision.')
    return current


class History:
    def __init__(self, root):
        self.saved = None
        self.epoch = secrets.token_hex(12)
        self.states = OrderedDict()
        self._payload = None
        self.manifest = None
        self.db = sqlite3.connect(local(root, '.living-review.sqlite3'), check_same_thread=False)
        self.db.executescript('CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, data BLOB);'
                              'CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY, state BLOB);'
                              'CREATE INDEX IF NOT EXISTS full_snapshots ON revisions(id) WHERE state IS NOT NULL;'
                              'CREATE TABLE IF NOT EXISTS section_edits (section_id TEXT, revision INTEGER, section BLOB,'
                              ' PRIMARY KEY(section_id,revision));'
                              'CREATE TABLE IF NOT EXISTS removals (id TEXT PRIMARY KEY, record BLOB NOT NULL);')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def sync_removals(self, docs, previous, sections, revision):
        """Persist deletions separately from the editable catalog and section snapshots."""
        records = {key: json.loads(raw) for key, raw in self.db.execute('SELECT id,record FROM removals')}
        for key, prior in previous.items():
            if key in sections:
                continue
            record = records.get(key)
            if record is None or not record.get('active'):
                history = list(record.get('history', [])) if record else []
                if record:
                    history.append({name: value for name, value in record.items() if name != 'history'})
                scopes = set(prior.get('impact', {}))
                if prior.get('slice'):
                    scopes.add(prior['slice'])
                else:
                    scopes.add('')
                record = {
                    'id': key, 'section': prior, 'active': True,
                    'affected_scopes': sorted(scopes), 'detected_revision': revision,
                    'instruction': '', 'reason': '', 'correction_id': '', 'decisions': [],
                    'consequential': False, 'history': history,
                }
            record['active'] = True
            records[key] = record

        for key, record in list(records.items()):
            if key in sections and record.get('active'):
                record['active'] = False
                records[key] = record
                continue
            if not record.get('active'):
                continue
            note = docs.removal_notes.get(key)
            if note:
                saved_note = {name: record.get(name, '') for name in
                              ('instruction', 'reason', 'correction_id', 'decisions', 'consequential')}
                if any(value for name, value in saved_note.items() if name != 'consequential') and saved_note != note:
                    history = record.setdefault('note_history', [])
                    prior_note = saved_note | {'stamp': record.get('note_stamp')}
                    if not history or history[-1] != prior_note:
                        history.append(prior_note)
                record.update(note)
                record['note_stamp'] = digest(encode(note))
            record['note_pending'] = not bool(record.get('instruction') and record.get('reason'))
            record['stamp'] = digest(encode([
                record.get('section'), record.get('affected_scopes'), record.get('instruction'),
                record.get('reason'), record.get('correction_id'), record.get('decisions'),
                record.get('consequential', False),
            ]))
            records[key] = record

        for key in docs.removal_notes:
            if key not in records or not records[key].get('active'):
                raise ValueError(f'{key}: removal note requires a previously recorded section deletion')

        persisted = {key: raw for key, raw in self.db.execute('SELECT id,record FROM removals')}
        changed_ids = set()
        for key, record in records.items():
            raw = encode(record)
            if persisted.get(key) != raw:
                changed_ids.add(key)
                record['updated_revision'] = revision
                raw = encode(record)
                records[key] = record
                self.db.execute('INSERT INTO removals(id,record) VALUES (?,?) '
                                'ON CONFLICT(id) DO UPDATE SET record=excluded.record', (key, raw))
        return records, changed_ids

    def sync(self, docs, dirty_paths=None):
        version = self.db.execute('PRAGMA data_version').fetchone()[0]
        incremental = bool(self.saved and version == self.saved[1] and dirty_paths is not None and dirty_paths
                          and docs.catalog_specs is not None and docs.control_paths.isdisjoint(dirty_paths)
                          and all(path in docs.section_path_ids for path in dirty_paths))
        if self.saved:
            stamps, saved_version, state = self.saved
            if saved_version == version:
                if dirty_paths is not None:
                    if not (set(dirty_paths) & stamps.keys()):
                        return state
                elif all(docs.stamp(path) == stamp for path, stamp in stamps.items()):
                    return state
        docs.sampled.clear()
        if not incremental:
            docs.observed.clear()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            transaction_version = self.db.execute('PRAGMA data_version').fetchone()[0]
            latest = self.db.execute('SELECT MAX(id) FROM revisions').fetchone()[0]
            if self.saved and (transaction_version != self.saved[1] or latest != self.saved[2]['revision']):
                # Another connection committed after the optimistic check. The
                # cached manifest and section map may now be stale, so reconcile
                # every watched input while holding the writer lock.
                incremental = False
                dirty_paths = None
                docs.sampled.clear()
                docs.observed.clear()
            version = transaction_version
            manifest = self.manifest if incremental else docs.json('manifest.json')
            sections = docs.sections(manifest, dirty_paths=dirty_paths,
                                     previous_sections=self.saved[2]['sections'] if incremental else None)
            if self.saved and self.saved[2]['revision'] == latest:
                previous = self.saved[2]['sections']
            else:
                previous = self.version(latest) if latest else {}
            candidates = (sections.keys() | previous.keys() if docs.evaluated_ids is None
                          else docs.evaluated_ids)
            evaluated = {key: sections[key] for key in candidates if key in sections}
            track_corrections(evaluated, previous, latest)
            changed_keys = [key for key in candidates if sections.get(key) != previous.get(key)]
            if latest is None or changed_keys:
                for key in changed_keys:
                    s = sections.get(key)
                    if s is None:
                        continue
                    ref = s.get('reference', {})
                    if ref.get('blob') and previous.get(key, {}).get('reference') != ref:
                        image, checksum = docs.read(ref['source'])
                        if checksum != ref['blob']:
                            raise ValueError('Reference image changed during save. Retry the revision.')
                        self.db.execute('INSERT OR IGNORE INTO blobs VALUES (?,?)', (checksum, image))
                    if not s['blob'] or previous.get(key, {}).get('blob') == s['blob']:
                        continue
                    raw, checksum = docs.content(s)
                    if checksum != s['blob']:
                        raise ValueError('Review source changed while saving. Retry the revision.')
                    self.db.execute('INSERT OR IGNORE INTO blobs VALUES (?,?)', (s['blob'], raw))
                initial_snapshot = encode(sections) if latest is None else None
                rev = self.db.execute('INSERT INTO revisions(state) VALUES (?)', (initial_snapshot,)).lastrowid
                if latest is not None:
                    self.db.executemany('INSERT INTO section_edits(section_id,revision,section) VALUES (?,?,?)',
                                        [(key, rev, encode(sections[key]) if key in sections else None)
                                         for key in changed_keys])
            else:
                rev = latest
            removals, changed_removals = self.sync_removals(docs, previous, sections, rev)
            if changed_removals and not changed_keys:
                rev = self.db.execute('INSERT INTO revisions(state) VALUES (NULL)').lastrowid
                for key in changed_removals:
                    record = removals[key]
                    record['updated_revision'] = rev
                    self.db.execute('UPDATE removals SET record=? WHERE id=?', (encode(record), key))
        approval = manifest.get('approvals', {}).get(manifest.get('active_approval_id'), {})
        # Immutable receipts remain on disk after a slice is reopened.
        register = manifest.get('slice_review', {})
        receipts = register.get('reviews', {})
        current_receipts = receipts if register.get('approval_id') == manifest.get('active_approval_id') else {}
        past_paths = [path for path in register.get('artifact_hashes', {})
                      if path.startswith('slice-proposals/') and '/reviews/' in path and path.endswith('.json')]
        approvals = [('', record) for record in manifest.get('approvals', {}).values()]
        approvals.extend((path.split('/')[1], docs.json(path)) for path in past_paths)
        approvals.extend(receipts.items())
        approved, approved_by_scope = {}, {}
        for scope, record in sorted(approvals, key=lambda entry: entry[1].get('approved_at', '')):
            reviewed = record.get('visual_review', {})
            approved_by_scope.setdefault(scope, {}).update(reviewed)
            approved.update(reviewed)
        groups = {scope: review_bindings(sections, scope, removals)
                  for scope in ['', *register.get('selected', {})]}
        current_by_scope = {'': approval.get('visual_review', {})}
        current_by_scope.update({scope: record.get('visual_review', {}) for scope, record in current_receipts.items()})
        current = {k: current_by_scope.get(s['slice'], {}).get(k) for k, s in sections.items()}
        current.update({key: current_by_scope[''].get(key) for key in removal_bindings(removals, '')})
        statuses = {k: ('Awaiting review' if k not in current_receipts else
                        'Approved' if current_by_scope[k] == group else 'Needs review')
                    for k, group in groups.items() if k}
        state = dict(slice_status=statuses, revision=rev, sections=sections, removals=removals,
                    approved=approved, current=current, approved_by_scope=approved_by_scope,
                    current_by_scope=current_by_scope, root=str(docs.root),
                    visual_review=groups, consent_hashes={k: digest(encode(v)) for k, v in groups.items() if v})
        for cache in (docs.cache, docs.parsed_json):
            for path in cache.keys() - docs.observed.keys():
                del cache[path]
        for key in list(docs.excerpts):
            if key[0] not in docs.observed:
                del docs.excerpts[key]
        observed = dict(docs.observed)
        # Section and removal edits advance revision. Receipts and approvals
        # can also change the display without editing a section.
        receipt_hashes = [docs.cache[path][2] for path in past_paths]
        signature = digest(encode([self.epoch, rev, docs.cache['manifest.json'][2], receipt_hashes]))
        etag = '"' + signature + '"'
        self.saved = observed, version, state
        self.manifest = manifest
        self._payload = None
        self.etag = etag
        self.states[etag] = state
        self.states.move_to_end(etag)
        while len(self.states) > 8:
            self.states.popitem(last=False)
        return state

    @property
    def payload(self):
        if self._payload is None and self.saved:
            self._payload = encode(self.saved[2])
        return self._payload or b'{}'

    def delta(self, etag):
        """Return a compact state patch if etag names a retained client base."""
        previous = self.states.get(etag)
        if previous is None or not self.saved:
            return None
        current = self.saved[2]

        def map_delta(before, after):
            sets, deletes, children = {}, [], {}
            for key in before.keys() - after.keys():
                deletes.append(key)
            for key, value in after.items():
                if key not in before:
                    sets[key] = value
                elif before[key] != value:
                    if isinstance(before[key], dict) and isinstance(value, dict):
                        nested = map_delta(before[key], value)
                        if nested['set'] or nested['delete'] or nested['children']:
                            children[key] = nested
                    else:
                        sets[key] = value
            return {'set': sets, 'delete': sorted(deletes), 'children': children}

        maps = {key: map_delta(previous[key], current[key]) for key in current
                if isinstance(current[key], dict) and isinstance(previous.get(key), dict)
                and current[key] != previous[key]}
        fields = {key: value for key, value in current.items()
                  if key not in maps and previous.get(key) != value}
        return encode({'fields': fields, 'maps': maps})

    def version(self, revision):
        return review_revisions.snapshot(self.db, revision)

    def previous(self, section_id, revision):
        history = review_revisions.section_history(self.db, section_id, revision)
        _, current = next(history, (None, None))
        if current and current.get('correction_base_revision'):
            start = current['correction_base_revision']
            _, baseline = next(review_revisions.section_history(self.db, section_id, start), (None, None))
            if baseline is not None:
                return dict(section=baseline, revision=start)

        # Older snapshots did not persist a correction boundary. Follow the
        # uninterrupted draft back to the last documented version.
        for prior_revision, prior in history:
            if prior is None:
                return None
            if prior == current:
                continue
            if current and prior.get('pending_note') and (
                    current.get('pending_note') == prior['pending_note']
                    or (not current.get('pending_note') and current.get('content_stamp') == prior.get('content_stamp'))):
                current = prior
                continue
            return dict(section=prior, revision=prior_revision)
        return None


def serve(root, port):
    docs = Documents(root)
    ui = ROOT / 'assets' / 'review-viewer'
    if port is None:
        port = 18000 + int(digest(str(root).encode())[:8], 16) % 30000
    if not 0 <= port <= 65535:
        raise ValueError('Viewer port must be between 0 and 65535')
    lock = Lock()
    with History(root) as history:
        changes = FileChanges(root)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with lock:
                    self.respond()

            def respond(self):
                if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                    self.send_error(403)
                    return
                route = urlsplit(self.path).path
                try:
                    if route == '/state':
                        before = history.saved
                        dirty = changes.changed_paths()
                        history.sync(docs, dirty_paths=dirty)
                        if dirty is None or before is None or (
                                before is not history.saved and before[0].keys() != docs.observed.keys()):
                            changes.refresh(docs.observed)
                        data = None
                        kind = 'application/json'
                    elif re.fullmatch(r'/revision/\d+', route):
                        data = encode(history.version(int(route.split('/')[-1])))
                        kind = 'application/json'
                    elif re.fullmatch(r'/previous/[a-z0-9_-]+/\d+', route):
                        _, _, key, rev = route.split('/')
                        prior = history.previous(key, int(rev))
                        data, kind = encode(prior), 'application/json'
                    elif re.fullmatch(r'/approved/[a-z0-9_-]+/[0-9a-f]{64}', route):
                        _, _, key, stamp = route.split('/')
                        latest = history.db.execute('SELECT MAX(id) FROM revisions').fetchone()[0]
                        versions = review_revisions.section_history(history.db, key, latest or 0)
                        item = next((dict(section=s, revision=n) for n, s in versions
                                     if s and (s['stamp'] == stamp or any(
                                         scope_stamp(s, scope) == stamp for scope in s['impact']))), None)
                        data, kind = encode(item), 'application/json'
                    elif re.fullmatch(r'/blob/[0-9a-f]{64}', route):
                        row = history.db.execute('SELECT data FROM blobs WHERE id=?', (route[6:],)).fetchone()
                        if not row:
                            self.send_error(404)
                            return
                        data, kind = row[0], 'application/octet-stream'
                    elif route == '/visual-integrity':
                        try:
                            from scripts import cross_screen, project_state, visual_baseline, visual_integrity
                        except ModuleNotFoundError:
                            import cross_screen
                            import project_state
                            import visual_baseline
                            import visual_integrity
                        warnings = []
                        with project_state.coordinator_lock(root):
                            manifest = json.loads(local(root, 'manifest.json').read_bytes())
                            records = visual_integrity.list_attempts(root, manifest=manifest, warnings=warnings)
                            warnings.extend(cross_screen.history_errors(root, manifest))
                            cross_screen_changes = cross_screen.review_changes(root, manifest)
                            baseline_errors = visual_baseline.history_errors(root, manifest)
                            warnings.extend(baseline_errors)
                            baselines = visual_baseline.catalog(root, manifest) if not baseline_errors else []
                        dispatchable = set()
                        next_action = manifest.get('next_action')
                        shared_blockers = {}
                        if manifest.get('slices'):
                            try:
                                packet = project_state.build_resume_packet(root, root.parents[2])
                                dispatchable.update(packet['dispatchable_slices'])
                                shared_blockers = packet['cross_screen_blockers']
                                next_action = packet['next_action']
                            except (project_state.StateError, OSError, ValueError, KeyError) as exc:
                                next_action = f'Cannot determine dispatchable slices: {exc}'
                        slices = {}
                        for slice_id, assignment in manifest.get('slices', {}).items():
                            dependencies = assignment.get('dependencies', []) if isinstance(assignment, dict) else []
                            slices[slice_id] = {
                                'status': 'blocked' if slice_id in shared_blockers else assignment.get('status'),
                                'superseded_by': assignment.get('superseded_by'),
                                'blockers': assignment.get('blockers', []) + shared_blockers.get(slice_id, []),
                                'dependencies': dependencies,
                                'independent': slice_id in dispatchable,
                            }
                        data, kind = encode({'attempts': records, 'cross_screen_changes': cross_screen_changes,
                                             'visual_baselines': baselines, 'project': {
                            'next_action': next_action, 'stage': manifest.get('stage'), 'slices': slices,
                            'history_warnings': warnings,
                        }}), 'application/json'
                    elif route.startswith('/visual-integrity/design/'):
                        try:
                            from scripts import visual_baseline
                        except ModuleNotFoundError:
                            import visual_baseline
                        approval_id, case_id = map(unquote, route[len('/visual-integrity/design/'):].split('/'))
                        manifest = json.loads(local(root, 'manifest.json').read_bytes())
                        data, kind = visual_baseline.approved_image(root, manifest, approval_id, case_id), 'image/png'
                    elif route.startswith('/visual-integrity/evidence/'):
                        try:
                            from scripts import visual_integrity
                        except ModuleNotFoundError:
                            import visual_integrity
                        relative = unquote(route[len('/visual-integrity/evidence/'):])
                        path = visual_integrity.resolve_evidence_path(root, relative)
                        data, kind = path.read_bytes(), 'image/png'
                    elif route[1:] in LIBRARIES:
                        data, kind = (ROOT / 'node_modules' / LIBRARIES[route[1:]]).read_bytes(), 'application/javascript'
                    elif route in ('/', '/viewer.js', '/comparisons.js', '/viewer.css'):
                        path = ui / (route[1:] or 'index.html')
                        data, kind = path.read_bytes(), mimetypes.guess_type(path)[0]
                    else:
                        self.send_error(404)
                        return
                    tag = history.etag if route == '/state' else '"' + digest(data) + '"'
                    if route == '/state' and self.headers.get('If-None-Match') == tag:
                        self.send_response(304)
                        self.send_header('ETag', tag)
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Vary', 'If-None-Match, X-Review-Delta')
                        self.end_headers()
                        return
                    is_delta = False
                    if (route == '/state' and self.headers.get('X-Review-Delta') == '1'
                            and self.headers.get('If-None-Match')):
                        patch = history.delta(self.headers['If-None-Match'])
                        if patch is not None:
                            data = patch
                            is_delta = True
                    if data is None:
                        data = history.payload if route == '/state' else b'{}'
                    self.send_response(200)
                    self.send_header('Content-Type', kind)
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('ETag', tag)
                    if is_delta:
                        self.send_header('X-Review-Delta', '1')
                    self.send_header('Cache-Control', 'no-store' if route == '/state' else 'no-cache')
                    if route == '/state':
                        self.send_header('Vary', 'If-None-Match, X-Review-Delta')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-src 'self' about:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                    self.end_headers()
                    self.wfile.write(data)
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                    if route == '/state':
                        changes.dirty = True
                    self.send_error(422, explain=str(exc))
        with ThreadingHTTPServer(('127.0.0.1', port), Handler) as server:
            print(f'Review viewer http://127.0.0.1:{server.server_port}/\nSources: {root}\nCatalog: {root / CATALOG}', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                changes.close()
