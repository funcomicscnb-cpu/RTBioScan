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
- explicit classifier-policy and scoring-policy versions.

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
do not change the contract ID. Content hashes and classification settings are the
compatibility identity.

## Default baseline

`conf/state_compatibility/reference_manifest_legacy_v1.tsv` records the
operational pre-remediation baseline:

- the FAST/LAST filter source and index;
- the installed COI and ITS2 BLAST indexes;
- the taxonomy memory seeds and synthetic-lineage map;
- the BLAST taxonomy database.

The manifest intentionally describes the installed operational indexes. It does
not activate the 1,493 FASTA-only COI tail records identified in Phase 1. Tail
admission remains a separate, audited reference release.

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

Phase 2 is still not the production completion gate until Phase 2.1c adds
backend-aware toolchain identity and the explicit contract-schema migration.
Until then, do not change the TaxonKit, BLAST, or LAST toolchain within an
existing state.

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
