#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
  echo "usage: $0 <adapter_marker_map.tsv> <target_marker> <fastq_file> [label]" >&2
  exit 2
fi

adapter_marker_map="$1"
target_marker="$2"
fastq_file="$3"
label="${4:-$(basename "$fastq_file")}"

if [ ! -f "$adapter_marker_map" ]; then
  echo "ERROR: adapter marker map is not a regular file: $adapter_marker_map" >&2
  exit 1
fi

if [ ! -r "$adapter_marker_map" ]; then
  echo "ERROR: adapter marker map is not readable: $adapter_marker_map" >&2
  exit 1
fi

if [ ! -f "$fastq_file" ]; then
  echo "ERROR: FASTQ file is not a regular file: $fastq_file" >&2
  exit 1
fi

if [ ! -r "$fastq_file" ]; then
  echo "ERROR: FASTQ file is not readable: $fastq_file" >&2
  exit 1
fi

tmp_fastq="${fastq_file}.tmp.$$"

awk -v MAP="$adapter_marker_map" -v TARGET="$target_marker" -v LABEL="$label" '
  function trim(s) {
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
    return s
  }
  function normalize_marker(marker, out) {
    out = toupper(trim(marker))
    if (out == "ITS" || out == "ITS1" || out == "ITS2") out = "ITS2"
    return out
  }
  function extract_adapter(header, rest) {
    if (match(header, /adapter=[^[:space:]|]+/)) {
      rest = substr(header, RSTART + 8, RLENGTH - 8)
      return rest
    }
    return ""
  }
  function extract_read_id(header, value) {
    value = header
    sub(/^@/, "", value)
    sub(/\|.*/, "", value)
    return value
  }
  function add_example(read_id, adapter, expected, observed, text) {
    if (example_count >= 5) return
    text = read_id
    if (adapter != "") text = text " adapter=" adapter
    if (expected != "") text = text " expected=" expected
    if (observed != "") text = text " observed=" observed
    examples[++example_count] = text
  }
  BEGIN {
    FS = OFS = "\t"
    target_norm = normalize_marker(TARGET)
    if (target_norm == "") {
      print "ERROR: target marker is empty" > "/dev/stderr"
      exit 1
    }
    while ((getline line < MAP) > 0) {
      line = trim(line)
      if (line == "") continue
      n = split(line, fields, /\t/)
      if (n < 2) {
        print "ERROR: malformed adapter marker map row: " line > "/dev/stderr"
        exit 1
      }
      adapter = trim(fields[1])
      marker = normalize_marker(fields[2])
      if (adapter == "" || marker == "") {
        print "ERROR: adapter marker map contains empty adapter or marker: " line > "/dev/stderr"
        exit 1
      }
      marker_by_adapter[adapter] = marker
    }
    close(MAP)
  }
  NR % 4 == 1 {
    header = $0
    next
  }
  NR % 4 == 2 {
    seq = $0
    next
  }
  NR % 4 == 3 {
    plus = $0
    next
  }
  NR % 4 == 0 {
    qual = $0
    adapter = extract_adapter(header)
    expected = (adapter in marker_by_adapter) ? marker_by_adapter[adapter] : ""
    observed = target_norm
    if (adapter != "" && expected != "" && expected == target_norm) {
      print header
      print seq
      print plus
      print qual
      kept++
      next
    }
    dropped++
    add_example(extract_read_id(header), adapter, expected, observed)
  }
  END {
    if (NR % 4 != 0) {
      print "ERROR: malformed FASTQ file (record not multiple of 4 lines): " LABEL > "/dev/stderr"
      exit 1
    }
    if (dropped > 0) {
      msg = "WARN: dropped " dropped " track marker/unit mismatches for " LABEL " target=" target_norm
      if (example_count > 0) {
        msg = msg " examples="
        for (i = 1; i <= example_count; i++) {
          msg = msg (i > 1 ? ", " : "") examples[i]
        }
      }
      print msg > "/dev/stderr"
    }
  }
' "$fastq_file" > "$tmp_fastq"

mv "$tmp_fastq" "$fastq_file"
