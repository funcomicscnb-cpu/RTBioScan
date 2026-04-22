#!/usr/bin/env python3
import argparse
import copy
import datetime as dt
import gzip
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PAGE_SIZE = 10
ICICLE_FAMILY_PALETTE = [
    "#4c956c", "#2c6e49", "#1d5c3a", "#1d4e89", "#3a7fad", "#6baed6",
    "#8b4513", "#c44b2b", "#e07b39", "#d4a017", "#9a7d0a", "#6b4226",
    "#7b2d8b", "#b5508a", "#c78b5e", "#3f6f3f", "#558b6e", "#7d6e83",
    "#5d7c6e", "#9b7653",
]
_MARKER_DEFAULT_COLORS = {"COI": "#2c6e49", "ITS2": "#1d4e89"}
MARKER_ROOT_COLORS = _MARKER_DEFAULT_COLORS  # backward-compat alias
_marker_color_cache = {}
RENDER_SIGNATURE_VERSION = "sample-assets-v1"
RENDER_EMBEDDED_SIGNATURE_VERSION = "embedded-v4"
RENDER_FIGURES_SIGNATURE_VERSION = "figures-v1"
DEFAULT_REPORT_ASSETS_DIR_NAME = "report_assets"
DEFAULT_FIGURES_DIR_NAME = "figures"
CURRENT_REPORT_ASSETS_DIR_NAME = DEFAULT_REPORT_ASSETS_DIR_NAME
CURRENT_FIGURES_DIR_NAME = DEFAULT_FIGURES_DIR_NAME
CURRENT_GROUP_VIEW = "sample"


def marker_color(marker):
    if marker not in _marker_color_cache:
        default = _MARKER_DEFAULT_COLORS.get(marker)
        if default:
            _marker_color_cache[marker] = default
        else:
            idx = len(_marker_color_cache)
            _marker_color_cache[marker] = ICICLE_FAMILY_PALETTE[idx % len(ICICLE_FAMILY_PALETTE)]
    return _marker_color_cache[marker]


