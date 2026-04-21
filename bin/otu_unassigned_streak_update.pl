#!/usr/bin/env perl
# otu_unassigned_streak_update.pl — track OTUs that remain unassigned across
# consecutive rounds and emit their member read IDs once the streak threshold
# is reached.
#
# Stable identity: marker|md5hash of the representative sequence (identical to
# the stable_key used by otu_size_streak_update.pl).  The representative is the
# member whose UUID is present in the hash map (built from the nr/rep FASTA).
# If no hash map is supplied, tracking degrades to the OTUB_N key (unstable).
#
# Usage:
#   otu_unassigned_streak_update.pl \
#     <otu_sizes_round.tsv>  <otu_members_round.tsv> \
#     <blast_evidence_file>  <prev_streak_state.tsv> \
#     <min_rounds>  <min_size>  <max_size> \
#     <out_prune_ids>  <out_stats.tsv>  [out_next_state.tsv]  [hash_map.tsv]
use strict;
use warnings;
use FindBin;
require "$FindBin::Bin/lib/taxon_util.pl";
*trim_text           = \&TaxonUtil::trim_text;
*is_numeric_taxid    = \&TaxonUtil::is_numeric_taxid;
*is_assigned_kingdom = \&TaxonUtil::is_assigned_kingdom;
*is_assigned_lineage = \&TaxonUtil::is_assigned_lineage;

my (
  $sizes_tsv,
  $members_tsv,
  $evidence_file,
  $prev_state_tsv,
  $min_rounds,
  $min_size,
  $max_size,
  $out_prune_ids,
  $out_stats_tsv,
) = @ARGV;
my $out_next_state = $ARGV[9];
my $hash_map_tsv   = $ARGV[10];

die "usage: $0 otu_sizes.tsv otu_members.tsv blast_evidence.tsv prev_state.tsv min_rounds min_size max_size out_prune_ids out_stats.tsv [out_next_state.tsv] [hash_map.tsv]\n"
  unless @ARGV >= 9 && @ARGV <= 11;

for my $int_arg ([$min_rounds, 'min_rounds'], [$min_size, 'min_size'], [$max_size, 'max_size']) {
  die "invalid $int_arg->[1] '$int_arg->[0]' (expected integer >= 1)\n"
    unless defined $int_arg->[0] && $int_arg->[0] =~ /^[0-9]+$/ && $int_arg->[0] >= 1;
}
die "max_size must be >= min_size\n" unless $max_size >= $min_size;

# --------------------------------------------------------------------------
# Stable-key helpers (mirrors otu_size_streak_update.pl)
# --------------------------------------------------------------------------
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
  return '' unless defined $otu && defined $hash && $hash ne '';
  my $marker = otu_marker($otu);
  return join('|', $marker, lc($hash));
}

sub is_stable_key {
  my ($k) = @_;
  return defined($k) && $k =~ /^[^|\t]+\|[0-9a-fA-F]{32}$/;
}

sub normalize_header_key {
  my ($v) = @_;
  $v = trim_text($v);
  $v =~ s/^#+//;
  return lc($v);
}

# --------------------------------------------------------------------------
# Load hash map: uuid_base -> md5hash  (built from the nr/rep FASTA)
# --------------------------------------------------------------------------
my %hash_by_base;
my $hash_map_loaded = 0;
if (defined $hash_map_tsv && $hash_map_tsv ne '' && -s $hash_map_tsv) {
  open my $HM, '<', $hash_map_tsv or die "open $hash_map_tsv: $!";
  while (my $line = <$HM>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($rid_full, $hash) = split /\t/, $line;
    next unless defined $rid_full && defined $hash && $rid_full ne '' && $hash ne '';
    my ($base) = split /\|/, $rid_full;
    next unless defined $base && $base ne '';
    # Last-wins for duplicates (ambiguous hashes handled at query time)
    $hash_by_base{$base} = $hash;
  }
  close $HM;
  $hash_map_loaded = 1;
}

# --------------------------------------------------------------------------
# Load OTU sizes
# --------------------------------------------------------------------------
my %size_by_otu;
if (-s $sizes_tsv) {
  open my $SZ, '<', $sizes_tsv or die "open $sizes_tsv: $!";
  while (my $line = <$SZ>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($otu, $n) = split /\t/, $line;
    next unless defined $otu && defined $n && $n =~ /^[0-9]+$/;
    $size_by_otu{$otu} = $n + 0;
  }
  close $SZ;
}

