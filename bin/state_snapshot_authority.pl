#!/usr/bin/env perl
# Joint-v2 capture, transaction and restore authority.
# Schema and operational boundaries: docs/r5_joint_v2_protocol.md.
use strict;
use warnings;
use Digest::SHA qw(sha256_hex);
use Fcntl qw(:DEFAULT :mode :flock F_GETFD F_SETFD FD_CLOEXEC);
use File::Temp qw(tempfile tempdir);
use File::Basename qw(dirname basename);
use FindBin;
use Cwd qw(abs_path);
use File::Spec;
my $LIMIT = 2 * 1024 * 1024 + 1024;
my $CHUNK = 1024 * 1024;
my $HEADER = "#RTB-JOINT-AUTHORITY\t2\n";
my @EXACT = qw(done_pod5.txt qced_reads_hq_accumulated.fasta round_index.tsv read_qscore_rolling.tsv otu_frozen_reps.fasta otu_frozen_reps.fasta.gz otu_active_pool.fasta otu_seen_hashes.tsv otu_consolidated_keys.tsv state_compatibility_manifest.tsv);
my %EXACT = map { $_ => 1 } @EXACT;
my @SUFFIX = qw(_assigned_read_ids_ever.list _protected_read_ids_ever.list _assigned_otu_keys_ever.list _pruned_barrier.list _pruned_archive.fasta _otu_size_streak.tsv _otu_unassigned_streak.tsv consensus_consolidated_ids.txt _seen_read_ids.tsv _on_target_state.tsv);
my @PARSER = qw(demult_rpt.txt demult_rpt.contract.tsv otu_def_rpt.txt otu_def_rpt.contract.tsv demult_bootstrap.seeded);
my @FATE = qw(read_fate_demux_seen.tsv read_fate_blast_seen.tsv demux_annotation_cache.tsv);
my @GRACE = qw(assigned_otu_member_ids_prev_round.list consensus_assigned_member_ids_prev_round.list);
sub fail { die "JOINT_GROUP_INCOMPLETE: @_\n" }
sub hexstr { unpack('H*', $_[0]) }
sub unhex {
    my ($s) = @_;
    fail('noncanonical hex') unless defined($s) && $s =~ /\A(?:[0-9a-f]{2})+\z/ && length($s) <= 2*$CHUNK;
    my $v = pack('H*', $s); fail('NUL in identity/path') if index($v,"\0") >= 0; return $v;
}
sub integer {
    my ($s) = @_;
    fail('integer overflow/noncanonical decimal') unless defined($s) && $s =~ /\A(?:0|[1-9][0-9]*)\z/ && (length($s)<19 || (length($s)==19 && $s le '9223372036854775807'));
    return 0+$s;
}
sub safe_component { defined($_[0]) && $_[0] =~ /\A[A-Za-z0-9_][A-Za-z0-9._-]*\z/ }
sub safe_path {
    my ($p)=@_;
    fail('unsafe relative path') if !defined($p) || !length($p) || length($p)>$CHUNK || $p =~ m{(?:\A/|/\z|//|\0)};
    fail('unsafe path component') if grep { $_ eq '.' || $_ eq '..' || $_ eq '' } split m{/},$p,-1;
    return $p;
}
sub f01 {
    my ($n)=@_; return 0 if $n =~ /^\./;
    return 1 if $EXACT{$n} || $n =~ /\Aotu_frozen_.*\.tsv\z/s;
    for (@SUFFIX) { return 1 if length($n)>=length($_) && substr($n,-length($_)) eq $_ }
    return 0;
}
sub node {
    my ($path)=@_;
    my @s=lstat($path); return 'A' if !@s && $!{ENOENT};
    fail("cannot lstat $path: $!") unless @s;
    return 'F' if S_ISREG($s[2]); return 'D' if S_ISDIR($s[2]); fail("unsafe node $path");
}
sub ancestors {
    my ($path)=@_; my $p=dirname($path); my @todo;
    while ($p ne '/' && $p ne '.') { push @todo,$p; my $next=dirname($p); last if $next eq $p; $p=$next }
    for (reverse @todo) { fail("unsafe ancestor $_") unless node($_) eq 'D' }
}
sub open_regular {
    my ($p)=@_; ancestors($p); my @before=lstat($p); fail("not a regular file $p") unless @before && S_ISREG($before[2]);
    sysopen(my $f,$p,O_RDONLY|O_NOFOLLOW) or fail("open $p: $!"); binmode($f); my @after=stat($f);
    fail("file changed while opening $p") unless @after && $before[0]==$after[0] && $before[1]==$after[1]; return $f;
}
sub digest {
    my ($p)=@_; my $f=open_regular($p); my $sha=Digest::SHA->new(256);my $size=0;
    while(1){ my $n=sysread($f,my $b,$CHUNK);fail("read $p: $!") unless defined $n; last if !$n;$sha->add($b);$size+=$n }
    close($f) or fail("close $p"); return ($size,$sha->hexdigest);
}
# sysread bounds allocation before looking for the next LF (readline itself is
# unbounded for a malicious single-line record).
sub reader { return { fh=>open_regular($_[0]), buf=>'', eof=>0 } }
sub line {
    my ($r)=@_;
    while(1){
        my $i=index($r->{buf},"\n");
        if($i>=0){fail('record line too large') if $i+1>$LIMIT;return substr($r->{buf},0,$i+1,'')}
        fail('record line too large') if length($r->{buf})>$LIMIT;
        if($r->{eof}){fail('unterminated record') if length($r->{buf});return undef}
        my $n=sysread($r->{fh},my $b,65536);fail('record read failure') unless defined $n;$r->{eof}=1 if !$n;$r->{buf}.=$b;
    }
}
# Bounded sorted runs; paths are hex encoded so byte order is portable and no
# payload filename is interpreted by a line-oriented OS command.
sub sorter { return { rows=>[],bytes=>0,runs=>[],dir=>$_[0] } }
sub flush_run {
    my($s)=@_;return unless @{$s->{rows}};
    my($f,$p)=tempfile('joint-sort-XXXXXXXX',DIR=>$s->{dir},UNLINK=>0);binmode($f);
    for(sort @{$s->{rows}}){print {$f} $_ or fail('sort write')}
    close($f) or fail('sort close');push @{$s->{runs}},$p;$s->{rows}=[];$s->{bytes}=0;
}
sub add_sort {my($s,$row)=@_;push @{$s->{rows}},$row;$s->{bytes}+=length($row);flush_run($s) if $s->{bytes}>=32*1024*1024}
sub merge_runs {
    my($s,@paths)=@_;my @read=map {reader($_)} @paths;my @heads=map {line($_)} @read;
    my($f,$p)=tempfile('joint-merge-XXXXXXXX',DIR=>$s->{dir},UNLINK=>0);binmode($f);
    while(1){my $k;for my $i(0..$#heads){next unless defined($heads[$i]);$k=$i if !defined($k)||$heads[$i] lt $heads[$k]}last unless defined($k);print {$f} $heads[$k] or fail('merge write');$heads[$k]=line($read[$k])}
    close($f) or fail('merge close');for(@read){close($_->{fh}) or fail('merge source close')}for(@paths){unlink($_) or fail('owned sort cleanup')}return $p;
}
sub sorted_file {
    my($s)=@_;flush_run($s);
    if(!@{$s->{runs}}){my($f,$p)=tempfile('joint-sort-XXXXXXXX',DIR=>$s->{dir},UNLINK=>0);close($f);return $p}
    while(@{$s->{runs}}>1){my @next;while(@{$s->{runs}}){my @batch=splice(@{$s->{runs}},0,16);push @next,@batch==1?$batch[0]:merge_runs($s,@batch)}$s->{runs}=\@next}
    return $s->{runs}[0];
}
sub shape {
    my($m)=@_;my(%g,%p);
    my $add=sub {my($id,$kind,$key,$rule,@paths)=@_;$g{$id}=[$kind,hexstr($key),$rule,scalar @paths];$p{$_}=$id for @paths};
    $add->('f01','f01','state','closed',map {"state_authority/$_"} @EXACT);
    $add->('cache','cache','state','tree','sequences/Consensus/.cache');
    $add->('consensus','consensus','state','vector',map {"sequences/Consensus/$_"} qw(consensus_ownership.tsv consolidated_consensus_ids.txt));
    $add->('sup','sup','state','vector',map {"state_authority/$_"} qw(blastreport_sup_annotated_pre.fastq blastreport_sup_annotated_pre.fastq.gz));
    for my $b(@{$m->{barcodes}}){my $h=hexstr($b);for my $family(['parser',\@PARSER,'parser'],['fate',\@FATE,'read-fate'],['grace',\@GRACE,'grace']){
        $add->("$family->[0].$h",$family->[2],$b,$family->[0] eq 'grace'?'vector':'unit',map {"state_authority/${b}_$_"} @{$family->[1]});
    }}
    my $i=0;for my $t(@{$m->{targets}}){++$i;$add->("blast.$i",'blast',"$i:$t",'unit',map {"state_authority/$_"} ("otu_blast_cache_$t.tsv","otu_blast_evidence_$t.tsv","memtax$i.txt"))}
    return (\%g,\%p);
}
sub parse {
    my($path,$work,$visit)=@_;my $r=reader($path);my $sha=Digest::SHA->new(256);my $whole=Digest::SHA->new(256);
    my $take=sub{my $s=line($r);fail('truncated authority') unless defined $s;fail('CR/NUL authority') if $s =~ /[\r\0]/;$sha->add($s);$whole->add($s);return $s};
    fail('JOINT_V2_REQUIRED') unless $take->() eq $HEADER;
    my $s=$take->();my($state)=$s=~/\Astate\t([A-Za-z0-9_.-]+)\n\z/;fail('invalid state') unless defined($state)&&$state ne '.'&&$state ne '..';
    $s=$take->();my($bh,$rh,$token)=$s=~/\Aboundary\t([0-9a-f]+)\t([0-9a-f]+)\t([0-9a-f]{64})\tfull_round\n\z/;fail('invalid boundary') unless defined($token);
    my $m={state=>$state,barcode=>unhex($bh),round=>unhex($rh),token=>$token,barcodes=>[],targets=>[]};fail('unsafe boundary') unless safe_component($m->{barcode})&&length($m->{round})&&$m->{round} ne '.'&&$m->{round} ne '..'&&$m->{round}!~m{[/\r\n\t]};
    $s=$take->();my($ctx)=$s=~/\Acontext\t([0-9a-f]+)\n\z/;$m->{context}=unhex($ctx);
    $s=$take->();my $prev='';while($s=~/\Abarcode\t([0-9a-f]+)\n\z/){my $b=unhex($1);fail('barcode order/safety') unless safe_component($b)&&$b gt $prev;push @{$m->{barcodes}},$b;$prev=$b;$s=$take->()}
    fail('boundary barcode outside roster') unless grep {$_ eq $m->{barcode}} @{$m->{barcodes}};
    my%targets;while($s=~/\Atarget\t([^\t]+)\t([0-9a-f]+)\n\z/){my($i,$t)=(integer($1),unhex($2));fail('target identity/order') unless $i==1+@{$m->{targets}} && $t=~/\A[A-Z0-9][A-Z0-9_.-]*\z/ && $t ne 'ITS' && $t ne 'ITS1' && !$targets{$t}++;push @{$m->{targets}},$t;$s=$take->()}
    fail('empty target roster') unless @{$m->{targets}};
    my($groups,$paths)=shape($m);my(%decl,%counts,%states);$prev='';
    while($s=~/\Agroup\t([^\t]+)\t([^\t]+)\t([^\t]+)\t([^\t]+)\t([^\t]+)\n\z/){my($g,$kind,$key,$rule,$n)=($1,$2,$3,$4,integer($5));fail('group order/identity') unless $g gt $prev&&exists($groups->{$g});$prev=$g;my$expected=$groups->{$g};fail('group metadata') unless $kind eq $expected->[0]&&$key eq $expected->[1]&&($expected->[2] eq 'unit' ? $rule=~/\Aall-(?:absent|present)\z/ : $rule eq $expected->[2]);fail('group count') if ($g ne 'f01'&&$g ne 'cache'&&$n!=$expected->[3])||!$n;$decl{$g}=[$rule,$n];$s=$take->()}
    fail('missing mandatory group') unless keys(%decl)==keys(%$groups);
    my$dirs=sorter($work);my$parents=sorter($work);my%fixed;my$n=0;$prev='';
    while($s=~/\Aentry\t([^\t]+)\t([^\t]+)\t([ADF])\t([^\t]+)\t([^\t]+)\n\z/){my($g,$h,$type,$size,$hash)=($1,$2,$3,$4,$5);my$p=safe_path(unhex($h));fail('entry duplicate/order') unless $p gt $prev;$prev=$p;fail('undeclared group') unless $decl{$g};
        if($type eq 'F'){integer($size);fail('invalid file digest') unless $hash=~/\A[0-9a-f]{64}\z/;fail('F0 digest') if !$size&&$hash ne sha256_hex('')}
        else{fail('non-file fields') unless $size eq '-'&&$hash eq '-'}
        if(exists($paths->{$p})){fail('wrong group') unless $g eq $paths->{$p};$fixed{$p}=1}
        elsif($g eq 'f01'&&$p=~m{\Astate_authority/([^/]+)\z}){my$name=$1;fail('invalid F01 member') unless safe_component($name)&&f01($name);if($type eq 'A'){my%optional=map{my$b=$_;map{($b.$_)=>1}@SUFFIX}@{$m->{barcodes}};fail('unrecognized absent F01 member') unless $optional{$name};push@{$m->{optional_absent}},$p}}
        elsif($g eq 'cache'&&index($p,'sequences/Consensus/.cache/')==0){fail('absent interior cache entry') if $type eq 'A'}
        else{fail('entry outside inventory')}
        fail('directory outside cache') if $type eq 'D'&&$g ne 'cache';
        fail('cache root is not a directory') if $p eq 'sequences/Consensus/.cache'&&$type eq 'F';
        if($g eq 'cache'){add_sort($dirs,"$h\n") if $type eq 'D';add_sort($parents,hexstr(dirname($p))."\n") if $p ne 'sequences/Consensus/.cache'}
        if($p eq 'state_authority/done_pod5.txt'){fail('missing completed ledger') unless $type eq 'F'&&$size>0}
        ++$counts{$g};$states{$g}{$type}=1;++$n;$visit->($m,$p,$type,$size,$hash) if $visit;
        # The footer is not part of the body hash.
        $s=line($r);fail('missing footer') unless defined $s;$whole->add($s);$sha->add($s) unless $s=~/\A#END\t/;
    }
    my($ng,$ne,$sum)=$s=~/\A#END\t(0|[1-9][0-9]*)\t(0|[1-9][0-9]*)\t([0-9a-f]{64})\n\z/;
    fail('invalid footer/count/digest') unless defined($sum)&&integer($ng)==keys(%decl)&&integer($ne)==$n&&$sum eq $sha->hexdigest;
    fail('trailing authority bytes') if defined(line($r));close($r->{fh}) or fail('authority close');
    fail('missing fixed entry') if grep {!$fixed{$_}} keys %$paths;
    for my$g(keys %decl){fail('group entry count') unless ($counts{$g}//0)==$decl{$g}[1];my$rule=$decl{$g}[0];fail('partial group') if $rule eq 'all-absent'&&keys(%{$states{$g}})!=1 || $rule eq 'all-absent'&&!$states{$g}{A} || $rule eq 'all-present'&&($states{$g}{A}||$states{$g}{D});}
    for my$g('parser.'.$bh,'fate.'.$bh){fail('current completed unit absent') unless $decl{$g}[0] eq 'all-present'}
    fail('absent cache with descendants') if $states{cache}{A}&&$counts{cache}!=1;
    my$dp=sorted_file($dirs);my$pp=sorted_file($parents);my$dr=reader($dp);my$pr=reader($pp);my$d=line($dr);
    while(my$p=line($pr)){while(defined($d)&&$d lt $p){$d=line($dr)}fail('cache parent missing/non-directory') unless defined($d)&&$d eq $p}
    close($dr->{fh});close($pr->{fh});unlink($dp) or fail('owned sort cleanup');unlink($pp) or fail('owned sort cleanup');
    $m->{groups}=\%decl;$m->{sha}=$whole->hexdigest;return $m;
}
1;
sub walk {
    my($path,$relative,$visit)=@_;my$t=node($path);$visit->($path,$relative,$t);return unless $t eq 'D';
    opendir(my$d,$path) or fail("opendir $path");while(defined(my$n=readdir($d))){next if $n eq '.'||$n eq '..';walk("$path/$n","$relative/$n",$visit)}closedir($d) or fail('closedir');
}
sub scan_names {
    my($dir,$visit)=@_;return if node($dir) eq 'A';fail("not directory $dir") unless node($dir) eq 'D';
    opendir(my$d,$dir) or fail("opendir $dir");while(defined(my$n=readdir($d))){next if $n eq '.'||$n eq '..';$visit->($n)}closedir($d) or fail('closedir');
}
sub pending_absent {
    my($s)=@_;scan_names($s,sub { fail("READ_FATE_NORMALIZATION_PENDING: $s/$_[0]") if $_[0]=~/\A\.read_fate_normalization_pending(?:\z|\.)/ });
}
sub control {
    my($n)=@_;return $n=~/\A(?:\.f03-transaction(?:\z|\.)|\.f03-txn(?:\z|\.)|\.AUTHORITY\.tmp\.|\.read_fate_normalization_pending(?:\z|\.))/;
}
sub round_control {$_[0]=~/\A(?:\.round_inflight\.|round_inflight\.txt\z|\.round_lock_(?:handoff|release|finish|revocation|events|operator_events|operator_pending|archives)(?:\z|\.))/}
sub residue {
    my($root,$allow_marker)=@_;return if node($root) eq 'A';
    walk($root,'',sub {my($p,$rel,$t)=@_;my$n=basename($p);return if $allow_marker&&$rel eq '/.f03-transaction'&&$t eq 'F';
        # Cache-internal protocol-looking names are ordinary cache data, except
        # the already protected round-lock namespace.
        fail("JOINT_TRANSACTION_INCOMPLETE: $p") if round_control($n) || (index($rel,'/sequences/Consensus/.cache/')!=0&&control($n));
    });
}
sub source_path {
    my($base,$p,$live)=@_;return "$base/$p" unless $live;
    return "$base/_state/".substr($p,length('state_authority/')) if index($p,'state_authority/')==0;
    return "$base/".substr($p,length('sequences/')) if index($p,'sequences/')==0;
    fail('unmapped source');
}
sub roster {
    my($live,$current,@retained)=@_;my%b=map{$_=>1}($current,@retained);
    scan_names("$live/_state",sub{my$n=shift;for my$s(@PARSER[1,3,4],@FATE,@GRACE){my$tail="_$s";if(length($n)>length($tail)&&substr($n,-length($tail)) eq $tail){my$x=substr($n,0,length($n)-length($tail));fail('unsafe barcode') unless safe_component($x);$b{$x}=1}}});
    return [sort keys %b];
}
sub group_checks {
    my($m,$base,$live,$work)=@_;my$s=$live?"$base/_state":"$base/state_authority";
    require "$FindBin::Bin/reporting_contract_sidecar.pl";
    require "$FindBin::Bin/reporting_parser_state_transaction.pl";
    for my$b(@{$m->{barcodes}}){
        my@g=map{node("$s/${b}_$_")}@PARSER;my$n=grep{$_ ne 'A'}@g;
        fail("parser.$b partial/absent current") if ($n&&$n!=5)||(!$n&&$b eq $m->{barcode});
        if($n){my@r;for my$kind(qw(demult_rpt otu_def_rpt)){push @r,ReportingContractSidecar::validate_report_and_sidecar(report_kind=>$kind,context=>$m->{context},report_path=>"$s/${b}_$kind.txt",sidecar_path=>"$s/${b}_$kind.contract.tsv")}
            fail("parser.$b OTU without demult") if $r[0]{state} eq 'empty'&&$r[1]{state} eq 'nonempty';
        }
        if($live&&node("$s/.parser_state_txn/$b") ne 'A'){
            my$p=ReportingParserStateTxn::canonical_paths(state_dir=>$s,barcode=>$b);
            walk($p->{tx_root},'',sub{fail('unsafe parser journal') if $_[2] ne 'F'&&$_[2] ne 'D'});
            fail('unfinished parser temporary') if node($p->{manifest_tmp_path}) ne 'A';
            my($meta,$bad)=ReportingParserStateTxn::_read_manifest_partial($p->{manifest_path});fail('malformed parser journal') if $bad;
            ReportingParserStateTxn::_validate_manifest_committed_minimal_or_die($meta,$p,$b);
            ReportingParserStateTxn::_validate_tx_root_entries_or_die($p);
            ReportingParserStateTxn::_validate_committed_live_state_or_die(paths=>$p,context=>$m->{context},meta=>$meta);
        }
        @g=map{node("$s/${b}_$_")}@FATE;$n=grep{$_ ne 'A'}@g;
        fail("read-fate.$b partial/absent current") if ($n&&$n!=3)||(!$n&&$b eq $m->{barcode});
    }
    require "$FindBin::Bin/lib/taxon_util.pl" unless defined &TaxonUtil::canonical_lineage;
    require "$FindBin::Bin/lib/RTBioScan/OTURefineBlastreport.pm";
    {package RTBioScan::R4A;require "$FindBin::Bin/cache_blast_by_hash.pl" unless defined &RTBioScan::R4A::evidence_read;}
    my%demands;
    if($live){
        for my$b(@{$m->{barcodes}}){my@in=("$s/otu_frozen_members.tsv","$s/${b}_otu_active_members.tsv");
            for my$i(0..1){next if node($in[$i]) eq 'A';my$r=reader($in[$i]);while(my$l=line($r)){chomp($l);next if $l eq '';my@f=split /\t/,$l,-1;fail("malformed membership $in[$i]") unless @f==3+$i&&length($f[0])&&length($f[1])&&$f[2]=~/\A[01]\z/;fail('unsafe membership identity') if $f[0]=~/[\r\n\0]/||$f[1]=~/\s/;fail('invalid active pool hash') if $i&&$f[3]!~/\A[0-9a-f]{64}\z/}close($r->{fh})}
            my($out,$clstr)=tempfile('joint-demand-XXXXXXXX',DIR=>$work,UNLINK=>1);close($out);
            system($^X,"$FindBin::Bin/otu_merge_clstr.pl",@in,$clstr)==0 or fail('membership merge failed');
            my($cid,%seen);my%configured=map{$_=>1}@{$m->{targets}};
            RTBioScan::R4A::each_line($clstr,sub {my$l=shift;if($l=~/\A>Cluster (\d+)\z/){$cid=$1;return}fail('malformed canonical member') unless defined($cid)&&$l=~/>([^\s]+)\.\.\.(?:\s|$)/;my($base,$marker)=split /\|/,$1;RTBioScan::OTURefineBlastreport::r4b_marker($marker);fail('unconfigured member marker') unless $configured{$marker};my$key="$base|$marker";fail('member in multiple clusters') if exists($seen{$key})&&$seen{$key} ne $cid;$seen{$key}=$cid;$demands{$marker}{$key}=1});
        }
    }
    my$i=0;for my$t(@{$m->{targets}}){++$i;my@p=map{"$s/$_"}("otu_blast_cache_$t.tsv","otu_blast_evidence_$t.tsv","memtax$i.txt");my$n=grep{node($_) ne 'A'}@p;
        fail("blast.$i partial group") if $n&&$n!=3;
        if(!$n){fail("BLAST_REQUIRED_EVIDENCE_MISSING: $m->{state} $i:$t sources=$s/otu_frozen_members.tsv,".join(',',map{"$s/${_}_otu_active_members.tsv"}@{$m->{barcodes}})." count=".scalar(keys %{$demands{$t}})) if $live&&$demands{$t}&&keys %{$demands{$t}};next}
        my$sig;for my$j(0..2){my$r=reader($p[$j]);my$h=line($r);close($r->{fh});my$kind=(qw(CACHE EVIDENCE MEMTAX))[$j];my$v=(2,1,2)[$j];my($x)=($h//'')=~/\A#RTB-R4-\Q$kind\E\t\Q$v\E\t([0-9a-f]{64})\n\z/;fail("invalid blast envelope $p[$j]") unless defined($x);fail('blast signature mismatch') if defined($sig)&&$sig ne $x;$sig=$x}
        my$c=RTBioScan::R4A::cache_read($p[0],$sig,undef,$t);RTBioScan::R4A::evidence_read($p[1]);RTBioScan::R4A::memtax_read($p[2],$sig);
        my$r=reader($p[1]);line($r);RTBioScan::R4A::evidence_rows($c,$c->{m},sub {my$got=line($r);fail('BLAST all-evidence projection mismatch') unless defined($got)&&$got eq "$_[0]\n"});my$end=line($r);fail('BLAST extra evidence') unless defined($end)&&$end=~/\A#END\t/&&!defined(line($r));close($r->{fh});
    }
}
sub census {
    my($m,$base,$live,$work)=@_;my($groups,$paths)=shape($m);$paths->{$_}='f01' for @{$m->{optional_absent}//[]};my$s=sorter($work);my(%count,%types);
    my$emit=sub{my($path,$g)=@_;my$src=source_path($base,$path,$live);ancestors($src) if node(dirname($src)) ne 'A';my$t=node($src);fail("directory outside cache $src") if $t eq 'D'&&$g ne 'cache';my($size,$sha)= $t eq 'F'?digest($src):('-','-');add_sort($s,join("\t",hexstr($path),$g,$t,$size,$sha)."\n");++$count{$g};$types{$g}{$t}=1};
    for my$p(keys %$paths){next if $p eq 'sequences/Consensus/.cache';$emit->($p,$paths->{$p})}
    my$state=$live?"$base/_state":"$base/state_authority";
    scan_names($state,sub{my$n=shift;my$p="state_authority/$n";return if exists($paths->{$p});return if !$live&&($n eq 'AUTHORITY'||$n eq '.lock');
        if(f01($n)){fail("unsafe F01 $n") unless safe_component($n);$emit->($p,'f01');return}
        fail("unassociated scientific state $n") if !$live || $n=~/\A(?:otu_blast_(?:cache|evidence)_.+\.tsv|memtax[0-9]+\.txt)\z/;
    });
    my$cache='sequences/Consensus/.cache';my$src=source_path($base,$cache,$live);
    if(node($src) eq 'A'){$emit->($cache,'cache')}
    else{walk($src,$cache,sub {my($p,$rel,$t)=@_;fail('protected cache namespace') if round_control(basename($p));$emit->($rel,'cache')})}
    for my$g(keys %$groups){$groups->{$g}[3]=$count{$g};if($groups->{$g}[2] eq 'unit'){
        fail("partial group $g") if $types{$g}{A}&&keys(%{$types{$g}})>1;
        $groups->{$g}[2]=$types{$g}{A}?'all-absent':'all-present';
    }}
    return ($groups,sorted_file($s));
}
sub record_write {
    my($m,$groups,$entries,$dest)=@_;ancestors($dest);sysopen(my$f,$dest,O_WRONLY|O_CREAT|O_EXCL,0600) or fail("create candidate $dest: $!");binmode($f);my$sha=Digest::SHA->new(256);my$n=0;
    my$emit=sub{my$l=shift;print {$f} $l or fail('candidate write');$sha->add($l)};
    $emit->($HEADER);$emit->("state\t$m->{state}\n");$emit->(join("\t",'boundary',hexstr($m->{barcode}),hexstr($m->{round}),$m->{token},'full_round')."\n");$emit->("context\t".hexstr($m->{context})."\n");
    $emit->("barcode\t".hexstr($_)."\n") for @{$m->{barcodes}};my$i=0;$emit->("target\t".(++$i)."\t".hexstr($_)."\n") for @{$m->{targets}};
    for my$g(sort keys %$groups){$emit->(join("\t",'group',$g,@{$groups->{$g}})."\n")}
    my$r=reader($entries);while(my$l=line($r)){chomp($l);my($h,$g,@rest)=split /\t/,$l,-1;$emit->(join("\t",'entry',$g,$h,@rest)."\n");++$n}close($r->{fh});
    print {$f} join("\t",'#END',scalar(keys %$groups),$n,$sha->hexdigest)."\n" or fail('candidate footer');close($f) or fail('candidate close');
}
sub check_capture {
    my($candidate,$root,$work,$sealed)=@_;my$m=parse($candidate,$work,sub{my($meta,$p,$t,$size,$sum)=@_;my$src="$root/$p";ancestors($src) if node(dirname($src)) ne 'A';fail("inventory type differs $src") unless node($src) eq $t;if($t eq 'F'){my($n,$h)=digest($src);fail("captured bytes differ $src") unless $n==$size&&$h eq $sum}});
    my($g,$e)=census($m,$root,0,$work);my($fh,$p)=tempfile('joint-proof-XXXXXXXX',DIR=>$work,UNLINK=>1);close($fh);unlink($p) or fail('proof reservation');record_write($m,$g,$e,$p);my(undef,$hash)=digest($p);fail('extra/missing governed content') unless $hash eq $m->{sha};unlink($e) or fail('owned enumeration cleanup');
    group_checks($m,$root,0,$work);
    if(node("$root/done_pod5.txt") ne 'A'){my($n,$h)=digest("$root/done_pod5.txt");my($n2,$h2)=digest("$root/state_authority/done_pod5.txt");fail('stale completion-ledger mismatch') unless $n==$n2&&$h eq $h2}
    if($sealed){my(undef,$h)=digest("$root/state_authority/AUTHORITY");fail('sealed record differs') unless $h eq $m->{sha}}
    return $m;
}
sub ensure_dir {
    my($p)=@_;my$t=node($p);return if $t eq 'D';fail("unsafe destination directory $p") unless $t eq 'A';ensure_dir(dirname($p));mkdir($p,0700) or fail("mkdir $p: $!");
}
sub remove_safe {
    my($p)=@_;my$t=node($p);return if $t eq 'A';ancestors($p);if($t eq 'D'){scan_names($p,sub{remove_safe("$p/$_[0]")});rmdir($p) or fail("rmdir $p: $!")}else{unlink($p) or fail("unlink $p: $!")}
}
sub copy_exact {
    my($src,$dest,$size,$sum,$tag)=@_;ensure_dir(dirname($dest));my$t=node($dest);fail("type conflict $dest") if $t ne 'A'&&$t ne 'F';
    if($t eq 'F'){my($n,$h)=digest($dest);return 0 if $n==$size&&$h eq $sum}
    my$in=open_regular($src);my$tmp=basename($dest) eq 'AUTHORITY'?dirname($dest)."/.AUTHORITY.tmp.$tag":"$dest.tmp.$tag";sysopen(my$out,$tmp,O_WRONLY|O_CREAT|O_EXCL,0600) or fail("create $tmp: $!");binmode($out);my$sha=Digest::SHA->new(256);my$n=0;
    while(1){my$k=sysread($in,my$b,$CHUNK);fail('copy read') unless defined $k;last if !$k;$n+=$k;$sha->add($b);my$off=0;while($off<$k){my$w=syswrite($out,$b,$k-$off,$off);fail('copy write') unless defined($w)&&$w>0;$off+=$w}}
    my @source_stat=stat($in);close($in) or fail('copy source close');close($out) or fail('copy destination close');chmod($source_stat[2]&07777,$tmp) or fail('incidental copy mode');fail("source changed during capture $src") unless $n==$size&&$sha->hexdigest eq $sum;rename($tmp,$dest) or fail("rename $tmp: $!");return $n;
}
# Reconciliation compares canonical paths, including destination-only paths.
# Deletion list is reverse sorted, so a directory is removed after its children.
sub transfer {
    my($candidate,$source,$dest,$live,$install,$work)=@_;my$m=parse($candidate,$work,undef);my($g,$wanted)=census($m,$source,$live,$work);
    my(undef,$expected)=digest($candidate);my($fh,$projection)=tempfile('joint-source-XXXXXXXX',DIR=>$work,UNLINK=>1);close($fh);unlink($projection);record_write($m,$g,$wanted,$projection);my(undef,$actual)=digest($projection);fail('source no longer matches candidate') unless $actual eq $expected;
    # Restore starts after its existing applying/wipe. Snapshot capture permits
    # reconciliation of old governed names, never control evidence.
    my$flat=$install?"$dest/_state":"$dest/state_authority";ensure_dir($flat);
    my%flat;my%cache;
    parse($candidate,$work,sub{my(undef,$p,$t)=@_;$flat{basename($p)}=$t if index($p,'state_authority/')==0});
    scan_names($flat,sub{my$n=shift;return if !$install&&($n eq '.lock'||$n eq 'AUTHORITY');return if exists($flat{$n});if(broad_governed($n)){remove_safe("$flat/$n")}elsif(!$install){fail("unknown authority residue $flat/$n")}});
    # Reconcile the invalidated canonical cache directly. A bounded merge of
    # names/types removes destination-only members; unchanged files are retained
    # only after exact content comparison. There is never a cache union.
    my$cons=$install?"$dest/Consensus":"$dest/sequences/Consensus";ensure_dir($cons);
    my$actual_paths=sorter($work);my$deletions=sorter($work);
    if(node("$cons/.cache") ne 'A'){
        walk("$cons/.cache",'sequences/Consensus/.cache',sub{
            my(undef,$p,$type)=@_;add_sort($actual_paths,hexstr($p)."\t$type\n");
        });
    }
    my$actual_file=sorted_file($actual_paths);my$ar=reader($actual_file);my$wr=reader($wanted);my$w=line($wr);
    while(my$a=line($ar)){
        chomp($a);my($h,$type)=split /\t/,$a;
        while(defined($w)&&(split /\t/,$w,2)[0] lt $h){$w=line($wr)}
        my@wanted=defined($w)?split(/\t/,$w):();
        if(!@wanted||$wanted[0] ne $h||$wanted[2] ne $type){
            my$p=unhex($h);my$reverse=hexstr(~$p).'ff';add_sort($deletions,"$reverse\t$h\n");
        }
    }
    close($ar->{fh});close($wr->{fh});unlink($actual_file) or fail('owned inventory cleanup');
    my$delete_file=sorted_file($deletions);my$dr=reader($delete_file);
    while(my$l=line($dr)){chomp($l);my(undef,$h)=split /\t/,$l;remove_safe(source_path($dest,unhex($h),$install))}
    close($dr->{fh});unlink($delete_file) or fail('owned deletion-list cleanup');
    my$written=0;
    parse($candidate,$work,sub{my(undef,$p,$t,$size,$sum)=@_;my$dst=source_path($dest,$p,$install);
        if($t eq 'A'){remove_safe($dst)}
        elsif($t eq 'D'){ensure_dir($dst)}
        else{$written+=copy_exact(source_path($source,$p,$live),$dst,$size,$sum,$m->{sha})}
    });
    unlink($wanted) or fail('owned census cleanup');return $written;
}
sub marker_text {my($m)=@_;return "#RTB-F03-TRANSACTION\t1\nstate\t$m->{state}\nboundary\t".hexstr($m->{round})."\t$m->{token}\tfull_round\nauthority_sha256\t$m->{sha}\n"}
sub marker_read {
    my($p)=@_;my$r=reader($p);my@l;push @l,line($r) for 1..4;fail('marker trailing bytes') if defined(line($r));close($r->{fh});fail('malformed transaction marker') if grep{!defined($_)}@l;
    fail('malformed transaction marker header') unless $l[0] eq "#RTB-F03-TRANSACTION\t1\n";
    my($s)=$l[1]=~/\Astate\t([A-Za-z0-9_.-]+)\n\z/;my($rh,$g)=$l[2]=~/\Aboundary\t([0-9a-f]+)\t([0-9a-f]{64})\tfull_round\n\z/;my($hash)=$l[3]=~/\Aauthority_sha256\t([0-9a-f]{64})\n\z/;
    fail('malformed transaction marker identity') unless defined($s)&&$s ne '.'&&$s ne '..'&&defined($g)&&defined($hash);my$round=unhex($rh);safe_path($round);fail('marker round component') if $round=~m{[/\r\n\t]};
    return {state=>$s,round=>$round,token=>$g,sha=>$hash,bytes=>join('',@l)};
}
sub write_atomic {
    my($p,$bytes,$tag)=@_;ensure_dir(dirname($p));my$tmp="$p.tmp.$tag";fail("unknown temporary $tmp") unless node($tmp) eq 'A';sysopen(my$f,$tmp,O_WRONLY|O_CREAT|O_EXCL,0600) or fail("create $tmp");binmode($f);print {$f} $bytes or fail('atomic write');close($f) or fail('atomic close');my(undef,$h)=digest($tmp);fail('atomic readback') unless $h eq sha256_hex($bytes);rename($tmp,$p) or fail('atomic rename');
}
sub assert_marker {my($m,$root)=@_;my$x=marker_read("$root/.f03-transaction");fail('transaction identity changed') unless $x->{bytes} eq marker_text($m)}
sub root_lock {
    my($root,$write)=@_;if($write){ensure_dir("$root/state_authority")};my$p="$root/state_authority/.lock";my$t=node($p);if(!$write&&$t eq 'A'){fail('sealed root lacks publication lock') if node("$root/state_authority/AUTHORITY") ne 'A';return undef;}fail('unsafe snapshot lock') if $t ne 'A'&&$t ne 'F';ancestors($p);sysopen(my$f,$p,$write?O_RDWR|O_CREAT|O_NOFOLLOW:O_RDONLY|O_NOFOLLOW,0600) or fail('root lock open');flock($f,$write?LOCK_EX:LOCK_SH) or fail('root lock');my$flags=fcntl($f,F_GETFD,0);fail('get root descriptor') unless defined($flags);fcntl($f,F_SETFD,$flags&~FD_CLOEXEC) or fail('inherit root descriptor');return $f;
}
sub guard {
    my($m,$live,$pin,$role)=@_;pending_absent("$live/_state");
    fail('full-round context mismatch') unless ($ENV{RTBIOSCAN_ROUND_LOCK_SCOPE}//'') eq 'full_round'&&($ENV{RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED}//0)!=1&&($ENV{RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN}//'') eq $m->{token}&&($ENV{RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE}//'') eq $m->{round}&&($ENV{RTBIOSCAN_ROUND_LOCK_STATE_DIR}//'') eq "$live/_state";
    system($^X,"$FindBin::Bin/round_lock_generation.pl",'guard-pin','--state-dir',"$live/_state",'--round-barcode',$m->{round},'--scope','full_round','--token',$m->{token},'--pin-token',$pin,'--role',$role)==0 or fail('generation/pin authentication');
}
sub prepare {
    my($live,$sid,$bc,$round,$token,$pin,$context,$targets,$candidate,@roots)=@_;
    fail('prepare interface') unless defined($candidate)&&@roots==2;
    my$m={state=>$sid,barcode=>$bc,round=>$round,token=>$token,context=>$context,targets=>[split /\|/,$targets,-1]};guard($m,$live,$pin,'backup_update_and_clean');
    my@old;for my$r(@roots){next if node("$r/state_authority/AUTHORITY") eq 'A';my$x=reader("$r/state_authority/AUTHORITY");my$h=line($x);close($x->{fh});if($h eq $HEADER){my$v=parse("$r/state_authority/AUTHORITY",dirname($candidate),undef);fail('prior state mismatch') unless $v->{state} eq $sid;push @old,@{$v->{barcodes}}}}
    $m->{barcodes}=roster($live,$bc,@old);group_checks($m,$live,1,dirname($candidate));
    presentation_set($m,$roots[1],$live);
    my($g,$e)=census($m,$live,1,dirname($candidate));record_write($m,$g,$e,$candidate);unlink($e) or fail('owned enumeration cleanup');my$checked=parse($candidate,dirname($candidate),undef);guard($checked,$live,$pin,'backup_update_and_clean');return 0;
}
sub seal {
    my($candidate,$t1,$t2)=@_;my$work=dirname($candidate);my$m=parse($candidate,$work,undef);assert_marker($m,$_) for($t1,$t2);
    check_capture($candidate,$t1,$work,0);transfer($candidate,$t1,$t2,0,0,$work);
    if(node("$t2/done_pod5.txt") ne 'A'){my($n,$h)=digest("$t1/state_authority/done_pod5.txt");copy_exact("$t1/state_authority/done_pod5.txt","$t2/done_pod5.txt",$n,$h,$m->{sha})}
    root_scan($t1,$m,0,1);root_scan($t2,$m,1,1);
    check_capture($candidate,$_, $work,0) for($t1,$t2);
    for my$r($t1,$t2){assert_marker($m,$r);my($n,$h)=digest($candidate);copy_exact($candidate,"$r/state_authority/AUTHORITY",$n,$h,$m->{sha})}
    check_capture($candidate,$_, $work,1) for($t1,$t2);assert_marker($m,$_) for($t1,$t2);
    for my$r($t1,$t2){unlink("$r/.f03-transaction") or fail("marker cleanup $r")}
    return 0;
}
sub transaction {
    my($candidate,$t1,$t2,$live,$pin,@argv)=@_;fail('transaction command missing') unless @argv;
    my$m=parse($candidate,dirname($candidate),undef);my@locks=map{root_lock($_,1)}($t1,$t2);guard($m,$live,$pin,'backup_update_and_clean');
    for my$r($t1,$t2){if(node("$r/.f03-transaction") ne 'A'){my$x=marker_read("$r/.f03-transaction");fail('foreign-state transaction') unless $x->{state} eq $m->{state}}root_scan($r,$m,$r eq $t2,1)}
    for my$r($t1,$t2){write_atomic("$r/.f03-transaction",marker_text($m),$m->{sha})}assert_marker($m,$_) for($t1,$t2);
    for my$r($t1,$t2){unlink("$r/state_authority/AUTHORITY") or $!{ENOENT} or fail('authority invalidation')}
    $ENV{RTB_JOINT_ROOT_FDS}=join(',',map{fileno($_)}@locks);system(@argv);my$status=$?;fail('supervised transaction shell failed') if $status!=0;return 0;
}
my %CHART = map {$_=>1} qw(reads_fate_per_round.tsv otu_fate_per_round.tsv otu_active_by_marker_per_round.tsv consensus_emitted_by_marker_per_round.tsv otu_assignments_species.tsv otu_assignments_genus.tsv otu_assignments_family.tsv consensus_assignments_species.tsv consensus_assignments_genus.tsv consensus_assignments_family.tsv frozen_otu_assignments_species.tsv consolidated_consensus_assignments_species.tsv otu_assignments_by_sample_species.tsv consensus_assignments_by_sample_species.tsv);
sub safe_sample {
    my($s)=@_;$s='' unless defined $s;$s=~s/^\s+|\s+$//g;$s=~s/[^A-Za-z0-9._-]+/_/g;$s='sample' if $s eq '';
    fail('unsafe generated sample component') if $s eq '.'||$s eq '..';return $s;
}
sub presentation_history {
    my($path)=@_;return [] if node($path) eq 'A';my$r=reader($path);my@rows;
    require JSON::PP;
    while(my$l=line($r)){my$x=eval{JSON::PP::decode_json($l)};fail("invalid presentation history $path") if $@||ref($x) ne 'HASH';push@rows,$x}close($r->{fh});return \@rows;
}
sub history_destinations {
    my($rows,$allowed,$root)=@_;
    for my$row(@$rows){my@fig=ref($row->{figures}) eq 'ARRAY'?@{$row->{figures}}:();
        if(ref($row->{sample_metrics}) eq 'HASH'){for my$x(values%{$row->{sample_metrics}}){push@fig,@{$x->{figures}} if ref($x) eq 'HASH'&&ref($x->{figures}) eq 'ARRAY'}}
        for my$f(@fig){next unless ref($f) eq 'HASH'&&defined($f->{path})&&!ref($f->{path})&&$f->{path}=~/\.png\z/i;
            my$dest=$f->{pdf_path};if(!defined($dest)||ref($dest)||$dest eq ''){$dest=$f->{path};$dest=~s/\.png\z/.pdf/i}
            next if $dest=~m{\A(?:[A-Za-z][A-Za-z0-9+.-]*:|/)}; # renderer does not resolve these
            safe_path($dest); # notably reject traversal before resolving any aliases
            my$rel=$dest;$rel=~s{\Aruns/[^/]+/}{};
            if($rel=~s{\Areport_assets/(embedded/|samples/)}{plots/pdf/$1}){
                fail("unknown history PDF destination $dest") unless $allowed->{$rel};
                ancestors("$root/$rel") if node(dirname("$root/$rel")) ne 'A';
            } elsif($rel=~m{\A(?:plots/pdf/|tables/)}){
                fail("unknown history PDF destination $dest") unless $allowed->{$rel};
            } elsif($rel=~m{\Areport_assets(?:/|\z)}){
                # Existing per-round asset links are not among async's promoted
                # embedded/sample families and cannot become joint sources.
                next;
            } else {fail("unknown history PDF destination $dest")}
        }
    }
}
sub presentation_set {
    my($m,$root,$live)=@_;$m//={targets=>[],barcodes=>[]};my%p;
    for my$n(keys%CHART){$p{"tables/$n"}=1;$p{"tables/to_figures/embedded/$n"}=1}
    for(qw(run_reads_fate.pdf run_otu_fate.pdf run_informative_otu.pdf run_consensus_emitted.pdf run_demultiplex_reads_by_marker.pdf)){$p{"plots/pdf/embedded/$_"}=1}
    for my$t(@{$m->{targets}}){my$slug=lc($t);$slug=~s/[^a-z0-9._-]+/_/g;$slug=~s/_+/_/g;$slug=~s/\A_+|_+\z//g;my$suffix=$t eq 'COI'||$t eq 'ITS2'?$t:$slug;for(qw(otu consensus frozen_otu consolidated_consensus)){$p{"plots/pdf/embedded/run_${_}_sunburst_$suffix.pdf"}=1}}
    my%samples;my@history;
    for my$path("$root/tables/report_history.jsonl","$root/report_history.jsonl",defined($live)?"$live/_state/report_history.jsonl":()){
        push@history,@{presentation_history($path)} if node(dirname($path)) ne 'A';
    }
    require "$FindBin::Bin/lib/sample_label.pl";
    local $ENV{RTBIOSCAN_TARGET_TOKENS}=join('|',@{$m->{targets}});
    for my$row(@history){
        if(ref($row->{sample_metrics}) eq 'HASH'){$samples{safe_sample($_)}=1 for keys%{$row->{sample_metrics}}}
        if(($row->{identity_mode}//'') eq 'track'&&ref($row->{track_unit_metrics}) eq 'HASH'){
            for my$x(values%{$row->{track_unit_metrics}}){next unless ref($x) eq 'HASH';my$label=$x->{track_sample_label};
                if(!defined($label)||$label eq ''){for my$k(qw(track_unit_id sample label track_replicate_id track_replicate_label)){my$v=$x->{$k}//'';if($v=~/\A(.+)_([0-9]+)_([^_]+)(?:_([A-Za-z0-9]+))?\z/){$label=$1;last}}}
                $samples{safe_sample($label)}=1 if defined($label)&&$label ne '';
            }
        }
    }
    for my$b(@{$m->{barcodes}}){my$path=defined($live)?"$live/_state/${b}_demult_rpt.txt":"$root/state_authority/${b}_demult_rpt.txt";next if node($path) eq 'A';my$r=reader($path);my$head=line($r);next unless defined($head);chomp($head);my@h=split /\t/,$head,-1;my($idx)=grep{$h[$_] eq 'sample'}0..$#h;fail('parser sample column absent') unless defined $idx;
        while(my$l=line($r)){chomp($l);my@v=split /\t/,$l,-1;next if @v<=$idx;my$raw=$v[$idx];my$label=SampleLabel::normalize_sample_base($raw);
            if(($m->{context}//'') eq 'track'){$label=SampleLabel::normalize_sample_label($raw);my$pattern=SampleLabel::configured_marker_suffix_pattern();$label=~s/${pattern}$//i if $pattern ne '';$label=~s/_(?:COI|ITS)\d*$//i}
            $samples{safe_sample(SampleLabel::stable_sample_id_from_label($label))}=1;
            if(($m->{context}//'') eq 'track'&&$label=~/\A(.+)_([0-9]+)_([^_]+)\z/){$samples{safe_sample($1)}=1}}close($r->{fh});
    }
    for my$s(keys%samples){$p{"plots/pdf/embedded/samples/${s}_reads_per_barcode.pdf"}=1;for my$type(qw(otu consensus)){for my$rank(qw(species genus family)){$p{"plots/pdf/embedded/samples/${s}_${type}_treemap_$rank.pdf"}=1}}
        for(qw(reads_time_history reads_cumulative_history otu_tax_time_history consensus_tax_time_history)){$p{"plots/pdf/samples/$s/${s}_$_.pdf"}=1}
        for my$t(@{$m->{targets}}){my$slug=lc($t);$slug=~s/[^a-z0-9._-]+/_/g;$slug=~s/_+/_/g;$slug=~s/\A_+|_+\z//g;for my$kind(qw(otu consensus)){for(qw(icicle sunburst)){$p{"plots/pdf/samples/$s/${s}_${kind}_${slug}_$_.pdf"}=1}}}
    }
    history_destinations(\@history,\%p,$root);
    return \%p;
}
sub figure_alias {
    my($root,$rel)=@_;return 0 unless $rel=~m{\Atables/to_figures/([^/]+)\z};my$n=$1;
    return 0 unless $n=~/\A[A-Za-z0-9_][A-Za-z0-9._-]*_(?:reads_time_rpt\.txt|reads_cumulative_rpt\.txt|read_info_rpt\.txt\.gz|summary_demult_rpt\.txt|otu_tax_time_rpt\.txt|otu_frozen_tax_time_rpt\.txt|consensus_tax_time_rpt\.txt|consensus_consolidated_tax_time_rpt\.txt|(?:otu|consensus|consensus_consolidated)_tax_(?:spc|gns)_[A-Za-z0-9_.-]+_treemap_rpt\.txt)\z/;
    return 0 unless -l "$root/$rel";my$target=readlink("$root/$rel");return 0 unless defined($target)&&$target eq "$root/tables/$n";ancestors($target);return node($target) eq 'F';
}
# A transported presentation payload is never a source, but its physical
# directories cannot hide publication or round-control residue. Do not follow
# its established presentation aliases while checking reserved basenames.
sub presentation_control_census {
    my($path)=@_;my@st=lstat($path);fail("presentation lstat $path") unless @st;
    my$n=basename($path);
    fail("JOINT_TRANSACTION_INCOMPLETE: $path") if control($n)||round_control($n);
    if(S_ISDIR($st[2])){scan_names($path,sub{presentation_control_census("$path/$_[0]")})}
    elsif(S_ISLNK($st[2])){
        my$target=readlink($path);my$expected;my$physical;
        if($path=~m{\A(.+)/live_round\z}){
            my$root=$1;fail("unsafe presentation alias $path") unless defined($target)&&$target=~m{\A\.live_round_payloads/([^/]+)\z}&&safe_component($1);
            $physical="$root/$target";ancestors($physical);fail("unsafe presentation alias $path") unless node($physical) eq 'D';return;
        }
        if($path=~m{\A(.+/\.live_round_payloads/[^/]+)/report_assets/([^/]+\.(png|pdf))\z}){
            $expected="../plots/$3/$2";$physical="$1/plots/$3/$2";
        } elsif($path=~m{\A(.+/\.live_round_payloads/[^/]+)/report_assets/samples/([^/]+)/([^/]+\.(png|pdf))\z}){
            $expected="../../../plots/$4/samples/$2/$3";$physical="$1/plots/$4/samples/$2/$3";
        }
        fail("unsafe presentation alias $path") unless defined($expected)&&defined($target)&&$target eq $expected;
        ancestors($physical);fail("unsafe presentation alias $path") unless node($physical) eq 'F';
    } elsif(!S_ISREG($st[2])){fail("unsafe presentation node $path")}
}
sub root_scan {
    my($root,$m,$t2,$allow_marker)=@_;my$p=$t2?presentation_set($m,$root):{};my%presentation_dirs;
    for my$leaf(keys%$p){my$d=dirname($leaf);while($d ne '.'){$presentation_dirs{$d}=1;$d=dirname($d)}}
    my$walk;$walk=sub{my($path,$rel)=@_;my$n=basename($path);my@st=lstat($path);return if !@st&&$!{ENOENT};fail("root lstat $path") unless @st;
        # Classify control/protected names before leaf presentation exemptions.
        my$in_cache=index($rel,'sequences/Consensus/.cache/')==0;
        fail("protected round-lock namespace in restore snapshot state: $path") if round_control($n);
        fail("JOINT_TRANSACTION_INCOMPLETE: $path") if !$in_cache&&control($n)&&!($allow_marker&&$rel eq '.f03-transaction'&&S_ISREG($st[2]));
        if($p->{$rel}){fail("non-leaf presentation destination $path") unless S_ISREG($st[2])||S_ISLNK($st[2]);}
        return if $p->{$rel};return if figure_alias($root,$rel);
        fail("unsafe presentation alias $path") if S_ISLNK($st[2])&&$rel=~m{\Atables/to_figures/};
        if($t2&&($rel eq 'live_round'||$rel eq '.live_round_payloads')&&(node("$root/tables") eq 'D'||node("$root/plots") eq 'D')){presentation_control_census($path);return}
        fail("unsafe symlink in restore snapshot state: $path") if S_ISLNK($st[2]);
        fail("unsafe snapshot node $path") unless S_ISREG($st[2])||S_ISDIR($st[2]);
        if($t2&&S_ISDIR($st[2])&&$rel=~m{\A(?:plots/pdf/|tables/to_figures/embedded/)}){fail("unknown async directory $path") unless $presentation_dirs{$rel}}
        if($t2&&S_ISREG($st[2])&&($rel=~m{\Atables/(?:to_figures/embedded/)?[^/]+\.tsv\z}||$rel=~m{\Aplots/pdf/})){fail("unknown async destination $path") if $rel=~m{\A(?:plots/pdf/|tables/to_figures/embedded/)};}
        if(S_ISDIR($st[2])){scan_names($path,sub{$walk->("$path/$_[0]",length($rel)?"$rel/$_[0]":$_[0])})}
    };$walk->($root,'');return $p;
}
sub completed_residue {
    my($root)=@_;return 1 if node("$root/state_authority") ne 'A';
    for my$d($root,"$root/tables","$root/sequences"){my$found=0;scan_names($d,sub{my$n=shift;$found=1 if ($n eq 'done_pod5.txt' ? (-s "$d/$n") : f01($n))||$n=~/\A(?:otu_blast_(?:cache|evidence)_.+\.tsv|memtax[0-9]+\.txt)\z/});return 1 if $found}
    return 0;
}
sub select_roots {
    my($sid,$live,$t1,$t2,$work)=@_;pending_absent("$live/_state");my(@state,@records);
    for my$i(0,1){my$r=($t1,$t2)[$i];if(node($r) eq 'A'){push@state,'absent';push@records,undef;next}
        fail('snapshot root not directory') unless node($r) eq 'D';
        my$m;if(node("$r/state_authority/AUTHORITY") ne 'A'){$m=parse("$r/state_authority/AUTHORITY",$work,undef);fail('requested state differs') unless $m->{state} eq $sid}
        root_scan($r,$m,$i,0);
        if(!$m){fail('JOINT_V2_REQUIRED: completed snapshot lacks joint-v2 restore authority') if completed_residue($r);push@state,'pristine';push@records,undef;next}
        check_capture("$r/state_authority/AUTHORITY",$r,$work,1);push@state,'valid';push@records,$m;
    }
    if($state[0] eq 'valid'&&$state[1] eq 'valid'){fail('JOINT_ROOTS_DIFFER') unless $records[0]{sha} eq $records[1]{sha};return 1}
    for my$i(0,1){if($state[$i] eq 'valid'){fail('JOINT_PRESENT_PEER_INVALID') unless $state[1-$i] eq 'absent';return $i+1}}
    return 0;
}
sub broad_governed {
    my($n)=@_;$n=~s/\.gz\z// if $n=~/\.(?:txt|tsv|csv)\.gz\z/;
    return 1 if f01($n)||control($n);return 1 if $n=~/\A(?:otu_blast_(?:cache|evidence)_.+\.tsv|memtax[0-9]+\.txt|blastreport_sup_annotated_pre\.fastq(?:\.gz)?)\z/;
    for my$s(@PARSER,@FATE,@GRACE){return 1 if length($n)>length($s)+1&&substr($n,-length($s)-1) eq "_$s"}
    return 0;
}
sub compat_copy {
    my($src,$dst,$scientific_flat,$consensus)=@_;my$n=basename($src);return if control($n)||round_control($n);return if $scientific_flat&&broad_governed($n);return if $consensus&&($n eq '.cache'||$n eq 'consensus_ownership.tsv'||$n eq 'consolidated_consensus_ids.txt');
    my$t=node($src);return if $t eq 'A';if($t eq 'D'){ensure_dir($dst);scan_names($src,sub{compat_copy("$src/$_[0]","$dst/$_[0]",$scientific_flat,$n eq 'Consensus')})}else{my($size,$h)=digest($src);copy_exact($src,$dst,$size,$h,$h)}
}
sub overlay_copy {
    my($root,$src,$dest)=@_;my$m;
    if(node("$root/state_authority/AUTHORITY") ne 'A'){
        my$work=tempdir('rtb-overlay-XXXXXXXX',DIR=>abs_path(File::Spec->tmpdir),CLEANUP=>1);
        $m=parse("$root/state_authority/AUTHORITY",$work,undef);
    }
    my$presentation=presentation_set($m,$root);my$copy;
    $copy=sub{my($from,$to)=@_;fail('overlay outside selected root') unless index($from,"$root/")==0;
        my$rel=substr($from,length($root)+1);my$n=basename($from);
        return if $presentation->{$rel}||figure_alias($root,$rel)||broad_governed($n);
        return if $rel=~m{(?:\A|/)Consensus/(?:\.cache(?:/|\z)|consensus_ownership\.tsv\z|consolidated_consensus_ids\.txt\z)};
        my$t=node($from);return if $t eq 'A';
        if($t eq 'D'){ensure_dir($to);scan_names($from,sub{$copy->("$from/$_[0]","$to/$_[0]")})}
        else{my($size,$hash)=digest($from);copy_exact($from,$to,$size,$hash,$hash)}
    };$copy->($src,$dest);return 0;
}
sub install {
    my($candidate,$root,$live,$work)=@_;my$m=check_capture($candidate,$root,$work,1);transfer($candidate,$root,$live,0,1,$work);
    parse($candidate,$work,sub{my(undef,$p,$t,$n,$h)=@_;my$actual=source_path($live,$p,1);fail('restored inventory type differs') unless node($actual) eq $t;if($t eq 'F'){my($size,$sum)=digest($actual);fail('restored bytes differ') unless $size==$n&&$sum eq $h}});return 0;
}
1;

sub emit_worker {
    my($dir,$wait,$ttl)=@_;integer($wait);integer($ttl);fail('worker control directory already exists') unless node($dir) eq 'A';ensure_dir($dir);
    my$shell=<<'RTB_WORKER_SHELL';
#!/bin/bash
set -euo pipefail
[ "$#" -eq 19 ] && [ "$1" = 2 ] || exit 64
worker_args=( "$@" )
STATE_ROOT="$2"
STATE_ID="$3"
BARCODE_HEX="$4"
ROUND_HEX="$5"
GENERATION="$6"
PARENT_PID="$7"
PARENT_PIN="$8"
PIN_FILE="$9"
GUARD="${10}"
GENERATION_HELPER="${11}"
AUTHORITY_HELPER="${12}"
REPAIR_HELPER="${13}"
DRIVER="${14}"
CONTROL="${19}"
export LC_ALL=C LANG=C LC_CTYPE=C
unset BASH_ENV ENV
unset RTBIOSCAN_ROUND_LOCK_PIN_TOKEN RTBIOSCAN_ROUND_LOCK_PIN_ROLE RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED
export RTBIOSCAN_ROUND_LOCK_STATE_DIR="$STATE_ROOT/_state"
export RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE
RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE="$(perl -e 'die unless $ARGV[0] =~ /\A(?:[0-9a-f]{2})+\z/; print pack("H*",$ARGV[0]);' "$ROUND_HEX")"
export RTBIOSCAN_ROUND_LOCK_SCOPE=full_round
export RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN="$GENERATION"
export RTBIOSCAN_ROUND_LOCK_HELPER="$GENERATION_HELPER"
export RTBIOSCAN_ROUND_LOCK_PIN_TOKEN_FILE="$PIN_FILE"
NATIVE_BEFORE="$(ps -p "$$" -o lstart=)"
[ -n "$NATIVE_BEFORE" ] || exit 65
source "$GUARD"
rtbioscan_round_lock_pin backup_read_fate_normalize
[ "${RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED:-0}" -eq 0 ] || exit 65
NATIVE_AFTER="$(ps -p "$$" -o lstart=)"
[ "$NATIVE_BEFORE" = "$NATIVE_AFTER" ] || exit 65
# Strip ps alignment whitespace consistently with the Python identity reader.
NATIVE_BEFORE="$(printf '%s' "$NATIVE_BEFORE" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
NATIVE_AFTER="$NATIVE_BEFORE"
python3 "$DRIVER" bootstrap "${worker_args[@]}" "$$" "$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN" "$NATIVE_BEFORE" "$NATIVE_AFTER"
waited=0
while [ ! -f "$CONTROL/ack" ]; do
    kill -0 "$PARENT_PID" 2>/dev/null || exit 65
    [ "$waited" -lt "$LOCK_WAIT" ] || exit 65
    sleep 1
    waited=$((waited + 1))
done
REPORT_HISTORY_LOCK="$STATE_ROOT/_state/.report_history.lock"
REPORT_LOCK_HOST="$(hostname 2>/dev/null || uname -n)"
REPORT_STALE_LOCK_DIR="${REPORT_HISTORY_LOCK}.lockdir"
source "${GUARD%/*}/lib/stale_lock_utils.sh"
remove_report_lock_if_stale() {
    rm -f "$REPORT_STALE_LOCK_DIR/meta.env" || return 1
    rmdir "$REPORT_STALE_LOCK_DIR" || return 1
}
waited=0
while ! mkdir "$REPORT_STALE_LOCK_DIR" 2>/dev/null; do
    reclaim_status=0
    stale_lock_maybe_reclaim "$REPORT_STALE_LOCK_DIR" "$REPORT_STALE_LOCK_DIR/meta.env" \
        "$REPORT_LOCK_HOST" "$REPORT_LOCK_STALE_TTL_SECONDS" 'read-fate history lock' \
        remove_report_lock_if_stale 0 || reclaim_status=$?
    [ "$reclaim_status" -ne 2 ] && [ "$reclaim_status" -ne 11 ] || exit 74
    [ "$reclaim_status" -ne 10 ] || continue
    [ "$waited" -lt "$LOCK_WAIT" ] || exit 74
    sleep 1
    waited=$((waited + 1))
done
{
    printf 'pid=%s\n' "$$"
    printf 'host=%s\n' "$REPORT_LOCK_HOST"
    printf 'started_epoch=%s\n' "$(date +%s)"
} > "$REPORT_STALE_LOCK_DIR/meta.env"
# No trap removes H, the worker pin or pending evidence. An unsuccessful
# worker leaves the established owner metadata and durable replay obligation.
python3 "$DRIVER" prepare "$CONTROL"
exec python3 "$DRIVER" apply "$CONTROL/active-context.json"
RTB_WORKER_SHELL
    my$driver=<<'RTB_WORKER_PYTHON';
"""Task-local RF-PIN driver, generated by the joint authority coordinator."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
import time

LIMIT = 2 * 1024 * 1024 + 1024
MAX_ID = 1024 * 1024
HEADER = b"RTB-READ-FATE-NORMALIZATION-PENDING\t4\n"
RETAINED = ("read_info_rpt.txt", "on_target_rpt.txt", "demult_rpt.txt", "blast_otu_pretax_rpt.txt", "blast_unassigned_reads_round.list", "blast_report_annotated_otu_evidence.txt", "blast_otu_noadapter_rpt.txt", "round_index.tsv")
FATE = ("read_fate_demux_seen.tsv", "read_fate_blast_seen.tsv", "demux_annotation_cache.tsv")
DEMULT_HEADER = "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value"
BLAST_HEADER = "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species"

def die(msg):
    raise RuntimeError("READ_FATE_NORMALIZATION_PENDING: " + msg)

def sha(b):
    return hashlib.sha256(b).hexdigest()

def safe(path, required=True, directory=False):
    path = Path(path)
    for p in reversed(path.parents):
        st = p.lstat()
        if not stat.S_ISDIR(st.st_mode):
            die(f"unsafe ancestor {p}")
    try:
        st = path.lstat()
    except FileNotFoundError:
        if required:
            die(f"missing required retained input {path}")
        return None
    if not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)):
        die(f"unsafe path {path}")
    return (st.st_dev, st.st_ino)

def read(path):
    before = safe(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if before != (st.st_dev, st.st_ino):
            die(f"changed input {path}")
        return f.read()

def digest(path):
    before = safe(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    h = hashlib.sha256()
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if before != (st.st_dev, st.st_ino):
            die(f"changed input {path}")
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()

def exclusive(path, data):
    safe(Path(path).parent, directory=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    if read(path) != data:
        die(f"write verification failed {path}")

def component(v):
    if not isinstance(v, str) or not v or v in (".", "..") or any(c in v for c in "/\x00\r\n\t"):
        die("unsafe identity")
    return v

def hx(v):
    return component(v).encode("utf-8").hex()

def unhex(v):
    if not re.fullmatch(r"(?:[0-9a-f]{2})+", v) or len(v) > 2 * MAX_ID:
        die("noncanonical encoded identity")
    return component(bytes.fromhex(v).decode("utf-8", "strict"))

def prefix(mapping, boundary):
    if boundary not in mapping:
        die(f"unmapped boundary {boundary}")
    rows = sorted((i, rb) for rb, i in mapping.items() if i <= mapping[boundary])
    return sha(b"RTB-ROUND-PREFIX\t1\n" + b"".join(f"{i}\t{hx(rb)}\n".encode() for i, rb in rows))

def unit(state, barcode, round_name, index, pref):
    body = f"RTB-READ-FATE-UNIT\t2\nstate\t{hx(state)}\nbarcode\t{hx(barcode)}\nthrough\t{hx(round_name)}\nindex\t{index}\nprefix\t{pref}\n"
    return [hx(barcode), hx(round_name), str(index), pref, sha(body.encode())]

def strict_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                die(f"duplicate round JSON key {path}: {key}")
            result[key] = value
        return result
    try:
        return json.loads(read(path), object_pairs_hook=unique, parse_constant=lambda value: die(f"nonfinite round JSON {path}: {value}"))
    except (ValueError, UnicodeError) as error:
        die(f"invalid round JSON {path}: {error}")

def stable_digest(obj):
    return sha(json.dumps({k: v for k, v in obj.items() if k not in ("read_fate", "warnings")}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii"))

def context_fields(ctx):
    return [ctx[name].encode("utf-8").hex() for name in ("targets", "target_taxa", "assignment_level")]

def retained_path(directory, barcode, suffix):
    return directory / ("round_index.tsv" if suffix == "round_index.tsv" else f"{barcode}_{suffix}")

def pending_read(path, state, mapping=None):
    identity = safe(path)
    body = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if identity != (st.st_dev, st.st_ino):
            die("pending identity changed while opening")
        def take(hashed=True):
            line = f.readline(LIMIT + 1)
            if not line or len(line) > LIMIT or not line.endswith(b"\n") or b"\r" in line or b"\0" in line:
                die("malformed/overlong pending line")
            if hashed:
                body.update(line)
            try:
                return line.decode("ascii")
            except UnicodeError:
                die("non-ASCII pending line")
        if take().encode() != HEADER or take() != f"state\t{hx(state)}\n":
            die("unknown/wrong-state witness")
        generation = re.fullmatch(r"generation\t([0-9a-f]{64})\n", take())
        if not generation or take() != "scope\tfull_round\n":
            die("pending generation/scope")
        context = re.fullmatch(r"context\t([0-9a-f]*)\t([0-9a-f]*)\t([0-9a-f]*)\n", take())
        if not context:
            die("pending context grammar")
        try:
            values = tuple(bytes.fromhex(value).decode("utf-8", "strict") for value in context.groups())
        except (ValueError, UnicodeError):
            die("pending context encoding")
        if any(len(value) > 2 * MAX_ID for value in context.groups()):
            die("pending context overlong")
        terminal = take()
        units, identities = [], set()
        line = take()
        while line.startswith("unit\t"):
            fields = line[:-1].split("\t")
            if len(fields) != 6:
                die("pending unit grammar")
            _, bh, rh, index, pref, commitment = fields
            b, r = unhex(bh), unhex(rh)
            if not re.fullmatch(r"[1-9][0-9]{0,8}", index) or not re.fullmatch(r"[0-9a-f]{64}", pref):
                die("pending index/prefix")
            expected = unit(state, b, r, int(index), pref)
            if expected != fields[1:] or r in identities or (units and int(units[-1][2]) >= int(index)):
                die("duplicate/reordered/inconsistent pending unit")
            identities.add(r)
            units.append(expected)
            line = take()
        if not units or terminal != "terminal\t" + "\t".join(units[-1]) + "\n" or line != f"count\t{len(units)}\n":
            die("pending terminal/count")
        records, seen_rounds = [], set()
        line = take()
        while line.startswith("round\t"):
            fields = line[:-1].split("\t")
            if len(fields) != 5 or not re.fullmatch(r"[1-9][0-9]{0,8}", fields[1]) or not re.fullmatch(r"[0-9a-f]{64}", fields[4]):
                die("pending round grammar")
            index = int(fields[1])
            round_name, barcode = unhex(fields[2]), unhex(fields[3])
            if round_name in seen_rounds or (records and index <= records[-1]["index"]):
                die("duplicate/reordered pending round")
            seen_rounds.add(round_name)
            raw = line.encode("ascii")
            inputs = []
            for suffix in RETAINED:
                entry = take()
                parts = entry[:-1].split("\t")
                if len(parts) not in (4, 6) or parts[:3] != ["input", str(index), suffix]:
                    die(f"missing/reordered pending input round={round_name} suffix={suffix}")
                if len(parts) == 4:
                    if parts[3] != "A" or suffix in RETAINED[:4]:
                        die(f"invalid absent pending input round={round_name} suffix={suffix}")
                elif parts[3] != "F" or not re.fullmatch(r"(?:0|[1-9][0-9]*)", parts[4]) or not re.fullmatch(r"[0-9a-f]{64}", parts[5]):
                    die(f"invalid file pending input round={round_name} suffix={suffix}")
                inputs.append(parts[3:])
                raw += entry.encode("ascii")
            records.append({"index": index, "round": round_name, "barcode": barcode, "stable": fields[4], "inputs": inputs, "raw": raw})
            line = take()
        if not records or line != f"rounds\t{len(records)}\n":
            die("pending rounds/count")
        if records[-1]["index"] != int(units[-1][2]) or records[-1]["round"] != unhex(units[-1][1]):
            die("pending terminal round mismatch")
        if mapping is not None:
            expected_rows = sorted((index, round_name) for round_name, index in mapping.items() if index <= records[-1]["index"])
            if [(record["index"], record["round"]) for record in records] != expected_rows:
                die("pending mapped round prefix changed")
        row_position = 0
        pref_hash = hashlib.sha256(b"RTB-ROUND-PREFIX\t1\n")
        for obligation in units:
            through = unhex(obligation[1])
            while row_position < len(records) and records[row_position]["index"] <= int(obligation[2]):
                record = records[row_position]
                pref_hash.update(f"{record['index']}\t{hx(record['round'])}\n".encode())
                row_position += 1
            if not row_position or records[row_position - 1]["index"] != int(obligation[2]) or records[row_position - 1]["round"] != through or records[row_position - 1]["barcode"] != unhex(obligation[0]):
                die(f"pending obligation round mismatch {through}")
            if obligation[3] != pref_hash.hexdigest():
                die(f"pending obligation prefix mismatch {through}")
        if take(hashed=False) != f"#END\t{body.hexdigest()}\n" or f.read(1):
            die("pending footer/digest/trailing bytes")
    return {"generation": generation[1], "units": units, "context": values, "records": records}

def pending_bytes(state, generation, units, context, records):
    body = HEADER + f"state\t{hx(state)}\ngeneration\t{generation}\nscope\tfull_round\ncontext\t".encode() + "\t".join(context).encode() + b"\n"
    body += ("terminal\t" + "\t".join(units[-1]) + "\n").encode()
    body += b"".join(("unit\t" + "\t".join(u) + "\n").encode() for u in units)
    body += f"count\t{len(units)}\n".encode()
    body += b"".join(record["raw"] for record in records)
    body += f"rounds\t{len(records)}\n".encode()
    return body + f"#END\t{sha(body)}\n".encode()

def normalized_id(value):
    v = value.strip()
    if not v or v.upper() == "NA":
        return ""
    return re.split(r"[\s|]", v, maxsplit=1)[0]

# Independent producer fold: no production sidecars or repaired JSON are used.
# Keep exact first-annotation row bytes and the producer's all-new-HSP behavior.
def expected_prefix(entries, capture=False):
    demux, blast, annotation_ids = set(), set(), set()
    annotation_rows, outputs, inputs, stable_json = [], {}, {}, {}
    for directory, obj in entries:
        b = component(obj["barcode"])
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", b):
            die("unsafe biological barcode")
        stable_json[str(directory / "round_report.json")] = {k: v for k, v in obj.items() if k not in ("read_fate", "warnings")}
        info = directory / f"{b}_read_info_rpt.txt"
        allowed = None
        inputs[str(info)] = digest(info)
        rows = read(info).decode("utf-8").splitlines()
        allowed = set()
        if rows and "read_id" in rows[0].split("\t"):
            col = rows[0].split("\t").index("read_id")
            for row in rows[1:]:
                parts = row.rstrip("\r").split("\t")
                if len(parts) > col and row.strip() and row != rows[0]:
                    rid = normalized_id(parts[col])
                    if rid:
                        allowed.add(rid)
        for suffix, seen, header, dest, multi in (
            ("demult_rpt.txt", demux, DEMULT_HEADER, "read_fate_demult_first_seen.tsv", False),
            ("blast_otu_pretax_rpt.txt", blast, BLAST_HEADER, "read_fate_blast_first_seen.tsv", True),
        ):
            p = directory / f"{b}_{suffix}"
            inputs[str(p)] = digest(p)
            lines = read(p).decode("utf-8").splitlines()
            head = (lines[0].rstrip("\r") or header) if lines else header
            emitted, current = [], set()
            for row in lines[1:]:
                row = row.rstrip("\r")
                if not row.strip() or row == lines[0].rstrip("\r"):
                    continue
                rid = normalized_id(row.split("\t")[0])
                if not rid:
                    continue
                if not multi and rid not in annotation_ids:
                    annotation_ids.add(rid)
                    annotation_rows.append(row)
                if rid not in seen:
                    seen.add(rid)
                    if allowed is None or rid in allowed:
                        current.add(rid)
                        emitted.append(row)
                elif multi and rid in current:
                    emitted.append(row)
            outputs[str(directory / f"{b}_{dest}")] = (head + "\n" + "".join(r + "\n" for r in emitted)).encode()
        for suffix in ("on_target_rpt.txt", "blast_unassigned_reads_round.list", "blast_report_annotated_otu_evidence.txt") + (("blast_otu_noadapter_rpt.txt", "round_index.tsv") if capture else ()):
            p = retained_path(directory, b, suffix)
            inputs[str(p)] = digest(p) if safe(p, required=False) else None
    fate = ["".join(r + "\n" for r in sorted(ids)).encode() for ids in (demux, blast)]
    fate.append((DEMULT_HEADER + "\n" + "".join(r + "\n" for r in annotation_rows)).encode())
    return fate, outputs, inputs, stable_json

# Expectations are generated before repair, from retained producer bytes and
# the unchanged Perl reporting contracts. Never invoke the repair helper here.
def contract_command(argv):
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        die(f"bounded contract failed {argv[1]}: {result.stderr.strip()}")
    return result


def expected_reports(ctx, entries, fate, outputs, inputs, stable_json, ordinal, capture=False):
    base = Path(ctx["control"]) / f"expected-{ordinal}"
    base.mkdir(mode=0o700)
    shadow = base / "_state"
    shadow.mkdir()
    bindir = Path(ctx["authority_helper"]).parent
    reports, unassigned = {}, {}
    # Preflight requires the four producer inputs, including present-empty.
    # The remaining retained inputs keep explicit A versus F state.
    def retain(source, target):
        present = safe(source, required=False)
        if present:
            if capture and str(source) in inputs:
                data = read(source)
                observed = sha(data)
                if inputs[str(source)] != observed:
                    die(f"retained producer changed during capture {source}")
                inputs[str(source)] = observed
                exclusive(target, data)
            else:
                inputs[str(source)] = digest(source)
                exclusive(target, read(source))
        else:
            if capture and inputs.get(str(source)) is not None:
                die(f"retained producer disappeared during capture {source}")
            inputs[str(source)] = None
        return present
    for directory, original in entries:
        b = original["barcode"]
        rd = base / directory.name
        rd.mkdir()
        for suffix in ("demult_rpt.txt", "blast_otu_pretax_rpt.txt", "read_info_rpt.txt", "on_target_rpt.txt", "blast_unassigned_reads_round.list", "blast_report_annotated_otu_evidence.txt", "blast_otu_noadapter_rpt.txt"):
            retain(directory / f"{b}_{suffix}", rd / f"{b}_{suffix}")
        for suffix in ("read_fate_demult_first_seen.tsv", "read_fate_blast_first_seen.tsv"):
            exclusive(rd / f"{b}_{suffix}", outputs[str(directory / f"{b}_{suffix}")])
        dest = rd / "expected.json"
        cmd = ["perl", str(bindir / "report_round_json.pl"), "--run-id", original["run_id"], "--barcode", b, "--round-barcode", original["round_barcode"], "--schema-version", str(original.get("schema_version") or "1.6"), "--targets", ctx["targets"], "--target-taxa", ctx["target_taxa"], "--out", str(dest)]
        for option, suffix in (("--demult", "demult_rpt.txt"), ("--blast-otu", "blast_otu_pretax_rpt.txt"), ("--read-fate-demult", "read_fate_demult_first_seen.tsv"), ("--read-fate-blast", "read_fate_blast_first_seen.tsv"), ("--read-info", "read_info_rpt.txt"), ("--on-target", "on_target_rpt.txt"), ("--blast-unassigned-ids", "blast_unassigned_reads_round.list")):
            if safe(rd / f"{b}_{suffix}", required=False):
                cmd += [option, str(rd / f"{b}_{suffix}")]
        if original.get("state_id"):
            cmd += ["--state-id", original["state_id"]]
        contract_command(cmd)
        expected = dict(stable_json[str(directory / "round_report.json")])
        built = json.loads(read(dest))
        expected["read_fate"] = built["read_fate"]
        def retained_warning(w):
            return str(w).replace(str(rd) + "/", str(directory) + "/")
        def noise(w):
            return bool(re.match(r"^(?:missing_or_empty:.*(?:RTBioScan_otu_lock_summary\.tsv|otu_members_blastdiag_stats\.tsv|RTBioScan_otu_size_streak\.tsv)$|missing_or_empty_data_rows:.*consensus_round_provenance\.tsv$|otu_fate_universe_empty:strict_round$|size_streak_inputs_missing:)", str(w)))
        warnings = [w for w in original.get("warnings", []) if not noise(w)] if isinstance(original.get("warnings"), list) else []
        for w in built.get("warnings", []):
            w = retained_warning(w)
            if not noise(w) and w not in warnings:
                warnings.append(w)
        expected["warnings"] = warnings
        reports[str(directory / "round_report.json")] = expected
        evidence = rd / f"{b}_blast_report_annotated_otu_evidence.txt"
        current = rd / "unassigned.list"
        if safe(evidence, required=False) and evidence.stat().st_size:
            contract_command(["perl", str(bindir / "blast_unassigned_read_ids.pl"), str(evidence), str(current), "--min-level", ctx["assignment_level"]])
            unassigned[str(Path(ctx["state_root"]) / "_state" / f"{b}_blast_unassigned_current.list")] = read(current).hex()
        else:
            unassigned[str(Path(ctx["state_root"]) / "_state" / f"{b}_blast_unassigned_current.list")] = ""
    state = Path(ctx["state_root"]) / "_state"
    last = entries[-1][1]
    b = last["barcode"]
    for name in ("round_index.tsv", "done_pod5.txt", "run_started_utc.txt", f"{b}_read_info_rpt.txt", f"{b}_on_target_rpt.txt"):
        retain(state / name, shadow / name)
    # R4's census and legacy provenance include foreign barcode evidence and
    # all globally mapped rounds, even beyond this reporting prefix.
    r4_names = sorted(p.name for p in state.iterdir() if "_blast_otu_" in p.name or p.name.startswith(".r4d-publish-"))
    for name in r4_names:
        retain(state / name, shadow / name)
    for line in read(state / "round_index.tsv").decode().splitlines():
        rb = component(line.split("\t")[0])
        rd = base / rb
        rd.mkdir(exist_ok=True)
        for suffix in ("blast_otu_pretax_rpt.txt", "blast_otu_noadapter_rpt.txt"):
            target = rd / f"{b}_{suffix}"
            if not target.exists():
                original_dir = state.parent / rb
                if original_dir.exists():
                    retain(original_dir / target.name, target)
    for suffix, data in zip(FATE, fate):
        exclusive(shadow / f"{b}_{suffix}", data)
    for p, data in unassigned.items():
        exclusive(shadow / Path(p).name, bytes.fromhex(data))
    # All independently reconstructed scientific values and immutable fields
    # enter the bounded run-report calculation. Run aggregation uses its established per-run
    # selector, without changing that selector or its schema.
    exclusive(shadow / "report_history.jsonl", b"".join(json.dumps(v).encode() + b"\n" for v in reports.values()))
    run_dest = base / "expected-run.json"
    cmd = ["perl", str(bindir / "report_run_json.pl"), "--history", str(shadow / "report_history.jsonl"), "--out", str(run_dest), "--run-id", last["run_id"], "--barcode", b, "--state-id", str(last.get("state_id") or Path(ctx["state_root"]).name), "--schema-version", str(last.get("schema_version") or "1.6"), "--report-rel-path", f"runs/{last['run_id']}/report.html", "--outdir", ctx["outdir"]]
    if (shadow / "run_started_utc.txt").exists():
        cmd += ["--run-started-utc-file", str(shadow / "run_started_utc.txt")]
    contract_command(cmd)
    run_obj = json.loads(read(run_dest))
    r4 = None
    # A resolver is reached after marker and accumulated-input eligibility,
    # even if it resolves absence and no run-status object is produced.
    selected = [o for o in reports.values() if o.get("run_id") == last["run_id"]]
    picked = max(selected, key=lambda o: (int(re.search(r"(\d+)(?!.*\d)", o["round_barcode"])[1]), o["round_barcode"]))
    markers = picked.get("markers") or {}
    order = markers.get("order") or []
    taxa = markers.get("target_taxa_by_marker") or {}
    if order and all(taxa.get(m) for m in order) and all((shadow / f"{b}_{suffix}").exists() and (shadow / f"{b}_{suffix}").stat().st_size for suffix in ("read_info_rpt.txt", "on_target_rpt.txt", "demux_annotation_cache.tsv")):
        r4 = resolved_contract(bindir, shadow, b)
    # These records may legitimately be adopted/sealed by the unchanged R4
    # resolver. Their expected final contract is checked separately below.
    mutable_r4 = {str(state / f"{b}_blast_otu_cumulative.commit"), str(state / f"{b}_blast_otu_cumulative.lock")}
    for p in mutable_r4:
        inputs.pop(p, None)
    return {"reports": reports, "unassigned": unassigned, "run": run_obj, "run_path": str(Path(ctx["outdir"]) / "report_html/runs" / component(last["run_id"]) / "run_report.json"), "r4": r4, "r4_barcode": b, "r4_names": r4_names}


def resolved_contract(bindir, state, barcode, readonly=False):
    # The caller uses this only on a private shadow or after the expected
    # live resolver has completed. Live committed authority is required below
    # before this reader can enter an adoption/sealing branch.
    code = 'require $ARGV[0]; '
    if readonly:
        code += 'no warnings "redefine"; *RTBioScan::R4DCumulative::seal_pre_r4d = sub { die "verification would seal R4" }; *RTBioScan::R4DCumulative::adopt_backup_record = sub { die "verification would adopt R4" }; '
    code += 'RTBioScan::R4DCumulative::resolve_cli($ARGV[1],$ARGV[2],"state");'
    raw = contract_command(["perl", "-e", code, str(bindir / "lib/RTBioScan/R4DCumulative.pm"), barcode, str(state)])
    result = json.loads(raw.stdout)[0]
    if not result.get("ok"):
        die(f"R4 resolver output {state}: {result.get('error')}")
    return {"mode": result.get("mode"), "kind": result.get("kind"), "generation": result.get("generation"), "members": {k: digest(p) for k, p in result["paths"].items()}}


def verify_report_contract(ctx, expected):
    for p, obj in expected["reports"].items():
        now = json.loads(read(p))
        if json.dumps(now.get("read_fate"), sort_keys=True) != json.dumps(obj["read_fate"], sort_keys=True):
            die(f"bounded read_fate mismatch {p}")
        if json.dumps({k: v for k, v in now.items() if k not in ("read_fate", "warnings")}, sort_keys=True) != json.dumps({k: v for k, v in obj.items() if k not in ("read_fate", "warnings")}, sort_keys=True):
            die(f"immutable bounded report mismatch {p}")
        if json.dumps(now.get("warnings"), sort_keys=True) != json.dumps(obj["warnings"], sort_keys=True):
            die(f"bounded warnings mismatch {p}")
    for p, value in expected["unassigned"].items():
        if read(p) != bytes.fromhex(value):
            die(f"bounded blast-unassigned mismatch {p}")
    actual = json.loads(read(expected["run_path"]))
    # Only wall-clock freshness fields vary between independent computations.
    # Their shape is checked; every other completion/context/scientific field
    # (including all nested read-fate values) must match exactly.
    clock_fields = {"last_updated_utc", "status_age_seconds", "status_label", "status_color"}
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", actual.get("last_updated_utc", "")):
        die(f"invalid bounded run-report completion {expected['run_path']}")
    comparable = lambda obj: json.dumps({k: v for k, v in obj.items() if k not in clock_fields}, sort_keys=True)
    if comparable(actual) != comparable(expected["run"]):
        die(f"bounded run-report mismatch {expected['run_path']}")
    try:
        stamp = datetime.strptime(actual["last_updated_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        if not ctx["started_epoch"] - 1 <= stamp <= time.time() + 1:
            die(f"stale bounded run-report completion {expected['run_path']}")
        if "status_cadence_seconds" in actual:
            last = datetime.strptime(actual["last_round_timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            age = int(stamp - last)
            ratio = age / actual["status_cadence_seconds"]
            label, color = ("Fresh", "green") if ratio <= 1.5 else ("Aging", "orange") if ratio <= 3 else ("Stale", "red")
            if (actual.get("status_age_seconds"), actual.get("status_label"), actual.get("status_color")) != (age, label, color):
                die(f"bounded run-report freshness mismatch {expected['run_path']}")
        elif any(k in actual for k in ("status_age_seconds", "status_label", "status_color")):
            die(f"unexpected bounded run-report freshness {expected['run_path']}")
    except (ValueError, TypeError, ZeroDivisionError) as error:
        die(f"invalid bounded run-report clock {expected['run_path']}: {error}")
    verify_r4_contract(ctx, expected)


def verify_r4_contract(ctx, expected):
    if expected["r4"] is not None:
        state = Path(ctx["state_root"]) / "_state"
        b = expected["r4_barcode"]
        if expected["r4"]["mode"] == "committed":
            record = state / f"{b}_blast_otu_cumulative.commit"
            safe(record)
            # Do not repair a missing/adoption record during verification.
            if b"\nkind\tbackup\n" in read(record):
                die(f"R4 output still requires adoption {record}")
        result = resolved_contract(Path(ctx["authority_helper"]).parent, state, b, readonly=True)
        if result != expected["r4"]:
            die(f"bounded R4 resolver mismatch {state}/{b}")


def verify_inputs(inputs, identities=None):
    for p, expected in inputs.items():
        identity = safe(p, required=False)
        if identities is not None and (list(identity) if identity else None) != identities[p]:
            die(f"retained producer changed (identity) {p}")
        current = digest(p) if safe(p, required=False) else None
        if current != expected:
            die(f"retained producer changed {p}")

def verify_outputs(state_path, entries, fate, outputs, stable_json):
    b = entries[-1][1]["barcode"]
    hashes = {}
    for suffix, expected in zip(FATE, fate):
        p = state_path / "_state" / f"{b}_{suffix}"
        if read(p) != expected:
            die(f"independent sidecar verification failed {p}")
        hashes[str(p)] = sha(expected)
    for name, expected in outputs.items():
        if read(name) != expected:
            die(f"first-seen verification failed {name}")
    expected_history = []
    for directory, obj in entries:
        p = directory / "round_report.json"
        now = json.loads(read(p))
        if json.dumps({k: v for k, v in now.items() if k not in ("read_fate", "warnings")}, sort_keys=True) != json.dumps(stable_json[str(p)], sort_keys=True):
            die(f"stable report fields changed {p}")
        if not isinstance(now.get("read_fate"), dict) or not isinstance(now.get("warnings"), list):
            die(f"invalid repaired report {p}")
        expected_history.append(now)
    history = [json.loads(row) for row in read(state_path / "_state/report_history.jsonl").splitlines()]
    if json.dumps(history, sort_keys=True) != json.dumps(expected_history, sort_keys=True):
        die("terminal history is not the exact ordered report prefix")
    return hashes

def native_start(pid):
    p = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}, check=True)
    result = p.stdout.strip()
    if not result:
        die("native process birth identity unavailable")
    return result

def guard_pin(ctx, token, role):
    subprocess.run(["perl", ctx["generation_helper"], "guard-pin", "--state-dir", ctx["state_root"] + "/_state", "--round-barcode", ctx["round"], "--scope", "full_round", "--token", ctx["generation"], "--pin-token", token, "--role", role], check=True)

def worker_identity(ctx):
    if native_start(ctx["worker_pid"]) != ctx["native_start"]:
        die("worker native identity changed")
    guard_pin(ctx, ctx["parent_pin"], "backup_update_and_clean")
    guard_pin(ctx, ctx["worker_pin"], "backup_read_fate_normalize")
    p = Path(ctx["state_root"]) / "_state/.round_inflight.lockdir/pins" / f"ready.{ctx['worker_pin']}.tsv"
    lines = read(p).splitlines(keepends=True)
    if not lines or lines[-1] != b"record_sha256\t" + sha(b"".join(lines[:-1])).encode() + b"\n":
        die("invalid pin record digest")
    pairs = [line[:-1].decode().split("\t") for line in lines[:-1]]
    if any(len(pair) != 2 for pair in pairs) or len(dict(pairs)) != len(pairs):
        die("invalid pin record fields")
    pin = dict(pairs)
    for name, value in {"token": ctx["generation"], "pin_token": ctx["worker_pin"], "round_barcode": ctx["round"], "scope": "full_round", "role": "backup_read_fate_normalize", "pid": str(ctx["worker_pid"]), "host": ctx["host"]}.items():
        if pin.get(name) != value:
            die("worker pin identity mismatch: " + name)
    if pin.get("process_start") != ctx["pin_start"]:
        die("pin start representation changed")
    if sys.platform.startswith("linux"):
        raw = Path(f"/proc/{ctx['worker_pid']}/stat").read_text()
        ticks = raw.rsplit(") ", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if pin["process_start"] != f"proc:{boot}:{ticks}":
            die("Linux process birth differs from pin")

def witness_census(state):
    witness = state / "_state/.read_fate_normalization_pending"
    for p in witness.parent.iterdir():
        if p.name.startswith(witness.name + "."):
            die(f"unknown pending residue {p}")
    return witness

def invoke_args(ctx, boundary):
    return [ctx["repair_helper"], "--live", "--skip-render", "--state-dir", ctx["state_root"], "--current-round-barcode", boundary, "--round-index-file", ctx["state_root"] + "/_state/round_index.tsv", "--targets", ctx["targets"], "--target-taxa", ctx["target_taxa"], "--blast-unassigned-min-level", ctx["assignment_level"], "--outdir", ctx["outdir"]]

def context_rounds(ctx, helper, boundary):
    state = Path(ctx["state_root"])
    mapping, entries = helper["authoritative_round_context"](state, state / "_state/round_index.tsv", boundary)
    checked = []
    for directory, obj in entries:
        exact = strict_json(directory / "round_report.json")
        if not isinstance(exact, dict) or json.dumps(exact, sort_keys=True, separators=(",", ":"), ensure_ascii=True) != json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True):
            die(f"round JSON changed during context read {directory}")
        checked.append((directory, exact))
    entries = checked
    if entries[-1][1]["round_barcode"] != boundary:
        die("nonterminal boundary")
    return mapping, entries

def required_presence(ctx, mapping, entries):
    for directory, obj in entries:
        b, round_name = component(obj["barcode"]), component(obj["round_barcode"])
        index = mapping[round_name]
        for suffix in RETAINED[:4]:
            path = retained_path(directory, b, suffix)
            if safe(path, required=False) is None:
                die(f"retained producer input absent without evidence of legitimate original absence: unit={b}/{round_name}/{index} round={round_name} path={path}")

def capture_records(mapping, entries, inputs):
    records = []
    for directory, obj in entries:
        round_name, b = obj["round_barcode"], obj["barcode"]
        index = mapping[round_name]
        stable = stable_digest(obj)
        raw = f"round\t{index}\t{hx(round_name)}\t{hx(b)}\t{stable}\n".encode("ascii")
        states = []
        for suffix in RETAINED:
            path = retained_path(directory, b, suffix)
            identity = safe(path, required=False)
            if identity is None:
                if suffix in RETAINED[:4]:
                    die(f"retained producer input absent without evidence of legitimate original absence: unit={b}/{round_name}/{index} round={round_name} path={path}")
                state = ["A"]
            else:
                expected = inputs.get(str(path))
                if expected is None:
                    die(f"uncaptured retained input round={round_name} path={path}")
                size = path.lstat().st_size
                state = ["F", str(size), expected]
            states.append(state)
            raw += (f"input\t{index}\t{suffix}\t" + "\t".join(state) + "\n").encode("ascii")
        records.append({"index": index, "round": round_name, "barcode": b, "stable": stable, "inputs": states, "raw": raw})
    return records

def verify_records(state, mapping, records, context, expected_context):
    if context != expected_context:
        die(f"replay context changed: recorded={context!r} current={expected_context!r}")
    expected_rows = sorted((index, round_name) for round_name, index in mapping.items() if index <= records[-1]["index"])
    if [(record["index"], record["round"]) for record in records] != expected_rows:
        die("authoritative mapped round prefix changed")
    for record in records:
        directory = state / record["round"]
        obj = strict_json(directory / "round_report.json")
        if not isinstance(obj, dict) or obj.get("barcode") != record["barcode"] or obj.get("round_barcode") != record["round"] or stable_digest(obj) != record["stable"]:
            die(f"stable round JSON changed round={record['round']} path={directory / 'round_report.json'}")
        for suffix, saved in zip(RETAINED, record["inputs"]):
            path = retained_path(directory, record["barcode"], suffix)
            identity = safe(path, required=False)
            if saved[0] == "A":
                if identity is not None:
                    die(f"retained input appeared round={record['round']} path={path}")
            elif identity is None:
                die(f"retained input disappeared round={record['round']} path={path}")
            elif str(path.lstat().st_size) != saved[1] or digest(path) != saved[2]:
                die(f"retained input bytes changed round={record['round']} path={path}")

def compare_captured_records(state, saved, current, context, expected_context):
    if context != expected_context:
        die(f"replay context changed: recorded={context!r} current={expected_context!r}")
    if len(saved) > len(current):
        die("historical round prefix is no longer authoritative")
    for previous, now in zip(saved, current):
        if (previous["index"], previous["round"], previous["barcode"]) != (now["index"], now["round"], now["barcode"]):
            die(f"historical round remapped round={previous['round']}")
        if previous["stable"] != now["stable"]:
            die(f"stable round JSON changed round={previous['round']} path={state / previous['round'] / 'round_report.json'}")
        for suffix, before, after in zip(RETAINED, previous["inputs"], now["inputs"]):
            if before != after:
                path = retained_path(state / previous["round"], previous["barcode"], suffix)
                change = "appeared" if before[0] == "A" else "disappeared" if after[0] == "A" else "bytes changed"
                die(f"retained input {change} round={previous['round']} path={path}")

def verify_captured_records(ctx, helper, mapping, records, inputs, context):
    current_mapping, entries = context_rounds(ctx, helper, ctx["round"])
    if current_mapping != mapping:
        die("authoritative mapped round prefix changed before publication")
    for directory, obj in entries:
        path = retained_path(directory, obj["barcode"], "round_index.tsv")
        inputs[str(path)] = digest(path) if safe(path, required=False) else None
    current = capture_records(mapping, entries, inputs)
    compare_captured_records(Path(ctx["state_root"]), records, current, context, context)

def prepare_plan(ctx, helper):
    worker_identity(ctx)
    state = Path(ctx["state_root"])
    witness = witness_census(state)
    original = read(witness) if safe(witness, required=False) else None
    old_identity = safe(witness, required=False)
    mapping, entries = context_rounds(ctx, helper, ctx["round"])
    if entries[-1][1]["barcode"] != ctx["barcode"]:
        die("current boundary barcode mismatch")
    mode = helper["authoritative_history_publish_mode"](state / "_state/report_history.jsonl", ctx["round"], mapping, entries)
    if mode not in ("append", "normalize"):
        die("unknown second order result")
    units, old = [], None
    if original is not None:
        old = pending_read(witness, ctx["state"], mapping)
        units = old["units"]
        if old["generation"] != ctx["generation"]:
            # Witness adoption never authorizes reclaiming an unfinished round.
            subprocess.run(["perl", ctx["generation_helper"], "verify-finish", "--state-dir", str(state / "_state"), "--round-barcode", unhex(units[-1][1]), "--scope", "full_round", "--token", old["generation"]], check=True)
    terminal = unit(ctx["state"], ctx["barcode"], ctx["round"], mapping[ctx["round"]], prefix(mapping, ctx["round"]))
    if units and int(units[-1][2]) >= int(terminal[2]):
        die("current obligation does not strictly extend the retained plan")
    units.append(terminal)
    required_presence(ctx, mapping, entries)
    capture = mode == "normalize" or original is not None
    prepared = []
    for obligation in units:
        boundary, barcode = unhex(obligation[1]), unhex(obligation[0])
        unit_map, unit_entries = context_rounds(ctx, helper, boundary)
        if unit_entries[-1][1]["barcode"] != barcode or obligation != unit(ctx["state"], barcode, boundary, unit_map[boundary], prefix(unit_map, boundary)):
            die(f"historical obligation remapped {boundary}")
        fate, outputs, inputs, stable_json = expected_prefix(unit_entries, capture and obligation == terminal)
        prepared.append((obligation, unit_entries, fate, outputs, inputs, stable_json))
    context = tuple(ctx[name] for name in ("targets", "target_taxa", "assignment_level"))
    records = []
    if capture:
        current_records = capture_records(mapping, entries, prepared[-1][4])
        if old is not None:
            compare_captured_records(state, old["records"], current_records, old["context"], context)
            old_count = len(old["records"])
            records = old["records"] + current_records[old_count:]
        else:
            records = current_records
    calls = []
    for obligation, unit_entries, fate, outputs, inputs, stable_json in prepared:
        contracts = expected_reports(ctx, unit_entries, fate, outputs, inputs, stable_json, len(calls), capture)
        calls.append({"unit": obligation, "fate": [b.hex() for b in fate], "outputs": {p: b.hex() for p, b in outputs.items()}, "inputs": inputs, "input_identities": {p: list(safe(p, required=False)) if safe(p, required=False) else None for p in inputs}, "stable_json": stable_json, "contracts": contracts})
    plan = {"context": ctx, "calls": calls, "normalize": mode == "normalize" or original is not None}
    control = Path(ctx["control"])
    if plan["normalize"]:
        verify_captured_records(ctx, helper, mapping, records, prepared[-1][4], context)
        wire = pending_bytes(ctx["state"], ctx["generation"], units, context_fields(ctx), records)
        temp = witness.with_name(witness.name + ".tmp." + sha(wire))
        exclusive(temp, wire)
        if safe(witness, required=False) != old_identity or (original is not None and read(witness) != original):
            die("witness changed before atomic adoption")
        os.replace(temp, witness)
        if read(witness) != wire:
            die("installed pending differs")
        plan["pending_identity"] = list(safe(witness))
        plan["pending_sha"] = sha(wire)
    if not plan["normalize"]:
        plan["unchanged_unit"] = {str(state / "_state" / f"{ctx['barcode']}_{suffix}"): digest(state / "_state" / f"{ctx['barcode']}_{suffix}") for suffix in FATE}
    exclusive(control / "repair-plan.json", json.dumps(plan, sort_keys=True).encode() + b"\n")

def verify_durable(ctx, helper, plan, witness):
    if list(safe(witness)) != plan["pending_identity"] or digest(witness) != plan["pending_sha"]:
        die("pending witness changed during replay")
    mapping, _ = context_rounds(ctx, helper, ctx["round"])
    saved = pending_read(witness, ctx["state"], mapping)
    if saved["generation"] != ctx["generation"] or saved["units"] != [call["unit"] for call in plan["calls"]]:
        die("pending obligations changed during replay")
    context = tuple(ctx[name] for name in ("targets", "target_taxa", "assignment_level"))
    verify_records(Path(ctx["state_root"]), mapping, saved["records"], saved["context"], context)

def release_history(ctx):
    lock = Path(ctx["state_root"]) / "_state/.report_history.lock.lockdir"
    if list(safe(lock, directory=True)) != ctx["history_identity"]:
        die("history lock identity changed")
    expected = f"pid={ctx['worker_pid']}\nhost={ctx['history_host']}\nstarted_epoch={ctx['history_epoch']}\n".encode()
    if read(lock / "meta.env") != expected or sorted(p.name for p in lock.iterdir()) != ["meta.env"]:
        die("history owner metadata changed")
    (lock / "meta.env").unlink()
    lock.rmdir()

def apply_plan(ctx, helper):
    if os.getpid() != ctx["worker_pid"]:
        die("scientific mutator is not the pin owner")
    worker_identity(ctx)
    control = Path(ctx["control"])
    plan_raw = read(control / "repair-plan.json")
    plan = json.loads(plan_raw)
    if plan["context"] != ctx:
        die("private plan context mismatch")
    state = Path(ctx["state_root"])
    witness = witness_census(state)
    receipts, latest = [], {}
    for call in plan["calls"]:
        u = call["unit"]
        boundary, b = unhex(u[1]), unhex(u[0])
        mapping, entries = context_rounds(ctx, helper, boundary)
        if u != unit(ctx["state"], b, boundary, mapping[boundary], prefix(mapping, boundary)):
            die("prefix changed before invocation")
        if plan["normalize"]:
            verify_durable(ctx, helper, plan, witness)
        verify_inputs(call["inputs"], call["input_identities"])
        if plan["normalize"]:
            worker_identity(ctx)
            saved = sys.argv
            sys.argv = invoke_args(ctx, boundary)
            try:
                try:
                    runpy.run_path(ctx["repair_helper"], run_name="__main__")
                except SystemExit as e:
                    if e.code not in (None, 0):
                        raise
            finally:
                sys.argv = saved
            verify_durable(ctx, helper, plan, witness)
            verify_inputs(call["inputs"], call["input_identities"])
            verify_report_contract(ctx, call["contracts"])
            hashes = verify_outputs(state, entries, [bytes.fromhex(x) for x in call["fate"]], {p: bytes.fromhex(x) for p, x in call["outputs"].items()}, call["stable_json"])
        else:
            hashes = plan["unchanged_unit"]
            for p, expected in hashes.items():
                if digest(p) != expected:
                    die("append unit changed while supervised")
        verify_inputs(call["inputs"], call["input_identities"])
        if plan["normalize"]:
            # Immutable historical receipts bind every verified output, not just
            # the three sidecars. Later calls replace only their own paths.
            output_paths = list(call["outputs"]) + list(call["contracts"]["reports"]) + list(call["contracts"]["unassigned"]) + [call["contracts"]["run_path"], str(state / "_state/report_history.jsonl")]
            hashes.update({p: digest(p) for p in output_paths})
        receipt = {"unit": u, "hashes": hashes, "inputs": call["inputs"], "input_identities": call["input_identities"], "expected_sha": sha(json.dumps(call["contracts"], sort_keys=True).encode())}
        receipts.append(receipt)
        latest.update(hashes)
        exclusive(control / f"unit-{u[2]}.receipt", json.dumps(receipt, sort_keys=True).encode() + b"\n")
    for call, receipt in zip(plan["calls"], receipts):
        verify_inputs(call["inputs"], call["input_identities"])
        retained = json.loads(read(control / f"unit-{call['unit'][2]}.receipt"))
        if retained != receipt:
            die("historical receipt differs")
    for p, expected in latest.items():
        if digest(p) != expected:
            die(f"latest verified bounded output changed {p}")
    if plan["normalize"]:
        verify_durable(ctx, helper, plan, witness)
        terminal = plan["calls"][-1]
        _, entries = context_rounds(ctx, helper, ctx["round"])
        verify_outputs(state, entries, [bytes.fromhex(x) for x in terminal["fate"]], {p: bytes.fromhex(x) for p, x in terminal["outputs"].items()}, terminal["stable_json"])
        verify_report_contract(ctx, terminal["contracts"])
        last_r4 = {call["contracts"]["r4_barcode"]: call["contracts"] for call in plan["calls"]}
        for expected in last_r4.values():
            verify_r4_contract(ctx, expected)
    worker_identity(ctx)
    if plan["normalize"]:
        verify_durable(ctx, helper, plan, witness)
    release_history(ctx)
    worker_identity(ctx)
    if plan["normalize"]:
        verify_durable(ctx, helper, plan, witness)
        witness.unlink()
    elif safe(witness, required=False):
        die("unexpected pending on append path")
    witness_census(state)
    receipt = {"context": ctx, "plan_sha": sha(plan_raw), "calls": receipts, "history_released": True, "pending_removed": True}
    exclusive(control / "success.receipt", json.dumps(receipt, sort_keys=True).encode() + b"\n")


def bootstrap(args):
    if len(args) != 23 or args[0] != "2":
        die("worker interface arity/version")
    version, state_root, sid, bh, rh, generation, parent_pid, parent_pin, pin_file, guard, generation_helper, authority_helper, repair_helper, driver, targets, target_taxa, assignment_level, outdir, control, actual_pid, worker_pin, native_before, native_after = args
    if native_before != native_after or native_start(int(actual_pid)) != native_before:
        die("worker birth changed across pin/exec")
    for value in (generation, parent_pin, worker_pin):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            die("worker token encoding")
    if not re.fullmatch(r"[1-9][0-9]*", parent_pid) or not re.fullmatch(r"[1-9][0-9]*", actual_pid):
        die("worker PID encoding")
    for p in (state_root, control, outdir):
        if not Path(p).is_absolute():
            die("worker paths must be absolute")
        safe(p, directory=True)
    for p in (guard, generation_helper, authority_helper, repair_helper, driver, pin_file):
        safe(p)
    if Path(state_root).name != sid or assignment_level not in ("family", "genus", "species"):
        die("worker state/options")
    if read(pin_file) != (worker_pin + "\n").encode():
        die("worker pin-token file mismatch")
    pin_path = Path(state_root) / "_state/.round_inflight.lockdir/pins" / f"ready.{worker_pin}.tsv"
    pin = dict(row.decode().split("\t", 1) for row in read(pin_path).splitlines())
    ctx = dict(state_root=state_root, state=component(sid), barcode=unhex(bh), round=unhex(rh), generation=generation, parent_pid=int(parent_pid), parent_pin=parent_pin, worker_pid=int(actual_pid), worker_pin=worker_pin, native_start=native_before, pin_start=pin["process_start"], host=pin["host"], history_host=subprocess.check_output(["hostname"], text=True).strip(), started_epoch=time.time(), generation_helper=generation_helper, authority_helper=authority_helper, repair_helper=repair_helper, targets=targets, target_taxa=target_taxa, assignment_level=assignment_level, outdir=outdir, control=control)
    worker_identity(ctx)
    raw = json.dumps(ctx, sort_keys=True).encode() + b"\n"
    exclusive(Path(control) / "ready.tmp", raw)
    os.rename(Path(control) / "ready.tmp", Path(control) / "ready.json")

def acknowledge(control, pid, generation, parent_pin, wait_seconds):
    ready = Path(control) / "ready.json"
    deadline = time.monotonic() + int(wait_seconds)
    while not ready.exists():
        if time.monotonic() >= deadline:
            die("worker readiness timeout")
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            die("worker died before readiness")
        time.sleep(0.05)
    raw = read(ready)
    ctx = json.loads(raw)
    if ctx["worker_pid"] != int(pid) or ctx["generation"] != generation or ctx["parent_pin"] != parent_pin:
        die("supervised child identity mismatch")
    worker_identity(ctx)
    exclusive(Path(control) / "ack", (sha(raw) + "\n").encode())

def activate(control):
    control = Path(control)
    raw = read(control / "ready.json")
    if read(control / "ack") != (sha(raw) + "\n").encode():
        die("parent acknowledgement differs")
    ctx = json.loads(raw)
    lock = Path(ctx["state_root"]) / "_state/.report_history.lock.lockdir"
    identity = safe(lock, directory=True)
    meta = read(lock / "meta.env").decode().splitlines()
    if len(meta) != 3 or meta[:2] != [f"pid={ctx['worker_pid']}", f"host={ctx['history_host']}"] or not re.fullmatch(r"started_epoch=[0-9]+", meta[2]):
        die("history lock ownership differs")
    ctx["history_identity"] = list(identity)
    ctx["history_epoch"] = meta[2].split("=", 1)[1]
    exclusive(control / "active-context.json", json.dumps(ctx, sort_keys=True).encode() + b"\n")
    helper = runpy.run_path(ctx["repair_helper"])
    prepare_plan(ctx, helper)

def parent_success(control, pid, generation, parent_pin):
    control = Path(control)
    ready = json.loads(read(control / "ready.json"))
    receipt = json.loads(read(control / "success.receipt"))
    if ready["worker_pid"] != int(pid) or ready["generation"] != generation or ready["parent_pin"] != parent_pin:
        die("success context mismatch")
    for k, v in ready.items():
        if receipt["context"].get(k) != v:
            die("success changed worker identity")
    if receipt.get("history_released") is not True or receipt.get("pending_removed") is not True:
        die("incomplete worker cleanup")
    if receipt["plan_sha"] != digest(control / "repair-plan.json"):
        die("private plan digest mismatch")
    plan = json.loads(read(control / "repair-plan.json"))
    if [r["unit"] for r in receipt["calls"]] != [r["unit"] for r in plan["calls"]]:
        die("success roster differs")
    state = Path(ready["state_root"])
    witness = witness_census(state)
    if safe(witness, required=False):
        die("witness remains after worker exit")
    guard_pin(ready, parent_pin, "backup_update_and_clean")
    # Print only the exact worker token for the parent to retire after wait.
    print(ready["worker_pin"])

if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    if mode == "inspect":
        witness = witness_census(Path(args[0]))
        if safe(witness, required=False):
            pending_read(witness, args[1])
            print("pending")
        else:
            print("absent")
    elif mode == "bootstrap":
        bootstrap(args)
    elif mode == "ack":
        acknowledge(*args)
    elif mode == "prepare":
        activate(*args)
    elif mode == "apply":
        ctx = json.loads(read(args[0]))
        apply_plan(ctx, runpy.run_path(ctx["repair_helper"]))
    elif mode == "success":
        parent_success(*args)
    else:
        die("unknown worker mode")
RTB_WORKER_PYTHON
    $shell =~ s/export LC_ALL=C LANG=C LC_CTYPE=C/export LC_ALL=C LANG=C LC_CTYPE=C\nLOCK_WAIT=$wait\nREPORT_LOCK_STALE_TTL_SECONDS=$ttl/;
    for my$pair(['worker.sh',$shell],['driver.py',$driver]){sysopen(my$f,"$dir/$pair->[0]",O_WRONLY|O_CREAT|O_EXCL,0600) or fail('create private worker');print {$f} $pair->[1] or fail('write worker');close($f) or fail('close worker')}
    return 0;
}
sub restore_supervise {
    my($sid,$live,$t1,$t2,@argv)=@_;fail('restore command missing') unless @argv;
    my@locks=grep{defined($_)}map{root_lock($_,0)}($t1,$t2);
    $ENV{RTB_JOINT_RESTART_OWNER}=$$;$ENV{RTB_JOINT_ROOT_FDS}=join(',',map{fileno($_)}@locks);
    exec {$argv[0]} @argv or fail('restore exec failed');
}

sub resume {
    my($candidate,$t1,$t2)=@_;my$work=dirname($candidate);my$m=parse($candidate,$work,undef);
    fail('completed context mismatch') unless ($ENV{RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED}//0)==1&&($ENV{RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN}//'') eq $m->{token}&&($ENV{RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE}//'') eq $m->{round};
    system($^X,"$FindBin::Bin/round_lock_generation.pl",'verify-finish','--state-dir',$ENV{RTBIOSCAN_ROUND_LOCK_STATE_DIR},'--round-barcode',$m->{round},'--scope','full_round','--token',$m->{token})==0 or fail('completed attempt authentication');
    my@locks=map{root_lock($_,1)}($t1,$t2);my@sealed;my@marked;
    for my$r($t1,$t2){push@marked,node("$r/.f03-transaction") ne 'A';assert_marker($m,$r) if $marked[-1];root_scan($r,$m,$r eq $t2,1);
        if(node("$r/state_authority/AUTHORITY") ne 'A'){my(undef,$h)=digest("$r/state_authority/AUTHORITY");fail('newer/different seal') unless $h eq $m->{sha};push@sealed,1}else{push@sealed,0}}
    if($sealed[0]&&$sealed[1]){check_capture($candidate,$_,$work,1) for($t1,$t2);for my$i(0,1){my$r=($t1,$t2)[$i];if($marked[$i]){assert_marker($m,$r);unlink("$r/.f03-transaction") or fail('cleanup-only marker unlink')}}return 0}
    fail('completed retry lacks matching marker pair') unless $marked[0]&&$marked[1];check_capture($candidate,$t1,$work,0);
    for my$r($t1,$t2){unlink("$r/state_authority/AUTHORITY") or $!{ENOENT} or fail('retry invalidation')}
    return seal($candidate,$t1,$t2);
}
my $cmd=shift(@ARGV)//'';
my $rc=eval {
    my$work=tempdir('rtb-joint-XXXXXXXX',DIR=>abs_path(File::Spec->tmpdir),CLEANUP=>1);
    if($cmd eq 'prepare'){prepare(@ARGV)}
    elsif($cmd eq 'transaction'){transaction(@ARGV)}
    elsif($cmd eq 'seal'){seal(@ARGV)}
    elsif($cmd eq 'resume'){resume(@ARGV)}
    elsif($cmd eq 'capture'){my($candidate,$live,$root,$pin)=@ARGV;my$m=parse($candidate,$work,undef);guard($m,$live,$pin,'backup_update_and_clean');assert_marker($m,$root);transfer($candidate,$live,$root,1,0,$work);guard($m,$live,$pin,'backup_update_and_clean');0}
    elsif($cmd eq 'emit-worker'){emit_worker(@ARGV)}
    elsif($cmd eq 'restore-supervise'){restore_supervise(@ARGV)}
    elsif($cmd eq 'select'){print select_roots(@ARGV,$work),"\n";0}
    elsif($cmd eq 'install'){my($root,$state)=@ARGV;install("$root/state_authority/AUTHORITY",$root,dirname($state),$work)}
    elsif($cmd eq 'compat-copy'){compat_copy(@ARGV);0}
    elsif($cmd eq 'overlay-copy'){overlay_copy(@ARGV)}
    elsif($cmd eq 'presentation-leaf'){
        my($root,$entry)=@ARGV;my$m;
        if(node("$root/state_authority/AUTHORITY") ne 'A'){$m=parse("$root/state_authority/AUTHORITY",$work,undef)}
        my$p=presentation_set($m,$root);ancestors($entry);
        index($entry,"$root/")==0&&$p->{substr($entry,length($root)+1)}?0:1;
    }
    elsif($cmd eq 'governed'){broad_governed($ARGV[0])?0:1}
    elsif($cmd eq 'parse'){my$m=parse($ARGV[0],$work,undef);print "$m->{sha}\n";0}
    elsif($cmd eq 'verify'){my$r=$ARGV[0];if(node($r) eq 'A'||!completed_residue($r)){3}else{check_capture("$r/state_authority/AUTHORITY",$r,$work,1);0}}
    else{fail('usage: state_snapshot_authority prepare|transaction|capture|seal|emit-worker|restore-supervise|select|install|compat-copy|verify|parse ...')}
};
if(!defined($rc)){print STDERR "ERROR: $@";
    if($cmd eq 'restore-supervise'||$cmd eq 'select'){print STDERR "ERROR: This snapshot cannot be restored safely. Preserve the original state and use restart_mode=reset only when no protected round-lock evidence exists and the original inputs can be replayed.\n"}
    exit($cmd eq 'verify'?2:1)}
exit($rc);
