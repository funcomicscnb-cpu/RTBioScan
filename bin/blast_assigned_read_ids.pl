#!/usr/bin/env perl
use strict;
use warnings;
use FindBin;
require "$FindBin::Bin/lib/taxon_util.pl";
*trim_text         = \&TaxonUtil::trim_text;
*is_numeric_taxid  = \&TaxonUtil::is_numeric_taxid;
*is_assigned_kingdom = \&TaxonUtil::is_assigned_kingdom;
*is_assigned_lineage = \&TaxonUtil::is_assigned_lineage;

my ($in_path, $out_path) = @ARGV;
if (!defined $in_path || !defined $out_path) {
    die "usage: blast_assigned_read_ids.pl <blast_annotated_tsv> <out_assigned_ids.list>\n";
}

sub normalize_header_key {
    my ($v) = @_;
    $v = trim_text($v);
    $v =~ s/^#+//;
    return lc($v);
}

open my $OUT, '>', $out_path or die "open $out_path: $!";

if (!-e $in_path || !-s $in_path) {
    close $OUT;
    exit 0;
}

open my $IN, '<', $in_path or do {
    close $OUT;
    die "open $in_path: $!";
};

my $first = <$IN>;
if (!defined $first) {
    close $IN;
    close $OUT;
    exit 0;
}
chomp $first;

my $raw_blast_semicolon = ($first =~ /;/ && $first !~ /\t/) ? 1 : 0;
my @first_cols = $raw_blast_semicolon ? split(/;/, $first, -1) : split(/\t/, $first, -1);
my %idx;
for my $i (0 .. $#first_cols) {
    my $k = normalize_header_key($first_cols[$i]);
    $idx{lc($k)} = $i if $k ne '';
}

my $header0 = normalize_header_key($first_cols[0] // '');
my $has_header = (
    $header0 eq 'read_id'
    || $header0 eq 'seq_id'
    || $header0 eq 'long_read_id'
) ? 1 : 0;
my $read_i = $has_header
    ? (
        exists($idx{'read_id'}) ? $idx{'read_id'}
        : (exists($idx{'seq_id'}) ? $idx{'seq_id'}
        : (exists($idx{'long_read_id'}) ? $idx{'long_read_id'} : 0))
    )
    : 0;
my $taxid_i = $has_header
    ? (
        exists($idx{'otu_taxid'}) ? $idx{'otu_taxid'}
        : (exists($idx{'taxid'}) ? $idx{'taxid'}
        : (exists($idx{'tax_id'}) ? $idx{'tax_id'} : undef))
    )
    : 1;
my $kingdom_i = $raw_blast_semicolon
    ? undef
    : ($has_header
    ? (exists($idx{'otu_kingdom'}) ? $idx{'otu_kingdom'} : (exists($idx{'kingdom'}) ? $idx{'kingdom'} : undef))
    : undef);
my $lineage_i = $raw_blast_semicolon
    ? undef
    : ($has_header
    ? (exists($idx{'lineage'}) ? $idx{'lineage'} : (exists($idx{'otu_lineage'}) ? $idx{'otu_lineage'} : undef))
    : 2);

my %assigned;
my $input_rows = 0;

my $consume_line = sub {
    my ($line) = @_;
    return if !defined $line;
    return if $line =~ /^\s*$/;
    my @f = $raw_blast_semicolon ? split(/;/, $line, -1) : split(/\t/, $line, -1);
    return if $read_i > $#f;
    my $rid_raw = trim_text($f[$read_i]);
    return if $rid_raw eq '';
    my ($uuid) = split /\|/, $rid_raw;
    return if !defined $uuid || $uuid eq '';
    $input_rows++;

    my $assigned_flag = 0;
    if (defined $taxid_i && $taxid_i <= $#f) {
        $assigned_flag = 1 if is_numeric_taxid($f[$taxid_i]);
    }
    if (!$assigned_flag && defined $kingdom_i && $kingdom_i <= $#f) {
        $assigned_flag = 1 if is_assigned_kingdom($f[$kingdom_i]);
    }
    if (!$assigned_flag && defined $lineage_i && $lineage_i <= $#f) {
        $assigned_flag = 1 if is_assigned_lineage($f[$lineage_i]);
    }
    $assigned{$uuid} = 1 if $assigned_flag;
};

$consume_line->($first) unless $has_header;
while (my $line = <$IN>) {
    chomp $line;
    $consume_line->($line);
}
close $IN;

if ($input_rows > 0 && scalar(keys %assigned) == 0) {
    warn "WARN: no assigned read IDs detected from $in_path (rows=$input_rows)\n";
}

for my $uuid (sort keys %assigned) {
    print {$OUT} "$uuid\n";
}
close $OUT;

exit 0;
