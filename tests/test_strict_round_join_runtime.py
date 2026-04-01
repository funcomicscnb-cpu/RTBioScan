import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
NEXTFLOW = REPO_ROOT / "nextflow"
CHANNEL_UTILS = REPO_ROOT / "lib" / "ChannelUtils.groovy"


def _setup_lib(tmp_path: Path) -> None:
    """Copy lib/ChannelUtils.groovy into tmp_path/lib/ so Nextflow auto-loads it."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir(exist_ok=True)
    (lib_dir / "ChannelUtils.groovy").write_text(
        CHANNEL_UTILS.read_text(encoding="utf-8"), encoding="utf-8"
    )


def _workflow_preamble() -> str:
    return "nextflow.enable.dsl=1\n\n"


def _run_workflow_text(tmp_path: Path, workflow_text: str) -> subprocess.CompletedProcess[str]:
    _setup_lib(tmp_path)
    workflow = tmp_path / "main.nf"
    workflow.write_text(workflow_text, encoding="utf-8")
    return subprocess.run(
        [str(NEXTFLOW), "run", str(workflow)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _channel_defs(channel_rows: list[str]) -> tuple[list[str], str]:
    channel_names: list[str] = []
    channel_defs: list[str] = []
    for idx, rows in enumerate(channel_rows):
        name = f"ch_{idx}"
        channel_names.append(name)
        channel_defs.append(
            f"{name} = Channel.of(\n"
            f"{rows}"
            ")\n"
        )
    return channel_names, "".join(channel_defs)


def _strict_round_join_workflow(left_rows: str, right_rows: str) -> str:
    return (
        _workflow_preamble()
        + "ch_0 = Channel.of(\n"
        + left_rows
        + ")\n\n"
        + "ch_1 = Channel.of(\n"
        + right_rows
        + ")\n\n"
        + "ChannelUtils.strictRoundJoin(ch_0, ch_1)\n"
        + "    .map { it.join('\\t') }\n"
        + "    .view()\n"
    )

def _run_workflow(tmp_path: Path, left_rows: str, right_rows: str) -> subprocess.CompletedProcess[str]:
    return _run_workflow_text(
        tmp_path,
        _strict_round_join_workflow(left_rows, right_rows),
    )


def _strict_round_join_all_workflow(channel_rows: list[str]) -> str:
    if not channel_rows:
        raise ValueError("channel_rows must not be empty")
    channel_names, channel_defs = _channel_defs(channel_rows)
    return (
        _workflow_preamble()
        + f"{channel_defs}\n"
        + f"ChannelUtils.strictRoundJoinAll([{', '.join(channel_names)}], 'strictRoundJoinAllRuntime')\n"
        + "    .map { it.join('\\t') }\n"
        + "    .view()\n"
    )


def _run_workflow_all(tmp_path: Path, channel_rows: list[str]) -> subprocess.CompletedProcess[str]:
    return _run_workflow_text(
        tmp_path,
        _strict_round_join_all_workflow(channel_rows),
    )


def test_strict_round_join_runtime_emits_expected_joined_rows(tmp_path: Path) -> None:
    left_rows = (
        "    tuple('bc01', 'round-1', 'left-a'),\n"
        "    tuple('bc01', 'round-2', 'left-b'),\n"
    )
    right_rows = (
        "    tuple('bc01', 'round-1', 'right-a'),\n"
        "    tuple('bc01', 'round-2', 'right-b'),\n"
    )
    result = _run_workflow(
        tmp_path,
        left_rows,
        right_rows,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "bc01\tround-1\tleft-a\tright-a" in output
    assert "bc01\tround-2\tleft-b\tright-b" in output


def test_strict_round_join_runtime_fails_on_missing_key(tmp_path: Path) -> None:
    left_rows = (
        "    tuple('bc01', 'round-1', 'left-a'),\n"
        "    tuple('bc01', 'round-2', 'left-b'),\n"
    )
    right_rows = "    tuple('bc01', 'round-1', 'right-a'),\n"
    result = _run_workflow(
        tmp_path,
        left_rows,
        right_rows,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "mismatch" in output.lower()


def test_strict_round_join_runtime_fails_on_duplicate_key(tmp_path: Path) -> None:
    left_rows = (
        "    tuple('bc01', 'round-1', 'left-a'),\n"
        "    tuple('bc01', 'round-1', 'left-b'),\n"
    )
    right_rows = "    tuple('bc01', 'round-1', 'right-a'),\n"
    result = _run_workflow(
        tmp_path,
        left_rows,
        right_rows,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "round-1" in output
    assert "detected join operation duplicate emission" in output.lower()


def test_strict_round_join_runtime_fails_on_malformed_row(tmp_path: Path) -> None:
    left_rows = "    tuple('bc01'),\n"
    right_rows = "    tuple('bc01', 'round-1', 'right-a'),\n"
    result = _run_workflow(
        tmp_path,
        left_rows,
        right_rows,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "strictroundjoin requires tuples with at least 2 key fields" in output.lower()


def test_strict_round_join_runtime_fails_on_blank_key(tmp_path: Path) -> None:
    left_rows = "    tuple('   ', 'round-1', 'left-a'),\n"
    right_rows = "    tuple('bc01', 'round-1', 'right-a'),\n"
    result = _run_workflow(
        tmp_path,
        left_rows,
        right_rows,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "strictroundjoin requires non-blank barcode and round_barcode" in output.lower()


def test_strict_round_join_runtime_fails_on_blank_round_key(tmp_path: Path) -> None:
    left_rows = "    tuple('bc01', '   ', 'left-a'),\n"
    right_rows = "    tuple('bc01', 'round-1', 'right-a'),\n"
    result = _run_workflow(
        tmp_path,
        left_rows,
        right_rows,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "strictroundjoin requires non-blank barcode and round_barcode" in output.lower()


def test_strict_round_join_all_runtime_emits_expected_three_channel_payload(tmp_path: Path) -> None:
    result = _run_workflow_all(
        tmp_path,
        [
            "    tuple('bc01', 'round-1', 'left-a'),\n"
            "    tuple('bc01', 'round-2', 'left-b'),\n",
            "    tuple('bc01', 'round-1', 'mid-a'),\n"
            "    tuple('bc01', 'round-2', 'mid-b'),\n",
            "    tuple('bc01', 'round-1', 'right-a'),\n"
            "    tuple('bc01', 'round-2', 'right-b'),\n",
        ],
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "bc01\tround-1\tleft-a\tmid-a\tright-a" in output
    assert "bc01\tround-2\tleft-b\tmid-b\tright-b" in output


def test_strict_round_join_all_runtime_fails_on_missing_key(tmp_path: Path) -> None:
    result = _run_workflow_all(
        tmp_path,
        [
            "    tuple('bc01', 'round-1', 'left-a'),\n"
            "    tuple('bc01', 'round-2', 'left-b'),\n",
            "    tuple('bc01', 'round-1', 'mid-a'),\n"
            "    tuple('bc01', 'round-2', 'mid-b'),\n",
            "    tuple('bc01', 'round-1', 'right-a'),\n",
        ],
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "mismatch" in output.lower()


def test_strict_round_join_all_runtime_fails_on_duplicate_key(tmp_path: Path) -> None:
    result = _run_workflow_all(
        tmp_path,
        [
            "    tuple('bc01', 'round-1', 'left-a'),\n",
            "    tuple('bc01', 'round-1', 'mid-a'),\n"
            "    tuple('bc01', 'round-1', 'mid-b'),\n",
            "    tuple('bc01', 'round-1', 'right-a'),\n",
        ],
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "detected join operation duplicate emission" in output.lower()


def test_strict_round_join_all_runtime_accepts_channel_arrays(tmp_path: Path) -> None:
    result = _run_workflow_text(
        tmp_path,
        _workflow_preamble()
        + "ch0 = Channel.of(tuple('bc01', 'round-1', 'left'))\n"
        + "ch1 = Channel.of(tuple('bc01', 'round-1', 'right'))\n"
        + "ChannelUtils.strictRoundJoinAll(([ch0, ch1] as Object[]), 'array_inputs').map { it.join('\\t') }.view()\n",
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "bc01\tround-1\tleft\tright" in output


def test_strict_round_join_all_runtime_rejects_non_channel_iterables(tmp_path: Path) -> None:
    result = _run_workflow_text(
        tmp_path,
        _workflow_preamble()
        + "ChannelUtils.strictRoundJoinAll(['not-a-channel'], 'bad_inputs')\n",
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "strictroundjoinall requires channel-like inputs" in output.lower()
