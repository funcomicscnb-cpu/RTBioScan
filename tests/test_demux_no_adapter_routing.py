import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_NF = REPO_ROOT / "main.nf"
ROUTER_BEGIN = "# S2_NO_ADAPTER_ROUTER_BEGIN"
ROUTER_END = "# S2_NO_ADAPTER_ROUTER_END"

NO_ADAPTER_VALUES = [
    "no_adapter",
    "NO_ADAPTER",
    "No_Adapter_1",
    "no_adapter_0",
    "no_adapter_01",
    "no_adapter_999",
]
ORDINARY_VALUES = [
    "sample_A",
    "no_adapterX",
    "sample_no_adapter",
    "no_adapter_1a",
    "no_adapter_",
    "no_adapter-1",
    "xno_adapter",
    "NO_ADAPTERX",
    "sample=1",
    "muestra_ñ",
]


def _production_router_function() -> str:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert text.count(ROUTER_BEGIN) == 1
    assert text.count(ROUTER_END) == 1
    block = text.split(ROUTER_BEGIN, 1)[1].split(ROUTER_END, 1)[0]
    return block.replace("\\$", "$").replace("\\\\", "\\").strip()


def _oracle_is_no_adapter(header: str) -> bool:
    for token in header.split("|"):
        if token.startswith("adapter="):
            value = token[len("adapter=") :].split(maxsplit=1)[0]
            lower = value.lower()
            return lower == "no_adapter" or re.fullmatch(r"no_adapter_[0-9]+", lower) is not None
    return False


def _records(model: str, reverse: bool) -> list[bytes]:
    values = NO_ADAPTER_VALUES + ORDINARY_VALUES
    records = []
    for index, value in enumerate(values):
        note = "|note=no_adapter" if value == "sample_A" else ""
        suffix = " runid=synthetic" if index % 2 else ""
        header = f"@read_{index:02d}|COI|{model}|barcode=bc{note}|adapter={value}{suffix}\n"
        if value == "sample_A":
            body = "no_adapter\n+no_adapter\nno_adapter\n"
        else:
            body = "ACGT\n+\nIIII\n"
        records.append((header + body).encode("utf-8"))
    if reverse:
        records.reverse()
    return records


def _run_production_router(
    tmp_path: Path,
    records: list[bytes],
    awk_executable: str,
) -> tuple[list[str], bytes]:
    annotated = tmp_path / "annotated.fastq"
    with_adapter_list = tmp_path / "with_adapter.list"
    no_adapter_fastq = tmp_path / "no_adapter.fastq"
    annotated.write_bytes(b"".join(records))

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "awk").symlink_to(awk_executable)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    shell = (
        "set -eu\n"
        + _production_router_function()
        + '\npartition_full_demux_fastq "$1" "$2" "$3"\n'
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            shell,
            "routing-test",
            str(annotated),
            str(with_adapter_list),
            str(no_adapter_fastq),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return with_adapter_list.read_text(encoding="utf-8").splitlines(), no_adapter_fastq.read_bytes()


AWK_IMPLEMENTATIONS = [pytest.param("/usr/bin/awk", id="bsd-awk")]
if shutil.which("gawk"):
    AWK_IMPLEMENTATIONS.append(pytest.param(shutil.which("gawk"), id="gnu-awk"))


@pytest.mark.parametrize("awk_executable", AWK_IMPLEMENTATIONS)
@pytest.mark.parametrize("model", ["sup", "hac"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
def test_full_demux_router_matches_independent_oracle_and_conserves_records(
    tmp_path: Path,
    awk_executable: str,
    model: str,
    reverse: bool,
) -> None:
    records = _records(model, reverse)
    expected_no_adapter = []
    expected_ordinary_headers = []
    expected_no_adapter_ids = []
    expected_ordinary_ids = []
    for record in records:
        lines = record.decode("utf-8").splitlines()
        header = lines[0]
        read_id = header[1:].split("|", 1)[0]
        if _oracle_is_no_adapter(header):
            expected_no_adapter.append(record)
            expected_no_adapter_ids.append(read_id)
        else:
            expected_ordinary_headers.append(header[1:])
            expected_ordinary_ids.append(read_id)

    ordinary_headers, no_adapter_bytes = _run_production_router(
        tmp_path, records, awk_executable
    )
    observed_no_adapter_ids = [
        line[1:].split("|", 1)[0]
        for line in no_adapter_bytes.decode("utf-8").splitlines()[::4]
    ]
    observed_ordinary_ids = [header.split("|", 1)[0] for header in ordinary_headers]

    assert ordinary_headers == expected_ordinary_headers
    assert no_adapter_bytes == b"".join(expected_no_adapter)
    assert observed_ordinary_ids == expected_ordinary_ids
    assert observed_no_adapter_ids == expected_no_adapter_ids
    assert set(observed_ordinary_ids).isdisjoint(observed_no_adapter_ids)
    assert len(records) == len(observed_ordinary_ids) + len(observed_no_adapter_ids)
    assert set(observed_ordinary_ids) | set(observed_no_adapter_ids) == {
        record.decode("utf-8").splitlines()[0][1:].split("|", 1)[0] for record in records
    }


def test_sup_and_hac_sites_use_the_shared_record_router_for_both_outputs() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert text.count("partition_full_demux_fastq \\\n") == 2
    for model in ["sup", "hac"]:
        call = re.compile(
            r"partition_full_demux_fastq \\\n[ \t]+"
            + re.escape(f"${{barcode}}_{model}_annotated_\\${{_t}}.fastq")
            + r" \\\n[ \t]+"
            + re.escape(f"${{barcode}}_{model}_annotated_with_adapter_\\${{_t}}.list")
            + r" \\\n[ \t]+"
            + re.escape(f"_tmp_noadapter_{model}_\\${{_t}}.fastq")
        )
        assert call.search(text)
    full_demux = text.split("# -- §2: Full-demux mode", 1)[1].split(
        "# -- §3: Primers-only mode", 1
    )[0]
    assert "grep -F -v no_adapter" not in full_demux
    assert "grep -F -A3 no_adapter" not in full_demux


@pytest.mark.skipif(shutil.which("samtools") is None, reason="samtools is not installed")
def test_router_list_header_shape_is_accepted_by_installed_samtools(tmp_path: Path) -> None:
    records = [
        b"@read_first|COI|hac|barcode=bc|adapter=sample_A\nACGT\n+\nIIII\n",
        b"@read_no_adapter|COI|hac|barcode=|adapter=no_adapter_1\nTGCA\n+\nIIII\n",
        b"@read_second|COI|hac|barcode=bc|adapter=no_adapterX\nGATC\n+\nIIII\n",
    ]
    ordinary_headers, _ = _run_production_router(tmp_path, records, "/usr/bin/awk")
    assert ordinary_headers == [
        "read_first|COI|hac|barcode=bc|adapter=sample_A",
        "read_second|COI|hac|barcode=bc|adapter=no_adapterX",
    ]

    result = subprocess.run(
        [
            shutil.which("samtools"),
            "faidx",
            str(tmp_path / "annotated.fastq"),
            "-r",
            str(tmp_path / "with_adapter.list"),
            "-f",
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == records[0] + records[2]
