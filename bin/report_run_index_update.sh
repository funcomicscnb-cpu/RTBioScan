#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 <run_report.json> <runs_index.jsonl> <lock_path>" 1>&2
  exit 2
fi

RUN_JSON="$1"
RUN_INDEX_JSONL="$2"
LOCK_PATH="$3"
LOCK_WAIT="${LOCK_WAIT:-300}"
LOCK_DIR="${LOCK_PATH}.lockdir"

if [ ! -s "$RUN_JSON" ]; then
  echo "ERROR: run report missing or empty: $RUN_JSON" 1>&2
  exit 2
fi

mkdir -p "$(dirname "$RUN_INDEX_JSONL")"

waited=0
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  sleep 1
  waited=$((waited + 1))
  if [ "$waited" -ge "$LOCK_WAIT" ]; then
    echo "ERROR: failed to acquire run index lock: $LOCK_PATH" 1>&2
    exit 2
  fi
done
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

TMP="$(mktemp "${RUN_INDEX_JSONL}.tmp.XXXXXX")"
if perl -MJSON::PP -e '
use strict;
use warnings;
my ($run_json, $index_jsonl, $out_path) = @ARGV;
open my $R, "<", $run_json or die "open run json: $!";
my $line = <$R>;
close $R;
die "empty run json\n" if !defined($line) || $line =~ /^\s*$/;
my $new = decode_json($line);
die "run json missing run_id\n" if !defined($new->{run_id}) || $new->{run_id} eq "";
my $new_key = $new->{run_id};

open my $OUT, ">", $out_path or die "open out: $!";
if (-s $index_jsonl) {
  open my $IN, "<", $index_jsonl or die "open index: $!";
  while (my $l = <$IN>) {
    chomp $l;
    if ($l =~ /^\s*$/) {
      next;
    }
    my $ok = eval { decode_json($l) };
    if ($@ || !defined $ok || ref($ok) ne "HASH") {
      print {$OUT} $l, "\n";
      next;
    }
    my $rk = defined($ok->{run_id}) ? $ok->{run_id} : "";
    next if $rk eq $new_key;
    print {$OUT} $l, "\n";
  }
  close $IN;
}
print {$OUT} encode_json($new), "\n";
close $OUT;
' "$RUN_JSON" "$RUN_INDEX_JSONL" "$TMP"; then
  mv "$TMP" "$RUN_INDEX_JSONL"
else
  rm -f "$TMP" 2>/dev/null || true
  exit 2
fi

exit 0
