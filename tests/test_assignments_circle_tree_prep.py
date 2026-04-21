import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "assignments_circle_tree_prep.pl"


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_cmd(cmd, cwd):
    return subprocess.run(cmd, check=True, cwd=cwd, capture_output=True, text=True)


def parse_output(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "marker\tpath\tweight"
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        marker, path, weight = line.split("\t")
        rows.append((marker, path, int(weight)))
    return rows


def test_consensus_circle_tree_prep_counts_and_paths(tmp_path: Path) -> None:
    blast = tmp_path / "consensus.tsv"
    write_text(
        blast,
        "\t".join(
            [
                "consensus_id",
                "barcode_by_homology",
                "consensus_family",
                "consensus_genus",
                "consensus_species",
            ]
        )
        + "\n"
        + "cons1\tCOI\tFamA\tGenA\tSpecA\n"
        + "cons2\tCOI\tFamA\tGenA\tSpecA\n"
        + "cons3\tITS2\t\tGenB\t\n"
        + "cons4\tCOI\tUnassigned\t\t\n"
    )
    out_coi = tmp_path / "out_coi.tsv"
    run_cmd(
        [
            "perl",
            str(SCRIPT),
            "--mode",
            "consensus",
            "--blast",
            str(blast),
            "--marker",
            "COI",
            "--out",
            str(out_coi),
        ],
        tmp_path,
    )
    rows = parse_output(out_coi)
    assert rows == [("COI", "Root;FamA;GenA;SpecA", 2)]

    out_its = tmp_path / "out_its.tsv"
    run_cmd(
        [
            "perl",
            str(SCRIPT),
            "--mode",
            "consensus",
            "--blast",
            str(blast),
            "--marker",
            "ITS2",
            "--out",
            str(out_its),
        ],
        tmp_path,
    )
    rows = parse_output(out_its)
    assert rows == [("ITS2", "Root;GenB", 1)]


def test_otu_circle_tree_prep_reads_weight(tmp_path: Path) -> None:
    blast = tmp_path / "blast_otu.tsv"
    write_text(
        blast,
        "\t".join(
            [
                "otu_id",
                "barcode_by_homology",
                "otu_family",
                "otu_genus",
                "otu_species",
                "perc_id",
                "read_id",
            ]
        )
        + "\n"
        + "OTU1\tCOI\tFamC\tGenC\tSpecC\t99.1\tread1\n"
        + "OTU1\tCOI\tFamC\tGenC\tSpecC\t98.1\tread2\n"
        + "OTU2\tCOI\tFamC\tGenC\tSpecC\t97.5\tread3\n"
        + "OTU3\tITS2\t\tGenD\tSpecD\t96.0\tread4\n"
    )
    sizes = tmp_path / "otu_sizes_round.tsv"
    write_text(
        sizes,
        "otu_id\tsize\n"
        "OTU1\t10\n"
        "OTU2\t5\n"
        "OTU3\t7\n",
    )
    out = tmp_path / "otu_out.tsv"
    run_cmd(
        [
            "perl",
            str(SCRIPT),
            "--mode",
            "otu",
            "--blast",
            str(blast),
            "--otu-sizes-round",
            str(sizes),
            "--marker",
            "COI",
            "--out",
            str(out),
        ],
        tmp_path,
    )
    rows = parse_output(out)
    assert rows == [("COI", "Root;FamC;GenC;SpecC", 15)]
