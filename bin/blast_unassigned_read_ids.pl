#!/usr/bin/env perl
# blast_unassigned_read_ids.pl — emit read IDs whose OTU is NOT assigned.
# Exact inverse of blast_assigned_read_ids.pl; uses the same TaxonUtil predicates.
#
# Usage: blast_unassigned_read_ids.pl <blast_annotated_tsv> <out_unassigned_ids.list>
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use FindBin;
require "$FindBin::Bin/lib/taxon_util.pl";
*trim_text           = \&TaxonUtil::trim_text;
*is_numeric_taxid    = \&TaxonUtil::is_numeric_taxid;
*is_assigned_kingdom = \&TaxonUtil::is_assigned_kingdom;
*is_assigned_lineage = \&TaxonUtil::is_assigned_lineage;
*is_unassigned_taxon = \&TaxonUtil::is_unassigned_taxon;

my $min_level = "genus";
GetOptions("min-level=s" => \$min_level) or die "invalid options\n";
$min_level = lc($min_level);
$min_level = "genus" unless $min_level eq "family" || $min_level eq "species";

my ($in_path, $out_path) = @ARGV;
if (!defined $in_path || !defined $out_path) {
    die "usage: blast_unassigned_read_ids.pl <blast_annotated_tsv> <out_unassigned_ids.list> [--min-level family|genus|species]\n";
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

my @first_cols = split /\t/, $first, -1;
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
my $kingdom_i = $has_header
    ? (exists($idx{'otu_kingdom'}) ? $idx{'otu_kingdom'} : (exists($idx{'kingdom'}) ? $idx{'kingdom'} : undef))
    : undef;
my $lineage_i = $has_header
    ? (exists($idx{'lineage'}) ? $idx{'lineage'} : (exists($idx{'otu_lineage'}) ? $idx{'otu_lineage'} : undef))
    : 2;
my $family_i = $has_header
    ? (exists($idx{'otu_family'})  ? $idx{'otu_family'}  : (exists($idx{'family'})  ? $idx{'family'}  : undef))
    : undef;
my $genus_i  = $has_header
    ? (exists($idx{'otu_genus'})   ? $idx{'otu_genus'}   : (exists($idx{'genus'})   ? $idx{'genus'}   : undef))
    : undef;
my $species_i = $has_header
    ? (exists($idx{'otu_species'}) ? $idx{'otu_species'} : (exists($idx{'species'}) ? $idx{'species'} : undef))
    : undef;
# Determine which column indices to check based on min_level
my @level_col_idxs = $min_level eq "species" ? ($species_i)
                   : $min_level eq "genus"   ? ($genus_i, $species_i)
                   :                           ($family_i, $genus_i, $species_i);

# Track per read_id: assigned wins over unassigned (first-assigned-wins semantics)
my %assigned;
my %seen_read;

my $consume_line = sub {
    my ($line) = @_;
    return if !defined $line;
    return if $line =~ /^\s*$/;
    my @f = split /\t/, $line, -1;
    return if $read_i > $#f;
    my $rid_raw = trim_text($f[$read_i]);
    return if $rid_raw eq '';
    my ($uuid) = split /\|/, $rid_raw;
    return if !defined $uuid || $uuid eq '';

    my $is_assigned = 0;
    if (defined $taxid_i && $taxid_i <= $#f) {
        $is_assigned = 1 if is_numeric_taxid($f[$taxid_i]);
    }
    if (!$is_assigned && defined $kingdom_i && $kingdom_i <= $#f) {
        $is_assigned = 1 if is_assigned_kingdom($f[$kingdom_i]);
    }
    if (!$is_assigned && defined $lineage_i && $lineage_i <= $#f) {
        $is_assigned = 1 if is_assigned_lineage($f[$lineage_i]);
    }
    if (!$is_assigned) {
        for my $ci (@level_col_idxs) {
            next unless defined $ci && $ci <= $#f;
            my $val = trim_text($f[$ci]);
            next if $val eq '' || uc($val) eq 'NA';
            if (!is_unassigned_taxon($val)) {
                $is_assigned = 1;
                last;
            }
        }
    }

    # Once a read is assigned, it stays assigned regardless of later rows
    if ($is_assigned) {
        $assigned{$uuid} = 1;
    } elsif (!exists $seen_read{$uuid}) {
        $seen_read{$uuid} = 1;
    }
};

$consume_line->($first) unless $has_header;
while (my $line = <$IN>) {
    chomp $line;
    $consume_line->($line);
}
close $IN;

# Emit reads that appeared but were never assigned
for my $uuid (sort keys %seen_read) {
    next if $assigned{$uuid};
    print {$OUT} "$uuid\n";
}
close $OUT;

exit 0;
