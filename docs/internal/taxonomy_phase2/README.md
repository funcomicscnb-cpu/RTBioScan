# Taxonomy remediation Phase 2: state compatibility contract

Phase 2 adds a compatibility boundary before any reference or classifier changes.
It does not change taxonomic assignments, scoring, reference contents, or report
behavior.

## Contract scope

At startup, after any requested restore or reset and before pipeline processes are
created, `bin/state_compatibility_contract.pl` binds the rolling state to:

- the SHA-256 of a versioned reference manifest;
- verified SHA-256 checksums of every artifact named by that manifest;
- the pinned TaxonKit release manifest and verified `nodes.dmp`, `names.dmp`,
  `merged.dmp`, and `delnodes.dmp` files;
- marker targets and target taxa;
- BLAST/LAST database and taxonomy-map selections;
- explicit classifier-policy and scoring-policy versions; and
- a schema-v2 fingerprint of the resolved command-line runtime, R consensus
  packages, and selected Dorado binary/models/device/effective arguments.

The resulting contract ID is written atomically to:

```text
<outdir>/temp/ongoing/state/<state_id>/_state/state_compatibility_manifest.tsv
```

The same file is included in rolling-state snapshots. Restored snapshots are
validated before any restored state can be consumed.

The contract ID is also interpolated into existing process cache tokens. A
contract change therefore changes the relevant Nextflow task signatures as well
as being rejected at the rolling-state boundary.

Execution profile and resolved filesystem paths are recorded for provenance but
do not change the contract ID. The live runtime backend, exact probed versions,
applicable Conda lock checksum, Dorado checksum, model configuration checksums,
and effective Dorado settings do change the schema-v2 contract ID.

## Default baseline

`conf/state_compatibility/reference_manifest_legacy_v1.tsv` records the
operational pre-remediation baseline:

- the FAST/LAST filter source and index;
- the installed COI and ITS2 BLAST indexes;
- the taxonomy memory seeds and synthetic-lineage map;
- the BLAST taxonomy database.

The manifest intentionally describes the installed operational indexes. It does
not activate the 1,493 line-start COI tail records or the embedded header
candidate subsequently identified by the strict source-integrity audit. Tail
repair/admission remains a separate, audited reference release; the raw FASTA
prefix is not an exact serialization of the effective legacy BLAST corpus.

`conf/state_compatibility/taxonomy_release_ncbi_2024-06-24.tsv` pins the exact
June-2024 TaxonKit baseline used before remediation. The source archive and all
four extracted files match the hashes captured in Phase 1. The default runtime
directory is:

```text
db/taxonomy/releases/ncbi-taxdump-2024-06-24
```

On the first verification, all reference-manifest entries and the four pinned
TaxonKit files are checked byte-for-byte. The resulting attestation is stored
in a private per-user cache. Later `cached` starts reuse verified hashes only
when device, inode, ownership, mode, size, mtime, and ctime are unchanged. A
changed entry alone is rehashed. `full` mode always rehashes every entry.

The configured FAST/LAST prefix, every marker BLAST prefix, BLAST taxonomy
directory, taxonomy-memory seed, and lineage map must also be represented in the
active manifest. A custom reference selection therefore requires a corresponding
reviewed manifest; it cannot reuse the legacy manifest identity. Operational
LAST, BLAST-v5, and BLAST taxonomy selections must contain their complete
expected component groups.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `state_compatibility_policy` | `strict` | `strict` rejects legacy state without a manifest; `adopt_legacy` permits one explicit adoption |
| `state_reference_manifest` | `conf/state_compatibility/reference_manifest_legacy_v1.tsv` | Checksummed operational reference set |
| `state_taxonomy_data_dir` | `db/taxonomy/releases/ncbi-taxdump-2024-06-24` | Required pinned TaxonKit runtime directory |
| `state_taxonomy_release_manifest` | `conf/state_compatibility/taxonomy_release_ncbi_2024-06-24.tsv` | Expected archive and runtime-file hashes |
| `state_reference_verification` | `cached` | `cached` uses private stat attestations; `full` rehashes every declared reference and taxonomy file |
| `state_verification_cache_dir` | empty | Cache root; empty derives `<outdir>/temp/_compatibility_cache`, with a private `uid-N` child |
| `state_classifier_policy_version` | `legacy-rank-string-v1` | Manual compatibility version for classifier semantics |
| `state_scoring_policy_version` | `legacy-first-single-hit-v1` | Manual compatibility version for scoring/selection semantics |
| `state_toolchain_policy_manifest` | `conf/runtime_compatibility/toolchain_legacy_v1.tsv` | Exact supported tool and R-package versions |
| `state_runtime_lock_manifest` | `auto` | In an activated Conda environment, select and bind the committed OS-specific lock; explicit paths are also accepted |
| `state_dorado_release_manifest` | empty | Optional qualified Dorado release manifest; an explicit installation without one is fingerprinted but recorded as unqualified |
| `state_contract_migration` | `strict` | `strict` rejects schema-v1 state; `attest_v1` performs the one-time explicit v1-to-v2 migration |

