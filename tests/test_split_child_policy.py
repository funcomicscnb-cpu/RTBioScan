"""S5 manufactured SAM truth cases: execute extracted production, never a copied policy.

All generated artifacts belong in pytest's external --basetemp. Mutation mirrors
are temporary and removed on exit. The Python oracle uses SAM field decomposition
and ordered byte records independently of the production Perl/awk expressions.
"""
from collections import Counter
from contextlib import contextmanager
import fnmatch
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "619b3bfe34f3cc0c9e096ce8c2711bd4eb32bc37"
SIDECAR = "bc01_split_children_excluded.list"
HEADER = b"@HD\tVN:1.6\tSO:unknown\n@CO\tpi:Z:not-a-record\n"
REPORT_HEADER = (b"read_id\tqc_filter\tbarcode\tkingdom\tkingdom_perc_identity"
                 b"\tkingdom_aln_length\ton_target_kingdom\n")


def sam(read_id, *tags, flag=4):
    return ("\t".join([read_id, str(flag), "*", "0", "0", "*", "*", "0", "0",
                       "ACGT", "IIII", *tags]) + "\n").encode()


def source():
    return (ROOT / "main.nf").read_text()


def region(text, name):
    start = text.index(f"    # {name} start\n")
    end = text.index(f"    # {name} end\n", start) + len(f"    # {name} end\n")
    return text[start:end]


def render(text, work):
    # Decode the Groovy GString layer, after resolving its fixture bindings.
    values = {"barcode": "bc01", "round_barcode": "round1",
              "ongoingStateDir": str(work / "state"), "task.cpus": "1",
              "baseDir": str(ROOT), "target_reads_list": "bc01_reads_target.list",
              "read_file": "reads.pod5", "params.file_wait_minutes * 60": "5",
              "params.file_wait_minutes": "0", "params.dorado_device": "cpu",
              "doradoBasecallerLauncher": "/bin/true", "doradoHacBasecallerArgs": "",
              "doradoSupBasecallerArgs": "", "doradoHacModel": "model",
              "doradoSupModel": "model", "params.min_quality_score": "10",
              "params.hq_quality_score": "15", "params.targets": "COI|ITS2",
              "params.min_read_lengths": "1|1", "params.max_read_lengths": "100|100"}
    for key, val in values.items():
        text = re.sub(r"(?<!\\)\$\{" + re.escape(key) + r"\}", lambda _: val, text)
    text = text.replace("\\\\", "\0").replace("\\$", "$")
    for escaped, decoded in ((r"\t", "\t"), (r"\n", "\n"), (r"\r", "\r")):
        text = text.replace(escaped, decoded)
    return text.replace("\0", "\\")


def executable(path, body):
    path.write_text(body)
    path.chmod(0o755)


def run_shell(work, body, *, prelude="", env=None):
    work.mkdir(parents=True, exist_ok=True)
    script = work / "fixture.sh"
    script.write_text("#!/bin/bash\nset -euo pipefail\nexport LC_ALL=C\n"
                      "barcode=bc01\nround_barcode=round1\n" + prelude + "\n" + body)
    syntax = subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr
    return subprocess.run(["/bin/bash", str(script)], cwd=work, capture_output=True,
                          env={**os.environ, **(env or {})}, text=True)


def oracle(sam_bytes, targets):
    mappings = {}
    for row in sam_bytes.splitlines():
        if row.startswith(b"@"):
            continue
        fields = row.split(b"\t")
        if len(fields) < 11:
            continue
        for field in fields[11:]:
            components = field.split(b":", 2)
            if len(components) == 3 and components[:2] == [b"pi", b"Z"] and components[2]:
                parents = mappings.setdefault(fields[0], set())
                parents.add(components[2])
    if any(len(parents) != 1 for parents in mappings.values()):
        raise ValueError("conflicting parents")
    retained, excluded, relations = [], [], []
    for raw in targets.splitlines(keepends=True):
        parts = raw.removesuffix(b"\n").split(b"|")
        valid = len(parts) == 2 and all(p and not any(bytes([c]).isspace() for c in p) for p in parts)
        if valid:
            relations.append(tuple(parts))
        if valid and parts[0] in mappings:
            excluded.append(b"\t".join([parts[0], next(iter(mappings[parts[0]])), parts[1]]) + b"\n")
        else:
            retained.append(raw)
    return b"".join(retained), b"".join(excluded), relations


