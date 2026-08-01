# RTBioScan — Developer Handoff (taxonomy remediation + runtime reproducibility)

Audience: a developer seeing this repository for the first time, taking over an in-flight
line of work. Read this top to bottom once, then act. Every claim below was verified against
the code; where a fact must be re-verified before acting, it says so. **Do not trust prose over
the code — re-run the checks in §9.**

Branch: `agent/taxonomy-state-compatibility-phase-2-1a`. Main branch: `main`.

---

## 0. TL;DR — what to do next

The Phase 2 implementation backlog is committed and validated. The next stage is deliberately
measurement-only:

> Run the opt-in, decision-neutral FAST shadow collector on representative production reads,
> quantify target/off-target competition and HAC/SUP compute impact, and independently adjudicate
> a reviewed subset of disagreements. See §7 for the exact entry criteria and outputs.

Do not curate references or enforce a new routing/classification policy until that evidence exists.
The deterministic fixture proves downstream defects, but it does not measure their live incidence.

---

## 1. What RTBioScan is (orientation)

RTBioScan is a **real-time, round-based, stateful Oxford Nanopore (ONT) metabarcoding pipeline**
written in **Nextflow DSL1**, orchestrating Bash + Perl + Python + R. It basecalls ONT reads
(Dorado), demultiplexes, filters, clusters into OTUs, BLASTs against marker reference databases,
assigns taxonomy (TaxonKit), builds consensus sequences, and produces an HTML report — repeatedly,
across many "rounds" within one run, accumulating persistent state.

Read `AGENTS.md` and `CLAUDE.md` in the repository root before touching anything; they encode
the non-negotiable invariants. Summary of the invariants:

- **Round semantics, state persistence/accumulation, file/path naming, resume/cache behavior,
  and output structure must be preserved.** Do not change round naming, state-path construction,
  accumulated-file semantics, or continuation logic unless the current stage explicitly requires it.
- **Performance is a feature.** Avoid extra process launches, extra file reads/writes, repeated
  parsing, channel fragmentation, or cache invalidation from cosmetic changes.
- **Conservative, staged refactors only.** No full rewrites, no unrelated edits, keep the pipeline
  runnable and validated after each stage, commit per stage. `main.nf` stays high-level orchestration.

### Environment / how to run and test

- **Platform: macOS on Apple Silicon (this dev machine is an Apple M1 Pro, 32 GB, macOS 14.5,
  Metal 3).** Process scripts run under **bash 3.2** (the macOS default) — no bash-4 features,
  no `${var[@]}` on empty arrays under `set -u` without guards.
- **Nextflow: use 22.10.8** for full-round runs (`NXF_VER=22.10.8 ./nextflow ...`). The locally
  installed 23.04.1 no longer supports this DSL1 pipeline.
- **Tests: `python3.11 -m pytest tests/ -q`** (pytest lives in the python3.11 env; the default
  `python3`/3.13 has no pytest). The closeout suite is 1,050 passing tests;
  `test_shell_syntax.py` runs `bash -n` on every `bin/**/*.sh` and `shellcheck` when available
  (shellcheck is absent here → those skip).
- **Reference DBs, Dorado binaries, taxonomy dumps are gitignored** (large). They are provisioned
  locally. `db/`, `runtime/dorado/releases/`, `runtime/dorado/qualification/` are ignored.

### Key files

- `main.nf` — all Nextflow processes + a large preamble (validation, restart handling, the
  state-compatibility contract call, the toolchain fingerprint call, Dorado wiring).
- `bin/` — Perl/Bash/Python/R helpers. `bin/lib/` — shared shell/Perl libraries.
- `conf/` — profiles and, new in this work, `conf/state_compatibility/`, `conf/runtime_compatibility/`,
  `conf/taxonomy_regression/`.
- `tests/` — pytest integration tests. `docs/internal/taxonomy_phase2/README.md` is the running
  design log for this whole effort — **keep it updated as you go**.
- `results/temp/ongoing/state/<state_id>/_state/` — the rolling state directory.

### The marker / target model (critical to understand the taxonomy work)

