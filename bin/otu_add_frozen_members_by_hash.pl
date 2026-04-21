#!/usr/bin/env perl
use strict;
use warnings;

my ($meta, $hash_map, $out) = @ARGV;
die "usage: $0 frozen_meta.tsv hash_map.tsv out_append.tsv\n" unless @ARGV == 3;

my (%hash2frozen, %hash2rep);
if (-s $meta) {
  open my $M, '<', $meta or die "open $meta: $!";
  while (<$M>) {
    chomp;
    next unless length;
    my ($frozen_id, $rep_id, $hash) = split /\t/;
    next unless defined $frozen_id && defined $hash;
    $hash2frozen{$hash} = $frozen_id;
    $hash2rep{$hash} = $rep_id if defined $rep_id && length $rep_id;
  }
  close $M;
}

open my $H, '<', $hash_map or die "open $hash_map: $!";
open my $O, '>', $out or die "open $out: $!";
while (<$H>) {
  chomp;
  next unless length;
  my ($rid, $hash) = split /\t/;
  next unless defined $rid && defined $hash;
  my $frozen_id = $hash2frozen{$hash} or next;
  my $rep_id = $hash2rep{$hash} // '';
  my $is_rep = ($rep_id && $rid eq $rep_id) ? 1 : 0;
  print $O join("\t", $frozen_id, $rid, $is_rep), "\n";
}
close $H;
close $O;
