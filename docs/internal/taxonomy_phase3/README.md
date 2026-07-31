# Phase 3 FAST shadow measurement preregistration

This directory defines the contract that must be completed before a
representative FAST shadow run. It does not select a dataset, invent
performance limits, or choose biological adjudication references.

Copy `fast_shadow_measurement_plan.template.json` to the external evidence
directory for the planned run, replace every placeholder, and set the
repository commit to the clean implementation commit that will be executed.
Keeping the populated plan outside this repository avoids the circular problem
of trying to embed a commit's own SHA inside that commit. Preserve the plan
read-only beside the run evidence and record the validator's plan SHA-256.

Validate all declared files, hashes, coverage classes, budgets, analysis
strata, and adjudication inputs:

```bash
python3 bin/validate_fast_shadow_measurement_plan.py \
  --plan /path/to/evidence/fast-shadow-plan-v1.json
```

Immediately before launching Nextflow, run the stronger preflight:

```bash
python3 bin/validate_fast_shadow_measurement_plan.py \
  --plan /path/to/evidence/fast-shadow-plan-v1.json \
  --preflight
```

Preflight additionally requires:

- the exact clean Git commit declared by the plan;
- the declared Nextflow and LAST binaries and versions;
- a successful accelerator qualification report matching Metal or CUDA;
- a candidate output directory that does not yet exist.

`--allow-dirty` exists only for validator development and tests. It must not be
used to qualify a production measurement.

The plan deliberately hard-codes the decision-neutral boundaries:

- `fast_filter_shadow` must be `true`;
- `taxonomy_behavior_change` must be `false`;
- the candidate and stable state IDs must differ;
- near ties must remain retained;
- input composition evidence and COI/ITS2 adjudication references must be
  content-addressed;
- the adjudication references must be explicitly independent of the FAST
  routing database.

The alignment-identity bins describe the LAST observations in the shadow TSV.
They are not treated as true per-read sequencing error. A separate,
content-addressed error-stratification protocol is required so the real
measurement defines that derivation before the results are inspected.

Passing this contract means the run is sufficiently preregistered to collect
routing-policy evidence. It does not make shadow disagreements biological
false positives or false negatives, and it does not qualify fixture data as
representative production data.
