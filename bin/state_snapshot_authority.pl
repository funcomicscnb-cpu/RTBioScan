#!/usr/bin/env perl
# state_snapshot_authority.pl — completeness authority for the restore snapshot
# of the authoritative rolling `_state` (R5-F01).
#
# backup_update_and_clean copies the authoritative `_state` files into
# `<root>/state_authority/` of each snapshot root and seals that copy with an
# internal record, `<root>/state_authority/AUTHORITY`:
#
#   #RTB-STATE-AUTHORITY<TAB>1
#   member<TAB><name><TAB><bytes><TAB><sha256>      (one per member, names ascending)
#   #END<TAB><member count><TAB><sha256 of every preceding byte>
#
# A root's record is removed before any of its members change and is renamed
# into place only after every member was written and re-verified, so an
# interrupted backup leaves no record rather than an accepted mixed copy.
# restart_handler.sh verifies a root read-only before its first mutation and
# installs the members into `_state` last, so no legacy per-round copy
# (sequences/qced_reads_hq_accumulated.fasta, tables/round_index.tsv) can
# override them.
#
# Usage:
#   state_snapshot_authority.pl publish <state_dir> <snapshot_root>...
#   state_snapshot_authority.pl verify  <snapshot_root>
#       exit 0: valid authority; 3: no completed-round state (pristine);
#       2: completed-round state without valid authority (refuse restore)
#   state_snapshot_authority.pl install <snapshot_root> <state_dir>
use strict;
use warnings;
use Digest::SHA;
use Fcntl qw(:flock O_WRONLY O_CREAT O_EXCL);

my $DIR_NAME = 'state_authority';
my $RECORD = 'AUTHORITY';
my $LOCK = '.lock';
my $HEADER = "#RTB-STATE-AUTHORITY\t1\n";
my $CHUNK = 1 << 20;

# The authoritative inventory: the restored rolling state that later rounds
# read, and the completed-round ledger. Derived scratch state rebuilt before
# use (*_otu_nr_hash_map.tsv, qced_reads_nr.fasta.clstr) is not bound.
my %EXACT = map { $_ => 1 } qw(
    done_pod5.txt
    qced_reads_hq_accumulated.fasta
    round_index.tsv
    read_qscore_rolling.tsv
    otu_frozen_reps.fasta
    otu_frozen_reps.fasta.gz
    otu_active_pool.fasta
    otu_seen_hashes.tsv
    otu_consolidated_keys.tsv
    state_compatibility_manifest.tsv
);
my @SUFFIX = qw(
    _assigned_read_ids_ever.list
    _protected_read_ids_ever.list
    _assigned_otu_keys_ever.list
    _pruned_barrier.list
    _pruned_archive.fasta
    _otu_size_streak.tsv
    _otu_unassigned_streak.tsv
    consensus_consolidated_ids.txt
    _seen_read_ids.tsv
    _on_target_state.tsv
);

sub governed {
    my ($name) = @_;
    return 0 if $name =~ /^\./;
    return 1 if $EXACT{$name};
    return 1 if $name =~ /^otu_frozen_.*\.tsv\z/s;
    for my $s (@SUFFIX) {
        return 1 if length($name) >= length($s) && substr($name, -length($s)) eq $s;
    }
    return 0;
}

sub safe_name { return $_[0] =~ /\A[A-Za-z0-9_][A-Za-z0-9._-]*\z/ && $_[0] ne $RECORD }

sub list_dir {
    my ($dir) = @_;
    opendir(my $dh, $dir) or die "cannot read directory $dir: $!\n";
    my @names = sort grep { $_ ne '.' && $_ ne '..' } readdir($dh);
    closedir($dh);
    return @names;
}

sub regular { my ($p) = @_; return !-l $p && -f _ }

# Copy src to every dest (fresh temps renamed into place), hashing the bytes.
sub copy_hashed {
    my ($src, $mode, @dests) = @_;
    open(my $in, '<:raw', $src) or die "cannot open $src: $!\n";
    my @st = stat($in) or die "cannot stat $src: $!\n";
    my (@outs, @tmps);
    for my $d (@dests) {
        my $tmp = "$d.tmp.$$";
        unlink($tmp);
        sysopen(my $out, $tmp, O_WRONLY | O_CREAT | O_EXCL, 0600) or die "cannot create $tmp: $!\n";
        binmode($out);
        push @outs, $out;
        push @tmps, $tmp;
    }
    my $sha = Digest::SHA->new(256);
    my $bytes = 0;
    while (1) {
        my $n = sysread($in, my $buf, $CHUNK);
        die "cannot read $src: $!\n" if !defined $n;
        last if $n == 0;
        $sha->add($buf);
        $bytes += $n;
        for my $out (@outs) {
            my $off = 0;
            while ($off < $n) {
                my $w = syswrite($out, $buf, $n - $off, $off);
                die "cannot write $src copy: $!\n" if !defined $w;
                $off += $w;
            }
        }
    }
    close($in);
    die "$src changed while it was copied\n" if $bytes != $st[7];
    for my $i (0 .. $#outs) {
        close($outs[$i]) or die "cannot close $tmps[$i]: $!\n";
        chmod($mode, $tmps[$i]) or die "cannot chmod $tmps[$i]: $!\n";
        rename($tmps[$i], $dests[$i]) or die "cannot install $dests[$i]: $!\n";
    }
    return ($bytes, $sha->hexdigest);
}

