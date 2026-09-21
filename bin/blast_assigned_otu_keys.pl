#!/usr/bin/env perl
use strict;
use warnings;
use FindBin;
use Getopt::Long qw(GetOptions);
require "$FindBin::Bin/lib/taxon_util.pl";
my $min_level='genus';my $persistent=0;
GetOptions('min-level=s'=>\$min_level,'persistent'=>\$persistent) or die "invalid options\n";
$min_level=lc($min_level);
my ($blast_path,$out_path,$hash_map_path)=@ARGV;
die "usage: blast_assigned_otu_keys.pl INPUT OUTPUT [HASH_MAP] [--min-level family|genus|species] [--persistent]\n" unless @ARGV==2 || @ARGV==3;
my %hash_by_base;
if (defined($hash_map_path) && -s $hash_map_path) {
    open my $f,'<',$hash_map_path or die "open $hash_map_path: $!\n";
    while (my $line=<$f>) {
        chomp $line;next unless $line =~ /\S/;
        my ($id,$hash,@extra)=split /\t/,$line,-1;
        die "invalid representative hash row\n" unless defined($hash) && $hash =~ /\A[0-9a-f]{32}\z/ && !@extra;
        my ($base)=split /\|/,$id;
        die "conflicting representative hash\n" if exists($hash_by_base{$base}) && $hash_by_base{$base} ne $hash;
        $hash_by_base{$base}=$hash;
    }
    close $f or die "close hash map: $!\n";
}
my (%assigned,%stable);
for my $row (@{TaxonUtil::assignment_rows($blast_path)}) {
    die "persistent keys require validated R4-B evidence\n" if $persistent && !$row->{_r4b_validated};
    my $otu=$row->{otu_id}//'';
    $otu=$1 if !$otu && $row->{read_id}=~/\|(OTUB_[^|]+)/;
    next unless $otu =~ /\AOTUB_[0-9]+-([A-Za-z0-9_.-]+)\z/;
    my $marker=TaxonUtil::canonical_marker_token($1);
    my ($base)=split /\|/,$row->{read_id};
    my $key=$row->{stable_key}//'NA';
    $key=$marker.'|'.$hash_by_base{$base} if !$persistent && $key eq 'NA' && exists $hash_by_base{$base};
    if ($key ne 'NA') {
        die "invalid stable representative key\n" unless $key =~ /\A\Q$marker\E\|[0-9a-f]{32}\z/;
        die "conflicting representative key for $otu\n" if exists($stable{$otu}) && $stable{$otu} ne $key;
        $stable{$otu}=$key;
    }
    $assigned{$otu}=1 if TaxonUtil::assignment_from_row($row,$min_level);
}
my %keys;
for my $otu (sort keys %assigned) {
    if (exists $stable{$otu}) { $keys{$stable{$otu}}=1; }
    elsif ($persistent) { warn "WARN: no stable representative key for $otu; omitted from persistent protection\n"; }
    else { warn "WARN: $otu is a display-only key; never use it for persistent protection\n";$keys{$otu}=1; }
}
TaxonUtil::atomic_text($out_path,join('',map { "$_\n" } sort keys %keys));
