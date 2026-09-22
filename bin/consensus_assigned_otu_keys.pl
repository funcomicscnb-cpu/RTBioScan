#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use FindBin;
require "$FindBin::Bin/lib/consensus_id_util.pl";
require "$FindBin::Bin/lib/tabular_schema_util.pl";

require "$FindBin::Bin/consensus_taxonomy_by_hash.pl";

my %opt;
GetOptions(
    'taxonomy-sidecar=s' => \$opt{taxonomy_sidecar},
    'blast-report=s' => \$opt{blast_report},
    'provenance=s'   => \$opt{provenance},
    'out=s'          => \$opt{out},
    'min-level=s'    => \$opt{min_level},
) or die "invalid arguments\n";

for my $req (qw(blast_report provenance out)) {
    die "missing required --$req\n" unless defined $opt{$req} && $opt{$req} ne '';
}
$opt{min_level} //= "genus";
$opt{min_level} = lc($opt{min_level});
$opt{min_level} = "genus" unless $opt{min_level} eq "family" || $opt{min_level} eq "species";

my %assigned_consensus=%{RTBioScan::ConsensusTaxonomy::assignments(
    $opt{taxonomy_sidecar},$opt{blast_report},$opt{min_level})};

my %assigned_keys;
open my $PR, '<', $opt{provenance} or die "open $opt{provenance}: $!";
my (undef, $idx_ref) = TabularSchemaUtil::read_required_header(
    $PR,
    $opt{provenance},
    'consensus_id',
    'otu_key',
);
my $cons_i = $idx_ref->{consensus_id};
my $otu_i = $idx_ref->{otu_key};
while (my $line = <$PR>) {
    last if !%assigned_consensus;
    chomp $line;
    $line =~ s/\r$//;
    next if $line =~ /^\s*$/;
    my @f = split /\t/, $line, -1;
    next if $cons_i > $#f || $otu_i > $#f;
    my $consensus_id = TabularSchemaUtil::trim_text($f[$cons_i]);
    my $otu_key = TabularSchemaUtil::trim_text($f[$otu_i]);
    next if $consensus_id eq '' || $otu_key eq '' || uc($otu_key) eq 'NA';
    next if !exists $assigned_consensus{$consensus_id};
    if ($opt{taxonomy_sidecar}) {
        my $r=$assigned_consensus{$consensus_id};
        die "consensus provenance/display mismatch\n" unless $r->{display_otu_key} eq $otu_key;
        next if $r->{stable_otu_key} eq 'NA';
    }
    $assigned_keys{$otu_key} = 1;
}
close $PR;

my $tmp = "$opt{out}.tmp";
open my $OUT, '>', $tmp or die "open $tmp: $!";
for my $otu_key (sort keys %assigned_keys) {
    print {$OUT} "$otu_key\n";
}
close $OUT;
rename $tmp, $opt{out} or die "rename $tmp -> $opt{out}: $!";

exit 0;
