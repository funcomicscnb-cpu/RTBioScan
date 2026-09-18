import os
from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_NF = REPO_ROOT / "main.nf"
RETRY_HELPER = REPO_ROOT / "bin" / "lib" / "retry.sh"
EMPTY_WARNING = (
    "WARN: Successful HAC basecalling yielded no reads; round will continue "
    "with an empty HAC contribution"
)
CONVERSION_ERROR = "ERROR: Failed to convert successful HAC basecalling output to FASTQ"
SKIP_WARNING = "WARN: No target read IDs for HAC basecalling; skipping HAC step for this POD5"
SAM_HEADER = "@HD\tVN:1.6\n"
SAM_NORMAL = SAM_HEADER + "read1\t4\t*\t0\t0\t*\t*\t0\t0\tACGT\tIIII\nread2\t4\t*\t0\t0\t*\t*\t0\t0\tTGCA\tJJJJ\n"
FASTQ_NORMAL = "@read1\nACGT\n+\nIIII\n@read2\nTGCA\n+\nJJJJ\n"
FILTERED_NORMAL = "@read1|targetA\nACGT\n+\nIIII\n@read2|targetB\nTGCA\n+\nJJJJ\n"


def _hac_shell(source: str | None = None) -> str:
    text = source if source is not None else MAIN_NF.read_text(encoding="utf-8")
    process = text.split("process hac_basecalling {", 1)[1].split(
        "process _reporting_hac_basecalling {", 1
    )[0]
    start = process.index("# Always create expected outputs")
    end_anchor = "fi  # SKIP_HAC"
    end = process.index(end_anchor, start) + len(end_anchor)
    return process[start:end] + "\n"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _render_shell(source: str, fixture: Path, *, wait_seconds: int = 5) -> str:
    substitutions = {
        "${barcode}": "bc01",
        "${target_reads_list}": str(fixture / "targets.list"),
        "${read_file}": str(fixture / "reads.pod5"),
        "${ongoingStateDir}": str(fixture / "state"),
        "${params.lock_wait_seconds}": "1",
        "${params.file_wait_minutes * 60}": str(wait_seconds),
        "${params.file_wait_minutes}": "0",
        "${baseDir}": str(fixture / "base"),
        "${doradoBasecallerLauncher}": "/bin/true",
        "${params.dorado_device}": "cpu",
        "${doradoHacBasecallerArgs}": "",
        "${params.min_quality_score}": "10",
        "${doradoHacModel}": "model",
        "${task.cpus}": "1",
        "${params.targets}": "targetA|targetB",
        "${params.min_read_lengths}": "1|1",
        "${params.max_read_lengths}": "100|100",
    }
    for old, new in substitutions.items():
        source = source.replace(old, new)
    source = source.replace('round_barcode="${round_barcode}"', 'round_barcode="round1"')
    source = source.replace("\\$", "$")
    source = source.replace("\\\\d", "\\d").replace("\\\\s", "\\s").replace("\\\\b", "\\b")
    return source


def _prepare_fixture(path: Path) -> tuple[Path, Path]:
    work = path / "work"
    work.mkdir(parents=True)
    (path / "state" / "_state").mkdir(parents=True)
    (path / "base" / "bin").mkdir(parents=True)
    (path / "reads.pod5").write_bytes(b"pod5 fixture")
    (path / "targets.list").write_text(
        "read1|targetA\nread2|targetB\n", encoding="utf-8"
    )
    _write_executable(
        path / "base" / "bin" / "with_dorado_lock.sh",
        "#!/bin/sh\nexit 99\n",
    )
    (path / "base" / "bin" / "fastq_add_annotations2ids.pl").write_text(
        r'''use strict;
my ($targets, $fastq) = @ARGV;
open my $tfh, '<', $targets or die $!;
my %annotation;
while (<$tfh>) { chomp; my ($id) = split /\|/; $annotation{$id} = $_; }
close $tfh;
open my $qfh, '<', $fastq or die $!;
my $emit = 0;
while (<$qfh>) {
    if (/^\@(\S+)/) {
        my $id = $1;
        $emit = exists $annotation{$id};
        s/^\@\Q$id\E/\@$annotation{$id}/ if $emit;
    }
    print if $emit;
}
close $qfh;
''',
        encoding="utf-8",
    )
    shim = path / "shims"
    shim.mkdir()
    _write_executable(
        shim / "samtools",
        """#!/bin/sh
case "${SAMTOOLS_MODE:-header}" in
  header) exit 0 ;;
  normal)
    printf '@read1\\nACGT\\n+\\nIIII\\n@read2\\nTGCA\\n+\\nJJJJ\\n'
    exit 0
    ;;
  fail)
    printf '@partial\\n'
    exit 23
    ;;
esac
exit 64
""",
    )
    _write_executable(
        shim / "seqkit",
        """#!/bin/sh
last=
for arg in "$@"; do last=$arg; done
cat "$last"
""",
    )
    _write_executable(
        shim / "pod5",
        """#!/bin/sh
case "${POD5_MODE:-unknown}" in
  zero) printf '0 reads\\n' ;;
  nonzero) printf '2 reads\\n' ;;
  unknown) exit 1 ;;
esac
""",
    )
    _write_executable(shim / "sleep", "#!/bin/sh\nexit 0\n")
    return work, shim