Any future behavior change to classifier or scoring semantics must increment the
corresponding version. Any reference change must use a new, reviewed manifest.

Empty per-marker `nonncbi_memtax` entries and an empty
`nonncbi_id2lineage_target` are valid compatibility inputs. The current
classifier emits no OTU lineage annotations when the lineage override file is
absent; startup warns about this degraded behavior. Adding a TaxonKit-only
fallback is deferred because it would change classification behavior.

Install the release from the audited archive with:

```bash
perl bin/install_taxonomy_release.pl \
  --archive /path/to/taxdump.tar.gz \
  --manifest conf/state_compatibility/taxonomy_release_ncbi_2024-06-24.tsv \
  --destination db/taxonomy/releases/ncbi-taxdump-2024-06-24
```

The installer is idempotent for an exact existing release and refuses to
replace mismatched data.

## Attestation trust boundary

The default cache is intended for a trusted single principal, including a
researcher-owned workspace used from an HPC job. Its user directory must be
owned by the effective user with mode `0700`; cache files must be regular,
non-symlink files owned by that user with mode `0600`. Unsafe cache files are
treated as absent and replaced only through a full verification.

Cached reuse is disabled when the reference root, effective taxonomy directory,
or a declared file is group/world writable. Use
`--state_reference_verification full` for CI, reference publication, initial
attestation seeding, group-shared installations, or any run crossing a trust
boundary.

Reference symlinks remain supported: their target bytes and target stat identity
are verified. Cache symlinks are not trusted.

Cached mode protects against ordinary accidental mutation; it is not a
cryptographic re-verification on every launch. The signature includes
high-resolution `ctime`, so a same-size edit with a restored `mtime` is detected
on filesystems that expose sub-second change times. Filesystems with coarse or
forgeable timestamps, same-account modification that can reproduce the complete
stat signature, administrator access, filesystem-specific ACL behavior,
writable ancestor directory replacement, and modification after startup
verification remain outside this cache's guarantee. Use full verification when
those risks apply. Production reference releases should use immutable,
content-addressed directories and read-only mounts or permissions.

## Phase 2.1b taxonomy packaging and propagation

Phase 2.1b removes the host-local `~/.taxonkit` dependency. The same resolved
directory is:

- verified by the host-side compatibility contract;
- exported as `TAXONKIT_DB` in both OTU and consensus taxonomy processes; and
- checked for visibility again inside the execution environment before
  classification starts.

Moving an existing Phase-2.1a state from `~/.taxonkit` to the canonical
directory is compatible only when all four content hashes remain identical.
The contract ID then stays unchanged and the state manifest records the former
taxonomy path. Any byte change remains an incompatible taxonomy change.

## Phase 2.1c resolved toolchain identity

`bin/runtime_toolchain_fingerprint.pl` probes the executables that will actually
run, rather than trusting a Nextflow profile name. The committed legacy policy
pins BLAST, LAST, TaxonKit, SeqKit, Cutadapt, VSEARCH, CD-HIT, Samtools, Seqtk,
R, DECIPHER, Biostrings, and Dorado. Missing tools, failed probes, non-executable
Dorado binaries, and version mismatches fail before any process launches.

An activated Conda runtime is distinguished from a host runtime and binds the
matching committed platform lock. The fingerprint also includes:

- the selected Dorado binary SHA-256 and exact version;
- FAST/HAC/SUP model names, `config.toml` checksums, and model-content
  identities;
