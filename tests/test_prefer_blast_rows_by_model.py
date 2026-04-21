import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "prefer_blast_rows_by_model.sh"


def _run_helper(tmp_path: Path, contents: str) -> str:
    inp = tmp_path / "input.txt"
    out = tmp_path / "output.txt"
    inp.write_text(contents, encoding="utf-8")
    subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--input",
            str(inp),
            "--output",
            str(out),
            "--policy",
            "sup_hac2sup_preferred",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.read_text(encoding="utf-8")


def test_prefers_sup_and_hac2sup_rows_for_annotated_reports(tmp_path: Path) -> None:
    output = _run_helper(
        tmp_path,
        "#header\n"
        "read1|COI|hac|adapter=a|OTUB_1-COI\tNA\n"
        "read2|COI|sup|adapter=a|OTUB_1-COI\tNA\n"
        "read3|COI|hac2sup|adapter=a|OTUB_1-COI\tNA\n",
    )
    assert "#header" in output
    assert "read2|COI|sup|adapter=a|OTUB_1-COI\tNA" in output
    assert "read3|COI|hac2sup|adapter=a|OTUB_1-COI\tNA" in output
    assert "read1|COI|hac|adapter=a|OTUB_1-COI\tNA" not in output


def test_falls_back_to_all_rows_without_preferred_models(tmp_path: Path) -> None:
    contents = (
        "read1|COI|hac|adapter=a|OTUB_1-COI\tNA\n"
        "read2|COI|hac_fixed|adapter=a|OTUB_1-COI\tNA\n"
    )
    output = _run_helper(tmp_path, contents)
    assert output == contents


def test_helper_preserves_all_rows_when_only_nonpreferred_models_exist(tmp_path: Path) -> None:
    contents = (
        "read1|COI|hac|adapter=a|OTUB_1-COI;hit;123\n"
        "read2|COI|fast|adapter=a|OTUB_2-COI;hit;456\n"
    )
    output = _run_helper(tmp_path, contents)
    assert output == contents


def test_helper_does_not_modify_input_file(tmp_path: Path) -> None:
    contents = (
        "read1|COI|sup|adapter=a|OTUB_1-COI\tNA\n"
        "read2|COI|hac|adapter=a|OTUB_1-COI\tNA\n"
    )
    inp = tmp_path / "input.txt"
    out = tmp_path / "output.txt"
    inp.write_text(contents, encoding="utf-8")
    subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--input",
            str(inp),
            "--output",
            str(out),
            "--policy",
            "sup_hac2sup_preferred",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert inp.read_text(encoding="utf-8") == contents


def test_prefers_by_model_token_from_first_field_only(tmp_path: Path) -> None:
    contents = (
        "read1|COI|hac|adapter=a|OTUB_1-COI\tcomment contains |sup| but model is hac\n"
        "read2|COI|sup|adapter=a|OTUB_1-COI\tNA\n"
    )
    output = _run_helper(tmp_path, contents)
    assert "read2|COI|sup|adapter=a|OTUB_1-COI\tNA" in output
    assert "read1|COI|hac|adapter=a|OTUB_1-COI\tcomment contains |sup| but model is hac" not in output
