#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use Digest::SHA qw(sha256_hex);
use File::Basename qw(basename dirname);
use File::Temp qw(tempfile);
use JSON::PP qw(encode_json decode_json);
use FindBin;
require "$FindBin::Bin/lib/sample_label.pl";

# Current OTU evidence and historical artifact ownership are separate inputs.
# No sequence hashing or representative selection by row order occurs here.
my $command = shift @ARGV // '';
my %o;
GetOptions(\%o, map { "$_=s" } qw(clstr hash-map targets map parsed out input
    sample legacy column cache key representative display count candidate-hash valid
    lock-state locked-keys carry prior ids drop reset public-display previous-ids projected-ids))
    or die "ERROR: consensus identity: invalid arguments\n";
die "ERROR: consensus identity: unexpected arguments\n" if @ARGV;

sub fail { die "ERROR: consensus identity: $_[0]\n" }
sub warn_legacy { warn "WARN: consensus identity: ignored unproven legacy $_[0]\n" }
sub lines {
    my ($path) = @_;
    return () unless defined($path) && length($path) && -f $path;
    open my $f, '<', $path or fail("cannot read $path: $!");
    my @rows = <$f>;
    close $f or fail("cannot close $path: $!");
    for (@rows) { s/\r?\n\z//; }
    return @rows;
}
sub bytes {
    open my $f, '<', $_[0] or fail("cannot read $_[0]: $!");
    binmode $f;
    local $/;
    my $data = <$f> // '';
    close $f or fail("cannot close $_[0]: $!");
    return $data;
}
sub publish {
    my ($path, $data) = @_;
    fail('missing output path') unless defined($path) && length($path);
    my ($f, $tmp) = tempfile('.consensus-identity-XXXXXX', DIR => dirname($path), UNLINK => 0);
    binmode $f;
    print {$f} $data or fail("cannot write $tmp: $!");
    close $f or fail("cannot close $tmp: $!");
    rename $tmp, $path or fail("cannot publish $path: $!");
}
sub marker {
    my ($m) = @_;
    fail('missing or malformed marker') unless defined($m) && $m =~ /\A[A-Za-z0-9_.-]+\z/;
    $m = uc $m;
    $m = 'ITS2' if $m =~ /\AITS(?:1|2)?\z/;
    fail('missing marker') if $m eq 'NA' || $m eq 'NULL';
    return $m;
}
sub digest {
    my ($h) = @_;
    fail('missing or malformed representative hash') unless defined($h) && $h =~ /\A[0-9a-fA-F]{32}\z/;
    return lc $h;
}
sub stable {
    my ($k) = @_;
    my @f = split /\|/, $k // '', -1;
    fail("malformed stable key '$k'") unless @f == 2;
    return marker($f[0]) . '|' . digest($f[1]);
}
sub token {
    my ($id) = @_;
    fail('missing or malformed representative ID') unless defined($id) && $id =~ /\A[^\s]+\z/;
    return (split /\|/, $id, -1)[0];
}
sub display {
    my ($d) = @_;
    fail('malformed display key') unless defined($d) && $d =~ /\AOTUB_[0-9]+(?:-[A-Za-z0-9_.-]+)?\z/;
    return $d;
}
sub id_marker {
    my ($id) = @_;
    my @t = split /\|/, $id, -1;
    token($id);
    fail('missing representative marker') unless @t > 1;
    return marker($t[1]);
}
sub load_map {
    my ($path) = @_;
    my %map;
    for my $line (lines($path)) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed current identity map') unless @f == 5 || @f == 6;
        display($f[0]);
        $f[1] = stable($f[1]);
        token($f[2]);
        $f[3] = digest($f[3]);
        $f[4] = marker($f[4]);
        fail('conflicting current identity map') unless $f[1] eq "$f[4]|$f[3]" && id_marker($f[2]) eq $f[4];
        fail("conflicting display mapping $f[0]") if exists($map{$f[0]}) && join("\t", @{$map{$f[0]}}) ne join("\t", @f);
        $map{$f[0]} = \@f;
    }
    return %map;
}
my (%proof, %artifact_hash);
my $proof_loaded = 0;
sub legacy_key {
    my ($sample, $key, $artifact) = @_;
    my %keys;
    if (!$proof_loaded++) { for my $line (lines($o{legacy})) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed historical ownership evidence') unless @f == 8;
        display($f[1]); token($f[3]);
        my $k = stable($f[5]);
        fail('conflicting historical ownership evidence') unless $k eq marker($f[2]).'|'.digest($f[4]) && id_marker($f[3]) eq marker($f[2]);
        fail('malformed historical artifact digest') unless $f[7] =~ /\A[0-9a-f]{64}\z/;
        push @{$proof{join("\t", @f[0,1,6])}}, [@f];
    } }
    return unless -f $artifact;
    my $sha = $artifact_hash{$artifact} //= sha256_hex(bytes($artifact));
    for my $f (@{$proof{join("\t", $sample, $key, basename($artifact))} // []}) {
        next unless $f->[7] eq $sha;
        $keys{stable($f->[5])} = 1;
    }
    fail("ambiguous historical ownership for $sample/$key") if keys(%keys) > 1;
    return (keys %keys)[0];
}
sub metadata {
    my ($path) = @_;
    my @rows = lines($path); my %meta;
    return %meta unless @rows;
    my $first = shift @rows;
    $meta{candidate} = $first;
    for my $line (@rows) {
        my @f = split /\t/, $line, -1;
        fail("malformed cache identity metadata $path") unless @f == 2 && $f[0] ne '' && !exists($meta{$f[0]});
        $meta{$f[0]} = $f[1];
    }
    if (exists $meta{stable_otu_key}) {
        $meta{stable_otu_key} = stable($meta{stable_otu_key});
        token($meta{representative_id}); display($meta{display_otu_key});
        fail("conflicting cache representative marker $path") unless (split /\|/, $meta{stable_otu_key})[0] eq id_marker($meta{representative_id});
    }
    return %meta;
}
sub meta_text {
    my ($row, $count, $candidate, $fasta) = @_;
    my $text = "$count\t$candidate\nstable_otu_key\t$row->[1]\nrepresentative_id\t$row->[2]\ndisplay_otu_key\t$row->[0]\npublic_display_key\t".($row->[5] // $row->[0])."\n";
    $text .= 'cache_sha256'."\t".sha256_hex(bytes($fasta))."\n" if -s $fasta;
    return $text;
}
# Validate the recorded public identity before it can authorize a projection.
# Legacy headers may omit OTU tags, but sample, marker and public token must agree.
sub report_id {
    my ($id) = @_;
    return $1 . '_' . SampleLabel::normalize_sample_label($2)
        if $id =~ /\A(Consensus[0-9]+|OTUB_[0-9]+)_(.+)\z/;
    return $id;
}
sub public_record {
    my ($sample, $key, $d, $id, $header) = @_;
    fail('malformed public ownership record') if grep { !defined($_) || ref($_) || !length($_) || /[\t\r\n]/ } @_;
    my $h = $header; $h =~ s/\A>//;
    my @t = split /\|/, $h, -1;
    fail('conflicting public ownership sample or ID') unless @t >= 3 && SampleLabel::normalize_sample_label($t[0]) eq SampleLabel::normalize_sample_label($sample) && $id eq "$t[1]_$t[0]";
    fail('conflicting public ownership marker') unless marker($t[2]) eq (split /\|/, $key)[0];
    my @otu = map { /\AOTU=(.+)\z/ ? $1 : () } @t;
    fail('conflicting public ownership display') if @otu > 1 || (@otu && $otu[0] !~ /\A\Q$d\E(?:-|\z)/);
    return report_id($id);
}
sub owners {
    my ($path) = @_;
    my (%owners, %public);
    for my $line (lines($path)) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed consensus ownership state') unless @f == 4 && $f[0] ne '';
        $f[1] = stable($f[1]); display($f[2]);
        my $entries = eval { decode_json($f[3]) };
        fail('malformed consensus ownership entries') if $@ || ref($entries) ne 'ARRAY';
        my $key = "$f[0]\t$f[1]";
        for my $entry (@$entries) {
            fail('malformed consensus ownership record') unless ref($entry) eq 'ARRAY' && @$entry == 2;
            my $public_id = public_record(@f[0..2], @$entry);
            fail('duplicate or conflicting public consensus ownership') if exists $public{$public_id};
            $public{$public_id} = $key;
        }
        fail("duplicate consolidated owner $key") if exists $owners{$key};
        $owners{$key} = [$f[0], $f[1], $f[2], $entries];
    }
    return %owners;
}
sub dropped {
    my %drop;
    for my $line (lines($o{drop}), lines($o{reset})) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed dropped stable key') unless @f == 1 || @f == 2;
        my $key = stable($f[-1]);
        $drop{@f == 1 ? $key : "$f[0]\t$key"} = 1;
    }
    return %drop;
}

