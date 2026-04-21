#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


DEMULT_HEADER = (
    "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t"
    "sampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value"
)
BLAST_HEADER = (
    "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\t"
    "aln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\t"
    "otu_order\totu_family\totu_genus\totu_species"
)

LEGACY_NOISE_WARNING_PATTERNS = (
    re.compile(r"^missing_or_empty:.*(?:RTBioScan_otu_lock_summary\.tsv|otu_members_blastdiag_stats\.tsv|RTBioScan_otu_size_streak\.tsv)$"),
    re.compile(r"^missing_or_empty_data_rows:.*consensus_round_provenance\.tsv$"),
    re.compile(r"^otu_fate_universe_empty:strict_round$"),
    re.compile(r"^size_streak_inputs_missing:"),
)


def normalize_read_id(value: str) -> str:
    value = (value or "").strip()
    if not value or value.upper() == "NA":
        return ""
    value = re.sub(r"\s.*$", "", value)
    value = re.sub(r"\|.*$", "", value)
    return value


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as handle:
        handle.write(text)
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def load_demux_cache_rows(source_path: Path, cache_seen: set[str]) -> list[str]:
    rows: list[str] = []
    if not source_path.exists():
        return rows
    lines = source_path.read_text(encoding="utf-8").splitlines()
    for raw in lines[1:]:
        line = raw.rstrip("\r")
        if not line.strip() or line == lines[0].rstrip("\r"):
            continue
        fields = line.split("\t")
        if not fields:
            continue
        rid = normalize_read_id(fields[0])
        if not rid or rid in cache_seen:
            continue
        cache_seen.add(rid)
        rows.append(line)
    return rows


def load_round_read_info_ids(read_info_path: Path) -> set[str] | None:
    if not read_info_path.exists():
        return None
    ids: set[str] = set()
    lines = read_info_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return ids
    header = lines[0].rstrip("\r").split("\t")
    try:
        read_id_idx = header.index("read_id")
    except ValueError:
        return ids
    for raw in lines[1:]:
        line = raw.rstrip("\r")
        if not line.strip() or line == lines[0].rstrip("\r"):
            continue
        fields = line.split("\t")
        if read_id_idx >= len(fields):
            continue
        rid = normalize_read_id(fields[read_id_idx])
        if rid:
            ids.add(rid)
    return ids


def write_first_seen_snapshot(
    source_path: Path,
    dest_path: Path,
    seen_ids: set[str],
    default_header: str,
    keep_all_rows_for_new_ids: bool,
    allowed_ids: set[str] | None = None,
) -> None:
    header_line = default_header
    output_rows: list[str] = []
    round_new_ids: set[str] = set()

    if source_path.exists():
        lines = source_path.read_text(encoding="utf-8").splitlines()
        if lines:
            header_line = lines[0].rstrip("\r") or default_header
            for raw in lines[1:]:
                line = raw.rstrip("\r")
                if not line.strip() or line == lines[0].rstrip("\r"):
                    continue
                fields = line.split("\t")
                if not fields:
                    continue
                rid = normalize_read_id(fields[0])
                if not rid:
                    continue
                is_global_new = rid not in seen_ids
                is_round_allowed = allowed_ids is None or rid in allowed_ids
                if is_global_new:
                    seen_ids.add(rid)
                    if is_round_allowed:
                        round_new_ids.add(rid)
                        output_rows.append(line)
                elif keep_all_rows_for_new_ids and rid in round_new_ids:
                    output_rows.append(line)

    text = header_line + "\n"
    if output_rows:
        text += "\n".join(output_rows) + "\n"
    atomic_write_text(dest_path, text)