- the selected device and effective per-stage basecaller arguments; and
- the qualified Dorado release-manifest checksum when one is supplied.

The device is deliberately compatibility-significant: changing, for example,
from `metal` to `cuda` or `cpu` makes an existing state incompatible because
backend-dependent floating-point behavior can change basecalls. Select the
recorded device again, use a new state, or explicitly reset; the pipeline never
silently combines results produced by different basecalling backends.

Supplying a qualified Dorado manifest additionally requires the selected binary
and model configurations to match that manifest and its installed release
layout; declared model artifacts must remain present at their pinned sizes, and
the complete manifest supplies their content identity. Without one, the
explicit binary and models remain usable for compatibility with existing
installations, but their directories are fully hashed, startup warns, and the
state records `unqualified_explicit`. Promotion evidence still requires
`validate_dorado_release.sh`; a state fingerprint is not a hardware
qualification.

Schema v1 did not record a runtime fingerprint. Its history therefore cannot be
reconstructed cryptographically. Migration is fail-closed and requires all
legacy reference/taxonomy/classifier fields to match plus a one-time operator
attestation. The schema-v2 manifest records the former contract ID, UTC
attestation time, and this limitation.

## Runtime qualification after container removal

The inherited `hecrp/nanortax` container belonged to another pipeline and is
not an RTBioScan runtime. Docker and Singularity are now explanatory erroring
profiles. The candidate managed runtime is represented by:

- `environment.yml`;
- `conda-lock-linux-64.yml`; and
- `conda-lock-osx-64.yml` (Rosetta on Apple Silicon).

The load-bearing pins are BLAST 2.15.0, LAST 1542, SeqKit 2.6.1, TaxonKit
0.14.2, and Cutadapt 4.6. LAST 1542 is required by the
`version=1542` field in `db/targets_All_tagged_nr95.prj`; rebuilding the index
and changing this pin must always be one coordinated reference release.

`bin/validate_runtime.sh` checks the versions, LAST index readability and exact
first-hit routing fixture, BLAST/SeqKit/Cutadapt behavior, and the five-taxon
TaxonKit rank matrix. Both locks pass against the shipped 1542 index and pinned
taxonomy: macOS `osx-64` under Rosetta and `linux-64` with Linux/amd64 binaries.
The macOS lock also loads the declared R/Bioconductor packages. The `conda`
Nextflow profile remains disabled because `process.conda = environment.yml`
would re-solve dependencies instead of using the committed lock. Install and
activate the platform lock, validate it, and run without `-profile conda`.

### Deterministic classification benchmark

`conf/taxonomy_regression/` is the post-FAST-basecall target-filter and taxonomy
benchmark that bridges Phase 2 and Phase 3. FAST labels such as Bacteria/Fungi
are operational exclusion buckets used to avoid unnecessary HAC/SUP
basecalling, not strict origin assignments. The benchmark freezes the legacy
A/B1 behavior, the unsupported B2 hypothesis, the -557/-561 synthetic-ID
collisions, marker controls, and fixed HAC/FAST-like variants. Its controls
include a genuine human COI query that correctly first-routes to the explicit
`COI|Human` exclusion bucket but has a human-like sequence in the
`COI|Bacteria` bucket. This captures bidirectional decoy contamination without
misstating Human as a configured `COI|Metazoa` target.

The Chain-A description is sequence-specific: the exact LR799917 accession is
correctly labelled `COI|Bacteria` in the FAST reference, while separate
host-labelled records in the downstream animal database are near-identical to
that bacterial query. A protocol-defined audit now searches three checksummed,
independently identified bacterial coxA controls without using label or
accession patterns to select downstream hits. On the shipped snapshot it emits
44 pending query/reference rows: five priority-review and 39 review. These are
review-queue results, not automatic contamination calls and not a claim of
biological exhaustiveness.

`chain_a_reference_candidates.tsv` records the reviewed subset. All five
priority records are confirmed reference-sequence contamination. The evidence
does not require the host specimens to be misidentified: `ISUP118-14` is a
morphology-identified *Galerita* leg specimen whose submitted sequence is
LR799917-class bacterial coxA, and `GBMIN70259-17` is a full-length 97.021%
Wolbachia coxA match. Independent same-species animal COI controls are strongly
discordant with both. `chain_a_priority_adjudication.tsv` pins those controls,
sequence hashes, pairwise metrics, external evidence status, and dispositions.

