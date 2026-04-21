import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SERIAL_SCRIPT = REPO_ROOT / "bin" / "otu_refine_blastreport.pl"
PARALLEL_SCRIPT = REPO_ROOT / "bin" / "otu_refine_blastreport_parallel.sh"
WORKER_SCRIPT = REPO_ROOT / "bin" / "otu_refine_blastreport_worker.pl"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_serial(tmp_path: Path, tax_rows: str, clstr_rows: str, lineage_rows: str, env=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tax_file = tmp_path / "blastreport.txt"
    clstr_file = tmp_path / "reads.clstr"
    lineage_file = tmp_path / "id2lineage.tsv"
    tax_file.write_text(tax_rows, encoding="utf-8")
    clstr_file.write_text(clstr_rows, encoding="utf-8")
    lineage_file.write_text(lineage_rows, encoding="utf-8")
    return subprocess.run(
        ["perl", str(SERIAL_SCRIPT), str(tax_file), str(clstr_file), str(lineage_file)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
    )


def _run_parallel(tmp_path: Path, tax_rows: str, clstr_rows: str, lineage_rows: str, threads: int, env=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tax_file = tmp_path / "blastreport.txt"
    clstr_file = tmp_path / "reads.clstr"
    lineage_file = tmp_path / "id2lineage.tsv"
    tax_file.write_text(tax_rows, encoding="utf-8")
    clstr_file.write_text(clstr_rows, encoding="utf-8")
    lineage_file.write_text(lineage_rows, encoding="utf-8")
    return subprocess.run(
        ["bash", str(PARALLEL_SCRIPT), str(tax_file), str(clstr_file), str(lineage_file), str(threads)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
    )


def _run_worker(tmp_path: Path, cluster_taxids_rows: str, clstr_rows: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cluster_taxids_file = tmp_path / "cluster_taxids.tsv"
    clstr_file = tmp_path / "reads.clstr"
    cluster_taxids_file.write_text(cluster_taxids_rows, encoding="utf-8")
    clstr_file.write_text(clstr_rows, encoding="utf-8")
    return subprocess.run(
        ["perl", str(WORKER_SCRIPT), str(cluster_taxids_file), str(clstr_file)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )


def test_parallel_wrapper_matches_serial_output_for_single_worker(tmp_path: Path) -> None:
    tax_rows = "read1|COI|sup;TX1\nread2|COI|sup;TX1\n"
    clstr_rows = (
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n"
    )
    lineage_rows = "TX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One\n"

    serial = _run_serial(tmp_path / "serial", tax_rows, clstr_rows, lineage_rows)
    parallel = _run_parallel(tmp_path / "parallel", tax_rows, clstr_rows, lineage_rows, 1)

    assert serial.returncode == 0, serial.stderr
    assert parallel.returncode == 0, parallel.stderr
    assert parallel.stdout == serial.stdout


def test_parallel_wrapper_matches_serial_output_for_cluster_shards(tmp_path: Path) -> None:
    tax_rows = (
        "read1|COI|sup;TX1\n"
        "read2|COI|sup;TX1\n"
        "read3|ITS2|sup;TX2\n"
        "read4|ITS2|sup;TX3\n"
        "read5|COI|sup;TX4\n"
        "read6|ITS2|sup;TX5\n"
        "read7|COI|sup;NA\n"
        "read8|ITS2|sup;TX_UNKNOWN\n"
    )
    clstr_rows = (
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n"
        ">Cluster 1\n"
        "0\t100nt, >read3|ITS2|sup... *\n"
        "1\t100nt, >read4|ITS2|sup... at +/99%\n"
        ">Cluster 2\n"
        "0\t100nt, >read5|COI|sup... *\n"
        "1\t100nt, >read6|ITS2|sup... at +/99%\n"
        ">Cluster 3\n"
        "0\t100nt, >read7|COI|sup... *\n"
        "1\t100nt, >read8|ITS2|sup... at +/99%\n"
    )
    lineage_rows = (
        "TX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One\n"
        "TX2\tK__Two;p__Two;c__Two;o__Two;f__Two;g__Two;s__Two\n"
        "TX4\tK__Four;p__Four;c__Four;o__Four;f__Four;g__Four;s__Four\n"
        "TX5\tK__Five;p__Five;c__Five;o__Five;f__Five;g__Five;s__Five\n"
    )

    serial = _run_serial(tmp_path / "serial", tax_rows, clstr_rows, lineage_rows)
    parallel = _run_parallel(tmp_path / "parallel", tax_rows, clstr_rows, lineage_rows, 3)

    assert serial.returncode == 0, serial.stderr
    assert parallel.returncode == 0, parallel.stderr
    assert parallel.stdout == serial.stdout
    assert "OTUB_0-COI\tTX1\tK__One" in parallel.stdout
    assert "read7|COI|sup|OTUB_3-COI\tTX_UNKNOWN\tK__Unassigned" in parallel.stdout
    assert "read8|ITS2|sup|OTUB_3-ITS2\tTX_UNKNOWN\tK__Unassigned" in parallel.stdout


def test_parallel_wrapper_matches_serial_output_with_taxonkit_resolution(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "taxonkit",
        """#!/usr/bin/env python3
import sys

args = sys.argv[1:]
data = sys.stdin.read().strip().splitlines()
if args and args[0] == "lineage":
    for row in data:
        print(f"{row}\\troot;child;leaf")
elif args and args[0] == "reformat":
    for row in data:
        taxid = row.split("\\t", 1)[0]
        print(f"{taxid}\\tignored\\tK__Root;p__Child;c__Leaf;o__Leaf;f__Leaf;g__Leaf;s__Leaf")
else:
    raise SystemExit(2)
""",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    tax_rows = (
        "read1|COI|sup;123\n"
        "read2|COI|sup;123\n"
        "read3|ITS2|sup;456\n"
        "read4|ITS2|sup;456\n"
    )
    clstr_rows = (
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n"
        ">Cluster 1\n"
        "0\t100nt, >read3|ITS2|sup... *\n"
        "1\t100nt, >read4|ITS2|sup... at +/99%\n"
    )
    lineage_rows = ""

    serial = _run_serial(tmp_path / "serial", tax_rows, clstr_rows, lineage_rows, env=env)
    parallel = _run_parallel(tmp_path / "parallel", tax_rows, clstr_rows, lineage_rows, 2, env=env)

    assert serial.returncode == 0, serial.stderr
    assert parallel.returncode == 0, parallel.stderr
    assert parallel.stdout == serial.stdout
    assert "K__Root;p__Child;c__Leaf;o__Leaf;f__Leaf;g__Leaf;s__Leaf" in parallel.stdout


def test_parallel_wrapper_preserves_normalized_fallback_precedence(tmp_path: Path) -> None:
    tax_rows = (
        "read1|zeta|sup;TX_FIRST\n"
        "read1|alpha|sup;TX_LAST\n"
    )
    clstr_rows = (
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|hac... *\n"
    )
    lineage_rows = (
        "TX_FIRST\tK__First;p__First;c__First;o__First;f__First;g__First;s__First\n"
        "TX_LAST\tK__Last;p__Last;c__Last;o__Last;f__Last;g__Last;s__Last\n"
    )

    serial = _run_serial(tmp_path / "serial", tax_rows, clstr_rows, lineage_rows)
    parallel = _run_parallel(tmp_path / "parallel", tax_rows, clstr_rows, lineage_rows, 2)

    assert serial.returncode == 0, serial.stderr
    assert parallel.returncode == 0, parallel.stderr
    assert parallel.stdout == serial.stdout
    assert "read1|COI|hac|OTUB_0-COI\tTX_LAST\tK__Last" in parallel.stdout


def test_parallel_wrapper_prefers_exact_match_over_normalized_fallback(tmp_path: Path) -> None:
    tax_rows = (
        "read1|COI|hac;TX_EXACT\n"
        "read1|zeta|sup;TX_FALLBACK\n"
    )
    clstr_rows = (
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|hac... *\n"
    )
    lineage_rows = (
        "TX_EXACT\tK__Exact;p__Exact;c__Exact;o__Exact;f__Exact;g__Exact;s__Exact\n"
        "TX_FALLBACK\tK__Fallback;p__Fallback;c__Fallback;o__Fallback;f__Fallback;g__Fallback;s__Fallback\n"
    )

    serial = _run_serial(tmp_path / "serial", tax_rows, clstr_rows, lineage_rows)
    parallel = _run_parallel(tmp_path / "parallel", tax_rows, clstr_rows, lineage_rows, 2)

    assert serial.returncode == 0, serial.stderr
    assert parallel.returncode == 0, parallel.stderr
    assert parallel.stdout == serial.stdout
    assert "read1|COI|hac|OTUB_0-COI\tTX_EXACT\tK__Exact" in parallel.stdout


def test_parallel_wrapper_caches_normalized_fallback_back_to_exact_ref_id(tmp_path: Path) -> None:
    tax_file = tmp_path / "blastreport.txt"
    clstr_file = tmp_path / "reads.clstr"
    tax_file.write_text("read1|zeta|sup;TX_LAST\n", encoding="utf-8")
    clstr_file.write_text(
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|hac... *\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "perl",
            "-MFindBin",
            f"-I{REPO_ROOT / 'bin' / 'lib'}",
            "-MRTBioScan::OTURefineBlastreport=load_target_tax_maps,collect_cluster_taxids_from_cluster_file",
            "-e",
            (
                'my ($blast,$clstr)=@ARGV; '
                'my ($target_tax,$target_tax_norm)=load_target_tax_maps($blast); '
                'collect_cluster_taxids_from_cluster_file($clstr,$target_tax,$target_tax_norm); '
                'print exists $target_tax->{"read1|COI|hac"} ? $target_tax->{"read1|COI|hac"} : "";'
            ),
            str(tax_file),
            str(clstr_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "TX_LAST"


def test_parallel_wrapper_writes_phase_timings_and_workload_stats(tmp_path: Path) -> None:
    tax_rows = (
        "read1|COI|sup;TX1\n"
        "read2|COI|sup;TX1\n"
        "read3|ITS2|sup;TX2\n"
        "read4|ITS2|sup;TX2\n"
    )
    clstr_rows = (
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n"
        ">Cluster 1\n"
        "0\t100nt, >read3|ITS2|sup... *\n"
        "1\t100nt, >read4|ITS2|sup... at +/99%\n"
    )
    lineage_rows = (
        "TX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One\n"
        "TX2\tK__Two;p__Two;c__Two;o__Two;f__Two;g__Two;s__Two\n"
    )

    result = _run_parallel(tmp_path, tax_rows, clstr_rows, lineage_rows, 2)

    assert result.returncode == 0, result.stderr

    phase_rows = (tmp_path / "otu_refine_phase_timings.tsv").read_text(encoding="utf-8").splitlines()
    assert phase_rows[0] == "round_barcode\tphase\tseconds"
    phase_names = {row.split("\t")[1] for row in phase_rows[1:] if row.strip()}
    assert {
        "shard_plan",
        "shard_materialize",
        "load_blast_tax_map",
        "cluster_taxid_pass",
        "cluster_taxid_validation",
        "shard_expand_workers",
        "pair_merge",
        "pairs_load",
        "lineage_resolution",
        "annotated_emit",
        "annotate_total",
        "wrapper_total",
    }.issubset(phase_names)
    phase_rows_ms = (tmp_path / "otu_refine_phase_timings_ms.tsv").read_text(encoding="utf-8").splitlines()
    assert phase_rows_ms[0] == "round_barcode\tphase\tseconds\tms"
    ms_fields = [row.split("\t") for row in phase_rows_ms[1:] if row.strip()]
    assert all(len(fields) == 4 for fields in ms_fields)
    phase_names_ms = {fields[1] for fields in ms_fields}
    assert phase_names_ms == phase_names
    assert all(fields[3].isdigit() for fields in ms_fields)
    ms_by_phase = {fields[1]: int(fields[3]) for fields in ms_fields}
    assert ms_by_phase["annotate_total"] + 5 >= ms_by_phase["lineage_resolution"]
    assert ms_by_phase["wrapper_total"] + 5 >= ms_by_phase["annotate_total"]

    workload_rows = (tmp_path / "otu_refine_workload_stats.tsv").read_text(encoding="utf-8").splitlines()
    assert workload_rows[0] == "key\tvalue"
    workload = dict(row.split("\t", 1) for row in workload_rows[1:] if row.strip())
    assert workload["cluster_count"] == "2"
    assert workload["blastreport_rows"] == "4"
    assert workload["cluster_taxids_rows"] == "2"
    assert workload["worker_count"] == "2"
    assert workload["shard_count"] == "2"
    assert workload["shard_scheduler_mode"] == "equal_record_count"
    assert workload["target_records_per_shard"] == "2"
    assert workload["smallest_shard_records"] == "2"
    assert workload["median_shard_records"] == "2"
    assert workload["largest_shard_records"] == "2"
    assert workload["largest_shard_fraction"] == "0.500000"
    assert workload["max_single_cluster_records"] == "2"
    assert workload["max_single_cluster_fraction"] == "0.500000"
    assert workload["merged_pairs_rows"] == "4"
    assert workload["annotated_rows"] == "4"
    assert workload["merged_pairs_bytes"].isdigit()
    assert int(workload["merged_pairs_bytes"]) > 0
    assert workload["annotated_bytes"].isdigit()
    assert int(workload["annotated_bytes"]) > 0
    assert workload["annotated_file_bytes"] == str(len(result.stdout.encode("utf-8")))


def test_parallel_wrapper_uses_equal_cluster_manifest_for_skewed_cluster_records(tmp_path: Path) -> None:
    tax_rows = "".join(f"read{i}|COI|sup;TX1\n" for i in range(1, 12))
    clstr_rows = (
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n"
        "2\t100nt, >read3|COI|sup... at +/99%\n"
        "3\t100nt, >read4|COI|sup... at +/99%\n"
        "4\t100nt, >read5|COI|sup... at +/99%\n"
        "5\t100nt, >read6|COI|sup... at +/99%\n"
        ">Cluster 1\n"
        "0\t100nt, >read7|COI|sup... *\n"
        ">Cluster 2\n"
        "0\t100nt, >read8|COI|sup... *\n"
        ">Cluster 3\n"
        "0\t100nt, >read9|COI|sup... *\n"
        ">Cluster 4\n"
        "0\t100nt, >read10|COI|sup... *\n"
        ">Cluster 5\n"
        "0\t100nt, >read11|COI|sup... *\n"
    )
    lineage_rows = "TX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One\n"
    debug_dir = tmp_path / "debug"
    env = os.environ.copy()
    env["OTU_REFINE_DEBUG_DIR"] = str(debug_dir)

    serial = _run_serial(tmp_path / "serial", tax_rows, clstr_rows, lineage_rows)
    parallel = _run_parallel(tmp_path / "parallel", tax_rows, clstr_rows, lineage_rows, 3, env=env)

    assert serial.returncode == 0, serial.stderr
    assert parallel.returncode == 0, parallel.stderr
    assert parallel.stdout == serial.stdout

    manifest_rows = (debug_dir / "shard_manifest.tsv").read_text(encoding="utf-8").splitlines()
    assert manifest_rows[0] == "shard_index\tstart_cluster\tend_cluster\tcluster_count\trecord_count\tshard_path"
    rows = [row.split("\t") for row in manifest_rows[1:] if row.strip()]
    assert [row[:5] for row in rows] == [
        ["1", "0", "0", "1", "6"],
        ["2", "1", "4", "4", "4"],
        ["3", "5", "5", "1", "1"],
    ]
    assert [Path(row[5]).name for row in rows] == [
        "shard_000001.clstr",
        "shard_000002.clstr",
        "shard_000003.clstr",
    ]
    workload = dict(
        row.split("\t", 1)
        for row in (tmp_path / "parallel" / "otu_refine_workload_stats.tsv").read_text(encoding="utf-8").splitlines()[1:]
        if row.strip()
    )
    assert workload["shard_scheduler_mode"] == "equal_record_count"
    assert workload["largest_shard_records"] == "6"
    assert workload["target_records_per_shard"] == "4"
    assert workload["smallest_shard_records"] == "1"
    assert workload["median_shard_records"] == "4"
    assert workload["largest_shard_fraction"] == "0.545455"
    assert workload["max_single_cluster_records"] == "6"
    assert workload["max_single_cluster_fraction"] == "0.545455"


def test_parallel_wrapper_fails_when_cluster_sizes_ids_do_not_match_cluster_taxids(tmp_path: Path) -> None:
    real_perl = subprocess.run(
        ["which", "perl"],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    ).stdout.strip()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "perl",
        f"""#!/usr/bin/env bash
set -euo pipefail
real_perl={real_perl!r}
if [[ "$#" -ge 2 && "$1" == "-MFindBin" && "$3" == "-MRTBioScan::OTURefineBlastreport=write_cluster_taxids_with_phase_timings" ]]; then
    cluster_taxids="${{8}}"
    cluster_sizes="${{9}}"
    printf '0\\tTX1\\n1\\tTX2\\n' > "$cluster_taxids"
    printf '0\\t1\\n9\\t1\\n' > "$cluster_sizes"
    exit 0
fi
exec "$real_perl" "$@"
""",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    tax_rows = "read1|COI|sup;TX1\nread2|ITS2|sup;TX2\n"
    clstr_rows = (
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        ">Cluster 1\n"
        "0\t100nt, >read2|ITS2|sup... *\n"
    )
    lineage_rows = "TX1\tK__One\nTX2\tK__Two\n"

    result = _run_parallel(tmp_path, tax_rows, clstr_rows, lineage_rows, 2, env=env)

    assert result.returncode != 0
    assert "cluster_sizes.tsv does not match cluster_taxids.tsv cluster IDs/order" in result.stderr


def test_parallel_wrapper_fails_when_cluster_sizes_record_count_is_invalid(tmp_path: Path) -> None:
    real_perl = subprocess.run(
        ["which", "perl"],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    ).stdout.strip()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "perl",
        f"""#!/usr/bin/env bash
set -euo pipefail
real_perl={real_perl!r}
if [[ "$#" -ge 2 && "$1" == "-MFindBin" && "$3" == "-MRTBioScan::OTURefineBlastreport=write_cluster_taxids_with_phase_timings" ]]; then
    cluster_taxids="${{8}}"
    cluster_sizes="${{9}}"
    printf '0\\tTX1\\n1\\tTX2\\n' > "$cluster_taxids"
    printf '0\\t1\\n1\\tbogus\\n' > "$cluster_sizes"
    exit 0
fi
exec "$real_perl" "$@"
""",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    tax_rows = "read1|COI|sup;TX1\nread2|ITS2|sup;TX2\n"
    clstr_rows = (
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        ">Cluster 1\n"
        "0\t100nt, >read2|ITS2|sup... *\n"
    )
    lineage_rows = "TX1\tK__One\nTX2\tK__Two\n"

    result = _run_parallel(tmp_path, tax_rows, clstr_rows, lineage_rows, 2, env=env)

    assert result.returncode != 0
    assert "invalid record count 'bogus' for cluster 1 in cluster_sizes.tsv" in result.stderr


def test_parallel_worker_fails_when_cluster_taxids_are_incomplete(tmp_path: Path) -> None:
    result = _run_worker(
        tmp_path,
        "0\tTX1\n",
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        ">Cluster 1\n"
        "0\t100nt, >read2|ITS2|sup... *\n",
    )

    assert result.returncode != 0
    assert "Missing cluster taxid for cluster 1" in result.stderr


def test_parallel_wrapper_returns_empty_output_when_inputs_are_empty(tmp_path: Path) -> None:
    tax_file = tmp_path / "blastreport.txt"
    clstr_file = tmp_path / "reads.clstr"
    lineage_file = tmp_path / "id2lineage.tsv"
    tax_file.write_text("", encoding="utf-8")
    clstr_file.write_text("", encoding="utf-8")
    lineage_file.write_text("TX1\tK__One\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(PARALLEL_SCRIPT), str(tax_file), str(clstr_file), str(lineage_file), "4"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
