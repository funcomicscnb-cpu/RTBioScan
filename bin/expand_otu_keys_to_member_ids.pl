#!/usr/bin/env perl
use strict;
use warnings;
use FindBin;
require "$FindBin::Bin/lib/taxon_util.pl";
*trim_text = \&TaxonUtil::trim_text;

my ($otu_keys_path, $otu_members_path, $out_members_path, $stats_path, $hash_map_path) = @ARGV;
if (!defined $otu_keys_path || !defined $otu_members_path || !defined $out_members_path) {
    die "usage: expand_otu_keys_to_member_ids.pl <otu_keys.list> <otu_members_round.tsv> <out_member_ids.list> [stats_out] [hash_map.tsv]\n";
}

# Load the set of OTU keys to expand (may contain stable keys, OTUB_N keys, or a mix)
my %keys;
if (-e $otu_keys_path && -s $otu_keys_path) {
    open my $OK, '<', $otu_keys_path or die "open $otu_keys_path: $!";
    while (my $line = <$OK>) {
        chomp $line;
        my $otu_key = trim_text($line);
        next if $otu_key eq '';
        $keys{$otu_key} = 1;
    }
    close $OK;
}

# Load hash map: uuid_base -> md5hash  (built from the nr/rep FASTA)
# Enables stable key resolution: OTU keys shift after re-clustering, but the
# representative sequence hash is invariant as long as the cluster seed is stable.
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

open my $OUT, '>', $out_members_path or die "open $out_members_path: $!";
if (!%keys || !-e $otu_members_path || !-s $otu_members_path) {
    close $OUT;
    if (defined $stats_path && $stats_path ne '') {
        open my $ST, '>', $stats_path or die "open $stats_path: $!";
        print {$ST} "protected_otu_keys_round_count\t" . (scalar keys %keys) . "\n";
        print {$ST} "protected_otu_member_ids_round_count\t0\n";
        close $ST;
    }
    exit 0;
}

open my $OM, '<', $otu_members_path or die "open $otu_members_path: $!";
my $hdr = <$OM>;
defined $hdr or die "empty $otu_members_path\n";
chomp $hdr;
my @cols = split /\t/, $hdr, -1;
my (%idx, $otu_i, $read_i);
for my $i (0 .. $#cols) {
    my $k = trim_text($cols[$i]);
    $idx{lc($k)} = $i if $k ne '';
}
$otu_i  = $idx{'otu_id'} // $idx{'otu_key'} // 0;
$read_i = $idx{'read_id'} // 1;

# Buffer all members by OTU key; resolve stable key per OTU via hash map
my %otu_uuids;      # otu_key -> [uuid, ...]
my %stable_by_otu;  # otu_key -> stable_key (resolved when representative UUID found in hash map)
while (my $line = <$OM>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my @f = split /\t/, $line, -1;
    next if $otu_i > $#f || $read_i > $#f;
    my $otu_key = trim_text($f[$otu_i]);
    next if $otu_key eq '';
    my $read_id = trim_text($f[$read_i]);
    next if $read_id eq '';
    my ($uuid) = split /\|/, $read_id, 2;
    next if !defined $uuid || $uuid eq '';
    push @{$otu_uuids{$otu_key}}, $uuid;
    # Resolve stable key once per OTU (when the representative UUID is in the hash map)
    if ($hash_map_loaded && !exists $stable_by_otu{$otu_key}) {
        my $hash = $hash_by_base{$uuid} // '';
        if ($hash ne '') {
            my $sk = stable_key($otu_key, $hash);
            $stable_by_otu{$otu_key} = $sk if $sk ne '';
        }
    }
}
close $OM;

my %member_ids;
for my $otu_key (keys %otu_uuids) {
    # Check direct match (OTUB_N key in ever-list, or exact key present)
    my $include = exists $keys{$otu_key};
    # Check stable key match (handles OTU renumbering after re-clustering)
    if (!$include && $hash_map_loaded) {
        my $sk = $stable_by_otu{$otu_key} // '';
        $include = ($sk ne '' && exists $keys{$sk});
    }
    if ($include) {
        $member_ids{$_} = 1 for @{$otu_uuids{$otu_key}};
    }
}

for my $read_id (sort keys %member_ids) {
    print {$OUT} "$read_id\n";
}
close $OUT;

if (defined $stats_path && $stats_path ne '') {
    open my $ST, '>', $stats_path or die "open $stats_path: $!";
    print {$ST} "protected_otu_keys_round_count\t" . (scalar keys %keys) . "\n";
    print {$ST} "protected_otu_member_ids_round_count\t" . (scalar keys %member_ids) . "\n";
    close $ST;
}

exit 0;
