import csv
import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "fast_filter_shadow.py"


def read_tsv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def main_shell_block(start_marker, end_marker, ongoing_state_dir, enabled=False):
    main_text = (REPO_ROOT / "main.nf").read_text(encoding="utf-8")
    block = main_text.split(start_marker, 1)[1].split(end_marker, 1)[0]
    return (
        block.replace("${ongoingStateDir}", str(ongoing_state_dir))
        .replace("${fastFilterShadowEnabled ? 1 : 0}", "1" if enabled else "0")
        .replace("\\$barcode\\\\_", "${barcode}_")
        .replace("\\$", "$")
    )


def test_shadow_records_competition_without_changing_first_hit(tmp_path):
    fasta = tmp_path / "reads.fasta"
    legacy = tmp_path / "legacy.tsv"
    shadow = tmp_path / "shadow.tsv"
    summary = tmp_path / "summary.tsv"
    fasta.write_text(">offtarget\nAACCGG\n>target\nACGTACGT\n>unaligned\nAAAA\n")
    last_output = (
        "target\tCOI|Metazoa|first\t99\t8\t0\t0\t1\t8\t1\t8\t1e-9\t100\n"
        "target\tCOI|Bacteria|second\t98\t8\t0\t0\t1\t8\t1\t8\t1e-8\t90\n"
        "offtarget\tCOI|Bacteria|first\t97\t6\t0\t0\t1\t6\t1\t6\t1e-7\t80\n"
        "offtarget\tCOI|Metazoa|second\t96\t6\t0\t0\t1\t6\t1\t6\t1e-6\t70\n"
    )
    completed = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--fasta",
            str(fasta),
            "--legacy-out",
            str(legacy),
            "--shadow-out",
            str(shadow),
            "--summary-out",
            str(summary),
            "--targets",
            "COI|ITS2",
            "--target-taxa",
            "Metazoa|Viridiplantae",
            "--round-barcode",
            "round1",
        ],
        input=last_output,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert legacy.read_text().splitlines() == [
        last_output.splitlines()[0],
        last_output.splitlines()[2],
    ]

    rows = {row["read_id"]: row for row in read_tsv(shadow)}
    assert rows["target"]["current_label"] == "COI|Metazoa"
    assert rows["target"]["current_retained"] == "1"
    assert rows["target"]["target_minus_offtarget_margin"] == "10"
    assert rows["target"]["competition_status"] == "target_leads"
    assert rows["offtarget"]["current_label"] == "COI|Bacteria"
    assert rows["offtarget"]["current_retained"] == "0"
    assert rows["offtarget"]["target_minus_offtarget_margin"] == "-10"
    assert rows["offtarget"]["competition_status"] == "offtarget_leads"
    assert rows["unaligned"]["competition_status"] == "no_alignment"

    metrics = {row["metric"]: row for row in read_tsv(summary)}
    assert metrics["all_input"]["reads"] == "3"
    assert metrics["all_input"]["bases"] == "18"
    assert metrics["current_retained"]["reads"] == "1"
    assert metrics["current_excluded"]["reads"] == "2"
    assert metrics["current_retained__target_leads"]["reads"] == "1"
    assert metrics["current_excluded__offtarget_leads"]["reads"] == "1"
    assert metrics["current_excluded__no_alignment"]["reads"] == "1"


