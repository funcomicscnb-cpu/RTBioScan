#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <blast_report> <samples_file> <out_dir>" >&2
  exit 2
fi

blast_report="$1"
samples_file="$2"
out_dir="$3"
identity_mode="$(printf '%s' "${RTBIOSCAN_EFFECTIVE_IDENTITY_MODE:-collapse}" | tr '[:upper:]' '[:lower:]')"
track_active_units="${RTBIOSCAN_TRACK_ACTIVE_UNITS:-}"
track_identity_tsv="${RTBIOSCAN_TRACK_IDENTITY_TSV:-}"
target_tokens_raw="${RTBIOSCAN_TARGET_TOKENS:-}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/lib/adapter_utils.sh"

if [ ! -f "$blast_report" ]; then
  echo "ERROR: blast report is not a regular file: $blast_report" >&2
  exit 1
fi

if [ ! -r "$blast_report" ]; then
  echo "ERROR: blast report is not readable: $blast_report" >&2
  exit 1
fi

if [ ! -f "$samples_file" ]; then
  echo "ERROR: samples file is not a regular file: $samples_file" >&2
  exit 1
fi

if [ ! -r "$samples_file" ]; then
  echo "ERROR: samples file is not readable: $samples_file" >&2
  exit 1
fi

mkdir -p "$out_dir"

