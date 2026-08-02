import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "bin" / "dorado_basecaller_input_compat.sh"


def _fake_dorado(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -eu
last=""
for arg in "$@"; do
    last="$arg"
done
[ "$1" = "basecaller" ]
[ -d "$last" ]
count=$(find "$last" -maxdepth 1 -type l | wc -l | tr -d ' ')
[ "$count" = "1" ]
printf '@HD\\tVN:1.6\\n'
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_directory_mode_stages_single_input_and_cleans_up(tmp_path: Path) -> None:
    dorado = tmp_path / "dorado"
    _fake_dorado(dorado)
    model = tmp_path / "model"
    model.mkdir()
    pod5 = tmp_path / "fixture.pod5"
    pod5.write_bytes(b"pod5 fixture")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    env = dict(os.environ)
    env["TMPDIR"] = str(temp_root)

    result = subprocess.run(
        [
            str(HELPER),
            "directory",
            str(dorado),
            "basecaller",
            "-x",
            "cpu",
            str(model),
            str(pod5),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "@HD\tVN:1.6\n"
    assert list(temp_root.iterdir()) == []


def test_file_mode_executes_without_staging(tmp_path: Path) -> None:
    dorado = tmp_path / "dorado"
    dorado.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    dorado.chmod(0o755)

    result = subprocess.run(
        [str(HELPER), "file", str(dorado), "basecaller", "model", "input.pod5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["basecaller", "model", "input.pod5"]


def test_main_wires_legacy_input_and_summary_without_changing_defaults() -> None:
    main_text = (REPO_ROOT / "main.nf").read_text(encoding="utf-8")
    config_text = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    assert 'dorado_input_mode = "file"' in config_text
    assert 'dorado_summary_bin = ""' in config_text
    assert main_text.count(
        "${doradoBasecallerLauncher} basecaller -x ${params.dorado_device}"
    ) == 3
    assert main_text.count("${doradoSummaryBin} summary") == 2
    assert 'SUP_DORADO_BIN="${doradoSummaryBin}"' in main_text