sub file_sha {
    my ($path) = @_;
    open(my $in, '<:raw', $path) or return undef;
    my $sha = Digest::SHA->new(256);
    $sha->addfile($in);
    close($in);
    return $sha->hexdigest;
}

sub render_record {
    my (@members) = @_;
    my $body = $HEADER;
    $body .= "member\t$_->[0]\t$_->[1]\t$_->[2]\n" for @members;
    return $body . "#END\t" . scalar(@members) . "\t" . Digest::SHA::sha256_hex($body) . "\n";
}

# Strict parse; returns ([name, bytes, sha]...) or dies with the reason.
sub parse_record {
    my ($text) = @_;
    die "record is not terminated by a newline\n" if $text !~ /\n\z/;
    my @lines = split /(?<=\n)/, $text;
    die "unknown record header\n" if !@lines || shift(@lines) ne $HEADER;
    my $end = pop(@lines) // '';
    my ($count, $digest) = $end =~ /\A#END\t(0|[1-9][0-9]*)\t([0-9a-f]{64})\n\z/
        or die "record end line is missing or malformed\n";
    my (@members, %seen, $prev);
    for my $line (@lines) {
        my ($name, $bytes, $sha) = $line =~ /\Amember\t([^\t\n]+)\t(0|[1-9][0-9]*)\t([0-9a-f]{64})\n\z/
            or die "malformed record line\n";
        die "unsafe member name\n" if !safe_name($name);
        die "member outside the authoritative inventory: $name\n" if !governed($name);
        die "member names are duplicated or out of order\n" if defined $prev && $name le $prev;
        $prev = $name;
        push @members, [$name, $bytes, $sha];
    }
    die "record member count mismatch\n" if $count != @members;
    my $body = substr($text, 0, length($text) - length($end));
    die "record digest mismatch\n" if Digest::SHA::sha256_hex($body) ne $digest;
    die "record does not bind the completed-round ledger done_pod5.txt\n"
        if !grep { $_->[0] eq 'done_pod5.txt' && $_->[1] > 0 } @members;
    return @members;
}

sub read_file {
    my ($path) = @_;
    open(my $in, '<:raw', $path) or return undef;
    local $/;
    my $t = <$in>;
    close($in);
    return $t // '';
}

# Verify one root's authority directory; returns members or dies.
sub verify_dir {
    my ($dir) = @_;
    die "no authority directory\n" if -l $dir || !-d $dir;
    my $rec = "$dir/$RECORD";
    die "no completeness record\n" if !-e $rec && !-l $rec;
    die "completeness record is not a regular file\n" if !regular($rec);
    my $text = read_file($rec);
    die "cannot read completeness record\n" if !defined $text;
    return check_dir($dir, $text, $RECORD);
}

# Every member of the record, and nothing else, is present with its bytes.
sub check_dir {
    my ($dir, $text, @ignore) = @_;
    my @members = parse_record($text);
    my %want = map { $_->[0] => $_ } @members;
    my %skip = map { $_ => 1 } ($LOCK, @ignore);
    for my $name (list_dir($dir)) {
        next if $skip{$name};
        die "unexpected entry in authority directory: $name\n" if !$want{$name};
    }
    for my $m (@members) {
        my $p = "$dir/$m->[0]";
        die "member missing: $m->[0]\n" if !-e $p && !-l $p;
        die "member is not a regular file: $m->[0]\n" if !regular($p);
        die "member size differs: $m->[0]\n" if -s _ != $m->[1];
        my $sha = file_sha($p);
        die "member digest differs: $m->[0]\n" if !defined $sha || $sha ne $m->[2];
    }
    return @members;
}

# Completed-round state in a snapshot root, of any generation.
sub has_residue {
    my ($root) = @_;
    return 1 if -e "$root/$DIR_NAME" || -l "$root/$DIR_NAME";
    return 1 if -s "$root/done_pod5.txt";
    for my $d ($root, "$root/tables", "$root/sequences") {
        next if !-d $d;
        for my $name (list_dir($d)) {
            next if $name eq 'done_pod5.txt';
            return 1 if governed($name);
        }
    }
    return 0;
}

