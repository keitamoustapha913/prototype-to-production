# Living blueprint and slice review

Start one documentation viewer for the initiative during design discussion. Codex edits the Markdown, JSON and illustration sources and records consequential exchanges through the usual A+ conversation workflow. Keep the browser open while discussing the blueprint and individual guides.

Run these commands from the skill directory, substituting the application root and initiative ID.

```sh
python3 scripts/project_state.py review-viewer --project-root /path/to/app --initiative-id billing-redesign
python3 scripts/project_state.py review-revision --project-root /path/to/app --initiative-id billing-redesign
```

Open the loopback URL printed by the first command. Each initiative uses a stable default port so reopening the URL restores the browser position. Supply a consistent alternate `--port` if the default is occupied. Install the local rendering libraries once with `npm ci`. Raster approval checks use Pillow from `python3 -m pip install -r requirements.txt`. The second command saves the current revision even when the browser is closed and prints full source-root and section information, exact approval hashes and the consolidated impact. Give the user the absolute paths to the edited documents.

Cross-screen inspections appear in **Visual Review Register** grouped by the original shared-component change. Filter by an affected slice or search by route, change ID or finding. Expand each state for original and updated captures, marked image regions, browser results and outstanding warnings. [Cross-Screen Visual Regression Protection](cross-screen-regression.md)

For implemented slices, open **Visual Review Register** in this same viewer. Filter by slice or status and inspect the saved screenshots and repair history. The register is available when you reopen the initiative after delivery. Its [implementation verification workflow](visual-integrity.md) runs on the actual application. After technical completion, [Application Completion Review](final-review.md) brings together the delivered features, visual comparisons, verification, warnings, correction history and explicit acceptance.

## Register the review material

Create `living-review/sections.json` inside the initiative directory printed by init. Use a stable ID for each component, document section, slide illustration or schema. Point directly to the authoritative source. For a long Markdown document, use a unique heading to review one section and its subheadings. Several entries can reference different headings of one document. Register each image or diagram as its own entry when it needs independent comparison. To show the existing screen beside a proposed layout, set the section’s optional `reference` to an initiative-relative screenshot path. Each revision preserves both images.

```json
{
  "sections": [
    {
      "id": "erp-control",
      "title": "Open ERP",
      "source": "inputs/navigation/examples/erp-control.html",
      "slice": "navigation-shell",
      "features": ["erp-access"],
      "contracts": ["erp-selection"],
      "owned_paths": ["src/navigation/erp-menu.ts"],
      "instruction": "Use one dropdown for Classic and New.",
      "reason": "Keep the header compact while preserving both destinations.",
      "decisions": ["erp-selector-correction"]
    }
  ]
}
```

IDs and paths above are illustrative. Omit `slice` for blueprint material. Use actual recorded feature IDs, shared contract names, source paths and conversation exchange IDs. The optional `requirements` array identifies audited baseline requirements and links to packets that cover them. For Markdown, the optional `heading` value is the heading text without its leading hashes. Supported sources include Markdown with fenced Mermaid, standalone Mermaid, JSON, SVG, PNG, JPEG, WebP and small sandboxed HTML/CSS component previews. Keep previews self-contained. Use Markdown for narrative, Mermaid for architecture, sequences, state or ER diagrams, JSON for schemas, and SVG or HTML/CSS when spacing, appearance or hierarchy matters.

## Removing a review section

Do not silently drop a catalog entry. Remove it from `sections` and add a durable explanation to the catalog's `removals` array:

```json
{
  "sections": [],
  "removals": [
    {
      "id": "erp-control",
      "instruction": "Remove the separate ERP preview",
      "reason": "The approved navigation keeps all ERP destinations in the shared menu.",
      "decisions": ["erp-preview-removal"],
      "consequential": true
    }
  ]
}
```

The `id` must identify a section already saved by the viewer. Both the instruction and reason are required before approval. Set `consequential` when removing the review item reflects a material user decision; that requires a linked, answered A+ exchange in `decisions`. The viewer archives the prior section and its source bytes, keeps its preview available, and displays the removal explanation. A receipt includes a `removed:<id>` hash for the removal record and the section's former review scopes. Editing the explanation creates a new durable note revision and changes that hash, so every affected scope needs fresh review. Removing the catalog note later does not erase the saved explanation. A section that was never assigned to a slice is bound to the blueprint receipt; a scoped section remains bound to its former slice owner and affected slices.

## Correct, compare and resume

1. Edit the affected draft source and the corresponding authoritative specification. For frozen evidence, create a new draft and point the same section ID at it. Retain approved bytes and receipts. Record the actual user instruction and reason, set `consequential: true` and link the A+ decision for a material correction, and save the updated catalog. For a correction spanning several files, prepare new versioned sources first, then atomically replace the catalog to publish the complete comparison together. Run `review-revision` at each meaningful correction, particularly before closing a session.
   A source edit appears immediately. If the instruction and reason are missing or carried over from the previous correction, the viewer marks the revision as a draft. Update the note to make it eligible for approval. When two distinct corrections genuinely have the same instruction and reason, set a new `correction_id` in the catalog. A new section can start without a correction note. Consequential changes require an answered, current A+ decision.

2. The viewer automatically checks for saved changes. The default Before panel shows the illustration at the start of the correction. Saving intermediate drafts and then adding the explanation keeps that starting point. You can select the last approved version, inspect changed JSON fields, Mermaid statements or source lines, or expand the context. Later saves preserve your chosen comparison and context while they apply. A missing source or failed illustration shows an error. The last successfully displayed illustration remains available in the same browser. Closing and reopening restores the selected section, comparison baseline, context and position.
3. For shared changes, inspect the consolidated impact list. The summary includes prior and current owners and consumers, including slices that lost a link. Direct entries cite the owning slice, feature references, shared contracts, referenced artifacts (including a comparison screenshot), or source ownership. Compatibility entries cite declared dependencies. Select any affected slice independently. Review ambiguous ownership with the user before locking. Keep raw Markdown and illustrations available through the CLI when browser rendering fails.
4. During discussion, save only affected sources and preview them. Defer scope checks to the blueprint or slice approval and cross-slice checks to collection validation. Leave the application, implementation tests and runnable prototype out of the live correction loop.

## Approval and package handoff

At the approval checkpoint run `review-revision`. Present the exact revision, affected requirements and document paths. Resolve broken illustrations, stale references, contradictions and outstanding decisions. For blueprint approval include the printed `visual_review[""]` map as the approval input’s `visual_review`. Include reviewed blueprint documents in the usual presentation evidence. For a slice use its map and add the printed `consent_hashes[slice_id]` as `visual_sha256` on its existing slice consent target. Include this hash in the displayed question. Supply the same section map as `visual_review` on the review decision. Keep the guide, proposal, handoff and review assets consistent with the displayed revision.

Visual receipts created by an earlier viewer that omitted correction notes or slice impact from its hashes require fresh consent. Their saved illustrations and decision records remain available.

Edits immediately make mismatched receipts stale. Reopen or amend the affected scope using the existing workflow, record a new decision and repeat scoped checks. Compatibility checks precede renewed consent. Validate the complete collection, lock the exact package, and obtain separate start authorization. An illustration revision records what was shown and confers no permission to implement.

Transfer the entire initiative, including `.living-review.sqlite3`, after stopping the viewer. It contains historical section snapshots, durable removal records and deduplicated source bytes for comparisons. The existing manifest, conversation records and immutable review receipts govern approval.
