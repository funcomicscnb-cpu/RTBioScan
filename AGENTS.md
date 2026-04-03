# RTBioScan agent rules

RTBioScan is a large, stateful, round-based Nextflow DSL1 pipeline.

## Core invariants

Always preserve:
- round/state semantics
- restart and resume behavior
- output structure and file naming
- accumulated-state behavior
- existing interfaces between Groovy, bash, Perl, Python, and R
- Nextflow DSL1 compatibility

Do not introduce behavior changes unless explicitly requested.

## Refactor policy

- Work one stage at a time
- Do not combine stages
- Prefer minimal, local edits
- Inspect only the minimum code needed
- Do not read full large files unless strictly necessary
- Do not modify unrelated sections
- Prefer extraction into `bin/` or `lib/` only when explicitly requested or clearly beneficial
- Preserve existing variable names unless explicitly requested otherwise
- Keep diffs small and easy to review

## File-scope discipline

Unless explicitly requested:
- do not edit tests
- do not edit `lib/`
- do not edit process bodies
- do not edit unrelated preamble sections
- do not rename files
- do not move files

When asked to modify one section, restrict edits to that section and any newly created file required by the task.

## Nextflow / Groovy rules

- Maintain Nextflow DSL1 syntax and semantics
- Be careful with Groovy script scoping:
  - top-level methods are hoisted
  - script-body `def` variables are not visible inside hoisted methods
- Do not move logic into `lib/` if it depends on Nextflow script-context built-ins such as:
  - `file()`
  - `exit 1,`
  - `log.*`
  - `workflow.*`
  - `tuple()`
- In `lib/`, use plain Groovy/Java constructs only

## Shell compatibility rules

All shell code must remain compatible with:
- macOS default bash 3.2
- common Linux bash environments

Therefore:
- avoid bash features newer than bash 3.2
- do not use `mapfile`
- do not use associative arrays
- do not use `declare -n` or namerefs
- do not rely on GNU-only behavior unless guarded
- prefer POSIX-safe constructs when practical
- quote variables carefully
- keep `sed`, `awk`, and `grep` usage portable across macOS and Linux
- do not assume GNU `sed -r`; prefer portable forms
- do not assume `readlink -f` exists
- do not assume GNU `date` options exist
- if portability is uncertain, prefer Perl/Python already present in the repo or a simpler shell pattern

## External script policy

For scripts in `bin/`:
- keep interfaces explicit
- prefer environment variables or simple positional arguments
- do not add dependencies unless explicitly requested
- preserve current logging and exit behavior
- keep scripts easy to lint with `bash -n`

## Performance and execution safety

- Avoid extra process launches unless explicitly part of the task
- Avoid extra file I/O
- Avoid changes that could affect scheduling, locking, or concurrency
- Preserve lock semantics exactly
- Preserve restart-handler semantics exactly
- Preserve cached/rolling-state behavior exactly

## Testing and validation

After each change, prefer:
- narrow diff review first
- then targeted validation

Unless explicitly asked, do not rewrite tests preemptively.
Only change tests if the code change legitimately requires it or a brittle test assumption is proven.

When relevant, prefer checks such as:
- `bash -n` for shell scripts
- targeted pytest runs
- lightweight Nextflow smoke checks

## Response style for code tasks

- Be concise
- Return only the requested code, diff, or validation notes
- Do not add extra refactors
- Stop after completing the requested stage