The 39 lower-tier records were then evaluated locally without disclosing
sequence data. The comparison graph contains those records plus only the five
priority records explicitly confirmed in `chain_a_reference_candidates.tsv`;
anchors remain absent from the lower-tier output. A lower-tier row becomes a
cross-family sequence/label-conflict candidate only when its bacterial-control
similarity-screen result is accompanied by a direct ≥95%-identity,
≥80%-shorter-coverage match carrying a different host family. This flags a
label-conflict candidate under the declared protocol, not bacterial origin or
which endpoint is wrong. Twenty-four records satisfy the rule: 23 through review
peers in the existing four components and `GMODL3842-22` through a confirmed priority
anchor. Fifteen remain unresolved and must not be automatically curated.
`chain_a_lower_tier_adjudication.tsv` and its provenance freeze the result and
explicitly distinguish review-peer from confirmed-anchor evidence.
`ISUP118-14`'s uniquely mapped synthetic taxid `-2704` must not be conflated with
the separate `-557`/`-561` namespace collisions.

The versioned policy `chain_a_downstream_coi_release_policy_v1.tsv` now keeps
evidence separate from release action. Its generic builder emits a 44-row master
disposition, a 29-row correctness-first quarantine projection, and a 15-row
retained-unresolved projection. The 24 conflict candidates remain labelled as
policy quarantines rather than confirmed bacterial contamination. Diagnostics
also disclose that three stored taxids lose their only source record and that no
exact copy of the 27 quarantined sequence hashes remains. No source FASTA, BLAST
index, pipeline configuration, or state identity is changed by this manifest
stage. These diagnostics observe the pinned full shipped FASTA; they do not
choose the canonical release base.

The subsequent source/index integrity audit supersedes the disposition
builder's permissive whole-source counts for source-health decisions. It finds
one non-leading header delimiter at physical record 791,433: analysis-only
splitting recovers a 524-nt record candidate after the 200-nt indexed sequence.
The resulting first 791,433 logical records match the legacy BLAST titles,
signed header taxids, order, and sequences exactly, while the analysis-logical
tail has 1,494 records. The audit changes no source, policy, database, pipeline,
or state identity.

Run the benchmark with:

```bash
python3 bin/validate_taxonomy_classification_fixture.py \
  --taxonomy-data-dir db/taxonomy/releases/ncbi-taxdump-2024-06-24
```

The expected results intentionally preserve the current defects. Phase 3 will
use a new corrected expectation while retaining this legacy baseline. The
fixture proves the downstream Chain-A trap but not its current FAST reachability:
all committed endosymbiont variants first-route to `COI|Bacteria`. Live
incidence must be measured in shadow mode on representative pre-filter reads;
a generic marker-length POD5 is workflow evidence, not incidence evidence.
Shadow margins measure routing and compute impact, not biological truth by
themselves; accuracy requires independently adjudicated controls or reviewed
reads against broader curated references. Any Phase 3 ambiguity policy must
retain near ties for HAC/SUP by default and may exclude them only with
independently strong off-target evidence.

Enable the decision-neutral collector with `--fast_filter_shadow true`. It
adds per-read and per-round read/base diagnostics to the round directory while
preserving the legacy first-hit target list. The default is `false`; no
threshold or alternative assignment is enforced in Phase 2.

Integration evidence from 2026-07-30:

- a Nextflow 22.10.8 CPU round exercised the non-empty FAST/shadow branch and
  completed all 15 processes;
- the resulting detail and summary files contained one 44-base no-alignment
  read, including `current_excluded__no_alignment = 1`;
- a second round forced only the shadow helper to fail, logged the legacy
  router fallback, omitted partial shadow files, and completed all 15
  processes.

This is workflow/fallback evidence, not classification-incidence evidence.
Direct Metal attempts launched through the Codex command executor failed while
loading Dorado tensors, but that executor is not a valid GPU qualification
context. A native standalone Metal probe launched from Terminal enumerated the
Apple M1 Pro, compiled and dispatched a compute kernel, and verified 256 GPU
results. Terminal-side one-read tests then succeeded for both Dorado
`0.2.3+4ed609d` with its native v4.2-alpha FAST model and Dorado
`0.7.0+71cc7442` with the configured v5 FAST model. Accelerator qualification
must therefore be launched through the same normal Terminal/Nextflow runtime
used in production, never inferred from the Codex executor.

