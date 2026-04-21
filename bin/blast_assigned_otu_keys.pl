#!/usr/bin/env perl
use strict;
use warnings;
use FindBin;
require "$FindBin::Bin/lib/taxon_util.pl";
*trim_text           = \&TaxonUtil::trim_text;
*is_unassigned_taxon = \&TaxonUtil::is_unassigned_taxon;
*is_numeric_taxid    = \&TaxonUtil::is_numeric_taxid;
*is_assigned_lineage = \&TaxonUtil::is_assigned_lineage;

use Getopt::Long qw(GetOptions);
my $min_level = "genus";
GetOptions("min-level=s" => \$min_level) or die "invalid options\n";
$min_level = lc($min_level);
$min_level = "genus" unless $min_level eq "family" || $min_level eq "species";

my ($blast_path, $out_path, $hash_map_path) = @ARGV;
if (!defined $blast_path || !defined $out_path) {
    die "usage: blast_assigned_otu_keys.pl <blast_otu_or_annotated> <out_otu_keys.list> [hash_map.tsv] [--min-level family|genus|species]\n";
}

sub otu_marker {
    my ($otu) = @_;
    if ($otu =~ /^OTUB_[^-]+-(.+)$/) {
        my $m = $1;
        return uc($m) if defined($m) && $m ne '';
    }
    return 'NA';
}

sub stable_key {
    my ($otu, $hash) = @_;
    return '' unless defined $otu && defined $hash && $hash ne '';
    my $marker = otu_marker($otu);
    return join('|', $marker, lc($hash));
}

sub normalize_header_key {
    my ($v) = @_;
    $v = trim_text($v);
    $v =~ s/^#+//;
    return lc($v);
}

sub assigned_from_fields {
    my ($f_ref, $idx_ref, $level) = @_;
    my @f = @{$f_ref};
    my $taxid_i = $idx_ref->{taxid};
    if (defined $taxid_i && $taxid_i <= $#f) {
        return 1 if is_numeric_taxid($f[$taxid_i]);
    }
    my $lineage_i = $idx_ref->{lineage};
    if (defined $lineage_i && $lineage_i <= $#f) {
        return 1 if is_assigned_lineage($f[$lineage_i]);
    }
    # Determine which taxonomy columns must be non-empty to satisfy the level requirement:
    # family (default): family OR genus OR species non-empty
    # genus:            genus OR species non-empty
    # species:          species non-empty
    my @check_cols = $level eq "species" ? qw(species)
                   : $level eq "genus"   ? qw(genus species)
                   :                       qw(family genus species);
    for my $k (@check_cols) {
        my $i = $idx_ref->{$k};
        next unless defined $i && $i <= $#f;
        my $val = trim_text($f[$i]);
        next if $val eq '' || uc($val) eq 'NA';
        return 1 if !is_unassigned_taxon($val);
    }
    return 0;
}

sub otu_from_row {
    my ($fields_ref, $otu_i, $read_i) = @_;
    my @f = @{$fields_ref};
    my $otu = '';
    if (defined $otu_i && $otu_i <= $#f) {
        $otu = trim_text($f[$otu_i]);
        $otu = '' if $otu eq '' || uc($otu) eq 'NA';
    }
    if ($otu eq '' && defined $read_i && $read_i <= $#f) {
        my $rid = $f[$read_i] // '';
        my @parts = split /\|/, $rid;
        for my $tok (@parts) {
            if ($tok =~ /^OTUB_/) {
                $otu = $tok;
                last;
            }
        }
    }
    return $otu;
}

# Load hash map: uuid_base -> md5hash  (built from the nr/rep FASTA)
# Only the representative read for each OTU appears in the hash map.
my %hash_by_base;
my $hash_map_loaded = 0;
if (defined $hash_map_path && $hash_map_path ne '' && -s $hash_map_path) {
    open my $HM, '<', $hash_map_path or die "open $hash_map_path: $!";
    while (my $line = <$HM>) {
        chomp $line;
        next if $line =~ /^\s*$/;
        my ($rid_full, $hash) = split /\t/, $line;
        next unless defined $rid_full && defined $hash && $rid_full ne '' && $hash ne '';
        my ($base) = split /\|/, $rid_full;
        next unless defined $base && $base ne '';
        $hash_by_base{$base} = $hash;
    }
    close $HM;
    $hash_map_loaded = 1;
}

