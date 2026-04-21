#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use File::Glob qw(bsd_glob GLOB_NOSORT);
use FindBin;
require "$FindBin::Bin/lib/consensus_id_util.pl";
require "$FindBin::Bin/lib/tabular_schema_util.pl";

# Identify read IDs that contributed to BLAST-assigned consensus sequences.
# A consensus is "assigned" when its family column in the blast report is non-empty
# and not "Unassigned" (i.e., assigned at family, genus, or species level).
# Read IDs are extracted from per-OTU _reads.list files in the Consensus directory,
# using the same structure as emit_consensus_round_provenance.pl.
#
# Usage: consensus_recovered_reads.pl \
#     --blast-report <consensus_blast_report_full.txt> \
#     --consensus-dir <Consensus/> \
#     --out <round_consensus_assigned_reads.list>

my %opt;
GetOptions(
    'blast-report=s'  => \$opt{blast_report},
    'consensus-dir=s' => \$opt{consensus_dir},
    'out=s'           => \$opt{out},
    'min-level=s'     => \$opt{min_level},
) or die "invalid arguments\n";

for my $req (qw(blast_report consensus_dir out)) {
    die "missing required --$req\n" unless defined $opt{$req} && $opt{$req} ne '';
}
$opt{min_level} //= "genus";
$opt{min_level} = lc($opt{min_level});
$opt{min_level} = "genus" unless $opt{min_level} eq "family" || $opt{min_level} eq "species";

# Level-appropriate column: genus non-empty => genus or better; family => family or better
my $check_col = $opt{min_level} eq "species" ? "species"
              : $opt{min_level} eq "genus"   ? "genus"
              :                                "family";

# --- Parse blast report: collect long_seq_ids meeting the level requirement ---
my %assigned;   # consensus_id => 1
if (-s $opt{blast_report}) {
    open my $fh, '<', $opt{blast_report}
        or die "open $opt{blast_report}: $!";
    my ($cols_ref) = TabularSchemaUtil::read_required_header(
        $fh,
        $opt{blast_report},
        'long_seq_id',
    );
    my @cols = @{$cols_ref};
    while (my $line = <$fh>) {
        chomp $line;
        $line =~ s/\r$//;
        my @f = split /\t/, $line, -1;
        # Header: long_seq_id consensus_taxid kingdom phylum class order family genus species
        my %row;
        @row{@cols} = @f;
        my $id      = ConsensusIdUtil::consensus_id_from_long_seq_id($row{long_seq_id});
        my $col_val = $row{$check_col} // '';
        $col_val =~ s/^\s+|\s+$//g;
        next if !defined $id || $id eq '';
        next if $col_val eq '' || lc($col_val) eq 'unassigned';
        $assigned{$id} = 1;
    }
    close $fh;
}

# Early exit: no assigned consensus → empty output
unless (%assigned) {
    open my $out, '>', $opt{out} or die "cannot write '$opt{out}': $!";
    close $out;
    exit 0;
}

# --- Walk Consensus directory: for each assigned consensus, collect read IDs ---
my %recovered;   # base_read_id => 1

if (-d $opt{consensus_dir}) {
    my @fasta_files = sort { $a cmp $b }
        bsd_glob("$opt{consensus_dir}/*/*_Merged_Consensus.fasta", GLOB_NOSORT);

    for my $fasta (@fasta_files) {
        next unless -s $fasta;
        my ($sample_dir) = $fasta =~ m{^(.*?)/[^/]+_Merged_Consensus\.fasta$};
        next unless defined $sample_dir && $sample_dir ne '';

        open my $fh, '<', $fasta or die "open $fasta: $!";
        while (my $line = <$fh>) {
            next unless $line =~ /^>/;
            chomp $line;
            $line =~ s/^>//;
            $line =~ s/\r$//;

            my $parsed = ConsensusIdUtil::parse_consensus_long_seq_id($line);
            next unless defined $parsed;
            next unless $assigned{$parsed->{consensus_id}};

            my $cons_token = $parsed->{cons_token};
            next if $cons_token eq '';

            my $reads_list = "$sample_dir/OriginalReads/${cons_token}_reads.list";
            next unless -s $reads_list;

            open my $rl, '<', $reads_list or die "open $reads_list: $!";
            while (my $rid = <$rl>) {
                chomp $rid;
                $rid =~ s/\r$//;
                $rid =~ s/^\s+|\s+$//g;
                next if $rid eq '';
                my ($base_id) = split /\|/, $rid, 2;
                $base_id =~ s/^\s+|\s+$//g;
                next if $base_id eq '';
                $recovered{$base_id} = 1;
            }
            close $rl;
        }
        close $fh;
    }
}

# --- Write sorted deduplicated output ---
my $tmp = $opt{out} . '.tmp';
open my $out, '>', $tmp or die "cannot write '$tmp': $!";
for my $id (sort keys %recovered) {
    print {$out} "$id\n";
}
close $out;
rename $tmp, $opt{out} or die "rename $tmp -> $opt{out}: $!";

exit 0;
