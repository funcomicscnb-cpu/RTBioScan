#!/usr/bin/env bash
set -euo pipefail

mode=update
case "$#" in
  3)
    case "$1" in --*) echo "ERROR: unknown run index mode: $1" >&2; exit 2 ;; esac
    RUN_JSON="$1"
    RUN_INDEX_JSONL="$2"
    LOCK_PATH="$3"
    if [ ! -s "$RUN_JSON" ]; then
      echo "ERROR: run report missing or empty: $RUN_JSON" >&2
      exit 2
    fi
    ;;
  4)
    case "$1" in
      --prune-seeded) mode=prune ;;
      --clean-filter) mode=clean ;;
      *) echo "ERROR: unknown run index mode: $1" >&2; exit 2 ;;
    esac
    RUN_JSON="$2" # run id in maintenance modes
    RUN_INDEX_JSONL="$3"
    LOCK_PATH="$4"
    if [ -z "$RUN_JSON" ] || [ ! -f "$RUN_INDEX_JSONL" ]; then
      echo "ERROR: incomplete run index maintenance request" >&2
      exit 2
    fi
    ;;
  *)
    echo "Usage: $0 <run_report.json> <runs_index.jsonl> <lock_path>" >&2
    echo "       $0 --prune-seeded|--clean-filter <run_id> <runs_index.jsonl> <lock_path>" >&2
    exit 2
    ;;
esac
[ -n "$RUN_INDEX_JSONL" ] && [ -n "$LOCK_PATH" ] || exit 2
if [ "$mode" = update ]; then
  mkdir -p "$(dirname "$RUN_INDEX_JSONL")"
fi

perl -MJSON::PP -MFcntl=:DEFAULT,:flock,:mode,F_GETFD,F_SETFD,FD_CLOEXEC -MErrno=EAGAIN,EWOULDBLOCK,EINTR,EEXIST -MFile::Temp=tempfile -MFile::Basename=dirname -e '
use strict;
use warnings;
my ($mode, $subject, $index, $base, $wait_limit) = @ARGV;
$wait_limit = 300 if !defined($wait_limit) || $wait_limit eq "";
die "invalid LOCK_WAIT\n" unless $wait_limit =~ /\A[0-9]+\z/;
my $stable = "$base.flock";
my $fence = "$base.lockdir";
my $waited = 0;

sub fail { print STDERR "ERROR: $_[0]\n"; exit 2 }
sub identity {
    my ($fh, $path) = @_;
    my @fd = stat($fh);
    my @name = lstat($path);
    return @fd && @name && S_ISREG($fd[2]) && S_ISREG($name[2])
        && $fd[3] == 1 && $name[3] == 1 && $fd[7] == 0 && $name[7] == 0
        && $fd[0] == $name[0] && $fd[1] == $name[1];
}
sub tick {
    my ($reason) = @_;
    sleep 1;
    $waited++;
    if ($waited >= $wait_limit) {
        print STDERR "ERROR: failed to acquire run index lock: $base\n";
        if ($reason eq "legacy") {
            my $quoted = $fence;
            $quoted =~ s/'"'"'/'"'"'"'"'"'"'"'"'/g;
            print STDERR "rmdir '\''$quoted'\''\n";
        }
        exit 2;
    }
}
my @initial = lstat($stable);
fail("unsafe run index stable lock: $stable")
    if @initial && (!S_ISREG($initial[2]) || $initial[3] != 1 || $initial[7] != 0);
sysopen(my $lock, $stable, O_RDWR | O_CREAT | O_NOFOLLOW, 0600)
    or fail("cannot open run index stable lock: $stable: $!");
identity($lock, $stable) or fail("run index stable lock identity changed: $stable");
while (!flock($lock, LOCK_EX | LOCK_NB)) {
    fail("run index flock unavailable: $stable: $!")
        unless $! == EAGAIN || $! == EWOULDBLOCK || $! == EINTR;
    tick("kernel");
}
identity($lock, $stable) or fail("run index stable lock identity changed: $stable");

# Probe an independent open file description in a separate process.
my $probe = fork();
defined($probe) or fail("cannot fork run index flock probe: $!");
if ($probe == 0) {
    sysopen(my $other, $stable, O_RDWR | O_NOFOLLOW) or exit 1;
    identity($other, $stable) or exit 1;
    my $acquired = flock($other, LOCK_EX | LOCK_NB);
    exit($acquired ? 1 : (($! == EAGAIN || $! == EWOULDBLOCK) ? 0 : 1));
}
waitpid($probe, 0);
$? == 0 or fail("run index flock conformance check failed: $stable");
identity($lock, $stable) or fail("run index stable lock identity changed: $stable");

