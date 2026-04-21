#!/usr/bin/perl

use strict;
use warnings;

use FindBin;
use lib "$FindBin::Bin/lib";

use RTBioScan::OTURefineBlastreport qw(print_full_output);

my ($input1, $input2, $non_ncbi_id2lineage) = @ARGV;

if ( !defined $input1 || !-e $input1 || -z $input1
  || !defined $input2 || !-e $input2 || -z $input2 ) {
    exit 0;
}

if ( !defined $non_ncbi_id2lineage || !-e $non_ncbi_id2lineage ) {
    print "#seq_id\ttax_id\tlineage\n";
    exit 0;
}

print_full_output($input1, $input2, $non_ncbi_id2lineage, \*STDOUT);
