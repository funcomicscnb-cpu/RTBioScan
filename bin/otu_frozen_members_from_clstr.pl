#!/usr/bin/env perl
use strict;
use warnings;

my ($clstr, $out_members, $out_unassigned, $mode, $db_only_policy) = @ARGV;
die "usage: $0 in.clstr out_frozen_members.tsv out_unassigned.list [strict|legacy] [auto|fail|warn_skip]\n"
  unless @ARGV >= 3 && @ARGV <= 5;
$mode = defined($mode) ? lc($mode) : 'strict';
die "Invalid mode '$mode' (expected strict|legacy)\n" unless $mode eq 'strict' || $mode eq 'legacy';
$db_only_policy = defined($db_only_policy) ? lc($db_only_policy) : 'auto';
die "Invalid db_only_policy '$db_only_policy' (expected auto|fail|warn_skip)\n"
  unless $db_only_policy eq 'auto' || $db_only_policy eq 'fail' || $db_only_policy eq 'warn_skip';

# Output contract:
# - Column 1: canonical frozen ID (FROZEN_<hash>)
# - Column 2: full read header
# - Column 3: representative flag (1/0)

open my $IN,  "<", $clstr          or die "open $clstr: $!";
open my $MEM, ">", $out_members    or die "open $out_members: $!";
open my $UN,  ">", $out_unassigned or die "open $out_unassigned: $!";

my @cluster_rows;
my $frozen_id = "";

sub canonical_frozen_id {
  my ($id) = @_;
  return '' unless defined $id && $id =~ /^FROZEN_/;
  $id =~ s/\|.*$//;
  return $id;
}

sub flush_cluster {
  return unless @cluster_rows;
  if ($frozen_id) {
    my $emitted = 0;
    for my $row (@cluster_rows) {
      my $id = $row->{id};
      # Exclude frozen DB-side records (FROZEN_<hash>|...) from member output.
      next if canonical_frozen_id($id) ne '';
      # Query reads assigned to a frozen cluster are members, not representatives.
      print $MEM join("\t", $frozen_id, $id, 0), "\n";
      $emitted++;
    }
    if ($emitted == 0) {
      my $eff_policy = $db_only_policy;
      if ($eff_policy eq 'auto') {
        $eff_policy = ($mode eq 'strict') ? 'fail' : 'warn_skip';
      }
      if ($eff_policy eq 'fail') {
        die "Frozen cluster '$frozen_id' in $clstr had no query member rows; refusing silent drop\n";
      }
      warn "WARN: Frozen cluster '$frozen_id' in $clstr had no query member rows; skipping cluster\n";
    }
  } else {
    for my $row (@cluster_rows) {
      my $id = $row->{id};
      next if canonical_frozen_id($id) ne '';
      print $UN "$id\n";
    }
  }
  @cluster_rows = ();
  $frozen_id = "";
}

while (<$IN>) {
  chomp;
  if (/^>Cluster/) {
    flush_cluster();
    next;
  }
  if (/^\s*\d+\s+\d+nt,\s+>(\S+)\.\.\.\s*(\*?)/) {
    my ($id, $is_rep) = ($1, $2);
    push @cluster_rows, { id => $id, is_rep => ($is_rep eq '*' ? 1 : 0) };
    if (!$frozen_id) {
      my $canon = canonical_frozen_id($id);
      $frozen_id = $canon if $canon ne '';
    }
  }
}
flush_cluster();

close $IN;
close $MEM;
close $UN;
