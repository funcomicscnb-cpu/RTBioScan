#!/usr/bin/env perl
use strict;
use warnings;

my ($meta, $hash_map, $out) = @ARGV;
die "usage: $0 frozen_meta.tsv hash_map.tsv out_append.tsv\n" unless @ARGV == 3;

my (%hash2frozen, %hash2rep, %frozen2hash, %rep2hash);
if (-s $meta) {
  open my $M, '<', $meta or die "open $meta: $!";
  while (<$M>) {
    chomp;
    next unless length;
    my @fields = split /\t/, $_, -1;
    my ($frozen_id, $rep_id, $hash) = @fields;
    die "Malformed frozen metadata in $meta at line $.\n"
      unless (@fields == 3 || @fields == 4)
        && defined $rep_id && length $rep_id
        && defined $hash && $hash =~ /^[0-9a-f]{32}$/
        && $frozen_id eq "FROZEN_$hash"
        && (@fields == 3 || $fields[3] =~ /^\S+$/);
    my ($base_rep) = split /\|/, $rep_id;
    die "Malformed representative ID in $meta at line $.\n"
      unless defined $base_rep && length $base_rep && $rep_id !~ /\s/;
    die "Conflicting frozen metadata in $meta at line $.\n"
      if (exists $hash2frozen{$hash} && ($hash2frozen{$hash} ne $frozen_id || $hash2rep{$hash} ne $rep_id))
        || (exists $frozen2hash{$frozen_id} && $frozen2hash{$frozen_id} ne $hash)
        || (exists $rep2hash{$base_rep} && $rep2hash{$base_rep} ne $hash);
    $hash2frozen{$hash} = $frozen_id;
    $hash2rep{$hash} = $rep_id;
    $frozen2hash{$frozen_id} = $hash;
    $rep2hash{$base_rep} = $hash;
  }
  close $M or die "close $meta: $!";
}

open my $H, '<', $hash_map or die "open $hash_map: $!";
my (%matched, %rid2hash);
while (<$H>) {
  chomp;
  next unless length;
  my @fields = split /\t/, $_, -1;
  my ($rid, $hash) = @fields;
  die "Malformed hash map in $hash_map at line $.\n"
    unless @fields == 2 && $rid =~ /^[^|\s][^\s]*$/
      && $hash =~ /^[0-9a-f]{32}$/;
  die "Conflicting hash map in $hash_map at line $.\n"
    if exists $rid2hash{$rid} && $rid2hash{$rid} ne $hash;
  $rid2hash{$rid} = $hash;
  $matched{$hash} = 1 if exists $hash2frozen{$hash};
}
close $H or die "close $hash_map: $!";

# Validate all input before replacing output. Raw depth supplies no additional
# NR membership; metadata retains authority even in a duplicate-only round.
open my $O, '>', $out or die "open $out: $!";
for my $hash (sort keys %matched) {
  print $O join("\t", $hash2frozen{$hash}, $hash2rep{$hash}, 1), "\n"
    or die "write $out: $!";
}
close $O or die "close $out: $!";
