import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "persist_otu_keys_ever.pl"


def test_persist_otu_keys_ever_unions_round_and_existing_keys(tmp_path: Path) -> None:
    round_keys = tmp_path / "round.list"
    ever_keys = tmp_path / "ever.list"
    stats = tmp_path / "persist_stats.tsv"
    round_keys.write_text("OTUB_2-COI\nOTUB_1-COI\n", encoding="utf-8")
    ever_keys.write_text("OTUB_0-COI\nOTUB_1-COI\n", encoding="utf-8")

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(round_keys), str(ever_keys), str(stats)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert ever_keys.read_text(encoding="utf-8").splitlines() == [
        "OTUB_0-COI",
        "OTUB_1-COI",
        "OTUB_2-COI",
    ]
    stats_map = dict(
        line.split("\t", 1) for line in stats.read_text(encoding="utf-8").splitlines() if "\t" in line
    )
    assert stats_map["round_otu_keys_count"] == "2"
    assert stats_map["protected_otu_keys_ever_count"] == "3"
