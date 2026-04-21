#!/usr/bin/env perl
use strict;
use warnings;

my ($clstr, $hash_map, $in_fasta, $min_members, $out_fasta, $out_stats, $out_kept_otus, $missing_policy) = @ARGV;
die "usage: $0 in.clstr hash_map.tsv in.fasta min_members out.fasta out.stats.tsv out_kept_otus.tsv [keep|drop]\n"
  unless @ARGV == 7 || @ARGV == 8;

if ($min_members !~ /^[0-9]+$/) {
    die "invalid min_members '$min_members' (expected integer >= 0)\n";
}
$missing_policy = defined($missing_policy) ? lc($missing_policy) : 'keep';
die "invalid missing_policy '$missing_policy' (expected keep|drop)\n"
  unless $missing_policy eq 'keep' || $missing_policy eq 'drop';

open my $HM, '<', $hash_map or die "open $hash_map: $!";
my %rid_to_hash;
my %hash_count;
while (my $line = <$HM>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($rid, $hash) = split /\t/, $line;
    next unless defined $rid && defined $hash && $rid ne '' && $hash ne '';
    $rid_to_hash{$rid} = $hash;
    $hash_count{$hash}++;
}
close $HM;

open my $CL, '<', $clstr or die "open $clstr: $!";
my %cluster_hashes;
my %hash_cluster;
my %ambiguous_hash;
my $cluster = '';
my $saw_cluster_header = 0;
my $clstr_records = 0;
my $ambiguous_hash_cluster = 0;

while (my $line = <$CL>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    if ($line =~ /^>Cluster\s+(\d+)/) {
        $cluster = "CLUST_$1";
        $saw_cluster_header = 1;
        next;
    }
    if ($line =~ /^\s*\d+\s+\S+,\s+>(.+?)\.\.\..*$/) {
        die "malformed .clstr: member line before cluster header\n" if !$saw_cluster_header;
        my $rid = $1;
        $rid =~ s/\s+$//;
        next if $rid eq '';
        $clstr_records++;
        my $hash = $rid_to_hash{$rid};
        next unless defined $hash && $hash ne '';
        $cluster_hashes{$cluster}{$hash} = 1;
        if (!exists $hash_cluster{$hash}) {
            $hash_cluster{$hash} = $cluster;
        } elsif ($hash_cluster{$hash} ne $cluster) {
            $ambiguous_hash_cluster++;
            delete $hash_cluster{$hash};
            $ambiguous_hash{$hash} = 1;
        }
        next;
    }
    die "malformed .clstr line: $line\n";
}
close $CL;
my $clstr_has_clusters = $saw_cluster_header ? 1 : 0;

my %cluster_size;
for my $cid (keys %cluster_hashes) {
    my $sum = 0;
    for my $hash (keys %{ $cluster_hashes{$cid} }) {
        next if $ambiguous_hash{$hash};
        $sum += ($hash_count{$hash} // 0);
    }
    $cluster_size{$cid} = $sum;
}

my %keep_cluster;
for my $cid (keys %cluster_size) {
    if ($min_members == 0 || $cluster_size{$cid} >= $min_members) {
        $keep_cluster{$cid} = 1;
    }
}

my $total_otus = scalar keys %cluster_size;
my $kept_otus = scalar keys %keep_cluster;
my $dropped_otus = $total_otus - $kept_otus;

open my $KO, '>', $out_kept_otus or die "open $out_kept_otus: $!";
for my $cid (sort keys %keep_cluster) {
    print {$KO} "$cid\n";
}
close $KO;

open my $IN, '<', $in_fasta or die "open $in_fasta: $!";
open my $OUT, '>', $out_fasta or die "open $out_fasta: $!";

my $total_reads = 0;
my $kept_reads = 0;
my $reads_missing_from_clstr = 0;

my @record = ();
sub flush_record {
    my ($rec_ref, $out_fh, $min, $missing_policy, $total_reads_ref, $kept_reads_ref, $missing_ref, $rid_to_hash_ref, $hash_cluster_ref, $keep_cluster_ref) = @_;
    return if !@$rec_ref;

    my $h = $rec_ref->[0];
    return if $h !~ /^>/;
    $$total_reads_ref++;

    if ($min == 0) {
        print {$out_fh} @$rec_ref;
        $$kept_reads_ref++;
        return;
    }

    my $rid = substr($h, 1);
    chomp $rid;
    $rid =~ s/\s.*$//;
    my $hash = $rid_to_hash_ref->{$rid};
    if (!defined $hash || $hash eq '') {
        $$missing_ref++;
        if ($missing_policy eq 'keep') {
            print {$out_fh} @$rec_ref;
            $$kept_reads_ref++;
        }
        return;
    }
    my $cid = $hash_cluster_ref->{$hash};
    if (!defined $cid || $cid eq '') {
        $$missing_ref++;
        if ($missing_policy eq 'keep') {
            print {$out_fh} @$rec_ref;
            $$kept_reads_ref++;
        }
        return;
    }
    if (exists $keep_cluster_ref->{$cid}) {
        print {$out_fh} @$rec_ref;
        $$kept_reads_ref++;
    }
}

while (my $line = <$IN>) {
    if ($line =~ /^>/) {
        flush_record(
            \@record, $OUT, $min_members, $missing_policy,
            \$total_reads, \$kept_reads, \$reads_missing_from_clstr,
            \%rid_to_hash, \%hash_cluster, \%keep_cluster
        );
        @record = ($line);
    } else {
        push @record, $line;
    }
}
flush_record(
    \@record, $OUT, $min_members, $missing_policy,
    \$total_reads, \$kept_reads, \$reads_missing_from_clstr,
    \%rid_to_hash, \%hash_cluster, \%keep_cluster
);

close $IN;
close $OUT;

my $dropped_reads = $total_reads - $kept_reads;

open my $ST, '>', $out_stats or die "open $out_stats: $!";
print {$ST} "min_members\t$min_members\n";
print {$ST} "missing_policy\t$missing_policy\n";
print {$ST} "clstr_has_clusters\t$clstr_has_clusters\n";
print {$ST} "clstr_records\t$clstr_records\n";
print {$ST} "ambiguous_hash_cluster\t$ambiguous_hash_cluster\n";
print {$ST} "total_otus\t$total_otus\n";
print {$ST} "kept_otus\t$kept_otus\n";
print {$ST} "dropped_otus\t$dropped_otus\n";
print {$ST} "total_reads\t$total_reads\n";
print {$ST} "kept_reads\t$kept_reads\n";
print {$ST} "dropped_reads\t$dropped_reads\n";
print {$ST} "reads_missing_from_clstr\t$reads_missing_from_clstr\n";
close $ST;

exit 0;
