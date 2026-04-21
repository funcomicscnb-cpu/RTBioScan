import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_reporting_getting_hq_pl_compiles():
    script = REPO_ROOT / "bin" / "reporting_getting_hq.pl"
    result = subprocess.run(
        ["perl", "-c", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (result.stdout + "\n" + result.stderr).strip()

