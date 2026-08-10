import hashlib
import os
import subprocess
from pathlib import Path

import pytest


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
    with (bindir / "dorado").open("a", encoding="utf-8") as handle:
        handle.write("# qualified hash-check padding\n")

    metallib = tmp_path / "lib" / "default.metallib"
    metallib.parent.mkdir()
    metallib.write_bytes(b"qualified Metal runtime library\n")

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
        (path / "weights.tensor").write_bytes(
            f"qualified {stage} model weights\n".encode()
        )
        model_paths[stage] = path
    return {
        "bindir": bindir,
        "dorado": bindir / "dorado",
        "metallib": metallib,
        **model_paths,
    }


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


def _qualified_manifest(tmp_path: Path, runtime: dict[str, Path]) -> Path:
    archive_payload = b"manifest-only qualified archive fixture\n"
    rows = [
        "# release_id=dorado-0.7.0-test",
        "# platform=test",
        "# expected_version=0.7.0+71cc7442",
        "# source_url=https://example.invalid/dorado.zip",
        "kind\tartifact\tsha256\tbytes\tmode\trole",
        (
            "archive\tdorado.zip\t"
            f"{hashlib.sha256(archive_payload).hexdigest()}\t"
            f"{len(archive_payload)}\t-\tDorado archive"
        ),
        (
            "runtime\tbin/dorado\t"
            f"{hashlib.sha256(runtime['dorado'].read_bytes()).hexdigest()}\t"
            f"{runtime['dorado'].stat().st_size}\t0555\tDorado executable"
        ),
        (
            "runtime\tlib/default.metallib\t"
            f"{hashlib.sha256(runtime['metallib'].read_bytes()).hexdigest()}\t"
            f"{runtime['metallib'].stat().st_size}\t0444\tMetal runtime library"
        ),
    ]
    for stage in ["fast", "hac", "sup"]:
        for filename in ["config.toml", "weights.tensor"]:
            artifact = runtime[stage] / filename
            rows.append(
                "model\t"
                f"models/{runtime[stage].name}/{filename}\t"
                f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}\t"
                f"{artifact.stat().st_size}\t0444\t{stage} {filename}"
            )
    manifest = tmp_path / "dorado-release.tsv"
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return manifest


def _tamper_same_size(path: Path) -> None:
    before = path.read_bytes()
    assert len(before) >= 2
    tampered = bytearray(before)
    tampered[-2] ^= 1
    path.write_bytes(tampered)
    assert path.stat().st_size == len(before)
    assert hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(before).digest()


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
    manifest = _qualified_manifest(tmp_path, runtime)

    result = _run(tmp_path, runtime, release_manifest=manifest)
    assert result.returncode == 0, result.stderr
    assert "unqualified runtime" not in result.stderr
    values = _values(tmp_path / "fingerprint.tsv")
    assert values["dorado_release_status"] == "qualified_manifest"
    assert values["dorado_release_id"] == "dorado-0.7.0-test"
    assert not (tmp_path / "dorado.zip").exists()
    assert values["dorado_release_manifest_sha256"] == hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
    # Golden produced by the vulnerable 78ed692 implementation from this fixture.
    assert values["fingerprint_id"] == (
        "77a32dc156092f7ca75cc7b420960934733288aedcde30149f376bc25a65dece"
    )
    assert "dorado_metallib_sha256" not in values


@pytest.mark.parametrize(
    "artifact",
    ["binary", "metallib", "fast_tensor", "hac_tensor", "sup_tensor"],
)
def test_qualified_dorado_manifest_rejects_same_size_artifact_tamper(
    tmp_path: Path, artifact: str
) -> None:
    runtime = _fake_runtime(tmp_path)
    manifest = _qualified_manifest(tmp_path, runtime)
    if artifact == "binary":
        target = runtime["dorado"]
        declared = "bin/dorado"
    elif artifact == "metallib":
        target = runtime["metallib"]
        declared = "lib/default.metallib"
    else:
        stage = artifact.removesuffix("_tensor")
        target = runtime[stage] / "weights.tensor"
        declared = f"models/{runtime[stage].name}/weights.tensor"
    _tamper_same_size(target)

    if artifact == "binary":
        probe = subprocess.run(
            [str(target), "--version"], capture_output=True, text=True, check=False
        )
        assert probe.returncode == 0
        assert probe.stdout.strip() == "0.7.0+71cc7442"

    result = _run(tmp_path, runtime, release_manifest=manifest)
    assert result.returncode != 0
    assert declared in result.stderr
    assert "checksum mismatch" in result.stderr


@pytest.mark.parametrize("artifact_state", ["missing", "symlink"])
def test_qualified_dorado_manifest_rejects_non_regular_runtime_artifact(
    tmp_path: Path, artifact_state: str
) -> None:
    runtime = _fake_runtime(tmp_path)
    manifest = _qualified_manifest(tmp_path, runtime)
    metallib = runtime["metallib"]
    if artifact_state == "missing":
        metallib.unlink()
    else:
        external = tmp_path / "external.metallib"
        external.write_bytes(metallib.read_bytes())
        metallib.unlink()
        metallib.symlink_to(external)

    result = _run(tmp_path, runtime, release_manifest=manifest)
    assert result.returncode != 0
    assert "lib/default.metallib" in result.stderr
    assert "not a regular non-symlink file" in result.stderr


@pytest.mark.parametrize("invalid_row", ["duplicate", "traversal"])
def test_qualified_dorado_manifest_rejects_unsafe_artifact_rows(
    tmp_path: Path, invalid_row: str
) -> None:
    runtime = _fake_runtime(tmp_path)
    manifest = _qualified_manifest(tmp_path, runtime)
    if invalid_row == "duplicate":
        row = next(
            line
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.startswith("runtime\tlib/default.metallib\t")
        )
    else:
        row = (
            "runtime\t../escape\t"
            f"{'0' * 64}\t1\t0444\tunsafe traversal fixture"
        )
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")

    result = _run(tmp_path, runtime, release_manifest=manifest)
    assert result.returncode != 0
    if invalid_row == "duplicate":
        assert "duplicate Dorado release artifact" in result.stderr
    else:
        assert "malformed Dorado release manifest row" in result.stderr
