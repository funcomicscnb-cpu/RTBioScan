import gzip
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "bin" / "lib" / "backup_sync.sh"


def run_helper(script: str, tmp_path: Path, extra_env: dict[str, str] | None = None) -> None:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", "-lc", f'source "{HELPER}"; set -euo pipefail; {script}'],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_sync_changed_files_copies_changed_files_and_preserves_unrelated_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "alpha.txt").write_text("one\n", encoding="utf-8")
    (dst / "stale.txt").write_text("keep\n", encoding="utf-8")

    run_helper('sync_changed_files "dst" "src/alpha.txt"', tmp_path)
    assert (dst / "alpha.txt").read_text(encoding="utf-8") == "one\n"
    assert (dst / "stale.txt").read_text(encoding="utf-8") == "keep\n"

    (src / "alpha.txt").write_text("two\n", encoding="utf-8")
    run_helper('sync_changed_files "dst" "src/alpha.txt"', tmp_path)
    assert (dst / "alpha.txt").read_text(encoding="utf-8") == "two\n"
    assert (dst / "stale.txt").read_text(encoding="utf-8") == "keep\n"


def test_publish_gzip_atomic_replaces_gzip_and_removes_uncompressed_destination(tmp_path: Path) -> None:
    src = tmp_path / "state"
    out = tmp_path / "ongoing"
    src.mkdir()
    out.mkdir()
    (src / "report.txt").write_text("first\n", encoding="utf-8")
    (out / "report.txt").write_text("stale\n", encoding="utf-8")

    run_helper('publish_gzip_atomic "state/report.txt" "ongoing/report.txt.gz"', tmp_path)
    assert not (out / "report.txt").exists()
    with gzip.open(out / "report.txt.gz", "rt", encoding="utf-8") as fh:
        assert fh.read() == "first\n"

    (src / "report.txt").write_text("second\n", encoding="utf-8")
    run_helper('publish_gzip_atomic "state/report.txt" "ongoing/report.txt.gz"', tmp_path)
    with gzip.open(out / "report.txt.gz", "rt", encoding="utf-8") as fh:
        assert fh.read() == "second\n"


def test_sync_changed_tree_updates_nested_content_and_preserves_destination_only_stale_files(tmp_path: Path) -> None:
    src = tmp_path / "ConsensusSrc"
    dst = tmp_path / "ConsensusDst"
    (src / "sampleA").mkdir(parents=True)
    (dst / "sampleB").mkdir(parents=True)
    (src / "sampleA" / "current.txt").write_text("fresh\n", encoding="utf-8")
    (dst / "sampleB" / "stale.txt").write_text("keep\n", encoding="utf-8")

    run_helper('sync_changed_tree "ConsensusSrc" "ConsensusDst"', tmp_path)
    assert (dst / "sampleA" / "current.txt").read_text(encoding="utf-8") == "fresh\n"
    assert (dst / "sampleB" / "stale.txt").read_text(encoding="utf-8") == "keep\n"

    (src / "sampleA" / "current.txt").write_text("updated\n", encoding="utf-8")
    run_helper('sync_changed_tree "ConsensusSrc" "ConsensusDst"', tmp_path)
    assert (dst / "sampleA" / "current.txt").read_text(encoding="utf-8") == "updated\n"
    assert (dst / "sampleB" / "stale.txt").read_text(encoding="utf-8") == "keep\n"


def test_backup_sync_helpers_fallback_without_rsync(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    tree = tmp_path / "tree"
    tree_dst = tmp_path / "tree_dst"
    src.mkdir()
    dst.mkdir()
    (tree / "nested").mkdir(parents=True)
    (src / "plain.txt").write_text("fallback\n", encoding="utf-8")
    (tree / "nested" / "value.txt").write_text("tree\n", encoding="utf-8")

    run_helper(
        'sync_changed_files "dst" "src/plain.txt"; sync_changed_tree "tree" "tree_dst"',
        tmp_path,
        extra_env={"BACKUP_SYNC_DISABLE_RSYNC": "1"},
    )

    assert (dst / "plain.txt").read_text(encoding="utf-8") == "fallback\n"
    assert (tree_dst / "nested" / "value.txt").read_text(encoding="utf-8") == "tree\n"
