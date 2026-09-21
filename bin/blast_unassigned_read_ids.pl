#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use FindBin;
require "$FindBin::Bin/lib/taxon_util.pl";
my $min_level = 'genus';
GetOptions('min-level=s'=>\$min_level) or die "invalid options\n";
$min_level=lc($min_level);
my ($in_path,$out_path)=@ARGV;
die "usage: blast_unassigned_read_ids.pl INPUT OUTPUT [--min-level family|genus|species]\n" unless @ARGV==2;
my ($seen,$assigned)=TaxonUtil::read_assignment_sets($in_path,$min_level);
TaxonUtil::atomic_text($out_path,join('',map { "$_\n" } sort grep { !$assigned->{$_} } keys %$seen));
