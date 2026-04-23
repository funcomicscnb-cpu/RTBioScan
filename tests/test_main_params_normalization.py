import os
from pathlib import Path
import pty
import re
import select
import shlex
import subprocess
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_NF = REPO_ROOT / "main.nf"
CHANNEL_UTILS = REPO_ROOT / "lib" / "ChannelUtils.groovy"
SUP_PATH_HELPER = REPO_ROOT / "bin" / "blast_sup_path.sh"
METADATA_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "metadata"


def _mask_quoted_shell_text(text: str) -> str:
    """Mask quoted shell text while preserving string length and quote positions."""
    def is_escaped(idx: int) -> bool:
        backslashes = 0
        j = idx - 1
        while j >= 0 and text[j] == "\\":
            backslashes += 1
            j -= 1
        return (backslashes % 2) == 1

    masked: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is None:
            masked.append(ch)
            if ch in {"'", '"'} and not is_escaped(i):
                quote = ch
            i += 1
            continue

        if quote == "'":
            if ch == "'":
                masked.append(ch)
                quote = None
            else:
                masked.append(" ")
            i += 1
            continue

        if ch == "\\" and i + 1 < len(text) and text[i + 1] in {'"', "\\", "$", "`"}:
            masked.extend((" ", " "))
            i += 2
            continue
        if ch == '"':
            masked.append(ch)
            quote = None
        else:
            masked.append(" ")
        i += 1
    return "".join(masked)


_INLINE_SHELL_WS = " \t\r\f\v"
_SHELL_ASSIGNMENT_NAME = r'[A-Za-z_][A-Za-z0-9_]*'
_SHELL_SIMPLE_ASSIGNMENT = (
    rf'{_SHELL_ASSIGNMENT_NAME}=(?:'
    r'[^\s"\']*'
    r'|"[^"]*"'
    r"|'[^']*'"
    r')'
)
_SHELL_ONE_OR_MORE_ASSIGNMENTS = rf'(?:{_SHELL_SIMPLE_ASSIGNMENT}\s+)+'
_CP_MV_PREFIX_PATTERN = "(?:" + "|".join((
    "",
    _SHELL_ONE_OR_MORE_ASSIGNMENTS,
    r'command\s+',
    rf'{_SHELL_ONE_OR_MORE_ASSIGNMENTS}command\s+',
    rf'env\s+(?:{_SHELL_SIMPLE_ASSIGNMENT}\s+)*',
    rf'{_SHELL_ONE_OR_MORE_ASSIGNMENTS}env\s+(?:{_SHELL_SIMPLE_ASSIGNMENT}\s+)*',
    rf'command\s+env\s+(?:{_SHELL_SIMPLE_ASSIGNMENT}\s+)*',
    rf'{_SHELL_ONE_OR_MORE_ASSIGNMENTS}command\s+env\s+(?:{_SHELL_SIMPLE_ASSIGNMENT}\s+)*',
)) + ")"
_FORBIDDEN_DEMULT_DEST_FORMS = (
    '${demult_rpt}',
    '"${demult_rpt}"',
    '${barcode}_demult_rpt.txt',
    '"${barcode}_demult_rpt.txt"',
    '"$barcode"_demult_rpt.txt',
    '${barcode}"_demult_rpt.txt"',
    '$barcode"_demult_rpt.txt"',
    '"${barcode}"_demult_rpt.txt',
    '"$barcode""_demult_rpt.txt"',
)
_FORBIDDEN_DEMULT_DEST_PATTERN = "(?:" + "|".join(
    re.escape(dest) for dest in _FORBIDDEN_DEMULT_DEST_FORMS
) + ")"
_FORBIDDEN_DEMULT_COMMAND_END = (
    r'(?:'
    r'(?=\s*(?:$|[\n;|)}]|&&|\|\||\||(?:[0-9]*)?(?:>>?|<)|[0-9]*<&|[0-9]*>&))'
    r'|(?=\s+#)'
    r')'
)
_FORBIDDEN_DEMULT_WRITE_PATTERNS = (
    re.compile(rf'^{_CP_MV_PREFIX_PATTERN}cp\b.*?\s+{_FORBIDDEN_DEMULT_DEST_PATTERN}{_FORBIDDEN_DEMULT_COMMAND_END}'),
    re.compile(rf'^{_CP_MV_PREFIX_PATTERN}mv\b.*?\s+{_FORBIDDEN_DEMULT_DEST_PATTERN}{_FORBIDDEN_DEMULT_COMMAND_END}'),
    re.compile(rf'^cat\b.*?(?:[0-9]*)>\s*{_FORBIDDEN_DEMULT_DEST_PATTERN}{_FORBIDDEN_DEMULT_COMMAND_END}'),
    re.compile(rf'^.*?(?<!>)(?:[0-9]*)>\s*{_FORBIDDEN_DEMULT_DEST_PATTERN}{_FORBIDDEN_DEMULT_COMMAND_END}'),
    re.compile(rf'^.*?(?:[0-9]*)>>\s*{_FORBIDDEN_DEMULT_DEST_PATTERN}{_FORBIDDEN_DEMULT_COMMAND_END}'),
)


def _skip_inline_shell_whitespace(text: str, idx: int) -> int:
    while idx < len(text) and text[idx] in _INLINE_SHELL_WS:
        idx += 1
    return idx


def _is_escaped_shell_char(text: str, idx: int) -> bool:
    backslashes = 0
    j = idx - 1
    while j >= 0 and text[j] == "\\":
        backslashes += 1
        j -= 1
    return (backslashes % 2) == 1


def _is_shell_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _match_shell_word(text: str, idx: int, word: str) -> bool:
    end = idx + len(word)
    return (
        text.startswith(word, idx)
        and (idx == 0 or not _is_shell_word_char(text[idx - 1]))
        and (end == len(text) or not _is_shell_word_char(text[end]))
    )


def _looks_like_shell_command_token(text: str, idx: int) -> bool:
    return idx < len(text) and text[idx] not in (_INLINE_SHELL_WS + "\n;&|)}")


def _consume_typed_shell_nesting(text: str, idx: int, stack: list[str]) -> int | None:
    if text.startswith("${", idx):
        stack.append("${")
        return idx + 2
    if text.startswith("$(", idx):
        stack.append("$(")
        return idx + 2
    if text[idx] == "`" and not _is_escaped_shell_char(text, idx):
        if stack and stack[-1] == "`":
            stack.pop()
        else:
            stack.append("`")
        return idx + 1
    if stack:
        if text[idx] == "}" and stack[-1] == "${":
            stack.pop()
            return idx + 1
        if text[idx] == ")" and stack[-1] == "$(":
            stack.pop()
            return idx + 1
    return None


def _is_top_level_inline_comment_start(text: str, idx: int, *, span_start: int | None = None) -> bool:
    if text[idx] != "#" or _is_escaped_shell_char(text, idx):
        return False
    if span_start is not None and idx == span_start:
        return True
    if idx == 0:
        return True
    prev = text[idx - 1]
    if prev in (_INLINE_SHELL_WS + "\n;|"):
        return True
    return idx >= 2 and text[idx - 2:idx] == "&&"


def _skip_top_level_inline_comment(text: str, idx: int) -> int:
    while idx < len(text) and text[idx] != "\n":
        idx += 1
    return idx


def _find_masked_shell_command_starts(text: str) -> list[int]:
    starts: list[int] = []
    idx = 0
    expect_command_start = True
    pending_case_in = False
    in_case_patterns = False
    nesting_stack: list[str] = []

    while idx < len(text):
        consumed = _consume_typed_shell_nesting(text, idx, nesting_stack)
        if consumed is not None:
            idx = consumed
            continue

        at_top_level = not nesting_stack
        if at_top_level and _is_top_level_inline_comment_start(text, idx):
            idx = _skip_top_level_inline_comment(text, idx)
            continue

        if pending_case_in:
            if not at_top_level:
                idx += 1
                continue
            if text[idx] in (_INLINE_SHELL_WS + "\n"):
                idx += 1
                continue
            if _match_shell_word(text, idx, "in"):
                word_end = idx + len("in")
                if word_end == len(text) or text[word_end] in (_INLINE_SHELL_WS + "\n"):
                    pending_case_in = False
                    in_case_patterns = True
                    expect_command_start = False
                    idx = word_end
                    continue
            if text.startswith("&&", idx) or text.startswith("||", idx):
                pending_case_in = False
                idx += 2
                expect_command_start = True
                continue
            if text[idx] == "|":
                pending_case_in = False
                idx += 1
                expect_command_start = True
                continue
            if text[idx] == ";":
                pending_case_in = False
                if idx + 1 < len(text) and text[idx + 1] == ";":
                    idx += 2
                    in_case_patterns = True
                else:
                    idx += 1
                expect_command_start = True
                continue
            idx += 1
            continue

        if in_case_patterns:
            if not at_top_level:
                idx += 1
                continue
            if text[idx] in (_INLINE_SHELL_WS + "\n"):
                idx += 1
                continue
            if _match_shell_word(text, idx, "esac"):
                word_end = idx + len("esac")
                if word_end == len(text) or not _is_shell_word_char(text[word_end]):
                    idx = word_end
                    in_case_patterns = False
                    expect_command_start = False
                    continue
            if text[idx] == ")":
                next_idx = idx + 1
                while next_idx < len(text) and text[next_idx] in (_INLINE_SHELL_WS + "\n"):
                    next_idx += 1
                if next_idx < len(text) and _looks_like_shell_command_token(text, next_idx):
                    idx = next_idx
                    in_case_patterns = False
                    expect_command_start = True
                    continue
            idx += 1
            continue

        if expect_command_start:
            if not at_top_level:
                idx += 1
                continue
            idx = _skip_inline_shell_whitespace(text, idx)
            if idx >= len(text):
                break
            if text[idx] == "\n":
                idx += 1
                expect_command_start = True
                continue
            if text.startswith("&&", idx) or text.startswith("||", idx):
                idx += 2
                expect_command_start = True
                continue
            if text[idx] == "|":
                idx += 1
                expect_command_start = True
                continue
            if text[idx] == ";":
                if idx + 1 < len(text) and text[idx + 1] == ";":
                    idx += 2
                    in_case_patterns = True
                else:
                    idx += 1
                expect_command_start = True
                continue
            if _match_shell_word(text, idx, "then"):
                word_end = idx + len("then")
                if word_end < len(text) and text[word_end] in _INLINE_SHELL_WS:
                    idx = word_end
                    expect_command_start = True
                    continue
            if _match_shell_word(text, idx, "do"):
                word_end = idx + len("do")
                if word_end < len(text) and text[word_end] in _INLINE_SHELL_WS:
                    idx = word_end
                    expect_command_start = True
                    continue
            if text[idx] == "(":
                next_idx = _skip_inline_shell_whitespace(text, idx + 1)
                if _looks_like_shell_command_token(text, next_idx):
                    idx += 1
                    expect_command_start = True
                    continue
            if text[idx] == "{":
                if idx + 1 < len(text) and text[idx + 1] in _INLINE_SHELL_WS:
                    idx += 1
                    expect_command_start = True
                    continue
            if text[idx] == "}":
                idx += 1
                expect_command_start = False
                continue

            starts.append(idx)
            pending_case_in = _match_shell_word(text, idx, "case")
            expect_command_start = False
            idx += 1
            continue

        if not at_top_level:
            idx += 1
            continue
        if text[idx] == "\n":
            idx += 1
            expect_command_start = True
            continue
        if text.startswith("&&", idx) or text.startswith("||", idx):
            idx += 2
            expect_command_start = True
            continue
        if text[idx] == "|":
            idx += 1
            expect_command_start = True
            continue
        if text[idx] == ";":
            if idx + 1 < len(text) and text[idx + 1] == ";":
                idx += 2
                in_case_patterns = True
            else:
                idx += 1
            expect_command_start = True
            continue
        idx += 1

    return starts


def _find_masked_shell_command_end(text: str, start: int) -> int:
    nesting_stack: list[str] = []
    idx = start
    while idx < len(text):
        consumed = _consume_typed_shell_nesting(text, idx, nesting_stack)
        if consumed is not None:
            idx = consumed
            continue
        if nesting_stack:
            idx += 1
            continue
        if text[idx] == "\n":
            return idx
        if _is_top_level_inline_comment_start(text, idx, span_start=start):
            return idx
        if text.startswith("&&", idx) or text.startswith("||", idx):
            return idx
        if text[idx] == ";":
            return idx
        if text[idx] == "|":
            return idx
        idx += 1
    return len(text)


def _has_forbidden_demult_write(shell_text: str) -> bool:
    masked_text = _mask_quoted_shell_text(shell_text)
    for start in _find_masked_shell_command_starts(masked_text):
        end = _find_masked_shell_command_end(masked_text, start)
        command_raw = shell_text[start:end]
        if any(pattern.search(command_raw) for pattern in _FORBIDDEN_DEMULT_WRITE_PATTERNS):
            return True
    return False


def _make_nextflow_shim(
    tmp_path: Path,
    *,
    config_stdout: str = "params.targets = 'COI|ITS2|EXTRA'\n",
    config_exit: int = 0,
    run_stdout: str = "",
    run_stderr: str = "NEXTFLOW_RUN_SENTINEL\n",
    run_exit: int = 86,
    log_stdout: str = "",
    log_stderr: str = "",
    log_exit: int = 0,
    clean_stdout: str = "",
    clean_stderr: str = "",
    clean_exit: int = 0,
) -> tuple[dict[str, str], Path]:
    shim_dir = tmp_path / "nextflow-shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "nextflow_calls.log"
    shim_path = shim_dir / "nextflow"
    shim_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "{\n"
        "  printf 'PWD\\t%s\\n' \"$PWD\"\n"
        "  for arg in \"$@\"; do\n"
        "    printf 'ARG\\t%s\\n' \"$arg\"\n"
        "  done\n"
        "  printf 'END\\n'\n"
        "} >> \"$NEXTFLOW_SHIM_LOG\"\n"
        "subcmd=''\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    config|run|log|clean)\n"
        "      subcmd=\"$arg\"\n"
        "      break\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
        "case \"$subcmd\" in\n"
        "  config)\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_CONFIG_STDOUT:-}\"\n"
        "    exit \"${NEXTFLOW_SHIM_CONFIG_EXIT:-0}\"\n"
        "    ;;\n"
        "  run)\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_RUN_STDOUT:-}\"\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_RUN_STDERR:-}\" >&2\n"
        "    exit \"${NEXTFLOW_SHIM_RUN_EXIT:-0}\"\n"
        "    ;;\n"
        "  log)\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_LOG_STDOUT:-}\"\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_LOG_STDERR:-}\" >&2\n"
        "    exit \"${NEXTFLOW_SHIM_LOG_EXIT:-0}\"\n"
        "    ;;\n"
        "  clean)\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_CLEAN_STDOUT:-}\"\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_CLEAN_STDERR:-}\" >&2\n"
        "    exit \"${NEXTFLOW_SHIM_CLEAN_EXIT:-0}\"\n"
        "    ;;\n"
        "esac\n"
        "echo 'unexpected nextflow invocation' >&2\n"
        "exit 98\n",
        encoding="utf-8",
    )
    shim_path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}:{env['PATH']}"
    env["NEXTFLOW_SHIM_LOG"] = str(log_path)
    env["NEXTFLOW_SHIM_CONFIG_STDOUT"] = config_stdout
    env["NEXTFLOW_SHIM_CONFIG_EXIT"] = str(config_exit)
    env["NEXTFLOW_SHIM_RUN_STDOUT"] = run_stdout
    env["NEXTFLOW_SHIM_RUN_STDERR"] = run_stderr
    env["NEXTFLOW_SHIM_RUN_EXIT"] = str(run_exit)
    env["NEXTFLOW_SHIM_LOG_STDOUT"] = log_stdout
    env["NEXTFLOW_SHIM_LOG_STDERR"] = log_stderr
    env["NEXTFLOW_SHIM_LOG_EXIT"] = str(log_exit)
    env["NEXTFLOW_SHIM_CLEAN_STDOUT"] = clean_stdout
    env["NEXTFLOW_SHIM_CLEAN_STDERR"] = clean_stderr
    env["NEXTFLOW_SHIM_CLEAN_EXIT"] = str(clean_exit)
    return env, log_path


def _make_wrapper_root(tmp_path: Path) -> tuple[Path, Path]:
    wrapper_root = tmp_path / "wrapper_root"
    (wrapper_root / "bin" / "lib").mkdir(parents=True)
    script = wrapper_root / "RTBioScan.sh"
    script.write_text((REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8"), encoding="utf-8")
    (wrapper_root / "bin" / "lib" / "stale_lock_utils.sh").write_text(
        (REPO_ROOT / "bin" / "lib" / "stale_lock_utils.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return wrapper_root, script


def _write_wrapper_nextflow_shim(
    wrapper_root: Path,
    env: dict[str, str],
    *,
    log_stdout: str = "",
    log_stderr: str = "",
    log_exit: int = 0,
    clean_stdout: str = "",
    clean_stderr: str = "",
    clean_exit: int = 0,
) -> Path:
    log_path = wrapper_root / "nextflow_calls.log"
    shim_path = wrapper_root / "nextflow"
    shim_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "{\n"
        "  printf 'PWD\\t%s\\n' \"$PWD\"\n"
        "  for arg in \"$@\"; do\n"
        "    printf 'ARG\\t%s\\n' \"$arg\"\n"
        "  done\n"
        "  printf 'END\\n'\n"
        "} >> \"$NEXTFLOW_SHIM_LOG\"\n"
        "subcmd=''\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    log|clean)\n"
        "      subcmd=\"$arg\"\n"
        "      break\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
        "case \"$subcmd\" in\n"
        "  log)\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_LOG_STDOUT:-}\"\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_LOG_STDERR:-}\" >&2\n"
        "    exit \"${NEXTFLOW_SHIM_LOG_EXIT:-0}\"\n"
        "    ;;\n"
        "  clean)\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_CLEAN_STDOUT:-}\"\n"
        "    printf '%s' \"${NEXTFLOW_SHIM_CLEAN_STDERR:-}\" >&2\n"
        "    exit \"${NEXTFLOW_SHIM_CLEAN_EXIT:-0}\"\n"
        "    ;;\n"
        "esac\n"
        "echo 'unexpected nextflow invocation' >&2\n"
        "exit 98\n",
        encoding="utf-8",
    )
    shim_path.chmod(0o755)
    env["NEXTFLOW_SHIM_LOG"] = str(log_path)
    env["NEXTFLOW_SHIM_LOG_STDOUT"] = log_stdout
    env["NEXTFLOW_SHIM_LOG_STDERR"] = log_stderr
    env["NEXTFLOW_SHIM_LOG_EXIT"] = str(log_exit)
    env["NEXTFLOW_SHIM_CLEAN_STDOUT"] = clean_stdout
    env["NEXTFLOW_SHIM_CLEAN_STDERR"] = clean_stderr
    env["NEXTFLOW_SHIM_CLEAN_EXIT"] = str(clean_exit)
    return log_path


def _run_command_via_pty(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    wait_for_output: str,
    input_bytes: bytes,
    timeout: float = 10.0,
) -> tuple[int, str]:
    pid, master_fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(argv[0], argv, env)

    output = bytearray()
    prompt_bytes = wait_for_output.encode("utf-8")
    input_sent = False
    deadline = time.time() + timeout
    exit_status: int | None = None

    try:
        while True:
            if time.time() > deadline:
                os.kill(pid, 9)
                _, exit_status = os.waitpid(pid, 0)
                partial_output = output.decode("utf-8", errors="replace")
                raise AssertionError(f"timed out waiting for PTY command completion; output={partial_output!r}")

            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
                    if (not input_sent) and prompt_bytes in output:
                        os.write(master_fd, input_bytes)
                        input_sent = True

            waited_pid, waited_status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                exit_status = waited_status
                break

        if not input_sent:
            raise AssertionError(f"did not observe PTY prompt {wait_for_output!r}")
    finally:
        os.close(master_fd)

    if exit_status is None:
        raise AssertionError("missing PTY child exit status")

    return os.waitstatus_to_exitcode(exit_status), output.decode("utf-8", errors="replace")


def _read_nextflow_invocations(log_path: Path) -> list[tuple[str, list[str]]]:
    invocations: list[tuple[str, list[str]]] = []
    if not log_path.exists():
        return invocations
    pwd = ""
    args: list[str] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line == "END":
            invocations.append((pwd, args))
            pwd = ""
            args = []
            continue
        kind, value = line.split("\t", 1)
        if kind == "PWD":
            pwd = value
        elif kind == "ARG":
            args.append(value)
    return invocations


def _write_wrapper_ps_shim(wrapper_root: Path, lines: list[str]) -> Path:
    ps_path = wrapper_root / "bin" / "ps"
    ps_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "cat <<'EOF'\n"
        + "\n".join(lines)
        + "\nEOF\n",
        encoding="utf-8",
    )
    ps_path.chmod(0o755)
    return ps_path


def _metadata_fixture_args(run_id: str = "TestRun") -> list[str]:
    return [
        "--run_id",
        run_id,
        "--metadata",
        str(METADATA_FIXTURES / "pipeline_info.tsv"),
        "--general_fasta",
        str(METADATA_FIXTURES / "general.fasta"),
        "--primers_fasta",
        str(METADATA_FIXTURES / "primers.fasta"),
    ]


def _normalize_bool_like_main(value, default=False):
    if value is None:
        return default
    s = str(value).strip().lower()
    if not s:
        return default
    if s in {"true", "1", "yes", "y", "on"}:
        return True
    if s in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError(value)


def _derive_glob_root_like_main(pattern: str | None) -> str | None:
    if not pattern:
        return None
    s = str(pattern)
    idx = len(s)
    for ch in ("*", "?", "[", "{"):
        i = s.find(ch)
        if 0 <= i < idx:
            idx = i
    prefix = s if idx == len(s) else s[:idx]
    if not prefix:
        return None
    explicit_dir = prefix.endswith(("/", "\\"))
    cleaned = prefix.rstrip("/\\")
    if not cleaned:
        return "."
    path = Path(cleaned)
    if explicit_dir or idx == len(s):
        return str(path)
    parent = path.parent
    return str(parent) if str(parent) else "."


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " y "])
def test_bool_normalization_truthy_values(value: str) -> None:
    assert _normalize_bool_like_main(value, default=False) is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", " n "])
