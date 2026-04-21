import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "Consensus_simple.sh"
VALIDATE_PHASE1_WORKLOAD = REPO_ROOT / "bin" / "lib" / "validate_phase1_workload.sh"
SYNC_SCRIPT = REPO_ROOT / "bin" / "sync_dir_atomic.sh"
DROP_FILTER_AWK = REPO_ROOT / "bin" / "consensus_drop_filter.awk"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _install_stub_tools(
    tmp_path: Path,
    *,
    include_seqtk: bool = True,
    include_vsearch: bool = True,
) -> Path:
    bindir = tmp_path / "stubbin"
    bindir.mkdir(parents=True, exist_ok=True)

    _write_exec(
        bindir / "seqkit",
        "#!/bin/bash\n"
        "exit 0\n",
    )
    _write_exec(
        bindir / "Rscript",
        "#!/bin/bash\n"
        "if [ \"$1\" = \"-e\" ]; then\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    if include_vsearch:
        _write_exec(
            bindir / "vsearch",
            "#!/bin/bash\n"
            "in=\"\"\n"
            "out=\"\"\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    --cluster_fast) in=\"$2\"; shift 2 ;;\n"
            "    --clusters) out=\"$2\"; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "if [ -z \"$in\" ] || [ -z \"$out\" ]; then\n"
            "  exit 2\n"
            "fi\n"
            "cp \"$in\" \"${out}0\"\n",
        )
    if include_seqtk:
        _write_exec(
            bindir / "seqtk",
            "#!/bin/bash\n"
            "if [ \"$1\" != \"subseq\" ]; then\n"
            "  exit 2\n"
            "fi\n"
            "fasta=\"$2\"\n"
            "ids=\"$3\"\n"
            "awk 'NR==FNR{want[$1]=1; next} /^>/{h=substr($0,2); sub(/ .*/,\"\",h); keep=(h in want)} keep{print}' \"$ids\" \"$fasta\"\n",
        )
    return bindir


def _write_common_inputs(
    tmp_path: Path,
    blast_lines: str,
    fasta_lines: str,
    qscore_lines: str,
) -> None:
    (tmp_path / "samples.txt").write_text("no_adapter\n", encoding="utf-8")
    (tmp_path / "blast_report_annotated.txt").write_text(blast_lines, encoding="utf-8")
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text(fasta_lines, encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text(qscore_lines, encoding="utf-8")


def _run_consensus(
    tmp_path: Path,
    env: dict,
    min_reads: str = "1",
    frozen_members: str = "",
    script_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if script_path is None:
        script_path = REPO_ROOT / "bin"
    env.setdefault("RTBIOSCAN_TARGET_TOKENS", "COI|ITS2")
    env.setdefault("RTBIOSCAN_TARGET_TAXA", "Metazoa|Viridiplantae")
    cmd = [
        "bash",
        str(SCRIPT),
        str(script_path),
        "98",
        min_reads,
        "50",
        "15",
        "20",
        frozen_members,
        "representative",
        "4",
    ]
    return subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True)


def _make_header(
    uuid: str,
    with_otu: bool,
    *,
    model: str = "hac2sup",
    target: str = "COI",
    barcode: str = "no_adapter_1",
    include_barcode: bool = True,
    otu_token: str = "OTUB_1-COI",
) -> str:
    parts = [uuid, target, model]
    if include_barcode:
        parts.append(f"barcode={barcode}")
    parts.append("adapter=no_adapter_1")
    base = "|".join(parts)
    if with_otu:
        return f"{base}|{otu_token}"
    return base


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
        values = line.split("\t")
        rows.append(dict(zip(header, values)))
    return rows


def _build_single_otu_case(
    tmp_path: Path,
    cluster_specs: list[tuple[str, int, int]],
) -> tuple[Path, Path]:
    bindir = _install_stub_tools(tmp_path)
    blast_lines = ""
    fasta_lines = ""
    qscore_lines = ""
    read_index = 1
    frozen_header = None
    for seq, count, qscore in cluster_specs:
        for _ in range(count):
            uuid = f"read-{read_index:02d}"
            blast_lines += f"{_make_header(uuid, True)}\tOTUB_1-COI\tMetazoa\tCOI\n"
            fasta_lines += f">{_make_header(uuid, False)}\n{seq}\n"
            qscore_lines += f"{uuid}\thac\t{qscore}\n"
            if frozen_header is None:
                frozen_header = _make_header(uuid, False)
            read_index += 1
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text(f"FROZEN_1\t{frozen_header}\t1\n", encoding="utf-8")
    return bindir, frozen


def _base_sig_env(bindir: Path) -> dict:
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_OTU_CONSOLIDATION_MODE"] = "significant_clusters"
    return env


def test_otub_suffix_normalization_recovers_consensus(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    uuids = [f"read-{i:02d}" for i in range(1, 11)]
    blast_lines = "".join(
        f"{_make_header(u, True)}\tOTUB_1-COI\tMetazoa\tCOI\n" for u in uuids
    )
    # FASTA intentionally uses HAC while blast headers use HAC2SUP; resolver must map both.
    fasta_lines = "".join(f">{_make_header(u, False, model='hac')}\nACGTACGTACGT\n" for u in uuids)
    qscore_lines = "".join(f"{u}\thac\t30\n" for u in uuids)
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)

    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text(
        f"FROZEN_1\t{uuids[0]}|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1\t1\n",
        encoding="utf-8",
    )
    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "OTUB_1-COI.consensus.fasta").write_text(
        ">no_adapter_1|OTUB_1|COI|reads-10|OTU=OTUB_1-COI|minQ=30|frozen=1|consolidated=1\n"
        "ACGTACGTACGT\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    status = _read_status(tmp_path / "Consensus" / "consolidated_ids_status.tsv")
    assert int(status.get("emitted_consensus_count", "0")) > 0
    assert status.get("id_mismatch_events") == "0"


def test_id_mismatch_policy_fail_stops_on_unmatched_ids(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    blast_header = _make_header("read-X", True)
    fasta_header = _make_header("read-Y", False)
    _write_common_inputs(
        tmp_path,
        f"{blast_header}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{fasta_header}\nACGT\n",
        "read-X\thac\t30\n",
    )
    (tmp_path / "otu_frozen_members.tsv").write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(tmp_path / "otu_frozen_members.tsv"))
    assert result.returncode != 0
    assert "ID mismatch" in result.stderr


def test_cache_kept_when_below_min_reads(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    hdr = _make_header("read-1", True)
    _write_common_inputs(
        tmp_path,
        f"{hdr}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{_make_header('read-1', False)}\nACGT\n",
        "read-1\thac\t30\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")
    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_cons = cache_dir / "OTUB_1-COI.consensus.fasta"
    cache_meta = cache_dir / "OTUB_1-COI.meta"
    cache_cons.write_text(">x|OTUB_1|COI|reads-1|OTU=OTUB_1-COI\nACGT\n", encoding="utf-8")
    cache_meta.write_text("1\thash\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "warn"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    result = _run_consensus(tmp_path, env, min_reads="5", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr
    assert cache_cons.exists()
    assert cache_meta.exists()


def test_empty_qscore_context_logs_na_quality_and_continues(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    hdr_otub = _make_header("read-1", True)
    hdr_norm = _make_header("read-1", False)
    _write_common_inputs(
        tmp_path,
        f"{hdr_otub}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{hdr_norm}\nACGT\n",
        "",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")
    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "OTUB_1-COI.pool.tsv").write_text(
        f"read-1\t{hdr_otub}\t1\t0\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "warn"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    env["CONSENSUS_DEBUG"] = "1"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr
    debug_log = (tmp_path / "Consensus" / "consensus_debug.log").read_text(encoding="utf-8")
    assert "min_cand=NA" in debug_log


def test_pool_merge_preserves_prior_rows_and_best_rank_qscore(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    reads = [
        ("read-shared", "OTUB_1-COI"),
        ("read-new", "OTUB_1-COI"),
    ]
    blast_lines = "".join(
        f"{_make_header(uid, True, otu_token=otu)}\t{otu}\tMetazoa\tCOI\n"
        for uid, otu in reads
    )
    fasta_lines = "".join(
        f">{_make_header(uid, False, model='hac')}\nACGTACGT\n"
        for uid, _ in reads
    )
    qscore_lines = "read-shared\tsup\t35\nread-new\thac\t25\n"
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pool_name = "OTUB_1-COI-no_adapter_1.pool.tsv"
    (cache_dir / pool_name).write_text(
        "\n".join(
            [
                f"read-keep\t{_make_header('read-keep', False, model='hac')}\t2\t20",
                f"read-shared\t{_make_header('read-shared', False, model='hac')}\t2\t15",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    result = _run_consensus(tmp_path, env, min_reads="2", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    pool_file = tmp_path / "Consensus" / ".cache" / "no_adapter" / pool_name
    rows = [
        ln.strip().split("\t")
        for ln in pool_file.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    by_uuid = {row[0]: row for row in rows}
    assert set(by_uuid) == {"read-keep", "read-shared", "read-new"}
    assert by_uuid["read-keep"][2:] == ["2", "20"]
    assert by_uuid["read-new"][2:] == ["2", "25"]
    assert by_uuid["read-shared"][2:] == ["3", "35"]


def test_missing_seqtk_fails_fast(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path, include_seqtk=False)
    hdr = _make_header("read-1", True)
    _write_common_inputs(
        tmp_path,
        f"{hdr}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{_make_header('read-1', False)}\nACGT\n",
        "read-1\thac\t30\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:/usr/bin:/bin"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode != 0
    assert "seqtk not found in PATH" in result.stderr


def test_missing_vsearch_fails_fast(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path, include_vsearch=False)
    hdr = _make_header("read-1", True)
    _write_common_inputs(
        tmp_path,
        f"{hdr}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{_make_header('read-1', False)}\nACGT\n",
        "read-1\thac\t30\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:/usr/bin:/bin"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode != 0
    assert "vsearch not found in PATH" in result.stderr


def test_scope_prefilter_status_is_written_and_counts_matches(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    blast_lines = (
        f"{_make_header('read-1', True, otu_token='OTUB_1-COI')}\tOTUB_1-COI\tMetazoa\tCOI\n"
        f"{_make_header('read-2', True, otu_token='OTUB_2-ITS2', target='ITS2')}\tOTUB_2-ITS2\tViridiplantae\tITS2\n"
        f"{_make_header('read-3', True, otu_token='OTUB_3-COI')}\tOTUB_3-COI\tFungi\tCOI\n"
    )
    fasta_lines = (
        f">{_make_header('read-1', False, model='hac')}\nACGT\n"
        f">{_make_header('read-2', False, model='hac', target='ITS2')}\nACGT\n"
        f">{_make_header('read-3', False, model='hac')}\nACGT\n"
    )
    qscore_lines = "read-1\thac\t30\nread-2\thac\t30\nread-3\thac\t30\n"
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    status = _read_status(tmp_path / "Consensus" / "prefilter_status.tsv")
    assert status.get("prefilter_input_rows") == "3"
    assert status.get("prefilter_output_rows") == "2"
    assert status.get("prefilter_metazoa_coi_rows") == "1"
    assert status.get("prefilter_viridiplantae_its2_rows") == "1"


def test_scope_prefilter_uses_kingdom_and_marker_columns(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    blast_lines = (
        f"{_make_header('read-1', True, otu_token='OTUB_Metazoa-COI')}\tOTUB_Metazoa-COI\tFungi\tITS2\n"
        f"{_make_header('read-2', True, otu_token='OTUB_2-COI')}\tOTUB_2-COI\tMetazoa\tCOI\n"
    )
    fasta_lines = (
        f">{_make_header('read-1', False, model='hac')}\nACGT\n"
        f">{_make_header('read-2', False, model='hac')}\nACGT\n"
    )
    qscore_lines = "read-1\thac\t30\nread-2\thac\t30\n"
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    status = _read_status(tmp_path / "Consensus" / "prefilter_status.tsv")
    assert status.get("prefilter_input_rows") == "2"
    assert status.get("prefilter_output_rows") == "1"
    assert status.get("prefilter_metazoa_coi_rows") == "1"


def test_otu_falls_back_to_column2_when_missing_in_header(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    hdr_blast = f"{_make_header('read-1', False)}|XTOKEN"
    hdr_fasta = _make_header("read-1", False, model="hac")
    _write_common_inputs(
        tmp_path,
        f"{hdr_blast}\tOTUB_55-COI\tMetazoa\tCOI\n",
        f">{hdr_fasta}\nACGT\n",
        "read-1\thac\t30\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    sample_meta = tmp_path / "Consensus" / "no_adapter" / "otu_meta.tsv"
    rows = [line.strip().split("\t") for line in sample_meta.read_text(encoding="utf-8").splitlines() if line.strip()]
    keys = {r[0] for r in rows}
    assert "OTUB_55-COI-no_adapter_1" in keys


def test_otu_key_without_barcode_is_stable_in_sample_meta(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    hdr_blast = _make_header(
        "read-1",
        True,
        include_barcode=False,
        otu_token="OTUB_77-COI",
    )
    hdr_fasta = _make_header("read-1", False, model="hac", include_barcode=False)
    _write_common_inputs(
        tmp_path,
        f"{hdr_blast}\tOTUB_77-COI\tMetazoa\tCOI\n",
        f">{hdr_fasta}\nACGT\n",
        "read-1\thac\t30\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    sample_meta = tmp_path / "Consensus" / "no_adapter" / "otu_meta.tsv"
    assert sample_meta.exists()
    rows = [line.strip().split("\t") for line in sample_meta.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows
    assert rows[0][0] == "OTUB_77-COI"


def test_otu_key_matching_is_exact_not_prefix_based(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    blast_lines = (
        f"{_make_header('read-1', True, otu_token='OTUB_12-COI')}\tOTUB_12-COI\tMetazoa\tCOI\n"
        f"{_make_header('read-2', True, otu_token='OTUB_123-COI')}\tOTUB_123-COI\tMetazoa\tCOI\n"
    )
    fasta_lines = (
        f">{_make_header('read-1', False, model='hac')}\nACGT\n"
        f">{_make_header('read-2', False, model='hac')}\nACGT\n"
    )
    qscore_lines = "read-1\thac\t30\nread-2\thac\t30\n"
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    sample_meta = tmp_path / "Consensus" / "no_adapter" / "otu_meta.tsv"
    rows = [line.strip().split("\t") for line in sample_meta.read_text(encoding="utf-8").splitlines() if line.strip()]
    keys = {r[0] for r in rows}
    assert "OTUB_12-COI-no_adapter_1" in keys
    assert "OTUB_123-COI-no_adapter_1" in keys
    assert len(keys) == 2


def test_strict_mode_keeps_running_when_sample_filter_has_no_matches(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    blast_header = "read-1|COI|hac2sup|barcode=barcode_1|adapter=barcode_1|OTUB_1-COI"
    fasta_header = "read-1|COI|hac|barcode=barcode_1|adapter=barcode_1"
    _write_common_inputs(
        tmp_path,
        f"{blast_header}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{fasta_header}\nACGT\n",
        "read-1\thac\t30\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    status = _read_status(tmp_path / "Consensus" / "consolidated_ids_status.tsv")
    assert status.get("emitted_consensus_count") == "0"


def test_seqtk_failure_warn_policy_does_not_abort(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    # Stub seqtk to fail so the batch-pool seqtk fallback path is exercised.
    _write_exec(
        bindir / "seqtk",
        "#!/bin/bash\n"
        "exit 1\n",
    )
    # O1b: samtools faidx is tried before seqtk; stub it to fail too so the
    # seqtk fallback (and its warning) is actually reached.
    _write_exec(
        bindir / "samtools",
        "#!/bin/bash\n"
        "exit 1\n",
    )
    hdr = _make_header("read-1", True)
    _write_common_inputs(
        tmp_path,
        f"{hdr}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{_make_header('read-1', False)}\nACGT\n",
        "read-1\thac\t30\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "warn"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode != 0
    assert "ERROR: Consensus seqtk extraction failed (pool batch)" in result.stderr


# ---------------------------------------------------------------------------
# PR3: sync_dir_atomic round-transition acceptance tests
# ---------------------------------------------------------------------------

def _run_sync(src: Path, dst: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(src), str(dst)],
        capture_output=True,
        text=True,
    )


def _run_drop_filter(
    mode: str,
    drop_file: Path,
    in_file: Path,
    *,
    drop_var: str | None = None,
    cwd: Path | None = None,
    global_suffix_mode: str = "strict",
) -> subprocess.CompletedProcess[str]:
    if drop_var is None:
        drop_var = str(drop_file)
    cmd = [
        "awk",
        "-v",
        f"MODE={mode}",
        "-v",
        f"DROP={drop_var}",
        "-v",
        f"ID_GLOBAL_SUFFIX_MODE={global_suffix_mode}",
        "-f",
        str(DROP_FILTER_AWK),
    ]
    if mode == "keys":
        cmd += [str(drop_file), str(in_file)]
    elif mode == "ids":
        cmd += [str(drop_file), str(in_file), str(in_file)]
    else:
        raise ValueError(mode)
    return subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd)


def test_sync_dir_atomic_initial_sync_matches_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "file_a.txt").write_text("alpha\n", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "file_b.txt").write_text("beta\n", encoding="utf-8")

    dst = tmp_path / "dst"
    result = _run_sync(src, dst)
    assert result.returncode == 0, result.stderr

    assert (dst / "file_a.txt").read_text(encoding="utf-8") == "alpha\n"
    assert (dst / "sub" / "file_b.txt").read_text(encoding="utf-8") == "beta\n"


def test_sync_dir_atomic_incremental_sync_removes_stale_and_adds_new(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "old_file.txt").write_text("old\n", encoding="utf-8")

    dst = tmp_path / "dst"
    result = _run_sync(src, dst)
    assert result.returncode == 0, result.stderr
    assert (dst / "old_file.txt").exists()

    # Mutate src: remove old file, add new file
    (src / "old_file.txt").unlink()
    (src / "new_file.txt").write_text("new\n", encoding="utf-8")

    result = _run_sync(src, dst)
    assert result.returncode == 0, result.stderr

    assert not (dst / "old_file.txt").exists(), "Stale file must be removed from dst"
    assert (dst / "new_file.txt").read_text(encoding="utf-8") == "new\n"


# ---------------------------------------------------------------------------
# PR5: batched seqtk candidate-read extraction acceptance tests
# ---------------------------------------------------------------------------

def _count_seqs(fasta: Path) -> int:
    """Count sequences in a FASTA file (by counting '>' lines)."""
    if not fasta.exists():
        return 0
    return sum(1 for ln in fasta.read_text(encoding="utf-8").splitlines() if ln.startswith(">"))


def test_batched_seqtk_routes_reads_to_correct_otus(tmp_path: Path) -> None:
    """Batched split-back must route reads by exact OTU key (OTUB_12 vs OTUB_123).

    With id_mismatch_policy=fail, a mis-routed read (e.g. OTUB_12's read landing in
    OTUB_123's file) leaves the target file empty → the mismatch validator aborts with
    exit 1.  A clean run (correct routing) exits 0 and both keys appear in otu_meta.tsv.
    """
    bindir = _install_stub_tools(tmp_path)

    blast_lines = (
        f"{_make_header('read-1', True, otu_token='OTUB_12-COI')}\tOTUB_12-COI\tMetazoa\tCOI\n"
        f"{_make_header('read-2', True, otu_token='OTUB_123-COI')}\tOTUB_123-COI\tMetazoa\tCOI\n"
    )
    fasta_lines = (
        f">{_make_header('read-1', False, model='hac')}\nAAAA\n"
        f">{_make_header('read-2', False, model='hac')}\nTTTT\n"
    )
    qscore_lines = "read-1\thac\t30\nread-2\thac\t30\n"
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    # fail policy: any mis-routed read leaves a file empty → mismatch validator → exit 1
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, (
        "Batched split-back mis-routed reads: mismatch validator fired.\n" + result.stderr
    )

    # Both OTU keys must be present in otu_meta.tsv (written before cleanup).
    sample_meta = tmp_path / "Consensus" / "no_adapter" / "otu_meta.tsv"
    rows = [ln.strip().split("\t") for ln in sample_meta.read_text(encoding="utf-8").splitlines() if ln.strip()]
    keys = {r[0] for r in rows}
    assert "OTUB_12-COI-no_adapter_1" in keys, f"OTUB_12 missing from otu_meta.tsv: {keys}"
    assert "OTUB_123-COI-no_adapter_1" in keys, f"OTUB_123 missing from otu_meta.tsv: {keys}"


def test_batched_seqtk_no_loss_multi_otu(tmp_path: Path) -> None:
    """Union extraction + split-back must not lose reads: total n_cand == reads supplied."""
    bindir = _install_stub_tools(tmp_path)

    # Three OTUs: 2 reads for OTU1, 2 for OTU2, 1 for OTU3 = 5 total.
    reads = [
        ("read-A", "OTUB_1-COI"),
        ("read-B", "OTUB_1-COI"),
        ("read-C", "OTUB_2-COI"),
        ("read-D", "OTUB_2-COI"),
        ("read-E", "OTUB_3-COI"),
    ]
    blast_lines = "".join(
        f"{_make_header(uid, True, otu_token=otu)}\t{otu}\tMetazoa\tCOI\n"
        for uid, otu in reads
    )
    fasta_lines = "".join(
        f">{_make_header(uid, False, model='hac')}\nACGT\n"
        for uid, _ in reads
    )
    qscore_lines = "".join(f"{uid}\thac\t30\n" for uid, _ in reads)
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    # otu_meta.tsv col-3 is n_cand (candidate read count).  Sum must equal 5.
    sample_meta = tmp_path / "Consensus" / "no_adapter" / "otu_meta.tsv"
    rows = [ln.strip().split("\t") for ln in sample_meta.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 3, f"Expected 3 OTU rows, got {len(rows)}: {rows}"
    total_cand = sum(int(r[2]) for r in rows if len(r) >= 3)
    assert total_cand == 5, (
        f"Expected 5 total candidate reads across 3 OTUs, got {total_cand}. Rows: {rows}"
    )


def test_consolidated_keys_written_by_cluster_rule(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    uuids = [f"read-{i:02d}" for i in range(1, 13)]
    blast_lines = "".join(
        f"{_make_header(u, True)}\tOTUB_1-COI\tMetazoa\tCOI\n" for u in uuids
    )
    fasta_lines = ""
    qscore_lines = ""
    for i, u in enumerate(uuids, start=1):
        seq = "AAAA" if i <= 11 else "CCCC"
        fasta_lines += f">{_make_header(u, False)}\n{seq}\n"
        qscore = 30 if i <= 11 else 10
        qscore_lines += f"{u}\thac\t{qscore}\n"
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)

    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text(
        f"FROZEN_1\t{_make_header(uuids[0], False)}\t1\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_LOCK_RATIO"] = "0.1"
    env["CONSENSUS_LOCK_MIN_CONS_READS"] = "10"
    env["CONSENSUS_PRUNE_FROZEN_POLICY"] = "until_consolidated"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    consolidated_keys = (tmp_path / "Consensus" / "otu_consolidated_keys.tsv").read_text(encoding="utf-8")
    assert "no_adapter\tOTUB_1-COI-no_adapter_1" in consolidated_keys
    lock_state = (tmp_path / "Consensus" / ".cache" / "no_adapter" / "lock_state.tsv").read_text(encoding="utf-8")
    assert "OTUB_1-COI-no_adapter_1\t1\t1" in lock_state


def test_lock_min_stable_rounds_requires_two_consecutive_passes(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    uuids = [f"read-{i:02d}" for i in range(1, 13)]
    blast_lines = "".join(
        f"{_make_header(u, True)}\tOTUB_1-COI\tMetazoa\tCOI\n" for u in uuids
    )
    fasta_lines = ""
    qscore_lines = ""
    for i, u in enumerate(uuids, start=1):
        seq = "AAAA" if i <= 11 else "CCCC"
        fasta_lines += f">{_make_header(u, False)}\n{seq}\n"
        qscore = 30 if i <= 11 else 10
        qscore_lines += f"{u}\thac\t{qscore}\n"
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text(
        f"FROZEN_1\t{_make_header(uuids[0], False)}\t1\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_LOCK_RATIO"] = "0.1"
    env["CONSENSUS_LOCK_MIN_CONS_READS"] = "10"
    env["CONSENSUS_LOCK_MIN_STABLE_ROUNDS"] = "2"
    env["CONSENSUS_PRUNE_FROZEN_POLICY"] = "until_consolidated"

    # Round 1: lock rule passes but should not consolidate yet (needs 2 consecutive rounds).
    result1 = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result1.returncode == 0, result1.stderr
    consolidated_round1 = (tmp_path / "Consensus" / "otu_consolidated_keys.tsv").read_text(encoding="utf-8")
    assert "OTUB_1-COI-no_adapter_1" not in consolidated_round1
    lock_state_round1 = (tmp_path / "Consensus" / ".cache" / "no_adapter" / "lock_state.tsv").read_text(encoding="utf-8")
    assert "OTUB_1-COI-no_adapter_1\t1\t1" in lock_state_round1

    # Round 2: consecutive pass should now consolidate.
    result2 = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result2.returncode == 0, result2.stderr
    consolidated_round2 = (tmp_path / "Consensus" / "otu_consolidated_keys.tsv").read_text(encoding="utf-8")
    assert "no_adapter\tOTUB_1-COI-no_adapter_1" in consolidated_round2


def test_significant_clusters_fraction_signature_compatibility_keeps_prior_state(tmp_path: Path) -> None:
    bindir, frozen = _build_single_otu_case(tmp_path, [("AAAA", 11, 30), ("CCCC", 1, 10)])
    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "lock_state.tsv").write_text("OTUB_1-COI-no_adapter_1\t1\t1\n", encoding="utf-8")
    (cache_dir / "consolidation_policy_signature.txt").write_text(
        "mode=significant_clusters|sig_min_cluster_reads=10|sig_min_cluster_qscore=20|sig_min_pool_fraction=0.10|sig_min_top_fraction=0.20|sig_min_stable_rounds=2\n",
        encoding="utf-8",
    )

    env = _base_sig_env(bindir)
    env["CONSENSUS_SIG_MIN_STABLE_ROUNDS"] = "2"

    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    consolidated_keys = (tmp_path / "Consensus" / "otu_consolidated_keys.tsv").read_text(encoding="utf-8")
    assert "no_adapter\tOTUB_1-COI-no_adapter_1" in consolidated_keys
    lock_state = (cache_dir / "lock_state.tsv").read_text(encoding="utf-8")
    assert "OTUB_1-COI-no_adapter_1\t2\t1" in lock_state


def test_top_two_gap_signature_ignores_fraction_only_threshold_changes(tmp_path: Path) -> None:
    bindir, frozen = _build_single_otu_case(tmp_path, [("AAAA", 11, 30), ("CCCC", 4, 30)])
    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "lock_state.tsv").write_text("OTUB_1-COI-no_adapter_1\t1\t1\n", encoding="utf-8")
    (cache_dir / "consolidation_policy_signature.txt").write_text(
        "mode=significant_clusters|sig_min_cluster_reads=10|sig_min_cluster_qscore=20|sig_min_stable_rounds=2|sig_rule=top_two_gap|sig_top2_min_ratio=2.0|sig_top2_min_delta_reads=4\n",
        encoding="utf-8",
    )

    env = _base_sig_env(bindir)
    env["CONSENSUS_SIG_RULE"] = "top_two_gap"
    env["CONSENSUS_SIG_TOP2_MIN_RATIO"] = "2.0"
    env["CONSENSUS_SIG_TOP2_MIN_DELTA_READS"] = "4"
    env["CONSENSUS_SIG_MIN_STABLE_ROUNDS"] = "2"
    env["CONSENSUS_SIG_MIN_POOL_FRACTION"] = "0.05"
    env["CONSENSUS_SIG_MIN_TOP_FRACTION"] = "0.10"

    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    rows = _read_tsv_rows(tmp_path / "Consensus" / "otu_lock_summary.tsv")
    row = next(row for row in rows if row["otu_key"] == "OTUB_1-COI-no_adapter_1")
    assert row["reason"] == "consolidated"
    lock_state = (cache_dir / "lock_state.tsv").read_text(encoding="utf-8")
    assert "OTUB_1-COI-no_adapter_1\t2\t1" in lock_state


def test_top_two_gap_requires_two_consecutive_passes(tmp_path: Path) -> None:
    bindir, frozen = _build_single_otu_case(tmp_path, [("AAAA", 11, 30), ("CCCC", 4, 30)])

    env = _base_sig_env(bindir)
    env["CONSENSUS_SIG_RULE"] = "top_two_gap"
    env["CONSENSUS_SIG_TOP2_MIN_RATIO"] = "2.0"
    env["CONSENSUS_SIG_TOP2_MIN_DELTA_READS"] = "4"
    env["CONSENSUS_SIG_MIN_STABLE_ROUNDS"] = "2"

    result1 = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result1.returncode == 0, result1.stderr
    rows1 = _read_tsv_rows(tmp_path / "Consensus" / "otu_lock_summary.tsv")
    row1 = next(row for row in rows1 if row["otu_key"] == "OTUB_1-COI-no_adapter_1")
    assert row1["reason"] == "stable_rounds"
    assert row1["otu_sig_rule"] == "top_two_gap"
    assert row1["top1_cluster_size"] == "10"
    assert row1["top2_cluster_size"] == "4"

    result2 = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result2.returncode == 0, result2.stderr
    rows2 = _read_tsv_rows(tmp_path / "Consensus" / "otu_lock_summary.tsv")
    row2 = next(row for row in rows2 if row["otu_key"] == "OTUB_1-COI-no_adapter_1")
    assert row2["reason"] == "consolidated"
    assert row2["stable_count"] == "2"


def test_top_two_gap_reports_ratio_failure(tmp_path: Path) -> None:
    bindir, frozen = _build_single_otu_case(tmp_path, [("AAAA", 11, 30), ("CCCC", 6, 30)])

    env = _base_sig_env(bindir)
    env["CONSENSUS_SIG_RULE"] = "top_two_gap"
    env["CONSENSUS_SIG_TOP2_MIN_RATIO"] = "2.0"
    env["CONSENSUS_SIG_TOP2_MIN_DELTA_READS"] = "4"
    env["CONSENSUS_SIG_MIN_STABLE_ROUNDS"] = "1"

    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    rows = _read_tsv_rows(tmp_path / "Consensus" / "otu_lock_summary.tsv")
    row = next(row for row in rows if row["otu_key"] == "OTUB_1-COI-no_adapter_1")
    assert row["reason"] == "top_two_ratio_failed"
    assert row["top2_ratio"] != "NA"
    assert row["top2_delta_reads"] == "4"


def test_top_two_gap_reports_delta_failure(tmp_path: Path) -> None:
    bindir, frozen = _build_single_otu_case(tmp_path, [("AAAA", 11, 30), ("CCCC", 5, 30)])

    env = _base_sig_env(bindir)
    env["CONSENSUS_SIG_RULE"] = "top_two_gap"
    env["CONSENSUS_SIG_TOP2_MIN_RATIO"] = "2.0"
    env["CONSENSUS_SIG_TOP2_MIN_DELTA_READS"] = "6"
    env["CONSENSUS_SIG_MIN_STABLE_ROUNDS"] = "1"

    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    rows = _read_tsv_rows(tmp_path / "Consensus" / "otu_lock_summary.tsv")
    row = next(row for row in rows if row["otu_key"] == "OTUB_1-COI-no_adapter_1")
    assert row["reason"] == "top_two_delta_failed"
    assert row["top2_ratio"] == "2.000000"
    assert row["top2_delta_reads"] == "5"


def test_top_two_gap_reports_ratio_and_delta_failure(tmp_path: Path) -> None:
    bindir, frozen = _build_single_otu_case(tmp_path, [("AAAA", 11, 30), ("CCCC", 9, 30)])

    env = _base_sig_env(bindir)
    env["CONSENSUS_SIG_RULE"] = "top_two_gap"
    env["CONSENSUS_SIG_TOP2_MIN_RATIO"] = "2.0"
    env["CONSENSUS_SIG_TOP2_MIN_DELTA_READS"] = "3"
    env["CONSENSUS_SIG_MIN_STABLE_ROUNDS"] = "1"

    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    rows = _read_tsv_rows(tmp_path / "Consensus" / "otu_lock_summary.tsv")
    row = next(row for row in rows if row["otu_key"] == "OTUB_1-COI-no_adapter_1")
    assert row["reason"] == "top_two_ratio_and_delta_failed"


def test_top_two_gap_ignores_low_qscore_competitor(tmp_path: Path) -> None:
    bindir, frozen = _build_single_otu_case(tmp_path, [("AAAA", 11, 30), ("CCCC", 9, 10)])

    env = _base_sig_env(bindir)
    env["CONSENSUS_SIG_RULE"] = "top_two_gap"
    env["CONSENSUS_SIG_TOP2_MIN_RATIO"] = "2.0"
    env["CONSENSUS_SIG_TOP2_MIN_DELTA_READS"] = "4"
    env["CONSENSUS_SIG_MIN_STABLE_ROUNDS"] = "1"

    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    rows = _read_tsv_rows(tmp_path / "Consensus" / "otu_lock_summary.tsv")
    row = next(row for row in rows if row["otu_key"] == "OTUB_1-COI-no_adapter_1")
    assert row["reason"] == "consolidated"
    assert row["top2_cluster_size"] == "0"
    sig_rows = _read_tsv_rows(tmp_path / "Consensus" / "significant_cluster_summary.tsv")
    leader_rows = [r for r in sig_rows if r["candidate_role"] == "leader" and r["significant_flag"] == "1"]
    assert leader_rows


def test_lock_reset_keys_clears_prior_lock_and_forces_recompute_path(tmp_path: Path) -> None:
    def setup_case(case_dir: Path) -> tuple[Path, Path]:
        case_dir.mkdir(parents=True, exist_ok=True)
        bindir = _install_stub_tools(case_dir)
        hdr_otub = _make_header("read-1", True)
        hdr_norm = _make_header("read-1", False, model="hac")
        _write_common_inputs(
            case_dir,
            f"{hdr_otub}\tOTUB_1-COI\tMetazoa\tCOI\n",
            f">{hdr_norm}\nACGTACGT\n",
            "read-1\thac\t30\n",
        )
        frozen = case_dir / "otu_frozen_members.tsv"
        frozen.write_text("", encoding="utf-8")
        cache_dir = case_dir / "Consensus" / ".cache" / "no_adapter"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_cons = cache_dir / "OTUB_1-COI-no_adapter_1.consensus.fasta"
        cache_meta = cache_dir / "OTUB_1-COI-no_adapter_1.meta"
        # No consolidated=1 tag: lock reuse should depend on previous lock keys, not cache header.
        cache_cons.write_text(
            ">no_adapter|OTUB_1|COI|reads-1|OTU=OTUB_1-COI-no_adapter_1|minQ=30|frozen=0\nACGTACGT\n",
            encoding="utf-8",
        )
        # Force recompute path when lock is cleared.
        cache_meta.write_text("1\tdeadbeefdeadbeefdeadbeefdeadbeef\n", encoding="utf-8")
        lock_prev = case_dir / "prev_otu_consolidated_keys.tsv"
        lock_prev.write_text("no_adapter\tOTUB_1-COI-no_adapter_1\n", encoding="utf-8")
        return bindir, lock_prev

    # Case A: no reset -> prior lock should keep cached consensus.
    case_a = tmp_path / "case_a"
    bindir_a, lock_prev_a = setup_case(case_a)
    env_a = os.environ.copy()
    env_a["PATH"] = f"{bindir_a}:{env_a.get('PATH', '')}"
    env_a["CONSENSUS_LOCK_ENABLED"] = "1"
    env_a["CONSENSUS_LOCK_KEYS_PREV"] = str(lock_prev_a)
    env_a["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env_a["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    res_a = _run_consensus(case_a, env_a, min_reads="1", frozen_members=str(case_a / "otu_frozen_members.tsv"))
    assert res_a.returncode == 0, res_a.stderr
    cache_cons_a = case_a / "Consensus" / ".cache" / "no_adapter" / "OTUB_1-COI-no_adapter_1.consensus.fasta"
    assert cache_cons_a.exists()

    # Case B: reset key provided -> lock is removed, recompute path runs, stale cache is not kept.
    case_b = tmp_path / "case_b"
    bindir_b, lock_prev_b = setup_case(case_b)
    env_b = os.environ.copy()
    env_b["PATH"] = f"{bindir_b}:{env_b.get('PATH', '')}"
    env_b["CONSENSUS_LOCK_ENABLED"] = "1"
    env_b["CONSENSUS_LOCK_KEYS_PREV"] = str(lock_prev_b)
    env_b["CONSENSUS_LOCK_RESET_KEYS"] = "OTUB_1-COI-no_adapter_1"
    env_b["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env_b["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    res_b = _run_consensus(case_b, env_b, min_reads="1", frozen_members=str(case_b / "otu_frozen_members.tsv"))
    assert res_b.returncode == 0, res_b.stderr
    cache_cons_b = case_b / "Consensus" / ".cache" / "no_adapter" / "OTUB_1-COI-no_adapter_1.consensus.fasta"
    assert not cache_cons_b.exists()
    consolidated_keys_b = (case_b / "Consensus" / "otu_consolidated_keys.tsv").read_text(encoding="utf-8")
    assert "OTUB_1-COI-no_adapter_1" not in consolidated_keys_b


def test_locked_otu_missing_from_current_round_is_carried_forward(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    # Current round only has OTUB_1 reads.
    hdr_otub1 = _make_header("read-1", True, otu_token="OTUB_1-COI")
    hdr_norm1 = _make_header("read-1", False, model="hac", otu_token="OTUB_1-COI")
    _write_common_inputs(
        tmp_path,
        f"{hdr_otub1}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{hdr_norm1}\nACGTACGT\n",
        "read-1\thac\t30\n",
    )
    (tmp_path / "otu_frozen_members.tsv").write_text("", encoding="utf-8")

    # Seed a locked OTU2 cache that has no reads in this round.
    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "OTUB_2-COI-no_adapter_1.consensus.fasta").write_text(
        ">no_adapter|OTUB_2|COI|reads-5|OTU=OTUB_2-COI-no_adapter_1|n=5|minQ=25|frozen=1|consolidated=1\nACGTACGT\n",
        encoding="utf-8",
    )
    (cache_dir / "OTUB_2-COI-no_adapter_1.meta").write_text("5\tabc123\n", encoding="utf-8")
    (cache_dir / "lock_state.tsv").write_text("OTUB_2-COI-no_adapter_1\t3\t1\n", encoding="utf-8")
    lock_prev = tmp_path / "prev_otu_consolidated_keys.tsv"
    lock_prev.write_text("no_adapter\tOTUB_2-COI-no_adapter_1\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_LOCK_KEYS_PREV"] = str(lock_prev)
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"

    res = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(tmp_path / "otu_frozen_members.tsv"))
    assert res.returncode == 0, res.stderr

    consolidated_keys = (tmp_path / "Consensus" / "otu_consolidated_keys.tsv").read_text(encoding="utf-8")
    assert "no_adapter\tOTUB_2-COI-no_adapter_1" in consolidated_keys
    lock_state = (tmp_path / "Consensus" / ".cache" / "no_adapter" / "lock_state.tsv").read_text(encoding="utf-8")
    assert "OTUB_2-COI-no_adapter_1\t3\t1" in lock_state
    sample_meta = (tmp_path / "Consensus" / "no_adapter" / "otu_meta.tsv").read_text(encoding="utf-8")
    assert "OTUB_2-COI-no_adapter_1\tno_adapter\t5\t25\t1\t1" in sample_meta


def test_locked_otu_missing_cache_is_not_marked_consolidated_and_state_defaults_to_zero(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    hdr_otub1 = _make_header("read-1", True, otu_token="OTUB_1-COI")
    hdr_norm1 = _make_header("read-1", False, model="hac", otu_token="OTUB_1-COI")
    _write_common_inputs(
        tmp_path,
        f"{hdr_otub1}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{hdr_norm1}\nACGTACGT\n",
        "read-1\thac\t30\n",
    )
    (tmp_path / "otu_frozen_members.tsv").write_text("", encoding="utf-8")

    # Lock key exists but there is no cache_consensus and no prior lock_state row.
    cache_dir = tmp_path / "Consensus" / ".cache" / "no_adapter"
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_prev = tmp_path / "prev_otu_consolidated_keys.tsv"
    lock_prev.write_text("no_adapter\tOTUB_2-COI-no_adapter_1\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_LOCK_KEYS_PREV"] = str(lock_prev)
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"

    res = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(tmp_path / "otu_frozen_members.tsv"))
    assert res.returncode == 0, res.stderr

    consolidated_keys = (tmp_path / "Consensus" / "otu_consolidated_keys.tsv").read_text(encoding="utf-8")
    assert "OTUB_2-COI-no_adapter_1" not in consolidated_keys
    lock_state = (tmp_path / "Consensus" / ".cache" / "no_adapter" / "lock_state.tsv").read_text(encoding="utf-8")
    assert "OTUB_2-COI-no_adapter_1\t0\t0" in lock_state
    sample_meta = (tmp_path / "Consensus" / "no_adapter" / "otu_meta.tsv").read_text(encoding="utf-8")
    assert "OTUB_2-COI-no_adapter_1\tno_adapter\t0\tNA\t1\t0" in sample_meta


def test_zero_emit_drops_removed_keys_from_consolidated_ids(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    hdr_otub1 = _make_header("read-1", True, otu_token="OTUB_1-COI")
    hdr_norm1 = _make_header("read-1", False, model="hac", otu_token="OTUB_1-COI")
    _write_common_inputs(
        tmp_path,
        f"{hdr_otub1}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{hdr_norm1}\nACGTACGT\n",
        "read-1\thac\t30\n",
    )
    (tmp_path / "otu_frozen_members.tsv").write_text("", encoding="utf-8")

    out_dir = tmp_path / "Consensus"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "consolidated_consensus_ids.txt").write_text(
        ">no_adapter|OTUB_2|COI|reads-5|OTU=OTUB_2-COI-no_adapter_1|minQ=25|frozen=1|consolidated=1\n"
        "OTUB_2_no_adapter\n"
        ">no_adapter|OTUB_9|COI|reads-3|OTU=OTUB_9-COI-no_adapter_1|minQ=20|frozen=1|consolidated=1\n"
        "OTUB_9_no_adapter\n",
        encoding="utf-8",
    )
    lock_prev = tmp_path / "prev_otu_consolidated_keys.tsv"
    lock_prev.write_text("no_adapter\tOTUB_2-COI-no_adapter_1\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_LOCK_KEYS_PREV"] = str(lock_prev)
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"

    # Force a zero-emission round (n_cand < min_reads), while lock carry-forward
    # adds OTUB_2 to drop list due to missing cache.
    res = _run_consensus(tmp_path, env, min_reads="5", frozen_members=str(tmp_path / "otu_frozen_members.tsv"))
    assert res.returncode == 0, res.stderr

    consolidated_ids = (out_dir / "consolidated_consensus_ids.txt").read_text(encoding="utf-8")
    assert "OTUB_2-COI-no_adapter_1" not in consolidated_ids
    assert "OTUB_2_no_adapter" not in consolidated_ids
    assert "OTUB_9-COI-no_adapter_1" in consolidated_ids
    assert "OTUB_9_no_adapter" in consolidated_ids

    consolidated_keys = (out_dir / "otu_consolidated_keys.tsv").read_text(encoding="utf-8")
    assert "no_adapter\tOTUB_2-COI-no_adapter_1" not in consolidated_keys


def test_zero_emit_without_drop_keeps_previous_consolidated_ids(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    hdr_otub1 = _make_header("read-1", True, otu_token="OTUB_1-COI")
    hdr_norm1 = _make_header("read-1", False, model="hac", otu_token="OTUB_1-COI")
    _write_common_inputs(
        tmp_path,
        f"{hdr_otub1}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{hdr_norm1}\nACGTACGT\n",
        "read-1\thac\t30\n",
    )
    (tmp_path / "otu_frozen_members.tsv").write_text("", encoding="utf-8")

    out_dir = tmp_path / "Consensus"
    out_dir.mkdir(parents=True, exist_ok=True)
    prev_ids = (
        ">no_adapter|OTUB_9|COI|reads-3|OTU=OTUB_9-COI-no_adapter_1|minQ=20|frozen=1|consolidated=1\n"
        "OTUB_9_no_adapter\n"
    )
    (out_dir / "consolidated_consensus_ids.txt").write_text(prev_ids, encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"

    # Zero-emission round with no dropped OTU keys should keep previous IDs as-is.
    res = _run_consensus(tmp_path, env, min_reads="5", frozen_members=str(tmp_path / "otu_frozen_members.tsv"))
    assert res.returncode == 0, res.stderr

    current_ids = (out_dir / "consolidated_consensus_ids.txt").read_text(encoding="utf-8")
    assert current_ids == prev_ids


def test_frozen_members_empty_warns_and_prevents_new_consolidation(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    uuids = [f"read-{i:02d}" for i in range(1, 11)]
    blast_lines = "".join(
        f"{_make_header(u, True)}\tOTUB_1-COI\tMetazoa\tCOI\n" for u in uuids
    )
    fasta_lines = "".join(f">{_make_header(u, False)}\nACGTACGTACGT\n" for u in uuids)
    qscore_lines = "".join(f"{u}\thac\t30\n" for u in uuids)
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"

    res = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert res.returncode == 0, res.stderr
    assert "frozen_members source missing or empty" in res.stderr

    status = _read_status(tmp_path / "Consensus" / "consolidated_ids_status.tsv")
    assert int(status.get("emitted_consensus_count", "0")) > 0
    assert status.get("kept_previous_ids") == "0"
    assert status.get("reason") == "emitted_no_prev"

    cons_ids = tmp_path / "Consensus" / "consolidated_consensus_ids.txt"
    assert not cons_ids.exists() or cons_ids.read_text(encoding="utf-8") == ""


def test_emitted_round_without_new_consolidations_keeps_previous_ids_and_status(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    uuids = [f"read-{i:02d}" for i in range(1, 11)]
    blast_lines = "".join(
        f"{_make_header(u, True)}\tOTUB_1-COI\tMetazoa\tCOI\n" for u in uuids
    )
    fasta_lines = "".join(f">{_make_header(u, False)}\nACGTACGTACGT\n" for u in uuids)
    qscore_lines = "".join(f"{u}\thac\t30\n" for u in uuids)
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    out_dir = tmp_path / "Consensus"
    out_dir.mkdir(parents=True, exist_ok=True)
    prev_ids = (
        ">no_adapter|OTUB_9|COI|reads-3|OTU=OTUB_9-COI-no_adapter_1|minQ=20|frozen=1|consolidated=1\n"
        "OTUB_9_no_adapter\n"
    )
    (out_dir / "consolidated_consensus_ids.txt").write_text(prev_ids, encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"

    res = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert res.returncode == 0, res.stderr

    status = _read_status(tmp_path / "Consensus" / "consolidated_ids_status.tsv")
    assert int(status.get("emitted_consensus_count", "0")) > 0
    assert status.get("kept_previous_ids") == "1"
    assert status.get("reason") == "emitted_keep_prev"

    current_ids = (out_dir / "consolidated_consensus_ids.txt").read_text(encoding="utf-8")
    assert current_ids == prev_ids


def test_drop_filter_corrects_status_when_ids_emptied(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    hdr = _make_header("read-1", True)
    _write_common_inputs(
        tmp_path,
        f"{hdr}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{_make_header('read-1', False)}\nACGT\n",
        "read-1\thac\t30\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    out_dir = tmp_path / "Consensus"
    out_dir.mkdir(parents=True, exist_ok=True)
    prev_ids = (
        ">no_adapter|OTUB_9|COI|reads-3|OTU=OTUB_9-COI-no_adapter_1|minQ=20|frozen=1|consolidated=1\n"
        "OTUB_9_no_adapter\n"
    )
    (out_dir / "consolidated_consensus_ids.txt").write_text(prev_ids, encoding="utf-8")
    lock_prev = tmp_path / "prev_keys.tsv"
    lock_prev.write_text("no_adapter\tOTUB_9-COI-no_adapter_1\n", encoding="utf-8")
    lock_state = out_dir / ".cache" / "no_adapter" / "lock_state.tsv"
    lock_state.parent.mkdir(parents=True, exist_ok=True)
    lock_state.write_text("OTUB_9-COI-no_adapter_1\t1\t1\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_LOCK_KEYS_PREV"] = str(lock_prev)
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"

    res = _run_consensus(tmp_path, env, min_reads="5", frozen_members=str(frozen))
    assert res.returncode == 0, res.stderr
    assert "locked but cache missing during carry-forward" in res.stderr

    status = _read_status(tmp_path / "Consensus" / "consolidated_ids_status.tsv")
    assert status.get("emitted_consensus_count") == "0"
    assert status.get("kept_previous_ids") == "0"
    assert status.get("reason") == "no_emission_no_prev"

    current_ids = (out_dir / "consolidated_consensus_ids.txt").read_text(encoding="utf-8")
    assert current_ids == ""


def test_frozen_members_missing_path_warns_and_prevents_new_consolidation(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    uuids = [f"read-{i:02d}" for i in range(1, 11)]
    blast_lines = "".join(
        f"{_make_header(u, True)}\tOTUB_1-COI\tMetazoa\tCOI\n" for u in uuids
    )
    fasta_lines = "".join(f">{_make_header(u, False)}\nACGTACGTACGT\n" for u in uuids)
    qscore_lines = "".join(f"{u}\thac\t30\n" for u in uuids)
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines)
    missing_frozen = tmp_path / "missing_otu_frozen_members.tsv"

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"

    res = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(missing_frozen))
    assert res.returncode == 0, res.stderr
    assert "frozen_members source missing or empty" in res.stderr

    status = _read_status(tmp_path / "Consensus" / "consolidated_ids_status.tsv")
    assert int(status.get("emitted_consensus_count", "0")) > 0
    assert status.get("kept_previous_ids") == "0"
    assert status.get("reason") == "emitted_no_prev"

    cons_ids = tmp_path / "Consensus" / "consolidated_consensus_ids.txt"
    assert not cons_ids.exists() or cons_ids.read_text(encoding="utf-8") == ""


def test_drop_filter_keys_removes_global_one_column_rows(tmp_path: Path) -> None:
    drop_file = tmp_path / "drop.tsv"
    keys_in = tmp_path / "keys.tsv"
    keys_out = tmp_path / "keys.out.tsv"

    drop_file.write_text("OTUB_2-COI-no_adapter_1\n", encoding="utf-8")
    keys_in.write_text(
        "OTUB_2-COI-no_adapter_1\n"
        "OTUB_9-COI-no_adapter_1\n"
        "no_adapter\tOTUB_2-COI-no_adapter_1\n",
        encoding="utf-8",
    )

    cp = _run_drop_filter("keys", drop_file, keys_in)
    keys_out.write_text(cp.stdout, encoding="utf-8")
    assert cp.returncode == 0

    kept = keys_out.read_text(encoding="utf-8")
    assert "OTUB_2-COI-no_adapter_1\n" not in kept
    assert "OTUB_9-COI-no_adapter_1\n" in kept
    assert "no_adapter\tOTUB_2-COI-no_adapter_1" not in kept


def test_drop_filter_ids_headerless_uses_exact_fallback_patterns(tmp_path: Path) -> None:
    drop_file = tmp_path / "drop.tsv"
    ids_in = tmp_path / "ids.txt"
    ids_out = tmp_path / "ids.out.txt"

    drop_file.write_text("no_adapter\tOTUB_2-COI-no_adapter_1\n", encoding="utf-8")
    ids_in.write_text(
        "OTUB_2-COI-no_adapter_1\n"
        "OTUB_2_no_adapter\n"
        "OTUB_2-COI-no_adapter_1_no_adapter\n"
        "OTUB_9_no_adapter\n",
        encoding="utf-8",
    )

    cp = _run_drop_filter("ids", drop_file, ids_in)
    ids_out.write_text(cp.stdout, encoding="utf-8")
    assert cp.returncode == 0

    kept = ids_out.read_text(encoding="utf-8")
    assert "OTUB_2-COI-no_adapter_1\n" not in kept
    assert "OTUB_2_no_adapter\n" not in kept
    assert "OTUB_2-COI-no_adapter_1_no_adapter\n" not in kept
    assert "OTUB_9_no_adapter\n" in kept


def test_drop_filter_ids_strict_mode_does_not_normalize_sample_suffix(tmp_path: Path) -> None:
    drop_file = tmp_path / "drop.tsv"
    ids_in = tmp_path / "ids.txt"
    ids_out = tmp_path / "ids.out.txt"

    drop_file.write_text("sample\tOTUB_2-COI-sample_2024\n", encoding="utf-8")
    ids_in.write_text(
        ">sample_2024|OTUB_2|COI|reads-5|OTU=OTUB_2-COI-sample_2024|minQ=25|frozen=1|consolidated=1\n"
        "OTUB_2_sample_2024\n",
        encoding="utf-8",
    )

    cp = _run_drop_filter("ids", drop_file, ids_in)
    ids_out.write_text(cp.stdout, encoding="utf-8")
    assert cp.returncode == 0
    assert ids_out.read_text(encoding="utf-8") == ids_in.read_text(encoding="utf-8")


def test_drop_filter_ids_mixed_header_file_also_drops_orphan_fallback_ids(tmp_path: Path) -> None:
    drop_file = tmp_path / "drop.tsv"
    ids_in = tmp_path / "ids.txt"
    ids_out = tmp_path / "ids.out.txt"

    drop_file.write_text("no_adapter\tOTUB_2-COI-no_adapter_1\n", encoding="utf-8")
    ids_in.write_text(
        ">no_adapter|OTUB_9|COI|reads-3|OTU=OTUB_9-COI-no_adapter_1|minQ=20|frozen=1|consolidated=1\n"
        "OTUB_2_no_adapter\n"
        "OTUB_9_no_adapter\n",
        encoding="utf-8",
    )

    cp = _run_drop_filter("ids", drop_file, ids_in)
    ids_out.write_text(cp.stdout, encoding="utf-8")
    assert cp.returncode == 0

    kept = ids_out.read_text(encoding="utf-8")
    assert "OTUB_2_no_adapter\n" not in kept
    assert "OTUB_9_no_adapter\n" in kept
    assert ">no_adapter|OTUB_9|COI|" in kept


def test_drop_filter_fails_when_drop_file_is_not_first_input(tmp_path: Path) -> None:
    drop_file = tmp_path / "drop.tsv"
    ids_file = tmp_path / "ids.txt"
    out_file = tmp_path / "ids.out.txt"

    drop_file.write_text("no_adapter\tOTUB_2-COI-no_adapter_1\n", encoding="utf-8")
    ids_file.write_text("OTUB_2_no_adapter\nOTUB_9_no_adapter\n", encoding="utf-8")

    cmd = [
        "awk",
        "-v",
        "MODE=ids",
        "-v",
        f"DROP={drop_file}",
        "-f",
        str(DROP_FILTER_AWK),
        str(ids_file),   # wrong order on purpose
        str(drop_file),
        str(ids_file),
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    assert cp.returncode == 2
    assert "DROP input must be the first file argument" in cp.stderr


def test_drop_filter_ids_fallback_ignores_non_id_like_lines(tmp_path: Path) -> None:
    drop_file = tmp_path / "drop.tsv"
    ids_in = tmp_path / "ids.txt"
    ids_out = tmp_path / "ids.out.txt"

    drop_file.write_text("no_adapter\tOTUB_2-COI-no_adapter_1\n", encoding="utf-8")
    ids_in.write_text(
        "# OTUB_2_no_adapter\n"
        "meta=OTUB_2_no_adapter\n"
        "OTUB_2_no_adapter\n",
        encoding="utf-8",
    )

    cp = _run_drop_filter("ids", drop_file, ids_in)
    ids_out.write_text(cp.stdout, encoding="utf-8")
    assert cp.returncode == 0

    kept_lines = ids_out.read_text(encoding="utf-8").splitlines()
    assert "# OTUB_2_no_adapter" in kept_lines
    assert "meta=OTUB_2_no_adapter" in kept_lines
    assert "OTUB_2_no_adapter" not in kept_lines


def test_drop_filter_ids_global_drop_strict_keeps_suffix_ids(tmp_path: Path) -> None:
    drop_file = tmp_path / "drop.tsv"
    ids_in = tmp_path / "ids.txt"
    ids_out = tmp_path / "ids.out.txt"

    drop_file.write_text("OTUB_2-COI-no_adapter_1\n", encoding="utf-8")
    ids_in.write_text(
        "OTUB_2-COI-no_adapter_1\n"
        "OTUB_2_no_adapter\n",
        encoding="utf-8",
    )

    cp = _run_drop_filter("ids", drop_file, ids_in, global_suffix_mode="strict")
    ids_out.write_text(cp.stdout, encoding="utf-8")
    assert cp.returncode == 0

    kept_lines = ids_out.read_text(encoding="utf-8").splitlines()
    assert "OTUB_2-COI-no_adapter_1" not in kept_lines
    assert "OTUB_2_no_adapter" in kept_lines


def test_drop_filter_ids_global_drop_heuristic_drops_suffix_ids(tmp_path: Path) -> None:
    drop_file = tmp_path / "drop.tsv"
    ids_in = tmp_path / "ids.txt"
    ids_out = tmp_path / "ids.out.txt"

    drop_file.write_text("OTUB_2-COI-no_adapter_1\n", encoding="utf-8")
    ids_in.write_text(
        "OTUB_2-COI-no_adapter_1\n"
        "OTUB_2_no_adapter\n"
        "OTUB_9_no_adapter\n",
        encoding="utf-8",
    )

    cp = _run_drop_filter("ids", drop_file, ids_in, global_suffix_mode="heuristic")
    ids_out.write_text(cp.stdout, encoding="utf-8")
    assert cp.returncode == 0

    kept_lines = ids_out.read_text(encoding="utf-8").splitlines()
    assert "OTUB_2-COI-no_adapter_1" not in kept_lines
    assert "OTUB_2_no_adapter" not in kept_lines
    assert "OTUB_9_no_adapter" in kept_lines


def test_locked_otu_revalidation_forces_recompute_when_due(tmp_path: Path) -> None:
    def setup_case(case_dir: Path) -> tuple[Path, Path]:
        case_dir.mkdir(parents=True, exist_ok=True)
        bindir = _install_stub_tools(case_dir)
        hdr_otub = _make_header("read-1", True)
        hdr_norm = _make_header("read-1", False, model="hac")
        _write_common_inputs(
            case_dir,
            f"{hdr_otub}\tOTUB_1-COI\tMetazoa\tCOI\n",
            f">{hdr_norm}\nACGTACGT\n",
            "read-1\thac\t30\n",
        )
        (case_dir / "otu_frozen_members.tsv").write_text("", encoding="utf-8")
        cache_dir = case_dir / "Consensus" / ".cache" / "no_adapter"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_cons = cache_dir / "OTUB_1-COI-no_adapter_1.consensus.fasta"
        cache_meta = cache_dir / "OTUB_1-COI-no_adapter_1.meta"
        cache_cons.write_text(
            ">no_adapter|OTUB_1|COI|reads-1|OTU=OTUB_1-COI-no_adapter_1|minQ=30|frozen=0\nACGTACGT\n",
            encoding="utf-8",
        )
        cache_meta.write_text("1\tdeadbeefdeadbeefdeadbeefdeadbeef\n", encoding="utf-8")
        lock_prev = case_dir / "prev_otu_consolidated_keys.tsv"
        lock_prev.write_text("no_adapter\tOTUB_1-COI-no_adapter_1\n", encoding="utf-8")
        return bindir, lock_prev

    # No revalidation due -> locked cache is reused.
    case_keep = tmp_path / "keep"
    bindir_keep, lock_prev_keep = setup_case(case_keep)
    env_keep = os.environ.copy()
    env_keep["PATH"] = f"{bindir_keep}:{env_keep.get('PATH', '')}"
    env_keep["CONSENSUS_LOCK_ENABLED"] = "1"
    env_keep["CONSENSUS_LOCK_KEYS_PREV"] = str(lock_prev_keep)
    env_keep["CONSENSUS_LOCK_REVALIDATE_EVERY_ROUNDS"] = "0"
    env_keep["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env_keep["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    res_keep = _run_consensus(case_keep, env_keep, min_reads="1", frozen_members=str(case_keep / "otu_frozen_members.tsv"))
    assert res_keep.returncode == 0, res_keep.stderr
    assert (case_keep / "Consensus" / ".cache" / "no_adapter" / "OTUB_1-COI-no_adapter_1.consensus.fasta").exists()

    # Revalidation due every round -> locked OTU is recomputed (cache not blindly reused).
    case_reval = tmp_path / "reval"
    bindir_reval, lock_prev_reval = setup_case(case_reval)
    env_reval = os.environ.copy()
    env_reval["PATH"] = f"{bindir_reval}:{env_reval.get('PATH', '')}"
    env_reval["CONSENSUS_LOCK_ENABLED"] = "1"
    env_reval["CONSENSUS_LOCK_KEYS_PREV"] = str(lock_prev_reval)
    env_reval["CONSENSUS_LOCK_REVALIDATE_EVERY_ROUNDS"] = "1"
    env_reval["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env_reval["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    env_reval["CONSENSUS_DEBUG"] = "1"
    res_reval = _run_consensus(case_reval, env_reval, min_reads="1", frozen_members=str(case_reval / "otu_frozen_members.tsv"))
    assert res_reval.returncode == 0, res_reval.stderr
    assert not (case_reval / "Consensus" / ".cache" / "no_adapter" / "OTUB_1-COI-no_adapter_1.consensus.fasta").exists()
    debug_log = (case_reval / "Consensus" / "consensus_debug.log").read_text(encoding="utf-8")
    assert "locked_cache_revalidation_triggered" in debug_log


def test_selector_worker_failure_fails_consensus_round(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path)
    _write_common_inputs(
        tmp_path,
        f"{_make_header('read-1', True)}\tOTUB_1-COI\tMetazoa\tCOI\n",
        f">{_make_header('read-1', False, model='hac')}\nACGT\n",
        "read-1\thac\t30\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")

    script_override = tmp_path / "bin_override"
    (script_override / "lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "bin" / "Consensus_simple.R", script_override / "Consensus_simple.R")
    shutil.copy2(REPO_ROOT / "bin" / "consensus_drop_filter.awk", script_override / "consensus_drop_filter.awk")
    shutil.copy2(REPO_ROOT / "bin" / "filter_blast_rows_by_adapter_class.sh", script_override / "filter_blast_rows_by_adapter_class.sh")
    shutil.copy2(REPO_ROOT / "bin" / "lib" / "adapter_utils.sh", script_override / "lib" / "adapter_utils.sh")
    _write_exec(
        script_override / "consensus_select_reads_by_rank.pl",
        "#!/usr/bin/env perl\n"
        "use strict;\n"
        "use warnings;\n"
        "exit 7;\n",
    )
    for p in script_override.rglob("*"):
        if p.is_file():
            p.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "warn"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"

    result = _run_consensus(
        tmp_path,
        env,
        min_reads="1",
        frozen_members=str(frozen),
        script_path=script_override,
    )
    assert result.returncode != 0
    assert "selector worker failed" in result.stderr
    # A5: enriched log must carry sample= and exit= context fields
    assert "sample=" in result.stderr
    assert "exit=" in result.stderr


def test_validate_phase1_workload_fails_on_missing_sel_prefix(tmp_path: Path) -> None:
    workload = tmp_path / "bad_workload.tsv"
    workload.write_text("otu-1\t1\t0\t0\t0\tfallback\t2\t0\tNA\tfallback\t\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(VALIDATE_PHASE1_WORKLOAD), str(workload)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "malformed phase1 workload row" in result.stderr


def test_validate_phase1_workload_fails_on_unknown_mode(tmp_path: Path) -> None:
    workload = tmp_path / "bad_workload.tsv"
    workload.write_text(
        "otu-1\t1\t0\t0\t0\tnone\t2\t0\tNA\tunknown_mode\tConsensus/no_adapter/otu-1_selection\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(VALIDATE_PHASE1_WORKLOAD), str(workload)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "invalid_phase1_mode" in result.stderr


def test_validate_phase1_workload_fails_on_invalid_uint_field(tmp_path: Path) -> None:
    workload = tmp_path / "bad_workload.tsv"
    workload.write_text(
        "otu-1\tX\t0\t0\t0\tfallback\t2\t0\tNA\tfallback\tConsensus/no_adapter/otu-1_selection\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(VALIDATE_PHASE1_WORKLOAD), str(workload)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "invalid_prev_stable_count" in result.stderr
