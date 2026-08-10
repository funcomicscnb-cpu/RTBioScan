#!/usr/bin/env python3
"""Validate and aggregate decision-neutral FAST shadow diagnostics."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


DETAIL_FIELDS = [
    "round_barcode",
    "read_id",
    "read_length",
    "current_subject",
    "current_label",
    "current_score",
    "current_retained",
    "best_target_subject",
    "best_target_label",
    "best_target_score",
    "best_target_pident",
    "best_target_aln_length",
    "best_offtarget_subject",
    "best_offtarget_label",
    "best_offtarget_score",
    "best_offtarget_pident",
    "best_offtarget_aln_length",
    "target_minus_offtarget_margin",
    "competition_status",
]
SUMMARY_FIELDS = ["round_barcode", "metric", "reads", "bases"]
COMPETITION_STATUSES = [
    "target_only",
    "offtarget_only",
    "target_leads",
    "offtarget_leads",
    "tie",
    "no_alignment",
]
DECISIONS = ["current_retained", "current_excluded"]
SUMMARY_METRICS = [
    "all_input",
    "aligned",
    "unaligned",
    *DECISIONS,
    *COMPETITION_STATUSES,
    *(
        f"{decision}__{status}"
        for decision in DECISIONS
        for status in COMPETITION_STATUSES
    ),
]


class MeasurementError(ValueError):
    pass


@dataclass
class RoundMeasurement:
    round_barcode: str
    detail_path: Path
    summary_path: Path
    detail_sha256: str
    summary_sha256: str
    metrics: dict[str, tuple[int, int]]
    buckets: dict[tuple[str, str, str], dict[str, object]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        type=Path,
        help="detail TSV or directory recursively containing shadow detail TSVs",
    )
    parser.add_argument(
        "--output-prefix",
        required=True,
        type=Path,
        help="prefix for _aggregate.tsv, _rounds.tsv, _inputs.tsv, and .json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace existing measurement outputs",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)


def discover_detail_files(inputs: list[Path]) -> list[Path]:
    discovered: dict[Path, None] = {}
    for input_path in inputs:
        if input_path.is_dir():
            candidates = input_path.rglob("*_fast_filter_shadow.tsv")
        elif input_path.is_file():
            candidates = [input_path]
        else:
            raise MeasurementError(f"input does not exist: {input_path}")
        for candidate in candidates:
            if not candidate.name.endswith("_fast_filter_shadow.tsv"):
                raise MeasurementError(
                    f"detail input must end with _fast_filter_shadow.tsv: {candidate}"
                )
            discovered[candidate.resolve()] = None
    if not discovered:
        raise MeasurementError("no FAST shadow detail TSVs were discovered")
    return sorted(discovered)


def paired_summary_path(detail_path: Path) -> Path:
    suffix = "_fast_filter_shadow.tsv"
    return detail_path.with_name(
        detail_path.name[: -len(suffix)] + "_fast_filter_shadow_summary.tsv"
    )


def read_rows(path: Path, expected_fields: list[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != expected_fields:
            raise MeasurementError(
                f"unexpected schema in {path}: {reader.fieldnames!r}"
            )
        return list(reader)


def nonnegative_int(text: str, field: str, path: Path) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise MeasurementError(f"invalid {field} in {path}: {text!r}") from exc
    if value < 0:
        raise MeasurementError(f"negative {field} in {path}: {value}")
    return value


def decimal_value(text: str, field: str, path: Path) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise MeasurementError(f"invalid {field} in {path}: {text!r}") from exc
    if not value.is_finite():
        raise MeasurementError(f"non-finite {field} in {path}: {text!r}")
    return value


def alignment_present(
    row: dict[str, str], prefix: str, path: Path
) -> tuple[bool, Decimal | None]:
    fields = [f"{prefix}_subject", f"{prefix}_label", f"{prefix}_score"]
    if prefix != "current":
        fields.extend([f"{prefix}_pident", f"{prefix}_aln_length"])
    values = [row[field] for field in fields]
    presence = [bool(value) for value in values]
    if len(set(presence)) != 1:
        raise MeasurementError(
            f"partial {prefix} alignment for {row['read_id']} in {path}"
        )
    score_text = row[f"{prefix}_score"]
    score = decimal_value(score_text, f"{prefix}_score", path) if score_text else None
    return presence[0], score


def marker_from_label(label: str) -> str:
    return label.split("|", 1)[0] if label else "unassigned"


def increment(
    metrics: dict[str, list[int]], metric: str, read_length: int
) -> None:
    metrics[metric][0] += 1
    metrics[metric][1] += read_length


def validate_detail(
    path: Path,
) -> tuple[
    str | None,
    dict[str, tuple[int, int]],
    dict[tuple[str, str, str], dict[str, object]],
]:
    rows = read_rows(path, DETAIL_FIELDS)
    round_values = {row["round_barcode"] for row in rows}
    if "" in round_values or len(round_values) > 1:
        raise MeasurementError(f"detail TSV must contain one non-empty round: {path}")
    round_barcode = next(iter(round_values)) if round_values else None
    seen_reads: set[str] = set()
    metrics: dict[str, list[int]] = {
        metric: [0, 0] for metric in SUMMARY_METRICS
    }
    buckets: dict[tuple[str, str, str], dict[str, object]] = defaultdict(
        lambda: {"reads": 0, "bases": 0, "margins": []}
    )

    for row in rows:
        read_id = row["read_id"]
        if not read_id or read_id in seen_reads:
            raise MeasurementError(f"empty or duplicate read_id in {path}: {read_id!r}")
        seen_reads.add(read_id)
        read_length = nonnegative_int(row["read_length"], "read_length", path)
        if row["current_retained"] not in {"0", "1"}:
            raise MeasurementError(
                f"invalid current_retained for {read_id} in {path}: "
                f"{row['current_retained']!r}"
            )
        status = row["competition_status"]
        if status not in COMPETITION_STATUSES:
            raise MeasurementError(
                f"invalid competition_status for {read_id} in {path}: {status!r}"
            )

        current_present, _ = alignment_present(row, "current", path)
        target_present, target_score = alignment_present(row, "best_target", path)
        offtarget_present, offtarget_score = alignment_present(
            row, "best_offtarget", path
        )
        if not current_present and row["current_retained"] != "0":
            raise MeasurementError(f"unaligned retained read in {path}: {read_id}")

        expected_presence = {
            "target_only": (True, False),
            "offtarget_only": (False, True),
            "target_leads": (True, True),
            "offtarget_leads": (True, True),
            "tie": (True, True),
            "no_alignment": (False, False),
        }[status]
        if (target_present, offtarget_present) != expected_presence:
            raise MeasurementError(
                f"alignment/status mismatch for {read_id} in {path}: {status}"
            )
        if status == "no_alignment" and current_present:
            raise MeasurementError(f"no_alignment read has a current hit in {path}: {read_id}")

        margin_text = row["target_minus_offtarget_margin"]
        margin: Decimal | None = None
        if target_present and offtarget_present:
            if not margin_text:
                raise MeasurementError(f"missing competition margin in {path}: {read_id}")
            assert target_score is not None and offtarget_score is not None
            margin = decimal_value(margin_text, "competition margin", path)
            expected_margin_text = f"{float(target_score) - float(offtarget_score):g}"
            decimal_value(expected_margin_text, "expected competition margin", path)
            if margin_text != expected_margin_text:
                raise MeasurementError(
                    f"incorrect competition margin for {read_id} in {path}: "
                    f"{margin_text!r} != {expected_margin_text!r}"
                )
            expected_status = (
                "target_leads"
                if margin > 0
                else "offtarget_leads"
                if margin < 0
                else "tie"
            )
            if status != expected_status:
                raise MeasurementError(
                    f"margin/status mismatch for {read_id} in {path}: {status}"
                )
        elif margin_text:
            raise MeasurementError(f"unexpected competition margin in {path}: {read_id}")

        decision = (
            "current_retained"
            if row["current_retained"] == "1"
            else "current_excluded"
        )
        increment(metrics, "all_input", read_length)
        increment(metrics, "aligned" if current_present else "unaligned", read_length)
        increment(metrics, decision, read_length)
        increment(metrics, status, read_length)
        increment(metrics, f"{decision}__{status}", read_length)

        key = (marker_from_label(row["current_label"]), decision, status)
        bucket = buckets[key]
        bucket["reads"] = int(bucket["reads"]) + 1
        bucket["bases"] = int(bucket["bases"]) + read_length
        if margin is not None:
            margins = bucket["margins"]
            assert isinstance(margins, list)
            margins.append(margin)

    frozen_metrics = {
        metric: (values[0], values[1]) for metric, values in metrics.items()
    }
    return round_barcode, frozen_metrics, dict(buckets)


def validate_summary(
    path: Path, detail_round: str | None, expected: dict[str, tuple[int, int]]
) -> str:
    rows = read_rows(path, SUMMARY_FIELDS)
    if [row["metric"] for row in rows] != SUMMARY_METRICS:
        raise MeasurementError(f"unexpected metric order or coverage in {path}")
    round_values = {row["round_barcode"] for row in rows}
    if "" in round_values or len(round_values) != 1:
        raise MeasurementError(f"summary TSV must contain one non-empty round: {path}")
    round_barcode = next(iter(round_values))
    if detail_round is not None and detail_round != round_barcode:
        raise MeasurementError(
            f"round mismatch between detail and summary in {path}: "
            f"{detail_round!r} != {round_barcode!r}"
        )
    for row in rows:
        observed = (
            nonnegative_int(row["reads"], "reads", path),
            nonnegative_int(row["bases"], "bases", path),
        )
        if observed != expected[row["metric"]]:
            raise MeasurementError(
                f"summary mismatch for {row['metric']} in {path}: "
                f"{observed} != {expected[row['metric']]}"
            )
    return round_barcode


def load_measurements(detail_paths: list[Path]) -> list[RoundMeasurement]:
    measurements: list[RoundMeasurement] = []
    seen_rounds: set[str] = set()
    for detail_path in detail_paths:
        summary_path = paired_summary_path(detail_path)
        if not summary_path.is_file():
            raise MeasurementError(f"paired summary TSV is missing: {summary_path}")
        detail_round, metrics, buckets = validate_detail(detail_path)
        round_barcode = validate_summary(summary_path, detail_round, metrics)
        if round_barcode in seen_rounds:
            raise MeasurementError(f"duplicate round_barcode across inputs: {round_barcode}")
        seen_rounds.add(round_barcode)
        measurements.append(
            RoundMeasurement(
                round_barcode=round_barcode,
                detail_path=detail_path,
                summary_path=summary_path,
                detail_sha256=sha256_file(detail_path),
                summary_sha256=sha256_file(summary_path),
                metrics=metrics,
                buckets=buckets,
            )
        )
    return sorted(measurements, key=lambda item: item.round_barcode)


def decimal_text(value: Decimal) -> str:
    text = format(value, ".6f").rstrip("0").rstrip(".")
    return text or "0"


def aggregate_buckets(
    measurements: list[RoundMeasurement],
) -> list[dict[str, object]]:
    combined: dict[tuple[str, str, str], dict[str, object]] = defaultdict(
        lambda: {"reads": 0, "bases": 0, "margins": []}
    )
    for measurement in measurements:
        for key, values in measurement.buckets.items():
            destination = combined[key]
            destination["reads"] = int(destination["reads"]) + int(values["reads"])
            destination["bases"] = int(destination["bases"]) + int(values["bases"])
            destination_margins = destination["margins"]
            source_margins = values["margins"]
            assert isinstance(destination_margins, list)
            assert isinstance(source_margins, list)
            destination_margins.extend(source_margins)

    rows: list[dict[str, object]] = []
    for (marker, decision, status), values in sorted(combined.items()):
        margins = values["margins"]
        assert isinstance(margins, list)
        rows.append(
            {
                "current_marker": marker,
                "current_decision": decision,
                "competition_status": status,
                "reads": values["reads"],
                "bases": values["bases"],
                "margin_observations": len(margins),
                "margin_min": decimal_text(min(margins)) if margins else "",
                "margin_max": decimal_text(max(margins)) if margins else "",
                "margin_mean": (
                    decimal_text(sum(margins, Decimal(0)) / len(margins))
                    if margins
                    else ""
                ),
            }
        )
    return rows


def tsv_text(fields: list[str], rows: list[dict[str, object]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=fields, delimiter="\t", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def output_paths(prefix: Path) -> dict[str, Path]:
    return {
        "aggregate": prefix.with_name(prefix.name + "_aggregate.tsv"),
        "rounds": prefix.with_name(prefix.name + "_rounds.tsv"),
        "inputs": prefix.with_name(prefix.name + "_inputs.tsv"),
        "json": prefix.with_name(prefix.name + ".json"),
    }


def build_outputs(
    measurements: list[RoundMeasurement], paths: dict[str, Path]
) -> dict[Path, str]:
    aggregate_rows = aggregate_buckets(measurements)
    round_rows: list[dict[str, object]] = []
    input_rows: list[dict[str, object]] = []
    total_metrics = {metric: [0, 0] for metric in SUMMARY_METRICS}
    for measurement in measurements:
        for metric in SUMMARY_METRICS:
            reads, bases = measurement.metrics[metric]
            total_metrics[metric][0] += reads
            total_metrics[metric][1] += bases
            round_rows.append(
                {
                    "round_barcode": measurement.round_barcode,
                    "metric": metric,
                    "reads": reads,
                    "bases": bases,
                }
            )
        input_rows.append(
            {
                "round_barcode": measurement.round_barcode,
                "detail_path": display_path(measurement.detail_path),
                "detail_sha256": measurement.detail_sha256,
                "summary_path": display_path(measurement.summary_path),
                "summary_sha256": measurement.summary_sha256,
                "reads": measurement.metrics["all_input"][0],
                "bases": measurement.metrics["all_input"][1],
            }
        )

    report = {
        "schema_version": "fast-filter-shadow-measurement-v1",
        "rounds": len(measurements),
        "reads": total_metrics["all_input"][0],
        "bases": total_metrics["all_input"][1],
        "metrics": {
            metric: {"reads": values[0], "bases": values[1]}
            for metric, values in total_metrics.items()
        },
        "buckets": aggregate_rows,
        "inputs": input_rows,
        "interpretation_boundary": (
            "Routing-policy evidence only; biological false-positive/negative labels "
            "require independent adjudication."
        ),
    }
    return {
        paths["aggregate"]: tsv_text(
            [
                "current_marker",
                "current_decision",
                "competition_status",
                "reads",
                "bases",
                "margin_observations",
                "margin_min",
                "margin_max",
                "margin_mean",
            ],
            aggregate_rows,
        ),
        paths["rounds"]: tsv_text(
            ["round_barcode", "metric", "reads", "bases"], round_rows
        ),
        paths["inputs"]: tsv_text(
            [
                "round_barcode",
                "detail_path",
                "detail_sha256",
                "summary_path",
                "summary_sha256",
                "reads",
                "bases",
            ],
            input_rows,
        ),
        paths["json"]: json.dumps(report, indent=2, sort_keys=True) + "\n",
    }


def main() -> int:
    args = parse_args()
    paths = output_paths(args.output_prefix)
    existing = [path for path in paths.values() if path.exists()]
    if existing and not args.force:
        raise MeasurementError(
            "measurement output already exists (use --force): "
            + ", ".join(str(path) for path in existing)
        )
    measurements = load_measurements(discover_detail_files(args.input))
    rendered = build_outputs(measurements, paths)
    for path, text in rendered.items():
        atomic_write(path, text)
    for name in ("aggregate", "rounds", "inputs", "json"):
        print(f"OK: wrote {paths[name]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MeasurementError, OSError) as exc:
        raise SystemExit(f"ERROR: {exc}")
