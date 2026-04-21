#!/usr/bin/env bash

trim_text_sh() {
  printf '%s\n' "${1:-}" | sed -E 's/^[[:space:]]+|[[:space:]]+$//g'
}

extract_adapter_from_read_id_sh() {
  local read_id="${1:-}"
  if [[ "$read_id" =~ adapter=([^[:space:]|]+) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

is_no_adapter_adapter_sh() {
  local adapter
  local adapter_lc
  adapter="$(trim_text_sh "${1:-}")"
  adapter_lc="$(printf '%s\n' "$adapter" | tr '[:upper:]' '[:lower:]')"
  [[ "$adapter_lc" =~ ^no_adapter(_[0-9]+)?$ ]]
}

normalize_sample_base_sh() {
  local sample
  local sample_lc
  local targets_raw
  local old_ifs
  local token
  local token_lc
  local suffix
  local tail
  sample="$(trim_text_sh "${1:-}")"
  if [ -z "$sample" ]; then
    return 1
  fi
  if is_no_adapter_adapter_sh "$sample"; then
    printf 'no_adapter\n'
    return 0
  fi
  sample="$(printf '%s\n' "$sample" | sed -E 's/_[0-9]+$//')"
  sample_lc="$(printf '%s\n' "$sample" | tr '[:upper:]' '[:lower:]')"
  targets_raw="${RTBIOSCAN_TARGET_TOKENS:-}"
  old_ifs="$IFS"
  IFS='|'
  for token in $targets_raw; do
    token="$(trim_text_sh "$token")"
    [ -n "$token" ] || continue
    token_lc="$(printf '%s\n' "$token" | tr '[:upper:]' '[:lower:]')"
    suffix="_${token_lc}"
    if [ "${sample_lc%$suffix}" != "$sample_lc" ]; then
      sample="${sample:0:$((${#sample} - ${#suffix}))}"
      sample_lc="${sample_lc:0:$((${#sample_lc} - ${#suffix}))}"
      break
    fi
    tail="${sample_lc##*${suffix}}"
    if [ "$tail" != "$sample_lc" ] && [[ "$tail" =~ ^[0-9]+$ ]]; then
      suffix="${suffix}${tail}"
      sample="${sample:0:$((${#sample} - ${#suffix}))}"
      sample_lc="${sample_lc:0:$((${#sample_lc} - ${#suffix}))}"
      break
    fi
  done
  IFS="$old_ifs"
  for token_lc in coi its; do
    suffix="_${token_lc}"
    if [ "${sample_lc%$suffix}" != "$sample_lc" ]; then
      sample="${sample:0:$((${#sample} - ${#suffix}))}"
      sample_lc="${sample_lc:0:$((${#sample_lc} - ${#suffix}))}"
      break
    fi
    tail="${sample_lc##*${suffix}}"
    if [ "$tail" != "$sample_lc" ] && [[ "$tail" =~ ^[0-9]*$ ]]; then
      suffix="${suffix}${tail}"
      sample="${sample:0:$((${#sample} - ${#suffix}))}"
      sample_lc="${sample_lc:0:$((${#sample_lc} - ${#suffix}))}"
      break
    fi
  done
  sample="$(printf '%s\n' "$sample" | sed -E 's/_[0-9]+_/_/')"
  sample="$(trim_text_sh "$sample")"
  [ -n "$sample" ] || return 1
  printf '%s\n' "$sample"
}

valid_barcoded_sample_sh() {
  local adapter="${1:-}"
  local normalized
  if is_no_adapter_adapter_sh "$adapter"; then
    return 1
  fi
  normalized="$(normalize_sample_base_sh "$adapter" 2>/dev/null || true)"
  if [ -z "$normalized" ] || [ "$normalized" = "no_adapter" ]; then
    return 1
  fi
  printf '%s\n' "$normalized"
}

adapter_matches_sample_sh() {
  local sample="${1:-}"
  local adapter="${2:-}"
  local normalized_sample
  local normalized_adapter
  normalized_sample="$(normalize_sample_base_sh "$sample" 2>/dev/null || true)"
  if [ -z "$normalized_sample" ]; then
    return 1
  fi
  if [ "$normalized_sample" = "no_adapter" ]; then
    is_no_adapter_adapter_sh "$adapter"
    return $?
  fi
  normalized_adapter="$(valid_barcoded_sample_sh "$adapter" 2>/dev/null || true)"
  [ -n "$normalized_adapter" ] || return 1
  [ "$normalized_adapter" = "$normalized_sample" ]
}
