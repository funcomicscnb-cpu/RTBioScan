import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FULL_DEMUX_START = "# -- §2: Full-demux mode (barcode + primer cutadapt on rolling SUP + HAC reads) --"
PRIMERS_ONLY_START = "# -- §3: Primers-only mode (primer-only cutadapt on rolling SUP + HAC reads) --"
NO_DEMUX_START = "# -- §4: No-demux / demux-off mode (FASTQ tag rewrite + accumulate) --"
MARKER_LOOP_START = 'for _i in "\\${!_TARGETS[@]}"; do'
MINIMUM_BINDING = '_ml="\\${_MIN_LENS[\\$_i]}"'


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        sibling = Path(sys.executable).parent / name
        if sibling.is_file() and os.access(sibling, os.X_OK):
            executable = str(sibling)
    if executable is None:
        pytest.skip(f"required RTBioScan executable unavailable: {name}")
    return executable


def _resolved_params(nextflow: str, *args: str) -> Dict[str, str]:
    result = subprocess.run(
        [nextflow, "config", *args, "-flat"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    params = {}
    for line in result.stdout.splitlines():
        if not line.startswith("params.") or " = " not in line:
            continue
        name, value = line.split(" = ", 1)
        params[name[len("params."):]] = value.strip("'\"")
    return params


def _continued_commands(source: str, executable: str) -> List[str]:
    lines = source.splitlines()
    commands = []
    index = 0
    while index < len(lines):
        line = lines[index].lstrip()
        if not line.startswith(executable + " "):
            index += 1
            continue
        parts = [line]
        while parts[-1].rstrip().endswith("\\"):
            index += 1
            assert index < len(lines), f"unterminated {executable} command"
            parts.append(lines[index].strip())
        commands.append(" ".join(part.rstrip("\\").strip() for part in parts))
        index += 1
    return commands


def _bounded_section(source: str, start: str, end: str) -> str:
    assert source.count(start) == 1, f"expected one start marker: {start}"
    assert source.count(end) == 1, f"expected one end marker: {end}"
    before, remainder = source.split(start, 1)
    section, after = remainder.split(end, 1)
    assert before or after
    return section


def _marker_loops(section: str) -> List[str]:
    lines = section.splitlines()
    loops = []
    for start_index, line in enumerate(lines):
        if line.strip() != MARKER_LOOP_START:
            continue
        previous_index = start_index - 1
        while previous_index >= 0 and not lines[previous_index].strip():
            previous_index -= 1
        assert previous_index >= 0
        assert lines[previous_index].strip() == "load_demux_target_arrays"

        end_index = start_index + 1
        while end_index < len(lines) and lines[end_index].strip() != "done":
            end_index += 1
        assert end_index < len(lines), "unterminated marker loop"
        loops.append("\n".join(lines[start_index:end_index + 1]))
    assert len(loops) == 2
    return loops


def _assert_production_minimum_enforcement(main_source: str) -> None:
    parameter_load = '_p_min_read_lengths="${params.min_read_lengths}"'
    minimum_array_load = "IFS='|' read -ra _MIN_LENS <<< \"\\$_p_min_read_lengths\""
    loader = _bounded_section(
        main_source,
        "load_demux_target_arrays() {",
        "build_track_adapter_marker_map() {",
    )
    assert main_source.count(parameter_load) == 2
    assert loader.count(parameter_load) == 1
    assert loader.count(minimum_array_load) == 1

    full_demux = _bounded_section(main_source, FULL_DEMUX_START, PRIMERS_ONLY_START)
    primers_only = _bounded_section(main_source, PRIMERS_ONLY_START, NO_DEMUX_START)
    loops = _marker_loops(full_demux) + _marker_loops(primers_only)

    for loop, expected_command_count in zip(loops, (2, 2, 1, 1)):
        commands = _continued_commands(loop, "cutadapt")
        assert loop.count(MINIMUM_BINDING) == 1
        assert len(commands) == expected_command_count
        assert loop.index(MINIMUM_BINDING) < loop.index("cutadapt ")
        for command in commands:
            assert '-m "\\${_ml}"' in command
            assert command.count(" -m ") == 1
            assert "--minimum-length" not in command


def _filter_and_trim(
    tmp_path: Path,
    seqkit: str,
    cutadapt: str,
    marker: str,
    minimum: int,
    maximum: int,
) -> Tuple[List[int], List[int]]:
    adapter = "ACGTTGCAACGTTGCAACGT"
    lengths = [minimum - 1, minimum, minimum + len(adapter) - 1,
               minimum + len(adapter), maximum, maximum + 1]
    input_fastq = tmp_path / f"{marker}.fastq"
    adapters_fasta = tmp_path / f"{marker}_adapters.fa"
    prefiltered_fastq = tmp_path / f"{marker}_prefiltered.fastq"
    trimmed_fastq = tmp_path / f"{marker}_trimmed.fastq"

    adapters_fasta.write_text(f">sample1\n{adapter}\n", encoding="utf-8")
    input_fastq.write_text(
        "".join(
            f"@r{length}|{marker}\n{adapter}{'G' * (length - len(adapter))}\n+\n{'I' * length}\n"
            for length in lengths
        ),
        encoding="utf-8",
    )
    with prefiltered_fastq.open("w", encoding="utf-8") as output:
        subprocess.run(
            [seqkit, "seq", "-j", "1", "-g", "-M", str(maximum),
             "-m", str(minimum), str(input_fastq)],
            check=True,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
        )
    subprocess.run(
        [cutadapt, "-g", f"file:{adapters_fasta}", "-j", "1", "--action=trim",
         "--rc", "-e", "0.1", "-m", str(minimum), "-o", str(trimmed_fastq),
         str(prefiltered_fastq)],
        check=True,
        capture_output=True,
        text=True,
    )

    def retained_lengths(path: Path) -> List[int]:
        headers = path.read_text(encoding="utf-8").splitlines()[::4]
        return [int(header.split("|", 1)[0][2:]) for header in headers]

    return retained_lengths(prefiltered_fastq), retained_lengths(trimmed_fastq)


def test_production_demultiplex_commands_enforce_marker_minimum_after_trim() -> None:
    main_source = (REPO_ROOT / "main.nf").read_text(encoding="utf-8")
    _assert_production_minimum_enforcement(main_source)


def test_broad_its2_profile_filter_and_post_trim_boundaries(tmp_path: Path) -> None:
    nextflow = _required_executable("nextflow")
    seqkit = _required_executable("seqkit")
    cutadapt = _required_executable("cutadapt")
    defaults = _resolved_params(nextflow)
    broad = _resolved_params(nextflow, "-profile", "broad_its2")

    assert defaults["targets"] == broad["targets"] == "COI|ITS2"
    assert defaults["min_read_lengths"] == "350|286"
    assert defaults["max_read_lengths"] == "532|360"
    assert broad["min_read_lengths"] == "350|200"
    assert broad["max_read_lengths"] == "532|800"

    markers = broad["targets"].split("|")
    minimums = [int(value) for value in broad["min_read_lengths"].split("|")]
    maximums = [int(value) for value in broad["max_read_lengths"].split("|")]
    assert len(markers) == len(minimums) == len(maximums)
    boundaries = dict(zip(markers, zip(minimums, maximums)))
    assert boundaries["COI"] == (350, 532) == (
        int(defaults["min_read_lengths"].split("|")[0]),
        int(defaults["max_read_lengths"].split("|")[0]),
    )

    for marker in ("COI", "ITS2"):
        minimum, maximum = boundaries[marker]
        before_trim, after_trim = _filter_and_trim(
            tmp_path, seqkit, cutadapt, marker, minimum, maximum
        )
        assert before_trim == [minimum, minimum + 19, minimum + 20, maximum]
        assert after_trim == [minimum + 20, maximum]
