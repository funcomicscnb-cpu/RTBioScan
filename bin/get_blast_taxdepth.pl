#!/usr/bin/env perl
use strict;
use warnings;
use FindBin;
require "$FindBin::Bin/cache_blast_by_hash.pl" unless defined &cache_read;

# MEMTAX v2: input taxid, species/genus/family/order identities, canonical JSON
# array for multiple source rank labels (a singleton retains its plain label). Only explicitly configured, marker-scoped seeds
# authorize synthetic depths; unversioned state is obsolete derived data.
sub resolve_memory {
    my ($needed,$previous,$sig,$seed,$target)=@_;
    my %memory=%{memtax_read($previous,$sig)};
    my $configured=seed_read($seed,$target);
    # Never promote an old derived negative row to configured seed authority.
    delete $memory{$_} for grep { /^-/ } keys %memory;
    for (keys %$needed) {
        $memory{$_}=$configured->{$_} // [('NA')x4,rank_labels('NA',1)] if $_<=0;
    }
    my @todo=sort { $a<=>$b } grep { $_>0 && !exists $memory{$_} } keys %$needed;
if (@todo) {
    fail('TAXONKIT_DB is required') unless defined($ENV{TAXONKIT_DB}) && -d $ENV{TAXONKIT_DB};
    atomic_write('.tmp_taxids',join('',map { "$_\n" } @todo));
    sub capture_taxonkit {
        my ($out,@args)=@_;
        open my $p,'-|','taxonkit',@args or fail('start taxonkit');
        local $/; my $text=<$p> // '';
        close $p or fail('taxonkit failed');
        atomic_write($out,$text);
    }
    capture_taxonkit('.tmp_taxids_lineages','lineage','-r','-t','.tmp_taxids');
    capture_taxonkit('.tmp_taxids_lineages_reformat','reformat','-f','{s};{g};{f};{o}','-P','-t','-i','2','.tmp_taxids_lineages');
    my %todo=map { $_=>1 } @todo; my %seen;
    for my $line (@{read_lines('.tmp_taxids_lineages_reformat')}) {
        my @v=split /\t/,$line,-1;
        fail('malformed taxonkit response') unless @v==6 && $todo{$v[0]} && !$seen{$v[0]}++;
        my @r=split /;/,$v[5],-1;
        # Unknown/deleted IDs have an empty formatted lineage, never taxid zero.
        @r=('','','','') if $v[5] eq '';
        fail('wrong taxonomy rank count') unless @r==4;
        for (@r) { $_='NA' if $_ eq ''; fail('invalid taxonomy rank identity') unless $_ eq 'NA' || /\A[1-9][0-9]*\z/; }
        my $rank=$v[3] eq '' ? 'NA' : $v[3];
        fail('invalid resolved rank name') unless $rank =~ /\A[a-z][a-z _-]*\z/ || $rank eq 'NA';
        $memory{$v[0]}=[@r,rank_labels($rank,1)];
    }
    fail('incomplete taxonomy batch') unless keys(%seen)==@todo;
}
    return \%memory;
}
sub attribution {
    my ($r,$status,$memory,$species,$genus,$pid)=@_;
    my $tax='NA';
    if ($status eq 'UNIQUE') {
        my $rank=$memory->{$r->[2]} // [('NA')x5];
        my $start=decimal_cmp($pid,$genus)<0 ? 2 : decimal_cmp($pid,$species)<0 ? 1 : 0;
        for my $i ($start..3) { if ($rank->[$i] ne 'NA') { $tax=$rank->[$i]; last; } }
    }
    return join(';',$tax,display_number($r->[3]),$r->[4],display_identity($r->[5]));
}
sub depth_main {
    my ($input1,$thr_spc,$thr_gns,$previous,$sig,$pending,$seed,$target)=@ARGV;
    fail('Usage: get_blast_taxdepth.pl EVIDENCE SPEC GENUS MEMTAX SIGNATURE PENDING [SEED TARGET]') unless @ARGV==6 || @ARGV==8;
    number($thr_spc,0,100); number($thr_gns,0,100);
    my ($evidence_sig,$groups)=evidence_read($input1);
    fail('evidence/signature mismatch') unless $sig eq $evidence_sig;
    my %needed;
    for my $id (keys %$groups) {
        fail('query/seed marker mismatch') if defined($target) && (split /\|/,$id)[1] ne $target;
        for (@{$groups->{$id}}) { $needed{$_->[0][2]}=1 unless $_->[1] eq 'NO_HIT'; }
    }
    my $memory=resolve_memory(\%needed,$previous,$sig,$seed,$target);
    my ($species,$genus)=map { decimal_key($_) } ($thr_spc,$thr_gns);
    my @out;
    for my $id (sort keys %$groups) {
        my ($r,$status,$ranked)=@{$groups->{$id}[0]}; next if $status eq 'NO_HIT';
        push @out,$id.';'.attribution($r,$status,$memory,$species,$genus,$ranked->[3]);
    }
    my @rows=map { join("\t",$_,@{$memory->{$_}}) } sort { $a<=>$b } keys %$memory;
    atomic_write($pending,sealed('MEMTAX',$sig,\@rows));
    print "$_\n" for @out;
}
depth_main() unless caller;
1;