# --------------------------------------------------------------------------
# Load OTU members; for each OTU also resolve the stable key via hash map
# --------------------------------------------------------------------------
my %members_by_otu;   # otu -> { uuid -> 1 }
my %stable_by_otu;    # otu -> stable_key (or '' if hash unavailable)
if (-s $members_tsv) {
  open my $MB, '<', $members_tsv or die "open $members_tsv: $!";
  while (my $line = <$MB>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($otu, $rid) = split /\t/, $line;
    next unless defined $otu && defined $rid && $otu ne '' && $rid ne '';
    # Store UUID (part before first |) for prune output
    my ($uuid) = split /\|/, $rid;
    next unless defined $uuid && $uuid ne '';
    $members_by_otu{$otu}{$uuid} = 1;
    # If this member is the representative (present in hash map), resolve key
    if ($hash_map_loaded && !exists $stable_by_otu{$otu}) {
      my $hash = $hash_by_base{$uuid} // '';
      if ($hash ne '') {
        my $sk = stable_key($otu, $hash);
        $stable_by_otu{$otu} = $sk if $sk ne '';
      }
    }
  }
  close $MB;
}

# --------------------------------------------------------------------------
# Determine per-OTU assignment from blast evidence
# --------------------------------------------------------------------------
my %otu_assigned;
if (-s $evidence_file) {
  open my $EV, '<', $evidence_file or die "open $evidence_file: $!";
  my $first = <$EV>;
  if (defined $first) {
    chomp $first;
    my @hdr_cols = split /\t/, $first, -1;
    my %idx;
    for my $i (0 .. $#hdr_cols) {
      my $k = normalize_header_key($hdr_cols[$i]);
      $idx{$k} = $i if $k ne '';
    }

    my $has_header = do {
      my $h0 = normalize_header_key($hdr_cols[0] // '');
      ($h0 eq 'read_id' || $h0 eq 'seq_id' || $h0 eq 'long_read_id') ? 1 : 0;
    };

    my $read_i = $has_header
      ? (exists $idx{'read_id'} ? $idx{'read_id'}
        : (exists $idx{'seq_id'} ? $idx{'seq_id'}
        : (exists $idx{'long_read_id'} ? $idx{'long_read_id'} : 0)))
      : 0;
    my $otu_i = $has_header
      ? (exists $idx{'otu_id'} ? $idx{'otu_id'}
        : (exists $idx{'otu'} ? $idx{'otu'} : undef))
      : undef;
    my $taxid_i = $has_header
      ? (exists $idx{'otu_taxid'} ? $idx{'otu_taxid'}
        : (exists $idx{'taxid'} ? $idx{'taxid'}
        : (exists $idx{'tax_id'} ? $idx{'tax_id'} : undef)))
      : 1;
    my $kingdom_i = $has_header
      ? (exists $idx{'otu_kingdom'} ? $idx{'otu_kingdom'} : (exists $idx{'kingdom'} ? $idx{'kingdom'} : undef))
      : undef;
    my $lineage_i = $has_header
      ? (exists $idx{'lineage'} ? $idx{'lineage'} : (exists $idx{'otu_lineage'} ? $idx{'otu_lineage'} : undef))
      : 2;

    my $parse_row = sub {
      my ($line) = @_;
      return if !defined $line || $line =~ /^\s*$/;
      my @f = split /\t/, $line, -1;
      return if $read_i > $#f;

      # Determine OTU key from explicit column or from OTUB_ token in read_id
      my $otu_key = '';
      if (defined $otu_i && $otu_i <= $#f) {
        $otu_key = trim_text($f[$otu_i]);
        $otu_key = '' if $otu_key eq '' || uc($otu_key) eq 'NA';
      }
      if ($otu_key eq '') {
        my $rid = $f[$read_i] // '';
        for my $tok (split /\|/, $rid) {
          if ($tok =~ /^OTUB_/) {
            $otu_key = $tok;
            last;
          }
        }
      }
      return if $otu_key eq '';

      # Check assignment
      my $assigned = 0;
      if (defined $taxid_i && $taxid_i <= $#f) {
        $assigned = 1 if is_numeric_taxid($f[$taxid_i]);
      }
      if (!$assigned && defined $kingdom_i && $kingdom_i <= $#f) {
        $assigned = 1 if is_assigned_kingdom($f[$kingdom_i]);
      }
      if (!$assigned && defined $lineage_i && $lineage_i <= $#f) {
        $assigned = 1 if is_assigned_lineage($f[$lineage_i]);
      }
      $otu_assigned{$otu_key} = 1 if $assigned;
    };

    $parse_row->($first) unless $has_header;
    while (my $line = <$EV>) {
      chomp $line;
      $parse_row->($line);
    }
  }
  close $EV;
}

# --------------------------------------------------------------------------
# Load previous streak state: stable_key -> streak_count
# Legacy OTU-key entries (no |hexhash suffix) are silently skipped so that
# the first run after the upgrade starts fresh rather than mis-tracking.
# --------------------------------------------------------------------------
my %prev_streak;
if (-s $prev_state_tsv) {
  open my $PS, '<', $prev_state_tsv or die "open $prev_state_tsv: $!";
  while (my $line = <$PS>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($key, $streak) = split /\t/, $line, 2;
    next unless defined $key && defined $streak;
    next unless $streak =~ /^[0-9]+$/;
    next unless is_stable_key($key);   # skip legacy OTUB_N-COI entries
    $prev_streak{$key} = $streak + 0;
  }
  close $PS;
}

# --------------------------------------------------------------------------
# Compute new streaks and prune candidates
# --------------------------------------------------------------------------
my %new_streak;      # stable_key -> new_streak_count
my %prune_uuids;
my $otus_total           = scalar keys %size_by_otu;
my $otus_in_range        = 0;
my $otus_assigned        = 0;
my $otus_missing_hash    = 0;
my $otus_streak_tracked  = 0;
my $otus_prune_candidate = 0;

for my $otu (sort keys %size_by_otu) {
  my $size = $size_by_otu{$otu};
  next if $size < $min_size || $size > $max_size;
  $otus_in_range++;

  if ($otu_assigned{$otu}) {
    $otus_assigned++;
    # Streak resets on assignment — do not save to new state
    next;
  }

  # Resolve stable key
  my $sk = $stable_by_otu{$otu} // '';
  if ($sk eq '') {
    if ($hash_map_loaded) {
      $otus_missing_hash++;
    }
    # Without a stable key we cannot reliably track streak; skip
    next;
  }

  my $prev   = $prev_streak{$sk} // 0;
  my $streak = $prev + 1;
  $new_streak{$sk} = $streak;
  $otus_streak_tracked++;

  if ($streak >= $min_rounds) {
    $otus_prune_candidate++;
    for my $uuid (keys %{ $members_by_otu{$otu} || {} }) {
      $prune_uuids{$uuid} = 1;
    }
  }
}

# --------------------------------------------------------------------------
# Write outputs
# --------------------------------------------------------------------------
open my $OUT_IDS, '>', $out_prune_ids or die "open $out_prune_ids: $!";
for my $uuid (sort keys %prune_uuids) {
  print {$OUT_IDS} "$uuid\n";
}
close $OUT_IDS;

open my $OUT_STATS, '>', $out_stats_tsv or die "open $out_stats_tsv: $!";
print {$OUT_STATS} "min_rounds\t$min_rounds\n";
print {$OUT_STATS} "min_size\t$min_size\n";
print {$OUT_STATS} "max_size\t$max_size\n";
print {$OUT_STATS} "hash_map_loaded\t$hash_map_loaded\n";
print {$OUT_STATS} "otus_total\t$otus_total\n";
print {$OUT_STATS} "otus_in_size_range\t$otus_in_range\n";
print {$OUT_STATS} "otus_assigned\t$otus_assigned\n";
print {$OUT_STATS} "otus_missing_hash\t$otus_missing_hash\n";
print {$OUT_STATS} "otus_streak_tracked\t$otus_streak_tracked\n";
print {$OUT_STATS} "otus_prune_candidate\t$otus_prune_candidate\n";
print {$OUT_STATS} "reads_prune_candidate\t" . (scalar keys %prune_uuids) . "\n";
close $OUT_STATS;

if (defined $out_next_state && $out_next_state ne '') {
  open my $OUT_STATE, '>', $out_next_state or die "open $out_next_state: $!";
  for my $sk (sort keys %new_streak) {
    print {$OUT_STATE} "$sk\t$new_streak{$sk}\n";
  }
  close $OUT_STATE;
}

exit 0;
