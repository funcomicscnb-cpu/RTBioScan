#!/usr/bin/env perl
use strict;
use warnings;

if (@ARGV != 1) {
    die "usage: $0 <blast_report_annotated_otu_full.txt>\n";
}

my ($blast_report) = @ARGV;

open my $fh, '<', $blast_report or die "ERROR: unable to read blast report: $blast_report\n";

my $observed_no_adapter = 0;
my $observed_non_no_adapter = 0;

sub trim_text {
    my ($value) = @_;
    $value = '' unless defined $value;
    $value =~ s/^\s+//;
    $value =~ s/\s+$//;
    return $value;
}

sub is_no_adapter {
    my ($adapter) = @_;
    $adapter = trim_text($adapter);
    return $adapter =~ /^no_adapter(?:_[0-9]+)?$/i ? 1 : 0;
}

sub normalize_sample_base {
    my ($sample) = @_;
    $sample = trim_text($sample);
    return undef if $sample eq '';
    return 'no_adapter' if is_no_adapter($sample);
    $sample =~ s/_[0-9]+$//;
    $sample =~ s/_[0-9]+_/_/;
    $sample = trim_text($sample);
    return undef if $sample eq '';
    return $sample;
}

sub valid_barcoded_sample {
    my ($adapter) = @_;
    return undef if is_no_adapter($adapter);
    my $normalized = normalize_sample_base($adapter);
    return undef if !defined($normalized) || $normalized eq '' || lc($normalized) eq 'no_adapter';
    return $normalized;
}

while (my $line = <$fh>) {
    chomp $line;
    next if $line eq '';
    my ($read_id) = split /\t/, $line, 2;
    next if !defined($read_id) || $read_id eq '';
    my ($adapter) = $read_id =~ /adapter=([^\s|]+)/;
    next if !defined($adapter) || $adapter eq '';
    if (is_no_adapter($adapter)) {
        $observed_no_adapter = 1;
    } elsif (defined valid_barcoded_sample($adapter)) {
        $observed_non_no_adapter = 1;
    }
    last if $observed_no_adapter && $observed_non_no_adapter;
}

my $separate_no_adapter = ($observed_no_adapter && $observed_non_no_adapter) ? 1 : 0;

print "observed_no_adapter\t$observed_no_adapter\n";
print "observed_non_no_adapter\t$observed_non_no_adapter\n";
print "separate_no_adapter\t$separate_no_adapter\n";
