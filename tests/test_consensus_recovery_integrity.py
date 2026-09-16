import hashlib
import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "Consensus_simple.sh"
VOUCHER_EXPORT = REPO_ROOT / "bin" / "voucher_export.sh"
VALIDATE_PHASE1_WORKLOAD = REPO_ROOT / "bin" / "lib" / "validate_phase1_workload.sh"
SYNC_SCRIPT = REPO_ROOT / "bin" / "sync_dir_atomic.sh"
DROP_FILTER_AWK = REPO_ROOT / "bin" / "consensus_drop_filter.awk"
EMIT_PROVENANCE = REPO_ROOT / "bin" / "emit_consensus_round_provenance.pl"
F01A_PRIVATE_TAG = "RTBIOSCAN_INTERNAL_OTU"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _install_stub_tools(
    tmp_path: Path,
    *,
    include_seqtk: bool = True,
    include_vsearch: bool = True,
    emit_consensus: bool = False,
) -> Path:
    bindir = tmp_path / "stubbin"
    bindir.mkdir(parents=True, exist_ok=True)

    _write_exec(
        bindir / "seqkit",
        "#!/bin/bash\n"
        "exit 0\n",
    )
    rscript_body = "#!/bin/bash\nif [ \"$1\" = \"-e\" ]; then exit 0; fi\nexit 0\n"
    if emit_consensus:
        rscript_body = (
            "#!/bin/bash\n"
            "if [ \"$1\" = \"-e\" ]; then exit 0; fi\n"
            "sample=$2\n"
            "cd \"Consensus/$sample\"\n"
            ": > \"${sample}_consensus.fasta\"\n"
            "for input in *_reads_sup.fasta; do\n"
            "  [ -f \"$input\" ] || continue\n"
            "  otu=${input%_reads_sup.fasta}\n"
            "  otu_header=$(printf '%s' \"$otu\" | tr '-' '|')\n"
            "  reads=$(awk '/^>/{n++} END{print n+0}' \"$input\")\n"
            "  sequence=$(awk '!/^>/{printf \"%s\", $0} END{print \"\"}' \"$input\")\n"
            "  printf '>%s|%s|reads-%s\\n%s\\n' \"$sample\" \"$otu_header\" \"$reads\" \"$sequence\" > \"${otu}_consensus.fasta\"\n"
            "  cat \"${otu}_consensus.fasta\" >> \"${sample}_consensus.fasta\"\n"
            "done\n"
        )
    _write_exec(bindir / "Rscript", rscript_body)
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
            "if [ \"${RTBIOSCAN_TEST_VSEARCH_ONE_PER_RECORD:-0}\" = \"1\" ]; then\n"
            "  awk -v out=\"$out\" 'BEGIN{RS=\">\"; ORS=\"\"} NR>1{print \">\" $0 > (out (NR-2)); close(out (NR-2))}' \"$in\"\n"
            "elif [ \"${RTBIOSCAN_TEST_VSEARCH_MIX_ALL:-0}\" = \"1\" ]; then\n"
            "  cp \"$in\" \"${out}0\"\n"
            "else\n"
            "  awk -v out=\"$out\" 'BEGIN{RS=\">\"; ORS=\"\"} NR>1{record=$0; seq=record; sub(/^[^\\n]*\\n/,\"\",seq); key=seq; gsub(/[\\r\\n]/,\"\",key); if(!(key in cluster)) cluster[key]=count++; file=out cluster[key]; print \">\" record >> file; close(file)}' \"$in\"\n"
            "fi\n"
            "if [ \"${RTBIOSCAN_TEST_VSEARCH_REVERSE:-0}\" = \"1\" ]; then\n"
            "  for cluster in \"${out}\"*; do\n"
            "    [ -f \"$cluster\" ] || continue\n"
            "    awk 'BEGIN{RS=\">\"; ORS=\"\"} NR>1{record[++n]=$0} END{for(i=n;i>0;i--) print \">\" record[i]}' \"$cluster\" > \"${cluster}.reverse\"\n"
            "    mv \"${cluster}.reverse\" \"$cluster\"\n"
            "  done\n"
            "fi\n",
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
    samples: str = "no_adapter\n",
) -> None:
    (tmp_path / "samples.txt").write_text(samples, encoding="utf-8")
    (tmp_path / "blast_report_annotated.txt").write_text(blast_lines, encoding="utf-8")
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text(fasta_lines, encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text(qscore_lines, encoding="utf-8")


def _run_consensus(
    tmp_path: Path,
    env: dict,
    min_reads: str = "1",
    frozen_members: str = "",
    script_path: Path | None = None,
    supply_target_contract: bool = True,
    reads_mode: str = "representative",
    consensus_script: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if script_path is None:
        script_path = REPO_ROOT / "bin"
    if supply_target_contract:
        env.setdefault("RTBIOSCAN_TARGET_TOKENS", "COI|ITS2")
        env.setdefault("RTBIOSCAN_TARGET_TAXA", "Metazoa|Viridiplantae")
    if consensus_script is None:
        consensus_script = SCRIPT
    cmd = [
        "bash",
        str(consensus_script),
        str(script_path),
        "98",
        min_reads,
        "50",
        "15",
        "20",
        frozen_members,
        reads_mode,
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
    adapter: str = "no_adapter_1",
    include_barcode: bool = True,
    otu_token: str = "OTUB_1-COI",
) -> str:
    parts = [uuid, target, model]
    if include_barcode:
        parts.append(f"barcode={barcode}")
    parts.append(f"adapter={adapter}")
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


def _run_taxonomy_fixture(
    tmp_path: Path,
    blast_lines: str,
    fasta_lines: str,
    qscore_lines: str,
    mode=None,
    min_reads: str = "1",
    samples: str = "no_adapter\n",
    emit_consensus: bool = False,
    round_id: str | None = None,
    target_tokens: str = "COI|ITS2",
    target_taxa: str = "Metazoa|Viridiplantae",
    frozen_members_text: str = "",
    extra_env: dict[str, str] | None = None,
    consensus_script: Path | None = None,
):
    bindir = _install_stub_tools(tmp_path, emit_consensus=emit_consensus)
    _write_common_inputs(tmp_path, blast_lines, fasta_lines, qscore_lines, samples=samples)
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text(frozen_members_text, encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["RTBIOSCAN_TARGET_TOKENS"] = target_tokens
    env["RTBIOSCAN_TARGET_TAXA"] = target_taxa
    if mode is not None:
        env["CONSENSUS_TAXONOMY_MODE"] = mode
    if round_id is not None:
        env["CONSENSUS_ROUND_ID"] = round_id
    if extra_env:
        env.update(extra_env)
    return _run_consensus(
        tmp_path,
        env,
        min_reads=min_reads,
        frozen_members=str(frozen),
        consensus_script=consensus_script,
    )


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


def _consensus_non_timing_snapshot(
    tmp_path: Path,
    *,
    exclude_admission_sidecar: bool = False,
) -> list[tuple[str, str]]:
    consensus = tmp_path / "Consensus"
    rows = []
    for path in sorted(candidate for candidate in consensus.rglob("*") if candidate.is_file()):
        relative = path.relative_to(consensus)
        if "timing" in path.name or path.name == "consensus_cache_hydration_stats.tsv":
            continue
        if exclude_admission_sidecar and relative == Path("consensus_taxonomy_admission.tsv"):
            continue
        rows.append((str(relative), hashlib.sha256(path.read_bytes()).hexdigest()))
    return rows


def _without_taxonomy_admission_sidecar(tmp_path: Path) -> Path:
    control_bin = tmp_path / "control_bin"
    control_bin.mkdir()
    shutil.copytree(REPO_ROOT / "bin" / "lib", control_bin / "lib")
    shutil.copy2(
        REPO_ROOT / "bin" / "filter_blast_rows_by_adapter_class.sh",
        control_bin / "filter_blast_rows_by_adapter_class.sh",
    )
    shutil.copy2(
        REPO_ROOT / "bin" / "partition_blast_rows_by_adapter_class.sh",
        control_bin / "partition_blast_rows_by_adapter_class.sh",
    )
    output = control_bin / "Consensus_simple.sh"
    lines = SCRIPT.read_text(encoding="utf-8").splitlines(keepends=True)
    stripped = []
    skipping = False
    for line in lines:
        if "RTBIOSCAN_TAXONOMY_ADMISSION_BEGIN" in line:
            assert not skipping
            skipping = True
            continue
        if "RTBIOSCAN_TAXONOMY_ADMISSION_END" in line:
            assert skipping
            skipping = False
            continue
        if not skipping:
            stripped.append(line)
    assert not skipping
    output.write_text("".join(stripped), encoding="utf-8")
    output.chmod(0o755)
    return output


def _taxonomy_fixture_text(
    specs: list[tuple[str, str, str, list[str], int]],
    *,
    sample: str = "sample_A",
    barcode: str = "",
    read_prefix: str = "admission",
) -> tuple[str, str, str, str]:
    blast_lines = []
    fasta_lines = []
    qscore_lines = []
    first_fasta_header = ""
    read_number = 0
    for otu, kingdom, marker, sequences, qscore in specs:
        for sequence in sequences:
            read_number += 1
            read_id = f"{read_prefix}-{read_number:02d}"
            blast_header = _make_header(
                read_id,
                True,
                target=marker,
                barcode=barcode,
                adapter=sample,
                otu_token=otu,
            )
            fasta_header = _make_header(
                read_id,
                False,
                model="hac",
                target=marker,
                barcode=barcode,
                adapter=sample,
            )
            if not first_fasta_header:
                first_fasta_header = fasta_header
            blast_lines.append(f"{blast_header}\t{otu}\t{kingdom}\t{marker}\n")
            fasta_lines.append(f">{fasta_header}\n{sequence}\n")
            qscore_lines.append(f"{read_id}\thac\t{qscore}\n")
    return "".join(blast_lines), "".join(fasta_lines), "".join(qscore_lines), first_fasta_header


def _f01a_sequence(index: int) -> str:
    alphabet = "ACGT"
    value = index
    encoded = []
    for _ in range(8):
        encoded.append(alphabet[value % 4])
        value //= 4
    return "ACGT" + "".join(encoded)


def _run_f01a_case(
    tmp_path: Path,
    *,
    mode: str,
    otu_specs: list[tuple[str, int, int, bool]],
    r_path: bool,
    mix_all: bool = False,
    taxonomy_mode: str = "required",
    rank1_otus: set[str] | None = None,
    primer: str = "COI_Probe",
    reverse_cluster: bool = False,
    hash_seed: int | None = None,
    debug: bool = False,
    reads_mode: str = "representative",
) -> tuple[subprocess.CompletedProcess[str], str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    bindir = _install_stub_tools(tmp_path, emit_consensus=True)
    if mode == "collapse":
        sample, barcode, adapter = "Larch", "", "Larch_COI"
    elif mode == "track":
        sample, barcode, adapter = "Sable_2_COI", "", "Sable_2_COI"
    elif mode == "primers":
        sample = barcode = adapter = primer
    else:
        raise AssertionError(f"unsupported F-01A mode: {mode}")

    blast_lines = []
    fasta_lines = []
    qscore_lines = []
    frozen_lines = []
    for otu_index, (otu, count, qscore, frozen) in enumerate(otu_specs, start=1):
        for read_index in range(1, count + 1):
            uuid = f"f01a-{otu_index:02d}-{read_index:02d}"
            blast_header = _make_header(
                uuid,
                True,
                barcode=barcode,
                adapter=adapter,
                otu_token=otu,
            )
            fasta_header = _make_header(
                uuid,
                False,
                model="hac",
                barcode=barcode,
                adapter=adapter,
            )
            use_rank1 = not r_path or (rank1_otus is not None and otu in rank1_otus)
            sequence = "ACGTACGTACGT" if use_rank1 else _f01a_sequence(read_index)
            blast_lines.append(f"{blast_header}\t{otu}\tMetazoa\tCOI\n")
            fasta_lines.append(f">{fasta_header}\n{sequence}\n")
            qscore_lines.append(f"{uuid}\thac\t{qscore}\n")
            if frozen and read_index == 1:
                frozen_lines.append(f"FROZEN_{otu_index}\t{fasta_header}\t1\n")

    _write_common_inputs(
        tmp_path,
        "".join(blast_lines),
        "".join(fasta_lines),
        "".join(qscore_lines),
        samples=f"{sample}\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("".join(frozen_lines), encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    env["CONSENSUS_LOCK_ENABLED"] = "0"
    env["CONSENSUS_PRUNE_FROZEN_POLICY"] = "never"
    env["CONSENSUS_TAXONOMY_MODE"] = taxonomy_mode
    if mode == "track":
        track_active_units = tmp_path / "track_active_units.txt"
        track_active_units.write_text(f"{sample}\n", encoding="utf-8")
        track_identity = tmp_path / "track_identity.tsv"
        track_identity.write_text(
            "sample_id\tmarker_id\tunit_id_collapse\tunit_id_track\n"
            f"voucher_track\tCOI\tSable_2\t{sample}\n",
            encoding="utf-8",
        )
        env["RTBIOSCAN_EFFECTIVE_IDENTITY_MODE"] = "track"
        env["RTBIOSCAN_TRACK_ACTIVE_UNITS"] = str(track_active_units)
        env["RTBIOSCAN_TRACK_IDENTITY_TSV"] = str(track_identity)
    if mix_all:
        env["RTBIOSCAN_TEST_VSEARCH_MIX_ALL"] = "1"
    if reverse_cluster:
        env["RTBIOSCAN_TEST_VSEARCH_REVERSE"] = "1"
    if hash_seed is not None:
        env["PERL_HASH_SEED"] = str(hash_seed)
        env["PERL_PERTURB_KEYS"] = "2"
    if debug:
        env["CONSENSUS_DEBUG"] = "1"
    result = _run_consensus(
        tmp_path,
        env,
        min_reads="1",
        frozen_members=str(frozen),
        reads_mode=reads_mode,
    )
    return result, sample


def _f01a_meta(tmp_path: Path, sample: str) -> dict[str, list[str]]:
    rows = {}
    path = tmp_path / "Consensus" / sample / "otu_meta.tsv"
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        rows[fields[0]] = fields
    return rows


def _f01a_public_artifacts(tmp_path: Path, sample: str) -> list[Path]:
    paths = [
        tmp_path / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta",
        tmp_path / "Consensus" / "consensus_otu_map.tsv",
        tmp_path / "Consensus" / "consolidated_consensus_ids.txt",
    ]
    return [path for path in paths if path.exists()]


def _f01a_reads_for_otu(tmp_path: Path, sample: str, otu: str) -> list[str]:
    merged = tmp_path / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta"
    for line in merged.read_text(encoding="utf-8").splitlines():
        if line.startswith(">") and f"|OTU={otu}|" in line:
            consensus_token = line[1:].split("|")[1]
            reads_path = tmp_path / "Consensus" / sample / "OriginalReads" / f"{consensus_token}_reads.list"
            if not reads_path.exists():
                return []
            return [read for read in reads_path.read_text(encoding="utf-8").splitlines() if read]
    raise AssertionError(f"missing final consensus for {otu}")


def _plant_f01a_stale_linkage(
    tmp_path: Path,
    sample: str,
    *,
    consensus_tokens: tuple[str, ...],
    internal_key: str | None = None,
) -> None:
    sample_dir = tmp_path / "Consensus" / sample
    original_reads = sample_dir / "OriginalReads"
    original_reads.mkdir(parents=True, exist_ok=True)
    for token in consensus_tokens:
        stale_id = f"stale-{token}"
        (original_reads / f"{token}_reads.list").write_text(
            f"{stale_id}|COI|hac\n",
            encoding="utf-8",
        )
        (original_reads / f"{token}_reads_sup.fasta").write_text(
            f">{stale_id}|COI|hac\nAAAA\n",
            encoding="utf-8",
        )
    if internal_key is not None:
        (sample_dir / f"{internal_key}_all_reads.list").write_text(
            "stale-per-key|COI|hac\n",
            encoding="utf-8",
        )


def test_f01a_full_demux_modes_keep_unsuffixed_public_otu_identity(tmp_path: Path) -> None:
    for mode in ("collapse", "track"):
        case_dir = tmp_path / mode
        result, sample = _run_f01a_case(
            case_dir,
            mode=mode,
            otu_specs=[("OTUB_7-COI", 10, 31, False)],
            r_path=False,
        )
        assert result.returncode == 0, result.stderr
        meta = _f01a_meta(case_dir, sample)
        assert meta["OTUB_7-COI"][2:] == ["10", "31", "0", "0"]
        merged = (case_dir / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta").read_text(
            encoding="utf-8"
        )
        assert "|OTU=OTUB_7-COI|n=10|minQ=31|frozen=0|consolidated=0" in merged
        assert "OTU=OTUB_7-COI-" not in merged
        assert F01A_PRIVATE_TAG not in merged


def test_f01a_reads_like_primer_text_cannot_override_count_priority(tmp_path: Path) -> None:
    stable_outputs = {}
    for primer in ("COI_reads-7", "COI_Probe-reads-99"):
        for reverse_cluster in (False, True):
            for hash_seed in (0, 7):
                case_dir = tmp_path / primer / f"reverse-{int(reverse_cluster)}" / f"seed-{hash_seed}"
                result, sample = _run_f01a_case(
                    case_dir,
                    mode="primers",
                    otu_specs=[
                        ("OTUB_7-COI", 12, 31, False),
                        ("OTUB_9-COI", 10, 40, False),
                    ],
                    r_path=True,
                    mix_all=True,
                    rank1_otus={"OTUB_7-COI"},
                    primer=primer,
                    reverse_cluster=reverse_cluster,
                    hash_seed=hash_seed,
                )
                assert result.returncode == 0, result.stderr

                meta = _f01a_meta(case_dir, sample)
                assert meta[f"OTUB_7-COI-{primer}"][2:4] == ["12", "31"]
                assert meta[f"OTUB_9-COI-{primer}"][2:4] == ["10", "40"]

                merged_path = case_dir / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta"
                merged = merged_path.read_text(encoding="utf-8")
                header = merged.splitlines()[0]
                fields = header[1:].split("|")
                otu_field = fields.index("OTU=OTUB_7-COI")
                assert fields[otu_field - 1] == "reads-12"
                assert fields[0] == primer
                assert header.startswith(
                    f">{primer}|Consensus0|COI|{primer.replace('-', '|')}|reads-12|"
                )
                assert "|OTU=OTUB_9-COI|" not in header
                assert F01A_PRIVATE_TAG not in header

                linked_reads = _f01a_reads_for_otu(case_dir, sample, "OTUB_7-COI")
                assert len(linked_reads) == 12
                assert all(read.startswith("f01a-01-") for read in linked_reads)
                output_bytes = merged_path.read_bytes()
                if primer in stable_outputs:
                    assert output_bytes == stable_outputs[primer]
                else:
                    stable_outputs[primer] = output_bytes


def test_f01a_cluster_total_unions_rank1_and_r_member_evidence(tmp_path: Path) -> None:
    cases = (
        (
            "rank1-first",
            [("OTUB_7-COI", 12, 31, False), ("OTUB_9-COI", 10, 40, False)],
            {"OTUB_7-COI"},
            False,
            "OTUB_7-COI",
        ),
        (
            "rank1-second-reversed",
            [("OTUB_7-COI", 10, 40, False), ("OTUB_9-COI", 12, 31, False)],
            {"OTUB_9-COI"},
            True,
            "OTUB_9-COI",
        ),
    )
    for case_name, otu_specs, rank1_otus, reverse_cluster, winner in cases:
        case_dir = tmp_path / case_name
        result, sample = _run_f01a_case(
            case_dir,
            mode="primers",
            otu_specs=otu_specs,
            r_path=True,
            mix_all=True,
            rank1_otus=rank1_otus,
            reverse_cluster=reverse_cluster,
            hash_seed=7,
            reads_mode="cluster_total",
        )
        assert result.returncode == 0, result.stderr

        merged = case_dir / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta"
        header = merged.read_text(encoding="utf-8").splitlines()[0]
        fields = header[1:].split("|")
        otu_field = fields.index(f"OTU={winner}")
        assert fields[otu_field - 1] == "reads-22"

        linked_reads = _f01a_reads_for_otu(case_dir, sample, winner)
        assert len(linked_reads) == 22
        assert len(set(linked_reads)) == 22
        assert sum(read.startswith("f01a-01-") for read in linked_reads) == otu_specs[0][1]
        assert sum(read.startswith("f01a-02-") for read in linked_reads) == otu_specs[1][1]

        provenance = case_dir / "consensus_round_provenance.tsv"
        provenance_result = subprocess.run(
            [
                "perl",
                str(EMIT_PROVENANCE),
                "--consensus-dir",
                str(case_dir / "Consensus"),
                "--round-barcode",
                "round_cluster_total",
                "--out",
                str(provenance),
            ],
            capture_output=True,
            text=True,
        )
        assert provenance_result.returncode == 0, provenance_result.stderr
        provenance_rows = _read_tsv_rows(provenance)
        assert len(provenance_rows) == 1
        assert provenance_rows[0]["otu_key"] == winner
        assert provenance_rows[0]["reads_used_round"] == "22"


def test_f01a_primers_rank1_and_r_headers_recover_full_internal_metadata(tmp_path: Path) -> None:
    for path_name, r_path in (("rank1", False), ("r", True)):
        case_dir = tmp_path / path_name
        result, sample = _run_f01a_case(
            case_dir,
            mode="primers",
            otu_specs=[("OTUB_7-COI", 10, 31, False)],
            r_path=r_path,
        )
        assert result.returncode == 0, result.stderr
        meta = _f01a_meta(case_dir, sample)
        assert meta["OTUB_7-COI-COI_Probe"][2:] == ["10", "31", "0", "0"]
        merged = (case_dir / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta").read_text(
            encoding="utf-8"
        )
        assert "|OTU=OTUB_7-COI|n=10|minQ=31|frozen=0|consolidated=0" in merged
        assert "OTU=OTUB_7-COI-COI_Probe" not in merged
        assert F01A_PRIVATE_TAG not in merged
        consensus_map = (case_dir / "Consensus" / "consensus_otu_map.tsv").read_text(
            encoding="utf-8"
        )
        assert "COI_Probe|OTUB_7|COI|COI_Probe|reads-10" in consensus_map
        assert "\tOTUB_7-COI\tCOI_Probe\t10\t31\t0\t0" in consensus_map
        assert F01A_PRIVATE_TAG not in consensus_map
        linked_reads = _f01a_reads_for_otu(case_dir, sample, "OTUB_7-COI")
        assert len(linked_reads) == 10
        assert all(read.startswith("f01a-01-") for read in linked_reads)


def test_f01a_primer_quality_winner_is_order_independent_and_links_reads(tmp_path: Path) -> None:
    cases = (
        (["OTUB_7-COI", "OTUB_9-COI"], [31, 40], "OTUB_9-COI", "f01a-02-"),
        (["OTUB_7-COI", "OTUB_9-COI"], [40, 31], "OTUB_7-COI", "f01a-01-"),
    )
    for case_index, (otus, qscores, winner, winner_read_prefix) in enumerate(cases, start=1):
        case_dir = tmp_path / f"case-{case_index}"
        result, sample = _run_f01a_case(
            case_dir,
            mode="primers",
            otu_specs=[
                (otus[0], 10, qscores[0], False),
                (otus[1], 10, qscores[1], False),
            ],
            r_path=True,
            mix_all=True,
        )
        assert result.returncode == 0, result.stderr
        merged = (case_dir / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta").read_text(
            encoding="utf-8"
        )
        assert f"|OTU={winner}|" in merged
        assert F01A_PRIVATE_TAG not in merged
        read_lists = list((case_dir / "Consensus" / sample / "OriginalReads").glob("*_reads.list"))
        assert len(read_lists) == 1
        linked_reads = [line for line in read_lists[0].read_text(encoding="utf-8").splitlines() if line]
        assert len(linked_reads) == 10
        assert all(line.startswith(winner_read_prefix) for line in linked_reads)


def test_f01a_consolidated_primer_candidate_beats_higher_read_unconsolidated_candidate(
    tmp_path: Path,
) -> None:
    result, sample = _run_f01a_case(
        tmp_path,
        mode="primers",
        otu_specs=[
            ("OTUB_7-COI", 12, 40, False),
            ("OTUB_9-COI", 10, 31, True),
        ],
        r_path=True,
        mix_all=True,
        rank1_otus={"OTUB_7-COI"},
    )
    assert result.returncode == 0, result.stderr
    meta = _f01a_meta(tmp_path, sample)
    assert meta["OTUB_7-COI-COI_Probe"][2:] == ["12", "40", "0", "0"]
    assert meta["OTUB_9-COI-COI_Probe"][2:] == ["10", "31", "1", "1"]
    merged = (tmp_path / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta").read_text(
        encoding="utf-8"
    )
    assert "|OTU=OTUB_9-COI|n=10|minQ=31|frozen=1|consolidated=1" in merged
    read_lists = list((tmp_path / "Consensus" / sample / "OriginalReads").glob("*_reads.list"))
    assert len(read_lists) == 1
    linked_reads = [line for line in read_lists[0].read_text(encoding="utf-8").splitlines() if line]
    assert len(linked_reads) == 10
    assert all(line.startswith("f01a-02-") for line in linked_reads)


def test_f01a_fresh_round_cache_reuse_links_current_evidence(tmp_path: Path) -> None:
    round1 = tmp_path / "round1"
    result1, sample = _run_f01a_case(
        round1,
        mode="primers",
        otu_specs=[("OTUB_7-COI", 10, 31, False)],
        r_path=True,
    )
    assert result1.returncode == 0, result1.stderr
    round1_merged = round1 / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta"
    round1_map = round1 / "Consensus" / "consensus_otu_map.tsv"

    round2 = tmp_path / "round2"
    shutil.copytree(round1 / "Consensus" / ".cache", round2 / "Consensus" / ".cache")
    assert not (round2 / "Consensus" / sample / "OriginalReads").exists()

    result2, _sample = _run_f01a_case(
        round2,
        mode="primers",
        otu_specs=[("OTUB_7-COI", 10, 31, False)],
        r_path=True,
        debug=True,
    )
    assert result2.returncode == 0, result2.stderr
    round2_merged = round2 / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta"
    round2_map = round2 / "Consensus" / "consensus_otu_map.tsv"
    assert round2_merged.read_bytes() == round1_merged.read_bytes()
    assert round2_map.read_bytes() == round1_map.read_bytes()
    assert _f01a_meta(round2, sample)["OTUB_7-COI-COI_Probe"][2:] == ["10", "31", "0", "0"]
    debug_log = (round2 / "Consensus" / "consensus_debug.log").read_text(encoding="utf-8")
    assert "cache_reuse count=10" in debug_log
    linked_reads = _f01a_reads_for_otu(round2, sample, "OTUB_7-COI")
    assert len(linked_reads) == 10
    assert all(read.startswith("f01a-01-") for read in linked_reads)


def test_f01a_current_evidence_replaces_stale_original_reads(tmp_path: Path) -> None:
    internal_key = "OTUB_7-COI-COI_Probe"
    rank1 = tmp_path / "rank1"
    _plant_f01a_stale_linkage(
        rank1,
        "COI_Probe",
        consensus_tokens=("Consensus0",),
        internal_key=internal_key,
    )
    result, sample = _run_f01a_case(
        rank1,
        mode="primers",
        otu_specs=[("OTUB_7-COI", 10, 31, False)],
        r_path=False,
    )
    assert result.returncode == 0, result.stderr
    linked_reads = _f01a_reads_for_otu(rank1, sample, "OTUB_7-COI")
    assert len(linked_reads) == 10
    assert not any(read.startswith("stale-") for read in linked_reads)
    assert all(read.startswith("f01a-01-") for read in linked_reads)
    assert not list((rank1 / "Consensus" / sample / "OriginalReads").glob("*_reads_sup.fasta"))

    round1 = tmp_path / "cache-round1"
    round1_result, _sample = _run_f01a_case(
        round1,
        mode="primers",
        otu_specs=[("OTUB_7-COI", 10, 31, False)],
        r_path=True,
    )
    assert round1_result.returncode == 0, round1_result.stderr
    round2 = tmp_path / "cache-round2"
    shutil.copytree(round1 / "Consensus" / ".cache", round2 / "Consensus" / ".cache")
    _plant_f01a_stale_linkage(
        round2,
        "COI_Probe",
        consensus_tokens=("Consensus0",),
        internal_key=internal_key,
    )
    round2_result, sample = _run_f01a_case(
        round2,
        mode="primers",
        otu_specs=[("OTUB_7-COI", 10, 31, False)],
        r_path=True,
        debug=True,
    )
    assert round2_result.returncode == 0, round2_result.stderr
    linked_reads = _f01a_reads_for_otu(round2, sample, "OTUB_7-COI")
    assert len(linked_reads) == 10
    assert not any(read.startswith("stale-") for read in linked_reads)
    assert all(read.startswith("f01a-01-") for read in linked_reads)
    assert not list((round2 / "Consensus" / sample / "OriginalReads").glob("*_reads_sup.fasta"))


def test_f01a_private_identity_never_reaches_public_outputs_or_provenance(tmp_path: Path) -> None:
    result, sample = _run_f01a_case(
        tmp_path,
        mode="primers",
        otu_specs=[("OTUB_7-COI", 10, 31, True)],
        r_path=True,
    )
    assert result.returncode == 0, result.stderr
    for artifact in _f01a_public_artifacts(tmp_path, sample):
        assert F01A_PRIVATE_TAG not in artifact.read_text(encoding="utf-8")
        assert "OTU=OTUB_7-COI-COI_Probe" not in artifact.read_text(encoding="utf-8")

    provenance = tmp_path / "consensus_round_provenance.tsv"
    provenance_result = subprocess.run(
        [
            "perl",
            str(EMIT_PROVENANCE),
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-barcode",
            "round_f01a",
            "--out",
            str(provenance),
        ],
        capture_output=True,
        text=True,
    )
    assert provenance_result.returncode == 0, provenance_result.stderr
    provenance_text = provenance.read_text(encoding="utf-8")
    assert "\tCOI_Probe\tOTUB_7-COI\t" in provenance_text
    assert F01A_PRIVATE_TAG not in provenance_text
    assert "OTUB_7-COI-COI_Probe" not in provenance_text


def test_f01a_adjacent_primer_cache_only_metadata_loss_remains_unchanged(tmp_path: Path) -> None:
    bindir = _install_stub_tools(tmp_path, emit_consensus=True)
    current_blast = _make_header(
        "current-read",
        True,
        barcode="COI_Probe",
        adapter="COI_Probe",
        otu_token="OTUB_8-COI",
    )
    current_fasta = _make_header(
        "current-read",
        False,
        model="hac",
        barcode="COI_Probe",
        adapter="COI_Probe",
    )
    _write_common_inputs(
        tmp_path,
        f"{current_blast}\tOTUB_8-COI\tMetazoa\tCOI\n",
        f">{current_fasta}\nACGTACGT\n",
        "current-read\thac\t31\n",
        samples="COI_Probe\n",
    )
    frozen = tmp_path / "otu_frozen_members.tsv"
    frozen.write_text("", encoding="utf-8")
    cache_dir = tmp_path / "Consensus" / ".cache" / "COI_Probe"
    cache_dir.mkdir(parents=True)
    cache_key = "OTUB_7-COI-COI_Probe"
    (cache_dir / f"{cache_key}.consensus.fasta").write_text(
        ">COI_Probe|OTUB_7|COI|reads-5|OTU=OTUB_7-COI\nACGTACGT\n",
        encoding="utf-8",
    )
    (cache_dir / f"{cache_key}.meta").write_text("5\tlegacy-hash\n", encoding="utf-8")
    (cache_dir / "lock_state.tsv").write_text(f"{cache_key}\t2\t1\n", encoding="utf-8")
    previous_keys = tmp_path / "previous_consolidated_keys.tsv"
    previous_keys.write_text(f"COI_Probe\t{cache_key}\n", encoding="utf-8")
    _plant_f01a_stale_linkage(
        tmp_path,
        "COI_Probe",
        consensus_tokens=("Consensus0", "Consensus1"),
    )

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["CONSENSUS_LOCK_ENABLED"] = "1"
    env["CONSENSUS_LOCK_KEYS_PREV"] = str(previous_keys)
    env["CONSENSUS_ID_MISMATCH_POLICY"] = "fail"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "warn"
    result = _run_consensus(tmp_path, env, min_reads="1", frozen_members=str(frozen))
    assert result.returncode == 0, result.stderr

    rows = _read_tsv_rows(tmp_path / "Consensus" / "consensus_otu_map.tsv")
    legacy_row = next(row for row in rows if row["otu_key"] == "OTUB_7-COI")
    assert legacy_row["n_reads"] == "NA"
    assert legacy_row["min_qscore"] == "NA"
    assert legacy_row["frozen_flag"] == "0"
    assert legacy_row["consolidated_flag"] == "0"
    assert _f01a_reads_for_otu(tmp_path, "COI_Probe", "OTUB_7-COI") == []
    original_reads = tmp_path / "Consensus" / "COI_Probe" / "OriginalReads"
    all_linked_reads = [
        read
        for reads_path in original_reads.glob("*_reads.list")
        for read in reads_path.read_text(encoding="utf-8").splitlines()
        if read
    ]
    assert not any(read.startswith("stale-") for read in all_linked_reads)
    assert not list(original_reads.glob("*_reads_sup.fasta"))


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


def test_required_taxonomy_mode_matches_default_bytes(tmp_path: Path) -> None:
    blast_lines = (
        f"{_make_header('read-1', True, barcode='', adapter='sample_A')}\tOTUB_1-COI\tMetazoa\tCOI\n"
        f"{_make_header('read-2', True, target='ITS2', barcode='', adapter='sample_A', otu_token='OTUB_2-ITS2')}\tOTUB_2-ITS2\tViridiplantae\tITS2\n"
        f"{_make_header('read-3', True, barcode='', adapter='sample_A', otu_token='OTUB_3-COI')}\tOTUB_3-COI\tViridiplantae\tCOI\n"
        f"{_make_header('read-4', True, barcode='', adapter='sample_A', otu_token='OTUB_4-COI')}\tOTUB_4-COI\tUnassigned\tCOI\n"
    )
    fasta_lines = (
        f">{_make_header('read-1', False, model='hac', barcode='', adapter='sample_A')}\nACGT\n"
        f">{_make_header('read-2', False, model='hac', target='ITS2', barcode='', adapter='sample_A')}\nACGT\n"
        f">{_make_header('read-3', False, model='hac', barcode='', adapter='sample_A')}\nACGT\n"
        f">{_make_header('read-4', False, model='hac', barcode='', adapter='sample_A')}\nACGT\n"
    )
    qscore_lines = "read-1\thac\t30\nread-2\thac\t30\nread-3\thac\t30\nread-4\thac\t30\n"
    default_dir = tmp_path / "default"
    required_dir = tmp_path / "required"

    default = _run_taxonomy_fixture(
        default_dir,
        blast_lines,
        fasta_lines,
        qscore_lines,
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_required_equivalence",
    )
    required = _run_taxonomy_fixture(
        required_dir,
        blast_lines,
        fasta_lines,
        qscore_lines,
        mode="required",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_required_equivalence",
    )

    assert default.returncode == 0, default.stderr
    assert required.returncode == 0, required.stderr
    assert _consensus_non_timing_snapshot(default_dir) == _consensus_non_timing_snapshot(required_dir)
    assert not (default_dir / "Consensus" / "consensus_taxonomy_admission.tsv").exists()
    assert not (required_dir / "Consensus" / "consensus_taxonomy_admission.tsv").exists()
    assert _read_status(default_dir / "Consensus" / "prefilter_status.tsv")["prefilter_output_rows"] == "2"


def test_allow_unassigned_keeps_configured_markers_and_compatible_pairs(tmp_path: Path) -> None:
    rows = (
        ("read-1", "COI", "OTUB_1-COI", "Unassigned", "COI"),
        ("read-2", "ITS2", "OTUB_2-ITS2", "Unassigned", "ITS2"),
        ("read-3", "COI", "OTUB_3-COI", "Metazoa", "COI"),
        ("read-4", "ITS2", "OTUB_4-ITS2", "Viridiplantae", "ITS2"),
        ("read-5", "COI", "OTUB_5-COI", "Viridiplantae", "COI"),
        ("read-6", "18S", "OTUB_6-18S", "Unassigned", "18S"),
    )
    blast_lines = "".join(
        f"{_make_header(read_id, True, target=target, barcode='', adapter='sample_A', otu_token=otu)}\t{otu}\t{kingdom}\t{marker}\n"
        for read_id, target, otu, kingdom, marker in rows
    )
    fasta_lines = "".join(
        f">{_make_header(read_id, False, model='hac', target=target, barcode='', adapter='sample_A')}\nACGT\n"
        for read_id, target, _otu, _kingdom, _marker in rows
    )
    qscore_lines = "".join(f"{read_id}\thac\t30\n" for read_id, *_rest in rows)

    result = _run_taxonomy_fixture(
        tmp_path,
        blast_lines,
        fasta_lines,
        qscore_lines,
        mode="allow_unassigned",
        samples="sample_A\n",
    )

    assert result.returncode == 0, result.stderr
    status = _read_status(tmp_path / "Consensus" / "prefilter_status.tsv")
    assert status.get("prefilter_input_rows") == "6"
    assert status.get("prefilter_output_rows") == "4"
    sample_meta = tmp_path / "Consensus" / "sample_A" / "otu_meta.tsv"
    keys = {line.split("\t", 1)[0] for line in sample_meta.read_text(encoding="utf-8").splitlines() if line.strip()}
    assert {
        "OTUB_1-COI",
        "OTUB_2-ITS2",
        "OTUB_3-COI",
        "OTUB_4-ITS2",
    }.issubset(keys)
    assert "OTUB_5-COI" not in keys
    assert "OTUB_6-18S" not in keys


def test_allow_unassigned_blocks_no_adapter_from_consensus_and_voucher_export(tmp_path: Path) -> None:
    pure_dir = tmp_path / "pure-no-adapter"
    pure_blast = (
        f"{_make_header('read-na', True)}\tOTUB_1-COI\tUnassigned\tCOI\n"
    )
    pure_fasta = f">{_make_header('read-na', False, model='hac')}\nACGT\n"
    pure_result = _run_taxonomy_fixture(
        pure_dir,
        pure_blast,
        pure_fasta,
        "read-na\thac\t30\n",
        mode="allow_unassigned",
        emit_consensus=True,
    )
    assert pure_result.returncode == 0, pure_result.stderr
    assert _read_status(pure_dir / "Consensus" / "prefilter_status.tsv")["prefilter_output_rows"] == "0"
    pure_merged = pure_dir / "Consensus" / "no_adapter" / "no_adapter_Merged_Consensus.fasta"
    assert not pure_merged.exists() or ">" not in pure_merged.read_text(encoding="utf-8")

    mixed_dir = tmp_path / "mixed"
    mapped_header = _make_header(
        "read-mapped",
        True,
        barcode="",
        adapter="sample_A",
        otu_token="OTUB_2-COI",
    )
    mapped_fasta_header = _make_header(
        "read-mapped",
        False,
        model="hac",
        barcode="",
        adapter="sample_A",
    )
    mixed_blast = (
        f"{mapped_header}\tOTUB_2-COI\tUnassigned\tCOI\n"
        f"{_make_header('read-na', True)}\tOTUB_1-COI\tUnassigned\tCOI\n"
    )
    mixed_fasta = (
        f">{mapped_fasta_header}\nACGT\n"
        f">{_make_header('read-na', False, model='hac')}\nTGCA\n"
    )
    mixed_result = _run_taxonomy_fixture(
        mixed_dir,
        mixed_blast,
        mixed_fasta,
        "read-mapped\thac\t30\nread-na\thac\t30\n",
        mode="allow_unassigned",
        samples="sample_A\nno_adapter\n",
        emit_consensus=True,
    )
    assert mixed_result.returncode == 0, mixed_result.stderr
    assert _read_status(mixed_dir / "Consensus" / "prefilter_status.tsv")["prefilter_output_rows"] == "1"
    mapped_merged = mixed_dir / "Consensus" / "sample_A" / "sample_A_Merged_Consensus.fasta"
    no_adapter_merged = mixed_dir / "Consensus" / "no_adapter" / "no_adapter_Merged_Consensus.fasta"
    assert mapped_merged.read_text(encoding="utf-8").count(">") == 1
    assert not no_adapter_merged.exists() or ">" not in no_adapter_merged.read_text(encoding="utf-8")

    results_dir = mixed_dir / "published-results"
    published_consensus = results_dir / "current" / "state" / "state_1" / "sequences" / "Consensus"
    shutil.copytree(mixed_dir / "Consensus", published_consensus)
    (results_dir / "current" / "state" / "state_1" / "tables").mkdir()
    identity_dir = results_dir / "sample_info" / "run_1"
    identity_dir.mkdir(parents=True)
    (identity_dir / "track_identity.tsv").write_text(
        "sample_id\tmarker_id\tunit_id_collapse\tunit_id_track\n"
        "voucher_A\tCOI\tsample_A\tsample_A\n",
        encoding="utf-8",
    )
    voucher_out = mixed_dir / "voucher-output"
    voucher_result = subprocess.run(
        [
            "bash",
            str(VOUCHER_EXPORT),
            "--results",
            str(results_dir),
            "--out",
            str(voucher_out),
            "--state",
            "state_1",
            "--run-id",
            "run_1",
        ],
        capture_output=True,
        text=True,
    )
    assert voucher_result.returncode == 0, voucher_result.stderr
    summary_rows = (voucher_out / "voucher_summary.tsv").read_text(encoding="utf-8").splitlines()
    assert summary_rows == [
        "sample\tmarker\treads\totu_key\tblast_suggestion",
        "voucher_A\tCOI\t1\tOTUB_2-COI\t",
    ]
    assert "no_adapter" not in (voucher_out / "voucher_sequences.fasta").read_text(encoding="utf-8")


def test_taxonomy_admission_sidecar_binds_current_winner_to_exact_fasta(tmp_path: Path) -> None:
    blast_a, fasta_a, qscores_a, _first_header = _taxonomy_fixture_text(
        [
            ("OTUB_1-COI", "Unassigned", "COI", ["ACGTACGT", "ACGTTCGT"], 31),
            ("OTUB_2-COI", "Metazoa", "COI", ["TTTTACGT", "TTTTTCGT"], 35),
            ("OTUB_3-COI", "Unassigned", "COI", ["GGGGACGT", "GGGGTCGT"], 33),
        ],
        sample="sample_A",
        read_prefix="sample-a",
    )
    blast_b, fasta_b, qscores_b, _ = _taxonomy_fixture_text(
        [
            ("OTUB_4-COI", "Unassigned", "COI", ["TGCATGCA", "TGCGTGCA"], 32),
            ("OTUB_5-COI", "Metazoa", "COI", ["CCCCACGT", "CCCCTCGT"], 34),
        ],
        sample="sample_B",
        read_prefix="sample-b",
    )
    result = _run_taxonomy_fixture(
        tmp_path,
        blast_a + blast_b,
        fasta_a + fasta_b,
        qscores_a + qscores_b,
        mode="allow_unassigned",
        samples="sample_A\nsample_B\n",
        emit_consensus=True,
        round_id="round_admission_1",
        extra_env={"RTBIOSCAN_TEST_VSEARCH_ONE_PER_RECORD": "1"},
    )
    assert result.returncode == 0, result.stderr

    sidecar = tmp_path / "Consensus" / "consensus_taxonomy_admission.tsv"
    sidecar_lines = sidecar.read_text(encoding="utf-8").splitlines()
    assert sidecar_lines[0] == (
        "round_barcode\tsample\totu_key\tconsensus_id\t"
        "taxonomy_admission_status\tmerged_fasta_sha256"
    )
    assert all(len(line.split("\t")) == 6 for line in sidecar_lines)
    rows = _read_tsv_rows(sidecar)
    assert len(rows) == 3
    assert [(item["sample"], item["otu_key"]) for item in rows] == [
        ("sample_A", "OTUB_1-COI"),
        ("sample_A", "OTUB_3-COI"),
        ("sample_B", "OTUB_4-COI"),
    ]
    data_lines = sidecar_lines[1:]
    assert data_lines == sorted(set(data_lines))
    row = rows[0]
    assert row["round_barcode"] == "round_admission_1"
    assert row["sample"] == "sample_A"
    assert row["otu_key"] == "OTUB_1-COI"
    assert row["taxonomy_admission_status"] == "unassigned"

    expected_otus = {
        "sample_A": {"OTUB_1-COI", "OTUB_3-COI"},
        "sample_B": {"OTUB_4-COI"},
    }
    assert {item["sample"] for item in rows} == set(expected_otus)
    merged_paths = {
        sample: tmp_path / "Consensus" / sample / f"{sample}_Merged_Consensus.fasta"
        for sample in expected_otus
    }
    merged_digests = {
        sample: hashlib.sha256(path.read_bytes()).hexdigest()
        for sample, path in merged_paths.items()
    }
    assert merged_digests["sample_A"] != merged_digests["sample_B"]
    for sidecar_row in rows:
        sample = sidecar_row["sample"]
        assert sidecar_row["round_barcode"] == "round_admission_1"
        assert sidecar_row["otu_key"] in expected_otus[sample]
        assert sidecar_row["taxonomy_admission_status"] == "unassigned"
        assert sidecar_row["merged_fasta_sha256"] == merged_digests[sample]
        for other_sample, other_digest in merged_digests.items():
            if other_sample != sample:
                assert sidecar_row["merged_fasta_sha256"] != other_digest

    merged = merged_paths["sample_A"]
    matching_header = next(
        line
        for line in merged.read_text(encoding="utf-8").splitlines()
        if line.startswith(">") and "|OTU=OTUB_1-COI|" in line
    )
    assert "|n=NA|" not in matching_header
    assert "|minQ=NA|" not in matching_header

    provenance = tmp_path / "Consensus" / "consensus_round_provenance.tsv"
    provenance_result = subprocess.run(
        [
            "perl",
            str(EMIT_PROVENANCE),
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-barcode",
            "round_admission_1",
            "--out",
            str(provenance),
        ],
        capture_output=True,
        text=True,
    )
    assert provenance_result.returncode == 0, provenance_result.stderr
    provenance_rows = {
        (item["sample"], item["otu_key"]): item for item in _read_tsv_rows(provenance)
    }
    for sidecar_row in rows:
        provenance_row = provenance_rows[(sidecar_row["sample"], sidecar_row["otu_key"])]
        assert sidecar_row["consensus_id"] == provenance_row["consensus_id"]
        assert sidecar_row["otu_key"] == provenance_row["otu_key"]
    sidecar_text = sidecar.read_text(encoding="utf-8")
    assert F01A_PRIVATE_TAG not in sidecar_text
    assert str(tmp_path) not in sidecar_text
    assert not list((tmp_path / "Consensus").rglob("_taxonomy_admission*.tmp"))


def test_taxonomy_admission_header_only_for_nonqualifying_evidence(tmp_path: Path) -> None:
    cases = [
        (
            "compatible",
            [("OTUB_1-COI", "Metazoa", "COI", ["ACGT", "ACGA"], 30)],
            "sample_A",
            "",
            "COI|ITS2",
            "Metazoa|Viridiplantae",
        ),
        (
            "configured-literal-unassigned",
            [("OTUB_1-COI", "Unassigned", "COI", ["ACGT", "ACGA"], 30)],
            "sample_A",
            "",
            "COI",
            "Unassigned",
        ),
        (
            "mixed-compatible-unassigned",
            [
                ("OTUB_1-COI", "Metazoa", "COI", ["ACGT"], 30),
                ("OTUB_1-COI", "Unassigned", "COI", ["ACGA"], 30),
            ],
            "sample_A",
            "",
            "COI",
            "Metazoa",
        ),
        (
            "assigned-mismatch",
            [("OTUB_1-COI", "Viridiplantae", "COI", ["ACGT"], 30)],
            "sample_A",
            "",
            "COI",
            "Metazoa",
        ),
        (
            "unconfigured-marker",
            [("OTUB_1-18S", "Unassigned", "18S", ["ACGT"], 30)],
            "sample_A",
            "",
            "COI",
            "Metazoa",
        ),
        (
            "malformed-otu-evidence",
            [("MALFORMED", "Unassigned", "COI", ["ACGT"], 30)],
            "sample_A",
            "",
            "COI",
            "Metazoa",
        ),
        (
            "no-adapter",
            [("OTUB_1-COI", "Unassigned", "COI", ["ACGT"], 30)],
            "no_adapter",
            "no_adapter_1",
            "COI",
            "Metazoa",
        ),
    ]
    expected_header = (
        "round_barcode\tsample\totu_key\tconsensus_id\t"
        "taxonomy_admission_status\tmerged_fasta_sha256"
    )
    for name, specs, sample, barcode, target_tokens, target_taxa in cases:
        case_dir = tmp_path / name
        blast, fasta, qscores, _first_header = _taxonomy_fixture_text(
            specs,
            sample=sample,
            barcode=barcode,
        )
        result = _run_taxonomy_fixture(
            case_dir,
            blast,
            fasta,
            qscores,
            mode="allow_unassigned",
            samples=f"{sample}\n",
            emit_consensus=True,
            round_id=f"round_{name}",
            target_tokens=target_tokens,
            target_taxa=target_taxa,
        )
        assert result.returncode == 0, result.stderr
        sidecar = case_dir / "Consensus" / "consensus_taxonomy_admission.tsv"
        assert sidecar.read_text(encoding="utf-8").splitlines() == [expected_header]


def test_taxonomy_admission_uses_emitted_vsearch_winner(tmp_path: Path) -> None:
    cases = (
        ("unassigned-wins", 3, 2, "OTUB_1-COI", 1, False),
        ("compatible-wins-reversed", 2, 3, "OTUB_2-COI", 0, True),
    )
    for name, unassigned_count, compatible_count, winner, expected_rows, reverse in cases:
        case_dir = tmp_path / name
        blast, fasta, qscores, _first_header = _taxonomy_fixture_text(
            [
                ("OTUB_1-COI", "Unassigned", "COI", ["ACGT"] * unassigned_count, 31),
                ("OTUB_2-COI", "Metazoa", "COI", ["ACGT"] * compatible_count, 35),
            ]
        )
        result = _run_taxonomy_fixture(
            case_dir,
            blast,
            fasta,
            qscores,
            mode="allow_unassigned",
            samples="sample_A\n",
            emit_consensus=True,
            round_id=f"round_{name}",
            extra_env={
                "RTBIOSCAN_TEST_VSEARCH_MIX_ALL": "1",
                "RTBIOSCAN_TEST_VSEARCH_REVERSE": "1" if reverse else "0",
            },
        )
        assert result.returncode == 0, result.stderr
        merged = case_dir / "Consensus" / "sample_A" / "sample_A_Merged_Consensus.fasta"
        headers = [line for line in merged.read_text(encoding="utf-8").splitlines() if line.startswith(">")]
        assert len(headers) == 1
        assert f"|OTU={winner}|" in headers[0]
        rows = _read_tsv_rows(case_dir / "Consensus" / "consensus_taxonomy_admission.tsv")
        assert len(rows) == expected_rows
        if expected_rows:
            assert rows[0]["otu_key"] == winner


def test_taxonomy_admission_current_cache_and_frozen_evidence_but_not_carry_forward(
    tmp_path: Path,
) -> None:
    specs = [("OTUB_1-COI", "Unassigned", "COI", ["ACGTACGT", "ACGTTCGT"], 31)]
    blast, fasta, qscores, first_header = _taxonomy_fixture_text(specs)

    cache_round1 = tmp_path / "cache-round1"
    result1 = _run_taxonomy_fixture(
        cache_round1,
        blast,
        fasta,
        qscores,
        mode="allow_unassigned",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_cache_1",
        extra_env={"CONSENSUS_LOCK_ENABLED": "0"},
    )
    assert result1.returncode == 0, result1.stderr

    cache_round2 = tmp_path / "cache-round2"
    shutil.copytree(cache_round1 / "Consensus" / ".cache", cache_round2 / "Consensus" / ".cache")
    result2 = _run_taxonomy_fixture(
        cache_round2,
        blast,
        fasta,
        qscores,
        mode="allow_unassigned",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_cache_2",
        extra_env={"CONSENSUS_LOCK_ENABLED": "0", "CONSENSUS_DEBUG": "1"},
    )
    assert result2.returncode == 0, result2.stderr
    assert _read_tsv_rows(cache_round2 / "Consensus" / "consensus_taxonomy_admission.tsv")[0][
        "round_barcode"
    ] == "round_cache_2"
    assert "cache_reuse count=2" in (
        cache_round2 / "Consensus" / "consensus_debug.log"
    ).read_text(encoding="utf-8")

    frozen_dir = tmp_path / "frozen-current"
    frozen_sequences = [_f01a_sequence(index) for index in range(1, 11)]
    frozen_blast, frozen_fasta, frozen_qscores, frozen_header = _taxonomy_fixture_text(
        [("OTUB_3-COI", "Unassigned", "COI", frozen_sequences, 40)]
    )
    frozen_result = _run_taxonomy_fixture(
        frozen_dir,
        frozen_blast,
        frozen_fasta,
        frozen_qscores,
        mode="allow_unassigned",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_frozen",
        frozen_members_text=f"FROZEN_1\t{frozen_header}\t1\n",
        extra_env={
            "CONSENSUS_LOCK_ENABLED": "0",
            "CONSENSUS_PRUNE_FROZEN_POLICY": "never",
        },
    )
    assert frozen_result.returncode == 0, frozen_result.stderr
    frozen_rows = _read_tsv_rows(frozen_dir / "Consensus" / "consensus_taxonomy_admission.tsv")
    assert len(frozen_rows) == 1
    frozen_merged = frozen_dir / "Consensus" / "sample_A" / "sample_A_Merged_Consensus.fasta"
    frozen_header_out = next(
        line for line in frozen_merged.read_text(encoding="utf-8").splitlines() if line.startswith(">")
    )
    assert "|n=NA|" not in frozen_header_out and "|minQ=NA|" not in frozen_header_out
    assert "|frozen=1|consolidated=1" in frozen_header_out

    carry_dir = tmp_path / "carry-forward"
    shutil.copytree(cache_round1 / "Consensus" / ".cache", carry_dir / "Consensus" / ".cache")
    previous_keys = carry_dir / "previous_keys.tsv"
    previous_keys.write_text("sample_A\tOTUB_1-COI\n", encoding="utf-8")
    carry_blast, carry_fasta, carry_qscores, _carry_header = _taxonomy_fixture_text(
        [("OTUB_2-COI", "Metazoa", "COI", ["TTTT", "TTTA"], 35)]
    )
    carry_result = _run_taxonomy_fixture(
        carry_dir,
        carry_blast,
        carry_fasta,
        carry_qscores,
        mode="allow_unassigned",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_carry",
        extra_env={
            "CONSENSUS_LOCK_ENABLED": "1",
            "CONSENSUS_LOCK_KEYS_PREV": str(previous_keys),
        },
    )
    assert carry_result.returncode == 0, carry_result.stderr
    carry_merged = carry_dir / "Consensus" / "sample_A" / "sample_A_Merged_Consensus.fasta"
    assert "|OTU=OTUB_1-COI|" in carry_merged.read_text(encoding="utf-8")
    assert _read_tsv_rows(carry_dir / "Consensus" / "consensus_taxonomy_admission.tsv") == []


def test_taxonomy_admission_tag_mismatch_stale_replacement_and_required_non_touch(
    tmp_path: Path,
) -> None:
    mismatch_dir = tmp_path / "tag-mismatch"
    mismatch_blast, mismatch_fasta, mismatch_qscores, _header = _taxonomy_fixture_text(
        [("OTUB_1-COI", "Unassigned", "COI", ["ACGTACGT", "ACGTTCGT"], 31)],
        barcode="sample_A",
    )
    mismatch_result = _run_taxonomy_fixture(
        mismatch_dir,
        mismatch_blast,
        mismatch_fasta,
        mismatch_qscores,
        mode="allow_unassigned",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_mismatch",
    )
    assert mismatch_result.returncode == 0, mismatch_result.stderr
    assert _read_tsv_rows(mismatch_dir / "Consensus" / "consensus_taxonomy_admission.tsv") == []
    mismatch_merged = mismatch_dir / "Consensus" / "sample_A" / "sample_A_Merged_Consensus.fasta"
    mismatch_header = next(
        line for line in mismatch_merged.read_text(encoding="utf-8").splitlines() if line.startswith(">")
    )
    assert "|n=NA|" not in mismatch_header and "|minQ=NA|" not in mismatch_header
    cached = next((mismatch_dir / "Consensus" / ".cache" / "sample_A").glob("*.consensus.fasta"))
    cached_header = cached.read_text(encoding="utf-8").splitlines()[0]
    assert len(cached_header[1:].split("|")) == 5

    stale_dir = tmp_path / "stale-replacement"
    stale_sidecar = stale_dir / "Consensus" / "consensus_taxonomy_admission.tsv"
    stale_sidecar.parent.mkdir(parents=True)
    stale_sidecar.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\ttaxonomy_admission_status\tmerged_fasta_sha256\n"
        "old_round\told_sample\tOTUB_OLD-COI\tConsensus9_old\tunassigned\t"
        + "0" * 64
        + "\n",
        encoding="utf-8",
    )
    clean_blast, clean_fasta, clean_qscores, _clean_header = _taxonomy_fixture_text(
        [("OTUB_3-COI", "Unassigned", "COI", ["TTTT", "TTTA"], 33)]
    )
    stale_result = _run_taxonomy_fixture(
        stale_dir,
        clean_blast,
        clean_fasta,
        clean_qscores,
        mode="allow_unassigned",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="current_round",
    )
    assert stale_result.returncode == 0, stale_result.stderr
    stale_rows = _read_tsv_rows(stale_sidecar)
    assert len(stale_rows) == 1
    assert stale_rows[0]["round_barcode"] == "current_round"
    assert stale_rows[0]["otu_key"] == "OTUB_3-COI"
    assert "old_round" not in stale_sidecar.read_text(encoding="utf-8")

    required_dir = tmp_path / "required-non-touch"
    required_sidecar = required_dir / "Consensus" / "consensus_taxonomy_admission.tsv"
    required_sidecar.parent.mkdir(parents=True)
    sentinel = b"pre-existing-required-mode-sentinel\n"
    required_sidecar.write_bytes(sentinel)
    required_result = _run_taxonomy_fixture(
        required_dir,
        clean_blast,
        clean_fasta,
        clean_qscores,
        mode="required",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="required_round",
    )
    assert required_result.returncode == 0, required_result.stderr
    assert required_sidecar.read_bytes() == sentinel


def test_taxonomy_admission_sidecar_does_not_change_existing_outputs(tmp_path: Path) -> None:
    candidate_dir = tmp_path / "candidate"
    control_dir = tmp_path / "control"
    required_candidate_dir = tmp_path / "required-candidate"
    required_control_dir = tmp_path / "required-control"
    control_script = _without_taxonomy_admission_sidecar(tmp_path)
    bash_check = subprocess.run(["/bin/bash", "-n", str(control_script)], capture_output=True, text=True)
    assert bash_check.returncode == 0, bash_check.stderr

    blast, fasta, qscores, _header = _taxonomy_fixture_text(
        [("OTUB_1-COI", "Unassigned", "COI", ["ACGTACGT", "ACGTTCGT"], 31)]
    )
    candidate = _run_taxonomy_fixture(
        candidate_dir,
        blast,
        fasta,
        qscores,
        mode="allow_unassigned",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_equivalence",
    )
    control = _run_taxonomy_fixture(
        control_dir,
        blast,
        fasta,
        qscores,
        mode="allow_unassigned",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_equivalence",
        consensus_script=control_script,
    )
    assert candidate.returncode == 0, candidate.stderr
    assert control.returncode == 0, control.stderr
    assert (candidate_dir / "Consensus" / "consensus_taxonomy_admission.tsv").exists()
    assert not (control_dir / "Consensus" / "consensus_taxonomy_admission.tsv").exists()

    for root in (candidate_dir, control_dir):
        provenance_result = subprocess.run(
            [
                "perl",
                str(EMIT_PROVENANCE),
                "--consensus-dir",
                str(root / "Consensus"),
                "--round-barcode",
                "round_equivalence",
                "--out",
                str(root / "Consensus" / "consensus_round_provenance.tsv"),
            ],
            capture_output=True,
            text=True,
        )
        assert provenance_result.returncode == 0, provenance_result.stderr

    assert _consensus_non_timing_snapshot(
        candidate_dir,
        exclude_admission_sidecar=True,
    ) == _consensus_non_timing_snapshot(control_dir)
    for relative in (
        Path("sample_A/sample_A_Merged_Consensus.fasta"),
        Path("consensus_otu_map.tsv"),
        Path("sample_A/otu_meta.tsv"),
        Path("consolidated_consensus_ids.txt"),
        Path("consensus_round_provenance.tsv"),
    ):
        assert (candidate_dir / "Consensus" / relative).read_bytes() == (
            control_dir / "Consensus" / relative
        ).read_bytes()

    required_candidate = _run_taxonomy_fixture(
        required_candidate_dir,
        blast,
        fasta,
        qscores,
        mode="required",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_required_equivalence",
    )
    required_control = _run_taxonomy_fixture(
        required_control_dir,
        blast,
        fasta,
        qscores,
        mode="required",
        samples="sample_A\n",
        emit_consensus=True,
        round_id="round_required_equivalence",
        consensus_script=control_script,
    )
    assert required_candidate.returncode == required_control.returncode == 0, (
        required_candidate.stderr,
        required_control.stderr,
    )
    required_candidate_snapshot = _consensus_non_timing_snapshot(required_candidate_dir)
    required_control_snapshot = _consensus_non_timing_snapshot(required_control_dir)
    assert [relative for relative, _digest in required_candidate_snapshot] == [
        relative for relative, _digest in required_control_snapshot
    ]
    assert dict(required_candidate_snapshot) == dict(required_control_snapshot)
    for root in (required_candidate_dir, required_control_dir):
        consensus = root / "Consensus"
        assert not (consensus / "consensus_taxonomy_admission.tsv").exists()
        assert not (consensus / "_taxonomy_admission_otus.tmp").exists()
        assert not list(consensus.glob("*/_taxonomy_admission_keys.tmp"))
        assert not list(consensus.glob("*/_taxonomy_admission.tmp"))


def test_taxonomy_admission_structure_and_unknown_semantics_are_guarded() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    blocks = []
    active = False
    current = []
    for line in source.splitlines():
        if "RTBIOSCAN_TAXONOMY_ADMISSION_BEGIN" in line:
            assert not active
            active = True
            current = []
            continue
        if "RTBIOSCAN_TAXONOMY_ADMISSION_END" in line:
            assert active
            active = False
            blocks.append("\n".join(current))
            continue
        if active:
            current.append(line)
    assert not active
    admission_source = "\n".join(blocks)
    assert "blast_report_annotated" not in admission_source
    assert "tmp_clean_blast_report_full" not in admission_source
    assert "Digest::SHA=sha256_hex" in admission_source
    assert source.count(
        "round_barcode\\tsample\\totu_key\\tconsensus_id\\t"
        "taxonomy_admission_status\\tmerged_fasta_sha256"
    ) == 1

    provenance_source = EMIT_PROVENANCE.read_text(encoding="utf-8")
    assert 'my $header = "round_barcode\\tsample\\totu_key\\tconsensus_id\\treads_used_round\\n";' in provenance_source
    docs = (REPO_ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    assert "missing or header-only sidecar" in docs
    assert "digest mismatch" in docs
    assert "literal `unassigned`" in docs
    assert "treated by future readers as unknown" in docs
    assert "changing from `allow_unassigned` to `required` requires a state reset" in docs
    assert "newest completed round" in docs


def test_taxonomy_mode_and_target_contract_fail_before_processing(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing-contract"
    missing_dir.mkdir()
    missing_env = os.environ.copy()
    missing_env.pop("RTBIOSCAN_TARGET_TOKENS", None)
    missing_env.pop("RTBIOSCAN_TARGET_TAXA", None)
    missing = _run_consensus(
        missing_dir,
        missing_env,
        frozen_members="",
        supply_target_contract=False,
    )
    assert missing.returncode != 0
    assert "structurally valid marker/taxon entries" in missing.stderr
    assert not (missing_dir / "Consensus").exists()

    cases = (
        ("invalid-mode", "unexpected", "COI|ITS2", "Metazoa|Viridiplantae", "CONSENSUS_TAXONOMY_MODE"),
        ("uppercase-mode", "REQUIRED", "COI|ITS2", "Metazoa|Viridiplantae", "CONSENSUS_TAXONOMY_MODE"),
        ("blank-taxa", "required", "COI|ITS2", "", "structurally valid marker/taxon entries"),
        ("misaligned-taxa", "required", "COI|ITS2", "Metazoa", "structurally valid marker/taxon entries"),
        ("blank-taxon-slot", "required", "COI|ITS2", "Metazoa|", "structurally valid marker/taxon entries"),
        ("duplicate-marker", "required", "COI|coi", "Metazoa|Metazoa", "structurally valid marker/taxon entries"),
        ("alias-duplicate-marker", "required", "ITS|ITS2", "Viridiplantae|Viridiplantae", "structurally valid marker/taxon entries"),
        ("comma-packed-marker", "required", "COI,ITS2|ITS2", "Metazoa|Viridiplantae", "structurally valid marker/taxon entries"),
        ("null-taxa", "required", "COI|ITS2", "null|null", "structurally valid marker/taxon entries"),
    )
    for name, mode, targets, target_taxa, expected_error in cases:
        case_dir = tmp_path / name
        case_dir.mkdir()
        env = os.environ.copy()
        env["CONSENSUS_TAXONOMY_MODE"] = mode
        env["RTBIOSCAN_TARGET_TOKENS"] = targets
        env["RTBIOSCAN_TARGET_TAXA"] = target_taxa
        result = _run_consensus(case_dir, env, frozen_members="")
        assert result.returncode != 0, name
        assert expected_error in result.stderr, f"{name}: {result.stderr}"
        assert not (case_dir / "Consensus").exists(), name


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
