#!/usr/bin/perl


$summary_file = $ARGV[0];
$sam_file = $ARGV[1];
$on_target_report = $ARGV[2];
$min_length = $ARGV[3];
$max_length = $ARGV[4];
$min_qscore = $ARGV[5];
$barcode_pipeline=$ARGV[6];

$report_file=$barcode_pipeline."_read_info_rpt.txt";
$header_flag=1;

open FILE, $summary_file or die "I couldn't open $summary_file\n";
while(<FILE>)
{
	chomp;
	@tr=split/\t/;

	if ($header_flag)
	{
		for($i=0;$i<@tr;$i++)
		{
			$header{$tr[$i]}=$i;
		}
		# Support both Dorado <1.x ("filename") and >=1.x ("input_filename")
		$fn_col = exists $header{"input_filename"} ? "input_filename" : "filename";
		$header_flag=0;
	}else
	{
		$hq_list{$tr[$header{"read_id"}]}=$tr[$header{"sequence_length_template"}]."\t".$tr[$header{"mean_qscore_template"}];
	}
}
close FILE;

$header_flag=1;
open FILE, $on_target_report or die "I couldn't open $on_target_report\n";
while(<FILE>)
{
	chomp;
	@tr=split/\t/;
	if ($header_flag)
	{
		$header_line=$_;
		for($i=0;$i<@tr;$i++)
		{
			$header{$tr[$i]}=$i;
		}
		open OUT_FILE, ">", "$report_file" or die "I couldn't open $report_file\n";
		print  OUT_FILE "$header_line\thac_length\thac_mean_qscore\n";

		$header_flag=0;
	}else
	{
		print OUT_FILE $_."\t";
		if(exists($hq_list{$tr[$header{"read_id"}]}))
		{
			print OUT_FILE $hq_list{$tr[$header{"read_id"}]}."\n";
		}else
		{
			print OUT_FILE "NA\tNA\n";
		}
		
	}
}
close FILE;

 

$fastq_name=$tr[$header{$fn_col}];
$fastq_name=~s/\.pod5$/_on_target_hac.fastq/;
system "samtools fastq $sam_file > $fastq_name";
