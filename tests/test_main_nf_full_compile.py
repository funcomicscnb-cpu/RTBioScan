"""Whole-repository ``main.nf`` compilation regression (integrated-audit finding F03).

Nextflow DSL1 captures the *source text* of every statement in a process ``script:`` section
as one Groovy ``String`` constant (``BodyDef.source``, built by
``nextflow.ast.NextflowDSLImpl#readSource``).  Groovy 3.0.13 rejects any String constant longer
than 65,535 UTF-16 code units (``ClassCompletionVerifier.checkStringExceedingMaximumLength``),
so a process whose script section grows past that size makes the *entire* ``main.nf`` fail to
compile under the required cached Nextflow 22.10.8 / Java 17 runtime with::

    String too long. The given string is N Unicode code units long, but only a maximum of
    65535 is allowed.

Bounded extracted-body tests cannot see this, so this module compiles the complete repository
``main.nf`` with the cached offline runtime (no Capsule bootstrap, no network) and requires the
launch to get *past* compilation to a deterministic early preamble guard.  A source-level size
guard fails first when any script section approaches the limit again.

Nothing is written into the repository: the launch directory, ``NXF_HOME``, ``NXF_TEMP`` and the
work directory are all pytest temporary paths.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_NF = REPO_ROOT / "main.nf"
NEXTFLOW_VERSION = "22.10.8"
JVM_STRING_LIMIT = 65_535
# Size guard for every process script section (UTF-16 code units of the captured source
# text, measured with a conservative upper bound).  Rationale: 65,535 is the hard JVM limit;
# 63,000 keeps a 2,535-unit safety band below it while still failing *before* the runtime
# would.  Any section that reaches this threshold must be reduced (relocate non-executable
# prose, or extract a self-contained shell section into a ``bin/`` helper), never raised here.
MAX_SCRIPT_SECTION_UTF16 = 63_000
STRING_LIMIT_MARKER = "String too long"
COMPILE_ERROR_MARKER = "Script compilation error"
PROFILE_GUARD_MARKER = "Unsupported RTBioScan execution profile: docker"
JAVA_ADD_OPENS = [
    "--add-opens=java.base/java.lang=ALL-UNNAMED",
    "--add-opens=java.base/java.io=ALL-UNNAMED",
    "--add-opens=java.base/java.nio=ALL-UNNAMED",
    "--add-opens=java.base/java.net=ALL-UNNAMED",
    "--add-opens=java.base/java.util=ALL-UNNAMED",
    "--add-opens=java.base/java.util.concurrent.locks=ALL-UNNAMED",
    "--add-opens=java.base/java.util.concurrent.atomic=ALL-UNNAMED",
    "--add-opens=java.base/java.nio.file.spi=ALL-UNNAMED",
    "--add-opens=java.base/sun.nio.ch=ALL-UNNAMED",
    "--add-opens=java.base/sun.nio.fs=ALL-UNNAMED",
    "--add-opens=java.base/sun.net.www.protocol.http=ALL-UNNAMED",
    "--add-opens=java.base/sun.net.www.protocol.https=ALL-UNNAMED",
    "--add-opens=java.base/sun.net.www.protocol.ftp=ALL-UNNAMED",
    "--add-opens=java.base/sun.net.www.protocol.file=ALL-UNNAMED",
    "--add-opens=java.base/jdk.internal.misc=ALL-UNNAMED",
    "--add-opens=java.base/java.util.regex=ALL-UNNAMED",
]


# --------------------------------------------------------------------------------------
# Cached runtime discovery (bounded: skip only when the cached runtime genuinely is absent)
# --------------------------------------------------------------------------------------
def _cached_nextflow_apps() -> Path | None:
    candidates = []
    if os.environ.get("RTB_NXF_APPS"):
        candidates.append(Path(os.environ["RTB_NXF_APPS"]))
    if os.environ.get("NXF_HOME"):
        candidates.append(Path(os.environ["NXF_HOME"]) / "capsule" / "apps" / f"nextflow-all_{NEXTFLOW_VERSION}")
    candidates.append(Path.home() / ".nextflow" / "capsule" / "apps" / f"nextflow-all_{NEXTFLOW_VERSION}")
    for apps in candidates:
        if (apps / f"nextflow-{NEXTFLOW_VERSION}.jar").is_file() and any(apps.glob("groovy-3.*.jar")):
            return apps
    return None


def _java_version(java: Path) -> int | None:
    try:
        cp = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)', cp.stderr + cp.stdout)
    return int(m.group(1)) if m else None


def _java17() -> Path | None:
    candidates = []
    if os.environ.get("RTB_JAVA17"):
        candidates.append(Path(os.environ["RTB_JAVA17"]))
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "bin" / "java")
    candidates.append(Path("/Library/Java/JavaVirtualMachines/microsoft-17.jdk/Contents/Home/bin/java"))
    if shutil.which("/usr/libexec/java_home"):
        cp = subprocess.run(["/usr/libexec/java_home", "-v", "17"], capture_output=True, text=True)
        if cp.returncode == 0 and cp.stdout.strip():
            candidates.append(Path(cp.stdout.strip()) / "bin" / "java")
    which = shutil.which("java")
    if which:
        candidates.append(Path(which))
    for java in candidates:
        if java.is_file() and _java_version(java) == 17:
            return java
    return None


def _launch(main_nf: Path, launch_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    apps = _cached_nextflow_apps()
    java = _java17()
    if apps is None:
        pytest.skip(f"cached offline Nextflow {NEXTFLOW_VERSION} runtime is not present on this host")
    if java is None:
        pytest.skip("no Java 17 runtime is available on this host")
    nxf_home = launch_dir / "nxf-home"
    nxf_temp = launch_dir / "nxf-temp"
    work = launch_dir / "work"
    for d in (nxf_home, nxf_temp, work):
        d.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(launch_dir),
        "TMPDIR": str(nxf_temp),
        "NXF_HOME": str(nxf_home),
        "NXF_TEMP": str(nxf_temp),
        "NXF_VER": NEXTFLOW_VERSION,
        "NXF_OFFLINE": "true",
        "NXF_DISABLE_CHECK_LATEST": "true",
        "NXF_ANSI_LOG": "false",
        # main.nf is UTF-8; Java 17 derives its default charset from the locale, so pin it.
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }
    command = [str(java), *JAVA_ADD_OPENS, "-Dfile.encoding=UTF-8", "-Dsun.jnu.encoding=UTF-8",
               "-cp", f"{apps}/*", "nextflow.cli.Launcher",
               "run", str(main_nf), "-work-dir", str(work), *args]
    return subprocess.run(command, cwd=launch_dir, env=env, capture_output=True, text=True,
                          check=False, timeout=300)


# --------------------------------------------------------------------------------------
# Source-level mirror of the constant Nextflow generates per process (conservative bound)
# --------------------------------------------------------------------------------------
def _process_blocks(text: str) -> list[tuple[str, int, int]]:
    """(name, first_line_index, last_line_index) per ``process NAME {`` block, by brace depth
    outside triple-quoted strings.  Raises if the scan disagrees with the raw ``^process`` count."""
    lines = text.split("\n")
    blocks: list[tuple[str, int, int]] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^process\s+(\w+)\s*\{", lines[i])
        if not m:
            i += 1
            continue
        depth = 0
        in_str: str | None = None
        j = i
        while j < len(lines):
            code = lines[j]
            if in_str is None and "//" in code and not re.search(r"[\"'].*//", code):
                code = code.split("//", 1)[0]
            k = 0
            while k < len(code):
                tri = code[k : k + 3]
                if in_str is None and tri in ('"""', "'''"):
                    in_str = tri
                    k += 3
                    continue
                if in_str is not None:
                    if tri == in_str:
                        in_str = None
                        k += 3
                        continue
                    k += 1
                    continue
                if code[k] == "{":
                    depth += 1
                elif code[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if in_str is None and depth == 0:
                break
            j += 1
        blocks.append((m.group(1), i, j))
        i = j + 1
    raw = len(re.findall(r"(?m)^process\s+\w+\s*\{", text))
    assert len(blocks) == raw, f"process block scan found {len(blocks)} blocks but {raw} 'process' headers"
    return blocks


def _script_section_upper_bound_utf16(text: str, start: int, end: int) -> int:
    """Upper bound on the UTF-16 length of the constant Nextflow captures for one process:
    every whole line from the ``script:``/``shell:``/``exec:`` label (or from the opening triple
    quote when the process has no label) through the closing triple quote.  readSource() only
    ever trims from these lines, so the real constant is never larger than this bound."""
    lines = text.split("\n")[start : end + 1]
    label = None
    for idx, line in enumerate(lines):
        if re.match(r"^\s*(script|shell|exec)\s*:\s*$", line):
            label = idx
    quotes = [idx for idx, line in enumerate(lines) if '"""' in line or "\'\'\'" in line]
    assert quotes, "process block without a triple-quoted script string"
    q_close = quotes[-1]
    if label is not None:
        first = label
    elif lines[q_close].count('"""') + lines[q_close].count("\'\'\'") >= 2:
        first = q_close  # single-line script string
    else:
        first = quotes[-2]  # opening quote line of the multi-line script string
    captured = "\n".join(lines[first : q_close + 1]) + "\n"
    return len(captured.encode("utf-16-le")) // 2


def _script_section_sizes() -> dict[str, int]:
    text = MAIN_NF.read_text(encoding="utf-8")
    return {name: _script_section_upper_bound_utf16(text, s, e) for name, s, e in _process_blocks(text)}


# --------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------
def test_process_script_sections_stay_below_jvm_constant_limit() -> None:
    sizes = _script_section_sizes()
    assert sizes, "no process blocks found in main.nf"
    assert MAX_SCRIPT_SECTION_UTF16 < JVM_STRING_LIMIT
    offenders = {name: n for name, n in sizes.items() if n > MAX_SCRIPT_SECTION_UTF16}
    largest = max(sizes.items(), key=lambda kv: kv[1])
    assert not offenders, (
        f"process script section(s) too close to the {JVM_STRING_LIMIT} code-unit Groovy constant "
        f"limit (guard {MAX_SCRIPT_SECTION_UTF16}): {offenders}. Nextflow captures the whole section as "
        f"one String constant; relocate non-executable prose or extract a bin/ helper. largest={largest}"
    )


def test_size_guard_mirror_sees_the_largest_process() -> None:
    # The mirror must be reading real sections: the largest one is a multi-thousand-line shell
    # body, so a trivially small maximum means the scan broke.
    sizes = _script_section_sizes()
    assert max(sizes.values()) > 20_000, sizes
    assert "blast_OTU_pretax" in sizes and "consensus" in sizes


def test_full_main_nf_compiles_and_reaches_profile_guard(tmp_path: Path) -> None:
    source = MAIN_NF.read_text(encoding="utf-8")
    assert PROFILE_GUARD_MARKER.split(":")[0] in source, "preamble profile guard text changed"
    launch = tmp_path / "launch"
    launch.mkdir()
    repo_entries_before = set(os.listdir(REPO_ROOT))
    cp = _launch(MAIN_NF, launch, "-profile", "docker")
    out = cp.stdout + cp.stderr
    assert STRING_LIMIT_MARKER not in out, f"F03 regression: whole main.nf no longer compiles:\n{out}"
    assert COMPILE_ERROR_MARKER not in out, out
    # Reaching the guard proves the complete script class compiled and started executing.
    assert PROFILE_GUARD_MARKER in out, out
    assert cp.returncode != 0, out
    # The launch wrote its log/work into the scratch launch dir, not next to the repository script.
    assert (launch / ".nextflow.log").is_file()
    assert set(os.listdir(REPO_ROOT)) == repo_entries_before


def test_compile_harness_detects_an_oversized_script_section(tmp_path: Path) -> None:
    """Negative control: the harness must report the string-limit failure class for a scratch copy
    whose blast_OTU_pretax section is padded past the limit.  This distinguishes a real compile
    from merely grepping source markers."""
    scratch = tmp_path / "repo"
    scratch.mkdir()
    for name in ("lib", "conf", "templates"):
        if (REPO_ROOT / name).is_dir():
            shutil.copytree(REPO_ROOT / name, scratch / name)
    for name in ("nextflow.config",):
        if (REPO_ROOT / name).is_file():
            shutil.copy2(REPO_ROOT / name, scratch / name)
    text = MAIN_NF.read_text(encoding="utf-8")
    anchor = "process blast_OTU_pretax {"
    assert text.count(anchor) == 1
    block_start = text.index(anchor)
    q_open = text.index('"""', block_start)
    pad_lines = (JVM_STRING_LIMIT // 60) + 100
    padding = "".join(f"\t# padding line {i:05d} to exceed the Groovy constant limit for the control\n"
                      for i in range(pad_lines))
    padded = text[: q_open + 3] + "\n" + padding + text[q_open + 3 :]
    (scratch / "main.nf").write_text(padded, encoding="utf-8")
    launch = tmp_path / "launch"
    launch.mkdir()
    cp = _launch(scratch / "main.nf", launch, "-profile", "docker")
    out = cp.stdout + cp.stderr
    assert cp.returncode != 0
    assert STRING_LIMIT_MARKER in out, out
    assert PROFILE_GUARD_MARKER not in out, out
