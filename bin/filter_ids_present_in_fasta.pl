#!/usr/bin/env perl
use strict;
use warnings;

my ($ids_path, $fasta_path, $out_path, $stats_path) = @ARGV;
if (!defined $ids_path || !defined $fasta_path || !defined $out_path) {
    die "usage: filter_ids_present_in_fasta.pl <ids.list> <reads.fasta> <out_ids.list> [stats_out]\n";
}

sub trim_text {
    my ($v) = @_;
    $v = '' unless defined $v;
    $v =~ s/^\s+|\s+$//g;
    return $v;
}

my %want;
if (-e $ids_path && -s $ids_path) {
    open my $ID, '<', $ids_path or die "open $ids_path: $!";
    while (my $line = <$ID>) {
        chomp $line;
        my $read_id = trim_text($line);
        next if $read_id eq '';
        my ($base) = split /\|/, $read_id, 2;
        next if !defined $base || $base eq '';
        $want{$base} = 1;
    }
    close $ID;
}

open my $OUT, '>', $out_path or die "open $out_path: $!";
if (!%want || !-e $fasta_path || !-s $fasta_path) {
    close $OUT;
    if (defined $stats_path && $stats_path ne '') {
        open my $ST, '>', $stats_path or die "open $stats_path: $!";
        print {$ST} "candidate_ids_total\t" . (scalar keys %want) . "\n";
        print {$ST} "present_in_fasta_count\t0\n";
        close $ST;
    }
    exit 0;
}

my %present;
open my $FA, '<', $fasta_path or die "open $fasta_path: $!";
while (my $line = <$FA>) {
    next unless $line =~ /^>/;
    chomp $line;
    $line =~ s/^>//;
    $line =~ s/\s.*$//;
    my ($base) = split /\|/, $line, 2;
    next if !defined $base || $base eq '';
    next if !exists $want{$base};
    $present{$base} = 1;
}
close $FA;

for my $read_id (sort keys %present) {
    print {$OUT} "$read_id\n";
}
close $OUT;

if (defined $stats_path && $stats_path ne '') {
    open my $ST, '>', $stats_path or die "open $stats_path: $!";
    print {$ST} "candidate_ids_total\t" . (scalar keys %want) . "\n";
    print {$ST} "present_in_fasta_count\t" . (scalar keys %present) . "\n";
    close $ST;
}

exit 0;