def test_empty_target_taxon_matches_any_taxon_for_marker(tmp_path):
    fasta = tmp_path / "reads.fasta"
    legacy = tmp_path / "legacy.tsv"
    shadow = tmp_path / "shadow.tsv"
    summary = tmp_path / "summary.tsv"
    fasta.write_text(">fungus\nACGT\n")
    completed = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--fasta",
            str(fasta),
            "--legacy-out",
            str(legacy),
            "--shadow-out",
            str(shadow),
            "--summary-out",
            str(summary),
            "--targets",
            "COI|ITS2",
            "--target-taxa",
            "Metazoa|",
            "--round-barcode",
            "round2",
        ],
        input=(
            "fungus\tITS2|Fungi|hit\t99\t4\t0\t0\t1\t4\t1\t4\t1e-4\t40\n"
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    row = read_tsv(shadow)[0]
    assert row["current_retained"] == "1"
    assert row["best_target_label"] == "ITS2|Fungi"


def test_main_keeps_legacy_path_when_shadow_is_disabled():
    main_text = (REPO_ROOT / "main.nf").read_text(encoding="utf-8")
    config_text = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    assert 'fast_filter_shadow = false' in config_text
    assert (
        "elif lastal ${baseDir}/${params.blast_filter_db} "
        "\\$barcode\\\\_fast.fasta -f BlastTab"
    ) in main_text
    assert "--shadow-out \\$barcode\\\\_fast_filter_shadow.tsv" in main_text
    assert "rerunning the unchanged legacy router" in main_text
    assert "legacy FAST router also failed" in main_text
    assert "preserving first hits from the original LAST stream" in main_text


def test_shadow_state_cleanup_removes_stale_diagnostics_on_disabled_rerun(tmp_path):
    state_dir = tmp_path / "state"
    round_dir = state_dir / "round1"
    round_dir.mkdir(parents=True)
    detail = round_dir / "barcode01_fast_filter_shadow.tsv"
    summary = round_dir / "barcode01_fast_filter_shadow_summary.tsv"
    detail.write_text("stale detail\n")
    summary.write_text("stale summary\n")

    cleanup = main_shell_block(
        "# FAST shadow state cleanup start",
        "# FAST shadow state cleanup end",
        state_dir,
    )
    completed = subprocess.run(
        ["bash", "-c", cleanup],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "barcode": "barcode01", "round_barcode": "round1"},
    )
    assert completed.returncode == 0, completed.stderr
    assert not detail.exists()
    assert not summary.exists()


