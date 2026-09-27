#!/usr/bin/env bash
set -euo pipefail

use_lock=1
if [ "${1:-}" = "--no-lock" ]; then
  use_lock=0
  shift
fi

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 [--no-lock] <round_report.json> <history.jsonl> <lock_path>" 1>&2
  exit 2
fi

ROUND_JSON="$1"
HISTORY_JSONL="$2"
LOCK_PATH="$3"
LOCK_WAIT="${LOCK_WAIT:-300}"
LOCK_DIR="${LOCK_PATH}.lockdir"

if [ ! -s "$ROUND_JSON" ]; then
  echo "ERROR: round report missing or empty: $ROUND_JSON" 1>&2
  exit 2
fi

mkdir -p "$(dirname "$HISTORY_JSONL")"

if [ "$use_lock" -eq 1 ]; then
  source "$(dirname "$0")/lib/lock_utils.sh"
  init_lock_helpers
  acquire_lock "$LOCK_PATH" || exit 2
  RTB_HISTORY_LOCK_FD=${acquired_lock_fds[$((${#acquired_lock_fds[@]}-1))]}
  export RTB_HISTORY_LOCK_OWNER=$$
fi

# Official retained-report layout opts into the shared authority contract even
# for direct callers. The historical three-path interface remains for legacy layouts.
if [ -z "${RTB_HISTORY_CURRENT:-}" ] && [ "$(basename "$HISTORY_JSONL")" = report_history.jsonl ]; then
  state_candidate="$(dirname "$(dirname "$HISTORY_JSONL")")"
  round_directory="$(dirname "$ROUND_JSON")"
  if [ "$(dirname "$round_directory")" = "$state_candidate" ]; then
    RTB_HISTORY_STATE="$state_candidate"
    RTB_HISTORY_CURRENT="$(basename "$round_directory")"
  fi
fi
if [ -n "${RTB_HISTORY_CURRENT:-}" ]; then
  python3 -B "$(dirname "$0")/report_history_state.py" rebuild-history \
    --state-dir "${RTB_HISTORY_STATE:?}" --current-round-barcode "$RTB_HISTORY_CURRENT" \
    --outdir "${RTB_HISTORY_OUTDIR:-}" --history "$HISTORY_JSONL" --lock-fd "${RTB_HISTORY_LOCK_FD:?}"
  exit $?
fi

TMP="$(mktemp "${HISTORY_JSONL}.tmp.XXXXXX")"
if perl -MJSON::PP -e '
use strict;
use warnings;
my ($round_json, $history_jsonl, $out_path) = @ARGV;
open my $R, "<", $round_json or die "open round json: $!";
my $line = <$R>;
close $R;
die "empty round json\n" if !defined($line) || $line =~ /^\s*$/;
my $new = decode_json($line);
for my $k (qw(run_id barcode round_barcode)) {
  die "round json missing key $k\n" if !defined($new->{$k}) || $new->{$k} eq "";
}
my $new_key = join("\t", $new->{run_id}, $new->{barcode}, $new->{round_barcode});

open my $OUT, ">", $out_path or die "open out: $!";
if (-s $history_jsonl) {
  open my $IN, "<", $history_jsonl or die "open history: $!";
  while (my $l = <$IN>) {
    chomp $l;
    if ($l =~ /^\s*$/) {
      next;
    }
    my $ok = eval { decode_json($l) };
    if ($@ || !defined $ok || ref($ok) ne "HASH") {
      # Preserve malformed lines so we do not silently destroy history.
      print {$OUT} $l, "\n";
      next;
    }
    my $rk = defined($ok->{run_id}) ? $ok->{run_id} : "";
    my $bk = defined($ok->{barcode}) ? $ok->{barcode} : "";
    my $qk = defined($ok->{round_barcode}) ? $ok->{round_barcode} : "";
    my $old_key = join("\t", $rk, $bk, $qk);
    next if $old_key eq $new_key;
    print {$OUT} $l, "\n";
  }
  close $IN;
}
print {$OUT} encode_json($new), "\n";
close $OUT;
' "$ROUND_JSON" "$HISTORY_JSONL" "$TMP"; then
  mv "$TMP" "$HISTORY_JSONL"
else
  rm -f "$TMP" 2>/dev/null || true
  exit 2
fi

exit 0