# A directory belongs to a possible legacy writer. A regular file is an
# upgraded fence and is reclaimed only while the stable kernel lock is held.
my $fence_fh;
while (1) {
    my @node = lstat($fence);
    if (@node) {
        if (S_ISDIR($node[2])) { tick("legacy"); next }
        fail("unsafe run index compatibility fence: $fence")
            unless S_ISREG($node[2]) && $node[3] == 1;
        unlink($fence) or fail("cannot reclaim run index compatibility fence: $fence: $!");
    }
    if (sysopen($fence_fh, $fence, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600)) {
        last;
    }
    next if $! == EEXIST;
    fail("cannot create run index compatibility fence: $fence: $!");
}
identity($lock, $stable) or fail("run index stable lock identity changed: $stable");
my @fence_id = stat($fence_fh);
my @fence_path = lstat($fence);
fail("run index compatibility fence changed: $fence")
    unless @fence_id && @fence_path && S_ISREG($fence_path[2])
        && $fence_id[0] == $fence_path[0] && $fence_id[1] == $fence_path[1];

my ($out, $tmp);
if ($mode eq "clean") {
    my $index_dir = dirname($index);
    -d $index_dir or fail("run index directory missing: $index_dir");
    ($out, $tmp) = tempfile("$index.tmp.XXXXXX", UNLINK => 0);
} else {
    ($out, $tmp) = tempfile("$index.tmp.XXXXXX", UNLINK => 0);
}
my $ok = eval {
    if ($mode eq "update") {
        open my $R, "<", $subject or die "open run json: $!";
        my $line = <$R>;
        close $R;
        die "empty run json\n" if !defined($line) || $line =~ /^\s*$/;
        my $new = decode_json($line);
        die "run json missing run_id\n" if !defined($new->{run_id}) || $new->{run_id} eq "";
        my $new_key = $new->{run_id};

        if (-s $index) {
            open my $IN, "<", $index or die "open index: $!";
            while (my $l = <$IN>) {
                chomp $l;
                if ($l =~ /^\s*$/) { next }
                my $row = eval { decode_json($l) };
                if ($@ || !defined $row || ref($row) ne "HASH") {
                    print {$out} $l, "\n";
                    next;
                }
                my $rk = defined($row->{run_id}) ? $row->{run_id} : "";
                next if $rk eq $new_key;
                print {$out} $l, "\n";
            }
            close $IN;
        }
        print {$out} encode_json($new), "\n";
    } else {
        my $flags = fcntl($lock, F_GETFD, 0);
        defined($flags) or die "get run index descriptor: $!";
        fcntl($lock, F_SETFD, $flags & ~FD_CLOEXEC)
            or die "inherit run index descriptor: $!";
        my $pid = fork();
        defined($pid) or die "fork run index filter: $!";
        if ($pid == 0) {
            open(STDOUT, ">&", $out) or die "redirect run index filter: $!";
            exec "python3", "-c", q{import sys, json
rid, path = sys.argv[1], sys.argv[2]
for line in open(path, encoding="utf-8"):
    try:
        if json.loads(line).get("run_id") != rid:
            sys.stdout.write(line)
    except Exception:
        sys.stdout.write(line)
}, $subject, $index;
            die "exec run index filter: $!";
        }
        waitpid($pid, 0);
        die "run index filter failed\n" if $? != 0;
    }
    close $out or die "close run index temporary file: $!";
    identity($lock, $stable) or die "run index stable lock identity changed: $stable\n";
    my @now = lstat($fence);
    die "run index compatibility fence changed: $fence\n"
        unless @now && S_ISREG($now[2]) && $now[0] == $fence_id[0]
            && $now[1] == $fence_id[1];
    rename($tmp, $index) or die "replace run index: $!";
    1;
};
if (!$ok) {
    print STDERR $@ if $@;
    unlink($tmp);
    exit 2;
}
my @last = lstat($fence);
if (@last && S_ISREG($last[2]) && $last[0] == $fence_id[0]
        && $last[1] == $fence_id[1]) {
    unlink($fence) or fail("cannot release run index compatibility fence: $fence: $!");
}
exit 0;
' "$mode" "$RUN_JSON" "$RUN_INDEX_JSONL" "$LOCK_PATH" "${LOCK_WAIT:-300}"
