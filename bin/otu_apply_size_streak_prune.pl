#!/usr/bin/env perl
use strict;
use warnings;

my ($in_fasta, $prune_ids_file, $out_fasta, $out_stats) = @ARGV;
die "usage: $0 in.fasta prune_ids.txt out.fasta out_stats.tsv\n" unless @ARGV == 4;

my %prune_id;
if (defined $prune_ids_file && -s $prune_ids_file) {
  open my $PID, '<', $prune_ids_file or die "open $prune_ids_file: $!";
  while (my $line = <$PID>) {
    chomp $line;
    $line =~ s/^\s+|\s+$//g;
    next if $line eq '';
    $prune_id{$line} = 1;
  }
  close $PID;
}
my $prune_ids_total = scalar keys %prune_id;

my $reads_total = 0;
my $reads_kept = 0;
my $reads_pruned = 0;
my %matched_id;

open my $OUT, '>', $out_fasta or die "open $out_fasta: $!";

if (defined $in_fasta && -s $in_fasta) {
  open my $IN, '<', $in_fasta or die "open $in_fasta: $!";
  my $header = '';
  my @seq = ();

  my $flush = sub {
    return if $header eq '';
    $reads_total++;
    my $rid = $header;
    $rid =~ s/^>//;
    $rid =~ s/\s.*$//;
    my ($base) = split /\|/, $rid;
    $base = '' unless defined $base;
    if ($base ne '' && exists $prune_id{$base}) {
      $reads_pruned++;
      $matched_id{$base} = 1;
    } else {
      $reads_kept++;
      print {$OUT} $header, "\n";
      print {$OUT} @seq if @seq;
    }
    $header = '';
    @seq = ();
  };

  while (my $line = <$IN>) {
    if ($line =~ /^>/) {
      $flush->();
      chomp $line;
      $header = $line;
      next;
    }
    push @seq, $line if $header ne '';
  }
  $flush->();
  close $IN;
}

close $OUT;

my $prune_ids_matched = scalar keys %matched_id;
my $prune_ids_unmatched = $prune_ids_total - $prune_ids_matched;

open my $ST, '>', $out_stats or die "open $out_stats: $!";
print {$ST} "reads_total\t$reads_total\n";
print {$ST} "reads_pruned\t$reads_pruned\n";
print {$ST} "reads_kept\t$reads_kept\n";
print {$ST} "prune_ids_total\t$prune_ids_total\n";
print {$ST} "prune_ids_matched\t$prune_ids_matched\n";
print {$ST} "prune_ids_unmatched\t$prune_ids_unmatched\n";
close $ST;

exit 0;
