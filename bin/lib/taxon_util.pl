package TaxonUtil;

use strict;
use warnings;

sub configured_marker_tokens {
  my $raw = $ENV{RTBIOSCAN_TARGET_TOKENS};
  return () if !defined $raw || $raw eq '';
  my @tokens;
  for my $part (split /\|/, $raw, -1) {
    my $marker = canonical_marker_token($part);
    push @tokens, $marker if defined $marker && $marker ne '';
  }
  return @tokens;
}

sub trim_text {
  my ($v) = @_;
  $v = '' unless defined $v;
  $v =~ s/^\s+|\s+$//g;
  return $v;
}

sub is_unassigned_taxon {
  my ($v) = @_;
  $v = trim_text($v);
  return 1 if $v eq '' || uc($v) eq 'NA' || lc($v) eq 'unassigned';
  return 0;
}

sub normalize_taxon {
  my ($v) = @_;
  return undef if is_unassigned_taxon($v);
  return trim_text($v);
}

sub canonical_marker_token {
  my ($v) = @_;
  $v = trim_text($v);
  return undef if $v eq '';
  my $candidate = uc($v);
  return 'ITS2' if $candidate eq 'ITS' || $candidate eq 'ITS1' || $candidate eq 'ITS2';
  return $candidate;
}

sub marker_filename_token {
  my ($marker) = @_;
  my $canonical = canonical_marker_token($marker);
  return '' if !defined $canonical || $canonical eq '';
  return 'COI' if $canonical eq 'COI';
  return 'ITS' if $canonical eq 'ITS2';
  $canonical =~ s/[^A-Z0-9._-]+/_/g;
  $canonical =~ s/_+/_/g;
  $canonical =~ s/^_+|_+$//g;
  return $canonical;
}

sub marker_slug {
  my ($marker) = @_;
  my $canonical = canonical_marker_token($marker);
  return '' if !defined $canonical || $canonical eq '';
  my $slug = lc($canonical);
  $slug =~ s/[^a-z0-9._-]+/_/g;
  $slug =~ s/_+/_/g;
  $slug =~ s/^_+|_+$//g;
  return $slug;
}

sub marker_from_token {
  my ($v) = @_;
  $v = trim_text($v);
  return undef if $v eq '';

  my @configured = configured_marker_tokens();
  for my $marker (@configured) {
    next if !defined $marker || $marker eq '';
    my $quoted = quotemeta($marker);
    return $marker if $v =~ /(?:^|[-_|])${quoted}(?:$|[-_|])/i;
  }

  return 'COI'  if $v =~ /-COI\b/i  || $v =~ /\bCOI\b/i;
  return 'ITS2' if $v =~ /-(?:ITS|ITS1|ITS2)\b/i || $v =~ /\b(?:ITS|ITS1|ITS2)\b/i;
  return 'OTHER';
}

# Returns true if $v looks like a valid numeric taxon ID (non-empty, not NA, all digits).
# NOTE: does NOT enforce >0; callers that need that check must add it explicitly.
sub is_numeric_taxid {
  my ($v) = @_;
  $v = trim_text($v);
  return 0 if $v eq '' || uc($v) eq 'NA';
  return $v =~ /^\d+$/ ? 1 : 0;
}

# Returns true if $v is a non-empty, non-NA, non-"unassigned" kingdom/single-taxon string.
sub is_assigned_kingdom {
  my ($v) = @_;
  $v = trim_text($v);
  return 0 if $v eq '' || uc($v) eq 'NA' || lc($v) eq 'unassigned';
  return 1;
}

# Returns true if semicolon-delimited $lineage contains at least one non-unassigned component.
sub is_assigned_lineage {
  my ($v) = @_;
  $v = trim_text($v);
  return 0 if $v eq '' || uc($v) eq 'NA';
  for my $part (split /;/, $v) {
    $part = trim_text($part);
    next if $part eq '';
    next if $part =~ /^[A-Za-z]__unassigned$/i;
    next if lc($part) eq 'unassigned';
    return 1;
  }
  return 0;
}

1;