def fast(work, data, targets, *, stale=False, prelude=""):
    work.mkdir(parents=True, exist_ok=True)
    state = work / "state" / "round1"
    state.mkdir(parents=True, exist_ok=True)
    if stale:
        (state / SIDECAR).write_bytes(b"STALE\n")
    (work / "bc01_fast.sam").write_bytes(data)
    (work / "bc01_reads_target.list").write_bytes(targets)
    text = source()
    start = text.index("    # FAST split-child partition start")
    end = text.index("\t\t# FAST shadow state publish start", start)
    result = run_shell(work, render(text[start:end], work), prelude=prelude)
    return result, state


def assert_partition(work, data, targets, *, stale=False):
    expected, excluded, relations = oracle(data, targets)
    result, state = fast(work, data, targets, stale=stale)
    assert result.returncode == 0, result.stderr
    actual = (work / "bc01_reads_target.list").read_bytes()
    assert actual == expected, "semantic: retained target bytes/order/membership"
    if excluded:
        assert (state / SIDECAR).exists(), "semantic: exclusion sidecar missing/name"
        assert (state / SIDECAR).read_bytes() == excluded, "semantic: exact sidecar bytes"
        assert (work / SIDECAR).read_bytes() == excluded, "semantic: local sidecar bytes"
        warning = (f"WARN: FAST excluded {len(excluded.splitlines())} split-child target relations; "
                   "barcode=bc01 round=round1")
        assert result.stderr.count(warning) == 1, "semantic: deterministic exclusion warning"
    else:
        assert not (state / SIDECAR).exists(), "semantic: stale/zero exclusion sidecar"
        assert not (work / SIDECAR).exists(), "semantic: zero local sidecar"
        assert "split-child" not in result.stderr, "semantic: zero exclusions silent"
    kept_relations = oracle(data, actual)[2]
    dropped_relations = [(r.split(b"\t")[0], r.split(b"\t")[2]) for r in excluded.splitlines()]
    assert Counter(kept_relations) + Counter(dropped_relations) == Counter(relations)
    assert not set(kept_relations) & set(dropped_relations)
    assert (state / "ROUND_FAILED.txt").exists() == (not expected)
    if not expected:
        assert (state / "ROUND_FAILED.txt").read_bytes() == b"No target reads for round1\n"
    outputs = source().split("process fast_on_target_detection {", 1)[1].split("script:", 1)[0]
    globs = re.findall(r'file\("([^\"]+)"\)', outputs)
    assert not any(fnmatch.fnmatch(SIDECAR, glob) for glob in globs)
    return actual, excluded


CASES = [
    ("no-split", sam("ordinary"), b"ordinary|COI\n"),
    ("targeted-child", sam("child", "pi:Z:parent"), b"child|COI\n"),
    ("mixed", sam("child", "pi:Z:parent") + sam("ordinary"), b"ordinary|COI\nchild|ITS2\n"),
    ("off-target-child", sam("child", "pi:Z:parent") + sam("ordinary"), b"ordinary|COI\n"),
    ("siblings", sam("child", "pi:Z:parent") + sam("sibling", "pi:Z:parent"), b"child|COI\n"),
    ("parent-retained", sam("child", "pi:Z:parent") + sam("parent"), b"child|COI\nparent|COI\n"),
    ("markers-duplicates", sam("child", "pi:Z:parent") + sam("ordinary"),
     b"ordinary|ITS2\nchild|COI\nordinary|COI\nchild|ITS2\nchild|COI\nordinary|ITS2\n"),
    ("optional-fields", sam("child", "qs:i:10", "pi:Z:parent", "RG:Z:run"), b"child|COI\n"),
    ("identical-mappings", sam("child", "pi:Z:parent") * 2, b"child|COI\n"),
    ("empty", sam("child", "pi:Z:parent"), b""),
    ("decoy-tags", sam("ordinary", "zz:Z:pi:Z:parent", "pi:i:42", "pi:Z:"), b"ordinary|COI\n"),
    ("malformed-preserved", sam("child", "pi:Z:parent"),
     b"child\nchild|\n|COI\nchild|COI|extra\nchild|CO I\nchild|COI\r\n\nordinary|COI"),
]


@pytest.mark.parametrize("label,data,targets", CASES, ids=[x[0] for x in CASES])
def test_partition_cases(tmp_path, label, data, targets):
    assert_partition(tmp_path, HEADER + data, targets)


