#!/usr/bin/env perl
use strict;
use warnings;
use Digest::MD5 qw(md5_hex);

my ($old_pool, $new_pool, $out_pool, $stats_out, $decisions_out, $include_hash_flag) = @ARGV;
die "usage: $0 old_pool.fasta new_pool.fasta out_pool.fasta [stats.tsv] [decisions.tsv] [include_hash]\n"
  unless @ARGV >= 3 && @ARGV <= 6;
$stats_out = '' unless defined $stats_out;
$decisions_out = '' unless defined $decisions_out;
$include_hash_flag = '' unless defined $include_hash_flag;
my $include_hash = ($include_hash_flag =~ /^(1|true|yes)$/i) ? 1 : 0;

sub read_token_id {
  my ($hdr) = @_;
  return '' unless defined $hdr;
  $hdr =~ s/^\s+|\s+$//g;
  $hdr =~ s/^>//;
  $hdr =~ s/\s.*$//;
  return $hdr;
}

sub base_read_id {
  my ($hdr) = @_;
  my $id = read_token_id($hdr);
  return '' unless length $id;
  if (index($id, '|') >= 0) {
    ($id) = split /\|/, $id, 2;
  }
  return $id;
}

sub model_from_header {
  my ($hdr) = @_;
  my $id = read_token_id($hdr);
  return '' unless length $id;
  my @f = split /\|/, $id;
  my $model = $f[2] // '';
  $model = lc($model);
  $model = 'hac' if $model eq 'hac_fixed' || $model eq 'hac2sup';
  return $model;
}

sub model_rank {
  my ($model) = @_;
  return 3 if $model eq 'sup';
  return 2 if $model eq 'hac';
  return 1 if $model eq 'fast';
  return 0;
}

sub better_record {
  my ($cand, $curr) = @_;
  return 1 if $cand->{rank} > $curr->{rank};
  return 0 if $cand->{rank} < $curr->{rank};
  return 1 if $cand->{source_rank} > $curr->{source_rank};
  return 0 if $cand->{source_rank} < $curr->{source_rank};
  # Deterministic tie-break independent of ingestion order.
  return ($cand->{header} cmp $curr->{header}) < 0 ? 1 : 0;
}

my %best;
my %best_new;
my %stats = (
  old_records       => 0,
  new_records       => 0,
  replaced_by_rank  => 0,
  replaced_by_new   => 0,
  replaced_by_lex   => 0,
);

sub ingest_fasta {
  my ($path, $source, $source_rank, $best_ref, $best_new_ref, $stats_ref) = @_;
  return unless defined $path && -s $path;
  open my $IN, '<', $path or die "open $path: $!";
  my ($hdr, $seq) = ('', '');
  while (<$IN>) {
    chomp;
    s/\r$//;
    if (/^>/) {
      if (length $hdr && length $seq) {
        _ingest_record($hdr, $seq, $source, $source_rank, $best_ref, $best_new_ref, $stats_ref);
      }
      $hdr = $_;
      $seq = '';
    } else {
      next unless length $hdr;
      $seq .= $_;
    }
  }
  if (length $hdr && length $seq) {
    _ingest_record($hdr, $seq, $source, $source_rank, $best_ref, $best_new_ref, $stats_ref);
  }
  close $IN;
}

sub _ingest_record {
  my ($hdr, $seq, $source, $source_rank, $best_ref, $best_new_ref, $stats_ref) = @_;
  my $id = read_token_id($hdr);
  return unless length $id;
  my $base = base_read_id($id);
  return unless length $base;
  my $model = model_from_header($id);
  my $rank = model_rank($model);

  if ($source eq 'old') {
    $stats_ref->{old_records}++;
  } else {
    $stats_ref->{new_records}++;
  }

  my $cand = {
    header      => $id,
    seq         => $seq,
    rank        => $rank,
    source      => $source,
    source_rank => $source_rank,
  };
  if ($source eq 'new') {
    if (!exists $best_new_ref->{$base} || better_record($cand, $best_new_ref->{$base})) {
      $best_new_ref->{$base} = $cand;
    }
  }

  if (!exists $best_ref->{$base}) {
    $best_ref->{$base} = $cand;
    return;
  }

  my $curr = $best_ref->{$base};
  return unless better_record($cand, $curr);

  if ($cand->{rank} > $curr->{rank}) {
    $stats_ref->{replaced_by_rank}++;
  } elsif ($cand->{source_rank} > $curr->{source_rank}) {
    $stats_ref->{replaced_by_new}++;
  } else {
    $stats_ref->{replaced_by_lex}++;
  }
  $best_ref->{$base} = $cand;
}

ingest_fasta($old_pool, 'old', 0, \%best, \%best_new, \%stats);
ingest_fasta($new_pool, 'new', 1, \%best, \%best_new, \%stats);

open my $OUT, '>', $out_pool or die "open $out_pool: $!";
for my $base (sort { $best{$a}{header} cmp $best{$b}{header} } keys %best) {
  my $r = $best{$base};
  print $OUT '>', $r->{header}, "\n", $r->{seq}, "\n";
}
close $OUT;

$stats{kept_records} = scalar(keys %best);

if (defined $stats_out && length $stats_out) {
  open my $S, '>', $stats_out or die "open $stats_out: $!";
  for my $k (sort keys %stats) {
    print $S join("\t", $k, $stats{$k}), "\n";
  }
  close $S;
}

if (defined $decisions_out && length $decisions_out) {
  open my $D, '>', $decisions_out or die "open $decisions_out: $!";
  for my $base (sort keys %best) {
    my $sel = $best{$base};
    my $new = $best_new{$base};
    my $decision = 'no_new';
    if ($new) {
      $decision = ($sel->{source} eq 'new') ? 'kept_new' : 'dropped_new';
    }
    my $sel_hash = $include_hash ? md5_hex(uc($sel->{seq})) : '';
    my $new_hash = ($include_hash && $new) ? md5_hex(uc($new->{seq})) : '';
    print $D join(
      "\t",
      $base,
      $sel->{header},
      $sel->{source},
      $sel->{rank},
      $sel_hash,
      ($new ? $new->{header} : ''),
      ($new ? $new->{rank} : ''),
      $new_hash,
      $decision
    ), "\n";
  }
  close $D;
}

exit 0;
