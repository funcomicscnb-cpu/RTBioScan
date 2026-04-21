import gzip
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "bin" / "blast_sup_path.sh"


def _write_cache(
    state_dir: Path,
    *,
    manifest_ids: list[str],
    fastq_records: str,
    summary_rows: str,
    summary_header: str = "input_filename\tbatch_id\tparent_read_id\tread_id\n",
    restart_token: str = "restart-1",
) -> None:
    cache_dir = state_dir / "sup_basecall_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "meta.tsv").write_text(
        textwrap.dedent(
            f"""\
            key\tvalue
            cache_schema_version\t2
            restart_token\t{restart_token}
            dorado_model\tmodel
            dorado_args\targs
            min_qscore\t15
            """
        ),
        encoding="utf-8",
    )
    (cache_dir / "manifest.tsv").write_text(
        "".join(f"{read_id}\tTS_Jun_prev\n" for read_id in manifest_ids),
        encoding="utf-8",
    )
    (cache_dir / "summary.tsv").write_text(
        summary_header + summary_rows,
        encoding="utf-8",
    )
    with gzip.open(cache_dir / "reads.fastq.gz", "wt", encoding="utf-8") as fh:
        fh.write(fastq_records)


def _run_harness(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "run_helper.sh"
    script.write_text(
        (
            """#!/usr/bin/env bash
set -euo pipefail
cd "__TMP_PATH__"
export LC_ALL=C
barcode="RTBioScan"
round_barcode="TS_Jun_0"
STATE_DIR="$PWD/state"
mkdir -p "$STATE_DIR"
SUP_CACHE_LOCK="$STATE_DIR/.sup_cache.lock"
SUP_CACHE_SCHEMA_VERSION="2"
SUP_CACHE_RESTART_TOKEN="restart-1"
SUP_CACHE_DORADO_MODEL="model"
SUP_CACHE_DORADO_ARGS="args"
SUP_CACHE_MIN_QSCORE="15"
SUP_CACHE_SKIP_PERSIST=0
SUP_DORADO_BIN="/bin/true"
SUP_TASK_CPUS="1"
SUP_FASTA_HQ_QCED="$PWD/reads.fasta"
cat > "$SUP_FASTA_HQ_QCED" <<'EOF'
>dummy|COI|hac|barcode=bc1|adapter=a1
ACGT
EOF
DORADO_SUMMARY_HEADER=$'input_filename\\tbatch_id\\tparent_read_id\\tread_id\\n'
SUP_PATH_TIMINGS_MS_FILE="$PWD/sup_path_timings_ms.tsv"
printf 'round_barcode\\tphase\\tseconds\\tms\\n' > "$SUP_PATH_TIMINGS_MS_FILE"
_timing_tick=0
now_ms() {
  _timing_tick=$((_timing_tick + 7))
  printf '%s\\n' "$_timing_tick"
}
append_sup_path_timing() {
  local phase="$1"
  local start="${2:-0}"
  local end="${3:-0}"
  local ms=0
  local sec=0
  if [[ "$start" =~ ^[0-9]+$ ]] && [[ "$end" =~ ^[0-9]+$ ]] && [ "$end" -ge "$start" ]; then
    ms=$(( end - start ))
    sec=$(( ms / 1000 ))
  fi
  printf '%s\\t%s\\t%s\\t%s\\n' "$round_barcode" "$phase" "$sec" "$ms" >> "$SUP_PATH_TIMINGS_MS_FILE"
}
seqkit() {
  local sub="${1:-}"
  shift || true
  if [ "$sub" != "faidx" ]; then
    echo "unsupported seqkit subcommand: $sub" >&2
    return 1
  fi
  local ids_file=""
  local fasta_in=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -j)
        shift 2
        ;;
      -l)
        ids_file="$2"
        shift 2
        ;;
      -r)
        fasta_in="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done
  perl -e '
    use strict;
    use warnings;
    my ($ids_f, $fasta_f) = @ARGV;
    open my $IDS, "<", $ids_f or die "open ids: $!";
    my (@ids, %want);
    while (<$IDS>) {
      chomp;
      next unless /\S/;
      push @ids, $_;
      $want{$_} = 1;
    }
    close $IDS;

    my %records;
    if (open my $FA, "<", $fasta_f) {
      my ($cur_id, $keep);
      while (<$FA>) {
        if (/^>/) {
          my $hdr = $_;
          (my $id = $hdr) =~ s/^>//;
          $id =~ s/\s.*//;
          chomp $id;
          $cur_id = $id;
          $keep = exists $want{$id} && !exists $records{$id};
          $records{$id} = $hdr if $keep;
          next;
        }
        next unless $keep && defined $cur_id;
        $records{$cur_id} .= $_;
      }
      close $FA;
    }

    for my $id (@ids) {
      next unless exists $records{$id};
      print $records{$id};
    }
  ' "$ids_file" "$fasta_in"
}
acquire_lock() {
  if [ "${SUP_TEST_LOCK_FAIL:-0}" = "1" ]; then
    return 1
  fi
  return 0
}
release_lock() {
  return 0
}
source "__HELPER_PATH__"
sync_candidate_counts() {
  hac2sup_candidate_unique_read_ids=$(awk 'NF && !seen[$0]++{c++} END{print c+0}' "${barcode}_blastreport_hac.list" 2>/dev/null || echo 0)
  hac2sup_candidate_rows="$hac2sup_candidate_unique_read_ids"
}
hac2sup_candidate_unique_read_ids=0
hac2sup_candidate_rows=0
if [ -f "${barcode}_blastreport_hac.list" ]; then
  sync_candidate_counts
fi
"""
            .replace("__TMP_PATH__", str(tmp_path))
            .replace("__HELPER_PATH__", str(HELPER))
            + body
            + "\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return subprocess.run(["bash", str(script)], cwd=tmp_path, capture_output=True, text=True, check=False)


def test_sup_cache_lookup_no_candidate_emits_zero_phases(tmp_path: Path) -> None:
    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            : > "${barcode}_blastreport_hac.list"
            sup_cache_lookup
            : > "${barcode}_blastreport_sup_new.fastq"
            sup_summary_write_header "${barcode}_round_sup_new.tsv"
            sup_cache_persist
            cat "$SUP_PATH_TIMINGS_MS_FILE"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    timings = (tmp_path / "sup_path_timings_ms.tsv").read_text(encoding="utf-8")
    assert "\tsup_cache_lookup\t0\t0\n" in timings
    assert "\tsup_cache_restore_fastq\t0\t0\n" in timings
    assert "\tsup_cache_restore_summary\t0\t0\n" in timings
    assert "\tsup_cache_persist\t0\t0\n" in timings


def test_sup_cache_lookup_miss_only_sets_all_candidates_missing_and_stable_timings(tmp_path: Path) -> None:
    (tmp_path / "RTBioScan_blastreport_hac.list").write_text("r1\nr2\n", encoding="utf-8")

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            sup_cache_lookup
            printf 'hit=%s miss=%s requested=%s\\n' "$sup_cache_hit_ids" "$sup_cache_miss_ids" "$dorado_sup_reads_requested"
            cat "${barcode}_blastreport_hac_missing.list"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "hit=0 miss=2 requested=2" in result.stdout
    assert (tmp_path / "RTBioScan_blastreport_hac_missing.list").read_text(encoding="utf-8") == "r1\nr2\n"
    timings = (tmp_path / "sup_path_timings_ms.tsv").read_text(encoding="utf-8")
    assert "\tsup_cache_lookup\t0\t" in timings
    assert "\tsup_cache_restore_fastq\t0\t0\n" in timings
    assert "\tsup_cache_restore_summary\t0\t0\n" in timings


def test_sup_cache_lookup_deduplicates_candidate_ids_for_accounting_and_dorado_requests(tmp_path: Path) -> None:
    (tmp_path / "RTBioScan_blastreport_hac.list").write_text("r1\nr1\nr2\n", encoding="utf-8")

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            sup_cache_lookup
            printf 'hit=%s miss=%s requested=%s unique=%s\\n' "$sup_cache_hit_ids" "$sup_cache_miss_ids" "$dorado_sup_reads_requested" "$hac2sup_candidate_unique_read_ids"
            cat "${barcode}_blastreport_hac_missing.list"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "hit=0 miss=2 requested=2 unique=2" in result.stdout
    assert (tmp_path / "RTBioScan_blastreport_hac_missing.list").read_text(encoding="utf-8") == "r1\nr2\n"


def test_sup_cache_hit_only_restores_and_merges_in_candidate_order(tmp_path: Path) -> None:
    (tmp_path / "RTBioScan_blastreport_hac.list").write_text("r2\nr1\n", encoding="utf-8")
    _write_cache(
        tmp_path / "state",
        manifest_ids=["r1", "r2"],
        fastq_records=(
            "@r1\nAAAA\n+\n####\n"
            "@r2\nCCCC\n+\n####\n"
        ),
        summary_rows=(
            "f\tb\tp\tr1\n"
            "f\tb\tp\tr2\n"
        ),
    )

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            sup_cache_lookup
            : > "${barcode}_blastreport_sup_new.fastq"
            sup_summary_write_header "${barcode}_round_sup_new.tsv"
            sup_merge_outputs
            printf 'hit=%s miss=%s requested=%s\\n' "$sup_cache_hit_ids" "$sup_cache_miss_ids" "$dorado_sup_reads_requested"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "hit=2 miss=0 requested=0" in result.stdout
    fastq = (tmp_path / "RTBioScan_blastreport_sup_pre.fastq").read_text(encoding="utf-8")
    assert fastq == "@r2\nCCCC\n+\n####\n@r1\nAAAA\n+\n####\n"
    summary = (tmp_path / "RTBioScan_round_sup.tsv").read_text(encoding="utf-8").splitlines()
    assert summary[1:] == ["f\tb\tp\tr2", "f\tb\tp\tr1"]


def test_sup_cache_summary_restore_uses_header_named_read_id_column(tmp_path: Path) -> None:
    (tmp_path / "RTBioScan_blastreport_hac.list").write_text("r2\nr1\n", encoding="utf-8")
    _write_cache(
        tmp_path / "state",
        manifest_ids=["r1", "r2"],
        fastq_records=(
            "@r1\nAAAA\n+\n####\n"
            "@r2\nCCCC\n+\n####\n"
        ),
        summary_header="filename\tread_id\trun_id\tchannel\n",
        summary_rows=(
            "f1\tr1\trun\t11\n"
            "f2\tr2\trun\t22\n"
        ),
    )

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            sup_cache_lookup
            : > "${barcode}_blastreport_sup_new.fastq"
            printf 'filename\tread_id\trun_id\tchannel\n' > "${barcode}_round_sup_new.tsv"
            sup_merge_outputs
            printf 'restored=%s merged=%s\\n' "$sup_cache_restored_summary_rows" "$sup_summary_rows_merged"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "restored=2 merged=2" in result.stdout
    summary = (tmp_path / "RTBioScan_round_sup.tsv").read_text(encoding="utf-8").splitlines()
    assert summary[1:] == ["f2\tr2\trun\t22", "f1\tr1\trun\t11"]


def test_sup_cache_partial_payload_corruption_reclassifies_to_miss_and_merge_preserves_all_requested_ids(tmp_path: Path) -> None:
    (tmp_path / "RTBioScan_blastreport_hac.list").write_text("r1\nr2\nr3\n", encoding="utf-8")
    _write_cache(
        tmp_path / "state",
        manifest_ids=["r1", "r2"],
        fastq_records="@r1\nAAAA\n+\n####\n",
        summary_rows="f\tb\tp\tr1\n",
    )

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            sup_cache_lookup
            cat > "${barcode}_blastreport_sup_new.fastq" <<'EOF'
            @r2
            CCCC
            +
            ####
            @r3
            GGGG
            +
            ####
            EOF
            cat > "${barcode}_round_sup_new.tsv" <<'EOF'
            input_filename\tbatch_id\tparent_read_id\tread_id
            f\tb\tp\tr2
            f\tb\tp\tr3
            EOF
            sup_merge_outputs
            printf 'hit=%s miss=%s requested=%s\\n' "$sup_cache_hit_ids" "$sup_cache_miss_ids" "$dorado_sup_reads_requested"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "hit=1 miss=2 requested=2" in result.stdout
    assert (tmp_path / "RTBioScan_blastreport_hac_cached.list").read_text(encoding="utf-8") == "r1\n"
    assert (tmp_path / "RTBioScan_blastreport_hac_missing.list").read_text(encoding="utf-8") == "r2\nr3\n"
    fastq = (tmp_path / "RTBioScan_blastreport_sup_pre.fastq").read_text(encoding="utf-8")
    assert fastq == "@r1\nAAAA\n+\n####\n@r2\nCCCC\n+\n####\n@r3\nGGGG\n+\n####\n"
    summary = (tmp_path / "RTBioScan_round_sup.tsv").read_text(encoding="utf-8").splitlines()
    assert summary[1:] == ["f\tb\tp\tr1", "f\tb\tp\tr2", "f\tb\tp\tr3"]


def test_sup_cache_reclassified_miss_uses_new_dorado_payload_not_stale_cached_payload(tmp_path: Path) -> None:
    (tmp_path / "RTBioScan_blastreport_hac.list").write_text("r1\nr2\n", encoding="utf-8")
    _write_cache(
        tmp_path / "state",
        manifest_ids=["r1", "r2"],
        fastq_records=(
            "@r1\nAAAA\n+\n####\n"
        ),
        summary_rows=(
            "f\tb\tp\tr1\n"
            "f_old\tb_old\tp_old\tr2\n"
        ),
    )

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            sup_cache_lookup
            cat > "${barcode}_blastreport_sup_new.fastq" <<'EOF'
            @r1
            AAAA
            +
            ####
            @r2
            NEW2
            +
            ####
            EOF
            cat > "${barcode}_round_sup_new.tsv" <<'EOF'
            input_filename\tbatch_id\tparent_read_id\tread_id
            f\tb\tp\tr1
            f_new\tb_new\tp_new\tr2
            EOF
            sup_merge_outputs
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    fastq = (tmp_path / "RTBioScan_blastreport_sup_pre.fastq").read_text(encoding="utf-8")
    assert "@r2\nNEW2\n+\n####\n" in fastq
    assert "@r2\nOLD2\n+\n!!!!\n" not in fastq
    summary = (tmp_path / "RTBioScan_round_sup.tsv").read_text(encoding="utf-8")
    assert "f_new\tb_new\tp_new\tr2\n" in summary
    assert "f_old\tb_old\tp_old\tr2\n" not in summary


def test_sup_cache_reset_failure_degrades_to_full_miss_and_skips_persist(tmp_path: Path) -> None:
    (tmp_path / "RTBioScan_blastreport_hac.list").write_text("r1\nr2\n", encoding="utf-8")
    _write_cache(
        tmp_path / "state",
        manifest_ids=[],
        fastq_records="",
        summary_rows="",
        restart_token="stale-restart",
    )

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            sup_cache_reset_locked() { return 1; }
            sup_cache_lookup
            cat > "${barcode}_blastreport_sup_new.fastq" <<'EOF'
            @r1
            AAAA
            +
            ####
            EOF
            cat > "${barcode}_round_sup_new.tsv" <<'EOF'
            input_filename\tbatch_id\tparent_read_id\tread_id
            f\tb\tp\tr1
            EOF
            sup_cache_persist
            printf 'hit=%s miss=%s requested=%s skip=%s\\n' "$sup_cache_hit_ids" "$sup_cache_miss_ids" "$dorado_sup_reads_requested" "$SUP_CACHE_SKIP_PERSIST"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "hit=0 miss=2 requested=2 skip=1" in result.stdout
    assert (tmp_path / "RTBioScan_blastreport_hac_missing.list").read_text(encoding="utf-8") == "r1\nr2\n"
    timings = (tmp_path / "sup_path_timings_ms.tsv").read_text(encoding="utf-8")
    assert "\tsup_cache_persist\t0\t0\n" in timings


def test_sup_cache_persist_double_failure_stays_soft_fail(tmp_path: Path) -> None:
    _write_cache(
        tmp_path / "state",
        manifest_ids=[],
        fastq_records="",
        summary_rows="",
    )

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            sup_fastq_extract_ordered() { return 1; }
            sup_cache_reset_locked() { return 1; }
            cat > "${barcode}_blastreport_sup_new.fastq" <<'EOF'
            @r1
            AAAA
            +
            ####
            EOF
            cat > "${barcode}_round_sup_new.tsv" <<'EOF'
            input_filename\tbatch_id\tparent_read_id\tread_id
            f\tb\tp\tr1
            EOF
            sup_cache_persist
            printf 'skip=%s\\n' "$SUP_CACHE_SKIP_PERSIST"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "skip=1" in result.stdout


def test_sup_cache_persist_skips_duplicate_ids_already_in_manifest(tmp_path: Path) -> None:
    _write_cache(
        tmp_path / "state",
        manifest_ids=["r1"],
        fastq_records="@r1\nOLD1\n+\n!!!!\n",
        summary_rows="f_old\tb_old\tp_old\tr1\n",
    )

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            cat > "${barcode}_blastreport_sup_new.fastq" <<'EOF'
            @r1
            NEW1
            +
            ####
            @r2
            NEW2
            +
            ####
            EOF
            cat > "${barcode}_round_sup_new.tsv" <<'EOF'
            input_filename\tbatch_id\tparent_read_id\tread_id
            f_new\tb_new\tp_new\tr1
            f_new\tb_new\tp_new\tr2
            EOF
            sup_cache_persist
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    manifest_rows = (tmp_path / "state" / "sup_basecall_cache" / "manifest.tsv").read_text(encoding="utf-8").splitlines()
    assert manifest_rows == ["r1\tTS_Jun_prev", "r2\tTS_Jun_0"]
    summary = (tmp_path / "state" / "sup_basecall_cache" / "summary.tsv").read_text(encoding="utf-8")
    assert summary.count("\tr1\n") == 1
    assert summary.count("\tr2\n") == 1


def test_sup_cache_persist_uses_header_named_read_id_column_and_populates_cache(tmp_path: Path) -> None:
    _write_cache(
        tmp_path / "state",
        manifest_ids=[],
        fastq_records="",
        summary_rows="",
        summary_header="filename\tread_id\trun_id\tchannel\n",
    )

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            cat > "${barcode}_blastreport_sup_new.fastq" <<'EOF'
            @r1
            AAAA
            +
            ####
            @r2
            CCCC
            +
            ####
            EOF
            cat > "${barcode}_round_sup_new.tsv" <<'EOF'
            filename\tread_id\trun_id\tchannel
            f1\tr1\trun\t11
            f2\tr2\trun\t22
            EOF
            sup_cache_persist
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    cache_dir = tmp_path / "state" / "sup_basecall_cache"
    manifest_rows = (cache_dir / "manifest.tsv").read_text(encoding="utf-8").splitlines()
    assert manifest_rows == ["r1\tTS_Jun_0", "r2\tTS_Jun_0"]
    summary = (cache_dir / "summary.tsv").read_text(encoding="utf-8").splitlines()
    assert summary[0] == "filename\tread_id\trun_id\tchannel"
    assert summary[1:] == ["f1\tr1\trun\t11", "f2\tr2\trun\t22"]
    with gzip.open(cache_dir / "reads.fastq.gz", "rt", encoding="utf-8") as fh:
        fastq = fh.read()
    assert "@r1\nAAAA\n+\n####\n" in fastq
    assert "@r2\nCCCC\n+\n####\n" in fastq


def test_sup_cache_persist_skips_update_when_summary_header_lacks_read_id(tmp_path: Path) -> None:
    _write_cache(
        tmp_path / "state",
        manifest_ids=[],
        fastq_records="",
        summary_rows="",
        summary_header="filename\tbatch_id\trun_id\tchannel\n",
    )

    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            cat > "${barcode}_blastreport_sup_new.fastq" <<'EOF'
            @r1
            AAAA
            +
            ####
            EOF
            cat > "${barcode}_round_sup_new.tsv" <<'EOF'
            filename\tbatch_id\trun_id\tchannel
            f1\tb1\trun\t11
            EOF
            sup_cache_persist
            printf 'skip=%s\\n' "$SUP_CACHE_SKIP_PERSIST"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "skip=1" in result.stdout
    assert "summary header lacks read_id" in result.stderr
    cache_dir = tmp_path / "state" / "sup_basecall_cache"
    assert (cache_dir / "manifest.tsv").read_text(encoding="utf-8") == ""
    summary = (cache_dir / "summary.tsv").read_text(encoding="utf-8").splitlines()
    assert summary == ["filename\tbatch_id\trun_id\tchannel"]


def test_sup_cache_progression_populates_cache_then_hits_on_next_round(tmp_path: Path) -> None:
    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            cat > "${barcode}_blastreport_sup_new.fastq" <<'EOF'
            @r1
            AAAA
            +
            ####
            @r2
            CCCC
            +
            ####
            EOF
            cat > "${barcode}_round_sup_new.tsv" <<'EOF'
            filename\tread_id\trun_id\tchannel
            f1\tr1\trun\t11
            f2\tr2\trun\t22
            EOF
            sup_cache_persist
            cat > "${barcode}_blastreport_hac.list" <<'EOF'
            r2
            r3
            EOF
            sync_candidate_counts
            sup_cache_lookup
            printf 'hit=%s miss=%s requested=%s\\n' "$sup_cache_hit_ids" "$sup_cache_miss_ids" "$dorado_sup_reads_requested"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "hit=1 miss=1 requested=1" in result.stdout


def test_sup_shared_candidate_extract_hac_only_preserves_unique_order(tmp_path: Path) -> None:
    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            cat > "$SUP_FASTA_HQ_QCED" <<'EOF'
            >r1
            AAAA
            >r2
            CCCC
            >r3
            GGGG
            EOF
            cat > "${barcode}_blastreport_hac.list" <<'EOF'
            r2
            r1
            r2
            EOF
            cat > "${barcode}_blastreport_hac_unique.list" <<'EOF'
            r2
            r1
            EOF
            sync_candidate_counts
            hac2sup_candidate_unique_read_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_unique.list")
            : > hac_fixed_readids.list
            sup_shared_candidate_extract
            printf 'union=%s hac=%s fixed=%s reads=%s\\n' "$shared_extract_union_ids" "$shared_extract_hac2sup_ids" "$shared_extract_hac_fixed_ids" "$shared_extract_fasta_reads"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "union=2 hac=2 fixed=0 reads=2" in result.stdout
    assert (tmp_path / "RTBioScan_qced_reads_hq_hac2sup.fasta").read_text(encoding="utf-8") == ">r2\nCCCC\n>r1\nAAAA\n"
    assert (tmp_path / "RTBioScan_qced_reads_hq_hac_fixed.fasta").read_text(encoding="utf-8") == ""


def test_sup_shared_candidate_extract_mixed_overlap_preserves_class_order_and_dedups_union(tmp_path: Path) -> None:
    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            cat > "$SUP_FASTA_HQ_QCED" <<'EOF'
            >r1
            AAAA
            >r2
            CCCC
            >r3
            GGGG
            EOF
            cat > "${barcode}_blastreport_hac.list" <<'EOF'
            r2
            r3
            EOF
            cat > "${barcode}_blastreport_hac_unique.list" <<'EOF'
            r2
            r3
            EOF
            cat > hac_fixed_readids.list <<'EOF'
            r3
            r1
            r3
            EOF
            sync_candidate_counts
            hac2sup_candidate_unique_read_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_unique.list")
            sup_shared_candidate_extract
            printf 'union=%s hac=%s fixed=%s reads=%s\\n' "$shared_extract_union_ids" "$shared_extract_hac2sup_ids" "$shared_extract_hac_fixed_ids" "$shared_extract_fasta_reads"
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "union=3 hac=2 fixed=2 reads=3" in result.stdout
    assert (tmp_path / "RTBioScan_qced_reads_hq_hac2sup.fasta").read_text(encoding="utf-8") == ">r2\nCCCC\n>r3\nGGGG\n"
    assert (tmp_path / "RTBioScan_qced_reads_hq_hac_fixed.fasta").read_text(encoding="utf-8") == ">r3\nGGGG\n>r1\nAAAA\n"
    timings = (tmp_path / "sup_path_timings_ms.tsv").read_text(encoding="utf-8")
    assert "\tshared_candidate_extract\t0\t" in timings


def test_sup_shared_candidate_extract_no_candidate_emits_empty_outputs_and_zero_timing(tmp_path: Path) -> None:
    result = _run_harness(
        tmp_path,
        textwrap.dedent(
            """\
            : > "${barcode}_blastreport_hac.list"
            : > "${barcode}_blastreport_hac_unique.list"
            sync_candidate_counts
            hac2sup_candidate_unique_read_ids=0
            : > hac_fixed_readids.list
            sup_shared_candidate_extract
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "RTBioScan_qced_reads_hq_hac2sup.fasta").read_text(encoding="utf-8") == ""
    assert (tmp_path / "RTBioScan_qced_reads_hq_hac_fixed.fasta").read_text(encoding="utf-8") == ""
    timings = (tmp_path / "sup_path_timings_ms.tsv").read_text(encoding="utf-8")
    assert "\tshared_candidate_extract\t0\t0\n" in timings
