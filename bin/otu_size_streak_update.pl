#!/usr/bin/env perl
use strict;
use warnings;

my (
  $sizes_tsv,
  $members_tsv,
  $hash_map_tsv,
  $prev_state_tsv,
  $min_rounds,
  $out_prune_ids,
  $out_state_tsv,
  $out_stats_tsv,
) = @ARGV;
my $eligible_size_streak_tsv = $ARGV[8];
my $eligible_counts_tsv = $ARGV[9];
my $min_members = $ARGV[10];

die "usage: $0 otu_sizes.tsv otu_members.tsv otu_hash_map.tsv prev_state.tsv min_rounds out_prune_ids.txt out_state.tsv out_stats.tsv [eligible_size_streak.tsv] [eligible_counts.tsv] [min_members]\n"
  unless @ARGV >= 8 && @ARGV <= 11;

die "invalid min_rounds '$min_rounds' (expected integer >= 1)\n"
  unless defined($min_rounds) && $min_rounds =~ /^[0-9]+$/ && $min_rounds >= 1;

if (!defined $min_members || $min_members !~ /^[0-9]+$/ || $min_members < 1) {
  $min_members = 1;
}

sub otu_marker {
  my ($otu) = @_;
  return 'NA' unless defined $otu;
  if ($otu =~ /^OTUB_[^-]+-(.+)$/) {
    my $m = $1;
    return uc($m) if defined($m) && $m ne '';
  }
  return 'NA';
}

sub stable_key {
  my ($otu, $hash) = @_;
  return '' unless defined $otu && defined $hash;
  return '' if $hash eq '';
  my $marker = otu_marker($otu);
  my $h = lc($hash);
  return join('|', $marker, $h);
}

sub trim_text {
  my ($v) = @_;
  return '' if !defined $v;
  $v =~ s/^\s+//;
  $v =~ s/\s+$//;
  return $v;
}

sub header_index {
  my ($cols_ref, @names) = @_;
  my %idx;
  for my $i (0 .. $#{$cols_ref}) {
    $idx{$cols_ref->[$i]} = $i;
  }
  for my $name (@names) {
    return $idx{$name} if exists $idx{$name};
  }
  return undef;
}

my %size_by_otu;
if (-s $sizes_tsv) {
  open my $SZ, '<', $sizes_tsv or die "open $sizes_tsv: $!";
  while (my $line = <$SZ>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($otu, $n) = split /\t/, $line;
    next unless defined $otu && defined $n;
    next unless $n =~ /^[0-9]+$/;
    $size_by_otu{$otu} = $n;
  }
  close $SZ;
}

my %members_by_otu;
if (-s $members_tsv) {
  open my $MB, '<', $members_tsv or die "open $members_tsv: $!";
  while (my $line = <$MB>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($otu, $rid) = split /\t/, $line;
    next unless defined $otu && defined $rid && $otu ne '' && $rid ne '';
    $members_by_otu{$otu}{$rid} = 1;
  }
  close $MB;
}

my %eligible_count_by_otu;
if (defined($eligible_counts_tsv) && $eligible_counts_tsv ne '' && -s $eligible_counts_tsv) {
  open my $EC, '<', $eligible_counts_tsv or die "open $eligible_counts_tsv: $!";
  my $hdr = <$EC>;
  if (defined $hdr) {
    chomp $hdr;
    my @cols = split /\t/, $hdr, -1;
    my $otu_idx = header_index(\@cols, 'otu_key', 'otu_id', 'OTU_id');
    my $cnt_idx = header_index(\@cols, 'eligible_pool_count', 'eligible_count', 'count');
    if (defined $otu_idx && defined $cnt_idx) {
      while (my $line = <$EC>) {
        chomp $line;
        next if $line =~ /^\s*$/;
        my @f = split /\t/, $line, -1;
        next if $otu_idx > $#f || $cnt_idx > $#f;
        my $otu = trim_text($f[$otu_idx]);
        my $cnt = trim_text($f[$cnt_idx]);
        next if $otu eq '' || uc($otu) eq 'NA';
        next unless $cnt =~ /^\d+$/;
        $eligible_count_by_otu{$otu} = $cnt + 0;
      }
    }
  }
  close $EC;
}

my %hash_by_base;
my %base_hash_ambig;
my $hash_rows_total = 0;
my $hash_rows_used = 0;
my $hash_rows_ambiguous = 0;
if (-s $hash_map_tsv) {
  open my $HM, '<', $hash_map_tsv or die "open $hash_map_tsv: $!";
  while (my $line = <$HM>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($rid_full, $hash) = split /\t/, $line;
    next unless defined $rid_full && defined $hash;
    next if $rid_full eq '' || $hash eq '';
    $hash_rows_total++;
    my ($base) = split /\|/, $rid_full;
    next unless defined $base && $base ne '';
    if (!exists $hash_by_base{$base}) {
      $hash_by_base{$base} = $hash;
      $hash_rows_used++;
      next;
    }
    next if $hash_by_base{$base} eq $hash;
    delete $hash_by_base{$base};
    $base_hash_ambig{$base} = 1;
    $hash_rows_ambiguous++;
  }
  close $HM;
}

