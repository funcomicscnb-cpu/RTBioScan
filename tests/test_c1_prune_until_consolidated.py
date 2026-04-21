import subprocess
from pathlib import Path

from tests.c1_prune_test_utils import read_pruned_ids, run_c1_prune_helper

def run_until_consolidated_awk(
    fasta: Path,
    consolidated_keys: Path,
    *,
    samples_file: Path | None = None,
    mixed_mode: str = "sample_scoped_only",
    no_adapter_hint: int = 0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    out_ids = fasta.parent / "active_ids.list"
    cp = run_c1_prune_helper(
        "until_consolidated",
        fasta,
        out_ids,
        consolidated_keys=consolidated_keys,
        samples_file=samples_file,
        mixed_mode=mixed_mode,
        noadapter_hint=no_adapter_hint,
        check=check,
    )
    cp.stdout = "\n".join(read_pruned_ids(out_ids))
    if cp.stdout:
        cp.stdout += "\n"
    return cp


def run_until_consolidated_active_ids(
    fasta: Path,
    consolidated_keys: Path,
    *,
    samples_file: Path | None = None,
    mixed_mode: str = "sample_scoped_only",
    no_adapter_hint: int = 0,
) -> list[str]:
    out_ids = fasta.parent / "active_ids.list"
    run_c1_prune_helper(
        "until_consolidated",
        fasta,
        out_ids,
        consolidated_keys=consolidated_keys,
        samples_file=samples_file,
        mixed_mode=mixed_mode,
        noadapter_hint=no_adapter_hint,
        check=True,
    )
    return read_pruned_ids(out_ids)


def test_until_consolidated_smoke_fixture_keeps_expected_id_only(tmp_path: Path) -> None:
    # Reproducible smoke fixture for C1 prune semantics.
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readA|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\nACGT\n"
        ">readB|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_1-COI\nTGCA\n"
        ">readC|COI|hac|barcode=sample_2024|adapter=sample_2024|OTUB_2-COI\nGGGG\n",
        encoding="utf-8",
    )
    keys = tmp_path / "otu_consolidated_keys.tsv"
    keys.write_text(
        "no_adapter\tOTUB_1-COI\n"
        "sample_2024\tOTUB_2-COI\n",
        encoding="utf-8",
    )
    samples = tmp_path / "samples.txt"
    samples.write_text("no_adapter\nsample_2024\nsampleB\n", encoding="utf-8")

    active_ids = run_until_consolidated_active_ids(fasta, keys, samples_file=samples)
    assert active_ids == ["readB|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_1-COI"]


def test_until_consolidated_adapter_barcode_falls_back_to_barcode_token(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readD|COI|hac|barcode=sampleX_1|adapter=barcode|OTUB_3-COI\nACGT\n",
        encoding="utf-8",
    )
    keys = tmp_path / "otu_consolidated_keys.tsv"
    keys.write_text("sampleX\tOTUB_3-COI\n", encoding="utf-8")
    samples = tmp_path / "samples.txt"
    samples.write_text("sampleX\n", encoding="utf-8")

    active_ids = run_until_consolidated_active_ids(fasta, keys, samples_file=samples)
    assert active_ids == []


def test_until_consolidated_mixed_keys_uses_sample_scope_only(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readE|COI|hac|barcode=sampleA_1|adapter=sampleA_1|OTUB_1-COI\nACGT\n"
        ">readF|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_2-COI\nTGCA\n",
        encoding="utf-8",
    )
    keys = tmp_path / "otu_consolidated_keys.tsv"
    # Global OTU_2 key must be ignored because sample-scoped rows exist.
    keys.write_text(
        "sampleA\tOTUB_1-COI\n"
        "OTUB_2-COI\n",
        encoding="utf-8",
    )
    samples = tmp_path / "samples.txt"
    samples.write_text("sampleA\nsampleB\n", encoding="utf-8")

    active_ids = run_until_consolidated_active_ids(fasta, keys, samples_file=samples)
    assert "readE|COI|hac|barcode=sampleA_1|adapter=sampleA_1|OTUB_1-COI" not in active_ids
    assert "readF|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_2-COI" in active_ids


