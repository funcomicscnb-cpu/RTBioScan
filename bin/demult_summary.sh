#!/bin/bash
set -euo pipefail

demult_file=$1
barcode=$2
script_dir="$(cd "$(dirname "$0")" && pwd)"
sample_label_lib="${script_dir}/lib/sample_label.pl"

printf "read_count\t%s\tsample_name\ttrack_mode\ttrack_sample_label\ttrack_unit_label\ttrack_replicate_number\ttrack_plate_label\ttrack_replicate_suffix\n" "$(head -1 "$demult_file" | cut -f2-8)" > "${barcode}_summary_demult_rpt.txt"

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
      my ($track_mode, $track_sample_label, $track_unit_label, $track_replicate_number, $track_plate_label, $track_replicate_suffix) = ("", "", "", "", "", "");
      if ($sample ne q{} && !SampleLabel::is_no_adapter_label($sample)) {
        my @marker_tokens = sort { length($b) <=> length($a) || $a cmp $b } SampleLabel::configured_marker_tokens();
        push @marker_tokens, qw(COI ITS2);
        my %seen_marker = ();
        @marker_tokens = grep {
          defined($_) && $_ ne q{} && !$seen_marker{uc($_)}++
        } @marker_tokens;
        if (@marker_tokens) {
          my $marker_alt = join("|", map { quotemeta($_) } @marker_tokens);
          if ($sample =~ /^(.*)_([0-9]+)_([^_]+)_(${marker_alt})$/i) {
            $track_mode = "track";
            $track_sample_label = $1;
            $track_replicate_number = $2;
            $track_plate_label = $3;
            $track_unit_label = join("_", $track_sample_label, $track_replicate_number, $track_plate_label);
            $track_replicate_suffix = join("_", $track_replicate_number, $track_plate_label);
          }
        }
      }
      print join("\t", @F, $sample_name, $track_mode, $track_sample_label, $track_unit_label, $track_replicate_number, $track_plate_label, $track_replicate_suffix);
    ' "$sample_label_lib" >> "${barcode}_summary_demult_rpt.txt"