- `params.targets = "COI|ITS2"`, `params.target_taxa = "Metazoa|Viridiplantae"`. So COI targets
  Metazoa (animals), ITS2 targets Viridiplantae (plants).
- The **FAST pre-filter** (`lastal` against a "tagged" decoy DB `db/targets_All_tagged_nr95`) labels
  each read `MARKER|TAXON` from its **first** LAST alignment (`awk '!seen[$1]++'` in
  `fast_on_target_detection`).
  A read is kept as a target only if its label matches `COI|Metazoa` / `ITS2|Viridiplantae`; other
  labels (`COI|Bacteria`, `COI|Fungi`, `COI|Human`, `COI|Archaea`, …) route the read **off-target**.
- **The FAST bacterial/fungal/human labels are an OPERATIONAL COMPUTE FILTER, not a taxonomy claim.**
  Their purpose is to route non-animal/non-plant reads away from expensive HAC/SUP basecalling
  (only target-labeled reads are HAC-basecalled). Everything downstream (OTU BLAST
  against marker DBs, TaxonKit) is the real taxonomy. Keep this framing — it drove the redesign of
  the taxonomy-regression benchmark from "is this bacterial?" to `retain_metazoa` / `retain_viridiplantae`
  / `exclude_off_target`.

---

## 2. The originating problem and where the investigation actually landed

**Original report:** "some bacteria reads are being misclassified as animal ones."

A long, adversarial investigation (many rounds, each independently verified against the code and
the databases) produced a **three-mode defect model** at the *reference-database* level:

- **Chain A** — a **bacterial-endosymbiont COI** sequence (Wolbachia, Rickettsia, the LR799917
  "uncultured bacterium" class) sits in the **animal** COI DB (`db/COInr98_2024Jun_RioNegro_Brazil`)
  under a **Metazoa/host-insect taxid** (documented BOLD contamination: endosymbiont amplicons
  deposited under host-arthropod taxonomy). The committed protocol-defined audit uses three
  checksummed, independently identified bacterial coxA controls and finds 44 query/reference rows
  on the shipped snapshot: five priority-review and 39 review. These counts define a review queue,
  not 44 contamination calls and not biological exhaustiveness. All five priority records are now
  confirmed reference-sequence contamination: the sequence evidence is bacterial/endosymbiont coxA
  even where the host specimen metadata may be valid. Local-only cross-family adjudication identifies
  24 lower-tier sequence/host-label-conflict candidates and leaves 15 explicitly unresolved. This
  lower-tier evidence does not by itself establish bacterial origin or which conflicting endpoint is
  wrong. If a bacterial read reaches the animal lane and hits a contaminated record,
  it inherits a clean Metazoa taxid → reported as an animal. The empty-kingdom guard cannot catch this
  (the taxid resolves cleanly to Metazoa).
- **Chain B1** — a genuinely bacterial sequence (`D11038`, *Bacillus* sp. PS3) in the animal DB under
  a **bacterial** taxid (1386). TaxonKit `{K}` (kingdom) is **empty** for bacteria (they have a
  superkingdom, no kingdom rank), and the JSON reporter's `is_kingdom_consistent`
  (`bin/report_round_json.pl:421`) treats empty kingdom as "keep" → bacterial ranks appear under COI.
- **Chain B2** — nine BOLD records under homonym bacterial genus taxids (1386/265/613/1372). Direct
  testing showed these are **not** genuine animal reads mislabeled by a homonym; they are foreign
  (bacterial-oxidase `ctaD` etc.) sequences at high query coverage — so B2-as-"animal-read-with-wrong-
  taxid" is **refuted**; they are simply off-target contamination in the animal DB. The
  `conf/taxonomy_regression/homonym_taxid_audit.tsv` records them with evidence tiers (5 high-confidence
  ≥97%, 5 moderate 80–90% but full-coverage to bacterial oxidase genes).

