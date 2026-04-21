#!/usr/bin/perl

use FindBin;
require "$FindBin::Bin/lib/sample_label.pl";

sub normalize_adapter_in_id {
	my ($id) = @_;
	return $id if !defined $id || $id !~ /adapter=/;
	my @parts = split(/\|/, $id, -1);
	for my $p (@parts) {
		if ($p =~ /^adapter=(.*)$/) {
			my $val = $1 // '';
			next if $val eq '';
			my $norm = SampleLabel::canonical_adapter_token($val);
			next unless defined $norm && $norm ne '';
			$p = "adapter=$norm";
		}
	}
	return join("|", @parts);
}

open FILE, $ARGV[0] or die "I couldn't open $ARGV[0]\n";
while(<FILE>)
{
	chomp;
	$_ = normalize_adapter_in_id($_);
	@tr=split/\|/;
	$annotated_id{$tr[0]}=$_;
}
close FILE;
$in=0;
open FILE, $ARGV[1] or die "I couldn't open $ARGV[1]\n";
while(<FILE>)
{
	if(/^\@([\w\-]+)\s/ )
	{
		$in=0;
		if(exists($annotated_id{$1}))
		{
			$a=$1;
			s/$a/$annotated_id{$a}/;
			$in=1;
		}
	}
	if($in)
	{
		print;
	}
	
}
close FILE;
