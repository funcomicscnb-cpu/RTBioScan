#!/usr/bin/env perl
use strict;
use warnings;

my ($promoted, $members, $meta, $hash_map, $out, $mode, $mixed_policy, $unsafe_flag) = @ARGV;
die "usage: $0 promoted_clusters.tsv active_members.tsv frozen_meta.tsv hash_map.tsv out_snapshot.tsv [strict|legacy] [pipe_only|error|emit_all] [allow_unsafe]\n"
  unless @ARGV >= 5 && @ARGV <= 8;

$mode = defined($mode) ? lc($mode) : 'strict';
die "Invalid mode '$mode' (expected strict|legacy)\n" unless $mode eq 'strict' || $mode eq 'legacy';
$mixed_policy = defined($mixed_policy) && length($mixed_policy) ? lc($mixed_policy) : 'pipe_only';
die "Invalid mixed policy '$mixed_policy' (expected pipe_only|error|emit_all)\n"
  unless $mixed_policy eq 'pipe_only' || $mixed_policy eq 'error' || $mixed_policy eq 'emit_all';
$unsafe_flag = defined($unsafe_flag) && length($unsafe_flag) ? lc($unsafe_flag) : 'false';
my $allow_unsafe = ($unsafe_flag eq '1' || $unsafe_flag eq 'true' || $unsafe_flag eq 'yes') ? 1 : 0;

# Output contract:
# - Column 1: canonical frozen ID (FROZEN_<hash>)
# - Column 2: read ID emitted from hash_map canonical rows (full IDs preferred)
# - Column 3: representative flag (1/0)
# Base read IDs are lookup fallback only.

sub base_read_id {
  my ($id) = @_;
  return '' unless defined $id;
  $id =~ s/^\s+|\s+$//g;
  $id =~ s/^>//;
  $id =~ s/\s.*$//;
  $id =~ s/\|.*$//;
  return $id;
}

my %prom;
if (-s $promoted) {
  open my $P, '<', $promoted or die "open $promoted: $!";
  while (<$P>) {
    chomp;
    next unless length;
    $prom{$_} = 1;
  }
  close $P;
}

open my $M, '<', $members or die "open $members: $!";
my %cluster_rep;
my @rows;
my ($members_has_pipe, $members_has_no_pipe) = (0, 0);
while (<$M>) {
  chomp;
  next unless length;
  my ($cid, $rid, $is_rep) = split /\t/;
  next unless $prom{$cid};
  push @rows, [$cid, $rid, $is_rep];
  if ($is_rep && !$cluster_rep{$cid}) {
    $cluster_rep{$cid} = $rid;
  }
  if ($rid =~ /\|/) { $members_has_pipe = 1; }
  else { $members_has_no_pipe = 1; }
}
close $M;

if ($mode eq 'strict' && $members_has_no_pipe) {
  die "Strict ID mode: non-pipe member IDs detected in promoted members from $members\n";
}
if ($mode eq 'legacy' && $members_has_pipe && $members_has_no_pipe) {
  print STDERR "WARN: Legacy ID mode: mixed promoted member ID styles detected in $members\n";
}

my %exact_rid2hash;
my %base2hashes;
my %hash2pipe;
my %hash2nopipe;
open my $HM, '<', $hash_map or die "open $hash_map: $!";
while (<$HM>) {
  chomp;
  next unless length;
  my ($rid, $hash) = split /\t/;
  next unless defined $rid && defined $hash;
  $exact_rid2hash{$rid} = $hash;
  my $base = base_read_id($rid);
  $base2hashes{$base}{$hash} = 1 if length $base;
  if ($rid =~ /\|/) {
    $hash2pipe{$hash}{$rid} = 1;
  } else {
    $hash2nopipe{$hash}{$rid} = 1;
  }
}
close $HM;