**CRITICAL, HARD-WON CONCLUSION — read this twice:** With **correct first-hit handling**, the FAST
filter routes clean *and* error-variant endosymbionts to `COI|Bacteria` **robustly** (measured
best-hit margins ~289–435 bits in favor of Bacteria; verified by direct blastn testing of the
committed fixtures). **No live upstream Chain-A FAST misroute has been demonstrated.** The
contaminated animal-DB records are a **latent downstream trap** — they only bite a read that has
*already* entered the animal lane. Earlier claims of a "deterministic Chain-A misroute" were a
**harness bug** (the taxonomy-replay harness kept the *last* alignment instead of the *first*; fixed,
with a regression). The `COI|Human` control is **off-target by policy** (Human is not in `target_taxa`),
so a human read routing to `COI|Human` is *correct* exclusion, not a false-exclusion — an earlier
"human false-exclusion" claim was wrong. The `COI|Bacteria` decoy bucket also contains an animal/human-
contaminated genome assembly (`SZWG01000034`), so **contamination is bidirectional**, but it currently
only narrows the human retention margin, it does not flip the outcome.

**Therefore:** the *live rate* of bacteria→animal (and of target false-exclusion) is **UNMEASURED**.
The correct instrument is the **opt-in FAST shadow instrumentation** (`bin/fast_filter_shadow.py`,
`--fast_filter_shadow true`, default false, decision-neutral): it records, per read, the first hit,
the best target and off-target hits/scores, the margin, retention, and joint disagreement metrics
(`current_excluded__target_leads` = candidate first-hit-order exclusion; `current_retained__offtarget_leads`
= candidate first-hit-order inclusion) plus reads/bases for basecalling-savings accounting. **Phase 3
should measure real incidence with this before committing behavior changes**, not act on the fixtures
as if they proved a dominant failure direction.

The deterministic **A/B1/B2 classification benchmark** (`conf/taxonomy_regression/`,
`bin/validate_taxonomy_classification_fixture.py`, `tests/test_taxonomy_classification_fixture.py`)
records *legacy* behavior separately from *corrected* dispositions, with versioned expectations, and
is designed to be the **shadow baseline** carried through Phase 3. It also includes the synthetic-taxid
collision cases (`-557`/`-561`, see §6/§8).

---

## 3. Runtime reproducibility — Phase 2.1a/2.1b/2.1c (DONE, committed)

Motivation: to deploy any taxonomy fix *attributably* (shadow → enforce, reversible), the runtime
had to become reproducible and version-bound first. Committed on this branch:

- **2.1a — State-compatibility contract** (`8107142`): `bin/state_compatibility_contract.pl` binds
  rolling state to a content-addressed identity (reference-manifest SHAs + taxonomy `.dmp` SHAs +
  classifier/scoring policy + targets/DB selections). Runs in the `main.nf` preamble; rejects resuming
  incompatible state (strict) or adopts legacy state on explicit request. Includes a **private,
  ownership/permission-checked attestation cache** so the ~6.4 GiB of reference hashing is a fast
  `stat` check on unchanged startups (with a `full` mode for CI/publish). Trust-boundary hardened
  (0700/0600, lstat symlink rejection, reject cache when references are group/world-writable).
- **2.1b — Taxonomy pinning + inherited-container retirement** (`706db78`): pinned the **June-2024
  NCBI taxonomy** as an immutable, checksummed release (`bin/install_taxonomy_release.pl`,
  `conf/state_compatibility/taxonomy_release_ncbi_2024-06-24.tsv`, installed read-only under the
  gitignored `db/taxonomy/releases/…`), removed the `~/.taxonkit` fallback, and **exports `TAXONKIT_DB`
  into the `blast_OTU_pretax` and `consensus` process scripts** so every taxonkit call uses the pinned
  dump. Also **retired the inherited `hecrp/nanortax:latest` container** (it belonged to a different
  pipeline, NanoRTax) — `docker`/`singularity` profiles are now fail-fast stubs, and the contract
  rejects resuming state produced under those profiles.
