import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "Consensus_simple.sh"
PRELAUNCH_GATE_HELPER = REPO_ROOT / "bin" / "lib" / "consensus_prelaunch_gate.sh"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _stub_tools(tmp_path: Path) -> Path:
    """Install minimal stubs for seqkit, Rscript, and vsearch."""
    bindir = tmp_path / "stubbin"
    bindir.mkdir(parents=True, exist_ok=True)
    _write_exec(bindir / "seqkit", "#!/bin/sh\nexit 0\n")
    _write_exec(bindir / "Rscript", "#!/bin/sh\nexit 0\n")
    _write_exec(
        bindir / "vsearch",
        "#!/bin/sh\n"
        "in=''; out=''\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --cluster_fast) in=\"$2\"; shift 2 ;;\n"
        "    --clusters) out=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "[ -n \"$in\" ] && [ -n \"$out\" ] && cp \"$in\" \"${out}0\"\n"
        "exit 0\n",
    )
    return bindir


def _read_status(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def _read_tsv_rows(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if not line:
            continue
        values = line.split("\t")
        rows.append(dict(zip(header, values)))
    return rows


def _mirror_delete(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    src_entries = {p.name for p in src.iterdir()} if src.exists() else set()
    dst_entries = {p.name for p in dst.iterdir()} if dst.exists() else set()

    for extra in sorted(dst_entries - src_entries):
        target = dst / extra
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    for name in sorted(src_entries):
        src_path = src / name
        dst_path = dst / name
        if src_path.is_dir():
            if dst_path.exists() and not dst_path.is_dir():
                dst_path.unlink()
            shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
        else:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)


def _run_prelaunch_gate(reads_path: Path, cache_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-lc",
            (
                f"source '{PRELAUNCH_GATE_HELPER}' && "
                f"consensus_prelaunch_should_run '{reads_path}' '{cache_root}'"
            ),
        ],
        capture_output=True,
        text=True,
    )


def test_consensus_reads_mode_invalid_fails(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(REPO_ROOT / "bin"),
            "99",
            "5",
            "50",
            "15",
            "20",
            "",
            "cluster-total",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode != 0


def test_prelaunch_true_noop_branch_keeps_header_only_outputs_without_mode_files(tmp_path: Path) -> None:
    reads_path = tmp_path / "qced_reads_hq_accumulated.fasta"
    cache_root = tmp_path / "Consensus" / ".cache"
    reads_path.write_text("", encoding="utf-8")

    preblast = tmp_path / "RTBioScan_preblastreport_join.txt"
    blast_report = tmp_path / "consensus_blast_report_full.txt"
    provenance = tmp_path / "consensus_round_provenance.tsv"
    preblast.write_text("qseqid,sseqid,evalue,length,pident\n", encoding="utf-8")
    blast_report.write_text(
        "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n",
        encoding="utf-8",
    )
    provenance.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n",
        encoding="utf-8",
    )

    result = _run_prelaunch_gate(reads_path, cache_root)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"
    assert preblast.read_text(encoding="utf-8") == "qseqid,sseqid,evalue,length,pident\n"
    assert blast_report.read_text(encoding="utf-8") == (
        "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"
    )
    assert provenance.read_text(encoding="utf-8") == (
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
    )
    assert not (tmp_path / "samples.txt").exists()
    assert not (tmp_path / "consensus_sample_mode.tsv").exists()


def test_prelaunch_cache_only_branch_detects_real_cache_probe_shape(tmp_path: Path) -> None:
    reads_path = tmp_path / "qced_reads_hq_accumulated.fasta"
    cache_root = tmp_path / "Consensus" / ".cache"
    reads_path.write_text("", encoding="utf-8")
    sample_cache_dir = cache_root / "sample_A"
    sample_cache_dir.mkdir(parents=True)
    (sample_cache_dir / "cached.consensus.fasta").write_text(">x\nACGT\n", encoding="utf-8")

    result = _run_prelaunch_gate(reads_path, cache_root)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"
    assert not (tmp_path / "samples.txt").exists()
    assert not (tmp_path / "consensus_sample_mode.tsv").exists()


def test_cached_only_mode_emits_cached_consensus(tmp_path: Path) -> None:
    """When sup_reads is empty but a cached consensus exists, cache-only mode
    carries it forward and emitted_consensus_count > 0."""
    bindir = _stub_tools(tmp_path)

    # samples.txt: single sample "no_adapter"
    (tmp_path / "samples.txt").write_text("no_adapter\n", encoding="utf-8")

    # Blast report: 5 reads assigned to OTUB_1-COI via adapter=no_adapter_1
    # Format: read_id\totu_id\tkingdom\tmarker (no header → prefilter uses defaults)
    blast_rows = "".join(
        f"read-{i:02d}|COI|hac2sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI"
        f"\tOTUB_1-COI\tMetazoa\tCOI\n"
        for i in range(1, 6)
    )
    (tmp_path / "blast_report_annotated.txt").write_text(blast_rows, encoding="utf-8")

    # sup_reads is EMPTY — triggers cache-only mode
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text("", encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text("", encoding="utf-8")

    # Cache file: name must match the otu_key the script generates.
    # make_key("OTUB_1-COI", "no_adapter_1") = "OTUB_1-COI-no_adapter_1"
    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True)
    (cache_dir / "OTUB_1-COI-no_adapter_1.consensus.fasta").write_text(
        ">no_adapter_1|OTUB_1|COI|reads-5\nACGTACGTACGT\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(REPO_ROOT / "bin"),
            "98",   # consensus_id
            "1",    # min_reads
            "50",   # max_reads
            "15",   # min_qscore
            "20",   # consolidated_min_qscore
            "",     # frozen_members (none)
            "representative",
            "4",    # max_N
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "cache-only mode" in result.stderr, "Expected INFO log about cache-only mode"

    status = _read_status(tmp_path / "Consensus" / "consolidated_ids_status.tsv")
    assert int(status.get("emitted_consensus_count", "0")) > 0, (
        f"Expected emitted_consensus_count > 0; status={status}\nstderr={result.stderr}"
    )


def test_cached_only_mode_lazy_hydrates_from_state_cache(tmp_path: Path) -> None:
    bindir = _stub_tools(tmp_path)

    (tmp_path / "samples.txt").write_text("no_adapter\n", encoding="utf-8")
    blast_rows = "".join(
        f"read-{i:02d}|COI|hac2sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI"
        f"\tOTUB_1-COI\tMetazoa\tCOI\n"
        for i in range(1, 6)
    )
    (tmp_path / "blast_report_annotated.txt").write_text(blast_rows, encoding="utf-8")
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text("", encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text("", encoding="utf-8")

    state_cache_root = tmp_path / "state_cache"
    state_cache_dir = state_cache_root / "no_adapter"
    state_cache_dir.mkdir(parents=True)
    (state_cache_dir / "OTUB_1-COI-no_adapter_1.consensus.fasta").write_text(
        ">no_adapter_1|OTUB_1|COI|reads-5\nACGTACGTACGT\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_CACHE_STATE_ROOT"] = str(state_cache_root)
    env["CONSENSUS_CACHE_SYNC_SCRIPT"] = str(REPO_ROOT / "bin" / "sync_dir_atomic.sh")
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(REPO_ROOT / "bin"),
            "98",
            "1",
            "50",
            "15",
            "20",
            "",
            "representative",
            "4",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "cache-only mode" in result.stderr
    hydrated_consensus = (
        tmp_path
        / "Consensus"
        / ".cache"
        / "no_adapter"
        / "OTUB_1-COI-no_adapter_1.consensus.fasta"
    )
    assert hydrated_consensus.exists()


def test_consensus_phase_timings_summary_keeps_header_and_merge_rows(tmp_path: Path) -> None:
    bindir = _stub_tools(tmp_path)

    (tmp_path / "samples.txt").write_text("no_adapter\n", encoding="utf-8")
    blast_rows = "".join(
        f"read-{i:02d}|COI|hac2sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI"
        f"\tOTUB_1-COI\tMetazoa\tCOI\n"
        for i in range(1, 6)
    )
    (tmp_path / "blast_report_annotated.txt").write_text(blast_rows, encoding="utf-8")
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text("", encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text("", encoding="utf-8")

    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True)
    (cache_dir / "OTUB_1-COI-no_adapter_1.consensus.fasta").write_text(
        ">no_adapter_1|OTUB_1|COI|reads-5\nACGTACGTACGT\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(REPO_ROOT / "bin"),
            "98",
            "1",
            "50",
            "15",
            "20",
            "",
            "representative",
            "4",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    phase_rows = _read_tsv_rows(tmp_path / "Consensus" / "consensus_phase_timings.tsv")
    assert phase_rows
    phase_text = (tmp_path / "Consensus" / "consensus_phase_timings.tsv").read_text(encoding="utf-8").splitlines()
    assert phase_text[0] == "round_id\tscope\tsample\tphase\tseconds\tms"
    for row in phase_rows:
        assert row["seconds"].isdigit()
        assert row["ms"].isdigit()
    phases = [row["phase"] for row in phase_rows]
    phase_set = set(phases)
    assert "counter_and_tmp_merge" in phase_set
    assert "sample_output_merge" in phase_set
    assert "sample_worker_compute_sum" in phase_set
    assert "aggregate_finalize" in phase_set
    assert phases.count("counter_and_tmp_merge") == 1
    assert phases.count("sample_output_merge") == 1
    assert phases.count("aggregate_finalize") == 1
    row_by_phase = {row["phase"]: row for row in phase_rows}
    assert int(row_by_phase["sample_worker_compute_sum"]["ms"]) >= 0
    assert int(row_by_phase["aggregate_finalize"]["ms"]) >= 0

    status = _read_status(tmp_path / "Consensus" / "consolidated_ids_status.tsv")
    assert int(status.get("emitted_consensus_count", "0")) > 0


def test_lazy_cache_hydration_only_restores_present_sample_dirs(tmp_path: Path) -> None:
    bindir = _stub_tools(tmp_path)

    (tmp_path / "samples.txt").write_text("sample_A\nsample_B\n", encoding="utf-8")
    (tmp_path / "blast_report_annotated.txt").write_text(
        "read-a1|COI|hac2sup|barcode=sample_A_1|adapter=sample_A_1|OTUB_1-COI\tOTUB_1-COI\tMetazoa\tCOI\n"
        "read-b1|COI|hac2sup|barcode=sample_B_1|adapter=sample_B_1|OTUB_2-COI\tOTUB_2-COI\tMetazoa\tCOI\n",
        encoding="utf-8",
    )
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text("", encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text("", encoding="utf-8")

    state_cache_root = tmp_path / "state_cache"
    state_cache_dir = state_cache_root / "sample_A"
    state_cache_dir.mkdir(parents=True)
    (state_cache_dir / "OTUB_1-COI-sample_A_1.consensus.fasta").write_text(
        ">sample_A_1|OTUB_1|COI|reads-1\nACGTACGT\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_CACHE_STATE_ROOT"] = str(state_cache_root)
    env["CONSENSUS_CACHE_SYNC_SCRIPT"] = str(REPO_ROOT / "bin" / "sync_dir_atomic.sh")
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(REPO_ROOT / "bin"),
            "98",
            "1",
            "50",
            "15",
            "20",
            "",
            "representative",
            "4",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    hydration_rows = {
        row["sample"]: row
        for row in _read_tsv_rows(tmp_path / "Consensus" / "cache_hydration_stats.tsv")
    }
    assert hydration_rows["sample_A"]["state_cache_present"] == "1"
    assert hydration_rows["sample_A"]["hydrated"] == "1"
    assert int(hydration_rows["sample_A"]["restored_files"]) >= 1
    assert hydration_rows["sample_B"]["state_cache_present"] == "0"
    assert hydration_rows["sample_B"]["hydrated"] == "0"
    assert hydration_rows["sample_B"]["restored_files"] == "0"

    assert (
        tmp_path
        / "Consensus"
        / ".cache"
        / "sample_A"
        / "OTUB_1-COI-sample_A_1.consensus.fasta"
    ).exists()
    sample_b_dir = tmp_path / "Consensus" / ".cache" / "sample_B"
    assert sample_b_dir.is_dir()
    assert not (sample_b_dir / "OTUB_1-COI-sample_A_1.consensus.fasta").exists()


def test_lazy_cache_hydration_fails_closed_when_sync_script_is_missing(tmp_path: Path) -> None:
    bindir = _stub_tools(tmp_path)

    (tmp_path / "samples.txt").write_text("no_adapter\n", encoding="utf-8")
    (tmp_path / "blast_report_annotated.txt").write_text(
        "read-01|COI|hac2sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\tOTUB_1-COI\tMetazoa\tCOI\n",
        encoding="utf-8",
    )
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text("", encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text("", encoding="utf-8")

    state_cache_root = tmp_path / "state_cache"
    state_cache_dir = state_cache_root / "no_adapter"
    state_cache_dir.mkdir(parents=True)
    (state_cache_dir / "OTUB_1-COI-no_adapter_1.consensus.fasta").write_text(
        ">no_adapter_1|OTUB_1|COI|reads-1\nACGTACGT\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_CACHE_STATE_ROOT"] = str(state_cache_root)
    env["CONSENSUS_CACHE_SYNC_SCRIPT"] = str(tmp_path / "missing_sync_dir_atomic.sh")
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(REPO_ROOT / "bin"),
            "98",
            "1",
            "50",
            "15",
            "20",
            "",
            "representative",
            "4",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "ERROR: consensus cache sync script not found" in result.stderr


def test_state_only_cache_sample_is_not_hydrated_during_sample_work(tmp_path: Path) -> None:
    bindir = _stub_tools(tmp_path)

    (tmp_path / "samples.txt").write_text("sample_A\n", encoding="utf-8")
    (tmp_path / "blast_report_annotated.txt").write_text(
        "read-a1|COI|hac2sup|barcode=sample_A_1|adapter=sample_A_1|OTUB_1-COI\tOTUB_1-COI\tMetazoa\tCOI\n",
        encoding="utf-8",
    )
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text("", encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text("", encoding="utf-8")

    state_cache_root = tmp_path / "state_cache"
    sample_a_state_dir = state_cache_root / "sample_A"
    sample_a_state_dir.mkdir(parents=True)
    (sample_a_state_dir / "OTUB_1-COI-sample_A_1.consensus.fasta").write_text(
        ">sample_A_1|OTUB_1|COI|reads-1\nACGTACGT\n",
        encoding="utf-8",
    )
    state_only_dir = state_cache_root / "sample_only_in_state"
    state_only_dir.mkdir(parents=True)
    (state_only_dir / "OTUB_X-COI-sample_only_in_state_1.consensus.fasta").write_text(
        ">sample_only_in_state_1|OTUB_X|COI|reads-1\nTTTTGGGG\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_CACHE_STATE_ROOT"] = str(state_cache_root)
    env["CONSENSUS_CACHE_SYNC_SCRIPT"] = str(REPO_ROOT / "bin" / "sync_dir_atomic.sh")
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            str(REPO_ROOT / "bin"),
            "98",
            "1",
            "50",
            "15",
            "20",
            "",
            "representative",
            "4",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    hydration_rows = {
        row["sample"]: row
        for row in _read_tsv_rows(tmp_path / "Consensus" / "cache_hydration_stats.tsv")
    }
    assert "sample_A" in hydration_rows
    assert "sample_only_in_state" not in hydration_rows


def test_state_only_cache_sample_survives_backfill_and_delete_persist(tmp_path: Path) -> None:
    state_cache_root = tmp_path / "state_cache"
    local_cache_root = tmp_path / "Consensus" / ".cache"
    persisted_cache_root = tmp_path / "persisted" / ".cache"

    sample_a_state_dir = state_cache_root / "sample_A"
    sample_a_state_dir.mkdir(parents=True)
    (sample_a_state_dir / "a.consensus.fasta").write_text(">a\nACGT\n", encoding="utf-8")

    state_only_dir = state_cache_root / "sample_only_in_state"
    state_only_dir.mkdir(parents=True)
    (state_only_dir / "state_only.consensus.fasta").write_text(">b\nTTTT\n", encoding="utf-8")

    sample_a_local_dir = local_cache_root / "sample_A"
    sample_a_local_dir.mkdir(parents=True)
    (sample_a_local_dir / "a.consensus.fasta").write_text(">a\nACGT\n", encoding="utf-8")

    persisted_cache_root.mkdir(parents=True)
    shutil.copytree(state_cache_root, persisted_cache_root, dirs_exist_ok=True)

    helper_script = REPO_ROOT / "bin" / "lib" / "consensus_cache_state.sh"
    sync_script = REPO_ROOT / "bin" / "sync_dir_atomic.sh"
    result = subprocess.run(
        [
            "bash",
            "-lc",
            (
                f"source '{helper_script}' && "
                f"count=$(consensus_cache_backfill_missing_count '{state_cache_root}' '{local_cache_root}') && "
                f"files=$(consensus_cache_backfill_missing_file_count '{state_cache_root}' '{local_cache_root}') && "
                f"[ \"$count\" = \"1\" ] && [ \"$files\" = \"1\" ] && "
                f"restore_missing_consensus_cache_dirs '{state_cache_root}' '{local_cache_root}' '{sync_script}'"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    _mirror_delete(local_cache_root, persisted_cache_root)

    assert (persisted_cache_root / "sample_A" / "a.consensus.fasta").exists()
    assert (
        persisted_cache_root
        / "sample_only_in_state"
        / "state_only.consensus.fasta"
    ).exists()


def test_consensus_warns_with_exit_code_and_stderr_when_adapter_filter_fails(tmp_path: Path) -> None:
    bindir = _stub_tools(tmp_path)
    custom_bin = tmp_path / "custom_bin"
    custom_bin.mkdir(parents=True, exist_ok=True)
    consensus_copy = custom_bin / "Consensus_simple.sh"
    consensus_copy.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    consensus_copy.chmod(0o755)
    _write_exec(
        custom_bin / "filter_blast_rows_by_adapter_class.sh",
        "#!/bin/sh\n"
        "echo 'forced filter failure' 1>&2\n"
        "exit 7\n",
    )

    (tmp_path / "samples.txt").write_text("no_adapter\n", encoding="utf-8")
    blast_rows = (
        "read-01|COI|hac2sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI"
        "\tOTUB_1-COI\tMetazoa\tCOI\n"
    )
    (tmp_path / "blast_report_annotated.txt").write_text(blast_rows, encoding="utf-8")
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text(
        ">read-01|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1\nACGT\n",
        encoding="utf-8",
    )
    (tmp_path / "read_qscore.tsv").write_text("read-01\thac\t30\n", encoding="utf-8")
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    result = subprocess.run(
        [
            "bash",
            str(consensus_copy),
            str(REPO_ROOT / "bin"),
            "98",
            "1",
            "50",
            "15",
            "20",
            str(frozen),
            "representative",
            "4",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert (
        "ERROR: filter_blast_rows_by_adapter_class.sh failed for sample=no_adapter "
        "exit_code=7 stderr=forced filter failure"
    ) in result.stderr


def test_best_consensus_addition_fallback_reads_list_from_sup_fasta(
    tmp_path: Path,
) -> None:
    """_best_consensus_addition() derives _reads.list from sup fasta when _all_reads.list absent.

    After the O-series refactors, ${otu_key}_all_reads.list files are no longer created in the
    OTU processing loop.  The function therefore leaves _reads.list unpopulated, causing
    emit_consensus_round_provenance.pl to emit reads_used_round=NA for every consensus.
    The fix adds a fallback: when _reads.list is still empty after the main block but
    _reads_sup.fasta was written, extract FASTA header IDs as the reads list.

    This test runs the fallback awk command directly (matching the inserted code verbatim)
    and verifies that the resulting _reads.list satisfies emit_consensus_round_provenance.pl.
    """
    # --- set up OriginalReads directory ---
    orig_dir = tmp_path / "OriginalReads"
    orig_dir.mkdir()
    sup_fasta = orig_dir / "Consensus0_reads_sup.fasta"
    # Three reads, two unique UUIDs (r1 appears twice — deduplication required).
    sup_fasta.write_text(
        ">r1|COI|hac|barcode=sample1\nACGT\n"
        ">r2|COI|hac|barcode=sample1\nTGCA\n"
        ">r1|COI|hac|barcode=sample1\nACGT\n",
        encoding="utf-8",
    )
    reads_list = orig_dir / "Consensus0_reads.list"

    # --- run the exact fallback awk command from _best_consensus_addition() ---
    awk_cmd = f"awk '/^>/{{sub(/^>/, \"\"); print}}' '{sup_fasta}' > '{reads_list}'"
    result = subprocess.run(["bash", "-c", awk_cmd], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert reads_list.exists(), "_reads.list not created by fallback"
    lines = reads_list.read_text(encoding="utf-8").splitlines()
    # Three headers extracted (deduplication happens in emit_consensus_round_provenance.pl).
    assert len(lines) == 3

    # --- verify emit_consensus_round_provenance.pl counts 2 unique reads ---
    emit_script = REPO_ROOT / "bin" / "emit_consensus_round_provenance.pl"
    cons_dir = tmp_path / "Consensus" / "sample1"
    cons_dir.mkdir(parents=True)
    (cons_dir / "sample1_Merged_Consensus.fasta").write_text(
        ">sample1|Consensus0|COI|reads-3|OTU=OTUB_1-COI\nACGT\n",
        encoding="utf-8",
    )
    # Place the reads.list where emit_consensus_round_provenance.pl expects it.
    emit_orig_dir = cons_dir / "OriginalReads"
    emit_orig_dir.mkdir()
    (emit_orig_dir / "Consensus0_reads.list").write_text(
        reads_list.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    out = tmp_path / "consensus_round_provenance.tsv"
    emit_result = subprocess.run(
        [
            "perl",
            str(emit_script),
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-barcode",
            "round_001",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert emit_result.returncode == 0, emit_result.stderr
    rows = out.read_text(encoding="utf-8").strip().splitlines()
    # Exactly one data row; reads_used_round must be numeric (2 unique UUIDs).
    assert len(rows) == 2
    fields = rows[1].split("\t")
    assert fields[-1] == "2", f"expected reads_used_round=2, got {fields[-1]}"