def test_shadow_state_publish_replaces_both_files_without_temp_residue(tmp_path):
    state_dir = tmp_path / "state"
    round_dir = state_dir / "round1"
    round_dir.mkdir(parents=True)
    detail = round_dir / "barcode01_fast_filter_shadow.tsv"
    summary = round_dir / "barcode01_fast_filter_shadow_summary.tsv"
    detail.write_text("stale detail\n")
    summary.write_text("stale summary\n")
    cleanup = main_shell_block(
        "# FAST shadow state cleanup start",
        "# FAST shadow state cleanup end",
        state_dir,
    )
    cleanup_completed = subprocess.run(
        ["bash", "-c", cleanup],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "barcode": "barcode01", "round_barcode": "round1"},
    )
    assert cleanup_completed.returncode == 0, cleanup_completed.stderr

    (tmp_path / detail.name).write_text("new detail\n")
    (tmp_path / summary.name).write_text("new summary\n")
    publish = main_shell_block(
        "# FAST shadow state publish start",
        "# FAST shadow state publish end",
        state_dir,
        enabled=True,
    )
    completed = subprocess.run(
        ["bash", "-c", publish],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "barcode": "barcode01",
            "round_barcode": "round1",
            "SHADOW_STATE_DETAIL": str(detail),
            "SHADOW_STATE_SUMMARY": str(summary),
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert detail.read_text() == "new detail\n"
    assert summary.read_text() == "new summary\n"
    assert not list(round_dir.glob("*.tmp.*"))


def test_shadow_state_missing_output_leaves_no_stale_or_partial_state(tmp_path):
    state_dir = tmp_path / "state"
    round_dir = state_dir / "round1"
    round_dir.mkdir(parents=True)
    detail = round_dir / "barcode01_fast_filter_shadow.tsv"
    summary = round_dir / "barcode01_fast_filter_shadow_summary.tsv"
    detail.write_text("stale detail\n")
    summary.write_text("stale summary\n")
    cleanup = main_shell_block(
        "# FAST shadow state cleanup start",
        "# FAST shadow state cleanup end",
        state_dir,
    )
    cleanup_completed = subprocess.run(
        ["bash", "-c", cleanup],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "barcode": "barcode01", "round_barcode": "round1"},
    )
    assert cleanup_completed.returncode == 0, cleanup_completed.stderr

    (tmp_path / detail.name).write_text("new detail\n")
    publish = main_shell_block(
        "# FAST shadow state publish start",
        "# FAST shadow state publish end",
        state_dir,
        enabled=True,
    )
    completed = subprocess.run(
        ["bash", "-c", publish],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "barcode": "barcode01",
            "round_barcode": "round1",
            "SHADOW_STATE_DETAIL": str(detail),
            "SHADOW_STATE_SUMMARY": str(summary),
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert not detail.exists()
    assert not summary.exists()
    assert not list(round_dir.glob("*.tmp.*"))


def test_shadow_state_copy_failure_removes_staged_and_persistent_files(tmp_path):
    state_dir = tmp_path / "state"
    round_dir = state_dir / "round1"
    round_dir.mkdir(parents=True)
    detail = round_dir / "barcode01_fast_filter_shadow.tsv"
    summary = round_dir / "barcode01_fast_filter_shadow_summary.tsv"
    (tmp_path / detail.name).write_text("new detail\n")
    (tmp_path / summary.name).write_text("new summary\n")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    real_cp = shutil.which("cp")
    assert real_cp is not None
    fake_cp = fake_bin / "cp"
    fake_cp.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  *_summary.tsv) exit 23 ;;\n"
        "esac\n"
        f'exec "{real_cp}" "$@"\n'
    )
    fake_cp.chmod(0o755)

    publish = main_shell_block(
        "# FAST shadow state publish start",
        "# FAST shadow state publish end",
        state_dir,
        enabled=True,
    )
    completed = subprocess.run(
        ["bash", "-c", publish],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "barcode": "barcode01",
            "round_barcode": "round1",
            "SHADOW_STATE_DETAIL": str(detail),
            "SHADOW_STATE_SUMMARY": str(summary),
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert "removing partial state" in completed.stderr
    assert not detail.exists()
    assert not summary.exists()
    assert not list(round_dir.glob("*.tmp.*"))


def test_legacy_output_matches_grep_awk_for_valid_blasttab(tmp_path):
    fasta = tmp_path / "reads.fasta"
    legacy = tmp_path / "legacy.tsv"
    shadow = tmp_path / "shadow.tsv"
    summary = tmp_path / "summary.tsv"
    fasta.write_text(">query1\nACGT\n>query2\nTGCA\n")
    last_output = (
        "# LAST metadata\n"
        "\n"
        "\n"
        "query2\tCOI|Bacteria|first\t99\t4\t0\t0\t1\t4\t1\t4\t1e-5\t50\n"
        "query1\tCOI|Metazoa|first\t99\t4\t0\t0\t1\t4\t1\t4\t1e-6\t60\n"
        "query2\tCOI|Metazoa|later\t98\t4\t0\t0\t1\t4\t1\t4\t1e-4\t40\n"
    )
    completed = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--fasta",
            str(fasta),
            "--legacy-out",
            str(legacy),
            "--shadow-out",
            str(shadow),
            "--summary-out",
            str(summary),
            "--targets",
            "COI|ITS2",
            "--target-taxa",
            "Metazoa|Viridiplantae",
            "--round-barcode",
            "round3",
        ],
        input=last_output,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    grep = subprocess.run(
        ["grep", "-v", "^#"],
        input=last_output,
        text=True,
        capture_output=True,
        check=False,
    )
    assert grep.returncode == 0
    awk = subprocess.run(
        ["awk", "!seen[$1]++"],
        input=grep.stdout,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    assert awk.returncode == 0
    assert legacy.read_bytes() == awk.stdout.encode()


def test_malformed_diagnostic_row_preserves_legacy_output(tmp_path):
    fasta = tmp_path / "reads.fasta"
    legacy = tmp_path / "legacy.tsv"
    shadow = tmp_path / "shadow.tsv"
    summary = tmp_path / "summary.tsv"
    fasta.write_text(">query\nACGT\n")
    malformed = "query\tCOI|Metazoa|hit\t99\n"
    completed = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--fasta",
            str(fasta),
            "--legacy-out",
            str(legacy),
            "--shadow-out",
            str(shadow),
            "--summary-out",
            str(summary),
            "--targets",
            "COI|ITS2",
            "--target-taxa",
            "Metazoa|Viridiplantae",
            "--round-barcode",
            "round4",
        ],
        input=malformed,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert legacy.read_text() == malformed
    assert not shadow.exists()
    assert not summary.exists()