## Optional Dorado releases

Dorado is not part of the Conda lock and is never updated automatically.
`0.7.0+71cc7442` is the audited baseline. The committed macOS ARM64 manifest
pins the official platform archive plus every active FAST, HAC, and SUP model
artifact. `bin/install_dorado_release.pl` installs immutable releases
side-by-side and never changes the configured binary or model paths.

`bin/validate_dorado_release.sh` has two gates:

- static verification of bytes, version, platform, models, `basecaller`, and
  `summary` command compatibility;
- live compatibility on an explicitly requested hardware device and the
  checksummed official POD5 declared by
  `conf/runtime_compatibility/dorado_qualification_fixture_v0.7.0.tsv`, using
  the exact FAST/HAC/SUP chunk, batch, overlap, quality, read-list, SAM, and
  summary interfaces used by RTBioScan. Only Metal or CUDA evidence qualifies
  a release as an accelerator candidate; CPU and other devices are recorded as
  diagnostic-only. The tiny fixture qualifies hardware and formats only. A
  representative accelerator-backed full-round shadow run remains required
  for biological classification and real-time performance qualification. A
  failed live attempt writes a read-only failed-status report and preserves
  stage logs, effective arguments, a checksum manifest, and environment
  metadata in the sibling `<report>.evidence` directory rather than deleting
  the diagnostic reason during temporary-directory cleanup. A per-report lock
  serializes concurrent attempts; handled HUP/INT/TERM signals preserve
  evidence and release it. Untrappable termination can leave a stale lock that
  requires ownership/process inspection before manual removal.

Candidate runs use a new `state_id` and output directory. Promotion is an
explicit reviewed configuration change. The candidate must run FAST, HAC, and
SUP without CPU fallback on the intended Metal or CUDA device and meet
predeclared throughput, p95 round-latency, total-basecalling-time, and
sequencer-input-headroom budgets against the stable release on identical
hardware and inputs. Failure of either accelerator compatibility or the
real-time budget rejects promotion; CPU success cannot override it. Rollback
selects the retained stable release and its untouched matching state. A Linux
x86-64 release remains unqualified until its platform manifest and live CUDA
evidence exist.

Dorado `0.2.3+4ed609d` is also supported as an explicit recovery candidate,
not a default. It uses `dorado_input_mode=directory`, a separately selected
`0.7.0+71cc7442` summary binary, native 5 kHz v4.2-alpha FAST/HAC/SUP models,
and `toolchain_dorado_0.2.3_v1.tsv`. RTBioScan's capability probe omits the
unsupported modern `--emit-sam` flag while retaining the advertised legacy
overlap/chunk/batch options. On 2026-07-30, all three model stages completed
the official one-read fixture on Metal through the compatibility launcher,
including HAC/SUP read-list input and modern summary parsing. The schema-v2
fingerprint binds both executables, the directory-staging helper, all model
content, device, and effective arguments. This proves hardware/interface
operation only; marker-length classification and sustained real-time
performance remain promotion gates.

The normalized capture in
`tests/fixtures/dorado_mixed_0_2_3_0_7_0/` and
`tests/test_dorado_mixed_summary_reporting.py` lock the mixed-version reporting
contract: the captured 0.2.3 FAST/HAC/SUP SAM records must agree with their
0.7.0 summary rows, populate the complete RTBioScan read-information table,
and remain readable by the SUP cache ID parser.

Schema v2 binds the selected Dorado binary, model configurations, optional
release manifest, device, and effective arguments into the state identity.

## Existing rolling state

The default `strict` policy deliberately refuses pre-Phase-2 state because its
reference and taxonomy provenance cannot be inferred safely.

After confirming that an existing state was produced with the recorded legacy
baseline, adopt it exactly once:

```bash
nextflow run main.nf \
  --state_id EXISTING_STATE \
  --state_compatibility_policy adopt_legacy \
  [the run's normal arguments]
```

The installed manifest records `legacy_adopted	1`. Subsequent runs use
`strict`; `adopt_legacy` does not override a mismatch once a contract exists.

