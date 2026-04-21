import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_barcode_source_guard.sh"


def _run(state_dir: Path, round_barcode: str, read_file: Path):
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--round-barcode",
            round_barcode,
            "--read-file",
            str(read_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_round_barcode_source_guard_allows_same_round_for_same_source(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    reads_dir = tmp_path / "reads"
    reads_dir.mkdir()
    read_file = reads_dir / "round_001.pod5"
    read_file.write_text("pod5", encoding="utf-8")

    first = _run(state_dir, "round_001", read_file)
    second = _run(state_dir, "round_001", read_file)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout.strip() == str(read_file.resolve())
    assert second.stdout.strip() == str(read_file.resolve())
    assert (state_dir / "round_barcode_sources.tsv").read_text(encoding="utf-8").splitlines() == [
        f"round_001\t{read_file.resolve()}"
    ]


def test_round_barcode_source_guard_rejects_same_basename_from_different_source(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    reads_a = tmp_path / "reads_a"
    reads_b = tmp_path / "reads_b"
    reads_a.mkdir()
    reads_b.mkdir()
    read_a = reads_a / "shared_round.pod5"
    read_b = reads_b / "shared_round.pod5"
    read_a.write_text("pod5-a", encoding="utf-8")
    read_b.write_text("pod5-b", encoding="utf-8")

    first = _run(state_dir, "shared_round", read_a)
    second = _run(state_dir, "shared_round", read_b)

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "round_barcode collision for 'shared_round'" in second.stderr
    assert str(read_a.resolve()) in second.stderr
    assert str(read_b.resolve()) in second.stderr


def test_round_barcode_source_guard_allows_different_round_barcodes(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    reads_dir = tmp_path / "reads"
    reads_dir.mkdir()
    read_a = reads_dir / "round_a.pod5"
    read_b = reads_dir / "round_b.pod5"
    read_a.write_text("pod5-a", encoding="utf-8")
    read_b.write_text("pod5-b", encoding="utf-8")

    first = _run(state_dir, "round_a", read_a)
    second = _run(state_dir, "round_b", read_b)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    lines = (state_dir / "round_barcode_sources.tsv").read_text(encoding="utf-8").splitlines()
    assert lines == [
        f"round_a\t{read_a.resolve()}",
        f"round_b\t{read_b.resolve()}",
    ]


def test_round_barcode_source_guard_rejects_malformed_existing_mapping(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    state_dir.mkdir()
    (state_dir / "round_barcode_sources.tsv").write_text("broken_round\t\n", encoding="utf-8")
    read_file = tmp_path / "broken_round.pod5"
    read_file.write_text("pod5", encoding="utf-8")

    result = _run(state_dir, "broken_round", read_file)

    assert result.returncode != 0
    assert "malformed round_barcode_sources.tsv entry for 'broken_round'" in result.stderr


def test_round_barcode_source_guard_rejects_non_regular_read_file(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    read_dir = tmp_path / "reads_dir"
    read_dir.mkdir()

    result = _run(state_dir, "round_001", read_dir)

    assert result.returncode != 0
    assert f"read file is not a regular file: {read_dir}" in result.stderr


def test_round_barcode_source_guard_rejects_invalid_lock_wait(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    read_file = tmp_path / "round_001.pod5"
    read_file.write_text("pod5", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--round-barcode",
            "round_001",
            "--read-file",
            str(read_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"ROUND_BARCODE_GUARD_LOCK_WAIT": "bad"},
    )

    assert result.returncode != 0
    assert "invalid ROUND_BARCODE_GUARD_LOCK_WAIT 'bad'" in result.stderr


def test_round_barcode_source_guard_fails_fast_on_invalid_stale_lock_helper_input() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'if [ "$reclaim_status" -eq 2 ]; then' in text
    assert "ERROR: stale_lock_maybe_reclaim rejected round barcode source guard lock parameters" in text


def test_round_barcode_source_guard_reclaims_stale_lock_from_dead_pid(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    lock_dir = state_dir / ".round_barcode_sources.lockdir"
    lock_dir.mkdir(parents=True)
    (lock_dir / "meta.env").write_text("pid=999999\nhost=testhost\nstarted_epoch=1\n", encoding="utf-8")
    read_file = tmp_path / "round_001.pod5"
    read_file.write_text("pod5", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--round-barcode",
            "round_001",
            "--read-file",
            str(read_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            "ROUND_BARCODE_GUARD_LOCK_WAIT": "2",
            "ROUND_BARCODE_GUARD_STALE_TTL": "300",
            "HOSTNAME": "testhost",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "reclaiming stale round barcode source guard lock" in result.stderr.lower()


def test_round_barcode_source_guard_reclaims_stale_lock_by_age(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    lock_dir = state_dir / ".round_barcode_sources.lockdir"
    lock_dir.mkdir(parents=True)
    (lock_dir / "meta.env").write_text("started_epoch=1\n", encoding="utf-8")
    read_file = tmp_path / "round_001.pod5"
    read_file.write_text("pod5", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--round-barcode",
            "round_001",
            "--read-file",
            str(read_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            "ROUND_BARCODE_GUARD_LOCK_WAIT": "2",
            "ROUND_BARCODE_GUARD_STALE_TTL": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "reclaiming stale round barcode source guard lock (age=" in result.stderr.lower()


def test_round_barcode_source_guard_times_out_when_stale_lock_cannot_be_removed(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    lock_dir = state_dir / ".round_barcode_sources.lockdir"
    lock_dir.mkdir(parents=True)
    (lock_dir / "meta.env").write_text("pid=999999\nhost=testhost\nstarted_epoch=1\n", encoding="utf-8")
    (lock_dir / "keep.txt").write_text("busy", encoding="utf-8")
    read_file = tmp_path / "round_001.pod5"
    read_file.write_text("pod5", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--round-barcode",
            "round_001",
            "--read-file",
            str(read_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            "ROUND_BARCODE_GUARD_LOCK_WAIT": "1",
            "ROUND_BARCODE_GUARD_STALE_TTL": "300",
            "HOSTNAME": "testhost",
        },
    )

    assert result.returncode != 0
    assert "reclaiming stale round barcode source guard lock" in result.stderr.lower()
    assert "failed to remove stale round barcode source guard lock" in result.stderr.lower()
    assert "timed out acquiring round barcode source guard lock" in result.stderr.lower()


def test_round_barcode_source_guard_rejects_unsupported_round_barcode_delimiters(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    read_file = tmp_path / "round_001.pod5"
    read_file.write_text("pod5", encoding="utf-8")

    result = _run(state_dir, "round\t001", read_file)

    assert result.returncode != 0
    assert "round_barcode contains unsupported tab/newline characters" in result.stderr


def test_round_barcode_source_guard_rejects_unsupported_canonical_path_delimiters(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    read_file = tmp_path / "round\t001.pod5"
    read_file.write_text("pod5", encoding="utf-8")

    result = _run(state_dir, "round_001", read_file)

    assert result.returncode != 0
    assert "canonical read path contains unsupported tab/newline characters" in result.stderr


def test_round_barcode_source_guard_rejects_invalid_stale_ttl(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    read_file = tmp_path / "round_001.pod5"
    read_file.write_text("pod5", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--round-barcode",
            "round_001",
            "--read-file",
            str(read_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"ROUND_BARCODE_GUARD_STALE_TTL": "bad"},
    )

    assert result.returncode != 0
    assert "invalid ROUND_BARCODE_GUARD_STALE_TTL 'bad'" in result.stderr
