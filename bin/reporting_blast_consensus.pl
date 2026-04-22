#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
require "$FindBin::Bin/lib/sample_label.pl";
require "$FindBin::Bin/lib/taxon_util.pl";

my ($blast_read, $blastreport_consensus, $barcode_pipeline, $consolidated_ids_file) = @ARGV;

if (!defined $blast_read || !defined $blastreport_consensus || !defined $barcode_pipeline) {
    die "Usage: reporting_blast_consensus.pl <blast_read_csv> <consensus_tax_tsv> <barcode> [consolidated_ids]\n";
}

my $report_blast_consensus_file = $barcode_pipeline . "_blast_consensus_tax_rpt.txt";
my $report_blast_consensus_consolidated_file = $barcode_pipeline . "_blast_consensus_tax_consolidated_rpt.txt";
my $dropped_rows_invalid_sseqid_format = 0;

sub parse_hit_taxid {
    my ($raw) = @_;
    $raw = '' if !defined $raw;
    $raw =~ s/^\s+//;
    $raw =~ s/\s+$//;
    return ('', '', 'invalid') if $raw eq '';
    if ($raw =~ /^(\d+)$/) {
        my $taxid = $1;
        return ($taxid, $taxid, 'numeric');
    }
    if ($raw =~ /(\S+)\|kraken:taxid\|(\d+)/) {
        return ($1, $2, 'kraken');
    }
    return ('', '', 'invalid');
}

my %consolidated;
if (defined $consolidated_ids_file && -s $consolidated_ids_file) {
    open(my $CID, '<', $consolidated_ids_file) or die "I couldn't open $consolidated_ids_file\n";
    while (my $line = <$CID>) {
        chomp $line;
        $line =~ s/^\s+//;
        $line =~ s/\s+$//;
        $line =~ s/^>//;
        next unless $line =~ /\S/;
        $consolidated{$line} = 1;
    }
    close $CID;
}

open(my $OUT, '>', $report_blast_consensus_file) or die "I couldn't open $report_blast_consensus_file\n";
my $OUTC;
if (%consolidated) {
    open($OUTC, '>', $report_blast_consensus_consolidated_file) or die "I couldn't open $report_blast_consensus_consolidated_file\n";
}
print $OUT join("\t",
    "consensus_id",
    "otu_key",
    "barcode_by_homology",
    "basecalling_model",
    "number_of_reads",
    "sample",
    "taxid",
    "blast_hit",
    "aln_length",
    "perc_id",
    "consensus_kingdom",
    "consensus_phylum",
    "consensus_class",
    "consensus_order",
    "consensus_family",
    "consensus_genus",
    "consensus_species",
) . "\n";
if ($OUTC) {
    print $OUTC join("\t",
        "consensus_id",
        "otu_key",
        "barcode_by_homology",
        "basecalling_model",
        "number_of_reads",
        "sample",
        "taxid",
        "blast_hit",
        "aln_length",
        "perc_id",
        "consensus_kingdom",
        "consensus_phylum",
        "consensus_class",
        "consensus_order",
        "consensus_family",
        "consensus_genus",
        "consensus_species",
    ) . "\n";
}

# blast_read is produced by the pipeline as a header-only CSV when there are no BLAST results.
# Older versions wrote a literal "Waiting" sentinel; do not rely on that.
my %read_blast;
open(my $IN, '<', $blast_read) or die "I couldn't open $blast_read\n";
while (my $line = <$IN>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if $line =~ /^qseqid[,;]/; # header-only placeholder

    # BLAST outfmt=10 is comma-delimited. Some upstream steps may use semicolons as delimiters.
    my @tr = (index($line, ',') >= 0) ? split(/,/, $line) : split(/;/, $line);
    next unless @tr >= 5;

    my $qseqid     = $tr[0];
    my $sseqid     = $tr[1];
    my $aln_length = $tr[3];
    my $perc_id    = $tr[4];
    next if !defined $qseqid || $qseqid eq '';

    $read_blast{$qseqid} = {
        long_taxid  => $sseqid,
        aln_length  => $aln_length,
        perc_id     => $perc_id,
    };
}
close $IN;

