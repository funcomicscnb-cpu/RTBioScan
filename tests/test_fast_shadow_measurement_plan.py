import hashlib
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "bin" / "validate_fast_shadow_measurement_plan.py"
TEMPLATE = (
    REPO_ROOT
    / "docs"
    / "internal"
    / "taxonomy_phase3"
    / "fast_shadow_measurement_plan.template.json"
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_file(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def build_valid_plan(tmp_path):
    dataset = write_file(tmp_path / "representative.pod5", "representative reads\n")
    composition = write_file(
        tmp_path / "composition.tsv",
        "class\tevidence\ncoi_target\tcontrol\nits2_target\tcontrol\noff_target\tcontrol\n",
    )
    nextflow = write_file(
        tmp_path / "nextflow",
        "#!/bin/sh\necho 'version 22.10.8 build 5859'\n",
    )
    nextflow.chmod(0o755)
    lastal = write_file(
        tmp_path / "lastal",
        "#!/bin/sh\necho 'lastal 1542'\n",
    )
    lastal.chmod(0o755)
    last_prj = write_file(tmp_path / "filter.prj", "version=1542\n")
    reference_manifest = write_file(
        tmp_path / "reference-manifest.tsv", "path\tsha256\trole\n"
    )
    dorado_manifest = write_file(
        tmp_path / "dorado-manifest.tsv",
        "# release_id=dorado-test\n"
        "# platform=osx-arm64\n"
        "# expected_version=0.7.0+test\n"
        "kind\tartifact\tsha256\tbytes\tmode\trole\n",
    )
    qualification = write_file(
        tmp_path / "qualification.tsv",
        "field\tvalue\n"
        "release_id\tdorado-test\n"
        "platform\tosx-arm64\n"
        "dorado_version\t0.7.0+test\n"
        "device\tmetal\n"
        "device_class\taccelerator\n"
        "qualification_scope\taccelerator_candidate\n"
        "status\taccelerator_compatibility_passed\n",
    )
    coi_manifest = write_file(tmp_path / "coi-reference.tsv", "COI reference\n")
    its2_manifest = write_file(tmp_path / "its2-reference.tsv", "ITS2 reference\n")
    coi_protocol = write_file(tmp_path / "coi-protocol.md", "COI protocol\n")
    its2_protocol = write_file(tmp_path / "its2-protocol.md", "ITS2 protocol\n")
    error_protocol = write_file(
        tmp_path / "error-stratification.md", "Error stratification protocol\n"
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    plan = {
        "schema_version": "fast-filter-shadow-measurement-plan-v1",
        "plan_id": "representative-shadow-v1",
        "created_utc": "2026-07-31T10:00:00Z",
        "repository_commit": commit,
        "dataset": {
            "representativeness_rationale": (
                "Deployment-matched controls and off-target material across normal rounds."
            ),
            "artifacts": [
                {
                    "artifact_id": "representative-pod5",
                    "path": str(dataset),
                    "sha256": sha256(dataset),
                    "bytes": dataset.stat().st_size,
                    "material_classes": [
                        "coi_target",
                        "its2_target",
                        "off_target",
                    ],
                    "evidence_path": str(composition),
                    "evidence_sha256": sha256(composition),
                }
            ],
        },
        "runtime": {
            "nextflow_bin": str(nextflow),
            "nextflow_bin_sha256": sha256(nextflow),
            "nextflow_version": "22.10.8",
            "lastal_bin": str(lastal),
            "lastal_bin_sha256": sha256(lastal),
            "lastal_version": "1542",
            "last_index_prj": str(last_prj),
            "last_index_prj_sha256": sha256(last_prj),
            "reference_manifest": str(reference_manifest),
            "reference_manifest_sha256": sha256(reference_manifest),
            "dorado_release_manifest": str(dorado_manifest),
            "dorado_release_manifest_sha256": sha256(dorado_manifest),
            "dorado_qualification_report": str(qualification),
            "dorado_qualification_report_sha256": sha256(qualification),
            "hardware_backend": "metal",
            "hardware_identity": "test-accelerator",
        },
        "namespace": {
            "stable_state_id": "stable-production",
            "candidate_state_id": "shadow-measurement-v1",
            "candidate_output_dir": str(tmp_path / "candidate-output"),
            "fast_filter_shadow": True,
            "taxonomy_behavior_change": False,
        },
        "performance_budgets": {
            "minimum_reads_per_second": {
                "fast": 100,
                "hac": 50,
                "sup": 10,
            },
            "minimum_bases_per_second": {
                "fast": 100000,
                "hac": 50000,
                "sup": 10000,
            },
            "maximum_p95_round_latency_seconds": 300,
            "maximum_total_basecalling_time_ratio": 1.1,
            "minimum_sequencer_input_headroom_ratio": 1.2,
        },
        "analysis": {
            "read_length_bins": [0, 250, 500, 1000, 2000],
            "alignment_identity_bins": [0, 80, 90, 95, 100],
            "error_stratification_protocol_path": str(error_protocol),
            "error_stratification_protocol_sha256": sha256(error_protocol),
            "near_tie_policy": "retain",
        },
        "adjudication": {
            "sampling_strategy": (
                "All disagreements when <=100; otherwise seeded sampling per bucket."
            ),
            "sample_per_marker_status": 25,
            "random_seed": 314159,
            "references": [
                {
                    "marker": "COI",
                    "name": "independent-coi",
                    "release": "v1",
                    "reference_manifest_path": str(coi_manifest),
                    "reference_manifest_sha256": sha256(coi_manifest),
                    "search_tool": "blastn",
                    "search_tool_version": "test",
                    "protocol_path": str(coi_protocol),
                    "protocol_sha256": sha256(coi_protocol),
                    "independent_from_routing_database": True,
                },
                {
                    "marker": "ITS2",
                    "name": "independent-its2",
                    "release": "v1",
                    "reference_manifest_path": str(its2_manifest),
                    "reference_manifest_sha256": sha256(its2_manifest),
                    "search_tool": "blastn",
                    "search_tool_version": "test",
                    "protocol_path": str(its2_protocol),
                    "protocol_sha256": sha256(its2_protocol),
                    "independent_from_routing_database": True,
                },
            ],
        },
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan_path, plan


def run_validator(plan_path, *extra_args):
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--plan",
            str(plan_path),
            *extra_args,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def rewrite_plan(plan_path, plan):
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")


def test_valid_plan_and_preflight_pass(tmp_path):
    plan_path, _ = build_valid_plan(tmp_path)

    validated = run_validator(plan_path)
    assert validated.returncode == 0, validated.stderr
    assert "FAST shadow measurement plan valid" in validated.stdout
    assert "plan_id=representative-shadow-v1" in validated.stdout
    assert f"plan_sha256={sha256(plan_path)}" in validated.stdout

    preflight = run_validator(plan_path, "--preflight", "--allow-dirty")
    assert preflight.returncode == 0, preflight.stderr
    assert "FAST shadow measurement preflight passed" in preflight.stdout


def test_template_is_invalid_until_every_choice_is_predeclared():
    completed = run_validator(TEMPLATE)
    assert completed.returncode != 0
    assert "unresolved placeholder" in completed.stderr


def test_rejects_incomplete_dataset_material_coverage(tmp_path):
    plan_path, plan = build_valid_plan(tmp_path)
    plan["dataset"]["artifacts"][0]["material_classes"] = [
        "coi_target",
        "off_target",
    ]
    rewrite_plan(plan_path, plan)

    completed = run_validator(plan_path)
    assert completed.returncode != 0
    assert "dataset material coverage must include exactly" in completed.stderr


def test_rejects_tampered_content_addressed_input(tmp_path):
    plan_path, plan = build_valid_plan(tmp_path)
    Path(plan["dataset"]["artifacts"][0]["path"]).write_text(
        "tampered reads\n", encoding="utf-8"
    )

    completed = run_validator(plan_path)
    assert completed.returncode != 0
    assert "SHA-256 mismatch" in completed.stderr


def test_rejects_behavior_change_or_near_tie_exclusion(tmp_path):
    plan_path, plan = build_valid_plan(tmp_path)
    plan["namespace"]["taxonomy_behavior_change"] = True
    rewrite_plan(plan_path, plan)
    changed = run_validator(plan_path)
    assert changed.returncode != 0
    assert "taxonomy_behavior_change must be false" in changed.stderr

    plan["namespace"]["taxonomy_behavior_change"] = False
    plan["analysis"]["near_tie_policy"] = "exclude"
    rewrite_plan(plan_path, plan)
    excluded = run_validator(plan_path)
    assert excluded.returncode != 0
    assert "near_tie_policy must remain retain" in excluded.stderr


def test_rejects_failed_accelerator_qualification(tmp_path):
    plan_path, plan = build_valid_plan(tmp_path)
    report_path = Path(plan["runtime"]["dorado_qualification_report"])
    report_path.write_text(
        report_path.read_text(encoding="utf-8").replace(
            "accelerator_compatibility_passed", "failed"
        ),
        encoding="utf-8",
    )
    plan["runtime"]["dorado_qualification_report_sha256"] = sha256(report_path)
    rewrite_plan(plan_path, plan)

    completed = run_validator(plan_path)
    assert completed.returncode != 0
    assert "qualification report status is 'failed'" in completed.stderr


def test_rejects_last_index_runtime_version_mismatch(tmp_path):
    plan_path, plan = build_valid_plan(tmp_path)
    plan["runtime"]["lastal_version"] = "1543"
    rewrite_plan(plan_path, plan)

    completed = run_validator(plan_path)
    assert completed.returncode != 0
    assert "LAST .prj version mismatch: '1542' != '1543'" in completed.stderr


def test_rejects_routing_manifest_as_adjudication_reference(tmp_path):
    plan_path, plan = build_valid_plan(tmp_path)
    reference = plan["adjudication"]["references"][0]
    reference["reference_manifest_path"] = plan["runtime"]["reference_manifest"]
    reference["reference_manifest_sha256"] = plan["runtime"][
        "reference_manifest_sha256"
    ]
    rewrite_plan(plan_path, plan)

    completed = run_validator(plan_path)
    assert completed.returncode != 0
    assert "reuses the routing manifest" in completed.stderr


def test_preflight_rejects_existing_output_namespace(tmp_path):
    plan_path, plan = build_valid_plan(tmp_path)
    Path(plan["namespace"]["candidate_output_dir"]).mkdir()

    completed = run_validator(plan_path, "--preflight", "--allow-dirty")
    assert completed.returncode != 0
    assert "candidate output directory already exists" in completed.stderr