def run_command(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def is_legacy_noise_warning(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in LEGACY_NOISE_WARNING_PATTERNS)


def discover_outdir(state_dir: Path) -> Path | None:
    text = str(state_dir.resolve())
    match = re.match(r"^(.*)/temp/ongoing/state/[^/]+/?$", text)
    if not match:
        return None
    return Path(match.group(1))


def round_sort_key(round_obj: dict, round_dir: Path) -> tuple[int, str]:
    round_barcode = str(round_obj.get("round_barcode") or round_dir.name)
    match = re.search(r"(\d+)(?!.*\d)", round_barcode)
    round_num = int(match.group(1)) if match else -1
    return (round_num, round_barcode)


def round_number(value: str) -> int | None:
    text = str(value or "")
    match = re.search(r"(\d+)(?!.*\d)", text)
    if not match:
        return None
    return int(match.group(1))


def round_is_future(round_barcode: str, current_round_barcode: str) -> bool:
    round_barcode = str(round_barcode or "")
    current_round_barcode = str(current_round_barcode or "")
    if not round_barcode or not current_round_barcode or round_barcode == current_round_barcode:
        return False
    current_num = round_number(current_round_barcode)
    round_num = round_number(round_barcode)
    if current_num is None or round_num is None:
        return False
    return round_num > current_num


def valid_round_numbers_from_feeder_metadata(metadata_dir: Path, run_id: str) -> set[int]:
    valid: set[int] = set()
    prefix = run_id + "_"
    suffix = "_slice.tsv"
    for path in metadata_dir.glob("*_slice.tsv"):
        name = path.name
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix) :]
        if not rest.endswith(suffix):
            continue
        num_str = rest[: -len(suffix)]
        if num_str.isdigit():
            valid.add(int(num_str))
    return valid


def history_publish_mode(state_dir: Path, history_path: Path, current_round_barcode: str) -> str:
    current_num = round_number(current_round_barcode)
    if current_num is None:
        return "normalize"
    if not history_path.exists() or history_path.stat().st_size == 0:
        for child in state_dir.iterdir():
            if not child.is_dir() or child.name == "_state":
                continue
            round_json = child / "round_report.json"
            if not round_json.exists():
                continue
            try:
                round_obj = json.loads(round_json.read_text(encoding="utf-8"))
            except Exception:
                return "normalize"
            round_barcode = str(round_obj.get("round_barcode") or child.name)
            if round_is_future(round_barcode, current_round_barcode):
                continue
            if round_barcode != current_round_barcode:
                return "normalize"
        return "append"

    previous_num: int | None = None
    seen_rounds: set[str] = set()
    with history_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                return "normalize"
            if not isinstance(obj, dict):
                return "normalize"
            round_barcode = str(obj.get("round_barcode") or "")
            if not round_barcode or round_barcode in seen_rounds:
                return "normalize"
            seen_rounds.add(round_barcode)
            round_num = round_number(round_barcode)
            if round_num is None:
                return "normalize"
            if previous_num is not None and round_num < previous_num:
                return "normalize"
            previous_num = round_num

    if previous_num is not None and current_num <= previous_num:
        return "normalize"

    history_rounds = {
        str(obj.get("round_barcode") or "")
        for obj in (
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if isinstance(obj, dict) and obj.get("round_barcode")
    }
    for child in state_dir.iterdir():
        if not child.is_dir() or child.name == "_state":
            continue
        round_json = child / "round_report.json"
        if not round_json.exists():
            continue
        try:
            round_obj = json.loads(round_json.read_text(encoding="utf-8"))
        except Exception:
            return "normalize"
        round_barcode = str(round_obj.get("round_barcode") or child.name)
        if round_is_future(round_barcode, current_round_barcode):
            continue
        round_num = round_number(round_barcode)
        if round_num is None:
            return "normalize"
        if round_barcode != current_round_barcode and round_barcode not in history_rounds:
            return "normalize"

    return "append"


def patch_round_read_fate(
    repo_root: Path,
    round_dir: Path,
    round_obj: dict,
    targets: str,
    target_taxa: str,
) -> dict:
    barcode = str(round_obj["barcode"])
    run_id = str(round_obj["run_id"])
    round_barcode = str(round_obj["round_barcode"])
    state_id = str(round_obj.get("state_id") or "")

    read_info = round_dir / f"{barcode}_read_info_rpt.txt"
    on_target = round_dir / f"{barcode}_on_target_rpt.txt"
    demult = round_dir / f"{barcode}_demult_rpt.txt"
    blast = round_dir / f"{barcode}_blast_otu_pretax_rpt.txt"
    read_fate_demult = round_dir / f"{barcode}_read_fate_demult_first_seen.tsv"
    read_fate_blast = round_dir / f"{barcode}_read_fate_blast_first_seen.tsv"
    blast_unassigned_ids = round_dir / f"{barcode}_blast_unassigned_reads_round.list"

    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(round_dir), suffix=".json", encoding="utf-8") as handle:
        tmp_json = Path(handle.name)

    cmd = [
        "perl",
        str(repo_root / "bin" / "report_round_json.pl"),
        "--run-id",
        run_id,
        "--barcode",
        barcode,
        "--round-barcode",
        round_barcode,
        "--schema-version",
        str(round_obj.get("schema_version") or "1.6"),
        "--targets",
        targets,
        "--target-taxa",
        target_taxa,
        "--out",
        str(tmp_json),
        "--demult",
        str(demult),
        "--read-fate-demult",
        str(read_fate_demult),
        "--blast-otu",
        str(blast),
        "--read-fate-blast",
        str(read_fate_blast),
    ]
    if blast_unassigned_ids.exists():
        cmd.extend(["--blast-unassigned-ids", str(blast_unassigned_ids)])
    if state_id:
        cmd.extend(["--state-id", state_id])
    if read_info.exists():
        cmd.extend(["--read-info", str(read_info)])
    if on_target.exists():
        cmd.extend(["--on-target", str(on_target)])

    try:
        run_command(cmd)
        repaired = json.loads(tmp_json.read_text(encoding="utf-8"))
    finally:
        if tmp_json.exists():
            tmp_json.unlink()

    round_obj["read_fate"] = repaired.get("read_fate", {})
    existing_warnings = round_obj.get("warnings")
    filtered_warnings = []
    if isinstance(existing_warnings, list):
        filtered_warnings.extend(w for w in existing_warnings if not is_legacy_noise_warning(str(w)))
    repaired_warnings = repaired.get("warnings")
    if isinstance(repaired_warnings, list):
        for warning in repaired_warnings:
            text = str(warning)
            if is_legacy_noise_warning(text) or text in filtered_warnings:
                continue
            filtered_warnings.append(text)
    round_obj["warnings"] = filtered_warnings
    return round_obj


