#! /usr/bin/perl

$input1 = $ARGV[0];
$thr_spc = $ARGV[1];
$thr_gns = $ARGV[2];


if(-f $ARGV[3])
{
	open PREV_TAX_INDEX, $ARGV[3];
	while(<PREV_TAX_INDEX>)
	{
		push @memory,$_;
		chomp;
		@tr=split/\t/;
		if($tr[$#tr] eq "species"){$species{"$tr[0]"}=$tr[0];}
		$genus{"$tr[0]"}=$tr[1];
		$family{"$tr[0]"}=$tr[2];
		$order{"$tr[0]"}=$tr[3];
		$tax{"$tr[0]"}=$tr[4];
		$done{"$tr[0]"}=1;
	}
	close PREV_TAX_INDEX;
}
open BLASTREPORT, $ARGV[0] or die "I couldn't open $ARGV[0]\n";
while(<BLASTREPORT>)
{
	chomp;
	@tr=split/\;/;
	if(!exists($done{"$tr[1]"})){$todo{$tr[1]}=1;}
}
close BLASTREPORT;

$"="\n";
@todos=keys(%todo);
open OUT_TMP, ">.tmp_taxids" or die "I couldn't open .tmp_taxids";
print  OUT_TMP "@todos";
close OUT_TMP;


system "taxonkit lineage -r -t .tmp_taxids > .tmp_taxids_lineages;taxonkit reformat -f \"{K};{p};{c};{o};{f};{g};{s}\" -P -t -i 2 .tmp_taxids_lineages > .tmp_taxids_lineages_reformat";

open TAXONOMY, ".tmp_taxids_lineages_reformat" or die "I couldn't open .tmp_taxids_lineages_reformat\n";
while(<TAXONOMY>)
{
	chomp;
	@tr=split/\t/;
	@tr2=split/\;/,$tr[5];
	$species{"$tr[0]"}=$tr2[6];
	$genus{"$tr[0]"}=$tr2[5];
	$family{"$tr[0]"}=$tr2[4];
	$order{"$tr[0]"}=$tr2[3];
	$tax{"$tr[0]"}=$tr[3];
	$done{"$tr[0]"}=1;
	push @memory, "$tr[0]\t$genus{$tr[0]}\t$family{$tr[0]}\t$order{$tr[0]}\t$tax{$tr[0]}\n";
}
$"=";";
open BLASTREPORT, $ARGV[0] or die "I couldn't open $ARGV[0]\n";
while(<BLASTREPORT>)
{
	chomp;
	@tr=split/\;/;
	if(exists($species{"$tr[1]"}) && $species{"$tr[1]"} ne '')
	{
		if($tr[$#tr]<$thr_gns)
		{
			if(exists($family{"$tr[1]"}) && $family{"$tr[1]"} ne '')
			{
				$tr[1]=$family{"$tr[1]"};
			}elsif(exists($order{"$tr[1]"}) && $order{"$tr[1]"} ne '')
			{
				$tr[1]=$order{"$tr[1]"};
			}else
			{
				$tr[1]="NA";
			}
		}elsif($tr[$#tr]<$thr_spc)
		{
			if(exists($genus{"$tr[1]"}) && $genus{"$tr[1]"} ne '')
			{
				$tr[1]=$genus{"$tr[1]"};
			}elsif(exists($family{"$tr[1]"}) && $family{"$tr[1]"} ne '')
			{
				$tr[1]=$family{"$tr[1]"};
			}elsif(exists($order{"$tr[1]"}) && $order{"$tr[1]"} ne '')
			{
				$tr[1]=$order{"$tr[1]"};
			}else
			{
				$tr[1]="NA";
			}
		}else
		{
			$tr[1]=$species{"$tr[1]"};
		}
	}elsif(exists($genus{"$tr[1]"}) && $genus{"$tr[1]"} ne '')
	{
		if($tr[$#tr]<$thr_gns)
		{
			if(exists($family{"$tr[1]"}) && $family{"$tr[1]"} ne '')
			{
				$tr[1]=$family{"$tr[1]"};
			}elsif(exists($order{"$tr[1]"}) && $order{"$tr[1]"} ne '')
			{
				$tr[1]=$order{"$tr[1]"};
			}else
			{
				$tr[1]="NA";
			}
		}elsif($tr[$#tr]<$thr_spc)
		{
			$tr[1]=$genus{"$tr[1]"};
		}else
		{
			$tr[1]=$genus{"$tr[1]"};
		}
	}elsif(exists($family{"$tr[1]"}) && $family{"$tr[1]"} ne '')
	{
		$tr[1]=$family{"$tr[1]"};
	}elsif(exists($order{"$tr[1]"}) && $order{"$tr[1]"} ne '')
	{
		$tr[1]=$order{"$tr[1]"};
	}else
	{
		$tr[1]="NA";
	}
	print"@tr\n";
}
close BLASTREPORT;

if($ARGV[3])
{
	open PREV_TAX_INDEX, ">$ARGV[3]" or die "I couldn't open $ARGV[3]\n;";
	print PREV_TAX_INDEX @memory;
	close PREV_TAX_INDEX;
}
