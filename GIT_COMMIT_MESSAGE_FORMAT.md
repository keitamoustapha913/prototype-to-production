# Git Commit Message Format

Commit messages must be detailed and bracketed.

Use this structure:

```text
[fix/chore/feat/ ... etc][scope] concise subject

[Summary]
...

[Details]
...

[Safety]
...

[Validation]
...

[Comprehension]
...

[Rollback]
...
```

## Requirements

- Begin with a bracketed change type such as `[fix]`, `[chore]`, or `[feat]`.
- Include a second bracketed scope identifying the affected area.
- Keep the first line concise and outcome-oriented.
- Make every section explicit and sufficiently detailed for a reviewer to understand the change without reconstructing the work.
- Describe validation performed, including relevant commands or checks and their results.
- Document safety considerations and any known limitations.
- Explain rollback steps precisely, including whether a full revert or selective restoration is appropriate.
