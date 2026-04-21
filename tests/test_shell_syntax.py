"""Syntax-check all shell scripts under bin/ and bin/lib/ using bash -n.

Catches bash syntax errors before they reach production.  shellcheck is used
in addition when it is available on the PATH.

Each script is checked as a separate pytest test so failures pin-point the
exact file.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
MAIN_NF = REPO_ROOT / "main.nf"

# Collect all .sh files under bin/ (including bin/lib/).
SHELL_SCRIPTS = sorted(BIN_DIR.rglob("*.sh"))


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_bash_syntax(script: Path) -> None:
    """bash -n must exit 0 for every shell script."""
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"bash -n reported syntax error in {script.relative_to(REPO_ROOT)}:\n{result.stderr}"
    )


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shellcheck(script: Path) -> None:
    """shellcheck must exit 0 for every shell script (skipped when shellcheck is absent)."""
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not found in PATH")
    result = subprocess.run(
        ["shellcheck", "-S", "error", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck reported errors in {script.relative_to(REPO_ROOT)}:\n{result.stdout}\n{result.stderr}"
    )


def test_dorado_basecalling_paths_use_lock_helper() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert text.count("with_dorado_lock.sh") >= 3
    assert "fast_on_target_detection" in text
    assert "hac_basecalling" in text
    assert "blast_OTU_pretax" in text


def test_serve_report_helper_usage() -> None:
    script = BIN_DIR / "serve_report.sh"
    assert script.exists()
    result = subprocess.run(
        ["bash", str(script), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "--wait-for-report" in result.stdout


def test_report_rebuild_helper_usage() -> None:
    script = BIN_DIR / "report_rebuild.sh"
    assert script.exists()
    result = subprocess.run(
        ["bash", str(script), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Usage: report_rebuild.sh" in result.stdout
