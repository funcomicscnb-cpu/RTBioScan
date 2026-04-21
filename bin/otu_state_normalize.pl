#!/usr/bin/env perl
use strict;
use warnings;

my ($members_in, $members_out, $meta_in, $meta_out) = @ARGV;
die "usage: $0 in_members.tsv out_members.tsv [in_meta.tsv out_meta.tsv]\n"
  unless defined $members_in && defined $members_out;
die "If meta input is provided, meta output must also be provided\n"
  if (defined($meta_in) xor defined($meta_out));

sub canonical_frozen_id {
  my ($fid) = @_;
  return '' unless defined $fid;
  $fid =~ s/^\s+|\s+$//g;
  $fid =~ s/\|.*$//;
  return $fid;
}

open my $IN, '<', $members_in or die "open $members_in: $!";
my %rows;
while (<$IN>) {
  chomp;
  next unless length;
  my @f = split /\t/;
  next unless @f >= 2;
  my $fid = canonical_frozen_id($f[0]);
  my $rid = $f[1];
  next unless $fid =~ /^FROZEN_/ && defined $rid && length $rid;
  my $is_rep = (defined $f[2] && $f[2] =~ /^[01]$/) ? $f[2] : 0;
  my $k = join("\t", $fid, $rid);
  if (!exists $rows{$k} || $is_rep > $rows{$k}) {
    $rows{$k} = $is_rep;
  }
}
close $IN;

open my $OUT, '>', $members_out or die "open $members_out: $!";
for my $k (sort keys %rows) {
  print $OUT join("\t", $k, $rows{$k}), "\n";
}
close $OUT;

if (defined $meta_in && defined $meta_out) {
  open my $MI, '<', $meta_in or die "open $meta_in: $!";
  my %meta_rows;
  while (<$MI>) {
    chomp;
    next unless length;
    my @f = split /\t/;
    next unless @f >= 2;
    my $fid = canonical_frozen_id($f[0]);
    next unless $fid =~ /^FROZEN_/;
    $f[0] = $fid;
    my $k = join("\t", @f);
    $meta_rows{$k} = 1;
  }
  close $MI;

  open my $MO, '>', $meta_out or die "open $meta_out: $!";
  print $MO "$_\n" for sort keys %meta_rows;
  close $MO;
}
