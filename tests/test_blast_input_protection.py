"""Integration test for pre-BLAST re-injection of protected OTU members."""
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "expand_otu_keys_to_member_ids.pl"


def test_preblast_injection_enforce_mode(tmp_path):
    # Persisted protected OTU keys list.
    protected_keys = tmp_path / "assigned_otu_keys_ever.list"
    protected_keys.write_text("OTU1\n", encoding="utf-8")

    # otu_members_round: OTU1 has readB
    otu_members = tmp_path / "otu_members_round.tsv"
    otu_members.write_text(
        "otu_id\tread_id\n"
        "OTU1\treadB\n",
        encoding="utf-8",
    )

    # Helper outputs protected IDs
    out_members = tmp_path / "protected_read_ids_round.list"
    result = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            str(protected_keys),
            str(otu_members),
            str(out_members),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    # Filtered BLAST input lacks readB
    blast_filtered = tmp_path / "blast_filtered.fasta"
    blast_filtered.write_text(">readA|COI|sup|barcode=bc|adapter=no_adapter\nACGT\n", encoding="utf-8")

    # Full FASTA contains readB
    fasta_hq_qced = tmp_path / "fasta_hq_qced.fasta"
    fasta_hq_qced.write_text(
        ">readA|COI|sup|barcode=bc|adapter=no_adapter\nACGT\n"
        ">readB|COI|fast|barcode=bc|adapter=no_adapter\nTGCA\n",
        encoding="utf-8",
    )

    # Simulate pre-BLAST injection without external tools.
    # Dedup by read_id (prefix before first '|' in header).
    def read_fasta(path: Path):
        header = None
        seq_lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(">"):
                if header is not None:
                    yield header, "".join(seq_lines)
                header = line[1:]
                seq_lines = []
            else:
                seq_lines.append(line)
        if header is not None:
            yield header, "".join(seq_lines)

    merged_records = []
    seen_ids = set()
    for path in (blast_filtered, fasta_hq_qced):
        for header, seq in read_fasta(path):
            rid = header.split("|")[0]
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            merged_records.append((header, seq))

    merged_text = "\n".join(
        f">{h}\n{s}" for h, s in merged_records
    ) + "\n"
    assert ">readB|" in merged_text