if [ "$identity_mode" = "track" ]; then
  if [ -z "$track_active_units" ]; then
    echo "ERROR: RTBIOSCAN_TRACK_ACTIVE_UNITS is required in track mode" >&2
    exit 1
  fi

  if [ -z "$track_identity_tsv" ]; then
    echo "ERROR: RTBIOSCAN_TRACK_IDENTITY_TSV is required in track mode" >&2
    exit 1
  fi

  if [ ! -f "$track_active_units" ]; then
    echo "ERROR: track active units file is not a regular file: $track_active_units" >&2
    exit 1
  fi

  if [ ! -r "$track_active_units" ]; then
    echo "ERROR: track active units file is not readable: $track_active_units" >&2
    exit 1
  fi

  if [ ! -f "$track_identity_tsv" ]; then
    echo "ERROR: track identity file is not a regular file: $track_identity_tsv" >&2
    exit 1
  fi

  if [ ! -r "$track_identity_tsv" ]; then
    echo "ERROR: track identity file is not readable: $track_identity_tsv" >&2
    exit 1
  fi

  local_roster_map="$out_dir/.sample_map.tsv"
  canonical_active_map="$out_dir/.track_active_units.tsv"
  : > "$local_roster_map"
  : > "$canonical_active_map"

  while IFS= read -r sample || [ -n "$sample" ]; do
    sample="$(trim_text_sh "$sample")"
    [ -n "$sample" ] || continue
    sample_out="$out_dir/${sample}.blast.tsv"
    : > "$sample_out"
    printf '%s\t%s\n' "$sample" "$sample_out" >> "$local_roster_map"
  done < "$samples_file"

  while IFS= read -r sample || [ -n "$sample" ]; do
    sample="$(trim_text_sh "$sample")"
    [ -n "$sample" ] || continue
    if is_no_adapter_adapter_sh "$sample"; then
      echo "ERROR: track_active_units.txt must not contain no_adapter entry: $sample" >&2
      exit 1
    fi
    printf '%s\n' "$sample" >> "$canonical_active_map"
  done < "$track_active_units"

  awk -v TARGETS_RAW="$target_tokens_raw" -v IDENTITY="$track_identity_tsv" -v ACTIVE_MAP="$canonical_active_map" -v LOCAL_MAP="$local_roster_map" '
    function trim(s) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      return s
    }
    function fatal(msg) {
      print msg > "/dev/stderr"
      exit 1
    }
    function normalize_marker(marker, out) {
      out = toupper(trim(marker))
      if (out == "ITS" || out == "ITS1" || out == "ITS2") out = "ITS2"
      return out
    }
    function is_supported_marker(marker, normalized, i, token) {
      normalized = normalize_marker(marker)
      if (normalized == "") return 0
      if (normalized == "COI" || normalized == "ITS2") return 1
      for (i = 1; i <= target_count; i++) {
        token = normalize_marker(target_tokens[i])
        if (token != "" && normalized == token) return 1
      }
      return 0
    }
    function is_no_adapter(adapter, a) {
      a = tolower(trim(adapter))
      return (a ~ /^no_adapter(_[0-9]+)?$/)
    }
    function extract_adapter(read_id, rest) {
      if (match(read_id, /adapter=[^[:space:]|]+/)) {
        rest = substr(read_id, RSTART + 8, RLENGTH - 8)
        return rest
      }
      return ""
    }
    function extract_marker(read_id,    parts, n) {
      n = split(read_id, parts, /\|/)
      if (n >= 2) return normalize_marker(parts[2])
      return ""
    }
    BEGIN {
      FS = OFS = "\t"
      target_count = split(TARGETS_RAW, target_tokens, /\|/)
    }
    FILENAME == IDENTITY {
      if (FNR == 1) {
        for (i = 1; i <= NF; i++) {
          if ($i == "unit_id_track") unit_col = i
          else if ($i == "marker_id") marker_col = i
        }
        if (!unit_col) fatal("ERROR: track_identity.tsv is missing unit_id_track header")
        if (!marker_col) fatal("ERROR: track_identity.tsv is missing marker_id header")
        next
      }
      unit_id = trim((unit_col <= NF) ? $unit_col : "")
      marker = normalize_marker((marker_col <= NF) ? $marker_col : "")
      if (unit_id == "") fatal("ERROR: track_identity.tsv contains empty unit_id_track")
      if (marker == "") fatal("ERROR: track_identity.tsv contains empty marker_id for unit_id_track: " unit_id)
      if ((unit_id in unit_marker) && unit_marker[unit_id] != marker) {
        fatal("ERROR: track_identity.tsv maps unit_id_track to multiple markers: " unit_id)
      }
      unit_marker[unit_id] = marker
      next
    }
    FILENAME == ACTIVE_MAP {
      if ($1 != "") {
        active[$1] = 1
      }
      next
    }
    FILENAME == LOCAL_MAP {
      if (NF >= 2 && $1 != "" && $2 != "") {
        out[$1] = $2
      }
      next
    }
    FNR == 1 {
      if ($1 == "read_id" || $1 == "seq_id" || $1 == "long_read_id") {
        has_header = 1
        for (i = 1; i <= NF; i++) {
          key = tolower(trim($i))
          if (key == "barcode_by_homology" || key == "marker" || key == "otu_marker" || key == "target") {
            blast_marker_col = i
          }
        }
        next
      }
      has_header = 0
    }
    {
      if ($0 ~ /^[[:space:]]*$/) next
      read_id = $1
      if (read_id ~ /^#/ || read_id == "read_id" || read_id == "seq_id" || read_id == "long_read_id") next
      adapter = extract_adapter(read_id)
      if (adapter == "") {
        print "ERROR: track mode requires adapter= token in blast row: " read_id > "/dev/stderr"
        exit 1
      }
      if (is_no_adapter(adapter)) {
        if (!("no_adapter" in out)) {
          print "ERROR: track mode observed adapter=no_adapter row but local consensus roster is missing no_adapter" > "/dev/stderr"
          exit 1
        }
        print $0 >> out["no_adapter"]
        next
      }
      if (!(adapter in active)) {
        print "ERROR: track mode observed adapter not present in track_active_units.txt: " adapter > "/dev/stderr"
        exit 1
      }
      if (!(adapter in unit_marker)) {
        fatal("ERROR: track mode observed adapter not present in track_identity.tsv: " adapter)
      }
      row_marker = ""
      if (has_header && blast_marker_col && blast_marker_col <= NF) {
        row_marker = normalize_marker($blast_marker_col)
        if (!is_supported_marker(row_marker)) {
          row_marker = ""
        }
      }
      if (row_marker == "") {
        row_marker = extract_marker(read_id)
      }
      expected_marker = unit_marker[adapter]
      if (row_marker != "" && expected_marker != "" && row_marker != expected_marker) {
        fatal("ERROR: track mode observed marker/unit mismatch: adapter=" adapter " expected_marker=" expected_marker " row_marker=" row_marker " read_id=" read_id)
      }
      if (!(adapter in out)) {
        print "ERROR: track mode adapter missing from local consensus roster: " adapter > "/dev/stderr"
        exit 1
      }
      print $0 >> out[adapter]
    }
  ' "$track_identity_tsv" "$canonical_active_map" "$local_roster_map" "$blast_report"
  exit 0
fi

sample_map="$out_dir/.sample_map.tsv"
target_tokens_raw="${RTBIOSCAN_TARGET_TOKENS:-}"
: > "$sample_map"

while IFS= read -r sample || [ -n "$sample" ]; do
  sample="$(trim_text_sh "$sample")"
  [ -n "$sample" ] || continue
  normalized="$(normalize_sample_base_sh "$sample" 2>/dev/null || true)"
  if [ -z "$normalized" ]; then
    continue
  fi
  sample_out="$out_dir/${sample}.blast.tsv"
  : > "$sample_out"
  printf '%s\t%s\n' "$normalized" "$sample_out" >> "$sample_map"
done < "$samples_file"

awk -v TARGETS_RAW="$target_tokens_raw" '
  function trim(s) {
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
    return s
  }
  function is_no_adapter(adapter, a) {
    a = tolower(trim(adapter))
    return (a ~ /^no_adapter(_[0-9]+)?$/)
  }
  function normalize_sample_base(sample, s) {
    n_targets = split(TARGETS_RAW, targets, /\|/)
    s = trim(sample)
    if (s == "") return ""
    if (is_no_adapter(s)) return "no_adapter"
    gsub(/_[0-9]+$/, "", s)
    s_lc = tolower(s)
    for (i = 1; i <= n_targets; i++) {
      marker = trim(targets[i])
      if (marker == "") continue
      marker_lc = tolower(marker)
      if (s_lc ~ "_" marker_lc "[0-9]*$") {
        sub(/_[^_]+$/, "", s)
        s_lc = tolower(s)
        break
      }
    }
    if (s_lc ~ /_(coi|its)[0-9]*$/) {
      sub(/_[^_]+$/, "", s)
    }
    gsub(/_[0-9]+_/, "_", s)
    s = trim(s)
    return s
  }
  function extract_adapter(read_id, rest) {
    if (match(read_id, /adapter=[^[:space:]|]+/)) {
      rest = substr(read_id, RSTART + 8, RLENGTH - 8)
      return rest
    }
    return ""
  }
  BEGIN {
    FS = OFS = "\t"
  }
  NR == FNR {
    if (NF >= 2 && $1 != "" && $2 != "") {
      out[$1] = $2
    }
    next
  }
  {
    if ($0 == "") next
    read_id = $1
    if (read_id ~ /^#/ || read_id == "read_id" || read_id == "seq_id" || read_id == "long_read_id") next
    adapter = extract_adapter(read_id)
    if (adapter == "") next
    normalized = normalize_sample_base(adapter)
    if (normalized != "" && (normalized in out)) {
      print $0 >> out[normalized]
    }
  }
' "$sample_map" "$blast_report"