def test_reversal_and_tag_permutation(tmp_path):
    targets = b"z|COI\nchild|COI\na|ITS2\nchild|ITS2\nchild|COI\n"
    data = HEADER + sam("child", "pi:Z:parent", "qs:i:10", "RG:Z:run")
    a, b = assert_partition(tmp_path / "forward", data, targets)
    c, d = assert_partition(tmp_path / "reverse", data, b"".join(reversed(targets.splitlines(True))))
    assert c == b"".join(reversed(a.splitlines(True)))
    assert d == b"".join(reversed(b.splitlines(True)))
    assert assert_partition(tmp_path / "tags", HEADER + sam("child", "RG:Z:run", "qs:i:10", "pi:Z:parent"), targets) == (a, b)


def test_conflicting_parents_do_not_publish(tmp_path):
    targets = b"child|COI\nordinary|ITS2\n"
    result, state = fast(tmp_path, HEADER + sam("child", "pi:Z:p1") + sam("child", "pi:Z:p2"), targets, stale=True)
    assert result.returncode != 0, "semantic: conflicting parents must fail"
    assert "conflicting FAST split-child parents for child" in result.stderr
    assert (tmp_path / "bc01_reads_target.list").read_bytes() == targets
    assert not (state / SIDECAR).exists()
    assert not list(tmp_path.glob("*.tmp.*"))


def test_stale_sidecar_removed(tmp_path):
    assert_partition(tmp_path, HEADER + sam("ordinary"), b"ordinary|COI\n", stale=True)


def test_atomic_publication(tmp_path):
    # Observe both boundaries: cp may write a partial temporary file, never final state.
    prelude = r'''
cp() {
    printf 'partial\n' > "$2"
    if [ -e "state/round1/bc01_split_children_excluded.list" ]; then
        printf 'exposed partial\n' > atomic-violation
    fi
    command cp "$@"
}
mv() {
    case "$*" in
        *state/round1/*)
            cmp "$2" bc01_split_children_excluded.list || return 73
            [ ! -e "state/round1/bc01_split_children_excluded.list" ] || printf 'stale\n' > atomic-violation
            printf 'rename\n' > atomic-observed ;;
    esac
    command mv "$@"
}
'''
    result, state = fast(tmp_path, HEADER + sam("child", "pi:Z:parent"), b"child|COI\n", stale=True, prelude=prelude)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "atomic-violation").exists(), "semantic: partial/stale diagnostic exposed"
    assert (tmp_path / "atomic-observed").exists(), "semantic: state publication must rename"
    assert (state / SIDECAR).read_bytes() == b"child\tparent\tCOI\n"
    failed = tmp_path / "failure"
    result, state = fast(failed, HEADER + sam("child", "pi:Z:parent"), b"child|COI\n", stale=True,
                         prelude='cp() { printf partial > "$2"; return 74; }')
    assert result.returncode != 0
    assert not (state / SIDECAR).exists()
    assert (failed / "bc01_reads_target.list").read_bytes() == b"child|COI\n"
    assert not list(state.glob("*.tmp.*"))


def report(work, data, targets):
    work.mkdir(parents=True, exist_ok=True)
    (work / "fast.sam").write_bytes(data)
    (work / "targets.list").write_bytes(targets)
    ids = list(dict.fromkeys(row.split(b"\t")[0] for row in data.splitlines() if not row.startswith(b"@")))
    (work / "summary.tsv").write_bytes(b"filename\tread_id\trun_id\tsequence_length_template\tmean_qscore_template\n" +
        b"".join(b"input.pod5\t" + rid + b"\trun\t4\t20\n" for rid in ids))
    (work / "blast.tsv").write_bytes(b"".join(rid + b"\tCOI|Metazoa\t100\t4\n" for rid in ids))
    fake = work / "shims"
    fake.mkdir()
    executable(fake / "samtools", "#!/bin/sh\nprintf '@ordinary\\nACGT\\n+\\nIIII\\n'\n")
    result = subprocess.run(["perl", str(ROOT / "bin/reporting_getting_on_target.pl"), "summary.tsv", "fast.sam", "blast.tsv", "targets.list", "1", "100", "bc01"],
                            cwd=work, env={**os.environ, "PATH": f"{fake}:/usr/bin:/bin"}, capture_output=True)
    assert result.returncode == 0, result.stderr
    return {p.name: p.read_bytes() for p in work.glob("*_rpt.txt")}


