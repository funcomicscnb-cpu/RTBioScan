import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLOT_SIG_SCRIPT = REPO_ROOT / "bin" / "plot_sig.sh"


def _check(sig_file: Path, key: str, *out_pngs: Path) -> int:
    return subprocess.run(
        ["bash", str(PLOT_SIG_SCRIPT), "check", str(sig_file), key,
         *(str(p) for p in out_pngs)],
        capture_output=True,
    ).returncode


def _commit(sig_file: Path, key: str) -> None:
    subprocess.run(
        ["bash", str(PLOT_SIG_SCRIPT), "commit", str(sig_file), key],
        check=True,
    )


def test_plot_sig_skips_when_sig_matches_and_output_exists(tmp_path: Path) -> None:
    """check exits 1 (skip) when sig file matches key and all output PNGs exist."""
    sig_file = tmp_path / "sigs" / "myplot.sig"
    sig_file.parent.mkdir()
    sig_file.write_text("k1\n", encoding="utf-8")
    png = tmp_path / "output.png"
    png.write_bytes(b"\x89PNG\r\n")  # dummy PNG

    rc = _check(sig_file, "k1", png)
    assert rc == 1, f"Expected exit 1 (skip), got {rc}"


def test_plot_sig_runs_when_sig_stale(tmp_path: Path) -> None:
    """check exits 0 (run) when sig file contains an old key."""
    sig_file = tmp_path / "myplot.sig"
    sig_file.write_text("old\n", encoding="utf-8")
    png = tmp_path / "output.png"
    png.write_bytes(b"\x89PNG\r\n")

    rc = _check(sig_file, "new", png)
    assert rc == 0, f"Expected exit 0 (run), got {rc}"


def test_plot_sig_runs_when_output_missing(tmp_path: Path) -> None:
    """check exits 0 (run) when sig matches but an output PNG is absent."""
    sig_file = tmp_path / "myplot.sig"
    sig_file.write_text("k1\n", encoding="utf-8")
    png = tmp_path / "missing.png"  # not created

    rc = _check(sig_file, "k1", png)
    assert rc == 0, f"Expected exit 0 (run), got {rc}"


def test_plot_sig_commit_writes_atomically(tmp_path: Path) -> None:
    """commit writes the key; a second commit with a different key overwrites it."""
    sig_file = tmp_path / "sigs" / "myplot.sig"

    _commit(sig_file, "first_key")
    assert sig_file.read_text(encoding="utf-8").strip() == "first_key"

    _commit(sig_file, "second_key")
    assert sig_file.read_text(encoding="utf-8").strip() == "second_key"
