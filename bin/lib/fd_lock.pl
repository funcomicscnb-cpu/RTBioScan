#!/usr/bin/env perl
use strict;
use warnings;
use Fcntl qw(:DEFAULT :flock :mode);
use POSIX qw(_exit);
use Sys::Hostname qw(hostname);
use Time::HiRes qw(time sleep);
use Errno qw(EAGAIN EWOULDBLOCK ESRCH EEXIST);
use File::Basename qw(dirname basename);
use File::Spec;

# The task shell owns the open file description. This process only locks its
# inherited descriptor; the detached drain process opens a DIFFERENT one.
my $VERSION = 'rtbioscan-lock-host-v2';
my $OLD_VERSION = 'rtbioscan-lock-host-v1';
my $BINDING = '.rtbioscan_lock_host_v1';
my $GUARD = '.rtbioscan_lock_host_v1.guard';
my $BARRIER = '.rtbioscan_state_reset.flock';
my $FENCE_VERSION = 'rtbioscan-fence-v2';
my $POLL = 0.2;

sub diagnostic {
    my ($s) = @_;
    $s = '' unless defined $s;
    $s =~ s/[\x00-\x1f\x7f]/?/g;
    return substr($s, 0, 4096);
}
sub fail {
    my ($code, $message) = @_;
    print STDERR diagnostic($message), "\n";
    exit $code;
}
sub qsh {
    my ($s) = @_;
    $s =~ s/'/'\\''/g;
    return "'$s'";
}
sub path_arg {
    my ($path) = @_;
    fail(70, 'invalid empty path') unless defined($path) && length($path);
    fail(70, 'unsafe lock path') if length($path) > 512 || $path =~ /[\x00-\x1f\x7f]/ || $path =~ m{(?:^|/)\.\.(?:/|$)};
    return File::Spec->rel2abs($path);
}
sub root_identity {
    my ($dir) = @_;
    my @st = lstat($dir);
    fail(72, "unsafe or replaced state root $dir")
        unless @st && S_ISDIR($st[2]);
    return "$st[0]:$st[1]";
}
sub host_hex {
    my $name = hostname();
    $name = 'unavailable' unless defined($name) && length($name);
    $name =~ s/[\x00-\x1f\x7f]/?/g;
    $name = substr($name, 0, 64);
    return unpack('H*', $name);
}
sub valid_identity {
    my ($identity) = @_;
    return $identity =~ /^macos:IOPlatformUUID:([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\z/
        && $1 =~ /[1-9a-f]/
        || $identity =~ /^linux:machine-id:([0-9a-f]{32})\z/ && $1 =~ /[1-9a-f]/;
}
sub machine_identity {
    my $platform = $^O;
    if ($platform eq 'darwin') {
        open(my $fh, '-|', '/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice', '-k', 'IOPlatformUUID')
            or fail(74, 'cannot run IOPlatformUUID provider');
        local $/;
        my $output = <$fh>;
        my $ok = close($fh);
        fail(74, 'IOPlatformUUID provider failed or was terminated') unless $ok && defined($output) && length($output) <= 65536;
        my @rows = split /\n/, $output;
        pop @rows while @rows && $rows[-1] =~ /^\s*\z/;
        fail(74, 'unexpected IOPlatformUUID provider output')
            unless @rows >= 3
                && (shift @rows) =~ /^\+-o [^\n]+<class IOPlatformExpertDevice,[^\n]*>\s*\z/
                && (shift @rows) =~ /^\s*\{\s*\z/
                && (pop @rows) =~ /^\s*\}\s*\z/;
        for my $row (@rows) {
            fail(74, 'unexpected IOPlatformUUID provider output')
                unless $row =~ /^\s+"[^"\n]+" = .+\z/;
        }
        my @lines = grep { /IOPlatformUUID/ } @rows;
        fail(74, 'missing or ambiguous IOPlatformUUID') unless @lines == 1;
        fail(74, 'malformed IOPlatformUUID')
            unless $lines[0] =~ /^\s*"IOPlatformUUID" = "([0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})"\s*\z/;
        my $id = lc $1;
        fail(74, 'malformed IOPlatformUUID') unless $id =~ /[1-9a-f]/;
        return "macos:IOPlatformUUID:$id";
    }
    if ($platform eq 'linux') {
        my $data = exact_file('/etc/machine-id', 128);
        fail(74, 'missing or unreadable Linux machine-id') unless defined $data;
        fail(74, 'malformed Linux machine-id') unless $data =~ /^([0-9A-Fa-f]{32})\n?\z/;
        my $id = lc $1;
        fail(74, 'zero Linux machine-id') unless $id =~ /[1-9a-f]/;
        return 'linux:machine-id:' . $id;
    }
    fail(74, "unsupported platform for stable machine identity: $platform");
}
sub same_inode {
    my ($fh, $path) = @_;
    my @a = stat($fh);
    my @b = lstat($path);
    return @a && @b && S_ISREG($b[2]) && $b[3] <= 1
        && $a[0] == $b[0] && $a[1] == $b[1];
}
sub exact_file {
    my ($path, $max) = @_;
    my @before = lstat($path);
    return undef unless @before && S_ISREG($before[2]) && $before[7] <= $max;
    sysopen(my $fh, $path, O_RDONLY) or return undef;
    unless (same_inode($fh, $path)) { close $fh; return undef; }
    my $data = '';
    while (length($data) <= $max) {
        my $n = sysread($fh, my $piece, $max + 1 - length($data));
        unless (defined $n) { close $fh; return undef; }
        last if $n == 0;
        $data .= $piece;
    }
    close $fh;
    return length($data) <= $max ? $data : undef;
}
sub write_all {
    my ($fh, $data) = @_;
    while (length $data) {
        my $n = syswrite($fh, $data);
        return 0 unless defined($n) && $n > 0;
        substr($data, 0, $n, '');
    }
    return 1;
}
sub append_migration_audit {
    my ($dir, $line) = @_;
    my $path = "$dir/.lock_migration.log";
    my @before = lstat($path);
    fail(73, "unsafe migration audit $path") if @before && !S_ISREG($before[2]);
    sysopen(my $log, $path, O_WRONLY|O_CREAT|O_APPEND, 0600)
        or fail(73, "cannot open migration audit $path: $!");
    fail(73, "migration audit inode changed: $path") unless same_inode($log, $path);
    write_all($log, "$line\n") && close($log)
        or fail(73, "cannot append migration audit $path");
}
sub temp_file {
    my ($dir, $prefix) = @_;
    for my $counter (0..99) {
        my $path = "$dir/$prefix.$$.$counter";
        if (sysopen(my $fh, $path, O_WRONLY|O_CREAT|O_EXCL, 0600)) {
            return ($fh, $path);
        }
        next if $! == EEXIST;
        fail(73, "cannot create temporary lock metadata: $!");
    }
    fail(73, 'temporary lock metadata namespace exhausted');
}
sub open_guard {
    my ($dir, $mode) = @_;
    my $path = "$dir/$GUARD";
    my @before = lstat($path);
    fail(74, 'host initialization guard is unsafe') if @before && !S_ISREG($before[2]);
    sysopen(my $fh, $path, O_RDWR|O_CREAT, 0600) or fail(74, "cannot open host initialization guard: $!");
    fail(74, 'host initialization guard inode changed') unless same_inode($fh, $path);
    flock($fh, $mode) or fail(74, "cannot lock host initialization guard: $!");
    return $fh;
}
sub binding_record {
    my ($identity) = @_;
    fail(74, 'invalid stable machine identity') unless valid_identity($identity);
    my ($platform, $kind, $value) = split /:/, $identity, 3;
    return "$VERSION\t$platform\t$kind\t$value\t" . host_hex() . "\n";
}
sub read_binding {
    my ($path, $current, $allow_old) = @_;
    my $record = exact_file($path, 256);
    fail(74, "malformed or unreadable host binding $path") unless defined $record;
    if ($record =~ /^\Q$OLD_VERSION\E\t([0-9a-f]{2,128})\n\z/) {
        my $old = "hostname-v1:$1";
        return $old if $allow_old;
        my $cmd = 'perl ' . qsh($0) . ' rebind-host ' . qsh(dirname($path)) . ' ' . qsh($old) . ' ' . qsh($current) . ' --confirm';
        fail(74, "HOST_BINDING_LEGACY $path: hostname identity is untrusted; run: $cmd");
    }
    fail(74, "malformed or unsupported host binding $path")
        unless $record =~ /^\Q$VERSION\E\t([^\t\n]+)\t([^\t\n]+)\t([^\t\n]+)\t([0-9a-f]{2,128})\n\z/;
    my $identity = "$1:$2:$3";
    fail(74, "malformed or unsupported host binding $path") unless valid_identity($identity);
    return $identity;
}
sub ensure_binding {
    my ($dir, $current) = @_;
    my $path = "$dir/$BINDING";
    unless (lstat($path)) {
        fail(74, "cannot inspect host binding: $!") unless $!{ENOENT};
        my ($fh, $temp) = temp_file($dir, '.rtbioscan_lock_host_v1.new');
        my $ok = write_all($fh, binding_record($current)) && close($fh);
        unless ($ok) { unlink($temp); fail(74, 'cannot write host binding'); }
        unless (link($temp, $path) || $! == EEXIST) {
            my $error = "$!";
            unlink($temp);
            fail(74, "cannot initialize host binding atomically: $error");
        }
        unlink($temp) or fail(74, 'cannot clean host initialization metadata');
    }
    my $bound = read_binding($path, $current);
    if ($bound ne $current) {
        my $cmd = 'perl ' . qsh($0) . ' rebind-host ' . qsh($dir) . ' ' . qsh($bound) . ' ' . qsh($current) . ' --confirm';
        fail(74, "HOST_BINDING_MISMATCH $path: expected=$bound actual=$current; first ensure the old host is stopped or no longer writing, all prior pipeline/task writers are stopped, and state uses one host with coherent local kernel locks; run: $cmd");
    }
}
sub probe_conformance {
    my ($dir, $a) = @_;
    # The A0-preserved host guard is the probe inode. It is never unlinked or
    # replaced, including when two tasks probe concurrently.
    my $path = "$dir/$GUARD";
    my @initial = stat($a);
    fail(71, 'unsafe lock probe inode') unless same_inode($a, $path);
    pipe(my $b_read, my $a_write) or fail(71, 'cannot create lock-probe pipe');
    pipe(my $a_read, my $b_write) or fail(71, 'cannot create lock-probe pipe');
    my $pid = fork();
    unless (defined $pid) { fail(71, 'cannot fork independent lock probe'); }
    if ($pid == 0) {
        close $a; close $b_read; close $b_write;
        my $b;
        my $ok = sysopen($b, $path, O_RDWR) && same_inode($b, $path);
        if ($ok) {
            my $got = flock($b, LOCK_EX|LOCK_NB);
            $ok = !$got && ($! == EWOULDBLOCK || $! == EAGAIN);
        }
        write_all($a_write, $ok ? "BLOCKED\n" : "FAIL\n");
        my $go = <$a_read>;
        $ok = 0 unless defined($go) && $go eq "GO\n";
        my $deadline = time() + 3;
        if ($ok) {
            while (!(flock($b, LOCK_EX|LOCK_NB))) {
                if (($! != EWOULDBLOCK && $! != EAGAIN) || time() >= $deadline) { $ok = 0; last; }
                sleep 0.02;
            }
        }
        $ok = 0 unless $ok && same_inode($b, $path);
        write_all($a_write, $ok ? "ACQUIRED\n" : "FAIL\n");
        close $a_write;
        close $a_read;
        close $b if $b;
        _exit($ok ? 0 : 1);
    }
    close $a_write; close $a_read;
    my $result = eval {
        local $SIG{ALRM} = sub { die "lock probe timed out\n" };
        alarm 6;
        my $first = <$b_read>;
        die "independent lock probe accepted a second holder\n" unless defined($first) && $first eq "BLOCKED\n";
        die "lock probe inode changed\n" unless same_inode($a, $path);
        close $a;
        print {$b_write} "GO\n";
        close $b_write;
        my $second = <$b_read>;
        die "independent lock probe did not acquire after close\n" unless defined($second) && $second eq "ACQUIRED\n";
        alarm 0;
        1;
    };
    my $error = $@;
    alarm 0;
    close $a if defined(fileno($a));
    close $b_write if defined(fileno($b_write));
    close $b_read;
    unless ($result) { kill 9, $pid; }
    waitpid($pid, 0);
    my $child_ok = $? == 0;
    my @end = lstat($path);
    my $stable = @end && S_ISREG($end[2]) && $initial[0] == $end[0] && $initial[1] == $end[1];
    fail(71, "flock conformance probe failed: $error") unless $result && $child_ok && $stable;
}
sub stable_barrier {
    my ($dir) = @_;
    my $path = "$dir/$BARRIER";
    my @st = lstat($path);
    if (!@st) {
        fail(72, "cannot inspect reset barrier $path: $!") unless $!{ENOENT};
        if (sysopen(my $new, $path, O_RDWR|O_CREAT|O_EXCL, 0600)) {
            close $new or fail(72, "cannot initialize reset barrier $path");
        } else {
            fail(72, "cannot initialize reset barrier $path: $!") unless $! == EEXIST;
        }
        @st = lstat($path);
    }
    fail(72, "unsafe reset barrier $path")
        unless @st && S_ISREG($st[2]) && $st[3] <= 1 && $st[7] == 0;
    return $path;
}
sub stable_state_file {
    my ($path) = @_;
    my @st = lstat($path);
    if (!@st) {
        fail(72, "cannot inspect stable lock $path: $!") unless $!{ENOENT};
        if (sysopen(my $new, $path, O_RDWR|O_CREAT|O_EXCL, 0600)) {
            close $new or fail(72, "cannot initialize stable lock $path");
        } else {
            fail(72, "cannot initialize stable lock $path: $!") unless $! == EEXIST;
        }
        @st = lstat($path);
    }
    fail(72, "unsafe stable lock $path")
        unless @st && S_ISREG($st[2]) && $st[3] <= 1;
}
sub owner_record {
    my ($fh) = @_;
    sysseek($fh, 0, 0);
    my $n = sysread($fh, my $raw, 256);
    return 'unreadable' unless defined $n;
    return diagnostic($raw);
}
sub fence_info {
    my ($dir) = @_;
    my @st = lstat($dir);
    return ('absent', undef) unless @st;
    return ('corrupt', undef) unless S_ISDIR($st[2]);
    opendir(my $dh, $dir) or return ('corrupt', undef);
    my @names = sort grep { $_ ne '.' && $_ ne '..' } readdir($dh);
    closedir $dh;
    if (@names == 1 && $names[0] eq 'v2owner') {
        my $record = exact_file("$dir/v2owner", 128);
        return ('v2', $1) if defined($record) && $record =~ /^\Q$FENCE_VERSION\E\t([0-9a-f]{16})\n\z/;
        return ('corrupt', undef);
    }
    if (@names == 1 && $names[0] eq 'meta.env') {
        my $record = exact_file("$dir/meta.env", 256);
        return ('corrupt', undef) unless defined($record) && $record =~ /^pid=([1-9][0-9]*)\nhost=([^\n\x00-\x1f]+)\n(?:started_epoch=[0-9]+\n)?\z/;
        return ('legacy', [$1, $2]);
    }
    return ('ownerless', undef) if @names == 0;
    return ('corrupt', undef);
}
sub token {
    my $raw = '';
    open(my $urandom, '<', '/dev/urandom') or fail(73, 'cannot generate lock token');
    my $n = read($urandom, $raw, 8);
    close $urandom;
    fail(73, 'cannot generate lock token') unless defined($n) && $n == 8;
    return unpack('H*', $raw);
}
sub remove_fence_if_token {
    my ($dir, $expected, $dir_dev, $dir_ino, $owner_dev, $owner_ino) = @_;
    my @directory = defined($dir_dev) ? lstat($dir) : ();
    return 0 if defined($dir_dev) && (!@directory || !S_ISDIR($directory[2])
        || $directory[0] != $dir_dev || $directory[1] != $dir_ino);
    my ($kind, $found) = fence_info($dir);
    return 0 unless $kind eq 'v2' && $found eq $expected;
    my @before = lstat("$dir/v2owner");
    return 0 if defined($owner_dev) && (!@before || $before[0] != $owner_dev || $before[1] != $owner_ino);
    my $record = exact_file("$dir/v2owner", 128);
    return 0 unless defined($record) && $record eq "$FENCE_VERSION\t$expected\n";
    my @after = lstat("$dir/v2owner");
    my @directory_after = defined($dir_dev) ? lstat($dir) : ();
    return 0 unless @before && @after && $before[0] == $after[0] && $before[1] == $after[1]
        && (!defined($dir_dev) || (@directory_after && S_ISDIR($directory_after[2])
            && $directory_after[0] == $dir_dev && $directory_after[1] == $dir_ino));
    unlink("$dir/v2owner") or return 0;
    rmdir($dir) or return 0;
    return 1;
}
sub write_fence {
    my ($dir, $kind, $new_token) = @_;
    if ($kind eq 'absent') {
        return 0 unless mkdir($dir, 0700);
        my $path = "$dir/v2owner";
        my $fh;
        unless (sysopen($fh, $path, O_WRONLY|O_CREAT|O_EXCL, 0600)) { rmdir($dir); return 0; }
        my $ok = write_all($fh, "$FENCE_VERSION\t$new_token\n") && close($fh);
        unless ($ok) { unlink($path); rmdir($dir); return 0; }
        return 1;
    }
    return 0 unless $kind eq 'v2';
    my ($fh, $temp) = temp_file($dir, '.v2owner.new');
    my $ok = write_all($fh, "$FENCE_VERSION\t$new_token\n") && close($fh);
    unless ($ok) { unlink($temp); return 0; }
    unless (rename($temp, "$dir/v2owner")) { unlink($temp); return 0; }
    return 1;
}
sub legacy_unowned_message {
    my ($dir) = @_;
    my @st = lstat($dir);
    my $mtime = @st ? $st[9] : 'unknown';
    my $cmd = 'perl ' . qsh($0) . ' adopt-legacy ' . qsh($dir) . ' --confirm';
    return "LEGACY_LOCK_UNOWNED $dir: created $mtime; no owner record. Confirm that no pre-upgrade RTBioScan task uses this state, then run: $cmd";
}
sub classify_and_fence {
    my ($dir, $wait, $start, $new_token) = @_;
    while (1) {
        my ($kind, $data) = fence_info($dir);
        if ($kind eq 'absent' || $kind eq 'v2') {
            return 1 if write_fence($dir, $kind, $new_token);
            fail(73, "cannot create compatibility fence $dir") if $kind eq 'v2' || ($kind eq 'absent' && !-d $dir);
        } elsif ($kind eq 'legacy') {
            my ($pid, $host) = @$data;
            if ($host eq hostname() && !kill(0, $pid) && $! == ESRCH) {
                my @before = lstat("$dir/meta.env");
                my @after = lstat("$dir/meta.env");
                if (@before && @after && $before[0] == $after[0] && $before[1] == $after[1]) {
                    append_migration_audit(dirname($dir),
                        'reclaim-legacy intent time=' . int(time()) . " host=$host pid=$pid path=$dir");
                }
                if (@before && @after && $before[0] == $after[0] && $before[1] == $after[1] && unlink("$dir/meta.env") && rmdir($dir)) {
                    append_migration_audit(dirname($dir),
                        'reclaim-legacy complete time=' . int(time()) . " host=$host pid=$pid path=$dir");
                    print STDERR diagnostic("WARN: reclaiming confirmed-dead legacy lock $dir pid=$pid host=$host"), "\n";
                    next;
                }
                fail(73, "cannot reclaim confirmed-dead legacy lock $dir");
            }
        } elsif ($kind eq 'corrupt') {
            fail(73, "corrupt compatibility fence $dir");
        }
        if (time() - $start >= $wait) {
            fail(75, legacy_unowned_message($dir)) if $kind eq 'ownerless';
            fail(75, "timed out after ${wait}s waiting for legacy lock $dir ($kind)");
        }
        sleep $POLL;
    }
}
sub reaper {
    my ($owner_fd, $barrier_fd, $lock_path, $fence_dir, $expected, $ack) = @_;
    POSIX::close($owner_fd);
    open(STDIN, '<', '/dev/null') or _exit(1);
    open(STDOUT, '>', '/dev/null') or _exit(1);
    open(STDERR, '>', '/dev/null') or _exit(1);
    my $fd_dir;
    opendir($fd_dir, '/dev/fd') || opendir($fd_dir, '/proc/self/fd') || _exit(1);
    my @fds = grep { /^\d+$/ } readdir($fd_dir);
    closedir($fd_dir);
    for my $n (@fds) {
        POSIX::close($n) if $n >= 3 && $n != fileno($ack) && $n != $barrier_fd;
    }
    sysopen(my $independent, $lock_path, O_RDWR) or _exit(1);
    unless (same_inode($independent, $lock_path)) { write_all($ack, "FAIL\n"); close $ack; _exit(1); }
    write_all($ack, "READY\n");
    close $ack;
    flock($independent, LOCK_EX) or _exit(1);
    remove_fence_if_token($fence_dir, $expected);
    close $independent;
    _exit(0);
}
sub start_reaper {
    my ($owner_fd, $barrier_fd, $lock_path, $fence_dir, $expected) = @_;
    pipe(my $read_ack, my $write_ack) or return 0;
    my $pid = fork();
    unless (defined $pid) { close $read_ack; close $write_ack; return 0; }
    if ($pid == 0) {
        close $read_ack;
        reaper($owner_fd, $barrier_fd, $lock_path, $fence_dir, $expected, $write_ack);
    }
    close $write_ack;
    my $ack = eval {
        local $SIG{ALRM} = sub { die 'reaper startup timed out' };
        alarm 5;
        my $line = <$read_ack>;
        alarm 0;
        $line;
    };
    alarm 0;
    close $read_ack;
    unless (defined($ack) && $ack eq "READY\n") {
        kill 9, $pid;
        waitpid($pid, 0);
        return 0;
    }
    return 1;
}
sub preflight_root {
    my ($dir) = @_;
    fail(70, 'lock parent must be a real directory') unless -d $dir && !-l $dir;
    my $identity = root_identity($dir);
    my $current = machine_identity();
    my $guard = open_guard($dir, LOCK_EX);
    ensure_binding($dir, $current);
    probe_conformance($dir, $guard);
    # The probe must release A's lock for B to acquire. Recheck the binding
    # under the guard after this gap, before permitting a barrier open.
    $guard = open_guard($dir, LOCK_SH);
    ensure_binding($dir, $current);
    stable_barrier($dir);
    fail(72, "state root changed during preflight: $dir")
        unless root_identity($dir) eq $identity;
    close $guard;
    return $identity;
}
sub preflight_command {
    my ($target) = @_;
    fail(70, 'usage: preflight TARGET') unless @_ == 1;
    $target = path_arg($target);
    print preflight_root(dirname($target)), "\n";
}
sub barrier_command {
    my ($fd, $wait, $target, $identity) = @_;
    fail(70, 'usage: barrier FD WAIT TARGET ROOT_ID') unless @_ == 4;
    fail(70, 'invalid inherited descriptor') unless $fd =~ /^[3-9]$/;
    fail(70, 'invalid lock wait') unless $wait =~ /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/ && $wait <= 86400;
    fail(70, 'invalid state root identity') unless $identity =~ /^[0-9]+:[0-9]+$/;
    $target = path_arg($target);
    my $dir = dirname($target);
    fail(72, "state root changed before reset barrier: $dir")
        unless root_identity($dir) eq $identity;
    my $path = stable_barrier($dir);
    open(my $barrier, "<&=$fd") or fail(70, 'inherited reset barrier descriptor is not open');
    fail(72, "reset barrier inode changed: $path") unless same_inode($barrier, $path);
    my $start = time();
    while (!flock($barrier, LOCK_SH|LOCK_NB)) {
        fail(71, "flock unsupported on $path: $!") unless $! == EWOULDBLOCK || $! == EAGAIN;
        fail(75, "timed out after $wait seconds waiting for reset barrier $path") if time() - $start >= $wait;
        sleep $POLL;
    }
    fail(72, "reset barrier inode changed: $path") unless same_inode($barrier, $path);
    my $current = machine_identity();
    fail(74, 'host binding changed before reset barrier acquisition')
        unless read_binding("$dir/$BINDING", $current) eq $current;
    stable_state_file("$target.flock");
    fail(72, "reset barrier inode changed: $path") unless same_inode($barrier, $path);
    fail(72, "state root changed before stable lock open: $dir")
        unless root_identity($dir) eq $identity;
}
sub lock_command {
    my ($fd, $barrier_fd, $wait, $target, $shell_pid, $identity) = @_;
    fail(70, 'usage: lock FD BARRIER_FD WAIT TARGET SHELLPID ROOT_ID') unless @_ == 6;
    fail(70, 'invalid inherited descriptor') unless $fd =~ /^[3-9]$/ && $barrier_fd =~ /^[3-9]$/ && $fd ne $barrier_fd;
    fail(70, 'invalid lock wait') unless $wait =~ /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/ && $wait <= 86400;
    fail(70, 'invalid shell pid') unless $shell_pid =~ /^[1-9][0-9]*$/;
    fail(70, 'invalid state root identity') unless $identity =~ /^[0-9]+:[0-9]+$/;
    $target = path_arg($target);
    my $dir = dirname($target);
    fail(70, 'lock parent must be a real directory') unless -d $dir && !-l $dir;
    fail(72, "state root changed before state lock acquisition: $dir")
        unless root_identity($dir) eq $identity;
    my $lock_path = "$target.flock";
    my $fence_dir = "$target.lockdir";
    open(my $barrier, "<&=$barrier_fd") or fail(70, 'inherited reset barrier descriptor is not open');
    my $barrier_path = stable_barrier($dir);
    fail(72, "reset barrier inode changed: $barrier_path")
        unless same_inode($barrier, $barrier_path) && flock($barrier, LOCK_SH|LOCK_NB);
    my $current = machine_identity();
    fail(74, 'host binding changed while holding reset barrier')
        unless read_binding("$dir/$BINDING", $current) eq $current;
    open(my $owner, "+<&=$fd") or fail(70, 'inherited lock descriptor is not open');
    fail(72, "lock file replaced or unsafe: $lock_path") unless same_inode($owner, $lock_path);
    my $start = time();
    while (!flock($owner, LOCK_EX|LOCK_NB)) {
        fail(71, "flock unsupported on $lock_path: $!") unless $! == EWOULDBLOCK || $! == EAGAIN;
        if (time() - $start >= $wait) {
            fail(75, "timed out after ${wait}s waiting for $target; live holder: " . owner_record($owner));
        }
        sleep $POLL;
    }
    fail(72, "lock file replaced during acquisition: $lock_path") unless same_inode($owner, $lock_path);
    fail(72, "state root changed during state lock acquisition: $dir")
        unless root_identity($dir) eq $identity;
    my $new_token = token();
    classify_and_fence($fence_dir, $wait, $start, $new_token);
    my $record = 'v=2 host=' . hostname() . " pid=$shell_pid ppid=" . getppid() . ' started=' . int(time()) . " token=$new_token label=" . basename($target) . " boot=-\n";
    truncate($owner, 0) or fail(73, "cannot write lock owner record: $!");
    sysseek($owner, 0, 0) == 0 && write_all($owner, $record) or fail(73, 'cannot write lock owner record');
    unless (start_reaper($fd, $barrier_fd, $lock_path, $fence_dir, $new_token)) {
        remove_fence_if_token($fence_dir, $new_token);
        fail(73, "cannot start compatibility-fence drain reaper for $target");
    }
    fail(72, "state root or stable lock changed before protected work: $dir")
        unless root_identity($dir) eq $identity
            && same_inode($barrier, $barrier_path)
            && same_inode($owner, $lock_path);
    exit 0;
}
sub adopt_command {
    my ($path, $confirm) = @_;
    fail(70, 'usage: adopt-legacy LOCKDIR --confirm') unless @_ == 2 && $confirm eq '--confirm';
    $path = path_arg($path);
    fail(70, 'legacy lock path must end in .lockdir') unless $path =~ /\.lockdir\z/;
    my $target = substr($path, 0, -8);
    my $dir = dirname($target);
    preflight_root($dir);
    my $barrier_path = stable_barrier($dir);
    sysopen(my $barrier, $barrier_path, O_RDONLY) or fail(72, 'cannot open reset barrier');
    fail(72, 'reset barrier inode changed') unless same_inode($barrier, $barrier_path);
    flock($barrier, LOCK_SH) or fail(71, 'cannot acquire shared reset barrier');
    fail(72, 'reset barrier inode changed') unless same_inode($barrier, $barrier_path);
    my $current = machine_identity();
    fail(74, 'host binding changed before legacy adoption')
        unless read_binding("$dir/$BINDING", $current) eq $current;
    my $lock_path = "$target.flock";
    stable_state_file($lock_path);
    sysopen(my $fh, $lock_path, O_RDWR) or fail(70, 'cannot open stable lock file');
    fail(72, 'stable lock file inode changed') unless same_inode($fh, $lock_path);
    flock($fh, LOCK_EX) or fail(71, 'cannot lock stable lock file');
    my ($kind) = fence_info($path);
    fail(73, "legacy adoption requires an empty ownerless lock directory: $path") unless $kind eq 'ownerless';
    my $audit = "$dir/.lock_migration.log";
    sysopen(my $log, $audit, O_WRONLY|O_CREAT|O_APPEND, 0600) or fail(73, 'cannot open migration audit log');
    write_all($log, 'adopt-legacy time=' . int(time()) . ' host=' . hostname() . " path=$path\n") && close($log) or fail(73, 'cannot append migration audit');
    rmdir($path) or fail(73, 'cannot remove confirmed ownerless legacy directory');
    close $fh;
    close $barrier;
    exit 0;
}
sub rebind_command {
    my ($dir, $old, $new, $confirm) = @_;
    fail(70, 'usage: rebind-host DIR OLD_IDENTITY NEW_IDENTITY --confirm') unless @_ == 4 && $confirm eq '--confirm';
    $dir = path_arg($dir);
    fail(70, 'invalid machine identities')
        unless ($old =~ /^hostname-v1:[0-9a-f]{2,128}\z/ || valid_identity($old))
            && valid_identity($new) && $old ne $new;
    my $root_id = root_identity($dir);
    fail(74, 'new identity is not this machine') unless $new eq machine_identity();
    my $guard = open_guard($dir, LOCK_EX);
    probe_conformance($dir, $guard);
    $guard = open_guard($dir, LOCK_EX);
    my $path = "$dir/$BINDING";
    my $bound = read_binding($path, $new, 1);
    fail(74, 'expected old machine identity does not match') unless $bound eq $old || $bound eq $new;
    my @binding_inode = lstat($path);
    fail(74, 'host binding unsafe') unless @binding_inode && S_ISREG($binding_inode[2]) && $binding_inode[3] <= 1;
    opendir(my $dh, $dir) or fail(74, 'cannot inspect state directory');
    my @entries = readdir($dh);
    closedir $dh;
    my %eligible = map { $_ => 1 } qw(
        .dorado.lock.lockdir .blastreport.lock.lockdir .blastreport_sup.lock.lockdir
        .qced_reads.lock.lockdir .otu_size_streak.lock.lockdir
        .sup_basecall_cache.lock.lockdir .done_pod5.lock.lockdir
        .report_history.lock.lockdir .report_live_publish.lock.lockdir
    );
    my @fences;
    my @lock_names;
    for my $name (@entries) {
        next if $name eq '.' || $name eq '..';
        fail(74, "cannot rebind while compatibility fence exists: $name")
            if $name =~ /\.lockdir\z/ && !$eligible{$name};
        if ($eligible{$name}) {
            my $target = substr($name, 0, -length('.lockdir'));
            push @fences, [$name, $target];
        }
        push @lock_names, $name if $name =~ /\.flock\z/ && $name ne $BARRIER;
    }
    # Keep every independent OFD locked until the fence and binding work is done.
    # The barrier is first, then the stable locks; no later step waits.
    my $barrier_path = stable_barrier($dir);
    sysopen(my $barrier, $barrier_path, O_RDWR) or fail(74, 'cannot inspect reset barrier');
    fail(74, 'reset barrier unsafe') unless same_inode($barrier, $barrier_path);
    flock($barrier, LOCK_EX|LOCK_NB) or fail(74, 'cannot rebind while kernel lock active: reset barrier');
    fail(74, 'reset barrier changed') unless same_inode($barrier, $barrier_path);
    my @held = ($barrier);
    my %locked;
    for my $name (@lock_names) {
        my $lock_path = "$dir/$name";
        sysopen(my $fh, $lock_path, O_RDWR) or fail(74, "cannot inspect stable lock $name");
        fail(74, "stable lock file unsafe: $name") unless same_inode($fh, $lock_path);
        flock($fh, LOCK_EX|LOCK_NB) or fail(74, "cannot rebind while kernel lock active: $name");
        fail(74, "stable lock file changed: $name") unless same_inode($fh, $lock_path);
        push @held, $fh;
        $locked{$name} = $fh;
    }
    my @recover;
    for my $fence (@fences) {
        my ($name, $target) = @$fence;
        fail(74, "compatibility fence changed: $name")
            unless $eligible{$name} && $target eq substr($name, 0, -length('.lockdir'));
        fail(74, "stable lock missing for compatibility fence: $name")
            unless exists $locked{"$target.flock"};
        my $fence_path = "$dir/$name";
        my ($kind, $token) = fence_info($fence_path);
        fail(74, "cannot rebind while compatibility fence exists: $name") unless $kind eq 'v2';
        my @st = lstat($fence_path);
        my @owner = lstat("$fence_path/v2owner");
        fail(74, "compatibility fence changed: $name")
            unless @st && S_ISDIR($st[2]) && @owner && S_ISREG($owner[2]) && $owner[3] <= 1;
        push @recover, [$name, $target, $token, $st[0], $st[1], $owner[0], $owner[1]];
    }
    if ($bound eq $new && !@recover) {
        close $_ for @held;
        close $guard;
        exit 0;
    }
    my ($out, $temp);
    if ($bound ne $new) {
        ($out, $temp) = temp_file($dir, '.rtbioscan_lock_host_v1.new');
        unless (write_all($out, binding_record($new)) && close($out)) {
            unlink($temp);
            fail(74, 'cannot write new host binding');
        }
    }
    my $audit = "$dir/.lock_migration.log";
    my $log;
    my @audit_before = lstat($audit);
    fail(74, 'migration audit unsafe') if @audit_before && !S_ISREG($audit_before[2]);
    unless (sysopen($log, $audit, O_WRONLY|O_CREAT|O_APPEND, 0600)) {
        unlink($temp) if defined $temp;
        fail(74, 'cannot open migration audit log');
    }
    fail(74, 'migration audit inode changed') unless same_inode($log, $audit);
    if (defined $temp && !write_all($log, 'rebind-host intent time=' . int(time()) . " old=$bound new=$new host=" . host_hex() . "\n")) {
        unlink($temp);
        fail(74, 'cannot write migration audit');
    }
    for my $fence (@recover) {
        my ($name, $target, $token, $dev, $ino, $owner_dev, $owner_ino) = @$fence;
        my $fence_path = "$dir/$name";
        my @st = lstat($fence_path);
        my @owner = lstat("$fence_path/v2owner");
        my ($kind, $current_token) = fence_info($fence_path);
        fail(74, "compatibility fence changed: $name")
            unless @st && S_ISDIR($st[2]) && $st[0] == $dev && $st[1] == $ino
                && @owner && S_ISREG($owner[2]) && $owner[3] <= 1
                && $owner[0] == $owner_dev && $owner[1] == $owner_ino
                && $eligible{$name} && $target eq substr($name, 0, -length('.lockdir'))
                && $kind eq 'v2' && $current_token eq $token
                && root_identity($dir) eq $root_id
                && same_inode($locked{"$target.flock"}, "$dir/$target.flock")
                && same_inode($barrier, $barrier_path);
        write_all($log, 'rebind-host fence-recovery intent time=' . int(time()) . " namespace=$target\n")
            or fail(74, 'cannot audit fence recovery intent');
        remove_fence_if_token($fence_path, $token, $dev, $ino, $owner_dev, $owner_ino)
            or fail(74, "cannot remove exact stale compatibility fence: $name");
        write_all($log, 'rebind-host fence-recovery complete time=' . int(time()) . " namespace=$target\n")
            or fail(74, 'cannot audit fence recovery completion');
    }
    fail(74, 'state root changed before host binding publication') unless root_identity($dir) eq $root_id;
    fail(74, 'reset barrier changed before host binding publication') unless same_inode($barrier, $barrier_path);
    for my $name (keys %locked) {
        fail(74, "stable lock changed before host binding publication: $name")
            unless same_inode($locked{$name}, "$dir/$name");
    }
    opendir(my $final_dh, $dir) or fail(74, 'cannot reinspect state directory');
    my @final_fences = grep { $eligible{$_} } readdir($final_dh);
    closedir $final_dh;
    fail(74, 'compatibility fence appeared before host binding publication') if @final_fences;
    my @binding_final = lstat($path);
    fail(74, 'host binding changed before publication')
        unless @binding_final && S_ISREG($binding_final[2]) && $binding_final[3] <= 1
            && $binding_final[0] == $binding_inode[0] && $binding_final[1] == $binding_inode[1]
            && read_binding($path, $new, 1) eq $bound;
    if (defined $temp && !rename($temp, $path)) {
        unlink($temp);
        fail(74, 'cannot replace host binding atomically');
    }
    if (defined $temp) {
        write_all($log, 'rebind-host complete time=' . int(time()) . " old=$bound new=$new host=" . host_hex() . "\n") or fail(74, 'cannot complete migration audit');
    }
    close $log;
    close $_ for @held;
    close $guard;
    exit 0;
}

my $command = shift @ARGV // '';
if ($command eq 'preflight') { preflight_command(@ARGV); }
elsif ($command eq 'barrier') { barrier_command(@ARGV); }
elsif ($command eq 'lock') { lock_command(@ARGV); }
elsif ($command eq 'adopt-legacy') { adopt_command(@ARGV); }
elsif ($command eq 'rebind-host') { rebind_command(@ARGV); }
else { fail(70, 'usage: fd_lock.pl preflight|barrier|lock|adopt-legacy|rebind-host ...'); }