def test_real_reporting_off_target(tmp_path):
    data = HEADER + sam("ordinary") + sam("child", "pi:Z:parent")
    retained, _ = assert_partition(tmp_path / "partition", data, b"ordinary|COI\nchild|COI\n")
    output = report(tmp_path / "report", data, retained)["bc01_on_target_rpt.txt"]
    assert output.splitlines(True)[0] == REPORT_HEADER
    assert output == (REPORT_HEADER + b"ordinary\tIN\tCOI\tMetazoa\t100\t4\tON_TARGET\n"
                      b"child\tIN\tCOI\tMetazoa\t100\t4\tOFF_TARGET\n"), "semantic: real report retains existing OFF_TARGET schema"


def diagnostic(work, stage, data, *, cache=False, awk=None):
    work.mkdir(parents=True, exist_ok=True)
    sam_name = "bc01_hac.sam" if stage == "HAC" else "bc01_blastreport_sup.sam"
    (work / sam_name).write_bytes(data)
    text = source()
    prelude = ""
    if stage == "SUP":
        start = text.index('if [ -s ${barcode}_blastreport_hac_missing.list ]; then')
        end = text.index('\n\t\t\t\t\t\tsup_merge_outputs', start)
        body = text[start:end]
        if not cache:
            (work / "bc01_blastreport_hac_missing.list").write_text("parent\n")
        prelude = '''BIN_DIR=unused
DORADO_LOCK=unused
DORADO_LOCK_WAIT=1
now_ms() { echo 0; }
append_sup_path_timing() { :; }
dorado_basecall_retry() { return 0; }
samtools() { :; }
sup_build_new_summary() { :; }
'''
    else:
        body = region(text, "HAC split-child diagnostic")
    if awk:
        prelude += f'awk() {{ "{awk}" "$@"; }}\n'
    result = run_shell(work, render(body, work), prelude=prelude)
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("stage", ["HAC", "SUP"])
@pytest.mark.parametrize("awk", ["/usr/bin/awk", shutil.which("gawk")])
def test_unique_stage_diagnostic(tmp_path, stage, awk):
    if not awk:
        pytest.skip("GNU awk not installed")
    data = HEADER + sam("child", "pi:Z:parent") + sam("child", "pi:Z:parent", flag=260) + sam("child", "pi:Z:parent", flag=2052) + sam("sibling", "qs:i:10", "pi:Z:parent") + sam("decoy", "zz:Z:pi:Z:p")
    result = diagnostic(tmp_path, stage, data, awk=awk)
    expected = f"WARN: {stage} emitted 2 unique split-child QNAMEs; barcode=bc01 round=round1"
    assert result.stderr.count(expected) == 1, "semantic: unique stage child warning"
    assert len([line for line in result.stderr.splitlines() if "split-child" in line]) == 1


@pytest.mark.parametrize("stage", ["HAC", "SUP"])
def test_zero_stage_silent(tmp_path, stage):
    result = diagnostic(tmp_path, stage, HEADER + sam("ordinary", "pi:Z:", "zz:Z:pi:Z:parent"))
    assert "split-child" not in result.stderr, "semantic: zero stage children silent"


def test_sup_cache_only_silent(tmp_path):
    result = diagnostic(tmp_path, "SUP", HEADER + sam("child", "pi:Z:parent"), cache=True)
    assert "split-child" not in result.stderr
    assert "skipping Dorado" in result.stderr


