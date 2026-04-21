import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = REPO_ROOT / "bin" / "sync_dir_atomic.sh"


def test_sync_dir_atomic_mirrors_and_removes_stale_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "keep.txt").write_text("new\n", encoding="utf-8")
    (src / ".hidden").write_text("hidden\n", encoding="utf-8")
    (src / "nested").mkdir()
    (src / "nested" / "inner.txt").write_text("inner\n", encoding="utf-8")

    dst.mkdir()
    (dst / "old.txt").write_text("old\n", encoding="utf-8")

    subprocess.run(["bash", str(SYNC_SCRIPT), str(src), str(dst)], check=True)

    assert (dst / "keep.txt").read_text(encoding="utf-8") == "new\n"
    assert (dst / ".hidden").read_text(encoding="utf-8") == "hidden\n"
    assert (dst / "nested" / "inner.txt").read_text(encoding="utf-8") == "inner\n"
    assert not (dst / "old.txt").exists()


def test_sync_dir_atomic_falls_back_when_rsync_fails(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "keep.txt").write_text("fallback\n", encoding="utf-8")
    dst.mkdir()
    (dst / "old.txt").write_text("old\n", encoding="utf-8")

    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    rsync_stub = stub_bin / "rsync"
    rsync_stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    rsync_stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}:/usr/bin:/bin"
    cp = subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(src), str(dst)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert cp.returncode == 0, cp.stderr
    assert "falling back to cp -Rp" in cp.stderr
    assert (dst / "keep.txt").read_text(encoding="utf-8") == "fallback\n"
    assert not (dst / "old.txt").exists()


def test_sync_dir_atomic_fails_when_source_missing(tmp_path: Path) -> None:
    src = tmp_path / "missing"
    dst = tmp_path / "dst"
    cp = subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(src), str(dst)],
        capture_output=True,
        text=True,
    )
    assert cp.returncode != 0
    assert "source directory does not exist" in cp.stderr
