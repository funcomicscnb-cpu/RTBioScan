#!/usr/bin/env perl
use strict;
use warnings;
use Digest::MD5 qw(md5_hex);

my ($counts, $rep_fa, $meta, $frozen_fa, $hist, $members, $promoted_out,
    $round_id, $min_rounds, $min_reads, $window, $drop_ratio, $min_frac) = @ARGV;

die "usage: $0 counts.tsv rep_fasta frozen_meta.tsv frozen_reps.fasta frozen_history.tsv frozen_members.tsv promoted_clusters.tsv round_id min_rounds min_reads window drop_ratio min_frac\n"
  unless @ARGV == 13;

my %frozen_hash;
if (-s $meta) {
  open my $M, '<', $meta or die "open $meta: $!";
  while (<$M>) {
    chomp; next unless length;
    my @p = split /\t/;
    my $h = $p[2];
    $frozen_hash{$h} = 1 if defined $h && length $h;
  }
  close $M;
}

# Load rep sequences
my %repseq;
open my $FA, '<', $rep_fa or die "open $rep_fa: $!";
my ($h, $s) = ('','');
while (<$FA>) {
  chomp;
  if (/^>/) {
    if ($h && $s ne '') { $repseq{$h} = $s; }
    $h = substr($_,1); $s = '';
  } else {
    $s .= $_;
  }
}
if ($h && $s ne '') { $repseq{$h} = $s; }
close $FA;

# Load history
my %hist;
if (-s $hist) {
  open my $H, '<', $hist or die "open $hist: $!";
  while (<$H>) {
    chomp; next unless length;
    my ($key, @vals) = split /\t/;
    $hist{$key} = \@vals if defined $key && length $key;
  }
  close $H;
}

# Load counts
my %cluster;
my $total = 0;
open my $C, '<', $counts or die "open $counts: $!";
while (<$C>) {
  chomp; next unless length;
  my ($cid, $rep, $n) = split /\t/;
  $n ||= 0;
  $cluster{$cid} = { rep => $rep, n => $n };
  $total += $n;
}
close $C;

# Update history per cluster hash
my %cluster_hash;
for my $cid (keys %cluster) {
  my $rep = $cluster{$cid}{rep} || '';
  my $seq = $repseq{$rep} || '';
  next unless $seq ne '';
  my $hash = md5_hex(uc($seq));
  $cluster_hash{$cid} = $hash;
  push @{ $hist{$hash} ||= [] }, $cluster{$cid}{n};
}

# Decide promotions
my @promoted;
open my $M_OUT,  '>>', $meta      or die "open $meta: $!";
open my $FF_OUT, '>>', $frozen_fa or die "open $frozen_fa: $!";
open my $FM_OUT, '>>', $members   or die "open $members: $!";
for my $cid (sort keys %cluster) {
  my $hash = $cluster_hash{$cid} or next;
  next if $frozen_hash{$hash};

  my $arr = $hist{$hash} || [];
  my $rounds = scalar(@$arr);
  my $reads = 0; $reads += $_ for @$arr;
  next if $rounds < $min_rounds;
  next if $reads < $min_reads;
  if ($total > 0 && $min_frac > 0) {
    my $frac = $cluster{$cid}{n} / $total;
    next if $frac < $min_frac;
  }

  if ($rounds >= $window) {
    my $ok = 1;
    for (my $i = $rounds-$window+1; $i < $rounds; $i++) {
      my $prev = $arr->[$i-1] || 0;
      my $cur  = $arr->[$i] || 0;
      next if $prev == 0;
      my $ratio = ($cur / $prev);
      if ($ratio < (1.0 - $drop_ratio)) { $ok = 0; last; }
    }
    next unless $ok;
  }

  my $frozen_id = "FROZEN_${hash}";
  push @promoted, $cid;
  $frozen_hash{$hash} = 1;

  print $M_OUT  join("\t", $frozen_id, $cluster{$cid}{rep}, $hash, $round_id), "\n";
  print $FF_OUT ">$frozen_id|$cluster{$cid}{rep}\n$repseq{$cluster{$cid}{rep}}\n";
  print $FM_OUT join("\t", $frozen_id, $cluster{$cid}{rep}, 1), "\n";
}
close $M_OUT;
close $FF_OUT;
close $FM_OUT;

# Write promoted cluster IDs
open my $P, '>', $promoted_out or die "open $promoted_out: $!";
print $P join("\n", @promoted), "\n" if @promoted;
close $P;

# Write updated history
open my $H2, '>', $hist or die "open $hist: $!";
for my $key (sort keys %hist) {
  print $H2 join("\t", $key, @{ $hist{$key} }), "\n";
}
close $H2;
