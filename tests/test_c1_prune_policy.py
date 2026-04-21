from pathlib import Path

from tests.c1_prune_test_utils import read_pruned_ids, run_c1_prune_helper

def test_policy_never_keeps_all_reads(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    out_ids = tmp_path / "active_ids.list"
    fasta.write_text(
        ">readA|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\nACGT\n"
        ">readB|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_1-COI\nTGCA\n",
        encoding="utf-8",
    )
    run_c1_prune_helper("never", fasta, out_ids)
    assert read_pruned_ids(out_ids) == [
        "readA|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI",
        "readB|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_1-COI",
    ]


def test_policy_always_prunes_frozen_member_uuids(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    out_ids = tmp_path / "active_ids.list"
    frozen = tmp_path / "otu_frozen_members.tsv"
    fasta.write_text(
        ">readA|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\nACGT\n"
        ">readB|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_1-COI\nTGCA\n",
        encoding="utf-8",
    )
    frozen.write_text(
        "FROZEN_x\treadA|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1\t1\n",
        encoding="utf-8",
    )
    run_c1_prune_helper("always", fasta, out_ids, frozen_members=frozen)
    assert read_pruned_ids(out_ids) == ["readB|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_1-COI"]


def test_policy_until_consolidated_with_missing_samples_disables_generic_alias_match(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    out_ids = tmp_path / "active_ids.list"
    cons = tmp_path / "otu_consolidated_keys.tsv"
    fasta.write_text(
        ">readC|COI|hac|barcode=sampleX_1|adapter=sampleX_1|OTUB_8-COI\nACGT\n"
        ">readD|COI|hac|barcode=sampleY_1|adapter=sampleY_1|OTUB_8-COI\nTGCA\n",
        encoding="utf-8",
    )
    cons.write_text("sampleX\tOTUB_8-COI\n", encoding="utf-8")
    run_c1_prune_helper("until_consolidated", fasta, out_ids, consolidated_keys=cons)
    assert read_pruned_ids(out_ids) == [
        "readC|COI|hac|barcode=sampleX_1|adapter=sampleX_1|OTUB_8-COI",
        "readD|COI|hac|barcode=sampleY_1|adapter=sampleY_1|OTUB_8-COI",
    ]


def test_until_consolidated_empty_active_ids_implies_empty_pruned_fasta(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    out_ids = tmp_path / "active_ids.list"
    pruned = tmp_path / "reads.pruned.fasta"
    cons = tmp_path / "otu_consolidated_keys.tsv"
    fasta.write_text(
        ">readE|COI|hac|barcode=sampleA|adapter=sampleA|OTUB_9-COI\nACGT\n",
        encoding="utf-8",
    )
    cons.write_text("sampleA\tOTUB_9-COI\n", encoding="utf-8")
    samples = tmp_path / "samples.txt"
    samples.write_text("sampleA\n", encoding="utf-8")
    run_c1_prune_helper("until_consolidated", fasta, out_ids, consolidated_keys=cons, samples_file=samples)
    assert read_pruned_ids(out_ids) == []
    # Mirror main.nf behavior: empty active_ids + non-empty consolidated keys truncates FASTA.
    if cons.exists() and cons.stat().st_size > 0 and out_ids.stat().st_size == 0:
        pruned.write_text("", encoding="utf-8")
    assert pruned.read_text(encoding="utf-8") == ""


def test_policy_until_consolidated_mixed_keys_fail_mode_errors(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    out_ids = tmp_path / "active_ids.list"
    cons = tmp_path / "otu_consolidated_keys.tsv"
    fasta.write_text(
        ">readM|COI|hac|barcode=sampleA_1|adapter=sampleA_1|OTUB_1-COI\nACGT\n",
        encoding="utf-8",
    )
    cons.write_text(
        "sampleA\tOTUB_1-COI\n"
        "OTUB_1-COI\n",
        encoding="utf-8",
    )
    cp = run_c1_prune_helper(
        "until_consolidated",
        fasta,
        out_ids,
        consolidated_keys=cons,
        mixed_mode="fail",
        check=False,
    )
    assert cp.returncode == 2
    assert "Mixed consolidated key formats detected" in cp.stderr
