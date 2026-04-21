import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "reporting_otu_definition.pl"


def test_reporting_otu_definition_adds_marker_to_otu_id(tmp_path: Path) -> None:
    clstr = tmp_path / "in.clstr"
    demult = tmp_path / "demult.tsv"
    barcode = "sampleA"
    round_id = "round1"
    out = tmp_path / f"{barcode}_otu_def_rpt.txt"
    out_members_round = tmp_path / f"{barcode}_otu_members_round.tsv"
    out_sizes_round = tmp_path / f"{barcode}_otu_sizes_round.tsv"

    clstr.write_text(
        ">Cluster 0\n"
        "0\t100nt, >readA|COI|sup|barcode=s1|adapter=s1... *\n"
        "1\t100nt, >readB|ITS2|hac|barcode=s1|adapter=s1... at +/99.0%\n"
        ">Cluster 1\n"
        "0\t100nt, >readC|COI|sup|barcode=s2|adapter=s2... *\n",
        encoding="utf-8",
    )
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\n"
        "readA\ts1\tsup\tsample1\tunknown\tunknown\tunknown\tunknown\n"
        "readB\ts1\thac\tsample1\tunknown\tunknown\tunknown\tunknown\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"] = "off"
    result = subprocess.run(
        ["perl", str(SCRIPT), str(clstr), str(demult), round_id, barcode, "COI", "ITS2"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    assert out_members_round.exists()
    assert out_sizes_round.exists()

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].endswith("OTU_id\tOTU_role")
    assert any("\tOTUB_0-COI\tREPRESENTATIVE" in ln for ln in lines[1:])
    assert any("\tOTUB_0-ITS2\tMEMBER" in ln for ln in lines[1:])
    assert any("\tOTUB_1-COI\tREPRESENTATIVE" in ln for ln in lines[1:])

    round_members_lines = out_members_round.read_text(encoding="utf-8").strip().splitlines()
    assert round_members_lines[0] == "otu_id\tread_id"
    assert "OTUB_0-COI\treadA" in round_members_lines[1:]
    assert "OTUB_0-ITS2\treadB" in round_members_lines[1:]
    assert "OTUB_1-COI\treadC" in round_members_lines[1:]

    round_sizes_lines = out_sizes_round.read_text(encoding="utf-8").strip().splitlines()
    assert round_sizes_lines[0] == "otu_id\tsize"
    assert "OTUB_0-COI\t1" in round_sizes_lines[1:]
    assert "OTUB_0-ITS2\t1" in round_sizes_lines[1:]
    assert "OTUB_1-COI\t1" in round_sizes_lines[1:]


def test_reporting_otu_definition_does_not_use_model_or_meta_as_marker(tmp_path: Path) -> None:
    clstr = tmp_path / "in.clstr"
    demult = tmp_path / "demult.tsv"
    barcode = "sampleA"
    round_id = "round1"
    out = tmp_path / f"{barcode}_otu_def_rpt.txt"

    clstr.write_text(
        ">Cluster 0\n"
        "0\t100nt, >readD|sup|barcode=s1|adapter=s1... *\n"
        "1\t100nt, >readE|barcode=s1|adapter=s1... at +/99.0%\n",
        encoding="utf-8",
    )
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\n"
        "readD\ts1\tsup\tsample1\tunknown\tunknown\tunknown\tunknown\n"
        "readE\ts1\thac\tsample1\tunknown\tunknown\tunknown\tunknown\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"] = "off"
    result = subprocess.run(
        ["perl", str(SCRIPT), str(clstr), str(demult), round_id, barcode, "COI", "ITS2"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert any("\tOTUB_0\tREPRESENTATIVE" in ln for ln in lines[1:])
    assert any("\tOTUB_0\tMEMBER" in ln for ln in lines[1:])
    assert not any("\tOTUB_0-sup\t" in ln for ln in lines[1:])
    assert not any("\tOTUB_0-barcode=s1\t" in ln for ln in lines[1:])
    assert "INFO: otu_marker_tokens" in result.stderr


def test_reporting_otu_definition_appends_only_whitelisted_targets(tmp_path: Path) -> None:
    clstr = tmp_path / "in.clstr"
    demult = tmp_path / "demult.tsv"
    barcode = "sampleA"
    round_id = "round1"
    out = tmp_path / f"{barcode}_otu_def_rpt.txt"

    clstr.write_text(
        ">Cluster 0\n"
        "0\t100nt, >readF|GENE_X|sup|barcode=s1|adapter=s1... *\n"
        "1\t100nt, >readG|COI|sup|barcode=s1|adapter=s1... at +/99.0%\n",
        encoding="utf-8",
    )
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\n"
        "readF\ts1\tsup\tsample1\tunknown\tunknown\tunknown\tunknown\n"
        "readG\ts1\tsup\tsample1\tunknown\tunknown\tunknown\tunknown\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"] = "off"
    result = subprocess.run(
        ["perl", str(SCRIPT), str(clstr), str(demult), round_id, barcode, "COI", "ITS2"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert any("\tOTUB_0\tREPRESENTATIVE" in ln and ln.startswith("readF\t") for ln in lines[1:])
    assert any("\tOTUB_0-COI\tMEMBER" in ln and ln.startswith("readG\t") for ln in lines[1:])
    assert "rejected=1" in result.stderr


def test_reporting_otu_definition_finds_whitelisted_target_beyond_second_token(tmp_path: Path) -> None:
    clstr = tmp_path / "in.clstr"
    demult = tmp_path / "demult.tsv"
    barcode = "sampleA"
    round_id = "round1"
    out = tmp_path / f"{barcode}_otu_def_rpt.txt"

    clstr.write_text(
        ">Cluster 0\n"
        "0\t100nt, >readH|barcode=s1|COI|sup|adapter=s1... *\n",
        encoding="utf-8",
    )
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\n"
        "readH\ts1\tsup\tsample1\tunknown\tunknown\tunknown\tunknown\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"] = "off"
    result = subprocess.run(
        ["perl", str(SCRIPT), str(clstr), str(demult), round_id, barcode, "COI", "ITS2"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert any("\tOTUB_0-COI\tREPRESENTATIVE" in ln and ln.startswith("readH\t") for ln in lines[1:])
    assert "allowed=1" in result.stderr


def test_reporting_otu_definition_with_empty_whitelist_has_zero_marker_counters(tmp_path: Path) -> None:
    clstr = tmp_path / "in.clstr"
    demult = tmp_path / "demult.tsv"
    barcode = "sampleA"
    round_id = "round1"
    out = tmp_path / f"{barcode}_otu_def_rpt.txt"

    clstr.write_text(
        ">Cluster 0\n"
        "0\t100nt, >readI|COI|sup|barcode=s1|adapter=s1... *\n",
        encoding="utf-8",
    )
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\n"
        "readI\ts1\tsup\tsample1\tunknown\tunknown\tunknown\tunknown\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"] = "off"
    result = subprocess.run(
        ["perl", str(SCRIPT), str(clstr), str(demult), round_id, barcode, "null", "NA"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert any("\tOTUB_0\tREPRESENTATIVE" in ln and ln.startswith("readI\t") for ln in lines[1:])
    assert "seen=0" in result.stderr
    assert "allowed=0" in result.stderr
    assert "rejected=0" in result.stderr


def test_reporting_otu_definition_parses_blank_suffix_member_lines(tmp_path: Path) -> None:
    clstr = tmp_path / "in.clstr"
    demult = tmp_path / "demult.tsv"
    barcode = "sampleA"
    round_id = "round1"
    out_members_round = tmp_path / f"{barcode}_otu_members_round.tsv"
    out_sizes_round = tmp_path / f"{barcode}_otu_sizes_round.tsv"

    clstr.write_text(
        ">Cluster 0\n"
        "0\t0nt, >readJ|COI|sup|barcode=s1|adapter=s1... *\n"
        "1\t0nt, >readK|COI|sup|barcode=s1|adapter=s1... \n",
        encoding="utf-8",
    )
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\n"
        "readJ\ts1\tsup\tsample1\tunknown\tunknown\tunknown\tunknown\n"
        "readK\ts1\tsup\tsample1\tunknown\tunknown\tunknown\tunknown\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"] = "off"
    result = subprocess.run(
        ["perl", str(SCRIPT), str(clstr), str(demult), round_id, barcode, "COI", "ITS2"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    round_members_lines = out_members_round.read_text(encoding="utf-8").strip().splitlines()
    assert "OTUB_0-COI\treadJ" in round_members_lines[1:]
    assert "OTUB_0-COI\treadK" in round_members_lines[1:]
    round_sizes_lines = out_sizes_round.read_text(encoding="utf-8").strip().splitlines()
    assert "OTUB_0-COI\t2" in round_sizes_lines[1:]