my %hash2emit;
for my $hash (keys %hash2pipe, keys %hash2nopipe) {
  my $has_pipe = exists $hash2pipe{$hash};
  my $has_nopipe = exists $hash2nopipe{$hash};
  if ($has_pipe && !$has_nopipe) {
    # Single-style pipe hash maps should always emit pipe IDs.
    $hash2emit{$hash} = [ sort keys %{ $hash2pipe{$hash} } ];
  } elsif (!$has_pipe && $has_nopipe) {
    $hash2emit{$hash} = [ sort keys %{ $hash2nopipe{$hash} } ];
  } else {
    # Mixed hash_map styles for the same hash.
    if ($mode eq 'strict') {
      die "Strict ID mode: mixed hash_map ID styles for hash '$hash' in $hash_map\n";
    }
    if ($mixed_policy eq 'error') {
      die "Legacy ID mode: mixed hash_map ID styles for hash '$hash' with mixed_policy=error\n";
    }
    if ($mixed_policy eq 'emit_all' && !$allow_unsafe) {
      die "Legacy ID mode: mixed_policy=emit_all requires allow_unsafe=true\n";
    }
    if ($mixed_policy eq 'pipe_only') {
      print STDERR "WARN: Legacy ID mode: mixed hash_map ID styles for hash '$hash'; emitting pipe IDs only\n";
      $hash2emit{$hash} = [ sort keys %{ $hash2pipe{$hash} } ];
    } elsif ($mixed_policy eq 'emit_all') {
      print STDERR "WARN: Legacy ID mode: mixed hash_map ID styles for hash '$hash'; emitting all IDs (unsafe)\n";
      my %merged = (%{ $hash2pipe{$hash} }, %{ $hash2nopipe{$hash} });
      $hash2emit{$hash} = [ sort keys %merged ];
    } else {
      # Defensive fallback for future policy changes.
      $hash2emit{$hash} = [ sort keys %{ $hash2pipe{$hash} } ];
    }
  }
}

my %rep_exact2frozen;
my %repbase2frozen;
if (-s $meta) {
  open my $F, '<', $meta or die "open $meta: $!";
  while (<$F>) {
    chomp;
    next unless length;
    my ($frozen_id, $rep_id) = split /\t/;
    next unless defined $frozen_id && defined $rep_id;
    $rep_exact2frozen{$rep_id} = $frozen_id;
    my $base = base_read_id($rep_id);
    $repbase2frozen{$base}{$frozen_id} = 1 if length $base;
  }
  close $F;
}

my $tmp_out = "$out.tmp.$$";
open my $O, '>', $tmp_out or die "open $tmp_out: $!";
my %seen;
my $MAX_UNRESOLVED_SAMPLES = 10;
my @unresolved_samples;
my $unresolved_total = 0;
for my $row (@rows) {
  my ($cid, $rid, $is_rep) = @$row;
  my $rep_id = $cluster_rep{$cid};
  next unless defined $rep_id && length $rep_id;

  my $frozen_id = $rep_exact2frozen{$rep_id};
  if (!defined $frozen_id) {
    my $rep_base = base_read_id($rep_id);
    my @fids = keys %{ $repbase2frozen{$rep_base} || {} };
    if (@fids > 1) {
      die "Ambiguous representative base ID '$rep_base' maps to multiple frozen IDs in $meta\n";
    }
    $frozen_id = $fids[0] if @fids == 1;
  }
  next unless defined $frozen_id;

  my $hash = $exact_rid2hash{$rid};
  if (!defined $hash) {
    if ($mode eq 'strict') {
      $unresolved_total++;
      push @unresolved_samples, $rid if @unresolved_samples < $MAX_UNRESOLVED_SAMPLES;
      next;
    }
    my $rid_base = base_read_id($rid);
    my @hs = keys %{ $base2hashes{$rid_base} || {} };
    if (@hs > 1) {
      die "Ambiguous base read ID '$rid_base' maps to multiple hashes in $hash_map\n";
    }
    $hash = $hs[0] if @hs == 1;
  }
  next unless defined $hash;

  my $rep_base = base_read_id($rep_id);
  for my $mrid (@{ $hash2emit{$hash} || [] }) {
    my $mrid_base = base_read_id($mrid);
    my $flag = ($mrid eq $rep_id || (length($rep_base) && $mrid_base eq $rep_base)) ? 1 : 0;
    my $key = join("\t", $frozen_id, $mrid);
    next if $seen{$key}++;
    print $O join("\t", $frozen_id, $mrid, $flag), "\n";
  }
}
close $O;
if ($mode eq 'strict' && $unresolved_total > 0) {
  unlink $tmp_out;
  my $sample_txt = join(", ", @unresolved_samples);
  die "Strict ID mode: unresolved promoted members ($unresolved_total total; samples: $sample_txt) (no exact hash map match)\n";
}
rename $tmp_out, $out or die "rename $tmp_out -> $out: $!";