def _run_hac(
    tmp_path: Path,
    *,
    source: str | None = None,
    targets: str | None = None,
    dorado_mode: str = "header",
    samtools_mode: str = "header",
    pod5_mode: str = "unknown",
    readable: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    work, shim = _prepare_fixture(tmp_path)
    if targets is not None:
        (tmp_path / "targets.list").write_text(targets, encoding="utf-8")
    if not readable:
        (tmp_path / "reads.pod5").unlink()
    body = _render_shell(_hac_shell(source), tmp_path)
    script = tmp_path / "hac.sh"
    prologue = f"""#!/bin/bash
set -euo pipefail
shopt -s nullglob
export LC_ALL=C
DORADO_LOCK={tmp_path / 'dorado.lock'}
DORADO_LOCK_WAIT=1
round_barcode=round1
dorado_basecall_retry() {{
  printf 'call\\n' >> {tmp_path / 'dorado.calls'}
  case "${{DORADO_MODE:-header}}" in
    failure) return 1 ;;
    header|below) printf '@HD\\tVN:1.6\\n' > "$2"; return 0 ;;
    normal)
      printf '@HD\\tVN:1.6\\nread1\\t4\\t*\\t0\\t0\\t*\\t*\\t0\\t0\\tACGT\\tIIII\\nread2\\t4\\t*\\t0\\t0\\t*\\t*\\t0\\t0\\tTGCA\\tJJJJ\\n' > "$2"
      return 0
      ;;
  esac
  return 70
}}
"""
    script.write_text(prologue + body, encoding="utf-8")
    syntax = subprocess.run(
        ["/bin/bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr
    env = {
        **os.environ,
        "PATH": f"{shim}:/usr/bin:/bin",
        "DORADO_MODE": dorado_mode,
        "SAMTOOLS_MODE": samtools_mode,
        "POD5_MODE": pod5_mode,
    }
    result = subprocess.run(
        ["/bin/bash", str(script)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, work


def _calls(tmp_path: Path) -> int:
    path = tmp_path / "dorado.calls"
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def _assert_required_outputs(work: Path) -> None:
    for name in ("bc01_hac.sam", "bc01_hac.fastq", "bc01_hac_filtered.fastq"):
        assert (work / name).is_file(), name
    assert not (work / "ROUND_FAILED.txt").exists()


def test_a_empty_target_list_preserves_skip_contract(tmp_path: Path) -> None:
    result, work = _run_hac(tmp_path, targets="")
    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == 0
    assert result.stderr.count(SKIP_WARNING) == 1
    assert EMPTY_WARNING not in result.stderr
    _assert_required_outputs(work)
    assert all((work / name).stat().st_size == 0 for name in (
        "bc01_hac.sam", "bc01_hac.fastq", "bc01_hac_filtered.fastq"
    ))


@pytest.mark.parametrize("dorado_mode", ["header", "below"])
def test_b_c_successful_zero_record_hac_is_valid(tmp_path: Path, dorado_mode: str) -> None:
    result, work = _run_hac(tmp_path, dorado_mode=dorado_mode)
    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == 1
    assert result.stderr.count(EMPTY_WARNING) == 1
    assert CONVERSION_ERROR not in result.stderr
    _assert_required_outputs(work)
    assert (work / "bc01_hac.sam").read_text(encoding="utf-8") == SAM_HEADER
    assert (work / "bc01_hac.fastq").stat().st_size == 0
    assert (work / "bc01_hac_filtered.fastq").stat().st_size == 0
    assert not (work / "bc01_read_names_hq.list").exists()
    assert not (work / "bc01_hac_annotated.fastq").exists()
    assert "Removing bc01_hac_annotated.fastq" in result.stderr


def test_d_nonempty_hac_outputs_are_exact_and_warning_free(tmp_path: Path) -> None:
    result, work = _run_hac(
        tmp_path, dorado_mode="normal", samtools_mode="normal", pod5_mode="nonzero"
    )
    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == 1
    assert EMPTY_WARNING not in result.stderr
    _assert_required_outputs(work)
    assert (work / "bc01_hac.sam").read_text(encoding="utf-8") == SAM_NORMAL
    assert (work / "bc01_hac.fastq").read_text(encoding="utf-8") == FASTQ_NORMAL
    assert (work / "bc01_hac_filtered.fastq").read_text(encoding="utf-8") == FILTERED_NORMAL


def test_e_retry_failure_remains_fatal(tmp_path: Path) -> None:
    result, work = _run_hac(tmp_path, dorado_mode="failure")
    assert result.returncode != 0
    assert _calls(tmp_path) == 1
    assert EMPTY_WARNING not in result.stderr
    assert (work / "bc01_hac.fastq").stat().st_size == 0
    assert not (work / "ROUND_FAILED.txt").exists()


def _run_retry_helper(tmp_path: Path, helper_text: str) -> subprocess.CompletedProcess[str]:
    helper = tmp_path / "retry.sh"
    helper.write_text(helper_text, encoding="utf-8")
    command = tmp_path / "zero-output"
    _write_executable(
        command,
        f"#!/bin/sh\nprintf 'call\\n' >> {tmp_path / 'legacy.calls'}\nexit 0\n",
    )
    harness = tmp_path / "legacy.sh"
    harness.write_text(
        f"""#!/bin/bash
set -e
source {helper}
export DORADO_RETRY_ATTEMPTS=3
export DORADO_RETRY_SLEEP_SECONDS=0
dorado_basecall_retry "legacy HAC" {tmp_path / 'legacy.sam'} {command}
""",
        encoding="utf-8",
    )
    return subprocess.run(
        ["/bin/bash", str(harness)], capture_output=True, text=True, check=False
    )


def test_f_real_retry_helper_rejects_zero_byte_success(tmp_path: Path) -> None:
    result = _run_retry_helper(tmp_path, RETRY_HELPER.read_text(encoding="utf-8"))
    assert result.returncode != 0
    assert (tmp_path / "legacy.calls").read_text(encoding="utf-8").count("call\n") == 3
    assert "failed after 3 attempts" in result.stderr
    assert not (tmp_path / "legacy.sam").exists()


def test_g_samtools_failure_is_fatal_and_partial_output_cannot_mask_it(tmp_path: Path) -> None:
    result, work = _run_hac(tmp_path, samtools_mode="fail")
    assert result.returncode != 0
    assert _calls(tmp_path) == 1
    assert result.stderr.count(CONVERSION_ERROR) == 1
    assert EMPTY_WARNING not in result.stderr
    assert (work / "bc01_hac.fastq").read_text(encoding="utf-8") == "@partial\n"
    assert not (work / "ROUND_FAILED.txt").exists()


def test_h_zero_read_pod5_precheck_remains_fatal(tmp_path: Path) -> None:
    result, work = _run_hac(tmp_path, pod5_mode="zero")
    assert result.returncode != 0
    assert _calls(tmp_path) == 0
    assert "ERROR: POD5 contains 0 reads; failing this run" in result.stderr
    assert EMPTY_WARNING not in result.stderr
    assert not (work / "ROUND_FAILED.txt").exists()


def test_i_readability_timeout_remains_fatal(tmp_path: Path) -> None:
    result, work = _run_hac(tmp_path, readable=False)
    assert result.returncode != 0
    assert _calls(tmp_path) == 0
    assert "ERROR: Timed out waiting 0 minute(s)" in result.stderr
    assert EMPTY_WARNING not in result.stderr
    assert not (work / "ROUND_FAILED.txt").exists()


def test_header_only_hac_is_accepted_by_existing_reporting_consumer(tmp_path: Path) -> None:
    summary = tmp_path / "round.tsv"
    summary.write_text(
        "input_filename\tread_id\tsequence_length_template\tmean_qscore_template\n",
        encoding="utf-8",
    )
    sam = tmp_path / "round.sam"
    sam.write_text(SAM_HEADER, encoding="utf-8")
    on_target = tmp_path / "on_target.tsv"
    on_target.write_text("read_id\tqc_filter\n", encoding="utf-8")
    shim = tmp_path / "shims"
    shim.mkdir()
    _write_executable(shim / "samtools", "#!/bin/sh\nexit 0\n")
    result = subprocess.run(
        [
            "perl",
            str(REPO_ROOT / "bin" / "reporting_getting_hq.pl"),
            str(summary),
            str(sam),
            str(on_target),
            "1",
            "100",
            "10",
            "bc01",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{shim}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bc01_read_info_rpt.txt").read_text(encoding="utf-8") == (
        "read_id\tqc_filter\thac_length\thac_mean_qscore\n"
    )


def _replace_once(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1, (old, source.count(old))
    return source.replace(old, new, 1)


@pytest.mark.parametrize(
    ("name", "mutate", "scenario", "killed_by"),
    [
        (
            "empty_exit",
            lambda s: _replace_once(s, EMPTY_WARNING + '" 1>&2', 'ERROR: empty HAC" 1>&2\n\t\t\t\texit 1'),
            {"dorado_mode": "header"},
            "empty must succeed",
        ),
        (
            "dorado_failure_nonfatal",
            lambda s: _replace_once(s, "# Hard fail: if HAC basecalling fails, continuing would silently reduce downstream assignments.\n\t\t\t\t\texit 1", "# mutant\n\t\t\t\t\tsuccess=1"),
            {"dorado_mode": "failure"},
            "Dorado failure must fail",
        ),
        (
            "masked_samtools",
            lambda s: _replace_once(s, "if ! samtools fastq -@ ${task.cpus} ${barcode}_hac.sam > ${barcode}_hac.fastq; then\n\t\t\techo \"ERROR: Failed to convert successful HAC basecalling output to FASTQ\" 1>&2\n\t\t\texit 1\n\t\tfi", "samtools fastq -@ ${task.cpus} ${barcode}_hac.sam > ${barcode}_hac.fastq || : > ${barcode}_hac.fastq"),
            {"samtools_mode": "fail"},
            "conversion failure must fail",
        ),
        (
            "warning_dropped",
            lambda s: _replace_once(s, 'echo "' + EMPTY_WARNING + '" 1>&2', ": # warning dropped"),
            {"dorado_mode": "header"},
            "empty warning required",
        ),
        (
            "failed_marker_created",
            lambda s: _replace_once(s, 'echo "' + EMPTY_WARNING + '" 1>&2', 'echo "' + EMPTY_WARNING + '" 1>&2\n\t\t\t\ttouch ROUND_FAILED.txt'),
            {"dorado_mode": "header"},
            "HAC must not create failed marker",
        ),
        (
            "required_output_missing",
            lambda s: _replace_once(
                s,
                ": > ${barcode}_hac.fastq\n\t\t: > ${barcode}_hac_filtered.fastq\n\t\tSKIP_HAC=0",
                ": > ${barcode}_hac.fastq\n\t\t: # filtered output omitted\n\t\tSKIP_HAC=0",
            ),
            {"targets": ""},
            "all outputs required on skip",
        ),
        (
            "empty_targets_fail",
            lambda s: _replace_once(s, "SKIP_HAC=1", "exit 1"),
            {"targets": ""},
            "empty target list must succeed",
        ),
        (
            "nonempty_bytes_changed",
            lambda s: _replace_once(s, ">> ${barcode}_hac_filtered.fastq", "> ${barcode}_hac_filtered.fastq"),
            {"dorado_mode": "normal", "samtools_mode": "normal"},
            "nonempty filtered bytes changed",
        ),
        (
            "warning_on_nonempty",
            lambda s: _replace_once(s, "perl ${baseDir}/bin/fastq_add_annotations2ids.pl", 'echo "' + EMPTY_WARNING + '" 1>&2\n\tperl ${baseDir}/bin/fastq_add_annotations2ids.pl'),
            {"dorado_mode": "normal", "samtools_mode": "normal"},
            "normal run must not warn",
        ),
    ],
)
def test_production_mutants_are_semantically_killed(
    tmp_path: Path, name: str, mutate, scenario: dict[str, str], killed_by: str
) -> None:
    mutant = mutate(MAIN_NF.read_text(encoding="utf-8"))
    result, work = _run_hac(tmp_path, source=mutant, **scenario)
    if name == "empty_exit":
        killed = result.returncode != 0
    elif name in {"dorado_failure_nonfatal", "masked_samtools"}:
        killed = result.returncode == 0
    elif name == "warning_dropped":
        killed = result.stderr.count(EMPTY_WARNING) != 1
    elif name == "failed_marker_created":
        killed = (work / "ROUND_FAILED.txt").exists()
    elif name == "required_output_missing":
        killed = not (work / "bc01_hac_filtered.fastq").exists()
    elif name == "empty_targets_fail":
        killed = result.returncode != 0
    elif name == "nonempty_bytes_changed":
        killed = (work / "bc01_hac_filtered.fastq").read_text(encoding="utf-8") != FILTERED_NORMAL
    else:
        killed = EMPTY_WARNING in result.stderr
    assert killed, f"surviving mutant {name}: {killed_by}"


def test_retry_helper_zero_byte_acceptance_mutant_is_semantically_killed(tmp_path: Path) -> None:
    source = RETRY_HELPER.read_text(encoding="utf-8")
    mutant = _replace_once(source, '[ "$rc" -eq 0 ] && [ -s "$tmp" ]', '[ "$rc" -eq 0 ] && [ -f "$tmp" ]')
    result = _run_retry_helper(tmp_path, mutant)
    assert result.returncode == 0
    assert (tmp_path / "legacy.calls").read_text(encoding="utf-8").count("call\n") == 1
    assert (tmp_path / "legacy.sam").is_file()
    assert (tmp_path / "legacy.sam").stat().st_size == 0
