#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use FindBin;
require "$FindBin::Bin/lib/consensus_id_util.pl";
require "$FindBin::Bin/lib/tabular_schema_util.pl";

my %opt;
GetOptions(
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

sub is_assigned_taxon {
    my ($v) = @_;
    $v = TabularSchemaUtil::trim_text($v);
    return 0 if $v eq '' || uc($v) eq 'NA' || lc($v) eq 'unassigned';
    return 1;
}

my %assigned_consensus;
if (-s $opt{blast_report}) {
    open my $BR, '<', $opt{blast_report} or die "open $opt{blast_report}: $!";
    my ($header_ref) = TabularSchemaUtil::read_required_header(
        $BR,
        $opt{blast_report},
        'long_seq_id',
    );
    my @header = @{$header_ref};
    while (my $line = <$BR>) {
        chomp $line;
        $line =~ s/\r$//;
        my @f = split /\t/, $line, -1;
        my %row;
        @row{@header} = @f;
        my $id = ConsensusIdUtil::consensus_id_from_long_seq_id($row{long_seq_id});
        next if !defined $id || $id eq '';
        # Due to masked report hierarchy: genus column is non-empty at genus or species level;
        # family column is non-empty at family, genus, or species level.
        my $check_col = $opt{min_level} eq "species" ? "species"
                      : $opt{min_level} eq "genus"   ? "genus"
                      :                                "family";
        my $assigned = is_assigned_taxon($row{$check_col}) ? 1 : 0;
        if ($assigned) {
            $assigned_consensus{$id} = 1;
        }
    }
    close $BR;
}

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