sub lock_root {
    my ($dir) = @_;
    open(my $fh, '>>', "$dir/$LOCK") or die "cannot open $dir/$LOCK: $!\n";
    flock($fh, LOCK_EX) or die "cannot lock $dir/$LOCK: $!\n";
    return $fh;
}

sub cmd_publish {
    my ($state, @roots) = @_;
    die "usage: publish <state_dir> <snapshot_root>...\n" if !defined $state || !@roots;
    my @dirs = map { "$_/$DIR_NAME" } @roots;
    for my $d (@dirs) {
        die "unsafe authority directory $d\n" if -l $d;
        if (!-d $d) { mkdir($d) or die "cannot create $d: $!\n"; }
    }
    my @locks = map { lock_root($_) } @dirs;
    # Invalidate every root before any member changes.
    for my $d (@dirs) {
        unlink("$d/$RECORD") or $!{ENOENT} or die "cannot invalidate $d/$RECORD: $!\n";
    }
    my @names;
    for my $name (list_dir($state)) {
        next if !governed($name);
        die "unsafe authoritative state name: $name\n" if !safe_name($name);
        die "authoritative state entry is not a regular file: $name\n" if !regular("$state/$name");
        push @names, $name;
    }
    die "no completed-round ledger in $state\n" if !-s "$state/done_pod5.txt";
    my %keep = map { $_ => 1 } @names;
    for my $d (@dirs) {
        for my $name (list_dir($d)) {
            next if $name eq $LOCK || $keep{$name};
            my $p = "$d/$name";
            die "unexpected directory in $d: $name\n" if -d $p && !-l $p;
            unlink($p) or die "cannot remove stale $p: $!\n";
        }
    }
    my @members;
    for my $name (@names) {
        my $mode = (stat("$state/$name"))[2] & 07777;
        my ($bytes, $sha) = copy_hashed("$state/$name", $mode, map { "$_/$name" } @dirs);
        push @members, [$name, $bytes, $sha];
    }
    my $record = render_record(@members);
    # Every member of every root is re-verified against the record before any
    # root's record is published.
    for my $d (@dirs) {
        eval { check_dir($d, $record); 1 } or die "snapshot $d failed verification: $@";
    }
    for my $d (@dirs) {
        my $tmp = "$d/.$RECORD.tmp.$$";
        open(my $out, '>:raw', $tmp) or die "cannot write $tmp: $!\n";
        print {$out} $record or die "cannot write $tmp: $!\n";
        close($out) or die "cannot write $tmp: $!\n";
        rename($tmp, "$d/$RECORD") or die "cannot publish $d/$RECORD: $!\n";
    }
    close($_) for @locks;
    return 0;
}

sub cmd_verify {
    my ($root) = @_;
    die "usage: verify <snapshot_root>\n" if !defined $root;
    return 3 if !-d $root || !has_residue($root);
    my @members = eval {
        my @sealed = verify_dir("$root/$DIR_NAME");
        my $ledger = "$root/done_pod5.txt";
        if (-e $ledger || -l $ledger) {
            my ($bound) = grep { $_->[0] eq 'done_pod5.txt' } @sealed;
            die "stale completion-ledger mismatch: snapshot root done_pod5.txt differs from sealed member\n"
                if !regular($ledger) || -s _ != $bound->[1]
                    || (file_sha($ledger) // '') ne $bound->[2];
        }
        @sealed;
    };
    if ($@) {
        print STDERR "ERROR: snapshot $root: $@";
        return 2;
    }
    return 0;
}

sub cmd_install {
    my ($root, $state) = @_;
    die "usage: install <snapshot_root> <state_dir>\n" if !defined $state;
    my $dir = "$root/$DIR_NAME";
    my @members = verify_dir($dir);
    my %bound = map { $_->[0] => 1 } @members;
    # No per-round or compatibility copy of a governed name survives: the
    # authoritative namespace of `_state` is exactly the record.
    for my $name (list_dir($state)) {
        next if $bound{$name} || !governed($name);
        my $p = "$state/$name";
        next if -d $p && !-l $p;
        unlink($p) or die "cannot remove $p: $!\n";
    }
    for my $m (@members) {
        my $src = "$dir/$m->[0]";
        my $mode = (stat($src))[2] & 07777;
        my ($bytes, $sha) = copy_hashed($src, $mode, "$state/$m->[0]");
        die "member changed during restore: $m->[0]\n" if $bytes != $m->[1] || $sha ne $m->[2];
    }
    return 0;
}

my $cmd = shift(@ARGV) // '';
my $rc = eval {
    $cmd eq 'publish' ? cmd_publish(@ARGV)
        : $cmd eq 'verify' ? cmd_verify(@ARGV)
        : $cmd eq 'install' ? cmd_install(@ARGV)
        : die "usage: $0 publish|verify|install ...\n";
};
if (!defined $rc) {
    print STDERR "ERROR: state_snapshot_authority $cmd: $@";
    exit 1;
}
exit $rc;
