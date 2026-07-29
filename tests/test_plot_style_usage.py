from pathlib import Path
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_plot_scripts_source_style_contract() -> None:
    plot_scripts = [
        "bin/Read_counts.R",
        "bin/Read_info_quality.R",
        "bin/Time_reads.R",
        "bin/Time_reads_cumulative.R",
        "bin/Time_taxonomy.R",
        "bin/Time_taxonomy_otu.R",
        "bin/Time_taxonomy_consensus.R",
        "bin/Time_taxonomy_consensus_consolidated.R",
        "bin/Treemap_abundance_genus.R",
        "bin/Treemap_abundance_species.R",
        "bin/Treemap_consensus_abundance_genus.R",
        "bin/Treemap_consensus_abundance_genus_consolidated.R",
        "bin/Treemap_consensus_abundance_species.R",
        "bin/Treemap_consensus_abundance_species_consolidated.R",
        "bin/Treemap_consensus_clusters_genus.R",
        "bin/Treemap_consensus_clusters_genus_consolidated.R",
        "bin/Treemap_consensus_clusters_species.R",
        "bin/Treemap_consensus_clusters_species_consolidated.R",
    ]

    missing = []
    for rel_path in plot_scripts:
        path = REPO_ROOT / rel_path
        if "plot_style.R" not in path.read_text(encoding="utf-8"):
            missing.append(rel_path)

    assert not missing, f"plot_style.R not sourced in: {', '.join(missing)}"


