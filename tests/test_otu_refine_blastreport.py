import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "otu_refine_blastreport.pl"


def _run(tmp_path: Path, tax_rows: str, clstr_rows: str, lineage_rows: str):
    tax_file = tmp_path / "blastreport.txt"
    clstr_file = tmp_path / "reads.clstr"
    lineage_file = tmp_path / "id2lineage.tsv"
    tax_file.write_text(tax_rows, encoding="utf-8")
    clstr_file.write_text(clstr_rows, encoding="utf-8")
    lineage_file.write_text(lineage_rows, encoding="utf-8")
    return subprocess.run(
        ["perl", str(SCRIPT), str(tax_file), str(clstr_file), str(lineage_file)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_single_cluster_is_finalized_without_crashing(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup;TX1\nread2|COI|sup;TX1\n",
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n",
        "TX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "#seq_id\ttax_id\tlineage",
        "read1|COI|sup|OTUB_0-COI\tTX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One",
        "read2|COI|sup|OTUB_0-COI\tTX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One",
    ]


def test_clusters_do_not_leak_taxon_state_across_boundaries(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup;TX1\nread2|COI|sup;TX2\nread3|COI|sup;TX3\n",
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n"
        ">Cluster 1\n"
        "0\t100nt, >read3|COI|sup... *\n",
        "TX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One\n"
        "TX2\tK__Two;p__Two;c__Two;o__Two;f__Two;g__Two;s__Two\n"
        "TX3\tK__Three;p__Three;c__Three;o__Three;f__Three;g__Three;s__Three\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "#seq_id\ttax_id\tlineage",
        "read1|COI|sup|OTUB_0-COI\tTX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One",
        "read2|COI|sup|OTUB_0-COI\tTX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One",
        "read3|COI|sup|OTUB_1-COI\tTX3\tK__Three;p__Three;c__Three;o__Three;f__Three;g__Three;s__Three",
    ]


def test_prefers_top_ranked_taxon_in_cluster(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup;TX1\nread2|COI|sup;TX1\nread3|COI|sup;TX2\n",
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n"
        "2\t100nt, >read3|COI|sup... at +/99%\n",
        "TX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One\n"
        "TX2\tK__Two;p__Two;c__Two;o__Two;f__Two;g__Two;s__Two\n",
    )

    assert result.returncode == 0, result.stderr
    out_lines = result.stdout.splitlines()
    assert out_lines[1].split("\t")[1] == "TX1"
    assert out_lines[2].split("\t")[1] == "TX1"


def test_output_rows_do_not_split_when_inputs_include_newlines(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup;TX1\nread2|COI|sup;TX1\n",
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n",
        "TX1\tK__One;p__One;c__One;o__One;f__One;g__One;s__One\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n\n") == 0
    assert len(result.stdout.splitlines()) == 3


def test_ignores_alignment_columns_in_semicolon_blast_rows(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup;36164;0.0;427;96.487\n"
        "read2|COI|sup;36164;0.0;425;95.100\n",
        ">Cluster 0\n"
        "0\t100nt, >read1|COI|sup... *\n"
        "1\t100nt, >read2|COI|sup... at +/99%\n",
        "36164\tK__Metazoa;p__Chordata;c__Aves;o__Passeriformes;f__Corvidae;g__Corvus;s__Corvus corax\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "#seq_id\ttax_id\tlineage",
        "read1|COI|sup|OTUB_0-COI\t36164\tK__Metazoa;p__Chordata;c__Aves;o__Passeriformes;f__Corvidae;g__Corvus;s__Corvus corax",
        "read2|COI|sup|OTUB_0-COI\t36164\tK__Metazoa;p__Chordata;c__Aves;o__Passeriformes;f__Corvidae;g__Corvus;s__Corvus corax",
    ]
