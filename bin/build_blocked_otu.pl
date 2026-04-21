#!/usr/bin/env perl
use strict;
use warnings;

my ($consf, $frozf, $blastf) = @ARGV;
die "usage: build_blocked_otu.pl <consolidated_otu.tsv> <frozen_read_ids.list> <blast_report_annotated_otu.txt>\n"
  unless defined $blastf;

my %cons;
if (open my $C, '<', $consf) {
    while (<$C>) {
        chomp;
        next unless /\S/;
        $cons{$_} = 1;
    }
    close $C;
}

my %froz;
if (open my $F, '<', $frozf) {
    while (<$F>) {
        chomp;
        next unless /\S/;
        $froz{$_} = 1;
    }
    close $F;
}

my %out;
if (open my $B, '<', $blastf) {
    while (<$B>) {
        next if /^#/;
        chomp;
        my @tr = split /\t/;
        my $hdr = $tr[0] // '';
        next unless $hdr ne '';
        my ($rid) = split /\|/, $hdr;
        next unless $rid ne '' && $froz{$rid};

        my $otu = '';
        my @parts = split /\|/, $hdr;
        for my $f (@parts) {
            if ($f =~ /^OTUB_/) {
                $otu = $f;
                last;
            }
        }

        my $target = $parts[1] // '';
        if ($otu ne '' && $otu !~ /-/ && $target ne '' && $target ne 'NA') {
            $otu = "${otu}-${target}";
        }

        my $sample = 'no_adapter_1';
        if ($hdr =~ /adapter=([^|]+)/) {
            $sample = $1 eq '' ? 'no_adapter_1' : $1;
        }

        my $sample_base = $sample;
        if ($sample_base =~ /^(.*)_\d+$/) {
            $sample_base = $1;
        }

        next unless $otu ne '' && ($cons{"$sample\t$otu"} || $cons{"$sample_base\t$otu"});
        $out{"$sample\t$otu"} = 1;
    }
    close $B;
}

for my $k (sort keys %out) {
    print "$k\n";
}
