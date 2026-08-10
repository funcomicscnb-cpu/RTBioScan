#!/usr/bin/env python3
"""Validate a preregistered, decision-neutral FAST shadow measurement plan."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "fast-filter-shadow-measurement-plan-v1"
MATERIAL_CLASSES = {"coi_target", "its2_target", "off_target"}
STAGES = {"fast", "hac", "sup"}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
PLAN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}")
STATE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")

TOP_LEVEL_KEYS = {
    "schema_version",
    "plan_id",
    "created_utc",
    "repository_commit",
    "dataset",
    "runtime",
    "namespace",
    "performance_budgets",
    "analysis",
    "adjudication",
}
DATASET_KEYS = {"representativeness_rationale", "artifacts"}
ARTIFACT_KEYS = {
    "artifact_id",
    "path",
    "sha256",
    "bytes",
    "material_classes",
    "evidence_path",
    "evidence_sha256",
}
RUNTIME_KEYS = {
    "nextflow_bin",
    "nextflow_bin_sha256",
    "nextflow_version",
    "lastal_bin",
    "lastal_bin_sha256",
    "lastal_version",
    "last_index_prj",
    "last_index_prj_sha256",
    "reference_manifest",
    "reference_manifest_sha256",
    "dorado_release_manifest",
    "dorado_release_manifest_sha256",
    "dorado_qualification_report",
    "dorado_qualification_report_sha256",
    "hardware_backend",
    "hardware_identity",
}
NAMESPACE_KEYS = {
    "stable_state_id",
    "candidate_state_id",
    "candidate_output_dir",
    "fast_filter_shadow",
    "taxonomy_behavior_change",
}
BUDGET_KEYS = {
    "minimum_reads_per_second",
    "minimum_bases_per_second",
    "maximum_p95_round_latency_seconds",
    "maximum_total_basecalling_time_ratio",
    "minimum_sequencer_input_headroom_ratio",
}
ANALYSIS_KEYS = {
    "read_length_bins",
    "alignment_identity_bins",
    "error_stratification_protocol_path",
    "error_stratification_protocol_sha256",
    "near_tie_policy",
}
ADJUDICATION_KEYS = {
    "sampling_strategy",
    "sample_per_marker_status",
    "random_seed",
    "references",
}
REFERENCE_KEYS = {
    "marker",
    "name",
    "release",
    "reference_manifest_path",
    "reference_manifest_sha256",
    "search_tool",
    "search_tool_version",
    "protocol_path",
    "protocol_sha256",
    "independent_from_routing_database",
}


class PlanError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="also verify repository state, tool versions, and a fresh output namespace",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="permit a dirty worktree during preflight (development/testing only)",
    )
    return parser.parse_args()


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PlanError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_plan(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise PlanError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanError("measurement plan must be a JSON object")
    return value


def require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PlanError(f"{label} must be an object")
    return value


def require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise PlanError(f"{label} must be an array")
    return value


def require_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise PlanError(f"{label} keys differ; missing={missing}, extra={extra}")


def require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{label} must be a non-empty string")
    text = value.strip()
    if (
        text.upper().startswith(("REPLACE", "TODO", "TBD"))
        or text.startswith("<")
        or text.endswith(">")
    ):
        raise PlanError(f"{label} contains an unresolved placeholder")
    return text


def require_sha256(value: object, label: str) -> str:
    text = require_string(value, label)
    if not SHA256_PATTERN.fullmatch(text):
        raise PlanError(f"{label} must be a lowercase SHA-256")
    return text


def require_positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{label} must be numeric")
    number = float(value)
    if number <= 0:
        raise PlanError(f"{label} must be greater than zero")
    return number


def require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PlanError(f"{label} must be a positive integer")
    return value


def resolve_path(value: object, label: str) -> Path:
    text = require_string(value, label)
    path = Path(text)
    return path if path.is_absolute() else REPO_ROOT / path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, expected_sha256: str, label: str) -> None:
    if not path.is_file():
        raise PlanError(f"{label} is not a regular file: {path}")
    observed = sha256_file(path)
    if observed != expected_sha256:
        raise PlanError(
            f"{label} SHA-256 mismatch for {path}: {observed} != {expected_sha256}"
        )


def validate_dataset(value: object) -> None:
    dataset = require_object(value, "dataset")
    require_keys(dataset, DATASET_KEYS, "dataset")
    require_string(
        dataset["representativeness_rationale"], "dataset.representativeness_rationale"
    )
    artifacts = require_list(dataset["artifacts"], "dataset.artifacts")
    if not artifacts:
        raise PlanError("dataset.artifacts must not be empty")

    observed_ids: set[str] = set()
    observed_paths: set[Path] = set()
    observed_classes: set[str] = set()
    for index, item in enumerate(artifacts):
        label = f"dataset.artifacts[{index}]"
        artifact = require_object(item, label)
        require_keys(artifact, ARTIFACT_KEYS, label)
        artifact_id = require_string(artifact["artifact_id"], f"{label}.artifact_id")
        if artifact_id in observed_ids:
            raise PlanError(f"duplicate dataset artifact_id: {artifact_id}")
        observed_ids.add(artifact_id)

        path = resolve_path(artifact["path"], f"{label}.path").resolve()
        if path in observed_paths:
            raise PlanError(f"duplicate dataset artifact path: {path}")
        observed_paths.add(path)
        expected_sha = require_sha256(artifact["sha256"], f"{label}.sha256")
        verify_file(path, expected_sha, f"{label}.path")
        expected_bytes = require_positive_int(artifact["bytes"], f"{label}.bytes")
        if path.stat().st_size != expected_bytes:
            raise PlanError(
                f"{label}.bytes mismatch for {path}: "
                f"{path.stat().st_size} != {expected_bytes}"
            )

        classes = require_list(
            artifact["material_classes"], f"{label}.material_classes"
        )
        if not classes or any(not isinstance(item, str) for item in classes):
            raise PlanError(f"{label}.material_classes must contain strings")
        class_set = set(classes)
        if len(class_set) != len(classes) or not class_set <= MATERIAL_CLASSES:
            raise PlanError(f"{label}.material_classes contains duplicates or unknown values")
        observed_classes.update(class_set)

        evidence_path = resolve_path(
            artifact["evidence_path"], f"{label}.evidence_path"
        )
        evidence_sha = require_sha256(
            artifact["evidence_sha256"], f"{label}.evidence_sha256"
        )
        verify_file(evidence_path, evidence_sha, f"{label}.evidence_path")

    if observed_classes != MATERIAL_CLASSES:
        raise PlanError(
            "dataset material coverage must include exactly "
            + ", ".join(sorted(MATERIAL_CLASSES))
        )


def read_key_value_report(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter="\t")
        rows = list(reader)
    if not rows or rows[0] != ["field", "value"]:
        raise PlanError(f"unexpected qualification report schema: {path}")
    values: dict[str, str] = {}
    for row in rows[1:]:
        if len(row) != 2 or not row[0] or row[0] in values:
            raise PlanError(f"malformed qualification report row in {path}: {row!r}")
        values[row[0]] = row[1]
    return values


def read_dorado_release_metadata(path: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if not line.startswith("# "):
                break
            fields = line[2:].split("=", 1)
            if len(fields) != 2 or not fields[0] or fields[0] in metadata:
                raise PlanError(f"malformed Dorado release metadata in {path}")
            metadata[fields[0]] = fields[1]
    required = {"release_id", "platform", "expected_version"}
    if not required <= set(metadata):
        raise PlanError(f"Dorado release metadata is incomplete in {path}")
    return metadata


def read_last_prj_version(path: Path) -> str:
    versions: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            fields = raw_line.rstrip("\r\n").split("=", 1)
            if len(fields) == 2 and fields[0] == "version":
                versions.append(fields[1])
    if len(versions) != 1 or not versions[0]:
        raise PlanError(f"LAST .prj must contain exactly one version field: {path}")
    return versions[0]


def validate_runtime(value: object) -> dict[str, object]:
    runtime = require_object(value, "runtime")
    require_keys(runtime, RUNTIME_KEYS, "runtime")
    file_pairs = [
        ("nextflow_bin", "nextflow_bin_sha256"),
        ("lastal_bin", "lastal_bin_sha256"),
        ("last_index_prj", "last_index_prj_sha256"),
        ("reference_manifest", "reference_manifest_sha256"),
        ("dorado_release_manifest", "dorado_release_manifest_sha256"),
        ("dorado_qualification_report", "dorado_qualification_report_sha256"),
    ]
    resolved: dict[str, object] = dict(runtime)
    for path_key, sha_key in file_pairs:
        path = resolve_path(runtime[path_key], f"runtime.{path_key}")
        expected_sha = require_sha256(runtime[sha_key], f"runtime.{sha_key}")
        verify_file(path, expected_sha, f"runtime.{path_key}")
        resolved[path_key] = path

    require_string(runtime["nextflow_version"], "runtime.nextflow_version")
    lastal_version = require_string(runtime["lastal_version"], "runtime.lastal_version")
    last_prj_path = resolved["last_index_prj"]
    assert isinstance(last_prj_path, Path)
    prj_version = read_last_prj_version(last_prj_path)
    if prj_version != lastal_version:
        raise PlanError(
            f"LAST .prj version mismatch: {prj_version!r} != {lastal_version!r}"
        )
    backend = require_string(runtime["hardware_backend"], "runtime.hardware_backend")
    if backend not in {"metal", "cuda"}:
        raise PlanError("runtime.hardware_backend must be metal or cuda")
    require_string(runtime["hardware_identity"], "runtime.hardware_identity")

    report_path = resolved["dorado_qualification_report"]
    release_manifest_path = resolved["dorado_release_manifest"]
    assert isinstance(report_path, Path) and isinstance(release_manifest_path, Path)
    report = read_key_value_report(report_path)
    release = read_dorado_release_metadata(release_manifest_path)
    expected_report = {
        "release_id": release["release_id"],
        "platform": release["platform"],
        "dorado_version": release["expected_version"],
        "device": backend,
        "device_class": "accelerator",
        "qualification_scope": "accelerator_candidate",
        "status": "accelerator_compatibility_passed",
    }
    for field, expected in expected_report.items():
        if report.get(field) != expected:
            raise PlanError(
                f"Dorado qualification report {field} is "
                f"{report.get(field)!r}; expected {expected!r}"
            )
    return resolved


def validate_namespace(value: object) -> dict[str, object]:
    namespace = require_object(value, "namespace")
    require_keys(namespace, NAMESPACE_KEYS, "namespace")
    stable = require_string(namespace["stable_state_id"], "namespace.stable_state_id")
    candidate = require_string(
        namespace["candidate_state_id"], "namespace.candidate_state_id"
    )
    if not STATE_ID_PATTERN.fullmatch(stable) or not STATE_ID_PATTERN.fullmatch(
        candidate
    ):
        raise PlanError("stable and candidate state IDs contain unsupported characters")
    if candidate == stable:
        raise PlanError("candidate_state_id must differ from stable_state_id")
    output_dir = resolve_path(
        namespace["candidate_output_dir"], "namespace.candidate_output_dir"
    )
    if namespace["fast_filter_shadow"] is not True:
        raise PlanError("namespace.fast_filter_shadow must be true")
    if namespace["taxonomy_behavior_change"] is not False:
        raise PlanError("namespace.taxonomy_behavior_change must be false")
    return {**namespace, "candidate_output_dir": output_dir}


def validate_budgets(value: object) -> None:
    budgets = require_object(value, "performance_budgets")
    require_keys(budgets, BUDGET_KEYS, "performance_budgets")
    for metric in ("minimum_reads_per_second", "minimum_bases_per_second"):
        stages = require_object(budgets[metric], f"performance_budgets.{metric}")
        require_keys(stages, STAGES, f"performance_budgets.{metric}")
        for stage in sorted(STAGES):
            require_positive_number(
                stages[stage], f"performance_budgets.{metric}.{stage}"
            )
    require_positive_number(
        budgets["maximum_p95_round_latency_seconds"],
        "performance_budgets.maximum_p95_round_latency_seconds",
    )
    require_positive_number(
        budgets["maximum_total_basecalling_time_ratio"],
        "performance_budgets.maximum_total_basecalling_time_ratio",
    )
    headroom = require_positive_number(
        budgets["minimum_sequencer_input_headroom_ratio"],
        "performance_budgets.minimum_sequencer_input_headroom_ratio",
    )
    if headroom <= 1:
        raise PlanError("minimum_sequencer_input_headroom_ratio must exceed 1")


def validate_bins(value: object, label: str, maximum: float | None = None) -> None:
    bins = require_list(value, label)
    if len(bins) < 2:
        raise PlanError(f"{label} must contain at least two boundaries")
    numbers: list[float] = []
    for index, item in enumerate(bins):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise PlanError(f"{label}[{index}] must be numeric")
        number = float(item)
        if number < 0:
            raise PlanError(f"{label}[{index}] must be non-negative")
        numbers.append(number)
    if numbers[0] != 0 or any(b <= a for a, b in zip(numbers, numbers[1:])):
        raise PlanError(f"{label} must start at zero and increase strictly")
    if maximum is not None and numbers[-1] != maximum:
        raise PlanError(f"{label} must end at {maximum:g}")


def validate_analysis(value: object) -> None:
    analysis = require_object(value, "analysis")
    require_keys(analysis, ANALYSIS_KEYS, "analysis")
    validate_bins(analysis["read_length_bins"], "analysis.read_length_bins")
    validate_bins(
        analysis["alignment_identity_bins"],
        "analysis.alignment_identity_bins",
        maximum=100,
    )
    protocol_path = resolve_path(
        analysis["error_stratification_protocol_path"],
        "analysis.error_stratification_protocol_path",
    )
    protocol_sha = require_sha256(
        analysis["error_stratification_protocol_sha256"],
        "analysis.error_stratification_protocol_sha256",
    )
    verify_file(
        protocol_path, protocol_sha, "analysis.error_stratification_protocol_path"
    )
    if analysis["near_tie_policy"] != "retain":
        raise PlanError("analysis.near_tie_policy must remain retain")


def validate_adjudication(value: object, runtime: dict[str, object]) -> None:
    adjudication = require_object(value, "adjudication")
    require_keys(adjudication, ADJUDICATION_KEYS, "adjudication")
    require_string(adjudication["sampling_strategy"], "adjudication.sampling_strategy")
    require_positive_int(
        adjudication["sample_per_marker_status"],
        "adjudication.sample_per_marker_status",
    )
    require_positive_int(adjudication["random_seed"], "adjudication.random_seed")
    references = require_list(adjudication["references"], "adjudication.references")
    observed_markers: set[str] = set()
    for index, item in enumerate(references):
        label = f"adjudication.references[{index}]"
        reference = require_object(item, label)
        require_keys(reference, REFERENCE_KEYS, label)
        marker = require_string(reference["marker"], f"{label}.marker")
        if marker not in {"COI", "ITS2"} or marker in observed_markers:
            raise PlanError(f"{label}.marker must uniquely cover COI and ITS2")
        observed_markers.add(marker)
        for field in ("name", "release", "search_tool", "search_tool_version"):
            require_string(reference[field], f"{label}.{field}")
        if reference["independent_from_routing_database"] is not True:
            raise PlanError(f"{label} must be independent from the routing database")
        for path_key, sha_key in (
            ("reference_manifest_path", "reference_manifest_sha256"),
            ("protocol_path", "protocol_sha256"),
        ):
            path = resolve_path(reference[path_key], f"{label}.{path_key}")
            expected_sha = require_sha256(reference[sha_key], f"{label}.{sha_key}")
            verify_file(path, expected_sha, f"{label}.{path_key}")
            if path_key == "reference_manifest_path":
                routing_path = runtime["reference_manifest"]
                routing_sha = runtime["reference_manifest_sha256"]
                assert isinstance(routing_path, Path) and isinstance(routing_sha, str)
                if path.resolve() == routing_path.resolve() or expected_sha == routing_sha:
                    raise PlanError(
                        f"{label}.reference_manifest_path reuses the routing manifest"
                    )
    if observed_markers != {"COI", "ITS2"}:
        raise PlanError("adjudication references must cover exactly COI and ITS2")


def run_version(command: list[str], env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        raise PlanError(f"version command failed ({' '.join(command)}): {output}")
    return output


def validate_preflight(
    plan: dict[str, object],
    runtime: dict[str, object],
    namespace: dict[str, object],
    allow_dirty: bool,
) -> None:
    expected_commit = require_string(
        plan["repository_commit"], "repository_commit"
    )
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    observed_commit = completed.stdout.strip()
    if completed.returncode != 0 or observed_commit != expected_commit:
        raise PlanError(
            f"repository commit mismatch: {observed_commit!r} != {expected_commit!r}"
        )
    if not allow_dirty:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if status.returncode != 0 or status.stdout:
            raise PlanError("preflight requires a clean worktree")

    nextflow_bin = runtime["nextflow_bin"]
    lastal_bin = runtime["lastal_bin"]
    assert isinstance(nextflow_bin, Path) and isinstance(lastal_bin, Path)
    nextflow_version = require_string(
        runtime["nextflow_version"], "runtime.nextflow_version"
    )
    nextflow_env = {**os.environ, "NXF_VER": nextflow_version}
    nextflow_output = run_version([str(nextflow_bin), "-version"], nextflow_env)
    if not re.search(rf"\bversion\s+{re.escape(nextflow_version)}\b", nextflow_output):
        raise PlanError(
            f"Nextflow version mismatch; expected {nextflow_version!r}: "
            f"{nextflow_output!r}"
        )

    lastal_version = require_string(runtime["lastal_version"], "runtime.lastal_version")
    lastal_output = run_version([str(lastal_bin), "-V"])
    if lastal_output != f"lastal {lastal_version}":
        raise PlanError(
            f"LAST version mismatch; expected 'lastal {lastal_version}': "
            f"{lastal_output!r}"
        )

    output_dir = namespace["candidate_output_dir"]
    assert isinstance(output_dir, Path)
    if output_dir.exists():
        raise PlanError(f"candidate output directory already exists: {output_dir}")


def validate_plan(
    plan_path: Path, *, preflight: bool = False, allow_dirty: bool = False
) -> tuple[str, str]:
    plan = load_plan(plan_path)
    require_keys(plan, TOP_LEVEL_KEYS, "measurement plan")
    if plan["schema_version"] != SCHEMA_VERSION:
        raise PlanError(f"schema_version must be {SCHEMA_VERSION!r}")
    plan_id = require_string(plan["plan_id"], "plan_id")
    if not PLAN_ID_PATTERN.fullmatch(plan_id):
        raise PlanError("plan_id contains unsupported characters or length")
    created_utc = require_string(plan["created_utc"], "created_utc")
    try:
        datetime.strptime(created_utc, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise PlanError("created_utc must use YYYY-MM-DDTHH:MM:SSZ") from exc
    commit = require_string(plan["repository_commit"], "repository_commit")
    if not COMMIT_PATTERN.fullmatch(commit):
        raise PlanError("repository_commit must be a full lowercase Git commit")

    validate_dataset(plan["dataset"])
    runtime = validate_runtime(plan["runtime"])
    namespace = validate_namespace(plan["namespace"])
    validate_budgets(plan["performance_budgets"])
    validate_analysis(plan["analysis"])
    validate_adjudication(plan["adjudication"], runtime)
    if preflight:
        validate_preflight(plan, runtime, namespace, allow_dirty)
    return plan_id, sha256_file(plan_path)


def main() -> int:
    args = parse_args()
    if args.allow_dirty and not args.preflight:
        raise PlanError("--allow-dirty requires --preflight")
    plan_id, plan_sha256 = validate_plan(
        args.plan, preflight=args.preflight, allow_dirty=args.allow_dirty
    )
    scope = "preflight passed" if args.preflight else "plan valid"
    print(f"OK: FAST shadow measurement {scope}")
    print(f"plan_id={plan_id}")
    print(f"plan_sha256={plan_sha256}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PlanError) as exc:
        raise SystemExit(f"ERROR: {exc}")
