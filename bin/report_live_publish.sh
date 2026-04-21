#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

stage_root=""
state_root=""
run_asset_root=""
round_barcode=""
lock_path=""

usage() {
  cat 1>&2 <<'USAGE'
Usage: report_live_publish.sh --stage-root DIR --state-root DIR --run-asset-root DIR --round-barcode ID --lock-path PATH
USAGE
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --stage-root)
      stage_root="${2:-}"
      shift 2
      ;;
    --state-root)
      state_root="${2:-}"
      shift 2
      ;;
    --run-asset-root)
      run_asset_root="${2:-}"
      shift 2
      ;;
    --round-barcode)
      round_barcode="${2:-}"
      shift 2
      ;;
    --lock-path)
      lock_path="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "ERROR: unknown argument: $1" 1>&2
      usage
      ;;
  esac
done

if [ -z "$stage_root" ] || [ -z "$state_root" ] || [ -z "$run_asset_root" ] || [ -z "$round_barcode" ] || [ -z "$lock_path" ]; then
  usage
fi

if [ ! -d "$stage_root/report_assets" ] || [ ! -d "$stage_root/tables" ] || [ ! -d "$stage_root/sequences" ] || [ ! -f "$stage_root/README.html" ]; then
  echo "ERROR: incomplete live-round stage at $stage_root" 1>&2
  exit 2
fi

lock_dir="${lock_path}.lockdir"
payload_root="$state_root/.live_round_payloads"
tmp_payload="$payload_root/${round_barcode}.tmp.$$"
publish_stamp="$(
  perl -MTime::HiRes=time -e 'my $t=time(); my $pid=$$; my $rand=int(rand(1000000)); printf "%d_%d_%06d\n", int($t * 1000000), $pid, $rand;'
)"
payload_name="${round_barcode}__${publish_stamp}"
final_payload="$payload_root/$payload_name"
final_payload_rel=".live_round_payloads/$payload_name"
state_live_link="$state_root/live_round"
run_live_link="$run_asset_root/live_round"

mkdir -p "$payload_root" "$run_asset_root"

waited=0
lock_wait="${LOCK_WAIT:-300}"
while ! mkdir "$lock_dir" 2>/dev/null; do
  sleep 1
  waited=$((waited + 1))
  if [ "$waited" -ge "$lock_wait" ]; then
    echo "ERROR: failed to acquire live-round publish lock: $lock_path" 1>&2
    exit 2
  fi
done

cleanup() {
  rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

atomic_replace_path() {
  local src="$1"
  local dst="$2"
  perl -e 'my ($src, $dst) = @ARGV; rename($src, $dst) or die "rename($src,$dst): $!\n";' "$src" "$dst"
}

old_target="$(readlink "$state_live_link" 2>/dev/null || true)"

rm -rf "$tmp_payload" 2>/dev/null || true
mkdir -p "$tmp_payload"
cp -Rp "$stage_root"/. "$tmp_payload"/
if [ -e "$final_payload" ] || [ -L "$final_payload" ]; then
  echo "ERROR: live-round payload path collision: $final_payload" 1>&2
  exit 2
fi
mv "$tmp_payload" "$final_payload"

state_next="$state_root/.live_round.next.$$"
ln -s "$final_payload_rel" "$state_next"
atomic_replace_path "$state_next" "$state_live_link"

run_next="$run_asset_root/.live_round.next.$$"
ln -s "$state_live_link/report_assets" "$run_next"
atomic_replace_path "$run_next" "$run_live_link"

if [ -n "$old_target" ] && [ "$old_target" != "$final_payload_rel" ]; then
  case "$old_target" in
    .live_round_payloads/*)
      rm -rf "$state_root/$old_target" 2>/dev/null || true
      ;;
  esac
fi

rm -rf "$stage_root" 2>/dev/null || true
