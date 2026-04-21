#!/usr/bin/perl

use strict;
use warnings;
use FindBin;

require "$FindBin::Bin/reporting_identity_contract.pl";
require "$FindBin::Bin/reporting_contract_sidecar.pl";

my $fastq_file = $ARGV[0];
my $results_dir = $ARGV[1];
my $round_dir = $ARGV[2];
my $barcode_pipeline = $ARGV[3];

my $report_file = $barcode_pipeline . "_demult_rpt.txt";
my $sidecar_file = $barcode_pipeline . "_demult_rpt.contract.tsv";
my $context = lc($ENV{"RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"} || '');
die "ERROR: RTBIOSCAN_DEMUX_IDENTITY_CONTEXT is required\n" if $context eq '';

sub ensure_dirs {
	for my $dir (
		"single_exp/fastq/hac",
		"single_exp/fastq/sup",
		"single_exp/fastq/fast",
		"$results_dir/single_exp/fastq/hac",
		"$results_dir/single_exp/fastq/sup",
		"$results_dir/single_exp/fastq/fast",
		"$results_dir/single_exp/fasta/hac",
		"$results_dir/single_exp/fasta/sup",
		"$results_dir/single_exp/fasta/fast",
	) {
		next if -d $dir;
		system("mkdir", "-p", $dir) == 0 or die "ERROR: failed to create directory $dir\n";
	}
}

ensure_dirs();

open my $out_fh, '>', $report_file or die "I couldn't open $report_file\n";
print $out_fh ReportingContractSidecar::canonical_header('demult_rpt'), "\n";

my %fastq_files;
my %fasta_files;
my $row_count = 0;

if (-e $fastq_file && -s $fastq_file) {
	open my $in_fh, '<', $fastq_file or die "I couldn't open $fastq_file\n";
	while (1) {
		my $h = <$in_fh>;
		last unless defined $h;
		my $seq = <$in_fh>;
		my $plus = <$in_fh>;
		my $qual = <$in_fh>;
		die "ERROR: malformed FASTQ record in $fastq_file\n"
			if !defined $seq || !defined $plus || !defined $qual;

		my $parsed = ReportingIdentityContract::parse_header($h, $context);
		die "ERROR: unrecognized or context-incompatible annotated read header: $h"
			if !$parsed;

		print $out_fh join(
			"\t",
			$parsed->{read_id},
			$parsed->{barcode_by_homology},
			$parsed->{basecalling_model},
			$parsed->{sample},
			$parsed->{platform},
			$parsed->{sampling_method},
			$parsed->{subsample},
			$parsed->{replicate},
			$parsed->{identity_scope},
			$parsed->{identity_value},
		), "\n";
		$row_count++;

		push @{$fastq_files{$parsed->{basecalling_model}}{$parsed->{sample}}}, $h . $seq . $plus . $qual;
		(my $fasta_header = $h) =~ s/^\@/>/;
		chomp $seq;
		push @{$fasta_files{$parsed->{basecalling_model}}{$parsed->{sample}}}, $fasta_header . $seq . "\n";
	}
	close $in_fh;
}

close $out_fh;

for my $model (keys %fastq_files) {
	for my $sample (keys %{$fastq_files{$model}}) {
		my $round_fastq = "single_exp/fastq/$model/${barcode_pipeline}_${sample}_${model}.fastq";
		open my $fq_fh, '>', $round_fastq or die "I couldn't open $round_fastq\n";
		print {$fq_fh} @{$fastq_files{$model}{$sample}};
		close $fq_fh;

		my $round_fasta = "$results_dir/single_exp/fasta/$model/${barcode_pipeline}_${sample}_${model}.fasta";
		open my $fa_fh, '>', $round_fasta or die "I couldn't open $round_fasta\n";
		print {$fa_fh} @{$fasta_files{$model}{$sample}};
		close $fa_fh;

		my $rolling_gz = "$results_dir/single_exp/fastq/$model/${sample}_${model}.fastq.gz";
		if (!-f $rolling_gz) {
			system "gzip -c $round_fastq > $rolling_gz";
		} else {
			system "gzip -c $round_fastq >> $rolling_gz";
		}
		system "rm $round_fastq";
	}
}

ReportingContractSidecar::write_sidecar_from_report(
	report_kind => 'demult_rpt',
	context => $context,
	report_path => $report_file,
	sidecar_path => $sidecar_file,
);

exit 0;
