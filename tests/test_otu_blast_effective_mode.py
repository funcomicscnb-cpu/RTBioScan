import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "otu_blast_effective_mode.sh"


def _run(configured_mode: str, skip_rounds: str, round_index: str):
    return subprocess.run(
        ["bash", str(SCRIPT), configured_mode, skip_rounds, round_index],
        capture_output=True,
        text=True,
        check=False,
    )


def _as_map(stdout: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        out[key] = value
    return out


def test_effective_mode_none_keeps_configured_mode() -> None:
    result = _run("observe", "none", "1")
    assert result.returncode == 0, result.stderr
    data = _as_map(result.stdout)
    assert data["effective_mode"] == "observe"
    assert data["reason"] == "skip_none"


def test_effective_mode_all_forces_off() -> None:
    result = _run("enforce", "all", "2")
    assert result.returncode == 0, result.stderr
    data = _as_map(result.stdout)
    assert data["effective_mode"] == "off"
    assert data["reason"] == "skip_all_rounds"


def test_effective_mode_numeric_window() -> None:
    in_window = _run("enforce", "3", "3")
    assert in_window.returncode == 0, in_window.stderr
    in_window_data = _as_map(in_window.stdout)
    assert in_window_data["effective_mode"] == "off"
    assert in_window_data["reason"] == "within_skip_window"

    after_window = _run("enforce", "3", "4")
    assert after_window.returncode == 0, after_window.stderr
    after_window_data = _as_map(after_window.stdout)
    assert after_window_data["effective_mode"] == "enforce"
    assert after_window_data["reason"] == "after_skip_window"


def test_effective_mode_zero_is_none() -> None:
    result = _run("observe", "0", "2")
    assert result.returncode == 0, result.stderr
    data = _as_map(result.stdout)
    assert data["effective_mode"] == "observe"
    assert data["skip_rounds"] == "none"


def test_effective_mode_rejects_invalid_values() -> None:
    bad_skip = _run("observe", "abc", "1")
    assert bad_skip.returncode != 0
    assert "Invalid skip_rounds" in bad_skip.stderr

    bad_mode = _run("maybe", "none", "1")
    assert bad_mode.returncode != 0
    assert "Invalid configured_mode" in bad_mode.stderr

    bad_round = _run("observe", "none", "0")
    assert bad_round.returncode != 0
    assert "Invalid round_index" in bad_round.stderr
