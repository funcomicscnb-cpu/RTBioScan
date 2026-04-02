RTBioScan coding rules:

- Work stage-by-stage (no multi-stage refactors)
- Inspect only minimal required code (avoid full-file reads)
- Do not modify process bodies unless explicitly requested
- Preserve round/state semantics and restart behavior
- Preserve output structure and file naming
- Avoid introducing new dependencies
- Prefer extraction (lib/, bin/) over inline complexity
- Maintain DSL1 compatibility
- Ensure macOS bash 3.2 compatibility for scripts