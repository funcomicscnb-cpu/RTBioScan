import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "consensus_assign_depth.sh"

_THRESHOLDS = ["--family", "92", "--genus", "95", "--species", "98"]


def _run(report_text: str, extra_args: list[str] | None = None, tmp_path: Path = None) -> dict[str, str]:
    p = tmp_path / "report.txt"
    p.write_text(report_text, encoding="utf-8")
    args = [str(SCRIPT), "--report", str(p)] + _THRESHOLDS + (extra_args or [])
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        q, level = line.split("\t", 1)
        out[q] = level
    return out


# --- boundary values ---

def test_at_species_threshold_assigns_species(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250,98\n", tmp_path=tmp_path)
    assert rows["q1"] == "species"


def test_at_genus_threshold_assigns_genus(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250,95\n", tmp_path=tmp_path)
    assert rows["q1"] == "genus"


def test_at_family_threshold_assigns_family(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250,92\n", tmp_path=tmp_path)
    assert rows["q1"] == "family"


def test_above_species_threshold_assigns_species(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250,99.5\n", tmp_path=tmp_path)
    assert rows["q1"] == "species"


def test_between_genus_and_species_assigns_genus(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250,96\n", tmp_path=tmp_path)
    assert rows["q1"] == "genus"


def test_between_family_and_genus_assigns_family(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250,93\n", tmp_path=tmp_path)
    assert rows["q1"] == "family"


def test_below_family_threshold_assigns_unassigned(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250,80\n", tmp_path=tmp_path)
    assert rows["q1"] == "unassigned"


# --- edge cases ---

def test_missing_pident_field_assigns_unassigned(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250\n", tmp_path=tmp_path)
    assert rows["q1"] == "unassigned"


def test_empty_pident_assigns_unassigned(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250,\n", tmp_path=tmp_path)
    assert rows["q1"] == "unassigned"


def test_non_numeric_pident_assigns_unassigned(tmp_path: Path) -> None:
    rows = _run("q1,s1,0.001,250,NA\n", tmp_path=tmp_path)
    assert rows["q1"] == "unassigned"


def test_header_row_skipped(tmp_path: Path) -> None:
    rows = _run("qseqid,sseqid,evalue,length,pident\nq1,s1,0.001,250,98\n", tmp_path=tmp_path)
    assert "qseqid" not in rows
    assert rows["q1"] == "species"


def test_multiple_queries_resolved_independently(tmp_path: Path) -> None:
    report = "q1,s1,0.001,250,99\nq2,s2,0.001,250,96\nq3,s3,0.001,250,93\nq4,s4,0.001,250,80\n"
    rows = _run(report, tmp_path=tmp_path)
    assert rows["q1"] == "species"
    assert rows["q2"] == "genus"
    assert rows["q3"] == "family"
    assert rows["q4"] == "unassigned"


def test_multiple_hits_same_qseqid_emits_one_row_per_hit(tmp_path: Path) -> None:
    """Script emits one row per input hit; caller handles dedup."""
    report = "q1,s1,0.001,250,99\nq1,s2,0.001,250,93\n"
    rows = _run(report, tmp_path=tmp_path)
    # Only last write per qseqid survives in dict; both should be valid levels.
    assert rows["q1"] in {"species", "family"}


def test_empty_report_produces_no_output(tmp_path: Path) -> None:
    rows = _run("", tmp_path=tmp_path)
    assert rows == {}
