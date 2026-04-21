#!/usr/bin/env perl
use strict;
use warnings;

my ($in_report, $out_members, $out_sizes, $out_stats) = @ARGV;
die "usage: $0 in_report.tsv out_members.tsv out_sizes.tsv [out_stats.tsv]\n"
  unless @ARGV >= 3 && @ARGV <= 4;

$out_stats = '' unless defined $out_stats;

sub parse_otu_token {
  my ($v) = @_;
  return '' unless defined $v;
  return '' if $v eq '';
  if ($v =~ /(OTUB_[^|[:space:]]+)/) {
    return $1;
  }
  return '';
}

sub parse_otu_key {
  my ($hdr, $col2, $fields_ref, $otu_col_idx) = @_;
  if (defined $otu_col_idx && $otu_col_idx >= 0 && defined $fields_ref->[$otu_col_idx]) {
    my $by_col = parse_otu_token($fields_ref->[$otu_col_idx]);
    return $by_col if $by_col ne '';
  }
  if (defined $hdr && $hdr ne '') {
    my @tok = split /\|/, $hdr;
    for my $t (@tok) {
      my $cand = parse_otu_token($t);
      return $cand if $cand ne '';
    }
  }
  my $by_col2 = parse_otu_token($col2);
  return $by_col2 if $by_col2 ne '';
  if (defined $fields_ref) {
    for my $f (@$fields_ref) {
      my $cand = parse_otu_token($f);
      return $cand if $cand ne '';
    }
  }
  return '';
}

sub parse_read_id {
  my ($raw, $fields_ref, $read_col_idx) = @_;
  my $hdr = $raw;
  if (defined $read_col_idx && $read_col_idx >= 0 && defined $fields_ref->[$read_col_idx]) {
    $hdr = $fields_ref->[$read_col_idx];
  }
  return '' unless defined $hdr;
  $hdr =~ s/^\s+|\s+$//g;
  return '' if $hdr eq '';
  $hdr =~ s/^>//;
  $hdr =~ s/\s.*$//;
  my ($rid) = split /\|/, $hdr;
  return defined($rid) ? $rid : '';
}

my %seen_pair;
my %size_by_otu;
my $rows_total = 0;
my $rows_kept = 0;
my $rows_skipped = 0;

if (-s $in_report) {
  open my $IN, '<', $in_report or die "open $in_report: $!";
  my $read_col_idx = -1;
  my $otu_col_idx = -1;
  my $header_checked = 0;
  while (my $line = <$IN>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if $line =~ /^#/;
    my @f = split /\t/, $line;
    if (!$header_checked) {
      for my $i (0 .. $#f) {
        if ($f[$i] eq 'read_id') { $read_col_idx = $i; }
        if ($f[$i] eq 'OTU_id')  { $otu_col_idx  = $i; }
      }
      if ($read_col_idx >= 0 && $otu_col_idx >= 0) {
        $header_checked = 1;
        next;
      }
      $header_checked = 1;
    }
    $rows_total++;
    my $hdr = $f[0] // '';
    my $col2 = $f[1] // '';
    my $otu = parse_otu_key($hdr, $col2, \@f, $otu_col_idx);
    my $rid = parse_read_id($hdr, \@f, $read_col_idx);
    if ($otu eq '' || $rid eq '') {
      $rows_skipped++;
      next;
    }
    my $key = join("\t", $otu, $rid);
    next if $seen_pair{$key}++;
    $rows_kept++;
    $size_by_otu{$otu}++;
  }
  close $IN;
}

open my $M, '>', $out_members or die "open $out_members: $!";
for my $k (sort keys %seen_pair) {
  print {$M} "$k\n";
}
close $M;

open my $S, '>', $out_sizes or die "open $out_sizes: $!";
for my $otu (sort keys %size_by_otu) {
  print {$S} join("\t", $otu, $size_by_otu{$otu}), "\n";
}
close $S;

if (defined $out_stats && $out_stats ne '') {
  open my $ST, '>', $out_stats or die "open $out_stats: $!";
  print {$ST} "rows_total\t$rows_total\n";
  print {$ST} "rows_kept_unique_pairs\t$rows_kept\n";
  print {$ST} "rows_skipped_missing_otu_or_read\t$rows_skipped\n";
  print {$ST} "otu_total\t" . (scalar keys %size_by_otu) . "\n";
  close $ST;
}

exit 0;
