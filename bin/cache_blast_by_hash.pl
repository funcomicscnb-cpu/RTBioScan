#!/usr/bin/env perl
use strict;
use warnings;
use Digest::MD5 qw(md5_hex);

my ($fasta, $cache, $out_cached, $out_new_fa, $out_hash) = @ARGV;
if (!defined $fasta || !defined $cache || !defined $out_cached || !defined $out_new_fa || !defined $out_hash) {
    die "Usage: cache_blast_by_hash.pl <fasta> <cache.tsv> <out_cached.csv> <out_new.fasta> <out_hash.tsv>\n";
}

my %cache = ();
if (-f $cache) {
    open my $C, '<', $cache or die "Cannot read $cache: $!\n";
    while (my $line = <$C>) {
        chomp $line;
        next if $line eq '';
        my ($h, $sseqid, $evalue, $length, $pident) = split(/\t/, $line);
        next if !defined $h || $h eq '';
        $cache{$h} = join(',', $sseqid // '', $evalue // '', $length // '', $pident // '');
    }
    close $C;
}

open my $OUTC, '>', $out_cached or die "Cannot write $out_cached: $!\n";
open my $OUTN, '>', $out_new_fa or die "Cannot write $out_new_fa: $!\n";
open my $OUTH, '>', $out_hash or die "Cannot write $out_hash: $!\n";

open my $F, '<', $fasta or die "Cannot read $fasta: $!\n";
local $/ = ">";
<$F>; # skip leading empty
while (my $chunk = <$F>) {
    chomp $chunk;
    next if $chunk eq '';
    my ($hdr, @seq) = split(/\n/, $chunk);
    my $seq = join('', @seq);
    $seq =~ s/\s+//g;
    next if $seq eq '';
    my ($id) = split(/\s+/, $hdr);
    my $hash = md5_hex($seq);
    if (exists $cache{$hash}) {
        print $OUTC $id, ',', $cache{$hash}, "\n";
    } else {
        print $OUTN ">", $id, "\n", $seq, "\n";
        print $OUTH $id, "\t", $hash, "\n";
    }
}
close $F;
close $OUTC;
close $OUTN;
close $OUTH;