def test_plot_scripts_runtime_smoke(tmp_path: Path) -> None:
    rscript = shutil.which("Rscript")
    if rscript is None:
        return

    read_counts_input = tmp_path / "sample_summary_demult_rpt.txt"
    read_counts_input.write_text(
        "sample\tbasecalling_model\tbarcode_by_homology\tread_count\treplicate\tsample_name\n"
        "sampleA\thac\tBC01\t10\t1\tsampleA\n",
        encoding="utf-8",
    )

    time_reads_input = tmp_path / "sample_reads_time_rpt.txt"
    time_reads_input.write_text(
        "run_id\ttime\treads\tdata\n"
        "r1\t0\t5\tsequencing\n",
        encoding="utf-8",
    )

    taxonomy_input = tmp_path / "sample_otu_tax_time_rpt.txt"
    taxonomy_input.write_text(
        "run_id\ttime\ttaxon\tidentifications\n"
        "r1\t0\tspecies\t1\n",
        encoding="utf-8",
    )

    scripts = [
        ("bin/Read_counts.R", [str(read_counts_input)]),
        ("bin/Time_reads.R", [str(time_reads_input)]),
        ("bin/Time_taxonomy.R", [str(taxonomy_input)]),
    ]

    for script, args in scripts:
        result = subprocess.run(
            [rscript, str(REPO_ROOT / script), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def test_read_info_quality_emits_placeholders_for_header_only_round(
    tmp_path: Path,
) -> None:
    script = (REPO_ROOT / "bin/Read_info_quality.R").read_text(encoding="utf-8")
    assert "if (nrow(data_long) == 0)" in script
    for suffix in [
        "_violin_quality_read_info.png",
        "_violin_length_read_info.png",
        "_violin_length_read_info_log.png",
        "_density_read_info.png",
        "_density_read_info_with_unmatched.png",
    ]:
        assert suffix in script


def test_read_counts_runtime_smoke_wrapper_style_invocation(tmp_path: Path) -> None:
    rscript = shutil.which("Rscript")
    if rscript is None:
        return

    shim_dir = tmp_path / "_read_counts_shim"
    shim_dir.mkdir()
    copied_script = shim_dir / "Read_counts.R"
    copied_script.write_bytes((REPO_ROOT / "bin/Read_counts.R").read_bytes())
    assert copied_script.read_bytes() == (REPO_ROOT / "bin/Read_counts.R").read_bytes()
    (shim_dir / "plot_style.R").write_text(
        """
theme_journal <- function() {
  ggplot2::theme_minimal()
}

save_plot_placeholder <- function(out_png, title = NULL, message = "No data available") {
  p <- ggplot2::ggplot() +
    ggplot2::theme_void() +
    ggplot2::annotate("text", x = 0, y = 0, label = message, size = 4)
  if (!is.null(title) && nzchar(title)) {
    p <- p + ggplot2::ggtitle(title)
  }
  save_plot_journal(p, out_png)
}

save_plot_journal <- function(p, out_png, out_pdf = NULL) {
  if (is.null(out_pdf) && !is.null(out_png) && grepl("\\\\.[Pp][Nn][Gg]$", out_png)) {
    out_pdf <- sub("\\\\.[Pp][Nn][Gg]$", ".pdf", out_png)
  }
  dir.create(dirname(out_png), recursive = TRUE, showWarnings = FALSE)
  file.create(out_png)
  if (!is.null(out_pdf)) {
    dir.create(dirname(out_pdf), recursive = TRUE, showWarnings = FALSE)
    file.create(out_pdf)
  }
  if (grepl("_reads_per_barcode\\\\.[Pp][Nn][Gg]$", out_png)) {
    sidecar <- sub("\\\\.[Pp][Nn][Gg]$", ".tsv", out_png)
    utils::write.table(p$data, sidecar, sep = "\\t", quote = FALSE, row.names = FALSE)
  }
}
""".lstrip(),
        encoding="utf-8",
    )

    read_counts_input = tmp_path / "sample_summary_demult_rpt.txt"
    read_counts_input.write_text(
        "sample\tbasecalling_model\tbarcode_by_homology\tread_count\treplicate\tsample_name\n"
        "sampleA_COI\thac\tCOI\t10\t1\tsampleA\n"
        "sampleA_ITS2\thac\tITS2\t5\t2\tsampleA\n"
        "sampleB_COI\thac\tCOI\t7\t1\tsampleB\n",
        encoding="utf-8",
    )

    output_prefix = tmp_path / "sample_assets" / "sampleA"
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            rscript,
            str(copied_script),
            str(read_counts_input),
            "sampleA",
            str(output_prefix),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    for suffix in (
        "_reads_per_barcode.png",
        "_reads_per_barcode.pdf",
        "_reads_per_sample.png",
        "_reads_per_sample.pdf",
        "_reads_per_sample_log.png",
        "_reads_per_sample_log.pdf",
    ):
        assert (tmp_path / f"sample_assets/sampleA{suffix}").exists()

    for suffix in (
        "_reads_per_barcode.png",
        "_reads_per_barcode.pdf",
        "_reads_per_sample.png",
        "_reads_per_sample.pdf",
        "_reads_per_sample_log.png",
        "_reads_per_sample_log.pdf",
    ):
        assert not (tmp_path / f"sample{suffix}").exists()

    sidecar = tmp_path / "sample_assets/sampleA_reads_per_barcode.tsv"
    assert sidecar.exists()
    sidecar_lines = sidecar.read_text(encoding="utf-8").strip().splitlines()
    assert sidecar_lines
    header = sidecar_lines[0].split("\t")
    assert "sample_name" in header
    sample_name_idx = header.index("sample_name")
    sample_names = {line.split("\t")[sample_name_idx] for line in sidecar_lines[1:] if line}
    assert sample_names == {"sampleA"}


def test_read_counts_runtime_rejects_legacy_summary_schema(tmp_path: Path) -> None:
    rscript = shutil.which("Rscript")
    if rscript is None:
        return

    legacy_input = tmp_path / "legacy_summary_demult_rpt.txt"
    legacy_input.write_text(
        "sample\tbasecalling_model\tbarcode_by_homology\tread_count\treplicate\n"
        "sampleA\thac\tBC01\t10\t1\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [rscript, str(REPO_ROOT / "bin/Read_counts.R"), str(legacy_input)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    stderr = result.stderr or result.stdout
    assert "missing required sample_name column" in stderr
    assert "regenerate the summary with the corrected schema" in stderr

    for pattern in (
        "*_reads_per_barcode.png",
        "*_reads_per_barcode.pdf",
        "*_reads_per_sample.png",
        "*_reads_per_sample.pdf",
        "*_reads_per_sample_log.png",
        "*_reads_per_sample_log.pdf",
    ):
        assert not list(tmp_path.glob(pattern))
