import hashlib
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "runtime_toolchain_fingerprint.pl"
POLICY = REPO_ROOT / "conf" / "runtime_compatibility" / "toolchain_legacy_v1.tsv"


def _write_executable(path: Path, output: str, *, exit_code: int = 0) -> None:
    path.write_text(
        "#!/bin/sh\n"
        f"cat <<'EOF'\n{output}\nEOF\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _fake_runtime(tmp_path: Path) -> dict[str, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    outputs = {
        "blastn": "blastn: 2.15.0+",
        "lastal": "lastal 1542",
        "taxonkit": "taxonkit v0.14.2",
        "seqkit": "seqkit v2.6.1",
        "cutadapt": "4.6",
        "vsearch": "vsearch v2.29.1",
        "cd-hit-est": "CD-HIT version 4.8.1",
        "samtools": "samtools 1.21",
        "seqtk": "Version: 1.4-r122",
        "Rscript": (
            "tool_Rscript\t4.3.3\n"
            "r_package_DECIPHER\t2.30.0\n"
            "r_package_Biostrings\t2.70.1"
        ),
        "dorado": "0.7.0+71cc7442",
    }
    for name, output in outputs.items():
        _write_executable(bindir / name, output)

    models = tmp_path / "models"
    model_names = {
        "fast": "dna_r10.4.1_e8.2_400bps_fast@v5.0.0",
        "hac": "dna_r10.4.1_e8.2_400bps_hac@v5.0.0",
        "sup": "dna_r10.4.1_e8.2_400bps_sup@v4.3.0",
    }
    model_paths: dict[str, Path] = {}
    for stage, name in model_names.items():
        path = models / name
        path.mkdir(parents=True)
        (path / "config.toml").write_text(
            f"[model]\nstage = \"{stage}\"\n",
            encoding="utf-8",
        )
        model_paths[stage] = path
    return {"bindir": bindir, "dorado": bindir / "dorado", **model_paths}