- **2.1c — Toolchain fingerprint** (`04cf86a`): `bin/runtime_toolchain_fingerprint.pl` +
  `bin/runtime_toolchain_probe.sh` capture the resolved runtime (BLAST 2.15.0, LAST 1542, TaxonKit
  0.14.2, SeqKit 2.6.1, Cutadapt 4.6, VSEARCH, CD-HIT, Samtools, Seqtk, R/DECIPHER/Biostrings, and
  Dorado binary/models/device/args) into the state identity as **schema v2**, with a fail-closed,
  operator-attested v1→v2 migration. A toolchain change now invalidates the contract and the Nextflow
  cache token. `conf/state_compatibility/reference_manifest_legacy_v1.tsv` and the toolchain policy
  files hold the pins. **These pins are load-bearing:** LAST **1542** must match the shipped LAST index
  (`db/targets_All_tagged_nr95.prj` records `version=1542`); TaxonKit **0.14.2** preserves the `{K}`
  kingdom/superkingdom semantics the whole taxonomy analysis depends on.

The `bin/validate_runtime.sh` behavioral validator (deterministic LAST first-hit routing fixture,
`{K}` kingdom matrix, production BLAST probe) exists and is CI-safe (structural) + runtime-validated.

---

## 4. Dorado / Metal — RESOLVED (do not re-open the wrong way)

A "Metal regression" appeared: Dorado 0.7.0 + v5 models failed on Metal with
`PytorchStreamReader failed reading file version` in an automation sandbox, having "worked earlier."
This spawned a lot of work. **Direct testing settled it:**

- **Dorado 0.7.0 + the v5 FAST model basecalls fine on Metal on this M1 Pro, consistently (5/5 runs,
  ~3.5k samples/s, ~6× CPU), with no error.** The failure is an **execution-context difference**
  (some automation sandboxes lack a valid Metal/GPU context; the native Terminal qualification did
  not). It is **not** a persistent regression, **not** v5-model-format-specific, and **not** a
  production problem. Production runs from a user/Terminal session with GPU access.
- Consequence: **0.7.0 + v5 is the production runtime.** Modern models, works on Metal. Qualify it
  (sustained throughput/latency) — see §6.
- **Dorado 0.2.3 recovery is an OPT-IN emergency fallback only, NOT the preferred runtime.** It was
  built and is functional and low-risk (default path untouched): `bin/dorado_basecaller_input_compat.sh`
  (single-POD5→directory adapter, since 0.2.3 needs a dir input), `--dorado_input_mode file|directory`
  (default `file`), `--dorado_summary_bin` (0.2.3 has no `summary` subcommand; use 0.7.0's, verified
  compatible), and `conf/runtime_compatibility/toolchain_dorado_0.2.3_v1.tsv` capturing the dual-binary
  identity. **Do not promote 0.2.3** — it downgrades to 2023-era v4.2 models (lower accuracy) and would
  force a full classification re-qualification. Keep it only for machines where 0.7.0's Metal genuinely
  can't run.
- **Retry hardening (`93649dc`):** `bin/lib/retry.sh` — backoff **300s→10s** (max ~20s across
  2 inter-attempt sleeps; total lock time also includes failed-command durations), and **error-aware
  fail-fast**: exit codes **126/127/132** (`dorado_retry_status_is_permanent`) and CLI-argument stderr
  (`dorado_retry_error_is_permanent`, matches `unknown/unrecognized argument`, `usage: dorado`) stop
  immediately; `PytorchStreamReader`/OOM/other non-zero exits stay retryable. The global Dorado lock is
  held across the short backoff (deliberate, to avoid concurrent GPU pressure).
- **Dorado failure-evidence preservation (`93649dc`):** `bin/validate_dorado_release.sh`
  now, on qualification failure, preserves an immutable (read-only 0444/0555), atomic, never-overwrite
  **evidence bundle** (`<report>.evidence/`) with stage logs, `dorado -vv`, resource limits, and a
  **privacy-allowlisted** hardware capture (Chipset Model, Metal Support, VRAM, Memory, macOS version;
  serials/UUIDs excluded), a `checksums.sha256` (tamper-evident), and a `failure-report.tsv` the
  `--report` path hard-links into. A **per-report `mkdir` lock** serializes concurrent attempts;
  HUP/INT/TERM preserve evidence and release the lock; SIGKILL/power-loss stale-lock recovery is
  documented. The **GPU-first promotion policy**: only `metal`/`cuda*` produce accelerator-candidate
  evidence; CPU is `diagnostic_only` (real-time needs GPU).
