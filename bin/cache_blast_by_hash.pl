#!/usr/bin/env perl
use strict;
use warnings;
use Digest::MD5 qw(md5_hex);
use Digest::SHA qw(sha256_hex);
use File::Temp qw(tempfile);
use File::Basename qw(dirname basename);
use Text::ParseWords qw(shellwords);
use POSIX qw(isfinite);
use JSON::PP ();
use Math::BigInt;

# R4-A: sealed TSV envelopes end with a record count and SHA-256 of the body.
# CACHE v2: Q hash; M query hash; H hash subject signed-taxid evalue length
# pident bitscore qstart qend sstart send qlen selected-bit. HSP identity is hash/subject/coordinates.
# EVIDENCE: query hash subject taxid evalue length pident bitscore coordinates
# qlen status. Equal biological tuples retain ALL subjects, including same-taxid
# subjects. Lexical order is serialization only. NO_HIT has 11 NA evidence cells.
# Legacy five-argument mode remains the consensus worker's separate interface.

sub fail { die "R4-A: @_\n" }
sub number {
    my ($v, $min, $max) = @_;
    fail("invalid number: $v") unless defined($v) && $v =~ /\A(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\z/ && isfinite(0+$v);
    my $key=decimal_key($v); fail('number outside range') if decimal_cmp($key,decimal_key($min))<0 || (defined($max) && decimal_cmp($key,decimal_key($max))>0);
    return 0+$v;
}
sub uint { my ($v)=@_; fail('invalid positive integer') unless defined($v) && $v =~ /\A[1-9][0-9]*\z/ && (length($v)<10 || (length($v)==10 && $v le '2147483647')); }
sub taxid { fail('invalid signed taxid') unless defined($_[0]) && $_[0] =~ /\A(?:0|[1-9][0-9]*|-[1-9][0-9]*)\z/; }
sub token { fail('invalid token') unless defined($_[0]) && $_[0] ne '' && $_[0] !~ /[\s\x00]/; }
sub query_id { token($_[0]); fail('query delimiter cannot be represented in public report') if $_[0] =~ /[,;]/; }
sub hash_id { fail('invalid sequence hash') unless $_[0] =~ /\A[0-9a-f]{32}\z/; }
sub read_lines {
    my ($path)=@_; fail("missing/unreadable file $path") unless -f $path && -r $path;
    open my $f,'<',$path or fail("read $path: $!");
    my @lines;
    while (my $s=<$f>) { fail("truncated final row in $path") unless $s =~ s/\r?\n\z//; fail('embedded CR/NUL') if $s =~ /[\r\x00]/; push @lines,$s; }
    close $f or fail("close $path: $!"); return \@lines;
}
sub each_line {
    my ($path,$visit)=@_; fail("missing/unreadable file $path") unless -f $path && -r $path;
    open my $f,'<',$path or fail('read input');
    while (my $line=<$f>) { fail('truncated final row') unless $line =~ s/\r?\n\z//; fail('embedded CR/NUL') if $line =~ /[\r\x00]/; $visit->($line); }
    close $f or fail('close input');
}
sub atomic_write {
    my ($path,$text)=@_;
    my ($f,$tmp)=tempfile('.r4-publish-XXXXXX',DIR=>dirname($path),UNLINK=>0);
    my $ok=eval { print {$f} $text or fail("write $tmp: $!"); close $f or fail("close $tmp: $!"); rename $tmp,$path or fail("rename $path: $!"); 1 };
    my $err=$@; unlink $tmp if -e $tmp; die $err unless $ok;
}
# CACHE v2 records carry a validated producer's selected-HSP bit. MEMTAX v2
# stores one rank label, or a canonical JSON array of two or more sorted labels.
# Singleton labels retain the existing spelling; arrays cannot hide duplicates.
sub schema_version { return $_[0] eq 'EVIDENCE' ? 1 : 2; }
sub sealed {
    my ($kind,$sig,$rows)=@_;
    my $body=join('',map { "$_\n" } @$rows);
    return "#RTB-R4-$kind\t".schema_version($kind)."\t$sig\n".$body."#END\t".scalar(@$rows)."\t".sha256_hex($body)."\n";
}
sub scan_sealed {
    my ($path,$kind,$visit)=@_;
    fail("missing/unreadable file $path") unless -f $path && -r $path;
    open my $f,'<',$path or fail("read $path: $!");
    my $line=<$f> // '';
    fail("unsupported $kind schema") unless $line =~ /\A#RTB-R4-\Q$kind\E\t([12])\t([a-f0-9]{64})\r?\n\z/;
    my ($version,$sig)=($1,$2);
    fail('unsupported evidence schema') if $kind eq 'EVIDENCE' && $version!=1;
    my $sha=Digest::SHA->new(256); my ($count,$end,$digest)=(0,0,undef);
    while (defined($line=<$f>)) {
        fail("truncated final row in $path") unless $line =~ s/\r?\n\z//;
        fail('embedded CR/NUL') if $line =~ /[\r\x00]/;
        if ($line =~ /^#END\t/) {
            $digest=$sha->hexdigest;
            fail("incomplete $kind envelope") unless $line eq "#END\t$count\t$digest";
            fail('data after envelope') if defined(<$f>); $end=1; last;
        }
        $sha->add("$line\n"); $count++; $visit->($line,$version,$sig);
    }
    close $f or fail('close sealed input'); fail("incomplete $kind envelope") unless $end;
    return ($sig,$version,$digest,$count);
}
sub unseal {
    my ($path,$kind)=@_; my @rows;
    my ($sig,$version,$digest,$count)=scan_sealed($path,$kind,sub { push @rows,$_[0] });
    return ($sig,\@rows,$version,$digest,$count);
}
# Nonnegative decimal: coefficient with no leading/trailing zeros, and decimal
# order = coefficient length + exponent. Zero has an empty coefficient. Integer
# order arithmetic is exact; unusually long exponent spellings use BigInt ONCE
# on ingestion, never in comparisons. Coefficients compare lexically, implicitly
# padded with zeros (canonical coefficients never end in zero).
sub decimal_key {
    my ($v)=@_;
    fail('invalid decimal') unless defined($v) && $v =~ /\A(\d+(?:\.\d*)?|\.\d+)(?:[eE]([+-]?\d+))?\z/;
    my ($digits,$exp)=($1,$2 // '0'); my $fraction=($digits =~ /\.(\d*)/ ? length($1) : 0);
    $digits =~ s/\.//; $digits =~ s/^0+//;
    return ['0',''] if $digits eq '';
    my $shift=length($digits)-$fraction;
    my $order=length($exp)<10 ? ''.(int($exp)+$shift) : Math::BigInt->new($exp)->badd($shift)->bstr;
    $digits =~ s/0+$//;
    return [$order,$digits];
}
sub integer_cmp {
    my ($a,$b)=@_; my $na=$a =~ s/^-//; my $nb=$b =~ s/^-//;
    return $nb-$na if $na != $nb;
    return (length($a)<=>length($b) || $a cmp $b) * ($na ? -1 : 1);
}
sub decimal_cmp {
    my ($a,$b)=@_;
    return (length($a->[1])>0)<=>(length($b->[1])>0) if $a->[1] eq '' || $b->[1] eq '';
    return integer_cmp($a->[0],$b->[0]) || $a->[1] cmp $b->[1];
}
sub decimal_scientific {
    my ($k)=@_; return '0e+0' if $k->[1] eq '';
    my $exp=length($k->[0])<10 ? ''.($k->[0]-length($k->[1])) : Math::BigInt->new($k->[0])->bsub(length($k->[1]))->bstr;
    return $k->[1].'e'.($exp =~ /^-/ ? '' : '+').$exp;
}
sub encode_labels { my @labels=sort @_; return @labels==1 ? $labels[0] : JSON::PP->new->canonical->encode(\@labels); }
sub rank_labels {
    my ($text,$version)=@_;
    my $labels=$version==2 && $text =~ /^\[/ ? eval { JSON::PP::decode_json($text) } : [$text];
    fail('invalid rank labels') unless ref($labels) eq 'ARRAY' && @$labels;
    my %seen;
    for (@$labels) { fail('invalid rank label') if ref($_) || !defined($_) || !(/\A[a-z][a-z _-]*\z/ || $_ eq 'NA') || $seen{$_}++; }
    my $canonical=encode_labels(@$labels);
    fail('noncanonical rank labels') if $version==2 && $text ne $canonical;
    return $canonical;
}
sub seed_read {
    my ($path,$target,$include_numeric)=@_; return {} unless defined($path) && $path ne '' && $path ne 'null';
    token($target); fail('seed requires marker scope') unless $target ne '';
    my $name=basename($path);
    fail('configured seed/marker mismatch') if ($name =~ /^COInr_/ && $target ne 'COI') || ($name =~ /^ITS2nr_/ && $target ne 'ITS2');
    my (%ranks,%labels);
    for my $line (@{read_lines($path)}) {
        next if $line =~ /^\s*$/;
        my @v=split /\t/,$line,-1; fail('malformed configured seed') unless @v==5;
        # Historical COI class placeholders have no identifier or rank identities.
        next if $v[0] eq '' && join("\t",@v) eq "\t\t\t\tclass";
        taxid($v[0]); fail('seed zero identifier') if $v[0] eq '0';
        rank_labels($v[4],1);
        for (@v[1..3]) { $_='NA' if $_ eq ''; taxid($_) unless $_ eq 'NA'; fail('seed zero rank identity') if $_ eq '0'; }
        my @tuple=($v[4] eq 'species' ? $v[0] : 'NA',@v[1..3]);
        fail('conflicting configured seed depth') if exists($ranks{$v[0]}) && join("\t",@tuple) ne join("\t",@{$ranks{$v[0]}});
        $ranks{$v[0]}=\@tuple; $labels{$v[0]}{$v[4]}=1;
    }
    my %out;
    for (keys %ranks) { next unless /^-/ || $include_numeric; $out{$_}=[@{$ranks{$_}},encode_labels(keys %{$labels{$_}})]; }
    return \%out;
}
sub file_digest {
    my ($p)=@_; open my $f,'<',$p or fail("read signature input $p: $!"); binmode $f;
    my $sha=Digest::SHA->new(256); $sha->addfile($f); close $f or fail('close signature input'); return $sha->hexdigest;
}
sub reference_files {
    my ($db,$seen)=@_; return () if $seen->{$db}++;
    # BLAST alias dependencies are part of the content signature as well.
    opendir my $dir,dirname($db) or fail("read database directory: $!");
    my $base=basename($db);
    my @files=sort map { dirname($db)."/$_" } grep { /^\Q$base\E\.(?:[0-9]+\.)?n/ && -f dirname($db)."/$_" } readdir $dir;
    closedir $dir;
    fail("no nucleotide BLAST database files for $db") unless @files;
    if (-f "$db.nal") {
        for my $line (@{read_lines("$db.nal")}) {
            next unless $line =~ /^DBLIST\s+(.*)/;
            for my $dep (shellwords($1)) { push @files,reference_files($dep =~ m{^/} ? $dep : dirname($db)."/$dep",$seen); }
        }
    }
    return @files;
}
sub signature {
    my ($db,$taxdir,@params)=@_;
    my ($seed,$target)=('', '');
    for (@params) { $seed=$1 if /^seed=(.*)$/; $target=$1 if /^target=(.*)$/; }
    my $seed_rows=seed_read($seed,$target,1);
    # Hash canonical validated CONTENT, so order and exact duplicate repetition
    # do not change scientific identity; any tuple/label change still invalidates.
    push @params,'seed-sha256='.sha256_hex(join('',map { join("\t",$_,@{$seed_rows->{$_}})."\n" } sort keys %$seed_rows));
    fail('TAXONKIT_DB must identify pinned taxdump') unless defined($taxdir) && -d $taxdir;
    my $ref=sha256_hex(join('',map { $_."\t".file_digest($_)."\n" } reference_files($db,{})));
    my $tax=sha256_hex(join('',map { $_."\t".file_digest("$taxdir/$_")."\n" } qw(nodes.dmp names.dmp merged.dmp delnodes.dmp)));
    print join("\t",sha256_hex(join("\t",'r4-a-v3-selected-cache2-memtax2-seed-labels-exact-decimal',$ref,$tax,@params)),$ref,$tax),"\n";
}
sub fasta {
    my ($path)=@_; open my $f,'<',$path or fail("read FASTA $path: $!");
    my (%seq,%map,$id,$s); $s='';
    my $finish=sub {
        return unless defined $id; fail('empty sequence') if $s eq '';
        fail('invalid nucleotide sequence') unless $s =~ /\A[ACGTRYSWKMBDHVNUacgtryswkmbdhvnu]+\z/;
        my $h=md5_hex($s); fail('conflicting query/hash relationship') if exists($map{$id}) && $map{$id} ne $h;
        fail('hash collision') if exists($seq{$h}) && $seq{$h} ne $s;
        $map{$id}=$h; $seq{$h}=$s;
    };
    while (my $line=<$f>) {
        if ($line =~ /^>(\S+)/) { $finish->(); $id=$1; query_id($id); $s=''; }
        else { $line =~ s/\s+//g; fail('sequence before FASTA header') if !defined($id) && $line ne ''; $s.=$line; }
    }
    $finish->(); close $f or fail('close FASTA'); return (\%seq,\%map);
}
sub hsp {
    my ($r)=@_; fail('wrong HSP field count') unless @$r==12;
    hash_id($r->[0]); token($r->[1]); taxid($r->[2]);
    uint($r->[4]);
    my %keys; for (3,5,6) { fail('nonfinite HSP decimal') unless isfinite(0+$r->[$_]); $keys{$_}=decimal_key($r->[$_]); }
    fail('percent identity outside range') if decimal_cmp($keys{5},['3','1'])>0;
    uint($_) for @$r[7..11];
    # BLAST qcov_hsp_perc uses rounded integer coverage (100/201 reports 50).
    fail('HSP below BLAST 50 percent query coverage') if int((abs($r->[8]-$r->[7])+1)*100/$r->[11]+0.5) < 50;
    fail('query coordinate exceeds length') if $r->[7]>$r->[11] || $r->[8]>$r->[11];
    # Canonical numeric spelling prevents input order from choosing cache bytes.
    for (3,5,6) { $r->[$_]=decimal_scientific($keys{$_}); }
    return [$r,@keys{6,3,5}];
}
sub hsp_key { return join("\t",@{$_[0]}[0,1,7,8,9,10]); }
sub ranked {
    my ($r)=@_; return [$r, map { decimal_key($r->[$_]) } (6,3,5)];
}
sub unpack_ranked { return ref($_[0][0]) ? $_[0][0] : [split /\t/,$_[0][0],-1]; }
sub pack_ranked { my ($r)=@_; $r->[0]=join("\t",@{$r->[0]}) if ref($r->[0]); return $r; }
sub add_hsp {
    my ($c,$r,$selected,$loading)=@_; my $ranked=hsp($r);
    my ($h,$s,$t)=@$r;
    fail('HSP without query record') unless exists $c->{q}{$h};
    fail('conflicting subject taxids') if exists($c->{tax}{$s}) && $c->{tax}{$s} ne $t;
    $c->{tax}{$s}=$t;
    fail('conflicting query lengths') if exists($c->{qlen}{$h}) && $c->{qlen}{$h} ne $r->[11];
    $c->{qlen}{$h}=$r->[11];
    my $key=hsp_key($r); my $row=($selected ? 1 : 0)."\t".join("\t",@$r);
    fail('conflicting duplicate HSP') if exists($c->{h}{$key}) && $c->{h}{$key} ne $row;
    return if exists $c->{h}{$key};
    $c->{h}{$key}=$row;
    push @{$c->{selected}{$h}},pack_ranked($ranked) if $selected;
    push @{$c->{dirty}},$ranked unless $loading;
}
sub empty_cache { return {q=>{},m=>{},h=>{},tax=>{},qlen=>{},selected=>{},dirty=>[]}; }
sub cache_read {
    my ($path,$sig,$spool_prefix,$target)=@_; my $c=empty_cache(); return $c if !-e $path;
    fail('unreadable cache') unless -f $path && -r $path;
    open my $f,'<',$path or fail('read cache'); my $head=<$f> // ''; close $f;
    if ($head !~ /^#RTB-/) {
        my %seen;
        for (@{read_lines($path)}) {
            my @v=split /\t/,$_,-1; fail('malformed legacy cache') unless @v==5;
            hash_id($v[0]); token($v[1]); number($v[2],0,undef); uint($v[3]); number($v[4],0,100);
            fail('conflicting legacy duplicate') if exists($seen{$v[0]}) && $seen{$v[0]} ne $_;
            $seen{$v[0]}=$_;
        }
        return $c;
    }
    # CACHE v2 is canonical Q/M/H order. A streaming worker retains selected
    # rows only; canonical HSP-key order detects all duplicate/conflicting rows
    # with one previous row, and validated history is spooled for merge-writing.
    my $spool; my ($last_type,$last_key,$last_row)=('', '', '');
    if (defined($spool_prefix) && $head =~ /^#RTB-R4-CACHE\t2\t/) {
        $c->{history}="$spool_prefix.history";
        open $spool,'>',$c->{history} or fail('open history spool');
    }
    my ($stored,$version)=scan_sealed($path,'CACHE',sub {
        my ($line,$v)=@_; my @r=split /\t/,$line,-1; my $type=shift @r;
        my $key=$type eq 'H' && @r>=12 ? hsp_key(\@r) : ($r[0] // '');
        if ($v==2) {
            my %order=(Q=>0,M=>1,H=>2);
            fail('invalid cache record order') unless exists $order{$type};
            fail('noncanonical cache order') if $last_type ne '' && ($order{$type}<$order{$last_type} || ($type eq $last_type && $key lt $last_key));
        }
        if ($type eq 'Q' && @r==1) { hash_id($r[0]); $c->{q}{$r[0]}=1; }
        elsif ($type eq 'M' && @r==2) { query_id($r[0]); hash_id($r[1]); fail('cached query/seed marker mismatch') if defined($target) && (split /\|/,$r[0])[1] ne $target; fail('conflicting query mapping') if exists($c->{m}{$r[0]}) && $c->{m}{$r[0]} ne $r[1]; $c->{m}{$r[0]}=$r[1]; }
        elsif ($type eq 'H') {
            my $selected=$v==2 ? pop @r : 0;
            fail('invalid selected-HSP flag') unless defined($selected) && $selected =~ /\A[01]\z/;
            if ($spool) {
                my $ranked=hsp(\@r); my ($h,$subject,$tax)=@r;
                fail('HSP without query record') unless $c->{q}{$h};
                fail('conflicting subject taxids') if exists($c->{tax}{$subject}) && $c->{tax}{$subject} ne $tax;
                $c->{tax}{$subject}=$tax;
                fail('conflicting query lengths') if exists($c->{qlen}{$h}) && $c->{qlen}{$h} ne $r[11];
                $c->{qlen}{$h}=$r[11];
                my $canonical=join("\t",'H',@r,$selected);
                if ($last_type eq 'H' && $key eq $last_key) { fail('conflicting duplicate HSP') unless $canonical eq $last_row; return; }
                push @{$c->{selected}{$h}},pack_ranked($ranked) if $selected;
                print {$spool} "$key\0$canonical\n" or fail('write history spool');
                $last_row=$canonical;
            } else { add_hsp($c,\@r,$selected,$v==2); }
        } else { fail('wrong cache field count/type'); }
        ($last_type,$last_key)=($type,$key);
    });
    close $spool or fail('close history spool') if $spool;
    for (values %{$c->{m}}) { fail('mapping without query hash') unless $c->{q}{$_}; }
    if ($version==2) {
        for my $h (keys %{$c->{qlen}}) {
            my $rs=$c->{selected}{$h}; fail('missing selected evidence') unless $rs && @$rs;
            my %subject;
            for (@$rs) { fail('invalid selected tie') if $subject{unpack_ranked($_)->[1]}++ || better($_,$rs->[0])!=0; }
        }
    }
    return empty_cache() unless $stored eq $sig;
    select_dirty($c) if $version==1;
    return $c;
}
sub better {
    my ($a,$b)=@_;
    return decimal_cmp($b->[1],$a->[1]) || decimal_cmp($a->[2],$b->[2]) || unpack_ranked($b)->[4]<=>unpack_ranked($a)->[4] || decimal_cmp($b->[3],$a->[3]);
}
sub selections {
    my ($c,$keys)=@_; my %best;
    for my $r ($keys ? @$keys : map { ranked([split /\t/,substr($_,2), -1]) } values %{$c->{h}}) {
        my ($h,$s)=@{$r->[0]};
        my $old=$best{$h}{$s}; my $cmp=$old ? better($r,$old) : -1;
        $best{$h}{$s}=$r if !$old || $cmp<0 || ($cmp==0 && join("\t",@{$r->[0]}) lt join("\t",@{$old->[0]}));
    }
    my %selected;
    for my $h (keys %best) {
        my @r=sort { better($a,$b) || $a->[0][1] cmp $b->[0][1] } values %{$best{$h}};
        $selected{$h}=[grep { better($_,$r[0])==0 } @r];
    }
    return \%selected;
}
sub select_dirty {
    my ($c)=@_; return unless @{$c->{dirty}};
    my $chosen=selections($c,$c->{dirty}); $c->{selected}{$_}=[map { pack_ranked($_) } @{$chosen->{$_}}] for keys %$chosen;
    $c->{dirty}=[];
}
sub cache_rows {
    my ($c,$emit)=@_;
    my %new_hash=map { (split /\t/,$_,2)[0]=>1 } keys %{$c->{h}};
    my %chosen=map { hsp_key(unpack_ranked($_))=>1 } map { @{$c->{selected}{$_} // []} } keys %new_hash;
    $emit->("Q\t$_") for sort keys %{$c->{q}};
    $emit->("M\t$_\t$c->{m}{$_}") for sort keys %{$c->{m}};
    my @new=sort keys %{$c->{h}}; my $index=0;
    if ($c->{history}) {
        open my $history,'<',$c->{history} or fail('read validated spool');
        while (my $line=<$history>) {
            chomp $line; my ($key,$row)=split /\0/,$line,2;
            while ($index<@new && $new[$index] lt $key) { my $k=$new[$index++]; $emit->("H\t".substr($c->{h}{$k},2)."\t".($chosen{$k} ? 1 : 0)); }
            fail('new HSP conflicts with existing history') if $index<@new && $new[$index] eq $key;
            $emit->($row);
        }
        close $history or fail('close validated spool');
    }
    while ($index<@new) { my $k=$new[$index++]; $emit->("H\t".substr($c->{h}{$k},2)."\t".($chosen{$k} ? 1 : 0)); }
}
sub cache_text { my ($c,$sig)=@_; my @rows; cache_rows($c,sub {push @rows,$_[0]}); return sealed('CACHE',$sig,\@rows); }
sub evidence_rows {
    my ($c,$map,$emit)=@_;
    for my $id (sort keys %$map) {
        my $h=$map->{$id}; my $rs=$c->{selected}{$h} // [];
        if (!@$rs) { $emit->(join("\t",$id,$h,('NA')x11,'NO_HIT')); next; }
        my $status=@$rs>1 ? 'DEFERRED_TIE' : 'UNIQUE';
        $emit->(join("\t",$id,@{unpack_ranked($_)},$status)) for sort { unpack_ranked($a)->[1] cmp unpack_ranked($b)->[1] } @$rs;
    }
}
sub evidence_text { my ($c,$sig,$map)=@_; my @rows; evidence_rows($c,$map,sub { push @rows,$_[0] }); return sealed('EVIDENCE',$sig,\@rows); }
sub evidence_read {
    my ($path)=@_; my ($sig,$rows,$version,$digest,$count)=unseal($path,'EVIDENCE'); my (%seen,%map,%groups,%by_hash,%subject_tax);
    for (@$rows) {
        my @v=split /\t/,$_,-1; fail('wrong evidence field count') unless @v==14;
        my $id=shift @v; my $status=pop @v; my $ranked; query_id($id); hash_id($v[0]);
        fail('conflicting query/hash evidence') if exists($map{$id}) && $map{$id} ne $v[0]; $map{$id}=$v[0];
        if ($status eq 'NO_HIT') { fail('malformed NO_HIT') unless join("\t",@v[1..11]) eq join("\t",('NA')x11); }
        else { fail('unknown evidence status') unless $status eq 'UNIQUE' || $status eq 'DEFERRED_TIE'; $ranked=hsp(\@v); fail('conflicting subject taxonomy') if exists($subject_tax{$v[1]}) && $subject_tax{$v[1]} ne $v[2]; $subject_tax{$v[1]}=$v[2]; }
        my $key=join("\t",$id,$v[1]); fail('conflicting duplicate evidence') if exists($seen{$key}) && $seen{$key} ne $_;
        next if exists $seen{$key}; $seen{$key}=$_; push @{$groups{$id}},[\@v,$status,$ranked];
    }
    for my $id (keys %groups) {
        my $rs=$groups{$id}; my $status=$rs->[0][1];
        my $identity=join("\n",sort map { join("\t",@{$_->[0]},$_->[1]) } @$rs);
        fail('conflicting attribution for one sequence hash') if exists($by_hash{$map{$id}}) && $by_hash{$map{$id}} ne $identity;
        $by_hash{$map{$id}}=$identity;
        fail('invalid tie cardinality') if ($status eq 'DEFERRED_TIE') != (@$rs>1);
        for (@$rs) { fail('unequal/inconsistent tie evidence') if $_->[1] ne $status || ($status eq 'DEFERRED_TIE' && better($_->[2],$rs->[0][2])!=0); }
    }
    return ($sig,\%groups,{signature=>$sig,rows=>$count,body_sha256=>$digest});
}
sub memtax_read {
    my ($previous,$sig)=@_; return {} unless -e $previous;
    fail('unreadable memtax') unless -f $previous && -r $previous;
    open my $f,'<',$previous or fail('read memtax'); my $head=<$f> // '';
    if ($head !~ /^#RTB/) {
        fail('misplaced memtax schema marker') if $head =~ /#RTB/;
        while (my $line=<$f>) { fail('misplaced memtax schema marker') if $line =~ /#RTB/; }
        close $f or fail('close legacy memtax'); return {};
    }
    close $f or fail('close memtax header');
    # Unversioned state is obsolete DERIVED data, never a configured seed.
    # Do not parse legacy rows (including historical blank lines) as authority.
    fail('malformed memtax schema marker') unless $head =~ /^#RTB-/;
    my %loaded;
    my ($stored)=scan_sealed($previous,'MEMTAX',sub {
        my ($line,$version)=@_; my @r=split /\t/,$line,-1;
        fail('wrong memtax field count') unless @r==6; taxid($r[0]);
        for (@r[1..4]) { taxid($_) unless $_ eq 'NA'; fail('zero resolved rank') if $_ eq '0'; fail('negative NCBI rank') if $r[0]>=0 && /^-/; }
        $r[5]=rank_labels($r[5],$version);
        fail('conflicting memtax duplicate') if exists($loaded{$r[0]}) && join("\t",@{$loaded{$r[0]}}) ne join("\t",@r[1..5]);
        $loaded{$r[0]}=[@r[1..5]];
    });
    return $stored eq $sig ? \%loaded : {};
}
sub prepare {
    my ($fa,$cache,$sig,$prefix)=@_; my ($seq,$map)=fasta($fa); my $c=cache_read($cache,$sig);
    for my $h (keys %$seq) { fail('cached query length differs from sequence hash') if exists($c->{qlen}{$h}) && $c->{qlen}{$h} != length($seq->{$h}); }
    my $new=join('',map { ">$_\n$seq->{$_}\n" } sort grep { !exists($c->{q}{$_}) } keys %$seq);
    atomic_write("$prefix.snapshot",cache_text($c,$sig)); atomic_write("$prefix.fasta",$new);
}
sub reference_taxid {
    my ($subject,$native)=@_;
    # Existing RTBioScan references explicitly annotate the reserved Kraken tag.
    # Keep the complete opaque subject; never infer taxonomy from its accession.
    # Native BLAST metadata is zero for these unparsed reference databases.
    my $annotated;
    if ($subject =~ /\|kraken:taxid\|([^|]+)\z/) { $annotated=$1; taxid($annotated); }
    if ($native eq 'N/A') { fail('reference has no explicit taxid') unless defined $annotated; return $annotated; }
    taxid($native);
    if (defined $annotated) {
        fail('conflicting native and annotated reference taxids') unless $native eq '0' || $native eq $annotated;
        return $annotated;
    }
    return $native;
}
sub complete {
    my ($fa,$snapshot,$sig,$blast,$prefix)=@_; fail('missing prepared snapshot') unless -f $snapshot;
    open my $header,'<',$snapshot or fail('read snapshot'); my $head=<$header> // ''; close $header;
    fail('prepared snapshot signature mismatch') unless $head =~ /\A#RTB-R4-CACHE\t[12]\t\Q$sig\E\r?\n\z/; my ($seq,$map)=fasta($fa); my $c=cache_read($snapshot,$sig);
    my %new=map { $_=>1 } grep { !exists($c->{q}{$_}) } keys %$seq;
    $c->{q}{$_}=1 for keys %new;
    my %subject_tax;
    for my $line (@{read_lines($blast)}) {
        my @v=split /\t/,$line,-1; fail('wrong BLAST field count') unless @v==12;
        $v[2]=reference_taxid($v[1],$v[2]);
        fail('BLAST returned unknown/already-cached query') unless $new{$v[0]};
        fail('BLAST query length does not match hash') unless $v[11] eq length($seq->{$v[0]});
        add_hsp($c,\@v);
    }
    for (keys %$map) { fail('conflicting historical query mapping') if exists($c->{m}{$_}) && $c->{m}{$_} ne $map->{$_}; $c->{m}{$_}=$map->{$_}; }
    select_dirty($c);
    atomic_write("$prefix.cache",cache_text($c,$sig));
    atomic_write("$prefix.evidence",evidence_text($c,$sig,$map));
    atomic_write("$prefix.all-evidence",evidence_text($c,$sig,$c->{m}));
}
sub public_row {
    my @v=split /;/,$_[0],-1; fail('invalid public row') unless @v==5;
    query_id($v[0]); taxid($v[1]) unless $v[1] eq 'NA'; number($v[2],0,undef); uint($v[3]); number($v[4],0,100); return @v;
}
sub round_report {
    my ($fa,$report)=@_; my ($seq,$map)=fasta($fa);
    for my $line (@{read_lines($report)}) { my @v=public_row($line); print "$line\n" if exists($map->{$v[0]}); }
}
sub csv_cell { my ($s)=@_; $s =~ s/"/""/g; return $s =~ /[,"\r\n]/ ? '"'.$s.'"' : $s; }
sub display_number { my $s=decimal_scientific(decimal_key($_[0])); $s =~ s/e\+0\z//; return $s; }
sub display_identity {
    my $k=decimal_key($_[0]); return '0.0' if $k->[1] eq '';
    my ($order,$d)=@$k;
    return '0.'.('0' x -$order).$d if $order<=0;
    return $d.('0' x ($order-length($d))).'.0' if $order>=length($d);
    substr($d,$order,0,'.'); return $d;
}
sub project {
    my ($path)=@_; my ($sig,$groups)=evidence_read($path);
    for my $id (sort keys %$groups) {
        for (@{$groups->{$id}}) { my ($r,$status)=@$_; next if $status eq 'NO_HIT'; print join(',',map { csv_cell($_) } ($id,$r->[1],display_number($r->[3]),$r->[4],display_identity($r->[5]))),"\n"; }
    }
}
sub merge_reports {
    my ($old,$new,$targets)=@_; my %active=map { token($_); $_=>1 } @{read_lines($targets)}; my %rows;
    for my $p ($old,$new) {
        for my $s (@{read_lines($p)}) {
            my @v=public_row($s);
            my @id=split /\|/,$v[0]; fail('missing target in report query') unless @id>1;
            next if $p eq $old && $active{$id[1]};
            fail('conflicting report assignments') if exists($rows{$v[0]}) && $rows{$v[0]} ne $s;
            $rows{$v[0]}=$s;
        }
    }
    print $rows{$_},"\n" for sort keys %rows;
}
sub legacy {
my ($fasta, $cache, $out_cached, $out_new_fa, $out_hash) = @ARGV;
if (!defined $fasta || !defined $cache || !defined $out_cached || !defined $out_new_fa || !defined $out_hash) {
    die "Usage: cache_blast_by_hash.pl <fasta> <cache.tsv> <out_cached.csv> <out_new.fasta> <out_hash.tsv>\n";
}

my %cache = ();
for my $line (@{read_lines($cache)}) {
    my @v=split /\t/,$line,-1;
    fail('wrong legacy cache field count') unless @v==5;
    hash_id($v[0]); token($v[1]); number($v[2],0,undef); uint($v[3]); number($v[4],0,100);
    my $value=join(',',@v[1..4]);
    fail('conflicting legacy cache duplicate') if exists($cache{$v[0]}) && $cache{$v[0]} ne $value;
    $cache{$v[0]}=$value;
}
# Validate the complete FASTA before producing legacy outputs too.
fasta($fasta);

open my $OUTC, '>', $out_cached or die "Cannot write $out_cached: $!\n";
open my $OUTN, '>', $out_new_fa or die "Cannot write $out_new_fa: $!\n";
open my $OUTH, '>', $out_hash or die "Cannot write $out_hash: $!\n";

open my $F, '<', $fasta or die "Cannot read $fasta: $!\n";
local $/ = ">";
<$F>; # skip leading empty
while (my $chunk = <$F>) {
    chomp $chunk;
    next if $chunk eq '';
    my ($hdr, @seq) = split(/\n/, $chunk);
    my $seq = join('', @seq);
    $seq =~ s/\s+//g;
    next if $seq eq '';
    my ($id) = split(/\s+/, $hdr);
    my $hash = md5_hex($seq);
    if (exists $cache{$hash}) {
        print $OUTC $id, ',', $cache{$hash}, "\n";
    } else {
        print $OUTN ">", $id, "\n", $seq, "\n";
        print $OUTH $id, "\t", $hash, "\n";
    }
}
close $F;
close $OUTC;
close $OUTN;
close $OUTH;

}
# Streaming temporary outputs are fully produced from validated in-memory data.
# A manifest binds their exact bytes for the later all-target publication barrier.
# Publication copies and verifies this digest; it does not repeat scientific
# parsing/selection of data already validated by this worker.
sub sink_open {
    my ($path,$kind,$sig)=@_;
    my ($f,$tmp)=tempfile('.r4-publish-XXXXXX',DIR=>dirname($path),UNLINK=>0);
    my $s={f=>$f,tmp=>$tmp,path=>$path,kind=>$kind,body=>Digest::SHA->new(256),whole=>Digest::SHA->new(256),count=>0};
    if ($kind) { my $head="#RTB-R4-$kind\t".schema_version($kind)."\t$sig\n"; print {$f} $head or fail('write header'); $s->{whole}->add($head); }
    return $s;
}
sub sink_row {
    my ($s,$row)=@_; my $text="$row\n";
    print {$s->{f}} $text or fail('write row'); $s->{whole}->add($text); $s->{body}->add($text); $s->{count}++;
}
sub sink_finish {
    my ($s)=@_;
    if ($s->{kind}) { my $tail="#END\t$s->{count}\t".$s->{body}->hexdigest."\n"; print {$s->{f}} $tail or fail('write footer'); $s->{whole}->add($tail); }
    close $s->{f} or fail('close generated output'); rename $s->{tmp},$s->{path} or fail('publish generated output');
    return $s->{whole}->hexdigest;
}
sub publish_ready {
    my ($from,$to,$manifest,$key)=@_;
    my %sha;
    for (@{read_lines($manifest)}) { my @v=split /\t/,$_,-1; fail('invalid generation manifest') unless @v==2 && $v[1]=~/\A[a-f0-9]{64}\z/ && !exists $sha{$v[0]}; $sha{$v[0]}=$v[1]; }
    fail('missing generation digest') unless $sha{$key};
    open my $in,'<',$from or fail('read generated input'); binmode $in;
    my ($out,$tmp)=tempfile('.r4-publish-XXXXXX',DIR=>dirname($to),UNLINK=>0); binmode $out;
    my $ok=eval {
        my $digest=Digest::SHA->new(256); my $buf;
        while (1) { my $n=read($in,$buf,1048576); fail('read generated data') unless defined $n; last unless $n; $digest->add($buf); print {$out} $buf or fail('write publication'); }
        close $in or fail('close generated input'); close $out or fail('close publication');
        fail('generated data changed before publication') unless $digest->hexdigest eq $sha{$key};
        rename $tmp,$to or fail('rename publication'); 1;
    };
    my $err=$@; unlink $tmp if -e $tmp; die $err unless $ok;
}
sub worker {
    my $depth_script=dirname(__FILE__).'/get_blast_taxdepth.pl';
    if (@_ && $_[0] eq '--depth-helper') { shift @_; $depth_script=shift @_; fail('missing depth helper') unless defined($depth_script) && -f $depth_script; }
    my ($fa,$cache,$sig,$prefix,$previous,$sp,$ge,$seed,$target,@blast)=@_;
    number($sp,0,100); number($ge,0,100); token($target);
    my ($seq,$map)=fasta($fa); my $c=cache_read($cache,$sig,$prefix,$target); # only persistent history parse
    for my $id (keys %$map) { fail('query/seed marker mismatch') unless (split /\|/,$id)[1] eq $target; fail('conflicting historical query mapping') if exists($c->{m}{$id}) && $c->{m}{$id} ne $map->{$id}; }
    for my $h (keys %$seq) { fail('cached query length differs from sequence hash') if exists($c->{qlen}{$h}) && $c->{qlen}{$h} != length($seq->{$h}); }
    my %new=map { $_=>1 } grep { !exists($c->{q}{$_}) } keys %$seq;
    atomic_write("$prefix.fasta",join('',map { ">$_\n$seq->{$_}\n" } sort keys %new));
    atomic_write("$prefix.raw",'');
    if (keys %new) {
        fail('missing BLAST command') unless @blast;
        my $pid=fork(); fail('fork BLAST') unless defined $pid;
        if (!$pid) { open STDOUT,'>',"$prefix.raw" or die $!; exec @blast,'-query',"$prefix.fasta"; die "exec BLAST: $!"; }
        waitpid($pid,0); fail('blastn failed after partial output') if $?;
    }
    $c->{q}{$_}=1 for keys %new;
    each_line("$prefix.raw",sub {
        my @v=split /\t/,$_[0],-1; fail('wrong BLAST field count') unless @v==12;
        $v[2]=reference_taxid($v[1],$v[2]);
        fail('BLAST returned unknown/already-cached query') unless $new{$v[0]};
        fail('BLAST query length does not match hash') unless $v[11] eq length($seq->{$v[0]});
        add_hsp($c,\@v);
    });
    for (keys %$map) { fail('conflicting historical query mapping') if exists($c->{m}{$_}) && $c->{m}{$_} ne $map->{$_}; $c->{m}{$_}=$map->{$_}; }
    select_dirty($c); # only new HSPs; cache-only does not call selections
    require File::Basename;
    require $depth_script;
    my %needed; for my $rs (values %{$c->{selected}}) { $needed{unpack_ranked($_)->[2]}=1 for @$rs; }
    my $memory=resolve_memory(\%needed,$previous,$sig,$seed,$target);
    my ($species,$genus)=map { decimal_key($_) } ($sp,$ge);
    my %attribution;
    for my $h (keys %{$c->{selected}}) {
        my $rs=$c->{selected}{$h}; my $first=$rs->[0];
        $attribution{$h}=attribution(unpack_ranked($first),@$rs>1 ? 'DEFERRED_TIE' : 'UNIQUE',$memory,$species,$genus,$first->[3]);
    }
    my %sinks;
    for my $spec (['cache','CACHE'],['memtax','MEMTAX'],['evidence','EVIDENCE'],['all-evidence','EVIDENCE'],['all-report',''],['report',''],['preblast','']) {
        $sinks{$spec->[0]}=sink_open("$prefix.$spec->[0]",$spec->[1],$sig);
    }
    cache_rows($c,sub { sink_row($sinks{cache},$_[0]) });
    sink_row($sinks{memtax},join("\t",$_,@{$memory->{$_}})) for sort { $a<=>$b } keys %$memory;
    # One projection traversal: no evidence reparsing and no taxonomy per alias.
    evidence_rows($c,$c->{m},sub {
        my ($line)=@_; sink_row($sinks{'all-evidence'},$line);
        my @v=split /\t/,$line,-1; my ($id,$h)=@v;
        sink_row($sinks{evidence},$line) if exists $map->{$id};
        return if $v[-1] eq 'NO_HIT';
        if (exists $map->{$id}) { sink_row($sinks{preblast},join(',',map {csv_cell($_)} ($id,$v[2],display_number($v[4]),$v[5],display_identity($v[6])))); }
    });
    for my $id (sort keys %{$c->{m}}) {
        my $h=$c->{m}{$id}; next unless exists $attribution{$h};
        my $line="$id;$attribution{$h}";
        sink_row($sinks{'all-report'},$line); sink_row($sinks{report},$line) if exists $map->{$id};
    }
    my @manifest;
    push @manifest,"$_\t".sink_finish($sinks{$_}) for sort keys %sinks;
    atomic_write("$prefix.manifest",join('',map { "$_\n" } @manifest));
}
sub main {
    my $mode=$ARGV[0] // '';
    if ($mode !~ /^--/) { legacy(); return; }
    shift @ARGV;
    if ($mode eq '--signature') { signature(@ARGV); }
    elsif ($mode eq '--prepare') { prepare(@ARGV); }
    elsif ($mode eq '--complete') { complete(@ARGV); }
    elsif ($mode eq '--round-report') { round_report(@ARGV); }
    elsif ($mode eq '--worker') { worker(@ARGV); }
    elsif ($mode eq '--publish-ready') { publish_ready(@ARGV); }
    elsif ($mode eq '--project') { project(@ARGV); }
    elsif ($mode eq '--merge') { merge_reports(@ARGV); }
    elsif ($mode eq '--publish') {
        my ($from,$to)=@ARGV; my $rows=read_lines($from);
        fail('unknown publication schema') unless @$rows && $rows->[0] =~ /^#RTB-R4-(CACHE|MEMTAX|EVIDENCE)\t/;
        my $kind=$1; my ($sig)=unseal($from,$kind);
        if ($kind eq 'CACHE') { cache_read($from,$sig); }
        elsif ($kind eq 'MEMTAX') { memtax_read($from,$sig); }
        else { evidence_read($from); }
        atomic_write($to,join('',map { "$_\n" } @$rows));
    }
    else { fail('unknown command'); }
}
main() unless caller;
1;
