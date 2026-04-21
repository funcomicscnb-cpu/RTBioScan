#! /usr/bin/perl

use strict;
use warnings;

my ($blast_report, $fasta_path) = @ARGV;
die "usage: $0 blast_report fasta\n" if !defined $blast_report || !defined $fasta_path;

my %wanted;
open my $BLASTREPORT, '<', $blast_report or die "I couldn't open $blast_report\n";
while (my $line = <$BLASTREPORT>) {
    chomp $line;
    next if $line !~ /\S/;
    my ($read_id) = split /\|/, $line, 2;
    next if !defined $read_id || $read_id eq '';
    $wanted{$read_id} = 1;
}
close $BLASTREPORT;

open my $FASTA, '<', $fasta_path or die "I couldn't open $fasta_path\n";
my $emit_current = 0;
while (my $line = <$FASTA>) {
    if ($line =~ /^>/) {
        $emit_current = 0;
        my $header = substr($line, 1);
        chomp $header;
        my ($read_id) = split /\|/, $header, 2;
        if (defined $read_id && exists $wanted{$read_id}) {
            $emit_current = 1;
            print $line;
        }
        next;
    }

    print $line if $emit_current;
}
close $FASTA;