- **Mixed-version reporting regression (`ae6bc60`):** `tests/test_dorado_mixed_summary_reporting.py`
  + `tests/fixtures/dorado_mixed_0_2_3_0_7_0/` — genuine captured fixtures (SAM `@PG VN:0.2.3`, 0.7.0
  summary) run through the *actual* reporting scripts; verifies the mixed-version summary yields the
  complete RTBioScan row and the SUP cache-ID parser works.

---

## 5. Phase 2 closeout state

The implementation backlog was split into attributable commits on
`agent/taxonomy-state-compatibility-phase-2-1a`:

- `8107142`, `706db78`, `801fd9f`, `8974497`, `a7b0304`, `9e078c9`, `04cf86a` — state,
  taxonomy, runtime-lock, Dorado-release, qualification, failed-round, and toolchain foundations.
- `edfb396` — restore the consensus fail-fast extraction boundary test contract.
- `e7a0675` — model feeder prefix repair correctly across launches.
- `40d8caf` — make zero/one-read quality plotting emit placeholders safely.
- `93649dc` — Dorado retry classification, qualification locks, failure evidence, and GPU policy.
- `b29e57c` — opt-in Dorado 0.2.3 recovery runtime with dual-binary identity.
- `ae6bc60` — captured mixed-version Dorado reporting compatibility.
- `c90eb02` — decision-neutral FAST competition shadowing with rerun-safe atomic diagnostics.
- `c6b263f` — deterministic taxonomy A/B1/B2 benchmark and legacy replay.
- `ddca100` — public runtime parameter registry reconciliation.

Closeout validation on 2026-07-30:

- `python3.11 -m pytest -q tests`: **1,050 passed, 56 skipped, 1 xpassed**;
- `python3 bin/check_param_registry.py`: passed with 167 registered parameters;
- the complete LAST 1542 / BLAST 2.15.0 / TaxonKit 0.14.2 taxonomy replay matched
  `conf/taxonomy_regression/legacy_expected.tsv` byte-for-byte;
- FAST-shadow focused tests cover disabled reruns, missing output, forced copy failure, and atomic
  replacement without temporary-file residue;
- accelerator-vs-CPU qualification status is exercised behaviorally in `tests/test_dorado_release.py`.

One closeout suite run observed a timing-sensitive feeder-log assertion after the underlying orphan
files had already been cleaned. The test passed immediately in isolation and the next complete suite
was green. Treat it as an intermittent test-harness timing issue only if it recurs; it is not evidence
of a product regression.

No Phase 3 taxonomy behavior change is implemented. The remaining gates are representative shadow
measurement, reviewed reference curation/rebuild, ancestry eligibility, competitive downstream
classification, routing-policy evaluation, and sustained accelerator performance qualification.

---

## 6. Forward plan — Phase 3 (the actual taxonomy fix) and finish

Overarching discipline (from all prior rounds, non-negotiable): **shadow-mode → enforcement, one
attributable change per release; keep the pipeline runnable and validated after each stage; measure
before acting; every behavior change carries a versioned expectation in the A/B1/B2 benchmark
(legacy vs corrected); state must be reset/reclassified when references or the classifier change
(the 2.1c contract already enforces this).** Ordered stages:

Current scope decision (2026-07-31): functional correctness and unintended behavior are in scope;
performance calibration and biological-accuracy policy are deferred. Representative FAST shadow
measurement remains required before changing FAST routing, but it is not a prerequisite for repairing
a deterministic downstream reference defect reproduced by forced marker-lane BLAST. Keep downstream
COI/BLAST curation separate from any FAST/LAST reference or routing release.

1. **Deferred FAST measurement gate.** Before changing the FAST reference or routing behavior, run
   `--fast_filter_shadow true` on representative data and analyze the joint metrics: how many reads are
   `current_excluded__target_leads` (candidate false-exclusions) vs
   `current_retained__offtarget_leads` (candidate false-inclusions), and the basecalling reads/bases at
   stake. Qualification/POD5 wiring fixtures are not representative incidence evidence. This gate does
   not block downstream-only curation of already reproduced reference defects.
