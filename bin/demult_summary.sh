#!/bin/bash
set -euo pipefail

demult_file=$1
barcode=$2
script_dir="$(cd "$(dirname "$0")" && pwd)"
sample_label_lib="${script_dir}/lib/sample_label.pl"

printf "read_count\t%s\tsample_name\n" "$(head -1 "$demult_file" | cut -f2-8)" > "${barcode}_summary_demult_rpt.txt"

tail -n +2 "$demult_file" \
  | cut -f2-8 \
  | sort \
  | uniq -c \
  | sed 's/^[[:space:]]*//' \
  | sed 's/[[:space:]]/\t/' \
  | perl -F'\t' -lane '
      BEGIN {
        my $lib = shift @ARGV;
        require $lib;
      }
      my $sample = defined $F[3] ? $F[3] : q{};
      my $sample_name = SampleLabel::normalize_sample_base($sample);
      print join("\t", @F, $sample_name);
    ' "$sample_label_lib" >> "${barcode}_summary_demult_rpt.txt"
