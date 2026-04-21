#!/usr/bin/env perl
use strict;
use warnings;

my ($state_dir, $mode, $report_out) = @ARGV;
die "usage: $0 state_dir [strict|legacy] [report.tsv]\n" unless defined $state_dir;

$mode = defined($mode) ? lc($mode) : 'strict';
die "Invalid mode '$mode' (expected strict|legacy)\n" unless $mode eq 'strict' || $mode eq 'legacy';
$report_out = defined($report_out) ? $report_out : '';

sub write_report {
  my ($path, $rows) = @_;
  return unless defined $path && length $path;
  open my $R, '>', $path or die "open $path: $!";
  for my $k (sort keys %$rows) {
    print $R join("\t", $k, $rows->{$k}), "\n";
  }
  close $R;
}

my $members = "$state_dir/otu_frozen_members.tsv";
my $meta    = "$state_dir/otu_frozen_meta.tsv";
my $active_pool = "$state_dir/otu_active_pool.fasta";

my %stats = (
  mode => $mode,
  members_exists => (-e $members ? 1 : 0),
  meta_exists => (-e $meta ? 1 : 0),
  members_rows => 0,
  members_pipe_rows => 0,
  members_nopipe_rows => 0,
  members_unique_pairs => 0,
  members_unique_pairs_pipe => 0,
  members_unique_pairs_nopipe => 0,
  meta_rows => 0,
  malformed_member_rows => 0,
  malformed_meta_rows => 0,
  mixed_member_id_styles => 0,
  noncanonical_frozen_ids_members => 0,
  noncanonical_frozen_ids_meta => 0,
  duplicate_member_pairs => 0,
  active_pool_exists => (-e $active_pool ? 1 : 0),
  active_pool_records => 0,
  active_pool_pipe_ids => 0,
  active_pool_nopipe_ids => 0,
  active_pool_malformed_ids => 0,
);

my %issues;
my $record_issue = sub {
  my ($msg) = @_;
  $issues{$msg} = 1;
  print STDERR "PRECHECK: $msg\n";
};

if (-s $members) {
  my ($has_pipe, $has_nopipe) = (0, 0);
  my %seen_pair;
  open my $M, '<', $members or die "open $members: $!";
  while (<$M>) {
    chomp;
    next unless length;
    $stats{members_rows}++;
    my @f = split /\t/;
    if (@f < 2) {
      $stats{malformed_member_rows}++;
      next;
    }
    my ($fid, $rid) = @f[0,1];
    if ($fid !~ /^FROZEN_[0-9a-fA-F]{32}$/) {
      $stats{noncanonical_frozen_ids_members}++;
    }
    if ($rid =~ /\|/) { $has_pipe = 1; $stats{members_pipe_rows}++; }
    else { $has_nopipe = 1; $stats{members_nopipe_rows}++; }
    my $k = "$fid\t$rid";
    if ($seen_pair{$k}++) {
      $stats{duplicate_member_pairs}++;
    } else {
      $stats{members_unique_pairs}++;
      if ($rid =~ /\|/) { $stats{members_unique_pairs_pipe}++; }
      else { $stats{members_unique_pairs_nopipe}++; }
    }
  }
  close $M;
  $stats{mixed_member_id_styles} = ($has_pipe && $has_nopipe) ? 1 : 0;
} elsif (-e $members) {
  $stats{members_rows} = 0;
}

if (-s $meta) {
  open my $F, '<', $meta or die "open $meta: $!";
  while (<$F>) {
    chomp;
    next unless length;
    $stats{meta_rows}++;
    my @f = split /\t/;
    if (@f < 2) {
      $stats{malformed_meta_rows}++;
      next;
    }
    my $fid = $f[0];
    if ($fid !~ /^FROZEN_[0-9a-fA-F]{32}$/) {
      $stats{noncanonical_frozen_ids_meta}++;
    }
  }
  close $F;
} elsif (-e $meta) {
  $stats{meta_rows} = 0;
}

if ($stats{malformed_member_rows} > 0) {
  $record_issue->("Malformed rows detected in otu_frozen_members.tsv");
}
if ($stats{malformed_meta_rows} > 0) {
  $record_issue->("Malformed rows detected in otu_frozen_meta.tsv");
}
if ($stats{noncanonical_frozen_ids_members} > 0) {
  $record_issue->("Non-canonical frozen IDs detected in otu_frozen_members.tsv");
}
if ($stats{noncanonical_frozen_ids_meta} > 0) {
  $record_issue->("Non-canonical frozen IDs detected in otu_frozen_meta.tsv");
}
if ($stats{mixed_member_id_styles}) {
  $record_issue->("Mixed member ID styles detected (pipe and non-pipe) in otu_frozen_members.tsv");
}
if ($stats{duplicate_member_pairs} > 0) {
  $record_issue->("Duplicate frozen/member pairs detected in otu_frozen_members.tsv");
}

if (-s $active_pool) {
  open my $AP, '<', $active_pool or die "open $active_pool: $!";
  while (<$AP>) {
    chomp;
    next unless /^>/;
    $stats{active_pool_records}++;
    my $id = $_;
    $id =~ s/^>//;
    $id =~ s/\s.*$//;
    if (!defined $id || $id eq '') {
      $stats{active_pool_malformed_ids}++;
      next;
    }
    if ($id =~ /\|/) { $stats{active_pool_pipe_ids}++; }
    else { $stats{active_pool_nopipe_ids}++; }
  }
  close $AP;
}
if ($stats{active_pool_malformed_ids} > 0) {
  $record_issue->("Malformed FASTA headers detected in otu_active_pool.fasta");
}

write_report($report_out, \%stats);

if ($mode eq 'strict' && scalar(keys %issues) > 0) {
  die "Preflight failed in strict mode; see diagnostics above\n";
}

exit 0;