# No BLAST rows -> keep header-only output.
if (!%read_blast) {
    close $OUT;
    exit 0;
}

# No consensus taxonomy -> keep header-only output.
if (!-s $blastreport_consensus) {
    close $OUT;
    exit 0;
}

open($IN, '<', $blastreport_consensus) or die "I couldn't open $blastreport_consensus\n";
my $header_flag = 1;
while (my $line = <$IN>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    if ($header_flag) {
        $header_flag = 0;
        next;
    }

    my @tr = split(/\t/, $line, -1);
    my $qseqid = $tr[0];
    next if !defined $qseqid || $qseqid eq '';
    next unless exists $read_blast{$qseqid};

    # qseqid format: sample|ConsensusNN|TARGET|reads-N (may include extra tokens)
    my @parts = split(/\|/, $qseqid);
    my $sample = $parts[0] // '';
    my $consensus = $parts[1] // '';
    my $reads = '';
    for my $p (@parts) {
        if ($p =~ /^reads\-(\d+)/) { $reads = $1; last; }
    }
    $sample = SampleLabel::normalize_sample_label($sample);
    next unless $sample ne '' && $consensus ne '' && $reads ne '';
    my $barcode_1 = '';
    my $otu_key = '';
    if ($qseqid =~ /\|OTU=([^|]+)/) {
        $otu_key = $1;
        if ($otu_key =~ /-(.+)$/) { $barcode_1 = $1; }
    }
    if ($barcode_1 eq '') {
        for my $p (@parts) {
            my $marker = TaxonUtil::marker_from_token($p);
            if (defined $marker && $marker ne '' && $marker ne 'OTHER') {
                $barcode_1 = $marker;
                last;
            }
        }
    }
    if ($barcode_1 eq '') {
        my $marker = TaxonUtil::marker_from_token($parts[2] // '');
        $barcode_1 = (defined $marker && $marker ne '' && $marker ne 'OTHER') ? $marker : ($parts[2] // 'NA');
    }

    my $consensus_id = $consensus . "_" . $sample;
    my $model = "consensus";

    my $long_taxid = $read_blast{$qseqid}->{long_taxid} // '';
    my ($hit_id, $taxid, $taxid_parse_mode) = parse_hit_taxid($long_taxid);
    if ($taxid_parse_mode eq 'invalid') {
        $dropped_rows_invalid_sseqid_format++;
        next;
    }

    # consensus taxonomy TSV from the pipeline:
    #   long_seq_id, consensus_taxid, kingdom, phylum, class, order, family, genus, species
    my ($kingdom, $phylum, $class, $order, $family, $genus, $species) = @tr[2..8];
    $kingdom = '' if !defined $kingdom;
    $phylum  = '' if !defined $phylum;
    $class   = '' if !defined $class;
    $order   = '' if !defined $order;
    $family  = '' if !defined $family;
    $genus   = '' if !defined $genus;
    $species = '' if !defined $species;

    my $out_line = join("\t",
        $consensus_id,
        $otu_key,
        $barcode_1,
        $model,
        $reads,
        $sample,
        $taxid,
        $hit_id,
        ($read_blast{$qseqid}->{aln_length} // ''),
        ($read_blast{$qseqid}->{perc_id} // ''),
        $kingdom,
        $phylum,
        $class,
        $order,
        $family,
        $genus,
        $species,
    );
    print $OUT $out_line, "\n";
    if ($OUTC && (exists $consolidated{$consensus_id} || exists $consolidated{$qseqid})) {
        print $OUTC $out_line, "\n";
    }
}
close $IN;
close $OUT;
close $OUTC if $OUTC;
warn "METRIC: dropped_rows_invalid_sseqid_format=$dropped_rows_invalid_sseqid_format\n";
