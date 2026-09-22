#!/usr/bin/env perl
package RTBioScan::ConsensusTaxonomy;
use strict;
use warnings;
use File::Basename qw(dirname);
use File::Spec;
use File::Temp qw(tempfile tempdir);
use Digest::SHA qw(sha256_hex);
use JSON::PP ();
use Getopt::Long qw(GetOptionsFromArray);
our $BIN=File::Spec->rel2abs(dirname(__FILE__));
require($BIN.'/lib/RTBioScan/OTURefineBlastreport.pm');
require($BIN.'/lib/consensus_id_util.pl');
require($BIN.'/lib/tabular_schema_util.pl');

# R4-C v1: one result per query, sorted by the complete query identifier.
# Candidate JSON contains every selected R4-A HSP tuple (12 fields, hash first).
our @FIELDS=qw(long_seq_id marker sample stable_otu_key display_otu_key consensus_id
    sequence_hash query_length resolved_taxid kingdom phylum class order family genus
    species status depth origin candidate_count candidates_json reason signature);
our @RANKS=qw(kingdom phylum class order family genus species);
sub fail { die "R4-C: @_\n" }
sub json { JSON::PP->new->canonical->allow_nonref(0) }
sub digest { RTBioScan::R4A::file_digest($_[0]) }
sub lines { RTBioScan::R4A::each_line(@_) }
sub atomic { RTBioScan::R4A::atomic_write(@_) }
sub usable { $_[0] eq 'ASSIGNED' || $_[0] eq 'AMBIGUOUS_TIE' }
sub signed { defined($_[0]) && $_[0] =~ /\A-?[1-9][0-9]*\z/ }
sub number { RTBioScan::R4A::decimal_key($_[0]) }
sub cmpnum { RTBioScan::R4A::decimal_cmp(number($_[0]),number($_[1])) }
sub seal {
    my ($kind,$sig,$body,$count)=@_;
    return "#RTB-R4C-$kind\t1\t$sig\n$body#END\t$count\t".sha256_hex($body)."\n";
}
sub unseal {
    my ($path,$kind)=@_;open my $f,'<',$path or fail("read $path: $!");
    my $head=<$f>//'';
    fail('unknown/truncated sidecar header') unless $head =~ /\A#RTB-R4C-\Q$kind\E\t1\t([0-9a-f]{64})\n\z/;
    my $sig=$1;my @rows;my $sha=Digest::SHA->new(256);my $end=0;
    while (my $line=<$f>) {
        fail('truncated sidecar row') unless $line =~ s/\n\z//;
        fail('invalid sidecar control character') if $line =~ /[\r\x00]/;
        if ($line =~ /^#END\t/) {
            fail('sidecar footer/count/checksum mismatch') unless $line eq '#END'."\t".scalar(@rows)."\t".$sha->hexdigest;
            fail('data after footer') if defined(<$f>);$end=1;last;
        }
        $sha->add("$line\n");push @rows,$line;
    }
    close $f or fail('close sidecar');fail('missing sidecar footer') unless $end;
    return ($sig,\@rows);
}
sub validate_record {
    my ($r)=@_;fail('wrong taxonomy field count') unless @$r==@FIELDS;
    fail('empty/control taxonomy field') if grep { !defined($_) || $_ eq '' || /[\t\r\n\x00]/ } @$r;
    my %v;@v{@FIELDS}=@$r;
    RTBioScan::R4A::query_id($v{long_seq_id});RTBioScan::OTURefineBlastreport::r4b_marker($v{marker});
    my $parsed=ConsensusIdUtil::parse_consensus_long_seq_id($v{long_seq_id});
    fail('query identity mismatch') unless $parsed && $parsed->{sample} eq $v{sample} && $parsed->{consensus_id} eq $v{consensus_id};
    my @tokens=split /\|/,$v{long_seq_id};
    fail('query marker mismatch') unless grep { $_ eq $v{marker} } @tokens[2..$#tokens];
    fail('invalid stable identity') unless $v{stable_otu_key} eq 'NA' || $v{stable_otu_key} =~ /\A\Q$v{marker}\E\|[0-9a-f]{32}\z/;
    my @display=map { /^OTU=(.+)$/ ? $1 : () } @tokens;
    fail('ambiguous query display identity') if @display>1;
    fail('query display identity mismatch') unless $v{display_otu_key} eq (@display ? $display[0] : 'NA');
    RTBioScan::R4A::hash_id($v{sequence_hash});RTBioScan::R4A::uint($v{query_length});
    fail('invalid resolved taxid') unless $v{resolved_taxid} eq 'NA' || signed($v{resolved_taxid});
    fail('unknown status') unless $v{status}=~/\A(?:ASSIGNED|AMBIGUOUS_TIE|NO_HIT|REFERENCE_UNRESOLVED|REFERENCE_INCONSISTENT|COMPUTATION_FAILED)\z/;
    fail('invalid origin') unless $v{origin}=~/\A(?:DIRECT|LCA|NONE)\z/;
    fail('invalid depth') unless $v{depth}=~/\A(?:-1|[0-6])\z/;
    my @ranks=@v{@RANKS};my $canonical=TaxonUtil::canonical_lineage(join(';',@ranks));
    fail('noncanonical lineage') unless $canonical && join("\t",@$canonical) eq join("\t",@ranks);
    fail('depth/lineage mismatch') unless $v{depth}==TaxonUtil::lineage_depth($canonical);
    fail('invalid candidate count') unless $v{candidate_count}=~/\A(?:0|[1-9][0-9]*)\z/;
    fail('invalid scientific signature') unless $v{signature}=~/\A[0-9a-f]{64}\z/;
    my $c=eval { json()->decode($v{candidates_json}) };
    fail('invalid candidate encoding') unless ref($c) eq 'ARRAY' && json()->encode($c) eq $v{candidates_json} && @$c==$v{candidate_count};
    my ($previous,$best)=('',undef);
    for my $h (@$c) {
        fail('invalid candidate tuple') unless ref($h) eq 'ARRAY' && @$h==12 && !grep { ref($_) || !defined($_) } @$h;
        my $copy=[@$h];my $ranked=RTBioScan::R4A::hsp($copy);
        fail('candidate subject/taxid mismatch') unless RTBioScan::R4A::reference_taxid($h->[1],$h->[2]) eq $h->[2];
        fail('noncanonical HSP') unless join("\t",@$h) eq join("\t",@$copy);
        fail('candidate query mismatch') unless $h->[0] eq $v{sequence_hash} && $h->[11] eq $v{query_length};
        fail('duplicated/unordered subject') unless $h->[1] gt $previous;$previous=$h->[1];
        fail('unequal best evidence') if $best && RTBioScan::R4A::better($ranked,$best)!=0;$best//=$ranked;
    }
    if (usable($v{status})) {
        fail('usable result lacks evidence/depth') unless @$c && $v{depth}>=0;
        fail('status/origin mismatch') unless $v{origin} eq ($v{status} eq 'ASSIGNED' ? 'DIRECT' : 'LCA');
        fail('LCA must retain multiple candidates and no invented taxid') if $v{origin} eq 'LCA' && (@$c<2 || $v{resolved_taxid} ne 'NA');
    } else {
        fail('unusable result carries assignment') unless $v{depth}==-1 && $v{origin} eq 'NONE' && $v{resolved_taxid} eq 'NA';
    }
    fail('NO_HIT has evidence') if $v{status} eq 'NO_HIT' && @$c;
    fail('non-NO_HIT lacks evidence') if $v{status} ne 'NO_HIT' && !@$c;
    $v{candidates}=$c;return \%v;
}
sub read_sidecar {
    my ($path,$expected)=@_;my ($sig,$lines)=unseal($path,'TAXONOMY');
    fail('stale taxonomy signature') if defined($expected) && $sig ne $expected;
    fail('wrong taxonomy header') unless @$lines && shift(@$lines) eq join("\t",@FIELDS);
    my (@rows,$last,%sequence,%public);$last='';
    for (@$lines) {
        my $row=validate_record([split /\t/,$_,-1]);
        fail('duplicated/conflicting/unordered result') unless $row->{long_seq_id} gt $last;$last=$row->{long_seq_id};
        fail('ambiguous public consensus identity') if $public{$row->{consensus_id}}++;
        my $key=join("\t",@$row{qw(marker sequence_hash signature)});
        my $science=json()->encode([@$row{qw(query_length resolved_taxid)},@$row{@RANKS},@$row{qw(status depth origin candidate_count candidates_json reason)}]);
        fail('conflicting attribution for sequence') if exists($sequence{$key}) && $sequence{$key} ne $science;$sequence{$key}=$science;
        push @rows,$row;
    }
    return ($sig,\@rows);
}
sub write_sidecar {
    my ($path,$sig,$rows)=@_;my $body=join("\t",@FIELDS)."\n";my %seen;
    for my $r (sort {$a->{long_seq_id} cmp $b->{long_seq_id}} @$rows) {
        fail('duplicate result') if $seen{$r->{long_seq_id}}++;
        my @v=@$r{@FIELDS};validate_record(\@v);$body.=join("\t",@v)."\n";
    }
    atomic($path,seal('TAXONOMY',$sig,$body,1+@$rows));
}
sub config {
    my ($o)=@_;my %t;
    for (qw(marker kingdom database seed lineage taxonomy family genus species evalue max_hsps)) {
        $t{$_}=$o->{$_}//$ENV{'RTB_R4C_'.uc($_)}//'';
    }
    for (qw(database lineage taxonomy seed)) { $t{$_}=File::Spec->rel2abs($t{$_}) if $t{$_} ne '' && $t{$_} ne 'null'; }
    RTBioScan::OTURefineBlastreport::r4b_marker($t{marker});
    fail('missing configured kingdom') if !$t{kingdom} || $t{kingdom}=~/[;\t\r\n]/;
    for (qw(family genus species evalue)) { RTBioScan::R4A::number($t{$_},0,$_ eq 'evalue' ? undef : 100); }
    fail('unordered thresholds') unless cmpnum($t{family},$t{genus})<=0 && cmpnum($t{genus},$t{species})<=0;
    RTBioScan::R4A::uint($t{max_hsps});return \%t;
}
sub signature {
    my ($t)=@_;my $sig=RTBioScan::OTURefineBlastreport::r4b_signature($t,$t->{taxonomy});
    my $lin=$t->{lineage} eq '' || $t->{lineage} eq 'null' ? 'disabled' : digest($t->{lineage});
    return sha256_hex(join("\t",'R4-C-v1-hsp12-taxonomy23',$sig,$lin,$t->{kingdom},'task=megablast','dust=no','outfmt=6 qseqid sseqid staxids evalue length pident bitscore qstart qend sstart send qlen','max_target_seqs=database-sequence-count','word=50','qcov=50'));
}
sub target_count {
    my ($database)=@_;fail('missing BLAST database') unless defined($database) && length($database);
    local $ENV{LC_ALL}='C';
    open my $info,'-|','blastdbcmd','-db',$database,'-info' or fail('read BLAST database count');
    my @counts;
    while (<$info>) {push @counts,$1 if /^\s*([0-9][0-9,]*) sequences;/;}
    close $info or fail('BLAST database count failed');
    fail('ambiguous/missing BLAST database count') unless @counts==1;
    $counts[0] =~ s/,//g;
    fail('invalid BLAST database count') unless $counts[0]=~/\A[1-9][0-9]*\z/ && $counts[0]<=2147483647;
    return $counts[0];
}
sub identity_map {
    my ($path)=@_;my %map;return \%map unless defined($path) && -f $path;
    lines($path,sub {
        my @r=split /\t/,$_[0],-1;fail('malformed identity map') unless @r==5 || @r==6;
        fail('invalid stable ownership') unless $r[1] eq "$r[4]|$r[3]" && $r[3]=~/\A[0-9a-f]{32}\z/ && (split /\|/,$r[2])[1] eq $r[4];
        for my $display ($r[0],@r==6 ? $r[5] : ()) {
            next if $display eq 'NA';fail('ambiguous current ownership') if exists($map{$display}) && $map{$display} ne $r[1];$map{$display}=$r[1];
        }
    });return \%map;
}
sub prepare {
    my ($o)=@_;my $t=config($o);my $sig=signature($t);
    my ($seq,$map)=RTBioScan::R4A::fasta($o->{fasta});my $owners=identity_map($o->{'identity-map'});my (%cached,@queries);
    if (-e $o->{state}) {
        my ($old,$rows)=read_sidecar($o->{state});
        if ($old eq $sig) {
            for (@$rows) {
                fail('cached marker/signature mismatch') unless $_->{marker} eq $t->{marker} && $_->{signature} eq $sig;
                $cached{$_->{sequence_hash}}=$_;
            }
        }
        else { warn "R4-C: stale taxonomy signature; rebuilding consensus evidence\n"; }
    }
    for my $id (sort keys %$map) {
        my @p=split /\|/,$id;my $parsed=ConsensusIdUtil::parse_consensus_long_seq_id($id);
        fail('invalid consensus query identity') unless $parsed && @p>=3 && grep { $_ eq $t->{marker} } @p[2..$#p];
        my @d=map { /^OTU=(.+)$/ ? $1 : () } @p;fail('ambiguous consensus OTU identity') if @d>1;
        my $display=@d ? $d[0] : 'NA';my $stable=$owners->{$display}//'NA';
        fail('ownership marker mismatch') if $stable ne 'NA' && (split /\|/,$stable)[0] ne $t->{marker};
        push @queries,{long_seq_id=>$id,marker=>$t->{marker},sample=>$parsed->{sample},consensus_id=>$parsed->{consensus_id},display_otu_key=>$display,stable_otu_key=>$stable,sequence_hash=>$map->{$id},query_length=>''.length($seq->{$map->{$id}})};
    }
    my %new=map {$_=>length($seq->{$_})} grep {!exists $cached{$_}} keys %$seq;
    my %used=map { $_=>$cached{$_} } grep { exists $seq->{$_} } keys %cached;
    my $plan={target=>$t,queries=>\@queries,cached=>\%used,new=>\%new,signature=>$sig};
    atomic("$o->{prefix}.plan",seal('PLAN',$sig,json()->encode($plan)."\n",1));
    atomic("$o->{prefix}.fasta",join('',map {">$_\n$seq->{$_}\n"} sort keys %new));
}
sub selected_hsps {
    my ($path,$new,$scratch)=@_;my $normalized="$scratch/hsps.tsv";open my $n,'>',$normalized or fail('create evidence spool');
    lines($path,sub {
        my @r=split /\t/,$_[0],-1;fail('wrong HSP field count') unless @r==12;
        $r[2]=RTBioScan::R4A::reference_taxid($r[1],$r[2]);
        fail('unknown/non-new HSP query') unless exists $new->{$r[0]};
        fail('query length differs from sequence') unless $r[11] eq $new->{$r[0]};
        RTBioScan::R4A::hsp(\@r);print {$n} join("\t",@r)."\n" or fail('write HSP spool');
    });close $n or fail('close HSP spool');
    # External ordering bounds memory and places conflicting duplicates adjacent.
    local $ENV{LC_ALL}='C';open my $sort,'-|','sort','-t',"\t",'-k2,2','-k1,1','-k8,11',$normalized or fail('sort HSP spool');
    my (%best,$last_key,$last_row,$subject,$tax);$last_key=$last_row=$subject=$tax='';
    while (my $line=<$sort>) {
        chomp $line;my @r=split /\t/,$line,-1;my $key=RTBioScan::R4A::hsp_key(\@r);
        if ($key eq $last_key) { fail('conflicting duplicate HSP evidence') unless $line eq $last_row;next; }
        ($last_key,$last_row)=($key,$line);
        fail('conflicting subject taxids') if $r[1] eq $subject && $r[2] ne $tax;($subject,$tax)=@r[1,2];
        my $ranked=RTBioScan::R4A::ranked(\@r);my $h=$r[0];my $old=$best{$h};
        my $cmp=$old ? RTBioScan::R4A::better($ranked,$old->{score}) : -1;
        if ($cmp<0) { $best{$h}={score=>$ranked,subjects=>{$r[1]=>\@r}}; }
        elsif (!$cmp) {
            my $previous=$old->{subjects}{$r[1]};
            $old->{subjects}{$r[1]}=\@r if !$previous || $line lt join("\t",@$previous);
        }
    }
    close $sort or fail('HSP sort failed');
    return {map {my $h=$_;($h=>[map {$best{$h}{subjects}{$_}} sort keys %{$best{$h}{subjects}}])} keys %best};
}
sub authority {
    my ($t)=@_;my (%good,%bad);
    return {} if $t->{lineage} eq '' || $t->{lineage} eq 'null';
    lines($t->{lineage},sub {
        my ($id,$text,@extra)=split /\t/,$_[0],-1;
        fail('malformed configured lineage authority') unless defined($text) && !@extra && $id=~/\A-[1-9][0-9]*\z/;
        my $r=TaxonUtil::canonical_lineage($text);fail('malformed configured lineage ranks') unless $r && $r->[0] ne 'NA';
        return if $r->[0] ne $t->{kingdom};
        if ($good{$id}) {
            my ($conflict,$missing)=RTBioScan::OTURefineBlastreport::r4b_compare($good{$id},$r);
            $bad{$id}='REFERENCE_INCONSISTENT' if @$conflict;
            $bad{$id}//='REFERENCE_UNRESOLVED' if @$missing;
        } else { $good{$id}=$r; }
    });
    return {map {$_=>RTBioScan::OTURefineBlastreport::r4b_result($bad{$_}//'ASSIGNED',$bad{$_} ? 'conflicting_marker_authority' : 'NA',$_,$good{$_},'DIRECT')} keys %good};
}
sub resolve {
    my ($t,$selected,$scratch)=@_;my %needed;for my $rs (values %$selected) { $needed{$_->[2]}=1 for @$rs; }
    my $auth=authority($t);return {} unless %needed;
    my $before=File::Spec->rel2abs('.');chdir $scratch or fail('enter taxonomy scratch');
    # This landed batch helper writes scratch-local temporary files, never state.
    { package RTBioScan::R4A; require($RTBioScan::ConsensusTaxonomy::BIN.'/get_blast_taxdepth.pl'); }
    local $ENV{TAXONKIT_DB}=$t->{taxonomy};
    my $memory=RTBioScan::R4A::resolve_memory(\%needed,"$scratch/absent.memtax",'0'x64,$t->{seed},$t->{marker});
    chdir $before or fail('leave taxonomy scratch');
    my %numeric=map {$_=>1} grep {$_>0} keys %needed;
    for (keys %needed) { for (@{$memory->{$_}}[0..3]) { $numeric{$_}=1 if $_ ne 'NA' && $_>0; } }
    my $numeric=RTBioScan::OTURefineBlastreport::r4b_numeric_lineages(\%numeric,$t->{taxonomy});
    my ($metadata,$issues)=RTBioScan::OTURefineBlastreport::r4b_metadata($t,\%needed);
    fail('conflicting reference accession identities') if keys %{$issues->{conflicting_accession}//{}};
    my %out;
    for my $h (sort keys %$selected) {
        my @resolved;
        for my $row (@{$selected->{$h}}) {
            my %identities;my $r=RTBioScan::OTURefineBlastreport::r4b_resolve_candidate($t,$row,$memory,$auth,$metadata,$issues,$numeric,\%identities);
            # The landed resolver can encounter contradictory title ranks in
            # hash order. Record a stable disposition, not its first mismatch.
            $r=RTBioScan::OTURefineBlastreport::r4b_result('REFERENCE_INCONSISTENT',"$row->[2]:contradictory_populated_ranks") if $r->{reason}=~/contradictory_populated_ranks/;
            if (usable($r->{status})) {
                my $cap=cmpnum($row->[5],$t->{species})>=0 ? 6 : cmpnum($row->[5],$t->{genus})>=0 ? 5 : cmpnum($row->[5],$t->{family})>=0 ? 4 : 3;
                $r->{ranks}=RTBioScan::OTURefineBlastreport::r4b_trim($r->{ranks},$cap);
                my $depth=TaxonUtil::lineage_depth($r->{ranks});
                if ($depth<0) { $r=RTBioScan::OTURefineBlastreport::r4b_result('REFERENCE_UNRESOLVED','no_usable_rank'); }
                elsif ($depth<$cap || $cap==3) {
                    my @ids=keys %{$identities{TaxonUtil::lineage_text($r->{ranks})}//{}};
                    $r->{taxid}=@ids==1 ? $ids[0] : 'NA';
                }
            }
            push @resolved,$r;
        }
        my $result;
        for my $status (qw(REFERENCE_INCONSISTENT REFERENCE_UNRESOLVED COMPUTATION_FAILED)) {
            my @bad=grep {$_->{status} eq $status} @resolved;
            if (@bad) {$result=RTBioScan::OTURefineBlastreport::r4b_result($status,join(',',sort map {$_->{reason}} @bad));last;}
        }
        if (!$result) {
            my %identities=map {RTBioScan::OTURefineBlastreport::r4b_identity($_)=>$_} @resolved;
            my %taxids=map {$_->[2]=>1} @{$selected->{$h}};
            $result=keys(%identities)==1 && (keys(%taxids)==1 || $resolved[0]{taxid} ne 'NA') ? $resolved[0]
                : RTBioScan::OTURefineBlastreport::r4b_lca(\@resolved,{});
            $result->{taxid}='NA' if $result->{origin} eq 'LCA';
        }
        $out{$h}=$result;
    }
    return \%out;
}
sub read_plan {
    my ($path)=@_;my ($sig,$rows)=unseal($path,'PLAN');fail('invalid plan') unless @$rows==1;
    my $plan=eval {json()->decode($rows->[0])};
    fail('invalid plan encoding') unless ref($plan) eq 'HASH' && json()->encode($plan) eq $rows->[0]
        && join(',',sort keys %$plan) eq 'cached,new,queries,signature,target'
        && $plan->{signature} eq $sig && ref($plan->{target}) eq 'HASH'
        && ref($plan->{queries}) eq 'ARRAY' && ref($plan->{cached}) eq 'HASH' && ref($plan->{new}) eq 'HASH';
    my $t=config($plan->{target});my (%ids,%public,%hashes);my $last='';
    for my $q (@{$plan->{queries}}) {
        fail('invalid plan query') unless ref($q) eq 'HASH' && join(',',sort keys %$q) eq
            'consensus_id,display_otu_key,long_seq_id,marker,query_length,sample,sequence_hash,stable_otu_key';
        my %r=(%$q, resolved_taxid=>'NA',status=>'NO_HIT',depth=>'-1',origin=>'NONE',candidate_count=>'0',candidates_json=>'[]',reason=>'no_hit',signature=>$sig);
        @r{@RANKS}=('NA')x7;validate_record([@r{@FIELDS}]);
        fail('plan marker mismatch') unless $q->{marker} eq $t->{marker};
        fail('duplicated/ambiguous/unordered plan query') unless $q->{long_seq_id} gt $last && !$public{$q->{consensus_id}}++;
        $last=$q->{long_seq_id};my $h=$q->{sequence_hash};
        fail('conflicting plan sequence length') if exists($hashes{$h}) && $hashes{$h} ne $q->{query_length};$hashes{$h}=$q->{query_length};
    }
    for my $h (keys %hashes) {
        fail('missing/overlapping plan evidence') unless exists($plan->{new}{$h}) != exists($plan->{cached}{$h});
        if (exists $plan->{new}{$h}) {fail('plan length mismatch') unless $plan->{new}{$h} eq $hashes{$h};}
        else {
            my $r=$plan->{cached}{$h};fail('invalid cached plan row') unless ref($r) eq 'HASH';validate_record([@$r{@FIELDS}]);
            fail('stale/incompatible cached plan row') unless $r->{signature} eq $sig && $r->{marker} eq $t->{marker} && $r->{sequence_hash} eq $h && $r->{query_length} eq $hashes{$h};
        }
    }
    fail('extraneous plan evidence') unless keys(%hashes)==keys(%{$plan->{new}})+keys(%{$plan->{cached}});
    return $plan;
}
sub complete {
    my ($o)=@_;my $plan=read_plan("$o->{prefix}.plan");my $sig=$plan->{signature};
    fail('stale prepared scientific signature') unless signature($plan->{target}) eq $sig;
    my $scratch=tempdir('r4c-XXXXXX',TMPDIR=>1,CLEANUP=>1);my $selected=selected_hsps($o->{raw},$plan->{new},$scratch);
    my $resolved=resolve($plan->{target},$selected,$scratch);my @results;
    for my $q (@{$plan->{queries}}) {
        my $h=$q->{sequence_hash};my %r=%$q;
        if (my $cached=$plan->{cached}{$h}) {
            validate_record([@$cached{@FIELDS}]);
            @r{qw(resolved_taxid status depth origin candidate_count candidates_json reason)}=@$cached{qw(resolved_taxid status depth origin candidate_count candidates_json reason)};
            @r{@RANKS}=@$cached{@RANKS};
        } else {
            fail('query not in plan') unless exists $plan->{new}{$h};
            my $res=$resolved->{$h}//RTBioScan::OTURefineBlastreport::r4b_result('NO_HIT','no_hit');
            @r{qw(resolved_taxid status origin reason)}=@$res{qw(taxid status origin reason)};@r{@RANKS}=@{$res->{ranks}};
            $r{depth}=''.TaxonUtil::lineage_depth($res->{ranks});$r{candidate_count}=''.scalar(@{$selected->{$h}//[]});$r{candidates_json}=json()->encode($selected->{$h}//[]);
        }
        $r{signature}=$sig;push @results,\%r;
    }
    write_sidecar("$o->{prefix}.taxonomy",$sig,\@results);
}
sub publish {
    my ($from,$to,$plans)=@_;fail('publication requires prepared generation') unless $plans && @$plans;
    my (@sigs,%queries,%markers);
    for my $path (@$plans) {
        my $p=read_plan($path);my $marker=$p->{target}{marker};fail('duplicate publication marker') if exists $markers{$marker};
        $markers{$marker}=$p->{signature};push @sigs,$p->{signature};
        for my $q (@{$p->{queries}}) {fail('duplicate publication query') if exists $queries{$q->{long_seq_id}};$queries{$q->{long_seq_id}}=$q;}
    }
    my ($sig,$rows)=read_sidecar($from);my $combined=sha256_hex(join("\n",sort @sigs));
    fail('publication signature mismatch') unless $sig eq $combined || (@sigs==1 && $sig eq $sigs[0]);
    fail('publication cardinality mismatch') unless @$rows==keys %queries;
    for my $r (@$rows) {
        my $q=$queries{$r->{long_seq_id}};fail('publication query mismatch') unless $q;
        fail('publication marker signature mismatch') unless $r->{signature} eq $markers{$r->{marker}};
        for (keys %$q) {fail('publication generation identity mismatch') unless $r->{$_} eq $q->{$_};}
    }
    # Publish the validated in-memory records, never reopen mutable input bytes.
    write_sidecar($to,$sig,$rows);
}
sub project {
    my ($rows,$full,$csv)=@_;
    my $table="long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n";
    my $raw="qseqid,sseqid,evalue,length,pident\n";
    for my $r (@$rows) {
        my @lin=usable($r->{status}) ? map {$_ eq 'NA' ? 'Unassigned' : $_} @$r{@RANKS} : ('Unassigned')x7;
        $table.=join("\t",$r->{long_seq_id},$r->{resolved_taxid},@lin)."\n";
        my $hs=$r->{candidates};my @metrics=@$hs ? (RTBioScan::R4A::display_number($hs->[0][3]),$hs->[0][4],sprintf('%.3f', 0 + $hs->[0][5])) : ('NA')x3;
        $raw.=join(',',map {RTBioScan::R4A::csv_cell($_)} ($r->{long_seq_id},$r->{origin} eq 'DIRECT' && @$hs==1 ? $hs->[0][1] : 'NA',@metrics))."\n";
    }
    atomic($full,$table);atomic($csv,$raw);
}
sub merge {
    my ($o,$inputs)=@_;my (@rows,@sigs);for my $file (@$inputs) {my ($s,$r)=read_sidecar($file);push @rows,@$r;push @sigs,$s;}
    my $sig=sha256_hex(join("\n",sort @sigs));write_sidecar($o->{out},$sig,\@rows);
    my (undef,$validated)=read_sidecar($o->{out});project($validated,$o->{full},$o->{csv});
}
# Legacy CLI report inputs remain a compatibility projection, never a cache or
# production authority. Production callers explicitly require the sealed sidecar.
sub assignments {
    my ($sidecar,$report,$level)=@_;fail('invalid minimum level') unless $level=~/\A(?:family|genus|species)\z/;
    my %assigned;
    if (defined($sidecar) && $sidecar ne '') {
        my (undef,$rows)=read_sidecar($sidecar);my %minimum=(family=>4,genus=>5,species=>6);
        for (@$rows) {$assigned{$_->{consensus_id}}=$_ if usable($_->{status}) && $_->{depth}>=$minimum{$level};}
    } elsif (-s $report) {
        open my $f,'<',$report or fail("open $report: $!");
        my ($head)=TabularSchemaUtil::read_required_header($f,$report,'long_seq_id');
        while (my $line=<$f>) {
            $line =~ s/\r?\n\z//;my @v=split /\t/,$line,-1;my %r;@r{@$head}=@v;
            my $id=ConsensusIdUtil::consensus_id_from_long_seq_id($r{long_seq_id});next unless defined($id) && length($id);
            $assigned{$id}=\%r if TaxonUtil::assignment_from_row(\%r,$level);
        }
        close $f or fail('close legacy projection');
    }
    return \%assigned;
}
sub main {
    my $command=shift @ARGV//' ';my %o;
    GetOptionsFromArray(\@ARGV,\%o,(map {"$_=s"} qw(fasta state prefix raw identity-map marker kingdom database seed lineage taxonomy family genus species evalue max_hsps input out full csv expected)),'plan=s@') or fail('invalid arguments');
    if ($command eq 'target-count') {print target_count($o{database})."\n";}
    elsif ($command eq 'prepare') {prepare(\%o);}
    elsif ($command eq 'complete') {complete(\%o);}
    elsif ($command eq 'publish') {publish($o{input},$o{out},$o{plan});}
    elsif ($command eq 'merge') {merge(\%o,\@ARGV);}
    elsif ($command eq 'validate') {read_sidecar($o{input},$o{expected});}
    else {fail('unknown command');}
}
main() unless caller;
1;
