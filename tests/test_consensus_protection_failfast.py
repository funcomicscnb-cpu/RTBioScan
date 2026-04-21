import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_NF = REPO_ROOT / "main.nf"


def _extract_provenance_failfast_block() -> str:
    text = MAIN_NF.read_text(encoding="utf-8")
    start_marker = "# consensus provenance failfast start"
    end_marker = "# consensus provenance failfast end"
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    block = text[start:end]
    return block.replace("${baseDir}", "$BASE_DIR").strip() + "\n"


def test_consensus_protection_fails_closed_when_provenance_helper_fails(tmp_path: Path) -> None:
    base_dir = tmp_path / "base"
    bin_dir = base_dir / "bin"
    bin_dir.mkdir(parents=True)
    helper = bin_dir / "emit_consensus_round_provenance.pl"
    helper.write_text("#!/usr/bin/env perl\nexit 1;\n", encoding="utf-8")
    helper.chmod(0o755)

    script = "set -euo pipefail\nround_barcode=\"round_001\"\n" + _extract_provenance_failfast_block()

    result = subprocess.run(
        ["bash", "-lc", script],
        cwd=tmp_path,
        env={"BASE_DIR": str(base_dir)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "ERROR: failed to emit consensus round provenance" in result.stderr
