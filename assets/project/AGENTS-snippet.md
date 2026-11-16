<!-- Add to the application's existing AGENTS.md only with user authorization. Preserve all existing instructions. -->

## Active UI redesign initiative

For UI/UX changes involving the currently approved prototype, use the installed `$prototype-to-production` skill. The active initiative is `design/redesigns/<initiative-id>/` (replace the placeholder with the actual ID). **Before writing code, after context compaction, on a new session, or before a stage transition**, read its `manifest.json`, `CHECKPOINT.md`, approval records and relevant slice contract; run the skill's `resume` and `validate` commands; inspect current Git/code state. If records conflict, references are missing/changed, or scope is undefined, pause and ask for clarification. Record material decisions and code-linked progress immediately in project files. No agent may treat chat memory as the authoritative approval. Workers write only their slice-local reports; the coordinator manages canonical state.