def rebuild_blast_unassigned_current(
    repo_root: Path,
    round_dir: Path,
    state_state_dir: Path,
    barcode: str,
    min_level: str,
) -> None:
    evidence = round_dir / f"{barcode}_blast_report_annotated_otu_evidence.txt"
    dest_path = state_state_dir / f"{barcode}_blast_unassigned_current.list"
    if not evidence.exists() or evidence.stat().st_size == 0:
        atomic_write_text(dest_path, "")
        return

    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(state_state_dir), suffix=".list", encoding="utf-8") as handle:
        tmp_path = Path(handle.name)
    try:
        run_command(
            [
                "perl",
                str(repo_root / "bin" / "blast_unassigned_read_ids.pl"),
                str(evidence),
                str(tmp_path),
                "--min-level",
                min_level,
            ]
        )
        text = tmp_path.read_text(encoding="utf-8") if tmp_path.exists() else ""
        atomic_write_text(dest_path, text)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild first-seen read-fate snapshots for an existing RTBioScan state, "
            "patch each round_report.json read_fate block, rebuild report_history.jsonl, "
            "and refresh the run/global HTML reports."
        )
    )
    parser.add_argument("--state-dir", required=True, help="Path to results/temp/ongoing/state/<state_id>")
    parser.add_argument("--targets", required=True, help="Pipe-separated marker list, e.g. COI|ITS2")
    parser.add_argument("--target-taxa", required=True, help="Pipe-separated taxon list aligned to --targets")
    parser.add_argument("--blast-unassigned-min-level", default="genus", help="Min taxonomy level for blast_unassigned_read_ids.pl: family | genus | species")
    parser.add_argument("--outdir", default="", help="Results outdir; auto-detected from --state-dir when omitted")
    parser.add_argument("--skip-render", action="store_true", help="Do not rerun report_rebuild.sh after repair")
    parser.add_argument("--live", action="store_true", help="Repair in place for the live reporting path without updating run index or rendering")
    parser.add_argument("--check-live-order", action="store_true", help="Print append or normalize for the current round and exit")
    parser.add_argument("--current-round-barcode", default="", help="Round barcode to evaluate for --check-live-order and to bound --live repairs")
    parser.add_argument(
        "--valid-rounds-from-feeder-metadata",
        default="",
        help=(
            "Feeder metadata dir (results/pod5/<run_id>/metadata). When set, rounds with no "
            "matching *_slice.tsv are excluded and their round_report.json renamed to .orphan."
        ),
    )
    args = parser.parse_args()
    if args.blast_unassigned_min_level not in {"family", "genus", "species"}:
        raise SystemExit(f"ERROR: invalid --blast-unassigned-min-level: {args.blast_unassigned_min_level}")

    state_dir = Path(args.state_dir).resolve()
    if not state_dir.is_dir():
        raise SystemExit(f"ERROR: state dir not found: {state_dir}")

    repo_root = Path(__file__).resolve().parents[1]
    outdir = Path(args.outdir).resolve() if args.outdir else discover_outdir(state_dir)
    state_state_dir = state_dir / "_state"
    history_path = state_state_dir / "report_history.jsonl"

    if args.check_live_order:
        if not args.current_round_barcode:
            raise SystemExit("ERROR: --current-round-barcode is required with --check-live-order")
        print(history_publish_mode(state_dir, history_path, args.current_round_barcode))
        return 0

    round_entries: list[tuple[Path, dict]] = []
    for child in state_dir.iterdir():
        if not child.is_dir() or child.name == "_state":
            continue
        round_json = child / "round_report.json"
        if not round_json.exists():
            continue
        round_obj = json.loads(round_json.read_text(encoding="utf-8"))
        round_entries.append((child, round_obj))

    if not round_entries:
        raise SystemExit(f"ERROR: no round_report.json files found under {state_dir}")

    round_entries.sort(key=lambda item: round_sort_key(item[1], item[0]))
    if args.live and args.current_round_barcode:
        round_entries = [
            (round_dir, round_obj)
            for round_dir, round_obj in round_entries
            if not round_is_future(str(round_obj.get("round_barcode") or round_dir.name), args.current_round_barcode)
        ]
        if not round_entries:
            raise SystemExit(
                f"ERROR: no round_report.json files remain after bounding live repair to {args.current_round_barcode}"
            )

    if args.valid_rounds_from_feeder_metadata:
        feeder_meta = Path(args.valid_rounds_from_feeder_metadata)
        if not feeder_meta.is_dir():
            raise SystemExit(f"ERROR: feeder metadata dir not found: {feeder_meta}")

        run_ids = {str(round_obj.get("run_id") or "") for _, round_obj in round_entries}
        run_ids.discard("")
        if len(run_ids) != 1:
            raise SystemExit(
                "ERROR: round_report.json files must have exactly one non-empty run_id "
                "when filtering by feeder metadata"
            )
        run_id_for_filter = next(iter(run_ids))
        valid_nums = valid_round_numbers_from_feeder_metadata(feeder_meta, run_id_for_filter)
        if not valid_nums:
            raise SystemExit(
                "ERROR: no *_slice.tsv files matching run_id were found in feeder metadata dir"
            )

        kept: list[tuple[Path, dict]] = []
        orphaned_dirs: list[Path] = []
        for round_dir, round_obj in round_entries:
            barcode = str(round_obj.get("barcode") or "")
            round_barcode = str(round_obj.get("round_barcode") or round_dir.name)
            n = round_number(round_barcode)
            if n is None:
                raise SystemExit(
                    f"ERROR: cannot derive numeric round suffix from round_barcode={round_barcode!r}"
                )
            if n in valid_nums:
                if not barcode:
                    raise SystemExit(f"ERROR: missing barcode in round_report.json for {round_dir.name}")
                demult = round_dir / f"{barcode}_demult_rpt.txt"
                blast = round_dir / f"{barcode}_blast_otu_pretax_rpt.txt"
                if not demult.exists():
                    raise SystemExit(f"ERROR: missing demult report for {round_dir.name}: {demult}")
                if not blast.exists():
                    raise SystemExit(f"ERROR: missing BLAST OTU report for {round_dir.name}: {blast}")
                kept.append((round_dir, round_obj))
            else:
                orphaned_dirs.append(round_dir)

        if not kept:
            raise SystemExit(
                "ERROR: no valid rounds remain after feeder-metadata filtering. "
                "Check that --valid-rounds-from-feeder-metadata points to the correct metadata dir."
            )

        for orphan_dir in orphaned_dirs:
            orphan_json = orphan_dir / "round_report.json"
            if orphan_json.exists():
                orphan_dest = orphan_json.with_name(orphan_json.name + ".orphan")
                if orphan_dest.exists():
                    orphan_dest.unlink()
                orphan_json.rename(orphan_dest)
        round_entries = kept

    seen_demux: set[str] = set()
    seen_blast: set[str] = set()
    history_lines: list[str] = []
    last_round_dir: Path | None = None
    last_round_obj: dict | None = None
    demux_cache_seen: set[str] = set()
    demux_cache_rows: list[str] = []

    for round_dir, round_obj in round_entries:
        barcode = str(round_obj["barcode"])
        demult = round_dir / f"{barcode}_demult_rpt.txt"
        blast = round_dir / f"{barcode}_blast_otu_pretax_rpt.txt"
        read_info = round_dir / f"{barcode}_read_info_rpt.txt"
        if not demult.exists():
            raise SystemExit(f"ERROR: missing demult report for {round_dir.name}: {demult}")
        if not blast.exists():
            raise SystemExit(f"ERROR: missing BLAST OTU report for {round_dir.name}: {blast}")

        round_read_ids = load_round_read_info_ids(read_info)
        demux_cache_rows.extend(load_demux_cache_rows(demult, demux_cache_seen))
        write_first_seen_snapshot(
            demult,
            round_dir / f"{barcode}_read_fate_demult_first_seen.tsv",
            seen_demux,
            DEMULT_HEADER,
            keep_all_rows_for_new_ids=False,
            allowed_ids=round_read_ids,
        )
        write_first_seen_snapshot(
            blast,
            round_dir / f"{barcode}_read_fate_blast_first_seen.tsv",
            seen_blast,
            BLAST_HEADER,
            keep_all_rows_for_new_ids=True,
            allowed_ids=round_read_ids,
        )

        patched = patch_round_read_fate(repo_root, round_dir, round_obj, args.targets, args.target_taxa)
        atomic_write_text(round_dir / "round_report.json", json.dumps(patched) + "\n")
        history_lines.append(json.dumps(patched))
        rebuild_blast_unassigned_current(
            repo_root,
            round_dir,
            state_state_dir,
            barcode,
            args.blast_unassigned_min_level,
        )
        last_round_dir = round_dir
        last_round_obj = patched

    atomic_write_text(history_path, "\n".join(history_lines) + "\n")
    if last_round_obj is not None:
        barcode = str(last_round_obj["barcode"])
        atomic_write_text(
            state_state_dir / f"{barcode}_read_fate_demux_seen.tsv",
            "\n".join(sorted(seen_demux)) + ("\n" if seen_demux else ""),
        )
        atomic_write_text(
            state_state_dir / f"{barcode}_read_fate_blast_seen.tsv",
            "\n".join(sorted(seen_blast)) + ("\n" if seen_blast else ""),
        )
        demux_cache_text = DEMULT_HEADER + "\n"
        if demux_cache_rows:
            demux_cache_text += "\n".join(demux_cache_rows) + "\n"
        atomic_write_text(
            state_state_dir / f"{barcode}_demux_annotation_cache.tsv",
            demux_cache_text,
        )

    if last_round_dir is not None and last_round_obj is not None:
        run_id = str(last_round_obj["run_id"])
        barcode = str(last_round_obj["barcode"])
        state_id = str(last_round_obj.get("state_id") or state_dir.name)
        report_rel_path = f"runs/{run_id}/report.html"
        if outdir is not None:
            run_report_json = outdir / "report_html" / "runs" / run_id / "run_report.json"
            run_report_json.parent.mkdir(parents=True, exist_ok=True)
        else:
            run_report_json = last_round_dir / "run_report.json"
        run_started_utc = state_state_dir / "run_started_utc.txt"
        run_cmd = [
            "perl",
            str(repo_root / "bin" / "report_run_json.pl"),
            "--history",
            str(history_path),
            "--out",
            str(run_report_json),
            "--run-id",
            run_id,
            "--barcode",
            barcode,
            "--state-id",
            state_id,
            "--schema-version",
            str(last_round_obj.get("schema_version") or "1.6"),
            "--report-rel-path",
            report_rel_path,
        ]
        if outdir is not None:
            run_cmd.extend(["--outdir", str(outdir)])
        if run_started_utc.exists():
            run_cmd.extend(["--run-started-utc-file", str(run_started_utc)])
        run_command(run_cmd)

        if outdir is not None and not args.live:
            run_index = outdir / "report_html" / "runs_index.jsonl"
            run_index_lock = outdir / ".runs_index.lock"
            run_command(
                [
                    "bash",
                    str(repo_root / "bin" / "report_run_index_update.sh"),
                    str(run_report_json),
                    str(run_index),
                    str(run_index_lock),
                ]
            )
            if not args.skip_render:
                render_cmd = [
                    "bash",
                    str(repo_root / "bin" / "report_rebuild.sh"),
                    "--outdir",
                    str(outdir),
                    "--state-id",
                    state_id,
                    "--run-id",
                    run_id,
                ]
                run_command(render_cmd)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
