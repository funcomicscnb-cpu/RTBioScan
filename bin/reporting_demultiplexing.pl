#!/usr/bin/perl

use FindBin;
require "$FindBin::Bin/lib/sample_label.pl";


$fastq_file = $ARGV[0];
$results_dir = $ARGV[1];
$round_dir=$ARGV[2];
$barcode_pipeline=$ARGV[3];

$report_file=$barcode_pipeline."_demult_rpt.txt";
$identity_mode = lc($ENV{"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE"} || 'collapse');
$demux_mode = lc($ENV{"RTBIOSCAN_DEMULT_MODE"} || 'off');

sub extract_keyed_token {
	my ($header, $key) = @_;
	return '' if !defined $header || !defined $key || $key eq '';
	if ($header =~ /(?:^|\|)\Q$key\E=([^|\s]*)/) {
		return defined($1) ? $1 : '';
	}
	return '';
}

sub derive_identity_fields {
	my ($header, $legacy_sample) = @_;
	my $adapter = extract_keyed_token($header, 'adapter');
	my $adapter_norm = SampleLabel::normalize_sample_label(
		(defined($adapter) && $adapter ne '') ? $adapter : 'no_adapter'
	);

	if ($demux_mode eq 'primers_only' && $adapter ne '' && $adapter_norm ne 'no_adapter') {
		return ('primer', $adapter);
	}
	if ($demux_mode eq 'full' && $identity_mode eq 'track' && $adapter ne '' && $adapter_norm ne 'no_adapter') {
		return ('unit', $adapter);
	}
	if ($demux_mode eq 'full' && defined($legacy_sample) && $legacy_sample ne '' && $legacy_sample ne 'no_adapter') {
		return ('sample', $legacy_sample);
	}
	if ($adapter_norm eq 'no_adapter' || $demux_mode eq 'off') {
		return ('unknown', 'no_adapter');
	}
	return ('unknown', 'unknown');
}

# Use single_exp instead of demultiplexed and mirror structure
if(!-d "single_exp")
{
	system "mkdir -p single_exp/fastq/hac";
	system "mkdir -p single_exp/fastq/sup";
	system "mkdir -p $results_dir/single_exp/fastq/hac";
	system "mkdir -p $results_dir/single_exp/fastq/sup";
	system "mkdir -p $results_dir/single_exp/fasta/hac";
	system "mkdir -p $results_dir/single_exp/fasta/sup";
}

open OUT_FILE, ">$report_file" or die "I couldn't open $report_file\n";
print OUT_FILE "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value\n";
open FILE, $fastq_file or die "I couldn't open $fastq_file\n";

# Parse FASTQ in 4-line blocks to avoid misclassifying sequence/quality lines as headers
while (1) {
    my $h = <FILE>;
    last unless defined $h;
    my $seq = <FILE> // last;
    my $plus = <FILE> // last;
    my $qual = <FILE> // last;

	chomp($h);
	my ($read_id,$barcode_1,$model,$sample,$platform,$sampling_method,$subsample,$replicate);
	my ($identity_scope, $identity_value);

    if ($h =~ /^\@(\S+)\|(\S+)\|(\S+)\|barcode\=\|adapter\=(\S+)$/) {
        $read_id=$1; $barcode_1=$2; $model=$3; $sample=$4;
        $model = ($model =~ /hac/) ? 'hac' : $model;
        $sample = SampleLabel::normalize_sample_label($sample);
        if($sample eq 'no_adapter') {
            ($platform,$sampling_method,$subsample,$replicate) = ('unknown','unknown','unknown','unknown');
        } else {
            my @tr=split(/_/,$sample);
            ($platform,$sampling_method,$subsample,$replicate) = (@tr[0..3]);
        }
    } elsif ($h =~ /^\@(\S+)(?:\|(\S+))?(?:\|(\S+))?$/) {
        $read_id=$1; $barcode_1=defined($2)?$2:'NA'; $model=defined($3)?$3:'hac';
        $sample='no_adapter'; ($platform,$sampling_method,$subsample,$replicate)=('unknown','unknown','unknown','unknown');
        $sample = SampleLabel::normalize_sample_label($sample);
	} else {
		# Unrecognized header; skip this record
		next;
	}

	($identity_scope, $identity_value) = derive_identity_fields($h, $sample);

	print OUT_FILE "$read_id\t$barcode_1\t$model\t$sample\t$platform\t$sampling_method\t$subsample\t$replicate\t$identity_scope\t$identity_value\n";

    # Collect fastq and fasta outputs
    push @{$fastq_files{"$model"}{"$sample"}}, "$h\n$seq$plus$qual";
    $h =~ s/^\@/>/;
    chomp($seq);
    push @{$fasta_files{"$model"}{"$sample"}}, "$h\n$seq\n";
}
close FILE;
close OUT_FILE;


foreach $model(keys(%fastq_files))
{
	foreach $each_sample(keys(%{$fastq_files{$model}}))
	{
		open OUT_FILE, ">single_exp/fastq/$model/$barcode_pipeline\_$each_sample\_$model\.fastq" or die "I couldn't open single_exp/fastq/$model/$barcode_pipeline\_$each_sample\_$model\.fastq\n";
		print OUT_FILE @{$fastq_files{$model}{$each_sample}};
		close OUT_FILE;
		open OUT_FILE, ">$results_dir/single_exp/fasta/$model/$barcode_pipeline\_$each_sample\_$model\.fasta" or die "I couldn't open $results_dir/single_exp/fasta/$model/$barcode_pipeline\_$each_sample\_$model\.fasta\n";
		print OUT_FILE @{$fasta_files{$model}{$each_sample}};
		close OUT_FILE;
		
#		system "gzip -N single_exp/$model/$barcode_pipeline\_$each_sample\_$model\.fastq";
		
		if(!-f "$results_dir/single_exp/fastq/$model/$each_sample\_$model\.fastq.gz")
		{
			system "gzip -c single_exp/fastq/$model/$barcode_pipeline\_$each_sample\_$model\.fastq > $results_dir/single_exp/fastq/$model/$each_sample\_$model\.fastq.gz";
		}else
		{
			system "gzip -c single_exp/fastq/$model/$barcode_pipeline\_$each_sample\_$model\.fastq >> $results_dir/single_exp/fastq/$model/$each_sample\_$model\.fastq.gz";
		}
		system "rm single_exp/fastq/$model/$barcode_pipeline\_$each_sample\_$model\.fastq";
	}
}