my %prev_streak;
my $prev_rows_loaded = 0;
my $prev_rows_legacy_skipped = 0;
if (-s $prev_state_tsv) {
  open my $PS, '<', $prev_state_tsv or die "open $prev_state_tsv: $!";
  while (my $line = <$PS>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($key, $marker, $rid, $otu, $streak) = split /\t/, $line;
    next unless defined $key && defined $streak;
    next unless $streak =~ /^[0-9]+$/;
    my $k = '';
    if (defined $key && $key =~ /^([^|\t]+)\|([0-9a-fA-F]{32})$/) {
      $k = $1 . '|' . lc($2);
    } else {
      $prev_rows_legacy_skipped++;
      next;
    }
    next if !defined $k || $k eq '';
    $prev_streak{$k} = $streak;
    $prev_rows_loaded++;
  }
  close $PS;
}

my $eligible_veto = ($min_members <= 1) ? 2 : $min_members;

my @size_streak_rows;
my %prune_read_ids;
my $otus_total = scalar keys %size_by_otu;
my $otus_size_streak = 0;
my $otus_missing_member = 0;
my $otus_size_streak_missing_hash = 0;
my $size_streak_with_prev = 0;
my $otus_prune_candidate = 0;

for my $otu (sort keys %size_by_otu) {
  my $size = $size_by_otu{$otu};
  if ($min_members <= 1) {
    next unless $size == 1;
  } else {
    next unless $size < $min_members;
  }
  if (exists $eligible_count_by_otu{$otu} && $eligible_count_by_otu{$otu} >= $eligible_veto) {
    next;
  }
  $otus_size_streak++;
  my @rids = sort keys %{ $members_by_otu{$otu} || {} };
  if (@rids != 1) {
    $otus_missing_member++;
    next;
  }
  my $rid = $rids[0];
  my $hash = $hash_by_base{$rid} // '';
  if ($hash eq '') {
    $otus_size_streak_missing_hash++;
    next;
  }
  my $marker = otu_marker($otu);
  my $key = stable_key($otu, $hash);
  next unless defined $key && $key ne '';
  my $prev = $prev_streak{$key} // 0;
  my $streak = $prev + 1;
  $size_streak_with_prev++ if $prev > 0;
  push @size_streak_rows, [$key, $marker, $rid, $otu, $streak];
  if ($streak >= $min_rounds) {
    $otus_prune_candidate++;
    $prune_read_ids{$rid} = 1;
  }
}

open my $OUT_IDS, '>', $out_prune_ids or die "open $out_prune_ids: $!";
for my $rid (sort keys %prune_read_ids) {
  print {$OUT_IDS} "$rid\n";
}
close $OUT_IDS;

open my $OUT_STATE, '>', $out_state_tsv or die "open $out_state_tsv: $!";
for my $r (@size_streak_rows) {
  print {$OUT_STATE} join("\t", @$r), "\n";
}
close $OUT_STATE;

open my $OUT_STATS, '>', $out_stats_tsv or die "open $out_stats_tsv: $!";
print {$OUT_STATS} "min_rounds\t$min_rounds\n";
print {$OUT_STATS} "otus_total\t$otus_total\n";
print {$OUT_STATS} "otus_size_streak\t$otus_size_streak\n";
print {$OUT_STATS} "otus_size_streak_missing_member_rows\t$otus_missing_member\n";
print {$OUT_STATS} "otus_size_streak_missing_hash_rows\t$otus_size_streak_missing_hash\n";
print {$OUT_STATS} "size_streak_with_prev\t$size_streak_with_prev\n";
print {$OUT_STATS} "otus_prune_candidate\t$otus_prune_candidate\n";
print {$OUT_STATS} "reads_prune_candidate\t" . (scalar keys %prune_read_ids) . "\n";
print {$OUT_STATS} "state_rows\t" . scalar(@size_streak_rows) . "\n";
print {$OUT_STATS} "hash_rows_total\t$hash_rows_total\n";
print {$OUT_STATS} "hash_rows_used\t$hash_rows_used\n";
print {$OUT_STATS} "hash_rows_ambiguous_base\t$hash_rows_ambiguous\n";
print {$OUT_STATS} "prev_state_rows_loaded\t$prev_rows_loaded\n";
print {$OUT_STATS} "prev_state_rows_legacy_skipped\t$prev_rows_legacy_skipped\n";
close $OUT_STATS;

exit 0;