def canonical_marker_token(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if upper in ("ITS", "ITS1", "ITS2"):
        return "ITS2"
    return upper


def marker_slug(marker):
    slug = canonical_marker_token(marker).lower()
    slug = re.sub(r"[^a-z0-9._-]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug


def _iter_marker_rows(data):
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def marker_order_from_data(data):
    rows = _iter_marker_rows(data)
    ordered = []
    seen = set()

    def add(marker):
        canonical = canonical_marker_token(marker)
        if not canonical or canonical in seen or canonical == "OTHER":
            return
        seen.add(canonical)
        ordered.append(canonical)

    for row in rows:
        markers = row.get("markers")
        if isinstance(markers, dict):
            for marker in markers.get("order") or []:
                add(marker)

    for row in rows:
        read_fate = row.get("read_fate") if isinstance(row.get("read_fate"), dict) else {}
        marker_counts = read_fate.get("marker_counts") if isinstance(read_fate.get("marker_counts"), dict) else {}
        for bucket in marker_counts.values():
            if isinstance(bucket, dict):
                for marker in bucket.keys():
                    add(marker)
        sample_metrics = row.get("sample_metrics") if isinstance(row.get("sample_metrics"), dict) else {}
        for sample in sample_metrics.values():
            if isinstance(sample, dict):
                demux_map = sample.get("reads_demux_by_marker")
                if isinstance(demux_map, dict):
                    for marker in demux_map.keys():
                        add(marker)
        for source_key, sub_key in (("otu", "active_by_marker_taxon"), ("consensus", "emitted_by_marker_taxon")):
            source = row.get(source_key) if isinstance(row.get(source_key), dict) else {}
            bucket = source.get(sub_key) if isinstance(source.get(sub_key), dict) else {}
            for nested_key in ("assigned", "unassigned"):
                nested = bucket.get(nested_key)
                if isinstance(nested, dict):
                    for marker in nested.keys():
                        add(marker)
            assignments = source.get("assignments_by_level") if isinstance(source.get("assignments_by_level"), dict) else {}
            for level_rows in assignments.values():
                if not isinstance(level_rows, list):
                    continue
                for item in level_rows:
                    if isinstance(item, dict):
                        add(item.get("marker"))

    if not ordered:
        ordered = ["COI", "ITS2"]
    return ordered


def marker_colors_from_data(data):
    colors = {}
    rows = _iter_marker_rows(data)
    for row in rows:
        markers = row.get("markers")
        if isinstance(markers, dict) and isinstance(markers.get("color_by_marker"), dict):
            for marker, color in markers["color_by_marker"].items():
                canonical = canonical_marker_token(marker)
                if canonical and isinstance(color, str) and color.strip():
                    colors[canonical] = color
    for marker in marker_order_from_data(data):
        colors.setdefault(marker, marker_color(marker))
    return colors


def _lighten_hex(color, factor=0.4):
    c = color.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def build_fate_style(markers, marker_colors=None):
    """Return (order, colors) for the public read fate stacked bar chart."""
    marker_colors = marker_colors or {}
    order = []
    colors = {}
    for marker in markers:
        base = marker_colors.get(marker, marker_color(marker))
        for prefix, shade in (
            ("BLAST-assigned", base),
            ("BLAST-unassigned", _lighten_hex(base, 0.45)),
            ("BLAST skipped", _lighten_hex(base, 0.7)),
        ):
            label = f"{prefix} {marker}"
            order.append(label)
            colors[label] = shade
    colors["On-target not demultiplexed"] = "#f4a261"
    colors["Off-target"] = "#e76f51"
    order.extend(["On-target not demultiplexed", "Off-target"])
    return order, colors


READ_FATE_ORDER, READ_FATE_COLORS = build_fate_style(["COI", "ITS2"])

ROUND_WORD_ORDER = {
    "zero": 0,
    "zeroth": 0,
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
}


def load_history(path: Path):
    rounds = []
    warnings = []
    if not path.exists() or path.stat().st_size == 0:
        return rounds, warnings
    for idx, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            warnings.append(f"malformed_history_line:{idx}")
            continue
        if not isinstance(obj, dict):
            warnings.append(f"non_object_history_line:{idx}")
            continue
        missing = [k for k in ("run_id", "barcode", "round_barcode") if not obj.get(k)]
        if missing:
            warnings.append(f"missing_required_keys:{idx}:{','.join(missing)}")
            continue
        rounds.append(obj)
    return rounds, warnings


def load_run_index(path: Path):
    runs = []
    warnings = []
    if not path.exists() or path.stat().st_size == 0:
        return runs, warnings
    for idx, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            warnings.append(f"malformed_run_index_line:{idx}")
            continue
        if not isinstance(obj, dict):
            warnings.append(f"non_object_run_index_line:{idx}")
            continue
        if not obj.get("run_id"):
            warnings.append(f"missing_run_id:{idx}")
            continue
        runs.append(obj)
    return runs, warnings


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def atomic_write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def compute_signature(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_signature(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def report_assets_dir(out_path: Path) -> Path:
    return out_path.parent / CURRENT_REPORT_ASSETS_DIR_NAME


def figures_dir(out_path: Path) -> Path:
    return out_path.parent / CURRENT_FIGURES_DIR_NAME


def report_assets_rel_path(run_id: str, *parts: str) -> str:
    suffix = "/".join(str(part).strip("/") for part in parts if str(part).strip("/"))
    base = CURRENT_REPORT_ASSETS_DIR_NAME
    if run_id:
        return f"runs/{run_id}/{base}" + (f"/{suffix}" if suffix else "")
    return base + (f"/{suffix}" if suffix else "")


def sample_signature_path(out_path: Path, run_id: str, safe_sample_id: str, base_name: str) -> Path:
    return report_assets_dir(out_path) / ".private_signatures" / "samples" / safe_sample_id / f"{base_name}.sig"


def should_regenerate_sample_asset(signature_path: Path, artifact_paths, signature: str) -> bool:
    if not signature_path.exists() or any(not p.exists() for p in artifact_paths):
        return True
    return read_signature(signature_path) != signature


def sort_rounds(rounds):
    def sort_key(item):
        round_barcode = item.get("round_barcode", "")
        natural_key = natural_round_key(round_barcode)
        if natural_key:
            return (0, natural_key, item.get("barcode", ""))
        ts = item.get("timestamp_utc")
        if isinstance(ts, str) and ts:
            try:
                norm = ts.replace("Z", "+00:00")
                parsed = dt.datetime.fromisoformat(norm)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return (1, parsed.timestamp(), round_barcode, item.get("barcode", ""))
            except Exception:
                pass
        return (2, float("inf"), round_barcode, item.get("barcode", ""))

    return sorted(rounds, key=sort_key)


def sort_runs(runs):
    def sort_key(item):
        ts = item.get("last_updated_utc") or item.get("last_round_timestamp_utc")
        if isinstance(ts, str) and ts:
            try:
                norm = ts.replace("Z", "+00:00")
                parsed = dt.datetime.fromisoformat(norm)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return (0, -parsed.timestamp(), item.get("run_id", ""))
            except Exception:
                pass
        return (1, float("inf"), item.get("run_id", ""))

    return sorted(runs, key=sort_key)


def natural_round_key(value):
    text = str(value or "")
    parts = re.split(r"([0-9]+)", text.lower())
    key = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
            continue
        subparts = re.split(r"([^a-z]+)", part)
        for subpart in subparts:
            if not subpart:
                continue
            if subpart in ROUND_WORD_ORDER:
                key.append((0, ROUND_WORD_ORDER[subpart]))
            else:
                key.append((1, subpart))
    return tuple(key)


def prefix_url(value: str, url_prefix: str) -> str:
    if not value or not url_prefix:
        return value
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
        return value
    lower = value.lower()
    if value.startswith("/") or value.startswith("//"):
        return value
    if lower.startswith("http://") or lower.startswith("https://"):
        return value
    return url_prefix + value


def relativize_run_url(value: str, run_id: str) -> str:
    if not value or not run_id:
        return value
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
        return value
    if value.startswith("/") or value.startswith("//"):
        return value
    run_prefix = f"runs/{run_id}/"
    if value.startswith(run_prefix):
        return value[len(run_prefix):]
    return value


def num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else 0


def num_any(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if value == value else 0
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            try:
                parsed = float(trimmed)
                return parsed if parsed == parsed else 0
            except Exception:
                return 0
    return 0


def clamp(value, min_value, max_value):
    x = value if isinstance(value, (int, float)) and value == value else 0
    if x < min_value:
        return min_value
    if x > max_value:
        return max_value
    return x


def has_value(value):
    return value is not None


def get_path(obj, path, fallback=None):
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return fallback
        cur = cur[key]
    return fallback if cur is None else cur


def iter_figures(rows):
    for row in rows:
        if not isinstance(row, dict):
            continue
        for fig in row.get("figures", []):
            if isinstance(fig, dict):
                yield fig
        sample_metrics = row.get("sample_metrics")
        if isinstance(sample_metrics, dict):
            for sample_entry in sample_metrics.values():
                if not isinstance(sample_entry, dict):
                    continue
                for fig in sample_entry.get("figures", []):
                    if isinstance(fig, dict):
                        yield fig


def map_figure_paths(rows, run_id, url_prefix):
    for fig in iter_figures(rows):
        for key in ("path", "pdf_path"):
            if not isinstance(fig.get(key), str):
                continue
            if run_id and not url_prefix:
                fig[key] = relativize_run_url(fig[key], run_id)
            elif url_prefix:
                fig[key] = prefix_url(fig[key], url_prefix)


def resolve_asset_fs_path(asset_path, out_path, run_id):
    if not isinstance(asset_path, str) or not asset_path:
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", asset_path):
        return None
    if asset_path.startswith("/") or asset_path.startswith("//"):
        return None
    if asset_path.startswith("runs/"):
        results_root = out_path.parents[2] if run_id and len(out_path.parents) >= 3 else out_path.parent
        return results_root / asset_path
    return out_path.parent / asset_path


def ensure_pdf_from_png(png_path, pdf_path):
    if not png_path.exists():
        return pdf_path.exists()
    if pdf_path.exists():
        try:
            if pdf_path.stat().st_mtime >= png_path.stat().st_mtime:
                return True
        except Exception:
            return True
    try:
        plt = _import_matplotlib()
    except Exception:
        return pdf_path.exists()
    image = plt.imread(png_path)
    height = image.shape[0] if hasattr(image, "shape") and len(image.shape) >= 1 else 1200
    width = image.shape[1] if hasattr(image, "shape") and len(image.shape) >= 2 else 1600
    fig_w = max(4.0, float(width) / 200.0)
    fig_h = max(3.0, float(height) / 200.0)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.imshow(image)
    ax.axis("off")
    fig.subplots_adjust(0, 0, 1, 1)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(pdf_path, format="pdf", bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    return pdf_path.exists()


def normalize_png_background(png_path):
    if not png_path.exists():
        return False
    try:
        from PIL import Image
    except Exception:
        return False
    try:
        img = Image.open(png_path).convert("RGBA")
    except Exception:
        return False
    alpha = img.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if alpha_min == alpha_max == 255:
        return False
    background = Image.new("RGBA", img.size, (255, 255, 255, 255))
    composited = Image.alpha_composite(background, img).convert("RGB")
    composited.save(png_path)
    return True


def backfill_figure_pdfs(rows, out_path, run_id):
    for fig in iter_figures(rows):
        path = fig.get("path")
        if not isinstance(path, str) or not path.lower().endswith(".png"):
            continue
        png_fs_path = resolve_asset_fs_path(path, out_path, run_id)
        if png_fs_path is None:
            continue
        if "circle_tree" in str(fig.get("id") or ""):
            try:
                normalize_png_background(png_fs_path)
            except Exception:
                pass
        pdf_path = fig.get("pdf_path")
        if not isinstance(pdf_path, str) or not pdf_path:
            pdf_path = re.sub(r"\.png$", ".pdf", path, flags=re.IGNORECASE)
            fig["pdf_path"] = pdf_path
        pdf_fs_path = resolve_asset_fs_path(pdf_path, out_path, run_id)
        if pdf_fs_path is None:
            continue
        try:
            fig["pdf_exists"] = bool(ensure_pdf_from_png(png_fs_path, pdf_fs_path))
        except Exception:
            fig["pdf_exists"] = bool(pdf_fs_path.exists())


def normalize_sample_base(raw):
    s = str(raw or "").strip()
    if not s:
        return ""
    if re.match(r"^no_adapter(?:_\d+)?$", s, re.IGNORECASE):
        return "no_adapter"
    s = re.sub(r"_\d+$", "", s)
    s = re.sub(r"_(?:[A-Z][A-Z0-9]{1,})$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"_\d+_", "_", s)
    return s.strip()


def sample_group_label(raw_label, collapse_track_units=False):
    label = str(raw_label or "").strip()
    if not collapse_track_units:
        return label
    return normalize_sample_base(label)


def track_sample_replicate_label(raw):
    if not isinstance(raw, dict):
        return ""
    direct = str(raw.get("track_sample_replicate_label") or "").strip()
    if direct:
        return direct
    sample_label = str(raw.get("track_sample_label") or "").strip()
    repnum = raw.get("track_replicate_number")
    repnum_text = str(repnum).strip() if repnum not in (None, "") else ""
    if sample_label and repnum_text:
        return f"{sample_label}_{repnum_text}"
    for candidate in (
        raw.get("track_replicate_label"),
        raw.get("track_replicate_id"),
        raw.get("label"),
        raw.get("sample"),
        raw.get("track_unit_id"),
    ):
        text = str(candidate or "").strip()
        if not text:
            continue
        match = re.match(r"^(.*?_\d+)_[^_]+$", text)
        if match:
            return match.group(1)
        return text
    return ""


def parse_track_replicate_label(raw):
    text = str(raw or "").strip()
    if not text:
        return {}
    match = re.match(r"^(.*)_([0-9]+)_([^_]+)$", text)
    if not match:
        return {}
    sample_label = str(match.group(1) or "").strip()
    if not sample_label:
        return {}
    return {
        "sample_label": sample_label,
        "replicate_number": match.group(2),
        "plate_label": match.group(3),
        "replicate_suffix": f"{match.group(2)}_{match.group(3)}",
        "replicate_label": text,
    }


def parse_track_unit_label(raw):
    text = str(raw or "").strip()
    if not text:
        return {}
    match = re.match(r"^(.*)_([0-9]+)_([^_]+)_([A-Za-z0-9]+)$", text)
    if not match:
        return {}
    marker = canonical_marker_token(match.group(4))
    sample_label = str(match.group(1) or "").strip()
    if not marker or not sample_label:
        return {}
    return {
        "sample_label": sample_label,
        "replicate_number": match.group(2),
        "plate_label": match.group(3),
        "replicate_suffix": f"{match.group(2)}_{match.group(3)}",
        "unit_label": f"{sample_label}_{match.group(2)}_{match.group(3)}",
        "unit_id": text,
        "marker": marker,
    }


def track_sample_label_value(raw):
    if isinstance(raw, dict):
        direct = str(raw.get("track_sample_label") or "").strip()
        if direct:
            return direct
        for candidate in (
            raw.get("track_unit_id"),
            raw.get("sample"),
            raw.get("label"),
            raw.get("track_replicate_id"),
            raw.get("track_replicate_label"),
        ):
            parsed = parse_track_unit_label(candidate)
            if parsed:
                return parsed["sample_label"]
            parsed = parse_track_replicate_label(candidate)
            if parsed:
                return parsed["sample_label"]
        return ""
    parsed = parse_track_unit_label(raw)
    if parsed:
        return parsed["sample_label"]
    parsed = parse_track_replicate_label(raw)
    if parsed:
        return parsed["sample_label"]
    return ""


def concise_track_replicate_label(raw):
    if not isinstance(raw, dict):
        return ""
    sample_label = track_sample_label_value(raw)
    for candidate in (
        raw.get("track_replicate_id"),
        raw.get("track_replicate_label"),
        raw.get("track_unit_id"),
        raw.get("label"),
        raw.get("sample"),
    ):
        text = str(candidate or "").strip()
        if not text:
            continue
        if sample_label and text.startswith(f"{sample_label}_"):
            return text[len(sample_label) + 1:]
        parsed = parse_track_replicate_label(text)
        if parsed:
            return parsed["replicate_suffix"]
        parsed = parse_track_unit_label(text)
        if parsed:
            return parsed["replicate_suffix"]
        return text
    return ""


def sample_row_matches(sample_value, sample_label, match_mode="exact"):
    raw_sample = str(sample_value or "").strip()
    raw_label = str(sample_label or "").strip()
    if match_mode == "track_sample":
        return track_sample_label_value(raw_sample) == raw_label
    if match_mode == "normalized":
        return normalize_sample_base(raw_sample) == normalize_sample_base(raw_label)
    return raw_sample == raw_label


def is_supported_otu_assignment_row(row):
    return num_any(get_path(row, ["reads_total"], None)) >= 5


def marker_from_filename(name):
    m = re.search(r"-([A-Za-z0-9_]+)\.consensus\.fasta$", str(name or ""), re.IGNORECASE)
    if not m:
        return ""
    return canonical_marker_token(m.group(1))


def format_taxonomy_assignment(parts):
    cleaned = []
    for raw in parts:
        value = str(raw or "").strip()
        if not value or value.upper() == "NA":
            continue
        if value in cleaned:
            continue
        cleaned.append(value)
    if not cleaned:
        return ""
    non_unassigned = [value for value in cleaned if value.lower() != "unassigned"]
    if non_unassigned:
        return " > ".join(non_unassigned)
    return "Unassigned"


def collect_consensus_sequence_rows(run_id, out_path, state_id=None):
    if not run_id:
        return []
    # Detect results_root by finding "report_html" in the path components and going up to its parent.
    # out_path for run-specific reports is {results}/report_html/runs/{run_id}/report.html,
    # so parents[3] == results_root, not parents[2]. Use component-based detection for robustness.
    _parts = out_path.parts
    _rhtml_idx = next((i for i, p in enumerate(_parts) if p == "report_html"), None)
    if _rhtml_idx is not None and _rhtml_idx > 0:
        results_root = Path(*_parts[:_rhtml_idx])
    elif len(out_path.parents) >= 4:
        results_root = out_path.parents[3]
    elif len(out_path.parents) >= 3:
        results_root = out_path.parents[2]
    else:
        results_root = out_path.parent
    state_key = state_id or run_id
    # The ongoing state dir is updated by the consensus process BEFORE the report renders.
    # The snapshot dirs (current/state, temp/current/state) are only populated by
    # backup_update_and_clean, which runs AFTER the report — so they always lag one round.
    ongoing_state_root = results_root / "temp" / "ongoing" / "state" / (state_id or run_id)
    live_round_root = results_root / "current" / "state" / state_key / "live_round"
    state_roots = [
        results_root / "current" / "state" / state_key,
        results_root / "temp" / "current" / "state" / state_key,
    ]
    # Ongoing state: Consensus/ is directly under the state root (no "sequences/" prefix).
    # Snapshot dirs: sequences/Consensus/ with the extra prefix.
    consensus_dirs = [live_round_root / "sequences" / "Consensus", ongoing_state_root / "Consensus"] + [
        state_root / "sequences" / "Consensus" for state_root in state_roots
    ]
    base_dir = next((path for path in consensus_dirs if path.exists() and path.is_dir()), None)
    if base_dir is None:
        return []

    consolidated_ids = set()
    for consolidated_ids_path in [path / "consolidated_consensus_ids.txt" for path in consensus_dirs]:
        if not consolidated_ids_path.exists():
            continue
        try:
            for raw_line in consolidated_ids_path.read_text(encoding="utf-8").splitlines():
                value = str(raw_line or "").strip()
                if value:
                    consolidated_ids.add(value)
        except Exception:
            continue

    def load_best_otu_assignments():
        by_otu = {}
        # Ongoing state stores tables under _state/; snapshots use tables/.
        report_paths = [
            live_round_root / "tables" / "RTBioScan_blast_otu_pretax_rpt.txt",
            live_round_root / "tables" / "RTBioScan_blast_otu_noadapter_rpt.txt",
            ongoing_state_root / "_state" / "RTBioScan_blast_otu_pretax_rpt.txt",
            ongoing_state_root / "_state" / "RTBioScan_blast_otu_noadapter_rpt.txt",
        ] + [
            state_root / "tables" / "RTBioScan_blast_otu_pretax_rpt.txt" for state_root in state_roots
        ] + [
            state_root / "tables" / "RTBioScan_blast_otu_noadapter_rpt.txt" for state_root in state_roots
        ]
        for path in report_paths:
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            if not lines:
                continue
            header = lines[0].split("\t")
            idx = {name: pos for pos, name in enumerate(header)}
            otu_idx = idx.get("otu_id")
            if otu_idx is None:
                continue
            family_idx = idx.get("otu_family")
            genus_idx = idx.get("otu_genus")
            species_idx = idx.get("otu_species")
            order_idx = idx.get("otu_order")
            class_idx = idx.get("otu_class")
            phylum_idx = idx.get("otu_phylum")
            kingdom_idx = idx.get("otu_kingdom")
            perc_idx = idx.get("perc_id")
            for line in lines[1:]:
                if not line.strip():
                    continue
                fields = line.split("\t")
                if otu_idx >= len(fields):
                    continue
                otu_key = fields[otu_idx].strip()
                if not otu_key:
                    continue
                assignment = format_taxonomy_assignment(
                    [
                        fields[kingdom_idx] if kingdom_idx is not None and kingdom_idx < len(fields) else "",
                        fields[phylum_idx] if phylum_idx is not None and phylum_idx < len(fields) else "",
                        fields[class_idx] if class_idx is not None and class_idx < len(fields) else "",
                        fields[order_idx] if order_idx is not None and order_idx < len(fields) else "",
                        fields[family_idx] if family_idx is not None and family_idx < len(fields) else "",
                        fields[genus_idx] if genus_idx is not None and genus_idx < len(fields) else "",
                        fields[species_idx] if species_idx is not None and species_idx < len(fields) else "",
                    ]
                )
                score = 0
                if assignment:
                    depth = max(0, assignment.count(" > "))
                    score = depth * 1000
                if perc_idx is not None and perc_idx < len(fields):
                    try:
                        score += int(round(float(fields[perc_idx]) * 10.0))
                    except Exception:
                        pass
                prev = by_otu.get(otu_key)
                if prev is None or score > prev["score"]:
                    by_otu[otu_key] = {"assignment": assignment, "score": score}
        return {otu_key: info["assignment"] for otu_key, info in by_otu.items()}

    def load_consensus_assignments():
        by_consensus_id = {}
        report_paths = [
            live_round_root / "tables" / "RTBioScan_blast_consensus_tax_rpt.txt",
            live_round_root / "tables" / "RTBioScan_blast_consensus_tax_representative_rpt.txt",
            live_round_root / "tables" / "RTBioScan_blast_consensus_tax_consolidated_rpt.txt",
            ongoing_state_root / "_state" / "RTBioScan_blast_consensus_tax_rpt.txt",
            ongoing_state_root / "_state" / "RTBioScan_blast_consensus_tax_representative_rpt.txt",
            ongoing_state_root / "_state" / "RTBioScan_blast_consensus_tax_consolidated_rpt.txt",
        ] + [
            state_root / "tables" / "RTBioScan_blast_consensus_tax_rpt.txt" for state_root in state_roots
        ] + [
            state_root / "tables" / "RTBioScan_blast_consensus_tax_representative_rpt.txt" for state_root in state_roots
        ] + [
            state_root / "tables" / "RTBioScan_blast_consensus_tax_consolidated_rpt.txt" for state_root in state_roots
        ]
        for path in report_paths:
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            if not lines:
                continue
            header = lines[0].split("\t")
            idx = {name: pos for pos, name in enumerate(header)}
            cons_idx = idx.get("consensus_id")
            sample_idx = idx.get("sample")
            marker_idx = idx.get("barcode_by_homology")
            reads_idx = idx.get("number_of_reads")
            if cons_idx is None or sample_idx is None or marker_idx is None or reads_idx is None:
                continue
            family_idx = idx.get("consensus_family")
            genus_idx = idx.get("consensus_genus")
            species_idx = idx.get("consensus_species")
            order_idx = idx.get("consensus_order")
            class_idx = idx.get("consensus_class")
            phylum_idx = idx.get("consensus_phylum")
            kingdom_idx = idx.get("consensus_kingdom")
            for line in lines[1:]:
                if not line.strip():
                    continue
                fields = line.split("\t")
                if cons_idx >= len(fields) or sample_idx >= len(fields) or marker_idx >= len(fields) or reads_idx >= len(fields):
                    continue
                consensus_id = fields[cons_idx].strip()
                sample = fields[sample_idx].strip()
                marker = fields[marker_idx].strip()
                reads_used = fields[reads_idx].strip()
                if not consensus_id or not sample or not marker or not reads_used:
                    continue
                assignment = format_taxonomy_assignment(
                    [
                        fields[kingdom_idx] if kingdom_idx is not None and kingdom_idx < len(fields) else "",
                        fields[phylum_idx] if phylum_idx is not None and phylum_idx < len(fields) else "",
                        fields[class_idx] if class_idx is not None and class_idx < len(fields) else "",
                        fields[order_idx] if order_idx is not None and order_idx < len(fields) else "",
                        fields[family_idx] if family_idx is not None and family_idx < len(fields) else "",
                        fields[genus_idx] if genus_idx is not None and genus_idx < len(fields) else "",
                        fields[species_idx] if species_idx is not None and species_idx < len(fields) else "",
                    ]
                )
                if assignment:
                    by_consensus_id[consensus_id] = {
                        "assignment": assignment,
                        "sample": sample,
                        "marker": marker,
                        "reads_used": reads_used,
                    }
        return by_consensus_id

    otu_assignments = load_best_otu_assignments()
    consensus_assignments = load_consensus_assignments()
    rows = []
    for fasta_path in sorted(base_dir.rglob("*_Merged_Consensus.fasta")):
        try:
            raw_lines = fasta_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        header = ""
        seq_parts = []

        def flush_record(current_header, current_parts):
            row_list = []
            if not current_header:
                return row_list
            sequence = "".join(current_parts).strip()
            if not sequence:
                return row_list
            header_parts = current_header.split("|")
            sample = header_parts[0].strip() if header_parts and header_parts[0].strip() else fasta_path.parent.name
            local_consensus_id = header_parts[1].strip() if len(header_parts) >= 2 and header_parts[1].strip() else fasta_path.stem
            marker = header_parts[2].strip() if len(header_parts) >= 3 and header_parts[2].strip() else marker_from_filename(fasta_path.name)
            reads_used = ""
            otu_key = ""
            for token in header_parts[3:]:
                token = token.strip()
                m = re.search(r"reads-(\d+)", token, re.IGNORECASE)
                if m and not reads_used:
                    reads_used = m.group(1)
                m = re.match(r"OTU=(.+)$", token, re.IGNORECASE)
                if m and not otu_key:
                    otu_key = m.group(1).strip()
            consensus_id = f"{local_consensus_id}_{sample}" if local_consensus_id and sample else local_consensus_id
            current = consensus_assignments.get(consensus_id) or {}
            consensus_assignment = current.get("assignment") or "Unassigned"
            otu_assignment = otu_assignments.get(otu_key, "Unassigned" if otu_key else "")
            is_consolidated = (
                consensus_id in consolidated_ids
                or current_header in consolidated_ids
                or any(str(token or "").strip().lower() == "consolidated=1" for token in header_parts[3:])
            )
            row_list.append(
                {
                    "sample": current.get("sample") or sample,
                    "marker": current.get("marker") or marker,
                    "consensus_id": consensus_id,
                    "otu_key": otu_key,
                    "otu_assignment": otu_assignment,
                    "consensus_assignment": consensus_assignment,
                    "reads_used": current.get("reads_used") or reads_used,
                    "length": len(sequence),
                    "header": current_header,
                    "sequence": sequence,
                    "is_consolidated": is_consolidated,
                }
            )
            return row_list

        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                rows.extend(flush_record(header, seq_parts))
                header = line[1:].strip()
                seq_parts = []
                continue
            seq_parts.append(line)
        rows.extend(flush_record(header, seq_parts))
    rows.sort(key=lambda row: (natural_round_key(row.get("sample", "")), natural_round_key(row.get("marker", "")), natural_round_key(row.get("consensus_id", ""))))
    return rows


def sanitize_filename(value):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return safe or "sample"


def blend_hex(color, target="#ffffff", factor=0.0):
    def parse(hex_color):
        hex_color = str(hex_color or "").strip().lstrip("#")
        if len(hex_color) == 3:
            hex_color = "".join(ch * 2 for ch in hex_color)
        if len(hex_color) != 6:
            return (153, 163, 155)
        try:
            return tuple(int(hex_color[idx:idx + 2], 16) for idx in (0, 2, 4))
        except Exception:
            return (153, 163, 155)

    base_rgb = parse(color)
    target_rgb = parse(target)
    alpha = clamp(factor, 0.0, 1.0)
    mixed = tuple(int(round(base * (1.0 - alpha) + dest * alpha)) for base, dest in zip(base_rgb, target_rgb))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def iter_sample_assignment_rows(round_obj, sample_label, source_key, level, marker=None, include_row=None, match_mode="exact"):
    source = round_obj.get(source_key) if isinstance(round_obj, dict) else None
    assignments = source.get("assignments_by_level") if isinstance(source, dict) else None
    rows = assignments.get(level) if isinstance(assignments, dict) else None
    if not isinstance(rows, list):
        return []
    marker_name = canonical_marker_token(marker) if marker else ""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if match_mode == "track_unit":
            if str(row.get("track_unit_id") or row.get("sample") or "").strip() != str(sample_label or "").strip():
                continue
        elif match_mode == "track_sample_replicate":
            if track_sample_replicate_label(row) != str(sample_label or "").strip():
                continue
        elif not sample_row_matches(row.get("sample"), sample_label, match_mode=match_mode):
            continue
        row_marker = canonical_marker_token(row.get("marker"))
        if marker_name and row_marker != marker_name:
            continue
        if callable(include_row) and not include_row(row):
            continue
        out.append(row)
    return out


def assignment_sunburst_highlight_spec(source_key):
    if source_key == "otu":
        return {
            "field": "frozen_otu_count",
            "label": "frozen OTUs",
            "tone": "frozen",
            "text_color": "#5b2a86",
        }
    if source_key == "consensus":
        return {
            "field": "consolidated_consensus_count",
            "label": "consolidated consensus sequences",
            "tone": "consolidated",
            "text_color": "#7a5a0a",
        }
    return {"field": "", "label": "", "tone": "", "text_color": "#20311c"}


def build_sample_taxonomy_tree(round_obj, sample_label, source_key, marker, include_row=None, match_mode="exact"):
    marker_name = canonical_marker_token(marker)
    highlight_spec = assignment_sunburst_highlight_spec(source_key)
    highlight_field = str(highlight_spec.get("field") or "")
    root = {"name": marker_name, "depth": 0, "leaf_value": 0.0, "leaf_special_value": 0.0, "children": {}}
    if not isinstance(round_obj, dict):
        return {
            "name": marker_name,
            "depth": 0,
            "leaf_value": 0.0,
            "special_value": 0.0,
            "value": 0.0,
            "highlight": False,
            "highlight_label": str(highlight_spec.get("label") or ""),
            "highlight_tone": str(highlight_spec.get("tone") or ""),
            "highlight_color": str(highlight_spec.get("text_color") or "#20311c"),
            "children": [],
        }

    def add_total(bucket, key, reads_total):
        bucket[key] = float(bucket.get(key, 0.0)) + reads_total

    def ensure_family(family_name):
        return root["children"].setdefault(
            family_name,
            {"name": family_name, "depth": 1, "leaf_value": 0.0, "leaf_special_value": 0.0, "children": {}},
        )

    def ensure_genus(family_node, genus_name):
        return family_node["children"].setdefault(
            genus_name,
            {"name": genus_name, "depth": 2, "leaf_value": 0.0, "leaf_special_value": 0.0, "children": {}},
        )

    family_totals = {}
    genus_totals = {}
    species_totals = {}
    family_special_totals = {}
    genus_special_totals = {}
    species_special_totals = {}

    for level in ("family", "genus", "species"):
        for row in iter_sample_assignment_rows(
            round_obj,
            sample_label,
            source_key,
            level,
            marker_name,
            include_row=include_row,
            match_mode=match_mode,
        ):
            reads_total = max(0.0, float(num_any(row.get("reads_total"))))
            if reads_total <= 0:
                continue
            highlight_total = max(0.0, float(num_any(row.get(highlight_field)))) if highlight_field else 0.0
            family = str(row.get("family") or "").strip() or "Unassigned family"
            genus = str(row.get("genus") or "").strip() or "Unassigned genus"
            species = str(row.get("species") or "").strip() or "Unassigned species"
            if level == "family":
                add_total(family_totals, family, reads_total)
                if highlight_field:
                    add_total(family_special_totals, family, highlight_total)
                continue
            if level == "genus":
                add_total(genus_totals, (family, genus), reads_total)
                if highlight_field:
                    add_total(genus_special_totals, (family, genus), highlight_total)
                continue
            add_total(species_totals, (family, genus, species), reads_total)
            if highlight_field:
                add_total(species_special_totals, (family, genus, species), highlight_total)

    for (family, genus, species), reads_total in species_totals.items():
        family_node = ensure_family(family)
        genus_node = ensure_genus(family_node, genus)
        species_node = genus_node["children"].setdefault(
            species,
            {"name": species, "depth": 3, "leaf_value": 0.0, "leaf_special_value": 0.0, "children": {}},
        )
        species_node["leaf_value"] += reads_total
        species_node["leaf_special_value"] += float(species_special_totals.get((family, genus, species), 0.0))

    covered_by_genus = {}
    covered_by_genus_special = {}
    for (family, genus, _species), reads_total in species_totals.items():
        add_total(covered_by_genus, (family, genus), reads_total)
        if highlight_field:
            add_total(covered_by_genus_special, (family, genus), float(species_special_totals.get((family, genus, _species), 0.0)))
    for (family, genus), reads_total in genus_totals.items():
        family_node = ensure_family(family)
        genus_node = ensure_genus(family_node, genus)
        genus_node["leaf_value"] += max(0.0, reads_total - float(covered_by_genus.get((family, genus), 0.0)))
        if highlight_field:
            genus_node["leaf_special_value"] += max(
                0.0,
                float(genus_special_totals.get((family, genus), 0.0)) - float(covered_by_genus_special.get((family, genus), 0.0)),
            )

    covered_by_family = {}
    covered_by_family_special = {}
    for (family, _genus), reads_total in genus_totals.items():
        add_total(covered_by_family, family, reads_total)
        if highlight_field:
            add_total(covered_by_family_special, family, float(genus_special_totals.get((family, _genus), 0.0)))
    for family, reads_total in family_totals.items():
        family_node = ensure_family(family)
        family_node["leaf_value"] += max(0.0, reads_total - float(covered_by_family.get(family, 0.0)))
        if highlight_field:
            family_node["leaf_special_value"] += max(
                0.0,
                float(family_special_totals.get(family, 0.0)) - float(covered_by_family_special.get(family, 0.0)),
            )

    def finalize(node):
        children = [finalize(child) for child in node["children"].values()]
        children.sort(key=lambda item: (-item["value"], item["name"]))
        total = float(node["leaf_value"]) + sum(child["value"] for child in children)
        special_total = float(node.get("leaf_special_value", 0.0)) + sum(float(child.get("special_value", 0.0)) for child in children)
        return {
            "name": node["name"],
            "depth": node["depth"],
            "leaf_value": float(node["leaf_value"]),
            "special_value": special_total,
            "value": total,
            "highlight": special_total > 0.0,
            "children": children,
        }

    out = finalize(root)
    out["highlight_label"] = str(highlight_spec.get("label") or "")
    out["highlight_tone"] = str(highlight_spec.get("tone") or "")
    out["highlight_color"] = str(highlight_spec.get("text_color") or "#20311c")
    return out


def iter_run_assignment_rows(round_obj, source_key, level, marker=None, include_row=None):
    """Like iter_sample_assignment_rows but without the sample filter (run-level aggregate)."""
    source = round_obj.get(source_key) if isinstance(round_obj, dict) else None
    assignments = source.get("assignments_by_level") if isinstance(source, dict) else None
    rows = assignments.get(level) if isinstance(assignments, dict) else None
    if not isinstance(rows, list):
        return []
    marker_name = canonical_marker_token(marker) if marker else ""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_marker = canonical_marker_token(row.get("marker"))
        if marker_name and row_marker != marker_name:
            continue
        if callable(include_row) and not include_row(row):
            continue
        out.append(row)
    return out


def build_run_taxonomy_tree(round_obj, source_key, marker, include_row=None):
    """Build taxonomy tree aggregated across all samples (no sample filter)."""
    marker_name = canonical_marker_token(marker)
    highlight_spec = assignment_sunburst_highlight_spec(source_key)
    highlight_field = str(highlight_spec.get("field") or "")
    root = {"name": marker_name, "depth": 0, "leaf_value": 0.0, "leaf_special_value": 0.0, "children": {}}
    if not isinstance(round_obj, dict):
        return {
            "name": marker_name,
            "depth": 0,
            "leaf_value": 0.0,
            "special_value": 0.0,
            "value": 0.0,
            "highlight": False,
            "highlight_label": str(highlight_spec.get("label") or ""),
            "highlight_tone": str(highlight_spec.get("tone") or ""),
            "highlight_color": str(highlight_spec.get("text_color") or "#20311c"),
            "children": [],
        }

    def add_total(bucket, key, reads_total):
        bucket[key] = float(bucket.get(key, 0.0)) + reads_total

    def ensure_family(family_name):
        return root["children"].setdefault(
            family_name,
            {"name": family_name, "depth": 1, "leaf_value": 0.0, "leaf_special_value": 0.0, "children": {}},
        )

    def ensure_genus(family_node, genus_name):
        return family_node["children"].setdefault(
            genus_name,
            {"name": genus_name, "depth": 2, "leaf_value": 0.0, "leaf_special_value": 0.0, "children": {}},
        )

    family_totals, genus_totals, species_totals = {}, {}, {}
    family_special_totals, genus_special_totals, species_special_totals = {}, {}, {}

    for level in ("family", "genus", "species"):
        for row in iter_run_assignment_rows(round_obj, source_key, level, marker_name, include_row=include_row):
            reads_total = max(0.0, float(num_any(row.get("reads_total"))))
            if reads_total <= 0:
                continue
            highlight_total = max(0.0, float(num_any(row.get(highlight_field)))) if highlight_field else 0.0
            family = str(row.get("family") or "").strip() or "Unassigned family"
            genus = str(row.get("genus") or "").strip() or "Unassigned genus"
            species = str(row.get("species") or "").strip() or "Unassigned species"
            if level == "family":
                add_total(family_totals, family, reads_total)
                if highlight_field:
                    add_total(family_special_totals, family, highlight_total)
            elif level == "genus":
                add_total(genus_totals, (family, genus), reads_total)
                if highlight_field:
                    add_total(genus_special_totals, (family, genus), highlight_total)
            else:
                add_total(species_totals, (family, genus, species), reads_total)
                if highlight_field:
                    add_total(species_special_totals, (family, genus, species), highlight_total)

    for (family, genus, species), rt in species_totals.items():
        genus_node = ensure_genus(ensure_family(family), genus)
        species_node = genus_node["children"].setdefault(
            species,
            {"name": species, "depth": 3, "leaf_value": 0.0, "leaf_special_value": 0.0, "children": {}},
        )
        species_node["leaf_value"] += rt
        species_node["leaf_special_value"] += float(species_special_totals.get((family, genus, species), 0.0))

    covered_by_genus = {}
    covered_by_genus_special = {}
    for (family, genus, _sp), rt in species_totals.items():
        add_total(covered_by_genus, (family, genus), rt)
        if highlight_field:
            add_total(covered_by_genus_special, (family, genus), float(species_special_totals.get((family, genus, _sp), 0.0)))
    for (family, genus), rt in genus_totals.items():
        genus_node = ensure_genus(ensure_family(family), genus)
        genus_node["leaf_value"] += max(0.0, rt - float(covered_by_genus.get((family, genus), 0.0)))
        if highlight_field:
            genus_node["leaf_special_value"] += max(
                0.0,
                float(genus_special_totals.get((family, genus), 0.0)) - float(covered_by_genus_special.get((family, genus), 0.0)),
            )

    covered_by_family = {}
    covered_by_family_special = {}
    for (family, _gn), rt in genus_totals.items():
        add_total(covered_by_family, family, rt)
        if highlight_field:
            add_total(covered_by_family_special, family, float(genus_special_totals.get((family, _gn), 0.0)))
    for family, rt in family_totals.items():
        family_node = ensure_family(family)
        family_node["leaf_value"] += max(0.0, rt - float(covered_by_family.get(family, 0.0)))
        if highlight_field:
            family_node["leaf_special_value"] += max(
                0.0,
                float(family_special_totals.get(family, 0.0)) - float(covered_by_family_special.get(family, 0.0)),
            )

    def finalize(node):
        children = [finalize(child) for child in node["children"].values()]
        children.sort(key=lambda item: (-item["value"], item["name"]))
        total = float(node["leaf_value"]) + sum(child["value"] for child in children)
        special_total = float(node.get("leaf_special_value", 0.0)) + sum(float(child.get("special_value", 0.0)) for child in children)
        return {
            "name": node["name"],
            "depth": node["depth"],
            "leaf_value": float(node["leaf_value"]),
            "special_value": special_total,
            "value": total,
            "highlight": special_total > 0.0,
            "children": children,
        }

    out = finalize(root)
    out["highlight_label"] = str(highlight_spec.get("label") or "")
    out["highlight_tone"] = str(highlight_spec.get("tone") or "")
    out["highlight_color"] = str(highlight_spec.get("text_color") or "#20311c")
    return out


def find_sample_round_entry(round_obj, sample_id, sample_label):
    if isinstance(round_obj, dict) and round_obj.get("identity_mode") == "track":
        track_unit_metrics = round_obj.get("track_unit_metrics")
        if not isinstance(track_unit_metrics, dict):
            return {}
        aggregate = {
            "sample_id": sample_id or sample_label,
            "label": sample_label,
            "reads_demux": 0,
            "reads_demux_coi": 0,
            "reads_demux_its2": 0,
            "reads_demux_by_marker": {},
            "reads_blast_assigned": 0,
            "otu_active": 0,
            "consensus_emitted": 0,
        }
        matched = 0
        for raw in track_unit_metrics.values():
            if not isinstance(raw, dict):
                continue
            if track_sample_label_value(raw) != str(sample_label or "").strip():
                continue
            matched += 1
            aggregate["reads_demux"] += num_any(get_path(raw, ["reads_demux"], 0))
            aggregate["reads_demux_coi"] += num_any(get_path(raw, ["reads_demux_coi"], 0))
            aggregate["reads_demux_its2"] += num_any(get_path(raw, ["reads_demux_its2"], 0))
            aggregate["reads_blast_assigned"] += num_any(get_path(raw, ["reads_blast_assigned"], 0))
            aggregate["otu_active"] += num_any(get_path(raw, ["otu_active"], 0))
            aggregate["consensus_emitted"] += num_any(get_path(raw, ["consensus_emitted"], 0))
            source = raw.get("reads_demux_by_marker") if isinstance(raw.get("reads_demux_by_marker"), dict) else {}
            for marker, value in source.items():
                canonical = canonical_marker_token(marker)
                if not canonical:
                    continue
                aggregate["reads_demux_by_marker"][canonical] = aggregate["reads_demux_by_marker"].get(canonical, 0) + num(value)
        return aggregate if matched else {}
    sample_metrics = round_obj.get("sample_metrics") if isinstance(round_obj, dict) else None
    if not isinstance(sample_metrics, dict):
        return {}
    if sample_id and sample_id in sample_metrics and isinstance(sample_metrics[sample_id], dict):
        return sample_metrics[sample_id]
    sample_base = normalize_sample_base(sample_label)
    for raw in sample_metrics.values():
        if not isinstance(raw, dict):
            continue
        if normalize_sample_base(raw.get("label") or raw.get("sample_id")) == sample_base:
            return raw
    return {}


def collect_round_sample_model_counts(run_id, sample_label, round_barcode, out_path, identity_mode="collapse"):
    results_root = out_path.parents[2] if len(out_path.parents) >= 3 else out_path.parent
    round_dir = results_root / "temp" / "ongoing" / "state" / run_id / str(round_barcode or "")
    demult_path = round_dir / "RTBioScan_demult_rpt.txt"
    if not demult_path.exists() or not demult_path.is_file():
        gz_path = demult_path.with_suffix(demult_path.suffix + ".gz")
        demult_path = gz_path if gz_path.exists() else demult_path
    counts = {"hac": 0, "sup": 0}
    if not demult_path.exists():
        return counts
    opener = gzip.open if demult_path.suffix == ".gz" else open
    try:
        with opener(demult_path, "rt", encoding="utf-8") as fh:
            header = fh.readline().rstrip("\n")
            cols = header.split("\t")
            idx = {name: pos for pos, name in enumerate(cols)}
            sample_idx = idx.get("sample")
            model_idx = idx.get("basecalling_model")
            if sample_idx is None or model_idx is None:
                return counts
            sample_base = normalize_sample_base(sample_label)
            for line in fh:
                if not line.strip():
                    continue
                fields = line.rstrip("\n").split("\t")
                if sample_idx >= len(fields) or model_idx >= len(fields):
                    continue
                if identity_mode == "track":
                    if track_sample_label_value(fields[sample_idx]) != str(sample_label or "").strip():
                        continue
                else:
                    if normalize_sample_base(fields[sample_idx]) != sample_base:
                        continue
                model = str(fields[model_idx]).strip().lower()
                if model in counts:
                    counts[model] += 1
    except Exception:
        return counts
    return counts


def collect_sample_round_metrics(round_obj, sample_id, sample_label):
    row = find_sample_round_entry(round_obj, sample_id, sample_label)
    identity_mode = round_obj.get("identity_mode") if isinstance(round_obj, dict) else "collapse"
    metrics = {
        "reads_demux": num_any(get_path(row, ["reads_demux"], None)),
        "reads_blast_assigned": num_any(get_path(row, ["reads_blast_assigned"], None)),
        "otu_active": num_any(get_path(row, ["otu_active"], None)),
        "consensus_emitted": num_any(get_path(row, ["consensus_emitted"], None)),
    }
    for source_key in ("otu", "consensus"):
        prefix = f"{source_key}_"
        include_row = is_supported_otu_assignment_row if source_key == "otu" else None
        source_data = round_obj.get(source_key, {}) if isinstance(round_obj, dict) else {}
        all_markers = list(marker_order_from_data([round_obj])) if isinstance(round_obj, dict) else []
        discovered = set(all_markers)
        for level_rows in (source_data.get("assignments_by_level") or {}).values():
            for r in (level_rows or []):
                mk = canonical_marker_token(r.get("marker"))
                if mk and mk != "OTHER" and mk not in discovered:
                    discovered.add(mk)
                    all_markers.append(mk)
        for level in ("species", "genus", "family"):
            counts_by_marker = {m: 0 for m in all_markers}
            reads_by_marker = {m: 0.0 for m in all_markers}
            for marker in all_markers:
                rows = iter_sample_assignment_rows(
                    round_obj,
                    sample_label,
                    source_key,
                    level,
                    marker,
                    include_row=include_row,
                    match_mode=("track_sample" if identity_mode == "track" else "exact"),
                )
                taxa = set()
                reads_total = 0.0
                for item in rows:
                    taxon = str(item.get("taxon") or item.get(level) or "").strip()
                    if taxon:
                        taxa.add(taxon)
                    reads_total += max(0.0, float(num_any(item.get("reads_total"))))
                counts_by_marker[marker] = len(taxa)
                reads_by_marker[marker] = reads_total
            for marker in all_markers:
                key = marker.lower()
                metrics[f"{prefix}{level}_count_{key}"] = counts_by_marker[marker]
                metrics[f"{prefix}{level}_reads_{key}"] = reads_by_marker[marker]
            # Legacy keys for backward compatibility with R scripts
            metrics[f"{prefix}{level}_count_coi"] = counts_by_marker.get("COI", 0)
            metrics[f"{prefix}{level}_count_its2"] = counts_by_marker.get("ITS2", 0)
            metrics[f"{prefix}{level}_reads_coi"] = reads_by_marker.get("COI", 0.0)
            metrics[f"{prefix}{level}_reads_its2"] = reads_by_marker.get("ITS2", 0.0)
            metrics[f"{prefix}{level}_count_total"] = sum(counts_by_marker.values())
            metrics[f"{prefix}{level}_reads_total"] = sum(reads_by_marker.values())
    return metrics


def build_sample_history_rows(sorted_rounds, sample_id, sample_label, out_path, run_id, run_start_epoch=None):
    rows = []
    hac_cumulative = 0
    sup_cumulative = 0
    total_cumulative = 0
    for round_obj in sorted_rounds:
        round_barcode = round_obj.get("round_barcode", "-") if isinstance(round_obj, dict) else "-"
        timestamp_utc = round_obj.get("timestamp_utc") if isinstance(round_obj, dict) else None
        epoch = None
        if isinstance(timestamp_utc, str) and timestamp_utc:
            try:
                parsed = dt.datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                epoch = int(parsed.timestamp())
            except Exception:
                epoch = None
        if epoch is None:
            fallback = natural_round_key(round_barcode)
            epoch = int(fallback) if fallback is not None else 0
        model_counts = collect_round_sample_model_counts(
            run_id,
            sample_label,
            round_barcode,
            out_path,
            identity_mode=(round_obj.get("identity_mode") if isinstance(round_obj, dict) else "collapse"),
        )
        hac_round = int(model_counts.get("hac", 0))
        sup_round = int(model_counts.get("sup", 0))
        hac_cumulative += hac_round
        sup_cumulative += sup_round
        metrics = collect_sample_round_metrics(round_obj, sample_id, sample_label)
        total_cumulative += int(metrics.get("reads_demux", 0))
        row = {
            "round_barcode": round_barcode,
            "time_epoch": epoch,
            "hac_reads_round": hac_round,
            "sup_reads_round": sup_round,
            "hac_reads_cumulative": hac_cumulative,
            "sup_reads_cumulative": sup_cumulative,
            "total_reads_cumulative": total_cumulative,
        }
        row.update(metrics)
        rows.append(row)
    if run_start_epoch is not None and rows and run_start_epoch < rows[0]["time_epoch"]:
        anchor = {k: (0 if isinstance(v, (int, float)) else v) for k, v in rows[0].items()}
        anchor["round_barcode"] = "t0"
        anchor["time_epoch"] = run_start_epoch
        rows.insert(0, anchor)
    return rows


def export_sample_history_plot_png_pdf(title, subtitle, rows, lines, png_path, pdf_path, log_scale=False, split_markers=False):
    png_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    plt = _import_matplotlib()

    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if split_markers:
        markers = []
        seen_markers = set()
        for line in (lines or []):
            marker = canonical_marker_token(line.get("marker"))
            if marker and marker not in seen_markers:
                seen_markers.add(marker)
                markers.append(marker)
        if not markers:
            markers = ["COI", "ITS2"]
        n = len(markers)
        fig, axes = plt.subplots(1, n, figsize=(6.4 * n, 4.8), sharey=True)
        axes = list(axes) if n > 1 else [axes]
    else:
        fig, ax = plt.subplots(figsize=(10.6, 4.6))
        axes = [ax]
        markers = [None]
    fig.patch.set_facecolor("white")
    for ax in axes:
        ax.set_facecolor("white")

    if not rows or not any(max(0.0, float(num_any(row.get(line.get("key"))))) > 0 for row in rows for line in lines):
        for ax in axes:
            ax.text(0.5, 0.56, "No sample history available", ha="center", va="center", fontsize=13, color="#4d5b48", transform=ax.transAxes)
            ax.text(0.5, 0.43, subtitle, ha="center", va="center", fontsize=10, color="#6c7a64", transform=ax.transAxes)
            ax.axis("off")
    else:
        x = list(range(len(rows)))
        labels = [str(row.get("round_barcode") or f"R{i + 1}") for i, row in enumerate(rows)]
        for ax, marker in zip(axes, markers):
            marker_lines = [line for line in lines if marker is None or line.get("marker") == marker]
            positive_seen = False
            for line in marker_lines:
                values = [max(0.0, float(num_any(row.get(line.get("key"))))) for row in rows]
                if not any(v > 0 for v in values):
                    continue
                plot_values = [max(1.0, v) for v in values] if log_scale else values
                ax.plot(
                    x,
                    plot_values,
                    label=line.get("label", line.get("key", "")),
                    color=line.get("color", "#2c6e49"),
                    linewidth=2.1,
                    marker="o",
                    markersize=3.6,
                    linestyle=line.get("linestyle", "-"),
                )
                positive_seen = positive_seen or any(v > 0 for v in values)
            ax.set_xticks(x)
            ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=8.5, color="#3d4d37")
            ax.tick_params(axis="y", labelsize=8.5, colors="#3d4d37")
            ax.grid(True, axis="y", color="#e4eadf", linewidth=0.8)
            for spine in ("top", "right"):
                ax.spines[spine].set_visible(False)
            ax.spines["left"].set_color("#cad2bf")
            ax.spines["bottom"].set_color("#cad2bf")
            if log_scale and positive_seen:
                ax.set_yscale("log")
            if marker is not None:
                ax.set_title(marker, fontsize=10.5, color="#2a3624", pad=8)
            if marker_lines:
                ax.legend(loc="upper left", fontsize=7.8, frameon=False)

    axes[0].set_title(title, loc="left", fontsize=14, fontweight="bold", color="#2a3624", pad=12)
    fig.text(0.125, 0.92, subtitle, ha="left", va="bottom", fontsize=9.5, color="#5c6955")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(png_path, format="png", dpi=200, bbox_inches="tight")
    fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
    plt.close(fig)


def run_r_plot(script_path, input_path, expected_png, expected_pdf, dest_png, dest_pdf):
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    dest_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sample_plot_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        local_input = tmpdir_path / Path(input_path).name
        shutil.copy2(input_path, local_input)
        subprocess.run(
            ["Rscript", str(script_path), str(local_input)],
            cwd=str(tmpdir_path),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        png_src = tmpdir_path / expected_png
        pdf_src = tmpdir_path / expected_pdf
        if not png_src.exists():
            raise FileNotFoundError(str(png_src))
        shutil.copy2(png_src, dest_png)
        if pdf_src.exists():
            shutil.copy2(pdf_src, dest_pdf)
        elif dest_png.exists():
            # Safety fallback: in practice save_plot_journal() always writes a PDF alongside the PNG,
            # so pdf_src.exists() is true and the copy path above is taken.  A missing dest_pdf for a
            # history figure is recovered by a full R re-run triggered by the extended artifact check
            # in should_regenerate_sample_asset() ([png_fs, pdf_fs]), not by ensure_pdf_from_png().
            # ensure_pdf_from_png() at line 430 in append_figures_from_metrics() continues to run
            # unconditionally for non-sig-gated figure paths.
            ensure_pdf_from_png(dest_png, dest_pdf)


def export_sample_history_reads_r(total_rows, sample_stub, png_path, pdf_path):
    with tempfile.TemporaryDirectory(prefix="sample_reads_hist_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / f"{sample_stub}_reads_time_rpt.txt"
        with input_path.open("w", encoding="utf-8") as fh:
            fh.write("run_id\ttime\tdata\treads\n")
            for row in total_rows:
                epoch = int(num_any(row.get("time_epoch", 0)))
                fh.write(f"{sample_stub}\t{epoch}\thac\t{int(num_any(row.get('hac_reads_round', 0)))}\n")
                fh.write(f"{sample_stub}\t{epoch}\tsup\t{int(num_any(row.get('sup_reads_round', 0)))}\n")
        run_r_plot(
            Path(__file__).with_name("Time_reads.R"),
            input_path,
            f"{sample_stub}_reads_time.png",
            f"{sample_stub}_reads_time.pdf",
            png_path,
            pdf_path,
        )


def export_sample_history_cumulative_r(total_rows, sample_stub, png_path, pdf_path):
    with tempfile.TemporaryDirectory(prefix="sample_cum_hist_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / f"{sample_stub}_reads_cumulative_rpt.txt"
        with input_path.open("w", encoding="utf-8") as fh:
            fh.write("run_id\ttime\ttotal_reads\totu_species_reads\tconsensus_species_reads\totu_genus_reads\tconsensus_genus_reads\n")
            for row in total_rows:
                epoch = int(num_any(row.get("time_epoch", 0)))
                fh.write(
                    "\t".join(
                        [
                            sample_stub,
                            str(epoch),
                            str(int(num_any(row.get("total_reads_cumulative", 0)))),
                            str(int(num_any(row.get("otu_species_reads_total", 0)))),
                            str(int(num_any(row.get("consensus_species_reads_total", 0)))),
                            str(int(num_any(row.get("otu_genus_reads_total", 0)))),
                            str(int(num_any(row.get("consensus_genus_reads_total", 0)))),
                        ]
                    ) + "\n"
                )
        run_r_plot(
            Path(__file__).with_name("Time_reads_cumulative.R"),
            input_path,
            f"{sample_stub}_reads_cumulative_log.png",
            f"{sample_stub}_reads_cumulative_log.pdf",
            png_path,
            pdf_path,
        )


def export_sample_history_taxonomy_r(total_rows, sample_stub, source_key, png_path, pdf_path):
    script_name = "Time_taxonomy_otu.R" if source_key == "otu" else "Time_taxonomy_consensus.R"
    suffix = "otu_tax_time" if source_key == "otu" else "consensus_tax_time"
    with tempfile.TemporaryDirectory(prefix=f"sample_{source_key}_hist_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / f"{sample_stub}_{suffix}_rpt.txt"
        prefix = f"{source_key}_"
        discovered_markers = []
        seen_markers = set()
        for row in total_rows:
            if not isinstance(row, dict):
                continue
            for key in row.keys():
                match = re.match(rf"^{re.escape(prefix)}(?:species|genus|family)_count_([a-z0-9._-]+)$", key)
                if not match:
                    continue
                marker_key = match.group(1)
                if marker_key in ("total", "coi", "its2"):
                    continue
                marker = canonical_marker_token(marker_key)
                if marker and marker not in seen_markers:
                    seen_markers.add(marker)
                    discovered_markers.append(marker)
        if not discovered_markers:
            discovered_markers = ["COI", "ITS2"]
        with input_path.open("w", encoding="utf-8") as fh:
            fh.write("run_id\ttime\ttaxon\tidentifications\n")
            for row in total_rows:
                epoch = int(num_any(row.get("time_epoch", 0)))
                for rank in ("species", "genus", "family"):
                    fh.write(f"{sample_stub}\t{epoch}\t{rank}\t{int(num_any(row.get(f'{prefix}{rank}_count_total', 0)))}\n")
                    for marker in discovered_markers:
                        marker_key = marker.lower()
                        fh.write(f"{sample_stub}\t{epoch}\t{rank}_{marker}_count\t{int(num_any(row.get(f'{prefix}{rank}_count_{marker_key}', 0)))}\n")
        run_r_plot(
            Path(__file__).with_name(script_name),
            input_path,
            f"{sample_stub}_{suffix}.png",
            f"{sample_stub}_{suffix}.pdf",
            png_path,
            pdf_path,
        )


def export_icicle_plot_png_pdf(title, subtitle, tree, png_path, pdf_path):
    png_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    plt = _import_matplotlib()
    from matplotlib.patches import Rectangle

    fig, ax = plt.subplots(figsize=(12, 4.6))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if not isinstance(tree, dict) or tree.get("value", 0) <= 0:
        ax.text(0.5, 0.56, "No taxonomy assignments available", ha="center", va="center", fontsize=13, color="#4d5b48", transform=ax.transAxes)
        ax.text(0.5, 0.43, subtitle, ha="center", va="center", fontsize=10, color="#6c7a64", transform=ax.transAxes)
        ax.axis("off")
    else:
        marker_name = canonical_marker_token(tree.get("name"))
        family_colors = {}
        max_depth = 4
        nodes = []

        def walk(node, x0, width, family_name=None):
            depth = int(node.get("depth", 0))
            name = str(node.get("name") or "")
            if depth == 1:
                family_name = name
            nodes.append({"node": node, "x0": x0, "width": width, "family_name": family_name})
            total = float(node.get("value", 0) or 0)
            if total <= 0:
                return
            child_x = x0
            for child in node.get("children", []):
                child_total = float(child.get("value", 0) or 0)
                if child_total <= 0:
                    continue
                child_width = width * (child_total / total)
                walk(child, child_x, child_width, family_name)
                child_x += child_width

        walk(tree, 0.0, float(tree.get("value", 0) or 0))

        for item in nodes:
            node = item["node"]
            depth = int(node.get("depth", 0))
            width = float(item["width"])
            if width <= 0:
                continue
            y0 = max_depth - depth - 1
            family_name = item["family_name"]
            if depth == 0:
                fill = MARKER_ROOT_COLORS.get(marker_name, "#3d4d37")
            else:
                if family_name not in family_colors:
                    family_colors[family_name] = ICICLE_FAMILY_PALETTE[len(family_colors) % len(ICICLE_FAMILY_PALETTE)]
                fill = blend_hex(family_colors[family_name], "#ffffff", 0.12 * max(0, depth - 1))
            rect = Rectangle((item["x0"], y0), width, 0.9, facecolor=fill, edgecolor="white", linewidth=1.2)
            ax.add_patch(rect)
            if width >= tree["value"] * 0.055:
                label = str(node.get("name") or "")
                value = float(node.get("value", 0) or 0)
                value_label = str(int(round(value))) if abs(value - round(value)) < 1e-6 else f"{value:.1f}"
                text = f"{label}\n{value_label}"
                text_color = "white" if depth == 0 else "#20311c"
                ax.text(item["x0"] + width / 2, y0 + 0.45, text, ha="center", va="center", fontsize=8.8, color=text_color, clip_on=True)

        total_value = float(tree.get("value", 0) or 0)
        ax.set_xlim(0, max(1.0, total_value))
        ax.set_ylim(0, max_depth)
        ax.set_yticks([max_depth - depth - 0.55 for depth in range(max_depth)])
        ax.set_yticklabels([marker_name or "Marker", "Family", "Genus", "Species"][:max_depth], fontsize=9, color="#3d4d37")
        ax.tick_params(axis="x", bottom=False, labelbottom=False)
        ax.tick_params(axis="y", length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.grid(False)

    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", color="#2a3624", pad=12)
    fig.text(0.125, 0.92, subtitle, ha="left", va="bottom", fontsize=9.5, color="#5c6955")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(png_path, format="png", dpi=200, bbox_inches="tight")
    fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
    plt.close(fig)


def export_sunburst_plot_png_pdf(title, subtitle, tree, png_path, pdf_path):
    png_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    plt = _import_matplotlib()
    from matplotlib.patches import Circle, Wedge

    fig, ax = plt.subplots(figsize=(9.5, 9.5), subplot_kw={"aspect": "equal"})
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if not isinstance(tree, dict) or tree.get("value", 0) <= 0:
        ax.text(0.5, 0.55, "No taxonomy assignments available", ha="center", va="center", fontsize=13, color="#4d5b48", transform=ax.transAxes)
        ax.text(0.5, 0.44, subtitle, ha="center", va="center", fontsize=10, color="#6c7a64", transform=ax.transAxes)
        ax.axis("off")
    else:
        marker_name = canonical_marker_token(tree.get("name"))
        highlight_color = str(tree.get("highlight_color") or "#20311c")
        family_colors = {}
        ring_width = 0.23
        inner_radius = 0.18
        max_depth = 3

        center = Circle((0, 0), inner_radius, facecolor=MARKER_ROOT_COLORS.get(marker_name, "#3d4d37"), edgecolor="white", linewidth=1.2)
        ax.add_patch(center)
        ax.text(0, 0.02, marker_name, ha="center", va="center", fontsize=11, fontweight="bold", color="white")
        ax.text(0, -0.09, str(int(round(tree.get("value", 0)))), ha="center", va="center", fontsize=8.5, color="white")

        leaf_wedges = []

        def draw_node(node, start_angle, end_angle, depth, family_name=None):
            if depth > max_depth:
                return
            if depth == 1:
                family_name = str(node.get("name") or "")
            if family_name not in family_colors:
                family_colors[family_name] = ICICLE_FAMILY_PALETTE[len(family_colors) % len(ICICLE_FAMILY_PALETTE)]
            face = blend_hex(family_colors[family_name], "#ffffff", 0.12 * max(0, depth - 1))
            inner = inner_radius + (depth - 1) * ring_width
            outer = inner + ring_width
            wedge = Wedge((0, 0), outer, start_angle, end_angle, width=ring_width, facecolor=face, edgecolor="white", linewidth=1.0)
            ax.add_patch(wedge)
            span = end_angle - start_angle
            has_visible_children = any(float(c.get("value", 0) or 0) > 0 for c in node.get("children", []))
            if not has_visible_children:
                leaf_wedges.append({
                    "theta": (start_angle + end_angle) / 2.0,
                    "span": span,
                    "depth": depth,
                    "name": str(node.get("name") or ""),
                    "value": float(node.get("value", 0) or 0),
                    "highlight": bool(node.get("highlight")),
                })
            total = float(node.get("value", 0) or 0)
            child_start = start_angle
            for child in node.get("children", []):
                child_total = float(child.get("value", 0) or 0)
                if total <= 0 or child_total <= 0:
                    continue
                child_span = span * (child_total / total)
                draw_node(child, child_start, child_start + child_span, depth + 1, family_name)
                child_start += child_span

        total = float(tree.get("value", 0) or 0)
        angle = 90.0
        for child in tree.get("children", []):
            child_total = float(child.get("value", 0) or 0)
            if total <= 0 or child_total <= 0:
                continue
            span = 360.0 * (child_total / total)
            draw_node(child, angle, angle + span, 1)
            angle += span

        # External callout labels: all leaves, top 30 by value
        candidates = list(leaf_wedges)
        candidates.sort(key=lambda x: -x["value"])
        candidates = candidates[:30]

        right_cands = [lw for lw in candidates if math.cos(math.radians(lw["theta"])) >= 0]
        left_cands  = [lw for lw in candidates if math.cos(math.radians(lw["theta"])) <  0]
        right_cands.sort(key=lambda x: -math.sin(math.radians(x["theta"])))
        left_cands.sort( key=lambda x: -math.sin(math.radians(x["theta"])))

        def make_y_positions(n):
            if n == 0:
                return []
            half = min(1.40, 0.14 * n)
            if n == 1:
                return [0.0]
            return [half - (2 * half / (n - 1)) * i for i in range(n)]

        right_ys = make_y_positions(len(right_cands))
        left_ys  = make_y_positions(len(left_cands))

        for group, ys, x_col, ha in [
            (right_cands, right_ys, +1.15, "left"),
            (left_cands,  left_ys,  -1.15, "right"),
        ]:
            for lw, y_lbl in zip(group, ys):
                theta_rad = math.radians(lw["theta"])
                start_r = inner_radius + lw["depth"] * ring_width + 0.02
                sx = start_r * math.cos(theta_rad)
                sy = start_r * math.sin(theta_rad)
                kx = (start_r + 0.07) * math.cos(theta_rad)
                ky = (start_r + 0.07) * math.sin(theta_rad)
                ax.plot([sx, kx, x_col], [sy, ky, y_lbl],
                        color="#aaaaaa", linewidth=0.55, solid_capstyle="round", zorder=1)
                ax.plot(sx, sy, "o", color="#aaaaaa", markersize=1.5, zorder=2)
                x_offset = 0.05 if ha == "left" else -0.05
                fontweight = "black" if lw.get("highlight") else "normal"
                text_color = highlight_color if lw.get("highlight") else "#20311c"
                ax.text(x_col + x_offset, y_lbl, lw["name"],
                        ha=ha, va="center", fontsize=11.0, fontweight=fontweight, color=text_color, zorder=3)

        ax.set_xlim(-1.55, 1.55)
        ax.set_ylim(-1.55, 1.55)
        ax.axis("off")

    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", color="#2a3624", pad=12)
    fig.text(0.125, 0.92, subtitle, ha="left", va="bottom", fontsize=9.5, color="#5c6955")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(png_path, format="png", dpi=200, bbox_inches="tight")
    fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
    plt.close(fig)


def build_sample_treemap_items(round_obj, sample_label, level, source_key="otu", match_mode="exact"):
    source = round_obj.get(source_key) if isinstance(round_obj, dict) else None
    assignments = source.get("assignments_by_level") if isinstance(source, dict) else None
    rows = assignments.get(level) if isinstance(assignments, dict) else None
    if not isinstance(rows, list):
        return []
    by_taxon = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if match_mode == "track_unit":
            if str(row.get("track_unit_id") or row.get("sample") or "").strip() != str(sample_label or "").strip():
                continue
        elif match_mode == "track_sample_replicate":
            if track_sample_replicate_label(row) != str(sample_label or "").strip():
                continue
        elif not sample_row_matches(row.get("sample"), sample_label, match_mode=match_mode):
            continue
        taxon = str(row.get("taxon") or row.get(level) or "").strip()
        if not taxon:
            continue
        reads_total = max(0.0, float(num_any(row.get("reads_total"))))
        if reads_total <= 0:
            continue
        family = str(row.get("family") or "").strip()
        item = by_taxon.setdefault(taxon, {"taxon": taxon, "family": family, "reads_total": 0.0})
        item["reads_total"] += reads_total
    items = list(by_taxon.values())
    items.sort(key=lambda item: (-item["reads_total"], item["taxon"]))
    return items


def treemap_layout(items, x, y, w, h, total):
    if not items:
        return []
    if len(items) == 1:
        item = dict(items[0])
        item.update({"x": x, "y": y, "w": w, "h": h})
        return [item]
    half = total / 2.0
    cum = 0.0
    split = len(items) - 1
    for idx in range(len(items) - 1):
        cum += items[idx]["reads_total"]
        if cum >= half:
            split = idx + 1
            break
    a = items[:split]
    b = items[split:]
    a_total = sum(item["reads_total"] for item in a)
    ratio = a_total / total if total > 0 else 0.5
    if w >= h:
        sw = w * ratio
        return treemap_layout(a, x, y, sw, h, a_total) + treemap_layout(b, x + sw, y, w - sw, h, max(total - a_total, 0.0))
    sh = h * ratio
    return treemap_layout(a, x, y, w, sh, a_total) + treemap_layout(b, x, y + sh, w, h - sh, max(total - a_total, 0.0))


def export_treemap_pdf(title, items, pdf_path):
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    plt = _import_matplotlib()
    from matplotlib.patches import Rectangle

    fig, ax = plt.subplots(figsize=(11, 3.8))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if not items:
        ax.text(0.5, 0.5, "No assignments.", ha="center", va="center", fontsize=11, color="#4d5b48", transform=ax.transAxes)
        ax.axis("off")
    else:
        total = sum(item["reads_total"] for item in items)
        tiles = treemap_layout(items, 0.0, 0.0, 1000.0, 220.0, total if total > 0 else 1.0)
        for tile in tiles:
            family = tile.get("family") or ""
            color_key = family or tile["taxon"] or ""
            color_index = sum(ord(ch) for ch in color_key) % len(ICICLE_FAMILY_PALETTE) if color_key else 0
            fill = ICICLE_FAMILY_PALETTE[color_index] if color_key else "#9aa39b"
            rect = Rectangle((tile["x"], tile["y"]), tile["w"], tile["h"], facecolor=fill, edgecolor="white", linewidth=1.0)
            ax.add_patch(rect)
            if tile["w"] >= 95 and tile["h"] >= 38:
                ax.text(
                    tile["x"] + tile["w"] / 2.0,
                    tile["y"] + tile["h"] / 2.0,
                    str(tile["taxon"]),
                    ha="center",
                    va="center",
                    fontsize=8,
                    color="white",
                )
        ax.set_xlim(0, 1000)
        ax.set_ylim(0, 220)
        ax.invert_yaxis()
        ax.axis("off")

    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", color="#2a3624", pad=12)
    fig.tight_layout()
    fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
    plt.close(fig)


def append_generated_sample_figures(sorted_rounds, latest_round, out_path, run_id, run_start_epoch=None, sample_plot_max=None):
    if not run_id or not isinstance(latest_round, dict):
        return
    ordered_samples = []
    use_track_unit_metrics = bool(
        latest_round.get("identity_mode") == "track"
        and isinstance(latest_round.get("track_unit_metrics"), dict)
    )
    if use_track_unit_metrics:
        grouped = {}
        for raw in latest_round.get("track_unit_metrics", {}).values():
            if not isinstance(raw, dict):
                continue
            sample_label = track_sample_label_value(raw)
            if not sample_label:
                continue
            entry = grouped.setdefault(sample_label, {
                "label": sample_label,
                "reads_demux": 0,
                "figures": [],
            })
            entry["reads_demux"] += num_any(raw.get("reads_demux", 0))
        for sample_label, raw in grouped.items():
            ordered_samples.append(
                (
                    sample_label,
                    raw,
                    -float(num_any(raw.get("reads_demux", 0))),
                    natural_round_key(sample_label),
                    natural_round_key(str(sample_label)),
                )
            )
    else:
        sample_metrics = latest_round.get("sample_metrics")
        if not isinstance(sample_metrics, dict):
            return
        for sample_id, raw in sample_metrics.items():
            if not isinstance(raw, dict):
                continue
            sample_label = str(raw.get("label") or sample_id or "sample")
            ordered_samples.append(
                (
                    sample_id,
                    raw,
                    -float(num_any(raw.get("reads_demux", 0))),
                    natural_round_key(sample_label),
                    natural_round_key(str(sample_id)),
                )
            )
    ordered_samples.sort(key=lambda item: (item[2], item[3], item[4]))
    selected_sample_ids = None
    if sample_plot_max is not None and sample_plot_max >= 0:
        selected_sample_ids = {item[0] for item in ordered_samples[:sample_plot_max]}

    figure_specs = [
        {
            "id": "sample_reads_time_history",
            "suffix": "reads_time_history",
            "title": "Reads vs Time (sample)",
            "description": "Cumulative HAC and SUP demultiplexed reads for this sample across rounds.",
            "section": "Run evolution",
            "order": 10,
            "kind": "history_reads",
        },
        {
            "id": "sample_reads_cumulative_history",
            "suffix": "reads_cumulative_history",
            "title": "Cumulative Reads (log, sample)",
            "description": "Cumulative reads plus supported OTU/consensus read support across rounds.",
            "section": "Run evolution",
            "order": 11,
            "kind": "history_cumulative",
        },
        {
            "id": "sample_otu_tax_time_history",
            "suffix": "otu_tax_time_history",
            "title": "OTU Taxonomy Over Time (sample)",
            "description": "Supported OTU taxonomy counts by marker across rounds.",
            "section": "Run evolution",
            "order": 12,
            "kind": "history_otu_tax",
        },
        {
            "id": "sample_consensus_tax_time_history",
            "suffix": "consensus_tax_time_history",
            "title": "Consensus Taxonomy Over Time (sample)",
            "description": "Consensus taxonomy counts by marker across rounds.",
            "section": "Run evolution",
            "order": 13,
            "kind": "history_consensus_tax",
        },
    ]

    # Discover all markers from the data (scan all rounds' assignment rows)
    _chart_markers = marker_order_from_data(sorted_rounds)

    _chart_order_base = 40
    for _di, _data_type in enumerate(("otu", "consensus")):
        for _mi, _mk in enumerate(_chart_markers):
            _ml = marker_slug(_mk)
            for _ci, (_chart_suffix, _chart_kind) in enumerate((("icicle", None), ("sunburst", "sunburst"))):
                _order = _chart_order_base + _di * len(_chart_markers) * 2 + _mi * 2 + _ci
                _spec = {
                    "id": f"sample_{_data_type}_{_ml}_{_chart_suffix}",
                    "suffix": f"{_data_type}_{_ml}_{_chart_suffix}",
                    "title": f"{'OTU' if _data_type == 'otu' else 'Consensus'} {'Icicle' if _chart_suffix == 'icicle' else 'Sunburst'} ({_mk})",
                    "description": f"Hierarchy: {_mk} -> family -> genus -> species assignments.",
                    "section": "Taxonomy",
                    "order": _order,
                    "source_key": _data_type,
                    "marker": _mk,
                }
                if _chart_kind:
                    _spec["kind"] = _chart_kind
                figure_specs.append(_spec)

    generated_ids = {spec["id"] for spec in figure_specs}

    def build_sample_taxonomy_tree_compat(round_obj, sample_label, source_key, marker, include_row=None, match_mode="exact"):
        try:
            return build_sample_taxonomy_tree(
                round_obj,
                sample_label,
                source_key,
                marker,
                include_row=include_row,
                match_mode=match_mode,
            )
        except TypeError as exc:
            # Test stubs and older helper overrides may still use the pre-match_mode signature.
            if "match_mode" not in str(exc):
                raise
            return build_sample_taxonomy_tree(
                round_obj,
                sample_label,
                source_key,
                marker,
                include_row=include_row,
            )

    for sample_id, raw, _reads_key, _label_key, _sample_key in ordered_samples:
        sample_label = str(raw.get("label") or sample_id or "sample")
        figures = [copy.deepcopy(fig) for fig in raw.get("figures", []) if isinstance(fig, dict) and fig.get("id") not in generated_ids]
        if selected_sample_ids is not None and sample_id not in selected_sample_ids:
            raw["figures"] = figures
            continue
        safe_sample_id = sanitize_filename(sample_id)
        history_rows = None
        for spec in figure_specs:
            base_name = f"{safe_sample_id}_{spec['suffix']}"
            png_rel = report_assets_rel_path(run_id, "samples", safe_sample_id, f"{base_name}.png")
            pdf_rel = report_assets_rel_path(run_id, "samples", safe_sample_id, f"{base_name}.pdf")
            png_fs = resolve_asset_fs_path(png_rel, out_path, run_id)
            pdf_fs = resolve_asset_fs_path(pdf_rel, out_path, run_id)
            if png_fs is None or pdf_fs is None:
                continue
            kind = spec.get("kind")
            signature_path = sample_signature_path(out_path, run_id, safe_sample_id, base_name)
            source_payload = None
            try:
                if kind == "history_reads":
                    history_rows = history_rows or build_sample_history_rows(sorted_rounds, sample_id, sample_label, out_path, run_id, run_start_epoch=run_start_epoch)
                    source_payload = history_rows
                elif kind == "history_cumulative":
                    history_rows = history_rows or build_sample_history_rows(sorted_rounds, sample_id, sample_label, out_path, run_id, run_start_epoch=run_start_epoch)
                    source_payload = history_rows
                elif kind == "history_otu_tax":
                    history_rows = history_rows or build_sample_history_rows(sorted_rounds, sample_id, sample_label, out_path, run_id, run_start_epoch=run_start_epoch)
                    source_payload = history_rows
                elif kind == "history_consensus_tax":
                    history_rows = history_rows or build_sample_history_rows(sorted_rounds, sample_id, sample_label, out_path, run_id, run_start_epoch=run_start_epoch)
                    source_payload = history_rows
                else:
                    include_row = is_supported_otu_assignment_row if spec["source_key"] == "otu" else None
                    tree = build_sample_taxonomy_tree_compat(
                        latest_round,
                        sample_label,
                        spec["source_key"],
                        spec["marker"],
                        include_row=include_row,
                        match_mode=("track_sample" if use_track_unit_metrics else "exact"),
                    )
                    source_payload = tree
                signature = compute_signature({
                    "version": RENDER_SIGNATURE_VERSION,
                    "run_id": run_id,
                    "sample_id": sample_id,
                    "figure_kind": kind or "taxonomy",
                    "marker": spec.get("marker"),
                    "payload": source_payload,
                })
                if should_regenerate_sample_asset(signature_path, [png_fs, pdf_fs], signature):
                    if kind == "history_reads":
                        export_sample_history_reads_r(history_rows, safe_sample_id, png_fs, pdf_fs)
                    elif kind == "history_cumulative":
                        export_sample_history_cumulative_r(history_rows, safe_sample_id, png_fs, pdf_fs)
                    elif kind == "history_otu_tax":
                        export_sample_history_taxonomy_r(history_rows, safe_sample_id, "otu", png_fs, pdf_fs)
                    elif kind == "history_consensus_tax":
                        export_sample_history_taxonomy_r(history_rows, safe_sample_id, "consensus", png_fs, pdf_fs)
                    elif spec.get("kind") == "sunburst":
                        export_sunburst_plot_png_pdf(
                            spec["title"],
                            f"{sample_label}: wedges scaled by assigned reads",
                            source_payload,
                            png_fs,
                            pdf_fs,
                        )
                    else:
                        export_icicle_plot_png_pdf(
                            spec["title"],
                            f"{sample_label}: widths scaled by assigned reads",
                            source_payload,
                            png_fs,
                            pdf_fs,
                        )
                    if png_fs.exists() and pdf_fs.exists():
                        atomic_write_text(signature_path, signature + "\n")
            except Exception:
                pass
            figures.append({
                "id": spec["id"],
                "title": spec["title"],
                "description": spec["description"],
                "section": spec["section"],
                "order": spec["order"],
                "path": png_rel,
                "pdf_path": pdf_rel,
                "exists": png_fs.exists(),
                "pdf_exists": pdf_fs.exists(),
            })
        raw["figures"] = figures


def build_run_like(run):
    summary = run.get("run_summary") if isinstance(run, dict) else None
    if not isinstance(summary, dict):
        return {"round_barcode": run.get("run_id", "-") if isinstance(run, dict) else "-", "_summary_missing": True}
    return {
        "round_barcode": run.get("run_id", "-"),
        "reads": summary.get("reads") or {},
        "read_fate": summary.get("read_fate") or {},
        "otu": summary.get("otu") or {},
        "consensus": summary.get("consensus") or {},
        "_summary_missing": False,
    }


def build_run_status_read_fate_like(run):
    read_fate = run.get("run_status_read_fate") if isinstance(run, dict) else None
    if not isinstance(read_fate, dict):
        return {"round_barcode": run.get("run_id", "-") if isinstance(run, dict) else "-", "_summary_missing": True}
    return {
        "round_barcode": run.get("run_id", "-"),
        "read_fate": read_fate,
        "_summary_missing": False,
    }


def detect_demux_enabled(rounds):
    for row in rounds:
        demux_total = get_path(row, ["read_fate", "demux_total_reads"], None)
        no_adapter = get_path(row, ["read_fate", "no_adapter_reads"], None)
        demux_enabled = get_path(row, ["read_fate", "demux_enabled"], None)
        if demux_enabled is True:
            return True
        if demux_enabled is False:
            continue
        if demux_total is not None and no_adapter is not None and num_any(demux_total) > num_any(no_adapter):
            return True
    return False


def resolve_demux_blast_assigned(params):
    if params["assignment_classified"] and params["has_adapter_assigned"]:
        return clamp(params["blast_assigned_adapter_raw_num"], 0, params["adapter_capacity"])
    if params["assignment_classified"] and params["has_global_assigned"]:
        return clamp(params["blast_assigned_raw_num"], 0, params["adapter_capacity"])
    return 0


def resolve_demux_adapter_seen(params):
    if params["has_explicit_blast_seen_adapter"]:
        return clamp(params["blast_seen_adapter_raw_num"], 0, params["adapter_universe"]), True
    if (
        params["has_explicit_blast_seen"]
        and params["has_explicit_blast_seen_no_adapter"]
        and params["has_explicit_blast_seen_unbucketed"]
        and params["blast_seen_unbucketed_raw_num"] == 0
    ):
        derived = max(0, params["blast_seen_raw_num"] - params["blast_seen_no_adapter_raw_num"])
        return clamp(derived, 0, params["adapter_universe"]), True
    return params["adapter_universe"], False


def is_blast_assignment_classified(read_fate):
    status = read_fate.get("blast_assignment_status")
    if status == "classified":
        return True
    if status == "unknown":
        return False
    return any(
        has_value(read_fate.get(key))
        for key in (
            "blast_assigned_reads",
            "blast_unassigned_reads",
            "blast_assigned_reads_adapter",
            "blast_unassigned_reads_adapter",
            "blast_seen_reads_adapter",
            "blast_seen_reads_no_adapter",
            "blast_seen_reads_unbucketed",
        )
    )


def build_reads_fate_rows(data):
    markers = marker_order_from_data(data)
    marker_colors = marker_colors_from_data(data)
    fate_order, fate_colors = build_fate_style(markers, marker_colors)
    rows = []
    for row in data:
        row_name = row.get("round_barcode", "-") if isinstance(row, dict) else "-"
        if isinstance(row, dict) and row.get("_summary_missing"):
            rows.append({
                "label": row_name,
                "segments": [{"label": lbl, "value": None} for lbl in fate_order],
            })
            continue
        read_fate = row.get("read_fate") if isinstance(row, dict) and isinstance(row.get("read_fate"), dict) else {}
        status = read_fate.get("marker_split_status")
        if status != "ok":
            rows.append({
                "label": row_name,
                "segments": [{"label": lbl, "value": None} for lbl in fate_order],
            })
            continue
        marker_counts = read_fate.get("marker_counts") if isinstance(read_fate.get("marker_counts"), dict) else {}
        chart_assigned = marker_counts.get("chart_blast_assigned") if isinstance(marker_counts.get("chart_blast_assigned"), dict) else {}
        chart_unassigned = marker_counts.get("chart_blast_unassigned") if isinstance(marker_counts.get("chart_blast_unassigned"), dict) else {}
        chart_skipped = marker_counts.get("chart_blast_skipped") if isinstance(marker_counts.get("chart_blast_skipped"), dict) else {}
        segments = []
        for marker in markers:
            segments.append({"label": f"BLAST-assigned {marker}", "value": chart_assigned.get(marker, get_path(read_fate, [f"chart_blast_assigned_{marker.lower()}"], None))})
            segments.append({"label": f"BLAST-unassigned {marker}", "value": chart_unassigned.get(marker, get_path(read_fate, [f"chart_blast_unassigned_{marker.lower()}"], None))})
            segments.append({"label": f"BLAST skipped {marker}", "value": chart_skipped.get(marker, get_path(read_fate, [f"chart_blast_skipped_{marker.lower()}"], None))})
        segments.extend([
            {"label": "On-target not demultiplexed", "value": get_path(read_fate, ["chart_on_target_not_demultiplexed"], None)},
            {"label": "Off-target", "value": get_path(read_fate, ["chart_off_target"], None)},
        ])
        rows.append({
            "label": row_name,
            "segments": segments,
        })
    seen_labels = []
    seen_set = set()
    for _row in rows:
        for _seg in _row.get("segments", []):
            _lbl = _seg["label"]
            if _lbl not in seen_set:
                seen_set.add(_lbl)
                seen_labels.append(_lbl)
    fate_order = [lbl for lbl in fate_order if lbl in seen_set]
    for lbl in seen_labels:
        if lbl not in fate_order:
            fate_order.append(lbl)
    fate_colors = {lbl: fate_colors.get(lbl, "#9aa39b") for lbl in fate_order}
    return rows, fate_order, fate_colors


def build_generic_stacked_rows(data, segment_defs):
    rows = []
    order = [seg["label"] for seg in segment_defs]
    colors = {seg["label"]: seg["color"] for seg in segment_defs}
    for row in data:
        label = row.get("round_barcode", "-") if isinstance(row, dict) else "-"
        segments = []
        for seg in segment_defs:
            if isinstance(row, dict) and row.get("_summary_missing"):
                value = None
            elif "compute" in seg:
                value = seg["compute"](row)
            else:
                value = get_path(row, seg["path"], None)
            if value is not None:
                value = max(0, num_any(value))
            segments.append({"label": seg["label"], "value": value})
        rows.append({"label": label, "segments": segments})
    return rows, order, colors


def _marker_taxon_specs(rounds_data, source_key, sub_key):
    """Build segment_defs for active_by_marker_taxon / emitted_by_marker_taxon dynamically."""
    markers = marker_order_from_data(rounds_data)
    marker_colors = marker_colors_from_data(rounds_data)
    specs = []
    for m in markers:
        k = m.lower()
        c = marker_colors.get(m, marker_color(m))
        specs.append({
            "label": f"{m} assigned",
            "path": [source_key, sub_key, f"{k}_assigned"],
            "color": c,
            "compute": lambda row, marker=m, legacy_key=f"{k}_assigned": get_path(row, [source_key, sub_key, "assigned", marker], get_path(row, [source_key, sub_key, legacy_key], None)),
        })
        specs.append({
            "label": f"{m} unassigned",
            "path": [source_key, sub_key, f"{k}_unassigned"],
            "color": _lighten_hex(c, 0.5),
            "compute": lambda row, marker=m, legacy_key=f"{k}_unassigned": get_path(row, [source_key, sub_key, "unassigned", marker], get_path(row, [source_key, sub_key, legacy_key], None)),
        })
    specs.append({"label": "Other assigned", "path": [source_key, sub_key, "other_assigned"], "color": "#6b5b95",
                  "compute": lambda row: get_path(row, [source_key, sub_key, "assigned", "OTHER"], get_path(row, [source_key, sub_key, "other_assigned"], None))})
    specs.append({"label": "Other unassigned", "path": [source_key, sub_key, "other_unassigned"], "color": "#b39cd0",
                  "compute": lambda row: get_path(row, [source_key, sub_key, "unassigned", "OTHER"], get_path(row, [source_key, sub_key, "other_unassigned"], None))})
    return specs


def collect_sample_totals(rounds, collapse_track_units=False):
    markers = marker_order_from_data(rounds)
    samples = {}
    for row in rounds:
        round_id = row.get("round_barcode", "-")
        use_track_unit_metrics = bool(
            row.get("identity_mode") == "track"
            and isinstance(row.get("track_unit_metrics"), dict)
            and (CURRENT_GROUP_VIEW in ("replicate", "track_detail") or collapse_track_units)
        )
        metrics_key = "track_unit_metrics" if use_track_unit_metrics else "sample_metrics"
        sample_metrics = row.get(metrics_key)
        if not isinstance(sample_metrics, dict):
            continue
        for sample_id, raw in sample_metrics.items():
            raw = raw if isinstance(raw, dict) else {}
            raw_label = str(raw.get("label") or raw.get("track_unit_id") or sample_id or "sample")
            if CURRENT_GROUP_VIEW == "track_detail":
                label = track_sample_replicate_label(raw) or raw_label
                entry_id = label or str(sample_id or "sample")
            elif CURRENT_GROUP_VIEW == "replicate":
                label = str(raw.get("track_replicate_label") or raw.get("track_replicate_id") or raw_label).strip()
                entry_id = label or str(sample_id or "sample")
            else:
                if use_track_unit_metrics:
                    label = track_sample_label_value(raw) or sample_group_label(raw_label, collapse_track_units=collapse_track_units)
                else:
                    label = sample_group_label(raw_label, collapse_track_units=collapse_track_units)
                entry_id = (label or str(sample_id or "sample")) if collapse_track_units else str(sample_id or "sample")
            entry = samples.setdefault(entry_id, {
                "sample_id": entry_id,
                "label": label,
                "track_sample_label": str(raw.get("track_sample_label") or ""),
                "track_replicate_number": num_any(raw.get("track_replicate_number")),
                "track_replicate_label": str(raw.get("track_replicate_label") or raw.get("track_replicate_id") or ""),
                "track_sample_replicate_label": track_sample_replicate_label(raw),
                "track_primer_label": str(raw.get("track_primer_label") or ""),
                "track_unit_id": str(raw.get("track_unit_id") or sample_id or ""),
                "totals": {
                    "reads_demux": 0,
                    "reads_demux_coi": 0,
                    "reads_demux_its2": 0,
                    "reads_demux_by_marker": {marker: 0 for marker in markers},
                    "reads_blast_assigned": 0,
                    "otu_active": 0,
                    "consensus_emitted": 0,
                },
                "rounds_by_id": {},
            })
            round_entry = entry["rounds_by_id"].setdefault(round_id, {
                "round_barcode": round_id,
                "reads_demux": 0,
                "reads_blast_assigned": 0,
                "otu_active": 0,
                "consensus_emitted": 0,
            })
            round_entry["reads_demux"] += num(raw.get("reads_demux"))
            round_entry["reads_blast_assigned"] += num(raw.get("reads_blast_assigned"))
            round_entry["otu_active"] += num(raw.get("otu_active"))
            round_entry["consensus_emitted"] += num(raw.get("consensus_emitted"))
            for key in ("reads_demux", "reads_demux_coi", "reads_demux_its2", "reads_blast_assigned", "otu_active", "consensus_emitted"):
                if key not in entry["totals"]:
                    entry["totals"][key] = 0
                entry["totals"][key] += num(raw.get(key))
            demux_by_marker = raw.get("reads_demux_by_marker") if isinstance(raw.get("reads_demux_by_marker"), dict) else {}
            for marker, value in demux_by_marker.items():
                canonical = canonical_marker_token(marker)
                if not canonical:
                    continue
                entry["totals"]["reads_demux_by_marker"][canonical] = entry["totals"]["reads_demux_by_marker"].get(canonical, 0) + num(value)
            for key in raw:
                if key.startswith("reads_demux_") and key != "reads_demux_by_marker":
                    if key not in entry["totals"]:
                        entry["totals"][key] = 0
                    entry["totals"][key] += num(raw.get(key))
    ordered = []
    for entry in samples.values():
        rounds_by_id = entry.pop("rounds_by_id", {})
        entry["rounds"] = list(rounds_by_id.values())
        ordered.append(entry)
    if CURRENT_GROUP_VIEW == "track_detail":
        ordered.sort(key=lambda item: (
            item.get("track_sample_label", ""),
            num_any(item.get("track_replicate_number")),
            item.get("track_sample_replicate_label") or item["label"],
        ))
    elif CURRENT_GROUP_VIEW == "replicate":
        ordered.sort(key=lambda item: (
            item.get("track_sample_label", ""),
            num_any(item.get("track_replicate_number")),
            item.get("track_replicate_label", item["label"]),
        ))
    else:
        ordered.sort(key=lambda item: (-item["totals"]["reads_demux"], item["label"]))
    return ordered


def collect_sample_replicate_bars(latest_round, sample_entry, collapse_track_units=False):
    if not isinstance(latest_round, dict) or not isinstance(sample_entry, dict):
        return []
    if CURRENT_GROUP_VIEW == "track_detail":
        track_unit_metrics = latest_round.get("track_unit_metrics")
        if not isinstance(track_unit_metrics, dict):
            return [{
                "label": str(sample_entry.get("label") or sample_entry.get("sample_id") or "Barcode"),
                "value": max(0.0, num_any(get_path(sample_entry, ["totals", "reads_demux"], 0))),
            }]
        target_label = str(sample_entry.get("track_sample_replicate_label") or sample_entry.get("label") or "").strip()
        primer_totals = {}
        for raw in track_unit_metrics.values():
            if not isinstance(raw, dict):
                continue
            if track_sample_replicate_label(raw) != target_label:
                continue
            primer_label = str(raw.get("track_replicate_label") or raw.get("track_replicate_id") or raw.get("track_unit_id") or "Barcode")
            primer_totals[primer_label] = primer_totals.get(primer_label, 0.0) + max(0.0, num_any(raw.get("reads_demux")))
        if primer_totals:
            return [
                {"label": label, "value": value}
                for label, value in sorted(
                    primer_totals.items(),
                    key=lambda item: (natural_round_key(item[0]), natural_round_key(item[0])),
                )
            ]
        return [{
            "label": str(sample_entry.get("label") or sample_entry.get("sample_id") or "Barcode"),
            "value": max(0.0, num_any(get_path(sample_entry, ["totals", "reads_demux"], 0))),
        }]
    if collapse_track_units:
        if latest_round.get("identity_mode") == "track":
            track_unit_metrics = latest_round.get("track_unit_metrics")
            target_label = str(sample_entry.get("label") or "").strip()
            if isinstance(track_unit_metrics, dict):
                replicate_totals = {}
                for raw in track_unit_metrics.values():
                    if not isinstance(raw, dict):
                        continue
                    if track_sample_label_value(raw) != target_label:
                        continue
                    replicate_label = concise_track_replicate_label(raw) or str(raw.get("track_replicate_id") or raw.get("track_unit_id") or "Barcode")
                    replicate_totals[replicate_label] = replicate_totals.get(replicate_label, 0.0) + max(0.0, num_any(raw.get("reads_demux")))
                if replicate_totals:
                    return [
                        {"label": label, "value": value}
                        for label, value in sorted(
                            replicate_totals.items(),
                            key=lambda item: (natural_round_key(item[0]), natural_round_key(item[0])),
                        )
                    ]
            sample_metrics = latest_round.get("sample_metrics")
            if not isinstance(sample_metrics, dict):
                return []
            replicate_totals = {}
            for sample_id, raw in sample_metrics.items():
                if not isinstance(raw, dict):
                    continue
                raw_label = str(raw.get("label") or sample_id or "Barcode").strip()
                if sample_group_label(raw_label, collapse_track_units=True) != target_label:
                    continue
                replicate_totals[raw_label] = replicate_totals.get(raw_label, 0.0) + max(0.0, num_any(raw.get("reads_demux")))
            return [
                {"label": label, "value": value}
                for label, value in sorted(
                    replicate_totals.items(),
                    key=lambda item: (natural_round_key(item[0]), natural_round_key(item[0])),
                )
            ]
        sample_metrics = latest_round.get("sample_metrics")
        if not isinstance(sample_metrics, dict):
            return []
        target_label = str(sample_entry.get("label") or "").strip()
        replicate_totals = {}
        for sample_id, raw in sample_metrics.items():
            if not isinstance(raw, dict):
                continue
            raw_label = str(raw.get("label") or sample_id or "Barcode").strip()
            if sample_group_label(raw_label, collapse_track_units=True) != target_label:
                continue
            replicate_totals[raw_label] = replicate_totals.get(raw_label, 0.0) + max(0.0, num_any(raw.get("reads_demux")))
        return [
            {"label": label, "value": value}
            for label, value in sorted(
                replicate_totals.items(),
                key=lambda item: (natural_round_key(item[0]), natural_round_key(item[0])),
            )
        ]

    sample_metrics = latest_round.get("sample_metrics")
    if not isinstance(sample_metrics, dict):
        return []

    sample_id = sample_entry.get("sample_id")
    sample_metrics_entry = sample_metrics.get(sample_id, {}) if sample_id in sample_metrics else {}
    replicates = sample_metrics_entry.get("replicates") if isinstance(sample_metrics_entry, dict) else None
    if not isinstance(replicates, dict):
        return []

    ordered_replicates = []
    for rep_id, rep_raw in replicates.items():
        rep_raw = rep_raw if isinstance(rep_raw, dict) else {}
        label = str(rep_raw.get("label") or rep_id or "Barcode")
        ordered_replicates.append(
            {
                "label": label,
                "sort_label": str(rep_id or label or ""),
                "value": max(0.0, num_any(rep_raw.get("reads_demux"))),
            }
        )
    ordered_replicates.sort(key=lambda item: (natural_round_key(item["sort_label"]), natural_round_key(item["label"])))
    return [{"label": item["label"], "value": item["value"]} for item in ordered_replicates]


def _import_matplotlib():
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    return plt


def export_stacked_bar_pdf(title, rows, order, colors, pdf_path):
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    active_labels = []
    for label in order:
        if any(
            isinstance(seg.get("value"), (int, float)) and seg.get("value", 0) > 0
            for row in rows
            for seg in row.get("segments", [])
            if seg.get("label") == label
        ):
            active_labels.append(label)
    if not active_labels:
        active_labels = [label for label in order if label in colors]

    numeric_rows = []
    max_total = 0
    for row in rows:
        values = {}
        total = 0
        has_numeric = False
        for seg in row.get("segments", []):
            value = seg.get("value")
            if isinstance(value, (int, float)):
                has_numeric = True
                values[seg["label"]] = max(0, float(value))
                total += max(0, float(value))
        max_total = max(max_total, total)
        numeric_rows.append((row.get("label", "-"), values, total, has_numeric))

    plt = _import_matplotlib()
    fig_h = max(2.8, 1.8 + 0.52 * max(1, len(rows)))
    fig, ax = plt.subplots(figsize=(11, fig_h))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if not any(item[3] for item in numeric_rows):
        ax.text(0.5, 0.5, "No data available", ha="center", va="center", fontsize=11, color="#4d5b48", transform=ax.transAxes)
        ax.axis("off")
    else:
        y_positions = list(range(len(numeric_rows)))
        lefts = [0.0] * len(numeric_rows)
        for label in active_labels:
            widths = [row_values.get(label, 0.0) for _, row_values, _, _ in numeric_rows]
            if not any(widths):
                continue
            ax.barh(
                y_positions,
                widths,
                left=lefts,
                color=colors.get(label, "#9aa39b"),
                edgecolor="white",
                linewidth=0.7,
                label=label,
                height=0.72,
            )
            lefts = [left + width for left, width in zip(lefts, widths)]

        max_total = max(1.0, max_total)
        for idx, (_, _, total, has_numeric) in enumerate(numeric_rows):
            label = "N/A" if not has_numeric else str(int(total) if abs(total - round(total)) < 1e-6 else round(total, 2))
            ax.text(total + max_total * 0.01, idx, label, va="center", ha="left", fontsize=9, color="#3d4d37")

        ax.set_yticks(y_positions)
        ax.set_yticklabels([label for label, _, _, _ in numeric_rows], fontsize=9, color="#2a3624")
        ax.invert_yaxis()
        ax.set_xlim(0, max_total * 1.12)
        ax.tick_params(axis="x", labelsize=9, colors="#4d5b48")
        ax.grid(axis="x", color="#e3e8dc", linewidth=0.8)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_visible(False)
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            legend = ax.legend(
                loc="upper center",
                bbox_to_anchor=(0.5, -0.12),
                ncol=2 if len(active_labels) <= 4 else 3,
                frameon=False,
                fontsize=8.5,
                handlelength=1.5,
                columnspacing=1.2,
            )
            if legend:
                for text in legend.get_texts():
                    text.set_color("#3d4d37")

    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", color="#2a3624", pad=12)
    fig.tight_layout()
    fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
    plt.close(fig)


def export_vertical_bar_pdf(title, bars, color, pdf_path):
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    plt = _import_matplotlib()
    labels = [str(item.get("label") or "-") for item in bars if isinstance(item, dict)]
    values = [max(0.0, float(num_any(item.get("value")))) for item in bars if isinstance(item, dict)]
    fig_w = max(5.5, 1.2 + 0.8 * max(1, len(labels)))
    fig, ax = plt.subplots(figsize=(fig_w, 4.8))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    if not values or not any(value > 0 for value in values):
        ax.text(0.5, 0.5, "No data available", ha="center", va="center", fontsize=11, color="#4d5b48", transform=ax.transAxes)
        ax.axis("off")
    else:
        bars_plot = ax.bar(range(len(values)), values, color=color, edgecolor="#ffffff", linewidth=0.8, width=0.72)
        max_value = max(values)
        for idx, (bar_item, value) in enumerate(zip(bars_plot, values)):
            label = str(int(round(value))) if abs(value - round(value)) < 1e-6 else f"{value:.1f}"
            ax.text(bar_item.get_x() + bar_item.get_width() / 2, value + max_value * 0.02, label, ha="center", va="bottom", fontsize=8.5, color="#3d4d37")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=8.5, color="#2a3624")
        ax.tick_params(axis="y", labelsize=9, colors="#4d5b48")
        ax.grid(axis="y", color="#e3e8dc", linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_ylim(0, max_value * 1.14 if max_value > 0 else 1)
        for spine in ax.spines.values():
            spine.set_visible(False)

    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", color="#2a3624", pad=12)
    fig.tight_layout()
    fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
    plt.close(fig)


def build_chart_exports(sorted_rounds, sorted_runs, out_path, run_id):
    chart_dir = report_assets_dir(out_path) / "embedded"
    exports = {}
    latest_round = sorted_rounds[-1] if sorted_rounds else {}
    _scope = run_id if run_id else "index"
    _sig_root = report_assets_dir(out_path) / ".private_signatures" / "embedded" / _scope
    # collapse_track_units controls how track units are grouped in chart exports.
    #
    # sample view (track mode):    True  — track units (sample_A_1, sample_A_2) are collapsed
    #                                       under a shared parent label (sample_A). Each parent
    #                                       gets one "reads per barcode" chart with per-unit bars.
    #
    # replicate view (track mode): False — primer-qualified track IDs remain separate groups.
    #
    # collapse mode (identity_mode != "track"): always False — no track units exist.
    collapse_track_units = bool(
        run_id
        and CURRENT_GROUP_VIEW == "sample"
        and isinstance(latest_round, dict)
        and latest_round.get("identity_mode") == "track"
    )

    def raw_pdf_path(filename):
        return report_assets_rel_path(run_id, "embedded", filename)

    def add_export(chart_id, title, filename, rows, order, colors):
        pdf_path = chart_dir / filename
        sig_path = _sig_root / f"{chart_id}.sig"
        sig = compute_signature({"version": RENDER_EMBEDDED_SIGNATURE_VERSION,
                                  "title": title, "rows": rows, "order": list(order),
                                  "colors": {k: v for k, v in colors.items() if k in order}})
        if not (sig_path.exists() and pdf_path.exists() and read_signature(sig_path) == sig):
            try:
                export_stacked_bar_pdf(title, rows, order, colors, pdf_path)
                if pdf_path.exists():
                    atomic_write_text(sig_path, sig + "\n")
            except Exception:
                pass
        if pdf_path.exists():
            return {"pdf_path": raw_pdf_path(filename)}
        return {}

    def add_vertical_export(chart_id, title, filename, bars, color):
        pdf_path = chart_dir / filename
        sig_path = _sig_root / f"{chart_id}.sig"
        sig = compute_signature({"version": RENDER_EMBEDDED_SIGNATURE_VERSION,
                                  "title": title, "bars": bars})
        if not (sig_path.exists() and pdf_path.exists() and read_signature(sig_path) == sig):
            try:
                export_vertical_bar_pdf(title, bars, color, pdf_path)
                if pdf_path.exists():
                    atomic_write_text(sig_path, sig + "\n")
            except Exception:
                pass
        if pdf_path.exists():
            return {"pdf_path": raw_pdf_path(filename)}
        return {}

    def add_sunburst_export(chart_id, title, subtitle, source_key, marker, include_row=None):
        png_path = chart_dir / f"{chart_id}.png"
        pdf_path = chart_dir / f"{chart_id}.pdf"
        sig_path = _sig_root / f"{chart_id}.sig"
        tree = build_run_taxonomy_tree(latest_round, source_key, marker, include_row=include_row)
        sig = compute_signature({"version": RENDER_EMBEDDED_SIGNATURE_VERSION,
                                  "title": title, "tree": tree})
        if not (sig_path.exists() and pdf_path.exists() and read_signature(sig_path) == sig):
            try:
                export_sunburst_plot_png_pdf(title, subtitle, tree, png_path, pdf_path)
                if pdf_path.exists():
                    atomic_write_text(sig_path, sig + "\n")
            except Exception:
                pass
        if pdf_path.exists():
            return {"pdf_path": raw_pdf_path(f"{chart_id}.pdf")}
        return {}

    if run_id:
        read_rows, read_order, read_colors = build_reads_fate_rows(sorted_rounds)
        exports["run_reads_fate"] = add_export("run_reads_fate", "Reads Fate Per Round (Round intake)", "run_reads_fate.pdf", read_rows, read_order, read_colors)

        otu_rows, otu_order, otu_colors = build_generic_stacked_rows(sorted_rounds, [
            {"label": "Informative dynamic", "path": ["otu", "canonical", "informative_dynamic"], "color": "#59a14f"},
            {"label": "Prune candidates", "path": ["otu", "pruned", "prune_candidates"], "color": "#9aa39b"},
            {"label": "Size-streak pruned", "path": ["otu", "pruned", "size_streak"], "color": "#f28e2b"},
            {"label": "BLAST-unassigned", "path": ["otu", "pruned", "blast_unassigned"], "color": "#e15759"},
        ])
        exports["run_otu_fate"] = add_export("run_otu_fate", "OTU Fate Per Round", "run_otu_fate.pdf", otu_rows, otu_order, otu_colors)

        active_rows, active_order, active_colors = build_generic_stacked_rows(
            sorted_rounds, _marker_taxon_specs(sorted_rounds, "otu", "active_by_marker_taxon")
        )
        exports["run_informative_otu"] = add_export(
            "run_informative_otu",
            "Informative OTUs (assigned vs unassigned)",
            "run_informative_otu.pdf",
            active_rows,
            active_order,
            active_colors,
        )

        cons_rows, cons_order, cons_colors = build_generic_stacked_rows(
            sorted_rounds, _marker_taxon_specs(sorted_rounds, "consensus", "emitted_by_marker_taxon")
        )
        exports["run_consensus_emitted"] = add_export(
            "run_consensus_emitted",
            "Consensus Emitted (assigned vs unassigned)",
            "run_consensus_emitted.pdf",
            cons_rows,
            cons_order,
            cons_colors,
        )

        if detect_demux_enabled(sorted_rounds):
            ordered_samples = collect_sample_totals(sorted_rounds, collapse_track_units=collapse_track_units)
            if ordered_samples:
                _configured_marker_order = marker_order_from_data(sorted_rounds)
                _demux_marker_keys = [
                    marker for marker in _configured_marker_order
                    if any(num(get_path(s, ["totals", "reads_demux_by_marker", marker], 0)) > 0 for s in ordered_samples)
                ]
                if not _demux_marker_keys:
                    _demux_marker_keys = list(_configured_marker_order) or ["COI", "ITS2"]
                sample_order = _demux_marker_keys
                sample_colors = {m: marker_color(m) for m in _demux_marker_keys}
                sample_rows = []
                for sample in ordered_samples:
                    sample_rows.append({
                        "label": sample["label"],
                        "segments": [
                            {"label": m, "value": get_path(sample, ["totals", "reads_demux_by_marker", m], 0)}
                            for m in _demux_marker_keys
                        ],
                    })
                exports["run_demultiplex_reads_by_marker"] = add_export(
                    "run_demultiplex_reads_by_marker",
                    "Demultiplexed Reads by Marker",
                    "run_demultiplex_reads_by_marker.pdf",
                    sample_rows,
                    sample_order,
                    sample_colors,
                )
                for sample in ordered_samples:
                    chart_id = f"sample_{sample['sample_id']}_reads_per_barcode"
                    safe_sample = sanitize_filename(sample["sample_id"])
                    sample_rows = collect_sample_replicate_bars(
                        latest_round,
                        sample,
                        collapse_track_units=collapse_track_units,
                    )
                    if not sample_rows:
                        sample_rows = [{"label": sample["label"], "value": sample["totals"]["reads_demux"]}]
                    exports[chart_id] = add_vertical_export(
                        chart_id,
                        f"Reads per Barcode (sample): {sample['label']}",
                        f"samples/{safe_sample}_reads_per_barcode.pdf",
                        sample_rows,
                        "#2c6e49",
                    )
                    for source_key, source_title in (("otu", "OTUs"), ("consensus", "Consensus")):
                        for level in ("species", "genus", "family"):
                            items = build_sample_treemap_items(
                                latest_round,
                                sample["label"],
                                level,
                                source_key=source_key,
                                match_mode=(
                                    "track_sample_replicate"
                                    if CURRENT_GROUP_VIEW == "track_detail"
                                    else (
                                        "track_sample"
                                        if collapse_track_units and latest_round.get("identity_mode") == "track"
                                        else ("normalized" if collapse_track_units else "exact")
                                    )
                                ),
                            )
                            treemap_chart_id = f"sample_{sample['sample_id']}_{source_key}_treemap_{level}"
                            pdf_name = f"samples/{safe_sample}_{source_key}_treemap_{level}.pdf"
                            pdf_path = chart_dir / pdf_name
                            treemap_sig_path = _sig_root / f"{treemap_chart_id}.sig"
                            treemap_sig = compute_signature({"version": RENDER_EMBEDDED_SIGNATURE_VERSION,
                                                             "items": items})
                            if not (treemap_sig_path.exists() and pdf_path.exists()
                                    and read_signature(treemap_sig_path) == treemap_sig):
                                try:
                                    export_treemap_pdf(
                                        f"Taxonomic Treemap ({source_title}, {level.title()}): {sample['label']}",
                                        items,
                                        pdf_path,
                                    )
                                    if pdf_path.exists():
                                        atomic_write_text(treemap_sig_path, treemap_sig + "\n")
                                except Exception:
                                    pass
                            if pdf_path.exists():
                                exports[treemap_chart_id] = {"pdf_path": raw_pdf_path(pdf_name)}
        _run_sunburst_specs = []
        for _mk in marker_order_from_data(sorted_rounds):
            _slug = marker_slug(_mk)
            _suffix = _mk if _mk in ("COI", "ITS2") else _slug
            _run_sunburst_specs.extend([
                (f"run_otu_sunburst_{_suffix}", f"OTUs ({_mk})", "otu", _mk, is_supported_otu_assignment_row),
                (f"run_consensus_sunburst_{_suffix}", f"Consensus ({_mk})", "consensus", _mk, None),
                (f"run_frozen_otu_sunburst_{_suffix}", f"Frozen OTUs ({_mk})", "otu", _mk, lambda r: (r.get("frozen_otu_count") or 0) > 0),
                (f"run_consolidated_consensus_sunburst_{_suffix}", f"Consolidated Consensus ({_mk})", "consensus", _mk, lambda r: (r.get("consolidated_consensus_count") or 0) > 0),
            ])
        for _cid, _title, _src, _mk, _inc in _run_sunburst_specs:
            exports[_cid] = add_sunburst_export(_cid, _title, f"wedges scaled by assigned reads ({_mk})", _src, _mk, include_row=_inc)
    else:
        total_pages = max(1, (len(sorted_runs) + PAGE_SIZE - 1) // PAGE_SIZE)
        for chart_id in ("index_reads_fate", "index_informative_otu", "index_consensus_emitted"):
            exports[chart_id] = {"pages": []}
        for page_idx in range(total_pages):
            page_runs = sorted_runs[page_idx * PAGE_SIZE:(page_idx + 1) * PAGE_SIZE]
            run_like = [build_run_like(run) for run in page_runs]
            run_status_like = [build_run_status_read_fate_like(run) for run in page_runs]
            read_rows, read_order, read_colors = build_reads_fate_rows(run_status_like)
            pdf_name = f"index_reads_fate_page_{page_idx + 1}.pdf"
            _title = "Reads Fate (Current run status)"
            _sig_path = _sig_root / f"index_reads_fate_page_{page_idx + 1}.sig"
            _sig = compute_signature({"version": RENDER_EMBEDDED_SIGNATURE_VERSION,
                                      "title": _title, "rows": read_rows, "order": list(read_order),
                                      "colors": {k: v for k, v in read_colors.items() if k in read_order}})
            if not (_sig_path.exists() and (chart_dir / pdf_name).exists()
                    and read_signature(_sig_path) == _sig):
                export_stacked_bar_pdf(_title, read_rows, read_order, read_colors, chart_dir / pdf_name)
                if (chart_dir / pdf_name).exists():
                    atomic_write_text(_sig_path, _sig + "\n")
            exports["index_reads_fate"]["pages"].append({"page": page_idx, "pdf_path": raw_pdf_path(pdf_name)})

            active_rows, active_order, active_colors = build_generic_stacked_rows(
                run_like, _marker_taxon_specs(run_like, "otu", "active_by_marker_taxon")
            )
            pdf_name = f"index_informative_otu_page_{page_idx + 1}.pdf"
            _title = "Informative OTUs (assigned vs unassigned)"
            _sig_path = _sig_root / f"index_informative_otu_page_{page_idx + 1}.sig"
            _sig = compute_signature({"version": RENDER_EMBEDDED_SIGNATURE_VERSION,
                                      "title": _title, "rows": active_rows, "order": list(active_order),
                                      "colors": {k: v for k, v in active_colors.items() if k in active_order}})
            if not (_sig_path.exists() and (chart_dir / pdf_name).exists()
                    and read_signature(_sig_path) == _sig):
                export_stacked_bar_pdf(_title, active_rows, active_order, active_colors, chart_dir / pdf_name)
                if (chart_dir / pdf_name).exists():
                    atomic_write_text(_sig_path, _sig + "\n")
            exports["index_informative_otu"]["pages"].append({"page": page_idx, "pdf_path": raw_pdf_path(pdf_name)})

            cons_rows, cons_order, cons_colors = build_generic_stacked_rows(
                run_like, _marker_taxon_specs(run_like, "consensus", "emitted_by_marker_taxon")
            )
            pdf_name = f"index_consensus_emitted_page_{page_idx + 1}.pdf"
            _title = "Consensus Emitted (assigned vs unassigned)"
            _sig_path = _sig_root / f"index_consensus_emitted_page_{page_idx + 1}.sig"
            _sig = compute_signature({"version": RENDER_EMBEDDED_SIGNATURE_VERSION,
                                      "title": _title, "rows": cons_rows, "order": list(cons_order),
                                      "colors": {k: v for k, v in cons_colors.items() if k in cons_order}})
            if not (_sig_path.exists() and (chart_dir / pdf_name).exists()
                    and read_signature(_sig_path) == _sig):
                export_stacked_bar_pdf(_title, cons_rows, cons_order, cons_colors, chart_dir / pdf_name)
                if (chart_dir / pdf_name).exists():
                    atomic_write_text(_sig_path, _sig + "\n")
            exports["index_consensus_emitted"]["pages"].append({"page": page_idx, "pdf_path": raw_pdf_path(pdf_name)})
    return exports


def map_chart_export_paths(chart_exports, run_id, url_prefix):
    for entry in chart_exports.values():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("pdf_path"), str):
            if run_id and not url_prefix:
                entry["pdf_path"] = relativize_run_url(entry["pdf_path"], run_id)
            elif url_prefix:
                entry["pdf_path"] = prefix_url(entry["pdf_path"], url_prefix)
        pages = entry.get("pages")
        if isinstance(pages, list):
            for page in pages:
                if not isinstance(page, dict) or not isinstance(page.get("pdf_path"), str):
                    continue
                if run_id and not url_prefix:
                    page["pdf_path"] = relativize_run_url(page["pdf_path"], run_id)
                elif url_prefix:
                    page["pdf_path"] = prefix_url(page["pdf_path"], url_prefix)


def write_chart_tsvs(sorted_rounds, tsv_dir, run_id=None, sig_root=None, report_html_name="report.html"):
    """Write TSV data tables for every chart in the run report (best-effort; errors are silently skipped)."""
    tsv_dir.mkdir(parents=True, exist_ok=True)
    latest_round = sorted_rounds[-1] if sorted_rounds else {}
    _scope = run_id if run_id else "index"

    def _write_tsv(path, header, rows, na_token=""):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\t".join(str(c) for c in header) + "\n")
            for row in rows:
                fh.write("\t".join(na_token if v is None else str(v) for v in row) + "\n")

    def _write_tsv_with_sig(path, header, rows, na_token=""):
        if sig_root is not None:
            tsv_name = path.name
            sig_path = sig_root / "figures" / _scope / f"{tsv_name}.sig"
            sig = compute_signature({"version": RENDER_FIGURES_SIGNATURE_VERSION,
                                     "header": list(header), "rows": [list(r) for r in rows], "na_token": na_token})
            if sig_path.exists() and path.exists() and read_signature(sig_path) == sig:
                return
            _write_tsv(path, header, rows, na_token=na_token)
            if path.exists():
                atomic_write_text(sig_path, sig + "\n")
        else:
            _write_tsv(path, header, rows, na_token=na_token)

    def stacked_to_tsv(rows, order, path, na_token=""):
        data_rows = []
        for r in rows:
            seg_map = {s["label"]: s["value"] for s in r.get("segments", [])}
            data_rows.append([r["label"]] + [seg_map.get(lbl) for lbl in order])
        _write_tsv_with_sig(path, ["round"] + list(order), data_rows, na_token=na_token)

    # Per-round stacked-bar charts
    try:
        rr, ro, _ = build_reads_fate_rows(sorted_rounds)
        stacked_to_tsv(rr, ro, tsv_dir / "reads_fate_per_round.tsv", na_token="N/A")
    except Exception:
        pass

    try:
        or_, oo, _ = build_generic_stacked_rows(sorted_rounds, [
            {"label": "Informative dynamic", "path": ["otu", "canonical", "informative_dynamic"], "color": ""},
            {"label": "Prune candidates",    "path": ["otu", "pruned", "prune_candidates"],       "color": ""},
            {"label": "Size-streak pruned",  "path": ["otu", "pruned", "size_streak"],            "color": ""},
            {"label": "BLAST-unassigned",    "path": ["otu", "pruned", "blast_unassigned"],       "color": ""},
        ])
        stacked_to_tsv(or_, oo, tsv_dir / "otu_fate_per_round.tsv")
    except Exception:
        pass

    for _fname, _src, _sub in [
        ("otu_active_by_marker_per_round.tsv",        "otu",       "active_by_marker_taxon"),
        ("consensus_emitted_by_marker_per_round.tsv", "consensus", "emitted_by_marker_taxon"),
    ]:
        try:
            specs = _marker_taxon_specs(sorted_rounds, _src, _sub)
            r2, o2, _ = build_generic_stacked_rows(sorted_rounds, specs)
            stacked_to_tsv(r2, o2, tsv_dir / _fname)
        except Exception:
            pass

    # Assignment tables from the latest round
    def write_assignments(src_key, level, count_field, fname):
        rows = []
        if isinstance(latest_round, dict):
            src = latest_round.get(src_key)
            if isinstance(src, dict):
                abl = src.get("assignments_by_level")
                level_rows = abl.get(level) if isinstance(abl, dict) else None
                if isinstance(level_rows, list):
                    rows = [r for r in level_rows
                            if isinstance(r, dict)
                            and (not count_field or num_any(r.get(count_field, 0)) > 0)]
        if not rows:
            return
        cols = list(dict.fromkeys(k for r in rows for k in r))
        _write_tsv_with_sig(tsv_dir / fname, cols, [[r.get(c, "") for c in cols] for r in rows])

    for _lvl in ("species", "genus", "family"):
        write_assignments("otu",       _lvl, None,                        f"otu_assignments_{_lvl}.tsv")
        write_assignments("consensus", _lvl, None,                        f"consensus_assignments_{_lvl}.tsv")
    write_assignments("otu",       "species", "frozen_otu_count",              "frozen_otu_assignments_species.tsv")
    write_assignments("consensus", "species", "consolidated_consensus_count",  "consolidated_consensus_assignments_species.tsv")
    write_assignments("otu",       "species", None,                            "otu_assignments_by_sample_species.tsv")
    write_assignments("consensus", "species", None,                            "consensus_assignments_by_sample_species.tsv")

    # Write README.html describing the figures directory (skip if already present)
    readme_path = tsv_dir / "README.html"
    if not readme_path.exists():
        run_label = run_id or (latest_round.get("run_id") if isinstance(latest_round, dict) else None) or "this run"
        rl = html.escape(str(run_label))
        readme_path.write_text(
            "<!doctype html>\n<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
            f"<title>Report Figures \u2014 {rl}</title>\n"
            "<style>\n"
            "*{box-sizing:border-box}\n"
            "body{font-family:\"IBM Plex Sans\",\"Segoe UI\",Arial,sans-serif;background:linear-gradient(180deg,#f7f8f4 0%,#eef1e8 100%);color:#1b2417;margin:0;min-height:100vh}\n"
            ".wrap{max-width:860px;margin:0 auto;padding:32px 24px}\n"
            "header{border-bottom:2px solid #cfd7c9;padding-bottom:18px;margin-bottom:26px}\n"
            ".brand{font-size:.78rem;color:#8a9b85;letter-spacing:.04em;text-transform:uppercase;margin:0 0 6px}\n"
            "h1{color:#2c6e49;font-size:1.55rem;margin:0 0 12px}\n"
            ".browse{display:inline-block;color:#3b7f5f;text-decoration:none;border:1px solid #cfd7c9;padding:6px 16px;border-radius:6px;background:#fff;font-size:14px;font-weight:500;margin-bottom:12px}\n"
            ".browse:hover{text-decoration:underline;background:#f4f8f2}\n"
            ".subtitle{color:#4d5b48;font-size:.92rem;margin:0}\n"
            "h2{color:#2c6e49;font-size:1.05rem;border-bottom:1px solid #e4e9e0;padding-bottom:3px;margin:26px 0 10px}\n"
            "table{width:100%;border-collapse:collapse;font-size:.875rem;margin-bottom:16px}\n"
            "th{text-align:left;background:#e8f0e4;padding:8px 12px;color:#2a3624;border-bottom:2px solid #cad2bf;font-size:.82rem;text-transform:uppercase;letter-spacing:.03em}\n"
            "td{padding:8px 12px;border-bottom:1px solid #edf1ea;vertical-align:top;line-height:1.45}\n"
            "tr:last-child td{border-bottom:none}\n"
            "td:first-child{font-family:\"IBM Plex Mono\",\"Courier New\",monospace;white-space:nowrap;color:#2c6e49;font-size:.83rem}\n"
            ".note{background:#f0f4ec;border-left:3px solid #3b7f5f;padding:12px 16px;border-radius:0 4px 4px 0;font-size:.875rem;margin:16px 0}\n"
            "footer{margin-top:40px;font-size:.78rem;color:#8a9b85;border-top:1px solid #dde5d7;padding-top:10px}\n"
            "</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
            f"  <header>\n    <p class=\"brand\">RTBioScan &middot; Run {rl}</p>\n"
            "    <h1>Report Figures</h1>\n"
            "    <a class=\"browse\" href=\"../report.html\">&#128202; Run Report</a>\n"
            "    <a class=\"browse\" href=\"./\">&#128193; Browse directory</a>\n"
            f"    <p class=\"subtitle\">Tab-separated data tables backing every chart in <a href=\"../report.html\">Run Info</a>. Run: <strong>{rl}</strong>.</p>\n"
            "  </header>\n\n  <main>\n"
            "    <h2>Per-round time-series charts</h2>\n"
            "    <table>\n      <thead><tr><th>File</th><th>Chart</th></tr></thead>\n      <tbody>\n"
            "        <tr><td>reads_fate_per_round.tsv</td><td>Read fate breakdown per round: BLAST-assigned, off-target, unassigned, on-target not demultiplexed, and more.</td></tr>\n"
            "        <tr><td>otu_fate_per_round.tsv</td><td>OTU fate counts per round: informative dynamic, prune candidates, size-streak pruned, BLAST-unassigned.</td></tr>\n"
            "        <tr><td>otu_active_by_marker_per_round.tsv</td><td>Active OTU counts split by marker (COI/ITS2) and assignment status per round.</td></tr>\n"
            "        <tr><td>consensus_emitted_by_marker_per_round.tsv</td><td>Consensus sequences emitted per marker and assignment status per round.</td></tr>\n"
            "      </tbody>\n    </table>\n\n"
            "    <h2>Assignment tables (latest round)</h2>\n"
            "    <table>\n      <thead><tr><th>File</th><th>Contents</th></tr></thead>\n      <tbody>\n"
            "        <tr><td>otu_assignments_species.tsv</td><td>All OTU taxonomy assignments at species level.</td></tr>\n"
            "        <tr><td>otu_assignments_genus.tsv</td><td>All OTU taxonomy assignments at genus level.</td></tr>\n"
            "        <tr><td>otu_assignments_family.tsv</td><td>All OTU taxonomy assignments at family level.</td></tr>\n"
            "        <tr><td>consensus_assignments_species.tsv</td><td>All consensus taxonomy assignments at species level.</td></tr>\n"
            "        <tr><td>consensus_assignments_genus.tsv</td><td>All consensus taxonomy assignments at genus level.</td></tr>\n"
            "        <tr><td>consensus_assignments_family.tsv</td><td>All consensus taxonomy assignments at family level.</td></tr>\n"
            "        <tr><td>frozen_otu_assignments_species.tsv</td><td>Species-level assignments for <strong>frozen OTUs</strong> only (stable, high-confidence clusters).</td></tr>\n"
            "        <tr><td>consolidated_consensus_assignments_species.tsv</td><td>Species-level assignments for <strong>consolidated consensus</strong> sequences only.</td></tr>\n"
            "        <tr><td>otu_assignments_by_sample_species.tsv</td><td>OTU species assignments across all samples (latest round).</td></tr>\n"
            "        <tr><td>consensus_assignments_by_sample_species.tsv</td><td>Consensus species assignments across all samples (latest round).</td></tr>\n"
            "      </tbody>\n    </table>\n\n"
            "    <div class=\"note\">All files are <strong>tab-separated</strong> with a header row. "
            "Empty cells indicate missing or not-applicable values. "
            "Files are regenerated each round and always reflect the latest completed round. "
            "Compatible with R (<code>read.table</code>), Python (<code>pandas.read_csv(..., sep='\\t')</code>), Excel, and any TSV-aware tool.</div>\n"
            "  </main>\n\n"
            f"  <footer>Generated by RTBioScan &middot; run {rl}</footer>\n"
            "</div>\n</body>\n</html>\n",
            encoding="utf-8",
        )


def main():
    ap = argparse.ArgumentParser(description="Render RTBioScan HTML report from JSONL history")
    ap.add_argument("--history", required=True, help="Path to report_history.jsonl")
    ap.add_argument("--template", required=True, help="Path to HTML template")
    ap.add_argument("--css", required=True, help="Path to CSS asset")
    ap.add_argument("--js", required=True, help="Path to JS asset")
    ap.add_argument("--out", required=True, help="Output HTML path")
    ap.add_argument("--state-out", default="", help="Output state JSON path (default: alongside --out as report_state.json)")
    ap.add_argument("--schema-version", default="1.2", help="Rendered report schema version")
    ap.add_argument("--auto-refresh-enabled", default="1", help="Enable browser auto-refresh polling (1/0)")
    ap.add_argument("--auto-refresh-seconds", type=int, default=15, help="Auto-refresh polling interval in seconds")
    ap.add_argument("--state-url", default="report_state.json", help="Relative URL used by browser to poll state")
    ap.add_argument("--group-view", default="sample", choices=["sample", "replicate", "track_detail"], help="Run grouping view to render")
    ap.add_argument("--figures-dir-name", default=DEFAULT_FIGURES_DIR_NAME, help="Directory name for generated TSV/chart assets")
    ap.add_argument("--report-assets-dir-name", default=DEFAULT_REPORT_ASSETS_DIR_NAME, help="Directory name for generated report assets")
    ap.add_argument("--run-index", default="", help="Optional runs_index.jsonl path")
    ap.add_argument("--run-id-filter", default="", help="Optional run_id filter for per-run reports")
    ap.add_argument("--url-prefix", default="", help="Optional URL prefix for assets/links (e.g. /)")
    ap.add_argument("--pod5-dir-url", default="", help="Relative URL to the run pod5 directory")
    ap.add_argument("--state-dir-url", default="", help="Relative URL to the run state directory")
    ap.add_argument("--figures-dir-url", default="", help="Relative URL to the run figures directory")
    ap.add_argument("--sample-info-url", default="", help="Relative URL to the run sample info directory")
    ap.add_argument("--run-config-url", default="", help="Relative URL to the run config file")
    ap.add_argument("--sample-plot-max", type=int, default=-1, help="Max generated per-sample HTML figure groups (-1 = unlimited, 0 = disabled)")
    args = ap.parse_args()
    global CURRENT_REPORT_ASSETS_DIR_NAME, CURRENT_FIGURES_DIR_NAME, CURRENT_GROUP_VIEW
    CURRENT_REPORT_ASSETS_DIR_NAME = args.report_assets_dir_name or DEFAULT_REPORT_ASSETS_DIR_NAME
    CURRENT_FIGURES_DIR_NAME = args.figures_dir_name or DEFAULT_FIGURES_DIR_NAME
    CURRENT_GROUP_VIEW = args.group_view or "sample"

    history_path = Path(args.history)
    template_path = Path(args.template)
    css_path = Path(args.css)
    js_path = Path(args.js)
    out_path = Path(args.out)
    if args.state_out:
        state_path = Path(args.state_out)
    else:
        state_path = out_path.with_name("report_state.json")

    auto_refresh_enabled = str(args.auto_refresh_enabled).strip().lower() in {"1", "true", "yes", "on"}
    auto_refresh_seconds = args.auto_refresh_seconds if args.auto_refresh_seconds and args.auto_refresh_seconds > 0 else 15

    history_bytes = b""
    if history_path.exists():
        history_bytes = history_path.read_bytes()
    report_revision = hashlib.sha256(history_bytes).hexdigest()

    rounds, parse_warnings = load_history(history_path)
    run_index = []
    run_warnings = []
    if args.run_index:
        run_index_path = Path(args.run_index)
        run_index, run_warnings = load_run_index(run_index_path)
    generated_at_utc = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.run_id_filter:
        rounds = [r for r in rounds if r.get("run_id") == args.run_id_filter]

    sorted_rounds = sort_rounds(rounds)
    latest_round = sorted_rounds[-1] if sorted_rounds else {}
    sorted_runs = sort_runs(run_index)
    url_prefix = args.url_prefix or ""
    if url_prefix and not url_prefix.endswith("/"):
        url_prefix = url_prefix + "/"

    chart_exports = {}
    try:
        chart_exports = build_chart_exports(sorted_rounds, sorted_runs, out_path, args.run_id_filter)
    except Exception as exc:
        parse_warnings.append(f"chart_pdf_export_failed:{exc.__class__.__name__}")
    run_start_epoch = None
    if args.run_id_filter:
        for _run in sorted_runs:
            if isinstance(_run, dict) and _run.get("run_id") == args.run_id_filter:
                _started = _run.get("started_utc")
                if isinstance(_started, str) and _started:
                    try:
                        _parsed = dt.datetime.fromisoformat(_started.replace("Z", "+00:00"))
                        if _parsed.tzinfo is None:
                            _parsed = _parsed.replace(tzinfo=dt.timezone.utc)
                        run_start_epoch = int(_parsed.timestamp())
                    except Exception:
                        pass
                break
    try:
        append_generated_sample_figures(
            sorted_rounds,
            latest_round,
            out_path,
            args.run_id_filter,
            run_start_epoch=run_start_epoch,
            sample_plot_max=(args.sample_plot_max if args.sample_plot_max >= 0 else None),
        )
    except Exception as exc:
        parse_warnings.append(f"sample_plot_export_failed:{exc.__class__.__name__}")
    if args.run_id_filter and isinstance(latest_round, dict):
        consensus = latest_round.get("consensus")
        if not isinstance(consensus, dict):
            consensus = {}
            latest_round["consensus"] = consensus
        all_sequence_rows = collect_consensus_sequence_rows(
            args.run_id_filter,
            out_path,
            latest_round.get("state_id") if isinstance(latest_round, dict) else None,
        )
        consensus["sequence_rows"] = all_sequence_rows
        consensus["consolidated_sequence_rows"] = [row for row in all_sequence_rows if row.get("is_consolidated")]
        # Attach per-replicate OTU read counts to each sequence row.
        # otu_replicate_reads is emitted by report_round_json.pl in collapse mode
        # and keyed by OTU_id -> [{label, count}, ...].
        _otu_rep_reads = latest_round.get("otu", {}).get("replicate_reads") or {}
        if _otu_rep_reads and isinstance(all_sequence_rows, list):
            for _seq_row in all_sequence_rows:
                _ok = _seq_row.get("otu_key", "")
                _smp = _seq_row.get("sample", "")
                _seq_row["replicate_reads"] = (
                    _otu_rep_reads.get(_ok, {}).get(_smp, []) if _ok else []
                )
    backfill_figure_pdfs(sorted_rounds, out_path, args.run_id_filter)
    map_figure_paths(sorted_rounds, args.run_id_filter, url_prefix)
    map_chart_export_paths(chart_exports, args.run_id_filter, url_prefix)
    if args.run_id_filter:
        try:
            write_chart_tsvs(
                sorted_rounds,
                figures_dir(out_path),
                run_id=args.run_id_filter,
                sig_root=report_assets_dir(out_path) / ".private_signatures",
                report_html_name=out_path.name,
            )
        except Exception as exc:
            parse_warnings.append(f"chart_tsv_export_failed:{exc.__class__.__name__}")
    if url_prefix:
        for run in sorted_runs:
            if isinstance(run, dict):
                for key in ("report_rel_path", "report_url"):
                    if isinstance(run.get(key), str):
                        run[key] = prefix_url(run.get(key), url_prefix)
                report_views = run.get("report_views")
                if isinstance(report_views, list):
                    for view in report_views:
                        if not isinstance(view, dict):
                            continue
                        for key in ("report_rel_path", "report_url"):
                            if isinstance(view.get(key), str):
                                view[key] = prefix_url(view.get(key), url_prefix)
    elif args.run_id_filter:
        for run in sorted_runs:
            if not isinstance(run, dict):
                continue
            report_views = run.get("report_views")
            if isinstance(report_views, list):
                for view in report_views:
                    if not isinstance(view, dict):
                        continue
                    for key in ("report_rel_path", "report_url"):
                        if isinstance(view.get(key), str):
                            view[key] = relativize_run_url(view.get(key), args.run_id_filter)

    current_run_entry = None
    if args.run_id_filter:
        for _run in sorted_runs:
            if isinstance(_run, dict) and _run.get("run_id") == args.run_id_filter:
                current_run_entry = _run
                break

    run_identity_mode = "collapse"
    if isinstance(latest_round, dict) and latest_round.get("identity_mode") == "track":
        run_identity_mode = "track"
    elif isinstance(current_run_entry, dict) and current_run_entry.get("identity_mode") == "track":
        run_identity_mode = "track"

    def view_label(view_id, identity_mode):
        if view_id == "sample":
            return "Run Info" if identity_mode == "track" else "Run Report"
        if view_id == "replicate":
            return "Primer Comparison"
        if view_id == "track_detail":
            return "Replicate Comparison"
        return "Run Report"

    current_report_view = args.group_view if args.run_id_filter else ""
    current_report_view_label = view_label(current_report_view, run_identity_mode) if current_report_view else ""
    report_view_links = []
    if args.run_id_filter and isinstance(current_run_entry, dict):
        raw_views = current_run_entry.get("report_views")
        if isinstance(raw_views, list):
            for raw_view in raw_views:
                if not isinstance(raw_view, dict):
                    continue
                view_id = str(raw_view.get("view_id") or raw_view.get("id") or "").strip()
                if not view_id:
                    continue
                href = raw_view.get("report_rel_path") or raw_view.get("report_url") or ""
                report_view_links.append({
                    "view_id": view_id,
                    "label": raw_view.get("label") or view_label(view_id, run_identity_mode),
                    "href": href,
                    "is_primary": bool(raw_view.get("is_primary")),
                    "is_active": view_id == current_report_view,
                })
    if args.run_id_filter and run_identity_mode == "track" and not report_view_links:
        report_view_links = [{
            "view_id": current_report_view or "sample",
            "label": current_report_view_label or "Run Info",
            "href": prefix_url(out_path.name, url_prefix),
            "is_primary": True,
            "is_active": True,
        }]
    figures_payload = latest_round.get("figures", []) if args.run_id_filter else []

    payload = {
        "rounds": sorted_rounds,
        "history_count": len(sorted_rounds),
        "generated_at_utc": generated_at_utc,
        "parse_warnings": parse_warnings + run_warnings,
        "figures": figures_payload,
        "run_index": sorted_runs,
        "chart_exports": chart_exports,
        "view_scope": "run" if args.run_id_filter else "index",
        "report_view": current_report_view,
        "report_view_links": report_view_links,
    }
    report_meta = {
        "schema_version": args.schema_version,
        "generated_at_utc": generated_at_utc,
        "report_revision": report_revision,
        "auto_refresh_enabled": auto_refresh_enabled,
        "refresh_interval_sec": auto_refresh_seconds,
        "state_url": args.state_url,
        "pod5_dir_url": args.pod5_dir_url,
        "state_dir_url": args.state_dir_url,
        "figures_dir_url": args.figures_dir_url,
        "sample_info_url": args.sample_info_url,
        "run_config_url": args.run_config_url,
        "report_view": current_report_view,
        "report_view_label": current_report_view_label,
        "report_view_links": report_view_links,
    }
    report_state = {
        "schema_version": args.schema_version,
        "generated_at_utc": generated_at_utc,
        "report_revision": report_revision,
    }

    def format_human_utc(ts):
        if not isinstance(ts, str) or not ts:
            return ts
        try:
            if ts.endswith("Z"):
                parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                parsed = dt.datetime.fromisoformat(ts)
            parsed = parsed.astimezone(dt.timezone.utc)
            return parsed.strftime("%b %d, %Y %H:%M:%S UTC")
        except Exception:
            return ts

    html_doc = read_text(template_path)
    html_doc = html_doc.replace("__REPORT_STYLE__", read_text(css_path))
    html_doc = html_doc.replace("__REPORT_SCRIPT__", read_text(js_path))
    if args.run_id_filter:
        if run_identity_mode == "track" and current_report_view == "sample":
            title = f"RTBioScan Run Info (run {args.run_id_filter})"
        elif run_identity_mode == "track" and current_report_view == "replicate":
            title = f"RTBioScan Primer Comparison (run {args.run_id_filter})"
        elif run_identity_mode == "track" and current_report_view == "track_detail":
            title = f"RTBioScan Replicate Comparison (run {args.run_id_filter})"
        else:
            title = f"RTBioScan Run Report (run {args.run_id_filter})"
    else:
        title = "RTBioScan Runs Status"
    html_doc = html_doc.replace("__REPORT_TITLE__", html.escape(title))
    html_doc = html_doc.replace("__GENERATED_AT__", html.escape(format_human_utc(payload["generated_at_utc"])))
    html_doc = html_doc.replace("__HISTORY_COUNT__", str(payload["history_count"]))
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    payload_json = payload_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    report_meta_json = json.dumps(report_meta, ensure_ascii=True, separators=(",", ":"))
    report_meta_json = report_meta_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html_doc = html_doc.replace("__PAYLOAD_JSON__", payload_json)
    html_doc = html_doc.replace("__REPORT_META_JSON__", report_meta_json)

    atomic_write_text(out_path, html_doc)

    # Best effort sidecar write: warn, but keep a valid HTML report if this fails.
    try:
        state_json = json.dumps(report_state, ensure_ascii=True, separators=(",", ":")) + "\n"
        atomic_write_text(state_path, state_json)
    except Exception as exc:
        print(f"WARN: failed to write report state sidecar: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
