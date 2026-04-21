package ConsensusIdUtil;

use strict;
use warnings;
use FindBin;
require "$FindBin::Bin/lib/sample_label.pl";

sub trim_text {
  my ($v) = @_;
  $v = '' unless defined $v;
  $v =~ s/^\s+|\s+$//g;
  return $v;
}

sub parse_consensus_long_seq_id {
  my ($value) = @_;
  my $raw = trim_text($value);
  $raw =~ s/^>//;
  return undef if $raw eq '';

  my @parts = split /\|/, $raw;
  return undef if @parts < 2;

  my $sample = SampleLabel::normalize_consensus_sample_label($parts[0]);
  my $cons_token = trim_text($parts[1]);
  return undef if $sample eq '' || $cons_token eq '';

  return {
    sample       => $sample,
    cons_token   => $cons_token,
    consensus_id => $cons_token . '_' . $sample,
    long_seq_id  => $raw,
  };
}

sub consensus_id_from_long_seq_id {
  my ($value) = @_;
  my $parsed = parse_consensus_long_seq_id($value);
  return undef if !defined $parsed;
  my $candidate = $parsed->{consensus_id};
  return undef if !defined $candidate || $candidate eq '';
  return $candidate;
}

1;