def test_bool_normalization_falsey_values(value: str) -> None:
    assert _normalize_bool_like_main(value, default=True) is False


@pytest.mark.parametrize("value", ["", "   ", None])
def test_bool_normalization_empty_uses_default(value) -> None:
    assert _normalize_bool_like_main(value, default=False) is False
    assert _normalize_bool_like_main(value, default=True) is True


@pytest.mark.parametrize("value", ["maybe", "2", "enabled"])
def test_bool_normalization_invalid_values_raise(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_bool_like_main(value, default=False)


def test_derive_glob_root_keeps_directory_before_wildcard() -> None:
    pattern = "results/pod5/June_Testing_3_MP/reads_rt_round_pod5/*pod5"
    assert _derive_glob_root_like_main(pattern) == "results/pod5/June_Testing_3_MP/reads_rt_round_pod5"


def test_derive_glob_root_uses_parent_for_filename_glob() -> None:
    pattern = "results/pod5/June_Testing_3_MP/reads_rt_round_pod5/round_*_reads.pod5"
    assert _derive_glob_root_like_main(pattern) == "results/pod5/June_Testing_3_MP/reads_rt_round_pod5"


def test_main_nf_uses_strict_bool_for_otu_force_prune_override() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert "def otuForcePruneOverride = parseBoolStrict(params.otu_force_prune_override, false, 'otu_force_prune_override')" in text
    assert "FORCE_PRUNE_OVERRIDE=\"${otuForcePruneOverride ? '1' : '0'}\"" in text
    assert "FORCE_PRUNE_OVERRIDE=\"${params.otu_force_prune_override ? '1' : '0'}\"" not in text
    assert "Invalid --${paramName} '${v}'. Allowed boolean values: true/false, 1/0, yes/no, on/off" in text


def test_main_nf_derive_glob_root_handles_explicit_glob_directories() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert "def explicitDir = prefix.endsWith('/') || prefix.endsWith(File.separator)" in text
    assert "def cleaned = prefix.replaceAll(" in text
    assert "def p = (explicitDir || idx == s.length()) ? f.getPath() : f.getParent()" in text


def test_main_nf_supports_primers_only_demultiplex_mode() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    # DemuxConfig class internals now live in lib/DemuxConfig.groovy (auto-loaded by Nextflow)
    demux_config_text = (REPO_ROOT / "lib" / "DemuxConfig.groovy").read_text(encoding="utf-8")
    assert "final String mode;" in demux_config_text
    assert 'DEMUX_MODE=\\"${mode ?: \'off\'}\\"' in demux_config_text
    assert "if (mode == 'primers_only') {" in text
    assert "return new DemuxConfig(true, 'primers_only', null, absPath(params.primer_indexes?.toString()))" in text
    assert '[ "\\$DEMUX_MODE" = "primers_only" ] && [ -f "\\$PRIMERS_PATH" ]' in text
    assert "--rename '{id}|sup|barcode={adapter_name}|adapter={adapter_name}'" in text
    assert "--rename '{id}|hac|barcode={adapter_name}|adapter={adapter_name}'" in text


def test_rtbioscan_precreates_realtime_directories_before_pipeline_start() -> None:
    text = (REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8")
    assert 'if [[ $feeder -eq 1 && -n "$run_id" ]]; then' in text
    assert '"${SCRIPT_DIR}/results/pod5/$run_id/reads_rt_round_pod5"' in text
    assert '"${SCRIPT_DIR}/results/pod5/$run_id/ori_round_pod5"' in text
    assert '"${SCRIPT_DIR}/results/pod5/$run_id/full_pod5"' in text
    assert '"${SCRIPT_DIR}/results/pod5/$run_id/done_round_pod5"' in text
    assert '"${SCRIPT_DIR}/results/pod5/$run_id/metadata"' in text


def test_rtbioscan_validates_feeder_input_folder_exists_and_checks_liveness() -> None:
    text = (REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8")
    assert 'if [[ $feeder -eq 1 && $skip_pod5 -eq 0 && -n "$input_folder" && ! -d "$input_folder" ]]; then' in text
    assert 'echo "ERROR: --input_folder directory does not exist: $input_folder" >&2' in text
    assert ': >"$feeder_log"' in text
    assert 'if ! kill -0 "$FEEDER_PID" 2>/dev/null; then' in text
    assert 'echo "ERROR: [RTBioScan] POD5 feeder exited immediately." >&2' in text
    assert 'echo "       Feeder log: $feeder_log" >&2' in text


def test_rtbioscan_defers_signal_cleanup_until_child_pids_are_recorded() -> None:
    text = (REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8")
    assert 'LAUNCH_IN_PROGRESS=0' in text
    assert 'PENDING_SIGNAL_NAME=""' in text
    assert 'PENDING_SIGNAL_EXIT_CODE=""' in text
    assert "finalize_pending_signal_if_needed() {" in text
    assert 'if [[ "${LAUNCH_IN_PROGRESS:-0}" -eq 1 ]]; then' in text
    assert 'finalize_pending_signal_if_needed' in text
    assert 'kill -s "$_signal" "$_nextflow_pid" 2>/dev/null || true' in text


def test_rtbioscan_prefers_explicit_primers_fasta_for_primer_indexes() -> None:
    text = (REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8")
    assert 'if [[ -n "$primers_fasta" ]]; then' in text
    assert 'nf_args+=(--primer_indexes "$primers_fasta")' in text
    assert 'nf_args+=(--primer_indexes "results/sample_info/$run_id/primers.fasta")' in text


def test_rtbioscan_examples_and_help_describe_config_derived_targets_for_metadata_paths() -> None:
    text = (REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8")
    assert '--targets <list>' in text
    assert "Optional pipe-separated marker targets; overrides" in text
    assert "config-derived params.targets and is forwarded to" in text
    assert "When --do_metadata / --feeder rely on config-derived params.targets" in text
    assert "-profile test   -c conf/file.config   -C conf/file.config" in text
    assert 'ERROR: explicit wrapper-level --targets is required when using --do_metadata or --feeder.' not in text
    assert 'Profile/config-derived targets do not satisfy this metadata setup requirement.' not in text


def test_usage_doc_lists_current_sample_info_artifacts_with_presence_tolerant_sidecars() -> None:
    text = (REPO_ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    assert "samples.txt             ← canonical 7-field compatibility projection" in text
    assert "{run_id}_metadata.txt   ← run-filtered metadata TSV rows" in text
    assert "replicate_roster.tsv    ← replicate-level roster, when present" in text
    assert "replicate_identity.tsv  ← collapse/track identity bridge, when present" in text
    assert "Writes the canonical 7-field compatibility projection" in text
    assert "Writes `results/sample_info/{run_id}/{run_id}_metadata.txt` as the run-filtered metadata TSV rows." in text
    assert (
        "Writes `results/sample_info/{run_id}/replicate_roster.tsv`, when present, and "
        "`results/sample_info/{run_id}/replicate_identity.tsv`, when present." in text
    )


def test_rtbioscan_has_config_target_resolution_helper_and_exact_command_layout() -> None:
    text = (REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8")
    assert "resolve_targets_from_config_or_die()" in text
    assert "partition_nextflow_args()" in text
    assert "build_normalized_nextflow_run_cmd()" in text
    assert 'cd "$SCRIPT_DIR"' in text
    assert "_config_cmd+=(config -flat)" in text
    assert 'NORMALIZED_NEXTFLOW_RUN_CMD+=(run main.nf)' in text
    assert '_config_cmd+=(main.nf)' in text
    assert 'NEXTFLOW_GLOBAL_ARGS+=("$_opt" "$_value")' in text
    assert 'NEXTFLOW_CONFIG_CMD_ARGS+=("$_opt" "$_value")' in text
    assert 'NEXTFLOW_RUN_ARGS+=("$_opt" "$_value")' in text
    assert '[[ -n "$resolved_targets" ]] && meta_args+=(--targets "$resolved_targets")' in text
    assert '[[ -n "$resolved_targets" ]] && feeder_args+=(--targets "$resolved_targets")' in text
    assert 'die_attached_config_selector "-profile"' in text
    assert 'die_attached_config_selector "-c"' in text
    assert 'die_attached_config_selector "-config"' in text
    assert 'die_attached_config_selector "-C"' in text
    assert 'die_attached_config_selector "-config-ignore-includes"' in text
    assert "config-affecting Nextflow option '$_opt' is missing its required value." in text
    assert "ERROR: unable to resolve params.targets from CLI --targets or the effective Nextflow config stack." in text
    assert "ERROR: failed to resolve params.targets via 'nextflow config -flat'; check profile/config arguments and config files." in text
    assert "ERROR: resolved params.targets from the effective Nextflow config stack is invalid; targets must be pipe-separated tokens without whitespace." in text


def test_rtbioscan_do_metadata_uses_config_derived_targets(tmp_path: Path) -> None:
    wrapper_root = tmp_path / "wrapper_root"
    (wrapper_root / "bin" / "lib").mkdir(parents=True)
    script = wrapper_root / "RTBioScan.sh"
    script.write_text((REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8"), encoding="utf-8")
    (wrapper_root / "bin" / "Metadata_pod5_processing.sh").write_text(
        (REPO_ROOT / "bin" / "Metadata_pod5_processing.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (wrapper_root / "bin" / "lib" / "stale_lock_utils.sh").write_text(
        (REPO_ROOT / "bin" / "lib" / "stale_lock_utils.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    sample_info_dir = wrapper_root / "results" / "sample_info" / "TestRun"
    env, log_path = _make_nextflow_shim(
        tmp_path,
        config_stdout="params.targets = 'ITS2|COI'\n",
        run_stderr="NEXTFLOW_RUN_SENTINEL\n",
        run_exit=86,
    )
    result = subprocess.run(
        ["bash", str(script), "--do_metadata", *_metadata_fixture_args(), "-profile", "test"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    assert result.returncode == 86
    assert "ERROR: unable to resolve params.targets" not in text
    assert "Targets: ITS2|COI" in result.stdout
    assert "==> [RTBioScan] Metadata setup complete." in result.stdout
    assert (sample_info_dir / "demult.fasta").exists()
    invocations = _read_nextflow_invocations(log_path)
    assert [args for _, args in invocations if "config" in args]


def test_rtbioscan_do_metadata_empty_targets_fail_in_feeder_metadata_path(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    fixtures = REPO_ROOT / "tests" / "fixtures" / "metadata"
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--do_metadata",
            "--run_id",
            "TestRun",
            "--targets",
            "",
            "--metadata",
            str(fixtures / "pipeline_info.tsv"),
            "--general_fasta",
            str(fixtures / "general.fasta"),
            "--primers_fasta",
            str(fixtures / "primers.fasta"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert "ERROR: wrapper option '--targets' is missing its required value." in text
    assert "ERROR: no valid targets were provided for metadata resolution" not in text
    assert "WARNING: --do_metadata is using the implicit default --targets" not in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/ for run 'TestRun' ..." not in text
    assert not (tmp_path / "results" / "sample_info" / "TestRun").exists()


def test_rtbioscan_pipeline_only_empty_targets_fail_before_pipeline_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    result = subprocess.run(
        ["bash", str(script), "--targets", "", "-profile", "test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert "ERROR: wrapper option '--targets' is missing its required value." in text
    assert "==> [RTBioScan] Starting pipeline ..." not in text


def test_rtbioscan_pipeline_only_whitespace_targets_fail_before_pipeline_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    result = subprocess.run(
        ["bash", str(script), "--targets", "   ", "-profile", "test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert "ERROR: explicit wrapper-level --targets must not be empty when using RTBioScan.sh." in text
    assert "==> [RTBioScan] Starting pipeline ..." not in text


def test_rtbioscan_pipeline_only_targets_with_internal_whitespace_fail_before_pipeline_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    result = subprocess.run(
        ["bash", str(script), "--targets", "CO I|ITS2", "-profile", "test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: explicit wrapper-level --targets contains invalid marker names; "
        "targets must be pipe-separated tokens without whitespace."
    ) in text
    assert "==> [RTBioScan] Starting pipeline ..." not in text


def test_rtbioscan_pipeline_only_targets_with_surrounding_token_whitespace_fail_before_pipeline_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    result = subprocess.run(
        ["bash", str(script), "--targets", " COI | ITS2 ", "-profile", "test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: explicit wrapper-level --targets contains invalid marker names; "
        "targets must be pipe-separated tokens without whitespace."
    ) in text
    assert "==> [RTBioScan] Starting pipeline ..." not in text


def test_rtbioscan_pipeline_only_targets_with_empty_separator_slot_fail_before_pipeline_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    result = subprocess.run(
        ["bash", str(script), "--targets", "COI||ITS2", "-profile", "test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: explicit wrapper-level --targets contains invalid marker names; "
        "targets must be pipe-separated tokens without whitespace."
    ) in text
    assert "==> [RTBioScan] Starting pipeline ..." not in text


def test_rtbioscan_pipeline_only_targets_with_trailing_empty_separator_slot_fail_before_pipeline_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    result = subprocess.run(
        ["bash", str(script), "--targets", "COI|", "-profile", "test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: explicit wrapper-level --targets contains invalid marker names; "
        "targets must be pipe-separated tokens without whitespace."
    ) in text
    assert "==> [RTBioScan] Starting pipeline ..." not in text


def test_rtbioscan_do_metadata_whitespace_targets_fail_before_metadata_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    fixtures = REPO_ROOT / "tests" / "fixtures" / "metadata"
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--do_metadata",
            "--run_id",
            "TestRun",
            "--targets",
            "   ",
            "--metadata",
            str(fixtures / "pipeline_info.tsv"),
            "--general_fasta",
            str(fixtures / "general.fasta"),
            "--primers_fasta",
            str(fixtures / "primers.fasta"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert "ERROR: explicit wrapper-level --targets must not be empty when using RTBioScan.sh." in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/ for run 'TestRun' ..." not in text
    assert "ERROR: no valid targets were provided for metadata resolution" not in text
    assert not (tmp_path / "results" / "sample_info" / "TestRun").exists()


def test_rtbioscan_do_metadata_targets_with_internal_whitespace_fail_before_metadata_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    fixtures = REPO_ROOT / "tests" / "fixtures" / "metadata"
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--do_metadata",
            "--run_id",
            "TestRun",
            "--targets",
            "COI|ITS 2",
            "--metadata",
            str(fixtures / "pipeline_info.tsv"),
            "--general_fasta",
            str(fixtures / "general.fasta"),
            "--primers_fasta",
            str(fixtures / "primers.fasta"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: explicit wrapper-level --targets contains invalid marker names; "
        "targets must be pipe-separated tokens without whitespace."
    ) in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/ for run 'TestRun' ..." not in text
    assert "ERROR: no valid targets were provided for metadata resolution" not in text
    assert not (tmp_path / "results" / "sample_info" / "TestRun").exists()


def test_rtbioscan_do_metadata_targets_with_surrounding_token_whitespace_fail_before_metadata_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    fixtures = REPO_ROOT / "tests" / "fixtures" / "metadata"
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--do_metadata",
            "--run_id",
            "TestRun",
            "--targets",
            " COI | ITS2 ",
            "--metadata",
            str(fixtures / "pipeline_info.tsv"),
            "--general_fasta",
            str(fixtures / "general.fasta"),
            "--primers_fasta",
            str(fixtures / "primers.fasta"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: explicit wrapper-level --targets contains invalid marker names; "
        "targets must be pipe-separated tokens without whitespace."
    ) in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/ for run 'TestRun' ..." not in text
    assert "ERROR: no valid targets were provided for metadata resolution" not in text
    assert not (tmp_path / "results" / "sample_info" / "TestRun").exists()


def test_rtbioscan_do_metadata_targets_with_empty_separator_slot_fail_before_metadata_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    fixtures = REPO_ROOT / "tests" / "fixtures" / "metadata"
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--do_metadata",
            "--run_id",
            "TestRun",
            "--targets",
            "COI||ITS2",
            "--metadata",
            str(fixtures / "pipeline_info.tsv"),
            "--general_fasta",
            str(fixtures / "general.fasta"),
            "--primers_fasta",
            str(fixtures / "primers.fasta"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: explicit wrapper-level --targets contains invalid marker names; "
        "targets must be pipe-separated tokens without whitespace."
    ) in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/ for run 'TestRun' ..." not in text
    assert "ERROR: no valid targets were provided for metadata resolution" not in text
    assert not (tmp_path / "results" / "sample_info" / "TestRun").exists()


def test_rtbioscan_do_metadata_targets_with_trailing_empty_separator_slot_fail_before_metadata_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    fixtures = REPO_ROOT / "tests" / "fixtures" / "metadata"
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--do_metadata",
            "--run_id",
            "TestRun",
            "--targets",
            "COI|",
            "--metadata",
            str(fixtures / "pipeline_info.tsv"),
            "--general_fasta",
            str(fixtures / "general.fasta"),
            "--primers_fasta",
            str(fixtures / "primers.fasta"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: explicit wrapper-level --targets contains invalid marker names; "
        "targets must be pipe-separated tokens without whitespace."
    ) in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/ for run 'TestRun' ..." not in text
    assert "ERROR: no valid targets were provided for metadata resolution" not in text
    assert not (tmp_path / "results" / "sample_info" / "TestRun").exists()


def test_rtbioscan_feeder_uses_config_derived_targets(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    env, _ = _make_nextflow_shim(
        tmp_path,
        config_stdout="params.targets = 'ITS2|COI'\n",
        run_stderr="NEXTFLOW_RUN_SENTINEL\n",
        run_exit=86,
    )
    try:
        result = subprocess.run(
            [
                "bash",
                str(script),
                "--feeder",
                "--run_id",
                "MyRun",
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "1",
                "-profile",
                "test",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        text = result.stdout + result.stderr
        assert result.returncode == 86
        assert "ERROR: unable to resolve params.targets" not in text
        assert "==> [RTBioScan] Feeder started" in result.stdout
        assert feeder_log.exists()
        deadline = time.time() + 2.0
        feeder_text = feeder_log.read_text(encoding="utf-8")
        while "Targets: ITS2|COI" not in feeder_text and time.time() < deadline:
            time.sleep(0.1)
            feeder_text = feeder_log.read_text(encoding="utf-8")
        assert "Targets: ITS2|COI" in feeder_text
    finally:
        if original_log is None:
            if feeder_log.exists():
                feeder_log.unlink()
        else:
            feeder_log.parent.mkdir(parents=True, exist_ok=True)
            feeder_log.write_text(original_log, encoding="utf-8")


def test_rtbioscan_feeder_empty_targets_fail_before_feeder_and_pipeline_start(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--feeder",
            "--run_id",
            "MyRun",
            "--input_folder",
            str(input_dir),
            "--targets",
            "",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert "ERROR: wrapper option '--targets' is missing its required value." in text
    assert "==> [RTBioScan] Starting POD5 feeder" not in text
    assert "==> [RTBioScan] Starting pipeline ..." not in text
    assert not (tmp_path / "results" / "pod5" / "MyRun").exists()


def test_rtbioscan_core_arg_errors_precede_target_resolution_failures(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"

    missing_run = subprocess.run(
        ["bash", str(script), "--do_metadata"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert missing_run.returncode != 0
    assert "--run_id is required with --feeder / --do_metadata" in missing_run.stderr
    assert "unable to resolve params.targets" not in missing_run.stderr

    missing_input = subprocess.run(
        ["bash", str(script), "--feeder", "--run_id", "MyRun"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert missing_input.returncode != 0
    assert "--feeder requires --input_folder" in missing_input.stderr
    assert "unable to resolve params.targets" not in missing_input.stderr


def test_rtbioscan_uses_resolved_targets_in_meta_and_feeder_args() -> None:
    text = (REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8")
    assert '[[ -n "$resolved_targets" ]] && meta_args+=(--targets "$resolved_targets")' in text
    assert '[[ -n "$resolved_targets" ]] && feeder_args+=(--targets "$resolved_targets")' in text
    assert 'if [[ $targets_explicit -eq 0 ]]; then' in text
    assert 'resolve_targets_from_config_or_die' in text


def test_rtbioscan_captures_nextflow_config_and_run_argv_with_correct_grouping_and_order(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    env, log_path = _make_nextflow_shim(
        tmp_path,
        config_stdout="params.targets = 'ITS2|COI'\n",
        run_stderr="NEXTFLOW_RUN_SENTINEL\n",
        run_exit=86,
    )
    cfg_one = tmp_path / "one.config"
    cfg_two = tmp_path / "two.config"
    cfg_one.write_text("params.foo = 'one'\n", encoding="utf-8")
    cfg_two.write_text("params.bar = 'two'\n", encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--do_metadata",
            *_metadata_fixture_args(),
            "-C",
            str(cfg_one),
            "-c",
            str(cfg_two),
            "-config-ignore-includes",
            "-profile",
            "test",
            "--watch",
            "false",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 86
    invocations = _read_nextflow_invocations(log_path)
    config_invocations = [(pwd, args) for pwd, args in invocations if "config" in args]
    assert len(config_invocations) == 1
    pwd, args = config_invocations[0]
    assert pwd == str(REPO_ROOT)
    assert args == [
        "-C",
        str(cfg_one),
        "-c",
        str(cfg_two),
        "-config-ignore-includes",
        "config",
        "-flat",
        "-profile",
        "test",
        "main.nf",
    ]
    run_invocations = [(pwd, args) for pwd, args in invocations if "run" in args]
    assert len(run_invocations) == 1
    run_pwd, run_args = run_invocations[0]
    assert run_pwd == str(REPO_ROOT)
    assert run_args[:6] == [
        "-C",
        str(cfg_one),
        "-c",
        str(cfg_two),
        "-config-ignore-includes",
        "run",
    ]
    assert run_args[6] == "main.nf"
    assert run_args[7:] == [
        "-profile",
        "test",
        "--watch",
        "false",
        "-name",
        "TestRun",
        "--run_id",
        "TestRun",
        "--reads",
        "results/pod5/TestRun/reads_rt_round_pod5/*pod5",
        "--ori_dir",
        "results/pod5/TestRun/ori_round_pod5/",
        "--indexes",
        "results/sample_info/TestRun/demult.fasta",
        "--primer_indexes",
        str(METADATA_FIXTURES / "primers.fasta"),
    ]
    assert "-C" not in run_args[7:]
    assert "-c" not in run_args[7:]
    assert "-config-ignore-includes" not in run_args[7:]


def test_rtbioscan_do_metadata_fails_when_config_has_no_targets(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    env, _ = _make_nextflow_shim(tmp_path, config_stdout="params.other = 'value'\n")
    result = subprocess.run(
        ["bash", str(script), "--do_metadata", "--run_id", "TestRun", "-profile", "test"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ERROR: unable to resolve params.targets from CLI --targets or the effective Nextflow config stack." in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/" not in text


def test_rtbioscan_do_metadata_fails_when_config_targets_are_invalid(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    env, _ = _make_nextflow_shim(tmp_path, config_stdout="params.targets = 'CO I|ITS2'\n")
    result = subprocess.run(
        ["bash", str(script), "--do_metadata", "--run_id", "TestRun", "-profile", "test"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ERROR: resolved params.targets from the effective Nextflow config stack is invalid; targets must be pipe-separated tokens without whitespace." in text
    assert (
        "ERROR: explicit wrapper-level --targets contains invalid marker names; "
        "targets must be pipe-separated tokens without whitespace."
    ) not in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/" not in text


def test_rtbioscan_do_metadata_fails_when_nextflow_config_command_fails(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    env, _ = _make_nextflow_shim(tmp_path, config_exit=7, run_exit=0, run_stderr="")
    result = subprocess.run(
        ["bash", str(script), "--do_metadata", "--run_id", "TestRun", "-profile", "test"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ERROR: failed to resolve params.targets via 'nextflow config -flat'; check profile/config arguments and config files." in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/" not in text


def test_rtbioscan_do_metadata_fails_when_config_selector_is_missing_its_value(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    result = subprocess.run(
        ["bash", str(script), "--do_metadata", "--run_id", "TestRun", "-profile"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ERROR: config-affecting Nextflow option '-profile' is missing its required value." in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/" not in text


def test_rtbioscan_do_metadata_rejects_attached_form_selector_when_config_lookup_is_needed(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    result = subprocess.run(
        ["bash", str(script), "--do_metadata", "--run_id", "TestRun", "-profile=test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ERROR: config-affecting Nextflow option '-profile' must use the separate-token form in RTBioScan.sh." in text
    assert "==> [RTBioScan] Setting up results/sample_info/TestRun/" not in text


def test_rtbioscan_do_metadata_with_explicit_targets_short_circuits_config_lookup_for_attached_forms(tmp_path: Path) -> None:
    script = REPO_ROOT / "RTBioScan.sh"
    env, log_path = _make_nextflow_shim(
        tmp_path,
        config_stdout="params.targets = 'SHOULD_NOT_BE_USED'\n",
        run_stderr="NEXTFLOW_RUN_SENTINEL\n",
        run_exit=86,
    )
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--do_metadata",
            "--targets",
            "ITS2|COI",
            *_metadata_fixture_args(),
            "-profile=test",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    text = result.stdout + result.stderr
    assert result.returncode == 86
    assert "ERROR: config-affecting Nextflow option '-profile' must use the separate-token form in RTBioScan.sh." not in text
    assert "Targets: ITS2|COI" in result.stdout
    invocations = _read_nextflow_invocations(log_path)
    assert not [args for _, args in invocations if "config" in args]
    run_invocations = [args for _, args in invocations if "run" in args]
    assert len(run_invocations) == 1
    assert "-profile=test" in run_invocations[0]


def test_rtbioscan_supports_temp_only_cleanup_modes() -> None:
    text = (REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8")
    assert 'LAUNCH_DIR="$(pwd -P)"' in text
    assert '--clean-ref <dir>' in text
    assert '--clean-temp <id>' in text
    assert '--clean-temp-all' in text
    assert 'choose only one cleanup mode: --clean, --clean-all, --clean-temp, or --clean-temp-all' in text
    assert 'append_cleanup_search_root "$LAUNCH_DIR"' in text
    assert 'append_cleanup_search_root "$SCRIPT_DIR"' in text
    assert 'history_contains_run()' in text
    assert 'resolve_cleanup_root_for_run()' in text
    assert 'lookup_state_id_for_run()' in text
    assert 'collect_nextflow_cache_targets_for_run()' in text
    assert 'feeder_lock_has_live_holder_for_root()' in text
    assert '"$_outdir/runs/${clean_run_id}"' in text
    assert '"${_root}/results/config/${_run_id}"' in text
    assert '"${_root}/results/runs/${_run_id}"' in text
    assert '"$_outdir/temp/ongoing/state/${_state_id}"' in text
    assert '"$_outdir/temp/current/state/${_state_id}"' in text
    assert '"$_outdir/ongoing/state/${_state_id}"' in text
    assert '_target_root="${clean_ref:-$LAUNCH_DIR}"' in text
    assert 'cd "$_target_root"' in text
    assert '_nextflow_clean_args=(-f -k "$clean_temp_run_id")' in text
    assert '[[ -d "${_target_root}/work" ]] && _work_dir="${_target_root}/work"' in text
    assert '"$_nextflow_bin" clean "${_nextflow_preview_args[@]}"' in text
    assert '"$_nextflow_bin" clean "${_nextflow_clean_args[@]}"' in text
    assert "per-task .command*" in text


def test_rtbioscan_clean_all_defaults_to_launch_dir(tmp_path: Path) -> None:
    launch_dir = tmp_path / "launch_root"
    (launch_dir / "results" / "report_html").mkdir(parents=True)
    (launch_dir / ".nextflow").mkdir()
    script = REPO_ROOT / "RTBioScan.sh"

    result = subprocess.run(
        ["bash", str(script), "--clean-all", "--dry-run"],
        cwd=launch_dir,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"under '{launch_dir}'" in result.stdout
    assert str(launch_dir / "results" / "report_html") in result.stdout


def test_rtbioscan_clean_temp_all_dry_run_does_not_require_nextflow(tmp_path: Path) -> None:
    launch_dir = tmp_path / "launch_root"
    wrapper_dir = tmp_path / "wrapper_root"
    (launch_dir / "results" / "temp").mkdir(parents=True)
    (launch_dir / "results" / "ongoing").mkdir(parents=True)
    (launch_dir / "work" / "ab").mkdir(parents=True)
    wrapper_dir.mkdir()
    script = wrapper_dir / "RTBioScan.sh"
    script.write_text((REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8"), encoding="utf-8")
    (wrapper_dir / "bin" / "lib").mkdir(parents=True)
    (wrapper_dir / "bin" / "lib" / "stale_lock_utils.sh").write_text(
        (REPO_ROOT / "bin" / "lib" / "stale_lock_utils.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(script), "--clean-temp-all", "--dry-run"],
        cwd=launch_dir,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"under '{launch_dir}'" in result.stdout
    assert str(launch_dir / "results" / "temp") in result.stdout
    assert str(launch_dir / "results" / "ongoing") in result.stdout
    assert f"clear   {launch_dir / 'work'}/*  (directory kept)" in result.stdout
    assert "nextflow executable not found" not in (result.stdout + result.stderr)


def test_rtbioscan_clean_run_prefers_launch_dir_history(tmp_path: Path) -> None:
    run_id = "unique_history_run"
    session_id = "cd7fd034-3ba3-42e9-b6b5-f7e4030cfe2e"
    launch_dir = tmp_path / "launch_root"
    (launch_dir / ".nextflow").mkdir(parents=True)
    (launch_dir / ".nextflow" / "cache" / session_id / "db").mkdir(parents=True)
    (launch_dir / ".nextflow" / "cache" / session_id / "db" / "LOCK").write_text("", encoding="utf-8")
    (launch_dir / "results" / "report_html" / "runs" / run_id).mkdir(parents=True)
    (launch_dir / "results" / "runs" / run_id / "report_assets" / ".private_signatures").mkdir(parents=True)
    (launch_dir / "work" / "ab").mkdir(parents=True)
    (launch_dir / ".nextflow" / "history").write_text(
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\t{session_id}\tnextflow run main.nf -name {run_id}\n",
        encoding="utf-8",
    )
    script = REPO_ROOT / "RTBioScan.sh"

    result = subprocess.run(
        ["bash", str(script), "--clean", run_id, "--dry-run"],
        cwd=launch_dir,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"in '{launch_dir}'" in result.stdout
    assert str(launch_dir / "results" / "report_html" / "runs" / run_id) in result.stdout
    assert str(launch_dir / "results" / "runs" / run_id) in result.stdout
    assert str(launch_dir / ".nextflow" / "cache" / session_id) in result.stdout
    assert "Nextflow cache/locks" in result.stdout
    assert f"nextflow -n -k {run_id}" in result.stdout
    assert f"clear   {launch_dir / 'work'}/*  (directory kept)" not in result.stdout


def test_rtbioscan_clean_run_removes_cache_locks_when_nextflow_clean_fails(tmp_path: Path) -> None:
    run_id = "cache_lock_run"
    session_id = "cd7fd034-3ba3-42e9-b6b5-f7e4030cfe2e"
    launch_dir = tmp_path / "launch_root"
    cache_dir = launch_dir / ".nextflow" / "cache" / session_id
    run_dir = launch_dir / "results" / "runs" / run_id
    cache_dir.joinpath("db").mkdir(parents=True)
    cache_dir.joinpath("db", "LOCK").write_text("", encoding="utf-8")
    run_dir.mkdir(parents=True)
    (launch_dir / ".nextflow" / "history").write_text(
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\t{session_id}\tnextflow run main.nf -name {run_id}\n",
        encoding="utf-8",
    )
    wrapper_root, script = _make_wrapper_root(tmp_path)
    env = os.environ.copy()
    log_path = _write_wrapper_nextflow_shim(
        wrapper_root,
        env,
        clean_stderr="Unable to acquire lock on session\n",
        clean_exit=1,
    )

    result = subprocess.run(
        ["bash", str(script), "--clean", run_id, "--yes"],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "removing run cache/locks so the run name can be reused" in result.stderr
    assert not cache_dir.exists()
    assert not run_dir.exists()
    assert (launch_dir / ".nextflow" / "history").read_text(encoding="utf-8") == ""
    invocations = _read_nextflow_invocations(log_path)
    assert [args for _, args in invocations if args and args[0] == "clean"] == [
        ["clean", "-n", "-k", run_id],
        ["clean", "-f", "-k", run_id],
    ]


def test_rtbioscan_clean_run_does_not_match_prefix_run_id_feeder(tmp_path: Path) -> None:
    run_id = "prefix-run-1"
    other_run_id = "prefix-run-12"
    launch_dir = tmp_path / "launch_root"
    run_dir = launch_dir / "results" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (launch_dir / ".nextflow").mkdir(parents=True)
    (launch_dir / ".nextflow" / "history").write_text(
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\tsession-id\tnextflow run main.nf -name {run_id}\n",
        encoding="utf-8",
    )
    wrapper_root, script = _make_wrapper_root(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{wrapper_root / 'bin'}:{env.get('PATH', '')}"
    log_path = _write_wrapper_nextflow_shim(wrapper_root, env, clean_stdout="NEXTFLOW_CLEAN_OK\n")
    _write_wrapper_ps_shim(
        wrapper_root,
        [f"999999 bash {wrapper_root / 'bin' / 'Metadata_pod5_processing.sh'} --run_id {other_run_id}"],
    )

    result = subprocess.run(
        ["bash", str(script), "--clean", run_id, "--yes"],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    text = result.stdout + result.stderr
    assert result.returncode == 0, text
    assert f"Checking for active feeder with run '{run_id}' before --clean" not in text
    assert not run_dir.exists()
    invocations = _read_nextflow_invocations(log_path)
    assert [args for _, args in invocations if args and args[0] == "clean"] == [
        ["clean", "-n", "-k", run_id],
        ["clean", "-f", "-k", run_id],
    ]


def test_rtbioscan_clean_prompt_precedes_same_run_feeder_stop() -> None:
    text = (REPO_ROOT / "RTBioScan.sh").read_text(encoding="utf-8")

    prompt_idx = text.index("if [[ $yes -eq 0 ]]; then")
    stop_idx = text.index('stop_clean_run_feeder_if_active_or_die "$_target_root" "$clean_run_id"')
    assert prompt_idx < stop_idx


def test_rtbioscan_clean_temp_dry_run_does_not_list_specific_work_dirs(tmp_path: Path) -> None:
    run_id = "temp_preview_run"
    launch_dir = tmp_path / "launch_root"
    work_dir = launch_dir / "work" / "aa" / "111111111111111111111111111111"
    work_dir.mkdir(parents=True)
    (launch_dir / ".nextflow").mkdir(parents=True)
    (launch_dir / "results" / "temp" / "ongoing" / "state" / run_id).mkdir(parents=True)
    (launch_dir / ".nextflow" / "history").write_text(
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\tsession\tnextflow run main.nf -name {run_id}\n",
        encoding="utf-8",
    )
    wrapper_root, script = _make_wrapper_root(tmp_path)
    env = os.environ.copy()
    log_path = _write_wrapper_nextflow_shim(wrapper_root, env, clean_stdout="NEXTFLOW_CLEAN_PREVIEW\n")

    result = subprocess.run(
        ["bash", str(script), "--clean-temp", run_id, "--dry-run"],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert str(work_dir) not in result.stdout
    invocations = _read_nextflow_invocations(log_path)
    assert not [args for _, args in invocations if args and args[0] == "log"]
    assert [args for _, args in invocations if args and args[0] == "clean"] == [["clean", "-n", "-k", run_id]]


def test_rtbioscan_clean_prompt_uses_tty_and_recovers_sane_mode(tmp_path: Path) -> None:
    run_id = "tty_clean_run"
    launch_dir = tmp_path / "launch_root"
    work_dir = launch_dir / "work" / "aa" / "111111111111111111111111111111"
    (launch_dir / ".nextflow").mkdir(parents=True)
    (launch_dir / "results" / "report_html" / "runs" / run_id).mkdir(parents=True)
    work_dir.mkdir(parents=True)
    (work_dir / ".command.sh").write_text("echo stub\n", encoding="utf-8")
    (launch_dir / ".nextflow" / "history").write_text(
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\tsession\tnextflow run main.nf -name {run_id}\n",
        encoding="utf-8",
    )
    wrapper_root, script = _make_wrapper_root(tmp_path)
    env = os.environ.copy()
    log_path = _write_wrapper_nextflow_shim(wrapper_root, env, clean_stdout="NEXTFLOW_CLEAN_OK\n")

    shell_command = (
        "stty -icanon echo -icrnl; "
        f"exec bash {shlex.quote(str(script))} --clean {shlex.quote(run_id)}"
    )
    exit_code, output = _run_command_via_pty(
        ["bash", "-lc", shell_command],
        cwd=launch_dir,
        env=env,
        wait_for_output="Delete the above? [y/N] ",
        input_bytes=b"y\r",
    )

    assert exit_code == 0, output
    assert "Delete the above? [y/N] " in output
    assert "==> [RTBioScan] Clean complete." in output
    assert "^M" not in output
    assert not (launch_dir / "results" / "report_html" / "runs" / run_id).exists()
    assert work_dir.exists()
    assert (work_dir / ".command.sh").exists()
    assert (launch_dir / ".nextflow" / "history").read_text(encoding="utf-8") == ""
    invocations = _read_nextflow_invocations(log_path)
    clean_invocations = [args for _, args in invocations if "clean" in args]
    assert len(clean_invocations) == 2
    assert clean_invocations[0] == ["clean", "-n", "-k", run_id]
    assert clean_invocations[1] == ["clean", "-f", "-k", run_id]


def test_rtbioscan_clean_temp_yes_preserves_work_stub_dirs(tmp_path: Path) -> None:
    run_id = "temp_remove_run"
    launch_dir = tmp_path / "launch_root"
    work_dir = launch_dir / "work" / "aa" / "111111111111111111111111111111"
    temp_state_dir = launch_dir / "results" / "temp" / "ongoing" / "state" / run_id
    current_state_dir = launch_dir / "results" / "temp" / "current" / "state" / run_id
    ongoing_state_dir = launch_dir / "results" / "ongoing" / "state" / run_id
    work_dir.mkdir(parents=True)
    (work_dir / ".command.sh").write_text("echo stub\n", encoding="utf-8")
    temp_state_dir.mkdir(parents=True)
    current_state_dir.mkdir(parents=True)
    ongoing_state_dir.mkdir(parents=True)
    (launch_dir / ".nextflow").mkdir(parents=True)
    history_text = (
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\tsession\tnextflow run main.nf -name {run_id}\n"
    )
    (launch_dir / ".nextflow" / "history").write_text(history_text, encoding="utf-8")
    wrapper_root, script = _make_wrapper_root(tmp_path)
    env = os.environ.copy()
    log_path = _write_wrapper_nextflow_shim(wrapper_root, env, clean_stdout="NEXTFLOW_CLEAN_OK\n")

    result = subprocess.run(
        ["bash", str(script), "--clean-temp", run_id, "--yes"],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert work_dir.exists()
    assert (work_dir / ".command.sh").exists()
    assert not temp_state_dir.exists()
    assert not current_state_dir.exists()
    assert not ongoing_state_dir.exists()
    assert (launch_dir / ".nextflow" / "history").read_text(encoding="utf-8") == history_text
    invocations = _read_nextflow_invocations(log_path)
    assert [args for _, args in invocations if args and args[0] == "clean"] == [
        ["clean", "-n", "-k", run_id],
        ["clean", "-f", "-k", run_id],
    ]


def test_rtbioscan_clean_temp_yes_handles_empty_rm_targets_without_nounset_failure(tmp_path: Path) -> None:
    run_id = "temp_no_targets_run"
    launch_dir = tmp_path / "launch_root"
    work_dir = launch_dir / "work" / "aa" / "111111111111111111111111111111"
    work_dir.mkdir(parents=True)
    (work_dir / ".command.sh").write_text("echo stub\n", encoding="utf-8")
    (launch_dir / ".nextflow").mkdir(parents=True)
    (launch_dir / ".nextflow" / "history").write_text(
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\tsession\tnextflow run main.nf -name {run_id}\n",
        encoding="utf-8",
    )
    wrapper_root, script = _make_wrapper_root(tmp_path)
    env = os.environ.copy()
    log_path = _write_wrapper_nextflow_shim(wrapper_root, env, clean_stdout="NEXTFLOW_CLEAN_OK\n")

    result = subprocess.run(
        ["bash", str(script), "--clean-temp", run_id, "--yes"],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in (result.stdout + result.stderr)
    assert "==> [RTBioScan] Clean complete." in result.stdout
    assert work_dir.exists()
    invocations = _read_nextflow_invocations(log_path)
    assert [args for _, args in invocations if args and args[0] == "clean"] == [
        ["clean", "-n", "-k", run_id],
        ["clean", "-f", "-k", run_id],
    ]


def test_rtbioscan_clean_without_tty_requires_yes(tmp_path: Path) -> None:
    run_id = "notty_clean_run"
    launch_dir = tmp_path / "launch_root"
    (launch_dir / ".nextflow").mkdir(parents=True)
    (launch_dir / "results" / "runs" / run_id).mkdir(parents=True)
    (launch_dir / ".nextflow" / "history").write_text(
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\tsession\tnextflow run main.nf -name {run_id}\n",
        encoding="utf-8",
    )
    wrapper_root, script = _make_wrapper_root(tmp_path)
    env = os.environ.copy()
    _write_wrapper_nextflow_shim(wrapper_root, env, clean_stdout="NEXTFLOW_CLEAN_PREVIEW\n")

    result = subprocess.run(
        ["bash", str(script), "--clean", run_id],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert (
        "ERROR: interactive cleanup confirmation requires a controlling terminal; "
        "rerun with --yes for non-interactive cleanup."
    ) in result.stderr
    assert (launch_dir / "results" / "runs" / run_id).exists()


def test_rtbioscan_clean_temp_does_not_remove_work_stubs_when_nextflow_clean_fails(tmp_path: Path) -> None:
    run_id = "temp_fail_run"
    launch_dir = tmp_path / "launch_root"
    work_dir = launch_dir / "work" / "aa" / "111111111111111111111111111111"
    work_dir.mkdir(parents=True)
    (work_dir / ".command.sh").write_text("echo stub\n", encoding="utf-8")
    (launch_dir / ".nextflow").mkdir(parents=True)
    (launch_dir / ".nextflow" / "history").write_text(
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\tsession\tnextflow run main.nf -name {run_id}\n",
        encoding="utf-8",
    )
    wrapper_root, script = _make_wrapper_root(tmp_path)
    env = os.environ.copy()
    log_path = _write_wrapper_nextflow_shim(wrapper_root, env, clean_stderr="NEXTFLOW_CLEAN_FAIL\n", clean_exit=1)

    result = subprocess.run(
        ["bash", str(script), "--clean-temp", run_id, "--yes"],
        cwd=launch_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "ERROR: Nextflow work/ temp cleanup failed." in result.stderr
    assert work_dir.exists()
    assert (work_dir / ".command.sh").exists()
    invocations = _read_nextflow_invocations(log_path)
    assert [args for _, args in invocations if args and args[0] == "clean"] == [
        ["clean", "-n", "-k", run_id],
        ["clean", "-f", "-k", run_id],
    ]


def test_rtbioscan_clean_ref_targets_explicit_root(tmp_path: Path) -> None:
    run_id = "unique_ref_run"
    launch_dir = tmp_path / "elsewhere"
    ref_dir = tmp_path / "ref_root"
    launch_dir.mkdir()
    (ref_dir / ".nextflow").mkdir(parents=True)
    (ref_dir / "results" / "sample_info" / run_id).mkdir(parents=True)
    (ref_dir / ".nextflow" / "history").write_text(
        f"2026-03-21 00:00:00\t1s\t{run_id}\tOK\thash\tsession\tnextflow run main.nf -name {run_id}\n",
        encoding="utf-8",
    )
    script = REPO_ROOT / "RTBioScan.sh"

    result = subprocess.run(
        ["bash", str(script), "--clean-ref", str(ref_dir), "--clean", run_id, "--dry-run"],
        cwd=launch_dir,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"in '{ref_dir}'" in result.stdout
    assert str(ref_dir / "results" / "sample_info" / run_id) in result.stdout


def test_main_nf_validates_otu_blast_min_members_and_mode() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    config_text = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    assert 'otu_blast_min_members = 3' in config_text
    assert "def otuBlastMinMembersStr = params.otu_blast_min_members.toString().trim()" in text
    assert "Invalid --otu_blast_min_members '${params.otu_blast_min_members}'. Provide an integer >= 0." in text
    assert 'otu_blast_filter_mode = "enforce"' in config_text
    assert "def otuBlastFilterModeCanonical = params.otu_blast_filter_mode.toString().trim().toLowerCase()" in text
    assert "Invalid --otu_blast_filter_mode '${params.otu_blast_filter_mode}'. Allowed values: off, observe, enforce" in text
    assert 'otu_blast_filter_skip_rounds = "3"' in config_text
    assert "def otuBlastFilterSkipRoundsRaw = params.otu_blast_filter_skip_rounds.toString().trim().toLowerCase()" in text
    assert "Invalid --otu_blast_filter_skip_rounds '${params.otu_blast_filter_skip_rounds}'. Allowed values: none, all, or integer >= 0." in text
    assert 'otu_blast_enforce_missing_max_frac = 0.1' in config_text
    assert "def otuBlastEnforceMissingMaxFracStr = params.otu_blast_enforce_missing_max_frac.toString().trim()" in text
    assert "Invalid --otu_blast_enforce_missing_max_frac '${params.otu_blast_enforce_missing_max_frac}'. Provide a decimal fraction in [0,1]." in text
    assert 'otu_blast_enforce_no_clusters_policy = "fallback_unfiltered"' in config_text
    assert "def otuBlastEnforceNoClustersPolicyCanonical = params.otu_blast_enforce_no_clusters_policy.toString().trim().toLowerCase()" in text
    assert "Invalid --otu_blast_enforce_no_clusters_policy '${params.otu_blast_enforce_no_clusters_policy}'. Allowed values: fail, fallback_unfiltered, allow_empty" in text
    assert 'otu_size_streak_mode = "enforce"' in config_text
    assert "def otuSizeStreakModeCanonical = params.otu_size_streak_mode.toString().trim().toLowerCase()" in text
    assert "Invalid --otu_size_streak_mode '${params.otu_size_streak_mode}'. Allowed values: off, observe, enforce" in text
    assert 'otu_size_streak_min_rounds = 3' in config_text
    assert "def otuSizeStreakMinRoundsStr = params.otu_size_streak_min_rounds.toString().trim()" in text
    assert "Invalid --otu_size_streak_min_rounds '${params.otu_size_streak_min_rounds}'. Provide an integer >= 1." in text


def test_main_nf_exports_otu_blast_filter_env_scaffolding() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert 'OTU_BLAST_MIN_MEMBERS="${otuBlastMinMembersStr}"' in text
    assert 'OTU_BLAST_FILTER_MODE="${otuBlastFilterModeCanonical}"' in text
    assert 'OTU_BLAST_FILTER_SKIP_ROUNDS="${otuBlastFilterSkipRoundsCanonical}"' in text
    assert 'OTU_BLAST_ENFORCE_MISSING_MAX_FRAC="${otuBlastEnforceMissingMaxFracStr}"' in text
    assert 'OTU_BLAST_ENFORCE_NO_CLUSTERS_POLICY="${otuBlastEnforceNoClustersPolicyCanonical}"' in text


def test_main_nf_wires_otu_blast_filter_helper_in_blast_process() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    helper_text = (REPO_ROOT / "bin" / "blast_otu_pretax.sh").read_text(encoding="utf-8")
    blast_block = text.split("process blast_OTU_pretax {", 1)[1].split("process _reporting_blast_pretax {", 1)[0]
    assert 'BLAST_INPUT_FASTA="${fasta_hq_qced}"' in text
    assert 'BLAST_FILTER_MISSING_POLICY="keep"' in text
    assert 'BLAST_FILTER_DECISION="${barcode}_blast_filter_decision.tsv"' in text
    assert 'ROUND_HASH_MAP="${barcode}_blast_round_hash_map.tsv"' in text
    assert 'ROUND_HASH_COUNTS="${barcode}_blast_round_hash_counts.tsv"' in text
    assert '"\\$BIN_DIR/otu_filter_reads_by_otu_size.pl"' in text
    assert '"\\$BIN_DIR/otu_blast_filter_decide.sh"' in text
    assert '${baseDir}/bin/otu_blast_effective_mode.sh' in text
    assert '${baseDir}/bin/round_index_assign.sh' in text
    assert '"${qced_reads_nr}"' in text
    assert '"\\$ROUND_HASH_MAP"' in text
    assert '"\\$ROUND_HASH_COUNTS"' in text
    assert '${baseDir}/bin/otu_hash_map_from_fasta.pl' in text
    assert '"\\$OTU_BLAST_MIN_MEMBERS"' in text
    assert '"\\$BLAST_FILTERED_FASTA"' in text
    assert '"\\$BLAST_FILTER_STATS"' in text
    assert '"\\$BLAST_FILTER_KEPT_OTUS"' in text
    assert '"\\$BLAST_FILTER_MISSING_POLICY"' in text
    assert '"\\$BLAST_FILTER_DECISION"' in text
    assert '"\\$OTU_BLAST_ENFORCE_MISSING_MAX_FRAC"' in text
    assert '"\\$OTU_BLAST_ENFORCE_NO_CLUSTERS_POLICY"' in text
    assert 'round_barcode="${round_barcode}"' in blast_block
    assert 'ROUND_INDEX_FILE="\\${STATE_DIR}/round_index.tsv"' in text
    assert 'OTU_BLAST_EFFECTIVE_MODE=\\$(effective_mode_value effective_mode)' in text
    assert 'if [ "\\$OTU_BLAST_EFFECTIVE_MODE" = "enforce" ]; then' in text
    assert 'BLAST_FILTER_MISSING_POLICY="drop"' in text
    assert 'if [ "\\$OTU_BLAST_EFFECTIVE_MODE" = "observe" ] || [ "\\$OTU_BLAST_EFFECTIVE_MODE" = "enforce" ]; then' in text
    assert 'decision_value() {' in text
    assert 'decision=\\$(decision_value decision)' in text
    assert 'reason=\\$(decision_value reason)' in text
    assert 'INFO: otu_blast_filter force_use_filtered=1 bypassing threshold and no-cluster decision knobs' in text
    assert 'BLAST_INPUT_FASTA="\\$BLAST_FILTERED_FASTA"' in text
    assert 'count_marker_reads() {' in blast_block
    assert 'extract_marker_reads() {' in blast_block
    assert 'MARKER_RESCUE_STATS="\\$ROUND_DIR/${barcode}_blast_marker_rescue.tsv"' in blast_block
    assert 'if [ "\\$orig_marker_count" -gt 0 ] && [ "\\$filtered_marker_count" -lt "\\$OTU_BLAST_MIN_MEMBERS" ]; then' in blast_block
    assert 'echo "INFO: rescuing sparse marker=\\$target_marker into BLAST input because filtered_count=\\$filtered_marker_count < min_members=\\$OTU_BLAST_MIN_MEMBERS (original_count=\\$orig_marker_count)" 1>&2' in blast_block
    assert '_p_targets="${params.targets}"' in text
    assert 'IFS=\'|\' read -ra _TARGETS   <<< "\\$_p_targets"' in text
    assert '"\\$BIN_DIR/blast_otu_pretax.sh" \\' in blast_block
    assert '"\\$_p_targets" \\' in blast_block
    assert '"\\$_p_blast_db_specs" \\' in blast_block
    assert 'seqkit grep -r -p' in helper_text
    assert '"${BARCODE}_${target}.fasta"' in helper_text
    assert '"${BASE_DIR}/bin/get_blast_taxdepth.pl"' in helper_text
    assert 'target_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/rtbioscan_blast_taxdepth_${idx}.XXXXXX")"' in helper_text
    assert 'run_target_worker() {' in helper_text
    assert 'active_indices=()' in helper_text
    assert 'concurrency="$THREADS"' in helper_text
    assert '-num_threads "$worker_threads"' in helper_text
    assert 'if ! seqkit grep -r -p "\\\\|${target}\\\\|" "$BLAST_INPUT_FASTA" > "$target_fasta"; then' in helper_text
    assert 'if [ ! -s "$target_fasta" ]; then' in helper_text


def test_main_nf_makes_otu_refine_failures_explicit_and_uses_new_helpers() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert 'if ! OTU_REFINE_ROUND_ID="${round_barcode}" \\' in text
    assert 'OTU_REFINE_PHASE_TIMINGS_FILE="\\$OTU_REFINE_PHASE_TIMINGS_FILE" \\' in text
    assert 'OTU_REFINE_PHASE_TIMINGS_MS_FILE="\\$OTU_REFINE_PHASE_TIMINGS_MS_FILE" \\' in text
    assert 'OTU_REFINE_WORKLOAD_STATS_FILE="\\$OTU_REFINE_WORKLOAD_STATS_FILE" \\' in text
    assert 'bash "\\$BIN_DIR/otu_refine_blastreport_parallel.sh" \\' in text
    assert 'ERROR: otu_refine_blastreport_parallel.sh failed for ${barcode}/${round_barcode}' in text
    assert '"\\$BIN_DIR/filter_sup_non_no_adapter_fasta.pl"' in text
    assert '${baseDir}/bin/detect_consensus_sample_mode.sh' in text
    assert "grep -q 'adapter=barcode' blast_report_annotated.txt" not in text
    wrapper_text = (REPO_ROOT / "bin" / "otu_refine_blastreport_parallel.sh").read_text(encoding="utf-8")
    assert 'PHASE_TIMINGS_FILE="${OTU_REFINE_PHASE_TIMINGS_FILE:-otu_refine_phase_timings.tsv}"' in wrapper_text
    assert 'PHASE_TIMINGS_MS_FILE="${OTU_REFINE_PHASE_TIMINGS_MS_FILE:-otu_refine_phase_timings_ms.tsv}"' in wrapper_text
    assert 'WORKLOAD_STATS_FILE="${OTU_REFINE_WORKLOAD_STATS_FILE:-otu_refine_workload_stats.tsv}"' in wrapper_text
    assert 'export OTU_REFINE_ANNOTATE_STATS_FILE="$annotate_stats_file"' in wrapper_text
    assert 'shard_manifest="$tmp_root/shard_manifest.tsv"' in wrapper_text
    assert 'cluster_sizes="$tmp_root/cluster_sizes.tsv"' in wrapper_text
    assert 'done < "$cluster_sizes"' in wrapper_text
    assert 'cluster_taxids="$tmp_root/cluster_taxids.tsv"' in wrapper_text
    assert 'printf \'shard_scheduler_mode\\t%s\\n\' "$shard_scheduler_mode"' in wrapper_text
    assert 'printf \'merged_pairs_rows\\t%s\\n\' "$merged_pairs_rows"' in wrapper_text
    assert 'printf \'annotated_file_bytes\\t%s\\n\' "$annotated_file_bytes"' in wrapper_text
    assert 'append_phase "wrapper_total"' in wrapper_text
    assert "invalid record count '$record_count' for cluster $cluster_id in cluster_sizes.tsv" in wrapper_text
    assert 'write_cluster_taxids_with_phase_timings($ARGV[0], $ARGV[1], $ARGV[2], $ARGV[3], $ARGV[4], $ARGV[5])' in wrapper_text
    assert "observed_clusters=\"$(grep -cve '^[[:space:]]*$' \"$cluster_taxids\" || true)\"" in wrapper_text
    assert 'ERROR: cluster_taxids.tsv is incomplete' in wrapper_text
    assert 'append_phase "shard_plan"' in wrapper_text
    assert 'append_phase "shard_materialize"' in wrapper_text
    assert 'append_phase "shard_expand_workers"' in wrapper_text
    assert 'append_phase "pair_merge"' in wrapper_text
    assert '"$cluster_sizes" \\' in wrapper_text
    assert 'write_workload_stats() {' in wrapper_text
    module_text = (REPO_ROOT / "bin" / "lib" / "RTBioScan" / "OTURefineBlastreport.pm").read_text(encoding="utf-8")
    assert 'use Time::HiRes qw(time);' in module_text
    assert 'sub _append_phase_timing {' in module_text
    assert 'sub write_cluster_taxids_with_phase_timings {' in module_text
    assert "'load_blast_tax_map'" in module_text
    assert "'cluster_taxid_pass'" in module_text
    assert "'lineage_resolution'" in module_text
    assert "'annotated_emit'" in module_text


def test_main_nf_tracks_sup_path_stats_and_timings() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    helper_text = SUP_PATH_HELPER.read_text(encoding="utf-8")
    assert 'SUP_PATH_STATS_FILE="${barcode}_sup_path_stats.tsv"' in text
    assert 'SUP_PATH_TIMINGS_MS_FILE="${barcode}_sup_path_timings_ms.tsv"' in text
    assert 'printf \'round_barcode\\tphase\\tseconds\\tms\\n\' > "\\$SUP_PATH_TIMINGS_MS_FILE"' in text
    assert "printf 'hac2sup_candidate_rows\\t0\\n'" in text
    assert "printf 'hac2sup_candidate_unique_read_ids\\t0\\n'" in text
    assert "printf 'sup_annotation_input_rows\\t0\\n'" in text
    assert "printf 'dorado_sup_sam_records\\t0\\n'" in text
    assert "printf 'dorado_sup_fastq_reads\\t0\\n'" in text
    assert "printf 'hac2sup_sup_fasta_reads\\t0\\n'" in text
    assert "printf 'sup_cache_hit_ids\\t0\\n'" in text
    assert "printf 'sup_cache_miss_ids\\t0\\n'" in text
    assert "printf 'sup_cache_restored_fastq_reads\\t0\\n'" in text
    assert "printf 'sup_cache_restored_summary_rows\\t0\\n'" in text
    assert "printf 'dorado_sup_reads_requested\\t0\\n'" in text
    assert "printf 'dorado_sup_sam_records_new\\t0\\n'" in text
    assert "printf 'dorado_sup_fastq_reads_new\\t0\\n'" in text
    assert "printf 'dorado_sup_summary_rows_new\\t0\\n'" in text
    assert "printf 'sup_pre_fastq_reads_merged\\t0\\n'" in text
    assert "printf 'sup_summary_rows_merged\\t0\\n'" in text
    assert "printf 'sup_cache_restore_missing_fastq_ids\\t0\\n'" in text
    assert "printf 'sup_cache_restore_missing_summary_ids\\t0\\n'" in text
    assert "printf 'shared_extract_union_ids\\t0\\n'" in text
    assert "printf 'shared_extract_hac2sup_ids\\t0\\n'" in text
    assert "printf 'shared_extract_hac_fixed_ids\\t0\\n'" in text
    assert "printf 'shared_extract_fasta_reads\\t0\\n'" in text
    assert 'SUP_CACHE_SKIP_PERSIST=0' in text
    assert 'source "\\$BIN_DIR/blast_sup_path.sh"' in text
    assert 'sup_candidate_extract' in text
    assert 'sup_shared_candidate_extract' in text
    assert 'sup_cache_lookup' in text
    assert 'sup_build_new_summary' in text
    assert 'sup_merge_outputs' in text
    assert 'sup_cache_persist' in text
    assert 'sup_post_dorado' in text
    assert 'sup_write_stats' in text
    assert 'sup_candidate_extract() {' in helper_text
    assert 'sup_shared_candidate_extract() {' in helper_text
    assert 'sup_cache_lookup() {' in helper_text
    assert 'sup_build_new_summary() {' in helper_text
    assert 'sup_merge_outputs() {' in helper_text
    assert 'sup_cache_persist() {' in helper_text
    assert 'sup_post_dorado() {' in helper_text
    assert 'sup_write_stats() {' in helper_text
    assert 'SUP_CACHE_SCHEMA_VERSION="2"' in text
    assert 'sup_summary_id_list() {' in helper_text
    assert 'append_sup_path_timing "sup_candidate_extract"' in helper_text
    assert 'append_sup_path_timing "shared_candidate_extract"' in helper_text
    assert 'append_sup_path_timing "sup_cache_lookup"' in helper_text
    assert 'append_sup_path_timing "sup_cache_restore_fastq"' in helper_text
    assert 'append_sup_path_timing "sup_cache_restore_summary"' in helper_text
    assert 'append_sup_path_timing "dorado_sup_basecaller"' in text
    assert 'append_sup_path_timing "sup_fastq_recover"' in text
    assert 'append_sup_path_timing "hac2sup_sup_fastq_subseq"' in helper_text
    assert 'append_sup_path_timing "hac2sup_sup_fasta_convert"' in helper_text
    assert 'append_sup_path_timing "hac2sup_header_remap"' in helper_text
    assert 'append_sup_path_timing "sup_annotation_prepare_and_emit"' in helper_text
    assert 'append_sup_path_timing "sup_fastq_merge"' in helper_text
    assert 'append_sup_path_timing "sup_summary_merge"' in helper_text
    assert 'append_sup_path_timing "sup_cache_persist"' in helper_text
    assert 'append_sup_path_timing "read_pident_build"' in text
    assert 'append_sup_path_timing "blocked_otu_build"' in text
    assert 'append_sup_path_timing "select_reads2sup"' in text
    assert 'append_sup_path_timing "taxonomy_cleanup_emit"' in text
    assert 'append_sup_path_timing "hac_fixed_extract"' in text
    assert "awk 'NF && !seen[$0]++{print $0}'" in helper_text
    assert '"${barcode}_blastreport_hac_unique.list"' in helper_text
    assert 'hac2sup_candidate_unique_read_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_unique.list")' in helper_text
    assert 'sup_count_nonempty_lines() {' in helper_text
    assert 'sup_unique_ids_in_order() {' in helper_text
    assert 'sup_fasta_extract_ordered() {' in helper_text
    assert 'sup_emit_zero_timing() {' in helper_text
    assert 'sup_emit_zero_timing "sup_cache_lookup"' in helper_text
    assert 'sup_emit_zero_timing "sup_cache_restore_fastq"' in helper_text
    assert 'sup_emit_zero_timing "sup_cache_restore_summary"' in helper_text
    assert 'sup_annotation_input_rows=$(sup_count_nonempty_lines "${barcode}_blastreport_sup.list")' in helper_text
    assert 'dorado_sup_summary_rows_new=$(sup_count_summary_rows "${barcode}_round_sup_new.tsv")' in helper_text
    assert 'dorado_sup_fastq_reads="$sup_pre_fastq_reads_merged"' in helper_text
    assert "hac2sup_sup_fasta_reads=$(awk '/^>/{c++} END{print c+0}' \"${barcode}_qced_reads_hq_hac2sup_sup.fasta\" 2>/dev/null || echo 0)" in helper_text
    assert 'echo "WARN: reclassifying incomplete SUP cache payloads as Dorado misses for this round"' in helper_text
    assert 'SUP cache hit/miss accounting drift detected after cache correction' in helper_text
    assert 'if ! sup_cache_prepare_locked; then' in helper_text
    assert 'if ! sup_summary_id_list "${barcode}_round_sup_new.tsv" "${barcode}_sup_cache_new_summary_ids.list"; then' in helper_text
    assert 'summary header lacks read_id' in helper_text
    assert 'sup_emit_zero_timing "sup_cache_persist"' in helper_text
    assert 'shared_extract_union_ids=$(sup_count_nonempty_lines "${barcode}_shared_extract_union_ids.list")' in helper_text
    assert 'sup_fasta_extract_ordered \\' in helper_text
    assert 'sup_emit_zero_timing "sup_fastq_merge"' in text
    assert 'sup_emit_zero_timing "sup_summary_merge"' in text
    assert 'sup_emit_zero_timing "sup_cache_persist"' in text
    assert 'copy_soft "\\$SUP_PATH_STATS_FILE" "\\$ROUND_DIR/${barcode}_sup_path_stats.tsv"' in text
    assert 'copy_soft "\\$SUP_PATH_TIMINGS_MS_FILE" "\\$ROUND_DIR/${barcode}_sup_path_timings_ms.tsv"' in text
    assert 'copy_soft "\\$SUP_PATH_TIMINGS_MS_FILE" "\\${STATE_DIR}/${barcode}_sup_path_timings_ms_last.tsv"' in text
    assert 'copy_soft ${barcode}_blastreport_hac.list "\\$ROUND_DIR/${barcode}_blastreport_hac.list"' in text
    assert 'copy_soft ${barcode}_blastreport_sup.list "\\$ROUND_DIR/${barcode}_blastreport_sup.list"' in text


def test_sup_path_stats_semantics_fixture(tmp_path: Path) -> None:
    sup_list = tmp_path / "RTBioScan_blastreport_sup.list"
    sup_list.write_text("r1\tCOI\nr2\tITS2\n", encoding="utf-8")
    sup_fastq = tmp_path / "RTBioScan_blastreport_sup_pre.fastq"
    sup_fastq.write_text(
        "@r1\nACGT\n+\n####\n"
        "@r2\nTGCA\n+\n####\n",
        encoding="utf-8",
    )
    sup_fasta = tmp_path / "RTBioScan_qced_reads_hq_hac2sup_sup.fasta"
    sup_fasta.write_text(
        ">r1|COI|hac|barcode=b1|adapter=b1\nACGT\n"
        ">r2|ITS2|hac|barcode=b2|adapter=b2\nTGCA\n",
        encoding="utf-8",
    )
    sam = tmp_path / "RTBioScan_blastreport_sup.sam"
    sam.write_text(
        "@SQ\tSN:ref\tLN:4\n"
        "r1\t0\tref\t1\t60\t4M\t*\t0\t0\tACGT\t####\n"
        "r2\t0\tref\t1\t60\t4M\t*\t0\t0\tTGCA\t####\n",
        encoding="utf-8",
    )
    timings = tmp_path / "RTBioScan_sup_path_timings_ms.tsv"
    timings.write_text(
        "round_barcode\tphase\tseconds\tms\n"
        "TS_Jun_0\tsup_annotation_prepare_and_emit\t0\t12\n",
        encoding="utf-8",
    )

    sup_annotation_input_rows = sum(
        1 for line in sup_list.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    dorado_sup_sam_records = sum(
        1 for line in sam.read_text(encoding="utf-8").splitlines() if line and not line.startswith("@")
    )
    dorado_sup_fastq_reads = len(sup_fastq.read_text(encoding="utf-8").splitlines()) // 4
    hac2sup_sup_fasta_reads = sum(
        1 for line in sup_fasta.read_text(encoding="utf-8").splitlines() if line.startswith(">")
    )
    header, row = timings.read_text(encoding="utf-8").splitlines()
    fields = row.split("\t")

    assert sup_annotation_input_rows == 2
    assert dorado_sup_sam_records == 2
    assert dorado_sup_fastq_reads == 2
    assert hac2sup_sup_fasta_reads == 2
    assert header == "round_barcode\tphase\tseconds\tms"
    assert fields[1] == "sup_annotation_prepare_and_emit"
    assert fields[3].isdigit()


def test_main_nf_derives_no_adapter_policy_from_observed_annotations() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert 'OBSERVED_NO_ADAPTER=0' in text
    assert 'OBSERVED_NON_NO_ADAPTER=0' in text
    assert 'NO_ADAPTER_POLICY_TSV="${barcode}_no_adapter_policy.tsv"' in text
    init_idx = text.index(': > "\\$NO_ADAPTER_POLICY_TSV"')
    guard_idx = text.index('if [ -s blast_report_annotated_otu_evidence.txt ]; then')
    validate_idx = text.index('validate_no_adapter_policy_value separate_no_adapter "\\$SEPARATE_NO_ADAPTER"')
    cp_idx = text.index('copy_soft "\\$NO_ADAPTER_POLICY_TSV" "\\$ROUND_DIR/\\$NO_ADAPTER_POLICY_TSV"')
    breakdown_idx = text.index('append_otu_refine_breakdown "no_adapter_policy"')
    assert init_idx < guard_idx
    assert validate_idx < cp_idx
    assert cp_idx < breakdown_idx
    assert 'if ! bash "\\$BIN_DIR/detect_no_adapter_policy.sh" blast_report_annotated_otu_evidence.txt > "\\$NO_ADAPTER_POLICY_TSV"; then' in text
    assert 'ERROR: detect_no_adapter_policy.sh failed for ${barcode}/${round_barcode}' in text
    assert 'no_adapter_policy_value() {' in text
    assert 'validate_no_adapter_policy_value() {' in text
    assert 'OBSERVED_NO_ADAPTER=\\$(no_adapter_policy_value observed_no_adapter)' in text
    assert 'OBSERVED_NON_NO_ADAPTER=\\$(no_adapter_policy_value observed_non_no_adapter)' in text
    assert 'SEPARATE_NO_ADAPTER=\\$(no_adapter_policy_value separate_no_adapter)' in text
    assert 'case "\\$value" in' in text
    assert '0|1)' in text
    assert 'ERROR: invalid no-adapter policy value for \\${key}: \'\\${value}\'' in text
    assert 'validate_no_adapter_policy_value observed_no_adapter "\\$OBSERVED_NO_ADAPTER"' in text
    assert 'validate_no_adapter_policy_value observed_non_no_adapter "\\$OBSERVED_NON_NO_ADAPTER"' in text
    assert 'validate_no_adapter_policy_value separate_no_adapter "\\$SEPARATE_NO_ADAPTER"' in text
    assert 'SEPARATE_NO_ADAPTER=0' in text
    assert 'mkdir -p "\\$ROUND_DIR" 2>/dev/null || true' in text
    assert 'INFO: no_adapter_policy observed_no_adapter=\\$OBSERVED_NO_ADAPTER observed_non_no_adapter=\\$OBSERVED_NON_NO_ADAPTER separate_no_adapter=\\$SEPARATE_NO_ADAPTER' in text
    assert 'cp blast_report_annotated_otu.txt blast_report_annotated_otu_evidence.txt 2>/dev/null || true' in text
    assert 'if ! "\\$BIN_DIR/select_reads2sup.pl" blast_report_annotated_otu.txt 50 keep_no_adapter ${barcode}_read_pident.tsv "\\$BLOCKED_OTU" > tmp; then' in text
    assert 'ERROR: select_reads2sup.pl failed for ${barcode}/${round_barcode}' in text
    assert '"\\$BIN_DIR/select_reads2sup.pl" blast_report_annotated_otu.txt 50 keep_no_adapter ${barcode}_read_pident.tsv "\\$BLOCKED_OTU" > tmp || :' not in text
    assert "sed -E 's/[Kpcofgs]__//g' tmp > blast_report_annotated_otu.txt || mv tmp blast_report_annotated_otu.txt" in text
    assert "sed -E 's/[Kpcofgs]__//g' blast_report_annotated_otu.txt \\" in text
    assert '"\\$BIN_DIR/prefer_blast_rows_by_model.sh" \\' in text
    assert '--output blast_report_annotated_preferred.txt \\' in text
    assert '--policy sup_hac2sup_preferred; then' in text
    assert '--mode annotated_tsv' not in text
    assert 'bash "\\$BIN_DIR/filter_blast_rows_by_adapter_class.sh" blast_report_annotated_otu_evidence.txt no_adapter > blast_report_annotated_otu_noadapter.txt' in text
    assert 'WARN: filter_blast_rows_by_adapter_class.sh failed for no_adapter split; continuing without split report' in text


def test_no_adapter_policy_round_dir_retention_with_empty_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "blast_report_annotated_otu_evidence.txt"
    evidence.write_text("", encoding="utf-8")
    policy = tmp_path / "RTBioScan_no_adapter_policy.tsv"
    round_dir = tmp_path / "state" / "TS_Jun_0"

    script = f"""
set -euo pipefail
NO_ADAPTER_POLICY_TSV="{policy}"
ONGOING_STATE_DIR="{tmp_path / 'state'}"
round_barcode="TS_Jun_0"
: > "$NO_ADAPTER_POLICY_TSV"
if [ -s "{evidence}" ]; then
  printf 'observed_no_adapter\\t1\\n' > "$NO_ADAPTER_POLICY_TSV"
  printf 'observed_non_no_adapter\\t1\\n' >> "$NO_ADAPTER_POLICY_TSV"
  printf 'separate_no_adapter\\t1\\n' >> "$NO_ADAPTER_POLICY_TSV"
fi
mkdir -p "${{ONGOING_STATE_DIR}}/${{round_barcode}}" 2>/dev/null || true
cp "$NO_ADAPTER_POLICY_TSV" "${{ONGOING_STATE_DIR}}/${{round_barcode}}/$(basename "$NO_ADAPTER_POLICY_TSV")" 2>/dev/null || true
"""

    result = subprocess.run(["bash", "-lc", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    retained = round_dir / policy.name
    assert retained.exists()
    assert retained.read_text(encoding="utf-8") == ""


def test_main_nf_uses_record_safe_fasta_filter_for_rolling_sup_rewrite() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert 'perl "\\$BIN_DIR/filter_sup_non_no_adapter_fasta.pl" "\\$PROTECTED_READ_IDS_EVER_STATE" "\\${STATE_DIR}/qced_reads_hq_accumulated.fasta" > tmp' in text
    assert 'WARN: filter_sup_non_no_adapter_fasta.pl failed; retaining existing rolling pool prior to append' in text
    assert 'grep -F -A 1 "|sup|" "\\${STATE_DIR}/qced_reads_hq_accumulated.fasta"' not in text
    assert 'KEEP_NO_ADAPTER=1' not in text


def test_main_nf_carries_protected_reads_into_otu_definition_pool() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert 'PROTECTED_READ_IDS_EVER_STATE="\\${STATE_DIR}/${barcode}_protected_read_ids_ever.list"' in text
    assert 'awk \'NR==FNR{ids[\\$1]=1; next} /^>/{uuid=substr(\\$0,2); sub(/[|].*/,"",uuid); keep=(uuid in ids); if(keep)print; next} keep{print}\' "\\$PROTECTED_READ_IDS_EVER_STATE" "\\$ROLLING_QCED" > ${barcode}_rolling_protected.fasta || : > ${barcode}_rolling_protected.fasta' in text
    assert 'cat ${barcode}_rolling_protected.fasta >> ${barcode}_qced_reads_hq_accumulated.fasta' in text
    assert 'rm -f ${barcode}_rolling_protected.fasta ${barcode}_rolling_sup.fasta ${barcode}_rolling_hac_fixed.fasta sup_ids.list' in text


def test_main_nf_uses_prev_round_grace_for_assigned_otu_and_consensus_members() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert 'ASSIGNED_OTU_MEMBER_IDS_GRACE_STATE="\\${STATE_DIR}/${barcode}_assigned_otu_member_ids_prev_round.list"' in text
    assert 'CONSENSUS_ASSIGNED_MEMBER_IDS_GRACE_STATE="\\${STATE_DIR}/${barcode}_consensus_assigned_member_ids_prev_round.list"' in text
    assert 'replace_read_ids_state() {' in text
    assert 'materialize_round_grace_ids() {' in text
    assert '"\\$ASSIGNED_OTU_MEMBER_IDS_GRACE_STATE" \\' in text
    assert '"\\$CONSENSUS_ASSIGNED_MEMBER_IDS_GRACE_STATE" \\' in text
    assert '"\\$ROUND_DIR/${barcode}_assigned_otu_member_ids_raw_current.list" \\' in text
    assert '"\\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_raw_current.list" \\' in text


def test_consensus_uses_shared_adapter_row_filter() -> None:
    text = (REPO_ROOT / "bin" / "Consensus_simple.sh").read_text(encoding="utf-8")
    assert "filter_blast_rows_by_adapter_class.sh" in text
    assert "tr '[:upper:]' '[:lower:]'" in text
    assert 'if [ "$lock_enabled" != "0" ] && [ "$lock_enabled" != "1" ]; then' in text
    assert 'lock_enabled=1' in text
    assert 'adapter_row_filter_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/filter_blast_rows_by_adapter_class.sh"' in text
    assert 'tmp_clean_blast_report_full.txt sample "$sample" > "$sample_blast" 2>"$filter_stderr"' in text
    assert 'ERROR: filter_blast_rows_by_adapter_class.sh failed for sample=$sample exit_code=$filter_status stderr=$filter_err' in text


def test_consensus_timing_outputs_are_wired() -> None:
    consensus_text = (REPO_ROOT / "bin" / "Consensus_simple.sh").read_text(encoding="utf-8")
    main_text = MAIN_NF.read_text(encoding="utf-8")
    helper_text = (REPO_ROOT / "bin" / "lib" / "consensus_cache_state.sh").read_text(encoding="utf-8")
    prelaunch_helper_text = (REPO_ROOT / "bin" / "lib" / "consensus_prelaunch_gate.sh").read_text(encoding="utf-8")
    assert 'phase_timings_file="$out_dir/consensus_phase_timings.tsv"' in consensus_text
    assert 'phase_timings_raw_file="$out_dir/consensus_phase_timings_raw.tsv"' in consensus_text
    assert 'sample_phase_timings_file="$out_dir/consensus_sample_phase_timings.tsv"' in consensus_text
    assert 'rscript_stats_file="$out_dir/consensus_rscript_stats.tsv"' in consensus_text
    assert 'sample_totals_file="$out_dir/consensus_sample_totals.tsv"' in consensus_text
    assert 'cache_hydration_stats_file="$out_dir/cache_hydration_stats.tsv"' in consensus_text
    assert "printf 'round_id\\tscope\\tsample\\tphase\\tseconds\\tms\\n' > \"$phase_timings_file\"" in consensus_text
    assert "printf 'round_id\\tscope\\tsample\\tphase\\tseconds\\tms\\n' > \"$phase_timings_raw_file\"" in consensus_text
    assert "printf 'round_id\\tscope\\tsample\\tphase\\tseconds\\tms\\n' > \"$sample_phase_timings_file\"" in consensus_text
    assert "printf 'round_id\\tsample\\totu_inputs\\tinput_headers\\tinput_bases\\toutput_consensus_records\\toutput_consensus_bases\\n' > \"$sample_totals_file\"" in consensus_text
    assert "printf 'round_id\\tsample\\tstate_cache_present\\thydrated\\trestored_files\\tseconds\\tms\\n' > \"$cache_hydration_stats_file\"" in consensus_text
    assert "perl -MTime::HiRes=time -e 'print int(time()*1000), \"\\n\"'" in consensus_text
    assert 'restored_files=$(find "$state_sample_dir" -type f | wc -l | tr -d \' \')' in consensus_text
    assert 'restored_files=$(find "$local_cache_dir" -type f | wc -l | tr -d \' \')' not in consensus_text
    assert "append_timing_row()" in consensus_text
    assert "append_duration_row()" in consensus_text
    assert 'append_timing_row "$phase_timings_raw_file" "global" "-" "startup_validation_and_state"' in consensus_text
    assert 'append_timing_row "$phase_timings_raw_file" "global" "-" "cpu_budget_and_scheduler_setup"' in consensus_text
    assert 'append_timing_row "$sample_timing_file" "sample" "$sample" "blast_filter_parse"' in consensus_text
    assert 'append_timing_row "$sample_timing_file" "sample" "$sample" "r_consensus_input_scan"' in consensus_text
    assert 'append_timing_row "$sample_timing_file" "sample" "$sample" "r_consensus_exec"' in consensus_text
    assert 'append_timing_row "$sample_timing_file" "sample" "$sample" "merge_vsearch_refine"' in consensus_text
    assert 'append_timing_row "$phase_timings_raw_file" "global" "-" "sample_worker_dispatch"' in consensus_text
    assert 'append_timing_row "$phase_timings_raw_file" "global" "-" "sample_worker_compute_elapsed"' in consensus_text
    assert 'append_timing_row "$phase_timings_raw_file" "global" "-" "consolidated_ids_finalize"' in consensus_text
    assert 'append_timing_row "$phase_timings_raw_file" "global" "-" "counter_and_tmp_merge"' in consensus_text
    assert 'append_timing_row "$phase_timings_raw_file" "global" "-" "sample_output_merge"' in consensus_text
    assert 'phase_timings_tmp="${phase_timings_file}.tmp"' in consensus_text
    assert 'phase_timings_out="${phase_timings_tmp:-$phase_timings_file}"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "global" "-" "startup_validation_and_state"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "global" "-" "sample_input_partition"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "global" "-" "cpu_budget_and_scheduler_setup"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "global" "-" "sample_worker_dispatch"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "global" "-" "counter_and_tmp_merge"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "global" "-" "sample_output_merge"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "worker_sum" "-" "sample_worker_compute_sum"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "worker_sum" "-" "cache_lookup_and_restore"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "worker_sum" "-" "consensus_generation"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "worker_sum" "-" "consensus_postprocess"' in consensus_text
    assert 'append_duration_row "$phase_timings_out" "global" "-" "consolidated_ids_finalize"' in consensus_text
    assert 'mv "$phase_timings_tmp" "$phase_timings_file" 2>/dev/null || true' in consensus_text
    assert "consensus_process_timings.tsv" in main_text
    assert "consensus_process_timings_ms.tsv" in main_text
    assert "consensus_process_metrics.tsv" in main_text
    assert "blast_process_timings.tsv" in main_text
    assert "consensus_rscript_stats.tsv" in main_text
    assert "consensus_sample_totals.tsv" in main_text
    assert 'CONSENSUS_CACHE_STATE_ROOT="${ongoingStateDir}/Consensus/.cache"' in main_text
    assert 'CONSENSUS_CACHE_SYNC_SCRIPT="${baseDir}/bin/sync_dir_atomic.sh"' in main_text
    assert 'sync_dir_atomic.sh ${ongoingStateDir}/Consensus/.cache Consensus/.cache' not in main_text
    assert "timing_now_ms() {" in main_text
    assert "append_process_time_value() {" in main_text
    assert "append_process_metric() {" in main_text
    assert 'source "${baseDir}/bin/lib/consensus_cache_state.sh"' in main_text
    assert 'source "${baseDir}/bin/lib/consensus_prelaunch_gate.sh"' in main_text
    assert "consensus_cache_backfill_missing_count() {" in helper_text
    assert "consensus_cache_backfill_missing_file_count() {" in helper_text
    assert "restore_missing_consensus_cache_dirs() {" in helper_text
    assert "consensus_prelaunch_has_reads() {" in prelaunch_helper_text
    assert "consensus_prelaunch_has_cache() {" in prelaunch_helper_text
    assert "consensus_prelaunch_should_run() {" in prelaunch_helper_text
    assert "consensus_cache_backfill_needed() {" not in main_text
    assert 'set -- "\\$CONSENSUS_CACHE_STATE_ROOT"/*/*.consensus.fasta' not in main_text
    assert 'cache_state_files=( "$cache_root"/*/*.consensus.fasta )' in prelaunch_helper_text
    assert """awk -F'\\t' 'NR>1 && \\$3=="1" && \\$4=="1" && \\$7 != "" && \\$7 !~ /[^0-9]/ {sum+=\\$7} END{print sum+0}'""" in main_text
    assert """awk -F'\\t' 'NR>1 && \\$3=="1" && \\$4=="1" && \\$7 != "" && \\$7 !~ /[^0-9]/ && \\$7>max {max=\\$7} END{print max+0}'""" in main_text
    assert """awk -F'\\t' 'NR>1 && \\$3=="1" && \\$4=="1" && \\$5 != "" && \\$5 !~ /[^0-9]/ {sum+=\\$5} END{print sum+0}'""" in main_text
    assert 'append_process_timing "prelaunch_decision_gate" "\\$_t_prelaunch_decision_gate_start" "\\$_t_prelaunch_decision_gate_end"' in main_text
    assert 'append_process_timing "prelaunch_sample_mode_detect" "\\$_t_prelaunch_sample_mode_detect_start" "\\$_t_prelaunch_sample_mode_detect_end"' in main_text
    assert 'append_process_timing "prelaunch_consensus_ids_restore" "\\$_t_prelaunch_consensus_ids_restore_start" "\\$_t_prelaunch_consensus_ids_restore_end"' in main_text
    assert 'append_process_timing "prelaunch_qscore_prepare" "\\$_t_prelaunch_qscore_prepare_start" "\\$_t_prelaunch_qscore_prepare_end"' in main_text
    assert 'append_process_timing "prelaunch_misc_setup"' not in main_text
    assert 'append_process_timing "consensus_prelaunch_setup" "\\$_t_consensus_prelaunch_start" "\\$_t_consensus_prelaunch_end"' in main_text
    assert 'append_process_timing "consensus_script_exec" "\\$_t_consensus_script_exec_start" "\\$_t_consensus_script_exec_end"' in main_text
    assert 'append_process_timing "consensus_postscript_local_checks" "\\$_t_consensus_postscript_start" "\\$_t_consensus_postscript_end"' in main_text
    assert '# Any future symlink optimization here depends on read_qscore.tsv remaining read-only' in main_text
    assert '# while preserving the local read_qscore.tsv path contract consumed by Consensus_simple.sh.' in main_text
    assert 'append_process_time_value "cache_restore_worker_sum" "\\$_cache_restore_seconds"' in main_text
    assert 'append_process_time_value "cache_restore_max_single_sample" "\\$_cache_restore_max_sample_seconds"' in main_text
    assert 'append_process_time_value "cache_restore_max_sample"' not in main_text
    assert 'append_process_metric "cache_restore_file_count" "\\$_cache_restore_file_count"' in main_text
    assert 'append_process_metric "cache_backfill_missing_sample_dirs" "\\$_cache_backfill_missing_count"' in main_text
    assert 'append_process_metric "cache_backfill_file_count" "\\$_cache_backfill_file_count"' in main_text
    assert """printf '%s\\t%s\\t%s\\n' "${round_barcode}" "\\$metric" "\\$value" >> consensus_process_metrics.tsv""" in main_text
    assert """printf '%s\\t%s\\t%s\\n' "${round_barcode}" "\\$metric" "\\$value" >> consensus_process_timings.tsv""" not in main_text
    assert 'restore_missing_consensus_cache_dirs "\\$CONSENSUS_CACHE_STATE_ROOT" "Consensus/.cache" "\\$CONSENSUS_CACHE_SYNC_SCRIPT"' in main_text
    assert 'append_process_timing "cache_backfill_before_persist" "\\$_t_cache_backfill_start" "\\$_t_cache_backfill_end"' in main_text
    assert 'append_process_timing "cache_persist" "\\$_t_consensus_persist_start" "\\$_t_consensus_persist_end"' in main_text
    assert '${ongoingStateDir}/_state/${barcode}_consensus_rscript_stats_last.tsv' in main_text
    assert '${ongoingStateDir}/_state/${barcode}_consensus_sample_totals_last.tsv' in main_text
    assert '${ongoingStateDir}/_state/${barcode}_consensus_process_timings_last.tsv' in main_text
    assert '${ongoingStateDir}/_state/${barcode}_consensus_process_timings_ms_last.tsv' in main_text
    assert '${ongoingStateDir}/_state/${barcode}_consensus_process_metrics_last.tsv' in main_text
    assert '${ongoingStateDir}/_state/${barcode}_cache_hydration_stats_last.tsv' in main_text
    assert '${ongoingStateDir}/${round_barcode}/${barcode}_consensus_phase_timings.tsv' in main_text
    assert '${ongoingStateDir}/${round_barcode}/${barcode}_consensus_sample_totals.tsv' in main_text
    assert '${ongoingStateDir}/${round_barcode}/${barcode}_consensus_process_timings_ms.tsv' in main_text
    assert '${ongoingStateDir}/${round_barcode}/${barcode}_consensus_process_metrics.tsv' in main_text
    assert '${ongoingStateDir}/${round_barcode}/${barcode}_cache_hydration_stats.tsv' in main_text
    assert '"\\${STATE_DIR}/${barcode}_blast_process_timings_last.tsv"' in main_text
    assert '"\\$ROUND_DIR/${barcode}_blast_process_timings.tsv"' in main_text
    assert '"\\$ROUND_DIR/${barcode}_otu_refine_phase_timings.tsv"' in main_text
    assert '"\\$ROUND_DIR/${barcode}_otu_refine_phase_timings_ms.tsv"' in main_text
    assert '"\\$ROUND_DIR/${barcode}_otu_refine_process_breakdown.tsv"' in main_text
    assert '"\\$ROUND_DIR/${barcode}_otu_refine_process_breakdown_ms.tsv"' in main_text
    assert '"\\$ROUND_DIR/${barcode}_otu_refine_workload_stats.tsv"' in main_text
    assert '"\\${STATE_DIR}/${barcode}_otu_refine_phase_timings_ms_last.tsv"' in main_text
    assert '"\\${STATE_DIR}/${barcode}_otu_refine_process_breakdown_last.tsv"' in main_text
    assert '"\\${STATE_DIR}/${barcode}_otu_refine_process_breakdown_ms_last.tsv"' in main_text
    assert "printf 'shard_scheduler_mode\\tequal_record_count\\n'" in main_text
    assert "printf 'merged_pairs_rows\\t0\\n'" in main_text
    assert "printf 'annotated_file_bytes\\t0\\n'" in main_text
    assert 'append_otu_refine_breakdown "wrapper_invoke_total"' in main_text
    assert "} >> \"\\$OTU_REFINE_PROCESS_BREAKDOWN_FILE\" 2>/dev/null || true" in main_text
    assert "} >> \"\\$OTU_REFINE_PROCESS_BREAKDOWN_MS_FILE\" 2>/dev/null || true" in main_text
    assert '${ongoingStateDir}/_state/${barcode}_blast_pressure_stats_last.tsv' in main_text

    init_pos = main_text.index("printf 'qseqid,sseqid,evalue,length,pident\\n' > ${barcode}_preblastreport_join.txt")
    gate_pos = main_text.index('append_process_timing "prelaunch_decision_gate" "\\$_t_prelaunch_decision_gate_start" "\\$_t_prelaunch_decision_gate_end"')
    detect_pos = main_text.index('if ! bash ${baseDir}/bin/detect_consensus_sample_mode.sh \\')
    ids_restore_pos = main_text.index('# Restore previous consolidated IDs so cached-only rounds can re-evaluate emitted headers.')
    qscore_pos = main_text.index('cp "\\$ROLLING_QS" read_qscore.tsv')
    script_exec_pos = main_text.index('append_process_timing "consensus_script_exec" "\\$_t_consensus_script_exec_start" "\\$_t_consensus_script_exec_end"')
    skip_msg_pos = main_text.index('echo "INFO: No sup_reads and no consensus cache; skipping Consensus_simple.sh this round" 1>&2')
    assert init_pos < gate_pos < detect_pos < ids_restore_pos < qscore_pos < script_exec_pos
    assert gate_pos < skip_msg_pos
    assert 'if consensus_prelaunch_has_reads "\\$CONSENSUS_SUP_READS"; then' in main_text
    assert 'if consensus_prelaunch_has_cache "\\$CONSENSUS_CACHE_STATE_ROOT"; then' in main_text


def test_main_nf_blast_process_fixes_preserve_fail_fast_timing_and_locking() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")

    assert 'BLAST_HIT_EXTRACT_FAILED=0' in text
    assert 'if ! perl "\\$BIN_DIR/focus_hq_tax_fasta.pl" "\\$BLAST_HIT_REPORT" ${fasta_hq_qced} > ${barcode}_tmp_focus_hit.fasta; then' in text
    assert 'BLAST_HIT_EXTRACT_FAILED=1' in text
    assert 'if [ "\\$BLAST_HIT_EXTRACT_FAILED" -ne 0 ] && [ -s "\\$BLAST_HIT_REPORT" ]; then' in text
    assert 'ERROR: failed to initialize rolling pool from BLAST-hit FASTA extraction' in text

    otu_refine_pos = text.index('append_process_timing "otu_refine" "\\$_t_otu_refine_start" "\\$_t_otu_refine_end"')
    assignment_start_pos = text.index('_t_assignment_state_updates_start=\\$(date +%s)')
    assignment_end_pos = text.index('append_process_timing "assignment_state_updates" "\\$_t_assignment_state_updates_start" "\\$_t_assignment_state_updates_end"')
    assert otu_refine_pos < assignment_start_pos < assignment_end_pos

    merge_start = text.index('STATE_BLASTREPORT_SNAPSHOT="${barcode}_blastreport_state_snapshot.txt"')
    merge_end = text.index('append_process_timing "blastreport_merge" "\\$_t_blastreport_merge_start" "\\$_t_blastreport_merge_end"')
    merge_block = text[merge_start:merge_end]
    assert 'cp "\\${STATE_DIR}/blastreport.txt" "\\$STATE_BLASTREPORT_SNAPSHOT"' in merge_block
    assert 'STATE_BLASTREPORT_EXISTS=1' in merge_block
    assert '"\\$STATE_BLASTREPORT_SNAPSHOT"' in merge_block
    assert 'rm -f "\\$STATE_BLASTREPORT_SNAPSHOT"' in merge_block

    publish_start = text.index('cp ${barcode}_blastreport_round.txt "\\$ROUND_DIR/blastreport.txt"')
    publish_end = text.index('copy_soft "\\$BLAST_FILTER_STATS" "\\$ROUND_DIR/${barcode}_blast_filter_stats.tsv"', publish_start)
    publish_block = text[publish_start:publish_end]
    assert 'if acquire_lock "\\${BLASTREPORT_LOCK}"; then' in publish_block
    assert 'STATE_BLASTREPORT_TMP="\\${STATE_DIR}/blastreport.txt.tmp.\\$\\$"' in publish_block
    assert 'cp ${barcode}_blastreport.txt "\\$STATE_BLASTREPORT_TMP"' in publish_block
    assert 'mv "\\$STATE_BLASTREPORT_TMP" "\\${STATE_DIR}/blastreport.txt"' in publish_block
    assert 'release_lock "\\${BLASTREPORT_LOCK}"' in publish_block


def test_main_nf_avoids_anchored_numeric_shell_regexes_in_process_scripts() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert '=~ ^[0-9]+\\$' not in text


def test_main_nf_sources_shared_lock_and_db_signature_helpers() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert 'source "${baseDir}/bin/lib/lock_utils.sh"' in text
    assert text.count('source "${baseDir}/bin/lib/lock_utils.sh"') >= 4
    assert text.count('init_lock_helpers') >= 4
    lock_utils_text = (REPO_ROOT / "bin" / "lib" / "lock_utils.sh").read_text(encoding="utf-8")
    assert "append_trap() {" in lock_utils_text
    assert "append_trap EXIT cleanup_locks" in lock_utils_text
    assert "hooks isolated from one another" in lock_utils_text
    assert "they see the original $?" in lock_utils_text
    stale_lock_utils_text = (REPO_ROOT / "bin" / "lib" / "stale_lock_utils.sh").read_text(encoding="utf-8")
    assert "stale_lock_maybe_reclaim() {" in stale_lock_utils_text
    assert 'db_sig_utils.sh' in text
    assert text.count('db_sig_utils.sh') >= 2


def test_main_nf_guards_round_barcode_collisions_before_round_dir_use() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    fast_block = text.split("process fast_on_target_detection {", 1)[1].split("process _reporting_fast_on_target {", 1)[0]
    assert "def staleLockTtlMinutesStr = params.stale_lock_ttl_minutes.toString().trim()" in text
    assert "Invalid --stale_lock_ttl_minutes '${params.stale_lock_ttl_minutes}'. Provide an integer >= 0." in text
    assert 'barcode=\\$(basename "${workflow.launchDir}")' in fast_block
    assert 'round_barcode=\\$(basename "${read_file}")' in fast_block
    assert 'READ_FILE_ABS=\\$(bash ${baseDir}/bin/round_barcode_source_guard.sh \\' in fast_block
    assert '--state-dir "${ongoingStateDir}/_state" \\' in fast_block
    assert '--round-barcode "\\$round_barcode" \\' in fast_block
    assert '--read-file "${read_file}")' in fast_block
    assert 'ERROR: round_barcode_source_guard.sh returned empty canonical path for round \'\\$round_barcode\'' in fast_block
    assert "printf 'read_file=%s\\n' \"\\$READ_FILE_ABS\"" in fast_block
    guard_pos = fast_block.index('READ_FILE_ABS=\\$(bash ${baseDir}/bin/round_barcode_source_guard.sh \\')
    round_dir_pos = fast_block.index('if [ ! -d ${ongoingStateDir}/\\$round_barcode/ ];')
    assert guard_pos < round_dir_pos
    assert 'source "${baseDir}/bin/lib/stale_lock_utils.sh"' in fast_block
    assert 'stale_lock_maybe_reclaim \\' in fast_block
    assert '"\\$(( ${staleLockTtlMinutesStr} * 60 ))" \\' in fast_block
    assert '"round lock" \\' in fast_block
    assert 'reclaim_status=\\$?' in fast_block
    assert 'if [ "\\$reclaim_status" -eq 2 ]; then' in fast_block
    assert 'ERROR: stale_lock_maybe_reclaim rejected round lock parameters' in fast_block
    assert 'if [ "\\$reclaim_status" -eq 10 ]; then' in fast_block


def test_main_nf_wires_size_streak_phase_b_in_reporting_otu_definition() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert text.count('OTU_SIZE_STREAK_MODE="${otuSizeStreakModeCanonical}"') >= 1
    assert text.count('OTU_SIZE_STREAK_MIN_ROUNDS="${otuSizeStreakMinRoundsStr}"') >= 1
    assert 'perl ${baseDir}/bin/reporting_otu_definition.pl ${otu_clstr} ${demult} ${round_barcode} ${barcode} "\\${_TARGETS[@]}"' in text
    assert 'OTU_SIZE_STREAK_STATE="${ongoingStateDir}/_state/${barcode}_otu_size_streak.tsv"' in text
    assert 'OTU_HASH_MAP_STATE="${ongoingStateDir}/_state/${barcode}_otu_nr_hash_map.tsv"' in text
    assert '[ -f "\\$OTU_HASH_MAP_STATE" ] || : > "\\$OTU_HASH_MAP_STATE"' in text
    assert 'cp "\\$HASH_MAP" "\\${STATE_DIR}/${barcode}_otu_hash_map.tsv.tmp" 2>/dev/null && mv "\\${STATE_DIR}/${barcode}_otu_hash_map.tsv.tmp" "\\${STATE_DIR}/${barcode}_otu_hash_map.tsv" || true' in text
    assert 'cp "\\$HASH_MAP" "${ongoingStateDir}/${round_barcode}/${barcode}_otu_hash_map.tsv" 2>/dev/null || true' in text
    assert 'if [ "\\$OTU_SIZE_STREAK_MODE" != "off" ] || [ "\\$OTU_BLAST_FILTER_MODE" != "off" ]; then' in text
    assert 'LOCK_WAIT=${params.lock_wait_seconds}' in text
    assert 'acquire_lock "\\$OTU_SIZE_STREAK_LOCK"' in text
    assert 'release_lock "\\$OTU_SIZE_STREAK_LOCK"' in text
    assert 'perl "${baseDir}/bin/otu_size_streak_update.pl"' in text
    assert '"\\$OTU_HASH_MAP_STATE" \\' in text
    assert 'WARN: size-streak lock unavailable; skipping _state size-streak updates this round' in text
    assert '_missing_hash=\\$(sgv otus_size_streak_missing_hash_rows)' in text
    assert 'WARN: otu_size_streak_missing_hash_rows=\\${_missing_hash} (size-streak skipped for missing hash)' in text


def test_main_nf_wires_unified_round_prune_flow_and_consensus_apply() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    config_text = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    blast_block = text.split("process blast_OTU_pretax {", 1)[1].split("process _reporting_blast_pretax {", 1)[0]
    consensus_block = text.split("process consensus {", 1)[1].split("process _reporting_consensus_tax {", 1)[0]
    assert 'prune_cumulative_pool_all = true' in config_text
    assert "def pruneCumulativePoolAll = parseBoolStrict(params.prune_cumulative_pool_all, true, 'prune_cumulative_pool_all')" in text
    assert 'PRUNE_CUMULATIVE_POOL_ALL="${pruneCumulativePoolAll ? \'1\' : \'0\'}"' in text
    assert 'ROUND_PRUNE_IDS="\\$ROUND_DIR/${barcode}_round_prune_ids.list"' in text
    assert 'ROUND_PRUNE_STATS="\\$ROUND_DIR/${barcode}_round_prune_stats.tsv"' in text
    assert 'ROUND_PRUNE_APPLY_STATS="\\$ROUND_DIR/${barcode}_round_prune_apply.tsv"' in text
    assert 'BLAST_FILTER_DROPPED_IDS="${barcode}_blast_filter_dropped_read_ids.list"' in text
    assert text.count('prune_round_orchestrate.sh') >= 2
    assert 'perl "\\$BIN_DIR/blast_assigned_otu_keys.pl" \\' in blast_block
    assert 'perl "\\$BIN_DIR/persist_otu_keys_ever.pl" \\' in blast_block
    assert 'perl "\\$BIN_DIR/expand_otu_keys_to_member_ids.pl" \\' in blast_block
    assert 'perl ${baseDir}/bin/consensus_assigned_otu_keys.pl \\' in consensus_block
    assert 'ERROR: failed to extract blast-assigned OTU keys' in blast_block
    assert 'ERROR: failed to persist blast-assigned OTU keys' in blast_block
    assert 'ERROR: failed to expand current blast-assigned OTU keys into member reads' in blast_block
    assert 'ERROR: failed to extract consensus-assigned OTU keys' in consensus_block
    assert 'ERROR: failed to expand current consensus-assigned OTU keys into member reads' in consensus_block
    assert 'ERROR: failed to persist consensus-assigned OTU keys' in consensus_block
    assert 'ERROR: failed to emit consensus round provenance' in consensus_block
    assert 'BLAST_PRESSURE_STATS="${ongoingStateDir}/${round_barcode}/${barcode}_blast_pressure_stats.tsv"' in consensus_block
    assert 'BLAST_PRESSURE_TMP="\\${BLAST_PRESSURE_STATS}.tmp"' in consensus_block
    assert 'blast_pressure_publish_ok=0' in consensus_block
    assert 'rm -f "\\$BLAST_PRESSURE_STATS" "\\$BLAST_PRESSURE_TMP"' in consensus_block
    assert 'pool_after_prune=\\$(grep -c \'^>\' "${ongoingStateDir}/_state/qced_reads_hq_accumulated.fasta" || echo 0)' in consensus_block
    assert '} > "\\$BLAST_PRESSURE_TMP" 2>/dev/null; then' in consensus_block
    assert 'if mv "\\$BLAST_PRESSURE_TMP" "\\$BLAST_PRESSURE_STATS" 2>/dev/null; then' in consensus_block
    assert 'blast_pressure_publish_ok=1' in consensus_block
    assert 'if [ "\\$blast_pressure_publish_ok" -eq 1 ] && awk -F\'\\t\' ' in consensus_block
    assert 'WARN: blast pressure stats sidecar incomplete; skipping _state refresh for ${barcode}/${round_barcode}' in consensus_block
    assert 'WARN: failed to publish blast pressure stats sidecar for ${barcode}/${round_barcode}' in consensus_block
    assert 'cp "\\$BLAST_PRESSURE_STATS" "${ongoingStateDir}/_state/${barcode}_blast_pressure_stats_last.tsv" 2>/dev/null || true' in consensus_block
    assert '--protected-read-ids-ever "\\$PROTECTED_READ_IDS_EVER" \\' in blast_block
    assert '--protected-read-ids-round "\\$PROTECTED_READ_IDS_ROUND" \\' in blast_block
    assert '--candidate size_streak "\\$SIZE_STREAK_PRUNE_IDS" \\' in blast_block
    assert '--candidate consensus_unassigned "\\$CONSENSUS_UNASSIGNED_PRUNE_IDS" \\' in blast_block
    assert '--candidate blast_unassigned "\\$BLAST_UNASSIGNED_IDS" \\' in blast_block
    assert '--c1-ids "\\$C1_PRUNE_IDS" \\' in blast_block
    assert '--merge-error-context "failed to merge round prune ID lists"; then' in blast_block
    assert '--protected-read-ids-ever "\\$PROTECTED_READ_IDS_EVER" \\' in consensus_block
    assert '--protected-read-ids-round "\\$PROTECTED_READ_IDS_ROUND" \\' in consensus_block
    assert '--protected-stats "\\$PROTECTED_STATS" \\' in consensus_block
    assert '--merge-error-context "failed to re-merge round prune ID lists after eligible-count update"; then' in consensus_block
    assert '"\\$ROUND_PRUNE_IDS" \\' in text
    assert 'cp "\\$ROUND_PRUNE_IDS" "\\${STATE_DIR}/${barcode}_round_prune_ids_last.list" 2>/dev/null || true' in text
    assert 'cp "\\$ROUND_PRUNE_STATS" "\\${STATE_DIR}/${barcode}_round_prune_stats_last.tsv" 2>/dev/null || true' in text
    assert 'bash ${baseDir}/bin/consensus_prune_apply.sh \\' in text
    assert '--apply-script   "${baseDir}/bin/reads_apply_prune_ids.pl"' in text


def test_main_nf_validates_round_lock_scope_and_cpu_maxfork_params() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    config_text = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    assert 'round_lock_scope = "full_round"' in config_text
    assert "def roundLockScopeCanonical = params.round_lock_scope.toString().trim().toLowerCase()" in text
    assert "Invalid --round_lock_scope '${params.round_lock_scope}'. Allowed values: full_round, dorado_only" in text
    assert 'maxforks_fast = 1' in config_text
    assert "def maxForksFastVal = maxForksFastStr.toInteger()" in text
    assert 'maxforks_reporting = 2' in config_text
    assert 'maxforks_consensus = 1' in config_text
    assert 'maxforks_stateful_core = 1' in config_text
    assert "def maxForksReportingVal = maxForksReportingStr.toInteger()" in text
    assert "def maxForksConsensusVal = maxForksConsensusStr.toInteger()" in text
    assert "def maxForksStatefulCoreVal = maxForksStatefulCoreStr.toInteger()" in text
    assert 'html_report_enabled = true' in config_text
    assert "def htmlReportEnabled = parseBoolStrict(params.html_report_enabled, true, 'html_report_enabled')" in text
    assert 'html_report_auto_refresh = true' in config_text
    assert "def htmlReportAutoRefresh = parseBoolStrict(params.html_report_auto_refresh, true, 'html_report_auto_refresh')" in text
    assert 'html_report_refresh_seconds = 15' in config_text
    assert "def htmlReportRefreshSecondsStr = params.html_report_refresh_seconds.toString().trim()" in text
    assert "Invalid --html_report_refresh_seconds '${params.html_report_refresh_seconds}'. Provide an integer >= 1." in text
    assert 'html_report_url_prefix = ""' in config_text
    assert "def htmlReportUrlPrefix = params.html_report_url_prefix.toString().trim()" in text
    assert 'html_report_sample_plot_max = 10' in config_text
    assert "def htmlReportSamplePlotMaxStr = params.html_report_sample_plot_max.toString().trim()" in text
    assert "Invalid --html_report_sample_plot_max '${params.html_report_sample_plot_max}'. Provide an integer >= 0." in text
    assert 'consensus_keep_original_reads = false' in config_text
    assert "def consensusKeepOriginalReads = parseBoolStrict(params.consensus_keep_original_reads, false, 'consensus_keep_original_reads')" in text
    assert 'prune_unassigned_clusters = false' in config_text
    assert "def pruneUnassignedClusters = parseBoolStrict(params.prune_unassigned_clusters, false, 'prune_unassigned_clusters')" in text
    assert 'prune_unassigned_drop_reads = false' in config_text
    assert "def pruneUnassignedDropReads = parseBoolStrict(params.prune_unassigned_drop_reads, false, 'prune_unassigned_drop_reads')" in text
    assert "prune_unassigned_drop_reads disabled because prune_unassigned_clusters is false" in text
    assert 'prune_unassigned_grace_rounds = 3' in config_text
    assert "def pruneUnassignedGraceRoundsStr = params.prune_unassigned_grace_rounds.toString().trim()" in text
    assert "Invalid --prune_unassigned_grace_rounds '${params.prune_unassigned_grace_rounds}'. Provide an integer >= 0." in text
    assert 'prune_unassigned_keep_top = 5' in config_text
    assert "def pruneUnassignedKeepTopStr = params.prune_unassigned_keep_top.toString().trim()" in text
    assert "Invalid --prune_unassigned_keep_top '${params.prune_unassigned_keep_top}'. Provide an integer >= 0." in text
    assert 'prune_cumulative_pool_all = true' in config_text
    assert "def pruneCumulativePoolAll = parseBoolStrict(params.prune_cumulative_pool_all, true, 'prune_cumulative_pool_all')" in text


def test_main_nf_wires_unassigned_cluster_prune_flow() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    blast_block = text.split("process blast_OTU_pretax {", 1)[1].split("process _reporting_blast_pretax {", 1)[0]
    consensus_block = text.split("process consensus {", 1)[1].split("process _reporting_consensus_tax {", 1)[0]
    assert 'consensus_inputs = ChannelUtils.strictRoundJoin(fastq_qced_consensus, blast2consensus)' in text
    assert 'tuple val(barcode), val(round_barcode), file(\'blast_report_annotated.txt\') into blast_agg_ch' in blast_block
    assert 'tuple val(barcode), val(round_barcode), file("${barcode}_blastreport_sup.sam"), file("${barcode}_round_sup.tsv"), file("${barcode}_blastreport_sup_pre.fastq"), file("${barcode}_preblastreport_join.txt"), file("blast_report_annotated_preferred.txt"), file("blast_report_annotated_noadapter.txt"), file("${barcode}_blast_filter_stats.tsv") into report_blast' in blast_block
    assert 'tuple val(barcode), val(round_barcode), file("blast_report_annotated.txt"), file("${barcode}_assigned_read_ids.list") into blast2consensus' in blast_block
    assert blast_block.count('perl "\\$BIN_DIR/blast_assigned_read_ids.pl" \\') == 1
    assert '"${barcode}_blastreport_round.txt" \\' in blast_block
    assert '${barcode}_assigned_read_ids.list' in blast_block
    assert 'protected_pool_added=0' in blast_block
    assert 'protected_ids_reappended' in blast_block
    assert 'PROTECTED_POOL_FASTA="\\${STATE_DIR}/${barcode}_rolling_pool_protected.fasta"' in blast_block
    assert 'tuple val(barcode), val(round_barcode), file(fasta_hq_qced), file(blast_report), file(assigned_read_ids) from consensus_inputs' in consensus_block
    assert 'CONSENSUS_PRUNE_UNASSIGNED_CLUSTERS="${pruneUnassignedClusters ? \'1\' : \'0\'}" \\' in consensus_block
    assert 'CONSENSUS_PRUNE_UNASSIGNED_DROP_READS="${(pruneUnassignedClusters && pruneUnassignedDropReads) ? \'1\' : \'0\'}" \\' in consensus_block
    assert 'CONSENSUS_PRUNE_UNASSIGNED_GRACE_ROUNDS="${pruneUnassignedGraceRoundsStr}" \\' in consensus_block
    assert 'CONSENSUS_PRUNE_UNASSIGNED_KEEP_TOP="${pruneUnassignedKeepTopStr}" \\' in consensus_block
    assert 'CONSENSUS_ASSIGNED_IDS="${assigned_read_ids}" \\' in consensus_block
    assert 'CONSENSUS_ROUND_INDEX_FILE="${ongoingStateDir}/_state/round_index.tsv" \\' in consensus_block
    assert 'CONSENSUS_KEEP_ORIGINAL_READS="${consensusKeepOriginalReads ? 1 : 0}" \\' in consensus_block
    assert 'if [ -f Consensus/pruned_unassigned_reads_round.list ]; then' in consensus_block
    assert 'consensus_pruned_unassigned_merge.sh \\' in consensus_block
    assert '--provenance consensus_round_provenance.tsv' in consensus_block
    # Depth resolver wiring: all three thresholds per target, deepest-level dedup, then mask.
    assert 'bash ${baseDir}/bin/consensus_assign_depth.sh \\' in consensus_block
    assert '--family   "\\$_id_fam" \\' in consensus_block
    assert '--genus    "\\$_id_gen" \\' in consensus_block
    assert '--species  "\\$_id_spec" \\' in consensus_block
    assert 'tmp_assign_levels_uniq.tsv' in consensus_block
    assert 'consensus_threshold_mask.sh tmp_assign_levels_uniq.tsv' in consensus_block
    # get_blast_taxdepth.pl must not appear in the consensus block (OTU-pretax path only).
    assert 'get_blast_taxdepth.pl' not in consensus_block
    # Dedup awk must use escaped field refs (\$1, \$2) — Nextflow triple-double-quote context.
    assert r'\$1 in best' in consensus_block


def test_main_nf_wires_run_started_utc_file() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    fast_block = text.split("process fast_on_target_detection {", 1)[1].split("process _reporting_fast_on_target {", 1)[0]
    backup_block = text.split("process backup_update_and_clean {", 1)[1]
    # Atomic write-once via noclobber; single assertion covers mechanism + exact write target.
    assert 'set -C; date -u \'+%Y-%m-%dT%H:%M:%SZ\' > "${ongoingStateDir}/_state/run_started_utc.txt"' in fast_block
    # --run-started-utc-file is now wired in backup_update_and_clean → report_run_json.pl call.
    assert '--run-started-utc-file "${ongoingStateDir}/_state/run_started_utc.txt"' in backup_block


def test_main_nf_getting_run_summary_conditional_consensus_consolidated_ids() -> None:
    """--consensus-consolidated-ids is only passed when the file is non-empty.

    Early rounds have an empty consolidated_ids file (no consolidations yet).
    Passing the path unconditionally triggers a spurious missing_or_empty warning
    in report_round_json.pl. The fix uses a _CONS_IDS_ARG guard so the arg is
    omitted when the file is empty, suppressing the false-positive warning.
    """
    text = MAIN_NF.read_text(encoding="utf-8")
    summary_block = text.split("process getting_run_summary {", 1)[1].split("process backup_update_and_clean {", 1)[0]
    # Guard must initialise the variable before the perl call.
    assert '_CONS_IDS_ARG=""' in summary_block
    # Conditional population using [ -s ] (non-empty file test).
    assert 'if [ -s "\\$ROUND_DIR/' in summary_block
    assert '_CONS_IDS_ARG="--consensus-consolidated-ids' in summary_block
    # Variable expansion (not hardcoded path) passed to report_round_json.pl.
    assert '\\$_CONS_IDS_ARG \\' in summary_block
    # Hardcoded form must NOT appear.
    assert '--consensus-consolidated-ids "\\$ROUND_DIR/' not in summary_block


def test_main_nf_routes_dorado_basecalling_through_lock_helper() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    fast_block = text.split("process fast_on_target_detection {", 1)[1].split("process _reporting_fast_on_target {", 1)[0]
    hac_block = text.split("process hac_basecalling {", 1)[1].split("process _reporting_hac_basecalling {", 1)[0]
    assert "process fast_on_target_detection {" in text
    assert "maxForks maxForksFastVal" in text
    assert text.count('DORADO_LOCK="${ongoingStateDir}/_state/.dorado.lock"') >= 2
    assert text.count('DORADO_LOCK_WAIT=${params.lock_wait_seconds}') >= 2
    assert 'DORADO_LOCK="\\${STATE_DIR}/.dorado.lock"' in text
    assert '${baseDir}/bin/with_dorado_lock.sh "\\$DORADO_LOCK" "\\$DORADO_LOCK_WAIT" "fast_on_target_detection:\\$round_barcode" -- \\' in text
    assert '${baseDir}/bin/with_dorado_lock.sh "\\$DORADO_LOCK" "\\$DORADO_LOCK_WAIT" "hac_basecalling:\\$round_barcode" -- \\' in text
    assert '"\\$BIN_DIR/with_dorado_lock.sh" "\\$DORADO_LOCK" "\\$DORADO_LOCK_WAIT" "blast_OTU_pretax:\\$round_barcode:sup" -- \\' in text
    assert 'ROUND_LOCK_SCOPE="${roundLockScopeCanonical}"' in text
    assert 'round_barcode="\\${round_barcode}"' not in fast_block
    assert 'round_barcode="${round_barcode}"' in hac_block
    assert 'round_barcode=\\$(basename "${read_file}")' in text
    assert 'if [ "\\$ROUND_LOCK_SCOPE" = "dorado_only" ] && [ "\\$ROUND_LOCK_EARLY_RELEASED" -ne 1 ]; then' in text
    assert 'INFO: round lock released after FAST basecalling (round_lock_scope=dorado_only)' in text


def test_main_nf_wires_round_report_json_history_and_html_render() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    fast_block = text.split("process fast_on_target_detection {", 1)[1].split("process _reporting_fast_on_target {", 1)[0]
    summary_block = text.split("process getting_run_summary {", 1)[1].split("process backup_update_and_clean {", 1)[0]
    backup_block = text.split("process backup_update_and_clean {", 1)[1]
    assert 'static def strictRoundJoinAll(channels, label = null)' in CHANNEL_UTILS.read_text(encoding='utf-8')
    assert 'getting_run_summary_inputs = ChannelUtils.strictRoundJoinAll([' in text
    assert "], 'getting_run_summary_inputs')" in text
    assert 'getting_run_summary_with_path = ChannelUtils.strictRoundJoin(getting_run_summary_inputs, get_summary_ch)' in text
    assert 'complete_round_with_path = ChannelUtils.strictRoundJoin(complete_round_ch, close_round_ch)' in text
    assert 'tuple env(barcode), env(round_barcode), val(read_path) into close_round_ch, get_summary_ch, failed_round_source_ch' in fast_block
    assert 'def failedRoundPlaceholderRoot = file("${baseDir}/bin/report_placeholders/failed_round", checkIfExists: true)' in text
    assert "if (!(resolvedContext in ['full_collapse', 'primers_only', 'full_track', 'off'])) {" in text
    assert 'def failedRoundPlaceholderAssets = resolveFailedRoundPlaceholderAssets(demuxIdentityContext)' in text
    assert 'failed_round_ch = failed_round_source_ch' in text
    assert 'rm -f ${ongoingStateDir}/\\$round_barcode/ROUND_FAILED.txt' in fast_block
    assert 'process failed_round_summary_placeholders {' not in text
    assert 'failed_round_ch.into {' in text
    assert 'failed_blst_rpt_summary_src_ch' in text
    assert 'failed_cons_rpt_summary_src_ch' in text
    assert 'failed_otu_def_rpt_summary_src_ch' in text
    assert 'failed_otu_def_rpt_sidecar_summary_src_ch' in text
    assert 'failed_demult_rpt_summary_src_ch' in text
    assert 'failed_demult_rpt_sidecar_summary_src_ch' in text
    assert 'failed_target_rpt_summary_src_ch' in text
    assert 'failed_blast_agg_src_ch' in text
    assert 'failed_cons_agg_src_ch' in text
    assert '[barcode, round_barcode, failedRoundPlaceholderAssets.blastOtuPretaxRpt, failedRoundPlaceholderAssets.readInfoRpt, failedRoundPlaceholderAssets.blastOtuNoadapterRpt, failedRoundPlaceholderAssets.blastFilterStats]' in text
    assert '[barcode, round_barcode, failedRoundPlaceholderAssets.blastConsensusTaxRpt, failedRoundPlaceholderAssets.consensusRoundProv]' in text
    assert '[barcode, round_barcode, failedRoundPlaceholderAssets.otuDefRpt, failedRoundPlaceholderAssets.otuMembersRound, failedRoundPlaceholderAssets.otuSizesRound]' in text
    assert '[barcode, round_barcode, failedRoundPlaceholderAssets.otuDefSidecar]' in text
    assert '[barcode, round_barcode, failedRoundPlaceholderAssets.demultRpt]' in text
    assert '[barcode, round_barcode, failedRoundPlaceholderAssets.demultSidecar]' in text
    assert '[barcode, round_barcode, failedRoundPlaceholderAssets.onTargetRpt]' in text
    assert '[barcode, round_barcode, failedRoundPlaceholderAssets.blastReportAnnotated]' in text
    assert '[barcode, round_barcode, failedRoundPlaceholderAssets.consensusBlastFull]' in text
    assert 'tuple val(barcode), val(round_barcode), file(blast_otu_pretax_rpt), file(read_info_rpt), file(blast_otu_noadapter_rpt), file(blast_filter_stats), file(blast_consensus_tax), file(consensus_round_provenance), file(otu_def_rpt), file(otu_members_round), file(otu_sizes_round), file(otu_def_rpt_sidecar), file(demult_rpt), file(demult_rpt_sidecar), file(on_target_rpt), file(summary), file(summary_otu), val(read_path) from getting_run_summary_with_path' in summary_block
    assert 'val(read_path) from get_summary_ch' not in summary_block
    assert 'tuple val(barcode), val(round_barcode), val(read_path) from complete_round_with_path' in backup_block
    assert 'tuple val(barcode), val(round_barcode), file("report_render.request") into report_render_request_ch' in backup_block
    assert 'val(read_path) from close_round_ch' not in backup_block
    assert 'ROUND_REPORT_JSON="\\$ROUND_DIR/round_report.json"' in summary_block
    assert 'ROUND_REPORT_JSON_STAGED="${barcode}_round_report_pre_frozen.json"' in summary_block
    assert 'ROUND_LIVE_STAGE="\\$ROUND_DIR/report_live_stage"' in summary_block
    assert 'REPORT_LIVE_ASSET_DIR="\\$ROUND_LIVE_STAGE/report_assets"' in summary_block
    assert 'REPORT_SAMPLE_STAGE_DIR="\\$REPORT_LIVE_ASSET_DIR/samples"' in summary_block
    assert 'REPORT_FIG_URL_PREFIX="runs/${run_name}/report_assets/live_round"' in summary_block
    assert 'REPORT_SAMPLE_FIG_URL_PREFIX="runs/${run_name}/report_assets/live_round/samples"' in summary_block
    assert 'ROUND_INDEX_FILE="\\$STATE_DIR/round_index.tsv"' in summary_block
    assert 'ACTIVE_PRUNE_COUNTS_OUT="\\$ROUND_DIR/active_prune_candidates_counts.tsv"' in summary_block
    assert 'OTU_SIZE_STREAK_IDS_LAST="\\$STATE_DIR/${barcode}_otu_size_streak_prune_ids_last.txt"' in summary_block
    assert 'perl ${baseDir}/bin/active_prune_candidates.pl \\' in summary_block
    assert '--otu-members-round "${otu_members_round}" \\' in summary_block
    assert '--otu-sizes-round "${otu_sizes_round}" \\' in summary_block
    assert '--round-index-file "\\$ROUND_INDEX_FILE" \\' in summary_block
    assert '--effective-mode-helper "${baseDir}/bin/otu_blast_effective_mode.sh" \\' in summary_block
    assert 'WARN: active_prune_candidates.pl failed (rc=\\$active_prune_rc)' in summary_block
    assert 'report_sample_read_counts_plots.sh \\' in summary_block
    assert 'perl ${baseDir}/bin/report_round_json.pl \\' in summary_block
    assert 'render_round_report_json() {' in summary_block
    assert 'ROUND_REPORT_TIMESTAMP_UTC=""' not in summary_block
    assert 'ROUND_TIMESTAMP_UTC="\\$(date -u +%Y-%m-%dT%H:%M:%SZ)"' in summary_block
    assert 'render_round_report_json "\\$ROUND_REPORT_JSON_STAGED"' in summary_block
    assert 'render_round_report_json "\\$ROUND_REPORT_JSON"' in summary_block
    assert 'render_round_report_json "\\$ROUND_REPORT_JSON" "\\$ROUND_REPORT_TIMESTAMP_UTC"' not in summary_block
    assert 'set -- --timestamp-utc "\\$timestamp_override"' not in summary_block
    assert '--timestamp-utc "\\$ROUND_TIMESTAMP_UTC" \\' in summary_block
    assert '"\\$@" \\' not in summary_block
    assert '--asset-snapshot-policy "latest_only" \\' in summary_block
    assert 'DEMULT_RPT_SOURCE="${ongoingStateDir}/_state/${barcode}_demult_rpt.txt"' in summary_block
    assert 'DEMULT_RPT_SOURCE="${demult_rpt}"' in summary_block
    assert 'cp "\\$DEMULT_RPT_SOURCE" "${barcode}_demult_rpt_cumulative.txt"' in summary_block
    assert 'bash ${baseDir}/bin/demult_summary.sh ${barcode}_demult_rpt_cumulative.txt ${barcode}' in summary_block
    assert 'cp "${barcode}_summary_demult_rpt.txt" "${ongoingStateDir}/_state/${barcode}_summary_demult_rpt.txt"' in summary_block
    assert '--demult "${demult_rpt}" \\' in summary_block
    assert '--read-fate-demult "${barcode}_read_fate_demult_first_seen.tsv" \\' in summary_block
    assert '--schema-version "2.0" \\' in summary_block
    assert '--otu-sizes-round "${otu_sizes_round}" \\' in summary_block
    assert '--otu-size-streak "\\$ROUND_DIR/${barcode}_otu_size_streak.tsv" \\' in summary_block
    assert '--active-prune-counts "\\$ACTIVE_PRUNE_COUNTS_OUT" \\' in summary_block
    assert '--blast-filter-dropped-ids "\\$ROUND_DIR/${barcode}_blast_filter_dropped_read_ids.list" \\' in summary_block
    assert '--round-index-file "\\$ROUND_DIR/round_index.tsv" \\' in summary_block
    assert '--consensus-round-provenance "${consensus_round_provenance}" \\' in summary_block
    assert '--read-fate-blast "${barcode}_read_fate_blast_first_seen.tsv" \\' in summary_block
    assert '_ROUND_FAILED_ARG=""' in summary_block
    assert '--round-failed-file \\$ROUND_DIR/ROUND_FAILED.txt' in summary_block
    assert '--json "\\$ROUND_REPORT_JSON_STAGED" \\' in summary_block
    assert summary_block.index('--json "\\$ROUND_REPORT_JSON_STAGED" \\') < summary_block.index('Time_taxonomy_otu_frozen.sig')
    assert 'perl ${baseDir}/bin/report_sync_figures.pl \\' in summary_block
    assert '--asset-dir "\\$REPORT_LIVE_ASSET_DIR" \\' in summary_block
    assert 'bash ${baseDir}/bin/report_live_stage.sh \\' in summary_block
    assert '--stage-root "\\$ROUND_LIVE_STAGE" \\' in summary_block
    assert '--sample-fig-dir "\\$REPORT_SAMPLE_STAGE_DIR" \\' in summary_block
    assert 'publish_report_history' not in summary_block
    assert summary_block.index('Time_taxonomy_otu_frozen.sig') < summary_block.index('wait || true')
    assert 'Time_taxonomy.R' not in summary_block
    assert 'Time_taxonomy_otu.R' in summary_block
    assert 'Time_taxonomy_otu.sig' in summary_block
    assert 'REPORT_HISTORY_JSONL="\\$STATE_TMP/report_history.jsonl"' in backup_block
    assert 'REPORT_HISTORY_LOCK="\\$STATE_TMP/.report_history.lock"' in backup_block
    assert 'REPORT_RENDER_LOCK="\\$STATE_TMP/.report_render.lock"' in backup_block
    assert 'REPORT_LIVE_PUBLISH_LOCK="\\$STATE_TMP/.report_live_publish.lock"' in backup_block
    assert 'RUN_REPORT_JSON="\\$ROUND_TMP/run_report.json"' in backup_block
    assert 'RUN_INDEX_JSONL="${params.outdir}/report_html/runs_index.jsonl"' in backup_block
    assert 'RUN_INDEX_LOCK="${params.outdir}/.runs_index.lock"' in backup_block
    assert 'RUN_REPORT_DIR="${params.outdir}/report_html/runs/${run_name}"' in backup_block
    assert 'RUN_REPORT_PENDING="\\$RUN_REPORT_DIR/.report_render_pending"' in backup_block
    assert 'REPORT_ASSET_DIR="${params.outdir}/report_html/runs/${run_name}/report_assets"' in backup_block
    assert 'history_lock_acquired=0' in backup_block
    assert 'release_report_history_lock() {' in backup_block
    assert 'acquire_report_history_lock() {' in backup_block
    assert 'publish_report_history() {' in backup_block
    assert '--check-live-order \\' in backup_block
    assert '--current-round-barcode "${round_barcode}" \\' in backup_block
    assert 'INFO: normalizing report history order for ${round_barcode}' in backup_block
    assert '--live \\' in backup_block
    assert '--skip-render \\' in backup_block
    assert "trap 'release_report_history_lock' EXIT HUP INT TERM" in backup_block
    assert 'trap - EXIT HUP INT TERM' in backup_block
    assert 'bash ${baseDir}/bin/report_live_publish.sh \\' in backup_block
    assert '--lock-path "\\$REPORT_LIVE_PUBLISH_LOCK"' in backup_block
    assert 'bash ${baseDir}/bin/report_history_append.sh --no-lock "\\$ROUND_REPORT_JSON" "\\$REPORT_HISTORY_JSONL" "\\$REPORT_HISTORY_LOCK"' in backup_block
    assert 'WARN: report history publication failed (rc=\\$history_rc)' in backup_block
    assert 'perl ${baseDir}/bin/report_run_json.pl \\' in backup_block
    assert 'bash ${baseDir}/bin/report_run_index_update.sh "\\$RUN_REPORT_JSON" "\\$RUN_INDEX_JSONL" "\\$RUN_INDEX_LOCK"' in backup_block
    assert 'ensure_local_round_alias() {' in summary_block
    assert 'ROUND_DEMULT_RPT_LOCAL="${barcode}_demult_rpt.txt"' in summary_block
    assert 'ROUND_DEMULT_SIDECAR_LOCAL="${barcode}_demult_rpt.contract.tsv"' in summary_block
    assert 'ROUND_OTU_RPT_LOCAL="${barcode}_otu_def_rpt.txt"' in summary_block
    assert 'ROUND_OTU_SIDECAR_LOCAL="${barcode}_otu_def_rpt.contract.tsv"' in summary_block
    assert 'ensure_local_round_alias "${demult_rpt}" "\\$ROUND_DEMULT_RPT_LOCAL"' in summary_block
    assert 'ensure_local_round_alias "${demult_rpt_sidecar}" "\\$ROUND_DEMULT_SIDECAR_LOCAL"' in summary_block
    assert 'ensure_local_round_alias "${otu_def_rpt}" "\\$ROUND_OTU_RPT_LOCAL"' in summary_block
    assert 'ensure_local_round_alias "${otu_def_rpt_sidecar}" "\\$ROUND_OTU_SIDECAR_LOCAL"' in summary_block
    assert '"\\$ROUND_DEMULT_RPT_LOCAL" \\' in summary_block
    assert '"\\$ROUND_DEMULT_SIDECAR_LOCAL" \\' in summary_block
    assert '"\\$ROUND_OTU_RPT_LOCAL" \\' in summary_block
    assert '"\\$ROUND_OTU_SIDECAR_LOCAL"' in summary_block
    assert 'write_report_metadata_files' in backup_block
    assert 'printf \'render=0\\nround_barcode=%s\\n\' "${round_barcode}" > "\\$RENDER_REQUEST_FILE"' in backup_block
    assert 'printf \'render=1\\nround_barcode=%s\\n\' "${round_barcode}" > "\\$RENDER_REQUEST_FILE"' in backup_block
    async_block = text.split("process async_report_render {", 1)[1]
    assert 'tuple val(barcode), val(round_barcode), file(render_request_file) from report_render_request_ch' in async_block
    assert 'REQUEST_RENDER="\\$(awk -F= \'/^render=/{print \\$2; exit}\' "${render_request_file}" 2>/dev/null || true)"' in async_block
    assert 'if ! acquire_lock_dir_wait "\\$REPORT_RENDER_LOCK" "\\$RENDER_LOCK_WAIT"; then' in async_block
    assert '--history "\\$SNAPSHOT_PATH" \\' in async_block
    assert '--skip-root-report \\' in async_block
    assert 'python3 ${baseDir}/bin/report_publication_check.py \\' in async_block
    assert 'clear_run_report_pending' in async_block
    assert 'REPORT_ROOT_RENDER_LOCK="${params.outdir}/.report_root_render.lock"' in async_block
    assert "blst_rpt_summary = preferRealRoundRows(blst_rpt_summary, failed_blst_rpt_summary, 'blst_rpt_summary')" in text
    assert "blast_agg_ch = preferRealRoundRows(blast_agg_ch, failed_blast_agg_ch, 'blast_agg_ch')" in text
    assert "cons_agg_ch = preferRealRoundRows(cons_agg_ch, failed_cons_agg_ch, 'cons_agg_ch')" in text
    demult_source_idx = summary_block.index('DEMULT_RPT_SOURCE="${ongoingStateDir}/_state/${barcode}_demult_rpt.txt"')
    cumulative_stage_idx = summary_block.index('cp "\\$DEMULT_RPT_SOURCE" "${barcode}_demult_rpt_cumulative.txt"')
    demult_summary_idx = summary_block.index('bash ${baseDir}/bin/demult_summary.sh ${barcode}_demult_rpt_cumulative.txt ${barcode}')
    summary_copy_idx = summary_block.index('cp "${barcode}_summary_demult_rpt.txt" "${ongoingStateDir}/_state/${barcode}_summary_demult_rpt.txt"')
    read_counts_idx = summary_block.index('_plot_key="Read_counts|')
    sample_assets_idx = summary_block.index('report_sample_read_counts_plots.sh \\')
    final_render_idx = summary_block.index('render_round_report_json "\\$ROUND_REPORT_JSON"')
    stage_sync_idx = summary_block.index('report_sync_figures.pl \\')
    live_stage_idx = summary_block.index('report_live_stage.sh \\')
    wait_idx = summary_block.index('wait || true')
    cumulative_stage_cmd = 'cp "\\$DEMULT_RPT_SOURCE" "${barcode}_demult_rpt_cumulative.txt"'
    post_stage_block = summary_block[cumulative_stage_idx:final_render_idx]
    post_stage_suffix = post_stage_block[len(cumulative_stage_cmd):]
    post_stage_suffix = re.sub(r'\\\s*\n\s*', ' ', post_stage_suffix)
    assert not _has_forbidden_demult_write(post_stage_suffix), post_stage_suffix
    assert demult_source_idx < cumulative_stage_idx < demult_summary_idx < summary_copy_idx < read_counts_idx < sample_assets_idx
    assert stage_sync_idx < live_stage_idx < final_render_idx
    assert demult_source_idx < wait_idx < final_render_idx


def test_forbidden_demult_write_scanner_shell_position_regression() -> None:
    preserved_matches = (
        '{ cp tmp "${demult_rpt}"; }',
        '(cp tmp "${demult_rpt}")',
        'case x in y) cp tmp "${demult_rpt}" ;; esac',
        'case x\nin\n  y) cp tmp "${demult_rpt}" ;;\nesac',
        'case x in y)cp tmp "${demult_rpt}" ;; esac',
        'case x in\ny)\ncp tmp "${demult_rpt}"\n;;\nesac',
        'case $(foo | bar) in y) cp tmp "${demult_rpt}" ;; esac',
        'case `foo && bar` in y) mv tmp "${barcode}_demult_rpt.txt" ;; esac',
        'case $(foo `bar | baz`) in y) cp tmp "${demult_rpt}" ;; esac',
        'case x in y) echo ok ;; z) cp tmp "${demult_rpt}" ;; esac',
        'if true; then cp tmp "${demult_rpt}"; fi',
        'FOO=1 cp tmp "${demult_rpt}"',
        'FOO="a b" cp tmp "${demult_rpt}"',
        "FOO='a b' cp tmp \"${demult_rpt}\"",
        'command cp tmp "${demult_rpt}"',
        'FOO=1 command cp tmp "${demult_rpt}"',
        'env VAR=1 mv tmp "${barcode}_demult_rpt.txt"',
        'env VAR="a b" mv tmp "${barcode}_demult_rpt.txt"',
        'FOO=1 env VAR=1 cp tmp "${demult_rpt}"',
        'command env VAR=1 cp tmp "${demult_rpt}"',
        'env VAR=1 cp tmp "${demult_rpt}"',
        'command env VAR=1 mv tmp "${barcode}_demult_rpt.txt"',
        'FOO=1 command env VAR=1 cp tmp "${demult_rpt}"',
        'printf ${foo} > "${demult_rpt}"',
        'printf ${var#pat} > "${demult_rpt}"',
        'printf ${var##pat} > "${demult_rpt}"',
        'echo $(cat x) > "${demult_rpt}"',
        'echo $(cat a | cat b) > "${demult_rpt}"',
        'printf $(foo; bar) > "${demult_rpt}"',
        'cp ${src} "${demult_rpt}"',
        'mv $(cat src) "${demult_rpt}"',
        'mv $(foo && bar) "${demult_rpt}"',
        'mv `foo && bar` "${demult_rpt}"',
        'echo `foo | bar` > "${barcode}_demult_rpt.txt"',
        'echo \\# not-comment > "${demult_rpt}"',
        'printf x 1>> "${barcode}_demult_rpt.txt"',
    )
    rejected_non_matches = (
        '# cp tmp "${demult_rpt}"',
        'echo ok # ; cp tmp "${demult_rpt}"',
        'echo ok # && cp tmp "${demult_rpt}"',
        'echo { cp tmp "${demult_rpt}"',
        'echo ( cp tmp "${demult_rpt}"',
        'echo x) cp tmp "${demult_rpt}"',
        'echo ${foo} cp tmp "${demult_rpt}"',
        'echo then cp tmp "${demult_rpt}"',
        'echo "(cp tmp ${demult_rpt})"',
        'echo "{ cp tmp ${demult_rpt} }"',
        'case x in y) echo ok ;; esac; echo x) cp tmp "${demult_rpt}"',
        'cp "${demult_rpt}" backup.tsv',
    )

    for shell_text in preserved_matches:
        assert _has_forbidden_demult_write(shell_text), shell_text

    for shell_text in rejected_non_matches:
        assert not _has_forbidden_demult_write(shell_text), shell_text


def test_main_nf_prune_apply_ordering_regression() -> None:
    """reads_apply_prune_ids.pl must be in consensus (via consensus_prune_apply.sh), not
    blast_OTU_pretax.  In the consensus process: recovery must precede collect (collect
    removes OriginalReads when keep=1), and collect must precede the prune-apply call.
    """
    text = MAIN_NF.read_text(encoding="utf-8")
    blast_block = text.split("process blast_OTU_pretax {", 1)[1].split("process consensus {", 1)[0]
    consensus_block = text.split("process consensus {", 1)[1].split("process _reporting_consensus_tax {", 1)[0]
    assert "reads_apply_prune_ids.pl" not in blast_block, (
        "reads_apply_prune_ids.pl should have been moved out of blast_OTU_pretax"
    )
    # reads_apply_prune_ids.pl is referenced as --apply-script arg in the consensus call.
    assert "reads_apply_prune_ids.pl" in consensus_block
    assert "consensus_recovered_reads.pl" in consensus_block
    assert "consensus_prune_apply.sh" in consensus_block
    assert "round_prune_ids_applied_last.list" in consensus_block
    assert "--c1-prune-ids" in consensus_block
    assert "_pruned_barrier.list" in consensus_block
    assert "--pruned-archive" in consensus_block
    assert "RECOVERY_IDS=" in consensus_block
    assert "WARN: failed to recover consensus-assigned reads fallback" in consensus_block
    # Ordering: recovery → collect → apply
    recovery_pos = consensus_block.index("consensus_recovered_reads.pl")
    collect_pos  = consensus_block.index("consensus_original_reads_collect.sh")
    apply_pos    = consensus_block.index("consensus_prune_apply.sh")
    assert recovery_pos < collect_pos, (
        "consensus_recovered_reads.pl must appear before consensus_original_reads_collect.sh "
        "(collect removes OriginalReads directories when keep=1)"
    )
    assert collect_pos < apply_pos, (
        "consensus_original_reads_collect.sh must appear before consensus_prune_apply.sh"
    )


def test_main_nf_wires_pruned_archive_recovery_defaults() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    otu_block = text.split("process OTU_definition {", 1)[1].split("process _reporting_OTU_definition {", 1)[0]

    assert "def otuPrunedRecoveryEnabled = parseBoolStrict(params.otu_pruned_recovery_enabled, false, 'otu_pruned_recovery_enabled')" in text
    assert "def otuPrunedRecoveryIdentity = formatOtuIdentity(params.otu_pruned_recovery_identity)" in text
    assert "def otuPrunedRecoveryTargetPolicyCanonical = params.otu_pruned_recovery_target_policy.toString().trim().toLowerCase()" in text
    assert "def otuPrunedRecoveryFailurePolicyCanonical = params.otu_pruned_recovery_failure_policy.toString().trim().toLowerCase()" in text

    assert 'OTU_PRUNED_RECOVERY_ENABLED="${otuPrunedRecoveryEnabled ? 1 : 0}"' in otu_block
    assert 'OTU_PRUNED_RECOVERY_ID="${otuPrunedRecoveryIdentity}"' in otu_block
    assert 'OTU_PRUNED_RECOVERY_TARGET_POLICY="${otuPrunedRecoveryTargetPolicyCanonical}"' in otu_block
    assert 'OTU_PRUNED_RECOVERY_FAILURE_POLICY="${otuPrunedRecoveryFailurePolicyCanonical}"' in otu_block
    assert 'PRUNED_ARCHIVE="\\${STATE_DIR}/${barcode}_pruned_archive.fasta"' in otu_block
    assert "otu_frozen_members_from_clstr.pl" in otu_block
    assert "archive_recovery recovered=" in otu_block


def test_main_nf_uses_keyed_joins_for_high_risk_multi_input_processes() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    reporting_hac_block = text.split("process _reporting_hac_basecalling {", 1)[1].split("process demultiplexing_hq_reads {", 1)[0]
    reporting_otu_block = text.split("process _reporting_OTU_definition {", 1)[1].split("process blast_OTU_pretax {", 1)[0]
    blast_block = text.split("process blast_OTU_pretax {", 1)[1].split("process _reporting_blast_pretax {", 1)[0]
    reporting_blast_block = text.split("process _reporting_blast_pretax {", 1)[1].split("process consensus {", 1)[0]

    assert 'static def strictRoundJoin(left, right, label = null)' in CHANNEL_UTILS.read_text(encoding='utf-8')
    assert '.join(rightKeyed, by: 0, failOnMismatch: true, failOnDuplicate: true)' in CHANNEL_UTILS.read_text(encoding='utf-8')
    assert 'hq_reads_report_with_fast_control = ChannelUtils.strictRoundJoin(hq_reads_report, fast_control)' in text
    assert 'tuple val(barcode), val(round_barcode), file(round_hac_sam), file(round_hac_fastq) from hq_reads_report_with_fast_control' in reporting_hac_block

    assert 'otu_def_reporting_inputs = ChannelUtils.strictRoundJoin(report_otu, demult_control)' in text
    assert 'tuple val(barcode), val(round_barcode), file(otu_clstr), file(demult) from otu_def_reporting_inputs' in reporting_otu_block

    assert 'blast_pretax_inputs = ChannelUtils.strictRoundJoinAll([' in text
    assert 'tuple val(barcode), val(round_barcode), file(fasta_hq_qced), file(qced_reads_nr), file(read_file), file(otu_def_rpt), file(otu_members_round), file(otu_sizes_round) from blast_pretax_inputs' in blast_block

    assert 'report_blast_inputs = ChannelUtils.strictRoundJoin(report_blast, hac_read_control)' in text
    assert 'tuple val(barcode), val(round_barcode), file(round_sup_sam), file(round_sup_tsv), file(blast_sup_fastq), file(blast_read), file(blast_report_otu), file(blast_report_noadapter), file(blast_filter_stats) from report_blast_inputs' in reporting_blast_block
    assert 'cp ${round_sup_tsv} ${barcode}_round_sup.tsv 2>/dev/null || true' in reporting_blast_block
    assert 'persist_sup_tsv ${barcode}_round_sup.tsv' in reporting_blast_block
    assert 'perl ${doradoBin} summary ${round_sup_sam}' not in reporting_blast_block
    assert '${doradoBin} summary ${round_sup_sam}' not in reporting_blast_block

    assert "process agg_reports {" not in text
    assert "agg_reports_inputs = ChannelUtils.strictRoundJoin(blast_agg_ch, cons_agg_ch)" not in text
    assert "reports_blast = ChannelUtils.strictRoundJoin(blast_agg_ch, cons_agg_ch, 'reports_blast')" in text
    assert '"\\$STATE_TMP"/*_seen_read_ids.tsv' in text
    assert '"\\$STATE_TMP"/*_on_target_state.tsv' in text
    assert 'getting_run_summary_with_path = ChannelUtils.strictRoundJoin(getting_run_summary_inputs, get_summary_ch)' in text
    assert 'complete_round_with_path = ChannelUtils.strictRoundJoin(complete_round_ch, close_round_ch)' in text

    assert 'channel pairing mismatch' not in text
    assert 'barcode2' not in text
    assert 'round_barcode2' not in text
    assert 'barcode3' not in text
    assert 'round_barcode3' not in text


def test_main_nf_fails_fast_on_round_join_key_loss() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    channel_utils_text = CHANNEL_UTILS.read_text(encoding='utf-8')
    assert 'static def strictRoundJoin(left, right, label = null)' in channel_utils_text
    assert '.join(rightKeyed, by: 0, failOnMismatch: true, failOnDuplicate: true)' in channel_utils_text
    assert 'static def isStrictRoundJoinChannelLike(ch)' in channel_utils_text
    assert 'ChannelUtils.strictRoundJoin' in text


def test_main_nf_wires_track_identity_into_demux_and_consensus_guards() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    demux_block = text.split("process demultiplexing_hq_reads {", 1)[1].split("# -- §3: Primers-only mode", 1)[0]
    consensus_block = text.split("# -- §3: Consensus_simple.sh execution (reads or cache-only mode) --", 1)[1].split(
        'echo "INFO: No sup_reads and no consensus cache; skipping Consensus_simple.sh this round" 1>&2',
        1,
    )[0]

    assert 'TRACK_IDENTITY_TSV="${sampleInfoDir}/track_identity.tsv"' in demux_block
    assert 'build_track_adapter_marker_map "\\$TRACK_IDENTITY_TSV" "\\$TRACK_ADAPTER_MARKER_MAP"' in demux_block
    assert 'filter_track_marker_unit_fastq.sh' in demux_block
    assert 'RTBIOSCAN_TRACK_IDENTITY_TSV="${sampleInfoDir}/track_identity.tsv"' in consensus_block


def test_main_nf_backup_update_and_clean_uses_incremental_backup_sync_helpers() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    backup_block = text.split("process backup_update_and_clean {", 1)[1].split('"""', 2)[1]

    assert 'source "${baseDir}/bin/lib/backup_sync.sh"' in backup_block
    assert 'if (( \\${#rpts[@]} )); then cp -f "\\${rpts[@]}" "\\$ONGOING_FINAL/"; fi' not in backup_block
    assert '[[ -e "\\$ONGOING_FINAL/${barcode}_read_info_rpt.txt" ]] && gzip -f "\\$ONGOING_FINAL/${barcode}_read_info_rpt.txt"' not in backup_block
    assert 'cp -R ${ongoingStateDir}/Consensus/. "\\$CURRENT_TEMP_ROOT/sequences/Consensus/" 2>/dev/null || true' not in backup_block
    assert 'cp -R ${ongoingStateDir}/Consensus/. "\\$CURRENT_ROOT/sequences/Consensus/" 2>/dev/null || true' not in backup_block
    assert 'sync_changed_files "\\$ONGOING_FINAL" "\\${rpts_plain[@]}"' in backup_block
    assert 'publish_gzip_atomic "\\$rpt" "\\$ONGOING_FINAL/\\$(basename -- "\\$rpt").gz"' in backup_block
    assert 'sync_changed_tree "${ongoingStateDir}/Consensus" "\\$CURRENT_TEMP_ROOT/sequences/Consensus" 2>/dev/null || true' in backup_block
    assert 'sync_changed_tree "${ongoingStateDir}/Consensus" "\\$CURRENT_ROOT/sequences/Consensus" 2>/dev/null || true' in backup_block
    assert '"\\$STATE_TMP"/read_qscore_rolling.tsv' in text
    assert '"\\$STATE_TMP"/*_seen_read_ids.tsv' in text
    assert '"\\$STATE_TMP"/*_on_target_state.tsv' in text

    for name in (
        '${barcode}_demult_rpt.txt',
        '${barcode}_otu_def_rpt.txt',
        '${barcode}_on_target_rpt.txt',
        '${barcode}_read_info_on_target_barcode_rpt.txt',
        '${barcode}_read_info_rpt.txt',
    ):
        assert name in backup_block
    assert 'strictRoundJoinAll requires channel-like inputs; element ${idx + 1} was ${ch?.getClass()?.name ?: \'null\'}' in CHANNEL_UTILS.read_text(encoding="utf-8")


def test_backup_update_and_clean_publishes_report_history_unconditionally() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    backup_block = text.split("process backup_update_and_clean {", 1)[1].split('"""', 2)[1]
    publish_block = backup_block.split('if [ "\\$report_live_publish_rc" -ne 0 ]; then', 1)[1].split('if [ "\\$history_rc" -ne 0 ]; then', 1)[0]

    assert 'set +e\n\t\t\tpublish_report_history\n\t\t\thistory_rc=\\$?\n\t\t\tset -e' in publish_block
    assert 'FEEDER_SLICE_SIDECAR' not in publish_block
    assert 'IS_FEEDER_SLICE_ROUND' not in publish_block
    assert 'skipping history append to prevent orphan round pollution' not in publish_block
    assert 'if [ "\\$history_rc" -ne 0 ]; then' in backup_block


def test_main_nf_defines_check_hostname_helper() -> None:
    text = MAIN_NF.read_text(encoding="utf-8")
    assert 'checkHostname()' in text
    assert 'def checkHostname() {' in text
