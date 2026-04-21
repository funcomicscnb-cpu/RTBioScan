#!/usr/bin/perl


$summary_file = $ARGV[0];
$sam_file = $ARGV[1];
$blast_result = $ARGV[2];
$on_target_list = $ARGV[3];
$min_length = $ARGV[4];
$max_length = $ARGV[5];
$barcode_pipeline=$ARGV[6];

$target_report_file=$barcode_pipeline."_on_target_rpt.txt";
$read_report_file=$barcode_pipeline."_read_info_rpt.txt";

$header_flag=1;
open FILE, $blast_result or die "I couldn't open $blast_result\n";
while(<FILE>)
{
	chomp;
	@tr=split/\t/;
	if ($header_flag)
	{
		$header{"read_id"}=0;
		$header{"name"}=1;
		$header{"kingdom_perc_identity"}=2;
		$header{"kingdom_aln_length"}=3;
		$header_flag=0;
	}
	
	@tr2 = split /\|/, $tr[$header{"name"}];
	
	$read_info{$tr[$header{"read_id"}]}{"barcode"}=$tr2[0];
	$read_info{$tr[$header{"read_id"}]}{"kingdom"}=$tr2[1];
	$read_info{$tr[$header{"read_id"}]}{"kingdom_perc_identity"}=$tr[$header{"kingdom_perc_identity"}];
	$read_info{$tr[$header{"read_id"}]}{"kingdom_aln_length"}=$tr[$header{"kingdom_aln_length"}];
	
}
close FILE;

open FILE, $on_target_list or die "I couldn't open $on_target_list\n";
while(<FILE>)
{
	chomp;
	@tr=split /\|/;
	$target_list{$tr[0]}=1;
}
close FILE;

open OUT_READS_FILE, ">", "$read_report_file" or die "I couldn't open $read_report_file\n";
print  OUT_READS_FILE "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\n";

open OUT_TARGET_FILE, ">", "$target_report_file" or die "I couldn't open $target_report_file\n";
print  OUT_TARGET_FILE "read_id\tqc_filter\tbarcode\tkingdom\tkingdom_perc_identity\tkingdom_aln_length\ton_target_kingdom\n";

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
		print OUT_READS_FILE $tr[$header{"read_id"}]."\t".$tr[$header{$fn_col}]."\t".$tr[$header{"run_id"}]."\t";
		if(exists($read_info{$tr[$header{"read_id"}]}{"barcode"}))# and ($read_info{$tr[$header{"read_id"}]}{"barcode"} eq "ITS2" || $read_info{$tr[$header{"read_id"}]}{"barcode"} eq "COI"))
		{
			print OUT_READS_FILE $read_info{$tr[$header{"read_id"}]}{"barcode"};
		}else
		{
			print OUT_READS_FILE "unmatched";
		}
		
		print OUT_READS_FILE "\t".$tr[$header{"sequence_length_template"}]."\t".$tr[$header{"mean_qscore_template"}]."\n";
		
		if(exists($read_info{$tr[$header{"read_id"}]}))
		{
			print OUT_TARGET_FILE $tr[$header{"read_id"}]."\tIN\t".$read_info{$tr[$header{"read_id"}]}{"barcode"}."\t".$read_info{$tr[$header{"read_id"}]}{"kingdom"}."\t".$read_info{$tr[$header{"read_id"}]}{"kingdom_perc_identity"}."\t".$read_info{$tr[$header{"read_id"}]}{"kingdom_aln_length"}."\t";
			
		}else
		{
			if($tr[$header{"sequence_length_template"}] < $min_length || $tr[$header{"sequence_length_template"}] > $max_length)
			{
				print OUT_TARGET_FILE $tr[$header{"read_id"}]."\tOUT\tNA\tNA\tNA\tNA\t";
			}else
			{
				print OUT_TARGET_FILE $tr[$header{"read_id"}]."\tIN\tUNKNOWN\tNA\tNA\tOFF_TARGET\t";
			}
		}
		if(exists($target_list{$tr[$header{"read_id"}]})){print OUT_TARGET_FILE "ON_TARGET\n";}
		else{print OUT_TARGET_FILE "OFF_TARGET\n";}
	}
}
close OUT_READS_FILE;
close OUT_TARGET_FILE;
$fastq_name=$tr[$header{$fn_col}];
$fastq_name=~s/\.pod5$/_fast.fastq.gz/;
system "samtools fastq $sam_file | gzip > $fastq_name";
