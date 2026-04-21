#!/usr/bin/env perl
use strict;
use warnings;

my ($clstr, $out_members, $out_counts) = @ARGV;
die "usage: $0 in.clstr out_members.tsv out_counts.tsv\n" unless @ARGV == 3;

open my $IN,  "<", $clstr       or die "open $clstr: $!";
open my $MEM, ">", $out_members or die "open $out_members: $!";
open my $CNT, ">", $out_counts  or die "open $out_counts: $!";

my $cluster = -1;
my $rep = "";
my $count = 0;

sub flush_cluster {
  return if $cluster < 0;
  print $CNT join("\t", "CLUST_$cluster", $rep, $count), "\n";
}

while (<$IN>) {
  chomp;
  if (/^>Cluster\s+(\d+)/) {
    flush_cluster();
    $cluster = $1;
    $rep = "";
    $count = 0;
    next;
  }
  if (/^\s*\d+\s+\d+nt,\s+>(\S+)\.\.\.\s*(\*?)/) {
    my $id = $1;
    my $is_rep = $2 eq '*';
    $rep = $id if $is_rep;
    $count++;
    print $MEM join("\t", "CLUST_$cluster", $id, $is_rep ? 1 : 0), "\n";
  }
}
flush_cluster();

close $IN;
close $MEM;
close $CNT;