2. **Reference curation + synchronized rebuild** (attributable sub-releases; downstream BLAST and
   FAST/LAST releases remain separate):
   - First curate the downstream animal COI DB only. The exact LR799917 accession is correctly labelled
     `COI|Bacteria` in the FAST reference; separate near-identical host-labelled downstream records are
     the demonstrated defect. Use `conf/taxonomy_regression/chain_a_reference_candidates.tsv` as the
     reviewed priority subset and `chain_a_lower_tier_adjudication.tsv` for the broader local-only
     dispositions. Keep confirmed bacterial contaminants, cross-family conflict candidates, and
     unresolved records as separate release evidence classes; retain all unresolved records unchanged.
   - Rebuild the downstream COI BLAST index from that curated canonical FASTA, use a new reference
     manifest/state identity, and compare legacy versus corrected benchmark expectations. Do not touch
     the FAST/LAST source or index in this sub-release.
   - Remove/relabel `D11038` (B1) and correct the nine homonym taxids (B2).
   - Treat FAST-reference cleanup (including `SZWG01000034`) and any LAST rebuild as a separate,
     measurement-gated routing release. A LAST rebuild must retain the pinned LAST 1542 format, recheck
     first-hit ordering, publish a new manifest/state identity, and requalify routing behavior.
   - Audit the known 1,493-record FASTA/BLAST-index desync separately (the shipped index is an exact
     stale prefix of the FASTA); introduce any appended tail as its own reference version.
3. **Superkingdom/ancestry eligibility guard** (fixes B1/B2; **must** land after step 2's homonym fix,
   or it would drop genuine animals):
   - Decide eligibility by **taxid ancestry against a configured target-clade taxid**
     (Metazoa=33208 for COI, Viridiplantae=33090 for ITS2), tri-state: descendant→`Consistent`;
     resolves-but-not-descendant (incl. Bacteria/Archaea AND Fungi/other-eukaryote)→`Inconsistent`;
     unresolvable→`Unresolved` (never silently `Consistent`). **Do NOT decide on `{k}`/`{K}` rank
     strings** — rank fragility is what caused the empty-kingdom bug.
   - **The resolver MUST handle synthetic negative taxids** (the non-NCBI `db/DBnr_2024Jun_id2lineage.txt`
     has **42,368** synthetic keys, all Metazoa/Viridiplantae). A pure NCBI-ancestry test would mark all
     of them `Unresolved` and gut legitimate animal assignments. Resolve synthetic IDs via the id2lineage
     map, **marker-scoped as `(marker, synthetic_taxid)`** — the map is globally corrupt (≈1,066 IDs
     collide across markers, e.g. `-557`/`-561` appear as BOTH Metazoa and Viridiplantae; last-write-wins
     currently mis-resolves them). Step 2 should repair the synthetic namespace (globally unique IDs
     preferred) before enforcement. The benchmark's `-557`/`-561` cases are the regression for this.
   - Apply as an **assignment-eligibility** rule (not a display filter) consistently across OTU, read,
     consensus, JSON/HTML, and legacy TSV/treemap surfaces; reconcile `report_round_json.pl` and
     `append_reports.pl` (they currently disagree on empty kingdom). Preserve rejected/ambiguous rows in
     diagnostics with a reason.
4. **Competitive downstream classification** (the durable Chain-A defense; DB cleanup alone can't cover
   unknown/future contaminants): at OTU-representative/consensus granularity, evaluate against BOTH the
   animal marker DB and a curated bacterial/endosymbiont reference set and require an **animal-over-
   bacterial score margin**; below-margin → ambiguous/off-target. Constraints: current OTU BLAST uses
   `-max_target_seqs 1` which would **never** surface a bacterial competitor — the search must *guarantee*
   a best hit per taxonomic class (raise the cap or run a labelled combined/second search) and compare by
   raw/bit score, not cross-DB E-values; verify endosymbiont coverage; hold a runtime budget (this is a
   real-time pipeline).