open my $OUT, '>', $out_path or die "open $out_path: $!";

if (!-e $blast_path || !-s $blast_path) {
    close $OUT;
    exit 0;
}

open my $IN, '<', $blast_path or do {
    close $OUT;
    die "open $blast_path: $!";
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
    $idx{$k} = $i if $k ne '';
}
my $header0 = normalize_header_key($first_cols[0] // '');
my $has_header = ($header0 eq 'read_id' || $header0 eq 'seq_id' || $header0 eq 'long_read_id') ? 1 : 0;

my $read_i = $has_header
    ? (
        exists($idx{'read_id'}) ? $idx{'read_id'}
        : (exists($idx{'seq_id'}) ? $idx{'seq_id'}
        : (exists($idx{'long_read_id'}) ? $idx{'long_read_id'} : 0))
    )
    : 0;
my $otu_i = $has_header
    ? (
        exists($idx{'otu_id'}) ? $idx{'otu_id'}
        : (exists($idx{'otu'}) ? $idx{'otu'} : undef)
    )
    : undef;
my %assign_idx = (
    taxid => ($has_header ? (exists($idx{'otu_taxid'}) ? $idx{'otu_taxid'} : (exists($idx{'taxid'}) ? $idx{'taxid'} : (exists($idx{'tax_id'}) ? $idx{'tax_id'} : undef))) : 1),
    family => ($has_header ? (exists($idx{'otu_family'}) ? $idx{'otu_family'} : (exists($idx{'family'}) ? $idx{'family'} : undef)) : undef),
    genus => ($has_header ? (exists($idx{'otu_genus'}) ? $idx{'otu_genus'} : (exists($idx{'genus'}) ? $idx{'genus'} : undef)) : undef),
    species => ($has_header ? (exists($idx{'otu_species'}) ? $idx{'otu_species'} : (exists($idx{'species'}) ? $idx{'species'} : undef)) : undef),
    lineage => ($has_header ? (exists($idx{'lineage'}) ? $idx{'lineage'} : (exists($idx{'otu_lineage'}) ? $idx{'otu_lineage'} : undef)) : undef),
);

my %assigned_otu;
my %stable_by_otu;  # otu_key -> stable_key (resolved when representative UUID is encountered)

my $consume = sub {
    my ($line) = @_;
    return if !defined $line || $line =~ /^\s*$/;
    my @f = split /\t/, $line, -1;
    return if $read_i > $#f;
    my $otu = otu_from_row(\@f, $otu_i, $read_i);
    return if $otu eq '';

    # Try to resolve stable key for this OTU (once per OTU; stops when representative found)
    if ($hash_map_loaded && !exists $stable_by_otu{$otu}) {
        my $rid = $f[$read_i] // '';
        my ($uuid) = split /\|/, $rid;
        if (defined $uuid && $uuid ne '') {
            my $hash = $hash_by_base{$uuid} // '';
            if ($hash ne '') {
                my $sk = stable_key($otu, $hash);
                $stable_by_otu{$otu} = $sk if $sk ne '';
            }
        }
    }

    return if !assigned_from_fields(\@f, \%assign_idx, $min_level);
    $assigned_otu{$otu} = 1;
};

$consume->($first) unless $has_header;
while (my $line = <$IN>) {
    chomp $line;
    $consume->($line);
}
close $IN;

for my $otu (sort keys %assigned_otu) {
    my $key = ($hash_map_loaded && exists $stable_by_otu{$otu} && $stable_by_otu{$otu} ne '')
        ? $stable_by_otu{$otu}
        : $otu;
    print {$OUT} "$key\n";
}
close $OUT;

exit 0;
