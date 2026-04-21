#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path


def _parse_embedded_json(html_text: str, marker: str):
    pattern = re.escape(marker) + r"\s*(\{.*?\});"
    match = re.search(pattern, html_text, re.S)
    if not match:
        raise ValueError(f"missing_marker:{marker.strip()}")
    return json.loads(match.group(1))


def _load_history(path: Path):
    rounds = []
    for idx, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            raise ValueError(f"malformed_history_line:{idx}")
        if not isinstance(obj, dict):
            raise ValueError(f"non_object_history_line:{idx}")
        rounds.append(obj)
    return rounds


def _sort_rounds(rounds):
    def sort_key(item):
        ts = item.get("timestamp_utc")
        if isinstance(ts, str) and ts:
            try:
                parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return (0, parsed.timestamp(), item.get("round_barcode", ""), item.get("barcode", ""))
            except Exception:
                pass
        return (1, float("inf"), item.get("round_barcode", ""), item.get("barcode", ""))

    return sorted(rounds, key=sort_key)


def main():
    ap = argparse.ArgumentParser(description="Validate per-run RTBioScan report publication consistency")
    ap.add_argument("--report-html", required=True)
    ap.add_argument("--report-state", required=True)
    ap.add_argument("--history", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--expected-report-view", default="", help="Optional expected payload/meta report_view value")
    args = ap.parse_args()

    report_html = Path(args.report_html)
    report_state = Path(args.report_state)
    history_path = Path(args.history)

    problems = []
    if not report_html.is_file():
        problems.append(f"missing_report_html:{report_html}")
    if not report_state.is_file():
        problems.append(f"missing_report_state:{report_state}")
    if not history_path.is_file():
        problems.append(f"missing_history:{history_path}")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1

    html_text = report_html.read_text(encoding="utf-8")
    try:
        report_meta = _parse_embedded_json(html_text, "window.REPORT_META =")
        payload = _parse_embedded_json(html_text, "window.REPORT_PAYLOAD =")
    except Exception as exc:
        print(f"invalid_report_html:{exc}", file=sys.stderr)
        return 1

    try:
        state = json.loads(report_state.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"invalid_report_state:{exc}", file=sys.stderr)
        return 1

    history_revision = hashlib.sha256(history_path.read_bytes()).hexdigest()
    meta_revision = report_meta.get("report_revision")
    state_revision = state.get("report_revision")
    if meta_revision != state_revision:
        problems.append(f"report_revision_mismatch:html={meta_revision}:state={state_revision}")
    if meta_revision != history_revision:
        problems.append(f"report_revision_history_mismatch:html={meta_revision}:history={history_revision}")
    if state_revision != history_revision:
        problems.append(f"report_state_history_mismatch:state={state_revision}:history={history_revision}")

    try:
        history_rounds = _load_history(history_path)
    except Exception as exc:
        problems.append(str(exc))
        history_rounds = []

    filtered_history = [row for row in history_rounds if row.get("run_id") == args.run_id]
    sorted_history = _sort_rounds(filtered_history)
    payload_rounds = payload.get("rounds")
    if not isinstance(payload_rounds, list):
        problems.append("missing_payload_rounds")
        payload_rounds = []

    if payload.get("view_scope") != "run":
        problems.append(f"unexpected_view_scope:{payload.get('view_scope')}")
    expected_view = args.expected_report_view.strip()
    if expected_view:
        payload_view = payload.get("report_view")
        meta_view = report_meta.get("report_view")
        if payload_view != expected_view:
            problems.append(f"unexpected_payload_report_view:{payload_view}")
        if meta_view != expected_view:
            problems.append(f"unexpected_meta_report_view:{meta_view}")
    if payload.get("history_count") != len(sorted_history):
        problems.append(
            f"history_count_mismatch:payload={payload.get('history_count')}:history={len(sorted_history)}"
        )

    payload_round_barcodes = [row.get("round_barcode") for row in payload_rounds if isinstance(row, dict)]
    history_round_barcodes = [row.get("round_barcode") for row in sorted_history]
    if payload_round_barcodes != history_round_barcodes:
        problems.append(
            "round_barcodes_mismatch:"
            f"payload={','.join(str(x) for x in payload_round_barcodes)}:"
            f"history={','.join(str(x) for x in history_round_barcodes)}"
        )

    latest_payload = payload_round_barcodes[-1] if payload_round_barcodes else ""
    latest_history = history_round_barcodes[-1] if history_round_barcodes else ""
    if latest_payload != latest_history:
        problems.append(f"latest_round_mismatch:payload={latest_payload}:history={latest_history}")

    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