5. **FAST routing hardening** (last, kept separate for attributable measurement): replace first-emitted-
   hit selection with **per-taxon best-score competition + a tri-state ambiguity margin** (near-ties are
   **retained or competitively rechecked, never excluded** on a missed positive threshold — a naïve binary
   gate would false-exclude near-margin targets like the `COI|Human`-competing human control). Validate
   with the *actual* LAST 1542 against the shipped index and realistic error profiles; measure the
   before/after routing change against the pre-rebuild baseline.
6. **Dorado 0.7.0 + v5 real-time qualification** (parallelizable): run the GPU-first qualification and a
   **sustained** FAST/HAC/SUP throughput + p95-latency measurement on Metal (the 1-read microbenchmarks
   are not throughput). Redesign follow-up: add exit-code classification tests, and note SUP may not be
   real-time on lower-memory Apple Silicon (8 GB M1) — qualify per intended hardware tier.

**Definition of done for the whole effort:** a bacterial-origin sequence cannot receive an animal
assignment merely because one reference carries a host taxid; all report surfaces consume the same
eligibility decision and agree; every reference/taxonomy/toolchain artifact is reproducible from a
checksummed manifest and bound into the state identity; incompatible cached/accumulated state cannot be
reused; and the committed A/B1/B2 (+ real-incident, if obtained) fixtures produce their corrected
expected outcomes.

---

## 7. The immediate next step, precisely

**Goal:** prepare the downstream-COI-only curation release from the now-dispositioned Chain-A queue.

The discovery half of this gate is now reproducible. `chain_a_audit_queries.tsv` declares three
independently supported bacterial controls by accession and sequence SHA-256.
`bin/audit_taxonomy_reference_candidates.py` uses only those sequences and declared BLAST/coverage
thresholds; it has no organism-name, sample, or problematic-read rule. The provenance artifact pins
the tool, parameters, source FASTAs, every BLAST index component, and the generated table. On the
shipped snapshot it emits 44 pending query/reference rows (five priority, 39 review), all explicitly
`pending_adjudication`.

The closed-set omission in the first local adjudication is corrected. The comparison graph now contains
all 39 review records plus only the five priority records explicitly marked `confirmed` in
`chain_a_reference_candidates.tsv`; anchors remain absent from the lower-tier output. The five priority
records are confirmed bacterial/endosymbiont reference-sequence contamination. The lower-tier artifact
records a different evidence class: 24 cross-family sequence/host-label-conflict candidates and 15
unresolved records. Twenty-three candidates have direct review-peer support; `GMODL3842-22` has direct
support from the confirmed `GBMIN70259-17` anchor at 96.018% identity over 452 nt and 95.763%
shorter-sequence coverage. The lower-tier rule establishes neither bacterial origin nor which endpoint
is wrong. The unresolved records were not treated as clean or contaminated by default.

1. Preserve `legacy_expected.tsv` and the shipped production FASTA/index byte-for-byte as the legacy
   release.
2. Regenerate both discovery and adjudication artifacts byte-for-byte. If the database, source FASTAs,
   tool, parameters, or scripts change, create a new audit snapshot rather than silently updating one.
3. Produce an explicit downstream-COI disposition manifest with three non-interchangeable reasons:
   five `confirmed_reference_sequence_contamination`, 24
   `cross_family_sequence_label_conflict_candidate`, and 15
   `unresolved_insufficient_local_discriminator`. For the correctness-first release, quarantining the
   24 conflict candidates is a conservative release-policy choice, not a biological-origin claim.
4. Build a new canonical downstream COI FASTA that quarantines the five confirmed contaminants and,
   once that policy is recorded in the release manifest, the 24 conflict candidates. Retain all 15
   unresolved records unchanged and list them in release diagnostics; do not silently relabel or remove
   them.
5. Build a new downstream BLAST index from that canonical FASTA and publish checksummed manifests plus
   a new reference/state identity. Do not touch the FAST/LAST source or index.
6. Add a separately versioned corrected benchmark expectation and compare it with the immutable legacy
   expectation across every report surface before considering release promotion.

Exit criteria for the next stage are a reproducible downstream-only reference release, explicit
quarantine manifest, retained-unresolved manifest, new state identity, and green legacy-versus-corrected
regressions. FAST/LAST cleanup, routing thresholds, representative incidence, throughput, and accuracy
calibration remain separate deferred work.

