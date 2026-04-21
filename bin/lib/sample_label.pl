package SampleLabel;

use strict;
use warnings;
use Digest::SHA qw(sha1_hex);
use File::Basename qw(dirname);
my $TAXON_UTIL_PATH = dirname(__FILE__) . "/taxon_util.pl";
require $TAXON_UTIL_PATH;

sub trim_text {
  my ($v) = @_;
  $v = '' unless defined $v;
  $v =~ s/^\s+|\s+$//g;
  return $v;
}

sub normalize_sample_label {
  my ($label) = @_;
  $label = trim_text($label);
  return 'unknown' if $label eq '';
  # Collapse all no_adapter variants (bare, replicate no_adapter_N, or marker-tagged
  # no_adapter|COI / no_adapter|ITS2) to the canonical 'no_adapter'.
  if ($label =~ /^no_adapter(?:[_|].+)?$/i) {
    return 'no_adapter';
  }
  return $label;
}

sub normalize_consensus_sample_label {
  my ($label) = @_;
  $label = trim_text($label);
  return '' if $label eq '';
  return 'no_adapter' if is_no_adapter_label($label);
  return $label;
}

sub extract_adapter_from_read_id {
  my ($read_id) = @_;
  return undef unless defined $read_id;
  return undef unless $read_id =~ /adapter=([^|\s]+)/;
  my $adapter = trim_text($1);
  return undef if $adapter eq '';
  return $adapter;
}

sub is_no_adapter_label {
  my ($label) = @_;
  $label = trim_text($label);
  return 0 if $label eq '';
  # Match bare no_adapter, replicate variant (no_adapter_N), and marker variant (no_adapter|COI)
  # that is produced when the second cutadapt primer-detection pass tags an unbarcode read.
  return $label =~ /^no_adapter(?:_[0-9]+)?(?:\|.*)?$/i ? 1 : 0;
}

sub normalize_sample_base {
  my ($label) = @_;
  $label = trim_text($label);
  return '' if $label eq '';
  return 'no_adapter' if is_no_adapter_label($label);
  $label =~ s/_[0-9]+$//;          # strip trailing replicate suffix _N
  my $marker_pattern = configured_marker_suffix_pattern();
  $label =~ s/${marker_pattern}$//i if $marker_pattern ne '';
  $label =~ s/_(?:COI|ITS)\d*$//i; # fallback for legacy labels without configured markers in env
  $label =~ s/_[0-9]+_/_/;         # strip embedded _N_
  return trim_text($label);
}

# Extract sequencing marker ('coi' or 'its2') from a raw sample label.
# Strips only the replicate suffix first, then looks for a known marker token.
# Returns 'coi', 'its2', or '' for unknown/absent.
sub extract_marker_from_label {
  my ($label) = @_;
  $label = trim_text($label);
  return '' if $label eq '' || is_no_adapter_label($label);
  (my $base = $label) =~ s/_[0-9]+$//;  # strip replicate suffix
  my @tokens = configured_marker_tokens();
  for my $marker (@tokens) {
    next if !defined $marker || $marker eq '';
    my $quoted = quotemeta($marker);
    return lc($marker) if $base =~ /(?:^|_)${quoted}(?:_[0-9]+)?$/i;
  }
  return 'its2' if $base =~ /_ITS\d*$/i;
  return 'coi'  if $base =~ /_COI\d*$/i;
  return '';
}

sub configured_marker_tokens {
  return TaxonUtil::configured_marker_tokens();
}

sub configured_marker_suffix_pattern {
  my @tokens = configured_marker_tokens();
  return '' if !@tokens;
  my @alts = map { quotemeta($_) } @tokens;
  return '_(?:' . join('|', @alts) . ')(?:[0-9]+)?';
}

sub valid_barcoded_sample_base {
  my ($label) = @_;
  return undef unless defined $label;
  return undef if is_no_adapter_label($label);
  my $normalized = normalize_sample_base($label);
  return undef if !defined $normalized || $normalized eq '' || lc($normalized) eq 'no_adapter';
  return $normalized;
}

sub canonical_adapter_token {
  my ($label) = @_;
  $label = trim_text($label);
  return undef if $label eq '';
  return 'no_adapter' if is_no_adapter_label($label);
  return valid_barcoded_sample_base($label);
}

sub normalize_sample_id_base {
  my ($label) = @_;
  my $id = lc(trim_text($label));
  $id =~ s/[^a-z0-9._-]+/_/g;
  $id =~ s/_+/_/g;
  $id =~ s/^_+|_+$//g;
  $id = 'sample' if $id eq '';
  return $id;
}

sub stable_sample_id_from_label {
  my ($label_raw) = @_;
  my $label = normalize_sample_label($label_raw);
  my $base = normalize_sample_id_base($label);
  my $hash = substr(sha1_hex($label), 0, 8);
  return "${base}_${hash}";
}

1;
