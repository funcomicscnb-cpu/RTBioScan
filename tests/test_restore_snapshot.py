from pathlib import Path


def test_restore_current_root_consensus(tmp_path):
    state_id = "state_x"
    current_root = tmp_path / "results" / "current" / "state" / state_id
    tables = current_root / "tables"
    sequences = current_root / "sequences" / "Consensus"
    tables.mkdir(parents=True)
    sequences.mkdir(parents=True)

    # Simulate critical rolling state files and consensus cache
    (tables / "read_qscore_rolling.tsv").write_text("read\tmodel\tq\n", encoding="utf-8")
    (tables / "RTBioScan_seen_read_ids.tsv").write_text("read-1\n", encoding="utf-8")
    (tables / "RTBioScan_on_target_state.tsv").write_text("read-1\tON_TARGET\tCOI\n", encoding="utf-8")
    (tables / "otu_frozen_members.tsv").write_text("otu\tread\n", encoding="utf-8")
    (tables / "RTBioScan_consensus_consolidated_ids.txt").write_text("id1\n", encoding="utf-8")
    (sequences / ".cache").mkdir(parents=True)
    (sequences / ".cache" / "dummy.txt").write_text("x\n", encoding="utf-8")

    # Simulate restore behavior: copy tables into ongoing state and restore consensus directory.
    ongoing_state = tmp_path / "results" / "temp" / "ongoing" / "state" / state_id / "_state"
    ongoing_state.mkdir(parents=True)
    for p in tables.iterdir():
        target = ongoing_state / p.name
        target.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")

    ongoing_consensus = tmp_path / "results" / "temp" / "ongoing" / "state" / state_id / "Consensus"
    ongoing_consensus.mkdir(parents=True)
    for p in (sequences).rglob("*"):
        if p.is_dir():
            (ongoing_consensus / p.relative_to(sequences)).mkdir(parents=True, exist_ok=True)
        else:
            target = ongoing_consensus / p.relative_to(sequences)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")

    assert (ongoing_state / "read_qscore_rolling.tsv").exists()
    assert (ongoing_state / "RTBioScan_seen_read_ids.tsv").exists()
    assert (ongoing_state / "RTBioScan_on_target_state.tsv").exists()
    assert (ongoing_state / "otu_frozen_members.tsv").exists()
    assert (ongoing_state / "RTBioScan_consensus_consolidated_ids.txt").exists()
    assert (ongoing_consensus / ".cache" / "dummy.txt").exists()