def test_until_consolidated_single_column_keys_remain_global(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readG|COI|hac|barcode=sampleA_1|adapter=sampleA_1|OTUB_9-COI\nACGT\n"
        ">readH|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_9-COI\nTGCA\n",
        encoding="utf-8",
    )
    keys = tmp_path / "otu_consolidated_keys.tsv"
    keys.write_text("OTUB_9-COI\n", encoding="utf-8")

    active_ids = run_until_consolidated_active_ids(fasta, keys)
    assert active_ids == []


def test_until_consolidated_sample_name_with_numeric_suffix_uses_raw_match(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readI|COI|hac|barcode=sample_2024|adapter=sample_2024|OTUB_5-COI\nACGT\n"
        ">readJ|COI|hac|barcode=sample|adapter=sample|OTUB_5-COI\nTGCA\n",
        encoding="utf-8",
    )
    keys = tmp_path / "otu_consolidated_keys.tsv"
    keys.write_text("sample\tOTUB_5-COI\n", encoding="utf-8")
    samples = tmp_path / "samples.txt"
    samples.write_text("sample\nsample_2024\n", encoding="utf-8")

    active_ids = run_until_consolidated_active_ids(fasta, keys, samples_file=samples)
    assert "readI|COI|hac|barcode=sample_2024|adapter=sample_2024|OTUB_5-COI" in active_ids
    assert "readJ|COI|hac|barcode=sample|adapter=sample|OTUB_5-COI" not in active_ids


def test_until_consolidated_missing_samples_file_does_not_guess_normalized_sample(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readK|COI|hac|barcode=sampleX_1|adapter=sampleX_1|OTUB_7-COI\nACGT\n",
        encoding="utf-8",
    )
    keys = tmp_path / "otu_consolidated_keys.tsv"
    keys.write_text("sampleY\tOTUB_7-COI\n", encoding="utf-8")

    active_ids = run_until_consolidated_active_ids(fasta, keys)
    assert "readK|COI|hac|barcode=sampleX_1|adapter=sampleX_1|OTUB_7-COI" in active_ids


def test_until_consolidated_missing_samples_file_does_not_apply_generic_alias_normalization(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readN|COI|hac|barcode=sampleX_1|adapter=sampleX_1|OTUB_8-COI\nACGT\n",
        encoding="utf-8",
    )
    keys = tmp_path / "otu_consolidated_keys.tsv"
    keys.write_text("sampleX\tOTUB_8-COI\n", encoding="utf-8")

    active_ids = run_until_consolidated_active_ids(fasta, keys)
    assert active_ids == ["readN|COI|hac|barcode=sampleX_1|adapter=sampleX_1|OTUB_8-COI"]


def test_until_consolidated_no_adapter_requires_hint_when_samples_missing(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readP|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_9-COI\nACGT\n",
        encoding="utf-8",
    )
    keys = tmp_path / "otu_consolidated_keys.tsv"
    keys.write_text("no_adapter\tOTUB_9-COI\n", encoding="utf-8")

    active_without_hint = run_until_consolidated_active_ids(fasta, keys, no_adapter_hint=0)
    active_with_hint = run_until_consolidated_active_ids(fasta, keys, no_adapter_hint=1)
    assert active_without_hint == ["readP|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_9-COI"]
    assert active_with_hint == []


def test_until_consolidated_mixed_keys_fail_policy_exits_with_error(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readM|COI|hac|barcode=sampleA_1|adapter=sampleA_1|OTUB_1-COI\nACGT\n",
        encoding="utf-8",
    )
    keys = tmp_path / "otu_consolidated_keys.tsv"
    keys.write_text(
        "sampleA\tOTUB_1-COI\n"
        "OTUB_1-COI\n",
        encoding="utf-8",
    )
    samples = tmp_path / "samples.txt"
    samples.write_text("sampleA\n", encoding="utf-8")

    cp = run_until_consolidated_awk(
        fasta,
        keys,
        samples_file=samples,
        mixed_mode="fail",
        check=False,
    )
    assert cp.returncode == 2
    assert "Mixed consolidated key formats detected" in cp.stderr
