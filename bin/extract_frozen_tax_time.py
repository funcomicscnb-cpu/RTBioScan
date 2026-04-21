#!/usr/bin/env python3
"""Extract frozen OTU taxonomy-over-time data from a round JSON and append to a cumulative report.

Usage:
    extract_frozen_tax_time.py --json <round_report.json> --run-id <barcode> --out <rpt.txt>

Output format (same as otu_tax_time_rpt.txt):
    run_id  time    taxon   identifications
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def iso_to_epoch(ts):
    """Parse ISO-8601 UTC timestamp to Unix epoch integer."""
    try:
        dt = datetime.strptime(ts.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def extract_frozen_counts(assignments_by_level):
    """Return (species_set, genus_set, family_set, reads_by_rank_marker) for frozen OTUs."""
    species_set = set()
    genus_set = set()
    family_set = set()
    reads_by_rank_marker = {}  # (rank, marker) -> total reads

    for level in ("species", "genus", "family"):
        rows = assignments_by_level.get(level, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            frozen_count = row.get("frozen_otu_count", 0)
            if not isinstance(frozen_count, (int, float)) or frozen_count <= 0:
                continue
            taxon = row.get("taxon") or row.get(level) or ""
            marker = (row.get("marker") or "").upper()
            reads = row.get("reads_total")
            reads = int(reads) if isinstance(reads, (int, float)) else 0

            if level == "species" and taxon:
                species_set.add(taxon)
                # Also populate genus/family from species-level rows
                genus = row.get("genus") or ""
                family = row.get("family") or ""
                if genus:
                    genus_set.add(genus)
                if family:
                    family_set.add(family)
            elif level == "genus" and taxon:
                genus_set.add(taxon)
                family = row.get("family") or ""
                if family:
                    family_set.add(family)
            elif level == "family" and taxon:
                family_set.add(taxon)

            if marker and reads > 0:
                key = (level, marker)
                reads_by_rank_marker[key] = reads_by_rank_marker.get(key, 0) + reads

    return species_set, genus_set, family_set, reads_by_rank_marker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="Path to round_report.json")
    ap.add_argument("--run-id", required=True, help="Barcode / run identifier")
    ap.add_argument("--out", required=True, help="Output cumulative report file")
    args = ap.parse_args()

    json_path = Path(args.json)
    if not json_path.is_file():
        print(f"ERROR: JSON not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: Cannot parse JSON {json_path}: {e}", file=sys.stderr)
        sys.exit(1)

    ts = data.get("timestamp_utc") or ""
    epoch = iso_to_epoch(ts)
    if epoch is None:
        print(f"ERROR: Cannot parse timestamp_utc '{ts}' in {json_path}", file=sys.stderr)
        sys.exit(1)

    otu = data.get("otu")
    if not isinstance(otu, dict):
        print("INFO: No 'otu' section in JSON; skipping frozen tax time output", file=sys.stderr)
        sys.exit(0)

    abl = otu.get("assignments_by_level")
    if not isinstance(abl, dict):
        print("INFO: No assignments_by_level; skipping", file=sys.stderr)
        sys.exit(0)

    sp_set, gn_set, fm_set, reads_rm = extract_frozen_counts(abl)

    run_id = args.run_id
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = "run_id\ttime\ttaxon\tidentifications"
    write_header = not out_path.exists()

    marker_order = []
    markers_meta = data.get("markers")
    if isinstance(markers_meta, dict) and isinstance(markers_meta.get("order"), list):
        marker_order = [str(marker).strip().upper() for marker in markers_meta["order"] if str(marker).strip()]
    observed_markers = sorted({marker for (_, marker) in reads_rm.keys() if marker and marker not in marker_order})
    marker_order.extend(observed_markers)

    lines = [
        f"{run_id}\t{epoch}\tspecies\t{len(sp_set)}",
        f"{run_id}\t{epoch}\tgenus\t{len(gn_set)}",
        f"{run_id}\t{epoch}\tfamily\t{len(fm_set)}",
    ]
    for rank in ("species", "genus", "family"):
        for marker in marker_order:
            lines.append(f"{run_id}\t{epoch}\t{rank}_{marker}_reads\t{reads_rm.get((rank, marker), 0)}")

    mode = "w" if write_header else "a"
    with out_path.open(mode, encoding="utf-8") as fh:
        if write_header:
            fh.write(header + "\n")
        for line in lines:
            fh.write(line + "\n")

    print(f"INFO: appended frozen tax time rows to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
