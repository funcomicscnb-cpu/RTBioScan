import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "blast_otu_pretax.sh"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_fake_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()

    _write_executable(
        fake_bin / "seqkit",
        """#!/usr/bin/env python3
import sys
from pathlib import Path

args = sys.argv[1:]
if not args:
    raise SystemExit(2)
if args[0] == "grep":
    pattern = args[args.index("-p") + 1].replace("\\\\|", "|")
    fasta_path = Path(args[-1])
    out = []
    record = []
    keep = False
    for line in fasta_path.read_text(encoding="utf-8").splitlines(True):
        if line.startswith(">"):
            if record and keep:
                out.extend(record)
            record = [line]
            keep = pattern in line
        else:
            record.append(line)
    if record and keep:
        out.extend(record)
    sys.stdout.write("".join(out))
elif args[0] == "sort":
    fasta_path = Path(args[-1])
    records = []
    header = None
    seq = []
    for line in fasta_path.read_text(encoding="utf-8").splitlines(True):
        if line.startswith(">"):
            if header is not None:
                records.append((header, "".join(seq)))
            header = line
            seq = []
        else:
            seq.append(line)
    if header is not None:
        records.append((header, "".join(seq)))
    records.sort(key=lambda rec: rec[0])
    for header, seq_text in records:
        sys.stdout.write(header)
        sys.stdout.write(seq_text)
else:
    raise SystemExit(2)
""",
    )
    _write_executable(
        fake_bin / "blastn",
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
query = Path(args[args.index("-query") + 1])
threads = args[args.index("-num_threads") + 1]
blast_log = os.environ.get("BLAST_LOG")
if blast_log:
    with open(blast_log, "a", encoding="utf-8") as fh:
        fh.write(f"{query.name}\\t{threads}\\n")
for line in query.read_text(encoding="utf-8").splitlines():
    if line.startswith(">"):
        qid = line[1:].split()[0]
        print(f"{qid},123,1e-20,100,99.0")
""",
    )
    _write_executable(
        fake_bin / "taxonkit",
        """#!/usr/bin/env python3
import sys
from pathlib import Path

args = sys.argv[1:]
if not args:
    raise SystemExit(2)
path = None
if "-t" in args:
    path = Path(args[args.index("-t") + 1])
elif args[-1] not in {"lineage", "reformat"}:
    path = Path(args[-1])
if path is not None and path.exists():
    sys.stdout.write(path.read_text(encoding="utf-8"))
""",
    )

    helper_base = tmp_path / "helper-base"
    helper_base.mkdir()
    os.symlink(REPO_ROOT / "bin", helper_base / "bin")
    (helper_base / "memtax1.txt").write_text("123\tGenusA\tFamilyA\tOrderA\t123\tspecies\n", encoding="utf-8")
    (helper_base / "memtax2.txt").write_text("123\tGenusB\tFamilyB\tOrderB\t123\tspecies\n", encoding="utf-8")

    db_root = tmp_path / "db-root"
    db_root.mkdir()
    (db_root / "coi_db").write_text("coi\n", encoding="utf-8")
    (db_root / "its_db").write_text("its\n", encoding="utf-8")
    taxdb_dir = tmp_path / "taxdb"
    taxdb_dir.mkdir()
    (taxdb_dir / "nodes.dmp").write_text("nodes\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return env, helper_base, db_root, taxdb_dir


def _run_helper(
    run_dir: Path,
    threads: int,
    fasta_text: str,
    targets: str = "COI|ITS2",
    blast_dbs: str = "coi_db|its_db",
    id_family: str = "92|93",
    id_genus: str = "95|96",
    id_spec: str = "98|99",
    memtax: str = "memtax1.txt|memtax2.txt",
) -> subprocess.CompletedProcess[str]:
    env, helper_base, db_root, taxdb_dir = _prepare_fake_environment(run_dir)
    env["BLAST_LOG"] = str(run_dir / "blast.log")
    fasta_path = run_dir / "reads.fasta"
    fasta_path.write_text(fasta_text, encoding="utf-8")
    round_dir = run_dir / "round"
    round_dir.mkdir()

    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "RTBioScan",
            str(fasta_path),
            str(run_dir / "state"),
            str(threads),
            str(helper_base),
            f"{db_root}/",
            str(taxdb_dir),
            str(round_dir),
            targets,
            blast_dbs,
            id_family,
            id_genus,
            id_spec,
            memtax,
            "11",
            "50",
        ],
        cwd=run_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_blast_otu_pretax_helper_produces_joined_reports(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        2,
        ">read1|COI|hac|barcode=bc1|adapter=a1\nACGT\n"
        ">read2|ITS2|hac|barcode=bc1|adapter=a2\nTGCA\n",
    )

    assert result.returncode == 0, result.stderr
    joined_preblast = (tmp_path / "RTBioScan_preblastreport_join.txt").read_text(encoding="utf-8")
    joined_blast = (tmp_path / "RTBioScan_blastreport_join.txt").read_text(encoding="utf-8")
    round_blast = (tmp_path / "RTBioScan_blastreport_round.txt").read_text(encoding="utf-8")

    assert "read1|COI|hac|barcode=bc1|adapter=a1,123,1e-20,100,99.0" in joined_preblast
    assert "read2|ITS2|hac|barcode=bc1|adapter=a2,123,1e-20,100,99.0" in joined_preblast
    assert "read1|COI|hac|barcode=bc1|adapter=a1;123;1e-20;100;99.0" in joined_blast
    assert "read2|ITS2|hac|barcode=bc1|adapter=a2;123;1e-20;100;99.0" in joined_blast
    assert joined_blast == round_blast
    assert (tmp_path / "round" / "RTBioScan_memtax1.txt").exists()
    assert (tmp_path / "round" / "RTBioScan_memtax2.txt").exists()


def test_blast_otu_pretax_parallel_target_workers_preserve_output_and_split_threads(tmp_path: Path) -> None:
    fasta_text = (
        ">read1|COI|hac|barcode=bc1|adapter=a1\nACGT\n"
        ">read2|ITS2|hac|barcode=bc1|adapter=a2\nTGCA\n"
        ">read3|COI|hac|barcode=bc2|adapter=a1\nACGT\n"
        ">read4|ITS2|hac|barcode=bc2|adapter=a2\nTGCA\n"
    )
    serial_dir = tmp_path / "serial"
    parallel_dir = tmp_path / "parallel"
    serial_dir.mkdir()
    parallel_dir.mkdir()

    serial_result = _run_helper(serial_dir, 1, fasta_text)
    parallel_result = _run_helper(parallel_dir, 4, fasta_text)

    assert serial_result.returncode == 0, serial_result.stderr
    assert parallel_result.returncode == 0, parallel_result.stderr

    assert (serial_dir / "RTBioScan_preblastreport_join.txt").read_text(encoding="utf-8") == (
        parallel_dir / "RTBioScan_preblastreport_join.txt"
    ).read_text(encoding="utf-8")
    assert (serial_dir / "RTBioScan_blastreport_join.txt").read_text(encoding="utf-8") == (
        parallel_dir / "RTBioScan_blastreport_join.txt"
    ).read_text(encoding="utf-8")

    blast_log = (parallel_dir / "blast.log").read_text(encoding="utf-8").splitlines()
    thread_counts = sorted(int(line.split("\t")[1]) for line in blast_log if line.strip())
    assert thread_counts == [2, 2]


def test_blast_otu_pretax_single_target_uses_all_reserved_threads(tmp_path: Path) -> None:
    result = _run_helper(
        tmp_path,
        4,
        ">read1|COI|hac|barcode=bc1|adapter=a1\nACGT\n"
        ">read2|COI|hac|barcode=bc2|adapter=a2\nTGCA\n",
    )

    assert result.returncode == 0, result.stderr
    blast_log = (tmp_path / "blast.log").read_text(encoding="utf-8").splitlines()
    thread_counts = [int(line.split("\t")[1]) for line in blast_log if line.strip()]
    assert thread_counts == [4]
    joined_blast = (tmp_path / "RTBioScan_blastreport_join.txt").read_text(encoding="utf-8")
    assert "read1|COI|hac|barcode=bc1|adapter=a1;123;1e-20;100;99.0" in joined_blast
    assert "read2|COI|hac|barcode=bc2|adapter=a2;123;1e-20;100;99.0" in joined_blast