---

## 8. Non-negotiable constraints and gotchas (things that WILL bite you)

- **bash 3.2** in process scripts; **macOS `awk`** lacks GNU extensions (no `match(s,r,arr)` with a
  capture array; no `/* */`); prefer Python byte-replacement over the Edit tool for mixed-tab Perl blocks.
- **Do not "fix" the Metal failure by downgrading Dorado.** 0.7.0+v5 works on Metal (verified). The
  sandbox failure is execution-context; treat it as diagnostics only.
- **Do not decide taxonomic eligibility on rank strings.** Use taxid ancestry (§6.3). The empty-`{K}`
  bug is the cautionary tale.
- **Synthetic negative taxids are marker-scoped and collision-ridden** (`-557`/`-561`). Any global
  taxid resolver must handle them via the id2lineage map keyed by `(marker, taxid)`, and the namespace
  should be repaired before enforcement (§6.2/6.3).
- **LAST index ↔ LAST version coupling:** the shipped `.prj` says `version=1542`; the pin is 1542.
  A rebuild changes ordering and requires bumping the pin in lockstep and resetting state.
- **The FAST filter is an operational compute filter, not taxonomy.** Bacterial/fungal/human labels
  route reads away from expensive basecalling; `COI|Human` is off-target *by policy* (Human ∉ target_taxa).
- **`fast_filter_shadow` is opt-in, default false, decision-neutral** — it must never change FAST
  retention. The main.nf default basecaller path must remain byte-for-byte the legacy `awk '!seen[$1]++'`
  behavior; the shadow helper reproduces it and falls back to the legacy router on failure.
- **State safety:** any reference/classifier/toolchain change must go through the 2.1c contract (new
  identity → state reset/migration). Never resume state across incompatible identities; the contract
  already refuses, and container-produced state is rejected.
- **Keep `docs/internal/taxonomy_phase2/README.md` current** — it is the authoritative running log.

---

## 9. How to re-verify the situation yourself (do this before acting — don't trust this doc blindly)

```bash
# 1. Pipeline sanity and documented parameter coverage
cat AGENTS.md; git log --oneline -18; git status --short
grep -nE "target_taxa|targets =|blast_id_family" nextflow.config
python3 bin/check_param_registry.py

# 2. The taxonomy defect model, on real data (rebuild the tiny decoy DB in a scratch dir):
#    - endosymbionts route to COI|Bacteria robustly (Chain A is a downstream trap, not a live misroute)
#    - COI|Human is a separate off-target bucket (Human not in target_taxa)
grep -oE ">COI\|(Human|Metazoa|Bacteria)\b" db/targets_All_tagged_nr95.fa | sort | uniq -c
awk -F= '$1=="version"{print}' db/targets_All_tagged_nr95.prj   # must equal the LAST pin (1542)

# 3. Dorado + Metal actually works with modern models (this is the key myth-buster):
pod5=results/pod5/QUAL_EMPTY_V3/done_round_pod5/rtbioscan-dorado-qualification-5khz.pod5
model=bin/dorado/bin/dna_r10.4.1_e8.2_400bps_fast@v5.0.0
bin/dorado/bin/dorado basecaller -x metal --emit-sam "$model" "$pod5" >/tmp/m.sam 2>/tmp/m.err
grep -c '^@' /tmp/m.sam; tail -3 /tmp/m.err   # expect success, ~thousands samples/s, no PytorchStreamReader

# 4. The synthetic-taxid collisions that any eligibility resolver must handle:
grep -nE "^-557\b|^-561\b" db/DBnr_2024Jun_id2lineage.txt   # each appears as Metazoa AND Viridiplantae

# 5. Re-derive the deterministic legacy taxonomy snapshot
conda run -n RTBioScan python3 bin/validate_taxonomy_classification_fixture.py \
  --taxonomy-data-dir db/taxonomy/releases/ncbi-taxdump-2024-06-24

# 6. Tests (use python3.11; the suite is large)
python3.11 -m pytest tests/ -q
```

If any of §9 contradicts the prose above, **the code wins** — update this document and proceed.
