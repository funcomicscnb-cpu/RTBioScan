#!/usr/bin/perl

use strict;
use warnings;

use FindBin;
use lib "$FindBin::Bin/lib";

use RTBioScan::OTURefineBlastreport qw(print_pairs_only_from_cluster_taxids_file);

my ($cluster_taxids_file, $input2) = @ARGV;

if ( !defined $cluster_taxids_file || !-e $cluster_taxids_file || -z $cluster_taxids_file
  || !defined $input2 || !-e $input2 || -z $input2 ) {
    exit 0;
}

print_pairs_only_from_cluster_taxids_file($cluster_taxids_file, $input2, \*STDOUT);
