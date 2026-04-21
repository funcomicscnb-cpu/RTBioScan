#!/usr/bin/perl

use strict;
use warnings;
use Time::HiRes qw(time);

use FindBin;
use lib "$FindBin::Bin/lib";

use RTBioScan::OTURefineBlastreport qw(append_phase_timing_row emit_annotated_entries);

my ($pairs_file, $lineage_file) = @ARGV;
my $phase_file = $ENV{OTU_REFINE_PHASE_TIMINGS_FILE} // '';
my $round_id = $ENV{OTU_REFINE_ROUND_ID} // '';
my $stats_file = $ENV{OTU_REFINE_ANNOTATE_STATS_FILE} // '';
my $t_annotate_total_start = time();

if ( !defined $pairs_file || !-e $pairs_file || -z $pairs_file ) {
    print "#seq_id\ttax_id\tlineage\n";
    if ($stats_file ne '' && open(my $stats_fh, '>', $stats_file)) {
        print {$stats_fh} "key\tvalue\nannotated_rows\t0\nannotated_bytes\t22\nannotated_file_bytes\t22\n";
        close $stats_fh;
    }
    append_phase_timing_row($phase_file, $round_id, 'annotate_total', $t_annotate_total_start, time());
    exit 0;
}

my @entries;
my $t_pairs_load_start = time();
open(my $pairs_fh, '<', $pairs_file) or die "Cannot open $pairs_file: $!";
while (my $line = <$pairs_fh>) {
    chomp $line;
    next if $line !~ /\S/;
    my ($seq_id, $taxid) = split /\t/, $line, 2;
    next if !defined $seq_id;
    $taxid = '' if !defined $taxid;
    push @entries, [ $seq_id, $taxid ];
}
close $pairs_fh;
my $t_pairs_load_end = time();
append_phase_timing_row($phase_file, $round_id, 'pairs_load', $t_pairs_load_start, $t_pairs_load_end);

my %annotate_stats = (
    annotated_rows  => 0,
    annotated_bytes => 0,
);
emit_annotated_entries(\@entries, $lineage_file, \*STDOUT, \%annotate_stats);

if ($stats_file ne '' && open(my $stats_fh, '>', $stats_file)) {
    print {$stats_fh} "key\tvalue\n";
    # annotated_rows excludes the header; annotated_file_bytes includes it.
    print {$stats_fh} "annotated_rows\t$annotate_stats{annotated_rows}\n";
    print {$stats_fh} "annotated_bytes\t$annotate_stats{annotated_bytes}\n";
    print {$stats_fh} "annotated_file_bytes\t$annotate_stats{annotated_bytes}\n";
    close $stats_fh;
}
append_phase_timing_row($phase_file, $round_id, 'annotate_total', $t_annotate_total_start, time());
