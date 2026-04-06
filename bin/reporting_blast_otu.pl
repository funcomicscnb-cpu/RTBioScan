#!/usr/bin/perl

use strict;
use warnings;
use FindBin;

require "$FindBin::Bin/reporting_identity_contract.pl";

my $summary_file = $ARGV[0];
my $blast_read = $ARGV[1];
my $blastreport_otu = $ARGV[2];
my $pre_read_info = $ARGV[3];
my $barcode_pipeline = $ARGV[4];
my $context = lc($ENV{"RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"} || '');
die "ERROR: RTBIOSCAN_DEMUX_IDENTITY_CONTEXT is required\n" if $context eq '';

my $report_file = $barcode_pipeline . "_read_info_rpt.txt";
my $report_blast_otu_file = $barcode_pipeline . "_blast_otu_pretax_rpt.txt";
my $default_read_info_header = "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore";

my %hq_list;
my %header;
my $header_flag = 1;

open my $summary_fh, '<', $summary_file or die "I couldn't open $summary_file\n";
while (my $line = <$summary_fh>) {
	chomp $line;
	my @tr = split /\t/, $line, -1;
	if ($header_flag) {
		for (my $i = 0; $i < @tr; $i++) {
			$header{$tr[$i]} = $i;
		}
		$header_flag = 0;
		next;
	}
	$hq_list{$tr[$header{"read_id"}]} = $tr[$header{"sequence_length_template"}] . "\t" . $tr[$header{"mean_qscore_template"}];
}
close $summary_fh;

$header_flag = 1;
open my $pre_fh, '<', $pre_read_info or die "I couldn't open $pre_read_info\n";
open my $report_fh, '>', $report_file or die "I couldn't open $report_file\n";
while (my $line = <$pre_fh>) {
	chomp $line;
	my @tr = split /\t/, $line, -1;
	if ($header_flag) {
		my %pre_header;
		for (my $i = 0; $i < @tr; $i++) {
			$pre_header{$tr[$i]} = $i;
		}
		%header = %pre_header;
		print $report_fh $line, "\tsup_length\tsup_mean_qscore\n";
		$header_flag = 0;
		next;
	}
	print $report_fh $line, "\t";
	if (exists $hq_list{$tr[$header{"read_id"}]}) {
		print $report_fh $hq_list{$tr[$header{"read_id"}]}, "\n";
	} else {
		print $report_fh "NA\tNA\n";
	}
}
if ($header_flag) {
	print $report_fh $default_read_info_header, "\tsup_length\tsup_mean_qscore\n";
}
close $pre_fh;
close $report_fh;

my %read_blast;
my $blast_read_rows = 0;
open my $blast_read_fh, '<', $blast_read or die "I couldn't open $blast_read\n";
while (my $line = <$blast_read_fh>) {
	chomp $line;
	next if $line eq '';
	my @csv = split(/,/, $line, -1);
	next if @csv < 5;
	next if $csv[0] eq 'qseqid';
	$blast_read_rows++;
	my $uuid = (split(/\|/, $csv[0]))[0];
	my @sseq = split(/\|/, $csv[1], -1);
	$read_blast{$uuid} = {
		hit_id => $sseq[0] // '',
		taxid => $sseq[2] // '',
		aln_length => $csv[3],
		perc_id => $csv[4],
	};
}
close $blast_read_fh;

open my $out_fh, '>', $report_blast_otu_file or die "I couldn't open $report_blast_otu_file\n";
print $out_fh "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n";

my $blastreport_rows = 0;
my $output_rows = 0;
open my $otu_fh, '<', $blastreport_otu or die "I couldn't open $blastreport_otu\n";
while (my $line = <$otu_fh>) {
	chomp $line;
	$line =~ s/\r$//;
	$line =~ s/\\t/\t/g;
	my @tr = split /\t/, $line, -1;
	next if !@tr;
	next if $tr[0] eq 'long_read_id' || $tr[0] eq '#seq_id' || $tr[0] eq 'seq_id' || $tr[0] eq 'read_id';
	next if $line eq '';
	$blastreport_rows++;
	my $parsed = ReportingIdentityContract::parse_header($tr[0], $context);
	die "ERROR: unrecognized or context-incompatible BLAST long_read_id: $tr[0]\n"
		if !$parsed;
	next if !exists $read_blast{$parsed->{read_id}};
	print $out_fh join(
		"\t",
		$tr[0],
		$parsed->{barcode_by_homology},
		$parsed->{basecalling_model},
		$parsed->{sample},
		$read_blast{$parsed->{read_id}}{hit_id},
		$read_blast{$parsed->{read_id}}{taxid},
		$read_blast{$parsed->{read_id}}{aln_length},
		$read_blast{$parsed->{read_id}}{perc_id},
		$parsed->{otu_id},
		$tr[1],
		$tr[2],
		$tr[3],
		$tr[4],
		$tr[5],
		$tr[6],
		$tr[7],
		$tr[8],
	), "\n";
	$output_rows++;
}
close $otu_fh;
close $out_fh;

if ($blastreport_rows == 0 && $blast_read_rows == 0) {
	exit 0;
}

die "ERROR: non-empty BLAST inputs collapsed to zero joined rows\n"
	if $output_rows == 0;

exit 0;
