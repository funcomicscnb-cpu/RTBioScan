#!/usr/bin/env perl
use strict;
use warnings;

my ($members, $hash_map, $hash_counts, $out, $mode, $stats_out) = @ARGV;
die "usage: $0 active_members.tsv hash_map.tsv hash_counts.tsv out_counts.tsv [strict|legacy] [stats.tsv]\n"
  unless @ARGV >= 4 && @ARGV <= 6;

$mode = defined($mode) ? lc($mode) : 'strict';
die "Invalid mode '$mode' (expected strict|legacy)\n" unless $mode eq 'strict' || $mode eq 'legacy';
$stats_out = '' unless defined $stats_out;

sub base_read_id {
  my ($id) = @_;
  return '' unless defined $id;
  $id =~ s/^\s+|\s+$//g;
  $id =~ s/^>//;
  $id =~ s/\s.*$//;
  $id =~ s/\|.*$//;
  return $id;
}

sub detect_member_styles {
  my ($path) = @_;
  my ($has_pipe, $has_no_pipe) = (0, 0);
  open my $FH, '<', $path or die "open $path: $!";
  while (<$FH>) {
    chomp;
    next unless length;
    my @f = split /\t/;
    my $rid = $f[1] // '';
    if ($rid =~ /\|/) { $has_pipe = 1; }
    else { $has_no_pipe = 1; }
  }
  close $FH;
  return ($has_pipe, $has_no_pipe);
}

sub write_stats {
  my ($path, $stats) = @_;
  return unless defined $path && length $path;
  open my $S, '>', $path or die "open $path: $!";
  for my $k (sort keys %$stats) {
    print $S join("\t", $k, $stats->{$k}), "\n";
  }
  close $S;
}

my %exact_rid2hash;
my %base2hashes;
open my $HM, '<', $hash_map or die "open $hash_map: $!";
while (<$HM>) {
  chomp;
  next unless length;
  my ($rid, $hash) = split /\t/;
  next unless defined $rid && defined $hash;
  $exact_rid2hash{$rid} = $hash;
  my $base = base_read_id($rid);
  $base2hashes{$base}{$hash} = 1 if length $base;
}
close $HM;

my %hcount;
open my $HC, '<', $hash_counts or die "open $hash_counts: $!";
while (<$HC>) {
  chomp;
  next unless length;
  my ($hash, $cnt) = split /\t/;
  next unless defined $hash;
  $hcount{$hash} = $cnt || 0;
}
close $HC;

my ($members_has_pipe, $members_has_no_pipe) = detect_member_styles($members);
my $members_mixed = ($members_has_pipe && $members_has_no_pipe) ? 1 : 0;
if ($mode eq 'strict' && $members_mixed) {
  die "Strict ID mode: mixed member ID styles detected in $members (contains both pipe and non-pipe IDs)\n";
}
if ($mode eq 'legacy' && $members_mixed) {
  print STDERR "WARN: Legacy ID mode: mixed member ID styles detected in $members; base fallback will be used only for non-exact IDs\n";
}
if ($mode eq 'strict' && $members_has_no_pipe) {
  die "Strict ID mode: non-pipe member IDs detected in $members; use legacy mode for recovery\n";
}

my (%rep, %sum);
my %stats = (
  mode => $mode,
  exact_hits => 0,
  fallback_hits => 0,
  misses => 0,
  ambiguous_ids => 0,
  mixed_input_detected => $members_mixed,
);
my $MAX_UNRESOLVED_SAMPLES = 10;
my @unresolved_samples;
my $unresolved_total = 0;

open my $M, '<', $members or die "open $members: $!";
while (<$M>) {
  chomp;
  next unless length;
  my ($cid, $rid, $is_rep) = split /\t/;
  $rep{$cid} = $rid if $is_rep;

  my $hash = $exact_rid2hash{$rid};
  if (defined $hash) {
    $stats{exact_hits}++;
  } else {
    my $base = base_read_id($rid);
    if ($mode eq 'strict') {
      $stats{misses}++;
      $unresolved_total++;
      push @unresolved_samples, $rid if @unresolved_samples < $MAX_UNRESOLVED_SAMPLES;
      next;
    }
    if (length $base) {
      my @hs = keys %{ $base2hashes{$base} || {} };
      if (@hs > 1) {
        $stats{ambiguous_ids}++;
        die "Ambiguous base read ID '$base' maps to multiple hashes in $hash_map\n";
      }
      if (@hs == 1) {
        $hash = $hs[0];
        $stats{fallback_hits}++;
      } else {
        $stats{misses}++;
      }
    } else {
      $stats{misses}++;
    }
  }

  next unless defined $hash;
  my $cnt = $hcount{$hash} // 0;
  $sum{$cid} += $cnt;
}
close $M;

if ($mode eq 'strict' && $unresolved_total > 0) {
  write_stats($stats_out, \%stats);
  my $sample_txt = join(", ", @unresolved_samples);
  die "Strict ID mode: unresolved member IDs ($unresolved_total total; samples: $sample_txt) (no exact hash map match)\n";
}

open my $O, '>', $out or die "open $out: $!";
for my $cid (sort keys %sum) {
  my $r = $rep{$cid} // '';
  print $O join("\t", $cid, $r, $sum{$cid}), "\n";
}
close $O;

write_stats($stats_out, \%stats);
