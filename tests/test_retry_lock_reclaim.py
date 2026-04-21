from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "lib" / "retry.sh"


def test_retry_reclaims_dead_lock_when_hostname_differs_only_by_suffix(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "hostname").write_text("#!/bin/sh\necho testhost.local\n", encoding="utf-8")
    os.chmod(fake_bin / "hostname", 0o755)

    lock_path = tmp_path / "dorado_lock"
    lockdir = Path(f"{lock_path}.lockdir")
    lockdir.mkdir()
    (lockdir / "meta.env").write_text(
        "pid=999999\nhost=testhost.home\nstarted_epoch=1\ndesc=FAST basecalling\nout=out.sam\n",
        encoding="utf-8",
    )

    cmd = f"""
set -euo pipefail
source "{SCRIPT}"
export PATH="{fake_bin}:$PATH"
export DORADO_LOCK_PATH="{lock_path}"
export DORADO_LOCK_WAIT_SECONDS=2
export DORADO_LOCK_TTL_SECONDS=999999
dorado_basecall_retry "test" "{tmp_path / 'out.sam'}" bash -lc 'printf "ok\\n"'
"""
    result = subprocess.run(
        ["bash", "-lc", cmd],
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "reclaiming stale dorado lock (dead pid=999999 host=testhost.home)" in result.stderr.lower()
    assert (tmp_path / "out.sam").read_text(encoding="utf-8") == "ok\n"