def _run(
    tmp_path: Path,
    runtime: dict[str, Path],
    *,
    backend: str = "host",
    policy_manifest: Path = POLICY,
    lock_manifest: Path | None = None,
    release_manifest: Path | None = None,
    summary_bin: Path | None = None,
    input_mode: str = "file",
    input_compat_helper: Path | None = None,
    fast_args: str = "--emit-sam --chunksize 1000",
) -> subprocess.CompletedProcess[str]:
    output = tmp_path / "fingerprint.tsv"
    command = [
        "perl",
        str(SCRIPT),
        "--policy-manifest",
        str(policy_manifest),
        "--runtime-backend",
        backend,
        "--dorado-bin",
        str(runtime["dorado"]),
        "--dorado-input-mode",
        input_mode,
        "--dorado-model",
        f"fast={runtime['fast']}",
        "--dorado-model",
        f"hac={runtime['hac']}",
        "--dorado-model",
        f"sup={runtime['sup']}",
        "--dorado-device",
        "metal",
        "--dorado-args",
        f"fast={fast_args}",
        "--dorado-args",
        "hac=--emit-sam --chunksize 2000",
        "--dorado-args",
        "sup=--emit-sam --chunksize 5000",
        "--output",
        str(output),
    ]
    if lock_manifest is not None:
        command.extend(["--runtime-lock-manifest", str(lock_manifest)])
    if release_manifest is not None:
        command.extend(["--dorado-release-manifest", str(release_manifest)])
    if summary_bin is not None:
        command.extend(["--dorado-summary-bin", str(summary_bin)])
    if input_compat_helper is not None:
        command.extend(
            ["--dorado-input-compat-helper", str(input_compat_helper)]
        )
    env = dict(os.environ)
    env["PATH"] = f"{runtime['bindir']}:{env.get('PATH', '')}"
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _values(path: Path) -> dict[str, str]:
    return dict(
        line.split("\t", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_fingerprint_binds_live_versions_dorado_models_device_and_args(
    tmp_path: Path,
) -> None:
    runtime = _fake_runtime(tmp_path)
    result = _run(tmp_path, runtime)
    assert result.returncode == 0, result.stderr
    assert "unqualified runtime" in result.stderr
    values = _values(tmp_path / "fingerprint.tsv")
    assert values["runtime_backend"] == "host"
    assert values["tool_lastal_version"] == "1542"
    assert values["r_package_DECIPHER_version"] == "2.30.0"
    assert values["dorado_version"] == "0.7.0+71cc7442"
    assert values["dorado_device"] == "metal"
    assert values["dorado_fast_args"] == "--emit-sam --chunksize 1000"
    assert values["dorado_release_status"] == "unqualified_explicit"
    assert len(values["dorado_fast_model_content_sha256"]) == 64
    canonical = "".join(
        f"{key}\t{values[key]}\n"
        for key in sorted(values)
        if key != "fingerprint_id"
    )
    assert values["fingerprint_id"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert result.stdout.strip() == values["fingerprint_id"]


def test_effective_dorado_argument_change_changes_fingerprint(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    first = _run(tmp_path, runtime)
    assert first.returncode == 0, first.stderr
    first_id = first.stdout.strip()

    second = _run(
        tmp_path,
        runtime,
        fast_args="--emit-sam --chunksize 2048",
    )
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() != first_id


def test_unqualified_model_content_change_changes_fingerprint(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    first = _run(tmp_path, runtime)
    assert first.returncode == 0, first.stderr
    first_id = first.stdout.strip()

    (runtime["hac"] / "weights.tensor").write_bytes(b"changed model weights")
    second = _run(tmp_path, runtime)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() != first_id


def test_runtime_version_mismatch_is_fail_closed(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    _write_executable(runtime["bindir"] / "lastal", "lastal 1454")
    result = _run(tmp_path, runtime)
    assert result.returncode != 0
    assert "lastal version mismatch: expected 1542, found 1454" in result.stderr


def test_conda_backend_requires_and_binds_lock_manifest(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    missing = _run(tmp_path, runtime, backend="conda")
    assert missing.returncode != 0
    assert "--runtime-lock-manifest is required" in missing.stderr

    lock = tmp_path / "conda-lock.yml"
    lock.write_text("fixture lock\n", encoding="utf-8")
    bound = _run(tmp_path, runtime, backend="conda", lock_manifest=lock)
    assert bound.returncode == 0, bound.stderr
    values = _values(tmp_path / "fingerprint.tsv")
    assert values["runtime_backend"] == "conda"
    assert values["runtime_lock_manifest_sha256"] == hashlib.sha256(
        lock.read_bytes()
    ).hexdigest()


def test_non_executable_dorado_is_rejected_before_version_probe(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    runtime["dorado"].chmod(0o644)
    result = _run(tmp_path, runtime)
    assert result.returncode != 0
    assert "runtime executable is not executable" in result.stderr


def test_legacy_dorado_binds_separate_summary_and_input_adapter(
    tmp_path: Path,
) -> None:
    runtime = _fake_runtime(tmp_path)
    _write_executable(runtime["dorado"], "0.2.3+4ed609d")
    summary_bin = runtime["bindir"] / "dorado-summary"
    _write_executable(summary_bin, "0.7.0+71cc7442")
    policy = tmp_path / "toolchain-legacy-dorado.tsv"
    policy.write_text(
        POLICY.read_text(encoding="utf-8").replace(
            "dorado\tdorado\t0.7.0+71cc7442\n",
            "dorado\tdorado\t0.2.3+4ed609d\n"
            "dorado\tsummary\t0.7.0+71cc7442\n",
        ),
        encoding="utf-8",
    )
    helper = tmp_path / "dorado-input-compat"
    _write_executable(helper, "compat helper")

    result = _run(
        tmp_path,
        runtime,
        policy_manifest=policy,
        summary_bin=summary_bin,
        input_mode="directory",
        input_compat_helper=helper,
    )
    assert result.returncode == 0, result.stderr
    values = _values(tmp_path / "fingerprint.tsv")
    assert values["dorado_version"] == "0.2.3+4ed609d"
    assert values["dorado_summary_version"] == "0.7.0+71cc7442"
    assert values["dorado_input_mode"] == "directory"
    assert values["dorado_summary_binary_sha256"] == hashlib.sha256(
        summary_bin.read_bytes()
    ).hexdigest()
    assert values["dorado_input_compat_helper_sha256"] == hashlib.sha256(
        helper.read_bytes()
    ).hexdigest()


def test_qualified_dorado_manifest_binds_selected_release_layout(
    tmp_path: Path,
) -> None:
    runtime = _fake_runtime(tmp_path)
    rows = [
        "# release_id=dorado-0.7.0-test",
        "# platform=test",
        "# expected_version=0.7.0+71cc7442",
        "# source_url=https://example.invalid/dorado.zip",
        "kind\tartifact\tsha256\tbytes\tmode\trole",
        (
            "runtime\tbin/dorado\t"
            f"{hashlib.sha256(runtime['dorado'].read_bytes()).hexdigest()}\t"
            f"{runtime['dorado'].stat().st_size}\t0555\tDorado executable"
        ),
    ]
    for stage in ["fast", "hac", "sup"]:
        config = runtime[stage] / "config.toml"
        rows.append(
            "model\t"
            f"models/{runtime[stage].name}/config.toml\t"
            f"{hashlib.sha256(config.read_bytes()).hexdigest()}\t"
            f"{config.stat().st_size}\t0444\t{stage} config"
        )
    manifest = tmp_path / "dorado-release.tsv"
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")

    result = _run(tmp_path, runtime, release_manifest=manifest)
    assert result.returncode == 0, result.stderr
    assert "unqualified runtime" not in result.stderr
    values = _values(tmp_path / "fingerprint.tsv")
    assert values["dorado_release_status"] == "qualified_manifest"
    assert values["dorado_release_id"] == "dorado-0.7.0-test"
    assert values["dorado_release_manifest_sha256"] == hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
