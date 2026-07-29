# RTBioScan Public Release Manifest

This list is for a public source release that remains runnable, while excluding outdated files, backups, generated state, and run-specific artifacts.

## Keep: core runtime files

Top level:

- `README.md`
- `LICENSE`
- `RTBioScan.sh`
- `main.nf`
- `nextflow.config`
- `environment.yml`
- `conda-lock-linux-64.yml`
- `conda-lock-osx-64.yml`
- `nextflow` only if you intentionally want to ship a pinned local Nextflow binary

Nextflow config and Groovy support:

- `conf/base.config`
- `conf/barcoding.config`
- `conf/voucher.config`
- `conf/xprize.config`
- `conf/state_compatibility/reference_manifest_legacy_v1.tsv`
- `conf/state_compatibility/taxonomy_release_ncbi_2024-06-24.tsv`
- `conf/runtime_validation/fast_routing_endosymbionts.fa`
- `conf/runtime_validation/fast_routing_endosymbionts.expected.tsv`
- `lib/ChannelUtils.groovy`
- `lib/DemuxConfig.groovy`

Docs to keep with the public release:

- `docs/README.md`
- `docs/concepts.md`
- `docs/installation.md`
- `docs/usage.md`
- `docs/output.md`
- `docs/report_schema.md`
- `docs/pipeline.md`
- `docs/params_reference.json`
- `docs/assets/`

Report/readme assets:

- `assets/report/template.html`
- `assets/report/run_template.html`
- `assets/report/report.css`
- `assets/report/report.js`
- `assets/report/figures.tsv`
- `assets/report/figures_sample.tsv`
- `assets/readme/pod5.html`
- `assets/readme/sample_info.html`
- `assets/readme/state.html`
- `assets/readme/run_config.html`

Runtime scripts:

- Keep the active scripts under `bin/`
- Keep `bin/lib/`
- Keep `bin/report_placeholders/failed_round/`

For the current default configuration, the release must include at least these Dorado model paths if Dorado is bundled:

- `bin/dorado/bin/dorado`
- `bin/dorado/bin/dna_r10.4.1_e8.2_400bps_fast@v5.0.0/`
- `bin/dorado/bin/dna_r10.4.1_e8.2_400bps_hac@v5.0.0/`
- `bin/dorado/bin/dna_r10.4.1_e8.2_400bps_sup@v4.3.0/`

## Keep: runtime data that must exist somewhere

The pipeline is not fully runnable without its databases. These can either be shipped with the release or documented as external downloads, but they must exist for the selected profile:

- `db/` content referenced by `nextflow.config`
- `db/taxonomy/releases/ncbi-taxdump-2024-06-24/`, containing the four
  checksummed TaxonKit runtime files declared by the taxonomy release manifest
- Any extra `db/` content referenced by the profile(s) you want to support publicly

Important:

- Do not remove LAST index members just because they look like backups. For example, `db/targets_All_tagged_nr95.bck` is part of the LAST database set, not an obsolete backup file.

## Additional files and dirs needed by bundled profiles

The files below are required in addition to the core runtime set when you want the public release to support runs with the bundled `.config` profiles.

### `-profile barcoding`

Additional requirements from `conf/barcoding.config`:

- `db/COInr_2024Jun_metazoa_memtax1.txt`
- `db/ITS2nr_2024Jun_viridiplantae_memtax2.txt`

Notes:

- `barcoding` does not add extra report-annotation files beyond the default runtime set.
- It still inherits the default databases from `nextflow.config`, including the default BLAST DBs and `db/taxdb`.

### `-profile voucher`

Additional requirements from `conf/voucher.config`:

- full LAST database prefix `db/targets_All_tagged_nr95.*`
- `db/COInr_2024Jun_metazoa_memtax1.txt`
- `db/ITS2nr_2024Jun_viridiplantae_memtax2.txt`

Additional runtime inputs implied by the profile:

- prepared barcode FASTA via `--indexes` or `results/sample_info/<run_id>/demult.fasta`
- prepared primer FASTA via `--primer_indexes` or `results/sample_info/<run_id>/primers.fasta`

Reason:

- `voucher` sets `demultiplex_mode = "full"`, so a voucher run is not usable without both demultiplexing FASTAs.

### `-profile xprize`

Additional requirements from `conf/xprize.config`:

- full LAST database prefix `db/targets_All_tagged_nr95.*`
- `db/COInr_2024Jun_metazoa_memtax1.txt`
- `db/ITS2nr_2024Jun_viridiplantae_memtax2.txt`
- `db/metazoa_spec_basics.txt`
- `db/viridiplantae_spec_basics.txt`
- `db/GBIF_iNAturalist_2024Jun_RioNegro_metazoa_gns.txt`
- `db/GBIF_iNAturalist_2024Jun_RioNegro_viridiplantae_gns.txt`

Notes:

- These are in addition to the default BLAST DBs and `db/taxdb` inherited from `nextflow.config`.

### `-profile test`

Additional requirements from `conf/test.config`:

- `conf/test.config`
- full LAST database prefix `db/targets_All_tagged_nr95.*`
- full BLAST database prefix `db/COInr_nr99_lca.*`
- full BLAST database prefix `db/ITS2_nr99_lca.*`
- `db/taxdb/`

Notes:

- `test` overrides `blast_db_specs`, so the default environmental BLAST DB prefixes are not sufficient for this profile.
- `test` sets `nonncbi_memtax = "|"`, so the memtax tables are not required specifically for the test profile.

## Optional to keep

These are useful, but not required for the runtime release itself:

- `tests/`
- `pytest.ini`
- `Makefile`

## Exclude: outdated or backup files

- `main.pre_refactor.nf`
- `bin/*.pre_*`
- `bin/consensus_prune_apply.sh.pre_O1`
- `conf/.xprize.config.swp`

If you bundle Dorado, the following model directories are not used by the current defaults and can be excluded unless you intentionally support alternate configs:

- `bin/dorado/bin/dna_r10.4.1_e8.2_400bps_fast@v4.3.0/`
- `bin/dorado/bin/dna_r10.4.1_e8.2_400bps_hac@v4.3.0/`
- `bin/dorado/bin/dna_r10.4.1_e8.2_400bps_sup@v5.0.0/`

## Exclude: generated state and run info

- `.nextflow/`
- `.nextflow.log*`
- `work/`
- `results/`
- `Consensus/`
- `.pytest_cache/`
- `bin/__pycache__/`
- `tests/__pycache__/`
- `*.pyc`
- `.DS_Store`
- `tmp.txt`

## Exclude: internal or local-only files

- `AGENTS.md`
- `CLAUDE.md`
- `docs/internal/`

## Runtime qualification before a complete public release

The candidate Conda specification and platform-separated lockfiles are bundled.
Do not enable or advertise the `conda` profile until the locked runtime passes
the committed LAST-index, routing, BLAST replay, TaxonKit rank-semantics,
SeqKit, Cutadapt, and minimal-round acceptance checks.