if ($command eq 'map') {
    fail('current merged cluster file is required') unless defined($o{clstr}) && -f $o{clstr};
    fail('representative hash map is required') unless defined($o{'hash-map'}) && -f $o{'hash-map'};
    my (%hash, %uuid_hash, %allowed, %clusters, %seen_id, %stable_cluster);
    $allowed{marker($_)} = 1 for split /\|/, $o{targets} // '';
    fail('configured markers are required') unless %allowed;
    for my $line (lines($o{'hash-map'})) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed NR hash map') unless @f == 2;
        my $u = token($f[0]); my $h = digest($f[1]);
        fail("ambiguous representative hash for $u") if (exists($hash{$f[0]}) && $hash{$f[0]} ne $h) || (exists($uuid_hash{$u}) && $uuid_hash{$u} ne $h);
        $hash{$f[0]} = $h; $uuid_hash{$u} = $h;
    }
    my $cluster;
    for my $line (lines($o{clstr})) {
        next if $line eq '';
        if ($line =~ /\A>Cluster ([0-9]+)\z/) {
            $cluster = "OTUB_$1";
            fail("duplicate cluster $cluster") if exists $clusters{$cluster};
            $clusters{$cluster} = [];
        } elsif (defined($cluster) && $line =~ /\A[0-9]+\s+[0-9]+(?:nt|aa), >([^\s]+)\.\.\.\s*(.*?)\s*\z/) {
            my ($id, $flag) = ($1, $2);
            fail("invalid representative flag for $id") unless $flag eq '*' || $flag eq '' || $flag =~ /\Aat (?:[+-]\/)?[0-9.]+%\z/;
            fail("duplicate canonical membership $id") if $seen_id{$id}++;
            push @{$clusters{$cluster}}, [$id, $flag eq '*' ? 1 : 0];
        } else { fail("malformed cluster membership: $line"); }
    }
    my @out;
    for my $c (sort keys %clusters) {
        my @rep = grep { $_->[1] } @{$clusters{$c}};
        fail("expected one representative for $c (found ".scalar(@rep).')') unless @rep == 1;
        my $id = $rep[0][0]; my $u = token($id);
        my $h = $hash{$id} // $uuid_hash{$u};
        fail("missing representative hash for $id") unless defined $h;
        my $m = id_marker($id);
        fail("unconfigured representative marker $m") unless $allowed{$m};
        my $key = "$m|$h";
        fail("stable key belongs to multiple canonical OTUs: $key") if exists($stable_cluster{$key}) && $stable_cluster{$key} ne $c;
        $stable_cluster{$key} = $c;
        my %aliases;
        for my $member (@{$clusters{$c}}) {
            my @t = split /\|/, $member->[0], -1;
            fail("conflicting member marker in $c") unless id_marker($member->[0]) eq $m;
            $aliases{"$c-$t[1]"} = 1;
        }
        push @out, join("\t", $_, $key, $id, $h, $m) for sort keys %aliases;
    }
    publish($o{out}, join('', map { "$_\n" } @out));
} elsif ($command eq 'bind') {
    my %map = load_map($o{map}); my (%bound, %current, %defaults);
    # Canonical defaults also cover OTUs absent from this sample's read partition.
    for my $d (sort keys %map) {
        my $r = $map{$d}; my ($bc) = $r->[2] =~ /(?:\A|\|)barcode=([^|]+)(?:\||\z)/;
        my $full = $d;
        $full .= "-$bc" if defined($bc) && $bc ne 'NA' && $full !~ /-\Q$bc\E\z/;
        display($full);
        $bound{$full} = [$full, @{$r}[1..4], $d];
        push @{$defaults{$r->[1]}}, $full;
    }
    for my $line (lines($o{parsed})) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed sample partition') unless @f == 5;
        my $r = $map{$f[1]} or fail("missing current identity for $f[1]");
        my $d = $f[1];
        $d .= "-$f[2]" if $f[2] ne '' && $f[2] ne 'NA' && $d !~ /-\Q$f[2]\E\z/;
        display($d);
        if (!$current{$r->[1]}++) {
            delete $bound{$_} for grep { exists($bound{$_}) && $bound{$_}[1] eq $r->[1] }
                @{$defaults{$r->[1]} // []};
        }
        fail("conflicting sample display mapping $d") if exists($bound{$d}) && $bound{$d}[1] ne $r->[1];
        $bound{$d} = [$d, @{$r}[1..4], $f[1]];
    }
    publish($o{out}, join('', map { join("\t", @{$bound{$_}})."\n" } sort keys %bound));
} elsif ($command eq 'keys') {
    my $col = ($o{column} // 1) - 1;
    fail('invalid key column') unless $col == 0 || $col == 1;
    my @out;
    for my $line (lines($o{input})) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        my $i = @f == 1 ? 0 : $col;
        my $k = $f[$i] // '';
        if ($k =~ /\AOTUB_/) {
            my $s = $i == 1 ? $f[0] : ($o{sample} // '');
            my $proven = legacy_key($s, $k, $o{input});
            if (!defined $proven) { warn_legacy("key $s/$k in ".basename($o{input})); next; }
            $k = $proven;
        } else { $k = stable($k); }
        $f[$i] = $k;
        push @out, join("\t", @f);
    }
    publish($o{out}, join('', map { "$_\n" } @out));
} elsif ($command eq 'current-keys') {
    my %map = load_map($o{map}); my %keys;
    my %known = map { $map{$_}[1] => 1 } keys %map;
    for my $line (lines($o{input})) {
        next if $line eq '';
        if ($line !~ /\AOTUB_/) {
            my $key = stable($line);
            fail("current stable key has no representative: $key") unless $known{$key};
            $keys{$key} = 1; next;
        }
        my $r = $map{$line} or fail("missing current display mapping $line");
        $keys{$r->[1]} = 1;
    }
    publish($o{out}, join('', map { "$_\n" } sort keys %keys));
} elsif ($command eq 'display-keys') {
    my %map = load_map($o{map}); my %reverse; my %out;
    push @{$reverse{$map{$_}[1]}}, $_ for sort keys %map;
    for my $line (lines($o{input})) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed stable key projection') unless @f == 1 || @f == 2;
        my $key = stable($f[-1]);
        for my $d (@{$reverse{$key} // []}) {
            $out{@f == 1 ? $d : "$f[0]\t$d"} = 1;
        }
    }
    publish($o{out}, join('', map { "$_\n" } sort keys %out));
} elsif ($command eq 'consolidated-display') {
    my %map = load_map($o{map}); my %owners = owners($o{prior}); my (%reverse, %out);
    push @{$reverse{$map{$_}[1]}}, $_ for sort keys %map;
    for my $owner (values %owners) {
        next unless @{$owner->[3]};
        my $all_consolidated = 1;
        for my $entry (@{$owner->[3]}) {
            my @flags = grep { /\Aconsolidated=/ } split /\|/, $entry->[1], -1;
            $all_consolidated = 0 unless @flags == 1 && $flags[0] eq 'consolidated=1';
        }
        next unless $all_consolidated;
        $out{"$owner->[0]\t$_"} = 1 for @{$reverse{$owner->[1]} // []};
    }
    publish($o{out}, join('', map { "$_\n" } sort keys %out));
} elsif ($command eq 'prune-view') {
    my %map = load_map($o{map}); my (%member, $cluster);
    for my $line (lines($o{clstr})) {
        if ($line =~ /\A>Cluster ([0-9]+)\z/) { $cluster = "OTUB_$1"; next; }
        next unless $line =~ />([^\s]+)\.\.\./;
        my $id = $1; my @t = split /\|/, $id, -1;
        my $display = "$cluster-$t[1]";
        fail('prune membership missing canonical identity') unless exists $map{$display};
        my $key = token($id).'|'.id_marker($id);
        fail('ambiguous canonical prune membership') if exists($member{$key}) && $member{$key} ne $display;
        $member{$key} = $display;
    }
    my @out;
    open my $f, '<', $o{input} or fail("cannot read prune input: $!");
    while (my $line = <$f>) {
        next unless $line =~ /\A>(\S+)/;
        my $id = $1; my @t = split /\|/, $id, -1;
        my $m = @t > 1 && $t[1] =~ /\A[A-Za-z0-9_.-]+\z/ ? marker($t[1]) : '';
        my $display = $member{"$t[0]|$m"};
        @t = grep { !/\A(?:OTUB_|OTU=)/ } @t;
        push @t, $display if defined $display;
        push @out, '>'.join('|', @t)."\n";
    }
    close $f or fail("cannot close prune input: $!");
    publish($o{out}, join('', @out));
} elsif ($command eq 'join') {
    my %map = load_map($o{map}); my @out;
    my %valid = map { $_ => 1 } lines($o{valid});
    for my $line (lines($o{input})) {
        next if $line eq '';
        my ($display) = split /\t/, $line, -1;
        my $r = $map{$display} or fail("missing current identity for $display");
        push @out, "$line\t$r->[1]\t".($valid{"$r->[1]\tconsensus.fasta"} ? 1 : 0)."\t$r->[2]\t".($r->[5] // $r->[0])."\n";
    }
    publish($o{out}, join('', @out));
} elsif ($command eq 'meta') {
    # The worker carries the validated row from plan/join. No full-map scan per OTU.
    my $key = stable($o{key});
    my ($marker, $hash) = split /\|/, $key;
    token($o{representative});
    fail('cache representative marker conflicts with stable identity') unless id_marker($o{representative}) eq $marker;
    my $r = [display($o{display}), $key, $o{representative}, $hash, $marker, display($o{'public-display'})];
    my $base = "$o{cache}/$r->[1]";
    publish("$base.meta", meta_text($r, $o{count} // 0, $o{'candidate-hash'} // 'NA', "$base.consensus.fasta"));
} elsif ($command eq 'cache') {
    my %map = load_map($o{map}); my %by_key;
    for my $d (sort keys %map) { $by_key{$map{$d}[1]} //= $map{$d}; }
    opendir my $dir, $o{cache} or fail("cannot read cache directory: $!");
    my @files = sort readdir $dir; closedir $dir;
    my (@migrate, %valid, %migration_source);
    for my $name (@files) {
        next unless $name =~ /\A(.+)\.(consensus\.fasta|pool\.tsv)\z/;
        my ($key, $kind) = ($1, $2); my $path = "$o{cache}/$name";
        next unless -s $path;
        if ($key =~ /\AOTUB_/) {
            my $proven = legacy_key($o{sample} // '', $key, $path);
            if (!defined($proven)) { warn_legacy("cache ".($o{sample}//'')."/$name"); next; }
            if (!exists $by_key{$proven}) { warn "WARN: consensus identity: cache owner absent from current OTU evidence: $proven\n"; next; }
            my $source_hash = sha256_hex(bytes($path));
            my $destination = "$proven\t$kind";
            fail("conflicting proven legacy cache sources for $destination")
                if exists($migration_source{$destination}) && $migration_source{$destination} ne $source_hash;
            $migration_source{$destination} = $source_hash;
            push @migrate, [$path, $proven, $kind, $key];
            next;
        }
        $key = stable($key);
        my %meta = metadata("$o{cache}/$key.meta");
        if (!exists($meta{stable_otu_key})) {
            fail("pool lacks stable ownership metadata: $name") if $kind eq 'pool.tsv';
            warn "WARN: consensus identity: ignored cache without stable identity: $name\n"; next;
        }
        fail("cache key conflicts with metadata: $name") unless $meta{stable_otu_key} eq $key;
        if (!exists $by_key{$key}) { warn "WARN: consensus identity: cache owner absent from current OTU evidence: $key\n"; next; }
        if ($kind eq 'consensus.fasta' && (!exists($meta{cache_sha256}) || $meta{cache_sha256} ne sha256_hex(bytes($path)))) {
            warn "WARN: consensus identity: ignored incomplete cache payload: $name\n"; next;
        }
        $valid{"$key\t$kind"} = 1;
    }
    # Validate every source before publishing any proven legacy copy. Originals stay intact.
    for my $m (@migrate) {
        my ($path, $key, $kind, $old_display) = @$m;
        next if $valid{"$key\t$kind"};
        my $r = $by_key{$key};
        my @row = ($old_display, @{$r}[1..4], $old_display);
        my ($old_public) = $old_display =~ /\A(OTUB_[0-9]+-\Q$r->[4]\E)(?:-|\z)/i;
        $row[5] = $old_public // $old_display;
        publish("$o{cache}/$key.$kind", bytes($path));
        my $old_meta = $path =~ s/\.(?:consensus\.fasta|pool\.tsv)\z/.meta/r;
        my $meta_owner = legacy_key($o{sample} // '', $old_display, $old_meta);
        my %old = defined($meta_owner) && $meta_owner eq $key ? metadata($old_meta) : ();
        my ($count, $candidate) = split /\t/, $old{candidate} // "0\tNA", -1;
        publish("$o{cache}/$key.meta", meta_text(\@row, $count, $candidate, "$o{cache}/$key.consensus.fasta"));
        $valid{"$key\t$kind"} = 1;
        warn "WARN: consensus identity: reused proven legacy cache $old_display as $key\n";
    }
    publish($o{out}, join('', map { "$_\n" } sort keys %valid));
} elsif ($command eq 'plan') {
    my %map = load_map($o{map}); my (%state, %locked, %reverse, %processed);
    my %valid = map { $_ => 1 } lines($o{valid});
    for my $d (sort keys %map) { $reverse{$map{$d}[1]} //= $d; }
    for my $line (lines($o{'lock-state'})) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed lock state') unless @f == 3 && $f[1] =~ /\A[0-9]+\z/ && $f[2] =~ /\A[01]\z/;
        my $k = stable($f[0]);
        fail("conflicting lock rows for $k") if exists($state{$k}) && join("\t", @{$state{$k}}) ne join("\t", @f[1,2]);
        $state{$k} = [@f[1,2]];
    }
    $locked{stable($_)} = 1 for grep { length } lines($o{'locked-keys'});
    my @out;
    for my $d (lines($o{input})) {
        next if $d eq '';
        my $r = $map{$d} or fail("missing current identity for $d");
        my $k = $r->[1]; $processed{$k} = 1; $reverse{$k} = $d;
        push @out, join("\t", $d, @{$state{$k} // [0,0]}, $locked{$k} ? 1 : 0, $k, $valid{"$k\tconsensus.fasta"} ? 1 : 0,
                        $r->[2], $r->[5] // $r->[0])."\n";
    }
    my @carry;
    for my $k (sort keys %locked) {
        next if $processed{$k};
        if (!exists($reverse{$k})) { warn "WARN: consensus identity: ignored lock without current representative evidence: $k\n"; next; }
        push @carry, join("\t", $reverse{$k}, @{$state{$k} // [0,0]}, $k, $valid{"$k\tconsensus.fasta"} ? 1 : 0)."\n";
    }
    publish($o{out}, join('', @out));
    publish($o{carry}, join('', @carry));
} elsif ($command eq 'publish-keys') {
    my @rows = lines($o{input});
    my $col = ($o{column} // 1) - 1;
    for my $line (@rows) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        stable($f[@f == 1 ? 0 : $col] // '');
    }
    if (-f $o{out} && grep { /(?:\A|\t)OTUB_/ } lines($o{out})) {
        my $old = bytes($o{out}); my $backup = $o{out}.'.legacy.'.sha256_hex($old);
        if (-e $backup) { fail('conflicting legacy preservation copy') unless bytes($backup) eq $old; }
        else { publish($backup, $old); }
        warn 'WARN: consensus identity: preserved legacy authority in '.basename($backup)."\n";
    }
    publish($o{out}, join('', map { "$_\n" } @rows));
} elsif ($command eq 'project-cache') {
    my %map = load_map($o{map}); my %reverse;
    for my $d (sort keys %map) { $reverse{$map{$d}[1]} //= $map{$d}; }
    my (%seen, @out);
    for my $path (lines($o{input})) {
        next unless -s $path;
        my $key = basename($path); $key =~ s/\.consensus\.fasta\z//;
        $key = stable($key);
        next if $seen{$key}++;
        my $r = $reverse{$key} or fail("cached owner missing current display: $key");
        my %meta = metadata("$o{cache}/$key.meta");
        fail('cached ownership changed during round') unless ($meta{stable_otu_key} // '') eq $key;
        my $old_public = $meta{public_display_key} // $meta{display_otu_key};
        my $public = $r->[5] // $r->[0];
        my ($number) = $public =~ /\A(OTUB_[0-9]+)/;
        my $text = bytes($path);
        $text =~ s{^>([^\r\n]+)}{
            my @t = split /\|/, $1, -1; my $has_otu = 0;
            for my $t (@t) {
                if ($t =~ /\AOTUB_[0-9]+\z/) { $t = $number; $has_otu = 1; }
                elsif ($t =~ /\AOTU=/) { $t = "OTU=$public"; $has_otu = 1; }
                elsif ($t =~ /\ARTBIOSCAN_INTERNAL_OTU=/) { $t = "RTBIOSCAN_INTERNAL_OTU=$r->[0]"; }
            }
            if (!$has_otu) {
                my ($reads) = grep { /\Areads-[0-9]+\z/ } @t;
                @t = ($t[0], $number, $r->[4], $reads // 'reads-0');
            }
            '>'.join('|', @t);
        }gme;
        push @out, $text;
    }
    publish($o{out}, join('', @out));
} elsif ($command eq 'owners') {
    my %map = load_map($o{map}); my %prior = owners($o{prior}); my %next = %prior;
    my %drop = dropped(); my (%emitted, %ids, %records, %eligible, %public, %prior_record);
    my $previous_text = defined($o{'previous-ids'}) && -f $o{'previous-ids'} ? bytes($o{'previous-ids'}) : '';
    my @previous_lines = split /(?<=\n)/, $previous_text;
    my %previous_ids = map { my $id = $_; $id =~ s/\A\s+|\s+\z//g; $id =~ s/\A>//; $id => 1 } @previous_lines;
    for my $key (keys %prior) {
        for my $entry (@{$prior{$key}[3]}) {
            my $header = $entry->[1]; $header =~ s/\A>//;
            $prior_record{$key."\t".report_id($entry->[0])} = 1
                if $previous_ids{$entry->[0]} || $previous_ids{$header};
        }
    }
    for my $key (keys %next) { delete $next{$key} if $drop{$key} || $drop{$next{$key}[1]}; }
    for my $line (lines($o{input})) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed emitted ownership record') unless @f == 6 && $f[4] =~ /\A[01]\z/ && $f[5] =~ /\A[01]\z/;
        if (!exists $map{$f[1]}) {
            my %public;
            for my $token (split /\|/, $f[3], -1) {
                $public{$1} = 1 if $token =~ /\AOTU=(.+)\z/;
            }
            fail('ambiguous emitted display identity') unless keys(%public) == 1;
            ($f[1]) = keys %public;
        }
        my $r = $map{$f[1]} or fail("emitted display lacks current representative identity: $f[1]");
        my $key = "$f[0]\t$r->[1]";
        my $public_id = public_record($f[0], $r->[1], $r->[5] // $r->[0], @f[2,3]);
        fail('duplicate or conflicting emitted public ownership') if exists $public{$public_id};
        $public{$public_id} = $key;
        delete $next{$key} unless $emitted{$key}++;
        next if $drop{$key} || $drop{$r->[1]};
        $records{$key} //= [$f[0], $r->[1], $f[1], []];
        push @{$records{$key}[3]}, [@f[2,3]];
        next unless $f[4] == 1 || ($f[5] == 0 && $prior_record{"$key\t$public_id"});
        $eligible{$key} = 1;
        $ids{$f[2]} = 1; $ids{$f[3]} = 1;
    }
    # Retire historical public aliases occupied by a different current owner.
    # Stable cache, lock and consolidated-key history remain independently keyed.
    for my $key (keys %next) {
        my @entries = grep { !exists($public{report_id($_->[0])}) || $public{report_id($_->[0])} eq $key } @{$next{$key}[3]};
        if (@entries) { $next{$key}[3] = \@entries; } else { delete $next{$key}; }
    }
    # Noncolliding historical IDs retain the existing zero/no-new-emission policy.
    # A collision can only re-enter through the validated current decision above.
    my @projected;
    for my $line (@previous_lines) {
        my $id = $line; $id =~ s/\A\s+|\s+\z//g; $id =~ s/\A>//;
        if ($id =~ /\|/) { my @t = split /\|/, $id, -1; $id = "$t[1]_$t[0]"; }
        push @projected, $line unless exists $public{report_id($id)};
    }
    # Keep every associated current record for the existing all-consolidated
    # SUP rule, while public consolidated IDs retain their per-record decision.
    $next{$_} = $records{$_} for keys %eligible;
    my @out;
    for my $key (sort keys %next) {
        my $r = $next{$key}; my %seen;
        my @entries = sort { $a->[0] cmp $b->[0] || $a->[1] cmp $b->[1] }
            grep { !$seen{join("\t", @$_)}++ } @{$r->[3]};
        push @out, join("\t", @{$r}[0..2], encode_json(\@entries))."\n";
    }
    # Both products are fully validated before either complete file is published.
    publish($o{out}, join('', @out));
    publish($o{ids}, join('', map { "$_\n" } sort keys %ids));
    publish($o{'projected-ids'}, join('', @projected)) if defined $o{'projected-ids'};
} elsif ($command eq 'filter-ids') {
    my %prior = owners($o{prior}); my %drop = dropped(); my (%remove, %keep);
    for my $key (keys %prior) {
        my $r = $prior{$key};
        my $table = $drop{$key} || $drop{$r->[1]} ? \%remove : \%keep;
        $table->{$_} = 1 for map { @$_ } @{$r->[3]};
    }
    fail('ambiguous public consensus ID ownership during filtering') if grep { $keep{$_} } keys %remove;
    publish($o{out}, join('', map { "$_\n" } grep { !$remove{$_} } lines($o{input})));
} elsif ($command eq 'drop-keys') {
    my %drop = dropped(); my @out;
    for my $line (lines($o{input})) {
        next if $line eq '';
        my @f = split /\t/, $line, -1;
        fail('malformed consolidated stable key') unless @f == 1 || @f == 2;
        my $key = stable($f[-1]);
        next if $drop{$key} || (@f == 2 && $drop{"$f[0]\t$key"});
        push @out, "$line\n";
    }
    publish($o{out}, join('', @out));
} else {
    fail("unknown command '$command'");
}