def hac(work, targets, data, text=None):
    work.mkdir(parents=True, exist_ok=True)
    (work / "bc01_reads_target.list").write_bytes(targets)
    (work / "reads.pod5").write_bytes(b"fixture")
    (work / "input.sam").write_bytes(data)
    # External Dorado is stubbed; actual SAM conversion is exercised separately below.
    fake = work / "shims"
    fake.mkdir()
    executable(fake / "samtools", "#!/usr/bin/awk -f\nBEGIN { FS=\"\\t\" }\n!/^@/ && NF>=11 { print \"@\" $1; print $10; print \"+\"; print $11 }\n")
    # Shell wrapper supplies only the SAM path to awk.
    (fake / "samtools").rename(fake / "sam-to-fastq")
    executable(fake / "samtools", '#!/bin/bash\nfor arg in "$@"; do last="$arg"; done\nexec "$(dirname "$0")/sam-to-fastq" "$last"\n')
    executable(fake / "seqkit", '#!/bin/bash\nfor arg in "$@"; do last="$arg"; done\ncat "$last"\n')
    executable(fake / "pod5", "#!/bin/sh\nexit 1\n")
    text = text or source()
    process = text.split("process hac_basecalling {", 1)[1]
    start = process.index("# Always create expected outputs")
    end = process.index("fi  # SKIP_HAC", start) + len("fi  # SKIP_HAC")
    prelude = '''DORADO_LOCK=unused
DORADO_LOCK_WAIT=1
dorado_basecall_retry() {
    cp bc01_read_names_hq.list requested.ids
    cp input.sam "$2"
}
'''
    result = run_shell(work, render(process[start:end], work), prelude=prelude,
                       env={"PATH": f"{fake}:/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    return {p.name: p.read_bytes() for p in work.iterdir()
            if p.name.endswith((".sam", ".fastq", ".ids"))}


def reachability(work, data=None, text=None):
    data = data if data is not None else HEADER + sam("parent") + sam("child", "pi:Z:parent") + sam("sibling", "pi:Z:parent")
    outputs = hac(work, b"parent|COI\n", data, text)
    assert outputs["requested.ids"] == b"parent\n", "semantic: exact HAC request identities"
    assert outputs["bc01_hac_filtered.fastq"] == b"@parent|COI\nACGT\n+\nIIII\n", "semantic: annotation gate rejects children/siblings"
    # Feed only surviving annotations to the real eligibility and request consumers.
    (work / "annotated.txt").write_bytes(b"".join(row[1:] + b"|hac|adapter=sample|OTU1\n" for row in outputs["bc01_hac_filtered.fastq"].splitlines() if row.startswith(b"@")))
    (work / "pident.tsv").write_bytes(b"")
    result = subprocess.run(["perl", str(ROOT / "bin/select_reads2sup.pl"), "annotated.txt", "50", "pident.tsv"], cwd=work, capture_output=True)
    assert result.returncode == 0, result.stderr
    (work / "blast_report_annotated_otu.txt").write_bytes(result.stdout)
    result = run_shell(work, f'source "{ROOT}/bin/blast_sup_path.sh"\nsup_candidate_extract\n',
                       prelude='now_ms() { echo 0; }\nappend_sup_path_timing() { :; }')
    assert result.returncode == 0, result.stderr
    request = (work / "bc01_blastreport_hac.list").read_bytes()
    assert request == b"parent\n", "semantic: SUP request excludes child/sibling by exact identity"
    # The same real annotation consumer also gates newly generated SUP QNAMEs.
    (work / "sup.annotations").write_bytes(b"parent|COI|sup\n")
    result = subprocess.run(["perl", str(ROOT / "bin/fastq_add_annotations2ids.pl"), "sup.annotations", "bc01_hac.fastq"], cwd=work, capture_output=True)
    assert result.returncode == 0
    assert result.stdout == b"@parent|COI|sup\nACGT\n+\nIIII\n", "semantic: SUP annotation gate remains exact"
    return {**outputs, "sup.request": request, "sup.annotated.fastq": result.stdout}


def test_exact_hac_sup_reachability(tmp_path):
    reachability(tmp_path)


def test_real_samtools(tmp_path):
    tool = shutil.which("samtools")
    if not tool:
        pytest.skip("samtools not installed")
    data = HEADER + sam("child", "pi:Z:parent") + sam("child", "pi:Z:parent", flag=260)
    (tmp_path / "input.sam").write_bytes(data)
    result = subprocess.run([tool, "fastq", str(tmp_path / "input.sam")], capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"@child\nACGT\n+\nIIII\n"


def head_source():
    return subprocess.check_output(["git", "show", f"{BASELINE}:main.nf"], cwd=Path(__file__).resolve().parents[1], text=True)


def test_non_split_head_byte_equivalence(tmp_path):
    data = HEADER + sam("ordinary") + sam("second")
    targets = b"second|ITS2\nordinary|COI\nordinary|COI\n"
    retained, _ = assert_partition(tmp_path / "fast", data, targets)
    assert report(tmp_path / "report-new", data, retained) == report(tmp_path / "report-head", data, targets)
    assert hac(tmp_path / "hac-new", retained, data) == hac(tmp_path / "hac-head", targets, data, head_source())
    assert reachability(tmp_path / "downstream-new", HEADER + sam("parent")) == reachability(
        tmp_path / "downstream-head", HEADER + sam("parent"), head_source())
    # SUP selection and annotation consumers are unchanged; compare their executable
    # outputs on identical ordinary inputs, along with each diagnostic-free SAM.
    for stage in ("HAC", "SUP"):
        before = HEADER + sam("ordinary")
        diagnostic(tmp_path / stage, stage, before)
        name = "bc01_hac.sam" if stage == "HAC" else "bc01_blastreport_sup.sam"
        assert (tmp_path / stage / name).read_bytes() == before


@contextmanager
def mirror(tmp_path):
    global ROOT
    original = ROOT
    with tempfile.TemporaryDirectory(prefix="mutant-", dir=tmp_path) as directory:
        ROOT = Path(directory)
        shutil.copyfile(original / "main.nf", ROOT / "main.nf")
        shutil.copytree(original / "bin", ROOT / "bin")
        try:
            yield ROOT
        finally:
            ROOT = original


def replace_once(path, old, new):
    text = path.read_text()
    assert old in text, "mutation construction failed"
    path.write_text(text.replace(old, new, 1))


def gstring(shell):
    return shell.replace("\\", "\\\\").replace("$", "\\$")


MUTANTS = [
    ("M1", "retained target bytes/order/membership"),
    ("M2", "retained target bytes/order/membership"),
    ("M3", "exclusion sidecar missing/name"),
    ("M4", "exclusion sidecar missing/name"),
    ("M5", "retained target bytes/order/membership"),
    ("M6", "retained target bytes/order/membership"),
    ("M7", "retained target bytes/order/membership"),
    ("M8", "unique stage child warning"),
    ("M9", "unique stage child warning"),
    ("M10", "unique stage child warning"),
    ("M11", "retained target bytes/order/membership"),
    ("M12", "conflicting parents must fail"),
    ("M13", "real report retains existing OFF_TARGET schema"),
    ("M14", "partial/stale diagnostic exposed"),
    ("M15", "SUP request excludes child/sibling by exact identity"),
]


@pytest.mark.parametrize("mutant,killer", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_mutation_controls(tmp_path, mutant, killer):
    """Only the named ordinary semantic assertion counts as a mutation kill."""
    with mirror(tmp_path) as root:
        main = root / "main.nf"
        text = main.read_text()
        data = HEADER + sam("child", "pi:Z:parent") + sam("sibling", "pi:Z:parent") + sam("parent")
        targets = b"z|ITS2\nchild|COI\nparent|COI\na|ITS2\nchild|COI\nz|ITS2\n"
        check = lambda: assert_partition(root / "fixture", data, targets)
        if mutant == "M1":
            block = region(text, "FAST split-child partition")
            main.write_text(text.replace(block, block.splitlines(True)[0] + "    :\n" + block.splitlines(True)[-1]))
        elif mutant == "M2":
            replace_once(main, r'\$parent{\$f[0]} = \$raw_parent;', r'\$parent{\$raw_parent} = \$f[0];')
        elif mutant == "M3":
            anchor = "    # FAST split-child partition end"
            replace_once(main, anchor, '    rm -f "\\$_split_state"\n' + anchor)
        elif mutant == "M4":
            replace_once(main, "_split_children_excluded.list", "_split_children_reads_target.list")
        elif mutant == "M5":
            anchor = '                ++\\$count;'
            expansion = r'''                for my $sibling (sort keys %parent) {
                    print {$keep} "$sibling|$2\n" if $sibling ne $1 && $parent{$sibling} eq $parent{$1};
                }
'''
            replace_once(main, anchor, gstring(expansion) + anchor)
        elif mutant == "M6":
            replace_once(main, '        my \\$count = 0;', '        my %seen_relation;\n        my \\$count = 0;')
            replace_once(main, '        while (<\\$tf>) {', '        while (<\\$tf>) {\n            next if \\$seen_relation{\\$_}++;')
        elif mutant == "M7":
            anchor = '    mv -f "\\$_split_targets_tmp"'
            replace_once(main, anchor, '    sort "\\$_split_targets_tmp" -o "\\$_split_targets_tmp"\n' + anchor)
        elif mutant in ("M8", "M9", "M10"):
            stage = "SUP" if mutant == "M10" else "HAC"
            block = region(text, f"{stage} split-child diagnostic")
            altered = block.replace('children[\\$1]=1', 'count++').replace('for (child in children) count++; ', '') if mutant == "M8" else block.splitlines(True)[0] + "    :\n" + block.splitlines(True)[-1]
            main.write_text(text.replace(block, altered))
            check = lambda: test_unique_stage_diagnostic(root / "fixture", stage, "/usr/bin/awk")
        elif mutant == "M11":
            replace_once(main, '/^pi:Z:(.+)\\$/', '/pi:Z:(.+)\\$/')
            check = lambda: assert_partition(root / "fixture", HEADER + sam("ordinary", "zz:Z:pi:Z:parent"), b"ordinary|COI\n")
        elif mutant == "M12":
            replace_once(main, 'if exists \\$parent{\\$f[0]} &&', 'if 0 && exists \\$parent{\\$f[0]} &&')
            check = lambda: test_conflicting_parents_do_not_publish(root / "fixture")
        elif mutant == "M13":
            replace_once(root / "bin/reporting_getting_on_target.pl", 'else{print OUT_TARGET_FILE "OFF_TARGET\\n";}', 'else{print OUT_TARGET_FILE "ON_TARGET\\n";}')
            check = lambda: test_real_reporting_off_target(root / "fixture")
        elif mutant == "M14":
            replace_once(main, 'cp "\\$_split_sidecar" "\\$_split_state_tmp" && mv -f "\\$_split_state_tmp" "\\$_split_state"', 'cp "\\$_split_sidecar" "\\$_split_state"')
            check = lambda: test_atomic_publication(root / "fixture")
        elif mutant == "M15":
            # A plausible prohibited fallback: append SAM parents to the SUP request.
            path = root / "bin/blast_sup_path.sh"
            anchor = '    hac2sup_candidate_rows=$(grep -cF'
            fallback = '''    awk -F '\\t' '!/^@/ { for(i=12;i<=NF;i++) if($i ~ /^pi:Z:.+/) {print substr($i,6); break} }' "${barcode}_hac.sam" >> "${barcode}_blastreport_hac.list"
'''
            replace_once(path, anchor, fallback + anchor)
            check = lambda: test_exact_hac_sup_reachability(root / "fixture")
        with pytest.raises(AssertionError, match="semantic: " + re.escape(killer)):
            check()


def test_non_split_fast_construction_head_equivalence(tmp_path):
    def execute(path, text):
        path.mkdir()
        (path / "bc01_qced_reads_kingdom.txt").write_bytes(
            b"ordinary\tCOI|Metazoa\t100\t4\nsecond\tITS2|Fungi\t100\t4\n")
        (path / "bc01_fast.sam").write_bytes(HEADER + sam("ordinary") + sam("second"))
        start = text.index('\t_p_targets_ft="${params.targets}"')
        end = text.index('\t\t# FAST shadow state publish start', start)
        body = render(text[start:end].replace('${params.target_taxa}', 'Metazoa|Fungi'), path)
        result = run_shell(path, body)
        assert result.returncode == 0, result.stderr
        files = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file() and p.name != 'fixture.sh'}
        assert files['bc01_reads_target.list'] == b'ordinary|COI\nsecond|ITS2\n'
        return files, result.stderr
    assert execute(tmp_path / 'new', source()) == execute(tmp_path / 'head', head_source())


def test_mixed_silently_dropped_child_hac_bytes(tmp_path):
    targets = b'ordinary|COI\nchild|COI\n'
    retained, _ = assert_partition(tmp_path / 'fast', HEADER + sam('ordinary') + sam('child', 'pi:Z:parent'), targets)
    # Model Dorado's existing raw-ID response: raw ordinary is returned, child absent.
    raw_response = HEADER + sam('ordinary')
    old = hac(tmp_path / 'old', targets, raw_response, head_source())
    new = hac(tmp_path / 'new', retained, raw_response)
    assert old.pop('requested.ids') == b'ordinary\nchild\n'
    assert new.pop('requested.ids') == b'ordinary\n'
    assert old == new


@pytest.mark.parametrize('data', [sam('child', 'pi:Z:p1', 'pi:Z:p2'),
                                 sam('offtarget', 'pi:Z:p1') + sam('offtarget', 'pi:Z:p2')])
def test_conflicting_optional_tags_or_off_target_identity(tmp_path, data):
    result, state = fast(tmp_path, HEADER + data, b'ordinary|COI\n')
    assert result.returncode != 0
    assert 'conflicting FAST split-child parents' in result.stderr
    assert (tmp_path / 'bc01_reads_target.list').read_bytes() == b'ordinary|COI\n'
    assert not (state / SIDECAR).exists()
