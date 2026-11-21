"""SQLite access to review snapshots and subsequent per-section revisions."""
import json


def snapshot(db, revision):
    if not db.execute('SELECT 1 FROM revisions WHERE id=?', (revision,)).fetchone():
        return {}
    base = db.execute('SELECT id,state FROM revisions WHERE id<=? AND state IS NOT NULL '
                      'ORDER BY id DESC LIMIT 1', (revision,)).fetchone()
    base_revision, sections = (base[0], json.loads(base[1])) if base else (0, {})
    changes = db.execute("""
        SELECT edit.section_id, edit.section FROM section_edits AS edit
        JOIN (SELECT section_id, MAX(revision) AS revision FROM section_edits
              WHERE revision > ? AND revision <= ? GROUP BY section_id) AS latest
        USING (section_id, revision)
    """, (base_revision, revision))
    for key, raw in changes:
        if raw is None:
            sections.pop(key, None)
        else:
            sections[key] = json.loads(raw)
    return sections


def section_history(db, key, through_revision):
    """Yield versions of one section, including deletions and older full snapshots."""
    rows = db.execute("""
        SELECT revision, section, 0 AS full FROM section_edits
        WHERE section_id=? AND revision<=?
        UNION ALL
        SELECT id, state, 1 FROM revisions WHERE id<=? AND state IS NOT NULL
        ORDER BY 1 DESC
    """, (key, through_revision, through_revision))
    for revision, raw, full in rows:
        value = json.loads(raw) if raw is not None else None
        yield revision, value.get(key) if full else value
