import subprocess
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "lib" / "stale_lock_utils.sh"


def _run(tmp_path: Path, meta_text: str, *, ttl: str = "300", hostname: str = "testhost", callback_body: str = 'rm -rf "$LOCK_DIR"') -> subprocess.CompletedProcess[str]:
    lock_dir = tmp_path / "lockdir"
    lock_dir.mkdir()
    lock_meta = lock_dir / "meta.env"
    lock_meta.write_text(meta_text, encoding="utf-8")
    script = f"""
set -euo pipefail
source "{SCRIPT}"
LOCK_DIR="{lock_dir}"
LOCK_META="{lock_meta}"
reclaim_cb() {{
{callback_body}
}}
set +e
stale_lock_maybe_reclaim "$LOCK_DIR" "$LOCK_META" "{hostname}" "{ttl}" "test lock" reclaim_cb 1
rc=$?
set -e
printf 'rc=%s\\n' "$rc"
"""
    return subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_stale_lock_utils_reclaims_dead_pid_lock(tmp_path: Path) -> None:
    result = _run(tmp_path, "pid=999999\nhost=testhost\nstarted_epoch=1\n")
    assert result.returncode == 0, result.stderr
    assert "rc=10" in result.stdout
    assert "reclaiming stale test lock (dead pid=999999 host=testhost)" in result.stderr.lower()


def test_stale_lock_utils_reclaims_age_based_lock(tmp_path: Path) -> None:
    result = _run(tmp_path, "started_epoch=1\n", ttl="1")
    assert result.returncode == 0, result.stderr
    assert "rc=10" in result.stdout
    assert "reclaiming stale test lock (age=" in result.stderr.lower()
    assert "warn: lock metadata:" in result.stderr.lower()


def test_stale_lock_utils_does_not_reclaim_same_host_live_pid_by_age(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        f"pid={os.getpid()}\nhost=testhost\nstarted_epoch=1\n",
        ttl="1",
    )
    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout
    assert "reclaiming stale test lock" not in result.stderr.lower()


def test_stale_lock_utils_reports_failed_reclaim(tmp_path: Path) -> None:
    callback = 'printf busy > "$LOCK_DIR/keep.txt"\nrmdir "$LOCK_DIR" 2>/dev/null || true'
    result = _run(tmp_path, "pid=999999\nhost=testhost\nstarted_epoch=1\n", callback_body=callback)
    assert result.returncode == 0, result.stderr
    assert "rc=11" in result.stdout
    assert "failed to remove stale test lock" in result.stderr.lower()


def test_stale_lock_utils_rejects_invalid_ttl(tmp_path: Path) -> None:
    result = _run(tmp_path, "started_epoch=1\n", ttl="bad")
    assert result.returncode == 0, result.stderr
    assert "rc=2" in result.stdout
    assert "invalid stale_lock_ttl_seconds 'bad'" in result.stderr