If provenance cannot be confirmed, use a new `--state_id` or explicitly reset
the state. Never adopt merely to bypass an incompatibility error.

Contract-bearing state whose recorded `execution_profile` contains `docker` or
`singularity` is rejected because the former image belonged to NanoRTax, not
RTBioScan. This automated check covers Phase-2.1a-and-later state manifests.
Pre-contract state has no trustworthy execution-backend provenance: never use
`adopt_legacy` for state known or suspected to have been produced with that
image. Start a new state and reanalyse instead.

### Schema-v1 to schema-v2 migration

For a contract-bearing schema-v1 state, first activate and validate the pinned
legacy runtime and confirm that the state was not produced by the inherited
NanoRTax container. Then migrate exactly once:

```bash
nextflow run main.nf \
  --state_id EXISTING_PHASE2_STATE \
  --state_contract_migration attest_v1 \
  [the run's normal arguments]
```

The migration cannot prove which historical binaries created the v1 state; the
operator attestation acknowledges that unavoidable limitation. A changed
legacy identity is rejected even with `attest_v1`. Subsequent starts use the
default `strict` setting and must reproduce the recorded schema-v2 toolchain
fingerprint.

## Mismatch behavior

A mismatch is fatal and reports the changed contract fields. The supported
responses are:

1. restore the matching reference, taxonomy, and policy versions;
2. select a new `--state_id`;
3. use the existing explicit `--restart_mode reset` workflow; or
4. later, use a reviewed migration/reclassification path.

There is no force flag that combines incompatible rolling state.

## Phase 2 acceptance checks

- A new state receives a deterministic contract.
- Reopening it with the same inputs succeeds.
- Changing a compatibility input fails before pipeline processes run.
- Legacy material fails under `strict`.
- Explicit legacy adoption succeeds once and is recorded.
- Reference artifacts are rejected if their bytes no longer match the manifest.
- Taxonomy artifacts are rejected unless size and SHA-256 match the pinned
  taxonomy release manifest.
- OTU and consensus processes receive the same canonical `TAXONKIT_DB`.
- A content-identical Phase-2.1a taxonomy-path migration preserves the contract
  ID and records its former path.
- The resolved runtime matches the exact toolchain policy before state is used.
- Changing a tool version, Conda lock, Dorado binary/model configuration,
  device, or effective Dorado arguments changes the schema-v2 identity.
- Schema-v1 state is rejected unless explicitly migrated; migration records the
  former contract ID and the historical-runtime trust limitation.
- An unchanged cached restart does not rehash or rewrite the attestation.
- Changing one file rehashes only that file.
- Unsafe cache ownership or permissions cannot authorize cached reuse.
- Complete LAST, BLAST-v5, and BLAST taxonomy component groups are required.
- The compatibility manifest is present in state-table snapshots.

Real-incident adjudication is not required for these checks. It remains useful
for later classifier validation, but the compatibility boundary is independently
testable.

The first deployment of the compatibility token changes task signatures for
processes that consume it, causing one intentional cache invalidation. Stable
contract IDs remain reusable thereafter.

## Phase 2 closeout

The Phase 2 implementation backlog is committed on
`agent/taxonomy-state-compatibility-phase-2-1a`. It includes runtime/state
reproducibility, pinned taxonomy, Dorado qualification and recovery support,
mixed-version reporting coverage, decision-neutral FAST shadow diagnostics,
the deterministic taxonomy benchmark, and zero/one-read plot hardening.

Closeout validation on 2026-07-30 produced 1,050 passing tests, 56 skips, and
one expected-pass marker. `bin/check_param_registry.py` passes with all runtime
parameters registered. The full LAST 1542 / BLAST 2.15.0 / TaxonKit 0.14.2
classification replay matches the committed legacy snapshot.

No taxonomy behavior change is enforced by this phase. The original Phase-3
FAST/LAST path starts by running `--fast_filter_shadow true` on representative
production reads and independently adjudicating a reviewed disagreement subset.
That measurement gate still governs FAST routing, performance, and accuracy
changes. Separately versioned downstream-only correctness curation can proceed
from reproduced reference defects without waiting for FAST incidence, while
ancestry eligibility and competitive classification remain later stages.
