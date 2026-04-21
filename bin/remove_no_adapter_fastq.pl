#!/usr/bin/perl

$i=0;
while(<STDIN>)
{
	if(/^@.*no_adapter/)
	{
		$i=1;
		next;
	}elsif($i>0 && $i<4)
	{
		$i++;
		next;
	}else
	{
		$i=0;
		print;
	}
}
close FILE;

