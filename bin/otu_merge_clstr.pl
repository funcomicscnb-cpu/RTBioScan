#!/usr/bin/env perl
use strict;
use warnings;

my ($frozen_mem, $active_mem, $out) = @ARGV;
die "usage: $0 frozen_members.tsv active_members.tsv out.clstr\n" unless @ARGV == 3;

my %clusters;

sub load_mem {
  my ($path, $prefix) = @_;
  return unless -s $path;
  open my $IN, '<', $path or die "open $path: $!";
  while (<$IN>) {
    chomp; next unless length;
    my ($cid, $id, $is_rep) = split /\t/;
    $cid = $prefix . $cid;
    push @{ $clusters{$cid} }, [$id, $is_rep];
  }
  close $IN;
}

load_mem($frozen_mem, 'F_');
load_mem($active_mem, 'A_');

open my $OUT, '>', $out or die "open $out: $!";
my $idx = 0;
for my $cid (sort keys %clusters) {
  print $OUT ">Cluster $idx\n";
  my $i = 0;
  for my $m (@{ $clusters{$cid} }) {
    my ($id, $is_rep) = @$m;
    my $star = $is_rep ? '*' : '';
    print $OUT "$i\t0nt, >$id... $star\n";
    $i++;
  }
  $idx++;
}
close $OUT;
